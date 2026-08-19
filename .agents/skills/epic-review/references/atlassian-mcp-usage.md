# Driving the Atlassian (Jira) MCP — Fetch & Update (never create), batched

Unlike `user-story` / `prd-decompose` (which create issues), `epic-review` only ever operates on
an epic and stories that already exist. Tool names vary by MCP version — discover what's
connected rather than hard-coding. This skill's blast radius is wider than `story-review`'s (one
epic, many stories), so batching matters more here.

## 1. Fetching the epic and its stories
- Fetch the epic by key, requesting **minimal fields**: summary, description, goal, and the
  child-story links.
- Fetch all child stories in as few batched calls as the connected MCP allows, requesting minimal
  fields per story: summary, description, the acceptance-criteria field (if the project has a
  dedicated one), status. Do this once, up front — don't re-fetch a story you've already loaded
  later in the same run.
- Do not fetch comments, changelogs, watchers, or attachments on the epic or any story unless the
  text itself references them.

## 2. Updating after the user approves the rewrite (Step 5)
- Update the epic's description only if the epic-level goal/description text itself changed.
- For each touched story, update its **existing** issue's description and/or acceptance-criteria
  field with the rewritten text — same rule as `story-review`: if the project has a dedicated
  acceptance-criteria field, write there; otherwise fold the rewritten Gherkin into the
  description in the same place `user-story` originally put it.
- Batch these writes where the MCP supports it rather than issuing one call per story in a loop
  with no batching, but never skip verifying each write.
- Do not change any issue's type, parent/epic link, status, or any field the interview didn't
  touch.
- After updating, verify each write succeeded (re-read summary/status per issue, not the full
  body) and report the confirmed set of updated keys back to the user.

## 3. If the MCP is unavailable
Work from `stories.md` / `story-map.json` on disk, produce the rewritten stories and
`epic-review.md` as normal, and tell the user the same edits still need to be applied to the Jira
epic and its child stories by hand (or once the MCP is reconnected). Don't block the rewrite on
Jira connectivity.

## 4. Safety rules
- Never create a new epic or story from this skill — if a given key doesn't resolve, or a child
  story appears in `stories.md` but not in Jira (or vice versa), stop and ask the user to
  reconcile rather than creating or deleting anything.
- If an update call fails (auth, permission, wrong field) for one story in a batch, stop and
  report exactly which key failed and why — don't let the local docs move on while that one
  Jira issue is left stale, and don't silently retry-skip it.

## Token-cost discipline (MCP results persist in context, and this skill loads MORE issues than story-review)
- **Minimal fields** on every fetch, for the epic AND every child story.
- **One batched read for all stories**, not one fetch per story key.
- **Don't re-fetch** — reuse what's already in context for the rest of the run.
- **Write concisely** — after updates, keep only the confirmed key + status per issue in context,
  not each full echoed payload; summarize the batch rather than pasting every response.
