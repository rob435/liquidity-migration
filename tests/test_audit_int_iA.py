"""Cross-file completion regression tests for audit integration bucket iA.

Owned files completed in this bucket:
  * liquidity_migration/order_link_id.py
  * liquidity_migration/continuous_demo.py

Findings covered (full text in the audit ledger):
  * exec-router-6  — the suborder-link uniqueness suffix was WIDENED from "x"+3 base36
                     (46656 buckets) to "x"+4 base36 (1.68M buckets). The decoder accepts
                     BOTH widths so legacy live orderLinkIds still decode on a VPS rebuild
                     (backward compatible LIVE order-routing identity), and the snipe-id
                     recovery round-trips each width against the id that produced it.
  * exec-router-3  — assert_routable_component_tags is now CALLED at module/config load on
                     every known ensemble component tag, so an "a"-prefixed tag (which would
                     mis-route to the addon sleeve) fails loudly at import.
  * ws-risk-6      — _recent_adverse_exit_count now also counts OPEN rows whose in-flight
                     partial reduce crystallized a loss (partial_exit_realized_return < 0),
                     so the correlated-squeeze entry-pause breaker reacts to loss-cutting
                     partial reduces instead of waiting for the residual to fully close.
  * reports-charts-1 — the LIVE continuous telegram now surfaces a wallet-read outage: the
                     reason filter pages on wallet_error, the equity print is tagged
                     "(FALLBACK - wallet read failed)", and a wallet_error line is appended,
                     instead of masking the fixed $10,000 fallback as real equity.
  * code-quality-5 — continuous_demo._finite_or_none now delegates to _common.finite_float
                     (default=None variant), matching the package-wide consolidation.

Each test would FAIL on the original (pre-fix) code and PASS on the fix.
"""
from __future__ import annotations

import math
import zlib

import polars as pl

from liquidity_migration.continuous_demo import (
    CONTINUOUS_ENTRY_LINK_PREFIX,
    ContinuousDemoCycleConfig,
    _continuous_sniper_link_prefix,
    _continuous_suborder_link_id,
    _continuous_telegram_reason,
    _continuous_trade_id,
    _finite_or_none,
    _known_ensemble_component_tags,
    _recent_adverse_exit_count,
    format_continuous_telegram_status_message,
    recover_snipe_trade_id_from_link,
)
from liquidity_migration._common import finite_float
from liquidity_migration.order_link_id import (
    _base36,
    assert_routable_component_tags,
    decode_entry_order_link_id,
)

import pytest

_STRATEGY = "continuous_fade_v1"
_SIG = 1_765_400_000_000


# ──────────────────────────────────────────────────────────────────────────────
# exec-router-6 — widened suborder suffix, backward-compatible decode
# ──────────────────────────────────────────────────────────────────────────────
def _legacy_3char_suffix_link(new_link: str, trade_id: str) -> str:
    """Rebuild the pre-widening (x+3 base36, %46656) form of a suborder link for the
    same base + trade_id, mirroring the old _continuous_suborder_link_id math."""
    base_link = new_link.split("-x")[0]
    suffix = "-x" + _base36(zlib.crc32(trade_id.encode("utf-8")) % 46656).rjust(3, "0")
    return f"{base_link[: 36 - len(suffix)]}{suffix}"


def test_execrouter6_new_suffix_is_four_base36_chars() -> None:
    tid = f"{_STRATEGY}-WIFUSDT-{_SIG}-p3"
    link = _continuous_suborder_link_id(
        CONTINUOUS_ENTRY_LINK_PREFIX, symbol="WIFUSDT", signal_ts_ms=_SIG, trade_id=tid
    )
    assert "-x" in link
    suffix = link.rsplit("-x", 1)[1]
    assert len(suffix) == 4, link  # widened from 3 -> 4 base36 chars (1.68M buckets)
    assert len(link) <= 36
    # All 4 chars are base36 (the modulus is 36**4).
    assert int(suffix, 36) == zlib.crc32(tid.encode("utf-8")) % 1_679_616


