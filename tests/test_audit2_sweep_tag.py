"""audit2 regression: FC-sweep run-dir tag must not collide on distinct values.

The old _fmt_pct_tag used f"{int(round(value*100)):03d}" — 1pp resolution plus
banker's rounding — so distinct --values (0.10, 0.104, 0.105) mapped to the same
tag -> same run_dir -> under --skip-existing the 2nd cell was mislabeled/clobbered.
The fix derives the tag from the EXACT value at 4-decimal precision, keeping it
filesystem-safe. These tests FAIL against the old code and PASS with the fix.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "long_native_sweep_fc_min_day.py"
)
_spec = importlib.util.spec_from_file_location("long_native_sweep_fc_min_day", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_fmt_pct_tag = _mod._fmt_pct_tag


def test_subpercent_values_map_to_distinct_tags():
    # The defective edge case: six distinct values that the old int-percent tag
    # collapsed into just two tags (0.10/0.104/0.105 -> '010', 0.115/0.12/0.125 -> '012').
    values = [0.10, 0.104, 0.105, 0.115, 0.12, 0.125]
    tags = [_fmt_pct_tag(v) for v in values]
    assert len(set(tags)) == 6, f"expected 6 distinct tags, got {tags}"


def test_banker_rounding_boundary_no_collision():
    # 0.105 -> 10.5; banker's rounding sent 10.5 -> 10, colliding with 0.10.
    # The exact-value tag keeps them apart.
    assert _fmt_pct_tag(0.10) != _fmt_pct_tag(0.105)


def test_tags_are_filesystem_safe():
    # No '.' or '-' that would create nested dirs / surprising paths.
    for v in [0.10, 0.105, -0.05, 0.0]:
        tag = _fmt_pct_tag(v)
        assert "." not in tag and "-" not in tag, tag
        # usable as a single path component
        assert "/" not in tag and tag == Path(tag).name


def test_representative_tag_is_stable_and_deterministic():
    # A normal input's tag is unchanged across calls and matches the documented form.
    assert _fmt_pct_tag(0.12) == _fmt_pct_tag(0.12)
    assert _fmt_pct_tag(0.12) == "0p1200"
    # Normal whole-percent values still produce clean, expected tags.
    assert _fmt_pct_tag(0.10) == "0p1000"
    assert _fmt_pct_tag(0.07) == "0p0700"
