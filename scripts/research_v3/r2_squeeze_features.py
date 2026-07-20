#!/usr/bin/env python3
"""R2/P2.1: squeeze-state feature set — causal build + per-feature PIT audit.

Tail-risk program P2.1 (`docs/tail_risk_program.md`): the raw material for
the R2 squeeze-state governor, built from fields none of the 29 prior
hypothesis families used. Lane-1 FEATURE BUILD ONLY: no outcome column is
read or joined here; governor design/grading is P2.2+.

Feature groups (hourly, book-level aggregates + per-symbol panels):

  G1 oi_*        Bybit ``open_interest`` (1h, 2021->): per-symbol log OI
                 change over 1h/24h and 24h acceleration; book aggregate =
                 turnover-weighted mean.
  G2 taker_*     ``taker_flow_5m`` (2023-03->): hourly taker-buy imbalance
                 (buy-sell)/(buy+sell), 24h rolling z; null before coverage.
  G3 premium_*   ``premium_index_1h``: level and 24h change z vs trailing
                 30d (min 10d).
  G4 funding_*   ``funding``: settlement rate level, jump vs prior
                 settlement, cross-sectional share with |rate| >= 0.10%/8h.
  G5 breadth_*   ``klines_1h``: share of PIT-manifest universe with
                 |ret_1h| >= 3% (melt/crash split), share with 24h return
                 >= +10% / <= -10%.
  positioning_lsr: NOT locally available (absent from bybit_full_pit as of
                 2026-07-20) — recorded data-gated, not silently skipped.

PIT policy, enforced uniformly: every feature consumed at decision bar t
uses source rows with timestamp <= t - 1 full bar of that source (one-bar
availability lag: OI/premium/kline rows lag 1h; 5m taker rows aggregate to
the hour then lag 1h; funding uses only settlements strictly before t).
The no-lookahead property is tested per feature group in
``tests/test_r2_squeeze_features.py`` (future-row mutation invariance).

Usage: .venv\\Scripts\\python.exe scripts/research_v3/r2_squeeze_features.py \\
    --start 2021-05-01 --end 2024-12-01
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_HOUR  # noqa: E402
from scripts.research_v3 import common  # noqa: E402

AVAILABILITY_LAG_BARS = 1
ABS_RET1_BREADTH = 0.03
RET24_BREADTH = 0.10
FUNDING_EXTREME = 0.001
PREMIUM_Z_WINDOW_H = 720
PREMIUM_Z_MIN = 240


def _lag(expr: pl.Expr) -> pl.Expr:
    """One-bar availability lag over the symbol's own series."""
    return expr.shift(AVAILABILITY_LAG_BARS).over("symbol")


def oi_features(oi: pl.DataFrame) -> pl.DataFrame:
    """G1: per-symbol OI log-changes and acceleration; consumed lagged."""
    frame = oi.sort(["symbol", "ts_ms"]).with_columns(
        (pl.col("open_interest").log() - pl.col("open_interest").shift(1).over("symbol").log()).alias(
            "_oi_d1h"
        ),
        (pl.col("open_interest").log() - pl.col("open_interest").shift(24).over("symbol").log()).alias(
            "_oi_d24h"
        ),
    )
    frame = frame.with_columns(
        (pl.col("_oi_d24h") - pl.col("_oi_d24h").shift(24).over("symbol")).alias("_oi_accel_24h")
    )
    return frame.select(
        "symbol",
        "ts_ms",
        _lag(pl.col("_oi_d1h")).alias("oi_change_1h"),
        _lag(pl.col("_oi_d24h")).alias("oi_change_24h"),
        _lag(pl.col("_oi_accel_24h")).alias("oi_accel_24h"),
    )


