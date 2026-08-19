---
name: pr-create
description: >
  Open a GitHub pull request for a verified story via the connected GitHub MCP, fill the PR body
  from the implementation summary and acceptance criteria, link the Jira issue, and transition
  the Jira story to In Review via the Atlassian MCP. Use this as the final step of the per-story
  delivery loop, after test-verify passes. Trigger this even when the user just says "open the
  PR", "create the pull request", "raise the PR for LEARN-21", "push and PR this", or "submit
  for review". This skill only runs on a story that PASSED test-verify; it creates the PR and
  updates Jira, and does NOT merge the PR (human review owns merge).
compatibility: >
  Tool-agnostic skill body (Claude Code or Codex). Requires the GitHub MCP to push the branch and
  open the PR, and the Atlassian (Jira) MCP to link and transition the issue. Falls back to git
  push + a printed PR body if an MCP is unavailable. Runs in the local Docker dev environment.
model: haiku   # tier: cheap (haiku=cheap, sonnet=standard, opus=strong)

---

# pr-create — Open the PR, Close the Loop

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

> **Cost & context:** This skill runs at the `model:` tier in its frontmatter (see
> `_SHARED-cost-and-models.md`). To avoid context bloat: load only the slices of artifacts you
> need (not whole files), don't re-read what's already in context, and for any MCP calls request
> minimal fields and batch reads rather than fetching repeatedly. MCP results persist for the
> session — keep them small.


The hand-off from agent work to human review. After `test-verify` passes, this skill pushes the
story branch, opens a GitHub PR through the **GitHub MCP** with a body assembled from the
artifacts the loop already produced, and moves the Jira story forward via the **Atlassian MCP**.
It deliberately stops at "PR open, ready for review" — merge is a human decision.

> Only run on a story that PASSED test-verify. Opening a PR for unverified work pushes the gate
> downstream onto the reviewer, which is what the loop is designed to avoid.

## Input it expects
- A verified story: `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/verification.md` with verdict PASS.
- The implementation summary and the branch `feat/<STORY_KEY>-...`.
- The story's acceptance criteria and requirement IDs (for the PR body + Jira link).
- The PR template installed by `project-scaffold` (`.github/PULL_REQUEST_TEMPLATE.md`).

## Output it produces
- A pushed branch and an open GitHub PR (via GitHub MCP) with a complete body.
- The Jira story linked to the PR and transitioned to In Review (via Atlassian MCP).
- A short hand-off note with the PR URL and the issue's new status.

## Procedure

### Step 1 — Pre-flight: confirm verification passed + stage any untracked artifacts
Check `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/verification.md` says PASS. If it's missing or BLOCK, stop — route back to
`test-verify` / `implement`. Do not open a PR for unverified work.

Then run `git status` and stage any untracked story artifact files — story-design.md,
test-plan.md, verification.md, code-review.md — that the loop produced but never committed.
These files belong on the branch and must be part of the PR. If any are untracked, add and
commit them now (before design-sync runs in Step 4):

```bash
git status
# for each untracked artifact under docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/:
git add docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/<artifact>.md
git commit -m "chore(<STORY_KEY>): add missing story artifact files"
```

### Step 2 — Push the branch (GitHub MCP)
Ensure the branch is `feat/<STORY_KEY>-<slug>`, committed with a message referencing the Jira
key (e.g. `LEARN-21: upload course materials`). Push it via the GitHub MCP. If the MCP exposes
no push tool, fall back to `git push` and continue.
If the active branch does not match the story key, stop before pushing or opening the PR and
reconcile the branch name first. Do not open a PR from a previous story's branch.

### Step 3 — Assemble the PR body
Fill the installed PR template from the loop's artifacts:
- **Title:** `<STORY_KEY>: <story title>`.
- **What it does:** from the implementation summary.
- **Acceptance criteria covered:** the Gherkin scenarios, checked.
- **Tests:** the verification result (suite green, coverage, lint) pasted/summarised.
- **Traceability:** the FR/NFR IDs.
- **Jira link:** the issue URL/key.
Keep it factual; the artifacts already contain the content.

### Step 4 — Sync the design onto the branch (design-sync), before opening the PR
Run `design-sync` for this story now, while still on the branch, so any reference/`AGENTS.md`
updates are committed to the SAME branch and become part of this PR (they ride with the merge,
not a follow-up). design-sync is proportionate — for most stories it's a near no-op (just a
changelog line); for stories that added an endpoint/field/module-detail it updates the affected
docs. It also flags any in-progress stories the change affects via the Atlassian MCP. If
design-sync detects a structural change that never went through a `tech-design` amendment, pause
and route there first. Commit its doc changes to the branch before proceeding.

### Step 5 — Open the PR (GitHub MCP)
Create the PR against the main branch via the GitHub MCP: head = story branch, base = main,
title + body from Step 3, labels if the repo uses them. The PR now includes both the code and
the design-sync doc commit. Capture the returned **PR URL/number**. Discover the exact tool/params
from the connected MCP rather than assuming; if creation fails (auth/permission), stop and report
so the user can fix access.

### Step 6 — Link and transition Jira (Atlassian MCP)
Via the Atlassian MCP:
- Add the PR URL to the Jira story (remote link or comment).
- Transition the story to **In Review** (confirm the exact transition name in the project;
  some workflows name it differently). Capture the new status.
Do this only after the PR is open, so the link is real.

### Step 7 — Hand off (stop at review)
Report the PR URL, the Jira story's new status, and the design-sync result (what was synced + any
flagged stories). Do NOT merge — human review (the `code-review` skill ran earlier; human owns
approval and merge). Tell the user the loop for this story is complete pending review, the design
references are current as of this PR, and the next story can start at `story-design`.

## If an MCP is unavailable
- No GitHub MCP: `git push` the branch and print the assembled PR title + body for the user to
  paste, plus the compare URL if known.
- No Atlassian MCP: print the PR link and the intended transition for the user to apply.
Never block the whole loop on connectivity — the branch and PR body are still produced.

## Boundaries
- Does not merge, approve, or close the PR.
- Does not re-run or re-verify tests (that was `test-verify`); it trusts the PASS report.
- Does not implement or change code.

## Reference files
- `references/mcp-pr-usage.md` — driving the GitHub + Atlassian MCPs (discover tools, capture
  IDs, transition safely, fallbacks).
- `examples/` — an assembled PR body + the Jira transition for story LEARN-21.