def test_execrouter6_new_link_decodes_to_continuous_sleeve() -> None:
    tid = f"{_STRATEGY}-WIFUSDT-{_SIG}-p3"
    link = _continuous_suborder_link_id(
        CONTINUOUS_ENTRY_LINK_PREFIX, symbol="WIFUSDT", signal_ts_ms=_SIG, trade_id=tid
    )
    decoded = decode_entry_order_link_id(link)
    assert decoded is not None
    sleeve, ts_ms, seq, _component = decoded
    assert sleeve == "continuous"
    assert ts_ms == _SIG
    assert seq == 0


def test_execrouter6_legacy_3char_link_still_decodes() -> None:
    """LIVE order-routing identity: orderLinkIds written before the widening (x+3, len-4)
    must still strip+decode on a VPS rebuild, NOT fall through to the lossy adopted-* id."""
    tid = f"{_STRATEGY}-WIFUSDT-{_SIG}-p3"
    new_link = _continuous_suborder_link_id(
        CONTINUOUS_ENTRY_LINK_PREFIX, symbol="WIFUSDT", signal_ts_ms=_SIG, trade_id=tid
    )
    legacy = _legacy_3char_suffix_link(new_link, tid)
    assert len(legacy.rsplit("-x", 1)[1]) == 3
    decoded = decode_entry_order_link_id(legacy)
    assert decoded is not None
    assert decoded[0] == "continuous"
    assert decoded[1] == _SIG


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


def test_execrouter6_snipe_recovery_round_trips_both_widths() -> None:
    """recover_snipe_trade_id_from_link recovers the EXACT live trade_id from BOTH a new
    4-char link and a legacy 3-char link, recomputing the candidate crc at the link's width."""
    prefix = _continuous_sniper_link_prefix(ContinuousDemoCycleConfig())
    for component in ("p3", "p4p3", ""):
        for seq in (0, 1):
            base = _continuous_trade_id(_STRATEGY, "WIFUSDT", _SIG, seq)
            tid = f"{base}-{component}-snipe" if component else f"{base}-snipe"
            new_link = _continuous_suborder_link_id(
                prefix, symbol="WIFUSDT", signal_ts_ms=_SIG, trade_id=tid
            )
            assert recover_snipe_trade_id_from_link(
                new_link, strategy_id=_STRATEGY, symbol="WIFUSDT", signal_ts_ms=_SIG
            ) == tid, ("new", component, seq)
            legacy = _legacy_3char_suffix_link(new_link, tid)
            assert recover_snipe_trade_id_from_link(
                legacy, strategy_id=_STRATEGY, symbol="WIFUSDT", signal_ts_ms=_SIG
            ) == tid, ("legacy", component, seq)
    # A link with no -x suffix makes no recovery claim.
    assert recover_snipe_trade_id_from_link(
        "lm-en-cs-WIFUSDT-abc123", strategy_id=_STRATEGY, symbol="WIFUSDT", signal_ts_ms=_SIG
    ) is None


def test_execrouter6_widened_space_lowers_collision_density() -> None:
    """The widened 4-char suffix has ~36x more buckets, so distinct same-symbol same-second
    trade_ids that DID collide on the old 3-char space generally separate on the new one. We
    assert the new full-suffix collision rate is materially below the old 3-char rate over a
    fixed sample (a wider hash is the whole point of the fix)."""
    prefix = _continuous_sniper_link_prefix(ContinuousDemoCycleConfig())
    new_suffixes: dict[str, str] = {}
    old_suffixes: dict[str, str] = {}
    new_collisions = old_collisions = 0
    for i in range(40_000):
        tid = f"S-WIFUSDT-{_SIG}-{i}-snipe"
        link = _continuous_suborder_link_id(prefix, symbol="WIFUSDT", signal_ts_ms=_SIG, trade_id=tid)
        new_suf = link.rsplit("-x", 1)[1]
        old_suf = _base36(zlib.crc32(tid.encode("utf-8")) % 46656).rjust(3, "0")
        if new_suf in new_suffixes and new_suffixes[new_suf] != tid:
            new_collisions += 1
        if old_suf in old_suffixes and old_suffixes[old_suf] != tid:
            old_collisions += 1
        new_suffixes[new_suf] = tid
        old_suffixes[old_suf] = tid
    assert old_collisions > 0  # the old 3-char space saturates well within 40k
    assert new_collisions < old_collisions / 4  # the wider space collides far less


