# ADR-001: Server-rendered HTML with EduAdmin Bootstrap-5 theme

**Status:** Accepted · **Date:** 2026-08-10 · **Deciders:** Saraav

## Context

The existing BheemBhai UI is a single-file vanilla HTML dashboard (~550 lines). EPIC BEEM-24
requires a multi-page admin interface: project management, workflow/policy CRUD, paginated
execution history, approval gate cards with file viewers, and auth flows (login/logout).

The default recommendation was React 19 + Vite + Tailwind — a SPA that would handle the
interactive CRUD and real-time polling cleanly. However, the existing Learn Portal project
(under the same team) has already invested in a production-ready server-rendered stack:
Jinja2 templates, the EduAdmin Bootstrap-5 theme (purchased, ThemeForest), and Alpine.js for
client interactivity. That stack has established conventions, reusable layouts, and a proven
pattern for admin UIs.

The decision is whether to adopt the SPA approach or reuse the Learn Portal's proven stack.

## Decision

**Use the Learn Portal's UI stack: server-rendered HTML with Jinja2 templates, the EduAdmin
Bootstrap-5 theme (semidark variant), and Alpine.js 3 for client interactivity.**

The existing `docs/ui-conventions.md` from Learn Portal becomes the UI source of truth for
BheemBhai. Theme artifacts are vendored from the same reference directory at
`../../ui_theme/themeforest-JVDUgCuV-eduadmin-responsive-bootstrap-admin-template-dashboard/bs5/main-semidark/`.

## Alternatives considered

- **React 19 + Vite + Tailwind (rejected):** Better component model for complex interactivity,
  but introduces a build step, a separate development workflow, and duplicates the design-system
  investment already made in the EduAdmin theme. The admin CRUD patterns needed by BEEM-24
  (tables, forms, modals, sidebars) are exactly what Bootstrap 5 + the EduAdmin theme already
  provide as copy-paste demo markup.
- **Vanilla JS (rejected):** No build step, simple deploy, but the current 550-line dashboard
  already strains at its limits. Multi-page CRUD with auth flows would be painful without a
  templating system and a component library.

## Consequences

- **Easier:** Reuse of the EduAdmin theme's demo markup (tables, forms, widgets, cards, auth
  pages) directly adapted into Jinja2 templates. No design system to build from scratch. Same
  stack as Learn Portal means shared conventions, debugging knowledge, and tooling.
- **Easier:** Server-rendered HTML means no API client, no client-side router, no state
  management library. Auth is handled by ALB + Cognito at the edge — the backend always knows
  who the user is when rendering a template.
- **Harder:** Real-time polling for execution tracking requires Alpine.js timers or an
  EventSource pattern — simpler than a SPA but needs care to avoid stale UI state.
- **Harder:** The gate-card file viewer (2 MB text files via signed URLs) needs a modal +
  async fetch pattern in Alpine — doable but worth noting as a non-trivial Alpine component.
