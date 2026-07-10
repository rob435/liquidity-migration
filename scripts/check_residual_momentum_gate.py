#!/usr/bin/env python3
"""Validate that a live residual-momentum gate has usable stable rows."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import polars as pl

MS_PER_DAY = 86_400_000


def inspect_gate(
    path: Path,
    *,
    max_stable_age_days: float = 2.0,
    min_current_stable_symbols: int = 20,
    now_ms: int | None = None,
) -> dict[str, Any]:
    if max_stable_age_days <= 0.0:
        raise ValueError("max_stable_age_days must be positive")
    if min_current_stable_symbols <= 0:
        raise ValueError("min_current_stable_symbols must be positive")
    if not path.exists():
        raise RuntimeError(f"missing parquet: {path}")
    table = pl.read_parquet(path)
    required = {"symbol", "ts_ms", "residual_momentum", "is_provisional"}
    missing = sorted(required - set(table.columns))
    if missing:
        raise RuntimeError(
            "missing provenance columns " + ",".join(missing)
            + "; rerun the residual-momentum refresh with the current code"
        )
    if table.schema["is_provisional"] != pl.Boolean:
        raise RuntimeError("is_provisional must be a boolean column")
    if table.schema["symbol"] != pl.String:
        raise RuntimeError("symbol must be a String column")
    if table.schema["ts_ms"] != pl.Int64:
        raise RuntimeError("ts_ms must be an Int64 column")
    if table.schema["residual_momentum"] not in (pl.Float32, pl.Float64):
        raise RuntimeError("residual_momentum must be a floating-point column")
    if table["is_provisional"].null_count() > 0:
        raise RuntimeError("is_provisional contains null provenance")
    invalid_keys = table.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | pl.col("ts_ms").is_null()
        | ((pl.col("ts_ms") % MS_PER_DAY) != 0)
    )
    if not invalid_keys.is_empty():
        raise RuntimeError("symbol/ts_ms contains null, blank, or non-daily keys")
    duplicate_keys = table.group_by(["symbol", "ts_ms"]).len().filter(pl.col("len") > 1)
    if not duplicate_keys.is_empty():
        raise RuntimeError(
            "duplicate (symbol,ts_ms) keys: "
            + repr(duplicate_keys.head(5).select("symbol", "ts_ms", "len").to_dicts())
        )
    invalid_values = table.filter(
        pl.col("residual_momentum").is_null()
        | (~pl.col("residual_momentum").is_finite())
    )
    if not invalid_values.is_empty():
        raise RuntimeError("residual_momentum contains null or non-finite values")
    stable = table.filter(
        (~pl.col("is_provisional"))
        & pl.col("residual_momentum").is_not_null()
        & pl.col("ts_ms").is_not_null()
    )
    if stable.is_empty():
        raise RuntimeError("no stable non-null rows")
    latest_stable_ts_ms = int(stable["ts_ms"].max())
    clock_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    latest_stable_day_ms = (latest_stable_ts_ms // MS_PER_DAY) * MS_PER_DAY
    clock_day_ms = (clock_ms // MS_PER_DAY) * MS_PER_DAY
    if latest_stable_day_ms > clock_day_ms + MS_PER_DAY:
        raise RuntimeError("latest stable day is more than one day in the future")
    stable_age_days = max(clock_ms - latest_stable_ts_ms, 0) / MS_PER_DAY
    if stable_age_days > max_stable_age_days:
        raise RuntimeError(
            f"stable edge is stale: age={stable_age_days:.2f}d "
            f"> max={max_stable_age_days:.2f}d"
        )
    current_stable = stable.filter(pl.col("ts_ms") == clock_day_ms)
    current_stable_symbols = int(current_stable["symbol"].n_unique())
    if current_stable_symbols < min_current_stable_symbols:
        raise RuntimeError(
            f"current-day stable cross-section is too small: symbols={current_stable_symbols} "
            f"< min={min_current_stable_symbols}"
        )
    latest_stable_symbols = int(
        stable.filter(pl.col("ts_ms") == latest_stable_ts_ms)["symbol"].n_unique()
    )
    return {
        "path": str(path),
        "rows": int(table.height),
        "stable_rows": int(stable.height),
        "stable_symbols": int(stable["symbol"].n_unique()),
        "latest_stable_ts_ms": latest_stable_ts_ms,
        "latest_stable_symbols": latest_stable_symbols,
        "current_stable_ts_ms": int(clock_day_ms),
        "current_stable_symbols": current_stable_symbols,
        "stable_age_days": round(stable_age_days, 3),
        "max_stable_age_days": float(max_stable_age_days),
        "min_current_stable_symbols": int(min_current_stable_symbols),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True)
    parser.add_argument("--max-stable-age-days", type=float, default=2.0)
    parser.add_argument("--min-current-stable-symbols", type=int, default=20)
    args = parser.parse_args()
    try:
        result = inspect_gate(
            Path(args.path).expanduser(),
            max_stable_age_days=args.max_stable_age_days,
            min_current_stable_symbols=args.min_current_stable_symbols,
        )
    except Exception as exc:  # noqa: BLE001 - CLI must return one actionable gate failure
        print(f"rmom gate invalid: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
