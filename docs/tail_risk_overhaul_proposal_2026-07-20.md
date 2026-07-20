# Tail-Risk & Exit Overhaul Proposal — 2026-07-20

Status: **adopted 2026-07-20 by operator instruction as the repository's main
research focus.** This document is the rationale/receipt; the live execution
state and next actions are `docs/tail_risk_program.md`. Lane-1 document; it
changes no runtime, authorizes no deployment, and makes no alpha claim. Evidence policy: `docs/governance.md`. Selection
accounting: `docs/hypothesis_ledger.md`. Failure taxonomy references are to
`docs/backtesting_errors_we_never_repeat.md` (items cited by number).

## 1. Diagnosis — why every exit and tail-risk fix failed

The failures are consistent enough to be a finding in their own right. Five
mechanisms, all with receipts:

**1a. The payoff shape punishes truncation.** Both sleeves earn their net from
a completed-TP tail: CONTINUOUS (short D9 pumps, 12% TP, 24h hold, no strategy
stop) completes TP on ~40% of its best buckets, and LONG goes negative on both
venues when the take-profit bucket is removed (−0.92%/−5.99%). Every
path-truncating rule tested sells the right tail to buy the left: 20/40/80%
fixed stops (each reduced MAR on both venues), a routine server/venue stop
(live-v2 redesign arm 02: returns collapse to −2%/−8%), stop-approach exits in
the 2026-06-18 exit-cause ablation (Bybit +70%→−11%, Binance +89%→−12%; the
full legacy software-exit stack took the ablation's mean return from +0.795 to
−0.014, and the breakeven trailing stop never fired once —
`backtest-runs/continuous_exit_cause_ablation_2026-06-18/`), MFE give-back
ladders (T-F: tight arms forfeit ~148pp of TP completions against ~144pp
captured), funding drain-exits (−5 to −11pp), and signal-invalidation exits
(sparse-to-zero hit). This is structure, not bad luck: on a book of
lottery-shaped trades, per-trade exit engineering converts insurance into a
guaranteed alpha tax.

**1b. The binding tail is common-mode, not per-trade** (item 21). Tail losses
concentrate on common-loss days (15 gate-on vs 28 gate-off per era; T-A: gate
removal doubles entries and takes ~5× the drawdown). A per-trade stop cannot
control a factor event — when it triggers it triggers across the whole book at
the worst prices (the 2026-07-19 demo day did this in miniature: 8 native-stop
closes in hours). The two sleeves' common-mode tails are opposite-signed
exposures to the same alt complex during BTC uptrends: an alt melt-up squeezes
CONTINUOUS shorts while a flash crash hits LONG longs. Nothing at the account
level today measures or governs the *net* book factor exposure — the BTC+ETH
hedge covers CONTINUOUS beta only, demo only; paper is unhedged and LONG is
entirely unhedged.

**1c. The window never contained enough tail information.** Effective sample is
unique decisions (~900 over three years; ~93% of component rows share a
decision — item 29), and the tail itself is ~15–28 common-loss days per era. No
exit rule can be honestly graded on ~20 events. Worse, exits act intraday while
the research surface is 1h bars: when stop and target both touch inside a bar
the path is unobservable (item 14). The surface is simultaneously *spent* (29
hypothesis families, 150+ configs, five generations — anything new from it is a
sixth-generation read) and *under-resolved* for the question asked of it.

**1d. When safety WAS graded as safety, it still pointed at the book level.**
The one properly tail-graded stop study (the 2026-06-20 disaster-stop
construction, commit `1fa7045`: 25–80% stops on 1m paths, graded on
worst-trade/CVaR/worst-day; its receipt and local artifacts were since
pruned and survive in git history at that commit) found that inverse-vol per-name sizing already bounds the
worst single trade to ~0.85% of book equity even on names with −143%/−258%
MAE; that wide price stops cap essentially none of the remaining tail (on
Binance they make the worst trade *worse* by filling the reverting intrabar
spike) while costing 0.9–4.7 MAR; and it concluded in its own words that the
real controls are gross caps, a position-level liquidation guard, and a
correlated-squeeze cap — the last of which was never built. Separately, T-I's
continuous intensity member Pareto-dominated the binary gate on every risk
dimension and was rejected only because MAR is ill-posed at negative net (item
27: an alpha metric mis-grading a risk tool). The evidence does not say the
tail is uncontrollable — it says the controls live in sizing, caps, hedges,
and book state, and part of that program was recommended and never
implemented.

**1e. Costs killed the marginal rules.** The frozen 45 bp round-trip hurdle —
and the measured reality behind it (first cost report: taker flow shows no
adverse selection; +19.4/+25.7 bps realized spread at 15s/1m handed to passive
counterparties) — means every added exit churns into a hurdle an order of
magnitude larger than any remaining signal-side improvement on the spent
surface. The passive-execution A/B now live on the paper owner is the only
lever that changes this physics, and its rolling record re-prices every
previously rejected marginal rule.

