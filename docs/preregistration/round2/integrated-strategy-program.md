# Research plan: Round 2 — integrated-strategy program (MAR-primary)

**Date:** 2026-05-29
**Author:** owner (drafted with assistant)
**Stage:** proposed — master plan for Round 2. Each sub-phase below gets its
own dated pre-registration file before its sweep runs.
**Compute target:** Ryzen 5950X (16C / 32T). No compute rationing. Whole
program expected to take weeks; that's acceptable.
**Optimization objective:** **(Return / Drawdown) tied as primary, Sharpe as
secondary tie-breaker.** See section "Optimization objective" for the
exact decision metric.
**Integrity standard:** `docs/backtesting_errors_we_never_repeat.md` is binding.

---

## TL;DR

Round 1's 7-phase program documented a clean null result across H1-H7.
Three findings carry forward:

1. **The current filter stack contains real load-bearing structure** —
   Round 1 Phase 0 confirmed `crowding`, `event_rank_frac`,
   `turnover_ratio` as decisive falsifiers when removed.
2. **The PIT data contains real univariate cross-venue IC signal** —
   Round 1 Phase 5 identified 5 features with stable cross-sectional
   short-side IC.
3. **The naive combined-signal portfolio architecture does not translate
   that IC into edge** — Round 1 Phase 6 falsified H7.

Round 2 takes the validated parts of BOTH systems (event-driven entries
with the load-bearing filter stack + the surviving IC features as
augmenting signal) and combines them with **Jane-Street-style
infrastructure** (risk-factor decomposition, 1/realized-vol position
sizing, per-name cost model, stress-test suite, capacity analysis).

Round 2 also corrects two Round 1 design errors:
- **Threshold rigidity:** Round 1 used the same strict Manifesto bar at
  every phase. Round 2 uses a **three-tier, demo-arbiter** system —
  Investigation (carry forward) → Demo-candidate (loose backtest gate onto
  the free forward-demo treadmill) → Real-money (strict; OOS + ≥30d demo +
  heavy stats). Permissive where being wrong is free, strict where it costs
  real money. See "Decision framework" below.
- **Sharpe-primary optimization:** Round 1 implicitly optimized for
  Sharpe Δ. Round 2 makes the objective explicit: **MAR ratio
  (annualized return / |max DD|) is primary; Sharpe is secondary
  tie-breaker.**

