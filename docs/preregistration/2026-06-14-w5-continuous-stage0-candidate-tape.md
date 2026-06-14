# Pre-registration: W5 Continuous Stage 0 - Candidate Tape & Baseline Reconstruction

**Date:** 2026-06-14
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Plan:** `docs/research_plans/w5_continuous_signal_alpha/01_stage0_candidate_tape.md`
**Contract:** `docs/research_plans/w5_continuous_signal_alpha/00_methodology_contract.md`

## What's changing

Build the W5 audit surface: a per-cycle **candidate tape** that reconstructs the
full *eligible* candidate set (selected **and** rejected-but-eligible) for the
frozen continuous control on both venues, plus a baseline reconstruction of the
frozen ensemble-hedged control ledger. This stage makes **no alpha claim**; it
is the gate for every later W5 score-entry / entry / exit / sniper / sizing /
path-shape / regime stage.

The candidate tape is emitted from the *same* decision code that selects live
entries (an additive `candidate_sink` inside
`continuous_events._run_trades`), not from a parallel reimplementation — this is
the only construction that avoids the "same-code illusion" (errors-we-never-
repeat #16). When the sink is absent (every existing caller), engine behavior is
byte-identical.

## Candidate definition (locked before the run)

A **candidate** is one row per `_fresh_entries` member fed into `_run_trades`
per decision cycle, per component, i.e. a symbol that, at a signal-bar close,
has already cleared: top composite decile (D9) on the rmom-low half (q25),
spell-fresh (gap > 1h), the liquidity gate (hourly `turnover_quote >= $500k`),
and the component's event trigger (`turn3_pop3` / `turn4_pop3` / `turn4_pop5` /
`none`). Each candidate is then tagged **selected** or **rejected** with the
exact reason the engine applied, in engine order: `no_bar_symbol`, `crowding`,
`breaker`, `cooldown`, `capacity`, `no_bar_entry`, `age`, `btc_trend_unknown`,
`btc_trend`, `soft3_quintile`, `decel`, `market`, `no_fill`, `selected`.

The D9 cross-section that failed the trigger/liquidity/spell gates is *not* a
candidate (it never became actionable); its size is reported only as a coverage
diagnostic. This is the precise survivorship fix the plan demands: score-entry
work must see the rejected-but-eligible peers, not only executed trades.

## Frozen control (the reconciliation target)

`continuous_ensemble_v1`, reconstructed from its four receipt-frozen components
via the deterministic engine (`scripts/rebuild_winner_base_component_ledgers.COMMON`
+ per-component overrides) and merged with the frozen weights / rebalance / 2f
hedge object (`continuous_forward_replay.FROZEN_FORWARD_CONFIG`):

- p3 `turn3_pop3` age240 tp10 w0.30; p4p3 `turn4_pop3` age240 tp10 w0.20;
  p4p5 `turn4_pop5` age240 tp10 w0.40; tp14 `none` age210 tp14 w0.10;
- rmom q25, BTC-uptrend gate, inverse-vol sizing, crowding max-fresh 2, funding ON;
- weights `{turn3p3:.30, turn4p3:.20, turn4p5:.40, age210tp14:.10}`,
  rebalance `w90_tv0.045_max4_ddh-0.04`, BTC+ETH 2f hedge.

## Hypothesis

The frozen control's full eligible candidate set is reconstructable per cycle on
both venues with selected/rejected outcomes, and the selected subset reconciles
**exactly** (by `(symbol, entry_signal_ts_ms)`) to the freshly-rebuilt component
ledgers and — on the overlapping window — to the existing W4 `00_frozen_no_stop`
control trades; the ensemble-hedged ledger and its R1 monthly returns reproduce
the frozen control within tight tolerance.

## Predicted direction + magnitude

- No alpha metric is predicted.
- Selected-vs-trades reconciliation: exact (0 mismatched rows) by construction.
- W4 cross-check (entries with `entry_signal_ts_ms < ms(2026-05-01)`): component
  trade counts within a small data-vintage drift of the W4 control
  (bybit anchors at the W4 full-window end 2026-06-10: turn3p3 823, turn4p3 771,
  turn4p5 686, age210tp14 943 — W5 counts are lower because the window ends ~40
  days earlier; the overlap rows must match).
