"""Regression tests for audit bucket b02.

Owned modules:
  - liquidity_migration/continuous_events.py
  - liquidity_migration/cli_parsers.py
  - scripts/continuous_forward_replay_orchestrator.py

Each test targets one audit finding and would FAIL on the pre-fix code.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR
from liquidity_migration.cli import build_parser
from liquidity_migration.continuous_events import (
    ContinuousEventConfig,
    _additive_summary,
    _apply_entry_order,
    _assert_funding_one_per_settlement,
    _assert_rmom_covers_window,
    _daily_pnl_metrics,
    _listing_ts_by_symbol,
    _resolve_end_ms,
    _run_trades,
    cross_sectional_decile,
    run_continuous_event_research,
)
from liquidity_migration.storage import write_dataset
from liquidity_migration.trade_lifecycle import _indexed_price_bars_by_symbol


# ---------------------------------------------------------------------------
# shared fixtures
# ---------------------------------------------------------------------------
def _grid_klines(symbols: list[str], n_bars: int, *, price: float = 100.0) -> pl.DataFrame:
    rows = []
    for sym in symbols:
        for i in range(n_bars):
            rows.append(
                {"ts_ms": i * MS_PER_HOUR, "symbol": sym, "open": price, "high": price,
                 "low": price, "close": price}
            )
    return pl.DataFrame(rows)


def _build_root(tmp_path, *, n_symbols: int = 26, n_bars: int = 720, funding_8h: bool = True):
    """Minimal synthetic full-PIT root: klines_1h + funding + residual_momentum.parquet."""
    root = tmp_path / "root"
    root.mkdir()
    start = 1_700_000_000_000
    start -= start % MS_PER_DAY
    rows = []
    for s in range(n_symbols):
        p = 100.0 + s
        for i in range(n_bars):
            wob = 1.0 + 0.02 * ((s * 7 + i * 13) % 11 - 5) / 5.0
            p = max(1.0, p * wob)
            rows.append(
                {"ts_ms": start + i * MS_PER_HOUR, "symbol": f"S{s:02d}", "open": p,
                 "high": p * 1.01, "low": p * 0.99, "close": p, "volume_base": 1000.0,
                 "turnover_quote": 1_000_000.0}
            )
    klines = pl.DataFrame(rows)
    write_dataset(klines, root, "klines_1h")

    fund = klines.select("ts_ms", "symbol")
    if funding_8h:
        fund = fund.filter((pl.col("ts_ms") // MS_PER_HOUR) % 8 == 0)
    fund = fund.with_columns(
        pl.lit(0.0001).alias("funding_rate"), pl.lit(480).alias("funding_interval_min")
    )
    write_dataset(fund, root, "funding")

    days = sorted({(start + i * MS_PER_HOUR) // MS_PER_DAY * MS_PER_DAY for i in range(n_bars)})
    rmom_rows = [
        {"symbol": f"S{s:02d}", "ts_ms": d, "residual_momentum": (s % 13) * 0.001 - 0.006}
        for d in days
        for s in range(n_symbols)
    ]
    pl.DataFrame(rmom_rows).write_parquet(root / "residual_momentum.parquet")
    return root, start, n_bars


def _iso(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# ===========================================================================
# cli-config-5: continuous-events --end empty default is data-driven, not a stale fixed date
# ===========================================================================
def test_continuous_events_end_date_default_is_empty_data_driven() -> None:
    """The dataclass default must NOT be a hardcoded past date (was 2026-05-28, which silently
    truncated the freshest weeks as the calendar advanced). An empty default signals data-driven."""
    assert ContinuousEventConfig().end_date == ""


def test_resolve_end_ms_clamps_to_day_after_last_kline_when_default(tmp_path) -> None:
    """Empty end_date -> clamp to the day AFTER the root's last kline (end-exclusive, so the final
    full day is included). A fixed past default would have stopped earlier and dropped recent days."""
    root, start, n_bars = _build_root(tmp_path, n_symbols=4, n_bars=200)
    cfg = ContinuousEventConfig(start_date=_iso(start), end_date="")
    last_ts = start + (n_bars - 1) * MS_PER_HOUR
    expected = (last_ts // MS_PER_DAY) * MS_PER_DAY + MS_PER_DAY
    assert _resolve_end_ms(root, cfg) == expected
    # An explicit end_date is still honored verbatim (frozen/forward runs pin it).
    cfg_explicit = ContinuousEventConfig(start_date=_iso(start), end_date=_iso(start + 3 * MS_PER_DAY))
    assert _resolve_end_ms(root, cfg_explicit) == _iso_to_ms(_iso(start + 3 * MS_PER_DAY))


def _iso_to_ms(date_str: str) -> int:
    from liquidity_migration.signal_harness import _date_str_to_ms

    return _date_str_to_ms(date_str)


# ===========================================================================
# pit-data-5: stale residual_momentum.parquet must fail loudly, not silently truncate
# ===========================================================================
def test_assert_rmom_covers_window_raises_when_rmom_lags(tmp_path) -> None:
    """A present-but-stale rmom (lagging the klines window beyond tolerance) must raise rather than
    let the left-join+null-filter silently drop the newest dates from the panel."""
    start = 1_700_000_000_000
    start -= start % MS_PER_DAY
    klines = pl.DataFrame(
        {"ts_ms": [start + d * MS_PER_DAY for d in range(20)], "symbol": ["A"] * 20}
    )
    # rmom stops 10 days before the klines window end -> well beyond the 2d tolerance.
    rmom = pl.DataFrame(
        {"symbol": ["A"] * 10, "day_ts": [start + d * MS_PER_DAY for d in range(10)],
         "residual_momentum": [0.0] * 10}
    )
    with pytest.raises(RuntimeError, match="STALE"):
        _assert_rmom_covers_window(rmom, klines, start_ms=start, root=tmp_path)


def test_assert_rmom_covers_window_allows_small_lag(tmp_path) -> None:
    """A freshly rebuilt rmom legitimately trails the newest kline day by a couple of days
    (precompute shift(3)); within tolerance it must NOT raise."""
    start = 1_700_000_000_000
    start -= start % MS_PER_DAY
    klines = pl.DataFrame(
        {"ts_ms": [start + d * MS_PER_DAY for d in range(20)], "symbol": ["A"] * 20}
    )
    rmom = pl.DataFrame(
        {"symbol": ["A"] * 18, "day_ts": [start + d * MS_PER_DAY for d in range(18)],
         "residual_momentum": [0.0] * 18}
    )  # lags by 1 day, within the 2d tolerance
    _assert_rmom_covers_window(rmom, klines, start_ms=start, root=tmp_path)  # no raise


def test_build_continuous_panel_raises_on_stale_rmom(tmp_path) -> None:
    """End-to-end: a root whose rmom is stale relative to klines must error in the panel build
    instead of returning a date-truncated panel (the 2026-06-03 silent-truncation hazard)."""
    root, start, n_bars = _build_root(tmp_path, n_symbols=8, n_bars=720)
    # Truncate residual_momentum to the first 10 days only, then build a window that needs more.
    rmom = pl.read_parquet(root / "residual_momentum.parquet")
    keep_max = start + 9 * MS_PER_DAY
    rmom.filter(pl.col("ts_ms") <= keep_max).write_parquet(root / "residual_momentum.parquet")
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 4 * MS_PER_DAY), end_date=_iso(start + 28 * MS_PER_DAY)
    )
    from liquidity_migration.continuous_events import build_continuous_panel

    with pytest.raises(RuntimeError, match="STALE"):
        build_continuous_panel(root, cfg, cache=False)


# ===========================================================================
# pit-signals-5: singleton cross-section must rank the lone candidate at 0, not drop it
# ===========================================================================
def _decile_input_one_symbol_on_a_day() -> pl.DataFrame:
    """k with the feature columns cross_sectional_decile needs; one symbol on one ts_ms group."""
    return pl.DataFrame(
        {
            "symbol": ["LONE"],
            "ts_ms": [3 * MS_PER_HOUR],
            "turnover_quote": [1_000_000.0],
            "ret168": [0.01],
            "ret72": [0.0],
            "rv_168h": [0.02],
            "vov": [0.001],
            "dist_low": [0.5],
        }
    )


def test_cross_sectional_decile_keeps_singleton_group() -> None:
    """A ts_ms group that collapses to ONE surviving symbol must still emit it (ranked 0), not be
    dropped by a 0/0=NaN rank denominator. Pre-fix the (len-1)=0 division yielded NaN and
    filter(_rr <= q) silently dropped the lone candidate."""
    k = _decile_input_one_symbol_on_a_day()
    day = (int(k["ts_ms"][0]) // MS_PER_DAY) * MS_PER_DAY
    rmom = pl.DataFrame({"symbol": ["LONE"], "day_ts": [day], "residual_momentum": [-0.005]})
    out = cross_sectional_decile(k, rmom, rmom_quantile=0.5)
    assert out.height == 1
    assert out["symbol"][0] == "LONE"
    assert out["composite"][0] == pytest.approx(0.0)  # lone candidate ranks at 0, not NaN


# ===========================================================================
# code-quality-6: _additive_summary delegates headline metrics to _daily_pnl_metrics
# ===========================================================================
def _toy_trades() -> pl.DataFrame:
    base = {
        "gross_return": 0.02, "cost_return": -0.001, "funding_return": 0.0005,
        "funding_mode": "modeled", "net_return": 0.0195, "entry_ts_ms": MS_PER_DAY,
    }
    rows = []
    for d in range(1, 6):
        r = dict(base)
        r["exit_ts_ms"] = d * MS_PER_DAY
        r["entry_ts_ms"] = (d - 1) * MS_PER_DAY + MS_PER_HOUR
        r["net_return"] = 0.01 if d % 2 else -0.005
        r["gross_return"] = r["net_return"] + 0.001
        rows.append(r)
    return pl.DataFrame(rows)


def test_additive_summary_headline_metrics_match_shared_helper() -> None:
    """The headline DD/MAR/Sharpe/return block in _additive_summary must come from the single
    _daily_pnl_metrics helper (no divergent second copy). Compare against the helper applied to
    the same equity frame."""
    from liquidity_migration.continuous_events import _additive_equity

    trades = _toy_trades()
    cfg = ContinuousEventConfig(split_date=_iso(2 * MS_PER_DAY + MS_PER_HOUR))
    summary = _additive_summary(trades, cfg)
    expected = _daily_pnl_metrics(_additive_equity(trades))
    for key in ("total_return", "annualized_return", "max_drawdown", "mar", "sharpe_like",
                "worst_day_return"):
        assert summary[key] == expected[key], key


def test_additive_summary_empty_trades_returns_zeroed_headline() -> None:
    """Empty-trades path still returns the same headline keys (no KeyError, no divergent shape)."""
    summary = _additive_summary(pl.DataFrame(schema={
        "gross_return": pl.Float64, "cost_return": pl.Float64, "funding_return": pl.Float64,
        "net_return": pl.Float64, "entry_ts_ms": pl.Int64, "exit_ts_ms": pl.Int64,
    }), ContinuousEventConfig())
    assert summary["n_trades"] == 0
    assert summary["total_return"] == 0.0
    assert summary["mar"] is None


# ===========================================================================
# pit-engine-2: backtest age gate uses authoritative PIT listing, not the clamped window start
# ===========================================================================
def test_listing_ts_by_symbol_reads_first_ever_bar(tmp_path) -> None:
    root, start, n_bars = _build_root(tmp_path, n_symbols=3, n_bars=50)
    listing = _listing_ts_by_symbol(root)
    assert listing["S00"] == start  # first-ever bar, independent of any window


def test_age_gate_uses_listing_not_window_start() -> None:
    """A symbol whose authoritative listing is far older than the loaded window start must NOT be
    age-gated, even though the window's first loaded bar is recent. Pre-fix the gate measured age
    from bars[...][0] (the clamped window start) and wrongly skipped old symbols near the edge."""
    # The loaded window's first bar is at ts 0; the symbol's TRUE listing is 100 days earlier.
    bars = _indexed_price_bars_by_symbol(_grid_klines(["OLD"], 60))
    true_listing = -100 * MS_PER_DAY  # listed long before the loaded window
    entries = pl.DataFrame(
        {"symbol": ["OLD"], "ts_ms": [2 * MS_PER_HOUR], "composite": [0.9],
         "turnover_quote": [1e6], "spell_end_ts": [2 * MS_PER_HOUR]}
    )
    cfg = ContinuousEventConfig(
        age_days_min=30, max_active=5, hold_hours=1, entry_delay_hours=1,
        use_funding=False, flat_round_trip_bps=0.0,
    )
    # With authoritative listing 100d old -> NOT age-gated -> one trade.
    trades_pass, _ = _run_trades(
        entries, bars, None, cfg, listing_ts_by_symbol={"OLD": true_listing}
    )
    assert trades_pass.height == 1, "old symbol must clear the age floor on its true listing"

    # Control: without a listing source, the gate falls back to the window's first bar (ts 0),
    # which is < 30d old at the entry -> skipped. This is the pre-fix (wrong-for-old-symbols)
    # behaviour, retained only as the no-listing fallback.
    trades_fallback, _ = _run_trades(entries, bars, None, cfg, listing_ts_by_symbol=None)
    assert trades_fallback.height == 0


# ===========================================================================
# w4-w5-stages-4: candidate_sink / candidate_tape and _apply_entry_order invariants
# ===========================================================================
def test_apply_entry_order_fcfs_is_identity() -> None:
    """fcfs must be a no-op reorder (reproduces the frozen control's (ts, symbol) order)."""
    entries = pl.DataFrame(
        {"symbol": ["B", "A", "C"], "ts_ms": [0, 0, 0], "composite": [0.1, 0.9, 0.5],
         "turnover_quote": [1e6, 1e6, 1e6], "spell_end_ts": [0, 0, 0]}
    )
    out = _apply_entry_order(entries, "fcfs")
    assert out["symbol"].to_list() == ["B", "A", "C"]  # untouched


def test_apply_entry_order_is_causal_within_ts() -> None:
    """Reordering only swaps SAME-ts candidates; a later ts can never jump ahead of an earlier one."""
    entries = pl.DataFrame(
        {"symbol": ["A", "B", "C", "D"], "ts_ms": [0, 0, MS_PER_HOUR, MS_PER_HOUR],
         "composite": [0.1, 0.9, 0.2, 0.8], "turnover_quote": [1e6] * 4,
         "spell_end_ts": [0, 0, MS_PER_HOUR, MS_PER_HOUR]}
    )
    out = _apply_entry_order(entries, "composite")
    ts = out["ts_ms"].to_list()
    assert ts == sorted(ts), "timestamps must stay non-decreasing (causal)"
    # within ts=0, highest composite first: B before A
    first_ts = out.filter(pl.col("ts_ms") == 0)["symbol"].to_list()
    assert first_ts == ["B", "A"]


def test_candidate_tape_selected_rows_match_executed_trades(tmp_path) -> None:
    """The W5 Stage-0 audit contract: the tape's selected rows must equal the executed trades
    (same-code reconstruction). Pre-existing finding had NO positive test for this."""
    root, start, n_bars = _build_root(tmp_path, n_symbols=26, n_bars=720)
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 4 * MS_PER_DAY), end_date=_iso(start + 28 * MS_PER_DAY),
        hold_hours=6, entry_delay_hours=1, max_active=10, use_funding=False,
    )
    tape_path = tmp_path / "tape.parquet"
    payload = run_continuous_event_research(root, config=cfg, candidate_tape_path=tape_path)
    tape = pl.read_parquet(tape_path)
    assert tape.height >= 1
    selected = tape.filter(pl.col("selected"))
    assert selected.height == payload["n_trades"] == payload["n_candidates_selected"]
    # Every non-selected row carries a concrete rejection reason (never the "selected" sentinel).
    rejected = tape.filter(~pl.col("selected"))
    assert (rejected["reason"] != "selected").all()


