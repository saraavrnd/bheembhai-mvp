# Review Rubric — the four areas

What to look for in each area. Findings get a severity, a file:line, a why-it-matters, and a
proposed fix. Review by reading the diff, not just by trusting tools.

## 1. Coding standards & conventions
- Naming reveals intent; consistent with the module's existing names.
- Structure matches the module/layer conventions from tech-design/project-scaffold.
- Error handling follows the project pattern (no bare excepts, no swallowed errors).
- Logging is present where useful and free of sensitive data.
- Config via the project's mechanism (env/settings), not hard-coded.
- No dead code, no obvious duplication that should be factored.
- Linter/formatter clean (run them; cite output), AND clarity the linter can't judge.

## 2. Security (see security-audit-checklist.md for detail)
- New input validated; no injection vectors.
- New endpoints/actions access-controlled per NFRs (RBAC).
- No secrets in source.
- PII handled per privacy NFRs.
- New deps scanned, pinned, reputable.
- No unsafe patterns (deserialization, SSRF, verbose error leakage, missing rate limits).

## 3. Acceptance-criteria intent
- Code honours the intent behind each scenario, not just the literal assertion.
- Edge cases beyond the written scenarios handled sensibly (or explicitly out of scope).
- Unhappy paths genuinely correct — not a shortcut that returns the expected string.
- Implementation matches the approved story-design interfaces/approach.

## 4. Maintainability
- Functions/modules cohesive and reasonably sized; low coupling.
- Readable without needing the author present.
- Comments explain "why" where non-obvious; no comments restating the code.
- Testable seams; no hidden global state.
- No avoidable complexity. Over-engineering (speculative generality) is a finding too.

## Severity guidance
- Critical: would cause real harm if merged (security hole, data loss, correctness bug).
- High: should fix before merge (real standards/security/maintainability problem).
- Medium: fix soon; not a blocker alone.
- Low/Nit: optional, stylistic.

Severity informs the human; this skill never auto-blocks. Be specific and fair — every finding
needs a location and a concrete proposed fix, so `implement` can act on it without guessing.
