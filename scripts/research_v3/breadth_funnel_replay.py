#!/usr/bin/env python3
"""Lane-1 funnel replay for the continuous_breadth_v1 candidate config.

Replays the LIVE CONTINUOUS admission funnel (the deployed producer's exact
selection semantics, not the historical crowd2 engine) hourly over seen data,
and reports the two numbers the breadth redesign needs before the config can
be committed (docs/breadth_redesign_2026-07-20.md):

  1. admitted bets/day under the candidate knobs (target >= 8-10);
  2. the average same-day pairwise correlation rho of per-bet returns
     (the power table in scripts/breadth_power.py assumes 0.15).

Funnel fidelity (translated line-for-line from continuous_demo):
- panel: the engine's own per-symbol features + cross_sectional_decile with
  the LIVE rmom quantile (0.33) and LIVE feature set; live exclusions;
- per hourly cycle: D9 -> turnover >= liq threshold -> per-component event
  trigger (turn3_pop3 / turn4_pop3 / turn4_pop5) -> 240d listing age ->
  not currently held -> top-composite, one GLOBAL per-cycle cap shared
  across components in profile order (a symbol may be taken by more than
  one component, as live);
- BTC uptrend gate at the signal day (deployed definition, fail closed);
- book state: entry at the close 2h after the signal bar start (live
  confirm-delay convention, matching render entry timestamps); exit at the
  first hourly close where the symbol has left D9, TP-12% touch intrabar,
  or the 48h max-hold cap; held symbols block re-entry while open (live has
  no post-exit cooldown).

Declared grid (all cells reported): liq_turnover_min in {500k baseline,
250k, 100k} x max_new_entries_per_cycle in {5 baseline, 10}. The "one
notch" relaxation is not numerically pinned in the design doc, so both
relaxed thresholds are reported and the config commit picks one.

rho estimator (matches the power formula's variance structure): pooled
method-of-moments over same-day pairs,
  rho_hat = [ sum_d ((S_d)^2 - Q_d) / sum_d n_d (n_d - 1) ] / sigma^2_pooled
with S_d / Q_d the day sum / sum of squares of demeaned per-bet gross
returns. Per-bet vol (bps) is reported so the power table can be re-run
with measured inputs.

Exploratory Lane-1; no alpha or promotion claim. Per-bet returns realize
the deployed shape over the render window (includes 2025+ at the same
already-rendered equity level T-A declared; V2 label-level holdout stays
unread). Outputs under reports/strategy-research-v3/t-k/<date>/.

Run through the POSIX shim:
  .venv\\Scripts\\python.exe scripts/research_v3/run_with_stub.py \\
      scripts/research_v3/breadth_funnel_replay.py --out-date 2026-07-20
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR, exact_duration_ms  # noqa: E402
from scripts.research_v3 import common  # noqa: E402

SIGNAL_START = "2023-04-04"  # first render signal day
SIGNAL_END_EXCLUSIVE = "2026-07-08"  # leaves >= 48h of bars for exits inside the root
FEATURE_WARMUP_DAYS = 40
LIQ_CELLS: tuple[float, ...] = (500_000.0, 250_000.0, 100_000.0)
CAP_CELLS: tuple[int, ...] = (5, 10)
MAX_ACTIVE = 25
ENTRY_PAUSE_AFTER_ADVERSE = 8  # live circuit breaker: net-negative covers in the window
ENTRY_PAUSE_WINDOW_MS = 1440 * 60_000
RMOM_QUANTILE_LIVE = 0.33
TAKE_PROFIT_PCT = 0.12
MAX_HOLD_HOURS = 48
ENTRY_DELAY_HOURS = 2  # signal bar start -> entry close (live confirm-delay convention)
AGE_DAYS_MIN = 240
COMPONENTS: tuple[tuple[str, str, float], ...] = (
    # (key, entry_event_trigger, weight) — ACTIVE_CONTINUOUS_COMPONENTS, age 240 / TP 12% shared.
    ("turn3p3", "turn3_pop3", 1.0 / 3.0),
    ("turn4p3", "turn4_pop3", 2.0 / 9.0),
    ("turn4p5", "turn4_pop5", 4.0 / 9.0),
)


def build_live_panel(root: Path, out_dir: Path) -> pl.DataFrame:
    """The live signal panel: engine features + cross-sectional decile at rmom 0.33."""
    from liquidity_migration.continuous_events import (
        DEFAULT_EXCLUDED_SYMBOLS,
        FEATURES,
        _read_window,
        cross_sectional_decile,
        per_symbol_timeseries_features,
        require_stable_residual_momentum,
    )

    cache = out_dir / "tk_live_panel_rmom33.parquet"
    if cache.is_file():
        return pl.read_parquet(cache)
    start_ms = int(dt.datetime.fromisoformat(SIGNAL_START + "T00:00:00+00:00").timestamp() * 1000)
    end_ms = int(
        dt.datetime.fromisoformat(SIGNAL_END_EXCLUSIVE + "T00:00:00+00:00").timestamp() * 1000
    ) + (MAX_HOLD_HOURS + ENTRY_DELAY_HOURS + 2) * MS_PER_HOUR
    k = _read_window(
        root, "klines_1h",
        start_ms=start_ms - FEATURE_WARMUP_DAYS * MS_PER_DAY,
        end_ms=end_ms,
        columns=["ts_ms", "symbol", "close", "turnover_quote"],
    )
    k = k.filter(~pl.col("symbol").is_in(list(DEFAULT_EXCLUDED_SYMBOLS)))
    features = per_symbol_timeseries_features(k)
    rmom = pl.read_parquet(root / "residual_momentum.parquet")
    rmom = require_stable_residual_momentum(rmom, source=root / "residual_momentum.parquet")
    rmom = rmom.rename({"ts_ms": "day_ts"})
    panel = cross_sectional_decile(
        features.filter(pl.col("ts_ms") >= start_ms),
        rmom,
        rmom_quantile=RMOM_QUANTILE_LIVE,
        feature_set=FEATURES,
    )
    panel.write_parquet(cache)
    return panel


def event_mask(trigger: str, frame: pl.DataFrame) -> pl.DataFrame:
    from liquidity_migration.continuous_events import _entry_event_expr

    return frame.filter(_entry_event_expr(trigger))


def simulate_cell(
    d9_by_ts: dict[int, pl.DataFrame],
    ts_grid: list[int],
    bars: dict[str, dict[str, Any]],
    listing_ts: dict[str, int],
    trend_lookup: dict[int, float],
    *,
    liq_min: float,
    cap: int,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Hourly live-selection simulation for one (liq, cap) cell."""
    age_ms = exact_duration_ms(days=AGE_DAYS_MIN)
    open_positions: dict[tuple[str, str], dict[str, Any]] = {}
    held_symbols: set[str] = set()
    bets: list[dict[str, Any]] = []
    adverse_exit_ts: list[int] = []  # net-negative cover times (circuit breaker input)
    paused_cycles = 0
    funnel_totals = {"d9": 0, "liquidity": 0, "event": 0, "age": 0, "available": 0, "admitted": 0}
    gate_open_days: set[int] = set()
    blocked_days: set[int] = set()

    def close_at(symbol: str, ts: int) -> float | None:
        b = bars.get(symbol)
        if b is None:
            return None
        i = bisect.bisect_left(b["ends"], ts)
        if i < len(b["ends"]) and b["ends"][i] == ts:
            return float(b["close"][i])
        return None

    for ts in ts_grid:
        d9_now = d9_by_ts.get(ts)
        d9_symbols = set(d9_now["symbol"].to_list()) if d9_now is not None else set()
        # --- exits at this hour's close (left-decile / TP touch / 48h cap) ---
        for key in list(open_positions):
            position = open_positions[key]
            symbol = position["symbol"]
            b = bars.get(symbol)
            if b is None:
                continue
            entry_ts = position["entry_ts"]
            eval_ts = ts + ENTRY_DELAY_HOURS * MS_PER_HOUR  # same clock as entries
            if eval_ts <= entry_ts:
                continue
            i = bisect.bisect_left(b["ends"], eval_ts)
            exit_price = None
            exit_reason = None
            if i < len(b["ends"]) and b["ends"][i] == eval_ts:
                lo = float(b["low"][i])
                tp = position["tp_price"]
                if lo <= tp:
                    exit_price, exit_reason = tp, "take_profit"
                elif symbol not in d9_symbols:
                    exit_price, exit_reason = float(b["close"][i]), "left_decile"
                elif eval_ts - entry_ts >= MAX_HOLD_HOURS * MS_PER_HOUR:
                    exit_price, exit_reason = float(b["close"][i]), "max_hold"
            elif eval_ts - entry_ts >= MAX_HOLD_HOURS * MS_PER_HOUR:
                # No bar (delisting): close at the last available price.
                j = min(max(i - 1, 0), len(b["ends"]) - 1)
                exit_price, exit_reason = float(b["close"][j]), "data_end"
            if exit_price is not None:
                gross = (position["entry_price"] - exit_price) / position["entry_price"]
                bets.append({**position, "exit_reason": exit_reason, "gross_return": gross,
                             "exit_ts": eval_ts})
                if gross < 0.0:
                    adverse_exit_ts.append(eval_ts)
                del open_positions[key]
        held_symbols = {p["symbol"] for p in open_positions.values()}

        if d9_now is None or d9_now.is_empty():
            continue
        day = (ts // MS_PER_DAY) * MS_PER_DAY
        trend = trend_lookup.get(day)
        if trend is None or trend <= 0.0:
            blocked_days.add(day)
            continue
        gate_open_days.add(day)

        funnel_totals["d9"] += d9_now.height
        liquid = d9_now.filter(pl.col("turnover_quote") >= liq_min)
        funnel_totals["liquidity"] += liquid.height
        # Live circuit breaker: pause fresh entries after >= N net-negative
        # covers in the trailing window (stateless, recomputed per cycle).
        eval_now = ts + ENTRY_DELAY_HOURS * MS_PER_HOUR
        recent_adverse = sum(1 for t in adverse_exit_ts[-64:] if eval_now - t <= ENTRY_PAUSE_WINDOW_MS)
        if recent_adverse >= ENTRY_PAUSE_AFTER_ADVERSE:
            paused_cycles += 1
            continue
        room = MAX_ACTIVE - len(open_positions)
        if room <= 0:
            continue
        admitted_this_cycle = 0
        entry_ts = ts + ENTRY_DELAY_HOURS * MS_PER_HOUR
        cycle_cap = min(cap, room)
        for component, trigger, weight in COMPONENTS:
            if admitted_this_cycle >= cycle_cap:
                break
            eventful = event_mask(trigger, liquid)
            funnel_totals["event"] += eventful.height
            aged = eventful.filter(
                pl.col("symbol").map_elements(
                    lambda s: (ts - listing_ts.get(s, ts)) >= age_ms, return_dtype=pl.Boolean
                )
            )
            funnel_totals["age"] += aged.height
            available = aged.filter(~pl.col("symbol").is_in(sorted(held_symbols)))
            funnel_totals["available"] += available.height
            for row in available.sort("composite", descending=True).iter_rows(named=True):
                if admitted_this_cycle >= cycle_cap:
                    break
                symbol = str(row["symbol"])
                entry_price = close_at(symbol, entry_ts)
                if entry_price is None or entry_price <= 0.0:
                    continue
                key = (symbol, component)
                if key in open_positions:
                    continue
                open_positions[key] = {
                    "symbol": symbol,
                    "component": component,
                    "weight": weight,
                    "signal_ts": ts,
                    "entry_ts": entry_ts,
                    "entry_price": entry_price,
                    "tp_price": entry_price * (1.0 - TAKE_PROFIT_PCT),
                    "composite": float(row["composite"]),
                }
                admitted_this_cycle += 1
        funnel_totals["admitted"] += admitted_this_cycle

    # Flush still-open positions as pending (excluded from rho; counted).
    pending = len(open_positions)
    frame = pl.from_dicts(bets, infer_schema_length=None) if bets else pl.DataFrame()
    stats = {
        "funnel_totals": funnel_totals,
        "gate_open_days": len(gate_open_days),
        "gate_blocked_days": len(blocked_days),
        "pending_open_at_end": pending,
        "paused_cycles": paused_cycles,
    }
    return frame, stats


def rho_estimate(bets: pl.DataFrame) -> dict[str, Any]:
    """Pooled same-day pairwise MoM correlation of per-bet gross returns."""
    if bets.is_empty():
        return {"rho": None, "n_bets": 0}
    frame = bets.with_columns(((pl.col("entry_ts") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
    returns = frame["gross_return"].to_numpy()
    mu = float(returns.mean())
    sigma2 = float(((returns - mu) ** 2).mean())
    cross_num = 0.0
    cross_den = 0.0
    multi_days = 0
    for _day, part in frame.partition_by("day", as_dict=True).items():
        r = part["gross_return"].to_numpy() - mu
        n = len(r)
        if n < 2:
            continue
        multi_days += 1
        s = float(r.sum())
        q = float((r**2).sum())
        cross_num += s * s - q
        cross_den += n * (n - 1)
    rho = (cross_num / cross_den) / sigma2 if cross_den > 0 and sigma2 > 0 else None
    return {
        "rho": rho,
        "n_bets": bets.height,
        "days_with_2plus_bets": multi_days,
        "per_bet_vol_bps": 10_000.0 * sigma2**0.5,
        "per_bet_mean_bps_context_only": 10_000.0 * mu,
    }


def main() -> int:
    from liquidity_migration.continuous_events import _btc_trend_returns, _read_window

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=common.DEFAULT_DATA_ROOT)
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    args = parser.parse_args()
    out_dir = common.REPORT_ROOT / "t-k" / args.out_date
    out_dir.mkdir(parents=True, exist_ok=True)

    panel = build_live_panel(args.data_root, out_dir)
    print(f"live panel: {panel.shape}", flush=True)
    d9 = panel.filter(pl.col("decile") == 9)
    d9_by_ts = {
        int(key[0] if isinstance(key, tuple) else key): part
        for key, part in d9.partition_by("ts_ms", as_dict=True).items()
    }
    ts_grid = sorted(d9_by_ts)
    print(f"D9 rows {d9.height} over {len(ts_grid)} hourly cycles", flush=True)

    # Bars for entries/exits: only symbols that ever reach D9 (memory bound).
    d9_symbols_all = sorted(set(d9["symbol"].to_list()))
    start_ms = ts_grid[0]
    end_ms = ts_grid[-1] + (MAX_HOLD_HOURS + ENTRY_DELAY_HOURS + 2) * MS_PER_HOUR
    k = _read_window(
        args.data_root, "klines_1h", start_ms=start_ms, end_ms=end_ms,
        columns=["ts_ms", "symbol", "close", "low"],
    ).filter(pl.col("symbol").is_in(d9_symbols_all))
    bars: dict[str, dict[str, Any]] = {}
    for key, part in k.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        bars[symbol] = {
            "ends": [int(v) + MS_PER_HOUR for v in part["ts_ms"].to_list()],
            "close": part["close"].to_list(),
            "low": part["low"].to_list(),
        }
    print(f"bars for {len(bars)} symbols", flush=True)

    listing_frame = (
        _read_window(
            args.data_root, "klines_1h",
            start_ms=0, end_ms=end_ms, columns=["ts_ms", "symbol"],
        )
        .filter(pl.col("symbol").is_in(d9_symbols_all))
        .group_by("symbol")
        .agg(pl.col("ts_ms").min().alias("first_ts"))
    )
    listing_ts = {str(s): int(t) for s, t in listing_frame.iter_rows()}
    del k, listing_frame

    btc = _read_window(
        args.data_root, "klines_1h",
        start_ms=start_ms - 45 * MS_PER_DAY, end_ms=end_ms,
        columns=["ts_ms", "symbol", "close"],
    ).filter(pl.col("symbol") == "BTCUSDT")
    trend_lookup = _btc_trend_returns(btc, lookback_days=30)

    grid_rows: list[dict[str, Any]] = []
    daily_frames: list[pl.DataFrame] = []
    for liq in LIQ_CELLS:
        for cap in CAP_CELLS:
            bets, stats = simulate_cell(
                d9_by_ts, ts_grid, bars, listing_ts, trend_lookup, liq_min=liq, cap=cap
            )
            rho = rho_estimate(bets)
            if not bets.is_empty():
                daily = (
                    bets.with_columns(((pl.col("entry_ts") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
                    .group_by("day")
                    .agg(pl.len().alias("n_bets"), pl.col("symbol").n_unique().alias("n_symbols"))
                    .with_columns(pl.lit(liq).alias("liq_min"), pl.lit(cap).alias("cap"))
                )
                daily_frames.append(daily)
            open_days = max(stats["gate_open_days"], 1)
            row = {
                "liq_min": liq,
                "cap": cap,
                "bets_total": rho["n_bets"],
                "gate_open_days": stats["gate_open_days"],
                "gate_blocked_days": stats["gate_blocked_days"],
                "bets_per_gate_open_day": rho["n_bets"] / open_days,
                # Denominator: gate-open days (open days without a single bet count as < 8).
                "share_open_days_ge8": (
                    int((daily["n_bets"] >= 8).sum()) / open_days if not bets.is_empty() else 0.0
                ),
                "rho": rho.get("rho"),
                "days_with_2plus_bets": rho.get("days_with_2plus_bets"),
                "per_bet_vol_bps": rho.get("per_bet_vol_bps"),
                "per_bet_mean_bps_context_only": rho.get("per_bet_mean_bps_context_only"),
                "pending_open_at_end": stats["pending_open_at_end"],
                "paused_cycles": stats["paused_cycles"],
                **{f"funnel_{k_}": v for k_, v in stats["funnel_totals"].items()},
            }
            grid_rows.append(row)
            print(f"cell liq={liq:.0f} cap={cap}: {json.dumps({k_: (round(v_, 4) if isinstance(v_, float) else v_) for k_, v_ in row.items()})}", flush=True)

    grid = pl.from_dicts(grid_rows, infer_schema_length=None)
    grid_path = out_dir / "tk_funnel_grid.csv"
    grid.write_csv(grid_path)
    daily_path = out_dir / "tk_daily_bets.parquet"
    if daily_frames:
        pl.concat(daily_frames, how="vertical").write_parquet(daily_path)

    common.write_manifest(
        out_dir,
        kind="strategy_research_v6_tk_breadth_funnel_replay",
        inputs={
            "data_root": str(args.data_root),
            "residual_momentum": common.sha256_file(args.data_root / "residual_momentum.parquet"),
            "signal_window": [SIGNAL_START, SIGNAL_END_EXCLUSIVE],
        },
        params={
            "rmom_quantile_live": RMOM_QUANTILE_LIVE,
            "components": [list(c) for c in COMPONENTS],
            "age_days_min": AGE_DAYS_MIN,
            "take_profit_pct": TAKE_PROFIT_PCT,
            "max_hold_hours": MAX_HOLD_HOURS,
            "entry_delay_hours": ENTRY_DELAY_HOURS,
            "grid": {"liq_turnover_min": list(LIQ_CELLS), "max_new_entries_per_cycle": list(CAP_CELLS)},
            "btc_gate": "uptrend (deployed definition, fail closed)",
            "cooldown": "held-while-open only (live semantics; no post-exit cooldown)",
            "rho_estimator": "pooled same-day pairwise MoM over demeaned gross returns",
        },
        output_files={"tk_funnel_grid.csv": grid_path, **(
            {"tk_daily_bets.parquet": daily_path} if daily_frames else {}
        )},
        extra={"explicit_non_conclusions": [
            "exploratory Lane-1 funnel replay on seen data; no alpha or promotion claim",
            "per-bet returns realize the deployed shape over the already-rendered window;"
            " V2 label-level holdout stays unread",
            "the config commit, not this replay, is the registration",
        ]},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
