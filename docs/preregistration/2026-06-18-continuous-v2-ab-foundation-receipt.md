# Continuous V2 A/B Foundation Receipt

**Date:** 2026-06-18; updated 2026-06-19 after the control/almanac/screen runs.
**Scope:** CONTINUOUS demo/paper research only. No real-money claim.
**Parent plan:** `docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md`
**Code HEAD while generated:** `ce4cdb1b8bece5521e0ca296aae4f9e7259462ca` plus local foundation changes.
**Run labels:** almanac `feature_almanac_data_proof`; A/B report audit label `exploratory`, run context `exploratory_registered_foundation`.

## What Was Implemented

Added checked-in dispatcher:

- `scripts/continuous_v2_ab_research_runner.py`

The runner has three modes:

- `--mode almanac`: builds the V2 feature almanac from full-PIT roots.
- `--mode ab`: runs registered V2 A/B arms through the repo-native component-ledger plus frozen rebalance/hedge path.
- `--mode screen`: joins the control ledgers to causal almanac features and writes discovery-only feature screens.

The runner supports `--arms`, `--venues`, `--start-date`, `--end-date`, `--out-root`, `--resume`, `--max-workers`, and `--date-tag`.

The V2 control path is explicit in code:

- Three current components: `p3`, `p4p3`, `p4p5`.
- `rmom_quantile=0.25`.
- `feature_set=("max_ret168",)`.
- `BTC_TREND_GATE=uptrend`.
- inverse-vol entry sizing with `target_vol_per_name=0.01` and clamp `2.0`.
- `entry_crowding_max_fresh=2`.
- 10% component TP and 24h fixed hold.
- no daemon/server stop stack.
- adverse-entry breaker retained at 8 adverse exits in 24h.
- frozen max4 rebalance, BTC+ETH hedge, and BTC-vol regime overlay.

No old local artifact directory is imported. The harness is a frozen component-ledger control path, not a literal shared-book daemon replay; do not compare its headline numbers byte-for-byte to the live-style InvVol+Max4 diagnostic receipt.

## Phase 0 Control Runs

Command:

```powershell
.\.venv\Scripts\python.exe scripts\continuous_v2_ab_research_runner.py --mode ab --arms V2_CONTROL,V2_CONTROL_DELAYED_FEATURES --start-date 2023-04-01 --end-date 2026-06-18 --date-tag 2026-06-19 --resume --max-workers 1
```

Output root:

- `backtest-runs/continuous_v2_ab_2026-06-19/`

Artifact audit:

- Bybit `V2_CONTROL`: 10/10 core artifacts present.
- Binance `V2_CONTROL`: 10/10 core artifacts present.
- Bybit `V2_CONTROL_DELAYED_FEATURES`: 10/10 core artifacts present.
- Binance `V2_CONTROL_DELAYED_FEATURES`: 10/10 core artifacts present.
- Audit caveat still applies: artifact completeness does not prove PIT hygiene, causal correctness, or OOS validity.

Control metrics:

| Venue | Return | Max DD | MAR | Sharpe-like | Worst day | Trades |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Bybit | +97.40% | -5.49% | 5.66 | 3.71 | -3.70% | 2367 |
| Binance | +84.02% | -3.27% | 8.19 | 3.51 | -2.25% | 2149 |
| Pooled mean | +90.71% | -4.38% | n/a | n/a | n/a | 4516 |

Delayed-feature sanity result:

| Venue | Return delta | DD delta | MAR delta | Sharpe-like delta | Trade-count delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| Bybit | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0 |
| Binance | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0 |

This passes the Phase 0 delayed-feature sanity check: current v2 control does not consume the uncertain external feature families.

The corrected p3 Bybit component count is 857 trades, matching the frozen component-ledger parity anchor. The remaining difference versus the live-style InvVol+Max4 receipt is engine-boundary, not a control reproduction pass/fail: this harness tests the frozen component-ledger object and excludes shared-book daemon state and sniper.

## Feature Almanac

Command:

```powershell
.\.venv\Scripts\python.exe scripts\continuous_v2_ab_research_runner.py --mode almanac --start-date 2023-04-01 --end-date 2026-06-18 --date-tag 2026-06-19
```

Output root:

- `backtest-runs/continuous_v2_feature_almanac_2026-06-19/`

Required files written:

- `feature_inventory.csv`
- `feature_tape_bybit.parquet`
- `feature_tape_binance.parquet`
- `coverage_by_symbol_year.csv`
- `coverage_by_component.csv`
- `latency_audit.csv`
- `negative_controls.csv`
- `feature_corr.csv`
- `readme.md`
- `summary.json`

Candidate tape sizes:

- Bybit: 12,541 rows, 599 symbols, 3 components.
- Binance: 12,893 rows, 570 symbols, 3 components.

Inventory result:

- Bybit: 24 of 38 features currently marked admissible.
- Binance: 24 of 38 features currently marked admissible.

New value-built external families:

