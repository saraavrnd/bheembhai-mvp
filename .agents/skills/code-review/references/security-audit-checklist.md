# Security Audit Checklist (defensive)

Reviewing the team's own code to make it safer before merge. Check each category against the
diff; for any finding, state the risk and propose the safer pattern. This is hardening, not
exploitation — describe what's wrong and how to fix it, not how to abuse it.

## Input handling & injection
- [ ] All new external input (request bodies, params, headers, uploads, file paths) is validated
      and/or parameterised.
- [ ] No string-built SQL/queries — use parameterised queries / the ORM safely.
- [ ] No shell/command construction from user input; if unavoidable, arguments are escaped/listed.
- [ ] File paths from input can't traverse (`../`) outside intended dirs.
- [ ] Template rendering doesn't interpolate untrusted input unsafely.

## Authentication & authorization
- [ ] New endpoints/actions require auth where the NFRs say so.
- [ ] Authorization is enforced (role/ownership checks), not just authentication.
- [ ] No way to act on another user's/tenant's data via an ID in the request without a check.

## Secrets & configuration
- [ ] No hard-coded credentials, API keys, tokens, or connection strings in source.
- [ ] Secrets come from env/secret store; `.env` is gitignored; `.env.example` has placeholders.
- [ ] No secrets in logs or error messages.

## Sensitive data / privacy
- [ ] PII handled per the privacy NFRs (e.g. masking before it reaches a model; encryption at
      rest expectations honoured at the data layer).
- [ ] Sensitive fields not logged, not returned in API responses that don't need them.

## Dependencies
- [ ] New/changed dependencies scanned for known vulnerabilities (run the scanner; cite output).
- [ ] Dependencies pinned to specific versions and from reputable sources.
- [ ] No unmaintained or typo-squatted-looking packages introduced.

## Unsafe patterns
- [ ] No unsafe deserialization of untrusted data (pickle/yaml.load/etc.).
- [ ] Outbound requests built from user input checked for SSRF (no fetching arbitrary internal
      URLs).
- [ ] Expensive/abusable endpoints have rate limiting or are noted as needing it.
- [ ] Errors returned to clients don't leak stack traces, queries, or internal paths.

## Output handling
- [ ] Data reaching a browser is encoded/escaped (XSS); content-type set correctly.
- [ ] Redirects/links built from input are validated (no open redirect).

## How to report
For each finding: category, file:line, the risk in one sentence, and the safer pattern as a
proposed fix routed to `implement`. Use severity (Critical/High/Medium/Low) so the team can
triage. Advisory — nothing here auto-blocks; it informs the human decision.
