# Pre-registration: W5 Continuous Stage 1 - Composite Score As Entry Priority

**Date:** 2026-06-14
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Plan:** `docs/research_plans/w5_continuous_signal_alpha/02_stage1_score_entry.md`
**Contract:** `docs/research_plans/w5_continuous_signal_alpha/00_methodology_contract.md`
**Depends on:** Stage 0 PASS
(`docs/preregistration/2026-06-14-w5-continuous-stage0-candidate-tape.md`).

## Question

Can score improve entry **priority at constant breadth**? This is NOT a filter
stage. The decision opportunities and the admitted-entry count stay matched to
the frozen control; only the *order in which contending candidates are admitted*
changes. A rule that "wins" by dropping trades is not entry-score alpha and is a
falsification, not a result.

## Mechanism (locked before the run)

The frozen control admits fresh candidates in `(signal_ts, symbol)` order
(symbol-alphabetical within a timestamp) subject to the unchanged engine state
(per-component crowding `max_fresh=2`, per-symbol cooldown, global
`max_active=25` heap, BTC-uptrend gate, age floors, inverse-vol sizing, 2f
hedge, rebalance object). Stage 1 changes **only the within-`signal_ts`
consideration order** of the candidate pool to descending arm priority score,
leaving every gate, the capacity heap, cooldown, sizing, exit, hedge, and
rebalance object byte-identical.

- Causality: priority uses only information available at `signal_ts`
  (`data_available_ts = signal_ts` on the Stage 0 tape). No cross-timestamp
  reordering (that would be look-ahead): a later-ts candidate can never be
  admitted ahead of an earlier-ts one.
- Constant breadth: per `(venue, component, signal_ts)` the number of admitted
  entries equals the control's. Because cooldown/capacity interactions are
  consideration-order-dependent, total admitted may drift slightly; the
  preregistered tolerance is **|Δ trade count| <= 2% per (venue, component)**.
  An arm whose breadth falls more than 2% below control is rejected as a
  disguised filter.
- Implementation: an additive engine knob orders the per-`signal_ts` candidate
  block by an injected `entry_priority` value; default (absent) reproduces the
  control's `(ts, symbol)` order. A0 must reproduce the Stage 0 control ledgers
  exactly (0 selected-only / 0 trade-only) as a wiring sanity check before any
  arm is interpreted.
