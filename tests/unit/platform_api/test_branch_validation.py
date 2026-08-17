"""Unit — source-branch override validation (run modal's editable branch field).

The engine cuts each run's branch off a source branch. The run modal lets the
user override it (ADR-013 deferred item); ``_valid_source_branch`` mirrors
git check-ref-format's essentials so a bad name is a clear 422 at submit time
instead of a container-less ``failed_execution`` at engine init.
"""

import pytest

from platform_api.routers.runs import _valid_source_branch

VALID = [
    "main",
    "develop",
    "release/2026.08",
    "feature/LNPRTL-50_story",
    "hotfix/lnprtl-50-bug",
    "a.b.c-d/e_f",
]


@pytest.mark.parametrize("name", VALID)
def test_accepts_valid_branch_names(name):
    assert _valid_source_branch(name) is None


@pytest.mark.parametrize("name", [
    "",                  # covered by the required-ness of the field, but cheap to pin
    " main",
    "main ",
    "-leading-dash",
    "trailing/",
    "trailing.",
    "dev..elop",         # path traversal in refs
    "fix/@{bad}",
    "double//slash",
    "main.lock",
    "has space",
    "has~tilde",
    "has^caret",
    "has:colon",
    "has?question",
    "has*star",
    "has[open",
    "has\\backslash",
])
def test_rejects_invalid_branch_names(name):
    assert _valid_source_branch(name) is not None
