# Codex + Claude Orchestrator

A small local orchestrator that runs a reviewable four-stage workflow:

1. Claude inspects the repository and writes an implementation plan.
2. Codex implements the task and runs relevant checks.
3. Claude reviews the working-tree diff. With `--hermes`, `--mir`, or `--mir-backend`, a chosen backend adds an independent second review (it never sees Claude's review).
4. Codex verifies and addresses actionable findings (skipped automatically when every review reports `NO ACTIONABLE FINDINGS`).

It does not commit, push, merge, bypass permissions, or discard changes. By default it refuses to start in a dirty repository.

## Requirements

- Python 3.10+
- Git
- Authenticated `codex` and `claude` CLIs on `PATH`

## Usage

```bash
python3 orchestrate.py \
  --repo /path/to/project \
  "Add rate limiting to the password-reset endpoint with regression tests"
```

Preview the commands and generated prompts without invoking either agent:

```bash
python3 orchestrate.py \
  --repo /path/to/project \
  --dry-run \
  "Describe the task"
```

Useful options:

```text
--codex-model MODEL
--claude-model MODEL
--hermes
--hermes-model MODEL
--mir NODE
--mir-backend {hermes,claude,codex}
--mir-skills-dir PATH
--max-budget-usd AMOUNT
--stage-timeout-seconds SECONDS
--skip-review-fix
--allow-dirty
```

`--stage-timeout-seconds` applies a wall-clock timeout to each agent invocation. By default, agent invocations have no timeout.

`--mir NODE` applies a mirror-node lens from `<mir-skills-dir>/<NODE>/SKILL.md`; the skills directory defaults to `~/.hermes/skills/mirror-nodes`. The mirror backend defaults to Hermes. Supplying `--mir` or `--mir-backend` enables the independent review without `--hermes`; `--hermes` remains available for backward compatibility. Hermes receives the node name through its native `--skills` option after the configured directory is used for pre-flight validation; Hermes itself resolves that name using its own skill configuration. Claude and Codex instead receive the validated skill file's text at the start of their review prompt.

After all stages finish, the orchestrator prints a plain-text summary of each executed stage's wall-clock duration and recorded cost, plus totals. The same text is saved as `summary.txt` in the run's artifacts directory.

Each run is preserved under the target repository's private Git directory at `.git/agent-collab/runs/<timestamp>/`, including prompts, responses, final status, and a final patch. The patch includes both tracked changes and non-ignored untracked files. Because artifacts live under `.git`, they do not pollute the working tree.

`run.json` records whether the mirror review was enabled and its chosen node, backend, and skills directory. It also records Claude's reported cost, token counts, and turn count under `usage`, plus wall-clock seconds under `durations`, keyed by the same stage names. Codex and Hermes stages are recorded as `null` under `usage`; malformed Claude JSON also falls back to plain-text output with `null` usage.

## Safety model

Claude runs in plan mode for analysis and review. Codex implementation stages run with workspace-write sandboxing. In the independent mirror review, Hermes runs with all toolsets disabled, Claude runs in plan mode with no tools, and Codex runs in a read-only sandbox. Every mirror backend reviews only the diff text embedded in its prompt. The prompts prohibit commits and pushes, but you should still inspect the resulting diff before committing it.
