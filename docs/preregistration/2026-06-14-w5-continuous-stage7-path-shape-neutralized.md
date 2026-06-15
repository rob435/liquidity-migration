# Pre-registration: W5 Continuous Stage 7 - Neutralized Path-Shape Scoring

**Date:** 2026-06-14
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Plan:** `docs/research_plans/w5_continuous_signal_alpha/08_stage7_path_shape_neutralized.md`
**Contract:** `docs/research_plans/w5_continuous_signal_alpha/00_methodology_contract.md`
**Depends on:** Stage 0 PASS
(`docs/preregistration/2026-06-14-w5-continuous-stage0-candidate-tape.md`).

## Question

Does the *shape of the move into a confirmed entry* carry stable, causal,
cross-venue information about per-notional net return **after** the symbol /
component / time-of-entry mix **and the production composite score** are removed?

This is the "Stage 3b" W4 promised and never ran. W4 Stage 3
(`docs/preregistration/2026-06-13-w4-continuous-stage3-path-shape.md`) found
`pre_6h_return`, `pre_24h_return`, `pre_24h_realized_vol` admissible **raw**, but
the `symbol_hash_bucket` negative control had a **97.10 bps** pooled top-bottom
spread — i.e. a large share of the apparent path-shape effect was "which symbol
is this," not "what shape was the path." Stage 7 neutralizes that confound and
decides the question on the **residual**. A raw spread is never sufficient.

## Population (locked)

- Executed/selected continuous entries of the frozen control
  (`continuous_ensemble_v1`), both venues, window
  `2023-04-01 <= signal_ts < 2026-05-01`.
- Source: the Stage 0 candidate tape selected rows
  (`~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14/candidate_tape_{venue}.parquet`,
  `selected == true`) joined to the rebuilt component ledgers
  (`~/SHARED_DATA/{venue}_full_pit/reports/w5_continuous_stage0_candidate_tape_2026-06-14/{component}/continuous_trades.csv`)
  on `(symbol, entry_signal_ts_ms)`.
- Per-notional net return (the target), identical to W4 Stage 3:
  `return_per_notional = net_return / max(|notional_weight|, 1e-12)`.
- **Selection-conditioning caveat (disclosed, not hidden):** realized returns
  exist only for *executed* entries. This is exactly the population where an
  entry-priority / sizing path-shape feature would be applied, so executed-entry
  IC is the decision-relevant measurement. A full-candidate counterfactual
  fixed-hold return (for rejected-but-eligible peers) is noted as a future
  robustness extension, not part of this decision.

## Causal features (locked; W4-binding definitions, never raw post-entry)

Computed only from bars at/before `signal_ts` (already on the Stage 0 tape):

- `pre_6h_return`, `pre_24h_return` (the nominated return-shape pair → arm P1);
- `pre_24h_realized_vol` (the run-up vol feature → arm P2);
- combined set of the three → arm P3.

Banned as causal scores (diagnostics only): `post_6h_adverse`,
`post_6h_favorable` (post-entry path). `pre_6h_upper_extension` and
`signal_bar_close_location` were W4-rejected and are not tested here.

## Neutralization (locked before the run)

A path-shape feature is admissible only on its **residual**, never raw. For each
tested feature `F`:

1. **Chronological walk-forward split** (coverage-safe, fully a priori, uses only
   timestamps — never returns or features): per venue, order selected entries by
   `signal_ts`; **train = earliest 60%, test = most recent 40%**. Decisions are
   made on the **pooled test fold**. (At Stage-0 counts — bybit 3223, binance
   2966 selected — the test fold is ~1289 / ~1186 events, both >= the 500 floor.)
2. **Confound design matrix** `C` (fit by OLS on the **train** fold only, frozen,
   applied forward to test):
   `C = [intercept, component dummies (turn4p3, turn4p5, age210tp14 vs turn3p3
   base), symbol_hash_bucket, sin(2π·hour/24), cos(2π·hour/24),
   sin(2π·month/12), cos(2π·month/12), composite]`
   where `hour` = hour-of-day of `order_submit_ts` (UTC), `month` = month-of-year
   (1–12) of `signal_ts` (cyclical so it generalizes forward; the specific
   `YYYY-MM` is **never** a frozen transform — months do not generalize), and
   `composite` is the production score already on the tape.
3. **Residual:** `F_res = F − C·β̂_train`, computed on the test fold. The decision
   IC is `Spearman(F_res, return_per_notional)` on the test fold. Because
   `composite` is in `C`, `F_res` is already marginal over the composite.
4. **Combined arm P3:** equal-weight average of the three features' train-fold
   z-scored residuals (z-mean/std frozen from train), then ranked.

## Arms (locked)

- `P0_control`: composite score only, no path-shape (reference IC of composite vs
  return on the test fold, for the marginal-over-composite comparison).
