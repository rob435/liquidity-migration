# Tri-Root Validation Protocol (Long-Only Momentum)

**Mandatory gates for promotion claims.** Not interchangeable with internal walk-forward splits inside canonical IS.

| Label | Root | Window | Role |
|---|---|---|---|
| **bybit_IS** | `~/SHARED_DATA/bybit_fullpit_1h` | 2023-05-03 → 2026-05-18 | In-sample (Bybit) |
| **bybit_OOS_2022** | `~/SHARED_DATA/bybit_oos_pre2023` | **2022-01-01 → 2023-01-01** | Bybit OOS (calendar 2022) |
| **binance_OOS_2020** | `~/SHARED_DATA/binance_oos_pit` | **2020-01-01 → 2021-01-01** | Binance OOS (calendar 2020) |

## Pass criteria (each window independently)

- **Daily Sharpe ≥ 3.0** (from equity curve daily returns — honest calendar metric)
- **Trades > 100** in that window

## What this is NOT

- Not the internal `train_2023_2024` / `oos_2025_2026` splits inside canonical — those are diagnostics only.
- Not a 1,548-config grid search on IS with OOS checked once — use `scripts/tri_root_creative_gate.py` for pre-registered **structural** hypotheses only.

## Runner

```bash
PYTHONPATH=. .venv/bin/python scripts/tri_root_creative_gate.py
```

Output: `~/SHARED_DATA/bybit_fullpit_1h/reports/tri_root_creative_gate.json`

## Baseline tri-root (2026-05-24)

| Preset | bybit_IS | bybit_OOS_2022 | binance_OOS_2020 |
|---|---|---|---|
| lo_skip0 | 2.43 ✗ | **−0.73** ✗ | **5.68** ✓ |
| lo_sharpe3_robust | **3.06** ✓ | **−4.62** ✗ | 6.38 (n=88) ✗ |

**Blocker:** Bybit calendar 2022 (bear). IS tuning does not transfer.