- Failure mode: missing roots, PIT gate fail, selected != component ledgers,
  missing timing fields, or rejected candidates not recoverable -> Stage 0 FAIL,
  and no W5 score/entry/exit/sniper/sizing/path-shape/regime stage may run.

## Window, roots, universe

- Window: `2023-04-01 <= signal_ts < 2026-05-01` (common full-PIT overlap; the
  W4 Stage 0 data clock end-gate, both venues).
- Roots (read-only source data; writes go only to `reports/<tag>/` subdirs and a
  SHARED_DATA artifact root):
  - [x] `~/SHARED_DATA/bybit_full_pit`
  - [x] `~/SHARED_DATA/binance_full_pit`
  - [x] forward demo/paper: untouched (no orders, no live state)
- Full-PIT universe is mandatory; current-universe diagnostics are not used.

## Timing fields (every candidate row)

- `decision_ts` = signal-bar close (`signal_ts_ms`);
- `data_available_ts` = signal-bar close (all 5 features are trailing closed-bar
  windows known at the bar close; rmom join is a causal day-floor lag1);
- `order_submit_ts` = `signal_ts_ms + 1h` (first bar after the deciding close);
- `fill_window` = `[signal_ts_ms + 1h, entry_bar_end_ts_ms]`
  (`entry_bar_end_ts_ms = signal_ts_ms + (1 + entry_delay_hours)*1h`);
- `exit_activation_ts` = entry bar end (fixed-hold TP/stop active from entry);
  recorded as the realized `exit_ts_ms` for selected rows;
- `state_initialization_ts` = window start (the concurrency heap / cooldown /
  circuit-breaker state warm-starts from `2023-04-01`).

## Path-shape features + negative controls (locked)

Causal path-shape features attached to every candidate at the signal bar, using
the **identical** definitions from the W4 Stage 3 receipt
(`docs/preregistration/2026-06-13-w4-continuous-stage3-path-shape.md`):
`pre_6h_return`, `pre_24h_return`, `pre_24h_realized_vol` (the nominated set),
plus `pre_6h_upper_extension`, `signal_bar_close_location` for diagnostics.

Negative controls (no market content; for the W4 path-shape parity the
`symbol_hash_bucket` 97 bps confound must be reproducible downstream):

- `symbol_hash_bucket` (W4 definition: `sha256(symbol)%1000 / 999`);
- `month_hash_bucket` (`sha256(YYYY-MM)%1000 / 999`);
- `shuffled_in_fold_composite` (composite permuted within calendar month with a
  deterministic month-seeded RNG — destroys real ranking, keeps the marginal).

## Decision rule (a priori)

Stage 0 **PASSES** only if, on **both** venues:

1. roots exist and the candidate tape is reconstructed for all four components;
2. the PIT partition gate passes (`full_pit_universe_pass = true`);
3. selected candidate rows reconcile exactly to the rebuilt component ledgers
   (0 selected-only and 0 trade-only rows on `(symbol, entry_signal_ts_ms)`);
4. selection counts by component and month reconcile to the component ledgers;
5. on the overlapping window, W5 component trades equal the W4 control trades
   restricted to `entry_signal_ts_ms < ms(2026-05-01)` (0 mismatches);
6. the ensemble-hedged ledger builds and its R1 monthly CSV is non-empty with
   finite metrics;
7. all six timing fields are present and non-null where applicable;
8. rejected-but-eligible candidates exist and are recoverable (rejected count > 0).

Any failure -> Stage 0 **FAIL**; do not run W5 Stage 1/2/3/4/5/7/8.

## Falsifier

Stage 0 fails if the candidate tape cannot be reconstructed on both venues, the
PIT gate fails, selected entries do not reconcile to the control (or to the W4
overlap), timing fields are missing, or rejected candidates are unavailable.

## Run command

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage0_candidate_tape.py \
  --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
  --out ~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14
