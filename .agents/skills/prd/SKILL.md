---
name: prd
description: >
  Convert a raw product idea or domain description into a structured, decomposable Product
  Requirements Document (PRD). Use this as the FIRST step of the PDLC chain whenever someone
  has a product idea, concept notes, a whitepaper, or domain details and needs a real PRD.
  Trigger this even when the user just says "write a PRD", "turn this idea into requirements",
  "draft requirements for my product", "I have a product concept", or pastes domain notes and
  asks what to build. The PRD this produces is designed to feed directly into the `prd-decompose`
  skill, so it is structured into numbered, traceable requirements rather than prose.
compatibility: >
  Tool-agnostic (Claude Code or Codex). No external tools required to produce the PRD. The
  output is a single Markdown file that downstream skills (prd-decompose, user-story) consume.
model: opus   # tier: strong (haiku=cheap, sonnet=standard, opus=strong)

---

# prd — Idea to PRD

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


First link in the PDLC chain. It turns an unstructured product idea into a PRD whose
structure is engineered for decomposition: every requirement is numbered, scoped, and
testable, so `prd-decompose` can split it into epics and `user-story` can derive stories
with acceptance criteria. A pretty prose PRD that can't be decomposed is a failure here.

> This skill is product-agnostic. The product could be a K-12 tutor, a payments API, or a
> warehouse app. Read the input, ask only what's essential, and produce the PRD.

## Input it expects
Any of: a one-line idea, a page of concept notes, a domain whitepaper, a transcript, or a
mix. The input may be incomplete — that is normal.

## Output it produces
One file: `PRD.md`, following `templates/PRD.md.tmpl`. It is self-contained and ready for
`prd-decompose`.

## Procedure
1. **Read the input fully.** Extract the problem, the users, and the core value before
   writing anything.
2. **Identify gaps, then ask only blocking questions.** Do not interrogate. Ask at most a
   few questions, and only for things that would change the PRD's shape (target users,
   must-have vs later, any hard constraints like compliance or platform). If the input is
   rich enough, proceed and state your assumptions inline instead of asking.
3. **Draft the PRD** from the template. Rules that make it decomposable:
   - Every functional requirement gets a stable ID: `FR-001`, `FR-002`, …
   - Non-functional requirements get `NFR-001`, … (performance, security, compliance,
     accessibility, scale).
   - Each requirement is **one testable capability**, written so success is checkable.
   - Group requirements under **feature areas** — these become natural epic boundaries.
   - Mark each requirement's release: `MVP` or `Later`. This drives scope decisions
     downstream.
   - Capture measurable **success metrics** (the PRD's definition of "working").
4. **Self-check** against `references/prd-quality-checklist.md`. Fix anything that fails.
   The most common failure is a requirement that bundles three capabilities into one — split
   it.
5. **Write `PRD.md`** to the location given for the PRD in the Project Layout table in `AGENTS.md`
   (current layout: `docs/product/PRD.md`). If no repo/AGENTS.md exists yet — i.e. the PRD is
   being written before `project-scaffold` — write it where the user wants and note that
   `project-scaffold` will place it under `docs/product/`. Tell the user
   it's ready to feed into `prd-decompose`.

## What makes a good requirement (the core rule)
A requirement must be **atomic and testable**. Compare:

- Bad (bundled, vague): "The system handles user content and shows useful feedback."
- Good (atomic, testable):
  - `FR-012` The system accepts a user-uploaded document (PDF, DOCX, TXT) up to 25 MB.
  - `FR-013` The system rejects unsupported file types with a clear error message.
  - `FR-014` The system displays processing status while a document is being prepared.

If you can't write a one-sentence acceptance check for a requirement, it isn't atomic yet.

## Handoff
End by telling the user: the PRD is at `<path>`, it contains N feature areas and M
requirements, and the next step is `prd-decompose` to turn feature areas into Jira epics.

## Reference files
- `templates/PRD.md.tmpl` — the structure to fill in.
- `references/prd-quality-checklist.md` — self-check before finishing.
- `examples/` — a worked PRD generated from a real product input.
