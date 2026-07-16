#!/usr/bin/env python3
"""Small, auditable Codex + Claude collaboration orchestrator."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time


class StageTimeoutError(RuntimeError):
    pass


def run(
    cmd: list[str],
    *,
    cwd: Path,
    stdin: str | None = None,
    stream: bool = False,
    timeout: float | None = None,
) -> str:
    if stream:
        # Tee stdout to the console while collecting it; stderr inherits the
        # terminal so warnings appear live. Writing stdin up front is safe
        # because prompts are far smaller than the pipe buffer.
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=timeout is not None,
        )
        timed_out = threading.Event()
        timeout_lock = threading.Lock()

        def terminate() -> None:
            with timeout_lock:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                        timed_out.set()
                    except ProcessLookupError:
                        pass

        timer = threading.Timer(timeout, terminate) if timeout is not None else None
        if timer:
            timer.daemon = True
            timer.start()
        try:
            if stdin is not None:
                try:
                    process.stdin.write(stdin)
                    process.stdin.close()
                except BrokenPipeError:
                    if not timed_out.is_set():
                        raise
            lines = []
            try:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    lines.append(line)
            finally:
                process.stdout.close()
                process.wait()
        finally:
            if timer:
                timer.cancel()
        with timeout_lock:
            did_time_out = timed_out.is_set()
        if did_time_out:
            raise TimeoutError
        if process.returncode:
            raise RuntimeError(f"Command failed ({process.returncode}): {shlex.join(cmd)}")
        return "".join(lines)
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            input=stdin,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError from None
    if result.returncode:
        detail = (result.stderr or result.stdout or "no output").strip()
        raise RuntimeError(f"Command failed ({result.returncode}): {shlex.join(cmd)}\n{detail}")
    return result.stdout or ""


def git(repo: Path, *args: str) -> str:
    return run(["git", *args], cwd=repo).strip()


def write(path: Path, content: str) -> None:
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def extract_result_and_usage(stdout: str) -> tuple[str, dict | None]:
    """Extract Claude's response and accounting metadata from JSON output."""
    try:
        payload = json.loads(stdout)
        result = payload["result"]
        usage = payload["usage"]
        if not isinstance(result, str) or not isinstance(usage, dict):
            raise TypeError
        return result, {
            **usage,
            "total_cost_usd": payload.get("total_cost_usd"),
            "num_turns": payload.get("num_turns"),
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        return stdout, None


def format_duration(seconds: float) -> str:
    """Render elapsed seconds as M:SS, or H:MM:SS once it reaches an hour."""
    total = max(int(round(seconds)), 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def build_summary(durations: dict[str, float], usage: dict[str, dict | None]) -> str:
    """Render a plain-text, aligned per-stage duration/cost table with a total row."""
    if not durations:
        return "No stages executed."
    rows = []
    total_seconds = 0.0
    total_cost = 0.0
    have_cost = False
    for name, seconds in durations.items():
        total_seconds += seconds
        cost = (usage.get(name) or {}).get("total_cost_usd")
        cost_text = ""
        if isinstance(cost, (int, float)):
            total_cost += cost
            have_cost = True
            cost_text = f"${cost:.4f}"
        rows.append((name, format_duration(seconds), cost_text))
    rows.append((
        "Total",
        format_duration(total_seconds),
        f"${total_cost:.4f}" if have_cost else "",
    ))
    name_width = max(len(row[0]) for row in rows)
    duration_width = max(len(row[1]) for row in rows)
    lines = [
        f"{name:<{name_width}}  {duration:>{duration_width}}  {cost}".rstrip()
        for name, duration, cost in rows
    ]
    lines.insert(-1, "-" * max(len(line) for line in lines))
    return "\n".join(lines)


def complete_diff(repo: Path, baseline: str) -> str:
    """Return a patch containing tracked changes and non-ignored untracked files."""
    chunks = [git(repo, "diff", "--binary", "--no-ext-diff", baseline, "--")]
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    for relative in filter(None, untracked.split("\0")):
        result = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--", "/dev/null", relative],
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # git diff --no-index returns 1 when it successfully finds a difference.
        if result.returncode not in (0, 1):
            raise RuntimeError(f"Could not capture untracked file {relative}: {result.stderr.strip()}")
        chunks.append(result.stdout)
    return "\n".join(chunk.rstrip() for chunk in chunks if chunk).rstrip()


def list_mir_nodes(skills_dir: Path) -> list[str]:
    if not skills_dir.is_dir():
        return []
    return sorted(
        path.name for path in skills_dir.iterdir() if (path / "SKILL.md").is_file()
    )


def resolve_mir_skill(skills_dir: Path, node: str) -> Path:
    available_nodes = list_mir_nodes(skills_dir)
    skill_file = skills_dir / node / "SKILL.md"
    if node in available_nodes:
        return skill_file
    available = ", ".join(available_nodes) or "(none found)"
    raise RuntimeError(
        f"Unknown mirror node {node!r} — no SKILL.md at {skills_dir / node}. "
        f"Available nodes in {skills_dir}: {available}"
    )


def mirror_review_enabled(
    hermes: bool, mir_nodes: str | list[str] | None, mir_backend: str | None
) -> bool:
    return hermes or bool(mir_nodes) or mir_backend is not None


def mirror_review_invocation(
    backend: str,
    *,
    repo: Path,
    output: Path,
    node: str | None = None,
    hermes_model: str | None = None,
    claude_model: str | None = None,
    codex_model: str | None = None,
    max_budget_usd: float | None = None,
) -> tuple[list[str], str | None, bool]:
    """Build the command, prompt flag, and JSON setting for a mirror reviewer."""
    if backend == "hermes":
        cmd = ["hermes", "-t", ""]
        if node:
            cmd += ["--skills", node]
        if hermes_model:
            cmd += ["-m", hermes_model]
        return cmd, "-z", False
    if backend == "claude":
        cmd = [
            "claude", "--print", "--no-session-persistence", "--permission-mode", "plan",
            "--tools", "", "--output-format", "json",
        ]
        if claude_model:
            cmd += ["--model", claude_model]
        if max_budget_usd is not None:
            cmd += ["--max-budget-usd", str(max_budget_usd)]
        return cmd, None, True
    if backend == "codex":
        cmd = [
            "codex", "exec", "-C", str(repo), "--sandbox", "read-only",
            "--color", "never", "--output-last-message", str(output), "-",
        ]
        if codex_model:
            cmd[2:2] = ["--model", codex_model]
        return cmd, None, False
    raise ValueError(f"Unsupported mirror backend: {backend}")


def invoke(
    name: str,
    cmd: list[str],
    prompt: str,
    output: Path,
    repo: Path,
    dry_run: bool,
    prompt_flag: str | None = None,
    timeout: float | None = None,
    parse_json: bool = False,
) -> dict | None:
    print(f"\n==> {name}", flush=True)
    write(output.with_suffix(output.suffix + ".prompt.md"), prompt)
    if dry_run:
        print(shlex.join(cmd))
        write(output, f"DRY RUN: {shlex.join(cmd)}")
        return None
    try:
        if prompt_flag is not None:
            prompt_args = [prompt] if prompt_flag == "" else [prompt_flag, prompt]
            result = run(cmd + prompt_args, cwd=repo, stream=True, timeout=timeout)
        else:
            result = run(cmd, cwd=repo, stdin=prompt, stream=True, timeout=timeout)
    except TimeoutError:
        raise StageTimeoutError(f"Stage timed out after {timeout:g} seconds: {name}") from None
    usage = None
    if parse_json:
        result, usage = extract_result_and_usage(result)
    # Codex writes its final message itself; Claude prints to stdout.
    if not output.exists():
        write(output, result)
    elif result.strip():
        write(output.with_suffix(output.suffix + ".stdout.log"), result)
    return usage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Have Claude plan/review and Codex implement/fix a task in a Git repository."
    )
    parser.add_argument("task", help="The concrete engineering task to complete")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Target Git repository")
    parser.add_argument("--codex-model", help="Optional Codex model override")
    parser.add_argument("--claude-model", help="Optional Claude model override")
    parser.add_argument("--hermes", action="store_true",
                        help="Add an independent second review from the Hermes agent (tools disabled)")
    parser.add_argument("--hermes-model", help="Optional Hermes model override")
    parser.add_argument(
        "--mir",
        action="append",
        help=("Mirror-node lens for the independent second review "
              "(a subdirectory of --mir-skills-dir containing SKILL.md); "
              "repeatable — each node reviews the same diff independently"),
    )
    parser.add_argument(
        "--mir-backend",
        choices=["hermes", "claude", "codex"],
        default=None,
        help=("Backend for the independent second review (default: hermes); "
              "passing this option enables the review"),
    )
    parser.add_argument(
        "--mir-skills-dir",
        type=Path,
        default=Path("~/.hermes/skills/mirror-nodes"),
        help="Directory of mirror-node skill folders, each containing SKILL.md",
    )
    parser.add_argument("--max-budget-usd", type=float, help="Budget for each Claude invocation")
    parser.add_argument("--stage-timeout-seconds", type=float,
                        help="Wall-clock timeout for each agent invocation (default: none)")
    parser.add_argument("--allow-dirty", action="store_true", help="Run despite existing changes")
    parser.add_argument("--skip-review-fix", action="store_true", help="Stop after Claude's review")
    parser.add_argument("--dry-run", action="store_true", help="Print commands and prompts only")
    args = parser.parse_args()
    if args.stage_timeout_seconds is not None and (
        not math.isfinite(args.stage_timeout_seconds) or args.stage_timeout_seconds <= 0
    ):
        parser.error("--stage-timeout-seconds must be greater than zero")
    return args


def main() -> int:
    args = parse_args()
    mir_backend = args.mir_backend or "hermes"
    mir_nodes: list[str] = args.mir or []
    mir_enabled = mirror_review_enabled(args.hermes, mir_nodes, args.mir_backend)
    mir_skills_dir = args.mir_skills_dir.expanduser().resolve()
    mir_skill_texts: dict[str, str | None] = {}
    for node in mir_nodes:
        skill_file = resolve_mir_skill(mir_skills_dir, node)
        mir_skill_texts[node] = (
            skill_file.read_text(encoding="utf-8") if mir_backend != "hermes" else None
        )

    executables = ["git", "codex", "claude"]
    if mir_enabled and mir_backend == "hermes":
        executables.append("hermes")
        print(
            "warning: hermes >=0.18.2 ignores `-t \"\"` (and --skills force-enables its "
            "declared toolsets), so hermes mirror reviewers run with FULL tool access — "
            "including file writes and terminal — despite the prompt telling them otherwise. "
            "Verified empirically 2026-07-15: reviewers overwrote each other's output files. "
            "Use --mir-backend claude or codex for an actually-sandboxed review until fixed.",
            file=sys.stderr,
        )
    for executable in executables:
        if not shutil.which(executable):
            raise RuntimeError(f"Required executable not found: {executable}")
    repo = args.repo.expanduser().resolve()
    if not repo.is_dir():
        raise RuntimeError(f"Not a directory: {repo}")
    repo = Path(git(repo, "rev-parse", "--show-toplevel"))

    status = git(repo, "status", "--porcelain")
    if status and not args.allow_dirty:
        raise RuntimeError("Repository has uncommitted changes; commit/stash them or use --allow-dirty.")
    if status:
        print(
            "warning: dirty tree — pre-existing changes will appear in the review and final diff",
            file=sys.stderr,
        )

    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    git_dir_value = git(repo, "rev-parse", "--path-format=absolute", "--git-dir")
    runs_dir = Path(git_dir_value) / "agent-collab" / "runs"
    artifacts = runs_dir / run_id
    attempt = 1
    while True:
        try:
            artifacts.mkdir(parents=True)
            break
        except FileExistsError:
            attempt += 1
            artifacts = runs_dir / f"{run_id}-{attempt}"
    baseline = git(repo, "rev-parse", "HEAD")
    metadata = {
        "task": args.task,
        "repo": str(repo),
        "baseline": baseline,
        "hermes": args.hermes,
        "mir_enabled": mir_enabled,
        "mir_nodes": mir_nodes,
        "mir_backend": mir_backend if mir_enabled else None,
        "mir_skills_dir": str(mir_skills_dir) if mir_enabled else None,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "usage": {},
        "durations": {},
    }
    run_metadata = artifacts / "run.json"
    write(run_metadata, json.dumps(metadata, indent=2))

    def record_stage(stage: str, usage: dict | None, duration: float) -> None:
        metadata["usage"][stage] = usage
        metadata["durations"][stage] = duration
        write(run_metadata, json.dumps(metadata, indent=2))

    plan = artifacts / "01-plan.md"
    codex_result = artifacts / "02-implementation.md"
    review = artifacts / "03-review.md"
    fix_result = artifacts / "04-fixes.md"

    claude_base = [
        "claude", "--print", "--no-session-persistence", "--permission-mode", "plan",
        "--tools", "Read,Grep,Glob,Bash", "--output-format", "json",
    ]
    if args.claude_model:
        claude_base += ["--model", args.claude_model]
    if args.max_budget_usd is not None:
        claude_base += ["--max-budget-usd", str(args.max_budget_usd)]

    started = time.monotonic()
    usage = invoke(
        "Claude: plan",
        claude_base,
        f"""You are the planning engineer. Analyze this repository and produce a concise, implementation-ready plan for the task below. Do not edit files. Include affected files, important constraints, tests, and risks.\n\nTASK:\n{args.task}""",
        plan,
        repo,
        args.dry_run,
        timeout=args.stage_timeout_seconds,
        parse_json=True,
    )
    record_stage("Claude: plan", usage, time.monotonic() - started)

    codex_cmd = [
        "codex", "exec", "-C", str(repo), "--sandbox", "workspace-write",
        "--color", "never", "--output-last-message", str(codex_result), "-",
    ]
    if args.codex_model:
        codex_cmd[2:2] = ["--model", args.codex_model]
    started = time.monotonic()
    usage = invoke(
        "Codex: implement",
        codex_cmd,
        f"""Implement the requested task in this repository. Read the plan at {plan}. Inspect the code yourself, keep changes scoped, and run relevant tests. Do not commit, push, or discard pre-existing changes.\n\nTASK:\n{args.task}""",
        codex_result,
        repo,
        args.dry_run,
        timeout=args.stage_timeout_seconds,
    )
    record_stage("Codex: implement", usage, time.monotonic() - started)

    started = time.monotonic()
    usage = invoke(
        "Claude: review",
        claude_base,
        f"""Act as a strict code reviewer. Review all working-tree changes relative to baseline commit {baseline} for the task below. Use git status, git diff, and inspect every relevant untracked file as well as tracked changes. Do not edit anything. Report only actionable correctness, security, regression, or missing-test findings. For every finding give severity, file/location, evidence, and a concrete fix. If there are none, say exactly: NO ACTIONABLE FINDINGS.\n\nTASK:\n{args.task}""",
        review,
        repo,
        args.dry_run,
        timeout=args.stage_timeout_seconds,
        parse_json=True,
    )
    record_stage("Claude: review", usage, time.monotonic() - started)

    reviews = [review]
    if mir_enabled:
        # The mirror reviewers never see Claude's review or use repository
        # tools, so every backend receives the diff inside its prompt. Each
        # node reviews the same diff independently of the others.
        diff_text = complete_diff(repo, baseline)
        if len(diff_text) > 120_000:
            diff_text = diff_text[:120_000] + "\n[diff truncated for length]"
        for node in mir_nodes or [None]:
            skill_text = mir_skill_texts.get(node) if node else None
            skill_prefix = f"{skill_text}\n\n---\n\n" if skill_text else ""
            mir_prompt = f"""{skill_prefix}Act as a strict, independent code reviewer. Another reviewer is assessing the same change separately; judge only from what is below. You have no tools this session — the complete working-tree diff is included. Report only actionable correctness, security, regression, or missing-test findings. For every finding give severity, file/location, evidence from the diff, and a concrete fix. If there are none, say exactly: NO ACTIONABLE FINDINGS.\n\nTASK:\n{args.task}\n\nDIFF (relative to baseline {baseline}):\n{diff_text or "(no changes detected)"}"""
            node_tag = f", {node}" if node else ""
            stage_label = f"Mir ({mir_backend}{node_tag}): independent review"
            mir_review = artifacts / (f"03b-mir-{node}-review.md" if node else "03b-mir-review.md")

            mir_cmd, prompt_flag, parse_json = mirror_review_invocation(
                mir_backend,
                repo=repo,
                output=mir_review,
                node=node,
                hermes_model=args.hermes_model,
                claude_model=args.claude_model,
                codex_model=args.codex_model,
                max_budget_usd=args.max_budget_usd,
            )

            started = time.monotonic()
            usage = invoke(
                stage_label,
                mir_cmd,
                mir_prompt,
                mir_review,
                repo,
                args.dry_run,
                prompt_flag=prompt_flag,
                timeout=args.stage_timeout_seconds,
                parse_json=parse_json,
            )
            record_stage(stage_label, usage, time.monotonic() - started)
            reviews.append(mir_review)

    if not args.skip_review_fix:
        texts = [] if args.dry_run else [p.read_text(encoding="utf-8") for p in reviews]
        if texts and all("NO ACTIONABLE FINDINGS" in t for t in texts):
            print("\n==> Codex: address review (skipped — all reviews reported no actionable findings)")
        else:
            review_refs = " and ".join(str(p) for p in reviews)
            started = time.monotonic()
            usage = invoke(
                "Codex: address review",
                codex_cmd[:-2] + [str(fix_result), "-"],
                f"""Read every review at {review_refs}. Verify every claim against the repository. Address all valid actionable findings for the task below, ignore unsupported suggestions, and run relevant tests. Do not commit or push. If a review says NO ACTIONABLE FINDINGS, it requires no changes.\n\nTASK:\n{args.task}""",
                fix_result,
                repo,
                args.dry_run,
                timeout=args.stage_timeout_seconds,
            )
            record_stage("Codex: address review", usage, time.monotonic() - started)

    if not args.dry_run:
        write(artifacts / "final.diff", complete_diff(repo, baseline))
        write(artifacts / "final.status", git(repo, "status", "--short"))
    summary_text = build_summary(metadata["durations"], metadata["usage"])
    write(artifacts / "summary.txt", summary_text)
    print(f"\n{summary_text}")
    print(f"\nComplete. Artifacts: {artifacts}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
