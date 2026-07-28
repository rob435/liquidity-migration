# Strategy program — reset 2026-07-21

This is the single current authority for strategy evidence, direction, and next
work. `docs/governance.md` still owns evidence policy, `STATE.md` owns deployed
state, and code/tests own implemented behavior. Historical research is useful
only through the compact priors below; its old plans, queues, reports, and
one-off runners are retired.

Nothing in this document changes the active demo/paper profiles, authorizes a
deployment, or opens the separate real-money boundary.

## Current truth

- The active profiles are `continuous_ensemble_v2` at revision
  `active_single_fund0_tp12_sl35_v1` (the single funding-gated cell — the
  profile id predates the 2026-07-26 replacement and no longer implies an
  ensemble) and `LongV11aDivWeekendVol`. They are demo/paper runtime
  configurations, not validated alpha claims.
- No researched replacement currently qualifies for implementation.
- Sleeve kill criteria and the paper passive-execution experiment remain active
  operational evidence surfaces. The prospective runtime-parity epoch and all its
  machinery were deleted on 2026-07-24; the forward stream is now just the
  rolling record under `docs/governance.md`.
- The account-kernel remediation was independent of this research reset and
  deployed with the 2026-07-25/26/27 rollouts of canonical `main`; `STATE.md`
  (Deployment) is the authority for what is installed.
- **Phase 0 of `docs/roadmap_2026-07-25.md` is complete (2026-07-25); see
  `docs/anomaly_research_2026-07-24.md` §16.** Three results change the position:
  - **CONTINUOUS's Sharpe 2.73 is withdrawn as evidence about the deployed
    sleeve.** Its backtest models no stop-loss; the deployed account attaches a
    ~2% native seatbelt that is CONTINUOUS's de facto exit rule because the
    component declares no stop of its own. On the same 2,344 modelled trades that
    stop takes the book from +18.24% / Sharpe 2.50 / t 4.56 to −2.54% / Sharpe
    −0.75. 77.5% of trades breach it, including 64.3% of the take-profit winners.
    LONG models its own declared stop and is not affected.
  - **`funding=partial` is closed as a non-issue**: 99.8% of notional is fully
    modelled, 2 trades per component are flagged. It never explained anything.
  - **The 4 bp cost error is narrower than reported.** LONG is priced at 45 bp and
    CONTINUOUS at a measured 24.12 bp round trip — both *conservative* against the
    realised 15.56 bp. The 4 bp assumption was confined to the cross-venue anomaly
    reads and `lane2_premium_momentum_blend_v1`. §12.2's 3.89× restatement of
    CONTINUOUS and §11.1's "implausibly cheap" are withdrawn.
  - The binding open item is therefore a **design** question — give CONTINUOUS a
    strategy-level exit its backtest also models — not another signal search.
- **Phase 1 and 2A/2B are complete (2026-07-25); see §17.** Screened on an
  11.4M-row / 636-symbol both-venue panel, 2021-2026, at the honest cost basis.
  - **Gate 1: 0 of 12 cells clear t ≥ 3.25.** No further sweeps, per roadmap §3.
  - **A second cost error, independent of the 4 bp one.** The long/short books are
    2x gross but were charged one round trip. Charged their *measured* turnover,
    the flat rate had **overcharged** slow-rotating momentum (11.9 bp actual) and
    **undercharged** fast-rotating premium (25.2 bp). Correcting it reranks the
    program: **premium_diff, the headline signal, is negative (t −0.69)**, and
    **funding carry — the designated dead control — is the strongest cell
    (t 3.05, Sharpe 1.34, positive in six of six eras)**.
  - **2A kills five of six mechanisms.** Every positive Bybit effect fails
    cross-venue replication (ratios 0.15–0.45); the only mechanism that replicates
    is dead on both venues. The replication escape is closed. "Bybit-local" must
    now be read as *uncorroborated*, not as a convenience.
  - **2B: the gate helped 6/12 books (50%)** — not a kill, not support. It is
    perfectly venue-consistent but mechanism-specific: it helps momentum-shaped
    books and harms carry/premium-shaped ones, so it is a candidate component, not
    a portfolio overlay. §14's headline lead replicates in direction
    (−9.73 → +24.88 bp/day) and still reaches only t 1.08.
- **Phase 5 is complete (2026-07-25); see §18–§19.** The program's conclusion is
  now economic rather than statistical.
  - **5A.** Gates here were tuned on Sharpe and are therefore mis-set for
    evidence: the momentum BTC gate should be `> −0.05`, not `> 0.00`, worth 0.28
    of t for a one-line change. premium_diff has **no positive cell** in 24
    settings and §14's conditional short peaks at t 1.30 across its whole curve —
    both closed across their parameter spaces, not just at their registered points.
  - **5A found one mechanism that clears**: perp-only funding carry, t 3.96 on a
    broad plateau, positive in **6 of 6 eras at every universe size**. It is
    **uninvestable** — 59–276% max drawdown, −10% to −35% worst-1% day — and its
    t-maximising cell fails cross-venue replication while its replicating cells
    have the worst tails.
  - **5C imported one external hypothesis and it explained our own result.** Robot
    Wealth predicted §18.4's exact failure mode in advance, and named the fix
    (delta-neutral spot-perp). Tested with the venue index as a spot proxy, the
    hedge works decisively — **worst-1% improves 24×, max drawdown 17×** — and at a
    7-day hold it clears t 4.33. **Then the era split killed it: the entire result
    is 2021.** The source's own decay caveat was correct.
  - **The synthesis (§19.5): in this market you are paid for holding the risk
    nobody wants, and not paid for the hedged version anybody can run.** The easy,
    popular construction was arbitraged out by 2022; the durable premium is
    compensation for idiosyncratic liquidation risk the current capital structure
    cannot survive. There is no free lunch left in the constructions this
    repository can express.
  - **Phase 3 amended**: the liquidation feed's priority is confirmed but its
    purpose has changed — it is the input that would let the one durable premium be
    *sized and survived*, not merely observed. Spot klines are a low-priority
    completeness item only; the proxy already answered the question, which saved
    the purchase.
