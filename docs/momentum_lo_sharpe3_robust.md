# LO Sharpe-3 Robust — `lo_sharpe3_robust` preset

**Date:** 2026-05-24 (round 3)  
**Change vs `lo_sharpe3`:** `max_realized_vol` **1.2 → 1.6** (allows more mid-vol names, +99 trades)

## Targets

| Target | `lo_sharpe3` | **`lo_sharpe3_robust`** |
|---|---:|---:|
| Daily Sharpe (canonical) | 3.95 | **3.06** ✓ |
| Trades (canonical) | 208 | **307** ✓ |
| oos_2025_2026 Sharpe | 0.57 | **0.76** |
| oos_2025_2026 return | +6.4% | **+9.3%** |

Still meets **Sharpe ≥ 3** and **>100 trades** on canonical; weak leg improved materially.

## Cross-root sanity (one-shot, pre-registered roots)

| Root | `lo_sharpe3_robust` | `lo_sharpe3` (tighter vol cap) |
|---|---|---|
| Bybit canonical 2023–26 | **307 trades, daily Sharpe 3.06, +49.7%** | 208 trades, daily Sharpe 3.95, +62.1% |
| Binance pre-2023 2020–23 | **282 trades, daily Sharpe 4.12, +71.1%** | 183 trades, daily Sharpe 3.47, +44.9% |
| Bybit pre-2023 2021–23 | **182 trades, daily Sharpe 1.74, +15.8%** | 120 trades, daily Sharpe 0.73, +7.0% |

**Key result:** loosening `max_realized_vol` 1.2 → 1.6 **more than doubles bybit_pre2023 daily Sharpe (0.73 → 1.74)** — the cleanest OOS root. This is the first preset in the sharpe3 family with all three roots positive. The canonical Sharpe drop (3.95 → 3.06) is the cost: less concentrated, more trades, but generalizes better.

Run label stays `exploratory_in_sample` — the 19-trial walk-forward selection doesn't disappear, but the three-root OOS pattern is now corroborating rather than failing.

## Config

Same as `lo_sharpe3` except **`max_realized_vol=1.6`**.

```bash
python -m liquidity_migration momentum-factor \
  --preset lo_sharpe3_robust \
  --start 2023-05-03 --end 2026-05-18
```

## Run label

`exploratory_in_sample` — second grid pass (quick robust hunt, 19 variants). Not `candidate` until forward paper confirms.

## Mechanism (unchanged)

Edge is **filtering**, not raw momentum rank: liquid names (turnover rank ≤ 15), sub-160% realized vol, positive TS momentum, carry off. Loosening vol cap 1.2→1.6 keeps Sharpe while fixing 2025–26 under-trading.
