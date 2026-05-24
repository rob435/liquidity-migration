# Long-Only Momentum — Trade Forensics Study

**Date:** 2026-05-24  
**Status:** `exploratory` — Sharpe 3.0 long-only **not met** on canonical; best honest daily Sharpe **~2.46** with adequate trade count.  
**Artifacts:** `~/SHARED_DATA/bybit_fullpit_1h/reports/momentum_lo_trade_study/`  
**Code:** `liquidity_migration/momentum_trade_forensics.py`, `scripts/momentum_lo_trade_study.py`

## What we ran

1. **193 long-only variants** on canonical Bybit PIT (2023-05 → 2026-05): rebalance 3/7d, universe 30/50, quantile 20/33%, regime on/off, carry 0/1.5, vol-target 0/15%, TS filter on/off.
2. **410,595 pooled trades** enriched with entry-time cross-section (rank, breadth, BTC 7d return, dispersion) and post-hoc path stats (MAE/MFE).
3. **Causal filter discovery** on train split only (features knowable at entry — **not** path MFE/MAE).
4. **8 candidate configs** re-run through the full `momentum_factor` pipeline with entry gates.

## Headline answer (Sharpe ≥ 3?)

| Config | Trades | Daily Sharpe | Basket Sharpe | Return | Max DD | Verdict |
|---|---:|---:|---:|---:|---:|---|
| LO_skip0 baseline | 540 | **2.43** | 0.93 | +40% | −15.5% | Best honest default |
| LO_carry0 (sweep winner) | 637 | **2.46** | 0.91 | +37% | −12.9% | Slight IS tweak: `carry_weight=0` |
| LO_vol_cap_063 | 74 | 6.31* | 2.36 | +71% | −7.4% | *Sparse — ~25 trades/yr; daily Sharpe inflated |
| LO_3d_rebal (more trades) | 1,479 | 0.92 | 0.52 | +17% | −15.2% | More trades, worse risk-adj return |

**Honest daily Sharpe ~2.5 with ~180 trades/year is the ceiling found in-sample.**  
Basket-level Sharpe stays ~0.9–2.4 depending on annualization. Sharpe 3 long-only net of 3× costs is not supported without either (a) very few trades, or (b) combining with another sleeve.

## Trade tapestry — what wins and what bleeds

From archetype segmentation on the pooled ledger (post-hoc labels):

| Archetype | Train mean net | Val mean net | Test mean net | Notes |
|---|---:|---:|---:|---|
| **slow_grind** | +0.41% | +0.27% | +0.03% | Low formation momentum; path-friendly — only archetype positive in all splits |
| rocket | +0.09% | +0.10% | −0.09% | High mom + high TS — regime-dependent |
| crowded_long | +0.08% | +0.26% | — | High carry — small test sample |
| **core_momentum** | −0.02% | −0.14% | −0.13% | **Default bucket — majority of trades, negative expectancy** |
| high_beta_lottery | −0.01% | ~0 | −0.11% | Vol > 150% ann — avoid |
| fade_risk | +0.51% | −0.13% | −0.18% | High mom, low TS — unstable |

**Jane-Street read:** the factor is paying you to *not* hold generic “top decile momentum” names (`core_momentum`). Edge concentrates in grindier, less-extended leaders and in avoiding high-vol lottery names.

## Causal entry filters (train-discovered, no lookahead)

Top single-axis rules on **entry-time** features only:

| Feature | Rule | Train Sharpe proxy | Interpretation |
|---|---|---:|---|
| realized_vol | ≤ 0.52 | 3.74 | Skip high-beta alts; biggest clean win |
| turnover_rank | ≤ 3 | 3.08 | Stick to top-3 liquidity names in universe |
| score (composite) | ≤ 0.27 | 3.08 | Avoid extreme z-score winners (crowded) |
| btc_7d_return | ≥ 12% | 2.94 | Only add risk when BTC trend is hot |
| universe_breadth | ≤ 27% | 2.50 | Prefer selective / weak-breadth days |
| momentum_dispersion | ≥ 0.19 | 2.20 | Need cross-sectional spread |

**Disqualified:** `path_mfe` / `path_mae` filters — they use future bars during the hold and produce fake Sharpe 6+.

## Validated pipeline configs

Full backtests with entry gates wired into `MomentumFactorConfig`:

```
# Best trade-frequency / Sharpe balance (recommended exploratory default)
mode=long_only, momentum_skip_days=0, carry_weight=0.0,
require_positive_ts_momentum_for_longs=True, vol_target_annual=0.15,
regime_off_scale=0.0, weekly rebalance

# Low-vol sleeve (fewer trades, higher return per trade — label sparse)
+ max_realized_vol=0.63   → 74 trades, +71% IS, all splits positive, basket Sharpe 2.36
```