- **The two owner-directed follow-ups were built 2026-07-25; see §20.**
  - **CONTINUOUS now declares a 35% stop and its backtest models the same
    stop.** The level comes from extending §16.3's counterfactual upward: every
    binding stop costs expectancy, so the declared level is the widest whose
    worst modeled outcome is still real — 35% binds on 4.9% of trades, and its
    slippage-capped worst fill (−48.5%) sits just inside the ~48% 2×-leverage
    liquidation distance, where a 40% trigger's would not. **The honest headline
    for the deployed variant is the sl35 render: Sharpe 1.87, +15.79%, max DD
    −2.85%** (2023-03→2026-07, hedged reconstruction) — replacing the withdrawn
    no-stop 2.73, versus the −2.54% the old 2% fallback produced, with the
    counterfactual and the engine agreeing within 3%. Profile revision
    `active_tp12_sl35_v1` is the change point; startup now rejects a CONTINUOUS
    component without a declared stop, and the equity-refresh parity gate asserts
    model = profile. Undeployed until the normal rollout; the first live
    `stop_loss` exits are the deployment check.
  - **Passive execution has a fast instrument beside the registered slow one.**
    The 2026-07-20 in-flow paper A/B remains the grader; §16.3's scale finding
    explains why it accrues slowly. `scripts/probe_passive_fill_ab.py` (protocol
    in `liquidity_migration/passive_fill_probe.py`, ITT accounting, written kill
    criteria) bounds the mechanism in hours — it answers whether the 5.40 bp
    passive floor is mechanically reachable, and only that. Blocked on demo
    credentials this box does not hold; run with the fleet stopped and flat.

### 2026-07-28 — research funding accounting corrected: settlements were charged twice

Found while answering the owner's question about Bybit's shortened funding
intervals. The cross-venue panel's funding-age column carries float epsilon
(`0.9999999999999999` one hour after a settlement), so the `age < 1.0`
settlement detector in **both** `financed_longs.settlement_exact_funding`
and `lane2_blend.settlement_exact_funding` charged every 8h/4h/2h
settlement **twice** (1h-interval symbols once — the next print overwrites
the epsilon bar). Fixed with an age-reset detector plus regression tests on
the real age shapes. **Change point: this commit, for both scorers;
registrations untouched** (M19 precedent). The deployed fleet is unaffected
— verified: no engine module consumes the panel or its age columns, and the
engine reads venue funding-history settlements by exact stamp
(`continuous_events`), which also handles Bybit's real 2025-26 cadence
shortening (2025 settlements: 52% 4h, 21% 1h, 7% 2h, ~20% 8h; 2021 was 100%
8h) by construction.

Consequences:

- **The 2026-07-26 financed-longs verdicts are withdrawn.** Corrected
  bench-window Sharpe: carry_hold **1.21** (was 2.56), financed_leaders
  **1.44** (was 2.21), Binance arm **1.01** (was 1.66) vs benchmark 1.84 —
  all three now beat on return only; **none meets the owner goal (return
  AND Sharpe)**. Corrected full-sample t 2.31 / 2.58 — below the ≈3.4
  multiple-testing bar. carry_hold's corrected attribution is funding +7.2
  vs price −3.4 (2.1:1): the premium is real, roughly half the registered
  size, and its 2021-22 bear-robustness (now +3.8/+3.0 bp/day) is
  withdrawn. Corrected table and scope:
  `docs/research_2026-07-26_financed_longs.md` §0; reproduction output
  `reports/financed_longs_corrected_2026-07-28/`.
- **`lane2_premium_momentum_blend_v1` and the anomaly-research
  settlement-exact numbers** (the leg-attribution reversal, the
  dispersion-gate withdrawal, funding-magnitude ledger rows) **are stale**
  pending re-derivation on the corrected scorer (queued below).
- Lane-2 forward scoring of every registered config inherits the corrected
  scorer from this commit; the per-print enter/exit thresholds' interval
  sensitivity (a −10 bp print is −30 bp/day on an 8h name but −60 bp/day on
  a 4h name) is a design gap for any successor config, not a defect in the
  registered ones. *(Same-day follow-up: the quant review below TESTED the
  daily-rate normalization and it collapses — the per-print gate is
  load-bearing, not a gap to fix.)*

### 2026-07-28 — canonical baselines after the correction (artifact cleanse)

Owner-directed cleanse: old comparison baselines are retired, and the
following are **the** citable truths until a recorded change point says
otherwise.

- **Deployed-sleeve baselines** — the comparison surface for any challenger:
  `reports/equity_curves_2026-07-28/` (standard render, window 2023-03-13 →
  2026-07-16, root `bybit_full_pit`, engine accounting — exact-stamp
  settlements, single-count verified in `trade_lifecycle._funding_lookup` /
  `_perp_funding_return` on 2026-07-28; handles 1h/2h/4h cadences by
  construction).
  - **CONTINUOUS** (deployed `active_single_fund0_tp12_sl35_v1`, hedged, 1×
    modeled): **+11.06% / maxDD −1.84% / Sharpe 1.45 / MAR 1.80**, worst day
    −0.81%, 655 trades — reproduces the promotion-note render exactly.
  - **LONG** (`LongV11aDivWeekendVol`, research 1× sizing; the deployed
    account runs the same signal at the 0.5 dial): **+30.98% / maxDD −3.66%
    / Sharpe 1.84 / MAR 2.53**, worst day −1.50%, 205 trades. Tail-dependent
    as always documented (2023-10 alone is +11.4%).
- **Registered-config truth table**:
  `reports/financed_longs_corrected_2026-07-28/` plus §0 of
  `docs/research_2026-07-26_financed_longs.md` — the only citable
  financed-longs numbers. Corrected carry_hold artifacts:
  `reports/carry_hold_equity_2026-07-28/`,
  `reports/carry_hold_trade_diagnostics_2026-07-28/`.
- **Superseded snapshots** moved to `reports/_superseded_2026-07-28/`
  (defect-era financed-longs outputs, the single_fund0 parity decomposition,
  the ladder-mechanism snapshot, and the entire pre-reset ≤2026-06 sweep
  layer); the README inside maps each to its replacement. Two dirs are
  deliberately retained in place as **frozen inputs of the registered
  admission-variant scorer**, not as baselines:
  `equity_curves_sl35_2026-07-26/` (the retired 3-cell render) and
  `continuous_redesign_2026-07-26/`.
- **Rule**: any pre-2026-07-28 number whose funding leg came through the
  cross-venue panel is non-citable unless re-derived on the corrected
  scorer. Historical evidence notes stand as receipts with their correction
  sections; they are not comparison surfaces.

### 2026-07-28 — carry-hold quant review; `lane2_carry_hold_v2` registered

Full review: `docs/research_2026-07-28_carry_hold_quant_review.md`
(mechanism, six declared theses, all grid cells, robustness). Change point:
this commit. Five-line summary:

