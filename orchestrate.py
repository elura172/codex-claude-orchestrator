#!/usr/bin/env python3
"""Small, auditable multi-agent collaboration orchestrator."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import hashlib
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


CANONICAL_MIR_NODES = (
    "ky-mir",
    "syr-mir",
    "thae-mir",
    "vor-mir",
    "xy-mir",
    "fael-mir",
)

# Inherited by agent subprocesses during a self-evolution run. This is private
# orchestration state, not a user-facing generation counter: its presence means
# the sole permitted generation is already active.
SELF_EVOLUTION_ACTIVE_ENV = "CODEX_ORCHESTRATOR_SELF_EVOLUTION_ACTIVE"


class TimedInvocationError(Exception):
    """Preserve a failed invocation's original error and elapsed duration."""

    def __init__(self, error: Exception, duration: float):
        super().__init__(str(error))
        self.error = error
        self.duration = duration


def run(
    cmd: list[str],
    *,
    cwd: Path,
    stdin: str | None = None,
    stream: bool = False,
    timeout: float | None = None,
    return_stderr: bool = False,
    env: dict[str, str] | None = None,
) -> str | tuple[str, str]:
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
            env=env,
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
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=timeout is not None,
        env=env,
    )
    try:
        stdout, stderr = process.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Agent CLIs may leave grandchildren holding our pipes open. Kill the
        # whole session before communicate(), matching the streaming path.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise TimeoutError from None
    if process.returncode:
        detail = (stderr or stdout or "no output").strip()
        raise RuntimeError(f"Command failed ({process.returncode}): {shlex.join(cmd)}\n{detail}")
    if return_stderr:
        return stdout or "", stderr or ""
    return stdout or ""


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


