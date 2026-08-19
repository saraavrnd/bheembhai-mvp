# Design Depth Guide

How much per-story design is enough — and when to stop and escalate.

## The principle: proportionate, not ceremonial
The note exists to make interface and data decisions explicit and reviewable before tests are
written. It is not documentation for its own sake. Match depth to story complexity.

| Story shape | Note depth |
|-------------|-----------|
| CRUD / simple read / config flag | A few lines per section; interfaces are the main content. |
| New endpoint with validation + unhappy paths | Half-page; spell out request/response and error shapes. |
| Touches multiple modules or a new data entity | Half-page; show the data delta and the module interaction. |
| Growing past one page | Story is probably too big — flag for splitting back in user-story. |

## What MUST be concrete (so tests can be written)
- Interface/endpoint signatures: route, method, request body, response body, status codes.
- Error/edge behaviour the acceptance criteria imply (the unhappy-path scenarios).
- Data-model deltas with field names/types, or an explicit "none".

## What to keep OUT
- Full pseudocode or line-by-line logic — that's `implement`'s job.
- System-wide architecture — that's `tech-design`.
- Anything already settled in an ADR (reference it, don't restate or re-decide it).

## The escalation rule (do not bend the architecture silently)
A per-story note may reveal the story needs something the committed architecture didn't plan
for. Signals:
- A new external integration or third-party service.
- A new cross-cutting concern (e.g. a new auth mode, a new background-job mechanism).
- A new service/process, or a change that contradicts an existing ADR.
- A data change that ripples across many modules rather than one.

When you see these, STOP. In the note's "Architecture impact" section, describe the gap and
recommend kicking back to `tech-design` to update the architecture and add an ADR. Let the user
decide. Quietly improvising around the architecture is exactly the failure this loop is designed
to prevent — the cost shows up later as inconsistency no single story is to blame for.

Most stories fit the existing architecture and need no escalation. The rule is for the few that
don't.

## Review is the point
This note is presented for approval before test-creator runs. The realignment that happens at
review — "actually, put that on the existing /materials endpoint, don't add a new one" — is the
cheapest correction available anywhere in the loop. Treat the pause as a feature, not friction.
