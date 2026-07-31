from __future__ import annotations

import pytest

from liquidity_migration.core.env_flags import (
    FALSE_ENV_VALUES,
    TRUE_ENV_VALUES,
    env_flag,
    explicitly_false_or_unset,
    reject_ambiguous_flag,
)


def test_true_and_false_sets_are_disjoint_and_cover_empty() -> None:
    assert not (TRUE_ENV_VALUES & FALSE_ENV_VALUES)
    assert "" in FALSE_ENV_VALUES


@pytest.mark.parametrize("value", sorted(TRUE_ENV_VALUES))
def test_env_flag_true_spellings(value: str) -> None:
    assert env_flag("X", environ={"X": value}) is True
    assert env_flag("X", environ={"X": f"  {value.upper()}  "}) is True


@pytest.mark.parametrize("value", sorted(FALSE_ENV_VALUES) + ["maybe", "2"])
def test_env_flag_false_and_ambiguous_spellings_are_not_truthy(value: str) -> None:
    assert env_flag("X", environ={"X": value}) is False


def test_env_flag_unset_is_false() -> None:
    assert env_flag("X", environ={}) is False


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off", " OFF "])
def test_explicitly_false_or_unset_accepts_only_false_spellings(value: str | None) -> None:
    assert explicitly_false_or_unset(value) is True


@pytest.mark.parametrize("value", ["1", "true", "maybe", "2", "enabled"])
def test_explicitly_false_or_unset_rejects_true_and_ambiguous(value: str) -> None:
    assert explicitly_false_or_unset(value) is False


def test_reject_ambiguous_flag_passes_unset_true_and_false() -> None:
    reject_ambiguous_flag("X", environ={})
    reject_ambiguous_flag("X", environ={"X": "1"})
    reject_ambiguous_flag("X", environ={"X": "off"})


def test_reject_ambiguous_flag_raises_for_unrecognised_value() -> None:
    with pytest.raises(RuntimeError, match="not a recognised boolean"):
        reject_ambiguous_flag("REAL_MONEY", environ={"REAL_MONEY": "maybe"})
