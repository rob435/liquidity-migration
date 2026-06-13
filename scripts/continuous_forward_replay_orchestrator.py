"""Forward-replay ORCHESTRATOR for the frozen continuous object (STATE open item).

Design receipt: docs/preregistration/continuous-forward-clock-spec-2026-06-09.md
— "the missing piece is the runner/orchestrator, not another decision rule."

Per venue, this runs the spec's exact sequence:
  1. re-run the four FROZEN component configs (identical to
     scripts/rebuild_winner_base_component_ledgers.py — imported, not copied)
     over full history to the root's current data end;
  2. build_full_ledger(pieces, btc_returns, btc_funding) — the banked
     BTC-hedged winner object, path-dependent from inception;
  3. update_forward_ledger(state_dir, venue, ledger) — verifies every stored
     day to 1e-9 then appends only new days (drift = hard error by design;
     a data-root revision that legitimately rewrites history requires
     archiving the state dir, per the replay module's contract);
  4. forward_readiness_summary(...) — the Tier-3-facing numbers.

Signal-replay evidence only — NOT a substitute for order-level execution
evidence (fills/slippage/funding/reconciliation), per the receipt.

    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/continuous_forward_replay_orchestrator.py \
        [--venues bybit,binance] [--forward-start 2026-06-10] [--state-dir ...]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import continuous_ensemble_rebalance_scout as scout  # noqa: E402
import rebuild_winner_base_component_ledgers as rb  # noqa: E402

import polars as pl  # noqa: E402

from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    run_continuous_event_research,
)
from liquidity_migration.continuous_forward_replay import (  # noqa: E402
    FROZEN_FORWARD_CONFIG,
    build_full_ledger,
    forward_readiness_summary,
    update_forward_ledger,
)

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
# component name (FROZEN_FORWARD_CONFIG weights key) -> rebuild-driver cell dir
NAME_TO_CELL = {
    "turn3p3": "merged_signal",
    "turn4p3": "age240_turn4pop3_crowd2",
    "turn4p5": "age240_turn4pop5_crowd2",
    "age210tp14": "age210_tp14_hold24_invvol10_crowd2",
}
CELL_OVERRIDES = {cell: ov for (_root, cell), ov in rb.CELLS.items()}


def btc_inputs(venue: str, days: list[int]) -> tuple[dict[int, float], dict[int, float]]:
    """BTC daily returns (consecutive-day, klines-based) + real funding day-sums.

    Same construction as the banked driver's ``btc_inputs`` but sourced from
    klines_1h directly (the driver's cached research feature panel does not
    exist off the original box)."""
    root = ROOTS[venue]
    closes = (
        pl.scan_parquet(str(root / "klines_1h" / "**" / "*.parquet"))
        .filter(pl.col("symbol") == "BTCUSDT")
        .select("ts_ms", "close")
        .collect()
        .with_columns(((pl.col("ts_ms") // 86_400_000) * 86_400_000).alias("day"))
        .group_by("day").agg(pl.col("close").last())
        .sort("day")
    )
    rets: dict[int, float] = {}
    prev_day, prev_close = None, None
    for day, close in closes.iter_rows():
        if prev_day is not None and day - prev_day == 86_400_000 and prev_close > 0:
            rets[int(day)] = float(close) / float(prev_close) - 1.0
        prev_day, prev_close = day, close
    fund: dict[int, float] = {}
    fdir = root / ("funding" if venue == "bybit" else "binance_usdm_funding")
    for day in days:
        date = dt.datetime.fromtimestamp(day / 1000, tz=dt.timezone.utc).date().isoformat()
        part = fdir / f"date={date}" / "symbol=BTCUSDT"
        if part.exists():
            fund[day] = float(
                pl.read_parquet(part, columns=["funding_rate"])["funding_rate"].sum()
            )
    return rets, fund


def data_end_day(root: Path) -> str:
    days = sorted(p.name.split("=", 1)[1] for p in (root / "klines_1h").iterdir()
                  if p.name.startswith("date="))
    if not days:
        raise FileNotFoundError(f"no kline partitions under {root}")
    return days[-1]


def ensure_cells(venue: str, work: Path, end_date: str) -> None:
    for cell, overrides in CELL_OVERRIDES.items():
        out_dir = work / venue / cell
        report = out_dir / "continuous_report.json"
        if report.exists():
            cfg_end = json.loads(report.read_text()).get("config", {}).get("end_date")
            if cfg_end == end_date:
                print(f"[{venue}] {cell}: current to {end_date}, skip", flush=True)
                continue
        t0 = time.time()
        cfg = ContinuousEventConfig(**{**rb.COMMON, **overrides, "end_date": end_date})
        payload = run_continuous_event_research(ROOTS[venue], config=cfg, report_dir=out_dir)
        print(f"[{venue}] {cell}: {payload['n_trades']} trades to {end_date} "
              f"({time.time() - t0:.0f}s)", flush=True)


def venue_update(venue: str, state_dir: Path, forward_start_ms: int) -> dict:
    work = state_dir / "_work"
    end_date = data_end_day(ROOTS[venue])
    ensure_cells(venue, work, end_date)
    pieces = {
        name: scout._load_source(scout.SourceSpec(work, cell), venue)[0]
        for name, cell in NAME_TO_CELL.items()
    }
    all_days = sorted({d for p in pieces.values() for d in p.days})
    rets, fund = btc_inputs(venue, all_days)
    ledger = build_full_ledger(pieces, rets, fund)
    res = update_forward_ledger(state_dir, venue, ledger)
    summary = forward_readiness_summary(state_dir, venue, forward_start_ms=forward_start_ms)
    return {
        "venue": venue,
        "data_end": end_date,
        "appended_days": res.appended_days,
        "verified_overlap_days": res.verified_overlap_days,
        "total_ledger_days": res.total_days,
        "readiness": summary,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venues", default="bybit,binance")
    ap.add_argument("--forward-start", default="2026-06-10",
                    help="forward-window start day (the clock start; default = VPS rebuild)")
    ap.add_argument("--state-dir", default=str(SHARED / "continuous_forward_replay"))
    args = ap.parse_args()
    state_dir = Path(args.state_dir).expanduser()
    fwd_ms = int(dt.datetime.fromisoformat(args.forward_start)
                 .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    out = {"config_object": FROZEN_FORWARD_CONFIG["object"], "venues": {}}
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        out["venues"][venue] = venue_update(venue, state_dir, fwd_ms)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
