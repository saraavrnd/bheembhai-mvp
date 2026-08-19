---
name: story-implement
description: >
  Drive a single user story end-to-end through the per-story delivery loop by orchestrating the
  six skills in order — story-design → test-creator → implement → test-verify → code-review →
  pr-create (which runs design-sync) — pausing at the two human checkpoints and handling the
  internal gate loops. Use this as the SINGLE entry point to implement a story: it is the thing
  you invoke instead of calling six skills by hand. Trigger this whenever the user says
  "implement <STORY-KEY>", "start LEARN-21", "build this story", "run the loop for <story>",
  "pick up <ticket>", or "take this story to a PR". This skill SEQUENCES and delegates — it does
  not re-implement the work of the six skills; it calls each in turn, enforces the checkpoints,
  and routes gate failures back to the right step.
compatibility: >
  Tool-agnostic (Claude Code or Codex). Delegates to the six per-story skills (each of which uses
  the Atlassian/GitHub MCPs as needed). Resolves artifact paths from the Project Layout table in
  AGENTS.md. Runs against the local Docker Compose dev environment, with integration and e2e
  tests targeting the deployed stack. Assumes the repo is already scaffolded
  (tech-design + project-scaffold done once); if not, it stops and points there first.
model: haiku   # tier: cheap (haiku=cheap, sonnet=standard, opus=strong)

---

# story-implement — One Story, End to End (meta-skill)

> **Artifact paths:** Resolve all project-artifact locations (architecture, data model, PRD,
> epics, stories, ADRs, API contracts, per-story notes, etc.) from the **Project Layout** table
> in `AGENTS.md` at the repo root — see `_SHARED-project-layout.md`. Do not hardcode paths; the
> current layout places these under `docs/` (e.g. `docs/architecture.md`, `docs/adr/`,
> `docs/api-contracts/`, `docs/product/`). **Per-story artifacts live in that story's own
> folder** `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/` with short, un-suffixed
> filenames (story-design.md, test-plan.md, verification.md, code-review.md, design-sync.md);
> epic artifacts in `docs/product/epics/<EPIC_KEY>/_epic/`. Resolve a story's files under its
> folder only. This skill's own `templates/`, `references/`,
> `examples/`, and `scripts/` stay inside the skill folder and never move under `docs/`.

> **Context & efficiency:** This skill runs at the `model:` tier in its frontmatter (see
> `_SHARED-cost-and-models.md`). To avoid context bloat: load only the slices of artifacts you
> need (not whole files), don't re-read what's already in context, and for any MCP calls request
> minimal fields and batch reads rather than fetching repeatedly. MCP results persist for the
> session — keep them small.


The single entry point for implementing a user story. Instead of invoking six skills by hand, you
invoke this one with a story key; it runs the per-story loop in order, stops at the two human
checkpoints, and self-corrects through the internal gate loops. It is an **orchestrator**: it
sequences and delegates to the six skills, it does not redo their work.

> Mental model: this skill is the conductor. `story-design`, `test-creator`, `implement`,
> `test-verify`, `code-review`, and `pr-create` are the musicians. The conductor sets order and
> timing and stops for the two reviews — it doesn't play the instruments itself.

## Input it expects
- A story key (e.g. `LEARN-21`) or the story itself.
- A scaffolded repo with `AGENTS.md` + the `docs/` tree present (from the one-time
  `tech-design` → `project-scaffold` bootstrap).

## Preconditions (check first, then proceed)

Then check:
1. **Repo scaffolded?** If there's no `AGENTS.md` / `docs/` tree, the project hasn't been
   bootstrapped — stop and tell the user to run `tech-design` → `project-scaffold` once first.
   This meta-skill is for the per-story loop, not the one-time bootstrap.
2. **Story exists?** Confirm the story key resolves to a real Jira story (via Atlassian MCP) with
   acceptance criteria. If not, point back to `user-story`.
