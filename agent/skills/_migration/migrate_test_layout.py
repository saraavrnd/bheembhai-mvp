#!/usr/bin/env python3
"""
migrate_test_layout.py — reorganize flat tests/ into the module-mirrored layout.

FROM (flat):
    tests/
      unit/test_health.py  test_passwords.py
      integration/test_health_api.py  test_password_persistence.py
      e2e/test_health_page.py

TO (mirrors source modules; test-type at top, module under each):
    tests/
      unit/<module>/test_*.py
      integration/<module>/test_*.py
      e2e/test_*.py            (e2e left at top by default; user-journey level)

MODULE INFERENCE (best signal first) — tests are HARDER to route than docs, so this never
guesses silently:
  1. IMPORTS: a test importing `from app.<module>...` or `import app.<module>` -> that module.
     (Strongest signal. Configurable source root via --app-root, default "app".)
  2. FILENAME: test_<stem>.py whose <stem> (or a token of it) matches an existing source module
     directory name -> that module.
  3. UNRESOLVED: left where it is and reported (or moved to <type>/_unsorted/ with --unsorted).

SAFETY:
  - Dry-run by default; --apply to move.
  - Idempotent: a test already under a <module>/ subfolder is skipped.
  - Never deletes. git mv in a git repo (preserves history), else os.rename.
  - Unresolved tests are REPORTED, never auto-placed into a wrong module.
  - e2e left flat unless --split-e2e is passed.

Usage:
    python migrate_test_layout.py --tests tests --src .            # dry-run
    python migrate_test_layout.py --tests tests --src . --apply    # move
    python migrate_test_layout.py --tests tests --src . --apply --unsorted   # park unresolved
    options: --app-root app   --split-e2e   --no-git

Exit: 0 ok, 1 error, 2 some tests unresolved (reported).
"""
import argparse
import os
import re
import subprocess
import sys

TEST_TYPES = ("unit", "integration", "e2e")
IMPORT_RE = None  # built from app-root


