# Strategy program — change log through 2026-08-03

Decanted verbatim from `docs/research/strategy_program.md` on 2026-08-03 so the
active document stays small. Paths inside reflect the docs layout at each
entry's date. The active queue and current truth live in
[`../strategy_program.md`](../strategy_program.md); evidence policy in
[`../governance.md`](../governance.md).

## Program phase record (2026-07-25, originally in "Current truth")

- **The instrument-repair phase is complete (2026-07-25); see
  `docs/archive/2026-07-24-anomaly-research.md` §16.** Three results change the position:
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
  - **Gate 1: 0 of 12 cells clear t ≥ 3.25.** No further sweeps were run.
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

## Dated change points (2026-07-26 .. 2026-08-03)

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
  `docs/archive/2026-07-26-financed-longs.md` §0; reproduction output
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
  `docs/archive/2026-07-26-financed-longs.md` — the only citable
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

Full review: `docs/archive/2026-07-28-carry-hold-quant-review.md`
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

Full tables: `docs/archive/2026-07-28-carry-hold-quant-review.md` §9.
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

### 2026-07-28 — owner promotion: `lane2_carry_hold_v3` is the carry-family lead

Owner decision, same day as registration. Change point: this commit. Five lines:

1. **What**: `lane2_carry_hold_v3` is promoted to LEAD config of the
   carry-hold family — the reference configuration for any future
   implementation work (runtime design per `docs/carry_hold.md` §7).
2. **Why**: owner selection after the 2026-07-28 quant review; seen-data
   basis Sharpe 1.38 / MAR 2.84 / max DD −28.7% vs v1's 1.02/0.97/−60.0%,
   with the review's honesty notes attached (clock luck, terminal-day
   frame, conditional-regime characterization).
3. **What changes operationally**: nothing live — the book has NO runtime,
   no venue access, and this promotion creates none. v1/v2 keep scoring;
   the ledger and paired differentials continue unchanged.
4. **Evidence**: full trade diagnostics at
   `reports/carry_hold_v3_trade_diagnostics_2026-07-28/` (1,670 trades,
   replica validated bar-identical to the registered scorer).
5. **Boundary**: demo/paper would require the §7 runtime build and its own
   owner dispatch.

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

### 2026-07-28 — loser anatomy on the v3 ledger; the stop family is closed

Review §11 (owner follow-up; no config change; Lane-1 seen data on the
registered v3 record). Five lines:

1. **What**: 60.1% of v3 trades lose, but losses are events, not
   processes — one daily candle carries ≥ half the loss in 98% of losers,
   and conditional on a ≥10% day-1/day-2 drawdown the REST of the trade
   still earns +1.3%/+2.1%. Losers' entry fingerprint (deep print, high
   vol, pumped ret3d) is the winners' fingerprint; the ret3d ≥ +50% bucket
   is median −6.4% but the largest profit pool (+187% book).
2. **Intraday stop grid** (hourly closes, −10…−30%, both fill
   conventions, spell-dead, settlement-exact partial funding, exact deltas
   on the registered series): EVERY cell worse on mean, Sharpe, max DD and
   MAR — e.g. −15% next-bar: 19.83→14.78 bp/d, Sharpe 1.376→1.149, DD
   −31%→−53%. Stops win the median stopped trade (+0.5 bp) and lose the
   mean (−23 bp): they sell the right tail, and they bleed worst exactly
   in the paying eras (2026: 64.7→35.7 bp/d).
3. **Durable negative**: reaction-based loser identification (any
   price-drawdown stop, daily or hourly, with or without re-entry) is
   refuted for this book. Do not re-propose without a new mechanism.
4. **Still-open doors**: same-symbol cross-venue funding confirmation at
   entry; suspend→hard-exit (suspension-touched trades average −6.5%);
   toxic-band hi→0 sliver (−11% book); turnover-rank-decay dropout
   warning (`open_at_series_end` cohort −16% book). OI stays banned.
5. **Artifacts**: `losers_early_id_diagnostics.txt`,
   `intraday_stop_grid.txt` in
   `reports/carry_hold_v3_trade_diagnostics_2026-07-28/`.

### 2026-07-29 — owner override: CONTINUOUS retired; CARRY sleeve deployed