def test_candidate_tape_none_path_is_additive(tmp_path) -> None:
    """With candidate_tape_path=None the run must be unchanged (n_trades identical) — the audit
    hook is purely additive."""
    root, start, n_bars = _build_root(tmp_path, n_symbols=26, n_bars=720)
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 4 * MS_PER_DAY), end_date=_iso(start + 28 * MS_PER_DAY),
        hold_hours=6, entry_delay_hours=1, max_active=10, use_funding=False,
    )
    no_tape = run_continuous_event_research(root, config=cfg)
    tape_path = tmp_path / "t.parquet"
    with_tape = run_continuous_event_research(root, config=cfg, candidate_tape_path=tape_path)
    assert no_tape["n_trades"] == with_tape["n_trades"]


def test_entry_order_composite_preserves_trade_set_when_capacity_non_binding(tmp_path) -> None:
    """When max_active does NOT bind, reordering candidates within a ts must yield the SAME executed
    trade SET as fcfs (only the order changed, not which names trade). A reorder that dropped or
    duplicated candidates would break the constant-breadth claim."""
    root, start, n_bars = _build_root(tmp_path, n_symbols=26, n_bars=720)
    common = dict(
        start_date=_iso(start + 4 * MS_PER_DAY), end_date=_iso(start + 28 * MS_PER_DAY),
        hold_hours=6, entry_delay_hours=1, max_active=10_000, use_funding=False,  # capacity non-binding
    )
    fcfs = run_continuous_event_research(root, config=ContinuousEventConfig(**common), entry_order="fcfs")
    comp = run_continuous_event_research(root, config=ContinuousEventConfig(**common), entry_order="composite")
    assert fcfs["n_trades"] == comp["n_trades"]