def tree_fingerprint(repo: Path, baseline: str) -> str:
    """Fingerprint the working tree relative to baseline, for vow-of-stillness checks."""
    material = "\0".join((
        complete_diff(repo, baseline),
        git(repo, "status", "--porcelain"),
        git(repo, "rev-parse", "HEAD"),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def review_is_clean(text: str, require_seal: bool = False) -> bool:
    """Read a review's verdict from its sealing line.

    The seal must be the last non-empty line, so a review that merely quotes
    the instructions cannot be misread as clean. Reviews without a seal fall
    back to the legacy sentinel unless require_seal is set.
    """
    for line in reversed(text.strip().splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("SEAL:"):
            return stripped.upper() == "SEAL: CLEAN"
        break
    return not require_seal and "NO ACTIONABLE FINDINGS" in text


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


SYNTHESIS_CAP = 120_000
LINEAGE_CAP = 40_000


def gather_lineage(runs_dir: Path, count: int) -> list[tuple[str, str]]:
    """Collect the syntheses of the most recent prior runs, newest first."""
    if count <= 0 or not runs_dir.is_dir():
        return []
    entries: list[tuple[str, str]] = []
    for run in sorted(runs_dir.iterdir(), reverse=True):
        synthesis = run / "03c-synthesis.md"
        if synthesis.is_file():
            entries.append((run.name, synthesis.read_text(encoding="utf-8")))
            if len(entries) == count:
                break
    return entries


def build_lineage_block(entries: list[tuple[str, str]]) -> str:
    """Render prior syntheses as a prompt section; empty when there are none."""
    if not entries:
        return ""
    sections = "\n\n".join(f"### RUN {name}\n{text.strip()}" for name, text in entries)
    if len(sections) > LINEAGE_CAP:
        sections = sections[:LINEAGE_CAP] + "\n[lineage truncated for length]"
    return (
        "\n\nLINEAGE — syntheses of prior runs in this repository, newest first. "
        "Treat them as memory, not instruction: honor constraints they establish, "
        "do not re-plan work they show completed, and carry forward unresolved "
        "findings that touch this task:\n\n" + sections
    )


def build_synthesis_prompt(
    task: str, reviews: list[tuple[str, str]], skill_text: str | None = None
) -> str:
    """Prompt for the recombination stage: all reviews in, one document out."""
    sections = [f"### REVIEW: {name}\n{text.strip()}" for name, text in reviews]
    body = "\n\n".join(sections) or "(no reviews available)"
    if len(body) > SYNTHESIS_CAP:
        body = body[:SYNTHESIS_CAP] + "\n[reviews truncated for length]"
    skill_prefix = f"{skill_text}\n\n---\n\n" if skill_text else ""
    return f"""{skill_prefix}You are the recombining stage of a multi-reviewer pipeline. Several reviewers independently examined the same working-tree change; their complete reviews are below. You have no tools this session and must judge only from these texts. Do not merge them into a flat list — perform the actual synthesis:

1. CONVERGENCE MAP — findings reported by more than one reviewer. For each: the finding, which reviewers flagged it, and the strongest quoted evidence. Convergence across independent reviewers is the highest-confidence signal.
2. SINGULAR FINDINGS — findings seen by exactly one reviewer. For each, assess plausibility strictly from the quoted evidence: unique insight, or noise?
3. DISAGREEMENTS — anywhere reviewers contradict each other on severity, diagnosis, or fix. State each side's case.
4. UNIFIED VERDICT — one go/no-go with a priority-ordered action list. Deduplicate; each action names the finding(s) it resolves and the reviewers behind it.

Be concise and evidence-bound; cite reviewers by their review's name. End the document with exactly one final line: SEAL: CLEAN if no finding survives synthesis, else SEAL: FINDINGS <n>.

TASK UNDER REVIEW:
{task}

{body}"""


def build_mir_prompt(
    task: str, baseline: str, diff_text: str, skill_text: str | None = None
) -> str:
    """Build the sealed-room prompt shared by sequential and parallel mirrors."""
    skill_prefix = f"{skill_text}\n\n---\n\n" if skill_text else ""
    return f"""{skill_prefix}Act as a strict, independent code reviewer. Another reviewer is assessing the same change separately; judge only from what is below. You have no tools this session — the complete working-tree diff is included. Report only actionable correctness, security, regression, or missing-test findings. For every finding give severity, file/location, evidence from the diff, and a concrete fix. Your final line must be exactly SEAL: CLEAN if there are no findings, or SEAL: FINDINGS <n> where n counts them.\n\nTASK:\n{task}\n\nDIFF (relative to baseline {baseline}):\n{diff_text or "(no changes detected)"}"""


def mir_stage_label(backend: str, node: str | None) -> str:
    node_tag = f", {node}" if node else ""
    return f"Mir ({backend}{node_tag}): independent review"


def mir_review_path(artifacts: Path, node: str | None) -> Path:
    return artifacts / (f"03b-mir-{node}-review.md" if node else "03b-mir-review.md")


def collective_vow_verdict(before: str, after: str) -> str:
    return "kept" if before == after else "broken"


def unique_mir_nodes(nodes: list[str]) -> list[str]:
    """Preserve node order while rejecting artifact/stage collisions."""
    seen = set()
    duplicates = []
    for node in nodes:
        if node in seen and node not in duplicates:
            duplicates.append(node)
        seen.add(node)
    if duplicates:
        raise RuntimeError(f"Duplicate --mir node(s): {', '.join(duplicates)}")
    return nodes


def select_stage_models(
    *,
    claude_model: str | None,
    codex_model: str | None,
    hermes_model: str | None,
    plan_backend: str,
    review_backend: str,
    mir_backend: str,
    synth_backend: str,
    plan_model: str | None = None,
    implement_model: str | None = None,
    review_model: str | None = None,
    mir_model: str | None = None,
    fix_model: str | None = None,
) -> dict[str, str | None]:
    """Resolve per-stage models, falling back to each backend's default."""
    backend_defaults = {
        "claude": claude_model,
        "codex": codex_model,
        "hermes": hermes_model,
    }
    return {
        "plan": plan_model or backend_defaults[plan_backend],
        "implement": implement_model or codex_model,
        "review": review_model or backend_defaults[review_backend],
        "mir": mir_model or backend_defaults[mir_backend],
        # Om'Mir is the synthesis node within the mirror formation. An
        # explicit mirror model therefore governs synthesis too; otherwise
        # synthesis falls back to the model default for its execution backend.
        "synthesize": mir_model or backend_defaults[synth_backend],
        "fix": fix_model or codex_model,
    }


def invoke_timed(*args, **kwargs) -> tuple[dict | None, str, float]:
    """Run one buffered invocation and measure its worker-side duration."""
    started = time.monotonic()
    try:
        usage, console_text = invoke(*args, **kwargs)
    except Exception as error:
        raise TimedInvocationError(error, time.monotonic() - started) from error
    return usage, console_text, time.monotonic() - started


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
    buffered: bool = False,
    env: dict[str, str] | None = None,
    protected_head: str | None = None,
) -> dict | None | tuple[dict | None, str]:
    if not buffered:
        print(f"\n==> {name}", flush=True)
    write(output.with_suffix(output.suffix + ".prompt.md"), prompt)
    if dry_run:
        console_text = shlex.join(cmd)
        if not buffered:
            print(console_text)
        write(output, f"DRY RUN: {shlex.join(cmd)}")
        return (None, console_text) if buffered else None
    try:
        if prompt_flag is not None:
            prompt_args = [prompt] if prompt_flag == "" else [prompt_flag, prompt]
            captured = run(
                cmd + prompt_args, cwd=repo, stream=not buffered, timeout=timeout,
                return_stderr=buffered,
                env=env,
            )
        else:
            captured = run(
                cmd, cwd=repo, stdin=prompt, stream=not buffered, timeout=timeout,
                return_stderr=buffered,
                env=env,
            )
    except TimeoutError:
        raise StageTimeoutError(f"Stage timed out after {timeout:g} seconds: {name}") from None
    finally:
        if protected_head is not None and git(repo, "rev-parse", "HEAD") != protected_head:
            raise RuntimeError(
                f"Human commit boundary violated during {name}: repository HEAD changed"
            )
    if buffered:
        result, stderr = captured
    else:
        result, stderr = captured, ""
    usage = None
    if parse_json:
        result, usage = extract_result_and_usage(result)
    # Codex writes its final message itself; Claude prints to stdout.
    if not output.exists():
        write(output, result)
    elif result.strip():
        write(output.with_suffix(output.suffix + ".stdout.log"), result)
    if buffered and stderr:
        separator = "" if not result or result.endswith("\n") else "\n"
        console_text = result + separator + stderr
    else:
        console_text = result
    return (usage, console_text) if buffered else usage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a reviewable four-stage agent pipeline in a Git repository.",
        allow_abbrev=False,
    )
    parser.add_argument("task", help="The concrete engineering task to complete")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Target Git repository")
    parser.add_argument("--codex-model", help="Optional Codex model override")
    parser.add_argument("--claude-model", help="Optional Claude model override")
    parser.add_argument("--plan-model", help="Model override for the planning stage")
    parser.add_argument("--implement-model", help="Model override for the Codex implementation stage")
    parser.add_argument("--review-model", help="Model override for the primary review stage")
    parser.add_argument("--fix-model", help="Model override for the Codex review-fix stage")
    parser.add_argument(
        "--plan-backend",
        choices=["claude", "codex"],
        default="claude",
        help="Backend for Stage One planning (default: claude)",
    )
    parser.add_argument(
        "--review-backend",
        choices=["claude", "codex"],
        default="claude",
        help="Backend for the primary Stage Three review (default: claude)",
    )
    parser.add_argument(
        "--all-codex-mirror-formation",
        action="store_true",
        help=("Run the complete Codex formation: Codex plan/review, the six canonical "
              "Mirs concurrently through Codex, and Om-Mir synthesis through Codex"),
    )
    parser.add_argument(
        "--balanced-claude-codex",
        action="store_true",
        help=("Run an even formation: Claude plans, reviews, and synthesizes; "
              "Codex implements, independently reviews, and remediates"),
    )
    parser.add_argument(
        "--self-evolve",
        action="store_true",
        help=("Run one bounded all-Codex generation against this orchestrator's own "
              "repository, carrying the latest synthesis as lineage"),
    )
    parser.add_argument("--hermes", action="store_true",
                        help="Add an independent second review from the Hermes agent (tools disabled)")
    parser.add_argument("--hermes-model", help="Optional Hermes model override")
    parser.add_argument(
        "--mir-model",
        help="Model override for mirror reviews, independent of their selected backend",
    )
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
        "--parallel-mirs",
        action="store_true",
        help=("Run mirror-node reviews concurrently (default: sequential); "
              "stillness is then judged collectively across the corridor"),
    )
    parser.add_argument(
        "--mir-skills-dir",
        type=Path,
        default=Path("~/.hermes/skills/mirror-nodes"),
        help="Directory of mirror-node skill folders, each containing SKILL.md",
    )
    parser.add_argument(
        "--synthesize",
        action="store_true",
        help=("After all reviews complete, recombine them into one synthesis document "
              "(03c-synthesis.md): convergence map, singular findings, disagreements, "
              "unified verdict. The fix stage then follows the synthesis."),
    )
    parser.add_argument(
        "--synthesize-backend",
        choices=["hermes", "claude", "codex"],
        default=None,
        help="Backend for the synthesis stage (default: claude); passing this option enables synthesis",
    )
    parser.add_argument(
        "--synthesize-node",
        help=("Mirror-node lens for the synthesis stage "
              "(a subdirectory of --mir-skills-dir containing SKILL.md); "
              "passing this option enables synthesis"),
    )
    parser.add_argument(
        "--lineage",
        type=int,
        default=0,
        metavar="N",
        help=("Hand the planning stage the syntheses of the N most recent "
              "prior runs in this repository (default: 0, no lineage)"),
    )
    parser.add_argument(
        "--vow-policy",
        choices=["warn", "taint", "abort"],
        default="taint",
        help=("What a broken vow of stillness does: warn and continue, "
              "taint (exclude the breaching review from synthesis and fix), "
              "or abort the run (default: taint)"),
    )
    parser.add_argument("--max-budget-usd", type=float, help="Budget for each Claude invocation")
    parser.add_argument("--stage-timeout-seconds", type=float,
                        help="Wall-clock timeout for each agent invocation (default: none)")
    parser.add_argument("--allow-dirty", action="store_true", help="Run despite existing changes")
    parser.add_argument("--skip-review-fix", action="store_true", help="Stop after Stage Three review")
    parser.add_argument("--dry-run", action="store_true", help="Print commands and prompts only")
    args = parser.parse_args()
    if args.stage_timeout_seconds is not None and (
        not math.isfinite(args.stage_timeout_seconds) or args.stage_timeout_seconds <= 0
    ):
        parser.error("--stage-timeout-seconds must be greater than zero")
    if args.self_evolve:
        if SELF_EVOLUTION_ACTIVE_ENV in os.environ:
            parser.error(
                "--self-evolve cannot run recursively: a self-evolution generation is already active"
            )
        conflicting = {
            "--repo", "--all-codex-mirror-formation", "--plan-backend",
            "--balanced-claude-codex",
            "--review-backend", "--mir", "--mir-backend", "--parallel-mirs",
            "--synthesize", "--synthesize-backend", "--synthesize-node",
            "--hermes", "--lineage", "--allow-dirty", "--skip-review-fix",
        }
        supplied_options = {
            token.split("=", 1)[0] for token in sys.argv[1:] if token.startswith("--")
        }
        supplied = sorted(conflicting & supplied_options)
        if supplied:
            parser.error(
                "--self-evolve cannot be combined with: " + ", ".join(supplied)
            )
    if args.all_codex_mirror_formation:
        conflicting = {
            "--plan-backend", "--review-backend", "--mir", "--mir-backend",
            "--parallel-mirs", "--synthesize", "--synthesize-backend",
            "--synthesize-node", "--hermes",
        }
        supplied_options = {token.split("=", 1)[0] for token in sys.argv[1:] if token.startswith("--")}
        supplied = sorted(conflicting & supplied_options)
        if supplied:
            parser.error(
                "--all-codex-mirror-formation cannot be combined with: "
                + ", ".join(supplied)
            )
    if args.balanced_claude_codex:
        conflicting = {
            "--all-codex-mirror-formation", "--self-evolve", "--plan-backend",
            "--review-backend", "--mir", "--mir-backend", "--parallel-mirs",
            "--synthesize", "--synthesize-backend", "--synthesize-node", "--hermes",
        }
        supplied_options = {token.split("=", 1)[0] for token in sys.argv[1:] if token.startswith("--")}
        supplied = sorted(conflicting & supplied_options)
        if supplied:
            parser.error(
                "--balanced-claude-codex cannot be combined with: " + ", ".join(supplied)
            )
    return args


