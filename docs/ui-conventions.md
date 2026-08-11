# UI Conventions — BheemBhai

**Status:** Starter (thin) · **Date:** 2026-08-10 · Grows via `design-sync` as UI stories land.

> The UI source of truth. `implement`'s frontend lens reads this for every frontend/full-stack
> story so UI comes out consistent. The stack is **server-rendered HTML** (not an SPA): FastAPI +
> Jinja2 templates, the **EduAdmin** Bootstrap-5 theme (purchased, vendored) as the visual layer,
> Alpine.js 3 for interactivity, Mermaid.js for diagrams. The theme + Bootstrap give us a design
> system — these conventions are mostly about using them consistently and the Jinja/Alpine
> patterns around them.

## Stack & where UI lives
- **Templating:** Jinja2 (FastAPI `Jinja2Templates`). Server renders HTML; no React/SPA.
- **Theme:** **EduAdmin** (ThemeForest, purchased), **semidark variant** — `light-skin` body
  class (light content area) with the theme's semidark CSS delta (dark `#172b4c` sidebar).
  Vendored at `app/static/vendor/eduadmin/` (theme CSS/JS/icons/images). Theme files are kept
  **unmodified** so updates stay drop-in — project tweaks go only in
  `app/static/css/bheembhai-overrides.css`. The theme's Google-Fonts `@import`s must be
  stripped (on-prem: no external requests); the system-font fallback lives in the overrides file.
- **Component reference (canonical):** ALL UI elements — tables, widgets, forms, pages,
  boxes/cards, charts, auth/error pages — must be adapted from the theme's own demo markup,
  not hand-invented. The reference folder is the full (un-slimmed) theme at
  `../../ui_theme/themeforest-JVDUgCuV-eduadmin-responsive-bootstrap-admin-template-dashboard/bs5/main-semidark/`
  (outside the repo, reference-only — never served, never linked). Pick the demo page for the
  element you're building (`tables_*.html`, `forms_*.html`, `widgets_*.html`, `ui_*.html`,
  `box_*.html`, `component_*.html`, `auth_*.html`, `error_*.html`, `index*.html` dashboards),
  copy its markup into our Jinja template, and adapt (our URLs, Jinja variables/blocks, i18n
  text, accessibility attrs). If a demo page pulls a JS plugin we haven't vendored, either
  vendor that plugin or choose the closest demo that works with what's already in
  `app/static/vendor/eduadmin/` — say which in the story-design note.
- **Styling/components:** Bootstrap 5.3 (vendored with the theme as `css/bootstrap.css`) — its
  grid, utilities, and components are the baseline.
- **Interactivity:** Alpine.js 3.x (vendored at `app/static/vendor/alpine/`) for client behavior
  (form state, toggles, polling, gate actions, Mermaid render-failure detection). Keep it
  lightweight — Alpine for sprinkles, not an app.
- **Theme chrome JS:** the theme's `template.js` + `vendors.min.js` (jQuery 3.7.1 + plugins) run
  the layout chrome (sidebar push-menu, treeview, dropdowns) — **jQuery is theme-internal only,
  never write app code against it.**
- **Diagrams:** Mermaid.js, client-side render.
- **Template location:** `app/templates/` — base layouts + page templates + reusable partials in
  `app/templates/partials/`. Static assets (custom CSS/JS, vendored theme/Alpine/Mermaid) in
  `app/static/`.

## Template structure (Jinja)
- **Two base layouts:**
  - `base.html` — authenticated pages. EduAdmin chrome: fixed header + collapsible sidebar +
    content wrapper; body `hold-transition light-skin sidebar-mini theme-primary fixed`. Blocks:
    `{% block title %}`, `{% block content %}`, `{% block scripts %}`, `{% block head %}`.
  - `auth_base.html` — anonymous pages (login redirect, error pages). Centered auth card over the
    theme's `bg-img` background, **no header/sidebar**. Blocks: `title`, `content`, `head`,
    `scripts`.
- **Chrome partials** — `partials/_header.html` (logo, push-menu toggle, user menu showing
  `user.email`) and `partials/_sidebar.html` (navigation). The sidebar filters menu items on
  `user.role` — every authenticated page route must pass Cognito claims into the template
  context as `user` (with at least `email` and `role`), on **every** render path including
  validation-error and success re-renders.
