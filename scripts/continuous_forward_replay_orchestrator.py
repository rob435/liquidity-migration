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
    frozen_hedge_regime,
    update_forward_ledger,
)
from liquidity_migration.continuous_regime import btcvol_intensity_series  # noqa: E402

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
# component name (FROZEN_FORWARD_CONFIG weights key) -> rebuild-driver cell dir
NAME_TO_CELL = {
    "turn3p3": "merged_signal",
    "turn4p3": "age240_turn4pop3_crowd2",
    "turn4p5": "age240_turn4pop5_crowd2",
}
CELL_OVERRIDES = {cell: ov for (_root, cell), ov in rb.CELLS.items()}


def btc_inputs(
    venue: str, days: list[int], symbol: str = "BTCUSDT"
) -> tuple[dict[int, float], dict[int, float]]:
    """Hedge-instrument daily returns (consecutive-day, klines-based) + real funding
    day-sums for ``symbol`` (default BTCUSDT; pass ETHUSDT for the 2f second leg).

    Same construction as the banked driver's ``btc_inputs`` but sourced from
    klines_1h directly (the driver's cached research feature panel does not
    exist off the original box)."""
    root = ROOTS[venue]
    closes = (
        pl.scan_parquet(str(root / "klines_1h" / "**" / "*.parquet"))
        .filter(pl.col("symbol") == symbol)
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
        part = fdir / f"date={date}" / f"symbol={symbol}"
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
        # Skip only when the report AND the CSV artifacts scout._load_source needs are all
        # present and current — a run interrupted after the report but before the CSVs (or
        # an older run that wrote no CSVs on a flat component) must re-run (audit-iter4/5).
        needed_csvs = (
            out_dir / "continuous_trades.csv",
            out_dir / "continuous_mtm_equity.csv",
            out_dir / "continuous_equity.csv",
        )
        if report.exists() and all(p.exists() for p in needed_csvs):
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
    # forward-replay-1: BTC+ETH 2f hedge — track the SAME object the live demo book
    # runs (operator decision 2026-06-14). The ETH second leg goes through
    # build_full_ledger's hedge_returns_2 / hedge_funding_2; FROZEN_FORWARD_CONFIG now
    # carries instrument2=ETHUSDT (its config-hash voids any prior btc_only ledger).
    rets, fund = btc_inputs(venue, all_days, "BTCUSDT")
    rets2, fund2 = btc_inputs(venue, all_days, "ETHUSDT")
    # BTC-vol regime-hedge overlay (operator-approved 2026-06-15; receipt:
    # docs/preregistration/2026-06-15-forward-btcvol-regime-hedge.md): a causal,
    # mean-1 daily intensity from the BTC return series scales BOTH 2f hedge legs
    # (hedge more in turbulence). The same params are part of frozen_config_hash and
    # the live demo book applies the identical signal, so demo and forward track one
    # object. regime=None reduces build_full_ledger to the plain 2f hedge.
    regime = frozen_hedge_regime()
    hedge_intensity = (
        btcvol_intensity_series(
            all_days, rets, regime["lam"], regime["vol_window"], regime["pct_window"]
        )
        if regime
        else None
    )
    ledger = build_full_ledger(
        pieces, rets, fund, rets2, fund2, hedge_intensity=hedge_intensity
    )
    res = update_forward_ledger(state_dir, venue, ledger)
    summary = forward_readiness_summary(state_dir, venue, forward_start_ms=forward_start_ms)
    return {
        "venue": venue,
        "status": "ok",
        "data_end": end_date,
        "appended_days": res.appended_days,
        "verified_overlap_days": res.verified_overlap_days,
        "total_ledger_days": res.total_days,
        "drift_detected": False,
        "readiness": summary,
    }


def _run_venue(venue: str, state_dir: Path, forward_start_ms: int) -> dict:
    """Per-venue wrapper that turns a drift / build failure into a reported status
    rather than aborting the whole run.

    ``update_forward_ledger`` raises a hard ``RuntimeError`` on overlap drift (correct as a
    same-code regression alarm). Without isolation, a drift on the first venue aborts the loop
    before the second venue ever runs, and — because nothing alerts on a non-advancing clock — the
    forward window can quietly stop accruing while the audit keeps printing a stale ``forward_days``
    value. Isolating each venue lets the healthy venue still append and surfaces the
    failure (drift vs other error) explicitly; ``main`` then exits non-zero so a manual/scheduled run
    cannot silently no-op. Self-healing a *legitimate* history revision (vs a code regression) still
    requires the state-dir re-base in continuous_forward_replay.update_forward_ledger and is out of
    scope here."""
    try:
        return venue_update(venue, state_dir, forward_start_ms)
    except Exception as exc:  # noqa: BLE001 - one venue's failure must not abort the other
        msg = str(exc)
        drift = "forward-ledger drift" in msg or "missing from the rebuilt ledger" in msg
        print(
            f"[{venue}] FORWARD-CLOCK STALL ({'drift' if drift else 'error'}): {msg}",
            file=sys.stderr,
            flush=True,
        )
        return {
            "venue": venue,
            "status": "drift" if drift else "error",
            "appended_days": 0,
            "drift_detected": drift,
            "error": msg,
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
    out: dict = {"config_object": FROZEN_FORWARD_CONFIG["object"], "venues": {}}
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        out["venues"][venue] = _run_venue(venue, state_dir, fwd_ms)
    # Observability: a venue that failed OR appended zero days on a run is a stalled clock.
    # Flag it loudly and exit non-zero so the stall is not silent (forward-replay-5).
    failed = [v for v, r in out["venues"].items() if r.get("status") != "ok"]
    stalled = [
        v for v, r in out["venues"].items()
        if r.get("status") == "ok" and int(r.get("appended_days") or 0) == 0
    ]
    out["failed_venues"] = failed
    out["stalled_venues"] = stalled
    print(json.dumps(out, indent=2))
    if failed:
        print(
            f"FORWARD-CLOCK STALLED for {failed}; the forward window is NOT advancing. "
            "If a data root was legitimately revised, archive/re-base the state dir per "
            "the continuous_forward_replay contract; otherwise this is a same-code regression.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    if stalled:
        print(
            f"WARNING: venues {stalled} appended 0 forward days this run (clock did not advance). "
            "Expected only if the data root has not refreshed since the prior run.",
            file=sys.stderr,
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