- `P1_residual_pre_return`: residualized `pre_6h_return` and `pre_24h_return`
  (reported individually and as the nominated pair).
- `P2_residual_runup_vol`: residualized `pre_24h_realized_vol`.
- `P3_residual_combined`: residualized combination of the three (locked equal
  z-weight).
- `P4_negative_control`: `symbol_hash_bucket` pushed through the **identical**
  pipeline, with confound set `C \ {symbol_hash_bucket}` (a variable cannot be a
  confound for itself). Because symbol-mix is *retained* in P4 but *removed* from
  P1–P3, P4 is a deliberately strong (conservative) control: path-shape must beat
  a control that still carries symbol structure.

Auxiliary controls reported (not the gate): `month_hash_bucket` and
`shuffled_in_fold_composite` through the same pipeline; and a
"symbol-retained" variant of P1–P3 (residualized against `C \ {symbol_hash_bucket}`)
so the reader sees the residual with and without symbol-hash removed. Raw
(un-neutralized) IC/spread is printed alongside every arm for context only.

## Metrics (test fold; per venue and pooled)

- Spearman IC of the residual score vs `return_per_notional` (decision statistic);
- top–bottom **tercile** spread (bps per notional) of the residual score;
- the same spread by chronological thirds of the test fold (fragility);
- residual marginal IC over the composite (partial Spearman, composite-only
  residualization of both sides) — cross-check of gate #6;
- coverage / missing-feature counts;
- raw (un-neutralized) IC and spread, for context;
- winsorized (1%/99%) tercile spread as a robustness cross-check (decision is on
  the unwinsorized mean-tercile spread to match W4; a winsorization sign flip is
  flagged).

## Pass bar (a priori) — admissible to feed Stage 1 / 2 / 5

A path-shape arm is admissible only if, on the **residual** test-fold score:

1. both venues have **>= 500** covered events;
2. Spearman IC has the **same sign** on both venues;
3. pooled top–bottom spread has the **same sign** on both venues **and
   >= 25 bps** per notional;
4. **>= 2 of 3** chronological thirds of the test fold agree in sign;
5. the residual arm's **absolute spread exceeds** the neutralized
   negative-control (`P4`) absolute spread — the gate W4 could not clear;
6. **positive marginal IC over the composite** (gate #3 statistic with composite
   partialled out is positive on the pooled test fold).

Default label is the lowest defensible: this stage is **`exploratory`** — it
changes no live book and is an admissibility gate only. An admissible residual
feeds a downstream engine stage (Stage 1 A2/A3/A4 priority, Stage 2 entry style,
Stage 5 `Z2_path_shape_size`) that must still beat its control on **pooled MAR,
both venues**, before anything is a demo/paper candidate. Admissibility here is
necessary, not sufficient.

## Falsifier

Reject path-shape as a usable feature if, after neutralization: the effect is no
larger than the neutralized hash-bucket control (P4); the IC flips sign across
venues; the spread lives in a single chronological third; the marginal IC over
the composite is <= 0 (it is just the composite relabeled); or coverage is
< 500 on a venue. A raw spread cannot rescue any of these — W4 already showed raw
path-shape is dominated by symbol mix.

## Window, roots, universe

- Window: `2023-04-01 <= signal_ts < 2026-05-01` (common full-PIT overlap).
- Roots (read-only; writes only to `~/SHARED_DATA/w5_continuous_stage7_*` and
  `reports/<tag>/`):
  - [x] `~/SHARED_DATA/bybit_full_pit`
  - [x] `~/SHARED_DATA/binance_full_pit`
  - [x] forward demo/paper: untouched (no orders, no live state).
- Full-PIT universe mandatory; current-universe diagnostics not used. The Stage 0
  PIT partition gate result is carried forward and re-asserted.

## Timing fields

Inherited from the Stage 0 tape (every selected row already declares
`decision_ts`, `data_available_ts`, `order_submit_ts`, `fill_window`,
`exit_activation_ts`, `state_initialization_ts`). Stage 7 adds no new fill; it
scores existing causal features against realized per-notional return.

## Run command

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage7_path_shape_neutralized.py \
  --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
  --stage0 ~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14 \
  --out ~/SHARED_DATA/w5_continuous_stage7_path_shape_neutralized_2026-06-14
