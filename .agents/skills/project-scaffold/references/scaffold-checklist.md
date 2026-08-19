# Scaffold Verification Checklist

Run this before handing off. The scaffold is done only when every box is checked by actually
running the commands — not by assuming they'd work.

## Builds & runs
- [ ] Fresh install succeeds with one documented command.
- [ ] Backend app boots without errors.
- [ ] Frontend app builds/serves without errors.
- [ ] `docker-compose up` brings the whole stack up.

## TDD harness (hard requirement)
- [ ] Unit test runner installed; one trivial unit test passes.
- [ ] Integration test runner installed; one trivial integration test passes.
- [ ] E2E runner installed; the walking-skeleton round-trip test passes.
- [ ] A single command runs the whole suite.

## Quality gates
- [ ] Linter + formatter configured; one command checks the repo and passes.
- [ ] CI runs lint + tests on every PR (local-runner friendly).

## Structure mirrors the design
- [ ] Folders match the components in architecture.md.
- [ ] Each architecture module exists as an (empty) stub home for code.
- [ ] Shared API types from api-contracts/ are placed in the contracts package.

## Hygiene
- [ ] README documents how to install, run, test, and contribute.
- [ ] .gitignore and .env.example present; no secrets committed.
- [ ] PR template installed; branch-naming convention documented (e.g. feat/LEARN-NN-slug).
- [ ] ADRs from tech-design copied into docs/.

## Scope discipline
- [ ] No feature logic implemented — only the walking skeleton.
- [ ] Stale-looking pinned versions flagged to the user rather than silently changed.

## Hand-off
- [ ] Verification result reported (what passed).
- [ ] User told the repo is ready for the per-story loop (test-creator → implement).