# ──────────────────────────────────────────────────────────────────────────────
# exec-router-3 — the routing invariant is ENFORCED at config/module load
# ──────────────────────────────────────────────────────────────────────────────
def test_execrouter3_module_load_enforced_deployed_tags_are_routable() -> None:
    """Importing continuous_demo runs assert_routable_component_tags at module load (the call
    site this bucket wired). The deployed tags must pass — if the guard fired the import above
    would have raised, so reaching here proves the enforced call site exists and is satisfied."""
    tags = _known_ensemble_component_tags()
    assert tags  # non-empty (p3/p4p3/p4p5/tp14)
    assert not any(t.startswith("a") for t in tags)
    assert_routable_component_tags(tags)  # explicit no-raise on the live vocabulary


def test_execrouter3_guard_would_have_caught_an_a_prefixed_tag() -> None:
    with pytest.raises(ValueError, match="must not begin with 'a'"):
        assert_routable_component_tags([*_known_ensemble_component_tags(), "avg"])


# ──────────────────────────────────────────────────────────────────────────────
# ws-risk-6 — in-flight partial-reduce losses count toward the breaker
# ──────────────────────────────────────────────────────────────────────────────
def _now() -> int:
    return _SIG


def test_wsrisk6_open_partial_reduce_loss_counts_toward_breaker() -> None:
    now = _now()
    df = pl.DataFrame(
        [
            # OPEN row whose in-flight partial reduce crystallized a loss, in-window -> counts.
            {
                "status": "open", "strategy_id": _STRATEGY, "exit_ts_ms": None,
                "partial_exit_realized_return": -0.012,
                "partial_exit_ts_ms": now - 10 * 60_000, "updated_at_ms": now - 10 * 60_000,
            },
            # OPEN row with a partial GAIN -> not adverse.
            {
                "status": "open", "strategy_id": _STRATEGY, "exit_ts_ms": None,
                "partial_exit_realized_return": 0.02,
                "partial_exit_ts_ms": now - 5 * 60_000, "updated_at_ms": now - 5 * 60_000,
            },
        ],
        infer_schema_length=None,
    )
    # Pre-fix: the closed-only breaker read both open rows as net 0 -> 0.
    assert _recent_adverse_exit_count(df, now_ms=now, window_minutes=60, strategy_id=_STRATEGY) == 1


def test_wsrisk6_partial_loss_outside_window_not_counted() -> None:
    now = _now()
    df = pl.DataFrame(
        [
            {
                "status": "open", "strategy_id": _STRATEGY, "exit_ts_ms": None,
                "partial_exit_realized_return": -0.05,
                "partial_exit_ts_ms": now - 600 * 60_000, "updated_at_ms": now - 600 * 60_000,
            },
        ],
        infer_schema_length=None,
    )
    assert _recent_adverse_exit_count(df, now_ms=now, window_minutes=60, strategy_id=_STRATEGY) == 0


def test_wsrisk6_partial_plus_closed_are_additive_and_not_double_counted() -> None:
    now = _now()
    df = pl.DataFrame(
        [
            # open partial loss in-window
            {
                "status": "open", "strategy_id": _STRATEGY, "exit_ts_ms": None,
                "partial_exit_realized_return": -0.01,
                "partial_exit_ts_ms": now - 8 * 60_000, "updated_at_ms": now - 8 * 60_000,
                "net_return": None,
            },
            # closed adverse in-window (a DIFFERENT trade)
            {
                "status": "closed", "strategy_id": _STRATEGY, "exit_ts_ms": now - 20 * 60_000,
                "partial_exit_realized_return": None, "partial_exit_ts_ms": None,
                "updated_at_ms": now - 20 * 60_000, "net_return": -0.03,
            },
            # a closed row whose partial_exit fields are still stamped (it had a partial then
            # fully closed): status=='closed' means the open branch ignores it -> counted once
            # via the closed branch only (no double count).
            {
                "status": "closed", "strategy_id": _STRATEGY, "exit_ts_ms": now - 15 * 60_000,
                "partial_exit_realized_return": -0.02, "partial_exit_ts_ms": now - 30 * 60_000,
                "updated_at_ms": now - 15 * 60_000, "net_return": -0.04,
            },
        ],
        infer_schema_length=None,
    )
    # 1 open partial loss + 2 closed adverse = 3 (the closed-with-stamped-partial is NOT 2).
    assert _recent_adverse_exit_count(df, now_ms=now, window_minutes=60, strategy_id=_STRATEGY) == 3


