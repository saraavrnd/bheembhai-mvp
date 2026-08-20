"""Environment-variable domain rules — name validation, refs, merge precedence."""

from types import SimpleNamespace

import pytest
from bheembhai.env_vars import (
    RESERVED_NAMES,
    TUNED_NAMES,
    env_var_ref,
    merge_env_var_rows,
    validate_env_var_name,
    validate_tunable_value,
)

# ── Name validation ──────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "REGION", "TOOL_API_KEY", "max_workers", "_internal", "A1_B2",
])
def test_valid_names_accepted(name):
    assert validate_env_var_name(name) == name


@pytest.mark.parametrize("name", [
    "", "1LEADING", "has-hyphen", "has space", "dot.name", "emoji🔥",
])
def test_invalid_names_rejected(name):
    with pytest.raises(ValueError, match="invalid environment variable name"):
        validate_env_var_name(name)


@pytest.mark.parametrize("name", sorted(RESERVED_NAMES))
def test_reserved_names_rejected(name):
    with pytest.raises(ValueError, match="owned by the engine"):
        validate_env_var_name(name)


@pytest.mark.parametrize("name", sorted(TUNED_NAMES))
def test_tunable_names_accepted(name):
    assert validate_env_var_name(name) == name


def test_tunables_are_not_reserved():
    assert TUNED_NAMES.isdisjoint(RESERVED_NAMES)


# ── Tunable value validation ─────────────────────────────────────────────

@pytest.mark.parametrize("value", ["1", "3", "42"])
def test_tunable_positive_ints_ok(value):
    validate_tunable_value("BB_MAX_STEP_VISITS", value)


@pytest.mark.parametrize("value", ["0", "-3", "abc", "", "1.5", None])
def test_tunable_bad_values_rejected(value):
    with pytest.raises(ValueError, match="positive integer"):
        validate_tunable_value("BB_MAX_ATTEMPTS", value)


def test_non_tunable_names_skip_value_validation():
    # Plain skill vars may hold anything.
    validate_tunable_value("TOOL_API_KEY", "anything at all")


# ── SecureStorage refs ───────────────────────────────────────────────────

def test_platform_ref_path():
    assert env_var_ref(None, "AUTH_KEY") == "/bheembhai/env/platform/AUTH_KEY"


def test_project_ref_path():
    assert (env_var_ref("0c42f5b7-7f3a-4c0a-9e4b-1a2b3c4d5e6f", "AUTH_KEY")
            == "/bheembhai/env/0c42f5b7-7f3a-4c0a-9e4b-1a2b3c4d5e6f/AUTH_KEY")


# ── Merge precedence ─────────────────────────────────────────────────────

def _row(name, scope, value):
    return SimpleNamespace(name=name, scope=scope, value=value)


def test_merge_project_overrides_platform():
    rows = [
        _row("REGION", "platform", "us-west-1"),
        _row("REGION", "project", "eu-central-1"),
        _row("AUTH_KEY", "platform", "platform-secret"),
    ]
    merged = merge_env_var_rows(rows)
    assert list(merged) == ["AUTH_KEY", "REGION"]
    assert merged["REGION"].value == "eu-central-1"
    assert merged["AUTH_KEY"].value == "platform-secret"


def test_merge_is_deterministic_regardless_of_input_order():
    rows = [
        _row("BETA", "project", "b"),
        _row("GAMMA", "project", "g"),
        _row("ALPHA", "platform", "a"),
        _row("BETA", "platform", "b-platform"),
    ]
    first = list(merge_env_var_rows(rows))
    for _ in range(5):
        assert list(merge_env_var_rows(list(reversed(rows)))) == first


def test_merge_project_only_rows_survive():
    rows = [_row("ONLY", "project", "v")]
    merged = merge_env_var_rows(rows)
    assert merged["ONLY"].value == "v"