3. **Dependencies resolved?** Check the epic's sequence (`docs/product/epics/<EPIC>/_epic/epic-sequence.json`
   from `epic-sequence`, and/or Jira "blocks" links). If the story is blocked by another story that
   isn't merged yet, STOP and tell the user which prerequisite must land first — don't build a
   consumer before its producer. If no sequence exists for the epic yet, recommend running
   `epic-sequence` first; the user may override and proceed for a story with no obvious
   dependencies.
4. **Branch hygiene?** Resolve the expected feature branch name for the story key:
   `feat/<STORY_KEY>-<short-slug>`. If the workspace is already on a different story branch,
   do not continue on it by accident. Create or switch to the correct branch before Step 1 of
   `implement`, or stop and ask the user to reconcile the branch state first.

### Picking the next story (optional convenience)
If invoked without a specific key (e.g. "implement the next story in LEARN-11"), read
`docs/product/epics/<EPIC>/_epic/epic-sequence.json` and pick the next eligible story — the lowest-wave story whose
`depends_on` are all merged. If several are eligible (same wave, independent), list them so the
user/team can assign them in parallel.

### Mark the story In Progress (do this once preconditions pass)
Before running `story-design`, transition the Jira story to **In Progress** via the Atlassian MCP.
This is the "picking it up now" moment — it makes the board reflect reality so the team (and
parallel workers) can see the story is actively being worked, not still in the backlog. Rules:
- Only after the preconditions above pass (repo scaffolded, story exists, dependencies resolved) —
  don't mark In Progress a story you're going to stop on.
