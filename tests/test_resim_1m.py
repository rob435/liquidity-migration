"""P0.1 1m re-simulation harness tests (tail-risk program).

Covers the four committed guarantees:
- exact-reproduction regression (independent 1h oracle == harness, plus a
  real-ledger spot check when the research data root is present);
- no-lookahead property (decisions at bar t use only <=t data);
- intrabar stop/TP ambiguity policy (adverse-first, counted, reported —
  taxonomy item 14);
- warm-state honesty (state initializes at entry; pre-entry bars unread —
  taxonomy item 15).
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from scripts.research_v3.resim_1m import (
    MS_PER_MINUTE,
    RENDER_1M_ROOT,
    Minute,
    TradeSpec,
    load_symbol_minutes,
    minute_dates,
    resolve_hourly_parity,
    resolve_intrabar,
    spec_from_row,
    validate_book,
)

MS_PER_HOUR = 3_600_000
ENTRY_TS = 1_700_000_000_000 - (1_700_000_000_000 % MS_PER_HOUR)  # aligned hour end


def make_minutes(
    hours: int,
    *,
    entry_price: float = 100.0,
    seed: int = 7,
    start_ts: int = ENTRY_TS,
    vol: float = 0.004,
) -> list[Minute]:
    """Random-walk 1m bars covering ``hours`` hours from ``start_ts``."""
    rng = random.Random(seed)
    price = entry_price
    minutes: list[Minute] = []
    for i in range(hours * 60):
        ts = start_ts + i * MS_PER_MINUTE
        o = price
        c = o * (1.0 + rng.gauss(0.0, vol))
        hi = max(o, c) * (1.0 + abs(rng.gauss(0.0, vol / 2)))
        lo = min(o, c) * (1.0 - abs(rng.gauss(0.0, vol / 2)))
        minutes.append((ts, hi, lo, c))
        price = c
    return minutes


def hourly_oracle(spec: TradeSpec, minutes: list[Minute]) -> tuple[int, float, str]:
    """Independent reference: aggregate to 1h bars, then the T-F walk verbatim."""
    hours: dict[int, list[Minute]] = {}
    for ts, hi, lo, cl in minutes:
        if ts < spec.entry_ts_ms:
            continue
        end = (ts // MS_PER_HOUR + 1) * MS_PER_HOUR
        hours.setdefault(end, []).append((ts, hi, lo, cl))
    ends = sorted(hours)
    for end in ends:
        rows = hours[end]
        hi = max(r[1] for r in rows)
        lo = min(r[2] for r in rows)
        cl = rows[-1][3]
        if spec.stop_price is not None and hi >= spec.stop_price:
            return end, spec.stop_price, "stop_loss"
        if lo <= spec.take_profit_price:
            return end, spec.take_profit_price, "take_profit"
        if end >= spec.planned_exit_ts_ms:
            return end, cl, "max_hold"
    last = ends[-1]
    return last, hours[last][-1][3], "data_end"


def spec(entry_price: float = 100.0, tp_pct: float = 0.12, hold_hours: int = 24, stop: float | None = None) -> TradeSpec:
    return TradeSpec(
        symbol="TESTUSDT",
        entry_ts_ms=ENTRY_TS,
        entry_price=entry_price,
        take_profit_price=entry_price * (1.0 - tp_pct),
        planned_exit_ts_ms=ENTRY_TS + hold_hours * MS_PER_HOUR,
        side="short",
        stop_price=stop,
    )


class TestExactReproduction:
    def test_oracle_parity_across_seeds(self) -> None:
        """The harness must equal the independent 1h oracle on random paths."""
        for seed in range(60):
            # vary vol so some paths hit TP, some ride to max_hold
            minutes = make_minutes(26, seed=seed, vol=0.002 + 0.004 * (seed % 5))
            s = spec(tp_pct=0.06)
            want = hourly_oracle(s, minutes)
            got = resolve_hourly_parity(s, iter(minutes))
            assert (got.exit_ts_ms, got.exit_price, got.exit_reason) == want, f"seed={seed}"

    def test_take_profit_lands_on_bar_end_at_tp_price(self) -> None:
        minutes = make_minutes(4, seed=1, vol=0.0)
        # force one minute inside hour 3 to touch TP intra-hour
        touched = [
            (ts, hi, 87.0 if idx == 150 else lo, cl)
            for idx, (ts, hi, lo, cl) in enumerate(minutes)
        ]
        s = spec(tp_pct=0.12)
        got = resolve_hourly_parity(s, iter(touched))
        assert got.exit_reason == "take_profit"
        assert got.exit_price == s.take_profit_price
        assert got.exit_ts_ms % MS_PER_HOUR == 0
        assert got.exit_ts_ms == (touched[150][0] // MS_PER_HOUR + 1) * MS_PER_HOUR

    def test_max_hold_at_boundary_close(self) -> None:
        minutes = make_minutes(26, seed=3, vol=0.0005)  # too quiet to hit 12% TP
        s = spec(tp_pct=0.12)
        got = resolve_hourly_parity(s, iter(minutes))
        assert got.exit_reason == "max_hold"
        assert got.exit_ts_ms == s.planned_exit_ts_ms
        boundary_minutes = [m for m in minutes if m[0] < s.planned_exit_ts_ms]
        assert got.exit_price == boundary_minutes[-1][3]
        assert got.boundary_gap is False

    def test_data_end_when_stream_stops_early(self) -> None:
        minutes = make_minutes(6, seed=4, vol=0.0005)
        s = spec(tp_pct=0.12)
        got = resolve_hourly_parity(s, iter(minutes))
        assert got.exit_reason == "data_end"
        assert got.exit_ts_ms == ENTRY_TS + 6 * MS_PER_HOUR
        assert got.exit_price == minutes[-1][3]

    def test_incomplete_hour_is_flagged_not_hidden(self) -> None:
        minutes = [m for i, m in enumerate(make_minutes(26, seed=5, vol=0.0005)) if not (70 <= i < 90)]
        got = resolve_hourly_parity(spec(tp_pct=0.12), iter(minutes))
        assert got.incomplete_hours >= 1

    def test_missing_whole_hour_is_counted(self) -> None:
        minutes = [m for m in make_minutes(26, seed=6, vol=0.0005) if (m[0] - ENTRY_TS) // MS_PER_HOUR != 2]
        got = resolve_hourly_parity(spec(tp_pct=0.12), iter(minutes))
        assert got.missing_hours == 1


class TestNoLookahead:
    def test_future_perturbation_cannot_change_resolution(self) -> None:
        for seed in (11, 12, 13, 14):
            minutes = make_minutes(26, seed=seed, vol=0.004)
            s = spec(tp_pct=0.05)
            base = resolve_hourly_parity(s, iter(minutes))
            mutated = [
                (ts, hi * 5.0, lo * 0.2, cl * 3.0) if ts >= base.exit_ts_ms else (ts, hi, lo, cl)
                for ts, hi, lo, cl in minutes
            ]
            got = resolve_hourly_parity(s, iter(mutated))
            assert (got.exit_ts_ms, got.exit_price, got.exit_reason) == (
                base.exit_ts_ms, base.exit_price, base.exit_reason,
            ), f"seed={seed}: bars at/after the exit changed the resolution"

    def test_future_perturbation_intrabar(self) -> None:
        minutes = make_minutes(26, seed=21, vol=0.004)
        s = spec(tp_pct=0.05)
        base = resolve_intrabar(s, iter(minutes))
        mutated = [
            (ts, hi * 5.0, lo * 0.2, cl * 3.0) if ts >= base.exit_ts_ms else (ts, hi, lo, cl)
            for ts, hi, lo, cl in minutes
        ]
        got = resolve_intrabar(s, iter(mutated))
        assert (got.exit_ts_ms, got.exit_price, got.exit_reason) == (
            base.exit_ts_ms, base.exit_price, base.exit_reason,
        )


class TestWarmStateHonesty:
    def test_pre_entry_bars_are_unread(self) -> None:
        minutes = make_minutes(26, seed=31, vol=0.001)
        s = spec(tp_pct=0.12)
        base = resolve_hourly_parity(s, iter(minutes))
        # prepend an extreme pre-entry hour: a naive walker would arm mfe/TP off it
        pre = [
            (ENTRY_TS - MS_PER_HOUR + i * MS_PER_MINUTE, 200.0, 1.0, 50.0)
            for i in range(60)
        ]
        got = resolve_hourly_parity(s, iter(pre + minutes))
        assert (got.exit_ts_ms, got.exit_price, got.exit_reason) == (
            base.exit_ts_ms, base.exit_price, base.exit_reason,
        )
        assert got.mae == base.mae and got.mfe == base.mfe, "pre-entry path leaked into excursions"

    def test_mae_mfe_start_at_zero(self) -> None:
        flat = [
            (ENTRY_TS + i * MS_PER_MINUTE, 100.0, 100.0, 100.0)
            for i in range(120)
        ]
        got = resolve_hourly_parity(spec(tp_pct=0.12), iter(flat))
        assert got.mae == 0.0 and got.mfe == 0.0


class TestIntrabarAmbiguityPolicy:
    def test_both_touch_same_bar_is_adverse_first_and_counted(self) -> None:
        s = spec(entry_price=100.0, tp_pct=0.10, stop=110.0)
        bars: list[Minute] = [
            (ENTRY_TS, 101.0, 99.0, 100.5),
            # one 1m bar touches BOTH the 110 stop and the 90 TP
            (ENTRY_TS + MS_PER_MINUTE, 111.0, 89.0, 100.0),
        ]
        got = resolve_intrabar(s, iter(bars))
        assert got.exit_reason == "stop_loss", "conservative ordering must be adverse-first"
        assert got.exit_price == 110.0
        assert got.ambiguous_bars == 1, "the ambiguous bar must be reported, not hidden"

    def test_tp_only_touch_exits_at_tp(self) -> None:
        s = spec(entry_price=100.0, tp_pct=0.10, stop=120.0)
        bars: list[Minute] = [
            (ENTRY_TS, 101.0, 99.0, 100.5),
            (ENTRY_TS + MS_PER_MINUTE, 100.2, 89.5, 95.0),
        ]
        got = resolve_intrabar(s, iter(bars))
        assert got.exit_reason == "take_profit"
        assert got.exit_price == s.take_profit_price
        assert got.ambiguous_bars == 0

    def test_hourly_parity_counts_ambiguity_too(self) -> None:
        s = spec(entry_price=100.0, tp_pct=0.10, stop=110.0)
        minutes = [
            (ENTRY_TS + i * MS_PER_MINUTE, 111.0 if i == 30 else 100.5, 89.0 if i == 45 else 99.8, 100.0)
            for i in range(60)
        ]
        got = resolve_hourly_parity(s, iter(minutes))
        # at 1h granularity both touches collapse into one ambiguous decision bar
        assert got.exit_reason == "stop_loss"
        assert got.ambiguous_bars == 1


REAL_DATA = RENDER_1M_ROOT.is_dir() and Path(
    Path(__file__).resolve().parents[1]
    / "reports" / "strategy-overhaul-v2" / "diagnostic-epoch-2026-07-17" / "phase3-analysis" / "barebones_ledger.parquet"
).exists()


@pytest.mark.skipif(not REAL_DATA, reason="research data roots not present on this host")
class TestRealLedgerSpotCheck:
    def test_render_book_sample_reproduces_exactly(self) -> None:
        from scripts.research_v3 import v4_shared

        book = v4_shared.load_render_book("gate_on").head(40)
        report = validate_book(book, book_name="spot_render", window_start_ts_ms=None)
        assert report["buckets"]["harness_mismatch"] == 0, report["mismatches_sample"]
        assert report["buckets"]["reproduced_exact"] > 0

    def test_barebones_sample_reproduces_exactly(self) -> None:
        import datetime as dt

        from scripts.research_v3 import common

        window_start = int(dt.datetime(2023, 3, 27, tzinfo=dt.timezone.utc).timestamp() * 1000)
        ledger = common.load_ledger("continuous").filter(
            pl_entry_after(window_start)
        ).head(40)
        report = validate_book(ledger, book_name="spot_barebones", window_start_ts_ms=window_start)
        assert report["buckets"]["harness_mismatch"] == 0, report["mismatches_sample"]


def pl_entry_after(ts: int):  # tiny helper kept module-level for reuse
    import polars as pl

    return pl.col("entry_ts_ms") >= ts


class TestSpecHelpers:
    def test_spec_from_row_defaults_planned_exit(self) -> None:
        row = {
            "symbol": "AAAUSDT", "entry_ts_ms": ENTRY_TS, "entry_price": 2.0,
            "take_profit_price": 1.76, "planned_exit_ts_ms": None, "side": "short",
        }
        s = spec_from_row(row)
        assert s.planned_exit_ts_ms == ENTRY_TS + 24 * MS_PER_HOUR

    def test_minute_dates_cover_entry_and_boundary(self) -> None:
        s = spec()
        days = minute_dates(s)
        assert len(days) >= 2

    def test_load_missing_symbol_returns_empty(self, tmp_path: Path) -> None:
        frame = load_symbol_minutes("NOPEUSDT", ["2024-01-01"], tmp_path)
        assert frame.is_empty()