# ===========================================================================
# cost-funding-5: snapshot-scrape funding must be rejected (would over-charge a short book)
# ===========================================================================
def test_assert_funding_one_per_settlement_rejects_hourly_snapshots(tmp_path) -> None:
    """Hourly funding stamps with an 8h declared interval = a snapshot scrape; exact-stamp dedup
    would count every hour as a settlement and OVER-charge funding. Must raise."""
    hour = MS_PER_HOUR
    funding = pl.DataFrame(
        {"symbol": ["AAA"] * 24, "ts_ms": [h * hour for h in range(24)],
         "funding_rate": [0.001] * 24, "funding_interval_min": [480] * 24}
    )
    with pytest.raises(RuntimeError, match="SNAPSHOT"):
        _assert_funding_one_per_settlement(funding, root=tmp_path)


def test_assert_funding_one_per_settlement_accepts_one_per_settlement(tmp_path) -> None:
    """One row per 8h settlement (declared 8h) is the canonical shape — must NOT raise. A real
    4h-settling alt whose Binance row WRONGLY declares 8h must also pass (it is legitimate
    one-per-settlement data; bucketing by the stored interval was the 4h-undercount bug)."""
    hour = MS_PER_HOUR
    ok_8h = pl.DataFrame(
        {"symbol": ["AAA"] * 12, "ts_ms": [h * hour for h in range(0, 96, 8)],
         "funding_rate": [0.001] * 12, "funding_interval_min": [480] * 12}
    )
    _assert_funding_one_per_settlement(ok_8h, root=tmp_path)  # no raise
    real_4h_wrong_declared = pl.DataFrame(
        {"symbol": ["BBB"] * 24, "ts_ms": [h * hour for h in range(0, 96, 4)],
         "funding_rate": [0.001] * 24, "funding_interval_min": [480] * 24}  # declares 8h, settles 4h
    )
    _assert_funding_one_per_settlement(real_4h_wrong_declared, root=tmp_path)  # no raise


