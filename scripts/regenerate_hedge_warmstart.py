"""Regenerate deploy/hedge_warmstart/{venue}_warmstart.csv from current data.

THE refresh mechanism for the 2f hedge's beta warm-start (operator queue item:
"regenerate the CSVs and define refresh cadence"). Cadence = run this script at
every data-root refresh and commit the CSVs (they sit in the deploy paths
filter, so the commit auto-deploys them to the live units).

Construction (matches the engine the betas were banked on):
- components = the four frozen winner_base cells (the parity-verified rebuilt
  ledgers; `scripts/rebuild_winner_base_component_ledgers.py`) combined on the
  frozen receipt weights;
- unit_ret[day] = gross + funding + scale-1 entry costs per LEDGER day (the
  scale-independent day return `apply_rebalance_rule` scales);
- btc_ret/eth_ret = same-calendar-day daily close-to-close from klines_1h.

--validate compares the regenerated series against the existing CSV on
overlapping dates before overwriting (semantics check; small diffs are the
rebuilt-ledger vintage, e.g. p3 858 vs 857 trades).

    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/regenerate_hedge_warmstart.py [--validate-only] [--days 200]
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

import continuous_ensemble_rebalance_scout as scout  # noqa: E402

from liquidity_migration.continuous_rebalance import scaled_entry_cost  # noqa: E402

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
OUT_DIR = Path(__file__).resolve().parent.parent / "deploy" / "hedge_warmstart"
WINNER = {"turn3p3": 0.30, "turn4p3": 0.20, "turn4p5": 0.40, "age210tp14": 0.10}
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
        if closes[prev] > 0:
            out[cur] = closes[cur] / closes[prev] - 1.0
    return out


def unit_series(venue: str) -> dict[int, float]:
    comps = {src: scout._load_source(scout.SOURCES[src], venue)[0] for src in WINNER}
    combined = scout._combine_components(comps, WINNER)
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


def validate(venue: str, rows: list[dict]) -> None:
    path = OUT_DIR / f"{venue}_warmstart.csv"
    if not path.exists():
        print(f"  [{venue}] no existing CSV to validate against")
        return
    old = {r["date"]: float(r["unit_ret"]) for r in csv.DictReader(path.open())}
    new = {r["date"]: float(r["unit_ret"]) for r in rows}
    overlap = sorted(set(old) & set(new))
    if not overlap:
        print(f"  [{venue}] no date overlap with existing CSV")
        return
    diffs = [abs(old[d] - new[d]) for d in overlap]
    import statistics
    print(
        f"  [{venue}] overlap {len(overlap)}d: max|Δunit| {max(diffs):.2e}, "
        f"mean|Δ| {statistics.mean(diffs):.2e} (vintage drift expected at ledger-rebuild scale)"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--validate-only", action="store_true")
    args = ap.parse_args()
    for venue in ROOTS:
        rows = regenerate(venue, args.days)
        validate(venue, rows)
        last = rows[-1]["date"] if rows else "none"
        if args.validate_only:
            print(f"  [{venue}] would write {len(rows)} rows, last day {last}")
            continue
        path = OUT_DIR / f"{venue}_warmstart.csv"
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["date", "unit_ret", "btc_ret", "eth_ret"])
            w.writeheader()
            w.writerows(rows)
        print(f"  [{venue}] wrote {len(rows)} rows -> {path.name}, last day {last}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
