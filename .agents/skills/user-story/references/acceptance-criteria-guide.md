# Acceptance Criteria Guide

Acceptance criteria are the contract between the story and the code/tests that follow. In an
AI build chain they matter even more: the `test-creator` and `implement` skills generate
artifacts directly from them, so any ambiguity becomes a wrong test or wrong code.

## The bar: every criterion is executable as a test
If turning a criterion into a test requires guessing a value, threshold, message, or state,
it is not done. Pin down the specifics.

## Use Gherkin (Given / When / Then)
- **Given** — the starting context / preconditions (who, what state).
- **When** — the single action being tested.
- **Then** — the observable, checkable outcome (and only that).

One scenario tests one behaviour. Don't chain unrelated behaviours into one scenario.

## Always include the unhappy paths
A story with only happy-path criteria is under-specified. Cover at least:
- Invalid / malformed input.
- Boundary values (limits, empty, max).
- Permission / auth failures where relevant.
- Empty / first-run state where relevant.

## Make outcomes concrete
| Vague | Concrete |
|-------|----------|
| "responds quickly" | "returns within 2 seconds" |
| "shows an error" | "shows the message 'File exceeds 25 MB limit'" |
| "handles big files" | "accepts files up to 25 MB; rejects above" |
| "works for teachers" | "a user with the Teacher role can …; a Student role cannot" |

## Tie back to requirements
List the FR/NFR IDs each story covers. Before finishing the epic's stories, confirm every
requirement mapped to the epic appears in at least one story's criteria. Uncovered requirement
= missing story.

## Size signal
If a story has more than ~7 scenarios, it is probably two stories. Split along the user goals.
