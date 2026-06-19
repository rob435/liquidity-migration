# Continuous V2 A4B Price/Carry Verdict

**Date:** 2026-06-19
**Parent plan:** `docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md`
**Amendment:** `docs/preregistration/2026-06-19-continuous-v2-ab-amendment-a4b-price-carry-regime.md`
**Scope:** CONTINUOUS demo/paper research only. No real-money claim.
**Run label:** `exploratory_registered_foundation`; audit label `exploratory`.

## What Ran

Command:

```powershell
.\.venv\Scripts\python.exe scripts\continuous_v2_ab_research_runner.py --mode ab --arms V2_CONTROL,V2_CONTROL_DELAYED_FEATURES,A4B_PRICE_CARRY_REGIME_HEDGE_INTENSITY,A4B_PRICE_CARRY_HASH_CONTROL --start-date 2023-04-01 --end-date 2026-06-18 --date-tag 2026-06-19 --resume --max-workers 1
```

Output root:

- `backtest-runs/continuous_v2_ab_2026-06-19/`

Robustness command:

```powershell
.\.venv\Scripts\python.exe scripts\continuous_v2_ab_research_runner.py --mode robustness --end-date 2026-06-18 --date-tag 2026-06-19 --n-boot 5000 --block 3 --seed 0
```

Robustness outputs:

- `backtest-runs/continuous_v2_ab_2026-06-19/robustness.csv`
- `backtest-runs/continuous_v2_ab_2026-06-19/robustness.json`
- `backtest-runs/continuous_v2_ab_2026-06-19/robustness_report.md`

The legacy `scripts/r1_robustness.py` expects the old `volume_event_*` report layout, so the checked-in continuous runner now writes the equivalent monthly-ledger diagnostics for this A/B format.

## Artifact Audit

MCP artifact audit result:

- Bybit `A4B_PRICE_CARRY_REGIME_HEDGE_INTENSITY`: 10/10 core artifacts present.
- Binance `A4B_PRICE_CARRY_REGIME_HEDGE_INTENSITY`: 10/10 core artifacts present.
- Bybit `A4B_PRICE_CARRY_HASH_CONTROL`: 10/10 core artifacts present.
- Binance `A4B_PRICE_CARRY_HASH_CONTROL`: 10/10 core artifacts present.

Audit caveat: artifact presence does not prove PIT, causal correctness, or untouched-OOS validity. Forward demo/paper remains the arbiter.

## A/B Metrics

| Arm | Venue | Return | Max DD | MAR | Sharpe-like | Worst day | Trades |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `V2_CONTROL` | Bybit | +97.40% | -5.49% | 5.660 | 3.708 | -3.70% | 2367 |
| `V2_CONTROL` | Binance | +84.02% | -3.27% | 8.185 | 3.514 | -2.25% | 2149 |
| `A4B_PRICE_CARRY_REGIME_HEDGE_INTENSITY` | Bybit | +98.44% | -5.15% | 6.100 | 3.756 | -3.71% | 2367 |
| `A4B_PRICE_CARRY_REGIME_HEDGE_INTENSITY` | Binance | +84.13% | -3.31% | 8.086 | 3.516 | -2.25% | 2149 |
| `A4B_PRICE_CARRY_HASH_CONTROL` | Bybit | +96.99% | -5.56% | 5.572 | 3.698 | -3.70% | 2367 |
| `A4B_PRICE_CARRY_HASH_CONTROL` | Binance | +84.16% | -3.38% | 7.926 | 3.523 | -2.25% | 2149 |

Pooled:

| Arm | Mean return | Min return | Mean max DD | Trades |
| --- | ---: | ---: | ---: | ---: |
| `V2_CONTROL` | +90.71% | +84.02% | -4.38% | 4516 |
| `A4B_PRICE_CARRY_REGIME_HEDGE_INTENSITY` | +91.28% | +84.13% | -4.23% | 4516 |
| `A4B_PRICE_CARRY_HASH_CONTROL` | +90.57% | +84.16% | -4.47% | 4516 |

## Robustness

Continuous-runner monthly robustness versus `V2_CONTROL`:

| Arm | Venue | Return delta | MAR delta | Top-3 positive-month share | Min LOO return delta | Bootstrap MAR delta p5 | P(MAR delta > 0) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `A4B_PRICE_CARRY_REGIME_HEDGE_INTENSITY` | Bybit | +1.03pp | +0.439 | 62.4% | +0.53pp | -0.250 | 71.7% |
| `A4B_PRICE_CARRY_REGIME_HEDGE_INTENSITY` | Binance | +0.11pp | -0.099 | 43.9% | -0.05pp | -0.570 | 23.0% |
| `A4B_PRICE_CARRY_HASH_CONTROL` | Bybit | -0.42pp | -0.089 | 81.0% | -0.88pp | -0.165 | 35.1% |
| `A4B_PRICE_CARRY_HASH_CONTROL` | Binance | +0.13pp | -0.260 | 50.4% | -0.23pp | -0.205 | 80.3% |

Cross-venue loose backtest rule:

- `A4B_PRICE_CARRY_REGIME_HEDGE_INTENSITY`: pooled MAR delta `+0.170`; Bybit MAR delta `+0.439`; Binance MAR delta `-0.099`. This clears the loose backtest-only demo-candidate rule but remains exploratory until forward/demo evidence exists.
- `A4B_PRICE_CARRY_HASH_CONTROL`: pooled MAR delta `-0.174`; falsified by pooled MAR delta <= 0.

Sub-period read:

- Bybit A4B return delta is positive in all three monthly thirds.
- Binance A4B is not: third 1 `+0.25pp`, third 2 `+0.01pp`, third 3 `-0.21pp`.
- Bootstrap left tails are negative on both venues, so the result is not robust enough to accept as alpha.

## Verdict

A4B is a legitimate next object for **operator-approved forward shadow/demo observation**, not an accepted parameter change and not a deployment instruction.

Reason:

- The real A4B timing score beats the hash control on pooled MAR and Bybit.
- The pooled loose backtest rule is positive.
- But Binance MAR worsens versus control, Binance's final third is negative, and the bootstrap MAR left tail is negative on both venues.
- The hash control does not beat A4B on pooled MAR, but Binance's tiny return/Sharpe improvement under the hash null is enough to keep the timing claim weak.

Do not wire A4B into the live demo/paper book from this receipt alone. If the operator wants to spend a forward slot, run it as a no-order shadow or a clearly separate demo/paper sleeve with this receipt as the starting hypothesis. Otherwise park it and move to the stronger path-shape shadow/screen idea from the foundation receipt.

No promotion, deployment, or real-money claim is made here.
