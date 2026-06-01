"""Phase 0c (continuous-fade plan §6 / Avenue F) — does FUNDING survive on the 24h continuous rmom short?

The intraday arc's hardest lesson (I2g-I2k): funding can eat ~80-89% of a fresh-pump short's edge and
SWING the verdict, so cost it BEFORE celebrating. Phase 0b found a robust all-weather 24h continuous
rmom short (funding-blind). This charges realized funding-to-exit over the [t, t+24h] hold on the D9
short leg (and the D0 long leg for the beta-neutral L/S), exactly as the validated i2 engine does:
short funding PnL = sum of funding_rate settlements in (entry, exit] (a short RECEIVES positive funding;
PAYS when the perp is in a crowded-short discount = rate<0).

Funding cumsum is as-of-joined (gap-robust): fund24(t) = cf(t+24h) - cf(t), cf = cumsum(funding_rate).
bybit funding is real (`funding`); binance is funding-blind in binance_full_pit (`binance_usdm_funding`
sparse — Avenue F4) so its funding row is reported but flagged optimistic. EXPLORATORY characterization
(per-ts decile means, not a backtest); NOT promotion evidence.

Read-outs (h24, delay 0): short_only_net & beta-neutral L/S net, funding-BLIND vs funding-INCLUDED,
full / early / recent + the 2023/2024/2025H1/recent buckets. Verdict: does the all-weather property
survive funding on the anchor venue (bybit)?

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p0c_continuous_funding.py
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.signal_harness import (  # noqa: E402
    _autodetect_dataset_names,
    _date_str_to_ms,
    _read_window,
)

SHARED = Path.home() / "SHARED_DATA"
START_DATE, END_DATE = "2023-04-01", "2026-05-28"
MS_PER_DAY = 86_400_000
HOLD_H = 24
FEATURES = ["rv_168h", "vov", "dist_low", "xsret7", "xsret3"]
ROUND_TRIP_BPS = 15.0
LISTING_SCAN_START = "2018-01-01"
VENUES = {  # venue -> (root, funding_dataset, funding_is_real)
    "bybit": (SHARED / "bybit_full_pit", "funding", True),
    "binance": (SHARED / "binance_full_pit", "binance_usdm_funding", False),
}
BUCKETS = [("2023", "2023-04-01", "2024-01-01"), ("2024", "2024-01-01", "2025-01-01"),
           ("2025H1", "2025-01-01", "2025-06-01"), ("recent", "2025-06-01", "2026-05-29")]
SPLIT_MS = _date_str_to_ms("2025-06-01")


def _panel(root: Path) -> pl.DataFrame:
    start_ms, end_ms = _date_str_to_ms(START_DATE), _date_str_to_ms(END_DATE)
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    k = _read_window(root, kname, start_ms=start_ms - 40 * MS_PER_DAY, end_ms=end_ms,
                     columns=["ts_ms", "symbol", "close"])
    if k.is_empty():
        return pl.DataFrame()
    k = k.filter(pl.col("close") > 0).unique(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
    k = k.with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret1"))
    k = k.with_columns(
        pl.col("ret1").rolling_std(window_size=168, min_samples=48).over("symbol").alias("rv_168h"),
        (pl.col("close") / pl.col("close").shift(72).over("symbol") - 1.0).alias("ret72"),
        (pl.col("close") / pl.col("close").shift(168).over("symbol") - 1.0).alias("ret168"),
        pl.col("close").rolling_min(window_size=720, min_samples=168).over("symbol").alias("min720"),
        pl.col("close").rolling_max(window_size=720, min_samples=168).over("symbol").alias("max720"),
    )
    k = k.with_columns(
        pl.col("rv_168h").rolling_std(window_size=720, min_samples=168).over("symbol").alias("vov"),
        pl.when(pl.col("max720") > pl.col("min720"))
        .then((pl.col("close") - pl.col("min720")) / (pl.col("max720") - pl.col("min720")))
        .otherwise(None).alias("dist_low"),
    )
    k = k.with_columns((pl.col("close").shift(-HOLD_H).over("symbol") / pl.col("close") - 1.0).alias("fwd"))
    k = k.with_columns(
        pl.col("ret168").rank().over("ts_ms").alias("xsret7"),
        pl.col("ret72").rank().over("ts_ms").alias("xsret3"),
    )
    return k.filter(pl.col("ts_ms") >= start_ms)


def _attach_rmom_lowhalf_decile(feat: pl.DataFrame, root: Path) -> pl.DataFrame:
    p = root / "residual_momentum.parquet"
    rmom = pl.read_parquet(p).rename({"ts_ms": "day_ts"})
    feat = feat.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"))
    feat = feat.join(rmom, on=["symbol", "day_ts"], how="left").filter(pl.col("residual_momentum").is_not_null())
    feat = feat.with_columns(
        ((pl.col("residual_momentum").rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias("_rr")
    ).filter(pl.col("_rr") <= 0.5)
    present = [f for f in FEATURES if f in feat.columns]
    feat = feat.with_columns([
        ((pl.col(f).rank().over("ts_ms") - 1) / (pl.len().over("ts_ms") - 1)).alias(f"_n_{f}") for f in present
    ])
    feat = feat.with_columns(pl.mean_horizontal([pl.col(f"_n_{f}") for f in present]).alias("composite"))
    return feat.with_columns(
        (((pl.col("composite").rank().over("ts_ms") - 1) * 10) // pl.len().over("ts_ms")).clip(0, 9).alias("decile")
    )


def _attach_funding(feat: pl.DataFrame, root: Path, ds: str) -> pl.DataFrame:
    files = glob.glob(str(root / ds / "**" / "*.parquet"), recursive=True)
    if not files:
        return feat.with_columns(pl.lit(0.0).alias("fund24")), 0.0
    fund = pl.scan_parquet(files).select(["ts_ms", "symbol", "funding_rate"]).collect()
    fund = fund.drop_nulls().sort(["symbol", "ts_ms"]).with_columns(
        pl.col("funding_rate").cum_sum().over("symbol").alias("cf")
    )
    cf = fund.select("symbol", "ts_ms", "cf")
    p = feat.sort(["symbol", "ts_ms"])
    p = p.join_asof(cf, on="ts_ms", by="symbol", strategy="backward").rename({"cf": "cf_entry"})
    p = p.with_columns((pl.col("ts_ms") + HOLD_H * 3_600_000).alias("ts_exit")).sort(["symbol", "ts_exit"])
    cf_exit = cf.rename({"ts_ms": "ts_exit_k", "cf": "cf_exit"})
    p = p.join_asof(cf_exit, left_on="ts_exit", right_on="ts_exit_k", by="symbol", strategy="backward")
    p = p.with_columns(pl.col("cf_entry").fill_null(0.0))
    p = p.with_columns((pl.col("cf_exit").fill_null(pl.col("cf_entry")) - pl.col("cf_entry")).alias("fund24"))
    cov = float(p["cf_exit"].is_not_null().mean())
    return p, cov


def _perts(feat_dec: pl.DataFrame) -> pl.DataFrame:
    """Per-ts top(D9)/bot(D0) mean fwd + mean funding."""
    sub = feat_dec.select("ts_ms", "decile", "fwd", "fund24").drop_nulls(["decile", "fwd"])
    sub = sub.with_columns(pl.col("fund24").fill_null(0.0))
    dt = sub.group_by(["ts_ms", "decile"]).agg(pl.col("fwd").mean().alias("dm"), pl.col("fund24").mean().alias("fm"))
    top = dt.filter(pl.col("decile") == 9).select("ts_ms", pl.col("dm").alias("t_r"), pl.col("fm").alias("t_f"))
    bot = dt.filter(pl.col("decile") == 0).select("ts_ms", pl.col("dm").alias("b_r"), pl.col("fm").alias("b_f"))
    return top.join(bot, on="ts_ms", how="inner")


def _agg(j: pl.DataFrame, lo=None, hi=None) -> dict | None:
    if lo is not None:
        j = j.filter(pl.col("ts_ms") >= lo)
    if hi is not None:
        j = j.filter(pl.col("ts_ms") < hi)
    if j.is_empty():
        return None
    c = ROUND_TRIP_BPS / 1e4
    t_r, t_f = float(j["t_r"].mean()), float(j["t_f"].mean())
    b_r, b_f = float(j["b_r"].mean()), float(j["b_f"].mean())
    return {
        "short_blind": round((-t_r - c) * 1e4, 0),
        "short_fund": round((-t_r + t_f - c) * 1e4, 0),   # short receives +funding
        "ls_blind": round((b_r - t_r - 2 * c) * 1e4, 0),
        "ls_fund": round((b_r - t_r + t_f - b_f - 2 * c) * 1e4, 0),
        "fund_short_bps": round(t_f * 1e4, 0),            # the short leg's funding (neg = drag)
        "n_ts": int(j.height),
    }


def main() -> int:
    out: dict = {}
    for venue, (root, ds, real) in VENUES.items():
        if not root.exists():
            print(f"SKIP {venue}")
            continue
        print(f"[{venue}] build panel + rmom_lo decile ...", flush=True)
        feat = _panel(root)
        if feat.is_empty():
            print(f"[{venue}] EMPTY")
            continue
        feat = _attach_rmom_lowhalf_decile(feat, root)
        feat, cov = _attach_funding(feat, root, ds)
        tag = "REAL" if real else "FUNDING-BLIND/optimistic (F4)"
        print(f"[{venue}] funding={ds} [{tag}] exit-coverage={cov:.1%}", flush=True)
        j = _perts(feat)
        res = {"full": _agg(j), "early": _agg(j, hi=SPLIT_MS), "recent": _agg(j, lo=SPLIT_MS),
               "buckets": {b: _agg(j, lo=_date_str_to_ms(a0), hi=_date_str_to_ms(a1)) for b, a0, a1 in BUCKETS}}
        out[venue] = {"funding_real": real, "exit_coverage": round(cov, 3), "results": res}

        def _line(label, r):
            if r:
                print(f"[{venue}]   {label:8s} short: blind={r['short_blind']:+5.0f} -> fund={r['short_fund']:+5.0f}"
                      f"  (fund_leg={r['fund_short_bps']:+5.0f})  |  L/S: blind={r['ls_blind']:+5.0f} -> fund={r['ls_fund']:+5.0f}"
                      f"  n={r['n_ts']}", flush=True)
        print(f"[{venue}] h24 rmom_lo  funding-BLIND -> funding-INCLUDED:", flush=True)
        for label in ("full", "early", "recent"):
            _line(label.upper(), res[label])
        for b, _a0, _a1 in BUCKETS:
            _line(b, res["buckets"][b])
        print(flush=True)

    out_path = SHARED / "p0c_continuous_funding_2026-05-31.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"DONE -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
