# DRAFT — Strategy Research V4 theses (post-V3 generation)

Status: exploratory research program, Lane-1/Lane-2 model, UNREGISTERED DRAFT.
Nothing here changes the deployed runtime, profile, sizing, or the active
90-day epoch. Successor to `DRAFT_strategy_research_v3_2026-07-19.md`, whose
four theses closed 2026-07-19 (T-A gate-removal refuted; T-B floor structure
correct but mis-parameterized and drain exits refuted; T-C acceleration premise
refuted; T-D forecasts real but below the declared materiality bar). Evidence
cards: `reports/strategy-research-v3/*/2026-07-19/summary.md`.

**Provenance warning (read first).** Every diagnostic number below was
computed AND inspected on 2026-07-19 from the spent V2 discovery surface
(barebones CONTINUOUS ledger, 16,745 shorts, entries 2021-05-06 → 2024-12-01).
These are post-hoc discoveries: the T-C/T-B feature panels were built for
other hypotheses, and the cuts below were chosen after seeing bucket tables.
Their selection risk is materially higher than V3's owner-derived mechanisms.
Nothing below is evidence. Each thesis's spent-surface counterfactual run
is design work; judgment comes only from data the rule's config commit
predates (forward rolling ledger), or from a future registered surface.

## Working rules (inherited from V3, plus one)

- Tune freely on the spent surface; label everything exploratory; report all
  grid cells; era-stability (early/late split at 2023-02-22) on every result.
- The `[2025-01-01, 2026-07-06)` label-level V2 holdout stays unread.
- Effect sizes always beside the modeled cost of the exact trade shape.
- **Double-verification across gate states (new, owner-directed).** T-A's
  paired renders exist precisely so mechanisms can be tested on a larger,
  differently-conditioned sample: the barebones ledger is ungated (16,745
  trades), the deployed-shape render books are gated (2,300 entries) and
  ungated (4,019 entries). A candidate rule must show the same-signed,
  comparable-magnitude effect on (a) the barebones ledger, (b) both render
  books where the rule is implementable there, and (c) both eras, before it
  becomes a forward-ledger prototype. A rule that only works under one gate
  state is a regime artifact until proven otherwise — that is itself a
  reportable finding, not a discard.

## Big-PC runbook

Same as V3 (interpreter `.venv\Scripts\python.exe`; whole-repo Ruff + focused
pytest, never `dev.sh check` on Windows; data root
`C:\Users\user\SHARED_DATA\bybit_full_pit`; outputs under
`reports/strategy-research-v3/<thesis>/<date>/` with manifests). New code in
`scripts/research_v3/`. POSIX-only runtime imports require
`scripts/research_v3/run_with_stub.py` for any render or RMOM rebuild.
Reusable V3 machinery: verified shared caches (funding panel, 1h kline slice,
aux panel), exact daily-curve reconstruction (`common.py`), lifecycle
re-simulation (`tc_pump_deceleration.resimulate_from`), floor/drain machinery
(`tb_funding_floor.py`), forecast table (`td_funding_forecast.py`), paired
render flag (`--research-disable-btc-gate`).

---

## T-E. Fresh-high entry conditioning (the strongest V3 residue)

**Observed structure (inspected, spent).** Bucketing all 16,745 entries by
hours since the trailing-168h high at the entry bar close:

| Bucket | n | net (% capital) | mean bps/trade | era signs |
| --- | ---: | ---: | ---: | --- |
| at high (≤1h) | 2,530 | **+8.03** | +0.32 | **+ / +** (+0.78 / +0.13) |
| 1–6h | 1,105 | −2.22 | −0.20 | + / − |
| 6–24h | 2,336 | −13.70 | −0.59 | − / − |
| >24h | 10,774 | −12.33 | −0.11 | + / − |

The at-high bucket is the only cut found in all of V3 that is positive in
both eras, and it also has the highest TP rate (23.4% late). It subsumes the
T-C result: "decelerating" entries were toxic mostly because they are stale
(their losses sit in the >24h sub-bucket, −10.86%).

**Mechanism story.** The sleeve shorts pump events. Shorting at the blow-off
high captures the immediate reversion impulse; entering hours-to-days after
the high means the impulse is spent, and the 24h max-hold window collects
cost and funding drag plus second-leg risk (V2 MAE −13.4%-class paths). Cost
per trade (24.4 bps/unit) exceeds mean gross (21.9 bps/unit), so pruning the
stale mass is the single largest available lever.