**What did NOT fail:** inverse-vol per-name sizing (the proven disaster
control), the prior-day 30-day BTC uptrend gate (removal ≈5× drawdown), the
BTC-risk 0.35× sizing overlay, gross/symbol/margin caps, the CONTINUOUS
adverse-reduction pause (a book-level breaker already in production), the demo
BTC+ETH beta hedge, and the venue-native disaster stop as an operational
process-death seatbelt. Every surviving control is a sizing, cap, hedge, or
book-state control. Every failure is a per-trade price-exit rule. That
asymmetry is the whole proposal.

## 2. What we stop doing

No sixth-generation per-trade exit or stop variants on the spent 1h surface.
The TP12/24h CONTINUOUS shape and the LONG 1.5-ATR/4-ATR/3d shape stay frozen
until (a) a render-native 1m re-simulation harness exists (the T-F standard:
reproduce every recorded exit exactly before testing a variant) and (b)
genuinely new data has arrived. Both grading styles have now been tried against
per-trade exits — alpha metrics (stops table, exit-cause ablation, T-F) and
tail metrics (the 2026-06-20 study) — and both closed them. Negative results
are priors, not prohibitions — but a revisit needs new mechanism, new data, or
new economics, not a new grid.

## 3. Core-logic overhaul — move tail control from the trade to the book

### R1 — Continuous risk intensity (re-registration of a metric-artifact kill)

Replace the binary BTC gate + discrete 0.35× overlay with one monotone
gross-exposure multiplier `m ∈ [0, 1]` driven by BTC trend strength and the
existing causal BTC-risk score. T-I's linear member already showed equal net,
−5pp maxDD, and better tail everywhere; it died on MAR-at-negative-net.
Re-register under the §5 tail metrics. Descends from the T-I family
(sixth-generation prior applies; hypothesis-ledger row recorded at commit) —
therefore graded on rolling forward days and the §4 new-to-us historical
surfaces, **not** the reserved holdout.

### R2 — Squeeze-state governor (the flagship; a genuinely new mechanism family)

CONTINUOUS's tail process is the liquidation-cascade melt-up; LONG's is the
uptrend flash crash. Both are partially observable in real time from fields
none of the 29 prior families used:

- OI level/acceleration (Bybit `open_interest` 1h, 2021→; Binance
  `metrics_5m` OI),
- positioning crowding (`positioning_lsr` on Bybit; top-trader long/short
  ratios in Binance `metrics_5m`),
- taker-flow imbalance (`taker_flow_5m`, 2023-03→),
- perp premium spikes (`premium_index_1h`, 2021→) and funding jumps,
- melt-up/crash breadth (share of the traded universe with |ret1| beyond a
  causal threshold),
- forward-recorded liquidation prints (§4-D4; no local history exists today).

Output: a causal book-level squeeze/crash index per side, consumed as (a) a
gross multiplier on the exposed sleeve, (b) hedge-intensity modulation, (c) an
entry-admission veto at the extreme state. No per-trade exits change; the book
de-risks as the cascade state develops. Build Lane-1 on the spent window,
commit the config, then grade **once** on the unopened `[2025-01-01,
2026-07-06)` holdout — this family is not descended from the 29, which is
exactly what the reserve was held for — then rolling forward. Holdout opening
is recorded in `docs/preregistration/INDEX.md` per the ledger rule.

### R3 — Structural insurance, judged as insurance (item 27)

Registered with cost budgets and trigger-quality metrics, not MAR:

- **R3a — book-level daily loss budget.** If realized day P&L breaches −X% of
  the capital reference, stop new entries and halve gross until the next UTC
  day. The direct test (the "continuous tail survival/loss-budget" study) was
  cancelled unrun — untested, not refuted. The nearest tested relative is a
  negative prior to respect: Book G's daily vol-target adjuster with a −4%
  drawdown-half governor was a pure leverage dial whose deviations slightly
  hurt, and it is rightly disabled. R3a differs in trigger (realized intraday
  loss, daily reset, entry-side only, existing exposure untouched) and in
  grading (insurance metrics, not return improvement). X from kill-criteria
  arithmetic (K1 = −5%/epoch ⇒ a daily budget near −1.5% binds well before the
  sleeve kill). Extends the existing adverse-reduction pause mechanism.
