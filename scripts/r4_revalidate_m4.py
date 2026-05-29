"""R4 re-validation on the M4 calendar-shift forward-return fix (commit 9f52819).

9f52819 changed ``signal_harness._attach_forward_returns`` from a positional row
shift to a CALENDAR-offset join, so ``fwd_ret_1d`` (the R4 regression target) is now
calendar-correct: a symbol's gapped day (delist->relist / data hole) yields a null
forward return instead of a misaligned multi-day one. The original R4 validation
(tag ``r4_risk_model_2026-05-29``) ran on the pre-fix target, so the committed
numbers must be re-derived on the corrected target before publishing.

This runner builds the 7-factor panel once per venue (full-PIT, M4-corrected) and
runs TWO cross-sectional fits:
  * 7-factor (incl. ``xs_rank_ret_3d``) -> RE-CONFIRM the drop decision: is
    xs_rank_ret_3d still sign-inconsistent / criterion-1-failing on corrected returns?
  * 6-factor (the canonical ``_FACTOR_COLUMNS``) -> regenerate criteria 1/2/3.

Read-only on the working roots (writes a JSON report to ~/SHARED_DATA). Full-PIT by
construction (build_feature_panel reads the *_full_pit root).

Dispatch (5950X, Windows):
    $env:POLARS_MAX_THREADS=8; .venv\\Scripts\\python.exe -u scripts\\r4_revalidate_m4.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.risk_model import (  # noqa: E402
    _FACTOR_COLUMNS,
    MS_PER_DAY,
    compute_btc_beta,
    fit_factor_returns,
)
from liquidity_migration.signal_harness import (  # noqa: E402
    _aggregate_daily_klines,
    _attach_daily_returns,
    _autodetect_dataset_names,
    _date_str_to_ms,
    _read_window,
    _xs_rank,
    build_feature_panel,
)

SHARED = Path.home() / "SHARED_DATA"
TAG = "r4_revalidate_m4_2026-05-29"
START_DATE, END_DATE = "2023-04-01", "2026-05-28"
TRADING_DAYS_PER_YEAR = 365
VENUES = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}

# 7-factor superset (the 6 canonical reused specs + xs_rank_ret_3d, the dropped one).
SPECS_7 = [
    "xs_rank_ret_3d", "xs_rank_ret_30d", "realized_vol_7d",
    "funding_rate_z", "liquidity_rank", "premium_index_z",
]
COLS_7 = [
    "btc_beta", "xs_rank_ret_3d", "xs_rank_ret_30d", "realized_vol_rank",
    "funding_rate_z", "liquidity_rank", "premium_index_z",
]


def _sharpe(series: list[float]) -> float:
    a = np.asarray([v for v in series if v is not None], dtype=float)
    if a.size < 2 or a.std(ddof=1) == 0.0:
        return 0.0
    return float(a.mean() / a.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))


def _build_panel7(root: Path) -> pl.DataFrame:
    """build_factor_panel logic, but with the 7-factor superset (incl. xs_rank_ret_3d)."""
    feat = build_feature_panel(
        root, start=START_DATE, end=END_DATE,
        feature_specs=",".join(SPECS_7), forward_horizons=(1,),
    )
    if feat.is_empty():
        return pl.DataFrame()
    feat = _xs_rank(feat, "realized_vol_7d", out_col="realized_vol_rank")
    start_ms, end_ms = _date_str_to_ms(START_DATE), _date_str_to_ms(END_DATE)
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    klines = _read_window(
        root, kname, start_ms=start_ms - 90 * MS_PER_DAY, end_ms=end_ms,
        columns=["ts_ms", "symbol", "open", "high", "low", "close", "volume_base", "turnover_quote", "date"],
    )
    if klines.is_empty():
        feat = feat.with_columns(pl.lit(None, dtype=pl.Float64).alias("btc_beta"))
    else:
        daily_returns = _attach_daily_returns(_aggregate_daily_klines(klines))
        feat = feat.join(compute_btc_beta(daily_returns), on=["symbol", "ts_ms"], how="left")
    keep = ["symbol", "ts_ms", "date"] + [c for c in COLS_7 if c in feat.columns]
    if "fwd_ret_1d" in feat.columns:
        keep.append("fwd_ret_1d")
    return feat.select(keep).sort(["ts_ms", "symbol"])


def _factor_sharpes(factor_returns: pl.DataFrame, cols: list[str]) -> dict:
    return {
        f: _sharpe(factor_returns.filter(pl.col("factor") == f)["factor_return"].to_list())
        for f in cols
    }


def _max_pairwise_corr(factor_returns: pl.DataFrame, cols: list[str]) -> dict:
    wide = factor_returns.pivot(values="factor_return", index="ts_ms", on="factor").drop_nulls()
    out = {}
    for fi in cols:
        mx, worst = 0.0, None
        for fj in cols:
            if fi == fj or fi not in wide.columns or fj not in wide.columns:
                continue
            xi, xj = wide[fi].to_numpy(), wide[fj].to_numpy()
            if xi.size >= 2 and xi.std() > 0 and xj.std() > 0:
                c = abs(float(np.corrcoef(xi, xj)[0, 1]))
                if c > mx:
                    mx, worst = c, fj
        out[fi] = {"max_abs_corr": mx, "with": worst}
    return out


def main() -> int:
    print(f"R4 re-validation (M4 fix)  tag={TAG}  {START_DATE}->{END_DATE}")
    print(f"7-factor superset={COLS_7}\ncanonical 6={_FACTOR_COLUMNS}\n", flush=True)
    for venue, root in VENUES.items():
        if not root.exists():
            print(f"SKIP {venue}: {root} not found")
            continue
        print(f"[{venue}] build 7-factor panel from {root} ...", flush=True)
        p7 = _build_panel7(root)
        if p7.is_empty():
            print(f"[{venue}] EMPTY panel -- skipping")
            continue
        present7 = [c for c in COLS_7 if c in p7.columns]
        present6 = [c for c in _FACTOR_COLUMNS if c in p7.columns]
        fr7, _ = fit_factor_returns(p7, factor_cols=present7)
        fr6, res6 = fit_factor_returns(p7, factor_cols=present6)
        sh7 = _factor_sharpes(fr7, present7)
        sh6 = _factor_sharpes(fr6, present6)

        raw = p7.select("fwd_ret_1d").drop_nulls()["fwd_ret_1d"].to_numpy()
        raw_std = float(raw.std()) if raw.size else 0.0
        r6 = res6["residual_return"].drop_nulls().to_numpy()
        ratio6 = (float(r6.std()) / raw_std) if raw_std > 0 else None

        report = {
            "venue": venue, "tag": TAG, "panel_rows": int(p7.height),
            "n_factor_return_days_7": int(fr7["ts_ms"].n_unique()),
            "n_factor_return_days_6": int(fr6["ts_ms"].n_unique()),
            "sharpe_7factor": sh7,
            "sharpe_6factor_canonical": sh6,
            "sharpe_6_all_positive": all(v > 0 for v in sh6.values()),
            "xs_rank_ret_3d_sharpe_7f": sh7.get("xs_rank_ret_3d"),
            "max_abs_pairwise_corr_6": _max_pairwise_corr(fr6, present6),
            "raw_fwd_ret_std": raw_std,
            "residual_std_6": float(r6.std()) if r6.size else 0.0,
            "residual_mean_6": float(r6.mean()) if r6.size else 0.0,
            "residual_std_over_raw_6": ratio6,
        }
        out = SHARED / f"{TAG}_{venue}.json"
        out.write_text(json.dumps(report, indent=2))
        print(
            f"[{venue}] xs_rank_ret_3d Sharpe(7f)={sh7.get('xs_rank_ret_3d'):.3f}  "
            f"canonical-6 Sharpe>0: {sum(1 for v in sh6.values() if v > 0)}/{len(present6)}  "
            f"resid6/raw={ratio6!r}  resid_mean={report['residual_mean_6']:.2e}  -> {out.name}\n",
            flush=True,
        )
    print("DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
