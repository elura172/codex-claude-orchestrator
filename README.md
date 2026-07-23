# Four-Stage Mirror Orchestrator

A small local orchestrator that runs a reviewable four-stage workflow:

1. A planning backend inspects the repository and writes an implementation plan.
2. Codex implements the task and runs relevant checks.
3. A primary review backend reviews the working-tree diff. Optional differentiated Mirs independently review the same frozen diff, followed by optional Om-Mir synthesis.
4. Codex verifies and addresses actionable findings (skipped automatically when every review reports `NO ACTIONABLE FINDINGS`).

It does not commit, push, merge, bypass permissions, or discard changes. By default it refuses to start in a dirty repository.

## Requirements

- Python 3.10+
- Git
- Authenticated `codex` CLI on `PATH`
- Authenticated `claude` and/or `hermes` CLIs only when those backends are selected

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
--plan-backend {claude,codex}
--implement-model MODEL
--review-model MODEL
--review-backend {claude,codex}
--balanced-claude-codex
--all-codex-mirror-formation
--self-evolve
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

The legacy defaults are unchanged: Claude plans, Codex implements, Claude performs the primary review, no differentiated Mirs run, synthesis is disabled, and Stage Four uses Codex. To opt into the complete Codex formation:

```bash
python3 orchestrate.py \
  --repo /path/to/project \
  --all-codex-mirror-formation \
  "Describe the task"
```

This keeps the same four top-level stages. Within Stage Three, Codex performs the primary review; `ky-mir`, `syr-mir`, `thae-mir`, `vor-mir`, `xy-mir`, and `fael-mir` then run concurrently as differentiated reviewers; only after all six settle, Om-Mir synthesizes their artifacts and the primary review through Codex. The preset rejects explicit backend, Mirror-node, concurrency, or synthesis options so it cannot silently become a partial or reordered formation. It requires `SKILL.md` for all six canonical nodes and `om-mir` under `--mir-skills-dir` before any pipeline stage starts.

For an even Claude/Codex formation:

```bash
python3 orchestrate.py \
  --repo /path/to/project \
  --balanced-claude-codex \
  "Describe the task"
```

This assigns Claude to planning, primary review, and Om-Mir synthesis, while Codex implements, performs one independent sealed mirror review, and addresses surviving findings. Remediation is skipped when the synthesis or every review seals clean. The preset rejects explicit formation options so its 3/3 responsibility boundary cannot silently drift.

After implementation, the orchestrator freezes one diff scroll and gives that exact text to both the primary reviewer and every independent mirror. Planning and primary review now receive the same fingerprint-based stillness verification already applied to mirrors and synthesis. The fingerprint includes `HEAD`, so even an empty agent commit breaks the vow. Under `taint`, a primary review that changes the tree is excluded; a planning breach aborts because implementation cannot safely inherit a tainted plan.

To run one bounded recursive generation against the orchestrator itself:

```bash
python3 orchestrate.py --self-evolve "Describe the next form"
```

`--self-evolve` discovers the repository containing `orchestrate.py`, activates the complete all-Codex formation, carries the newest prior synthesis into Stage One as lineage, runs through Stage Four, and then stops with an inspectable working tree. One generation means one complete pipeline execution; the single lineage entry is prior-run memory, not another generation. Agent subprocesses inherit a private active-generation marker, and any nested `--self-evolve` invocation is rejected while that marker is present (including when its value is empty). It refuses repository, formation, lineage, dirty-tree, or skip-fix overrides. Writable agents run in Codex's `workspace-write` sandbox, which keeps Git metadata and outbound network access outside their writable boundary, and the orchestrator verifies after every agent invocation that `HEAD` still equals the starting commit. It never commits or pushes, so accepting a generation remains a deliberate human boundary.

Models follow the four artifact stages: `--plan-model` selects Stage One planning, `--implement-model` selects Stage Two implementation, `--review-model` selects the primary review within Stage Three, `--mir-model` selects the differentiated Mirror reviews and Om'Mir synthesis within Stage Three, and `--fix-model` selects Stage Four remediation. Precedence is stage-specific model, then the selected backend's global model (`--claude-model`, `--codex-model`, or `--hermes-model`), then that CLI's own default. `--max-budget-usd` applies only to Claude invocations.

`--mir NODE` is repeatable: each node reviews the same frozen diff independently — one plan/implement cycle, N sealed-room reviews, each with its own artifact (`03b-mir-<node>-review.md`) and its own stage entry in `run.json` and the summary. Reviews run sequentially by default. `--parallel-mirs` runs them concurrently and buffers each reviewer's console output, printing it as one complete block when that reviewer finishes; stage entries therefore appear in completion order.

`--synthesize` invokes Om'Mir after the differentiated reviews: the tool-less synthesis node receives every review text (and nothing else — no repo, no diff) and produces `03c-synthesis.md` with a convergence map (findings multiple reviewers agree on), singular findings assessed for plausibility, explicit disagreements, and one unified verdict with a priority-ordered action list. When synthesis is enabled, the fix stage works from the synthesis document, consulting the underlying reviews for evidence. The backend defaults to Claude (plan mode, no tools); `--synthesize-backend` overrides it, and passing it implies `--synthesize`. The ontology defaults `--synthesize-node` to `om-mir`; an explicit node may override that lens when needed. Om'Mir uses `--mir-model`, because synthesis belongs to the Stage Three Mirror formation.