- Contention diagnostic: report the **same-cycle replacement count** vs control
  (how many admitted entries differ from the control at a contended `signal_ts`).
  If replacement count is ~0, there is no contention to exploit and the verdict
  is a structural null ("score priority has no room under the current crowding
  gate"), which feeds Stage 8 (regime/crowding) and Stage 5 (sizing), not a
  Stage 1 win.

## Arms

All arms read the Stage 0 candidate tape and use the same engine config and the
same maximum active positions. Priority score = the listed quantity, descending.

- `A0_frozen_control`: the Stage 0 frozen `continuous_ensemble_v1` selection and
  ledgers (control).
- `A1_current_score_priority`: priority = current `composite` (the production
  score). **Runs now.**
- `A5_negative_control_priority`: priority = `symbol_hash_bucket`. No market
  content; an arm that does not beat A5 is using no real cross-sectional
  information. **Runs now.** (`month_hash_bucket` is NOT an arm: it is constant
  within a `signal_ts`, so as a within-cycle priority it is degenerate /
  identical to FCFS. It stays a tape column for other stages.)
- `A2_path_neutralized_priority`: priority = residualized `pre_24h_return`
  (Stage 7 admissible residual). **Gated on Stage 7.**
- `A3_blended_priority`: fixed walk-forward blend of `composite` rank and the
  neutralized `pre_24h_return` rank. **Gated on Stage 7.**
- `A4_vol_path_priority`: fixed walk-forward blend of `composite` rank and
  neutralized `pre_24h_realized_vol` rank. **Gated on Stage 7.**

A2/A3/A4 are registered but **not runnable from this receipt**: their residual
definition, sign, fold scheme, and blend weights are produced by the Stage 7
neutralized path-shape receipt
(`docs/research_plans/w5_continuous_signal_alpha/08_stage7_path_shape_neutralized.md`)
and must be appended (here or in the Stage 7 receipt) and locked **before** they
run. This prevents choosing a path-shape sign after seeing Stage 1 output.
Path-shape priority may only use a Stage 7-admissible residual feature; a raw
(un-neutralized) path-shape score is barred (W4 showed it is dominated by symbol
mix, 97 bps `symbol_hash_bucket` spread).

The first Stage 1 RUN is **A0 + A1 + A5** (the pure "does score-ordering at
constant breadth beat FCFS, and does it beat the hash negative control").

## Neutralization (for the gated path-shape arms)

Residualization is fit **only inside the training fold** and frozen for the
test fold: regress the candidate feature on component + `symbol_hash_bucket`
(and a within-fold seasonal term), take the residual, freeze the transform,
apply it forward. No training on future candidate outcomes. Folds are
chronological (train = earlier window, test = later window); the exact split and
the frozen coefficients are recorded in the Stage 7 receipt. `month` is used for
within-fold IC measurement only, never as a frozen transform (months do not
generalize forward).

## Metrics (per arm and venue, plus pooled)

- selected trade count and Δ vs control (breadth check);
- same-cycle replacement count vs control;
- average priority score by selected bucket;
- total return, MAR, max drawdown, worst day, bps per trade;
- rank IC of priority vs per-notional net return;
- top-vs-bottom candidate return spread (per notional);
- R1 pooled MAR delta (`scripts/r1_robustness.py`);
- bootstrap and leave-one-month-out fragility (reported, never used to rescue);
- component attribution.

## Required ledgers / artifacts

R1-compatible directories per arm:
`~/SHARED_DATA/{venue}_full_pit/reports/w5_continuous_stage1_score_entry_2026-06-14/{arm}/`
each with `ensemble_hedged_ledger.csv`, `volume_event_best_monthly.csv`,
`volume_event_research_report.json`, a selected-entry CSV, and a replacement
audit CSV (control-admitted vs arm-admitted per contended cycle). Plus a
top-level `stage1_summary.{json,md}`, effect-size table, fragility table, and a
2x-cost stress arm for any cell that clears the base pass bar.

## Roots that will be touched

- [x] `~/SHARED_DATA/bybit_full_pit` (read; writes only to `reports/<tag>/`)
- [x] `~/SHARED_DATA/binance_full_pit` (read; writes only to `reports/<tag>/`)
- [x] forward demo/paper: untouched (no orders)

## Decision rule (a priori) / Pass bar

An arm advances to Stage 6 interaction testing only if, vs A0:

- positive total return on **both** venues;
- pooled MAR delta `> +0.1`;
- neither venue MAR delta `< -0.5`;
- the negative control (A5) is materially weaker (arm pooled MAR delta exceeds
  A5 pooled MAR delta by `>= +0.1`);
- breadth not reduced by more than the 2% tolerance on either venue;
- at least two of three chronological thirds share the pooled direction;
- survives the 2x-cost stress arm.

## Falsifier

Reject this exact score-entry mechanism if it: only works on one venue; reduces
breadth materially (disguised filter); is matched by the negative control;
fails A0 same-count reconstruction; depends on post-entry or future information;
or shows ~0 same-cycle replacement count (no contention -> no mechanism, banked
as a structural null that redirects effort to Stage 8 crowding/regime and
Stage 5 sizing).

## Run command

```bash
# A0 + A1 + A5 (path-shape arms gated on Stage 7)
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage1_score_entry.py \
  --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
  --arms A0_frozen_control,A1_current_score_priority,A5_negative_control_priority \
  --out ~/SHARED_DATA/w5_continuous_stage1_score_entry_2026-06-14
```

## Post-run results

Run UTC 2026-06-14, arms A0/A1/A5, both venues, window
`2023-04-01 <= signal_ts < 2026-05-01`. Artifacts
`~/SHARED_DATA/w5_continuous_stage1_score_entry_2026-06-14/`
(`stage1_summary.{json,md}`, `stage1_replacement_audit.csv`,
`stage1_summary.csv`) and per-arm R1 ledgers under
`~/SHARED_DATA/{venue}_full_pit/reports/w5_continuous_stage1_score_entry_2026-06-14/`.
Code hash `a5149635…` (working tree; git HEAD `c05aa8b`, uncommitted).

A0 wiring sanity — `fcfs` reproduces the Stage 0 frozen control **exactly**:
bybit & binance `rows_match=True`, `equity_allclose_1e-9=True`,
`max_equity_abs_diff=0.0`.

Arm outcomes (vs A0):

| Venue | Arm | Trades | Return | MAR | Replacements vs A0 |
|---|---|---:|---:|---:|---:|
| bybit | A1 composite | 3223 | 0.7136 | 4.40 | 0 |
| bybit | A5 symbol-hash | 3223 | 0.7136 | 4.40 | 0 |
| binance | A1 composite | 2966 | 0.6754 | 5.53 | 0 |
| binance | A5 symbol-hash | 2966 | 0.6754 | 5.53 | 0 |

A1 and A5 produce **byte-identical ledgers to A0** on both venues: pooled MAR
delta `0.0`, **0 total replacements** across 3 years × 4 components × 2 venues,
breadth Δ 0.0%. The registered "~0 replacement count → structural null"
condition fires: there is **no within-`signal_ts` contention to exploit**. In the
frozen control's per-component architecture the crowding gate caps fresh entries
at 2 per timestamp while `max_active=25`, so contending candidates never compete
for a slot in a way reordering could change — confirmed at the strongest level
(zero contended cycles, not merely a small effect).

## Verdict

**NULL (structural) — A1/A5 do not advance.** Score-as-entry-priority at constant
breadth is a *mechanical no-op* for the frozen control: the gate already resolves
all within-cycle contention, so reordering cannot change a single admission. This
is a clean kill of the within-cycle entry-priority mechanism (not a marginal
miss). It is banked, and it redirects the score-as-information lever to where
breadth is genuinely held constant while the score can change outcomes:

- **Stage 7** (neutralized path-shape) — does path-shape carry residual,
  causal info beyond the composite, net of the symbol-mix confound? Runs next;
  it also unlocks the gated A2/A3/A4 path-shape priority arms here.
- **Stage 5** (score/path-shape-weighted **sizing** at constant breadth) — same
  entries, notional scaled by score; this holds breadth fixed *and* uses the
  score, so it has real room the entry-ordering lever lacked.

Distinct (breadth-changing, NOT Stage 1) future mechanisms noted for the record:
a **score-conditioned crowding admission** ("when >2 fresh candidates, admit the
top-2 by score instead of dropping the whole cycle") would have real room —
Stage 0 showed crowding rejected 1169 bybit / 2767 binance eligible candidates —
but it changes breadth and belongs to a Stage 8 admission/regime arm, not this
constant-breadth priority test. Do not relabel it as Stage 1.