**Execution plan.**
1. Declare the grid now: skip entries with hours_since_high_168h > H for
   H ∈ {1, 6, 24} (three cells; no other thresholds will be tried on this
   surface). Secondary declared variant: sizing tilt (weight 1.0 at-high,
   0.5 for 1–6h, 0.25 beyond) instead of hard skip.
2. Re-run the fixed-capital recurrence per cell (existing machinery), full
   metrics + era split + salience check (gross forgone vs cost+funding
   saved), exactly as T-B/T-C reported.
3. Double-verification arm: recompute the same feature from PIT klines for
   the two render books' entry sets (component `continuous_trades.csv`
   entries carry timestamps/symbols) and report the bucket monotonicity on
   the gate-on and gate-off books separately. No engine change needed for
   the diagnostic arm; an engine-level entry condition is built only if the
   ledger-level rule survives.
4. Deliverable: one grid table + bucket tables per book; winners become
   forward-ledger prototypes with the H value frozen at commit time.

**Failure modes to report, not hide:** the at-high bucket may proxy listing
age, volatility, or symbol-mix drift; report bucket composition by year,
symbol count, and overlap with the T-G funding buckets.

## T-F. MFE give-back ladder (adaptive exits)

**Observed structure (inspected, spent).** 53% of trades reach ≥+5% MFE.
The 6,228 that reach ≥+5% and still miss TP12 finish at mean +1.9% gross
after touching mean +7.9% — a ~6%/trade give-back, era-stable (5.8% early,
6.0% late). The entire profit mass of the book flows through this pool: the
MFE≥5% cohort sums to +408% of capital in net contribution while the whole
book nets −20%.

**Relationship to prior closures (mandatory context).** The 2026-06-20 1m
intrabar push closed a "dynamic-TP" book with no candidate on the both-venue
sub-hourly engine. This thesis is a different object: the barebones 1h shape
has NO adaptive exits at all (pure TP12 / 24h max-hold / no stop), the
lifecycle machinery already carries dormant `mfe_giveback_trigger_pct` /
`mfe_giveback_retain_pct` / `breakeven_arm_pct` hooks, and the target is
give-back capture on 1h bars, not intrabar timing. If this fails too, the
adaptive-exit direction is dead on both granularities and gets recorded as
such.

**Execution plan.**
1. Declared grid: arm at MFE ≥ A for A ∈ {4%, 6%, 8%}; exit at bar close
   once close-return retraces below R × MFE for R ∈ {0.5, 0.7}; plus the
   breakeven variant (arm at A, exit at close-return ≤ 0). 6 + 3 = 9 cells,
   all reported.
2. Re-simulate every trade from its recorded entry on 1h bars with the same
   fill conventions as `resimulate_from` (TP touch precedence, boundary
   close), charging funding per settlement to the new exit.
3. The decisive accounting: captured give-back vs forfeited TP completions
   (29.8% of the MFE≥5% pool currently completes to TP12) vs unchanged
   trades. Era split; MAE distribution shift; interaction with T-E cells
   (grid runs on both the full ledger and the surviving T-E book — declared
   as a 2-axis report, no post-hoc cell addition).
4. Double-verification arm: engine-level giveback exists as config; if the
   ledger-level rule survives, render both gate states with the winning
   (A, R) via a research-only config override, using the T-A flag pattern.
5. Deliverable: 9-cell grid × {full ledger, T-E-filtered} × era, with the
   forfeited-TP decomposition explicit.

## T-G. Funding-state entry conditioner (T-B's mechanism, correct comparator)

**Observed structure (inspected, spent).** Bucketing by the settled rate
known at entry (strictly-PIT "prev" convention):

| Rate bucket | n | net (% capital) | mean bps | era signs |
| --- | ---: | ---: | ---: | --- |
| deep neg (< −0.1%/interval) | 1,305 | −5.91 | −0.45 | **− / −** (−0.41 / −0.48) |
| neg | 2,303 | −3.30 | −0.14 | + / − |
| ~zero | 10,190 | −12.28 | −0.12 | + / − |
| pos (shorts paid) | 2,947 | +1.26 | +0.04 | + / ~0 |

T-B's floor was the right mechanism with the wrong comparator: measured
against the 12% TP distance it bound on 23–83 trades; the deep-negative
bucket holds 1,305 trades whose funding (−13.3%) swamps their gross (+10.4%),
negative in BOTH eras — the only era-stable-negative funding cut.

**Execution plan.**
1. Declared grid: skip entries with known_prev rate < K for
   K ∈ {−0.05%, −0.1%, −0.2%} per interval; declared secondary: shrink to
   half weight instead of skip.
