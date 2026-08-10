#!/usr/bin/env python3
"""
migrate_product_layout.py — reorganize a flat docs/product/ into the per-story folder layout.

FROM (flat):
    docs/product/
      PRD.md  epics.md  epic-map.json
      stories-<EPIC>.md  story-map-<EPIC>.json  epic-sequence-<EPIC>.{md,json}
      story-design-<STORY>.md  test-plan-<STORY>.md  verification-<STORY>.md
      code-review-<STORY>.md  design-sync-<STORY>.md

TO (nested):
    docs/product/
      PRD.md  epics.md  epic-map.json                         (unchanged, project-level)
      epics/<EPIC>/_epic/   stories.md  story-map.json  epic-sequence.md  epic-sequence.json
      epics/<EPIC>/stories/<STORY>/  story-design.md  test-plan.md  verification.md
                                     code-review.md  design-sync.md

Story->epic routing comes from epic-map.json. The story key (e.g. LNPRTL-30) is matched to the
epic whose requirement set / story list contains it; if epic-map doesn't list stories directly,
the script also reads story-map-<EPIC>.json files (which map epic -> stories) to route.

SAFETY:
  - Dry-run by default. Pass --apply to actually move files.
  - Idempotent: files already in the new location are skipped; re-running is safe.
  - Never deletes. Uses git mv when in a git repo (preserves history), else os.rename.
  - Anything it cannot confidently route is REPORTED and left in place (never guessed).

Usage:
    python migrate_product_layout.py --root docs/product            # dry-run (shows plan)
    python migrate_product_layout.py --root docs/product --apply     # perform moves
    python migrate_product_layout.py --root docs/product --apply --no-git   # force os.rename

Exit codes: 0 ok, 1 error, 2 some files unrouted (reported).
"""
import argparse
import json
import os
import re
import subprocess
import sys

# filename prefix -> (scope, new basename). scope: "story" or "epic".
STORY_FILES = {
    "story-design-": ("story", "story-design.md"),
    "test-plan-": ("story", "test-plan.md"),
    "verification-": ("story", "verification.md"),
    "code-review-": ("story", "code-review.md"),
    "design-sync-": ("story", "design-sync.md"),
}
EPIC_FILES = {
    "stories-": ("epic", "stories.md"),
    "story-map-": ("epic", "story-map.json"),
    "epic-sequence-": ("epic", "epic-sequence.md"),   # .md
}
# epic-sequence also has a .json variant handled below.
PROJECT_FILES = {"PRD.md", "epics.md", "epic-map.json", "product-overview.md"}

KEY_RE = re.compile(r"([A-Z][A-Z0-9]+-\d+)")  # e.g. LNPRTL-30