1. **What**: `configs/lane2_carry_hold_v2.json` — v1's exact state machine,
   but each held name's weight follows the premium being paid:
   `w = 0.10 × clip(|trailing 24h settled funding| / 120 bp-per-day, 0.25, 1.0)`.
2. **Why**: the review's depth-monotonicity test — deepest-carry name-days
   earn ~230 bp/day while decayed-carry holdings (≥ −12 bp/day) bleed
   −67 bp/day; sizing by depth was the only lever of four that survived
   its declared grid (exit-tighten flat; breadth tilt and daily-rate entry
   actively hurt).
3. **Seen-data effect** (module path, 2021-01..2026-07): same mean (17.0 vs
   18.0 bp/day, paired t −0.4), max DD −60.0% → −48.6%, Sharpe 1.02 → 1.11,
   MAR 0.97 → 1.25, turnover −27%. Cost: shallow-carry years pay about half
   of v1's era mean (2023: 14.4 vs 26.0 bp/day) — a stated regime bet.
4. **Evidence status**: Lane-1 selection on seen data (t 2.53, below the
   ~3.4 new-mechanism bar; this is a sizing refinement, not a discovery).
   v1 keeps scoring untouched; the paired daily differential v2−v1 is the
   primary forward comparison.
5. **Also from the review**: the v1 **Binance replication is withdrawn**
   (corrected scorer: t 0.4, Sharpe 0.18 — the doubled funding leg was the
   replication; carry-hold is single-venue evidence), and the registered
   recipe's vol-target overlay hurts both versions on corrected accounting
   (raw is the primary basis).

### 2026-07-28 — wave 2 (owner Sharpe-2 goal): `lane2_carry_hold_v3`; two integrity findings; data refreshed

Full tables: `docs/research_2026-07-28_carry_hold_quant_review.md` §9.
Change point: this commit. Summary:

1. **Registered `lane2_carry_hold_v3`** — v2 plus three filters with
   declared mechanisms (toxic-band [−30%,−5%) 3d-return block/suspend;
   min 5%/day 30d-vol entry floor; +30 bp/2d trail-recovery exit).
   Module path: 19.8 bp/day, t 3.13, Sharpe 1.38 / MAR 2.84 / DD −28.7%
   (v2: 1.09 / 1.21 / −48.6%); bench window 1.71 / 4.84. The paired daily
   differential vs v2 (+3.1 bp/day seen-data) is the forward experiment;
   v1 and v2 keep scoring.
2. **The owner's unconditional Sharpe ≥ 2 target was NOT reached** — ~95
   cells plateau at ~1.4–1.55 single-clock; the honest supportable
   statement is CONDITIONAL: Sharpe 2.15–2.35 on the PIT deep-funding
   half of days, EV-noise on the shallow half. Every refuted lever is
   tabled in the review doc.
3. **Integrity findings that apply program-wide**: (a) single-clock
   financed-longs numbers ride midnight decision-hour luck (12-offset
   sweep spans 0.30–1.52; ensemble level ~1.2 full / ~1.5 bench); the v3
   filters' improvement is clock-robust, the LEVEL is not; (b) the daily
   frame's forward-return requirement is an implicit look-ahead that
   exits names 24h before their final bar (~+0.13 Sharpe, flips 2022);
   (c) a print-clock variant knife-catches in 2026 (−94 bp/day era) —
   parked without a persistence mechanism.
4. **Data**: research refresh completed (both venues through 2026-07-27);
   the Binance rmom append-overlap guard fired and was run to ground —
   stored artifact irreproducible from byte-identical inputs by any code
   vintage (pre-M9 duplicate-sweeping read at write time is the leading
   explanation); both rmom artifacts force-rewritten per the guard's own
   instructions (496,685 / 467,525 rows), unifying on the post-M21
   definition. Panel rebuilt 2021→2026-07-27; ledger history verified
   byte-stable; forward ledger live with v3 and both paired differentials.

### 2026-07-28 — wave 3: `lane2_funding_spread_v1`; the funding-carry program

Review §10. Change point: this commit. Five lines:

1. **What**: the same crowded-short premium captured market-neutrally —
   long the perp where funding is more negative, short the same symbol's
   perp on the other venue; hysteresis on the trailing settled spread
   (enter |80| bp/day, exit |20|), both legs' fees charged. New mechanism,
   absent from both negative ledgers.
2. **Seen-data**: 5.1 bp/day, t 2.92, Sharpe 1.34 full / 1.61 bench, max
   DD −16.7%, one-way 0.087/day; offset-STABLE (1.10–1.28 across clocks);
   corr +0.09 with carry_hold_v3. Basis risk measured honestly: the price
   legs' difference runs 677 bp/day sd exactly when the spread is wide.
3. **The program**: PIT 60d vol-parity over {carry_hold_v3, funding_spread}
   measures bench-window Sharpe 2.34 / DD −11.2% / MAR 5.07 at the
   standard clock, 1.93–2.34 across clocks; full-window 1.55–1.87. The
   owner's Sharpe ≥ 2 target is met on the program's standard quote basis
   robustly across clocks, NOT met on the strictest basis (full window,
   worst clock). Both numbers are the claim.
4. **Evidence status**: Lane-1 selection; the spread book's forward run
   plus the reconstructable combination grade it. Ledger scores it daily.
5. **Feasibility notes for any future sizing**: two-venue margin (~2× per
   unit), leg-execution asynchrony unmodelled, 2023-24 eras ~zero (the
   book goes dormant when funding normalizes).

### 2026-07-27 — recorded change points from the repo-wide audit remediation

Three fixes from `docs/audit/2026-07-27-repo-wide-multi-agent-audit.md` change
numbers rather than only correctness, so they are change points, not refactors.
All three were owner-approved on 2026-07-27 before landing. None is deployed;
the rollout dispatch belongs to the owner.

- **CONTINUOUS live entry behaviour — crowding counting base (audit M2).**
  The live sleeve counted the `entry_crowding_max_fresh` cap on the
  age-filtered, funding-admitted frame; the equity-evidence engine counts it on
  its funding-admitted **fresh entrants**, *before* the per-entry age gate
  (`_run_trades` checks crowding at the top of the loop and the age gate ~15
  lines later). With crowd cap 2 and age floor 240 d, an hour where two old
  pumps share a signal timestamp with one young pump was counted 3 by the engine
  (skip the whole stack) and 2 live (enter both) — the live book took entries
  the validated engine crowd-skipped, in exactly the fresh-listing-squeeze hours
  both gates exist for. The live base now mirrors the engine, which required
  carrying the previous confirmed bar's decile/trigger columns beside the
  deciding bar so `_fresh_entries` can be reproduced. **This is a live
  entry-behaviour change**: it can only ever skip *more* entries than before,
  never fewer. Where the previous bar's state is unavailable the fallback treats
  every row as fresh, which over-counts the crowd — the conservative direction
  for a gate whose purpose is refusing squeeze windows. Change point: this
  commit. The forward CONTINUOUS record is continuous across it; entry counts in
  crowded hours are not comparable to days before it.