2. Combination cell (declared): skip only when BOTH rate < K AND the T-D
   mean-reversion forecast (meanrev φ0.5, the tail winner) predicts the
   cumulative 24h funding stays below K×n — this is where T-D's tail skill
   becomes economically testable without the failed Stage-2 framing.
3. Recurrence + era split + salience check per cell; overlap matrix with
   T-E buckets (how much of deep-neg is also stale?).
4. Double-verification arm: same bucket diagnostic on both render books'
   entries (funding panel already covers their symbols on bybit).
5. Also close T-B's open question in parallel: verify Bybit's
   funding-timing semantics (is the next settlement's rate fixed at interval
   start?) from venue documentation/API — it decides whether the era-stable
   "next-rate" floor variant is registrable at all.

## T-H. Expected-net ranker (ML, numpy-native, anti-leak by construction)

**Thesis.** A shallow model over the features V3 already computed per trade
(freshness, momentum shape, funding state, modeled cost, signal score,
settlement cadence) ranks entries by expected net better than any single
cut, and its bottom decile is droppable without losing the gross engine.
The honest question is incremental value: does it beat the simple
T-E ∧ T-G conditioners it partly encodes?

**Execution plan.**
1. Feature set frozen at spec time (all PIT at entry bar close):
   hours_since_high_168h, r_1h, mom_delta, known_rate_prev, funding EWMA
   (hl3), n_intervals_planned_hold, cost_per_unit, score, symbol age in
   days, and NOTHING else. No feature additions after the first fit.
2. Model: ridge regression on winsorized features and rank-transformed
   target (net per unit notional), plus a logistic P(net>0) twin — numpy
   only, no new dependencies. Walk-forward: expanding window, quarterly
   refits, first fit after 2022-06-30; every trade scored strictly
   out-of-fit-window.
3. Declared actions: drop bottom decile of walk-forward score; sizing
   proportional to score quintile (0.25×–1.5×, fixed map). Both compared
   against (i) baseline, (ii) T-E best cell, (iii) T-G best cell,
   (iv) T-E ∧ T-G — the ML thesis survives only if it beats (iv), not just
   baseline.
4. Full recurrence + era split; coefficient stability across refits
   reported (a sign-flipping model is a refutation regardless of net).
5. Double-verification arm: score the render books' entries with the
   frozen model and report decile monotonicity per gate state.

## T-I. Regime intensity instead of the binary gate

**Motivation (from T-A, both arms inspected).** The gate is decisively right
in the late era (maxDD −1.2% vs −6.4%) and costs return in the early era
(+8.5% vs +10.6%). A binary gate is the crudest member of a family; the
right question is whether a continuous conditioning of ENTRY SIZE on BTC
trend/vol keeps the tail protection while reclaiming part of the early-era
sample.

**Execution plan.**
1. Declared family, three members only: (a) binary gate (baseline),
   (b) linear intensity s = clip(btc_30d_return / 10%, 0, 1) scaling entry
   weight, (c) two-sided vol-conditioned intensity (full size in uptrend,
   quarter size in downtrend, half otherwise). No other shapes on this
   surface.
2. Implement at ledger level first on the barebones book using the gate-off
   render's BTC daily trend series (already computed by `_btc_trend_returns`)
   — the barebones ledger is ungated, so intensity applies as per-trade
   weight scaling in the recurrence.
3. If a member beats both T-A arms on MAR with no worse tail-day count, run
   the paired renders for it via the research-flag pattern (extending the
   flag to an intensity parameter, same guards).
4. Tail arm identical to T-A (common-loss dates + 2024-08-06), era split,
   both gate states — this thesis IS the double-verification design applied
   to the gate itself.

---

## Verification harness and sequencing

Run order: T-E → T-G (shared panels, pure post-processing, one day) →
T-F (re-simulation, heavier) → T-H (needs T-E/T-G results as comparators) →
T-I (renders last, heaviest). Every thesis reports: all declared cells, era
split, both-gate-state diagnostics where applicable, salience check, bucket
composition drift, and an explicit exploratory label. Winners get their
config committed and judged exclusively by the forward rolling ledger
(Lane 2) — one row per prototype per UTC day, evidence = the run of days the
commit predates. Promotion remains a separate owner decision through the
normal post-epoch deploy flow.

Explicit non-conclusions, standing: no alpha, robustness, candidate,
deployment, sizing, or real-money claim arises from any spent-surface result
in this program.