```

## Artifacts

Under `~/SHARED_DATA/w5_continuous_stage7_path_shape_neutralized_2026-06-14/`:

- `stage7_events_{venue}.csv` (test-fold rows: feature, residual, composite,
  symbol_hash_bucket, component, hour, month, return_per_notional);
- `stage7_feature_effects.csv` (raw + residual IC/spread, per venue/pooled/arm);
- `stage7_thirds.csv` (chronological-third residual spreads, test fold);
- `stage7_neutralization_coeffs.json` (frozen train β per arm/venue);
- `stage7_summary.{json,md}` (root identity, code hash, frozen config hash, PIT
  status, per-arm admissibility, run label, falsifier outcome).

## Post-run results

Run UTC 2026-06-14, both venues, window `2023-04-01 <= signal_ts < 2026-05-01`,
git HEAD `5dd4e12` (Stage 7 code uncommitted; code hash `4e33c121…`), frozen
forward config hash `1fc760f1…`. Walk-forward split: earliest 60% train / most
recent 40% test by `signal_ts`. Coverage: bybit 1288 / binance 1191 test events
(both >= the 500 floor). Artifacts
`~/SHARED_DATA/w5_continuous_stage7_path_shape_neutralized_2026-06-14/`
(`stage7_summary.{json,md}`, `stage7_feature_effects.csv`, `stage7_thirds.csv`,
`stage7_events_{venue}.csv`, `stage7_neutralization_coeffs.json`). Stage 0 PASS
reproduced locally first (all gates true, both venues).

Per-venue residual statistics (test fold):

| Venue | Arm | Spearman IC | Tercile spread bps |
|---|---|---:|---:|
| bybit | P1 pre-return pair | 0.2205 | 252.45 |
| bybit | P2 run-up vol | 0.1926 | 170.93 |
| bybit | P3 combined | 0.2209 | 236.14 |
| bybit | P4 symbol-hash (neg) | 0.1260 | 237.25 |
| bybit | composite (raw ref) | 0.1829 | 193.92 |
| binance | P1 pre-return pair | 0.2082 | 212.74 |
| binance | P2 run-up vol | 0.2208 | 81.70 |
| binance | P3 combined | 0.2425 | 159.61 |
| binance | P4 symbol-hash (neg) | 0.0871 | 184.80 |
| binance | composite (raw ref) | 0.0988 | 67.73 |

Marginal IC over composite (pooled test): `pre_6h_return` +0.189,
`pre_24h_return` +0.195, `pre_24h_realized_vol` +0.178 — all positive.

**The registered decision statistic (cross-venue pooled tercile spread) is
invalid for this data (stop-work finding).** A 400-draw null characterization of
the test fold shows the tercile-spread sampling SD is enormous because
per-notional returns are heavy-tailed: per-symbol-random spread SD = 128 bps
(bybit) / 175 bps (binance), 95% band ±240 / ±339 bps; per-trade-random spread
SD = 74 / 96 bps. So the 25-bps floor and the "beat the neg-control spread"
comparison are pure noise — the symbol-hash control spread (237/185) and the
path-shape spreads (252/213) all sit *inside* the per-symbol null band. The
pooled-spread non-monotonicity (P1 pooled 206.70 < both per-venue 252.45 and
212.74) is the symptom that exposed it.

**The robust statistic is rank-IC, and it is clean.** Per-symbol-random IC null
SD = 0.047 / 0.051, 95% band ±0.09 / ±0.10. Against that null:

- path-shape residual IC = 0.22 / 0.21 clears the per-symbol null by ~4–5 SD on
  both venues;
- the symbol-hash control IC = 0.13 / 0.09 is roughly half as large (and *not*
  significant on binance — inside the ±0.10 band);
- path-shape IC is ~2× the symbol-hash control IC on both venues and stays
  positive marginal over composite.

## Verdict

**NULL as registered** — no arm clears the predeclared pooled-spread-vs-control
gate (#5). But that gate is a **methodology error**: the tercile-spread statistic
is noise-dominated for this heavy-tailed return cross-section (per-symbol null SD
128–175 bps), so it cannot decide the question. I am **not** overturning the
registered verdict by swapping statistics after seeing output. Stage 7's
`exploratory` label stands and **no admissibility is claimed** from it.

This is therefore **not a clean kill** of path-shape. The robust rank-IC
(path-shape 0.21–0.22, ~4–5 SD over the per-symbol null, ~2× the symbol-hash
control, same sign both venues, positive over composite) is a real
cross-sectional signal — but it could still be symbol *selection* rather than
within-symbol *timing*. The decisive, predeclared follow-up is **Stage 7b —
within-symbol fixed-effects rank-IC with permutation nulls**
(`docs/preregistration/2026-06-14-w5-continuous-stage7b-within-symbol-pathshape.md`).
Within-symbol demeaning removes ALL symbol mix by construction (`symbol_hash` is
constant within a symbol → its within-symbol IC is identically zero), isolating
exactly the harvestable part: for a given symbol, do its higher-path-shape
entries earn more, beyond the composite? 7b's verdict is the binding one for the
path-shape-as-entry/sizing-feature question; it gates Stage 1 A2/A3/A4 and the
Stage 5 `Z2_path_shape_size` bucket.