def test_funding_research_run_rejects_snapshot_root(tmp_path) -> None:
    """End-to-end: a root whose funding is an hourly snapshot scrape must fail the research run
    (with use_funding) rather than silently over-charge."""
    root, start, n_bars = _build_root(tmp_path, n_symbols=26, n_bars=720, funding_8h=False)
    cfg = ContinuousEventConfig(
        start_date=_iso(start + 4 * MS_PER_DAY), end_date=_iso(start + 20 * MS_PER_DAY),
        hold_hours=6, entry_delay_hours=1, max_active=10, use_funding=True,
    )
    with pytest.raises(RuntimeError, match="SNAPSHOT"):
        run_continuous_event_research(root, config=cfg)


# ===========================================================================
# test-gaps-4: live continuous rmom day-floor join is day-D <- rmom[D] (no off-by-one)
# ===========================================================================
def test_decile_join_attaches_decision_day_rmom_not_next_day() -> None:
    """The day-floor join must attach residual_momentum[D] to a day-D panel row, NEVER rmom[D+1].
    A one-day misalignment would pull a future rmom onto the decision day (a live look-ahead).

    Construction: two symbols, two days, one ts_ms each. rmom designates the LOW symbol per day —
    A low on day0, B low on day1. With rmom_quantile=0.5 the low symbol survives. If the join were
    off-by-one, day1 would keep A (rmom[day0]'s low) instead of B (rmom[day1]'s low)."""
    start = 1_700_000_000_000
    start -= start % MS_PER_DAY
    day0, day1 = start, start + MS_PER_DAY
    ts0, ts1 = day0 + 3 * MS_PER_HOUR, day1 + 3 * MS_PER_HOUR
    feat = {"ret168": 0.0, "ret72": 0.0, "rv_168h": 0.01, "vov": 0.001, "dist_low": 0.5}
    k = pl.DataFrame(
        [
            {"symbol": "A", "ts_ms": ts0, "turnover_quote": 1e6, **feat},
            {"symbol": "B", "ts_ms": ts0, "turnover_quote": 1e6, **feat},
            {"symbol": "A", "ts_ms": ts1, "turnover_quote": 1e6, **feat},
            {"symbol": "B", "ts_ms": ts1, "turnover_quote": 1e6, **feat},
        ]
    )
    # Per-day-VARYING rmom: A is the low (gets selected) on day0; B is the low on day1.
    rmom = pl.DataFrame(
        [
            {"symbol": "A", "day_ts": day0, "residual_momentum": -0.01},  # low on day0
            {"symbol": "B", "day_ts": day0, "residual_momentum": 0.01},
            {"symbol": "A", "day_ts": day1, "residual_momentum": 0.01},
            {"symbol": "B", "day_ts": day1, "residual_momentum": -0.01},  # low on day1
        ]
    )
    out = cross_sectional_decile(k, rmom, rmom_quantile=0.5)
    sel_day0 = set(out.filter(pl.col("ts_ms") == ts0)["symbol"].to_list())
    sel_day1 = set(out.filter(pl.col("ts_ms") == ts1)["symbol"].to_list())
    assert sel_day0 == {"A"}, "day0 must select the symbol rmom[day0] marks low"
    assert sel_day1 == {"B"}, "day1 must select rmom[day1]'s low, NOT rmom[day0]'s (off-by-one)"