Change points: commit `6331222` (retirement) and the CARRY build commit that
follows it; two guarded rollouts, receipts in `STATE.md`. Five lines:

1. **What**: by explicit owner instruction ("depromote the continuous strat
   from demo and paper, and replace it with this one"), the CONTINUOUS
   sleeve is retired from BOTH fleets (`sleeves.env` off/off) and a new CARRY
   sleeve deploys the promoted `lane2_carry_hold_v3` on demo + paper.
2. **How**: the producer replays the registered scorer's own functions on a
   90-day live frame (`prepare_decision`; parity vs the research frame over
   the last 90 days: identical on every shared bar, differing only at the
   decision bar the research frame cannot see); daily decision at 00:00
   UTC computed ~00:20; declared stop 0.35, no TP; sizing w × equity × 1.0
   under unchanged account caps. Details: `docs/carry_hold.md` §7.
3. **Envelope**: the retired CONTINUOUS profile block is shrunk to minimum
   (`max_active` 1) so the freed envelope funds CARRY's full registered
   shape (gross cap 1.0 × capital reference); any CONTINUOUS re-promotion
   must re-size explicitly.
4. **Evidence**: the Lane-2 forward scorer keeps grading the registered
   config independently. The known live-vs-scored divergence is the
   registered terminal-day frame caveat.
5. **Boundary**: demo + paper only; mainnet untouched.

### 2026-07-30 — CARRY sizing anchored to the decision; the live sleeve was rebalancing to its own P&L

Found from the owner's Telegram feed, confirmed against the live account
journal. **Change point: this commit** — the registered config
`lane2_carry_hold_v3` is untouched, but the *deployed execution* of it
changes, so `lane2_carry_hold_v3`'s live forward record has a discontinuity
here and the pre-2026-07-30 portion is not comparable on turnover or cost.

1. **What was happening.** `_carry_target_plan` sized every cycle as
   `weight × live_equity × multiplier` and republished whenever the result
   drifted past a 0.1% dead-band. Live equity is the account mark, which
   moves *because of* the open book, so the day's targets were a function of
   the book's own unrealized P&L — and with a direction: equity rises when
   the longs rise, so the target rises, so the sleeve buys after the move and
   sells after a fall.
2. **Measured cost, 2026-07-30 00:00–13:00 UTC** (account journal,
   `data/bybit-account-execution/account_journal/events.jsonl`): **133
   closing events, every one of them `carry resize: depth rescale` and not
   one a strategy exit**; **$84.7k of notional traded against a ~$30k gross
   book**, ~2.8× turnover in thirteen hours on a sleeve whose intended
   holding period is a day. At the measured 15.56 bp round trip that is
   ≈$66/day of spread, ≈9%/yr of the $255k account, against a mechanism whose
   whole edge is collected funding. Daily reduction counts: 1 (2026-07-26,
   pre-CARRY) → 23 (07-29, deployment day) → 133 (07-30).
3. **Fix, two parts.** `CarryCycleState.sizing_equity` anchors the sizing
   equity to `decision_ts_ms`, so intraday targets are constant and a
   converged book stays converged; and the resize dead-band moves 0.001 →
   0.05, set where the tracking error it buys exceeds the spread it spends.
   The anchor is the load-bearing half — a test pinning the regression still
   fails on a 5% equity drift with the wider band alone.
4. **What this deliberately gives up.** The sleeve no longer de-risks
   intraday when equity falls; it de-risks at the next decision. That matches
   what a daily-rebalance strategy is, and the declared 0.35 stop plus the
   native disaster stop remain the capital-preservation path, unchanged.
   Losing the daemon's cross-cycle state (restart) re-anchors to the current
   mark once; the dead-band absorbs that unless the move is large.
5. **Second, separate defect fixed in the same change.** Every reduction was
   announced twice — `⚠️ Local journal reduction … awaiting venue
   reconciliation` and then its retraction — because the notification path
   reused the reduction *admission* gate, which compares the current kernel
   position against the last venue snapshot and therefore trips after every
   fill by construction. Journaled venue snapshots were clean all day
   (1,556/1,556), confirming the disagreement was never real. Telegram now
   gets a `settling` status with a 30-second window; the admission gate is
   untouched and still refuses to send a reduce-only order on stale evidence.
   Measured basis: all 108 alarms that day were retracted within 14 seconds,
   median 7.

### 2026-07-27 — recorded change points from the repo-wide audit remediation

Three fixes from that audit change numbers rather than only correctness, so
they are change points, not refactors.
All three were owner-approved on 2026-07-27 before landing and deployed the same
day at ~18:26 UTC in the `f1626565f` batch (Actions run 30293398218); `STATE.md`
carries the rollout evidence.

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
  full-calendar correction recorded in `docs/archive/2026-07-26-financed-longs.md`
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
`docs/archive/2026-07-26-financed-longs.md`.

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
counted/journaled). Evidence basis: `docs/research_findings.md` §1 (CONTINUOUS)
and §2 (CONTINUOUS shape).

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
prior-epoch state on the first post-deploy cycles. Rollout remains
owner-dispatched; this commit deploys nothing.

