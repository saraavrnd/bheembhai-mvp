# Driving the Atlassian (Jira) MCP — Fetch & Update (never create)

Unlike `user-story` (which creates issues), `story-review` only ever operates on a story that
already exists. Tool names vary by MCP version — discover what's connected rather than
hard-coding.

## 1. Fetching the story
- Fetch by key, requesting **minimal fields**: summary, description, the acceptance-criteria
  field (if the project has a dedicated one), status, and the parent/epic link.
- Do not fetch comments, changelogs, or attachments unless the story text itself references
  them — those are expensive and rarely needed here.
- If only the story key is known and you need the epic, use the parent/epic-link field from this
  fetch rather than a second query.

## 2. Updating the story (after the user approves the rewrite in Step 5)
- Update the **existing** issue's description and/or acceptance-criteria field with the rewritten
  story text. Use whatever update/edit-issue tool the connected MCP exposes.
- If the project has a dedicated acceptance-criteria field, write there; otherwise fold the
  rewritten Gherkin into the description in the same place `user-story` originally put it.
- Do not change the issue type, parent link, status, or any field the interview didn't touch.
- After updating, verify the write succeeded (re-read summary/status, not the full body) and
  report the confirmed state back to the user — don't assume success from an unchecked call.

## 3. If the MCP is unavailable
Work from the pasted story text, produce the rewritten story and `story-review.md` as normal, and
tell the user the Jira issue itself still needs the same edit applied by hand (or once the MCP is
reconnected). Don't block the rewrite on Jira connectivity.

## 4. Safety rules
- Never create a new issue from this skill — if the given key doesn't resolve, stop and ask the
  user for the correct key rather than creating one.
- If the update call fails (auth, permission, wrong field), stop and report the exact error and
  which field failed, rather than silently leaving the Jira copy stale while the local docs move
  on.

## Token-cost discipline (MCP results persist in context)
- **Minimal fields** on every fetch — never pull full history when key/summary/status/description
  will do.
- **Don't re-fetch** — reuse what's already in context from Step 1 for the rest of the run.
- **Write concisely** — after an update, keep only the confirmed key + status in context, not the
  full echoed payload.
