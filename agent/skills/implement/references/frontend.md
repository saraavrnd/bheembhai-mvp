# Frontend Lens — building UI stories well

Loaded by `implement` when `story-design` tags a story **frontend** or **full-stack**. Backend
stories ignore this. Its job: make UI come out consistent and well-built, not ad hoc, by (1)
pointing at the project's own UI conventions and (2) carrying the generic UI craft that holds
regardless of project.

## First: read the project's UI conventions
Before writing any UI, read `docs/ui-conventions.md` (created by `tech-design`, kept current by
`design-sync`). It is the SOURCE OF TRUTH for this project's component structure, design tokens,
state pattern, and UI standards. Apply it. If the story introduces a new pattern not yet covered,
implement it consistently with what's there, and note it so `design-sync` can promote it into the
conventions (this is how the design system grows out of an initially ad-hoc UI).

If `docs/ui-conventions.md` doesn't exist yet, flag it — UI stories should not be built without it,
or they'll drift. Recommend running `tech-design` to establish it first.

## Generic UI craft (true regardless of project)
Even with conventions in hand, hold this bar:

### Works for any frontend stack
This lens is framework-neutral. "Component" means whatever the project's UI unit is — a React/Vue
component in an SPA, or a Jinja/Handlebars partial/macro + light JS (e.g. Alpine.js) in a
server-rendered app. The project's `docs/ui-conventions.md` defines the actual stack and patterns;
this lens defers to it and adds the generic craft below. Don't assume an SPA — check the
conventions first.

## Structure & state
- UI units small and focused; one responsibility each. Mirror the source-tree / template-tree
  convention the project uses.
- State lives where the conventions say (component-local, a shared store, server-rendered + a light
  JS layer like Alpine) — don't introduce a new state mechanism per story.
- API/data flows through the project's pattern — don't hand-roll fetch logic if a pattern exists;
  in a server-rendered app, prefer rendering data server-side and using JS only for interactivity.

### The states every UI must handle (not just the happy path)
- **Loading** — show a loading indicator while data is in flight.
- **Error** — show a usable error state, not a blank screen or a crash.
- **Empty** — handle "no data" distinctly from loading.
- **Success** — the normal render.
A UI story that only renders the success state is incomplete — `test-verify`/`code-review` should
flag it.

### Accessibility (the non-negotiables)
- Semantic HTML (button is a `<button>`, not a clickable `<div>`).
- Labels for inputs; alt text for meaningful images.
- Keyboard operable (focusable, sensible tab order); visible focus.
- Sufficient color contrast per the conventions.

### Responsive & consistent
- Works across the breakpoints the conventions define; don't hard-code desktop-only layouts.
- Use the design tokens (spacing, color, type) from the conventions — never magic pixel values or
  one-off colors that bypass the token scale. This is what prevents visual drift.

## Tests for UI (with test-creator)
UI stories' tests match the stack:
- **SPA:** component/interaction tests (render + loading/error/empty/success + interactions).
- **Server-rendered (Jinja/Bootstrap/Alpine etc.):** template-render tests (the right HTML/markup
  is produced for a given context) + any Alpine-driven behavior, plus e2e for the journey.
- **e2e (Playwright):** the user-journey acceptance scenarios, both stacks.
Place them per the test layout (`tests/unit/<module>/`, `tests/e2e/`), mirroring source. The
project's `ui-conventions.md` / `testing-strategy.md` says which apply.

## Full-stack stories
When the story is full-stack, build the backend slice (default `implement` path) AND the UI slice
(this lens), and make sure the UI consumes the real API contract from `docs/api-contracts/` — not a
mock that drifts from the actual endpoint. The integration/e2e tests prove the two halves meet.
