# Drill Checklist — What Counts as a Real Gap

A real gap is something a developer, `test-creator`, or `implement` would have to *guess* to
proceed. A nitpick is stylistic and doesn't change what gets built or tested. Only real gaps earn
a question in the interview.

## 1. Actor & precondition clarity
- Is the actor's role/entity actually defined in `data-model.md` / `architecture.md`? ("teacher",
  "admin", "guest" — does this system model that distinction, or is the story assuming one?)
- Does the "Given" state assume an entity or lifecycle stage exists (a course, an enrollment, a
  processed document) that isn't guaranteed by a prerequisite requirement or an earlier story?
- Is the precondition's *scope* clear — does "a signed-in teacher" mean any teacher, or the
  teacher who owns the resource being acted on? Ownership/permission slips hide here.

**Not a gap:** the role is named and already exists elsewhere in the data model with the same
meaning.

## 2. Acceptance-criteria completeness
- Happy path only? A story with zero unhappy-path scenarios is under-specified by default.
- Missing categories: invalid/malformed input, boundary values (empty, max, zero, one-past-max),
  permission/auth failure, empty/first-run state, concurrent/duplicate action.
- Does a scenario chain more than one behavior into a single Given/When/Then? That's not a
  completeness gap but flag it — it will produce an untestable scenario.

**Not a gap:** an edge case that genuinely cannot occur given the system's actual constraints
(verify against the code/architecture before asking — don't invent hypothetical inputs the UI or
schema already rules out).

## 3. Concreteness of outcomes
Pin down anything a test would have to guess a value for:
| Vague | Ask for |
|-------|---------|
| "quickly" / "fast" | a number and unit |
| "shows an error" | the exact message or a specific error code |
| "handles large files/lists" | the actual limit |
| "notifies the user" | which channel (banner, email, toast) and when |
| "works for teachers" | what specifically differs for a non-teacher |

**Not a gap:** the value is already pinned elsewhere (e.g. a global limit in `data-model.md` or
an ADR) — cite it instead of asking.

## 4. Architecture / data-model fit
- Does satisfying this story need an entity, field, endpoint, or external integration that
  doesn't exist in `architecture.md` / `data-model.md` / `api-contracts/`, and isn't already
  another in-flight story's deliverable (check `story-map.json` / sibling stories first)?
- If yes: this is not a story-ambiguity gap, it's an **architecture-conflict gap**. Don't resolve
  it with an interview answer — flag it for the Step 6 escalation note (route to `tech-design`).

**Not a gap:** the story references something already sketched in `data-model.md` as planned but
not yet built by an earlier story in the same epic (that's normal sequencing, not a conflict).

## 5. Silent NFRs
Does the capability carry an implicit non-functional expectation the story doesn't state —
performance under load, data retention/deletion, accessibility (keyboard/screen-reader), i18n,
audit logging, rate limiting? Only ask if the PRD's NFRs plausibly apply here and the story is
silent on it; don't invent NFRs the PRD never raised.

## 6. Traceability
- Are the FR/NFR IDs the story claims to cover still worded the same in the current `PRD.md`?
- Does the story's acceptance criteria actually exercise every requirement it claims, or just
  gesture at it?

## 7. Overlap / duplication with sibling stories
Check the epic's other stories (`stories.md`, `story-map.json`). Does this story quietly
duplicate behavior another story already owns, or contradict one (e.g. two stories both claim to
define what "processing" status means)? Flag for the user to decide which owns it.

## 8. Scope boundary
Is "Out of scope" present and specific, or missing/generic ("misc edge cases")? A missing scope
boundary is exactly the kind of gap that lets a story balloon during `story-design`.

## Prioritizing the interview
Rank surviving gaps by how badly a wrong guess here would propagate: an architecture-conflict
gap or a missing unhappy-path scenario costs a wrong test and wrong code downstream; a vague
adjective costs a re-ask. Ask the expensive ones first.
