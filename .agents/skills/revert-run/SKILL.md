---
name: revert-run
description: >
  Revert work produced by a prior skill run: resolve the target run from the local append-only
  ledger, show a human-approved revert preview, then undo the corresponding git-tracked changes
  and record the outcome. Use when the user asks to revert, undo, roll back, or back out a
  story run, branch, or generated artifacts.
compatibility: >
  Tool-agnostic (Claude Code or Codex). Uses git for repo rollback and the local run ledger at
  `.local/beembhai/run-ledger.jsonl` for audit/lookup. No MCP required.
model: haiku   # tier: cheap (haiku=cheap, sonnet=standard, opus=strong)

---

# revert-run — Preview First, Then Roll Back

> **Purpose:** back out the effects of a prior skill run without guessing. The skill resolves the
> target from the shared local ledger, shows exactly what would be reverted, waits for explicit
> confirmation, and then applies the rollback. The ledger is append-only; it is never cleaned or
> rewritten as part of a normal revert.

> **Ledger path:** `.local/beembhai/run-ledger.jsonl` under the repo root. This path is already
> gitignored via `.local/`, so the audit trail stays local-only.

> **Hook helper:** `.agents/skills/_tooling/run_ledger.py` is the canonical writer/reader for the
> shared ledger.
> Future skill steps append events through this helper so revert always has the same structure to
> read.

## When to use
- The user asks to revert, undo, roll back, or back out a previous skill run.
- A completed or in-progress story needs to be abandoned and the repo changes removed.
- The user wants a preview of what would be deleted or restored before any rollback happens.

## What the ledger records
Each append-only entry should carry enough information to reconstruct and revert a run:
- `run_id`
- `parent_run_id` / `supersedes_run_id` when applicable
- `story_key`
- `skill_name`
- `branch`
- `step`
- `timestamp`
- `event` (`started`, `updated`, `completed`, `reverted`, `failed`)
- `changed_files`
- `commits`
- `external_actions`
- `reversible` / `non_reversible` notes

## Procedure

### Step 1 — Resolve the target
Prefer the most specific input available:
1. explicit `run_id`
2. branch name
3. story key + "latest run"

If multiple runs match and the target is not unique, stop and ask the user to choose. Do not
guess.

### Step 2 — Build the revert preview
Read the ledger entries for the target run and summarise:
- the branch and commits involved
- the files/artifacts that would be reverted
- any uncommitted changes that would be discarded
- any external side effects that cannot be safely reverted
- whether the rollback is reversible, partial, or destructive

Show the preview and ask for explicit confirmation before changing anything.

### Step 3 — Apply the rollback
Use the least destructive option that correctly undoes the run:
- For uncommitted work: restore the tracked files and clean only the previewed generated files.
- For committed feature-branch work: prefer `git revert` so history stays auditable.
- Use `git reset --hard` only when the user explicitly approves destructive cleanup and the branch
  is disposable.
- Never touch unrelated local work.

If the run touched external systems, only perform compensating actions that are explicitly known
to be safe. If the side effect is not reversible, say so clearly and leave it recorded.

### Step 4 — Record the revert
Append a new ledger event for the revert outcome:
- target run id
- what was actually reverted
- what remains
- any non-reversible leftovers
- confirmation timestamp

Do not delete or rewrite prior ledger entries.

### Step 5 — Report back
Tell the user:
- what was reverted
- what was not reverted
- whether the branch is now clean
- whether a manual follow-up is needed

## Rules
- Always preview before revert.
- Always require confirmation after the preview.
- Never silently widen the scope beyond the resolved target run.
- Never treat the ledger as disposable scratch space; it is the audit trail.
- If the user wants to revert something that was never recorded, say that the target cannot be
  resolved from the ledger and ask for a branch or commit instead.

## What this skill is not
- Not a product rollback coordinator for external systems that do not have a compensating action.
- Not a replacement for GitHub/Jira recovery flows.
- Not a license to delete audit history.
