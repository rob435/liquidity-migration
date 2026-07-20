#!/usr/bin/env python3
"""Lane-2 forward daily scorer for the freshness-gate-override prototype.

The prototype (``t-j/2026-07-20/prototype_freshness_gate_override.json``) was
registered by commit ``8d4ba37…``; its evidence is exclusively the run of UTC
days that start at or after that commit's timestamp.  This scorer turns
extended paired renders (same layout and flags as T-A) into an append-only
daily ledger:

  reports/strategy-research-v3/t-j/forward/forward_ledger.csv

One row per UTC day from the first forward day on: BTC-gate state, the
blocked entries the override would have admitted (gate-off-book realization,
the prototype's declared measurement), and their realized net — both
component-summed and single-counted.  Rows are hash-chained
(``row_hash = sha256(prev_hash + canonical row)``); a re-run verifies the
existing chain and refuses to alter history — it can only append days whose
outcomes are complete (entry day finished, exits realized, no data_end).

Runbook per scoring session (all read-only against runtime):
  1. refresh the bybit full-PIT research root through the target day + 2;
  2. re-run the paired renders (baseline and --research-disable-btc-gate)
     into a fresh directory with the T-A layout;
  3. run this scorer through the POSIX shim:
     .venv\\Scripts\\python.exe scripts/research_v3/run_with_stub.py \\
         scripts/research_v3/tj_forward_scorer.py --render-root <dir>

``--preview`` scores the PRE-commit days from the existing T-A renders into
``forward_preview.csv`` (context only, never evidence) to validate mechanics.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_DAY  # noqa: E402
from scripts.research_v3 import common, v4_shared  # noqa: E402

REGISTRATION_COMMIT = "8d4ba374087f030ac77f1be403e3c5b47fc6bb20"
FRESH_OVERRIDE_MAX_HOURS = 1.0
FORWARD_DIR = common.REPORT_ROOT / "t-j" / "forward"
LEDGER_COLUMNS = [
    "date", "btc_trend_30d", "gate_blocked", "n_blocked_entries", "n_override_admitted",
    "n_pending_exits", "net_admitted_components", "net_admitted_unique",
    "n_admitted_unique", "prev_hash", "row_hash",
]


def registration_boundary_ms() -> int:
    """First UTC midnight at or after the registration commit's timestamp."""
    stamp = subprocess.run(
        ["git", "show", "-s", "--format=%cI", REGISTRATION_COMMIT],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.strip()
    commit_ms = int(dt.datetime.fromisoformat(stamp).timestamp() * 1000)
    return ((commit_ms + MS_PER_DAY - 1) // MS_PER_DAY) * MS_PER_DAY


def canonical_row(row: dict[str, Any]) -> str:
    return json.dumps(
        {k: row[k] for k in LEDGER_COLUMNS if k not in ("prev_hash", "row_hash")},
        sort_keys=True, separators=(",", ":"),
    )


def chain_hash(prev_hash: str, row: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical_row(row)).encode("utf-8")).hexdigest()


def verify_ledger(path: Path) -> tuple[list[dict[str, Any]], str]:
    if not path.is_file():
        return [], hashlib.sha256(b"tj-forward-genesis").hexdigest()
    rows = pl.read_csv(path, schema_overrides={"date": pl.String}).to_dicts()
    prev = hashlib.sha256(b"tj-forward-genesis").hexdigest()
    for row in rows:
        if row["prev_hash"] != prev:
            raise RuntimeError(f"forward ledger chain broken at {row['date']} (prev_hash mismatch)")
        expected = chain_hash(prev, row)
        if row["row_hash"] != expected:
            raise RuntimeError(f"forward ledger chain broken at {row['date']} (row_hash mismatch)")
        prev = row["row_hash"]
    return rows, prev


def blocked_set(books: dict[str, pl.DataFrame]) -> pl.DataFrame:
    frames = []
    for comp in v4_shared.RENDER_COMPONENTS:
        on_ids = set(books["gate_on"].filter(pl.col("component") == comp)["trade_id"].to_list())
        off = books["gate_off"].filter(pl.col("component") == comp)
        frames.append(off.filter(~pl.col("trade_id").is_in(sorted(on_ids))))
    return pl.concat(frames, how="vertical")


def freshness_for(book: pl.DataFrame, klines: pl.DataFrame) -> pl.DataFrame:
    closes_by_symbol: dict[str, tuple[list[int], list[float]]] = {}
    for key, part in klines.sort(["symbol", "bar_end_ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        closes_by_symbol[symbol] = (
            [int(v) for v in part["bar_end_ts_ms"].to_list()],
            [float(v) for v in part["close"].to_list()],
        )
    hours: list[float | None] = []
    for trade in book.iter_rows(named=True):
        ends, closes = closes_by_symbol.get(str(trade["symbol"]), ([], []))
        value, _status = v4_shared.hours_since_high_tolerant(int(trade["entry_ts_ms"]), ends, closes)
        hours.append(value)
    return book.with_columns(pl.Series("hours_since_high_168h", hours, dtype=pl.Float64))


def main() -> int:
    from liquidity_migration.continuous_events import _btc_trend_returns

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-root", type=Path, default=v4_shared.TA_DIR,
                        help="directory containing render-baseline/ and render-gate-off/")
    parser.add_argument("--data-root", type=Path, default=common.DEFAULT_DATA_ROOT)
    parser.add_argument("--preview", action="store_true",
                        help="score PRE-commit days into forward_preview.csv (context only)")
    args = parser.parse_args()

    FORWARD_DIR.mkdir(parents=True, exist_ok=True)
    boundary_ms = registration_boundary_ms()
    boundary_day = common.iso_date(boundary_ms)
    print(f"registration boundary: first forward day {boundary_day}", flush=True)

    books = {
        arm: v4_shared.load_render_book(arm, base_dir=args.render_root) for arm in ("gate_on", "gate_off")
    }
    blocked = blocked_set(books)
    last_entry_ms = max(int(books[arm]["entry_ts_ms"].max()) for arm in books)

    if args.preview:
        window_lo = int(blocked["entry_ts_ms"].min())
        window_hi = min(boundary_ms, last_entry_ms + MS_PER_DAY)
        ledger_rows: list[dict[str, Any]] = []
        prev = hashlib.sha256(b"tj-forward-genesis").hexdigest()
        out_path = FORWARD_DIR / "forward_preview.csv"
    else:
        existing, prev = verify_ledger(FORWARD_DIR / "forward_ledger.csv")
        print(f"existing forward ledger: {len(existing)} rows, chain verified", flush=True)
        window_lo = (
            int(dt.datetime.fromisoformat(existing[-1]["date"] + "T00:00:00+00:00").timestamp() * 1000)
            + MS_PER_DAY
            if existing
            else boundary_ms
        )
        # A day is scoreable only when its 24h holds could have completed
        # inside the render window (entry day + 2 fully rendered).
        window_hi = ((last_entry_ms // MS_PER_DAY) - 1) * MS_PER_DAY
        ledger_rows = list(existing)
        out_path = FORWARD_DIR / "forward_ledger.csv"
    if window_lo >= window_hi:
        print("no scoreable days in range; nothing to do", flush=True)
        return 0

    # Freshness needs 1h closes for blocked symbols with an 8-day lookback.
    scoped = blocked.filter(
        (pl.col("entry_ts_ms") >= window_lo - 8 * MS_PER_DAY) & (pl.col("entry_ts_ms") < window_hi)
    )
    symbols = set(scoped["symbol"].to_list())
    lo_date = dt.datetime.fromtimestamp((window_lo - 9 * MS_PER_DAY) / 1000, tz=dt.timezone.utc).date()
    hi_date = dt.datetime.fromtimestamp(window_hi / 1000, tz=dt.timezone.utc).date() + dt.timedelta(days=1)
    klines = common.read_kline_slice(args.data_root, start=lo_date, end_exclusive=hi_date, symbols=symbols)
    scoped = freshness_for(scoped, klines).with_columns(
        (
            pl.col("hours_since_high_168h").is_not_null()
            & (pl.col("hours_since_high_168h") <= FRESH_OVERRIDE_MAX_HOURS)
        ).alias("override_admitted")
    )

    btc = common.read_kline_slice(
        args.data_root, start=dt.date(2021, 3, 1), end_exclusive=hi_date, symbols={"BTCUSDT"}
    )
    trend_lookup = _btc_trend_returns(btc, lookback_days=30)

    details_dir = FORWARD_DIR / ("details_preview" if args.preview else "details")
    day = window_lo
    appended = 0
    while day < window_hi:
        date = common.iso_date(day)
        trend = trend_lookup.get(day)
        day_blocked = scoped.filter((pl.col("entry_ts_ms") >= day) & (pl.col("entry_ts_ms") < day + MS_PER_DAY))
        admitted = day_blocked.filter(pl.col("override_admitted"))
        pending = admitted.filter(pl.col("exit_reason") == "data_end")
        scored = admitted.filter(pl.col("exit_reason") != "data_end")
        unique = scored.unique("trade_id")
        row: dict[str, Any] = {
            "date": date,
            "btc_trend_30d": None if trend is None else round(float(trend), 8),
            "gate_blocked": bool(trend is not None and trend <= 0.0),
            "n_blocked_entries": day_blocked.height,
            "n_override_admitted": admitted.height,
            "n_pending_exits": pending.height,
            "net_admitted_components": round(float(scored["net_return"].sum()), 10),
            "net_admitted_unique": round(float(unique["net_return"].sum()), 10),
            "n_admitted_unique": unique.height,
            "prev_hash": prev,
        }
        if pending.height and not args.preview:
            # Do not bank a day with unrealized exits; stop here and resume
            # after the renders extend past it.
            print(f"{date}: {pending.height} admitted entries still open (data_end) - stopping", flush=True)
            break
        row["row_hash"] = chain_hash(prev, row)
        prev = row["row_hash"]
        ledger_rows.append(row)
        appended += 1
        if scored.height:
            day_dir = details_dir / f"date={date}"
            day_dir.mkdir(parents=True, exist_ok=True)
            scored.write_parquet(day_dir / "admitted.parquet")
        day += MS_PER_DAY

    frame = pl.from_dicts(ledger_rows, infer_schema_length=None).select(LEDGER_COLUMNS)
    tmp = out_path.with_suffix(".tmp.csv")
    frame.write_csv(tmp)
    tmp.replace(out_path)
    label = "preview (PRE-commit, context only)" if args.preview else "forward ledger"
    print(
        json.dumps(
            {
                "mode": label,
                "rows_total": frame.height,
                "rows_appended": appended,
                "admitted_net_components_sum": float(frame["net_admitted_components"].sum()),
                "admitted_net_unique_sum": float(frame["net_admitted_unique"].sum()),
                "out": str(out_path),
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
