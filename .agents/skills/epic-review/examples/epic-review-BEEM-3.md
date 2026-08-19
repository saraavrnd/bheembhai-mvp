# Epic Review — BEEM-3: Project access, identity & run ownership

**Status:** ILLUSTRATIVE (worked example, not an applied repo change) · **Date:** 2026-07-19 ·
**Stories reviewed:** BEEM-17, BEEM-18, BEEM-19, BEEM-16, BEEM-12, BEEM-13, BEEM-14, BEEM-15

> Worked example built from this repo's real `docs/product/epics/BEEM-3/_epic/stories.md` and
> `docs/api-contracts/beembhai-api.openapi.yaml`, run the way this skill would right after
> `user-story` produced all eight BEEM-3 stories. The gaps below are real (found while drilling
> BEEM-15 by hand before this skill existed); the answers are a plausible illustration of how the
> interview would resolve them, not an applied change to the repo. Shows the thing a per-story
> `story-review` would have to rediscover independently for BEEM-12, BEEM-13, and BEEM-15 if run
> three separate times instead of once here.

## Context loaded
- All 8 BEEM-3 stories from `stories.md` (BEEM-17, 18, 19, 16, 12, 13, 14, 15) — full set, not a
  sample.
- `epic-map.json` — BEEM-3 covers FR-001 through FR-006.
- `docs/api-contracts/beembhai-api.openapi.yaml` — `User.platformRole` enum
  `[PLATFORM_ADMIN, STANDARD]`; `Membership.role` enum
  `[PROJECT_MANAGER, DEVELOPER, DEVOPS, REVIEWER]`; `POST /projects/{projectId}/members`
  (upsert semantics).
- `docs/data-model.md` — `User`, `Project`, `Membership` entities; no `PolicyVersion` internal
  schema beyond `id, project_id, version, status`.
- `PRD.md` FA-1/FA-3/FA-4/FA-6 sections and `epic-sequence.md`'s epic ordering table (Epic 3:
  Versioned workflow & policy definitions runs after Epic 1, i.e. after BEEM-3).

## Gaps found

### Gap 1 — Cross-story actor & role consistency
- **Stories touched:** BEEM-12, BEEM-13, BEEM-15 (all three use "admin" / "project admin" as an
  actor).
- **What was missing/ambiguous:** None of the three stories' actors map to an actual enum value.
  `User.platformRole` has `PLATFORM_ADMIN`/`STANDARD` (platform-wide); `Membership.role` has
  `PROJECT_MANAGER`/`DEVELOPER`/`DEVOPS`/`REVIEWER` (project-scoped) — no `ADMIN` value in
  either. A single-story review of BEEM-15 alone could plausibly invent its own answer; reviewing
  all three together shows they need the *same* answer or they'll disagree on who can do what.
- **Question asked:** "Across BEEM-12, BEEM-13, and BEEM-15, is 'admin' always the platform admin
  (`platformRole = PLATFORM_ADMIN`), or can a project member with `PROJECT_MANAGER` also perform
  these actions within their own project?"
- **Answer (illustrative):** Platform admin only, for all three stories — consistent with
  BEEM-16's bootstrap note that it seeds "the prerequisite for BEEM-12, BEEM-13, and BEEM-15."
  `PROJECT_MANAGER` is a run/approval-eligibility role, not a project-administration role, in
  this data model.
- **Resulting change:** All three stories' "Given a[n] admin/project admin is signed in"
  preconditions reworded to "Given a signed-in user with `platformRole = PLATFORM_ADMIN`," applied
  identically across BEEM-12, BEEM-13, and BEEM-15 in the same pass.

### Gap 2 — Cross-story / cross-epic architecture fit (dependency note missing, not a conflict)
- **Stories touched:** BEEM-15.
- **What was missing/ambiguous:** ACs 2, 3, and 5 all depend on "the required approval role for a
  gated step" being knowable. Per `PRD.md`, that mapping lives in the policy definition, which is
  Epic 3 / `BHEEM-5` — a different epic, not yet built. `epic-sequence.md` shows it runs after
  BEEM-3, so this is legitimate cross-epic sequencing, not an architecture conflict — but BEEM-15's
  own Notes/dependencies section is silent about it, naming only "the approval-gate workflow from
  the orchestration epic."
