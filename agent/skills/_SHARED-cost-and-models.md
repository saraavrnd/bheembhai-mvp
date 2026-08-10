# Shared Convention — Model Tiers & Token-Cost Discipline

> Referenced by every PDLC skill. Not a skill itself. Two goals: route each skill to the right
> (cheapest sufficient) model, and stop skills from bloating context.

## 1. Model tiers → Claude Code aliases (what goes in `model:` frontmatter)
Claude Code's `model:` field accepts **aliases**, not arbitrary tier words and not full model
strings. Putting a tier word like `cheap` in `model:` causes the error: "the selected model
(cheap) may not exist or you may not have access to it." So each skill's frontmatter uses the
**alias**; the conceptual tier is kept in an inline comment for humans.

| Tier (concept) | `model:` value in frontmatter | Maps to (current) |
|----------------|-------------------------------|-------------------|
| `cheap` | `haiku` | Claude Haiku 4.5 |
| `standard` | `sonnet` | Claude Sonnet 4.6 |
| `strong` | `opus` | Claude Opus 4.8 |
| (no switch) | `inherit` | whatever the session is on |

Aliases auto-track the current version of each tier, so you don't edit skills when a newer
Haiku/Sonnet/Opus ships. Frontmatter looks like:
```yaml
model: haiku   # tier: cheap (haiku=cheap, sonnet=standard, opus=strong)
```

**Behaviour:** a skill's `model:` overrides the session's current model for that skill's execution
(intentional — that's the point). `inherit` or omitting the field = run on the session model.

**If your client does NOT honour per-skill `model:`** (some Codex setups): the alias is then just
documentation — run strong-tier skills (`tech-design`, `story-design`, `code-review`, `prd`) in an
Opus session and the rest in a cheaper session, switching with `/model`.

**Ollama / non-Anthropic later:** an alias won't point at a local model on its own. Either
configure your client's model routing to map an alias to a local endpoint, or switch the session
model manually per tier. Revisit this file when wiring Ollama in.

## 2. Per-skill tier assignments (rationale)
| Skill | Tier | Why |
|-------|------|-----|
| prd | strong | turning vague ideas into atomic, testable requirements is high-judgment |
| prd-decompose | standard | grouping + Jira creation; moderate judgment |
| user-story | standard | slicing + acceptance criteria; moderate judgment |
| tech-design | strong | architecture & stack trade-offs — the highest-stakes reasoning |
| project-scaffold | standard | mostly mechanical, but wiring choices need some judgment |
| story-design | strong | the per-story "how" + escalation calls drive everything downstream |
| test-creator | cheap | translating acceptance criteria into tests is largely mechanical |
| implement | standard | real coding; bump to strong for genuinely complex stories |
| test-verify | cheap | running suites + checklist inspection; low reasoning |
| code-review | strong | security + maintainability judgment is the whole point |
| design-sync | cheap | diffing + targeted doc edits; mechanical |
| pr-create | cheap | orchestration + MCP calls; almost no reasoning |
| epic-sequence | cheap | graph build + topological sort is mechanical (it reads a lot, but doesn't reason hard) |
| story-implement | cheap | pure orchestration; the heavy thinking happens in the skills it calls |

Note: a meta/orchestrator skill (`story-implement`) should be `cheap` — it sequences and delegates;
the sub-skills it invokes carry their own tiers and do the expensive thinking. Don't run the
orchestrator on a strong model "to be safe" — that pays top price for traffic-directing.

## 3. Read slices, not whole documents (prevents the 1M-context fallback)
The biggest context bloat is loading full artifacts when only a slice is needed. Rules:
- Load the SECTION you need, not the whole file (e.g. a story-design note's produces/consumes
  lines, not the entire note; the data model's entity NAMES, not the full schema).
- Don't re-read a file you already have in context this session.
- Summarise long inputs to the fields you act on, then work from the summary.
- For many items (e.g. every story in an epic), pull a compact list first; fetch full detail only
  for the specific items you must inspect.

## 4. Keep MCP results small (the Atlassian footprint)
MCP tool results persist in context for the whole session, so every Jira call accumulates. For any
skill that uses the Atlassian/GitHub MCP:
- **Request minimal fields.** Ask only for the fields you need (key, summary, status, links) — not
  full issue bodies/changelogs.
- **Batch reads.** One query that returns the epic's stories beats N single-issue fetches.
- **Don't re-fetch.** Reuse what's already in context; don't poll the same issue repeatedly.
- **Write concisely.** When creating/transitioning issues, don't echo back the whole returned
  payload into context — keep the key and status, drop the rest.
- **Operator tips:** `/compact` between stories to flush accumulated tool results; disable MCP
  servers a skill doesn't need for that run.

## 4b. Context compaction — the dominant cost lever (measured)
On a real run, a single story with NO compaction cost ~$5.80, of which ~$3.2 was **cache-read**
tokens (10.7M of them) — context being re-read on every step. In one uninterrupted session,
context only grows and every step re-reads everything before it, so re-reads compound into
millions of tokens. This usually dwarfs model-tier and output costs. Mitigate:
- **Fresh/compacted session per story** — never carry one story's context into the next.
- **`/compact` within a story after the implement↔test-verify loop PASSES** — the TDD iteration is
  the biggest throwaway-context generator; flush it before code-review.
- It's safe because hand-offs are artifact-based: each step re-reads the small artifact it needs
  (story-design.md, verification.md), not the whole conversation history.
A story that costs multiples of expectation is almost always un-compacted context, not the model.

## 5. Quick triage when a skill errors on context size or a story costs too much
1. **Check the per-model + cache split** (`_tooling/story_tokens.py`). High **cache-read** = context
   re-read too many times → COMPACT (section 4b). This is the most common cause of a pricey story.
2. Is it loading whole files it only needs slices of? (Lever 3 above.)
3. Is it carrying accumulated MCP results? `/compact`, or batch/minimise the calls.
4. Only after those: consider the larger-context model — a fallback, not the fix.