- **Lane-2 financed-longs scoring — flat-day turnover (audit M19).** The
  full-calendar correction recorded in `docs/research_2026-07-26_financed_longs.md`
  on 2026-07-26 lived only in that note: `daily_scores` still iterated the bars
  present in `weights`, so the documented reproduction command printed the
  active-days-only view and contradicted the registered table, and a flat
  gate-flip day charged neither the exit into it nor the re-entry out of it.
  `daily_scores` now iterates every decision bar in the record. Re-scored on
  today's panel: Sharpe raw 2.56 / 2.21 / 1.66 and t 4.69 / 4.04 / 3.03 on the
  bench window — the registered table, reproduced by the registered script for
  the first time. No verdict moves. The three full-sample t-values that were
  still quoted on the old basis (4.88 / 4.04 / 2.79) are corrected to
  4.87 / 4.01 / 2.77. Change point: this commit, for the *scorer*; the three
  registrations and their commit dates are untouched. *(2026-07-28: these
  reproduced values themselves carried the settlement double-count — see the
  2026-07-28 correction section above; corrected verdicts supersede them.)*
- **Residual momentum — registered calendar window (audit M21).**
  `residual_momentum_expr` is row-positional, so `rolling_sum(7).shift(3)`
  reached the 10th *present* row and spanned more than ten calendar days for a
  gapped symbol (delist/relist, archive hole, dropped factor day) — no
  causality violation, but not the registered `sum(residual_return[D-9..D-3])`.
  The residual frame is now densified to a per-symbol contiguous daily grid
  first. **This rewrites history for gapped symbols rather than appending to
  it.** The deployed daily refresh is unaffected: `run_continuous_rmom_refresh.sh`
  already passes `--full-rewrite` because live roots are rolling stores. On a
  stable research root the append overlap verify fires once, by design, with a
  message that now distinguishes a deliberate definition change from source
  drift; rerun that root with `--full-rewrite`. Change point: this commit.

### 2026-07-26 — the financed-longs program

**Superseded 2026-07-28: the scorer double-counted every sub-daily funding
settlement; corrected, no config beats the benchmark on Sharpe — see the
2026-07-28 correction section above. The registration receipts below stand
as written; their funding-dependent numbers are inflated.**

An owner-directed one-day program (goal: three alphas that beat the deployed
CONTINUOUS system on return and Sharpe at full measured costs) ran ~18 new
mechanism families through the honest harness and registered three configs.
Full evidence note, including the 22-row negative-results ledger:
`docs/research_2026-07-26_financed_longs.md`.

- **Benchmark regenerated from primary artifacts**: CONTINUOUS sl35 render,
  2023-03-13→2026-07-16: Sharpe 1.84, +15.85%, max DD −2.85%
  (`equity_curves_sl35_2026-07-26`).
- **Registered (commit = registration), scored at measured turnover ×
  7.78 bp/side with settlement-exact funding. Corrected same day to the
  full-calendar basis (flat days = 0, matching the benchmark's own
  accounting): the two Bybit books beat the benchmark on return AND Sharpe
  on both raw and vol-targeted bases; the Binance arm beats on return only
  (Sharpe 1.66 vs 1.84) and stands as the replication arm:**
  - `lane2_carry_hold_v1` — long top-100 names while settled funding < −10
    bp/8h (exit > −3 bp), cap 0.10/name, gross cap 1.0. Bench full-calendar
    Sharpe 2.57 raw / 2.41 vt, t 4.69. Attribution: +13.06 units funding vs
    −3.86 price — a carry payment with a named counterparty (crowded
    shorts). First positive mechanism in this program to survive cross-venue
    replication (Binance ratio 0.50).
  - `lane2_financed_leaders_v1` — top 1-week-momentum decile admitted only
    while the name's funding ≤ 0 and BTC 30d > −0.05. Bench full-calendar
    Sharpe 2.21 raw / 1.87 vt, t 4.05. The financing condition is the alpha:
    without it the same book bleeds −61 bp/day in 2022; the funding-cap
    curve is monotone.
  - `lane2_financed_leaders_binance_v1` — the Binance-native replication arm
    (ratio 0.65; bench full-calendar 1.66 raw — return beats, Sharpe does
    not), registered so the forward record grades both arms symmetrically.
    corr +0.82 with the Bybit arm; explicitly a replication object, not an
    independent discovery.
- **Family disclosure**: the two Bybit books correlate +0.75 (41% name-day
  overlap) — one macro-premium (the market pays longs while shorts are paying
  funding) harvested in two phases. Portfolio construction over them must use
  the measured correlations.
- **Structural negatives with standing value**: every short-side construction
  fails (the payment is always on the long side of forced flow); nothing
  intraday survives the measured round trip (the one "settlement-clock"
  effect was a conditioning artifact caught by the prior-stamp PIT re-run);
  crash-absorption is real but sub-bar and Bybit-local; the OI-purge
  conditioning from the cascade literature does not discriminate on this
  panel.
- These are Lane-1 selections; their forward records begin at the
  registration commit. The deployed sleeves are unchanged, remain the
  controls, and promotion of any financed-longs config requires its own
  rolling record and five-line note per `docs/governance.md`.

### 2026-07-26 — CONTINUOUS replaced with the single funding-gated cell (operator override)

The deployed three-component CONTINUOUS ensemble was replaced by the redesign's
V3 shape — one `turn3_pop3` cell (age 240d, TP 12%, declared stop 35%, weight
1.0) plus the settled-funding admission `funding_min_at_entry = 0.0` ("only
fade pumps whose longs are paying"; last settled print at-or-before the
signal-bar close, never a predicted rate; unknown funding admits and is
counted/journaled). Evidence basis: `docs/continuous_redesign_2026-07-26.md`.

**Promotion note (recorded change point):**

