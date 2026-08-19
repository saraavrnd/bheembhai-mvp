# Faked-Green Checklist

A test suite can be green without the feature being done. These are the ways that happens — and
what to look for. Inspect the branch's diff since `test-creator` wrote the tests.

## 1. Tests neutralised
- `@pytest.mark.skip` / `skipif` / `xfail` added to a test that should run.
- Test commented out or deleted relative to the test-plan.
- `return` early / `assert True` replacing the real body.
**Check:** every test named in `test-plan-<STORY_KEY>.md` exists, runs, and is not skipped.

## 2. Assertions weakened
- Expected exact value changed to something looser (exact error string → substring → removed).
- Status-code assertion dropped or broadened (`== 413` → `>= 400`).
- A "Then" from the acceptance criteria no longer asserted.
**Check:** assertions still match the acceptance criteria verbatim where the criteria specify
values.

## 3. Production code that cheats
- Special-casing the test's exact input (e.g. `if filename == "test.pdf": return ok`).
- Returning a hard-coded value the test happens to expect, with no real logic behind it.
- Swallowing exceptions (`try/except: pass`) so an unhappy-path test stops failing without the
  behaviour being implemented.
**Check:** the implementation generalises beyond the test fixtures; unhappy paths have real
handling.

## 4. Coverage illusions
- Suite green but a scenario has no corresponding test at all.
- Happy path tested, unhappy paths missing.
**Check:** scenario → test mapping is complete, including boundary/invalid/permission cases.

## 5. Regression masking
- A previously passing test now skipped/modified to accommodate the new code.
**Check:** the pre-existing suite passes unchanged; no old test was altered to fit new code.

## How to use this
Run the suite, then read the diff of test files and the new production code against these
patterns. If any are present, the verdict is BLOCK — name the file/line and route the fix to the
owning step (implement for cheating code, test-creator for weakened/missing tests). A clean pass
on all five sections is what "honest green" means.
