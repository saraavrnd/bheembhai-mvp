# The TDD Green Phase — making tests pass the right way

The goal is green tests that mean something. It's trivial to make tests pass dishonestly; the
discipline is to make them pass by building the feature.

## Do
- Write the simplest code that makes the failing tests pass AND matches the acceptance criteria.
- Build to the interfaces approved in the story-design note.
- Reuse what the design named instead of re-creating it.
- Implement the unhappy paths the tests assert (exact errors, status codes, no side effects on
  rejection).
- Run the full suite + lint after the story's tests go green, to catch regressions.

## Never (these fake green)
- Delete or comment out an assertion to pass.
- Loosen an expected value (change "File exceeds 25 MB limit" to a substring match) to dodge a
  failure.
- Skip / xfail / disable a test to get a green run.
- Hard-code the test's expected output in the production path so it only works for the test input.
- Catch-and-swallow errors so an unhappy-path test stops failing without the behaviour existing.

If a test is genuinely wrong, that is a deliberate, visible change: fix the story/design, fix the
test with the corrected expectation, and note it. Quietly weakening a test to go green destroys
the value of the whole TDD loop.

## The over-engineering trap
Greenfield tempts you to build "the proper general solution" now. Don't. Make THIS story's tests
pass with minimal, clean code. The next story will pull in what it needs when it exists. A
modular monolith with clear seams (from tech-design) means you can refactor later cheaply — you
don't have to pre-build for it.

## The silent-architecture-change trap
If passing a test seems to need a new external integration, a new service, or a new cross-cutting
mechanism not in the design — stop. That's an architecture change, and it belongs in
`story-design` (escalating to `tech-design` + an ADR), not buried in an implementation. Bending
the architecture quietly is how a codebase drifts away from its own design.

## Definition of green
This story's tests pass + the pre-existing suite still passes + lint is clean + no test was
weakened. Anything less is not green.
