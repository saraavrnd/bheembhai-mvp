# Shared Convention — Resolving Project Artifact Paths

> This convention is referenced by every PDLC skill. It is NOT a skill itself.

All PDLC skills read and write **project artifacts** (architecture, data model, PRD, epics,
stories, ADRs, API contracts, per-story notes, etc.) at locations declared in the **Project
Layout** table inside `AGENTS.md` at the repo root.

## The rule
1. Before reading or writing any project artifact, open `AGENTS.md` (repo root) and read its
   **## Project Layout** table to resolve the artifact's current path.
2. Use that resolved path. Do NOT hardcode a path from memory or from a skill's prose — the
   table is the single source of truth, and layouts move.
3. If `AGENTS.md` or the Project Layout table is missing (e.g. very first run before
   `project-scaffold`), fall back to the default layout below and create artifacts there.

## Default layout (used only until AGENTS.md exists)
| Artifact | Default path |
|----------|--------------|
| AGENTS.md | `AGENTS.md` (repo root) |
| Architecture | `docs/architecture.md` |
| Data model | `docs/data-model.md` |
| Tech stack | `docs/tech-stack.md` |
| Testing strategy | `docs/testing-strategy.md` |
| UI conventions | `docs/ui-conventions.md` |
| ADRs | `docs/adr/ADR-NNN-*.md` |
| API contracts | `docs/api-contracts/*.openapi.yaml` |
| Design history (proposals) | `docs/design-history/` |
| Design changelog | `docs/CHANGELOG-design.md` |
| PRD | `docs/product/PRD.md` |
| Epics | `docs/product/epics.md` |
| Epic map | `docs/product/epic-map.json` |
| Per-epic folder | `docs/product/epics/<EPIC_KEY>/` |
| Stories list | `docs/product/epics/<EPIC_KEY>/_epic/stories.md` |
| Story map | `docs/product/epics/<EPIC_KEY>/_epic/story-map.json` |
| Epic sequence | `docs/product/epics/<EPIC_KEY>/_epic/epic-sequence.md` and `.json` |
| Epic review log | `docs/product/epics/<EPIC_KEY>/_epic/epic-review.md` |
| Per-story folder | `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/` |
| Story review log | `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/story-review.md` |
| Story design note | `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/story-design.md` |
| Test plan | `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/test-plan.md` |
| Verification report | `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/verification.md` |
| Code-review report | `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/code-review.md` |
| Design-sync note | `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/design-sync.md` |

## Per-story folder rule (scales to hundreds of stories)
All artifacts for ONE story live in that story's own folder:
`docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/`. Inside it, filenames are SHORT and carry NO
story-key suffix (the folder already identifies the story): `story-review.md`, `story-design.md`,
`test-plan.md`, `verification.md`, `code-review.md`, `design-sync.md`. Any reference to a story's artifact is
resolved under that story's folder ONLY — never the flat `docs/product/` root. Likewise, an epic's
own artifacts (stories list, story-map, epic-sequence, **epic-review.md**) live in
`docs/product/epics/<EPIC_KEY>/_epic/`. Project-level artifacts (PRD, epics.md, epic-map.json) stay
at `docs/product/`. To locate a story's folder you need its epic key — resolve it via
`docs/product/epic-map.json` (epic → stories) if you only have the story key.

## Not covered by this rule
A skill's OWN `templates/`, `references/`, `examples/`, and `scripts/` are skill tooling. They
stay inside that skill's folder (wherever the agent loads skills from, e.g. `.agents/skills/` or
a Codex path) and are NEVER placed under `docs/`. Only project artifacts follow the table above.
