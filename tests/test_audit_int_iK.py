"""Cross-file integration regression tests for the iK audit bucket.

cli-config-7: discover-universe must reject the contradictory exclusion flags
(--exclude-defaults vs --include-excluded) at parse time instead of silently
picking the include branch in cli._universe_config_from_args. The fix groups all
four exclusion flags into one argparse mutually-exclusive group in
cli_parsers._add_discover_universe_parser, while keeping the two legacy aliases
(--exclude-majors / --include-majors) hidden (argparse.SUPPRESS) for backward
compatibility.
"""
from __future__ import annotations

import argparse

import pytest

from liquidity_migration.cli import build_parser


def _parse(args):
    return build_parser().parse_args(["discover-universe", *args])


def test_exclude_defaults_alone_parses() -> None:
    ns = _parse(["--exclude-defaults"])
    assert ns.exclude_majors is True
    assert ns.include_majors is False


def test_include_excluded_alone_parses() -> None:
    ns = _parse(["--include-excluded"])
    assert ns.include_majors is True
    assert ns.exclude_majors is False


def test_legacy_exclude_majors_alias_still_works() -> None:
    # Backward compat: the hidden --exclude-majors alias keeps setting exclude_majors.
    ns = _parse(["--exclude-majors"])
    assert ns.exclude_majors is True
    assert ns.include_majors is False


def test_legacy_include_majors_alias_still_works() -> None:
    # Backward compat: the hidden --include-majors alias keeps setting include_majors.
    ns = _parse(["--include-majors"])
    assert ns.include_majors is True
    assert ns.exclude_majors is False


def test_no_exclusion_flags_defaults_false() -> None:
    ns = _parse([])
    assert ns.exclude_majors is False
    assert ns.include_majors is False


def test_contradictory_exclude_and_include_is_parse_error() -> None:
    # The core fix: contradictory pair must hard-error rather than silently drop
    # --exclude-defaults (cli-config-7).
    with pytest.raises(SystemExit) as exc:
        _parse(["--exclude-defaults", "--include-excluded"])
    assert exc.value.code == 2


def test_contradictory_legacy_aliases_is_parse_error() -> None:
    # The contradiction is detected through the hidden aliases too.
    with pytest.raises(SystemExit) as exc:
        _parse(["--exclude-majors", "--include-majors"])
    assert exc.value.code == 2


def test_contradictory_mixed_public_and_alias_is_parse_error() -> None:
    with pytest.raises(SystemExit) as exc:
        _parse(["--include-excluded", "--exclude-majors"])
    assert exc.value.code == 2


def test_exclusion_aliases_remain_hidden_in_help() -> None:
    # The legacy aliases stay argparse.SUPPRESS: they must not appear in the
    # discover-universe help text, only the public flags do.
    parser = build_parser()
    discover_subparser = parser._subparsers._group_actions[0].choices["discover-universe"]
    help_text = discover_subparser.format_help()
    assert "--exclude-defaults" in help_text
    assert "--include-excluded" in help_text
    assert "--exclude-majors" not in help_text
    assert "--include-majors" not in help_text


def test_exclusion_flags_are_in_a_mutually_exclusive_group() -> None:
    # Structural assertion: the four exclusion flags share one mutually-exclusive
    # group so the conflict is enforced by argparse, not by ad-hoc runtime logic.
    parser = build_parser()
    discover_subparser = parser._subparsers._group_actions[0].choices["discover-universe"]
    target_options = {
        "--exclude-defaults",
        "--exclude-majors",
        "--include-excluded",
        "--include-majors",
    }
    for group in discover_subparser._mutually_exclusive_groups:
        group_options = {
            opt for action in group._group_actions for opt in action.option_strings
        }
        if target_options <= group_options:
            assert isinstance(group, argparse._MutuallyExclusiveGroup)
            break
    else:  # pragma: no cover - defensive
        pytest.fail("exclusion flags are not in a single mutually-exclusive group")