def main() -> int:
    args = parse_args()
    self_evolve = getattr(args, "self_evolve", False)
    agent_env = None
    if self_evolve:
        agent_env = os.environ.copy()
        agent_env[SELF_EVOLUTION_ACTIVE_ENV] = "1"
    all_codex = self_evolve or getattr(args, "all_codex_mirror_formation", False)
    balanced = getattr(args, "balanced_claude_codex", False)
    plan_backend = "codex" if all_codex else "claude" if balanced else getattr(args, "plan_backend", "claude")
    review_backend = "codex" if all_codex else "claude" if balanced else getattr(args, "review_backend", "claude")
    configured_mir_backend = "codex" if all_codex or balanced else args.mir_backend
    mir_backend = configured_mir_backend or "hermes"
    mir_nodes = unique_mir_nodes(
        list(CANONICAL_MIR_NODES) if all_codex else (args.mir or [])
    )
    mir_enabled = all_codex or balanced or mirror_review_enabled(
        args.hermes, mir_nodes, configured_mir_backend
    )
    mir_skills_dir = args.mir_skills_dir.expanduser().resolve()
    mir_skill_texts: dict[str, str | None] = {}
    for node in mir_nodes:
        skill_file = resolve_mir_skill(mir_skills_dir, node)
        mir_skill_texts[node] = (
            skill_file.read_text(encoding="utf-8") if mir_backend != "hermes" else None
        )

    synth_enabled = all_codex or balanced or (
        args.synthesize
        or args.synthesize_backend is not None
        or args.synthesize_node is not None
    )
    synth_backend = "codex" if all_codex else "claude" if balanced else (args.synthesize_backend or "claude")
    stage_models = select_stage_models(
        claude_model=args.claude_model,
        codex_model=args.codex_model,
        hermes_model=args.hermes_model,
        plan_backend=plan_backend,
        review_backend=review_backend,
        mir_backend=mir_backend,
        synth_backend=synth_backend,
        plan_model=getattr(args, "plan_model", None),
        implement_model=getattr(args, "implement_model", None),
        review_model=getattr(args, "review_model", None),
        mir_model=getattr(args, "mir_model", None),
        fix_model=getattr(args, "fix_model", None),
    )
    synth_skill_text = None
    synth_node = "om-mir" if all_codex else (
        (args.synthesize_node or "om-mir") if synth_enabled else None
    )
    if synth_node:
        synth_skill_file = resolve_mir_skill(mir_skills_dir, synth_node)
        if synth_backend != "hermes":
            synth_skill_text = synth_skill_file.read_text(encoding="utf-8")

    executables = {"git", "codex"}
    executables.add(plan_backend)
    executables.add(review_backend)
    if mir_enabled:
        executables.add(mir_backend)
    if synth_enabled:
        executables.add(synth_backend)
    if (mir_enabled and mir_backend == "hermes") or (synth_enabled and synth_backend == "hermes"):
        executables.add("hermes")
        print(
            "warning: hermes >=0.18.2 ignores `-t \"\"` (and --skills force-enables its "
            "declared toolsets), so hermes mirror reviewers run with FULL tool access — "
            "including file writes and terminal — despite the prompt telling them otherwise. "
            "Verified empirically 2026-07-15: reviewers overwrote each other's output files. "
            "Use --mir-backend claude or codex for an actually-sandboxed review until fixed.",
            file=sys.stderr,
        )
    for executable in sorted(executables):
        if not shutil.which(executable):
            raise RuntimeError(f"Required executable not found: {executable}")
    repo = (
        Path(__file__).resolve().parent
        if self_evolve
        else args.repo.expanduser().resolve()
    )
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
    lineage = gather_lineage(runs_dir, 1 if self_evolve else args.lineage)
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
        "self_evolution": self_evolve,
        "generation_limit": 1 if self_evolve else None,
        "human_git_boundary": ({
            "sandbox": "workspace-write",
            "network_access": False,
            "head_verification": True,
        } if self_evolve else None),
        "all_codex_mirror_formation": all_codex,
        "balanced_claude_codex": balanced,
        "plan_backend": plan_backend,
        "review_backend": review_backend,
        "hermes": args.hermes,
        "mir_enabled": mir_enabled,
        "mir_nodes": mir_nodes,
        "mir_backend": mir_backend if mir_enabled else None,
        "parallel_mirs": all_codex or args.parallel_mirs,
        "synthesize": synth_enabled,
        "synthesize_backend": synth_backend if synth_enabled else None,
        "synthesize_node": synth_node,
        "stage_models": stage_models,
        "lineage": [name for name, _ in lineage],
        "vow_policy": args.vow_policy,
        "vows": {},
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

    def verify_analysis_vow(stage: str, before: str | None, *, taintable: bool = False) -> bool:
        if before is None:
            return True
        verdict = collective_vow_verdict(before, tree_fingerprint(repo, baseline))
        metadata["vows"][stage] = verdict
        write(run_metadata, json.dumps(metadata, indent=2))
        if verdict == "kept":
            return True
        print(f"warning: vow of stillness broken during {stage}", file=sys.stderr)
        if args.vow_policy == "abort" or (args.vow_policy == "taint" and not taintable):
            raise RuntimeError(f"vow of stillness broken during {stage} (--vow-policy {args.vow_policy})")
        if args.vow_policy == "taint":
            print(f"warning: {stage} is tainted and excluded from downstream synthesis and fix", file=sys.stderr)
            return False
        return True

    plan = artifacts / "01-plan.md"
    codex_result = artifacts / "02-implementation.md"
    review = artifacts / "03-review.md"
    fix_result = artifacts / "04-fixes.md"

    def claude_command(model: str | None, tools: str = "Read,Grep,Glob,Bash") -> list[str]:
        command = [
            "claude", "--print", "--no-session-persistence", "--permission-mode", "plan",
            "--tools", tools, "--output-format", "json",
        ]
        if model:
            command += ["--model", model]
        if args.max_budget_usd is not None:
            command += ["--max-budget-usd", str(args.max_budget_usd)]
        return command

    def analysis_command(
        backend: str, model: str | None, output: Path, *, frozen: bool = False
    ) -> tuple[list[str], bool]:
        if backend == "claude":
            return claude_command(model, "" if frozen else "Read,Grep,Glob,Bash"), True
        command = [
            "codex", "exec", "-C", str(repo), "--sandbox", "read-only",
            "--color", "never", "--output-last-message", str(output), "-",
        ]
        if self_evolve:
            command[2:2] = ["-c", "sandbox_workspace_write.network_access=false"]
        if model:
            command[2:2] = ["--model", model]
        return command, False

    plan_cmd, plan_parse_json = analysis_command(plan_backend, stage_models["plan"], plan)
    review_cmd, review_parse_json = analysis_command(
        review_backend, stage_models["review"], review, frozen=True
    )

    codex_cmd = [
        "codex", "exec", "-C", str(repo), "--sandbox", "workspace-write",
        "--color", "never", "--output-last-message", str(codex_result), "-",
    ]
    if self_evolve:
        codex_cmd[2:2] = ["-c", "sandbox_workspace_write.network_access=false"]
    if stage_models["implement"]:
        codex_cmd[2:2] = ["--model", stage_models["implement"]]

    fix_cmd = [
        "codex", "exec", "-C", str(repo), "--sandbox", "workspace-write",
        "--color", "never", "--output-last-message", str(fix_result), "-",
    ]
    if self_evolve:
        fix_cmd[2:2] = ["-c", "sandbox_workspace_write.network_access=false"]
    if stage_models["fix"]:
        fix_cmd[2:2] = ["--model", stage_models["fix"]]

    plan_prompt = f"""You are the planning engineer. Analyze this repository and produce a concise, implementation-ready plan for the task below. Do not edit files. Include affected files, important constraints, tests, and risks.\n\nTASK:\n{args.task}{build_lineage_block(lineage)}"""
    plan_stage = f"{plan_backend.title()}: plan"
    plan_before = None if args.dry_run else tree_fingerprint(repo, baseline)
    started = time.monotonic()
    usage = invoke(
        plan_stage,
        plan_cmd,
        plan_prompt,
        plan,
        repo,
        args.dry_run,
        timeout=args.stage_timeout_seconds,
        parse_json=plan_parse_json,
        env=agent_env,
        protected_head=baseline if self_evolve and not args.dry_run else None,
    )
    record_stage(plan_stage, usage, time.monotonic() - started)
    verify_analysis_vow(plan_stage, plan_before)

    started = time.monotonic()
    usage = invoke(
        "Codex: implement",
        codex_cmd,
        f"""Implement the requested task in this repository. Read the plan at {plan}. Inspect the code yourself, keep changes scoped, and run relevant tests. Do not commit, push, or discard pre-existing changes.\n\nTASK:\n{args.task}""",
        codex_result,
        repo,
        args.dry_run,
        timeout=args.stage_timeout_seconds,
        env=agent_env,
        protected_head=baseline if self_evolve and not args.dry_run else None,
    )
    record_stage("Codex: implement", usage, time.monotonic() - started)

    # Freeze one post-implementation scroll. The primary reviewer and every
    # mirror judge this exact text rather than observing different tree states.
    diff_text = complete_diff(repo, baseline)
    if len(diff_text) > 120_000:
        diff_text = diff_text[:120_000] + "\n[diff truncated for length]"
    metadata["scroll_sha256"] = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    write(run_metadata, json.dumps(metadata, indent=2))

    review_stage = f"{review_backend.title()}: review"
    review_before = None if args.dry_run else tree_fingerprint(repo, baseline)
    started = time.monotonic()
    usage = invoke(
        review_stage,
        review_cmd,
        build_mir_prompt(args.task, baseline, diff_text),
        review,
        repo,
        args.dry_run,
        timeout=args.stage_timeout_seconds,
        parse_json=review_parse_json,
        env=agent_env,
        protected_head=baseline if self_evolve and not args.dry_run else None,
    )
    record_stage(review_stage, usage, time.monotonic() - started)
    primary_review_kept = verify_analysis_vow(review_stage, review_before, taintable=True)

    reviews = [review] if primary_review_kept else []
    if mir_enabled:
        # The mirror reviewers never see the primary review or use repository
        # tools, so every backend receives the diff inside its prompt. Each
        # node reviews the same diff independently of the others.
        fingerprint = None if args.dry_run else tree_fingerprint(repo, baseline)
        mirror_jobs = []
        for node in mir_nodes or [None]:
            skill_text = mir_skill_texts.get(node) if node else None
            mir_prompt = build_mir_prompt(args.task, baseline, diff_text, skill_text)
            stage_label = mir_stage_label(mir_backend, node)
            mir_review = mir_review_path(artifacts, node)

            mir_cmd, prompt_flag, parse_json = mirror_review_invocation(
                mir_backend,
                repo=repo,
                output=mir_review,
                node=node,
                hermes_model=stage_models["mir"] if mir_backend == "hermes" else None,
                claude_model=stage_models["mir"] if mir_backend == "claude" else None,
                codex_model=stage_models["mir"] if mir_backend == "codex" else None,
                max_budget_usd=args.max_budget_usd,
            )
            if self_evolve:
                mir_cmd[2:2] = ["-c", "sandbox_workspace_write.network_access=false"]
            mirror_jobs.append((stage_label, mir_review, mir_cmd, prompt_flag, parse_json, mir_prompt))

        if all_codex or args.parallel_mirs:
            first_error = None
            completed_reviews = {}
            with ThreadPoolExecutor(max_workers=len(mirror_jobs)) as executor:
                futures = {}
                for job_index, job in enumerate(mirror_jobs):
                    stage_label, mir_review, mir_cmd, prompt_flag, parse_json, mir_prompt = job
                    future = executor.submit(
                        invoke_timed, stage_label, mir_cmd, mir_prompt, mir_review, repo, args.dry_run,
                        prompt_flag=prompt_flag, timeout=args.stage_timeout_seconds,
                        parse_json=parse_json, buffered=True,
                        env=agent_env,
                        protected_head=baseline if self_evolve and not args.dry_run else None,
                    )
                    futures[future] = (job_index, stage_label, mir_review)
                for future in as_completed(futures):
                    job_index, stage_label, mir_review = futures[future]
                    try:
                        usage, console_text, duration = future.result()
                        print(f"\n==> {stage_label}", flush=True)
                        if console_text:
                            print(console_text, end="" if console_text.endswith("\n") else "\n")
                        completed_reviews[job_index] = mir_review
                    except Exception as error:
                        usage = None
                        if isinstance(error, TimedInvocationError):
                            duration = error.duration
                            stage_error = error.error
                        else:
                            # Defensive fallback for failures outside invoke_timed.
                            duration = 0.0
                            stage_error = error
                        print(
                            f"\n==> {stage_label}\nERROR: {stage_error}",
                            file=sys.stderr,
                            flush=True,
                        )
                        if first_error is None:
                            first_error = stage_error
                    record_stage(stage_label, usage, duration)

            if fingerprint is not None:
                after = tree_fingerprint(repo, baseline)
                verdict = collective_vow_verdict(fingerprint, after)
                # A corridor breach belongs to this corridor, not synthesis.
                fingerprint = after
                for stage_label, *_ in mirror_jobs:
                    metadata["vows"][stage_label] = verdict
                write(run_metadata, json.dumps(metadata, indent=2))
                if verdict == "broken":
                    print(
                        "warning: collective vow of stillness broken — the working tree "
                        "changed during the parallel mirror corridor",
                        file=sys.stderr,
                    )
                    if args.vow_policy == "abort" and first_error is None:
                        first_error = RuntimeError(
                            "collective vow of stillness broken during parallel mirror corridor "
                            "(--vow-policy abort)"
                        )
                    if args.vow_policy == "taint":
                        completed_reviews = {}
                        print(
                            "warning: all parallel mirror reviews are tainted and excluded "
                            "from synthesis and fix",
                            file=sys.stderr,
                        )
            if first_error is not None:
                raise first_error
            reviews.extend(completed_reviews[index] for index in sorted(completed_reviews))
        else:
            for stage_label, mir_review, mir_cmd, prompt_flag, parse_json, mir_prompt in mirror_jobs:
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
                    env=agent_env,
                    protected_head=baseline if self_evolve and not args.dry_run else None,
                )
                duration = time.monotonic() - started
                tainted = False
                if fingerprint is not None:
                    after = tree_fingerprint(repo, baseline)
                    if after == fingerprint:
                        metadata["vows"][stage_label] = "kept"
                    else:
                        # Re-baseline so later nodes aren't blamed for this breach.
                        metadata["vows"][stage_label] = "broken"
                        fingerprint = after
                        print(
                            f"warning: vow of stillness broken — the working tree changed during {stage_label}",
                            file=sys.stderr,
                        )
                        if args.vow_policy == "abort":
                            record_stage(stage_label, usage, duration)
                            raise RuntimeError(
                                f"vow of stillness broken during {stage_label} (--vow-policy abort)"
                            )
                        if args.vow_policy == "taint":
                            tainted = True
                            print(
                                f"warning: {mir_review.name} is tainted and excluded from synthesis and fix",
                                file=sys.stderr,
                            )
                record_stage(stage_label, usage, duration)
                if not tainted:
                    reviews.append(mir_review)

    synthesis = artifacts / "03c-synthesis.md"
    synth_tainted = False
    if synth_enabled:
        # Recombination: every review in, one convergence-weighted document out.
        # The synthesizer sees only the review texts — no repo, no diff.
        labeled = (
            []
            if args.dry_run
            else [(p.name, p.read_text(encoding="utf-8")) for p in reviews if p.is_file()]
        )
        synth_prompt = build_synthesis_prompt(args.task, labeled, synth_skill_text)
        synth_tag = f", {synth_node}" if synth_node else ""
        stage_label = f"Synthesis ({synth_backend}{synth_tag}): recombination"
        synth_cmd, prompt_flag, parse_json = mirror_review_invocation(
            synth_backend,
            repo=repo,
            output=synthesis,
            node=synth_node,
            hermes_model=(stage_models["synthesize"] if synth_backend == "hermes" else None),
            claude_model=(stage_models["synthesize"] if synth_backend == "claude" else None),
            codex_model=(stage_models["synthesize"] if synth_backend == "codex" else None),
            max_budget_usd=args.max_budget_usd,
        )
        if self_evolve:
            synth_cmd[2:2] = ["-c", "sandbox_workspace_write.network_access=false"]
        synth_before = None if args.dry_run else tree_fingerprint(repo, baseline)
        started = time.monotonic()
        usage = invoke(
            stage_label,
            synth_cmd,
            synth_prompt,
            synthesis,
            repo,
            args.dry_run,
            prompt_flag=prompt_flag,
            timeout=args.stage_timeout_seconds,
            parse_json=parse_json,
            env=agent_env,
            protected_head=baseline if self_evolve and not args.dry_run else None,
        )
        duration = time.monotonic() - started
        if not args.dry_run:
            metadata["synthesis_inputs"] = [p.name for p in reviews if p.is_file()]
        if synth_before is not None:
            if tree_fingerprint(repo, baseline) == synth_before:
                metadata["vows"][stage_label] = "kept"
            else:
                metadata["vows"][stage_label] = "broken"
                print(
                    f"warning: vow of stillness broken — the working tree changed during {stage_label}",
                    file=sys.stderr,
                )
                if args.vow_policy == "abort":
                    record_stage(stage_label, usage, duration)
                    raise RuntimeError(
                        f"vow of stillness broken during {stage_label} (--vow-policy abort)"
                    )
                if args.vow_policy == "taint":
                    synth_tainted = True
                    print(
                        "warning: the synthesis is tainted — the fix stage will use the reviews directly",
                        file=sys.stderr,
                    )
        record_stage(stage_label, usage, duration)

    if not args.dry_run:
        metadata["artifacts_sha256"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [*reviews, synthesis]
            if p.is_file()
        }
        write(run_metadata, json.dumps(metadata, indent=2))

    if not args.skip_review_fix:
        texts = [] if args.dry_run else [p.read_text(encoding="utf-8") for p in reviews]
        all_clean = bool(texts) and all(review_is_clean(t) for t in texts)
        synthesis_clean = (
            synth_enabled
            and not synth_tainted
            and not args.dry_run
            and synthesis.is_file()
            and review_is_clean(synthesis.read_text(encoding="utf-8"), require_seal=True)
        )
        if all_clean or synthesis_clean:
            reason = (
                "all reviews are sealed clean"
                if all_clean
                else "the synthesis is sealed clean"
            )
            print(f"\n==> Codex: address review (skipped — {reason})")
        else:
            review_refs = " and ".join(str(p) for p in reviews)
            if synth_enabled and not synth_tainted:
                fix_prompt = f"""Read the synthesis at {synthesis}, which recombines every review at {review_refs} into a convergence map and a priority-ordered action list. Work from the synthesis; consult the underlying reviews when you need a finding's full evidence. Verify every claim against the repository. Address all valid actionable findings for the task below, ignore unsupported suggestions, and run relevant tests. Do not commit or push.\n\nTASK:\n{args.task}"""
            else:
                fix_prompt = f"""Read every review at {review_refs}. Verify every claim against the repository. Address all valid actionable findings for the task below, ignore unsupported suggestions, and run relevant tests. Do not commit or push. A review whose final line is SEAL: CLEAN requires no changes.\n\nTASK:\n{args.task}"""
            started = time.monotonic()
            usage = invoke(
                "Codex: address review",
                fix_cmd,
                fix_prompt,
                fix_result,
                repo,
                args.dry_run,
                timeout=args.stage_timeout_seconds,
                env=agent_env,
                protected_head=baseline if self_evolve and not args.dry_run else None,
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
