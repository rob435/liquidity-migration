# Pre-registration: W4 Continuous Stage 1 - Stop / Exit Realism

**Date:** 2026-06-13
**Author:** Codex
**Stage:** complete

## What's changing

Run the frozen `continuous_ensemble_v1` component family on both full-PIT roots
under registered stop/protective-exit arms, then combine the component ledgers
with the frozen ensemble weights and frozen BTC hedge/rebalance object.

## Hypothesis

The frozen component signal may remain economically usable after modeling the
live 25% disaster stop and protective exits, but the result is fragile if it
depends on a friendly stop fill. This stage tests the live stop/protective-exit
mechanism and the adverse stop-fill falsifier directly.

## Predicted direction + magnitude

- The 25% disaster stop alone should reduce the worst tail at the cost of some
  return, with small-to-moderate MAR impact.
- The failed-fade/breakeven protective exits should be insurance, not alpha:
  acceptable if drawdown and worst-path metrics improve without destroying
  both-venue return.
- The uncapped bar-extreme stop arm is expected to be worse than capped stop
  fills; if it flips the conclusion, the stage is not robust enough for a live
  lifecycle claim.

Failure mode if wrong: the live stop/protective exits erase positive return in
either venue, materially worsen MAR/drawdown, or depend on capped stop fills so
strongly that tick/5m calibration becomes mandatory before any lifecycle claim.

## Arms

All arms use the frozen four component definitions:

- p3: `turn3_pop3`, age >= 240d, TP10, weight 0.30
- p4p3: `turn4_pop3`, age >= 240d, TP10, weight 0.20
- p4p5: `turn4_pop5`, age >= 240d, TP10, weight 0.40
- tp14: no event trigger, age >= 210d, TP14, weight 0.10

Common component base: `rmom_quantile=0.25`, `feature_set=("max_ret168",)`,
`liq_turnover_min=500000`, `entry_delay_hours=1`, `exit_mode="fixed"`,
`hold_hours=24`, `btc_trend_gate="uptrend"`, inverse-vol sizing target 1%,
crowding cap 2, funding on.

Registered arms:

- `00_frozen_no_stop`: exact frozen component replay control.
- `01_disaster_stop_capped`: add `stop_loss_pct=0.25`,
  `stop_fill_mode="bar_extreme_capped"`, `stop_slippage_cap_pct=0.10`.
- `02_stop_ff6_be10`: add capped disaster stop plus `failed_fade_hours=6`,
  `failed_fade_loss_pct=0.04`, `failed_fade_min_mfe_pct=0.01`,
  `breakeven_arm_pct=0.10`.
- `03_stop_uncapped_extreme`: add `stop_loss_pct=0.25`,
  `stop_fill_mode="bar_extreme"` as the adverse stop-fill falsifier.

## Roots that will be touched

- [x] bybit_full_pit
- [x] binance_full_pit
- [ ] forward demo/paper (no live order or paper-shadow change)

## Decision rule (a priori)

Primary comparison is each non-control arm versus `00_frozen_no_stop` after
frozen ensemble weighting and frozen BTC hedge/rebalance. Metrics are read in
return units, MAR delta, drawdown/worst-day units, and bps per component trade.

`02_stop_ff6_be10` is "insurance-supported for forward watch only" if:

- total return remains positive on both venues;
- pooled MAR delta is no worse than -0.25;
- no venue MAR delta is worse than -0.50;
- daily max drawdown or worst-day loss improves on both venues; and
- the uncapped stop-fill falsifier (`03_stop_uncapped_extreme`) does not flip
  either venue to non-positive return.

It is rejected in this registered form if return is non-positive on either
venue, pooled MAR delta <= -0.50, any venue MAR delta <= -1.00, or the uncapped
stop-fill falsifier changes the conclusion. A rejection blocks only this exact
stop/protective-exit mechanism.

## Amendment A - Data-Gate Window Adjustment (2026-06-13)

Stage 0 showed that local Bybit `klines_1h`/manifest currently end at
`2026-06-02`, while local Binance `klines_1h`/manifest end at `2026-04-30`.
No Stage 1 alpha result has been run or inspected. The original `2026-06-10`
end boundary is therefore not runnable on both mandatory venues from the
current local roots.

For this Stage 1 run only, the historical working-dataset window is amended to
`2023-04-01 <= signal_ts < 2026-05-01`, the common local full-PIT boundary.
This is a data-availability amendment, not a threshold or decision-rule change.
Forward evidence remains zero and is not cited.

## Run command

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python scripts/w4_continuous_stop_exit_realism.py \
  --venues bybit,binance \
  --start 2023-04-01 \
  --end 2026-05-01 \
  --out ~/SHARED_DATA/w4_continuous_stage1_stop_exit_2026-06-13

PYTHONPATH=. .venv/bin/python scripts/r1_robustness.py \
  --sweep-tag w4_continuous_stage1_stop_exit_2026-06-13 \
  --control 00_frozen_no_stop \
  > ~/SHARED_DATA/w4_continuous_stage1_stop_exit_2026-06-13/stage1_r1_robustness.txt