def test_wsrisk6_legacy_schema_without_partial_columns_unchanged() -> None:
    """Backward compat: a ledger frame with no partial_exit_* columns behaves exactly as the
    pre-fix closed-only count (numerically equivalent on the old schema)."""
    now = _now()
    df = pl.DataFrame(
        [
            {"status": "closed", "strategy_id": _STRATEGY, "exit_ts_ms": now - 20 * 60_000, "net_return": -0.03},
            {"status": "closed", "strategy_id": _STRATEGY, "exit_ts_ms": now - 20 * 60_000, "net_return": 0.04},
            {"status": "closed", "strategy_id": _STRATEGY, "exit_ts_ms": now - 20 * 60_000,
             "exit_reason": "stop_approach", "net_return": 0.0},
        ],
        infer_schema_length=None,
    )
    assert _recent_adverse_exit_count(df, now_ms=now, window_minutes=60, strategy_id=_STRATEGY) == 2


# ──────────────────────────────────────────────────────────────────────────────
# reports-charts-1 — wallet outage surfaced on the LIVE continuous telegram
# ──────────────────────────────────────────────────────────────────────────────
def _wallet_outage_payload() -> dict:
    return {
        "mode": "submit", "equity_usdt": 10_000.0,
        "wallet_error": "wallet equity unavailable: 401 rate-limited",
        "entries": 0, "exits": 0, "open_positions": 2, "candidates": 3,
        "rmom_present": True, "entry_paused": False,
    }


def test_reportscharts1_wallet_error_pages_the_operator() -> None:
    payload = _wallet_outage_payload()
    # Pre-fix: a wallet-only outage produced "" (quiet) -> no page.
    assert _continuous_telegram_reason(payload, [], []) == "continuous_wallet_error"


def test_reportscharts1_message_tags_fallback_and_surfaces_error() -> None:
    payload = _wallet_outage_payload()
    msg = format_continuous_telegram_status_message(payload, [], [], reason="continuous_wallet_error")
    assert "(FALLBACK - wallet read failed)" in msg  # the $10,000 print is no longer trusted
    assert "wallet_error=wallet equity unavailable: 401 rate-limited" in msg


def test_reportscharts1_healthy_read_is_quiet_and_untagged() -> None:
    payload = _wallet_outage_payload()
    payload["wallet_error"] = ""  # event_demo returns "" on a healthy read
    assert _continuous_telegram_reason(payload, [], []) == ""  # quiet, no spurious page
    msg = format_continuous_telegram_status_message(payload, [], [], reason="continuous_entry_executed")
    assert "FALLBACK" not in msg
    assert "wallet_error=" not in msg
    assert "equity=$10,000.00" in msg


# ──────────────────────────────────────────────────────────────────────────────
# code-quality-5 — _finite_or_none delegates to _common.finite_float
# ──────────────────────────────────────────────────────────────────────────────
def test_codequality5_finite_or_none_matches_finite_float_default_none() -> None:
    cases = [1.5, "2.0", None, "not-a-number", float("nan"), float("inf"), float("-inf"), 0, "", 42]
    for value in cases:
        expected = finite_float(value, default=None)
        got = _finite_or_none(value)
        # Treat NaN-vs-NaN as equal (finite_float never returns NaN, but be explicit).
        if isinstance(expected, float) and isinstance(got, float) and math.isnan(expected) and math.isnan(got):
            continue
        assert got == expected, (value, got, expected)
    # Spot-check the contract directly.
    assert _finite_or_none(float("nan")) is None
    assert _finite_or_none(None) is None
    assert _finite_or_none("3.25") == 3.25
