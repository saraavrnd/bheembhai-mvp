# Product Layout Migration

One-time tool to reorganize an existing **flat** `docs/product/` into the **per-story folder**
layout the skills now expect.

## What it does
Moves files from the old flat names into the nested structure:

```
FROM                                    TO
docs/product/                           docs/product/
  PRD.md  epics.md  epic-map.json         PRD.md  epics.md  epic-map.json        (unchanged)
  stories-<EPIC>.md                       epics/<EPIC>/_epic/stories.md
  story-map-<EPIC>.json                   epics/<EPIC>/_epic/story-map.json
  epic-sequence-<EPIC>.md/.json           epics/<EPIC>/_epic/epic-sequence.md/.json
  story-design-<STORY>.md                 epics/<EPIC>/stories/<STORY>/story-design.md
  test-plan-<STORY>.md                    epics/<EPIC>/stories/<STORY>/test-plan.md
  verification-<STORY>.md                 epics/<EPIC>/stories/<STORY>/verification.md
  code-review-<STORY>.md                  epics/<EPIC>/stories/<STORY>/code-review.md
  design-sync-<STORY>.md                  epics/<EPIC>/stories/<STORY>/design-sync.md
```

Story → epic routing comes from the `story-map-<EPIC>.json` files (which list each epic's
stories). Project-level files (PRD, epics.md, epic-map.json) stay put.

## How to run
```bash
# 1. Dry-run first — shows the full move plan, changes nothing
python migrate_product_layout.py --root docs/product

# 2. Review the plan and the "NOT routed" list (if any)

# 3. Apply
python migrate_product_layout.py --root docs/product --apply
```

## Safety
- **Dry-run by default** — nothing moves until you pass `--apply`.
- **Idempotent** — re-running after a successful migration moves 0 files.
- **Never deletes** — only moves. Uses `git mv` in a git repo (preserves history); falls back to
  rename otherwise. Force rename with `--no-git`.
- **No guessing** — any file it can't confidently route (e.g. a story not listed in any
  story-map) is left in place and reported; exit code 2 signals "some files unrouted, review them".

## After migrating
1. Commit the moves (`git add -A && git commit -m "migrate docs/product to per-story folders"`).
2. The skills already resolve paths from the Project Layout table in `CLAUDE.md`, which encodes
   the new structure — so no skill changes are needed.
3. If any files were left unrouted, place them by hand: find the story's epic in `epic-map.json` /
   the relevant `story-map`, then move it under `epics/<EPIC>/stories/<STORY>/`.

## Note on routing data
Routing reads the flat `story-map-<EPIC>.json` files at the root. Run the migration BEFORE moving
those by other means — the tool moves them itself as part of the run. If your story-maps live
elsewhere or stories aren't listed in them, the affected story files will be reported as unrouted
for manual placement.

---

# Test Layout Migration

`migrate_test_layout.py` reorganizes a **flat** `tests/` into the module-mirrored layout the
skills now expect: `tests/unit|integration|e2e/<module>/test_*.py`, mirroring `app/<module>/`.

## How module is inferred (tests are harder to route than docs)
Unlike docs, there's no map saying which module a test belongs to, so the tool infers — best
signal first, and never guesses silently:
1. **Imports** (strongest): a test with `from app.<module> import …` routes to that module.
2. **Filename**: `test_<stem>.py` whose stem matches an existing `app/<module>/` directory name.
3. **Unresolved**: if neither resolves, the test is LEFT IN PLACE and reported (or parked in
   `tests/<type>/_unsorted/` with `--unsorted`) — never auto-placed into a wrong module.

## How to run
```bash
# dry-run — shows the inferred plan + any unresolved tests
python migrate_test_layout.py --tests tests --src . --app-root app

# review; for unresolved tests, either add an `from app.<module> import` to the test or
# plan to move it by hand

# apply
python migrate_test_layout.py --tests tests --src . --app-root app --apply

# options:
#   --app-root <name>   source package dir (default: app)
#   --split-e2e         also sub-folder e2e by module (default: e2e stays flat)
#   --unsorted          park unresolved tests in <type>/_unsorted/ instead of leaving in place
#   --no-git            use os.rename even in a git repo
```

## Safety
- **Dry-run by default**; `--apply` to move. **Never deletes.** `git mv` preserves history.
- **Idempotent** — tests already under a `<module>/` subfolder are skipped on re-run.
- **No wrong guesses** — unresolved tests are reported, not misplaced. Exit code 2 signals "some
  tests unresolved, review them."
- **e2e left flat** by default (e2e are user-journey level, not module-bound); use `--split-e2e`
  if you want them sub-foldered too.

## After migrating
1. Commit the moves.
2. Run the suite to confirm discovery still finds everything (`pytest` recurses sub-folders by
   default; check no flat-only path config).
3. Place any unresolved tests by hand into their `tests/<type>/<module>/` folder.
