# PRD Quality Checklist

Run through this before finishing. The PRD must pass all of these to be decomposable.

## Structure
- [ ] Every functional requirement has a unique stable ID (FR-NNN).
- [ ] Every non-functional requirement has a unique stable ID (NFR-NNN).
- [ ] Every requirement is mapped to exactly one feature area.
- [ ] Every feature area has a code (FA-N) and a one-line description.
- [ ] Every requirement is marked MVP or Later.

## Atomicity & testability (the important part)
- [ ] No requirement bundles multiple capabilities (no "and also" hiding a second feature).
- [ ] Each requirement can be turned into at least one pass/fail acceptance check.
- [ ] Requirements describe WHAT, not HOW (no premature implementation detail).

## Completeness
- [ ] The problem statement names the user and the pain, not the solution.
- [ ] At least one measurable success metric exists.
- [ ] Security/privacy and (if relevant) compliance requirements are present, not assumed.
- [ ] Out-of-scope is explicit, so decomposition doesn't over-reach.

## Common fixes
- A vague requirement → split into 2-3 atomic ones.
- A "the system is fast/secure/usable" line → convert to a measurable NFR.
- A feature area with 0 requirements → either add them or drop the area.
- A feature area with 15+ requirements → likely two epics; consider splitting the area.
