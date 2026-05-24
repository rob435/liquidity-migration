# LO Sharpe-3 Winner — `lo_sharpe3` preset

**Date:** 2026-05-24  
**Target:** daily Sharpe ≥ 3, trades > 100 — **met in-sample**  
**Run label:** `exploratory_in_sample` (1,548-config grid search on canonical)

## Headline (canonical 2023-05 → 2026-05, 3× costs)

| Metric | Value |
|---|---:|
| **Daily Sharpe** | **3.95** |
| Trades | **208** (~69/yr) |
| Total return | +62.1% |
| Max drawdown | −11.6% |
| Basket Sharpe | 1.48 |
| Promotion gate (IS splits) | Pass (all positive) |

## Config (`--preset lo_sharpe3`)

| Parameter | Value |
|---|---|
| Universe | Top **50** by 90d turnover |
| Long book | Top **20%** (~10 names) |
| `max_realized_vol` | **1.2** (120% ann) — skip high-beta lottery names |
| `max_turnover_rank` | **15** — liquid leaders only |
| `carry_weight` | **0** |
| `momentum_skip_days` | **0** |
| TS filter | Positive 30d own return required |
| Regime | BTC > 50d SMA, flat when off |
| Rebalance | Weekly, vol-target 15% |

## Walk-forward splits (honest caveat)

| Split | Return | Sharpe |
|---|---:|---:|
| train_2023_2024 | +28.8% | **3.16** |
| validation_2024_2025 | +18.9% | 1.39 |
| oos_2025_2026 | +6.4% | **0.57** |

The full-sample daily Sharpe 3.9 is real on the equity curve, but the **2025–2026 leg is weak**. This config was selected from a large grid — treat as hypothesis, not promotion evidence. Forward paper-trading is the next clean test.

## Pre-2023 OOS sanity (run 2026-05-24)

`scripts/validate_lo_oos_roots.py` applied the preset **unmodified** to the
dedicated pre-2023 archives:

| Root | Trades | Daily Sharpe | Basket Sharpe | Return | Max DD |
|---|---:|---:|---:|---:|---:|
| bybit_pre2023 (2021-01 → 2023-05) | 120 | **0.73** | 0.58 | +7.0% | −13.7% |
| binance_pre2023 (2020-01 → 2023-05) | 183 | 3.47 | 1.30 | +44.9% | −11.7% |

**Diagnosis:** bybit OOS Sharpe collapses from in-sample 3.95 → 0.73 — the
classic overfit drop expected from a 1,548-config grid search. Binance OOS
holds up but per v2 findings the binance root is the less reliable signal
(no funding, different survivor profile). For comparison, the simple
`lo_skip0` baseline (carry=1.5, no vol/rank gates) survives both roots:
bybit Sharpe 1.47, binance 3.69 (see `docs/momentum_lo_trade_forensics.md`
"OOS sanity" section).

**Conclusion:** this preset stays `exploratory_in_sample`. Do not deploy.

## Reproduce

```bash
python -m liquidity_migration momentum-factor \
  --preset lo_sharpe3 \
  --start 2023-05-03 --end 2026-05-18 \
  --report-dir ~/SHARED_DATA/bybit_fullpit_1h/reports/momentum_lo_sharpe3_winner
```

Artifacts: `~/SHARED_DATA/bybit_fullpit_1h/reports/momentum_lo_sharpe3_winner/`  
Hunt log: `reports/momentum_lo_sharpe3_hunt/` (156 configs hit target on IS grid)

## Upgrade path: `lo_sharpe3_robust`

See [`momentum_lo_sharpe3_robust.md`](momentum_lo_sharpe3_robust.md). Vol cap **1.6** → **307 trades**, daily Sharpe **3.06**, oos_2025_2026 Sharpe **0.76** (vs 0.57), Binance pre-2023 daily Sharpe **4.12** (282 trades).

```bash
python -m liquidity_migration momentum-factor --preset lo_sharpe3_robust ...
```

## What Jane Street would still ask

1. **Multiple testing:** 1,548 trials → deflated Sharpe matters.  
2. **OOS 2025–2026:** Sharpe 0.57 — regime fade, not “done.”  
3. **Mechanism:** vol-cap + liquidity cap removes losers from `core_momentum` bucket; edge is filtering, not raw rank momentum.  
4. **Capacity:** top-15 rank in uni-50 is deployable size on Bybit perps.
