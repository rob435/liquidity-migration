# T-D — Funding forecasts beyond the next interval (exploratory, Lane 1)

**Status: EXPLORATORY.** Walk-forward development inside the spent window.
No alpha, robustness, or promotion claim; not confirmatory evidence.

## What ran

- Sample: 866,365 realized settlements (431 CONTINUOUS-ledger symbols,
  2021-05-01 → 2024-12-01), built from the shared funding panel; cadence taken
  from observed settlement spacing (never the `funding_interval_min` label).
  Targets: realized cumulative funding over (t, t+24h/48h/72h].
- All predictors PIT-known at t: just-settled rate, trailing settlement history,
  and 1h premium-index / mark / index / open-interest bars with bar end ≤ t
  (aux panel: 6,479,778 rows, hash in manifest).
- Declared candidates vs persistence (`rate_t × n_settlements`): EWMA
  (half-lives 3/10/30 settlements), mean-reversion to a trailing-90 mean
  (φ 0.5/0.8), pooled OLS on premium/basis/OI features fitted on the early era
  only. Train < 2023-02-22 (271,352 rows); scored ≥ (581,804 rows). Tail
  thresholds (|target| q95/q99) fixed from the train era.
- **Stage-2 trigger declared before scores were inspected:** ≥10% improvement
  vs persistence on BOTH overall MAE and q95 tail MAE, scored era, 24h horizon.

## Results (scored era)

| Horizon | Persistence MAE / tail-q95 | Best candidate | MAE / tail-q95 | Δ |
|---|---|---|---|---|
| 24h | 3.81 / 86.1 bps | meanrev φ0.8 (MAE) | 3.72 / 78.5 | −2.4% / −8.8% |
| 24h | — | meanrev φ0.5 (tail) | 4.00 / 74.8 | +5.0% / **−13.1%** |
| 48h | 8.40 / 184.5 | ewma hl3 | 7.72 / 156.9 | −8.1% / −15.0% |
| 72h | 13.32 / 274.2 | ewma hl3 | 11.87 / 227.0 | −10.9% / −17.2% |

- Persistence degrades with horizon exactly as the thesis predicts, and the
  predictability that exists concentrates in the tail (crazy-funding) cases —
  the cases that motivated the thesis. RMSE gains are larger than MAE gains
  (24h: 18.4 vs 24.4 bps for meanrev φ0.5, −24%), i.e. the candidates mostly
  fix the extremes, not the bulk.
- OLS-on-premium adds nothing over the cheap univariate models and covers only
  68% of settlements (aux features missing early history) — not competitive.
- **Stage-2: NOT TRIGGERED.** meanrev φ0.5 beat the tail bar (−13.1%) but not
  the overall-MAE bar; no candidate met both. Per the declared rule, the T-B
  floor substitution was not run. The rule stands as registered; the observed
  near-miss does not revise it retroactively.

## Read

Cumulative funding beyond the next interval is somewhat predictable from
PIT-known inputs, mostly at 48–72h horizons and in the tails, via shrinkage-style
rules (short-half-life EWMA / mean-reversion). At the 24h horizon that T-B's
floor actually uses, the improvement over persistence is real but small — below
the declared materiality bar.

## Limitations

- Single walk-forward split (early → late); no rolling refits.
- Pooled across symbols; no per-symbol or regime conditioning.
- Errors are measured in rate space, not translated into trade economics
  (Stage 2, which would have done that, did not trigger).
- Spent window; development evidence only.

## Next action

If a future declared design targets 48–72h holds or tail protection, ewma_hl3 /
meanrev_phi0.5 are the leads to register prospectively. Nothing advances to the
forward ledger from this stage.

Artifacts: `td_scoreboard.csv` (all 21 cells), `td_settlement_table.parquet`
(local; hash in `manifest.json`), manifest with model definitions, split, and
the Stage-2 rule + outcome.