`--lineage N` hands the planning stage the syntheses (`03c-synthesis.md`) of the N most recent prior runs in the same repository, newest first, capped at 40K characters. Findings then accumulate across runs instead of being rediscovered: the plan honors constraints established by earlier reviews and carries forward unresolved findings. Runs without a synthesis are skipped; the run's `run.json` records which prior runs were handed over.

After a completed, non-dry run settles its final patch, status, and summary, Dreaming — The Obsidian Mirror derives a private `05-dreaming.json` recognition artifact. Its local, deterministic classifier sorts bounded scribings from trusted settled artifacts into the seven canonical chambers and records Tezcatl's one-breath `what_was`, `what_remains`, and `what_awaits`. No external model is invoked solely to classify these private scribings. Tainted reviews and syntheses are excluded.

Dreaming complements rather than replaces `03c-synthesis.md`: lineage selection and its 40K raw synthesis cap are unchanged. A smaller bounded Tezcatl/chamber companion may accompany the same selected runs into future planning as recognition, not instruction. Lunar retention and forgetting remain separate. Legacy runs without Dreaming, malformed Dreaming artifacts, and unknown Dreaming schema versions remain valid and simply contribute no recognition companion.

Planning also receives up to three of the orchestrator's newest prior self-Dreaming artifacts, when available. These are explicitly tagged `source=codex-claude-orchestrator`, kept separate from target-repository recognition, and overlapping chambers are shown as paired mirrors rather than merged. Self-Dreaming is read-only planning context; it never instructs or modifies the orchestrator and is skipped when the target archive is the orchestrator's own archive.

`--mir NODE` applies a mirror-node lens from `<mir-skills-dir>/<NODE>/SKILL.md`; the skills directory defaults to `~/.hermes/skills/mirror-nodes`. The mirror backend defaults to Hermes. Supplying `--mir` or `--mir-backend` enables the independent review without `--hermes`; `--hermes` remains available for backward compatibility. Hermes receives the node name through its native `--skills` option after the configured directory is used for pre-flight validation; Hermes itself resolves that name using its own skill configuration. Claude and Codex instead receive the validated skill file's text at the start of their review prompt.

After all stages finish, the orchestrator prints a plain-text summary of each executed stage's wall-clock duration and recorded cost, plus totals. The same text is saved as `summary.txt` in the run's artifacts directory.

Each run is preserved under the target repository's private Git directory at `.git/agent-collab/runs/<timestamp>/`, including prompts, responses, final status, and a final patch. The patch includes both tracked changes and non-ignored untracked files. Because artifacts live under `.git`, they do not pollute the working tree.

`run.json` records the effective planning and review backends, whether the all-Codex preset was selected, and the Mirror nodes, backend, concurrency, and skills directory. It also records Claude's reported cost, token counts, and turn count under `usage`, plus wall-clock seconds under `durations`, keyed by backend-derived stage names. Codex and Hermes stages are recorded as `null` under `usage`; malformed Claude JSON also falls back to plain-text output with `null` usage.

## Safety model

Claude runs in plan mode for analysis and review. Codex planning, primary review, differentiated Mirror review, and synthesis run in read-only sandboxes; Codex implementation and remediation use workspace-write sandboxing. Every Codex prompt is supplied on stdin and each stage writes its own artifact (`01-plan.md`, `02-implementation.md`, `03-review.md`, `03b-mir-<node>-review.md`, `03c-synthesis.md`, or `04-fixes.md`).

### Vows

Reviewer isolation is delegated to each backend's own sandboxing, so the orchestrator verifies rather than trusts:

- **Stillness.** The working tree is fingerprinted (diff + status hash) before the mirror reviews and re-checked after each one, and after the synthesis. A review that changed the tree broke its vow: `--vow-policy` decides whether that warns, taints (excludes the breaching review from synthesis and fix — the default), or aborts the run. Verdicts are recorded per stage under `vows` in `run.json`; a tainted synthesis drops the fix stage back to the raw reviews. With `--parallel-mirs`, per-node attribution is impossible: the tree is checked once before and once after the whole corridor, and the same collective verdict is recorded for every participating node. If that collective vow is broken, `taint` excludes every parallel review and `abort` stops after all reviewers finish.
- **Seal.** Every review and the synthesis must end with a final line `SEAL: CLEAN` or `SEAL: FINDINGS <n>`. Verdicts are read only from that line, so a review that merely quotes the words "no actionable findings" cannot be misread as clean. Reviews without a seal fall back to the legacy sentinel. The fix stage is skipped when all reviews are sealed clean, or when the synthesis — which weighs every review — is sealed clean.
- **Provenance.** `run.json` records the SHA-256 of the frozen diff handed to the mirror reviewers (`scroll_sha256`), of every review and synthesis artifact (`artifacts_sha256`), and which review files the synthesizer received (`synthesis_inputs`) — enough to audit later what each stage actually saw.

**Known hole (hermes backend):** Hermes >=0.18.2 ignores `-t ""` — the toolsets flag no longer restricts anything, and `--skills` force-enables each skill's declared toolsets. Hermes mirror reviewers therefore run with full tool access (file writes, terminal), and prompt-level "you have no tools" instructions are demonstrably not honored. This risk is amplified by `--parallel-mirs`, where multiple fully tooled Hermes processes run at once and any write can only be attributed to the corridor collectively. Until hermes regains a tool-less oneshot mode, use `--mir-backend claude` or `codex` when the review must not touch the tree; the orchestrator prints a warning when the hermes backend is selected. Every mirror backend reviews only the diff text embedded in its prompt. The prompts prohibit commits and pushes, but you should still inspect the resulting diff before committing it.