def is_git_repo(path):
    try:
        subprocess.run(["git", "-C", path, "rev-parse"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def load_epic_map(root):
    """Return dict: story_key -> epic_key, built from epic-map.json (+ story-map files as backup)."""
    story_to_epic = {}
    epic_keys = set()
    em = os.path.join(root, "epic-map.json")
    if os.path.exists(em):
        try:
            data = json.load(open(em))
            for e in data.get("epics", []):
                ek = e.get("epic_key")
                if ek:
                    epic_keys.add(ek)
                # epic-map maps epics->requirement IDs, not stories, so it gives epic keys only.
        except Exception as ex:
            print(f"  warn: could not parse epic-map.json ({ex})", file=sys.stderr)

    # story-map-<EPIC>.json files map epic -> stories; use them to route stories.
    for fn in os.listdir(root):
        m = re.match(r"story-map-(.+)\.json$", fn)
        if m:
            ek = m.group(1)
            epic_keys.add(ek)
            try:
                sm = json.load(open(os.path.join(root, fn)))
                for s in sm.get("stories", []):
                    sk = s.get("story_key")
                    if sk:
                        story_to_epic[sk] = ek
            except Exception as ex:
                print(f"  warn: could not parse {fn} ({ex})", file=sys.stderr)

    return story_to_epic, epic_keys


def epic_key_from_filename(fn):
    """For epic-level files stories-<EPIC>.md etc., extract the epic key."""
    for pfx in list(EPIC_FILES) + ["epic-sequence-"]:
        if fn.startswith(pfx):
            rest = fn[len(pfx):]
            rest = re.sub(r"\.(md|json)$", "", rest)
            return rest
    return None


def plan_moves(root, story_to_epic, epic_keys):
    moves = []      # (src, dst)
    unrouted = []   # (filename, reason)
    for fn in sorted(os.listdir(root)):
        full = os.path.join(root, fn)
        if os.path.isdir(full):
            continue
        if fn in PROJECT_FILES:
            continue  # project-level, stays put

        # epic-level files (handle epic-sequence .json vs .md explicitly first)
        if fn.startswith("epic-sequence-"):
            ek = epic_key_from_filename(fn)
            if not ek:
                unrouted.append((fn, "could not extract epic key")); continue
            newname = "epic-sequence.json" if fn.endswith(".json") else "epic-sequence.md"
            moves.append((full, os.path.join(root, "epics", ek, "_epic", newname)))
            continue

        matched = False
        for pfx, (_scope, newname) in EPIC_FILES.items():
            if pfx == "epic-sequence-":
                continue  # handled above
            if fn.startswith(pfx):
                ek = epic_key_from_filename(fn)
                if ek:
                    dst = os.path.join(root, "epics", ek, "_epic", newname)
                    moves.append((full, dst)); matched = True
                else:
                    unrouted.append((fn, "could not extract epic key"))
                    matched = True
                break
        if matched:
            continue

        # story-level files
        for pfx, (_scope, newname) in STORY_FILES.items():
            if fn.startswith(pfx):
                km = KEY_RE.search(fn[len(pfx):])
                if not km:
                    unrouted.append((fn, "no story key in filename"))
                    matched = True
                    break
                sk = km.group(1)
                ek = story_to_epic.get(sk)
                if not ek:
                    unrouted.append((fn, f"story {sk} not found in any story-map (can't route to epic)"))
                    matched = True
                    break
                dst = os.path.join(root, "epics", ek, "stories", sk, newname)
                moves.append((full, dst)); matched = True
                break
        if not matched:
            # unknown file — leave it, report it
            unrouted.append((fn, "unrecognized file pattern — left in place"))
    return moves, unrouted


def do_move(src, dst, use_git, root):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        return "skip (already exists)"
    if use_git:
        try:
            subprocess.run(["git", "-C", root, "mv", os.path.relpath(src, root),
                            os.path.relpath(dst, root)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            return "moved (git mv)"
        except subprocess.CalledProcessError:
            pass  # fall back to rename
    os.rename(src, dst)
    return "moved (rename)"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="docs/product", help="path to the flat product dir")
    ap.add_argument("--apply", action="store_true", help="perform moves (default: dry-run)")
    ap.add_argument("--no-git", action="store_true", help="use os.rename even in a git repo")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 1

    story_to_epic, epic_keys = load_epic_map(root)
    print(f"routing: {len(story_to_epic)} stories mapped across {len(epic_keys)} epics")

    moves, unrouted = plan_moves(root, story_to_epic, epic_keys)

    use_git = (not args.no_git) and is_git_repo(root)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n[{mode}] {len(moves)} file(s) to move"
          f"{'  (git mv)' if use_git and args.apply else ''}:\n")
    for src, dst in moves:
        rel_s = os.path.relpath(src, root)
        rel_d = os.path.relpath(dst, root)
        if args.apply:
            result = do_move(src, dst, use_git, root)
            print(f"  {rel_s}  ->  {rel_d}   [{result}]")
        else:
            exists = "  (dst exists, would skip)" if os.path.exists(dst) else ""
            print(f"  {rel_s}  ->  {rel_d}{exists}")

    if unrouted:
        print(f"\n!! {len(unrouted)} file(s) NOT routed (left in place — review manually):")
        for fn, reason in unrouted:
            print(f"  - {fn}: {reason}")

    if not args.apply:
        print(f"\nDry-run only. Re-run with --apply to perform the moves.")
    else:
        print(f"\nDone. Moved {len(moves)} file(s).")
        if unrouted:
            print("Some files were left in place — see above.")

    return 2 if unrouted else 0


if __name__ == "__main__":
    sys.exit(main())
