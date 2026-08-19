---
name: code-review
description: >
  Review a verified story's implementation against coding standards, a security audit, the
  acceptance criteria, and maintainability — producing an advisory report of findings by
  severity and proposing concrete fixes, which are routed back to `implement` to apply. Use this
  as the review step of the per-story delivery loop, after test-verify passes and before
  pr-create. Trigger this even when the user just says "review the code", "do a code review for
  LEARN-21", "security review this", "check coding standards", or "audit before PR". This skill
  is advisory (it reports everything, blocks nothing) and does NOT edit code itself — it proposes
  fixes and hands them to `implement`. After any fixes, the loop re-runs `test-verify`.
compatibility: >
  Tool-agnostic (Claude Code or Codex). Reads the diff/branch, the project's standards (from
  project-scaffold / tech-design), the story acceptance criteria, and the verification report.
  May run available static-analysis/linters/dependency scanners in the local Docker dev
  environment to support findings. Does not modify code.
model: opus   # tier: strong (haiku=cheap, sonnet=standard, opus=strong)

---

# code-review — Standards, Security, Acceptance, Maintainability (advisory)

> **Artifact paths:** Resolve all project-artifact locations (architecture, data model, PRD,
> epics, stories, ADRs, API contracts, per-story notes, etc.) from the **Project Layout** table
> in `AGENTS.md` at the repo root — see `_SHARED-project-layout.md`. Do not hardcode paths; the
> current layout places these under `docs/` (e.g. `docs/architecture.md`, `docs/adr/`,
> `docs/api-contracts/`, `docs/product/`). **Per-story artifacts live in that story's own
> folder** `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/` with short, un-suffixed
> filenames (story-design.md, test-plan.md, verification.md, code-review.md, design-sync.md);
> epic artifacts in `docs/product/epics/<EPIC_KEY>/_epic/`. Resolve a story's files under its
> folder only. This skill's own `templates/`, `references/`,
> `examples/`, and `scripts/` stay inside the skill folder and never move under `docs/`.

> **Cost & context:** This skill runs at the `model:` tier in its frontmatter (see
> `_SHARED-cost-and-models.md`). To avoid context bloat: load only the slices of artifacts you
> need (not whole files), don't re-read what's already in context, and for any MCP calls request
> minimal fields and batch reads rather than fetching repeatedly. MCP results persist for the
> session — keep them small.


The quality lens between a verified implementation and the PR. `test-verify` already confirmed
the code is *correct* (tests pass honestly); this skill asks whether it is *good* — does it meet
the team's standards, is it secure, does it actually satisfy the acceptance criteria's intent,
and will it be maintainable. It is **advisory**: it surfaces every finding with a severity so a
human can judge, and it **blocks nothing**. It does **not** edit code — it proposes fixes and
routes them to `implement`, keeping review independent of editing (the same separation that makes
`test-verify` trustworthy).

> Review, don't rewrite. This skill reads and reports. Fixes are applied by `implement`, then the
> loop re-runs `test-verify` so changed code is never shipped unverified.

## Input it expects
- The implementation on branch `feat/<STORY_KEY>-...` and its diff vs main.
- `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/verification.md` = PASS (review correct code, not broken code).
- The story's acceptance criteria + `story-design.md (in the story folder: docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/)` (intent + intended interfaces).
- Project standards: lint/format config, conventions, and any security/compliance NFRs from
  `tech-design` (e.g. encryption, RBAC, PII handling).

## Output it produces
- `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/code-review.md`: findings grouped by the four areas, each with a severity
  (Critical / High / Medium / Low / Nit), location (file:line), why it matters, and a proposed
  fix. Advisory verdict — a summary, never a block.
- A prioritised **fix list** suitable for handing to `implement` (the proposals, ordered by
  severity), plus the explicit note that fixes go through `implement` → `test-verify` before
  `pr-create`.

## The four review areas