- Confirm the exact transition name in the project (workflows vary: "In Progress", "In Dev",
  "Start Work"); if the story is already In Progress, leave it (idempotent — don't error).
- If the Atlassian MCP isn't available, note the intended transition and continue; this is a
  status convenience, not a hard gate.
The full lifecycle this skill drives:
`To Do → (here) In Progress → … → (pr-create) In Review → (human) merge/Done`.

The orchestrator also owns the shared run ledger hook: append a `started` event when a run
begins, append step outcomes as the six delegate skills progress, and append a terminal event
before handing off. Use `.agents/skills/_tooling/run_ledger.py` so the record shape stays
uniform for revert.

## The two human checkpoints (this skill PAUSES at both)
- **Checkpoint 1 — story-design approval.** After `story-design` produces the note, STOP and get
  the user's approval before any tests are written. This is the cheapest place to realign.
- **Checkpoint 2 — PR review/merge.** After `pr-create` opens the PR, STOP. Human review owns
  approval and merge. This skill never merges.

Everything between the two checkpoints is agent-driven, with the internal loops below.

## The sequence (what this skill runs)

```
[Checkpoint 1]
story-design ──approve?──► test-creator ──► implement ──► test-verify ──► code-review ──► pr-create
     ▲ │escalation                                │BLOCK         ▲              │accepted fixes
     │ ▼                                          ▼              │              ▼
 tech-design(amend)                          implement/test-creator        implement → test-verify
                                                                                          [Checkpoint 2]
```

### Step 1 — story-design  →  Checkpoint 1
Run `story-design` for the story **to completion now** — actually read the inputs and WRITE the
design note (`story-design.md`) in this turn. Do not merely describe what the skill will do, do not
announce it and wait — there is nothing to wait for; this agent is the one producing the note.
**Only pause AFTER the note exists.** Pausing at Checkpoint 1 means "the note is written, here it
is, approve it?" — never "I'm about to start, shall I?". A pause before the artifact exists is a
bug: produce first, then pause.
Before presenting the note, confirm it names the durable system of record for any persisted core
entity the story touches. If the note can be satisfied by a file-backed, in-memory, or other
container-local shim instead of the architecture's source of truth, send it back to
`story-design` (or `tech-design` if the gap is architectural) before moving on.

The downstream `implement` step creates the story branch from `origin/main`, so any local
uncommitted or unpushed work that should be included in the story must be committed or stashed
before the loop starts. That keeps every story branch based on the latest merged remote main.

It produces the thin design note and may **escalate** (the story needs an architecture change).
Handle the two outcomes:
- **Normal:** once the note is written, PRESENT it and PAUSE for approval (Checkpoint 1). On
  approval, continue. On change requests, have `story-design` revise and re-present (loop here
  until approved).
- **Escalation:** do NOT proceed. Route to `tech-design` (amend mode) for the architecture change
  + ADR, get that approved, then resume `story-design`. Only continue once the design note is
  approved.

### Step 2 — test-creator (TDD red)
Run `test-creator` against the approved design + acceptance criteria. It writes failing tests and
confirms they fail for the right reason. If it reports it can't (e.g. ambiguous criteria), pause
and resolve before continuing.

### Step 3 — implement (TDD green)  [internal loop with test-creator]
Run `implement` to make the tests pass on the `feat/<KEY>` branch. The tight red/green iteration
lives inside `implement`. If `implement` hits something needing a design change, it escalates the
same way as Step 1 (back to story-design/tech-design) — handle it, then resume.

### Step 4 — test-verify (honest-green gate)  [BLOCK loop]
Run `test-verify`. Two outcomes:
- **PASS:** continue to code-review.
- **BLOCK:** route the fix to the owning step it names (`implement` for cheating code,
  `test-creator` for weak/missing tests), apply, then **re-run `test-verify`**. Loop until PASS.
  Do not skip this gate or proceed on a BLOCK.

> **↻ Compact here — PAUSE and tell the user.** `/compact` is a CLI command only the USER can run;
> the agent cannot invoke it. So at this point the agent must STOP and surface an explicit message
> to the user, e.g.: *"test-verify PASSED. The implement↔verify loop has built up a large context
> that the rest of the story will keep re-reading. Please run `/compact` now, then tell me to
> continue to code-review."* Then WAIT for the user to compact and resume. Don't proceed silently
> — if the user skips it, that's their call, but the prompt must be shown. The verification result
> lives in `verification.md` and code-review reads the diff + standards, so compacting loses
> nothing.

### Step 5 — code-review (advisory)  [fix loop]
Run `code-review`. It reports findings by severity and proposes fixes — it blocks nothing.
- **Findings accepted (by the user, or per a standing policy):** route them to `implement`, which
  applies them; because code changed, **re-run `test-verify`** (Step 4) before continuing. Then
  proceed.
- **All waived / no findings:** continue.
Surface the report to the user so they can decide; honour their call on which findings to fix.

### Step 6 — pr-create (+ design-sync)  →  Checkpoint 2
Run `pr-create`, which first runs `design-sync` (committing any reference/`AGENTS.md` updates to
the branch and flagging affected in-progress stories), then opens the GitHub PR via MCP and
transitions the Jira story to In Review. STOP at the open PR — human review owns merge.

### Step 7 — Report
Summarise the run: the story key, the checkpoints hit, any loops taken (BLOCKs, accepted review
fixes, escalations), the PR URL, and the design-sync result (synced/no-op + flagged stories).
Include the Jira **status lifecycle** for the story this run drove: In Progress (set at start) →
In Review (set by pr-create) — confirming both transitions actually happened. Tell the user the
loop for this story is complete pending review, and the next story is initiated by invoking this
skill again with the next key.

## Orchestration rules
- **Execute, don't narrate.** When delegating to a skill, actually RUN it in that turn — read the
  inputs, do the work, produce the artifact. Do not describe what the skill "will" do and then wait
  ("the skill will read the story and produce a note… waiting for it to complete"). There is no
  separate process to wait for — this agent is the one doing it. Narrate-then-stall forces the user
  to nudge the flow forward at every step, which defeats the orchestrator. The only legitimate
  pauses are the two human checkpoints (after the design note exists; after the PR is open) and the
  user-facing `/compact` prompts — all of which come AFTER the work of that step is done, never
  before.
- **Delegate, don't duplicate.** Call each skill; never re-implement its logic inside this one.
  If a skill's behaviour needs to change, change that skill, not this orchestrator.
- **Honour the gates.** A `test-verify` BLOCK stops forward progress until resolved. The
  story-design and PR checkpoints are hard human stops.
- **Stop on escalation.** An architecture change pauses the loop for `tech-design` (amend) +
  approval; never let a story bend the architecture to keep the loop moving.
- **One story at a time.** This skill drives a single story to a PR. For parallel work, invoke it
  separately per story (each on its own branch); `design-sync` keeps the shared `docs/` current
  and flags overlaps at merge.
- **Commit artifacts immediately after each skill produces them.** story-design.md,
  test-plan.md, verification.md, and code-review.md are written to the branch by their
  respective skills — `git add` and commit each one as soon as it exists. Do not leave them
  untracked; untracked files follow the developer across branch switches and can be silently
  omitted from the PR. The `pr-create` pre-flight runs `git status` as a safety net, but the
  clean habit is: artifact written → staged and committed in the same step.
- **Idempotent-minded.** If re-invoked on a story already mid-loop (branch/PR exists), detect the
  current state and resume from the right step rather than starting over or duplicating a PR.
- **Branch-accurate.** Every story run must end up on the branch for that story key. If the active
  branch belongs to a different story, stop the run or switch to the correct branch before work
  proceeds.
- **Compact context at checkpoints.** In a single uninterrupted session, context only grows —
  every step re-reads everything before it. **`/compact` is a CLI command only the USER can run —
  the agent cannot invoke it.** So at each flush point the agent must PAUSE and explicitly ask the
  user to run `/compact`, then wait for them to resume. Surface a clear message (e.g. "Large
  context built up; please run `/compact`, then tell me to continue") rather than silently
  continuing or pretending to compact. The flush points:
  - **After each completed story** — tell the user to start the next story in a fresh/compacted
    session; story 2 must not carry story 1's tool results and file reads.
  - **Within a story, after the implement↔test-verify loop reaches PASS** (see Step 4's callout) —
    the TDD iteration is the biggest throwaway-context generator; prompt the user to `/compact`
    before `code-review`.
  - **After a `test-verify` BLOCK loop resolves** — prompt to drop the failed-attempt context.
  - **After heavy MCP reads** (e.g. `epic-sequence`'s Jira pull) — prompt to compact once the
    distilled result is captured to an artifact.
  Compacting is safe because every step reads its inputs from ARTIFACTS (story-design.md,
  test-plan.md, verification.md) — the next step re-reads the small artifact it needs, not the
  whole conversation history. If the user chooses to skip a compaction, respect that; the
  requirement is that the prompt is SHOWN, not that compaction is forced. This is what keeps a
  story from accumulating unnecessary context churn.
  - **Make resumption clean:** before asking the user to compact, state the resume point
    concretely — the story key, what's done (e.g. "tests green, verification.md written"), and the
    next step ("continue to code-review"). After the user compacts, they say continue and the
    agent picks up from that artifact-backed state. The idempotency rule already lets the agent
    detect current state from the artifacts/branch, so a compaction mid-story never loses the
    thread.

## What this skill is NOT
- Not the bootstrap — `tech-design` / `project-scaffold` run once before any story; this is the
  per-story loop only.
- Not a merger — it stops at the open PR (Checkpoint 2).
- Not a replacement for the six skills — it orchestrates them.

## Reference files
- `references/loop-orchestration.md` — the gate/loop/escalation handling in detail.
- `templates/story-run-report.md.tmpl` — the end-of-run summary.
- `examples/` — a worked end-to-end run for story LEARN-21 (including one test-verify BLOCK and an
  accepted review fix, to show the loops in action).
