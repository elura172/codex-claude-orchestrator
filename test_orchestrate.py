import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from orchestrate import (
    LINEAGE_CAP,
    SYNTHESIS_CAP,
    build_lineage_block,
    build_summary,
    build_synthesis_prompt,
    extract_result_and_usage,
    gather_lineage,
    format_duration,
    invoke,
    main,
    mirror_review_enabled,
    mirror_review_invocation,
    resolve_mir_skill,
    review_is_clean,
    tree_fingerprint,
)


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