# ===========================================================================
# cli-config-6: download-data / binance-proxy --start/--end boundary semantics documented
# ===========================================================================
def test_download_data_end_help_documents_exclusive_boundary() -> None:
    parser = build_parser()
    help_by_dest = {a.dest: (a.help or "") for a in _subparser_actions(parser, "download-data")}
    assert "Inclusive" in help_by_dest["start"]
    assert "Exclusive" in help_by_dest["end"]
    assert "not included" in help_by_dest["end"].lower()


def test_binance_proxy_end_help_documents_exclusive_boundary() -> None:
    parser = build_parser()
    help_by_dest = {a.dest: (a.help or "") for a in _subparser_actions(parser, "download-binance-proxy")}
    assert "Inclusive" in help_by_dest["start"]
    assert "Exclusive" in help_by_dest["end"]
    assert "not included" in help_by_dest["end"].lower()


def _subparser_actions(parser, name: str):
    import argparse

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[name]._actions
    raise AssertionError(f"subparser {name} not found")


# ===========================================================================
# test-gaps-5: order-submission safety defaults (store_true => off) are pinned
# ===========================================================================
@pytest.mark.parametrize(
    "subcommand",
    ["event-risk-cycle", "event-risk-ws", "long-native-event-demo-cycle", "continuous-event-demo-cycle"],
)
def test_order_submission_flags_default_off(subcommand: str) -> None:
    """The never-arm-by-default contract: every order-submitting daemon parser must default
    submit_orders and confirm_demo_orders to False. A store_true->default=True regression would
    silently arm live order submission and otherwise go unnoticed."""
    parser = build_parser()
    args = parser.parse_args([subcommand])
    assert args.submit_orders is False, f"{subcommand} must NOT submit orders by default"
    assert args.confirm_demo_orders is False, f"{subcommand} must NOT confirm demo orders by default"