```text
Claim: the single funding-gated turn3_pop3 cell preserves most of the deployed
  CONTINUOUS return at materially better drawdown/MAR with two fewer hand-tuned
  parameter sets; the funding admission is the mechanism (V3 vs V0/V1/V8).
Config commit: this commit — profile revision active_single_fund0_tp12_sl35_v1
  (the commit is the registration; the sleeve's forward run restarts here).
Forward record (days, net delta vs baseline, tail behavior): none yet — Lane-1
  seen-data evidence only; the deployed ensemble's record ends at this change
  point and the new revision accrues from it.
Decision: operator override 2026-07-26, replacing rolling-record promotion
  (overrides the register-first recommendation in the redesign note §3).
Date: 2026-07-26.
```

**The honest render reconciliation — the shipped rule is stricter than the
research render that produced the decision numbers.** The research admission
used the cross-venue panel's funding column; 28% of fresh entries (1,494 of
5,401) had no panel row at all — 92% of those from 147 Bybit-only contracts
outside the both-venue panel universe — and were admitted blind as "unknown".
The engine's root funding dataset covers essentially all of them, so the
shipped rule rejects 434 additional known-negative entries (1,834 rejected
total vs the research's 1,400; zero unknowns historically). Line-item
decomposition: 650 of the research's 704 trades reproduce byte-identically
(+9.16% shared net on both sides); the 54 excluded trades carried +2.30%
in the seen window; 5 new chain-effect trades ≈ 0. All hedged, full-calendar,
2023-03 → 2026-07, standard render:

| book | trades | total | maxDD | Sharpe | MAR |
| --- | ---: | ---: | ---: | ---: | ---: |
| deployed 3-cell ensemble (old baseline) | 2,372 | +15.85% | −2.85% | 1.84 | 1.66 |
| research V3 (blind-admits included) | 704 | +13.72% | −1.69% | 1.72 | 2.43 |
| **shipped single_fund0 (this commit)** | **655** | **+11.06%** | **−1.84%** | **1.45** | **1.80** |

Stated plainly: versus the deployed ensemble the shipped shape still wins on
drawdown (−1.84% vs −2.85%) and MAR (1.80 vs 1.66) — the side of the trade the
operator chose — but the Sharpe concession is 1.84 → 1.45 and the return
concession −4.79 pp, both larger than the −0.12 / −2.13 pp the redesign table
showed, because that table's V3 row embedded the blind admissions. The
excluded population (negative-funding Bybit-only pumps) was *profitable* in
the seen window, which cuts against the funding thesis on that sub-population;
54 trades on seen data decide nothing. The forward record arbitrates. (Both
named follow-ups were run 2026-07-27 — see the next section: unknown-admits
is empty on the root basis, and the venue-scoped admission variant is now
the registered lead.)

Deliberate identity consequence recorded with the change point: the new
`ContinuousEventConfig` field shifts `config_hash()` / `kernel_strategy_id`
for every CONTINUOUS config, and the cycle-status funnel schema is v2; the
sizer's authoritative-chain self-heal (`ddbded5`) is expected to rebase
prior-epoch state on the first post-deploy cycles. The sleeve kill criteria
(`docs/preregistration/sleeve_kill_criteria_2026-07-20.md`) continue to govern
the sleeve unchanged. Rollout remains owner-dispatched; this commit deploys
nothing.

### 2026-07-27 — ladder mechanism decomposed; admission variants; one registered lead

Owner-directed follow-up on the replacement ("why did the 3-cell ensemble
work — was it a gradual scale-in/TWAP — and what is barebones single_fund0
missing?"). Full evidence, all cells and negatives:
`docs/continuous_ladder_mechanism_2026-07-27.md`; reproduction/scorer:
`scripts/render_continuous_admission_variants.py`. Lane-1 on seen data.

- **The TWAP/scale-in story is refuted twice.** The nested triggers fired in
  the same hour (median rung lag 0.0h; 81% of base pumps were already
  pop5-grade at entry) — the retired ensemble was an *amplitude* weighting,
  not a time ladder. Forcing a real time ladder (3 tranches at delay 1/2/3h)
  loses return: later entries are strictly worse (+9.21% → +6.06% → +8.07%
  additive by tranche).
- **The retired book's fc-Sharpe edge fully decomposes** into the funding
  bill it kept paying (the admission is a cost filter, not selection —
  gross-only Sharpe identical), a long-BTC/ETH hedge overlay that is gated
  beta (+2.3–2.6 pp in this uptrend-gated window), an impact-slicing
  modeling artifact (~+0.1 bp/day), and calendar density (646 vs 554 active
  days). At the shipped cell's active-day quality, fc parity with 1.84 needs
  ≈832 active days — unreachable inside the funding-gated shape. The shape
  trades calendar density for drawdown/MAR; that remains the operator's
  chosen side.
- **The amplitude ladder is dead under the funding admission** (rung order
  inverts; every reweighting loses to the plain single cell) — the strict
  rungs were largely a funding proxy.
- **Closed empty: the unknown-admits follow-up.** On root funding there are
  zero historical unknowns and the 240-day age gate makes runtime unknowns
  near-impossible; reject-unknown reproduces the shipped book
  trade-for-trade.
- **New negative rows** (do not re-test without new mechanism/data/defect):
  crowd-3 and crowd-off (monotone worse — crowd-2 blocks toxic density),
  hold-12 (destroys the edge; also deprioritizes a cooldown-only engine
  field), hold-48 (buys density at −3.18% maxDD), TWAP tranches, fund0
  ladders at all tested weights, V9/V10 standalone.
- **Registered lead (commit = registration): venue-scoped admission** — the
  funding floor applies only to both-venue symbols; Bybit-only contracts
  admit regardless of funding sign. Hedged +12.76% / −1.75% / MAR 2.09 /
  fc 1.61 vs shipped-shape base +11.39% / −1.84% / 1.78 / 1.49 — the only
  variant beating the base on every aggregate axis. Unspun: the delta is 43
  trades and the aggregate win is carried by 2025 (loses in 2024 and 2026);
  mechanism is coherent (the toxic signature is the *both-venue* crowded
  short; 2025 is when negative funding became ubiquitous on Bybit-only
  names). Forward scoring: re-run the script with a later `--end-date`,
  venue-scoped vs base, on post-commit days. The deployed profile is
  untouched; promotion would need a deliberate admission-scope engine field
  (identity-shifting), a frozen both-venue registry, and the five-line note.

## What survived the audit

| Evidence | Decision-useful conclusion | Decision |
| --- | --- | --- |
| Strategy Overhaul V2 | About 29 families and more than 150 configurations exhausted the existing hourly entry/exit/sizing surface. Fixed-capital barebones books were approximately -3.23% LONG and -20.23% CONTINUOUS after modeled costs and funding. Full account parity was not established. | Stop tuning descendants of that surface. |
| Historical sleeve curves | Some historical curves are positive, but LONG is materially dependent on a small take-profit tail and CONTINUOUS does not have complete live-runtime reconstruction. | Keep as descriptive controls, not promotion evidence. |
| Breadth study | CONTINUOUS increased from about 6.55 to 7.30 bets per open day, but per-bet volatility was about 1,000 bp and average dependence about 0.21. A 25 bp effect would need roughly 5.6 years at that information rate. | Breadth alone is not a research direction. Fix quantization only as an execution-validity issue. |
| Young-listing lifecycle | The 2021-24 unconditional short effect reversed in 2025-26. A day-0 long was negative or flat. The required listing-week 1-minute cost data had zero symbol/date overlap with the 27,398-row event panel. | Retire calendar-age rules and the proposed T-L v2. |
| Execution cost | The first 23 measured demo fills showed positive 15-second/1-minute realized spread against our taker flow. The paper maker-first A/B is running toward 100 fills per arm. | Continue measuring execution separately; do not confuse cost improvement with alpha. |
| Cross-venue follow-ups merged 2026-07-21 | A Bybit turnover-collapse listing short looked strong by era (+247/+246/+510 bp at day 2) but failed in every Binance era (-415/-41/-290 bp). Hedged extreme-funding carry was negative across every declared arm on both venues. Naive pump-event longs were negative in 23 of 24 venue/era cells; D9 and BTC-uptrend short-path differences were only about +26 to +62 bp and uncertain. | Preserve venue divergence, the post-2025 negative-funding explosion, and the small D9/uptrend directional effect as anomaly leads. Retire the fixed admission bars, bulk reports, and one-off runners. |
| Book-level overlay follow-ups | A monotone BTC-risk intensity bought roughly 19-33% tail relief for about 3.8 percentage points/year of net premium on the deployed-shape render. A realized daily loss budget helped mainly on the negative barebones surface, while a cluster cap never bound the deployed-shape book. | These are priced, regime-dependent insurance diagnostics—not automatic governors. Retire the staged hardcoded implementations; revisit through open anomaly research if new evidence warrants it. |

The old reserved V2 label tape was not opened. It is not earmarked for the new
program: a descendant would inherit too much design exposure, while a genuinely
new strategy is better graded on post-commit days.

## Reset research read

All work in this section is Lane-1 exploration on already-seen local data. It
shaped the new plan and cannot grade it.

### Young listings: turnover decay was the only interesting lead

At event day 2, six rules were declared from price extension, turnover
retention, and already-settled funding before their day-2-to-day-7 outcomes were
read. Trades used actual hold-period funding, 100 bp round-trip cost, and a
listing-month block bootstrap.

| Rule | N | Mean net | Median net | 95% block CI |
| --- | ---: | ---: | ---: | ---: |
| Short every listing | 896 | -59 bp | +460 bp | -468 to +274 bp |
| Short when turnover retention is below 0.5 | 243 | +348 bp | +493 bp | +66 to +606 bp |
| Short pumped-and-decayed listings | 5 | -1,111 bp | -434 bp | -3,119 to +630 bp |
| Short crowded/decaying listings | 28 | -4,580 bp | +581 bp | -15,462 to +404 bp |
| Long pumped listings with persistent turnover | 98 | -722 bp | -1,015 bp | -1,341 to -105 bp |

The turnover-decay short was positive in aggregate but had only nine 2021-22
observations and each era-specific interval crossed zero. It is a mechanism
lead, not a candidate. Persistent-attention continuation was directly refuted.

### Mature symbols: the simple mechanism did not generalize

The same idea was then falsified on the canonical Bybit daily panel
(`2022-01-03` through `2026-07-03`, 889 symbols). Signals required 240 observed
days, at least 12 million USDT daily turnover, exact daily continuity, entry at
the next daily close, exit five days later, and at least seven days between
signals for a symbol. This screen includes price and round-trip cost but not
funding, so it is optimistic for a short strategy.

| Rule | Cost | N | Full mean | 2023-24 mean | 2025-26 mean | 95% block CI, full |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Short turnover decay | 100 bp | 5,729 | -45 bp | -158 bp | +84 bp | -181 to +86 bp |
| Short turnover decay | 200 bp | 5,729 | -145 bp | -258 bp | -16 bp | -279 to -16 bp |
| Short pumped + decayed | 100 bp | 241 | -170 bp | -774 bp | +298 bp | -916 to +475 bp |
| Long pumped + persistent | 100 bp | 5,171 | -119 bp | +71 bp | -312 bp | -320 to +86 bp |
| Long pumped + persistent | 200 bp | 5,171 | -219 bp | -29 bp | -412 bp | -422 to -11 bp |

Conclusion: price extension, listing age, and turnover retention are context,
not a standalone signal. Their pooled medians hide severe era dependence.

## Starting hypothesis, not mandated direction: Crowding Transfer

The first promising question changes the object being predicted. Instead of
asking whether a coin that pumped will continue or reverse, it asks whether
leveraged demand is moving into or out of **Bybit relative to the broader
market**. This is a place to start, not a prescribed destination. Research may
falsify it, split it into narrower mechanisms, or replace it with a more
interesting anomaly discovered in the data.

### Mechanism

1. Ask whether causal Bybit-minus-Binance premium, settled funding, or price
   basis describes local crowding better as a level, change, acceleration, or
   disagreement among measures.
2. Ask whether open-interest, taker-flow, turnover, or price transitions lead
   or lag that crowding, and whether the answer changes by regime.
3. Test whether any apparent effect survives removal of BTC/ETH beta and common
   cross-sectional moves; do not assume the correct trade is a naked short.
4. Study long and short asymmetries independently. Do not force symmetry or a
   matched pair when the data suggests only one side is interesting.
5. Treat response horizon and normalization behavior as research surfaces.
   Exchange-native disaster protection remains an account safety layer, not an
   alpha parameter to mine.

If supported, this family would differ from both active sleeves: the signal
would be a cross-venue state change, the portfolio could be beta-controlled,
and the trade would not require calendar age, a pump threshold, or broad market
direction.

### Feasibility already checked

Re-measured 2026-07-24; the earlier taker-flow line was materially wrong and is
corrected here. Full detail in `docs/audit/2026-07-24-repo-and-strategy-audit.md`.

- Bybit hourly premium, funding, index, mark, and open-interest partitions span
  `2021-01-01` through `2026-07-17`. **Bybit open interest is the deepest
  unused asset: 2,024 daily partitions growing 6 → 636 symbols.**
- Binance hourly premium and funding span late 2019 through `2026-07-17`.
- Common-symbol counts on the latest partition: 579 klines, 566 premium, 466
  funding; 466 symbols carry both-venue kline + funding + premium together.
- **Bybit `taker_flow_5m` and `tick_ohlc_1m` are not panels.** They hold 401
  distinct symbols but a *median of 11 days each* (min 1, max 78), scattered
  `2023-03-29` through `2026-05-24` — event windows, not cross-sectional
  coverage. `positioning_lsr` and `binance_usdm_metrics_5m` are empty.
- Binance OI and taker flow are wide but shallow: ~637/658 symbols over only
  70/67 days from `2026-04-27`.
- **Consequence:** cross-venue microstructure research is not currently
  possible. Long-history work must use price, premium, funding, basis, and
  Bybit OI. Design for that surface rather than assuming flow data exists.

## Proper work plan

### Research selection policy — no hardcoded performance gates

Lane-1 research has no universal Sharpe, return, trade-count, cost, era-sign,
or configuration-count hurdle. Those are properties to measure, not laws. An
anomaly is interesting when it is surprising, economically interpretable,
stable somewhere important, sharply regime-specific, useful for explaining a
known failure, or revealing of a data/execution artifact. Negative, inverted,
and conditional effects count as discoveries.

Choose follow-ups by expected information gain, mechanism plausibility,
effect-size shape, uncertainty, concentration, executable economics, and how
different the idea is from spent work. Record the judgment. Do not turn it into
a numeric pass/fail formula after the fact.

The hard boundaries are evidence physics: causal availability, honest
population/PIT scope, missingness, executable fills/costs/funding for a
performance claim, reconstructable accounting, and provenance. A violation
changes what the result can mean; it does not make the diagnostic useless.
Runtime and real-money safety boundaries remain unchanged.

### P0 — minimal causal research substrate

Build the smallest reusable panel that can answer the first questions, not
another family of bespoke report scripts or a months-long infrastructure
project.

- Exact symbol mapping with collisions and contract differences rejected.
- Decision time, source publication/availability time, a claim-appropriate
  execution delay, and no backward fill across missing venue data.
- Bybit/Binance price, mark, index, premium, settled funding, turnover, and the
  available OI/taker fields; every field carries a coverage flag.
- Manifest with Git/config/data hashes, date and population bounds, coverage by
  venue/year, and all exclusions.
- If common-population coverage or timing cannot support a proposed claim,
  narrow or relabel that claim and preserve the gap as an anomaly. A root name
  is not evidence.

Deliverables: a reusable cross-venue panel builder, focused synthetic
timing/mapping tests, and one compact manifest. Get to a first anomaly read
quickly; add fields only when a live research question requires them.

### P1 — anomaly atlas

Explore freely on already-seen data and keep an honest search log. Start with,
but do not limit the work to:

1. venue lead/lag, premium/funding/basis disagreement, and convergence paths;
2. price/OI/taker-flow divergences and transitions rather than static levels;
3. capital transfer between symbols, clusters, and venues;
4. funding-clock, time-of-week, volatility, liquidity, and market-regime
   asymmetries;
5. anomalies in what the active sleeves admit, reject, miss, or lose money on;
6. sign-inverted, time-shifted, and venue-local controls that expose artifacts;
7. unexpected data gaps, contract-lifecycle behavior, or microstructure effects
   that may be more valuable than the intended signal.

For every useful read, show the complete tested surface and enough time,
symbol, cluster, and regime decomposition to reveal instability. Put gross
next to actual or claim-appropriate stressed costs and funding. Report effect
size, uncertainty, concentration, turnover, capacity, common-factor exposure,
and missingness as continuous evidence rather than reducing them to pass/fail.

Maintain a compact anomaly catalog: observation, why it is interesting,
plausible mechanism, data touched, strongest artifact explanation, economic
shape, and the next discriminating test. Follow as many leads as remain
decision-useful; retire only duplicated plumbing and questions that no longer
teach anything.

### P2 — deepen the most informative anomalies

For leads that imply a tradable claim:

- try to disprove the proposed mechanism with timing, venue-local, sign,
  universe, and common-factor controls;
- compare sensible unhedged and hedged expressions without assuming one is
  preferred;
- replay through the account journal and venue rules when the claim reaches
  portfolio P&L;
- attribute gross, funding, fees, spread, impact, hedge P&L, residual beta,
  missed trades, and tail concentration;
- separate an unavailable live feature or optimistic cross-venue fill from a
  genuinely executable paper design.

Several anomalies may remain alive. The output is a better map of the market,
not an artificially forced winner.

### P3 — rolling forward grade

When a formulation becomes worth grading, commit its exact config and scorer
before the first new day; that commit is the registration. Append one row per
new day. Grade only post-commit decisions and keep mechanics-only days
separate. Multiple distinct formulations may accumulate their own honest
records. The existing LONG/CONTINUOUS sleeves remain controls and are not
modified to help a challenger.

Promotion requires the five-line note in `docs/governance.md`, a recorded
change point, stable paper execution, no sleeve kill-rule breach, and an
explicit replacement/migration diff. Promotion means demo only. Mainnet still
requires a separate owner instruction naming the deployment and risk boundary.

### P4 — directions remain open

Crowding Transfer is one starting family, not a gate around creativity.
Price-independent funding/premium carry, cross-sectional transfer, execution
reversion, regime-conditioned sleeve redesign, or a mechanism not anticipated
here may be better. Revisit an old family only with a new mechanism, new data,
or a corrected defect—not another threshold sweep wearing a new name. True
cross-exchange execution is a new capability and stays simulation/paper-only
until both legs, atomic failure handling, collateral fragmentation,
liquidation, transfer, and venue-outage risk are modeled and deliberately
authorized.

## Live task queue

**Sequenced plan: `docs/roadmap_2026-07-25.md`.** That document supersedes the
ordering of this list. The measured position it starts from: ~44 mechanisms
tested means the corrected significance threshold is t = 3.25, and our best
signals are t 1.30-2.06 at the measured 15.56 bp round trip. There is no
validated edge yet, and execution work cannot create one (its ceiling is
Sharpe 0.69 -> ~1.17). Completed items below are retained as the evidence trail.


- [x] Collapse old evidence into decision-useful priors.
- [x] Falsify simple young-listing continuation and mature turnover-decay rules.
- [x] Verify a viable long-history cross-venue premium/funding overlap.
- [x] Build the minimal P0 causal substrate and publish its coverage map.
      `liquidity_migration/cross_venue_panel.py` +
      `scripts/build_cross_venue_panel.py`, built 2026-07-24 over the
      both-venue population from `2021-01-01`. Coverage lives in each shard's
      `manifest.json`; two source defects it exposed are recorded in
      `docs/audit/2026-07-24-repo-and-strategy-audit.md`.
- [x] Produce the P1 anomaly search with the full log, and consolidate it.
      `docs/anomaly_research_2026-07-24.md` — 37 mechanisms tested identically.
      Survivors are cross-venue premium divergence and 1-week cross-sectional
      momentum, both concentrated in the *most* liquid names and effectively
      uncorrelated (+0.009). Funding carry broke in 2025-26 exactly when funding
      inverted. The 24h-display rollover is a confirmed mechanism that does not
      pay. The edge is non-monotone — essentially all of it is the short leg. Venue
      volume-share migration — the most direct test of the Crowding Transfer
      starting hypothesis below — is dead; the price dislocation pays, the flow
      migration does not. Scoring primitives are
      `liquidity_migration/cross_section.py`.
- [x] Withdraw the delisting-decay lead. The 220.8 bp/day figure used a
      look-ahead label (contract stops appearing). No point-in-time trigger
      reaches it: turnover collapse identifies dying contracts at **0.96× lift**,
      and the same trigger pays *more* on contracts that never died (+38.0 bp,
      t 4.26), so the residual is generic "short low-turnover", not delisting.
      No announcement-lead-time check can rescue it.
- [x] Withdraw the weekly-horizon recommendation. The rising t-stat was an
      overlap artifact; under disjoint sampling t peaks at 24h (3.48) and falls
      to 1.18 at 168h. Hold 24h.
- [x] Settlement-exact funding replay. Charging funding only at settlements
      inside the hold (not `rate × hours/8`) **reverses the leg attribution**:
      premium 33.63→16.55 bp, momentum 16.98→35.42 bp, blend unchanged at ~26.
      The blend is robust to the funding treatment; the legs are not.
- [x] Withdraw the dispersion gate. Under settlement-exact funding it gives
      Sharpe 1.30 vs 1.29 ungated and a *worse* compounded drawdown (51.6% vs
      46.1%). It was an artifact of the funding approximation.
- [x] Compounded accounting and volatility target. The blend was never near
      liquidation — worst day −29.17%, no day below −50%; the >100% drawdowns in
      the earlier caveat were single legs, not the blend. A 15% annual vol target
      (cap 3×) lifts Sharpe 1.24→1.59 and cuts compounded drawdown 46%→13.6%.
- [x] Decompose `premium_diff` by venue. Net of each venue's own settlement-exact
      funding, **Bybit carries the return** (23.81 bp, t 2.06 at 24h) and Binance
      does not (11.42 bp, t 1.01); adding a Binance leg dilutes to 17.62. The
      effect is Bybit-local, so **true cross-venue execution is not worth building
      for this signal**. Caveat: the premium leg is marginal and clears t = 2 only
      at 24h.
- [x] **Lane-2 registration**: `configs/lane2_premium_momentum_blend_v1.json`,
      executable as `liquidity_migration/lane2_blend.py`. Daily, top-100 Bybit,
      50/50 premium + 1-week momentum continuation, settlement-exact funding, 15% vol
      target; no dispersion gate, no Binance leg, no maturity filter. Per
      `docs/governance.md` the commit is the registration; it grades forward from
      that commit on days it never saw.
- [x] **2026-07-26 financed-longs program**: three Lane-2 registrations
      (`lane2_carry_hold_v1`, `lane2_financed_leaders_v1`,
      `lane2_financed_leaders_binance_v1`) against the regenerated CONTINUOUS
      sl35 benchmark (Sharpe 1.84, +15.85%) at measured costs. On the
      full-calendar basis the two Bybit books beat it on return AND Sharpe; the
      Binance replication arm beats on return only (Sharpe 1.66 vs 1.84) — see
      the registration block above, which has said so since the same-day
      correction. Module `liquidity_migration/financed_longs.py`, reproduction
      `scripts/screen_financed_longs.py` (reproduces the registered table
      directly since the 2026-07-27 M19 turnover fix), evidence
      `docs/research_2026-07-26_financed_longs.md` with the 22-row
      negative-results ledger.
- [ ] Score the five financed-longs configs on each new completed UTC day
      (rolling forward record; the registration commit is the change point;
      since 2026-07-28 the scorer charges each settlement exactly once;
      `lane2_carry_hold_v2`/`v3` score beside v1 with the paired daily
      differentials v2−v1 and v3−v2 as the primary comparisons). Tooling:
      `scripts/score_financed_longs_forward.py` appends
      `reports/financed_longs_forward/ledger.csv` (append-first, idempotent,
      `forward_eligible` flagged). Panel refreshed through 2026-07-27 on
      2026-07-28; the daily sequence is research-refresh → panel 2026
      rebuild (`scripts/build_cross_venue_panel.py --start 2021-01-01`,
      full rebuild — the index is whole-file) → ledger append.
- [ ] Re-derive the settlement-exact surfaces on the corrected scorer:
      the `lane2_premium_momentum_blend_v1` table, the anomaly-research
      funding-leg numbers (leg-attribution reversal, dispersion-gate
      withdrawal), and financed-longs negative-ledger rows 1/2/13–17/20
      (2026-07-28 double-count correction).
- [ ] Score the venue-scoped CONTINUOUS admission variant beside the shipped
      shape on post-2026-07-27 days
      (`scripts/render_continuous_admission_variants.py admission --end-date …`;
      registered 2026-07-27, evidence
      `docs/continuous_ladder_mechanism_2026-07-27.md` §5 — the 2025-carried
      era profile is the thing the forward record must test).
- [ ] Read the paper passive-execution A/B for realised maker-fill probability
      (target was 100 fills per arm). This is the last unmeasured cost input and
      needs VPS data.
- [ ] Run `scripts/check_kill_criteria.py` against the deployed sleeves. Needs
      the canonical account journal, which is VPS-only.
- [ ] Orthogonalise `basis` against `premium_diff` — they are one family and
      should not be double-counted.

No other strategy task list is active.