- **Reuse via partials** — `{% include "partials/_card.html" %}`, macros for repeated markup
  (e.g. `{% macro field(...) %}` for form inputs). Don't copy-paste markup between templates;
  factor it into a partial or macro.
- **Naming:** page templates `snake_case.html` matching the route; partials prefixed `_`.
- **Icons:** use the theme's vendored packs — icomoon (`icon-*` classes, multi-path glyphs need
  the `<span class="pathN">` children) and themify (`ti-*`). Don't add new icon CDN links.

## Bootstrap usage discipline (this is what prevents drift)
- **Use Bootstrap's design tokens, not one-off values.** Spacing via Bootstrap's scale
  (`m-*`, `p-*`, `gap-*`); colors via Bootstrap semantic classes (`text-primary`, `bg-light`,
  `btn-danger`, etc.); typography via Bootstrap classes. Avoid inline styles and custom pixel
  values that bypass the scale.
- **Use Bootstrap components** (buttons, forms, cards, modals, alerts, nav, tables) rather than
  hand-rolling. Customize via Bootstrap's utility classes first; only add custom CSS when Bootstrap
  genuinely can't express it, and put it in `app/static/css/` with a clear, scoped class name.
- **Grid & responsive:** use the Bootstrap grid (`container`, `row`, `col-*`) and responsive
  breakpoints (`sm/md/lg/xl`). Don't hard-code fixed widths; design mobile-first.

## Alpine.js patterns
- Attach state with `x-data` at the smallest sensible scope; events with `x-on:`/`@`; binding with
  `x-bind:`/`:`. Keep logic small — if a component needs real complexity, that's a signal to push
  it server-side.
- **Polling for live run updates** — the primary BheemBhai interactivity. The run detail page uses
  an Alpine `x-init` timer that polls `/api/poll?since=<cursor>` every 2 seconds. Events update
  the step tracker, gate cards, and cost display in-place. Stop polling when the run reaches a
  terminal state (completed/failed).
- **Gate cards** — at a paused gate, the UI shows the step outcome, the review files (fetched via
  signed URL), and Approve / Request Changes buttons. Buttons POST to `/api/runs/{id}/decision`
  and are disabled while the request is in flight (Alpine `:disabled` bound to a `submitting`
  flag).
- **File viewer** — async fetch of the signed URL from `/api/runs/{id}/file?path=`, then render
  the text content in a modal. Show a loading spinner while fetching, an error alert on failure,
  and "file too large" message if the response exceeds 2 MB.
- **Mermaid render-failure detection**: detect a failed client-side Mermaid render and show a
  fallback; reuse it wherever diagrams render.

## Required UI states (every interactive view)
Server-rendered, but these still apply — handle them in the template/Alpine, not just the happy path:
- **Loading** — for Alpine-driven async (polling, file fetches, form submissions), show a
  Bootstrap spinner while in flight.
- **Error** — show a Bootstrap `alert-danger` with a usable message; never a blank page or silent
  failure. Server errors render an error template, not a stack trace.
- **Empty** — distinct "nothing yet" state (e.g. "No runs yet", "No projects"), not a blank area.
- **Success** — the normal render.

## Accessibility (non-negotiable)
- Semantic HTML — real `<button>`, `<nav>`, `<label for>`, headings in order. Bootstrap classes on
  semantic elements, not `<div>` soup.
- Every form input has an associated `<label>`; meaningful images have `alt`.
- Keyboard operable; visible focus (don't disable Bootstrap's focus outlines).
- Color contrast meets WCAG AA — Bootstrap's defaults mostly do; verify any custom colors.

## BheemBhai-specific pages & patterns

### Page inventory (MVP)
| Page | Route | Template | Description |
|------|-------|----------|-------------|
| Dashboard | `/` | `dashboard.html` | Project list, recent runs, quick-start button |
| Project detail | `/projects/{id}` | `project_detail.html` | Integrations, workflows, policies, run history for one project |
| Workflow editor | `/projects/{id}/workflows/{id}` | `workflow_edit.html` | YAML editor (textarea with validation feedback) |
| Policy editor | `/projects/{id}/policies/{id}` | `policy_edit.html` | YAML editor with workflow-step reference sidebar |
| Run detail | `/runs/{id}` | `run_detail.html` | Step tracker, gate cards, cost, file viewer |
| Run history | `/runs` | `run_history.html` | Paginated table of past runs with state badges |