def is_git_repo(path):
    try:
        subprocess.run(["git", "-C", path, "rev-parse"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def discover_modules(src_root, app_root):
    """Return the set of source module names under <src>/<app_root>/ — both package dirs
    (e.g. auth/) AND top-level module files (e.g. main.py -> 'main', db.py -> 'db')."""
    base = os.path.join(src_root, app_root)
    mods = set()
    if os.path.isdir(base):
        for name in os.listdir(base):
            full = os.path.join(base, name)
            if os.path.isdir(full) and not name.startswith((".", "_")) and name != "__pycache__":
                mods.add(name)
            elif name.endswith(".py") and name != "__init__.py" and not name.startswith("_"):
                mods.add(name[:-3])  # module file: main.py -> main
    return mods


def infer_from_imports(path, app_root, modules):
    """Strongest signal: parse import lines for app.<module>."""
    pat = re.compile(rf'(?:from|import)\s+{re.escape(app_root)}\.(\w+)')
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(8000)  # imports are near the top
    except Exception:
        return None
    hits = [m.group(1) for m in pat.finditer(head) if m.group(1) in modules]
    if hits:
        # most frequent module import wins
        return max(set(hits), key=hits.count)
    return None


def infer_from_filename(fn, modules):
    """Weaker: match a token of the filename stem to a module name."""
    stem = re.sub(r'^test_', '', os.path.splitext(fn)[0])
    tokens = re.split(r'[_\-]', stem)
    # exact module-name token match
    for t in tokens:
        if t in modules:
            return t
    # substring: a module name contained in the stem (e.g. 'password' -> module 'passwords'? no;
    # but module 'auth' won't match 'password'). Only do safe containment both directions.
    for mod in modules:
        if mod in stem or stem in mod:
            return mod
    return None


def already_nested(rel_within_type):
    """True if the test is already in a <module>/ subfolder (not directly under the type dir)."""
    return os.path.dirname(rel_within_type) not in ("", ".")


def plan(tests_root, src_root, app_root, modules, split_e2e, use_unsorted):
    moves, unresolved = [], []
    for ttype in TEST_TYPES:
        tdir = os.path.join(tests_root, ttype)
        if not os.path.isdir(tdir):
            continue
        if ttype == "e2e" and not split_e2e:
            continue  # leave e2e flat by default
        for root, _dirs, files in os.walk(tdir):
            for fn in files:
                if not (fn.startswith("test_") and fn.endswith((".py", ".ts", ".js", ".spec.ts"))):
                    continue
                full = os.path.join(root, fn)
                rel_within = os.path.relpath(full, tdir)
                if already_nested(rel_within):
                    continue  # already under a module folder -> idempotent skip
                mod = infer_from_imports(full, app_root, modules) or \
                      infer_from_filename(fn, modules)
                if mod:
                    dst = os.path.join(tdir, mod, fn)
                    moves.append((full, dst, mod))
                else:
                    if use_unsorted:
                        dst = os.path.join(tdir, "_unsorted", fn)
                        moves.append((full, dst, "_unsorted"))
                    unresolved.append((os.path.relpath(full, tests_root),
                                       "no app.<module> import and no filename match"))
    return moves, unresolved


def do_move(src, dst, use_git, root):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        return "skip (exists)"
    if use_git:
        try:
            subprocess.run(["git", "-C", root, "mv", os.path.relpath(src, root),
                            os.path.relpath(dst, root)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            return "moved (git mv)"
        except subprocess.CalledProcessError:
            pass
    os.rename(src, dst)
    return "moved (rename)"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tests", default="tests", help="path to tests dir")
    ap.add_argument("--src", default=".", help="repo/source root containing the app dir")
    ap.add_argument("--app-root", default="app", help="source package dir name (default: app)")
    ap.add_argument("--apply", action="store_true", help="perform moves (default dry-run)")
    ap.add_argument("--split-e2e", action="store_true", help="also sub-folder e2e tests by module")
    ap.add_argument("--unsorted", action="store_true",
                    help="move unresolved tests into <type>/_unsorted/ instead of leaving in place")
    ap.add_argument("--no-git", action="store_true", help="use os.rename even in a git repo")
    args = ap.parse_args()

    tests_root = os.path.abspath(args.tests)
    src_root = os.path.abspath(args.src)
    if not os.path.isdir(tests_root):
        print(f"ERROR: tests dir not found: {tests_root}", file=sys.stderr); return 1

    modules = discover_modules(src_root, args.app_root)
    if not modules:
        print(f"WARNING: no source modules found under {args.src}/{args.app_root}/ — "
              f"inference will rely on filenames only.", file=sys.stderr)
    else:
        print(f"source modules discovered ({len(modules)}): {', '.join(sorted(modules))}")

    moves, unresolved = plan(tests_root, src_root, args.app_root, modules,
                             args.split_e2e, args.unsorted)
    use_git = (not args.no_git) and is_git_repo(src_root)
    mode = "APPLY" if args.apply else "DRY-RUN"

    print(f"\n[{mode}] {len(moves)} test file(s) to move:\n")
    for src, dst, mod in moves:
        rs, rd = os.path.relpath(src, tests_root), os.path.relpath(dst, tests_root)
        if args.apply:
            res = do_move(src, dst, use_git, src_root)
            print(f"  {rs}  ->  {rd}   [{mod}] [{res}]")
        else:
            print(f"  {rs}  ->  {rd}   [module: {mod}]")

    if unresolved:
        print(f"\n!! {len(unresolved)} test(s) UNRESOLVED "
              f"({'parked in _unsorted/' if args.unsorted else 'left in place'} — place by hand):")
        for rel, why in unresolved:
            print(f"  - {rel}: {why}")
        print("  Tip: add an explicit `from app.<module> import ...` to the test, or move it "
              "into the right tests/<type>/<module>/ folder manually.")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to perform the moves.")
    else:
        print(f"\nDone. Moved {len(moves)} file(s).")
    return 2 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