- **Question asked:** "BEEM-15's eligibility scenarios assume a step's required role is already
  known. Should BEEM-15's own scope be limited to storing `Membership.role` and exposing an
  eligibility check given a role passed in, with the required-role-per-step mapping explicitly
  left to the policy epic (BHEEM-5) — and should that dependency be written down here?"
- **Answer (illustrative):** Yes to both — scope BEEM-15 to membership + eligibility check only;
  add the missing dependency note.
- **Resulting change:** BEEM-15's Notes/dependencies now also reads: "The mapping from a gated
  step to its required approval role is defined by the policy record built in BHEEM-5 (Epic 3);
  this story only stores project roles and evaluates eligibility given a required role supplied
  by the orchestrator." No architecture change needed — `tech-design` is not invoked for this gap.

### Gap 3 — Dependency-note sanity (one-sided claim)
- **Stories touched:** BEEM-16, BEEM-12, BEEM-13, BEEM-15.
- **What was missing/ambiguous:** BEEM-16's notes state it is "the prerequisite seed for BEEM-12,
  BEEM-13, and BEEM-15." None of those three stories' own Notes/dependencies mention BEEM-16 —
  a one-sided dependency claim. Read individually, each of the three looks self-contained; only
  comparing all four together surfaces the mismatch.
- **Question asked:** "Should BEEM-12, BEEM-13, and BEEM-15 each state 'depends on BEEM-16 (bootstrap
  admin)' explicitly, to match BEEM-16's own claim?"
- **Answer (illustrative):** Yes.
- **Resulting change:** Added "Depends on `BEEM-16` for the first platform admin account" to the
  Notes/dependencies of BEEM-12, BEEM-13, and BEEM-15. (This is a documentation fix only — it does
  not change `epic-sequence`'s computed order, which already builds BEEM-16 first on other
  grounds.)

## Architecture impact
None — Gap 2 is a legitimate, already-correctly-sequenced cross-epic dependency; the only change
needed was writing it down. No gap here requires a new entity, field, or endpoint.

## Story changes summary
`stories.md`:
- BEEM-12, BEEM-13, BEEM-15: "admin"/"project admin" preconditions reworded to
  `platformRole = PLATFORM_ADMIN`.
- BEEM-15: Notes/dependencies gained the BHEEM-5 policy-mapping dependency.
- BEEM-12, BEEM-13, BEEM-15: Notes/dependencies each gained an explicit "Depends on BEEM-16."

## Requirement coverage check
FR-001 through FR-006 each still map to at least one story's acceptance criteria after the
rewrite (the actor rewording didn't remove any scenario). FR-005 is claimed by BEEM-17, BEEM-18,
BEEM-19, BEEM-16, and BEEM-14 — all five are non-contradicting slices (API registration, browser
UI, visual-mockup alignment, bootstrap seeding, sign-in/verify/reset), consistent with FA-1's
intentional split. No FR/NFR wording drift found against the current `PRD.md`.

## Dependency-note sanity check
Found and reconciled the one-sided BEEM-16 → {BEEM-12, BEEM-13, BEEM-15} claim (Gap 3). No other
dependency note in `stories.md` referred to a non-existent story key. Build order itself is left
to `epic-sequence` — this pass only fixed the documentation mismatch.

---
*On approval: `docs/product/epics/BEEM-3/_epic/stories.md` would be updated in place for BEEM-12,
BEEM-13, and BEEM-15. No Jira keys were used in this illustrative pass. Next: `story-review` for
any story that still feels individually underspecified (BEEM-15 likely still wants one — the
Reviewer-role concreteness questions are single-story concerns, not covered here), then
`story-design`.*