### 2026-07-27 — ladder mechanism decomposed; admission variants; one registered lead

Owner-directed follow-up on the replacement ("why did the 3-cell ensemble
work — was it a gradual scale-in/TWAP — and what is barebones single_fund0
missing?"). Full evidence, all cells and negatives:
`docs/archive/2026-07-27-continuous-ladder-mechanism.md`; reproduction/scorer:
`scripts/research/render_continuous_admission_variants.py`. Lane-1 on seen data.

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

### 2026-08-01 — LONG v12 registered (owner instruction)

Every v11a quirk was ablated one at a time on the real engine
(`_run_long_pipeline`, validated against the stored report: 294 trades here
against 292 there, the gap being eight extra days). ~165 cells. The selectivity
filters are the alpha and all survive; the stop was the one number that was
wrong. Full ledger including every negative below and in `docs/trading_logic.md`.

**Promotion note (recorded change point):**

```text
Claim: LONG's stop is measured as 1.5x ATR-14d, a two-week average, on a name
  that moved 2.5 sigma TODAY, so it sits inside the noise of the move that
  triggered the entry (67 of 294 trades stopped out). Opening it to 3x ATR and
  tightening back to 1.5x after 48h is worth +0.48 bp/day, t 3.27, n 1927:
  total 38.5% -> 51.6%, daily Sharpe 1.24 -> 1.49, worst dip -4.4% -> -3.9%,
  better or equal in all six calendar years, and LESS concentrated (best-20
  share 78% -> 62%). Gross is flat (0.027 -> 0.028), so it is not leverage.
Config commit: this commit — long_v12_profile(), execution identity
  long_native_v12_wide_stop (separate id because that string is a persisted
  account-journal key). v11a is untouched and bit-identical.
Forward record (days, net delta vs baseline, tail behavior): none yet — Lane-1
  seen-data evidence only. The forward clock starts at this commit. Every number
  above is simulated on data that also chose the rule.
Decision: REGISTER on owner instruction 2026-08-01. NOT deployed, and not
  deployable by a profile flip: the producer publishes stop_loss_pct once in the
  entry target's metadata and cannot revise it, so the 48h tightening needs
  _plan_time_stop_exits extended to fire on a breached decayed stop. The wide
  initial stop alone IS config-only but is t 1.84, below the 2.5 bar.
Date: 2026-08-01.
```

**Negatives from the same sweep — measured, not assumed, all do-not-retest.**
Every funding gate on the LONG event fails (16 cells, none beat 1.24; on the days
LONG fires the median 3d funding is +9.0 bp and only 12.7% are ≤ 0, so CARRY's
condition does not exist there). Every "sell into strength" rally exit fails (15
cells — trailing the high-water mark, breakeven ratchets, exit-on-lower-close;
best 1.17, and the 2026-06 trailing test that "already refuted" this was
confounded with hold-7). Loss-only cooldown fails (0.87). Concentrating on the
best 1–2 candidates a day fails (t −2.38 / −2.00). No config-only cell clears the
bar: a stop × hold sweep tops out at t 1.84 and shortening the hold is not a
substitute for the tightening (stop 3× at hold 2d is t −0.28) — the value is in
cutting only what is losing, not in cutting everything.

Measured-but-unrun ideas from this sweep — the momentum leg, financed-leaders,
the carry liquidity screen and the capital-efficiency lever — are written up
separately in [`research_theses.md`](../strategy_program.md), each with the specific
thing that disqualifies it.

**Two sleeves.** CARRY and LONG v12 correlate **+0.012** across all 24 decision
clocks (+0.002 to +0.024), now explicable: CARRY is long what the crowd is short
and paying for, LONG buys what the crowd has just piled into. At registered sizes
they are an order of magnitude apart in risk (≈47% vs ≈5.5% annualised), so the
meaningful construction scales LONG up. At equal risk: 16.56 bp/day, Sharpe 1.81,
worst dip −24.2%, against CARRY alone at 14.46 / 1.13 / −45.6%. Scaling LONG 8.5×
is an envelope decision, not a research one — the Sharpe cost is zero
(`max_concurrent_positions` 10 → 5 doubles size at 1.24 → 1.27) but the margin is
real, and the owner declined `notional_multiplier` 1.0 on 2026-07-28 at ~4×.

### 2026-08-03 — LONG v12 wired into the runtime (owner instruction)

**Recorded change point: the LONG sleeve's deployed profile switches v11a → v12**
("wire v12 into the live systems, paper, demo, live"). No evidence changed — the
numbers above stand exactly as registered on 2026-08-01 — this entry records the
runtime mechanism and the live-book switch date.

What was built, closing the registration's stated gap:

- Each v12 entry freezes its own decay contract in the entry target metadata
  (`stop_decay_after_ms`, `decayed_stop_loss_pct` = 1.5 × signal-day
  `atr_14d_pct`) next to the wide `stop_loss_pct` (3 × ATR). The published
  venue-native stop is never revised; a later profile change cannot rewrite the
  decay terms of a standing position.
- `_plan_time_stop_exits` now also fires on a breached decayed stop: past the
  decay age and live price ≤ `entry_fill_price × (1 − decayed_stop_loss_pct)`
  → zero target, journal reason `decayed_stop_loss`. Checked on the 60s cycle
  grid against the backtest's hourly intrabar lows; the exit is a market order
  after the breach is seen (delayed-execution family, same as the entry caveat),
  with the wide venue stop armed underneath throughout.
- LONG planning reads **both** registered identities, so v11a components open at
  the switch keep their exits, capacity accounting, and 7-day cooldown history,
  and drain under their own published terms within the 3-day max hold. Exit
  targets are keyed under each trade's own identity (the target key embeds it).
- Profile selection is explicit end-to-end: `LONG_STRATEGY_PROFILE=v12` in the
  LONG unit files (demo and mainnet; a paper unit existed until the same-day
  paper retirement) → `--strategy-profile` → `long_v12_profile()`. An unknown
  selector fails startup instead of defaulting.
- Mainnet is **wiring only**: `LONG_MAINNET_SLEEVE` stays off, `REAL_MONEY`
  stays unset, no credential exists. Arming remains the owner's separate act
  under `docs/real_money.md`.

Forward evidence: the config's rolling clock started at the 2026-08-01
registration commit; live demo targets under the v12 identity begin at
this deployment (receipt in `STATE.md`). Live decayed-stop behavior runs the
60s-grid market-exit case, one convention step from the simulated intrabar
stop-price fill — the same class of caveat already recorded for entries.

### 2026-08-03 — paper trading retired (owner instruction)

**Recorded change point: the paper fleet is removed** — paper owner, LONG/CARRY/
CONTINUOUS paper producers, the target mirror, their sleeves.env toggles, the
`demo-operational` deploy profile, the demo-paper watchdog scope, paper Telegram,
`PAPER_EQUITY_USDT` provisioning, and the follower market-data mode the paper
producers rode. Demo (real venue, simulated fills) is the only practice book;
the mainnet route is unchanged (wired, off).

No evidence changed: paper was `integration_only_uncalibrated` — routing
evidence, never fill or performance evidence — so no forward record is
truncated by this. Its journals stay on disk as history; nothing reads them.
The in-flow passive-execution A/B (arm B) retires with it at 2/8 fills; the
probe instrument and the measured 5.40 bp floor stand. Old host state
(`account-paper-execution.env`, the paper config mirror) is removed by the
deploy that carries this change; the paper runtime user stays if present,
inert.

