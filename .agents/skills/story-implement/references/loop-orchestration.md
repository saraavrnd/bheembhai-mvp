# Loop Orchestration — gates, loops, escalation

How `story-implement` handles the non-linear parts of the per-story loop. The happy path is
linear; these rules cover when it isn't.

## The two human checkpoints (hard stops)
1. **story-design approval** — after the design note, before any test. Pause; proceed only on
   approval. Revise-and-re-present until approved.
2. **PR review/merge** — after pr-create opens the PR. Pause; humans own approval and merge. This
   skill never merges.

Everything between these is agent-driven.

## The internal loops (self-correcting, no human needed unless asked)

### TDD red/green (inside implement)
`implement` runs the story's tests, edits code, re-runs, until green. This loop is internal to the
`implement` step — the orchestrator just waits for it to report green (full suite + lint).

### test-verify BLOCK loop
After implement, `test-verify` returns PASS or BLOCK.
- PASS → continue.
- BLOCK → read which step it blames:
  - cheating/incomplete code → back to `implement`,
  - weak/missing/over-deleted tests → back to `test-creator`.
  Apply the fix, then RE-RUN test-verify. Repeat until PASS. Never proceed past a BLOCK.

### code-review fix loop
`code-review` is advisory (blocks nothing). Present findings to the user (or apply a standing
policy, e.g. "auto-accept Critical/High"). For accepted findings:
- route to `implement` → code changes → RE-RUN test-verify (changed code must be re-verified) →
  optionally re-run code-review on the delta → continue.
For waived/none: continue directly to pr-create.

## The escalation branch (architecture change)
Two skills can escalate: `story-design` (at design time) and `implement` (if a change surfaces
mid-build). On escalation:
1. STOP the loop — do not bend the architecture to keep moving.
2. Route to `tech-design` (amend mode): propose the delta + a new ADR, get user approval.
3. Resume the loop at `story-design` (re-confirm the note against the amended architecture), then
   continue forward.
Escalation is rare; most stories fit the existing architecture.

## Resuming a story already in progress (idempotency)
If invoked on a story that already has a branch/PR:
- branch exists, no PR → resume from the step after the last completed artifact (e.g. tests exist
  but unverified → run test-verify).
- PR already open → report it; don't open a duplicate. Offer to re-run design-sync or update the
  PR body if needed.
Detect state from the artifacts present (story-design note, test-plan, verification report) and
the branch/PR, then resume at the right step rather than restarting.

## Parallel stories
This skill drives ONE story to a PR. For multiple stories at once, invoke it per story; each runs
on its own `feat/<KEY>` branch. `design-sync` (inside each pr-create) keeps the shared `docs/`
references current and comments on any in-progress story whose design a merge affected — so the
parallel streams re-align without colliding. If a flag arrives for a story you're mid-loop on,
re-run `story-design` for it to re-check against the updated references before continuing.

## The one rule that holds it together
Delegate, don't duplicate. Every step is a call to one of the six skills. The orchestrator owns
ORDER, GATES, and CHECKPOINTS — never the work itself. If behaviour needs to change, change the
underlying skill.