### Sidebar navigation
```html
<!-- partials/_sidebar.html — BheemBhai -->
<li class="nav-item" role="presentation">
  <a href="/" class="nav-link {% if active_page == 'home' %}active{% endif %}">
    <i class="ti-home"></i><span>Dashboard</span>
  </a>
</li>
<li class="nav-item" role="presentation">
  <a href="/projects" class="nav-link {% if active_page == 'projects' %}active{% endif %}">
    <i class="ti-folder"></i><span>Projects</span>
  </a>
</li>
<li class="nav-item" role="presentation">
  <a href="/runs" class="nav-link {% if active_page == 'runs' %}active{% endif %}">
    <i class="ti-control-play"></i><span>Runs</span>
  </a>
</li>
```

### Run state badges
Use Bootstrap badge classes mapped to run states:
- `pending` → `badge-secondary`
- `running` → `badge-primary`
- `awaiting_approval` → `badge-warning`
- `completed` → `badge-success`
- `failed` → `badge-danger`
- `retrying` → `badge-info`

### Step tracker (vertical timeline)
Adapted from the theme's timeline/widget markup. Each step is a row with:
- Dot (colored by state: pending=gray, running=blue, completed=green, failed=red, awaiting_approval=amber)
- Step label (from workflow)
- Status badge
- Duration + cost (when complete)
- Artifact links (when complete — opens file viewer modal)

### Gate card
At a paused gate, show a themed card (EduAdmin box component) with:
- Step outcome summary (the agent's closing text)
- Reviewer's file list (fetched from step artifacts, each clickable via signed URL)
- "Show all changed files" toggle (falls back to full git diff if no BB_REVIEW lines)
- Approve button (green) + Request Changes button (amber) + comment textarea
- Both buttons disabled during submission (Alpine `:disabled`)

### Forms pattern
All CRUD forms (project create, integration setup, workflow/policy edit) follow the same pattern:
- Server-rendered POST with CSRF token
- Validation errors rendered as `alert-danger` at the top of the form
- Field-level errors rendered as `.invalid-feedback` below the input
- Disabled submit button during submission (Alpine `x-bind:disabled="submitting"`)
- Success redirects to the detail/list page with a flash message (Bootstrap `alert-success`)

## Patterns (grows over time — design-sync promotes here)

Established patterns from implemented stories. Every new UI story must follow these.

- **Themed page layout** (established by base implementation): authenticated pages
  `{% extends "base.html" %}` and put page content in `{% block content %}`; anonymous/auth pages
  extend `auth_base.html`. The route passes `user={<cognito claims>}` into the template
  context so the header shows the signed-in email and the sidebar role-filters. New sidebar items
  go in `partials/_sidebar.html`, gated on `user.role` (server-side JWT validation remains
  the real authz boundary — the sidebar filter is UX only). All theme assets are served from
  `/static/vendor/...` — no CDN links on any page.

- **Page title without breadcrumbs**: the app's navigation is shallow. Do **not** include
  breadcrumb `<nav>` / `<ol class="breadcrumb">` markup. The `{% block content_header %}` block
  contains only a `<h3 class="page-title">` inside the standard
  `.content-header > .d-flex > .me-auto` wrapper. Page titles use the
  "Manage \<Entity\>" format for list/manage pages (e.g. "Manage Projects") and
  "\<Action\> \<Entity\>" for single-action pages.

- **Sidebar active state**: every authenticated page template sets
  `{% set active_page = '<key>' %}` immediately after `{% extends "base.html" %}`.
  `partials/_sidebar.html` reads `active_page|default('')` to add the `active` class to the
  matching `<li>`. Supported keys: `home`, `projects`, `runs`, `settings`. New sidebar
  items added by future stories must add their key here and follow the same pattern.

- **Run polling** (established by run-detail story): the run detail page uses an Alpine timer
  (`setInterval` in `x-init`) to poll `/api/poll?since=<cursor>` every 2 seconds. Events update
  the step tracker DOM in-place (Alpine `x-html` or direct DOM manipulation). Polling stops
  when a terminal event arrives (`run_completed` or `run_failed`).

- e.g. the gate card component, the file viewer modal, the YAML editor with live validation,
  the project integration setup wizard will land here as stories build them.

## Out of scope (by stack choice)
- No React/Vue/SPA patterns, no client-side router, no JS build-heavy component framework — this is
  server-rendered HTML with Alpine sprinkles. Don't introduce SPA patterns without an ADR.
