#!/usr/bin/env python3
"""R1 rolling forward shadow scorer — intensity vs deployed gate, one row per day.

Lane-2 scorer for the registered ``r1_intensity_v1`` config
(`reports/tail-risk-program/r1/r1_intensity_v1.json`; the git commit that
added that file is the registration — the boundary is resolved from git, not
hardcoded). Every completed UTC day after the boundary appends one
hash-chained row comparing what the deployed weight (binary gate x discrete
0.35 band) vs the R1 weight (linear10 x ramp) would have applied to that
day's completed gate_off entry cohort. Pure shadow: reads render books and
klines, changes no runtime.

A day is scored only when every gate_off entry signal-dated that day has a
completed exit (no ``data_end``); scoring stops at the first pending day.
Re-runs verify the full existing chain and only append.

Usage: .venv\\Scripts\\python.exe scripts/research_v3/run_with_stub.py \\
    scripts/research_v3/r1_forward_scorer.py
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
from scripts.research_v3.r1_intensity_lane1 import (  # noqa: E402
    BTC_KLINE_START,
    BTC_LOOKBACK_DAYS,
    member_weight,
    replay_btc_risk_by_day,
)

CONFIG_PATH = REPO / "reports" / "tail-risk-program" / "r1" / "r1_intensity_v1.json"
FORWARD_DIR = REPO / "reports" / "tail-risk-program" / "r1-forward"
DIVERGENCE_MIN_DELTA = 0.25
LEDGER_COLUMNS = [
    "date", "btc_trend_30d", "btc_risk_score", "score_warmup",
    "m_deployed", "m_r1", "divergence_day", "n_entries", "n_pending",
    "net_unweighted", "net_deployed", "net_r1",
    "prev_hash", "row_hash",
]


def registration_boundary_ms() -> int:
    """First UTC midnight at/after the commit that ADDED the config file."""
    stamp = subprocess.run(
        ["git", "log", "--diff-filter=A", "--follow", "--format=%cI", "--",
         str(CONFIG_PATH.relative_to(REPO))],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    if not stamp:
        raise RuntimeError(
            "r1_intensity_v1.json has no add-commit: the registration is the commit;"
            " commit the config before scoring forward days"
        )
    commit_ms = int(dt.datetime.fromisoformat(stamp[-1]).timestamp() * 1000)
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
        return [], hashlib.sha256(b"r1-forward-genesis").hexdigest()
    rows = pl.read_csv(path, schema_overrides={"date": pl.String}).to_dicts()
    prev = hashlib.sha256(b"r1-forward-genesis").hexdigest()
    for row in rows:
        if row["prev_hash"] != prev:
            raise RuntimeError(f"r1 forward ledger chain broken at {row['date']} (prev_hash)")
        if row["row_hash"] != chain_hash(prev, row):
            raise RuntimeError(f"r1 forward ledger chain broken at {row['date']} (row_hash)")
        prev = row["row_hash"]
    return rows, prev


def deployed_weight(trend: float | None, score: float | None, warmup: bool) -> float:
    return member_weight("binary_discrete35", trend, score, warmup)


def r1_weight(trend: float | None, score: float | None, warmup: bool) -> float:
    return member_weight("linear10_ramp", trend, score, warmup)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=common.DEFAULT_DATA_ROOT)
    parser.add_argument("--render-root", type=Path, default=None)
    args = parser.parse_args()

    from liquidity_migration.continuous_events import _btc_trend_returns

    FORWARD_DIR.mkdir(parents=True, exist_ok=True)
    ledger_path = FORWARD_DIR / "forward_ledger.csv"
    existing, prev_hash = verify_ledger(ledger_path)
    scored_dates = {row["date"] for row in existing}
    boundary_ms = registration_boundary_ms()

    book = v4_shared.load_render_book("gate_off", base_dir=args.render_root)
    end_exclusive = dt.datetime.fromtimestamp(
        int(book["exit_ts_ms"].max()) / 1000, tz=dt.timezone.utc
    ).date() + dt.timedelta(days=2)
    btc_klines = common.read_kline_slice(
        args.data_root, start=BTC_KLINE_START, end_exclusive=end_exclusive, symbols={"BTCUSDT"}
    )
    trend_lookup = _btc_trend_returns(btc_klines, lookback_days=BTC_LOOKBACK_DAYS)
    risk_lookup = replay_btc_risk_by_day(btc_klines)

    book = book.with_columns(
        ((pl.col("entry_signal_ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("signal_day_ms")
    )
    day_ms = boundary_ms
    last_day = int(book["signal_day_ms"].max())
    appended: list[dict[str, Any]] = []
    while day_ms <= last_day:
        date = dt.datetime.fromtimestamp(day_ms / 1000, tz=dt.timezone.utc).date().isoformat()
        cohort = book.filter(pl.col("signal_day_ms") == day_ms)
        pending = cohort.filter(pl.col("exit_reason") == "data_end").height
        if pending:
            print(f"stopping at {date}: {pending} entries still open in the render window", flush=True)
            break
        if date not in scored_dates:
            trend = trend_lookup.get(day_ms)
            score, warmup = risk_lookup.get(day_ms, (None, True))
            m_dep = deployed_weight(trend, score, warmup)
            m_r1 = r1_weight(trend, score, warmup)
            net = float(cohort["net_return"].sum()) if cohort.height else 0.0
            row: dict[str, Any] = {
                "date": date,
                "btc_trend_30d": None if trend is None else round(float(trend), 8),
                "btc_risk_score": None if score is None else round(float(score), 8),
                "score_warmup": bool(warmup),
                "m_deployed": round(m_dep, 8),
                "m_r1": round(m_r1, 8),
                "divergence_day": abs(m_r1 - m_dep) >= DIVERGENCE_MIN_DELTA,
                "n_entries": cohort.height,
                "n_pending": 0,
                "net_unweighted": round(net, 10),
                "net_deployed": round(m_dep * net, 10),
                "net_r1": round(m_r1 * net, 10),
                "prev_hash": prev_hash,
            }
            row["row_hash"] = chain_hash(prev_hash, row)
            prev_hash = row["row_hash"]
            appended.append(row)
        day_ms += MS_PER_DAY

    if appended:
        frame = pl.from_dicts(existing + appended, infer_schema_length=None).select(LEDGER_COLUMNS)
        frame.write_csv(ledger_path)
    summary = {
        "boundary": dt.datetime.fromtimestamp(boundary_ms / 1000, tz=dt.timezone.utc).date().isoformat(),
        "existing_rows": len(existing),
        "appended_rows": len(appended),
        "cum_net_deployed": round(sum(float(r["net_deployed"]) for r in existing + appended), 8),
        "cum_net_r1": round(sum(float(r["net_r1"]) for r in existing + appended), 8),
        "divergence_days": sum(1 for r in existing + appended if r["divergence_day"] in (True, "true")),
    }
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
