# Driving the GitHub + Atlassian MCPs for PRs

`pr-create` opens the PR and updates Jira through connected MCPs, not via hard-coded API calls.
Tool names vary by MCP version — discover them, don't assume.

## GitHub MCP
### Before creating
- List the available GitHub tools (create PR, push, get repo, list branches).
- Confirm the **repo** (owner/name) and the **base branch** (usually `main`). Don't guess.

### Push the branch
- Ensure the branch is `feat/<STORY_KEY>-<slug>` and commits reference the Jira key.
- Use the MCP's push tool; if none exists, fall back to `git push -u origin <branch>`.

### Open the PR
Provide: repo, head (story branch), base (main), title (`<STORY_KEY>: <title>`), body (the
assembled template), labels (if used). **Capture the returned PR number/URL** — you need it for
the Jira link and the hand-off.
- If the call fails with auth/permission, STOP and report; let the user fix access. Don't
  silently fall back in a way that hides a credentials problem.

## Atlassian (Jira) MCP
### Link the PR to the story
- Add the PR URL as a remote link (preferred) or a comment on the story. Use the PR captured
  above so the link is real.

### Transition the story
- Find the story's available transitions; pick the one meaning "In Review" (the name varies per
  workflow — confirm rather than assuming a fixed string).
- Apply the transition; capture the resulting status to report back.
- If the transition isn't available (workflow doesn't allow it from the current state), report
  the current status and the valid transitions rather than forcing it.

## Sequencing & safety
- Order: push → open PR → capture URL → link Jira → transition Jira. Each step depends on the
  previous; don't transition the issue before the PR exists.
- Idempotency: before opening, check whether a PR for this branch already exists (re-runs
  shouldn't create duplicates). If one exists, update/report it instead.
- Stop-on-failure: a failed PR creation or transition halts the step with a clear error; don't
  proceed to later steps on a broken earlier one.

## Fallbacks (never block the loop)
- No GitHub MCP → `git push` + print the PR title/body and compare URL for manual creation.
- No Atlassian MCP → print the PR link and the intended transition for the user to apply.
The branch and PR body are produced regardless of connectivity.

## Boundary
This skill never merges, approves, or closes the PR. Merge is a human decision (or a future
`code-review` skill). It also does not re-run tests — it trusts the PASS from `test-verify`.

## Token-cost discipline (MCP results persist in context)
GitHub and Atlassian MCP results stay in context for the whole session. Keep them small:
- Request minimal fields; don't pull full issue/PR bodies or diffs you don't need.
- Capture only the IDs/URLs/status you must carry forward; drop the rest of the payload.
- Don't re-fetch the same issue/PR; reuse what's in context.
- Operator: `/compact` between stories; disable MCP servers not needed for the run.
See `_SHARED-cost-and-models.md`.