CLI example:

```bash
PYTHONPATH=. .venv/bin/python -m liquidity_migration momentum-factor \
  --start 2023-05-03 --end 2026-05-18 \
  --mode long_only --momentum-skip-days 0 --carry-weight 0 \
  --require-positive-ts-momentum-for-longs \
  --vol-target-annual 0.15 --regime-off-scale 0.0 \
  --report-dir ~/SHARED_DATA/bybit_fullpit_1h/reports/momentum_lo_carry0
```

(`max_realized_vol` / `max_turnover_rank` are config fields — wire CLI flags if you promote this.)

## Why more trades hurt

3-day rebalance + wider quantile pushed **1,479 trades** but **daily Sharpe 0.92**. Churn eats edge; the signal is weekly-scale. More names ≠ more alpha here.

## Update — Sharpe 3 target met (in-sample grid)

After a **1,548-config** causal filter hunt (`scripts/hunt_lo_sharpe3.py`), preset **`lo_sharpe3`** achieves:

- **Daily Sharpe 3.95**, **208 trades**, +62% return, −11.6% max DD  
- See [`momentum_lo_sharpe3_winner.md`](momentum_lo_sharpe3_winner.md)

## Honest run labels

| Result | Label |
|---|---|
| **lo_sharpe3** daily Sharpe 3.95, 208 trades | `exploratory_in_sample` (grid-mined) |
| LO_carry0 daily Sharpe 2.46, 637 trades | `exploratory_in_sample` |
| LO_vol_cap_063 daily Sharpe 6.3, 74 trades | `exploratory_sparse` — do not promote on headline Sharpe |
| Pooled filter mining | `exploratory` — pre-test on OOS or forward only |
| lo_sharpe3 oos_2025_2026 split | Sharpe **0.57** — weak; forward test required |

## OOS sanity — pre-2023 roots (run 2026-05-24)

`scripts/validate_lo_oos_roots.py` re-ran `lo_skip0` and `lo_sharpe3` presets on
the dedicated pre-2023 OOS archives (untouched by the trade-study tuning):

| Preset | Root | Trades | Daily Sharpe | Basket Sharpe | Return | Max DD |
|---|---|---:|---:|---:|---:|---:|
| **lo_skip0** | bybit_pre2023 | 320 | **1.47** | 0.75 | +13.9% | −8.9% |
| **lo_skip0** | binance_pre2023 | 698 | **3.69** | 1.39 | +59.1% | −14.8% |
| lo_sharpe3 | bybit_pre2023 | 120 | **0.73** | 0.58 | +7.0% | −13.7% |
| lo_sharpe3 | binance_pre2023 | 183 | 3.47 | 1.30 | +44.9% | −11.7% |
| **lo_sharpe3_robust** | bybit_pre2023 | 182 | **1.74** | 0.86 | +15.8% | −11.7% |
| **lo_sharpe3_robust** | binance_pre2023 | 282 | **4.12** | 1.55 | +71.1% | −12.1% |

**Read:** the simple `lo_skip0` baseline survives both pre-2023 roots. The
grid-mined `lo_sharpe3` collapses to **Sharpe 0.73 on bybit_pre2023** (vs IS
3.95) — textbook overfit signature, exactly what the multiple-testing concern
predicts. Binance OOS holds up for both, but per v2 findings the binance root
is the less reliable signal. **Treat lo_skip0 as the OOS-validated default;
lo_sharpe3 stays `exploratory_in_sample`.** New `lo_carry0` preset codifies
recommendation #1 below — no OOS evidence yet (forward-test required).

Artifacts: `~/SHARED_DATA/bybit_fullpit_1h/reports/momentum_lo_oos_sanity/`

## Recommended next steps

1. **Adopt `carry_weight=0`** as the LO default pending one forward paper-trading window (OOS roots are spent per integrity standard).
2. **Optional vol-cap sleeve** (`max_realized_vol=0.63`) as a low-turnover booster inside a combined book — not standalone at Sharpe 3.
3. **Do not** promote path-stat filters; keep them for post-trade review only.
4. For **Sharpe 3 portfolio**: combine LO_carry0 (~2.5 daily) with the validated short sleeve (~3.4) — already plotted in `docs/lo_skip0_and_combined.png`.
5. Forward-walk 6+ months is the only clean evidence left.

## Reproduce

```bash
PYTHONPATH=. .venv/bin/python scripts/momentum_lo_trade_study.py
PYTHONPATH=. .venv/bin/python scripts/validate_lo_candidates.py
```
