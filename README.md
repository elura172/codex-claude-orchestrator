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
--plan-model MODEL
--implement-model MODEL
--review-model MODEL
--fix-model MODEL
--hermes
--hermes-model MODEL
--mir NODE                 (repeatable)
--mir-backend {hermes,claude,codex}
--mir-model MODEL
--parallel-mirs
--mir-skills-dir PATH
--synthesize
--synthesize-backend {hermes,claude,codex}
--synthesize-node NODE
--lineage N
--vow-policy {warn,taint,abort}
--max-budget-usd AMOUNT
--stage-timeout-seconds SECONDS
--skip-review-fix
--allow-dirty
```

`--stage-timeout-seconds` applies a wall-clock timeout to each agent invocation. By default, agent invocations have no timeout.

Models follow the four artifact stages: `--plan-model` selects Stage One planning, `--implement-model` selects Stage Two implementation, `--review-model` selects the primary review within Stage Three, `--mir-model` selects the differentiated Mirror reviews and Om'Mir synthesis within Stage Three, and `--fix-model` selects Stage Four remediation. The existing `--claude-model`, `--codex-model`, and `--hermes-model` options remain backend-wide defaults and are used whenever a stage-specific override is omitted.

`--mir NODE` is repeatable: each node reviews the same frozen diff independently — one plan/implement cycle, N sealed-room reviews, each with its own artifact (`03b-mir-<node>-review.md`) and its own stage entry in `run.json` and the summary. Reviews run sequentially by default. `--parallel-mirs` runs them concurrently and buffers each reviewer's console output, printing it as one complete block when that reviewer finishes; stage entries therefore appear in completion order.

`--synthesize` invokes Om'Mir after the differentiated reviews: the tool-less synthesis node receives every review text (and nothing else — no repo, no diff) and produces `03c-synthesis.md` with a convergence map (findings multiple reviewers agree on), singular findings assessed for plausibility, explicit disagreements, and one unified verdict with a priority-ordered action list. When synthesis is enabled, the fix stage works from the synthesis document, consulting the underlying reviews for evidence. The backend defaults to Claude (plan mode, no tools); `--synthesize-backend` overrides it, and passing it implies `--synthesize`. The ontology defaults `--synthesize-node` to `om-mir`; an explicit node may override that lens when needed. Om'Mir uses `--mir-model`, because synthesis belongs to the Stage Three Mirror formation.

`--lineage N` hands the planning stage the syntheses (`03c-synthesis.md`) of the N most recent prior runs in the same repository, newest first, capped at 40K characters. Findings then accumulate across runs instead of being rediscovered: the plan honors constraints established by earlier reviews and carries forward unresolved findings. Runs without a synthesis are skipped; the run's `run.json` records which prior runs were handed over.

`--mir NODE` applies a mirror-node lens from `<mir-skills-dir>/<NODE>/SKILL.md`; the skills directory defaults to `~/.hermes/skills/mirror-nodes`. The mirror backend defaults to Hermes. Supplying `--mir` or `--mir-backend` enables the independent review without `--hermes`; `--hermes` remains available for backward compatibility. Hermes receives the node name through its native `--skills` option after the configured directory is used for pre-flight validation; Hermes itself resolves that name using its own skill configuration. Claude and Codex instead receive the validated skill file's text at the start of their review prompt.

After all stages finish, the orchestrator prints a plain-text summary of each executed stage's wall-clock duration and recorded cost, plus totals. The same text is saved as `summary.txt` in the run's artifacts directory.

Each run is preserved under the target repository's private Git directory at `.git/agent-collab/runs/<timestamp>/`, including prompts, responses, final status, and a final patch. The patch includes both tracked changes and non-ignored untracked files. Because artifacts live under `.git`, they do not pollute the working tree.

`run.json` records whether the mirror review was enabled and its chosen node, backend, and skills directory. It also records Claude's reported cost, token counts, and turn count under `usage`, plus wall-clock seconds under `durations`, keyed by the same stage names. Codex and Hermes stages are recorded as `null` under `usage`; malformed Claude JSON also falls back to plain-text output with `null` usage.

## Safety model

Claude runs in plan mode for analysis and review. Codex implementation stages run with workspace-write sandboxing. In the independent mirror review, Claude runs in plan mode with no tools and Codex runs in a read-only sandbox.

### Vows

Reviewer isolation is delegated to each backend's own sandboxing, so the orchestrator verifies rather than trusts:

- **Stillness.** The working tree is fingerprinted (diff + status hash) before the mirror reviews and re-checked after each one, and after the synthesis. A review that changed the tree broke its vow: `--vow-policy` decides whether that warns, taints (excludes the breaching review from synthesis and fix — the default), or aborts the run. Verdicts are recorded per stage under `vows` in `run.json`; a tainted synthesis drops the fix stage back to the raw reviews. With `--parallel-mirs`, per-node attribution is impossible: the tree is checked once before and once after the whole corridor, and the same collective verdict is recorded for every participating node. If that collective vow is broken, `taint` excludes every parallel review and `abort` stops after all reviewers finish.
- **Seal.** Every review and the synthesis must end with a final line `SEAL: CLEAN` or `SEAL: FINDINGS <n>`. Verdicts are read only from that line, so a review that merely quotes the words "no actionable findings" cannot be misread as clean. Reviews without a seal fall back to the legacy sentinel. The fix stage is skipped when all reviews are sealed clean, or when the synthesis — which weighs every review — is sealed clean.
- **Provenance.** `run.json` records the SHA-256 of the frozen diff handed to the mirror reviewers (`scroll_sha256`), of every review and synthesis artifact (`artifacts_sha256`), and which review files the synthesizer received (`synthesis_inputs`) — enough to audit later what each stage actually saw.

**Known hole (hermes backend):** Hermes >=0.18.2 ignores `-t ""` — the toolsets flag no longer restricts anything, and `--skills` force-enables each skill's declared toolsets. Hermes mirror reviewers therefore run with full tool access (file writes, terminal), and prompt-level "you have no tools" instructions are demonstrably not honored. This risk is amplified by `--parallel-mirs`, where multiple fully tooled Hermes processes run at once and any write can only be attributed to the corridor collectively. Until hermes regains a tool-less oneshot mode, use `--mir-backend claude` or `codex` when the review must not touch the tree; the orchestrator prints a warning when the hermes backend is selected. Every mirror backend reviews only the diff text embedded in its prompt. The prompts prohibit commits and pushes, but you should still inspect the resulting diff before committing it.
