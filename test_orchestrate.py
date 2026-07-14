import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrate import (
    extract_result_and_usage,
    invoke,
    mirror_review_enabled,
    mirror_review_invocation,
    resolve_mir_skill,
)


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