# ===========================================================================
# forward-replay-5: orchestrator isolates per-venue drift and surfaces a stalled clock
# ===========================================================================
def _load_orchestrator():
    path = Path(__file__).resolve().parents[1] / "scripts" / "continuous_forward_replay_orchestrator.py"
    spec = importlib.util.spec_from_file_location("_orch_b02", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_orchestrator_run_venue_isolates_drift(monkeypatch) -> None:
    """A drift RuntimeError in one venue must be captured as a per-venue 'drift' status (so the
    other venue still runs), NOT propagate and abort the whole run."""
    orch = _load_orchestrator()

    def boom(venue, state_dir, fwd):
        raise RuntimeError(f"{venue}: forward-ledger drift on day 123 column equity: ...")

    monkeypatch.setattr(orch, "venue_update", boom)
    res = orch._run_venue("bybit", Path("/tmp/x"), 0)
    assert res["status"] == "drift"
    assert res["drift_detected"] is True
    assert res["appended_days"] == 0


def test_orchestrator_run_venue_isolates_generic_error(monkeypatch) -> None:
    """A non-drift failure is reported as 'error' (still isolated), not silently swallowed."""
    orch = _load_orchestrator()

    def boom(venue, state_dir, fwd):
        raise FileNotFoundError("no kline partitions")

    monkeypatch.setattr(orch, "venue_update", boom)
    res = orch._run_venue("binance", Path("/tmp/x"), 0)
    assert res["status"] == "error"
    assert res["drift_detected"] is False


def test_orchestrator_main_exits_nonzero_on_stall(monkeypatch, capsys) -> None:
    """A stalled clock (a venue that drifted/failed) must make main() exit non-zero so a manual or
    scheduled run cannot silently no-op while forward_days quietly stops advancing."""
    orch = _load_orchestrator()

    def one_ok_one_drift(venue, state_dir, fwd):
        if venue == "bybit":
            return {"venue": venue, "status": "ok", "appended_days": 3, "drift_detected": False}
        raise RuntimeError(f"{venue}: forward-ledger drift on day 9 column equity")

    monkeypatch.setattr(orch, "venue_update", one_ok_one_drift)
    monkeypatch.setattr("sys.argv", ["orch", "--venues", "bybit,binance", "--state-dir", "/tmp/sd_b02"])
    rc = orch.main()
    assert rc == 1
    out = capsys.readouterr()
    assert "binance" in out.err