- **R3b — cluster caps (the tail study's own unbuilt recommendation).** Cap
  simultaneous same-direction exposure to correlated pump clusters (start
  crude: trailing-correlation clusters over the traded universe). Single-name
  and account caps exist; a cluster dimension does not — the 2026-06-20 study
  explicitly recommended a correlated-squeeze cap and it was never
  implemented. No fitting, deploy-when-ready with a recorded change point.
- **R3c — protection-layer accounting.** The live native stops are an
  operational safety net that research never owned. Attribute their realized
  cost (2026-07-19: eight closes, −9.49 USDT) as an explicit insurance-premium
  line in the forward record so the safety/alpha boundary stays visible. While
  here, re-anchor the pruned 2026-06-20 disaster-stop receipt from git history
  (commit `1fa7045`; no local `backtest-runs/` copy remains): the most
  load-bearing sizing-is-the-disaster-control claim should not rest on a
  pruned doc.
- **R3d (later) — convexity overlay.** Long OTM BTC/ETH calls against the
  CONTINUOUS melt-up tail / puts against the LONG crash tail, premium budgeted
  next to gross. Needs an options data root and a carry model; medium-term.

### S1 — Cross-venue migration signals (the repository's own name, finally used)

Binance vs Bybit OI/LSR/taker-flow lead-lag and venue-share migration as (a)
entry-quality context, (b) a book-level de-risk trigger — liquidity migrating
away from the execution venue predicts exit-quality collapse. Candidate second
new family; same governance as R2 (but the holdout is spent after its first
read — S1 grades on forward days unless the owner prefers it over R2 for the
one holdout read).

### S2 — Anti-book (speculative, last)

The squeeze index inverted is a long-cascade entry signal: a third sleeve that
is long the tail the short book fears, with defined risk — an internal hedge
that earns rather than costs. The frozen backlog estimands C-H1/C-H2 (D9 vs
D7/D8 24h short-directional paths, with the frozen four-test/α=0.05/98.75%-CI
family rule) are the registered starting point. Only after R1–R3 have records.

## 4. The data program — "larger or different", concretely

- **D1 — open the already-local, never-opened slices.** Binance
  `[2020-01-01, 2021-05-01)` klines+funding and Bybit `[2021-01-01,
  2021-05-01)` predate the V2 discovery window and, per the hypothesis ledger,
  no generation touched them. Verify untouched status, record a provenance
  note, commit R1/R3 configs **first**, then grade once. Universe is thin that
  far back (age/turnover gates thin it further) — this is common-mode-regime
  evidence (COVID crash, DeFi-summer pumps, early-2021 blowoff), not per-name
  entry evidence, and is stated as such.
- **D2 — backfill deeper.** Extend the Binance builder toward venue origin
  (funding already reaches 2019-09; klines upstream availability to be
  verified at fetch time). Pure acquisition; enlarges the tail-event library.
- **D3 — finish the granular library and build the 1m harness now.** Bybit
  `tick_ohlc_1m` 2023-03→2026-05 is already local; the owner-directed
  `bybit_render_1m` + `binance_vision_alt` fetches (documented today, not yet
  present on this host) complete coverage. Deliverable: the render-native 1m
  re-simulator (T-F exact-reproduction standard) that any future exit-shape
  work must pass through, and 1m-resolved intraday excursion truth for the §5
  tail metrics.
- **D4 — forward-first new fields.** Start recording the Bybit liquidation
  stream and persisting live-L2 depth summaries now; the fields accrue one day
  per day and no venue history exists to buy. Third-party liquidation archives
  may be evaluated as research-only, provenance-labelled context.
- **D5 — optional third venue.** Hyperliquid serves full-history funding, OI,
  and transparent liquidations (2023→) — the missing liquidation field on a
  structurally different venue plus a native-listing population. A correlated
  crypto venue is robustness, not independence, and is framed that way.
- **D6 — the forward stream itself.** The rolling Lane-2 ledger and the
  passive-execution A/B keep accruing. If arm B's ≥10 bps/side materializes,
  the cost hurdle that killed several marginal rules changes, and specific
  closures may be prospectively re-registered under new economics.

## 5. Grading rules registered before any run

- **Metrics:** ES95/ES99 of daily book P&L, max drawdown, common-loss-tail-day
  count, net including costs and funding — era-split, all grid cells reported,
  forgone upside next to avoided cost. MAR is banned at negative net (T-I
  lesson). Safety layers (R3) are additionally judged on trigger correctness,
  false-positive rate, and realized premium vs. budget — not on return
  improvement (item 27).
- **Effective N:** unique decisions → simultaneous waves → 28-day blocks
  (item 29); component rows are never counted as independent.
- **Selection accounting:** every config commit records its hypothesis-ledger
  descent and family count; R2/S1 are the only candidates for the one-shot
  holdout read, because they are the only non-descended families.
- **Kill criteria:** each promoted arm gets pre-committed kill rules in the
  `sleeve_kill_criteria` pattern before its first forward day, checked by the
  weekly `ops.sh kill-criteria` cadence.
- **Runtime boundary:** nothing here changes demo/paper behavior without a
  five-line promotion note and recorded change point; real money remains a
  separate, unopened door.

## 6. Sequencing

1. **Now (no evidence risk):** D3 harness on local 1m data; D1 untouched-status
   verification + provenance note; D4 forward recorders; D2 backfill kicked
   off.
2. **Commit R1 + R3a/R3b configs** (the commit is the registration); grading
   starts accruing immediately on forward days and on D1/D2 surfaces when they
   land.
3. **R2 Lane-1 build** on the spent window → config commit → single holdout
   read → rolling forward.
4. **S1** behind R2; **R3d/D5** as capacity allows; **S2** only after the
   R-layers have records.

## 7. What this proposal does not claim

No alpha, robustness, or promotion claim; no statement that any proposed layer
will work. It claims only that (a) the per-trade exit program is closed by
evidence, (b) the book-level program is the surviving direction and has two
never-tested, well-posed candidates (R2, R3a), and (c) the listed data moves
create the only honest new grading surfaces available: unopened past, finer
resolution, new fields, and forward days.
