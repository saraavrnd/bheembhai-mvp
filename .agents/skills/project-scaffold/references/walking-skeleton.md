# The Walking Skeleton

A walking skeleton is the **thinnest possible end-to-end slice** that exercises every layer of
the architecture and actually runs. It is the single most valuable thing `project-scaffold`
produces, because it proves the wiring before any feature depends on it.

## What it IS
- One trivial path that touches each tier: e.g. frontend → API → (optionally) DB → back.
- Concretely: a `/health` (or `/ping`) endpoint returning a fixed ok payload, a frontend that
  calls it and renders the result, and one end-to-end test asserting the round trip.
- Containerised: it comes up under `docker-compose up` exactly as real features will.
- Tested: at least one passing test per layer, plus the e2e round-trip test.

## What it is NOT
- Not a feature. No business logic, no real data model behaviour, no auth flows.
- Not pretty. No styling beyond what proves rendering works.
- Not exhaustive. One path, not every endpoint.

## Why it matters for TDD
The walking skeleton is the harness every future failing test runs against. If the skeleton
builds, runs in Docker, and its trivial tests pass, then when `test-creator` writes a failing
test for a real story, a red result means the feature is missing — not that the project is
misconfigured. It removes ambiguity from every later red/green cycle.

## Done test
You can run, in a clean checkout:
1. one command to install,
2. one command to build,
3. one command to test (passes),
4. `docker-compose up` → the skeleton path responds.

If all four hold, the skeleton walks and the repo is ready for the per-story loop.
