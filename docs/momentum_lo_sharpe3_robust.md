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

| Root | Trades | Daily Sharpe | Return |
|---|---:|---:|---:|
| Bybit canonical 2023–26 | 307 | **3.06** | +49.7% |
| Binance pre-2023 2020–23 | 282 | **4.12** | +71.1% |
| Bybit pre-2023 (`lo_sharpe3` only) | 120 | 0.73 | +7.0% |

Binance OOS is the stronger validation for the filtered-momentum mechanism; Bybit pre-2023 remains thin for the tight vol-cap family.

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