14 R-phases (R0-R13) + 4 continuous (C0-C3), conditional dependencies,
~weeks of wall-time on the 5950X. No hard deadline. Default outcome remains
"do nothing if all hypotheses falsify." (See "Status & 5950X dispatch" for
what's shipped vs pending.)

**Lead R1 candidate from 2026-05-29 Mac-side exploratory peek:**
`R1_drop_all_4` (drops `day_return`, `stop_pressure`, `realized_loss`,
`rank_max` — the 4 R1 DROP / RE-TEST filters identified from Round 1
Phase 0 LOO data) showed Pareto improvement on BOTH venues over the
full 2023-04-01 → 2026-05-28 window including May 2026 stress:
**Bybit MAR Δ +1.29** (4.92 → 6.21, return +65%, max DD -3.6pp
shallower), **Binance MAR Δ +1.03** (1.45 → 2.48, return +29%, max DD
-9.7pp shallower). Single exploratory run, no sub-period stability,
no R4 residual-Sharpe, no R7 stress, no R11 OOS — **NOT yet promoted**.
R1 elevates this cell to lead-candidate priority and applies the full
Manifesto pipeline. **Re-baseline cascade pre-committed below** (see
"Lead-candidate re-baseline cascade" subsection) — IF the cell clears
every gate, R2-R10 re-baseline against the drop_all_4 stack instead
of production.

**R12 (sniper-entry execution layer)** runs in parallel with R4-R10.
It is NOT optional — operator instruction is to extend the program
with sub-1h *execution* refinement. The signal stays at end-of-day
close (1d resolution); the FILL becomes intraday-optimized. R12
adds 1m kline ingestion for entry windows, a sniper entry simulator
(limit/pullback/volume-spike/TWAP/market-control variants), univariate
sniper tests, and R9 sniper variants. Missed fills are first-class
data (no silent drop). See "Sub-phase R12" for the full breakdown.

**C-phases (continuous-signal architecture)** run in parallel with
R-phases. The current strategy generates signals once per calendar
day (when daily aggregations finalize at UTC close). Round 2 also
tests an alternative where the signal is **continuously re-evaluated**
using WS-fed rolling-window features. The two architectures (daily /
continuous) share R1-R8 infrastructure (filter audit, IC test, risk
model, sizing, cost model, stress test, capacity) but differ in
feature definitions and backtest framework. R9 integrates each
architecture's best cells; R10 demo-candidate + R11 OOS gates evaluate the best
of both. See "Two signal architectures in scope" below and
"Sub-phases C0-C3" for the continuous-signal implementation details.

---

## Status & 5950X dispatch (updated 2026-05-28)

**Shipped to the codebase** — all opt-in; defaults unchanged, so the running R1
sweep and the frozen live profile are byte-identical:
- **R5 sizing** — `--position-weighting risk_equal` (absolute
  `target_vol_per_name / realized_vol`, clamped) + `--target-vol-per-name`.
  Backtest done; live-runner per-name wiring is post-validation.
- **R6 down-payment (E3/E4)** — `CostConfig.exit_cost_multiplier` (exit-leg
  asymmetry) and `maker_fill_probability=0.0` to model the live 100%-taker fill.
  Full per-name/per-bar R6 still pending.
- **E6** — Bybit funding settlements surfaced in `reconcile-demo-bybit` (the
  short's funding tailwind, which closedPnl never showed).
- **R13** — new sub-phase, pre-registered at the end of this doc.

**Phase ledger:**

| Phase | Status |
|---|---|
| R0 doc cleanup | done |
| **R1** filter audit (`max_active=12`) | **RUNNING on the 5950X** |
| R2 per-feature decile sort | code ready (`liquidity_migration/r2_decile_sort.py`); awaiting dispatch |
| R3 bearish-stack honest test | pending (~3h filter-flag code) |
| R4 risk-factor model | pending (~3d code) |
| **R5** 1/realized-vol sizing | **backtest shipped** (`risk_equal`); cells opt in via flag |
| R6 per-name/per-bar cost | **E3/E4 down-payment shipped**; full model pending |
| R7 stress / R8 capacity | pending (depend on R4 / R6) |
| R9 assembly / R10 promote / R11 OOS | pending |
| R12 sniper-entry execution | pending (~3-4d code) |
| **R13** exit-rule re-opt | **ready** — dispatch after R1 confirms drop_all_4 |
| C0-C3 continuous signal | pending (~5-7d code) |

**Dispatch (5950X, 8 cells × 4 polars threads = 32 SMT):**

```
SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4 .venv/bin/python -u scripts/r1_filter_audit_sweep.py
# after R1 confirms drop_all_4 as lead:
SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4 .venv/bin/python -u scripts/r13_exit_rule_sweep.py
```
Per-cell verdicts: `.venv/bin/python scripts/r1_robustness.py --sweep-tag <tag>`.

---

## Codebase note — `event_demo` refactor LANDED (2026-05-28)

`event_demo.py` was split into a module family (`event_demo_{data,entries,
planning,exits,reports}.py`) and `volume_events.py` into
`volume_events_{filters,features,charts,validation}.py`. The `volume-events` CLI
and all flags are unchanged, so every sweep + the signal harness run as-is. The
package `__init__` preloads the hubs, so importing any sibling cold is safe.
Round-1 phase sweep scripts were removed in the 2026-05-28 cleanup;
`scripts/{r1_filter_audit_sweep,r13_exit_rule_sweep,_sweep_runtime,r1_robustness}.py`
are the canonical sweep tooling.

---

## Two signal architectures in scope

Round 1 ran exclusively on the **daily-frequency signal architecture**:
features are defined as calendar-day aggregations (e.g. `liquidity_rank`
= rank of a symbol's total UTC-day quote turnover among universe peers);
the predicate fires at most once per (symbol, day); entries are
mechanically delayed +1h after daily close to avoid same-bar leakage.

Round 2 puts this architecture on **equal footing with a continuous-
signal alternative**:

### Architecture A — Daily (current production, Round 1 inheritance)

- **Feature definition:** calendar-day aggregations. `liquidity_rank` =
  rank of UTC-day total quote turnover. `prior7_liquidity_rank` = mean
  of `liquidity_rank` over the prior 7 calendar days. Etc.
- **Predicate evaluation:** once per UTC daily close, after all bars
  for the day have finalized.
- **Signal frequency:** at most one signal per (symbol, day).
- **Entry delay:** +1h leakage guard (signal_close → entry).
- **Cooldown:** 5 calendar days.
- **WS role:** observation only — knows the daily-close bar the moment
  it finalizes; monitors open positions for SL/TP exits; provides
  low-latency state. Does NOT drive signal generation.

### Architecture B — Continuous (new in Round 2)

- **Feature definition:** rolling-window aggregations.
  `rolling_24h_liquidity_rank` = rank of trailing-24h quote turnover,
  recomputed every K minutes (default K=60 minutes — natural cadence
  match to 1h kline closes).
  `prior_24h_rank_avg_7d` = mean of `rolling_24h_liquidity_rank`
  sampled hourly over the trailing 168 hours. Etc.
- **Predicate evaluation:** every K minutes (default K=60).
- **Signal frequency:** can fire at any K-minute step, not gated by
  daily close. In practice gated by cooldown (see below) — most names
  fire ≤ 1 signal per few days.
- **Entry delay:** can be **smaller or zero**. The continuous feature
  is already a rolling window — there's no "same-bar leakage" risk
  the way there is for daily features. Default proposal: 0h entry
  delay (immediate), with R12 sniper layer still applicable for fill
  improvement.
- **Cooldown:** 120 hours (= 5 days, same total duration, expressed in
  hours).
- **WS role:** signal-driving. KlineStreamManager publishes the 1h
  kline close; continuous-signal engine recomputes rolling features
  for all universe symbols; if the predicate now goes true on a
  cooled-down name, the signal fires immediately.

### Why both, why now

Round 1's WS infrastructure was built (M1-M14) but is currently
under-exploited — only observation, not signal generation. The user
correctly identified that a constantly-observing system shouldn't be
gated by an artificial daily-close clock if the data permits otherwise.

Architecture B is a non-trivial redesign (different features,
different backtest engine, different cooldown semantics) but the
research infrastructure built in R1-R8 applies equally to both. The
marginal cost of testing B alongside A is reasonable; the upside is
a meaningfully different strategy that might Pareto-dominate the
daily version.

### What both architectures share (R1-R8 + R10 + R11)

- **R1 filter audit** — most filters port directly; a few need rolling
  equivalents (e.g. `day_return ≥ 0` → `rolling_24h_return ≥ 0`)
- **R2 per-feature standalone test** — the same 5 IC features apply
  to both architectures (their definition is already rolling-friendly);
  decile sorts can be performed at both daily and continuous frequencies
- **R3 bearish stack** — mirror logic works in both architectures
- **R4 risk-factor model** — factor exposures are computed at entry
  time regardless of when entry happens
- **R5 1/realized-vol sizing** — sizing logic identical
- **R6 cost model** — cost model identical (in fact, sniper-style fill
  flavors are available for both architectures via R12)
- **R7 stress tests** — same named events; each architecture replayed
- **R8 capacity** — same per-name capacity ceiling logic
- **R10 demo-candidate gate** — same Tier 2 bar (MAR-primary, pooled);
  separate cells per architecture
- **R11 OOS gate** — same pre-2023 root test (Tier 3); separate cells per
  architecture

### What's architecture-specific (C-phases)

- **C0** — Continuous-signal architecture spec + rolling-feature
  registry + continuous-evaluation engine
- **C1** — Continuous-signal feature engineering + cross-venue univariate
  IC test (does the rolling-feature predicate fire enough? in the right
  places? consistently across venues?)
- **C2** — Continuous-signal R9 variant (integrated continuous strategy)
- **C3** — Continuous-signal stress test (R7 events replayed against
  the continuous architecture; both architectures may stress
  differently)

See "Sub-phases C0-C3" later in this doc for full details.

### Operator expectation: A vs B is an EMPIRICAL question

We do not pre-commit to which architecture wins. R10/R11 evaluates each
separately under the same three-tier bars. Possible outcomes:

| Outcome | Interpretation | Next step |
|---|---|---|
| A passes, B fails | Daily signal is a Pareto-best for this data | Promote A's best cell; B-architecture is closed for this dataset |
| B passes, A fails | Continuous signal exploits WS infrastructure correctly; daily signal was leaving edge on the table | Promote B's best cell; replace existing strategy class |
| Both pass | Two viable strategy lines; operator decides whether to run both in parallel or pick one based on capacity + ops complexity | Operator decision at promotion time |
| Both fail | Documented null across both architectures | Strategy stays in current state |

The C-phase decision rule is the SAME Investigation/Promotion structure
as R-phases. No special pleading.

---

## Lessons from Round 1 (what changed our priors)

### Confirmed
- **3 filters are load-bearing** (LOO removal destroyed both venues):
  `crowding`, `event_rank_frac`, `turnover_ratio`. Do not casually drop.
- **5 features have stable cross-venue IC at fwd_ret_3d**:
  `vol_of_vol_30d` (|IC|=0.087), `realized_vol_7d` (0.081),
  `dist_from_30d_low` (0.071), `xs_rank_ret_7d` (0.043),
  `xs_rank_ret_3d` (0.039). All negative IC = predict short.
- **Bybit and Binance have venue-specific optima.** Phase 2 showed
  Bybit's best rank-improvement threshold is ~200-300 while Binance's
  is ~100-150. Production default (150) is the joint compromise.
- **Universe widening hurts Sharpe but not DD.** Phase 1 showed 474-only
  has +1.09 Sharpe vs 764-full but DD shift to -42% is unexplained.

### Surprised
- **Combined-signal portfolio did NOT beat event-driven.** Even with 5
  surviving features, every Phase 6 cell underperformed baseline on
  Sharpe (best: -1.90 Sharpe Δ on Bybit). The discrete-event-driven
  architecture beats a naive continuous-rank portfolio on this data.
- **Deterioration direction was structurally untestable.** Quality-
  positive filters (day_return ≥ 0, residual ≥ 0.08, close_location
  ≥ 0.30) exclude bearish names by construction. H2/H3
  falsified-by-construction, not falsified by evidence.

### What we still don't know
- **Where the -22% → -42% DD shift came from.** H1 falsified, so not
  universe. Remaining candidates: u32 bug-fix removed real signal, April
  2025+ regime change, code drift in some other component. **R7 stress
  test phase will partially answer this.**
- **Whether a properly-implemented combined portfolio beats event-driven.**
  Phase 6's implementation had a known exposure-inflation caveat. R9
  integrated strategy will test the corrected version.
- **Whether the inverse-direction edge actually exists.** H2 needs a
  bearish-tuned filter stack to test honestly. **R3 will do this.**

---

## Optimization objective

**Primary metric: MAR ratio = annualized_return / |max_drawdown|.**

Annualization formula (geometric, standard):
`annualized_return = (1 + total_return) ** (365.25 / window_days) - 1`.

Equivalent names: Calmar ratio, return-over-DD. Worked example using the
actual Phase 0 / R1 baseline that R-phases inherit (window 2023-04-01 →
2026-04-30 = 1125 days = 37 months = 3.08 years; promoted profile run on
`bybit_full_pit` and `binance_full_pit`):

| Venue | Total return | Period | Annualized | Max DD | MAR |
|---|--:|---|--:|--:|--:|
| Bybit | +3856% (+38.56×) | 1125 d (37 m / 3.08 y) | +230.0%/yr | -42.11% | **+5.46** |
| Binance | +421% (+4.21×) | 1125 d (37 m / 3.08 y) | +70.9%/yr | -42.20% | **+1.68** |

> *Correction note (2026-05-28, second iteration):* The original draft of
> this table cited Bybit total = +518.76% / 17 m / +231.5%/yr / MAR +5.50
> and Binance total = +66.12% / 17 m / +45.4%/yr / MAR +1.11. That mixed
> two different sweeps: the **total_return values** were transcribed from
> the 2026-05-28 cost-tweak sweep (different baseline config, baseline
> Bybit return +518.76% / +5.1876× over an ~18-month window), while the
> **annualized / MAR values for Bybit** happen to match the Phase 0 / R1
> baseline at 1125 days within rounding (+231.5%/yr vs the real +230.0%/yr).
> Binance was wrong on both axes.
>
> An intermediate fix (commit `3e86b69`) recomputed the table as if the
> Phase 0 baseline were `+518.76%` at 1125 days, producing MAR +1.92 /
> +0.44 — internally consistent but wrong about the underlying baseline.
> The numbers above (commit landing this sentence) reproduce R1's
> `R1_baseline_v2` cell exactly (which in turn reproduces Phase 0's
> `00_baseline` bit-identically). Verified by `compute_mar` /
> `compute_annualized_return` in `scripts/apply_decision_rule.py` and
> tests `test_compute_mar_round1_baseline_actual_window` /
> `test_compute_annualized_return_round1_baseline_actual_window`.
>
> The decision thresholds below (Investigation: MAR Δ > 0 majority
> venues; Promotion: MAR Δ ≥ +0.5 both venues) are deltas-vs-control
> and are NOT changed by this correction. Only operator intuition about
> the baseline anchor moves.

Why MAR over Sharpe as primary:

1. **Sharpe is risk-adjusted return where "risk" = volatility.** That
   penalizes upside volatility too. Operators care about DD, not σ.
2. **MAR directly answers "how much do I make vs how much can I lose."**
   That's the operator's actual question.
3. **MAR is robust to fat tails.** Sharpe assumes Gaussian. Crypto isn't.
4. **MAR is leverage-invariant the same way Sharpe is** (both numerator
   and denominator scale with leverage), so it's a fair comparison metric.

Sharpe remains a **secondary tie-breaker** for two Pareto-equivalent
cells with the same MAR. Sharpe also remains the falsifier bound (a cell
with negative Sharpe is rejected regardless of MAR — because negative
Sharpe means the return doesn't cover the volatility cost of taking it).

### Pareto requirement on (return, drawdown)

A cell is at least as good as the control if **NEITHER** of these is
worse:
- annualized return < control's annualized return
- |max DD| > control's |max DD|

I.e., to improve on control we need EITHER higher return at same-or-
lower DD, OR lower DD at same-or-higher return. **MAR captures both
sides of this constraint.** A cell that boosts return by 50% while
doubling DD has lower MAR than the control — correctly rejected as a
"more leverage in disguise" trade.

---

## Decision framework — three-tier (demo-arbiter)

The program advances findings through three gates, ordered by how expensive a
false positive is at each:

1. **Investigation** — should we keep studying this cell? (cheap: just attention)
2. **Demo-candidate** — should this go on the forward-demo treadmill? (cheap:
   demo is paper, costs nothing)
3. **Real-money** — should real capital go behind it? (expensive: real money)

Governing principle: **be permissive where being wrong is free, strict where
it is expensive.** The heavy statistics live at the Real-money gate; the
backtest→demo gate is deliberately loose so findings flow. **The forward demo
is the real arbiter** — the one evidence surface a backtest cannot overfit,
and it is inexhaustible.

```text
backtest sweeps
  → [Tier 1: Investigation]  carry forward / descriptive / falsified
  → [Tier 2: Demo-candidate] forward-demo + paper-shadow treadmill, queue for OOS
  → [Tier 3: Real-money]     OOS pass + ≥30d demo + heavy stats → mainnet
```

> **Changelog.** Through 2026-05-28 this section was a two-tier "Strictness
> Manifesto v2" (Investigation + a single strict Promotion bar forwarding
> straight to OOS). Restructured to the three-tier model below on 2026-05-28
> by operator instruction, **on principle, not to rescue a specific cell** (the
> new bar is re-applied blind): the symmetric "+0.5 MAR on both venues"
> rejected genuine venue-asymmetric edge — Round 1 established Bybit/Binance
> have different optima — and four overlapping fragility tests (Pareto-both +
> sign-consistent-3-thirds + residual-Sharpe + bootstrap/LOO) stacked into a
> bar almost nothing could clear. The Real-money gate (Tier 3) is NOT loosened.

### Tier 1 — Investigation (sub-phases R1-R8)

A cell is **investigation-positive** if **ALL** of:
- MAR Δ > 0 on majority of venues (2/2 OR 1/2 with the other not worse than
  -0.5 MAR)
- No return sign-flip vs control (both venues remain same-signed)
- ≥30 trades on Bybit (≥20 on Binance) if a trade-based cell

Falsifies (decisive close) if **ANY**: MAR Δ ≤ -1.0 either venue; return goes
negative on a venue that was positive in control; DD > 70% either venue;
trade count < 10 / sub-period either venue.

Cells neither investigation-positive nor falsified are **descriptive** —
recorded for context, not carried forward.

### Tier 2 — Demo-candidate (→ forward demo + queue for R11 OOS)

The loosened backtest gate. A cell is **demo-eligible** if **ALL** of:
- **Return positive on BOTH venues** — direction consistency, the cheap and
  genuine overfit guard. A one-venue fluke that is negative on the other does
  NOT advance.
- **Pooled MAR Δ > +0.1** — pooled = equal-weight mean of the two venue MAR
  deltas. Replaces the old symmetric per-venue +0.5, so genuine
  venue-asymmetric edge advances.
- **Neither venue worse than MAR Δ ≥ -0.5** — don't demo something actively
  harmful on a venue.
- **≥30 Bybit / ≥20 Binance trades total** — sample-size sanity.

That is the whole gate. Fragility diagnostics — block-bootstrap pooled MAR-Δ
p5, leave-one-month-out concentration, sub-period sign-consistency, residual
Sharpe — are **REPORTED for every demo-candidate but do NOT block.** They set
the *order* candidates go on the treadmill (most robust first), not whether
they go. Computed by `scripts/r1_robustness.py`.

Demo-falsifies if **ANY**: return negative on either venue; pooled MAR Δ ≤ 0;
DD > 70% either venue. (A cell between 0 and +0.1 pooled MAR Δ is descriptive
— neither demo-eligible nor falsified.)

### Tier 3 — Real-money (demo → mainnet) — STRICT

Where being wrong is expensive, so the heavy checks live here. A cell is
**real-money-eligible** only if **ALL** of:
- **R11 pre-2023 OOS pass:** MAR > 0 both venues all 3 sub-periods; DD < 50%
  both venues all sub-periods; sign-consistent; ≥20 Bybit / ≥15 Binance
  trades / sub-period.
- **≥30 days forward demo** with daily paper-shadow reconciliation (operator
  may require 60-90d for higher-conviction sizing).
- **Block-bootstrap pooled MAR-Δ p5 ≥ 0** (seed = 0, block = 3 months,
  n = 5000) — the one fragility gate that matters, applied where it counts.
- **Residual Sharpe ≥ +0.3** after the R4 factor model (not just selling vol
  / buying beta).
- **R7 stress pass** (DD < 50% in every named event) + **R8 capacity ceiling
  ≥ 10× intended deployment size**.

No mainnet without all of the above.

### Falsifier (decisive close, all tiers)

A cell that hits a falsifier at any tier is **closed-rejected**. It cannot be
resurrected without a new dated pre-reg with explicit new motivation.
Falsifier hits are first-class evidence.

### Multiple-testing control — the demo treadmill, not an FDR cap

With the forward demo as arbiter, the **demo treadmill itself is the
multiple-testing control:** every demo-candidate must independently re-prove
itself on fresh, un-overfittable forward data, which no amount of backtest
multiple-testing can fake. A false positive just produces a flat/negative
forward curve and is dropped — cheaply. Parallel candidates run as
paper-shadow forward configs (simulation on live data, no orders); promotion
to the live demo account is a separate operator step.

The one finite surface that CAN be burned is the pre-2023 OOS root (threat
#18). So the only hard cap lives there: **max 5 cells may consume the pre-2023
OOS per calendar quarter**; excess demo-candidates run forward first and wait
their turn at OOS. Forward demo/paper is uncapped.

---

## Sub-phase R0 — Round 1 doc cleanup (immediate)

**Purpose:** Reduce navigation noise so future sessions don't have to
wade through superseded scaffolding.

**Actions:**
1. **Delete** `docs/preregistration/2026-05-27-phase7-pre2023-oos-gate.md` —
   never ran (no Round 1 finalists). The pre-reg has no executed
   evidence to preserve; the parent plan's Phase 7 section captures the
   design.
2. **Keep** all Round 1 verdict docs (`phase0/1/2/5/6-verdict.md`) +
   `program-verdict.md` + the parent plan — these are HISTORICAL
   EVIDENCE per the integrity standard.
3. **Keep** Round 1 sub-phase pre-regs (`phase0/1/2/5/6-*.md`) — these
   document what was promised before the run, which is part of the
   evidence chain.
4. **Update** `STATE.md` to point at Round 2 as the active program.
5. **Update** `docs/research_findings.md` headline to note Round 1
   complete, Round 2 starting.

**Compute:** zero. Pure docs work.

**Output:** one doc-cleanup commit, separate from Round 2 substantive work.

---

## Sub-phase R1 — Per-filter hypothesis audit

**Purpose:** For every filter in the production stack, state the
*economic mechanism* it claims to capture and decide its fate using
Round 1's Phase 0 LOO evidence under the looser "any filter that helps
Sharpe even by a little bit doesn't have to cross the strict threshold"
criterion.

### Method

For each filter, populate this row:

| Filter | Hypothesis | LOO Sharpe Δ (by/bn) | LOO DD Δ (by/bn) | Decision | Reason |
|---|---|---|---|---|---|

Decisions:
- **KEEP** — removal hurt either venue meaningfully (operator's softer
  threshold: any negative Sharpe Δ on either venue OR DD widening ≥3pp
  on either)
- **DROP** — removal didn't hurt OR helped on both venues
- **RE-TEST** — LOO degenerate or evidence mixed; needs a non-LOO test
  before deciding

### Round 1 Phase 0 evidence applied

| Filter | Hypothesis (why we'd expect it to help) | by Sh Δ | bn Sh Δ | by DD Δ | bn DD Δ | Decision |
|---|---|--:|--:|--:|--:|---|
| `crowding` (union_pathology) | Detects late/stalled entries where cohort already crowded; pathology indicators sum to "this entry is statistically late" | -0.61 | -0.25 | -1.4pp | +6.6pp | **KEEP** (falsifier) |
| `event_rank_frac` (≤0.90) | Caps event-of-day rank; top-10% scorers already arb'd by obvious traders; stay in unobvious window | -1.37 | -0.79 | +16.2pp | +26.5pp | **KEEP** (falsifier) |
| `turnover_ratio` (≥6.0) | Today's turnover ≥6× prior 7d mean; ensures signal day is genuine outlier-volume, not quiet drift | -1.33 | -0.64 | +27.1pp | +11.3pp | **KEEP** (falsifier) |
| `entry_delay` (1h) | 1h delay from signal close to entry; prevents same-bar leakage + matches realistic execution | -0.11 | -0.31 | +6.7pp | +16.1pp | **KEEP** |
| `cooldown` (5d) | 5d between trades on same symbol; prevents over-concentration on multi-pumping name | -0.32 | +0.01 | +1.0pp | -2.8pp | **KEEP** (Bybit benefit) |
| `rank_min` (31) | Skip top-30 by liquidity (BTC/ETH/SOL too obvious / too well-arb'd) | -0.42 | -0.13 | +0.5pp | -0.7pp | **KEEP** |
| `residual_return` (≥0.08) | Signal-day return net of market ≥+8%; ensures move is idiosyncratic, not beta | -0.26 | +0.07 | -1.0pp | -6.1pp | **KEEP** (Bybit benefit) |
| `close_location` (≥0.30) | Close in top 70% of intraday range; ensures signal day closes strong, not on the low | +0.04 | -0.32 | -5.0pp | +12.6pp | **KEEP** (Binance benefit) |
| `pit_age` (≥90d) | Symbol listed ≥90d; protects against new-listing pump dynamics that don't generalize | +0.04 | -0.16 | -3.7pp | +5.1pp | **KEEP** (Binance benefit) |
| `rank_max` (≤400) | Skip bottom-of-liquidity tail (low-cap names too friction-bound) | +0.11 | +0.08 | -4.9pp | -1.6pp | **RE-TEST** (LOO suggests filter hurts both venues mildly) |
| `realized_loss` (≥6 stops/5d) | Stop firing on names with too many recent losses; basket risk-off when peers are stopping out | +0.10 | +0.00 | -1.2pp | +0.0pp | **RE-TEST** (Bybit benefit on removal; Binance no-op) |
| `day_return` (≥0) | Signal day must be positive return; ensures we're not buying the dip | +0.02 | +0.03 | +0.0pp | +0.0pp | **DROP** (no-op both venues; Occam) |
| `stop_pressure` (≥7 stops/10d) | Stop firing on basket-level stress | -0.03 | +0.05 | +0.9pp | -4.1pp | **DROP** (no-op both venues; Occam) |
| `max_active` (3) | Position cap — at most 3 concurrent trades | (degenerate) | (degenerate) | (degenerate) | (degenerate) | **KEEP** (LOO degenerate; production value untested but mechanism essential) |

### Decisions

**KEEP without further testing (10 filters):** crowding,
event_rank_frac, turnover_ratio, entry_delay, cooldown, rank_min,
residual_return, close_location, pit_age, max_active.

**RE-TEST individually before drop (2 filters):** rank_max,
realized_loss. Each gets a single-cell test where the filter is removed
WITH the rest of the stack intact, run on full window + 3 sub-periods,
both venues. Investigation-bar threshold applies.

**DROP without further test (2 filters):** day_return, stop_pressure.
LOO Δ within ±0.05 Sharpe on both venues = genuinely no-op. Each gets
a separate test only if anyone challenges the drop with a hypothesis.

### Hypothesis testing for the DROP candidates

For day_return and stop_pressure (the planned drops), R1 includes a
single "remove these two" cell to verify the joint drop doesn't
surface a missing interaction effect. Investigation bar.

### Cell list (R1)

| Cell | Description | Priority |
|---|---|---|
| `R1_baseline_v2` | Production filter stack as-is (control) | required |
| **`R1_drop_all_4`** | **Production minus `day_return` + `stop_pressure` + `realized_loss` + `rank_max` — the LEAD CANDIDATE per 2026-05-29 Mac exploratory** | **highest, dispatch first** |
| `R1_drop_day_return` | Production minus `day_return` | normal |
| `R1_drop_stop_pressure` | Production minus `stop_pressure` | normal |
| `R1_drop_both_noops` | Production minus both `day_return` and `stop_pressure` | normal |
| `R1_retest_rank_max` | Production minus `rank_max` (re-confirms Phase 0 finding under longer window) | normal |
| `R1_retest_realized_loss` | Production minus `realized_loss` | normal |

7 cells × 2 venues = 14 runs. Window 2023-04-01 → 2026-05-28 (extended to
match the lead-candidate exploratory window). Tier 2 Demo-candidate bar for
all cells. Compute: ~14 × 8 min = ~112 min sequential, ~35 min at 4-way
parallel (longer at max_active=12 — more trades per run).

#### AMENDMENT 2026-05-28 — wide funnel: max_active 3 → 12

R1 now runs at **`max_active_symbols = 12`** (not the production 3). Rationale
(operator decision, pre-registered before the run):

- **Gather a large trade dataset.** At 3 slots, ~25% of qualifying signals are
  turned away for capacity and the result swings on which 3 names win slots in
  a given month (the small-sample fragility behind `drop_all_4`'s 3-month-
  dependent edge). 12 slots ≈ ~4× the trades → a far more reliable read on the
  real edge.
- **Then filter down with the features.** The per-trade ledger already records
  all IC-feature values at entry (`vol_of_vol_30d`, `realized_vol_7d`,
  `dist_from_30d_low`, `xs_rank_ret_7d/3d`, …). The wide-funnel ledger is the
  raw material for a post-hoc study: which feature thresholds select the
  winning subset? (The R2/R9 feature-selection work, now fed by a richer pool.)
- **Risk is still bounded — by gross exposure, not by the count.**
  `gross_exposure` stays 1.0, so each of 12 names is ~8% of equity (same total
  bet, thinner slices). The systematic/beta exposure (12 correlated alt-shorts)
  is governed by gross + the **R4 factor caps**, and the thinner per-name slices
  make **R5 1/realized-vol sizing** more load-bearing.
- **Comparability caveat:** the control (`R1_baseline_v2` / `00_baseline`) also
  runs at 12 slots, so all cells compare at 12. These numbers are NOT directly
  comparable to the max_active=3 exploratory `drop_all_4` numbers in the table
  below.

Dispatch: desktop **5950X** (not the Mac), tag `r1_filter_audit_max12_2026-05-28`,
via `scripts/r1_filter_audit_sweep.py` (already set to 12). Verdict + fragility
via `scripts/r1_robustness.py --sweep-tag r1_filter_audit_max12_2026-05-28`.

### Lead-candidate priority (`R1_drop_all_4`)

The 2026-05-29 Mac exploratory showed `R1_drop_all_4` Pareto-improving
on BOTH venues over the extended window:

| Venue | MAR (baseline → cell) | Δ Return | Δ Max DD | Δ Sharpe |
|---|---|--:|--:|--:|
| Bybit | 4.92 → 6.21 (Δ +1.29) | +65% | -3.6pp shallower | +0.25 |
| Binance | 1.45 → 2.48 (Δ +1.03) | +29% | -9.7pp shallower | +0.13 |

That run is **single-sample exploratory** — no sub-period check, no
R4 residual-Sharpe, no R7 stress, no R11 OOS. Round 2 R1 runs it through
the proper pipeline. Specifically:

- Dispatched FIRST in R1
- Tested on full extended window + 3 sub-period thirds (2023-Q3+2024-Q1
  / 2024-Q2+2025-Q1 / 2025-Q2+2026-Q2 split, or equivalent)
- **Tier 2 Demo-candidate bar** applied (return positive both venues +
  pooled MAR Δ > +0.1 + neither venue worse than -0.5 + trade minimums)
- Fragility diagnostics — bootstrap pooled MAR-Δ p5, leave-one-month-out,
  sub-period sign-consistency, residual Sharpe (if R4 ready) — computed and
  **recorded in the verdict for context**, but per the framework they do NOT
  gate Tier 2; the heavy stats gate at Tier 3 (real money) only

If `R1_drop_all_4` clears the Tier 2 bar, it becomes the active
**re-baseline candidate** for R2-R10 (see "Lead-candidate re-baseline
cascade" below).

### Output

`docs/preregistration/<DATE>-r1-per-filter-audit-verdict.md` with the
final filter-stack decision and the `R1_drop_all_4` verdict against the
Tier 2 Demo-candidate bar (with fragility diagnostics recorded). If it
clears, the re-baseline cascade triggers automatically per pre-commitment.

### Lead-candidate re-baseline cascade (PRE-COMMITTED)

This cascade is committed in writing NOW so it cannot be litigated
after seeing results.

**Trigger:** `R1_drop_all_4` clears the **Tier 2 Demo-candidate bar** at R1.
Re-baselining is a research-comparison choice (what subsequent cells compare
against), not a real-money action, so it triggers on the loose backtest→demo
bar — NOT the strict Tier 3 gate:
- Return positive on **both** venues vs `R1_baseline_v2` (control)
- Pooled MAR Δ > +0.1 (mean of the two venue MAR deltas)
- Neither venue worse than MAR Δ ≥ -0.5
- ≥30 Bybit / ≥20 Binance trades total

Fragility diagnostics (bootstrap pooled MAR-Δ p5, leave-one-month-out,
sub-period sign-consistency, residual Sharpe) are computed and recorded in
the R1 verdict for context, but do NOT gate the cascade.

**If triggered:**
- R2, R3, R5, R6, R7, R8, R9, R10, R11 cells RE-BASELINE against the
  drop_all_4 stack (i.e. they compare to drop_all_4, not to production)
- The three-tier thresholds STAY IDENTICAL (they are deltas; they hold
  against any baseline)
- Cell tables in R2-R10 stay identical (the variations tested are
  unchanged; only the comparison reference shifts)
- The **Tier 3 real-money gate is still fully required** before any
  production change OR mainnet (OOS + ≥30d demo + bootstrap + residual
  Sharpe + stress + capacity)

**If NOT triggered** (drop_all_4 fails the Tier 2 Demo-candidate bar):
- Production filter stack remains the baseline for all subsequent
  R-phases
- `R1_drop_all_4` is filed as "did not clear the Demo-candidate bar in
  proper R1 — Round 1 LOO directional signal stands but does not justify
  a re-baseline"
- Round 2 continues as originally designed

**Critical constraint:** the re-baseline does NOT itself constitute
promotion to production demo / mainnet. The drop_all_4 stack still
must clear R7 stress + R11 OOS + 30-day forward demo with paper-shadow
reconciliation before any live deployment. The re-baseline only means
"R2-R10 compare future candidate cells against the drop_all_4 stack
instead of production, because drop_all_4 has cleared the R1 bar."

---

## Sub-phase R2 — Per-feature standalone test + correlation matrix

**Purpose:** Round 1 Phase 5 surfaced 5 features with cross-venue IC.
Phase 6 jumped to combination before measuring standalone P&L or
feature correlations. R2 does the missing work.

### Method

For each of 5 surviving features, run a **daily decile-sort backtest**:

1. Each day, rank the eligible universe by feature value
2. Short the top decile, long the bottom decile (or short-only top
   decile, since strategy is short-side)
3. Hold for N days (test N ∈ {1, 3, 7})
4. Size 1/realized_vol_7d per name (anticipating R5)
5. Apply per-name per-bar cost model (anticipating R6); for R2 use
   cost_multiplier 3 as legacy compatibility
6. Compute MAR, Sharpe, DD, decile-spread P&L time series

Investigation-bar threshold applies.

### Features and their hypotheses

| Feature | Mechanism hypothesis | Literature anchor | Expected MAR |
|---|---|---|---|
| `vol_of_vol_30d` | Vol-of-vol = regime instability; high vov names are mid-pump-cycle, vulnerable to comedown | Tail-risk premium (equities); GARCH state | Modest positive |
| `realized_vol_7d` | "Low-vol anomaly" — high recent vol = overreaction state, mean-reversion premium for shorting | Frazzini-Pedersen 2014 (equities); replicated in crypto | Modest positive |
| `dist_from_30d_low` | Extended from base = overbought; short-horizon mean reversion dominates | Inverse of 52-week-high effect (George-Hwang 2004 long version) at shorter window | Modest positive |
| `xs_rank_ret_7d` | Short-horizon momentum reversal — names that pumped over last 7d revert | Jegadeesh 1990 (equities); same pattern in crypto | Modest positive |
| `xs_rank_ret_3d` | Same as 7d but shorter window — even faster mean reversion | Same literature | Modest positive (likely correlated with 7d) |

### Correlation matrix

After R2 standalone tests complete, compute 5×5 Spearman correlation
matrix on the daily decile-spread P&L. **Strong hypothesis:** the 5
features collapse to ~2 orthogonal factors:

- **Factor A: "vol/extension state"** (vol_of_vol_30d + realized_vol_7d
  + dist_from_30d_low)
- **Factor B: "short-horizon momentum reversal"** (xs_rank_ret_3d +
  xs_rank_ret_7d)

If the correlation matrix confirms this clustering (intra-cluster ρ ≥
0.4, inter-cluster ρ ≤ 0.2), we use the factor structure in R9
combination. If not, we use all 5 features but weight by IC × (1/avg
intra-feature corr) — diversification-adjusted.

### Cell list (R2)

5 features × 3 horizons × 2 venues = 30 standalone-decile cells, plus 1
correlation matrix computation × 2 venues.

Window: 2021-01-01 → 2026-04-30 (full data root, longest available).
Sub-periods: 3 thirds for stability check.

Investigation bar applies per-cell. The **per-feature standalone P&L
findings are descriptive only** — no individual feature graduates to
Promotion alone. The output feeds R9 integration.

Compute: ~30 cells × ~5 min (signal_harness is fast) = ~150 min wall.

### Output

`docs/preregistration/<DATE>-r2-per-feature-standalone-verdict.md` with:
- Per-feature decile-spread P&L, MAR, Sharpe, DD per horizon per venue
- 5×5 correlation matrix per venue
- PCA decomposition reporting how much variance the top-2 components
  explain (target: ≥80%)
- Feature-group recommendation for R9 (likely 2-factor or 5-equal-weight)

---

## Sub-phase R3 — Bearish stack honest test (H2 retried)

**Purpose:** Round 1 Phase 2 found H2 falsified-by-construction —
deterioration direction produces 0 trades because the existing
quality-positive filters exclude bearish names. R3 tests the bearish
hypothesis honestly: with appropriate mirror-imaged filters.

### Method

Construct a "bearish filter stack" by mirror-imaging the quality gates:

| Filter | Bullish (current) | Bearish (mirror) |
|---|---|---|
| `liquidity_migration_day_return_min` | ≥ 0.0 | ≤ 0.0 |
| `liquidity_migration_residual_return_min` | ≥ 0.08 | (new: residual ≤ -0.08) |
| `liquidity_migration_close_location_min` | ≥ 0.30 | (new: close_location ≤ 0.70) |
| `liquidity_migration_rank_direction` | improvement | **deterioration** |
| `liquidity_migration_rank_improvement_min` | 150 | 150 (absolute magnitude) |
| `crowding_filter` | union_pathology | same (R1 confirmed load-bearing) |
| `turnover_ratio_min` | 6.0 | same (R1 confirmed load-bearing) |
| `event_rank_frac_max` | 0.90 | same (R1 confirmed load-bearing) |
| `entry_delay_hours` | 1 | same |
| `cooldown_days` | 5 | same |
| `universe_rank` | 31..400 | same |
| `pit_age_days_min` | 90 | same |
| `max_active_symbols` | 3 | 3 |
| `stop_loss_pct` | 0.12 (long-tail risk) | 0.12 (same; shorts have unbounded upside) |
| `take_profit_pct` | 0.26 | (TBD: maybe none, since bearish continuation has no symmetric target) |

### Code changes required

- `liquidity_migration` CLI: add `--liquidity-migration-residual-return-max`
  (matching the new bearish minimum direction). The existing `*-min`
  flag stays for the bullish stack.
- Similarly `--liquidity-migration-close-location-max`.
- `volume_events_cell.sh`: add `--mirror-quality-filters` shorthand that
  flips the three quality filters' direction.

Estimated effort: ~3 hours including tests.

### Cell list (R3)

3 cells × 2 venues = 6 runs:

| Cell | Description |
|---|---|
| `R3_baseline_v2` | Production filter stack (bullish), as control |
| `R3_bearish_only` | Mirror-image filter stack, deterioration direction |
| `R3_market_neutral` | Both stacks running in parallel with separate slot pools (3 long-side improvement entries + 3 short-side deterioration entries; balanced basket) |

Investigation bar. Window: 2021-01-01 → 2026-04-30.

**Note on R3_market_neutral:** if both legs investigation-positive,
this is the most interesting cell — it's a market-neutral version of
the strategy. If it Promotion-eligible at R10, that's a meaningful
strategy-class improvement.

Compute: ~6 cells × ~10 min = ~60 min wall.

### Output

`docs/preregistration/<DATE>-r3-bearish-stack-verdict.md`.

If R3_bearish_only investigation-positive: a parallel "bearish" line
opens that gets its own R9 integration + R10 promotion test.

If R3_market_neutral investigation-positive: this is the lead candidate
for R9.

If both R3 cells investigation-negative: H2 is decisively closed even
under appropriate filters. The deterioration direction does not carry
short-side edge in this data. Bug-driven trades were genuinely
capture-by-accident, not edge.

---

## Sub-phase R4 — Risk-factor model construction (Jane-Street-style)

**Purpose:** Round 1 results are measured against $0 — no factor model
strips known systematic risk premia. R4 builds a 5-8 factor model for
crypto perp returns so every Round 2 strategy can be evaluated on
**residual alpha** (the part not explained by exposure to known factors).

### Proposed factors

1. **BTC beta** — regression of name's daily returns on BTC's daily
   returns over rolling 60d window. Captures market exposure.
2. **Cross-sectional 3d momentum** — rank within universe of trailing
   3-day returns. Captures short-horizon trend factor.
3. **Cross-sectional 30d momentum** — same at longer horizon. Captures
   longer-horizon trend.
4. **Realized vol regime** — annualized 7d vol, ranked cross-sectionally.
   Captures vol-tier exposure.
5. **Funding rate exposure** — current funding rate Z-score
   cross-sectionally. Captures carry-tilt.
6. **Liquidity tier** — log(7d ADV), ranked cross-sectionally.
   Captures small-cap risk premium.
7. **Alt-season factor** — equal-weight return of top 20 alts vs BTC.
   Captures alt-rotation regime.
8. **Mark-index premium** — current mark-index spread Z-score. Captures
   positioning intensity.

### Method

For each (date, symbol) in the PIT panel, compute the 8 factor
exposures using only data available at decision_ts. Then per-day,
cross-sectionally regress that day's realized returns on the 8 factor
loadings (controlling for the residual). Output:

- Factor return time series (8 × ~1,500 days for Bybit, ~1,800 for Binance)
- Factor loadings per (date, symbol) — for residual-return computation
- Residual return time series per (date, symbol) — the "after-factor" return

### Validation

The model is valid if:
- Each factor's daily return Sharpe > 0 (factor is real)
- |Per-factor avg correlation with realized vol| < 0.3 (factors are not
  proxies for each other)
- Residual return cross-section has mean ~0 and std smaller than raw
  return std (factor model captures meaningful variance)

If validation fails, drop the underperforming factors and iterate.
Target: 5-6 stable factors per venue.

### Strategy residualization

Once the factor model exists, every Round 2 strategy cell's P&L can be
**decomposed**:

```
Strategy P&L = sum over trades of:
  (factor_exposure_at_entry · factor_returns_during_hold)  ← explained
  + residual_return_during_hold                            ← unexplained = candidate alpha
```

A cell's **residual Sharpe** is the Sharpe of its residual returns.
It is REPORTED at Tier 2 (demo-candidate) for context; at the **Tier 3
real-money gate** it is a hard requirement (≥ +0.3) — otherwise
the cell is "selling vol" or "buying beta" rather than carrying real
alpha.

### Code changes

New module `liquidity_migration/risk_model.py`:

```python
def build_factor_panel(data_root, *, start, end) -> pl.DataFrame
def fit_factor_returns(factor_panel) -> pl.DataFrame
def compute_residual_returns(factor_panel, factor_returns) -> pl.DataFrame
def decompose_strategy_pnl(trade_ledger, factor_returns, factor_loadings) -> dict
```

CLI: `risk-model {build-panel, fit-returns, residualize-trades}`.

Effort: ~3 days of code + tests. This is the largest single addition
in Round 2 by code volume.

### Output

`docs/preregistration/<DATE>-r4-risk-model-verdict.md`. Factor
selection finalized; integration spec for R9.

---

## Sub-phase R5 — 1/realized-vol position sizing

**Status (2026-05-28): backtest SHIPPED.** Implemented as a new opt-in
`position_weighting="risk_equal"` mode (reusing the existing
`--position-weighting` enum rather than adding a separate `--position-sizing`
flag) + `--target-vol-per-name`. `equal` stays default. Live-runner per-name
wiring (see "Code changes") is post-validation — the live runner still sizes
equal-weight (documented at `target_order_notional_pct_equity`).

**Purpose:** Replace dollar-equal sizing with risk-equal sizing per
name. JS-style table stakes; typically shrinks DD by 20-30% without
changing the strategy.

### Method

For each cell that fires entries, compute:

```python
position_size_usd = (gross_exposure × equity)
                    × (target_vol_per_name / realized_vol_7d_for_name)
                    / max(1, max_active_symbols)
```

Where:
- `target_vol_per_name` is a config knob (e.g. 1.5% daily vol per name).
- `realized_vol_7d_for_name` is the annualized 7d vol of the name at
  signal close.
- Sum-of-positions cap remains `gross_exposure × equity`.

This makes a 100% vol name take half the position of a 50% vol name —
both contributing equal risk dollars, not equal position dollars.

### Validation

Re-run the R1 baseline_v2 cell with 1/realized-vol sizing and compare:
- MAR should improve (DD shrinks, return roughly stable)
- Sharpe should improve (volatility of P&L drops)
- Trade count, win rate unchanged (sizing change doesn't affect entry
  decisions)
- Max single-trade contribution to DD should drop

If MAR doesn't improve OR Sharpe degrades materially, the sizing is
wrong (calibration of target_vol_per_name needs tuning). Investigation
bar applies.

### Code changes

Modify the `event_demo` runner (post-refactor: the relevant
`event_demo_{entries,planning}.py` module — verify at implementation; see the
"Codebase note") and the backtest equivalents to support `--position-sizing
{dollar_equal, risk_equal}` and a new `--target-vol-per-name` knob. Default
stays `dollar_equal` for backward compatibility; cells opt in via flag.

Effort: ~1 day code + tests.

### Cell list (R5)

| Cell | Description |
|---|---|
| `R5_baseline_dollar_equal` | Production sizing (control) |
| `R5_risk_equal_1pct` | 1/realized-vol, target vol = 1% daily/name |
| `R5_risk_equal_1.5pct` | 1/realized-vol, target vol = 1.5% daily/name |
| `R5_risk_equal_2pct` | 1/realized-vol, target vol = 2% daily/name |

4 cells × 2 venues = 8 runs. Investigation bar.

### Output

`docs/preregistration/<DATE>-r5-position-sizing-verdict.md`. The
winning target_vol value pins R9's sizing knob.

---

## Sub-phase R6 — Per-name, per-bar cost model

**Status (2026-05-28): E3/E4 down-payment SHIPPED** (not the full model). Two
opt-in `CostConfig` knobs land ahead of the regression-calibrated surface:
`exit_cost_multiplier` (per-leg asymmetry — the cover leg of a short costs more,
default 1.0 = symmetric) and the existing `maker_fill_probability` (set to 0.0
to model the deployed 100%-taker Market execution exactly = 15 bps round-trip,
instead of leaning on `cost_multiplier=3` to paper over a maker blend the live
engine never gets). The full per-name/per-bar model below is still pending.

**Purpose:** Replace the single `cost_multiplier=3` with a model that
varies cost by name (liquidity tier), size relative to ADV, time of
day, and hold-period funding.

### Method

Calibrate the cost surface from forward demo + paper-shadow data.
Specifically:

1. For each demo trade, compute model-predicted cost (decompose into
   spread + impact + funding + maker/taker share).
2. Compute realized cost = (paper-shadow execution price - demo
   execution price) + (paper funding - demo funding) for the matched
   trade.
3. Regress realized cost on (size/ADV, hour of day, vol regime, name
   liquidity tier) using OLS.
4. The fitted regression IS the cost model.

### Functional form (initial)

```
predicted_cost_bps = α
                   + β1 × (size_usd / ADV_30d)
                   + β2 × vol_7d
                   + β3 × spread_proxy_at_entry
                   + β4 × hour_of_day_indicator
                   + β5 × funding_rate × hold_hours / 8
```

α captures the base half-spread + slippage floor. β1-β5 are calibrated
per-venue (Bybit and Binance differ structurally).

### Validation

The cost model is valid if:
- Predicted vs realized cost correlation > 0.5 (R² > 0.25)
- Model recovers the venue's published taker fee at α (sanity check)
- Out-of-sample (last 30 days of forward demo) prediction RMSE within
  20% of in-sample

If validation fails, drop the worst-performing β term and refit.

### Recosting Round 2 cells

Every Round 2 cell that produces trades gets two cost-attributions:
- **Legacy:** `cost_multiplier=3` flat (matches Round 1)
- **Model:** predicted cost from R6 per trade

The two are reported side-by-side in cell verdicts. A cell must clear its
gate (Tier 2 to reach demo; Tier 3 for real money) **under the model cost**
— not just the legacy flat cost. This protects against "this only works if
costs are ignored."

### Code changes

New module `liquidity_migration/cost_model.py`. Integrates with
existing backtest cost-application logic via a new
`--cost-model {flat, model}` flag.

Effort: ~2 days code + tests + calibration.

### Output

`docs/preregistration/<DATE>-r6-cost-model-verdict.md`. Cost model
spec + validation evidence + delta vs legacy on all R1 cells (so we
know which cells were over- vs under-counting costs).

---

## Sub-phase R7 — Stress test suite (named historical events replay)

**Purpose:** Quantify strategy behaviour during named tail events.
Round 1 noted the strategy is "regime-conditional"; R7 makes that
concrete by replaying specific historical regime breaks and measuring
P&L, max DD during the event, and time to recovery.

### Events to replay

| Event | Date range | What happened |
|---|---|---|
| BTC March 2020 crash | 2020-03-09 → 2020-03-20 | -50% BTC in 5 days, deleveraging cascade |
| LUNA collapse | 2022-05-08 → 2022-05-18 | $40B+ market cap evaporated in 10 days |
| 3AC / June 2022 deleveraging | 2022-06-12 → 2022-06-22 | Hedge fund liquidations triggered cross-venue cascade |
| FTX collapse | 2022-11-08 → 2022-11-15 | Largest exchange failure to date; cross-venue contagion |
| Yen carry unwind | 2024-08-05 → 2024-08-12 | Cross-asset deleveraging; crypto crashed despite no idiosyncratic catalyst |
| April 2025 regime shift | 2025-04-01 → 2025-04-30 | Inferred from Round 1 results — month where the strategy first showed major drawdown |
| Nov-Dec 2025 stretch | 2025-11-01 → 2025-12-31 | The losing-months stretch in Round 1 baseline equity curve |
| May 2026 drawdown | 2026-05-01 → 2026-05-28 | The current ongoing -42% DD |

### Method

For each event, run the R9 integrated strategy (after R9 completes) on
the event window only, with:
- Strategy state warm-started from data ending 90 days before event
  (matches a realistic live restart scenario)
- Same fill model, cost model, position sizing as the R10 demo-candidate run
- No look-ahead — only data known at each tick

Report per event:
- Trades opened during event
- Trades closed during event
- P&L during event
- Max DD during event
- Days from event end to recovery (high-water-mark)
- Comparison to baseline strategy P&L during same event

### Validation criteria

Stress-test "pass" requires:
- Strategy DD during ANY event ≤ -50% (i.e. doesn't go beyond historical
  baseline DD in any single event)
- Strategy P&L during 3 / 8 events ≥ baseline P&L during the same
  events (i.e. doesn't underperform baseline in tail events)
- Days to recovery ≤ 180 from any event (strategy doesn't get stuck)

If a strategy cell fails any of these on any event, it's
**promotion-falsified** regardless of its full-window metrics. This is
the strongest tail-risk gate in Round 2.

### Compute

~8 events × ~5 min stress backtest per event per cell. For each R10
candidate cell, ~40 min wall.

### Output

`docs/preregistration/<DATE>-r7-stress-test-verdict.md`. Per-cell
stress event table + pass/fail per criterion.

---

## Sub-phase R8 — Capacity analysis

**Purpose:** Compute the AUM ceiling at which strategy's own market
impact erodes Sharpe meaningfully. JS-style: never deploy a strategy
without knowing this number.

### Method

For each R10 candidate cell, simulate scaled-up versions:

| Scale | Notional per name |
|---|---|
| 1× (current) | dollar_equal or risk_equal as configured |
| 5× | 5× the per-name notional |
| 10× | 10× |
| 25× | 25× |
| 50× | 50× |
| 100× | 100× |

For each scale, apply the R6 cost model (which has a size/ADV term),
recompute trade P&L. Find the scale at which Sharpe drops 30% below
1× Sharpe. That's the **capacity ceiling**.

### Output

For each R10 candidate, a capacity curve (scale vs Sharpe vs MAR) and
a single capacity ceiling number reported in the verdict.

### Validation

A cell is "deployable" only if its capacity ceiling implies real-money
AUM ≥ 10× the operator's intended deployment size. For typical retail
target sizes ($10k-$100k notional), this means capacity ceiling ≥
$100k notional per name (current effective ~$3k per name on demo
suggests we're at <1% of capacity, so this should not bind for most
cells).

### Compute

Per cell: 6 scales × ~10 min recost = ~60 min wall.

### Output

`docs/preregistration/<DATE>-r8-capacity-verdict.md`. Per-cell
capacity curve.

---

## Sub-phase R9 — Integrated strategy assembly

**Purpose:** Combine the validated outputs of R1-R8 into ONE integrated
strategy specification, then run the candidate cells.

### Architecture

The integrated strategy = **event-driven entries augmented by IC signal,
sized by risk, costed by model, capped by factor exposure.**

Specifically, for each (date, candidate symbol):

1. **Event-driven entry gate** (from R1 filter audit, kept filters):
   the symbol must pass the event-detection filter stack (the production
   stack minus any R1-validated drops). If gate fails: skip.

2. **IC-augmented signal score** (from R2 standalone + R4 risk model):
   compute the orthogonalized 2-factor signal from R2 (Factor A: vol/
   extension state; Factor B: short-horizon mean reversion). Symbol's
   signal_score = sum of factor loadings weighted by their IC
   magnitudes (sign-corrected; all features have negative IC so
   weighted negatively means short).

3. **Signal threshold:** symbol's |signal_score| must exceed a
   pre-registered threshold (TBD: tested in R9 sub-cells at multiple
   thresholds; the "rank top decile by signal_score" is the default
   pinning rule).

4. **Risk-exposure caps** (from R4): the candidate's factor exposure
   (BTC beta, momentum, vol regime, etc.) must not push the active
   basket beyond pre-registered per-factor exposure caps (e.g.
   |basket BTC beta| ≤ 0.5). If adding the candidate would breach a
   cap, skip.

5. **Position sizing** (from R5): risk-equal sizing with target vol
   pinned from R5 winner.

6. **Cost-adjusted P&L** (from R6): model cost predicted per trade,
   subtracted from realized return.

### Variants tested (R9 cell list)

7 integrated-strategy cells × 2 venues = 14 runs:

| Cell | Description |
|---|---|
| `R9_event_only` | Event-driven only (whatever is the active baseline per the R1 re-baseline cascade — production OR drop_all_4 if R1 promoted it), risk-equal sized, model-costed. Control. |
| `R9_event_plus_ic` | Event-driven + IC signal additive (signal must exceed threshold OR be event-driven) |
| `R9_event_AND_ic` | Event-driven AND IC signal (both must fire — strictest) |
| `R9_event_OR_ic_factor_capped` | event OR ic, with R4 factor exposure caps active |
| `R9_ic_only_top_decile` | Pure IC signal, top-decile-by-signal, no event filter |
| `R9_market_neutral` | Bullish event-driven + bearish event-driven (from R3) in parallel slots |
| `R9_market_neutral_factor_capped` | Same as above + R4 factor caps |

Investigation bar at this stage; the winning cell forwards to R10 for the
Tier 2 Demo-candidate gate.

Compute: ~14 cells × ~15 min = ~210 min wall.

### Output

`docs/preregistration/<DATE>-r9-integrated-strategy-verdict.md`. The
integrated-strategy spec finalized + best cell identified.

---

## Sub-phase R10 — Demo-candidate gate (Tier 2) + Tier-3 prep

**Purpose:** Apply the **Tier 2 Demo-candidate bar** (return positive both
venues + pooled MAR Δ > +0.1 + neither venue worse than -0.5 + trade
minimums) to the R9 candidate(s) to decide what advances to forward demo +
R11 OOS. At the same time, compute every **Tier 3 (real-money) input** so the
later real-money decision has them ready.

### Method

Each R9 investigation-positive cell gets:
- Full window run + 3 sub-periods, both venues → **Tier 2 demo-eligibility**
- The fragility diagnostics + Tier-3 inputs, computed and recorded (they do
  NOT gate Tier 2, but a cell needs them to clear Tier 3 later):
  - block-bootstrap pooled MAR-Δ p5 + leave-one-month-out (`r1_robustness.py`)
  - R4 residual Sharpe
  - R6 model-cost recosting (Tier 2 must hold under model cost too)
  - R7 stress-test result
  - R8 capacity ceiling

Demo-eligible cells advance to the forward treadmill and queue for R11 OOS.
The only hard cap is the pre-2023 OOS quarterly limit (max 5 cells / quarter);
ranking for that queue is by combined pooled MAR Δ, then by bootstrap p5
(most robust first).

### Cell list (R10)

Conditional on R9 outputs. At most ~5 cells × 2 venues × 3 sub-periods
× ~10 min = ~5h wall.

### Output

`docs/preregistration/<DATE>-r10-demo-candidate-verdict.md`. The
demo-candidate(s) forwarded to forward demo + R11 OOS, with all Tier-3
inputs recorded.

---

## Sub-phase R11 — Pre-2023 OOS gate (mandatory final)

**Purpose:** Same as Round 1 Phase 7. The only clean evidence surface
remaining for this strategy is the pre-2023 dedicated OOS roots.

### Pre-requisite

Pre-2023 Bybit + Binance roots must exist on the 5950X. If not,
rebuild before R11 (~6h data download per venue).

### Method

For each R10 finalist, run on:
- Pre-2023 Bybit OOS root (full window + 3 sub-period thirds)
- Pre-2023 Binance OOS root (same)

R11 is the OOS component of the **Tier 3 real-money gate**. Apply the Tier 3
OOS criteria on the pre-2023 data:
- MAR > 0 on both venues, all sub-periods
- DD < 50% on both venues, all sub-periods
- Sign-consistent direction
- ≥20 trades/sub-period on Bybit (≥15 on Binance)

### Output

`docs/preregistration/<DATE>-r11-pre2023-oos-verdict.md`. Verdict per
candidate.

### Forward state

A finalist passing R11 is **paper_ready** per the integrity standard,
eligible for forward demo deployment. The demo deployment itself is a
separate operator decision; the research program ends at R11 verdict.

If ZERO finalists pass R11: **PROGRAM COMPLETE — DOCUMENTED NULL.**
Strategy stays in current state. Forward demo + paper continue.

---

## Sub-phase R12 — Sniper entry execution layer

**Purpose:** The current strategy enters mechanically at `signal_close
+ 1h` at market price. That's an opportunity-cost choice: the 1h delay
is a leakage guard, but the +1h market fill is naïve — it ignores
intraday microstructure that might offer a materially better fill
price within the entry window.

R12 layers **execution alpha** on top of the existing signal: the
signal stays at end-of-day close (1d resolution); only the FILL becomes
intraday-optimized. Variants tested include limit orders with timeout-
fallback, pullback-waiting, volume-spike-triggered entries, and TWAP
slicing.

This is **not** a sub-1h signal extension (which would be a separate
research program). It is an execution-layer refinement that improves
per-trade entry price without touching the entry-decision logic.

### Pre-requisites

- 1m kline data available for the entry window per (signal_date, symbol)
  pair. We do NOT need 1m klines for the full 5-year universe — only
  for the [signal_close, signal_close + Nh] window per actual signal.
  This is targeted, not bulk.
- R6 cost model with maker/taker rebate-aware components (sniper limit
  fills earn maker rebates on Bybit; market fallbacks pay taker fees;
  the cost model must distinguish).

### Honesty discipline: missed fills are first-class data

Sniper logic involves a fundamental trade-off:
- **Wait for a better price** → some trades never fill (the price doesn't
  retrace, the volume spike doesn't happen, etc.)
- **Take the market** → mechanical and guaranteed, but no execution
  alpha

The discipline: **every signal that doesn't fill within the window is
counted as a $0-P&L trade for sniper variants.** This is the operational
truth — a missed entry isn't free, it's an opportunity cost. Phase 6
of Round 1 failed partly because it ignored holding-period exposure
accounting. R12 doesn't repeat that mistake — fill-rate becomes a
first-class metric alongside P&L.

A sniper variant where 40% of signals never fill is NOT comparable to
the control as "Sharpe improvement" — the relevant comparison is
**total P&L per signal** (filled trades' P&L summed AND divided by
total signals attempted, not just filled). The cost-of-missed-fills
calculation is binding.

### Sub-phase R12a — Targeted 1m kline ingestion for entry windows

**Method:** For every (signal_date, symbol) in the historical trade
ledger across both venues, download/derive 1m klines for the window
`[signal_close, signal_close + 24h]`. Use the existing
`archive-download-klines` path (trade-archive aggregation → 1m klines)
which is already implemented.

Storage: a new dataset `klines_1m_entry_windows` partitioned by
(symbol, signal_date). Targeted, not universe-wide.

**Validation:** Sum of (96 × 15m volume) within a 1d window equals the
1d volume from the existing 1h dataset; close-of-last-1m-bar aligns
with close-of-1h-bar.

**Compute:** Per-signal 1m kline download is ~30s wall on a sequential
connection. Round 1 baseline produced ~600 signals on Bybit + ~420 on
Binance = ~1020 signals. At 4-way parallelism, that's ~2h wall.
Round 2's R1-R5 may produce more signals; budget ~6-12h wall total
for the historical backfill.

If 1m data isn't reachable via archive for some old (symbol, date)
pairs (the data-quality gap we documented in Round 1's PIT audit),
those signals get a fallback: sniper logic uses the 1h kline only
and emits a `sniper_data_quality=degraded` flag in the trade row.

### Sub-phase R12b — Sniper entry simulator (code)

**New module:** `liquidity_migration/sniper_entry.py`

```python
def simulate_sniper_entry(
    signal_close_ts: int,
    signal_close_price: float,
    kline_panel_1m: pl.DataFrame,  # [signal_close, signal_close + 24h] 1m bars
    *,
    flavor: str,                   # see flavors below
    side: str,                     # "short" or "long"
    window_hours: float,           # max wait before market fallback
    limit_pct: float | None,       # for limit_pct_then_market flavor
    volume_mult: float | None,     # for volume_spike_then_market flavor
    twap_slices: int | None,       # for twap_split flavor
    pullback_pct: float | None,    # for pullback_threshold flavor
) -> SniperFill:
    """Return a SniperFill with fill_ts, fill_price, fill_type
    ('limit' | 'market_fallback' | 'pullback' | 'volume_spike' | 'twap' | 'missed'),
    and `missed: bool` flag."""
```

**5 sniper flavors (cells):**

| Flavor | Logic | When it shines |
|---|---|---|
| `market_at_1h` | Current behaviour: market fill at signal_close + 1h. Control. | Signal is reliable; price moves away quickly |
| `limit_pct_then_market` | Place limit at `signal_close × (1 + limit_pct)` for shorts (i.e. wait for retracement up); market fallback at `signal_close + window_hours`. | Choppy signal-day reactions where price tends to retrace within a few hours |
| `volume_spike_then_market` | Wait for a 1m volume > `volume_mult × prior_1h_mean_volume` within the window; enter at the next 1m close after the spike. Market fallback at `signal_close + window_hours`. | Signal-day continuation pattern where the move is volume-confirmed |
| `twap_split` | Break the position into `twap_slices` equal-time pieces over `window_hours`; fill each at the corresponding 1m bar close. Always fully fills. | Large positions where market impact matters; lower-confidence signals |
| `pullback_threshold` | Wait for 1m close to move `pullback_pct` adverse from signal_close (for shorts: price moves UP by pullback_pct). Enter on the next bar. Market fallback at `signal_close + window_hours`. | Mean-reversion-favorable signals where the entry edge requires waiting for the initial momentum to spend |

All flavors are PIT-clean: the simulator only consumes 1m kline data
in chronological order; no future-peek.

**Tests pin:**
- PIT causality per flavor
- Missed-fill flag correctness
- Fill price matches the bar's close (not the bar's low/high — that
  would be future-peek)
- `market_at_1h` flavor matches existing strategy's fill price to
  within 1 bps (regression test for the control)

Effort: ~2 days code + tests.

### Sub-phase R12c — Sniper univariate test

**Method:** For each of the 5 sniper flavors, re-run the production
strategy from R1 baseline_v2 against both venues with the sniper
entry layer inserted. Measure:

| Metric | Why |
|---|---|
| Avg fill improvement vs market@1h | The execution alpha number |
| % of signals filled | The fill-rate cost |
| Per-signal P&L (filled trades P&L / total signals) | The honest comparison metric |
| MAR ratio | Round 2's primary objective |
| Sharpe | Secondary tie-breaker |
| Avg slippage by hour-of-day | Sanity: are some hours systematically better fills |

**Investigation bar applies.** A sniper flavor investigation-positives
if per-signal P&L beats market@1h on majority of venues (even after
counting missed fills as $0). The improvement must come from EITHER
better filled-trade entry price OR better selection of which signals
to take (a "smart abstain" signal).

**Cell list:** 5 flavors × 2 venues × ~7 parameterizations
(limit_pct ∈ {0.5%, 1%, 1.5%}; volume_mult ∈ {2, 3, 5}; twap_slices ∈
{4, 8}; pullback_pct ∈ {0.5%, 1%, 1.5%}; window_hours ∈ {2, 4, 8})
= ~70 sniper-config cells. Compute: ~10 min/cell on cached 1m
windows = ~150 min wall sequential, ~20 min at 8-way parallel.

### Sub-phase R12d — R9 × sniper integration

**Triggered if:** R9 produces ≥1 investigation-positive cell AND R12c
produces ≥1 sniper flavor that beats market@1h.

**Method:** For each R9 winner, build a "R9 + sniper" variant using
the best R12c sniper flavor. Test on both venues, full window + 3
sub-period thirds. Tier 2 Demo-candidate bar applies (Tier-3 inputs recorded).

**Cell list:** Up to 5 R9 winners × 1 best sniper = 5 cells × 2 venues
= 10 runs. Compute: ~30 min/cell = ~5h wall.

### Sub-phase R12e — Entry-delay reduction sweep

**Purpose:** With the sniper layer in place, the +1h leakage-guard
delay might be over-conservative. Test reducing it.

**Cells:** Best R9-sniper variant with `window_hours ∈ {0.5, 1, 2, 4, 8}`.
Each recosted with R6 cost model (shorter delays = potentially
different cost profile).

**Investigation bar.** Compute: 5 cells × 2 venues = 10 runs × ~15 min
= ~30 min wall.

**Falsifier specific to R12e:** if reducing delay below 1h produces a
material PIT violation (e.g. the signal-close kline isn't actually
finalized until ~30s after close due to v5 API delivery latency), the
sub-1h delay variants are rejected as non-executable in live.

### Sub-phase R12f — Sniper stress test

**Triggered if:** any R12d cell is demo-eligible (stress is a Tier-3 input
computed for every demo-candidate).

**Purpose:** R7 events replayed with sniper logic. Sniper variants
may behave very differently in stressed regimes (limit orders don't
fill when the market is one-way; volume spikes are everywhere;
pullbacks don't materialize). Need explicit evidence the sniper layer
doesn't make tail-event behavior worse.

**Method:** Run the R12d demo-eligible cells against each R7
event. Same pass criteria (DD < 50% in any event).

**Compute:** Per cell, ~40 min wall. ~3-5 finalists × ~40 min = ~3h.

### Sub-phase R12 sequencing summary

```
R12a (1m data ingestion) ─┐
                          ├─> R12b (simulator code) ─> R12c (univariate test)
                          │                              │
                          │                              ▼
                          │                          R12d (R9 × sniper) ──> R12e (delay sweep)
                          │                              │                  │
                          │                              ▼                  ▼
                          └─────────────────────────> R12f (sniper stress)
                                                         │
                                                         ▼
                                                   forward to R10 demo-candidate
                                                   + R11 OOS gates (same as non-sniper cells)
```

R12 runs in parallel with R4-R9; sniper-variant cells go through the
same R10 (Promotion) and R11 (OOS) gates as the non-sniper R9 cells.

### Code changes for R12

- `liquidity_migration/sniper_entry.py` (new module, ~2 days)
- `liquidity_migration/cli.py` extension: `--entry-flavor` flag on
  `volume-events` accepting `{market, limit_pct, volume_spike, twap,
  pullback}` and the associated parameterization flags
- `volume_events_cell.sh` helper: `--sniper-flavor X --sniper-params 'K=V,…'`
  passes through cleanly
- `scripts/build_entry_window_1m_klines.py` (new, ~half day): the targeted
  1m kline ingester from R12a
- Test fixtures: synthetic 1m kline panels + expected fill results per
  flavor (~half day)

Total R12 code work: **~3-4 days**.

### Output

`docs/preregistration/<DATE>-r12-sniper-entry-verdict.md` per
sub-sub-phase as they complete. R12d/e/f get incorporated into the
R10/R11 verdicts since they're forwarded through the same gates.

---

## Sub-phases C0-C3 — Continuous-signal architecture

These four sub-phases build and test the **Architecture B** alternative
to the daily-signal Architecture A. C-phases share R1-R8 + R10 + R11
infrastructure (see "Two signal architectures in scope" above) and run
in parallel with the R-phase work.

### Sub-phase C0 — Architecture spec + rolling-feature registry + continuous-evaluation engine

**Purpose:** Build the foundational infrastructure for Architecture B.
Without this, C1-C3 can't run.

**Components:**

**C0a — Rolling-feature registry** (new module
`liquidity_migration/continuous_features.py`)

Defines the rolling-window counterparts of every daily feature the
strategy uses. Each entry pins:
- Feature name (e.g. `rolling_24h_liquidity_rank`)
- Rolling window length (e.g. 24h, 168h)
- Update cadence (e.g. every 60min step)
- Computation: cross-sectional vs per-name
- Causality bound: which timestamp the feature value is "as of"

Initial registry table (mapping daily → continuous):

| Daily feature | Continuous equivalent | Window | Update | XS/PerName |
|---|---|---|---|---|
| `liquidity_rank` | `rolling_24h_liquidity_rank` | trailing 24h | 60min | XS |
| `prior7_liquidity_rank` | `rolling_24h_liquidity_rank_avg_7d` | trailing 7×24h rolling-24h-rank samples averaged | 60min | XS |
| `rank_improvement` | `rolling_24h_rank_improvement` | (current - prior_avg) of the rolling 24h ranks | 60min | XS |
| `turnover_ratio` | `rolling_turnover_ratio_24h_vs_7d` | trailing 24h turnover / trailing 7×24h mean | 60min | per-name |
| `event_rank_fraction` | `rolling_24h_event_rank_fraction` | rank of event-score / universe size, at any tick | 60min | XS |
| `day_return` | `rolling_24h_return` | (current_close - close_24h_ago) / close_24h_ago | 60min | per-name |
| `residual_return` | `rolling_24h_residual_return` | rolling_24h_return - universe-mean rolling_24h_return | 60min | XS-derived |
| `close_location` | `rolling_24h_price_location` | (current_price - rolling_24h_low) / (rolling_24h_high - rolling_24h_low) | 60min | per-name |
| `pit_age_days` | same (calendar age, doesn't need rolling) | symbol launch_ts vs now | once | per-name |
| `crowding_filter` | `rolling_24h_crowding_score` | union-pathology with rolling-24h inputs | 60min | XS-derived |
| `stop_pressure_*` | `rolling_window_stop_count` | stops fired by basket in trailing K hours | 60min | global |
| `realized_loss_pressure_*` | `rolling_window_realized_loss_count` | losing exits in trailing K hours | 60min | global |

The rolling-feature registry is the heart of Architecture B. Every
strategy decision in continuous mode uses these features instead of
their daily equivalents. All are PIT-clean by construction (rolling
windows look backward only).

**C0b — Continuous-evaluation engine** (new module
`liquidity_migration/continuous_events.py`)

Implements the K-minute step loop:

```python
def run_continuous_events_backtest(
    data_root: Path,
    *,
    start: str, end: str,
    config: ContinuousEventsConfig,  # rolling-feature thresholds
    step_minutes: int = 60,
) -> BacktestResult:
    """
    For each K-minute step in [start, end]:
      1. Refresh rolling features for all universe symbols
         (cached: only the names whose 1h kline just closed need
         re-aggregation)
      2. For each universe name with feature values now satisfying
         the predicate AND not in cooldown:
           emit signal
      3. For each signal:
           if free slot in basket: schedule entry at signal_ts + entry_delay
           else: drop (cooldown still applies to dropped signals to
                 avoid signal-spamming)
      4. For each active position: check SL/TP/hold-timeout
      5. Settle PnL, update equity, advance to next step
    """
```

The engine maintains:
- Cached rolling-feature state per (name, step)
- Active position state
- Per-name cooldown countdown (in hours)
- Equity curve at K-minute resolution
- Trade ledger with `decision_ts` precise to the minute (not just to
  the calendar day)

**C0c — Backtest validation** (regression test against daily mode)

If we run Architecture B with `step_minutes=1440` (one step per
calendar day) and all rolling-window lengths set to 24h, the results
should be **bit-identical** to the daily-mode (Architecture A) backtest.
This validates the continuous engine correctness — it's a
generalization of the daily engine, not a re-implementation.

Failure of this validation = the continuous engine has a feature
definition mismatch that must be fixed before C1+ runs.

**Code work:** ~5-7 days total (rolling features + engine + validation
+ tests). Largest single code addition in Round 2.

**Output:** `docs/preregistration/<DATE>-c0-continuous-engine-verdict.md`
confirming the validation, with the engine ready for C1.

### Sub-phase C1 — Continuous-signal feature engineering + univariate IC

**Purpose:** Run the Phase-5-equivalent univariate IC test using the
rolling-feature versions of the 5 IC survivors. Confirms the
continuous-feature definitions preserve (or strengthen) the cross-
venue signal.

**Method:** Same as R2 standalone tests but with continuous features
and K-minute forward returns:

- Rolling versions of `vol_of_vol_30d`, `realized_vol_7d`,
  `dist_from_30d_low`, `xs_rank_ret_7d`, `xs_rank_ret_3d`
- Forward returns at K-minute horizons: {1h, 3h, 24h, 72h, 168h}
- Cross-venue IC + sub-period stability + sign-consistency thresholds
  same as R2 / Phase 5

**Expected output:**
- IC values for each (feature × horizon × venue) combination
- Comparison of continuous vs daily IC magnitudes (does continuous
  preserve signal?)
- Optimal forward horizon for continuous architecture
- Per-feature decile-spread P&L at K-minute resolution

**Investigation bar:** Same as Phase 5 (|IC| ≥ 0.03, |t| ≥ 3, sign-
consistent cross-venue).

**Cell list:** 5 features × 5 horizons × 2 venues = 50 IC measurements.
Compute: ~30-60 min wall (signal_harness is fast).

**Output:** `docs/preregistration/<DATE>-c1-continuous-ic-verdict.md`
with the feature set pinned for C2.

### Sub-phase C2 — Continuous-signal R9 variant

**Purpose:** Build the integrated continuous-signal strategy (the
Architecture B equivalent of R9), test it, and feed demo-eligible
cells to R10/R11.

**Architecture:** Same as R9 but with continuous-signal substrate:

1. **Continuous-feature gate** (from R1 + C0 + C1): symbol's rolling
   features must pass the (continuous version of the) production filter
   stack
2. **IC-augmented signal score** (from C1): orthogonalized rolling-
   feature signal
3. **Signal threshold:** as R9
4. **Risk-exposure caps** (from R4): factor loadings computed at
   continuous decision_ts
5. **Position sizing** (from R5): risk-equal, computed at
   continuous decision_ts (uses rolling realized vol)
6. **Cost-adjusted P&L** (from R6): per-name per-bar cost model
   applied to actual (potentially intra-day) fills

**Variants tested (C2 cell list):**

7 integrated-continuous-strategy cells × 2 venues = 14 runs:

| Cell | Description |
|---|---|
| `C2_event_only` | Continuous-event-driven only, risk-equal sized, model-costed. The Architecture B equivalent of `R9_event_only`. |
| `C2_event_plus_ic` | Continuous event + IC additive |
| `C2_event_AND_ic` | Continuous event AND IC threshold |
| `C2_ic_only_top_decile` | Pure continuous IC, no event filter |
| `C2_market_neutral` | Bullish continuous + bearish continuous in parallel slots |
| `C2_event_OR_ic_factor_capped` | event OR ic with R4 factor caps |
| `C2_market_neutral_factor_capped` | Market neutral + factor caps |

Investigation bar; the winning cell forwards to R10 for the Tier 2
Demo-candidate gate.

Compute: ~14 cells × ~30 min (continuous engine is slower than daily
because of per-K-minute recomputation; partial caching helps) = ~7h
wall at 4-way parallel.

**Output:** `docs/preregistration/<DATE>-c2-continuous-r9-verdict.md`.
Architecture B's best cell identified; forwards to R10.

### Sub-phase C3 — Continuous-signal stress test

**Triggered if:** any C2 cell is demo-eligible at R10 (stress is a Tier-3
input computed for every demo-candidate).

**Purpose:** R7's named-event replay applied to the continuous
strategy. Architecture B may stress very differently from A:

- Continuous signal might enter trades *mid-event* (no daily-close
  gating), increasing tail-event entry frequency
- Cooldown is in hours, so continuous can re-enter the same name
  within 5 days of an event-induced stop
- WS feed quality during tail events is a venue-specific risk (Bybit
  feed has paused during prior cascade events)

**Method:** Same as R7 (named events from March 2020 → May 2026
replay) with the C2 demo-eligible cells. Pass criteria identical
to R7: DD < 50% in any event, basket-correlation < 0.6, no day with
>3 simultaneous stop-outs.

**Additional continuous-specific check:** During each event, log the
fraction of WS-feed disruption minutes (if any cached WS feed had >5
minute gap). If >5% of event minutes had feed disruption AND a stop
fired during that disruption window, the cell is flagged as
"WS-feed-fragile" — real-money-eligible (Tier 3) only with explicit
operator acknowledgment.

**Output:** `docs/preregistration/<DATE>-c3-continuous-stress-verdict.md`.

### C-phase total compute estimate

| Phase | Cells/work | Wall |
|---|---|---|
| C0 — engine + features (code) | ~5-7 days code | (overlaps with R4/R6 weeks) |
| C0c — validation regression | 2 venues | ~10 min |
| C1 — univariate IC | 50 measurements | ~30-60 min |
| C2 — integrated continuous | 14 cells | ~7h at 4-way par. |
| C3 — stress test | per finalist | ~40 min/finalist |

C-phases add **~1 week of code work + ~8h of compute** on top of the
base R-phase + R12 program. Net Round 2 estimate: ~2.5-3 weeks wall
time, with ~10 days being code.

---

## Compute plan

### Per-cell environment

```bash
export POLARS_MAX_THREADS=4
export SWEEP_MAX_WORKERS=8
```

8 cells × 4 polars threads = 32 threads = full 5950X SMT.

### Phase-by-phase wall-time estimate on 5950X

| Phase | Cells | Wall (8-way par.) | Cumulative |
|---|---:|---:|---:|
| R0 — Doc cleanup | 0 | 0 min | 0 min |
| R1 — Per-filter audit | 12 | 30 min | 30 min |
| R2 — Per-feature standalone | 30 | 30 min | 1 h |
| R3 — Bearish stack | 6 | 60 min | 2 h |
| R4 — Risk model build | (code work) | ~3 days | 2 h + 3d |
| R5 — Sizing calibration | 8 | 30 min | 3d 2.5h |
| R6 — Cost model build | (code work) | ~2 days | 5d 2.5h |
| R7 — Stress tests | per-finalist | 40 min each | + 40 min/finalist |
| R8 — Capacity | per-finalist | 60 min each | + 60 min/finalist |
| R9 — Integrated assembly | 14 | 210 min | 5d 6h + |
| R10 — Promotion gate | up to 5 finalists | 5 h | 5d 11h |
| R11 — OOS gate | up to 5 finalists | 55 min/finalist | + 55 min/finalist |
| R12a — 1m kline ingestion | targeted per signal | ~6-12 h | + 12 h |
| R12b — Sniper simulator | (code work) | ~2 days | + 2 days |
| R12c — Sniper univariate | ~70 | ~20 min | + 20 min |
| R12d — R9 × sniper | up to 10 | ~5 h | + 5 h |
| R12e — Delay reduction | 10 | 30 min | + 30 min |
| R12f — Sniper stress | per finalist | ~40 min/finalist | + 40 min/finalist |
| C0 — Continuous engine (code) | (code work) | ~5-7 days | + 5-7 days |
| C0c — Engine validation | 2 venues | ~10 min | + 10 min |
| C1 — Continuous univariate IC | 50 measurements | ~30-60 min | + 1 h |
| C2 — Continuous R9 variant | 14 | ~7 h at 4-way par. | + 7 h |
| C3 — Continuous stress | per finalist | ~40 min/finalist | + 40 min/finalist |

**Total: ~2.5-3 weeks wall time** with ~10 days being code work
(R4 risk model + R6 cost model + R12a/b sniper infrastructure +
C0 continuous engine + features). R12 adds ~3-4 days of code +
~24h compute; C-phases add ~5-7 days code + ~8h compute on top of
the base R-phase Round 2. The 5950X handles the parallel sweeps; the
code work is the bottleneck.

The operator should expect:
- **Day 1-2:** R0 + R1 + R2 (mostly mechanical sweeps)
- **Day 3-5:** R4 (risk model build) + R6 (cost model build) — code work
- **Day 4-6 (parallel):** R12a (1m ingestion) + R12b (sniper simulator) — independent of R4/R6
- **Day 6-8:** R3 + R5 + R7 (depends on R4) + R8 (depends on R6) + R9 + R12c (univariate)
- **Day 9-10:** R10 + R12d (R9 × sniper integration)
- **Day 11-12:** R11 + R12e (delay sweep) + R12f (sniper stress) if any finalist emerges

Conditional shortcuts:
- If R1 finds no DROPs/RE-TESTS productive, R1 takes 30 min not 2h
- If R3 bearish stack investigation-negative, R3_market_neutral cell
  in R9 is skipped, R9 shrinks
- If R5 winner is ≈ dollar-equal in result, sizing change doesn't
  propagate — R9 cells use legacy sizing
- If R6 cost model is materially different from cost_multiplier=3,
  R10 needs cells re-run under the new cost; otherwise legacy
  cost-multiplier=3 is fine
- If R12c finds no sniper flavor that beats market@1h, R12d/e/f are
  skipped — sniper layer concluded "no execution alpha here"
- If R12a's 1m kline ingestion fails for material % of signals (e.g.
  >20% no-data-available), R12 reports degraded coverage and we
  decide whether to proceed at reduced rigour or pause
- If NO R9 cell investigation-positives AND NO R12d cell
  investigation-positives, R10 + R11 are skipped; program closes
  with documented null

---

## Threats to inference

Cross-referenced to `docs/backtesting_errors_we_never_repeat.md`.

| # | Threat | Mitigation |
|---|---|---|
| #1 | Future universe selection | Full 764-symbol manifest (v5-listing supplement always-on per K1-K5 refactor). No PIT contamination from today's coverage. |
| #2 | Future info in signals | All R2 features computed at end-of-day close (decision_ts). All forward returns entry+1h → entry+1h+Nd. Signal harness tests pin causality per feature. R4 factor exposures computed at entry, not from data after. |
| #4 | Revised / non-PIT data | All runs against full-PIT roots. No retroactive manifest filtering for promotion (Phase 1 biased_benchmark commitment is binding). |
| #15 | Warm-started state | R7 stress tests explicitly use cold-start with 90 days of warm-up data, matching realistic live restart. R9 cells use standard volume-events cold-start. |
| #16 | Same-code illusion | All R2-R10 features and filters live in production-shipped code (signal_harness, risk_model, cost_model modules). Demo daemon honours the same flags as backtest — no backtest-only branches. |
| #17 | Parameter mining | Three-tier demo-arbiter structure: Investigation and Demo-candidate gates are deliberately loose (no real-money consequence); the heavy stats gate only at Tier 3 (real money). The 2026-05-28 loosening was on principle (venue heterogeneity, redundant tests), pre-registered, and re-applied blind — not to rescue a seen cell. Multiple-testing control is the forward-demo treadmill; only the finite pre-2023 OOS is capped (5/quarter). |
| #18 | OOS reuse | Pre-2023 roots have been touched twice (original "fail everything" call + Round 1 plan would have used them but didn't because no Phase 7 finalist emerged). R11 OOS dilution is real. Mitigation: ≥30 days of fresh forward-demo data must accumulate before any R11 finalist goes to mainnet conversation. |
| #19 | Multiple testing | Across R1+R2+R3+R5+R9+R10+R12 cells, ~150 cells total (R12c alone adds ~70 sniper config cells). The forward-demo treadmill is the multiple-testing control — every demo-candidate must independently re-prove itself on fresh, un-overfittable forward data, which backtest multiple-testing cannot fake. The one finite surface, the pre-2023 OOS root, is capped at 5 cells/quarter. R12c uses the Investigation bar, so its 70 cells inflate exploration but consume neither demo nor OOS budget. Investigation failures are NOT re-tested under different cell configs. |
| #2  | Future info in signals — sniper-specific | R12 sniper simulator consumes 1m kline panel in chronological order; flow=open→fill is enforced by the simulator (no future-peek). Tests pin per-flavor PIT causality. Fill price uses bar close, not bar low/high (which would peek). |
| #22 | Venue mechanics fantasy — sniper-specific | R12c maker/taker rebate accounting: limit fills earn the venue's maker rebate; market fallbacks pay taker fees. R6 cost model must distinguish these two paths or R12 promotion-eligibility under it is invalid. |
| #23 | Pretty-report bias — sniper-specific | Sniper variants MUST report fill-rate alongside Sharpe/MAR. A "great Sharpe, 30% fill rate" cell is not demo-eligible because the 70% filled trades P&L is meaningless without counting the 30% missed opportunities. |
| #2  | Future info in signals — continuous-specific | Rolling-window features are PIT-clean by construction (only look backward). C0c regression validation (continuous engine with 1d step + 24h window = bit-identical to daily backtest) is the binding correctness check; if it fails, continuous results are invalid. |
| #13 | Timestamp & resampling leakage — continuous-specific | Continuous engine's "as-of-N-min-step" timestamps must align exactly with the K-minute step boundaries; off-by-one is a future-peek. Tests pin step alignment per feature. |
| #15 | Warm-started state — continuous-specific | C0c regression validates cold-start; C2 cells use cold-start in backtest. Live deployment of a continuous strategy requires the same 90d-warmup pattern as the daily strategy. |
| #16 | Same-code illusion — continuous-specific | Continuous backtest engine MUST be the same code path the live continuous daemon would use. If we ship a continuous strategy, the daemon code is the C0 engine called with `live=True`, not a separate re-implementation. Otherwise demo↔backtest divergence is guaranteed. |
| #22 | Venue mechanics fantasy — continuous-specific | WS feed gaps during high-stress events (March 2020, FTX) are a real risk for continuous strategies that rely on minute-resolution feature updates. C3 stress test flags any cell whose stops fire during WS-feed-gap minutes; those cells are "WS-feed-fragile" and require operator acknowledgment. |
| #20 | Bad accounting | R6 cost model fixes the single-multiplier-3 problem from Round 1. All R10+ cells must clear under the model cost, not just legacy flat cost. |
| #21 | Hidden common risk | R4 factor model explicitly decomposes basket risk into 8 named factors. R9 cells optionally cap per-factor exposure. Cells with residual Sharpe < +0.3 are rejected at the Tier 3 real-money gate (catches "you're just selling vol" disguised as alpha). |
| #22 | Venue mechanics fantasy | R6 cost model calibrated against paper-shadow vs demo slippage — uses real venue mechanics, not theoretical. Hold-period funding included. |
| #23 | Pretty-report bias | Every cell produces trade ledger, equity curve, monthly P&L, factor-decomposed P&L, stress-test scorecard, capacity curve, residual-Sharpe report. Mandatory artifacts per cell. |
| #24 | Unreconciled live drift | R11-passing finalists go to demo first with daily paper-shadow reconciliation. No mainnet path that skips ≥30 days demo. |
| #25 | All-or-nothing compute | All sub-phase orchestrators inherit from `scripts/_sweep_runtime.py` shared parallel orchestrator (from R0 cleanup). Every cell's report flushed before next cell starts. |

### Specific Round 1 lessons applied

- **From Phase 0 falsifier-hits:** 3 named load-bearing filters
  preserved without question. No casual "let's see if removing X helps"
  experiments without a fresh pre-reg with new hypothesis.
- **From Phase 1 null:** the universe-widening contribution to DD is
  small. Phase 1-style biased_benchmark tests are SKIPPED in Round 2 —
  the universe question is closed.
- **From Phase 5 success:** the IC harness works. Re-use it in R2/R9
  without re-validation work.
- **From Phase 6 failure:** combined portfolios need orthogonalization,
  proper holding-period accounting, real cost model. R9 does all
  three. The Phase 6 implementation is NOT inherited as-is.
- **From Phase 2 venue-divergence:** cross-venue Manifesto remains the
  primary discrimination filter. Bybit-only winners are NOT promoted.

---

## Pre-registration commitments

By committing this plan, the operator + assistant commit in advance to:

1. **MAR is the primary metric.** Sharpe is secondary tie-breaker.
   Switching back to Sharpe-primary mid-program is explicit
   p-hacking and forbidden.
2. **Three-tier thresholds are pre-committed.** Investigation (R1-R8) and
   Demo-candidate (R10) gates are deliberately loose; the strict Real-money
   (Tier 3) gate is NOT loosened. The 2026-05-28 loosening was on principle,
   pre-registered, and re-applied blind. No FURTHER loosening to rescue a
   seen cell — and the Tier 3 gate stays strict.
3. **The forward-demo treadmill is the multiple-testing control**, not an
   FDR cap. The one finite surface, the pre-2023 OOS root, is capped at
   **5 cells per calendar quarter** (ranked by combined pooled MAR Δ, then
   bootstrap p5); forward demo/paper is uncapped.
4. **R11 OOS is the final gate.** R11 failure = closed. No "Round 3"
   to rescue near-misses.
5. **No production filter change** is made on basis of R1-R8 results.
   Production stack stays as-is until R11 produces a finalist (which
   triggers a separate operator decision about demo deployment).
6. **No mainnet** until ≥30 days of forward demo evidence post-R11
   pass, with daily reconciliation against same-config paper-shadow.
7. **All R0-R9 results are `exploratory` or `biased_benchmark` per
   the integrity standard.** Only R11-passing cells reach `candidate`.
   Only forward-demo-confirmed cells reach `paper_ready`.
8. **No off-menu cells.** New cells require an amendment commit to
   this doc before running. Silent menu expansion is forbidden.
9. **Failure cases are first-class evidence.** If R10/R11 produces
   ZERO finalists, the program ends with a documented null. The
   strategy stays in its current state. There is no "ship something"
   obligation.
10. **No hard end-date.** This is deliberately different from Round 1.
    The operator's instruction is "weeks if needed." If a sub-phase
    legitimately takes longer than its estimate (e.g. R4 risk model
    needs iteration), that is acceptable. The discipline is in NOT
    shortcutting the workflow, not in racing to a deadline.

---

## Timeline (no hard deadline; weeks acceptable)

| Week | Activity |
|---|---|
| Week 1 | R0 (doc cleanup) + R1 (filter audit) + R2 (per-feature) + R3 (bearish stack). Code: 3 small filter-related additions for R3. R12a starts in parallel (1m kline ingestion runs as background download). C0 code work begins (rolling-feature registry + engine skeleton). |
| Week 2 | R4 (risk model — 3 days code) + R5 (sizing — 1 day code + sweep) + R12b (sniper simulator — 2 days code, parallel with R4) + C0 continues (engine implementation + tests) |
| Week 3 | R6 (cost model — 2 days code + calibration) + R7 (stress tests pending R4/R6) + R8 (capacity pending R6) + R12c (sniper univariate test, depends on R12a+R12b ready) + C0c validation (regression vs daily mode) + C1 (continuous univariate IC) |
| Week 4 | R9 (integrated strategy assembly) + R10 (demo-candidate gate) + R12d (R9 × sniper) + C2 (continuous R9 variant). Conditional on R9 + R12c + C2 candidates. |
| Week 5 | R11 (OOS gate) + R12e (delay sweep) + R12f (sniper stress test) + C3 (continuous stress test) if any R10 finalist from EITHER architecture. Conditional on data-root state. |
| Week 6+ | Forward demo deployment proposal IF R11 passing. 30-day demo + paper-shadow reconciliation. Then operator decision on mainnet. If BOTH architectures pass R11, operator decides whether to deploy one or run them in parallel (ops-complexity tradeoff). |

If R1/R2/R3 produce nothing meaningful, R4-R10 can still run as
infrastructure investment (the risk model + cost model are useful
regardless of whether they produce a candidate strategy this round).
The infrastructure becomes durable assets for any future research.

---

## Open questions / things to confirm before kicking off

1. **Venue-specific config?** Phase 2 confirmed Bybit and Binance have
   different optimal rank-improvement thresholds. R10 could allow
   per-venue threshold tuning (e.g. Bybit uses 250, Binance uses 125).
   This doubles the parameter space but matches reality. **Default in
   R10: joint threshold (single value tested against both venues).**
   Per-venue is a possible R10 amendment if joint-threshold cells
   investigation-negative across the board.

2. **Forward-demo runway length?** 30 days post-R11 pass is the
   minimum. Operator may want 60-90 days for higher-conviction
   mainnet sizing. Default: 30 days minimum, operator decides
   longer at promotion time.

3. **Bearish stack as separate strategy?** If R3 produces a
   investigation-positive bearish cell AND R9_market_neutral is
   demo-eligible, do we deploy the long+short basket OR
   maintain just the bearish line as a separate strategy on top of
   existing short-only? **Default: market-neutral basket; if
   operator wants the bearish line standalone that's a separate
   pre-reg.**

4. **Capacity vs deployment size?** R8 reports a number; operator
   decides actual deployment size. Default constraint: never deploy
   >10% of R8 capacity ceiling.

5. **What if a candidate strategy is demo-eligible but uses
   features beyond current PIT data (e.g. needs OI from Binance
   pre-2024 which we don't have)?** Per Round 1 program verdict
   Option D: backfill the data before R11. Default: data backfill is
   ALLOWED before R11; the strategy stays consistent with
   currently-published-data discipline.

---

## Appendices

### Appendix A — Filter hypothesis library (detailed)

For each KEPT filter from R1, the longer-form hypothesis + literature
anchor + observable signature:

- **`crowding_filter` (union_pathology):** ...detection of late entries
  via OR-aggregated stress indicators... [TBD: detailed]
- **`event_rank_frac_max` (0.90):** Caps event score at 90th percentile.
  Hypothesis: top decile of event scorers is over-traded population.
  Literature: cross-sectional attention literature (Da et al 2011).
  Signature: removal lets in highest-event-score names, which Round 1
  Phase 0 confirmed crashes Sharpe by >1.0 on both venues.
- [10 more filters, written out at the time R1 verdict is committed]

### Appendix B — Feature hypothesis library (detailed)

For each R2 feature, detailed mechanism + literature + decile-spread
prediction:

- **`vol_of_vol_30d`:** Daily standard deviation of daily returns over
  30d. High vov = unstable vol regime, GARCH-like clustering.
  Literature: tail risk premium (Bollerslev-Tauchen 2009); volatility
  cascade dynamics (Calvet-Fisher 2008). Predicted: short-side
  decile-spread Sharpe 0.5-1.0 standalone.
- [4 more features, written out at the time R2 verdict is committed]

### Appendix C — Risk-factor library (R4)

For each proposed factor, motivation + measurement spec + expected
behaviour:

- **BTC beta:** OLS regression of name's daily returns on BTC's daily
  returns over rolling 60d. Captures market direction risk.
  Expected: most alts have β in [0.7, 1.3]; majors have β closer to
  1.0; meme coins have β > 1.5 with high noise.
- [7 more factors, written out at the time R4 verdict is committed]

### Appendix D — CLI templates per sub-phase

Reproducible CLI for each R-phase's typical cell, with all baseline
flags filled in. (To be auto-generated from `scripts/volume_events_cell.sh`
once R5/R6 sizing + cost flags are added.)

---

## What is NOT in scope for Round 2

Explicitly out:

- **ML signal combiners.** Round 2 uses linear combinations (equal-Z,
  IC-weighted, PCA-orthogonalized). Tree models / neural nets are
  deferred to a hypothetical Round 3 only if Round 2 produces a
  candidate that we want to enhance.
- **News / sentiment features.** No NLP data ingested.
- **On-chain features.** No on-chain pipeline.
- **Cross-venue arbitrage / pairs.** Each venue tested independently;
  cross-venue is a different strategy class.
- **Long-only strategy.** Bearish + market-neutral are in scope per R3;
  pure long is not.
- **Sub-1h SIGNAL generation at < 60-minute K-step.** Round 2's
  continuous architecture (C-phases) re-evaluates the predicate every
  K minutes; default and tested K is 60 (matching 1h kline cadence).
  Sub-60-minute K-steps (e.g. K=15, K=5, K=1) are NOT in scope: they
  require kline data faster than 1h (which we don't have universe-wide),
  drive backtest compute proportionally higher, and approach HFT
  territory where retail can't compete. K=60 captures the WS-driven
  continuous-evaluation value; sub-60 is a future research program.
- **HFT / order book microstructure.** No order book ingestion; R12
  and C-phases work off 1m / 1h kline aggregates only. Sub-second
  execution, queue position, lit/dark order routing — all out.
- **Alternative asset classes.** Bybit + Binance USD-M perps only.

These exclusions are deliberate scope discipline. Each could be a
future research program on its own pre-reg.

---

## Summary of what makes Round 2 different from Round 1

| Aspect | Round 1 | Round 2 |
|---|---|---|
| Primary metric | Sharpe | **MAR (return/DD)** |
| Threshold tiers | Single (Manifesto strict) | **Three, demo-arbiter (Investigation → Demo-candidate → Real-money); loose where being wrong is free, strict where it costs real money** |
| Filter audit | LOO only (single-threshold strict) | LOO + softer-criterion + individual hypothesis test |
| Feature work | IC test only, then naive combination | IC test + standalone decile + correlation matrix + PCA + orthogonalized combination |
| Bearish hypothesis | Falsified-by-construction (no test) | **Honest test via R3 mirror-stack** |
| Risk model | None (returns vs $0) | **8-factor crypto perp model (R4)** |
| Position sizing | Dollar-equal | **1/realized-vol (R5)** |
| Cost model | Single multiplier ×3 | **Per-name, per-bar regression model (R6)** |
| Tail risk | Implicit via DD reporting | **Named-event stress test suite (R7)** |
| Capacity | Not measured | **Per-cell capacity curve (R8)** |
| Entry execution | Market @ signal_close + 1h (mechanical) | **Sniper layer (R12): limit / pullback / volume-spike / TWAP variants with PIT-clean 1m simulation; missed fills as first-class data** |
| Signal frequency | Daily (calendar-day predicate eval) | **Two architectures in parallel: Architecture A (daily, R-phases) + Architecture B (continuous K=60min, C-phases). Both share R1-R8 infrastructure.** |
| WS infrastructure role | Observation + execution only | **Signal-driving for Architecture B (continuous rolling-feature engine)** |
| Strategy architecture | Event-driven only | **Event-driven + IC-augmented + factor-capped + risk-sized + cost-aware + sniper-executed; continuous-signal variant also tested** |
| Hard deadline | 2026-06-15 (19-day buffer) | None (weeks acceptable) |
| Multiple-testing control | FDR ceiling: 3 candidates to OOS | **Forward-demo treadmill (fresh data can't be overfit); only the finite pre-2023 OOS is capped, at 5 cells/quarter** |
| Mandatory artifacts per cell | Ledger + equity + monthly | + factor-decomposed P&L + stress scorecard + capacity curve + residual Sharpe + sniper fill-rate (R12 variants) |

Round 2 is **bigger, slower, more rigorous, and more honest about
what counts as evidence.** It also produces durable infrastructure
(risk model, cost model, stress harness, capacity analyzer) that
outlasts any single strategy decision.

---

## Sub-phase R13 — exit-rule re-optimization (added 2026-05-28; conditional on R1)

**Gap this closes.** R1–R12 re-optimize the ENTRY (filters, features, sizing,
cost) and the execution FILL (R12 sniper), but no phase re-optimizes the EXIT
RULE. The 2026-05-23 exit-ladder sweep found exits (failed_fade params + holding
period) among the highest-leverage knobs (+18% Sharpe), yet the promoted exit
(take_profit 0.26, failed_fade off, rank_exit 0.55) was tuned for the OLD entry
population. R1's `drop_all_4` changes that population, so the optimal exit very
likely shifts. Leaving the exit fixed while re-optimizing the entry is an
unforced inconsistency.

**Strictly conditional on R1** confirming `drop_all_4` as the lead candidate.

**Method.** Baseline = the R1 lead candidate (wide-funnel baseline + the four
filter drops). Each cell overrides ONLY exit knobs, so trade ENTRIES are
identical across cells and every metric delta is a pure exit-rule effect:
take_profit in {0.21, 0.26, 0.30}, failed_fade in {off, 6h/3%/1%mfe,
6h/4%/1%mfe}, rank_exit_threshold in {0.45, 0.55, 0.65}, fixed stop in {0.10,
0.12}. 8 cells x 2 venues, window 2023-04-01 -> 2026-05-28. Dispatcher:
`scripts/r13_exit_rule_sweep.py` (tag `r13_exit_rule_2026-05-28`).

**Decision rule — Tier-1 Investigation** (in-sample; no OOS consumed). MAR Delta
> 0 on the majority of venues vs `00_baseline_drop4`, no return sign-flip, >=30
Bybit / >=20 Binance trades. Verdict via `scripts/r1_robustness.py`. A winning
exit cell feeds R9 assembly and must still clear R11 OOS + the forward-demo gate
before any real-money consideration — R13 does NOT shortcut Tier-3.
