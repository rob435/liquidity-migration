"""Regenerate deploy/hedge_warmstart/{venue}_warmstart.csv from current data.

THE refresh mechanism for the 2f hedge's beta warm-start (operator queue item:
"regenerate the CSVs and define refresh cadence"). Cadence = run this script at
every data-root refresh and commit the CSVs (they sit in the deploy paths
filter, so the commit auto-deploys them to the live units).

Construction (matches the engine the betas were banked on):
- components = the three current frozen continuous_ensemble_v2 cells (the parity-verified rebuilt
  ledgers; `scripts/rebuild_continuous_component_ledgers.py`) combined on the
  frozen receipt weights;
- unit_ret[day] = gross + funding + scale-1 entry costs per LEDGER day (the
  scale-independent day return `apply_rebalance_rule` scales);
- btc_ret/eth_ret = same-calendar-day daily close-to-close from klines_1h.

--validate compares the regenerated series against the existing CSV on
overlapping dates and GATES the overwrite (semantics check; small diffs are the
rebuilt-ledger vintage, e.g. p3 858 vs 857 trades). The warm-start CSV feeds the
live 2f hedge beta (continuous_hedge_manager.load_warmstart_2f) and auto-deploys
on commit, so a regression must not be written silently: if the max |delta_unit_ret|
over the overlap exceeds --max-unit-drift, or the regeneration has FEWER rows
than the banked CSV, the overwrite is REFUSED unless --force is given. --force
keeps the manual escape hatch for a legitimate data-vintage shift.

    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/regenerate_hedge_warmstart.py [--validate-only] [--days 200] \
        [--max-unit-drift 1e-3] [--force]
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import polars as pl  # noqa: E402

from liquidity_migration.continuous_component_sources import (  # noqa: E402
    CONTINUOUS_COMPONENT_SOURCES,
    ContinuousComponentSource,
    load_continuous_component_source,
)
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    combine_continuous_components,
    scaled_entry_cost,
)

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
OUT_DIR = Path(__file__).resolve().parent.parent / "deploy" / "hedge_warmstart"
FALLBACK_COMPONENT_ROOT = Path(
    os.environ.get(
        "CONTINUOUS_COMPONENT_FALLBACK_ROOT",
        str(SHARED / "continuous_deployed_equity_refresh_2026-06-12" / "components"),
    )
).expanduser()
# Current three-component object frozen 2026-06-18; renorm = old/0.90.
WINNER = {"turn3p3": 0.3333333333333333, "turn4p3": 0.2222222222222222, "turn4p5": 0.4444444444444444}
MS_DAY = 86_400_000


def daily_closes(root: Path, symbol: str) -> dict[int, float]:
    df = (
        pl.scan_parquet(str(root / "klines_1h" / "**" / "*.parquet"))
        .filter(pl.col("symbol") == symbol)
        .select("ts_ms", "close")
        .collect()
        .with_columns(((pl.col("ts_ms") // MS_DAY) * MS_DAY).alias("day"))
        .group_by("day").agg(pl.col("close").last())
        .sort("day")
    )
    return {int(d): float(c) for d, c in df.iter_rows()}


def daily_returns(closes: dict[int, float]) -> dict[int, float]:
    days = sorted(closes)
    out: dict[int, float] = {}
    for prev, cur in zip(days, days[1:]):
        # audit2: only emit a calendar-consecutive return; a missing UTC day
        # would otherwise mislabel a multi-day move as one day (mirrors the
        # gap-guarded twin in continuous_forward_replay_orchestrator.btc_inputs).
        if cur - prev == MS_DAY and closes[prev] > 0:
            out[cur] = closes[cur] / closes[prev] - 1.0
    return out


def load_component_for_warmstart(src: str, venue: str):
    spec = CONTINUOUS_COMPONENT_SOURCES[src]
    try:
        return load_continuous_component_source(spec, venue)
    except FileNotFoundError as original_error:
        fallback = ContinuousComponentSource(FALLBACK_COMPONENT_ROOT, spec.cell)
        try:
            return load_continuous_component_source(fallback, venue)
        except FileNotFoundError:
            raise original_error


def unit_series(venue: str) -> dict[int, float]:
    comps = {src: load_component_for_warmstart(src, venue)[0] for src in WINNER}
    combined = combine_continuous_components(comps, WINNER)
    out: dict[int, float] = {}
    for day in combined.days:
        ret = combined.gross_by_day.get(day, 0.0) + combined.funding_by_day.get(day, 0.0)
        ret += scaled_entry_cost(combined.cost_events.get(day, []), 1.0, combined.impact_exponent)
        out[int(day)] = float(ret)
    return out


def regenerate(venue: str, n_days: int) -> list[dict]:
    root = ROOTS[venue]
    unit = unit_series(venue)
    btc = daily_returns(daily_closes(root, "BTCUSDT"))
    eth = daily_returns(daily_closes(root, "ETHUSDT"))
    rows = []
    for day in sorted(unit)[-n_days:]:
        rows.append({
            "date": dt.datetime.fromtimestamp(day / 1000, tz=dt.timezone.utc).date().isoformat(),
            "unit_ret": unit[day],
            "btc_ret": btc.get(day, ""),
            "eth_ret": eth.get(day, ""),
        })
    return rows


def validate(venue: str, rows: list[dict]) -> dict:
    """Compare the regenerated series against the banked CSV on overlapping dates.

    Returns a dict the overwrite gate consumes:
      max_drift : max |delta_unit_ret| over the overlap (0.0 when no CSV/overlap)
      old_rows  : row count of the existing CSV (0 when none)
      new_rows  : row count of the regenerated series
      overlap   : number of shared dates
    """
    path = OUT_DIR / f"{venue}_warmstart.csv"
    new_rows = len(rows)
    if not path.exists():
        print(f"  [{venue}] no existing CSV to validate against")
        return {"max_drift": 0.0, "old_rows": 0, "new_rows": new_rows, "overlap": 0}
    old = {r["date"]: float(r["unit_ret"]) for r in csv.DictReader(path.open())}
    new = {r["date"]: float(r["unit_ret"]) for r in rows}
    overlap = sorted(set(old) & set(new))
    if not overlap:
        print(f"  [{venue}] no date overlap with existing CSV")
        return {"max_drift": 0.0, "old_rows": len(old), "new_rows": new_rows, "overlap": 0}
    diffs = [abs(old[d] - new[d]) for d in overlap]
    import statistics
    max_drift = max(diffs)
    print(
        f"  [{venue}] overlap {len(overlap)}d: max|delta_unit| {max_drift:.2e}, "
        f"mean|delta| {statistics.mean(diffs):.2e} (vintage drift expected at ledger-rebuild scale)"
    )
    return {"max_drift": max_drift, "old_rows": len(old), "new_rows": new_rows, "overlap": len(overlap)}


def overwrite_blocked(venue: str, report: dict, *, max_drift: float, force: bool) -> str | None:
    """Reason the overwrite must be refused, or None to allow it.

    Guards the live 2f-hedge warm-start CSV (backfill-writers-5): a regenerated
    series that diverges materially from the banked one, or that has FEWER rows
    (a short/regressed run), must not silently overwrite + auto-deploy. --force
    is the explicit escape hatch for a known-good data-vintage shift.
    """
    if force:
        return None
    if report["old_rows"] and not report["overlap"]:
        return "regeneration has no date overlap with the existing CSV"
    if report["overlap"] and report["max_drift"] > max_drift:
        return (f"max|delta_unit| {report['max_drift']:.2e} over {report['overlap']}d exceeds "
                f"--max-unit-drift {max_drift:.2e}")
    if report["old_rows"] and report["new_rows"] < report["old_rows"]:
        return (f"regeneration has {report['new_rows']} rows < existing {report['old_rows']} "
                f"(short/regressed run)")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--max-unit-drift", type=float, default=1e-3,
                    help="Refuse the overwrite when max|delta_unit_ret| over the overlap exceeds this "
                         "(unless --force). Default 1e-3 admits ledger-rebuild vintage drift.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite even when the drift/row-count gate would refuse (known-good "
                         "data-vintage shift).")
    args = ap.parse_args()
    refused = False
    for venue in ROOTS:
        rows = regenerate(venue, args.days)
        report = validate(venue, rows)
        last = rows[-1]["date"] if rows else "none"
        block = overwrite_blocked(venue, report, max_drift=args.max_unit_drift, force=args.force)
        if args.validate_only:
            print(f"  [{venue}] would write {len(rows)} rows, last day {last}")
            if block is not None:
                print(f"  [{venue}] WOULD REFUSE overwrite: {block}. Re-run with --force to override.")
                refused = True
            continue
        if block is not None:
            print(f"  [{venue}] REFUSING overwrite: {block}. Re-run with --force to override.")
            refused = True
            continue
        path = OUT_DIR / f"{venue}_warmstart.csv"
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["date", "unit_ret", "btc_ret", "eth_ret"])
            w.writeheader()
            w.writerows(rows)
        print(f"  [{venue}] wrote {len(rows)} rows -> {path.name}, last day {last}")
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
