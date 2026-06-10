"""Tests for the PE2 provisional trigger-hour panel (long_native)."""

from __future__ import annotations

import polars as pl

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.long_native import LongNativeConfig, _provisional_trigger_panel

DAY1 = 1_700_000_000_000 - (1_700_000_000_000 % MS_PER_DAY)  # aligned day start


def _hourly(symbol: str, start_ms: int, closes: list[float], turnover: float) -> pl.DataFrame:
    n = len(closes)
    return pl.DataFrame({
        "ts_ms": [start_ms + i * MS_PER_HOUR for i in range(n)],
        "symbol": [symbol] * n,
        "high": [c * 1.001 for c in closes],
        "low": [c * 0.95 for c in closes],
        "close": closes,
        "turnover_quote": [turnover] * n,
    })


def _features(symbol: str, day_end_ms: int, **over) -> dict:
    row = {"symbol": symbol, "ts_ms": day_end_ms, "sigma_daily_30d": None,
           "in_universe": True, "regime_on": True, "eth_regime_on": True}
    row.update(over)
    return row


def _cfg(**over) -> LongNativeConfig:
    base = {"fc_provisional_entry": True, "fc_use_sigma_threshold": True,
            "fc_min_day_return": 0.15, "fc_min_close_location": 0.7,
            "fc_top_volume_rank_max": 10}
    base.update(over)
    return LongNativeConfig(**base)


def test_trigger_fires_on_pump_and_dedupes_cluster() -> None:
    # symbol A: flat 100 for 30 hours, then jumps to 120 and stays — ret24 crosses
    # the fixed 15% threshold at the jump hour and stays crossed (cluster).
    closes = [100.0] * 30 + [120.0] * 42
    kl = pl.concat([
        _hourly("AAAUSDT", DAY1, closes, turnover=1e6),
        _hourly("BBBUSDT", DAY1, [50.0] * 72, turnover=2e6),
    ])
    day1_end = DAY1 + MS_PER_DAY
    day2_end = DAY1 + 2 * MS_PER_DAY
    feats = pl.DataFrame([
        _features("AAAUSDT", day1_end), _features("BBBUSDT", day1_end),
        _features("AAAUSDT", day2_end), _features("BBBUSDT", day2_end),
    ])
    panel = _provisional_trigger_panel(kl, feats, _cfg())
    # jump at index 30 (bar end = DAY1 + 31h) -> belongs to day 2 -> keyed by day2_end
    assert list(panel.keys()) == [day2_end]
    trigs = panel[day2_end]
    assert len(trigs) == 1  # cluster deduped to its head
    assert trigs[0] == (DAY1 + 31 * MS_PER_HOUR, "AAAUSDT")


def test_gates_block_triggers() -> None:
    closes = [100.0] * 30 + [120.0] * 42
    kl = _hourly("AAAUSDT", DAY1, closes, turnover=1e6)
    day1_end = DAY1 + MS_PER_DAY

    # out of universe at d-1
    feats = pl.DataFrame([_features("AAAUSDT", day1_end, in_universe=False)])
    assert _provisional_trigger_panel(kl, feats, _cfg()) == {}

    # regime off at d-1
    feats = pl.DataFrame([_features("AAAUSDT", day1_end, regime_on=False)])
    assert _provisional_trigger_panel(kl, feats, _cfg()) == {}

    # no prior-day feature row at all -> inner join drops the day
    feats = pl.DataFrame([_features("AAAUSDT", day1_end + 30 * MS_PER_DAY)])
    assert _provisional_trigger_panel(kl, feats, _cfg()) == {}

    # sigma threshold path: huge prior-day sigma pushes the bar above the move
    feats = pl.DataFrame([_features("AAAUSDT", day1_end, sigma_daily_30d=1.0)])
    assert _provisional_trigger_panel(kl, feats, _cfg()) == {}


def test_volume_rank_gate() -> None:
    # AAA pumps but is rank 11 by trailing turnover -> blocked at rank_max=10
    frames = [_hourly("AAAUSDT", DAY1, [100.0] * 30 + [120.0] * 42, turnover=1e3)]
    for i in range(11):
        frames.append(_hourly(f"BIG{i:02d}USDT", DAY1, [50.0] * 72, turnover=1e9))
    kl = pl.concat(frames)
    day1_end = DAY1 + MS_PER_DAY
    feats = pl.DataFrame([_features("AAAUSDT", day1_end)])
    assert _provisional_trigger_panel(kl, feats, _cfg()) == {}
    # with a permissive rank the trigger fires
    assert _provisional_trigger_panel(kl, feats, _cfg(fc_top_volume_rank_max=20)) != {}
