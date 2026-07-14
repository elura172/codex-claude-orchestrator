import tempfile
import unittest
from pathlib import Path

from orchestrate import extract_result_and_usage, invoke


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


if __name__ == "__main__":
    unittest.main()
