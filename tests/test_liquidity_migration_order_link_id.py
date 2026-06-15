"""Tests for liquidity_migration.order_link_id (relocated from audit bucket b13).

Findings covered:
  * exec-router-3 — order_link_id enforces "component tag must not start with 'a'".
  * test-gaps-8   — _split_order_link_id truncation/collision guard is exercised.
"""
from __future__ import annotations

import pytest

from liquidity_migration.order_link_id import (
    _split_order_link_id,
    assert_routable_component_tags,
    decode_entry_order_link_id,
)


# ──────────────────────────────────────────────────────────────────────────────
# exec-router-3 — component tags must not start with 'a'
# ──────────────────────────────────────────────────────────────────────────────
def test_assert_routable_component_tags_passes_deployed_tags() -> None:
    # The deployed continuous tags are p3/p4p3/p4p5/tp14 — none start with 'a'.
    assert_routable_component_tags(["", "p3", "p4p3", "p4p5", "tp14", "s"])  # no raise


def test_assert_routable_component_tags_raises_for_a_prefixed_tag() -> None:
    # An 'a'-prefixed component ('avg') would build lm-en-cavg-… which decodes to the
    # 'ca' ADDON sleeve, mis-routing the fill (exec-router-3). Must fail loud.
    with pytest.raises(ValueError, match="must not begin with 'a'"):
        assert_routable_component_tags(["p3", "avg"])


def test_a_prefixed_component_actually_misroutes_today() -> None:
    # Demonstrates WHY the assertion matters: the colliding link decodes to the addon
    # sleeve with a garbled component tag, which assert_routable_component_tags forbids.
    sleeve, _ts, _seq, comp = decode_entry_order_link_id("lm-en-cavg-BTC-abcde")
    assert (sleeve, comp) == ("continuous_addon", "vg")  # the mis-route the guard prevents


# ──────────────────────────────────────────────────────────────────────────────
# test-gaps-8 — _split_order_link_id truncation / collision guard
# ──────────────────────────────────────────────────────────────────────────────
def test_split_order_link_id_truncation_keeps_suffix_and_stays_under_cap() -> None:
    # A 34-char base with idx 1 and 2: the -s{idx} suffix must survive (base truncated
    # FIRST), the result must stay <=36 chars, and the two sub-orders must be DISTINCT.
    base = "lm-en-cp4p5-SOMEVERYLONGSYMBOLNAME-abc"[:34]
    assert len(base) == 34
    s1 = _split_order_link_id(base, 1)
    s2 = _split_order_link_id(base, 2)
    assert len(s1) <= 36 and len(s2) <= 36
    assert s1.endswith("-s1") and s2.endswith("-s2")  # suffix not chopped
    assert s1 != s2  # no collision


def test_split_order_link_id_no_truncation_for_short_base() -> None:
    # Current real ~24-char bases are unchanged: base preserved, suffix appended.
    base = "lm-en-cp3-BTC-abcde"
    out = _split_order_link_id(base, 3)
    assert out == f"{base}-s3"
    assert len(out) <= 36


# ──────────────────────────────────────────────────────────────────────────────
# exec-router-6 — the widened len-4/len-5 suborder-suffix strip must NOT eat a real
# ts36 tail (relocated from audit bucket iA).
# ──────────────────────────────────────────────────────────────────────────────
def test_execrouter6_six_char_ts36_tail_is_not_stripped_as_a_suffix() -> None:
    """A real ts36 tail is 6 chars for current epoch-seconds (even one starting with 'x',
    e.g. base36(2e9)='x2qxvk'); the widened len-4/len-5 strip must NOT eat it."""
    link = "lm-en-cp3-WIFUSDT-x2qxvk"  # x2qxvk = base36(2_000_000_000), a real ts36 tail
    decoded = decode_entry_order_link_id(link)
    assert decoded is not None
    sleeve, ts_ms, _seq, component = decoded
    assert sleeve == "continuous"
    assert component == "p3"  # tag intact -> the tail was treated as ts36, not stripped
    assert ts_ms == 2_000_000_000 * 1000