def taker_features(taker_5m: pl.DataFrame) -> pl.DataFrame:
    """G2: hourly taker-buy imbalance and 24h z; consumed lagged."""
    hourly = (
        taker_5m.with_columns(((pl.col("ts_ms") // MS_PER_HOUR) * MS_PER_HOUR).alias("hour_ts"))
        .group_by(["symbol", "hour_ts"])
        .agg(
            pl.col("taker_buy_quote").sum().alias("_buy"),
            pl.col("taker_sell_quote").sum().alias("_sell"),
        )
        .sort(["symbol", "hour_ts"])
        .rename({"hour_ts": "ts_ms"})
    )
    hourly = hourly.with_columns(
        pl.when(pl.col("_buy") + pl.col("_sell") > 0)
        .then((pl.col("_buy") - pl.col("_sell")) / (pl.col("_buy") + pl.col("_sell")))
        .otherwise(None)
        .alias("_imb")
    ).with_columns(
        ((pl.col("_imb") - pl.col("_imb").rolling_mean(24, min_samples=12).over("symbol"))
         / pl.col("_imb").rolling_std(24, min_samples=12).over("symbol").clip(lower_bound=1e-12)).alias("_imb_z")
    )
    return hourly.select(
        "symbol", "ts_ms",
        _lag(pl.col("_imb")).alias("taker_imbalance_1h"),
        _lag(pl.col("_imb_z")).alias("taker_imbalance_z24"),
    )


def premium_features(premium: pl.DataFrame) -> pl.DataFrame:
    """G3: premium level and 24h-change z vs trailing 30d; consumed lagged."""
    frame = premium.sort(["symbol", "ts_ms"]).with_columns(
        (pl.col("close") - pl.col("close").shift(24).over("symbol")).alias("_prem_d24h")
    )
    frame = frame.with_columns(
        ((pl.col("_prem_d24h")
          - pl.col("_prem_d24h").rolling_mean(PREMIUM_Z_WINDOW_H, min_samples=PREMIUM_Z_MIN).over("symbol"))
         / pl.col("_prem_d24h").rolling_std(PREMIUM_Z_WINDOW_H, min_samples=PREMIUM_Z_MIN).over("symbol")
         .clip(lower_bound=1e-12)).alias("_prem_z")
    )
    return frame.select(
        "symbol", "ts_ms",
        _lag(pl.col("close")).alias("premium_level"),
        _lag(pl.col("_prem_d24h")).alias("premium_change_24h"),
        _lag(pl.col("_prem_z")).alias("premium_change_z30d"),
    )


def funding_features(funding: pl.DataFrame) -> pl.DataFrame:
    """G4: settlement-level features; a settlement is known only strictly
    after its ts (rows are consumed via a strictly-backward as-of join)."""
    return (
        funding.sort(["symbol", "ts_ms"])
        .with_columns(
            (pl.col("funding_rate") - pl.col("funding_rate").shift(1).over("symbol")).alias(
                "funding_jump"
            ),
            (pl.col("funding_rate").abs() >= FUNDING_EXTREME).alias("funding_extreme"),
        )
        .select("symbol", "ts_ms", "funding_rate", "funding_jump", "funding_extreme")
    )


def breadth_features(klines: pl.DataFrame) -> pl.DataFrame:
    """G5: book-level melt-up/crash breadth over the PIT universe; lagged."""
    frame = klines.sort(["symbol", "ts_ms"]).with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("_ret1"),
        (pl.col("close") / pl.col("close").shift(24).over("symbol") - 1.0).alias("_ret24"),
    )
    per_hour = (
        frame.group_by("ts_ms")
        .agg(
            pl.len().alias("universe_n"),
            (pl.col("_ret1") >= ABS_RET1_BREADTH).mean().alias("_melt_1h"),
            (pl.col("_ret1") <= -ABS_RET1_BREADTH).mean().alias("_crash_1h"),
            (pl.col("_ret24") >= RET24_BREADTH).mean().alias("_melt_24h"),
            (pl.col("_ret24") <= -RET24_BREADTH).mean().alias("_crash_24h"),
        )
        .sort("ts_ms")
    )
    return per_hour.select(
        "ts_ms",
        pl.col("universe_n").shift(AVAILABILITY_LAG_BARS).alias("breadth_universe_n"),
        pl.col("_melt_1h").shift(AVAILABILITY_LAG_BARS).alias("breadth_melt_1h"),
        pl.col("_crash_1h").shift(AVAILABILITY_LAG_BARS).alias("breadth_crash_1h"),
        pl.col("_melt_24h").shift(AVAILABILITY_LAG_BARS).alias("breadth_melt_24h"),
        pl.col("_crash_24h").shift(AVAILABILITY_LAG_BARS).alias("breadth_crash_24h"),
    )


def _read(root: Path, dataset: str, start: dt.date, end: dt.date, columns: list[str]) -> pl.DataFrame:
    files = common.partition_files(root, dataset, start, end)
    if not files:
        return pl.DataFrame()
    frame = pl.concat([pl.read_parquet(f, columns=columns) for f in files])
    return frame.sort("ts_ms") if "ts_ms" in frame.columns else frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True, help="end-exclusive")
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    parser.add_argument("--data-root", type=Path, default=common.DEFAULT_DATA_ROOT)
    args = parser.parse_args()

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    warm = start - dt.timedelta(days=45)  # feature warm-up inputs (documented)
    out_dir = REPO / "reports" / "tail-risk-program" / f"p21-squeeze-features-{args.out_date}"
    out_dir.mkdir(parents=True, exist_ok=True)
    root = args.data_root

    manifest = _read(root, "archive_trade_manifest", start, end, ["date", "symbol"])
    klines = _read(root, "klines_1h", warm, end, ["ts_ms", "symbol", "close", "turnover_quote"])
    if not manifest.is_empty():  # PIT membership gate on the breadth universe
        klines = (
            klines.with_columns(
                pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.date().cast(pl.String).alias("_date")
            )
            .join(manifest.unique().rename({"date": "_date"}), on=["_date", "symbol"], how="semi")
            .drop("_date")
        )
    oi = _read(root, "open_interest", warm, end, ["ts_ms", "symbol", "open_interest"])
    taker = _read(root, "taker_flow_5m", warm, end, ["ts_ms", "symbol", "taker_buy_quote", "taker_sell_quote"])
    premium = _read(root, "premium_index_1h", warm, end, ["ts_ms", "symbol", "close"])
    funding = _read(root, "funding", warm, end, ["ts_ms", "symbol", "funding_rate"])

    outputs: dict[str, pl.DataFrame] = {}
    stats: dict[str, dict] = {}
    start_ms = int(dt.datetime.combine(start, dt.time(), tzinfo=dt.timezone.utc).timestamp() * 1000)
    for name, frame in (
        ("oi", oi_features(oi) if not oi.is_empty() else pl.DataFrame()),
        ("taker", taker_features(taker) if not taker.is_empty() else pl.DataFrame()),
        ("premium", premium_features(premium) if not premium.is_empty() else pl.DataFrame()),
        ("funding", funding_features(funding) if not funding.is_empty() else pl.DataFrame()),
        ("breadth", breadth_features(klines) if not klines.is_empty() else pl.DataFrame()),
    ):
        if not frame.is_empty():
            frame = frame.filter(pl.col("ts_ms") >= start_ms)  # warm-up rows are inputs only
        outputs[name] = frame
        value_cols = [c for c in frame.columns if c not in ("symbol", "ts_ms")]
        stats[name] = {
            "rows": frame.height,
            "symbols": (frame["symbol"].n_unique() if "symbol" in frame.columns and frame.height else None),
            "first_ts": (common.iso_date(int(frame["ts_ms"].min())) if frame.height else None),
            "last_ts": (common.iso_date(int(frame["ts_ms"].max())) if frame.height else None),
            "null_share": (
                {c: float(frame[c].null_count() / frame.height) for c in value_cols} if frame.height else {}
            ),
        }
        path = out_dir / f"squeeze_{name}.parquet"
        frame.write_parquet(path)
        stats[name]["sha256"] = common.sha256_file(path)
        print(f"group done: {name} rows={frame.height}", flush=True)

    stats["positioning_lsr"] = {
        "status": "DATA-GATED: positioning_lsr dataset absent from bybit_full_pit as of 2026-07-20;"
        " acquisition is a separate task before this group can exist"
    }
    receipt = {
        "kind": "tail_risk_p21_squeeze_feature_build",
        "window": [args.start, args.end],
        "warm_input_start": warm.isoformat(),
        "pit_policy": f"one-bar availability lag (shift {AVAILABILITY_LAG_BARS}) per source;"
        " funding consumed strictly after settlement ts; breadth universe gated by archive_trade_manifest",
        "no_outcome_columns": "no forward return, label, or trade outcome is read or joined here",
        "groups": stats,
    }
    (out_dir / "build_receipt.json").write_text(json.dumps(receipt, indent=1), encoding="utf-8")
    common.write_manifest(
        out_dir,
        kind="tail_risk_p21_squeeze_feature_build",
        inputs={"data_root": str(root), "window": [args.start, args.end]},
        params={"availability_lag_bars": AVAILABILITY_LAG_BARS,
                "thresholds": {"abs_ret1_breadth": ABS_RET1_BREADTH, "ret24_breadth": RET24_BREADTH,
                               "funding_extreme": FUNDING_EXTREME}},
        output_files={"build_receipt.json": out_dir / "build_receipt.json"},
        extra={"explicit_non_conclusions": [
            "feature build only; no outcome read; no governor design or grading",
            "positioning_lsr is data-gated (absent locally) and recorded, not skipped silently",
        ]},
    )
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "null_share"} for k, v in stats.items()},
                     default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
