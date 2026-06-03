"""Regression lock for the PIT membership trading-day keying (FIX A).

The daily-close signal is stamped at 00:00 UTC of the day AFTER the bar it
summarises (``volume_features`` builds ``ts_ms = day_start_ms + one period``).
PIT membership must therefore be validated against the signal's *trading day*
(the date of ``ts_ms - 1 ms``), not the stamp date. Keying on the stamp date
inflated the archive publishing lag by a full day and produced spurious
``pit_membership_fail`` rejections on the recent tail (the symptom that broke
the backtest<->paper reconciliation on 2026-05-30, e.g. HEMIUSDT).
"""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl

from liquidity_migration.volume_events_features import _attach_event_archive_membership


def _ms(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def _manifest(rows: list[tuple[str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"symbol": [r[0] for r in rows], "date": [r[1] for r in rows]},
        schema={"symbol": pl.Utf8, "date": pl.Utf8},
    )


def test_close_stamped_signal_validates_against_trading_day() -> None:
    # FOO signal stamped at 2026-05-30 00:00 == the 2026-05-29 daily close.
    # The manifest covers the trading day (05-29) but NOT the stamp day (05-30).
    features = pl.DataFrame(
        {"symbol": ["FOO"], "ts_ms": [_ms(2026, 5, 30)]},
        schema={"symbol": pl.Utf8, "ts_ms": pl.Int64},
    )
    manifest = _manifest([("FOO", "2026-05-28"), ("FOO", "2026-05-29")])
    out = _attach_event_archive_membership(features, manifest)
    assert out["tradable_membership_flag"].to_list() == [True]
    # The raw stamp-day column is still passed through the output schema.
    assert out["date"].to_list() == ["2026-05-30"]
    # The internal membership-day key must not leak into the output schema.
    assert "_membership_day" not in out.columns


def test_symbol_age_keys_on_trading_day_not_stamp_day() -> None:
    """``symbol_age_days`` / ``pit_age_days`` must be measured from the signal's
    *trading day* (date(ts_ms-1ms)), the same key the membership flag uses -- not the
    +1-day stamp ``date``. Keying the age on the stamp inflated every age by exactly one
    day, shifting the age300 listing-age gate (reconcile-ledger-4)."""
    # FOO stamped 2026-05-30 00:00 -> 05-29 trading day; first listed 2026-05-20.
    # Age from the trading day = (05-29 - 05-20) = 9, NOT (05-30 - 05-20) = 10.
    features = pl.DataFrame(
        {"symbol": ["FOO"], "ts_ms": [_ms(2026, 5, 30)]},
        schema={"symbol": pl.Utf8, "ts_ms": pl.Int64},
    )
    manifest = _manifest([("FOO", "2026-05-20"), ("FOO", "2026-05-29")])
    out = _attach_event_archive_membership(features, manifest)
    assert out["symbol_age_days"].to_list() == [9]
    assert out["pit_age_days"].to_list() == [9.0]


def test_signal_with_no_trading_day_membership_is_rejected() -> None:
    # QUX last traded 2026-05-25; the 05-29 trading day is outside coverage.
    features = pl.DataFrame(
        {"symbol": ["QUX"], "ts_ms": [_ms(2026, 5, 30)]},
        schema={"symbol": pl.Utf8, "ts_ms": pl.Int64},
    )
    manifest = _manifest([("QUX", "2026-05-24"), ("QUX", "2026-05-25")])
    out = _attach_event_archive_membership(features, manifest)
    assert out["tradable_membership_flag"].to_list() == [False]


def test_mixed_batch_keys_each_event_on_its_own_trading_day() -> None:
    features = pl.DataFrame(
        {
            "symbol": ["FOO", "BAR", "QUX"],
            "ts_ms": [_ms(2026, 5, 30), _ms(2026, 5, 29), _ms(2026, 5, 30)],
        },
        schema={"symbol": pl.Utf8, "ts_ms": pl.Int64},
    )
    manifest = _manifest(
        [
            ("FOO", "2026-05-29"),  # FOO traded the 05-29 trading day -> member
            ("BAR", "2026-05-28"),  # BAR traded the 05-28 trading day -> member
            ("QUX", "2026-05-25"),  # QUX stale -> non-member
        ]
    )
    out = _attach_event_archive_membership(features, manifest).sort("symbol")
    got = dict(zip(out["symbol"].to_list(), out["tradable_membership_flag"].to_list()))
    assert got == {"FOO": True, "BAR": True, "QUX": False}


def test_trading_day_helpers_key_on_ts_minus_1ms() -> None:
    """The shared ``_common`` helpers (the single source of truth for both live PIT
    sites) must key on ``date(ts_ms - 1ms)`` and the polars expr must agree with the
    scalar form exactly -- guards the 2026-05-30 off-by-one against re-introduction."""
    from liquidity_migration._common import trading_day_expr, trading_day_from_ts

    midnight = _ms(2026, 5, 30)  # 00:00:00.000 UTC -> summarises the 05-29 trading day
    midday = _ms(2026, 5, 30) + 12 * 3_600_000  # mid-day stamp keeps its own day

    assert trading_day_from_ts(midnight) == "2026-05-29"
    assert trading_day_from_ts(midday) == "2026-05-30"

    expr_days = (
        pl.DataFrame({"ts_ms": [midnight, midday]}, schema={"ts_ms": pl.Int64})
        .select(trading_day_expr("ts_ms").dt.strftime("%Y-%m-%d").alias("d"))["d"]
        .to_list()
    )
    assert expr_days == ["2026-05-29", "2026-05-30"]
