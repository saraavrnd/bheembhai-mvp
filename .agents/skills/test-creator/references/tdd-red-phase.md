# The TDD Red Phase — what a valid failing test looks like

In TDD, tests are written first and must fail before implementation. But not every failure is a
valid red. Distinguishing good red from bad red is the core skill here.

## Good red (the test is valid; the feature is just missing)
- An assertion fails because the value/state isn't what the criteria require.
- An endpoint returns 404 because the route doesn't exist yet.
- A function/class is referenced and is missing or returns a placeholder.
- The behaviour under test is genuinely not implemented.

These mean: the harness works, the test is wired correctly, and `implement` has a clear target.

## Bad red (the test itself is broken — fix it before proceeding)
- ImportError / ModuleNotFound — wrong path or the test imports something that should already
  exist from the scaffold.
- Fixture/setup error — the test never reaches its assertion.
- Syntax error / typo in the test.
- The runner can't find the test at all.

A test that ERRORS is not a valid red. Fix it so it FAILS cleanly on the assertion instead.

## Why the walking skeleton matters here
Because `project-scaffold` proved the harness runs (trivial tests green), any red you see now is
attributable to the feature, not the plumbing. If you get a harness-level error, that's a
regression in setup, not a normal red — stop and fix the harness.

## A test that passes immediately is a smell
If a brand-new test passes before any implementation:
- the behaviour may already exist (then the story may be partly done — confirm), or
- the test is too weak (asserts something trivially true). Strengthen it to actually encode the
  acceptance criterion.

## Pin expectations to the acceptance criteria
Assert the exact status code, error message, and state the criteria specify. "Then it is
rejected with the message 'File exceeds 25 MB limit'" becomes an assertion on that exact string
and a 413 — not a loose "some error occurred". Vague assertions let vague implementations pass.

## Don't implement to go green here
The moment you write production code to satisfy a test, you've left test-creator and entered
`implement`. Keep them separate so the red phase stays a clean, reviewable checkpoint.
