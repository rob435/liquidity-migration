from __future__ import annotations

import pytest

from liquidity_migration.core.env_flags import (
    FALSE_ENV_VALUES,
    TRUE_ENV_VALUES,
    env_flag,
    env_positive_float,
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


def test_reject_ambiguous_flag_passes_unset_true_and_false() -> None:
    reject_ambiguous_flag("X", environ={})
    reject_ambiguous_flag("X", environ={"X": "1"})
    reject_ambiguous_flag("X", environ={"X": "off"})


def test_reject_ambiguous_flag_raises_for_unrecognised_value() -> None:
    with pytest.raises(RuntimeError, match="not a recognised boolean"):
        reject_ambiguous_flag("REAL_MONEY", environ={"REAL_MONEY": "maybe"})


# --------------------------------------------------------------------------
# env_positive_float remains the strict parser for the few positive-number
# environment settings that are still live.
# --------------------------------------------------------------------------


def test_an_absent_dial_takes_the_committed_default() -> None:
    assert env_positive_float("EXAMPLE_POSITIVE_FLOAT", environ={}) is None


def test_a_dial_is_read_as_its_number() -> None:
    assert env_positive_float("X", environ={"X": " 3.0 "}) == 3.0


def test_a_present_but_empty_dial_refuses_rather_than_reverting() -> None:
    # The line is there, so somebody meant to set a size. Falling back to the
    # committed default here is how a fleet trades 3.0 while its operator reads
    # the file and believes the number they deleted.
    with pytest.raises(ValueError, match="present but empty"):
        env_positive_float("EXAMPLE_POSITIVE_FLOAT", environ={"EXAMPLE_POSITIVE_FLOAT": "  "})


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-2", "3x", ""])
def test_a_dial_that_is_not_a_positive_finite_number_refuses(value: str) -> None:
    with pytest.raises(ValueError):
        env_positive_float("EXAMPLE_POSITIVE_FLOAT", environ={"EXAMPLE_POSITIVE_FLOAT": value})