- funding level/change: Bybit 100.0% coverage, Binance 99.94% coverage.
- premium level/change: Bybit 100.0% coverage, Binance 99.85% coverage.

Still gated:

- OI level/change/acceleration: Bybit 72.98% coverage; Binance 6.64%-7.66%.
- taker-flow horizons: Bybit 47.32% coverage; Binance 7.44%.
- market/idiosyncratic flow: values exist only as candidate-symbol aggregates, not full-market residualized flow.
- `flow_resid_return` and `flow_squeeze`: intentionally not value-built.
- long/short ratio, liquidations, and depth/book-thinning fields.

Honest read: the almanac is now enough for control reproduction plus BTC/market/funding/premium regime context. It is not enough to run C2/C3 as serious order-flow arms, and it is not enough to run A4 as written because the preregistered multifactor regime score includes aggregate OI/taker-flow squeeze pressure.

## Discovery Screens

Command:

```powershell
.\.venv\Scripts\python.exe scripts\continuous_v2_ab_research_runner.py --mode screen --start-date 2023-04-01 --end-date 2026-06-18 --date-tag 2026-06-19 --venues bybit,binance
```

Output root:

- `backtest-runs/continuous_v2_feature_screens_2026-06-19/`

Required files written:

- `trade_feature_screen.csv`
- `daily_feature_screen.csv`
- `executed_feature_tape_bybit.parquet`
- `executed_feature_tape_binance.parquet`
- `daily_feature_tape_bybit.parquet`
- `daily_feature_tape_binance.parquet`
- `readme.md`
- `summary.json`

Screen population:

| Venue | Executed trade rows | Daily rows | Unmatched feature rows |
| --- | ---: | ---: | ---: |
| Bybit | 2367 | 617 | 0 |
| Binance | 2149 | 566 | 0 |

Discovery reads:

- `B1_SCORE_MARGIN_SIZING` is not ready from this screen. `score_margin_d9_d8` has weak trade-level signal, mixed top-minus-bottom return, and is comparable to null controls.
- Path-shape fields are more interesting than score margin: `path_ret_6h_max` has same-sign symbol-demeaned trade rank-IC on both venues. That is a screen result only; it points to a future path-shape receipt/shadow, not an immediate unregistered A/B.
- Regime features show some plausible tail-risk context (`funding_change`, BTC vol, breadth/dispersion), but A4 as written remains blocked by missing OI/taker-flow squeeze inputs.
- Order-flow/squeeze features remain data-gated for both-venue claims. Binance executed order-flow coverage is only about 8% and almanac candidate coverage is about 7%; Bybit order-flow coverage is not enough to make a both-venue C2/C3 claim.

## Verification

Focused tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_continuous_v2_ab_research_runner.py
```

Result: 6 passed.

Lint:

```powershell
.\.venv\Scripts\python.exe -m ruff check scripts\continuous_v2_ab_research_runner.py tests\test_continuous_v2_ab_research_runner.py
```

Result: all checks passed.

## Blocked Arms

Do not run `A4_REGIME_HEDGE_INTENSITY`, `C2_MARKET_FLOW_HEDGE_INTENSITY`, or `C3_FLOW_SQUEEZE_HEDGE_INTENSITY` as serious arms from this receipt.

- `A4_REGIME_HEDGE_INTENSITY`: blocked as written because OI/taker-flow squeeze inputs are not admissible. A price/funding/premium-only regime arm would need a dated amendment and a new arm id; it must not be mislabeled A4.
- `C2_MARKET_FLOW_HEDGE_INTENSITY`: blocked because full-market residualized flow is not available with both-venue/full-window coverage.
- `C3_FLOW_SQUEEZE_HEDGE_INTENSITY`: blocked because OI and taker-flow coverage are insufficient, and `flow_squeeze` is not value-built.
- `B1_SCORE_MARGIN_SIZING`: not blocked by data coverage, but the screen is too weak to justify spending a serious A/B slot.

Exactly next: no serious A/B arm is ready from the current evidence without a dated amendment. The strongest lawful next step is either:

- write an amendment for a narrower price/funding/premium regime hedge-intensity arm, with a new arm id and hash control; or
- run a path-shape-specific screen/shadow receipt before any path-shape lifecycle intervention.

## 2026-06-19 Follow-Up

The narrower price/carry regime arm was registered and run as `A4B_PRICE_CARRY_REGIME_HEDGE_INTENSITY`, with `A4B_PRICE_CARRY_HASH_CONTROL` as the null:

- Amendment: `docs/preregistration/2026-06-19-continuous-v2-ab-amendment-a4b-price-carry-regime.md`
- Verdict: `docs/preregistration/2026-06-19-continuous-v2-a4b-price-carry-verdict.md`

Short read: A4B clears the loose backtest-only pooled MAR rule, but Binance MAR worsens, bootstrap left tails are negative, and the timing claim stays weak. It is not accepted as a parameter change and must not be wired into the demo/paper book from this receipt alone.

No promotion, deployment, or real-money claim is made here.
