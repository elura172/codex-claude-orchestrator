import argparse
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from orchestrate import (
    CANONICAL_MIR_NODES,
    LINEAGE_CAP,
    SYNTHESIS_CAP,
    TimedInvocationError,
    build_lineage_block,
    build_mir_prompt,
    build_summary,
    build_synthesis_prompt,
    collective_vow_verdict,
    extract_result_and_usage,
    gather_lineage,
    format_duration,
    invoke,
    invoke_timed,
    main,
    mir_review_path,
    mir_stage_label,
    mirror_review_enabled,
    mirror_review_invocation,
    parse_args,
    resolve_mir_skill,
    review_is_clean,
    run,
    select_stage_models,
    tree_fingerprint,
    unique_mir_nodes,
)


def namespace(**overrides):
    values = dict(
        task="test task", repo=Path("."), codex_model=None, claude_model=None,
        plan_model=None, implement_model=None, review_model=None, fix_model=None,
        plan_backend="claude", review_backend="claude",
        all_codex_mirror_formation=False, self_evolve=False,
        hermes=False, hermes_model=None,
        mir_model=None, mir=None, mir_backend=None, parallel_mirs=False,
        synthesize=False, synthesize_backend=None, synthesize_node=None,
        lineage=0, vow_policy="taint", mir_skills_dir=Path("skills"),
        max_budget_usd=None, stage_timeout_seconds=None, allow_dirty=False,
        skip_review_fix=False, dry_run=True,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class SynthesisPromptTests(unittest.TestCase):
    def test_labels_and_texts_included(self) -> None:
        prompt = build_synthesis_prompt(
            "the task",
            [("03-review.md", "finding A"), ("03b-mir-ky-mir-review.md", "finding B")],
        )
        self.assertIn("### REVIEW: 03-review.md", prompt)
        self.assertIn("finding A", prompt)
        self.assertIn("### REVIEW: 03b-mir-ky-mir-review.md", prompt)
        self.assertIn("finding B", prompt)
        self.assertIn("the task", prompt)
        self.assertIn("CONVERGENCE MAP", prompt)

    def test_skill_text_prefixes_prompt(self) -> None:
        prompt = build_synthesis_prompt("t", [("r.md", "x")], skill_text="OM LENS")
        self.assertTrue(prompt.startswith("OM LENS\n\n---\n\n"))
        self.assertIn("CONVERGENCE MAP", prompt)

    def test_empty_reviews_placeholder(self) -> None:
        prompt = build_synthesis_prompt("t", [])
        self.assertIn("(no reviews available)", prompt)

    def test_overlong_reviews_truncated(self) -> None:
        prompt = build_synthesis_prompt("t", [("big.md", "x" * (SYNTHESIS_CAP + 1000))])
        self.assertIn("[reviews truncated for length]", prompt)
        self.assertLess(len(prompt), SYNTHESIS_CAP + 2000)


class SealTests(unittest.TestCase):
    def test_clean_seal_on_last_line(self) -> None:
        self.assertTrue(review_is_clean("I looked at everything.\n\nSEAL: CLEAN\n"))
        self.assertTrue(review_is_clean("seal: clean"))

    def test_findings_seal_is_not_clean(self) -> None:
        self.assertFalse(review_is_clean("HIGH: race in run().\n\nSEAL: FINDINGS 1"))

    def test_quoted_sentinel_cannot_fake_cleanliness(self) -> None:
        text = (
            "The prompt told me to say exactly: NO ACTIONABLE FINDINGS if none.\n"
            "But there are three HIGH findings.\n"
            "SEAL: FINDINGS 3"
        )
        self.assertFalse(review_is_clean(text))

    def test_legacy_sentinel_fallback(self) -> None:
        self.assertTrue(review_is_clean("NO ACTIONABLE FINDINGS"))
        self.assertFalse(review_is_clean("NO ACTIONABLE FINDINGS", require_seal=True))

    def test_no_verdict_is_not_clean(self) -> None:
        self.assertFalse(review_is_clean("HIGH: something is wrong."))
        self.assertFalse(review_is_clean(""))

    def test_malformed_seal_is_not_clean(self) -> None:
        self.assertFalse(review_is_clean("all good\nSEAL: PROBABLY FINE"))


class StillnessTests(unittest.TestCase):
    def test_fingerprint_detects_and_forgives_tree_changes(self) -> None:
        from orchestrate import git

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q")
            git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-q", "--allow-empty", "-m", "seed")
            baseline = git(repo, "rev-parse", "HEAD")

            before = tree_fingerprint(repo, baseline)
            mark = repo / "mark.txt"
            mark.write_text("a spirit was here\n", encoding="utf-8")
            during = tree_fingerprint(repo, baseline)
            mark.unlink()
            after = tree_fingerprint(repo, baseline)

            self.assertNotEqual(before, during)
            self.assertEqual(before, after)


class LineageTests(unittest.TestCase):
    def test_gathers_newest_first_and_skips_runs_without_synthesis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            for name, synthesis in [
                ("20260101-000000", "oldest"),
                ("20260102-000000", None),
                ("20260103-000000", "middle"),
                ("20260104-000000", "newest"),
            ]:
                run = runs / name
                run.mkdir()
                if synthesis is not None:
                    (run / "03c-synthesis.md").write_text(synthesis, encoding="utf-8")

            entries = gather_lineage(runs, 2)

            self.assertEqual(
                entries,
                [("20260104-000000", "newest"), ("20260103-000000", "middle")],
            )

    def test_zero_count_and_missing_dir_return_empty(self) -> None:
        self.assertEqual(gather_lineage(Path("/nonexistent"), 3), [])
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(gather_lineage(Path(directory), 0), [])

    def test_block_labels_runs_and_is_empty_without_entries(self) -> None:
        self.assertEqual(build_lineage_block([]), "")
        block = build_lineage_block([("20260104-000000", "finding X")])
        self.assertIn("LINEAGE", block)
        self.assertIn("### RUN 20260104-000000", block)
        self.assertIn("finding X", block)

    def test_overlong_lineage_truncated(self) -> None:
        block = build_lineage_block([("big", "x" * (LINEAGE_CAP + 1000))])
        self.assertIn("[lineage truncated for length]", block)
        self.assertLess(len(block), LINEAGE_CAP + 2000)


class SummaryTests(unittest.TestCase):
    def test_format_duration(self) -> None:
        self.assertEqual(format_duration(0), "0:00")
        self.assertEqual(format_duration(12.4), "0:12")
        self.assertEqual(format_duration(65), "1:05")
        self.assertEqual(format_duration(3661), "1:01:01")

    def test_empty_summary(self) -> None:
        self.assertEqual(build_summary({}, {}), "No stages executed.")

    def test_summary_aligns_stages_and_totals_recorded_costs(self) -> None:
        summary = build_summary(
            {"Claude: plan": 12.4, "Codex: implement": 65},
            {
                "Claude: plan": {"total_cost_usd": 0.0421},
                "Codex: implement": None,
            },
        )
        lines = summary.splitlines()

        self.assertIn("Claude: plan", lines[0])
        self.assertIn("0:12", lines[0])
        self.assertTrue(lines[0].endswith("$0.0421"))
        self.assertIn("Codex: implement", lines[1])
        self.assertTrue(lines[1].endswith("1:05"))
        self.assertEqual(lines[-2], "-" * max(len(line) for line in lines if line != lines[-2]))
        self.assertIn("Total", lines[-1])
        self.assertIn("1:17", lines[-1])
        self.assertTrue(lines[-1].endswith("$0.0421"))

    def test_zero_cost_is_recorded(self) -> None:
        summary = build_summary(
            {"Claude: plan": 1},
            {"Claude: plan": {"total_cost_usd": 0.0}},
        )

        self.assertEqual(summary.count("$0.0000"), 2)

    def test_costless_summary_has_blank_total_cost(self) -> None:
        summary = build_summary(
            {"Codex: implement": 1},
            {"Codex: implement": None},
        )

        self.assertNotIn("$", summary)
        self.assertTrue(summary.splitlines()[-1].endswith("0:01"))

    def test_main_persists_and_prints_summary_from_recorded_stages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git_dir = repo / ".git"
            git_dir.mkdir()
            args = argparse.Namespace(
                task="test task", repo=repo, codex_model=None, claude_model=None,
                hermes=False, hermes_model=None, mir=None, mir_backend=None,
                parallel_mirs=False,
                synthesize=False, synthesize_backend=None, synthesize_node=None,
                lineage=0, vow_policy="taint",
                mir_skills_dir=repo / "skills", max_budget_usd=None,
                stage_timeout_seconds=None, allow_dirty=False,
                skip_review_fix=False, dry_run=True,
            )

            def fake_git(_repo: Path, *git_args: str) -> str:
                if git_args == ("rev-parse", "--show-toplevel"):
                    return str(repo)
                if git_args == ("rev-parse", "--path-format=absolute", "--git-dir"):
                    return str(git_dir)
                if git_args == ("rev-parse", "HEAD"):
                    return "baseline"
                if git_args == ("status", "--porcelain"):
                    return ""
                raise AssertionError(git_args)

            usages = [
                {"total_cost_usd": 0.1}, None,
                {"total_cost_usd": 0.2}, None,
            ]
            output = io.StringIO()
            with (
                patch("orchestrate.parse_args", return_value=args),
                patch("orchestrate.shutil.which", return_value="/bin/tool"),
                patch("orchestrate.git", side_effect=fake_git),
                patch("orchestrate.invoke", side_effect=usages),
                patch("orchestrate.time.monotonic", side_effect=range(0, 16, 2)),
                redirect_stdout(output),
            ):
                self.assertEqual(main(), 0)

            artifacts = next((git_dir / "agent-collab" / "runs").iterdir())
            metadata = json.loads((artifacts / "run.json").read_text())
            summary = (artifacts / "summary.txt").read_text().rstrip()
            self.assertEqual(
                list(metadata["durations"]),
                ["Claude: plan", "Codex: implement", "Claude: review", "Codex: address review"],
            )
            self.assertEqual(list(metadata["durations"].values()), [2, 2, 2, 2])
            self.assertEqual(summary.count("$0.3000"), 1)
            self.assertIn(summary, output.getvalue())


class ExtractResultAndUsageTests(unittest.TestCase):
    def test_extracts_result_and_usage(self) -> None:
        stdout = (
            '{"result":"response","total_cost_usd":0.25,"num_turns":2,'
            '"usage":{"input_tokens":10,"output_tokens":20}}'
        )

        result, usage = extract_result_and_usage(stdout)

        self.assertEqual(result, "response")
        self.assertEqual(usage, {
            "input_tokens": 10,
            "output_tokens": 20,
            "total_cost_usd": 0.25,
            "num_turns": 2,
        })

    def test_top_level_metadata_wins_over_usage_keys(self) -> None:
        stdout = (
            '{"result":"response","total_cost_usd":0.25,"num_turns":2,'
            '"usage":{"total_cost_usd":99,"num_turns":99}}'
        )

        _, usage = extract_result_and_usage(stdout)

        self.assertEqual(usage["total_cost_usd"], 0.25)
        self.assertEqual(usage["num_turns"], 2)

    def test_missing_result_falls_back_to_stdout(self) -> None:
        stdout = '{"usage":{"input_tokens":10}}'

        self.assertEqual(extract_result_and_usage(stdout), (stdout, None))

    def test_invalid_usage_falls_back_to_stdout(self) -> None:
        stdout = '{"result":"response","usage":null}'

        self.assertEqual(extract_result_and_usage(stdout), (stdout, None))

    def test_malformed_json_falls_back_to_stdout(self) -> None:
        stdout = "plain response"

        self.assertEqual(extract_result_and_usage(stdout), (stdout, None))


class InvokeTests(unittest.TestCase):
    def test_dry_run_returns_null_usage_and_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "result.md"

            usage = invoke("stage", ["tool"], "prompt", output, root, True)

            self.assertIsNone(usage)
            self.assertEqual(output.read_text(), "DRY RUN: tool\n")
            self.assertEqual(
                output.with_suffix(".md.prompt.md").read_text(),
                "prompt\n",
            )

    def test_empty_prompt_flag_appends_prompt_positionally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "result.md"

            invoke("stage", ["printf", "%s"], "positional prompt", output, root, False,
                   prompt_flag="")

            self.assertEqual(output.read_text(), "positional prompt\n")

    def test_buffered_returns_output_without_printing_and_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "result.md"
            console = io.StringIO()
            with (
                patch("orchestrate.run", return_value=("complete review\n", "warning\n")) as mocked_run,
                redirect_stdout(console),
            ):
                result = invoke(
                    "mirror", ["tool"], "prompt", output, root, False, buffered=True
                )

            self.assertEqual(result, (None, "complete review\nwarning\n"))
            self.assertEqual(console.getvalue(), "")
            self.assertEqual(output.read_text(), "complete review\n")
            self.assertFalse(mocked_run.call_args.kwargs["stream"])
            self.assertTrue(mocked_run.call_args.kwargs["return_stderr"])

    def test_buffered_json_parsing_does_not_mix_stderr_into_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "result.md"
            payload = '{"result":"review","usage":{}}'
            with patch("orchestrate.run", return_value=(payload, "backend warning\n")):
                usage, console = invoke(
                    "mirror", ["tool"], "prompt", output, root, False,
                    parse_json=True, buffered=True,
                )

            self.assertEqual(usage["num_turns"], None)
            self.assertEqual(output.read_text(), "review\n")
            self.assertIn("backend warning", console)

    def test_buffered_dry_run_returns_command_without_printing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "result.md"
            console = io.StringIO()
            with redirect_stdout(console):
                result = invoke(
                    "mirror", ["tool", "an arg"], "prompt", output, root, True,
                    buffered=True,
                )

            self.assertEqual(result, (None, "tool 'an arg'"))
            self.assertEqual(console.getvalue(), "")

    def test_buffered_timeout_kills_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = (
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "time.sleep(30)"
            )
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                run([sys.executable, "-c", script], cwd=root, timeout=0.1)
            self.assertLess(time.monotonic() - started, 2.0)


class MirrorPureLogicTests(unittest.TestCase):
    def test_cli_defaults_preserve_legacy_pipeline(self) -> None:
        with patch.object(sys, "argv", ["orchestrate.py", "task"]):
            args = parse_args()

        self.assertEqual(args.plan_backend, "claude")
        self.assertEqual(args.review_backend, "claude")
        self.assertFalse(args.all_codex_mirror_formation)
        self.assertIsNone(args.mir)
        self.assertFalse(args.parallel_mirs)
        self.assertFalse(args.synthesize)

    def test_all_codex_preset_rejects_explicit_formation_options(self) -> None:
        for option in ("--plan-backend", "--mir", "--parallel-mirs", "--synthesize"):
            value = ["codex"] if option == "--plan-backend" else (
                ["ky-mir"] if option == "--mir" else []
            )
            with (
                self.subTest(option=option),
                patch.object(
                    sys, "argv",
                    ["orchestrate.py", "--all-codex-mirror-formation", option, *value, "task"],
                ),
                self.assertRaises(SystemExit),
                redirect_stdout(io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                parse_args()

    def test_cli_rejects_abbreviated_backend_options(self) -> None:
        with (
            patch.object(sys, "argv", ["orchestrate.py", "--plan-b", "codex", "task"]),
            self.assertRaises(SystemExit),
            redirect_stdout(io.StringIO()),
            patch("sys.stderr", io.StringIO()),
        ):
            parse_args()

    def test_self_evolve_is_bounded_and_rejects_scope_overrides(self) -> None:
        with patch.object(sys, "argv", ["orchestrate.py", "--self-evolve", "task"]):
            args = parse_args()
        self.assertTrue(args.self_evolve)
        self.assertFalse(args.all_codex_mirror_formation)

        for option, value in (("--repo", ["/tmp/repo"]), ("--lineage", ["2"]),
                              ("--allow-dirty", []), ("--skip-review-fix", [])):
            with (
                self.subTest(option=option),
                patch.object(
                    sys, "argv", ["orchestrate.py", "--self-evolve", option, *value, "task"],
                ),
                self.assertRaises(SystemExit),
                redirect_stdout(io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                parse_args()

    def test_all_codex_preset_expands_canonical_formation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git_dir = repo / ".git"
            git_dir.mkdir()
            skills_dir = repo / "skills"
            for node in (*CANONICAL_MIR_NODES, "om-mir"):
                skill = skills_dir / node / "SKILL.md"
                skill.parent.mkdir(parents=True)
                skill.write_text(f"{node} lens", encoding="utf-8")
            args = namespace(
                repo=repo, mir_skills_dir=skills_dir,
                all_codex_mirror_formation=True, skip_review_fix=True, dry_run=False,
            )

            def fake_git(_repo: Path, *git_args: str) -> str:
                return {
                    ("rev-parse", "--show-toplevel"): str(repo),
                    ("rev-parse", "--path-format=absolute", "--git-dir"): str(git_dir),
                    ("rev-parse", "HEAD"): "baseline",
                    ("status", "--porcelain"): "",
                    ("status", "--short"): "",
                }[git_args]

            invocations = []
            mirror_barrier = threading.Barrier(len(CANONICAL_MIR_NODES))

            def fake_invoke(*call_args, **kwargs):
                invocations.append((call_args, kwargs))
                if call_args[0].startswith("Mir ("):
                    mirror_barrier.wait(timeout=2)
                call_args[3].write_text("SEAL: CLEAN\n", encoding="utf-8")
                return (None, "complete\n") if kwargs.get("buffered") else None

            with (
                patch("orchestrate.parse_args", return_value=args),
                patch("orchestrate.shutil.which", return_value="/bin/tool"),
                patch("orchestrate.git", side_effect=fake_git),
                patch("orchestrate.complete_diff", return_value="frozen diff"),
                patch("orchestrate.tree_fingerprint", return_value="still"),
                patch("orchestrate.invoke", side_effect=fake_invoke),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(), 0)

            artifacts = next((git_dir / "agent-collab" / "runs").iterdir())
            metadata = json.loads((artifacts / "run.json").read_text())
            self.assertEqual(metadata["mir_nodes"], list(CANONICAL_MIR_NODES))
            self.assertNotIn("om-mir", metadata["mir_nodes"])
            self.assertTrue(metadata["parallel_mirs"])
            self.assertEqual(metadata["plan_backend"], "codex")
            self.assertEqual(metadata["review_backend"], "codex")
            self.assertEqual(metadata["mir_backend"], "codex")
            self.assertEqual(metadata["synthesize_backend"], "codex")
            self.assertEqual(metadata["synthesize_node"], "om-mir")
            self.assertEqual(
                metadata["synthesis_inputs"],
                ["03-review.md", *[
                    f"03b-mir-{node}-review.md" for node in CANONICAL_MIR_NODES
                ]],
            )
            for filename in metadata["synthesis_inputs"][1:]:
                self.assertTrue((artifacts / filename).is_file())
            calls_by_stage = {call_args[0]: (call_args, kwargs) for call_args, kwargs in invocations}
            for stage in ("Codex: plan", "Codex: review"):
                call_args, kwargs = calls_by_stage[stage]
                command = call_args[1]
                self.assertEqual(command[-1], "-")
                self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
                self.assertFalse(kwargs["parse_json"])
            implement_command = calls_by_stage["Codex: implement"][0][1]
            self.assertEqual(
                implement_command[implement_command.index("--sandbox") + 1],
                "workspace-write",
            )
            stages = [call_args[0] for call_args, _ in invocations]
            synthesis_index = stages.index("Synthesis (codex, om-mir): recombination")
            self.assertTrue(all(
                stages.index(mir_stage_label("codex", node)) < synthesis_index
                for node in CANONICAL_MIR_NODES
            ))

    def test_stage_models_use_backend_defaults_and_independent_overrides(self) -> None:
        defaults = select_stage_models(
            claude_model="claude-default",
            codex_model="codex-default",
            hermes_model="hermes-default",
            plan_backend="claude",
            review_backend="claude",
            mir_backend="hermes",
            synth_backend="codex",
        )
        self.assertEqual(defaults, {
            "plan": "claude-default",
            "implement": "codex-default",
            "review": "claude-default",
            "mir": "hermes-default",
            "synthesize": "codex-default",
            "fix": "codex-default",
        })

        overrides = select_stage_models(
            claude_model="claude-default",
            codex_model="codex-default",
            hermes_model="hermes-default",
            plan_backend="codex",
            review_backend="codex",
            mir_backend="claude",
            synth_backend="hermes",
            plan_model="plan-specific",
            implement_model="implement-specific",
            review_model="review-specific",
            mir_model="mir-specific",
            fix_model="fix-specific",
        )
        self.assertEqual(overrides, {
            "plan": "plan-specific",
            "implement": "implement-specific",
            "review": "review-specific",
            "mir": "mir-specific",
            "synthesize": "mir-specific",
            "fix": "fix-specific",
        })

    def test_prompt_contains_inputs_and_empty_diff_placeholder(self) -> None:
        prompt = build_mir_prompt("task text", "abc123", "")
        self.assertIn("task text", prompt)
        self.assertIn("abc123", prompt)
        self.assertIn("(no changes detected)", prompt)

    def test_prompt_prefixes_skill_and_contains_diff(self) -> None:
        prompt = build_mir_prompt("task", "base", "the diff", "LENS")
        self.assertTrue(prompt.startswith("LENS\n\n---\n\n"))
        self.assertIn("the diff", prompt)

    def test_stage_and_artifact_names_match_existing_convention(self) -> None:
        self.assertEqual(
            mir_stage_label("claude", "ky-mir"),
            "Mir (claude, ky-mir): independent review",
        )
        self.assertEqual(
            mir_review_path(Path("artifacts"), "ky-mir"),
            Path("artifacts/03b-mir-ky-mir-review.md"),
        )
        self.assertEqual(
            mir_review_path(Path("artifacts"), None),
            Path("artifacts/03b-mir-review.md"),
        )

    def test_collective_vow_verdict(self) -> None:
        self.assertEqual(collective_vow_verdict("same", "same"), "kept")
        self.assertEqual(collective_vow_verdict("before", "after"), "broken")

    def test_duplicate_nodes_are_rejected_without_reordering(self) -> None:
        self.assertEqual(unique_mir_nodes(["beta", "alpha"]), ["beta", "alpha"])
        with self.assertRaisesRegex(RuntimeError, "Duplicate --mir node.*alpha"):
            unique_mir_nodes(["alpha", "beta", "alpha"])

    def test_worker_side_duration_excludes_main_thread_retrieval(self) -> None:
        with (
            patch("orchestrate.invoke", return_value=(None, "text")),
            patch("orchestrate.time.monotonic", side_effect=[10.0, 12.5]),
        ):
            self.assertEqual(invoke_timed(), (None, "text", 2.5))

    def test_worker_side_duration_is_preserved_on_failure(self) -> None:
        with (
            patch("orchestrate.invoke", side_effect=RuntimeError("failed")),
            patch("orchestrate.time.monotonic", side_effect=[10.0, 13.25]),
        ):
            with self.assertRaises(TimedInvocationError) as raised:
                invoke_timed()

        self.assertIsInstance(raised.exception.error, RuntimeError)
        self.assertEqual(raised.exception.duration, 3.25)

    def test_timed_invocations_can_overlap(self) -> None:
        def slow_invoke(*_args, **_kwargs):
            time.sleep(0.2)
            return None, "complete\n"

        started = time.monotonic()
        with (
            patch("orchestrate.invoke", side_effect=slow_invoke),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = [executor.submit(invoke_timed) for _ in range(2)]
            for future in futures:
                self.assertEqual(future.result()[0:2], (None, "complete\n"))

        self.assertLess(time.monotonic() - started, 0.35)

    def test_parallel_main_records_each_node_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git_dir = repo / ".git"
            git_dir.mkdir()
            skills_dir = repo / "skills"
            for node in ("alpha", "beta"):
                skill = skills_dir / node / "SKILL.md"
                skill.parent.mkdir(parents=True)
                skill.write_text(f"{node} lens", encoding="utf-8")
            args = argparse.Namespace(
                task="test task", repo=repo, codex_model=None, claude_model=None,
                hermes=False, hermes_model=None, mir=["alpha", "beta"],
                mir_backend="claude", parallel_mirs=True,
                synthesize=False, synthesize_backend=None, synthesize_node=None,
                lineage=0, vow_policy="taint", mir_skills_dir=skills_dir,
                max_budget_usd=None, stage_timeout_seconds=None, allow_dirty=False,
                skip_review_fix=True, dry_run=True,
            )

            def fake_git(_repo: Path, *git_args: str) -> str:
                values = {
                    ("rev-parse", "--show-toplevel"): str(repo),
                    ("rev-parse", "--path-format=absolute", "--git-dir"): str(git_dir),
                    ("rev-parse", "HEAD"): "baseline",
                    ("status", "--porcelain"): "",
                }
                if git_args in values:
                    return values[git_args]
                raise AssertionError(git_args)

            def fake_invoke(*call_args, **kwargs):
                return (None, f"output for {call_args[0]}\n") if kwargs.get("buffered") else None

            console = io.StringIO()
            with (
                patch("orchestrate.parse_args", return_value=args),
                patch("orchestrate.shutil.which", return_value="/bin/tool"),
                patch("orchestrate.git", side_effect=fake_git),
                patch("orchestrate.complete_diff", return_value="frozen diff"),
                patch("orchestrate.invoke", side_effect=fake_invoke),
                redirect_stdout(console),
            ):
                self.assertEqual(main(), 0)

            artifacts = next((git_dir / "agent-collab" / "runs").iterdir())
            metadata = json.loads((artifacts / "run.json").read_text())
            self.assertTrue(metadata["parallel_mirs"])
            for node in ("alpha", "beta"):
                label = mir_stage_label("claude", node)
                self.assertIn(label, metadata["durations"])
                self.assertIn(label, metadata["usage"])
                self.assertIn(f"==> {label}\noutput for {label}\n", console.getvalue())

    def test_parallel_breach_is_collective_tainted_and_rebaselined_for_synthesis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git_dir = repo / ".git"
            git_dir.mkdir()
            skills_dir = repo / "skills"
            for node in ("alpha", "beta", "om-mir"):
                skill = skills_dir / node / "SKILL.md"
                skill.parent.mkdir(parents=True)
                skill.write_text("lens", encoding="utf-8")
            args = argparse.Namespace(
                task="task", repo=repo, codex_model=None, claude_model=None,
                hermes=False, hermes_model=None, mir=["alpha", "beta"],
                mir_backend="claude", parallel_mirs=True,
                synthesize=True, synthesize_backend="claude", synthesize_node=None,
                lineage=0, vow_policy="taint", mir_skills_dir=skills_dir,
                max_budget_usd=None, stage_timeout_seconds=None, allow_dirty=False,
                skip_review_fix=True, dry_run=False,
            )

            def fake_git(_repo: Path, *git_args: str) -> str:
                return {
                    ("rev-parse", "--show-toplevel"): str(repo),
                    ("rev-parse", "--path-format=absolute", "--git-dir"): str(git_dir),
                    ("rev-parse", "HEAD"): "baseline",
                    ("status", "--porcelain"): "",
                    ("status", "--short"): "",
                }[git_args]

            def fake_invoke(*call_args, **kwargs):
                output = call_args[3]
                output.write_text("SEAL: CLEAN\n", encoding="utf-8")
                return (None, "review output\n") if kwargs.get("buffered") else None

            with (
                patch("orchestrate.parse_args", return_value=args),
                patch("orchestrate.shutil.which", return_value="/bin/tool"),
                patch("orchestrate.git", side_effect=fake_git),
                patch("orchestrate.complete_diff", return_value="frozen diff"),
                patch("orchestrate.tree_fingerprint", side_effect=["before", "after", "after", "after"]),
                patch("orchestrate.invoke", side_effect=fake_invoke),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(), 0)

            artifacts = next((git_dir / "agent-collab" / "runs").iterdir())
            metadata = json.loads((artifacts / "run.json").read_text())
            for node in ("alpha", "beta"):
                self.assertEqual(metadata["vows"][mir_stage_label("claude", node)], "broken")
            self.assertEqual(
                metadata["vows"]["Synthesis (claude, om-mir): recombination"],
                "kept",
            )
            self.assertEqual(metadata["synthesize_node"], "om-mir")
            self.assertEqual(metadata["synthesis_inputs"], ["03-review.md"])


class ResolveMirSkillTests(unittest.TestCase):
    def test_returns_existing_skill_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skills_dir = Path(directory)
            skill_file = skills_dir / "node-b" / "SKILL.md"
            skill_file.parent.mkdir()
            skill_file.write_text("lens")

            self.assertEqual(resolve_mir_skill(skills_dir, "node-b"), skill_file)

    def test_unknown_node_lists_available_nodes_in_sorted_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skills_dir = Path(directory)
            for node in ("zeta", "alpha"):
                skill_file = skills_dir / node / "SKILL.md"
                skill_file.parent.mkdir()
                skill_file.write_text("lens")

            with self.assertRaises(RuntimeError) as raised:
                resolve_mir_skill(skills_dir, "missing")

            message = str(raised.exception)
            self.assertIn("missing", message)
            self.assertIn("Available nodes", message)
            self.assertLess(message.index("alpha"), message.index("zeta"))


class MirrorReviewConfigurationTests(unittest.TestCase):
    def test_each_legacy_or_new_option_enables_review(self) -> None:
        self.assertTrue(mirror_review_enabled(True, None, None))
        self.assertTrue(mirror_review_enabled(False, "node", None))
        self.assertTrue(mirror_review_enabled(False, None, "claude"))
        self.assertFalse(mirror_review_enabled(False, None, None))

    def test_hermes_uses_native_skill_argument(self) -> None:
        command, prompt_flag, parse_json = mirror_review_invocation(
            "hermes", repo=Path("/repo"), output=Path("review.md"), node="mirai"
        )

        self.assertEqual(command, ["hermes", "-t", "", "--skills", "mirai"])
        self.assertEqual(prompt_flag, "-z")
        self.assertFalse(parse_json)

    def test_claude_reads_large_prompt_from_stdin(self) -> None:
        command, prompt_flag, parse_json = mirror_review_invocation(
            "claude", repo=Path("/repo"), output=Path("review.md")
        )

        self.assertIn("--print", command)
        self.assertIsNone(prompt_flag)
        self.assertTrue(parse_json)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = "x" * 150_000
            with patch("orchestrate.run", return_value="review") as mocked_run:
                invoke("mirror", command, prompt, root / "review.md", root, False,
                       prompt_flag=prompt_flag)

            self.assertEqual(mocked_run.call_args.kwargs["stdin"], prompt)

    def test_codex_reads_stdin_in_read_only_sandbox(self) -> None:
        command, prompt_flag, parse_json = mirror_review_invocation(
            "codex", repo=Path("/repo"), output=Path("review.md")
        )

        self.assertEqual(command[-1], "-")
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIsNone(prompt_flag)
        self.assertFalse(parse_json)


if __name__ == "__main__":
    unittest.main()