```

## Required artifacts

- Per-component per-venue `continuous_trades.csv`,
  `continuous_mtm_equity.csv`, and `continuous_report.json`.
- Per-arm per-venue ensemble ledger and monthly returns.
- R1-compatible `volume_event_best_monthly.csv` and
  `volume_event_research_report.json`.
- Stage summary JSON/CSV/Markdown with data-root identity, code hash, config
  hashes, effect sizes, adverse-path diagnostics, and the registered falsifier.

## Amendment B - PIT Metadata Repair (2026-06-13)

The first R1 pass marked the Stage 1 reports partial-PIT because the W4 harness
reader incorrectly assumed `archive_trade_manifest/date=*/symbol=*` partitions.
The actual full-PIT roots store manifests as `archive_trade_manifest/date=*/part.parquet`
with `symbol` as a column. This was a metadata gate bug, not a trade simulation
or threshold bug.

The harness was patched and run in `--metadata-only` mode. Trade ledgers were
not rerun or changed. The repaired PIT gate passes both venues for the amended
window:

- Bybit: `manifest_pairs=442397`, `required_pairs=442369`,
  `kline_pairs_in_window=442891`, `missing_symbols=0`,
  `missing_required_pairs=0`.
- Binance: `manifest_pairs=417956`, `required_pairs=417956`,
  `kline_pairs_in_window=418463`, `missing_symbols=0`,
  `missing_required_pairs=0`.

Metadata repair details are embedded in
`~/SHARED_DATA/w4_continuous_stage1_stop_exit_2026-06-13/stage1_summary.json`:
trade-simulation code hash `d03c4d13e3688cf3d670e15358519e1ddfe033cae1aad9dc7c86c401ab4a7b0d`,
repair code hash `83b942ac0fa81c9efbff04638886e8b495a83aa75f8cd7bec47a459f65f4cdc8`,
`trade_simulation_rerun=false`.

## Post-run results

Artifacts:

- Stage summary:
  `~/SHARED_DATA/w4_continuous_stage1_stop_exit_2026-06-13/stage1_summary.{json,csv,md}`.
- Adverse-path rows:
  `~/SHARED_DATA/w4_continuous_stage1_stop_exit_2026-06-13/stage1_adverse_path.csv`.
- R1 robustness receipt:
  `~/SHARED_DATA/w4_continuous_stage1_stop_exit_2026-06-13/stage1_r1_robustness.txt`.
- Per-venue per-arm ledgers and R1 JSON:
  `~/SHARED_DATA/{bybit,binance}_full_pit/reports/w4_continuous_stage1_stop_exit_2026-06-13/`.

Run identity:

- Git HEAD: `e7ce8c81ad076a055aa59d64362333024a78c7af`.
- Frozen forward config hash:
  `1fc760f14567a204d73f36d5ffb81243d40196338ec72f9e7b4f137f431f0017`.
- Full-PIT roots: `~/SHARED_DATA/bybit_full_pit`,
  `~/SHARED_DATA/binance_full_pit`.
- Window: `2023-04-01 <= signal_ts < 2026-05-01`.

Primary registered arm `02_stop_ff6_be10` versus `00_frozen_no_stop`:

| Venue | Control Return | Arm Return | Return Delta | Control MAR | Arm MAR | MAR Delta | Control DD | Arm DD | Worst Day |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `bybit` | 0.7136 | 0.0261 | -0.6875 | 4.3957 | 0.0858 | -4.3099 | -0.0527 | -0.0985 | -0.0448 |
| `binance` | 0.6754 | 0.0439 | -0.6315 | 5.5311 | 0.2049 | -5.3261 | -0.0397 | -0.0696 | -0.0435 |

Adverse stop-fill falsifier `03_stop_uncapped_extreme`:

- Bybit stayed barely positive (`return=0.0202`, `MAR=0.0580`) but with worse
  drawdown (`-0.1129`) than control.
- Binance flipped negative (`return=-0.0333`, `MAR=-0.1119`), triggering the
  registered stop-fill falsifier.

R1 robustness after the PIT metadata repair:

- `01_disaster_stop_capped`: FALSIFY, pooled MAR delta `-3.90`.
- `02_stop_ff6_be10`: FALSIFY, pooled MAR delta `-3.97`.
- `03_stop_uncapped_extreme`: FALSIFY, return <= 0 on Binance.
- Bootstrap `P(delta > 0)=0%` for annual-return and MAR deltas in both venues
  for the primary `02_stop_ff6_be10` arm.
- Third-split positivity failed for every non-control arm in both venues.

## Verdict

REJECTED for the exact registered mechanism: capped 25% disaster stop plus
failed-fade/breakeven protective exits, and the uncapped stop-fill falsifier.

This is not a promotion, paper-ready, or real-money result. It does not close
the broader exit-realism family; it blocks only this exact stop/protective-exit
implementation on this registered window and evidence set.