### 1. Coding standards & conventions
Beyond what the linter auto-checks: naming, structure, consistency with existing modules, error
handling patterns, logging, configuration use, dead code, duplication, adherence to the project's
documented conventions. Run the project linter/formatter to support findings, but also judge what
tools can't (clarity, fit with surrounding code).

### 2. Security audit (defensive)
Review the code to make it safer. Check the common categories against the diff:
- Input validation & injection (SQL/command/template/path traversal) on any new input handling.
- AuthN/AuthZ: are new endpoints/actions properly access-controlled per the NFRs (e.g. RBAC)?
- Secrets: no hard-coded credentials/keys/tokens; config via env, not source.
- Sensitive data: PII handled per the privacy NFRs (masking, encryption-at-rest expectations).
- Dependencies: new/changed deps scanned for known vulnerabilities; pinned, reputable.
- Unsafe patterns: unsafe deserialization, SSRF on outbound calls, missing rate limits on
  expensive endpoints, verbose errors leaking internals.
- Output handling: encoding/escaping where data reaches a browser or other sink.
Run any available dependency/secret/static scanners and cite their output. This is defensive
hardening of the team's own code — flag, explain the risk, propose the safer pattern.

### 3. Acceptance-criteria intent
`test-verify` confirmed the tests pass; this checks the code honours the *intent* behind the
criteria, not just the literal assertions: are edge cases beyond the written scenarios handled
sensibly, is the unhappy-path behaviour genuinely correct (not just returning the expected string
via a shortcut), does the implementation match what `story-design` approved?

### 4. Maintainability
Readability, function/module size and cohesion, coupling, naming that reveals intent, comments
where the "why" isn't obvious, testability, and whether the change adds avoidable complexity or
tech debt. Flag over-engineering as readily as under-engineering — both cost later.

## Severity scale (for triage, not gating)
- **Critical** — security hole or correctness bug that would cause real harm if merged.
- **High** — standards/security/maintainability issue that should be fixed before merge.
- **Medium** — worth fixing soon; not a merge-blocker on its own.
- **Low / Nit** — minor or stylistic; optional.
Severity guides the human; this skill never converts a severity into an automatic block.

## Procedure
1. **Gather context** — diff, standards/NFRs, acceptance criteria, story-design, PASS report.
2. **Run supporting tools** — linter, formatter check, dependency/secret/static scanners if
   available; capture output as evidence (don't rely on tools alone — review by reading too).
3. **Review each of the four areas** against the diff; record findings with severity, file:line,
   rationale, and a concrete proposed fix.
4. **Write `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/code-review.md`** — findings grouped by area, a severity summary count,
   and an advisory note (e.g. "2 High, 3 Medium — recommend addressing High before PR").
5. **Produce the fix list for `implement`** — proposals ordered by severity, phrased as
   actionable changes. Do NOT apply them here.
6. **Hand off** — present the report. The user/team decides which findings to fix. If any are
   accepted: route them to `implement`, which applies them, then `test-verify` re-runs, then
   `pr-create`. If none are accepted (advisory, all waived): proceed to `pr-create`.

## Why advisory + propose-only
- **Advisory** keeps human judgment in charge — the agent informs, it doesn't gatekeep. You can
  ship a Medium finding knowingly without fighting the tool.
- **Propose, don't edit** keeps the reviewer independent of the implementer. A skill that both
  finds and silently fixes can hide what it changed; routing fixes through `implement` +
  `test-verify` keeps every change visible and verified.

## Boundaries
- Does not edit code (fixes go to `implement`).
- Does not block the PR (advisory only).
- Does not re-run or replace `test-verify`; if fixes are applied, `test-verify` runs again.
- Does not merge.

## Reference files
- `templates/code-review-report.md.tmpl` — the findings report + fix list.
- `references/review-rubric.md` — what to check in each of the four areas.
- `references/security-audit-checklist.md` — the defensive security categories in detail.
- `examples/` — a worked advisory review for story LEARN-21.