```

## Artifacts

Under `~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14/` and the
per-venue `reports/w5_continuous_stage0_candidate_tape_2026-06-14/` cells:

- `candidate_tape_{venue}.parquet` (selected + rejected, all features/controls);
- `selected_entries_{venue}.csv`, `rejected_entries_{venue}.csv`;
- `baseline_reconstruction_{venue}.csv` (per-component + W4 overlap reconcile);
- `ensemble_hedged_ledger.csv`, `volume_event_best_monthly.csv`,
  `volume_event_research_report.json` per cell (R1-compatible);
- `stage0_summary.json`, `stage0_summary.md`, `pit_gate.json`,
  `config_hashes.json`, `code_hash.txt`.

## Post-run results

Run UTC 2026-06-14, window `2023-04-01 <= signal_ts < 2026-05-01`, both venues.
Artifacts: `~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14/`
(`candidate_tape_{venue}.parquet`, `selected_entries_{venue}.csv`,
`rejected_entries_{venue}.csv`, `baseline_reconstruction_{venue}.csv`,
`stage0_summary.{json,md}`, `pit_gate.json`, `config_hashes.json`,
`code_hash.txt`) plus per-cell R1 ledgers under
`~/SHARED_DATA/{venue}_full_pit/reports/w5_continuous_stage0_candidate_tape_2026-06-14/`.
Code hash `d6d4b68b…` (working tree; git HEAD `c05aa8b` — Stage-0 code is
uncommitted, commit SHA to be recorded if/when the operator approves a commit).
Frozen forward config hash `1fc760f1…`.

Candidate tape (per component, selected | rejected):

| Venue | turn3p3 | turn4p3 | turn4p5 | age210tp14 | total cand | selected | rejected |
|---|---|---|---|---|---:|---:|---:|
| bybit | 823 \| 3918 | 771 \| 3391 | 686 \| 2441 | 943 \| 2389 | 15362 | 3223 | 12139 |
| binance | 722 \| 4042 | 661 \| 3451 | 561 \| 2404 | 1022 \| 3931 | 16794 | 2966 | 13828 |

Rejection reasons — bybit: age 6396, btc_trend 3164, cooldown 1410, crowding
1169; binance: age 7052, btc_trend 2886, crowding 2767, cooldown 1123. (The
rejected-but-eligible set is large on both venues — the survivorship surface
Stage 1 needs.)

Gate outcomes (both venues):

1. roots present, all 4 component tapes reconstructed — PASS.
2. PIT partition gate `full_pit_universe_pass=true`; 0 missing symbols, 0 missing
   required pairs (bybit 718 manifest / 763 kline symbols; binance 682 / 694).
3. selected candidate rows reconcile exactly to the rebuilt component ledgers
   (0 selected-only, 0 trade-only) on every cell — PASS.
4. selection counts by month reconcile to the ledgers on every cell — PASS.
5. W4 overlap exact: W5 selections == W4 `00_frozen_no_stop` trades with
   `entry_signal_ts_ms < ms(2026-05-01)` on all 8 cells (0 w4-only, 0 w5-only).
   The W5 selected counts equal the full W4-window counts (book went flat before
   2026-05-01, consistent with STATE.md). Binance turn3p3 = 722 matches the
   documented parity anchor.
6. ensemble-hedged control rebuilt both venues: bybit total return 0.7136, MAR
   4.40, DD -5.27%, Sharpe 2.94, 673 ledger days / 35 months; binance 0.6754,
   MAR 5.53, DD -3.97%, Sharpe 2.93, 663 days / 34 months. R1 monthly CSVs
   non-empty with finite metrics — PASS.
7. all six timing fields present and non-null where applicable — PASS.
8. path-shape feature coverage 100% (15362/15362 bybit, 16794/16794 binance on
   pre_6h_return / pre_24h_return / pre_24h_realized_vol); negative controls
   (symbol_hash_bucket, month_hash_bucket, shuffled_in_fold_composite) attached
   to every row — PASS.

Engine instrumentation is additive (`candidate_sink` default None); the existing
continuous suite (107 tests) passes unchanged, so the frozen control's numerics
are unaffected. A short-window smoke confirmed selected-tape == executed-trades
(39==39) with rejected reasons captured.

## Verdict

**PASS** (both venues). The full eligible candidate set — selected and
rejected-but-eligible — is reconstructable per cycle from the same decision code
that selects live entries, reconciles exactly to the frozen control and to the
W4 control on the overlap, the PIT gate passes, and the ensemble-hedged control
rebuilds with sane metrics. No alpha is claimed. W5 Stage 1 (same-breadth score
entry) may proceed under its own dated preregistration; Stages 2/3/4/5/7/8
remain gated behind their own receipts.
