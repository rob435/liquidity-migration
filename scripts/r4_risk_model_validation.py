"""R4 — risk-factor model validation run (Round 2).

Pre-reg: docs/research_summary.md sub-phase R4.

Runs the R4 risk model (liquidity_migration/risk_model.py) on each full_pit venue
root and checks the plan's three validation criteria:

  1. FACTOR REALITY:  each factor's daily factor-return Sharpe > 0.
  2. NON-REDUNDANCY:  pairwise |corr| of factor-return series < 0.3 (factors are
     not proxies for each other).
  3. VARIANCE CAPTURE: the factor model reduces residual variance by MORE than
     chance — the real residual std is below a within-day target-shuffle permutation
     null (p_value < 0.05). (The old `residual_std < raw_std` check was an in-sample
     R^2>=0 tautology that passed even for a zero-signal model; see
     risk_model.residual_variance_capture.)

These gate whether the residual-Sharpe (Tier-3) machinery rests on a sound factor
model. Per the plan, factors failing (1)/(2) are candidates to drop (target 5-6
stable factors per venue) -- this run reports; the R4 verdict doc decides.

Full-PIT by construction (build_factor_panel reads the *_full_pit root). In-process,
one venue at a time (build_factor_panel does 2 sequential klines reads -> peak
~20-23 GB) -> memory-safe on 32 GB.

Dispatch (5950X):
    POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/r4_risk_model_validation.py
Windows: ``$env:POLARS_MAX_THREADS=8; .venv\\Scripts\\python.exe -u scripts\\r4_risk_model_validation.py``

Artifacts (~/SHARED_DATA): r4_risk_model_2026-05-29_<venue>.json (per-factor Sharpe,
pairwise corr, residual variance reduction).
"""
from __future__ import annotations

import json
import sys
from datetime import date
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
    build_factor_panel,
    fit_factor_returns,
    residual_variance_capture,
)

SHARED = Path.home() / "SHARED_DATA"
TAG = "r4_risk_model_2026-05-29"
START_DATE = "2023-04-01"
END_DATE = "2026-05-28"
TRADING_DAYS_PER_YEAR = 365
VENUES = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}


def _sharpe(series: list[float]) -> float:
    a = np.asarray([v for v in series if v is not None], dtype=float)
    if a.size < 2 or a.std(ddof=1) == 0.0:
        return 0.0
    return float(a.mean() / a.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))


def main() -> int:
    window_days = (date.fromisoformat(END_DATE) - date.fromisoformat(START_DATE)).days
    print(f"R4 risk-model validation  tag={TAG}  window={START_DATE}->{END_DATE} ({window_days}d)")
    print(f"factors={_FACTOR_COLUMNS}\n")

    for venue, root in VENUES.items():
        if not root.exists():
            print(f"SKIP {venue}: root not found at {root}")
            continue
        print(f"[{venue}] build_factor_panel from {root} ...", flush=True)
        panel = build_factor_panel(root, start=START_DATE, end=END_DATE)
        if panel.is_empty():
            print(f"[{venue}] EMPTY panel -- skipping")
            continue
        print(f"[{venue}] panel rows={panel.height}", flush=True)

        factor_returns, residuals = fit_factor_returns(panel, factor_cols=_FACTOR_COLUMNS, target_col="fwd_ret_1d")
        present = [c for c in _FACTOR_COLUMNS if c in panel.columns]

        # (1) per-factor return Sharpe
        factor_sharpe = {}
        for f in present:
            ser = factor_returns.filter(pl.col("factor") == f)["factor_return"].to_list()
            factor_sharpe[f] = _sharpe(ser)

        # (2) pairwise |corr| of factor-return series (wide on ts_ms)
        wide = factor_returns.pivot(values="factor_return", index="ts_ms", on="factor").drop_nulls()
        corr_flags = {}
        max_abs_corr = {}
        for fi in present:
            mx = 0.0
            worst = None
            for fj in present:
                if fi == fj or fi not in wide.columns or fj not in wide.columns:
                    continue
                xi = wide[fi].to_numpy()
                xj = wide[fj].to_numpy()
                if xi.size >= 2 and xi.std() > 0 and xj.std() > 0:
                    c = abs(float(np.corrcoef(xi, xj)[0, 1]))
                    if c > mx:
                        mx, worst = c, fj
            max_abs_corr[fi] = {"max_abs_corr": mx, "with": worst}
            corr_flags[fi] = mx >= 0.3

        # (3) variance capture vs a within-day target-shuffle permutation null
        # (replaces the in-sample residual_std<raw_std tautology).
        vc = residual_variance_capture(panel, factor_cols=_FACTOR_COLUMNS, target_col="fwd_ret_1d")
        res = residuals["residual_return"].drop_nulls().to_numpy()
        res_mean = float(res.mean()) if res.size else 0.0
        # B4 transparency: forward-survivorship exposure — panel rows whose
        # strictly-forward return is null (delisting/data-gap terminal days) are
        # necessarily dropped from every cross-sectional factor-return regression.
        n_null_target = int(panel.select(pl.col("fwd_ret_1d").is_null().sum()).item())

        report = {
            "venue": venue, "tag": TAG, "window_days": window_days,
            "panel_rows": int(panel.height), "n_factor_return_days": int(factor_returns["ts_ms"].n_unique()),
            "factor_sharpe": factor_sharpe,
            "factors_sharpe_positive": {f: (s > 0) for f, s in factor_sharpe.items()},
            "max_abs_pairwise_corr": max_abs_corr,
            "factors_redundant_ge_0p3": corr_flags,
            "residual_mean": res_mean,
            # Honest variance-capture: same-population raw/residual std + permutation null.
            "variance_capture": vc,
            "captures_real_variance": vc["captures_real_variance"],
            "raw_fwd_ret_std": vc["raw_std"], "residual_std": vc["residual_std"],
            "residual_std_over_raw": vc["residual_std_over_raw"],
            "fwd_survivorship_null_target_rows": n_null_target,
            "fwd_survivorship_null_target_frac": (n_null_target / panel.height) if panel.height else 0.0,
        }
        out = SHARED / f"{TAG}_{venue}.json"
        out.write_text(json.dumps(report, indent=2))
        n_pos = sum(1 for v in factor_sharpe.values() if v > 0)
        n_redundant = sum(1 for v in corr_flags.values() if v)
        print(
            f"[{venue}] factors Sharpe>0: {n_pos}/{len(present)}  redundant(|corr|>=0.3): {n_redundant}  "
            f"resid_std/raw_std={vc['residual_std_over_raw']!r}  captures_real_variance={vc['captures_real_variance']} "
            f"(p={vc['p_value']!r}, null_p05_ratio={vc['null_ratio_p05']!r})  resid_mean={res_mean:.2e}  "
            f"null_target_rows={n_null_target}  -> {out.name}\n",
            flush=True,
        )

    print("DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
