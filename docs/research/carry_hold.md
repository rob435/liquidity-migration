# Carry-hold — strategy document

The owner-selected lead strategy: hold the liquid perpetuals that crowded shorts
are paying to press, and collect the crowd fee (funding). This document is the
single reference for what carry-hold is, why it works, what it has been tested
against, how it runs, and what would kill it. Dated history lives in
[`CHANGELOG.md`](../../CHANGELOG.md) and [`docs/research/archive/`](archive/README.md).

**Registered config**: `configs/lane2_carry_hold_v6.json`, registered
2026-08-19 — §3 states every parameter in it. **Executable**: the rule in
`liquidity_migration/rules/carry_hold.py`, scored by
`liquidity_migration/research/backtest/financed_longs.py`.

**Status: Lane-2 registered, accruing a forward record since the registration
commit, and PROMOTED to both CARRY producers on 2026-08-19** by owner override —
producer profile `carry_hold_v6_live_v1`, 0 scored forward days at promotion.
Promotion note in [`strategy_program.md`](strategy_program.md), deploy receipt in
[`CHANGELOG.md`](../../CHANGELOG.md).

- The **registered forward experiment** is the capital-normalised paired daily
  differential v6 − v5 on shared days. `lane2_carry_hold_v4` and
  `lane2_carry_hold_v5` keep scoring as comparators.
- The deployed **execution clock is v7** (profile `carry_hold_v7_live_v1`): it
  trades this config's membership byte-identical and fires the early exit on the
  venue's pre-settlement running rate inside the last 15 minutes. **v7 is an
  execution clock, not a registered config: it shares v6's config file and never
  gets a JSON of its own**, because a second file would split one forward record
  in two. Clock evidence: [`research_findings.md`](research_findings.md)
  §Settlement-instant timing.
- Numbers here are scored on the settlement-exact funding detector corrected
  2026-07-28; the correction itself is recorded in
  [`archive/2026-07-26-financed-longs.md`](archive/2026-07-26-financed-longs.md) §0.

## 1. The trade in one paragraph

Hold long, with perpetual futures, the handful of liquid names whose funding
rate has gone deeply negative — the names crowded shorts are paying to press —
and stay in each name until its funding normalizes. Collect the funding three
times a day; ride whatever squeeze develops; cap every name at 10% of the book
and the whole book at 1× gross so cascades dilute the book instead of levering
it. That is the whole strategy. There is no forecast in it: the position *is*
the payment.

## 2. Mechanism and counterparty

Funding is the price of one side of a crowded perp market. When it prints
deeply negative, shorts are so crowded they pay longs ~3×/day to keep the
position on. The carry-hold book supplies the long side of that demand:

- **Who pays**: leveraged shorts (directional bears, hedgers, delta-hedged
  structures) who must hold through the settlement stamps.
- **Why it persists**: the payment is compensation for real risk — these names
  are usually falling, sometimes to zero (LUNA was entered by this rule in May
  2022 and is in the record). The premium survives *because* the risk is
  unpleasant: the unhedgeable version pays in 6/6 eras, while the comfortable
  delta-neutral version was arbitraged out by 2022
  ([`archive/2026-07-24-anomaly-research.md`](archive/2026-07-24-anomaly-research.md)
  §18.5–§19.5).
- **Measured attribution 2021-2026**: **+7.2 units of capital from funding
  received against −3.4 from price** — a 2.1:1 carry payment, not a price
  anomaly.

## 3. The exact rule

Universe and cadence:

- Venue Bybit USDT perps; universe = **top-100 by trailing-24h quote
  turnover**, re-ranked at each decision bar; 168h of price history required.
- Decisions once per day at the 00:00 UTC hourly bar close.

State machine, per name (all inputs are the **last settled** funding rate —
a historical, already-paid print; no predicted rates):

- **Enter LONG** when settled funding < **−10 bp/8h**.
- **Stay** while settled funding < **−3 bp/8h** (hysteresis; funding noise
  between −3 and −10 does not churn the position).
- **Exit** at the first decision bar where funding has recovered to ≥ −3 bp, or
  where the trailing daily rate has recovered **+30 bp over 2 days** — the
  squeeze is over.

Entry filters:

- **Toxic band**: no entry, and holds suspend to zero weight, while the 3-day
  return sits in **[−30%, 0%)** — the cohort where shorts are slowly winning.
- **Minimum volatility**: no entry while trailing 30d daily vol is under
  **5%/day**; a pinned price has no squeeze fuel.

Sizing — **0.10 of capital per name**, multiplied by four size legs that
compose multiplicatively, with **total gross capped at 1.0**:

- **Depth**, how much the crowd is paying now:
  `clip((|trailing 24h settled funding| / 120 bp-per-day) ** 1.5, 0.25, 1.0)`.
  Names at or above the reference keep full size; names at ratio ≤ 0.397 land on
  the floor. The exponent bends the ladder toward where the book's measured
  response actually changes, which is above ~1.4× the reference.
- **Persistence**, whether this name pays habitually: the share of the symbol's
  last **20 settlements** that printed deeper than the 10 bp entry threshold.
  Below a **0.10** cut the multiplier is **0.0** — the name is not held. Counted
  in the symbol's own settlement sequence, never on a clock, because the venue's
  interval mix shifts (below).
- **Flow**, whether the crowd is still arriving: trailing-24h turnover against
  72h earlier. Growth under **+40%** halves the name. A null or non-finite
  growth fails **open** at full size.
- **Whale**, whether the informed side is leaving: the 3-day change in Binance's
  top-trader long/short position ratio. A fall of more than **0.26** halves the
  name. Values older than 48h are treated as null and fail **open** at full
  size. This is the book's one non-Bybit input.

When more than ten names qualify (cascades), every weight scales down pro rata.
The cap direction matters: the un-capped book trebles gross exactly during
market-wide cascades, which is when correlation goes to one.

Optional (registered) **15% annual vol target, 3× leverage cap**, scale computed
from strictly-prior 30d realized vol, leverage-change turnover charged. It is
carried for recipe comparability; **raw is the primary basis** (§4). Sizing
controls drawdown *depth* only; see §6.

Costs and accounting, as scored:

- Fees: measured one-way turnover × **7.78 bp/side** (the measured demo taker
  fee; turnover averages 0.35 units/day ⇒ ≈2.7 bp/day).
- Funding settlement-exact; entries at the decision bar close
  (`execution_delay_ms=0` convention — see delay robustness in §5).
- Not modelled: slippage beyond the taker fee, market impact, partial fills,
  borrow, margin cost, venue outage.

**A per-print threshold is not a daily rate.** Bybit runs mixed funding
intervals — 100% 8h in 2021 against **52% 4h / 21% 1h in 2025**, and 73–80% of
this book's 2025-26 held name-days are on sub-8h names — so the −10/−3 bp
per-print thresholds mean different daily carry per symbol. Normalising them to
a daily-equivalent rate is tested and refuted: the variant collapses, per-print
acuteness is load-bearing
([`archive/2026-07-28-carry-hold-quant-review.md`](archive/2026-07-28-carry-hold-quant-review.md)).

## 4. What the evidence says

Everything here except the forward record is Lane-1 work on seen data: it
selected the rule, it does not grade it (§8).

**The mechanism, full sample 2021-2026**, measured on the base book (§3's state
machine at a flat 0.10 per name, `lane2_carry_hold_v1`): full-sample **t 2.31**,
against the program bar of t ≥ 2.5 ([`governance.md`](governance.md) §2). By
era, bp/day: 2021 **+3.8** · 2022 **+3.0** · 2023 **+26.0** · 2024 **+13.7** ·
2025 **+30.3** · 2026 **+32.5** — every year positive, but 2021-22 is thin and
this book makes no bear-robustness claim.

**Against the deployed benchmark.** Benchmark: the CONTINUOUS sl35 render over
2023-03-13→2026-07-16, Sharpe 1.84, +15.85%, max DD −2.85%; the shipped
CONTINUOUS shape is now revision `active_single_fund0_tp12_sl35_v1`, whose
same-window render is Sharpe 1.45 / +11.06% / max DD −1.84%, and forward
comparisons grade against that one. Full-calendar basis everywhere (flat days =
0 in the denominator, identical to the benchmark's own accounting). On that
window the base book is **Sharpe 1.21 raw / 1.05 vol-targeted**: carry-hold
**does not beat the deployed sleeve on Sharpe**, return does, and the owner goal
was both. Raw is the primary basis precisely because the vol-target overlay
hurts here (1.05 < 1.21). The v4 render is the family's best measured figure on
that window — **Sharpe 1.88, MAR 6.11** — above the retired 1.84 research
benchmark but on seen data, so it grades nothing.

Three bench-window rows have no corrected counterpart and are quoted as the
registration-era scorer produced them, funding leg included: total return
**+21,943% raw / +342% vol-targeted**, worst day (vt) **−5.7%**, t **4.69**.

MAR is compounded annualised return ÷ max drawdown, the convention
`scripts/research/equity_curves.sh` renders; simple annualisation of the daily
mean gives a different number and the two must never be mixed. MAR is also not
scale-free — always say which capital a MAR is on.

**The registered config on seen data** (panel 2021-01-01..2026-08-19, midnight
grid, 1,778 days): mean net **+21.82 bp/day**, Sharpe **1.842**, worst dip
**−18.6%**, mean gross **0.0724**.

**The registered forward experiment** — the capital-normalised paired daily
differential against v5 on shared days — measures **+0.63 bp/day at t +2.86** on
the midnight grid, and is positive in **24 of 24** hourly clock phases at a mean
of **+0.43 bp/day**: cite the mean, not the midnight cell. At its own capital
the pair is a wash by construction (−0.16 bp/day, t −1.00) — v6 holds the same
names as v5 on ~3.5% less gross, so the claim is capital released, not return.

- **Placebo**: the per-name-day depth adjustment handed to 20 random
  permutations of the held book scores mean t −0.26, best +2.23, and **0 of 20**
  reach the real +2.86.
- **Plateau**: exponent 1.25 / 1.5 / 2.0 give +0.36 / +0.63 / +0.96 at t +2.90 /
  +2.86 / +2.71. **1.5 is frozen as the midpoint of the probed plateau, not a
  fitted optimum**; 2.0 was declined because it pushes further toward
  deep-name concentration that measures as regime-local.
- **Era shape** of that differential: 2021 +0.16, 2022 −0.14, 2023 +0.05, 2024
  −0.13, 2025 +1.50, 2026 +2.78 — a flat floor, no materially negative year, and
  the economic weight in 2025-26. Expect roughly nothing from the bend outside
  squeeze regimes.

**The persistence leg's placebos**, which are why it was registered at all:
sizing *up* the isolated prints scores −14.44 bp/day (t −2.73); handing the
identical distribution of position sizes to the wrong names scores −15.26
(t −2.71) — the load-bearing control, because it holds size distribution and
gross constant. Null persistence fails open and never fires on this book (0 of
3,314 held name-days), so it is not a listing-age screen. The isolated deep
print is the only losing cohort in the book (−16.7 bp/name-day over a third of
held name-days); every bucket above the 10% cut earns +99 to +135.

**One parameter carries known debt.** The toxic band's high edge sits at 0%
rather than −5% at the owner's direction: the [−5%, 0) cohort earns −34.4
bp/name-day, but on its own the change measures **t 1.12 — it does not clear the
bar**. Its contribution is on its own line in the config so a later reader can
withdraw it without touching the sizing result.

**What every number here rides on.** The single-clock level is
decision-hour lucky — the same construction over 12 daily offsets spans Sharpe
0.30–1.52 and midnight is the best cell (ensemble ~1.2). The registered daily
frame exits every name 24h before its final panel bar, worth roughly +0.13
Sharpe in research's favour. And the owner's unconditional Sharpe-2 target is
**not** reached: the supportable version is conditional, 2.15–2.35 on the PIT
deep-funding half of days.

The same premium captured market-neutrally as a cross-venue funding spread was
measured and then deleted by operator override; its numbers stand in
[`archive/2026-07-28-carry-hold-quant-review.md`](archive/2026-07-28-carry-hold-quant-review.md)
§10, the config and its scorer code do not exist.

## 5. Validation battery (what was actually checked)

The battery was measured at registration, before the funding correction. Where
§4 restates one of its numbers, §4 is the number that stands.

1. **Cross-venue replication**: identical construction on Binance funding and
   prices gives **t 0.4 / Sharpe 0.18 — it does not replicate**. The doubled
   funding leg *was* the earlier positive result. Single-venue (Bybit) evidence
   until shown otherwise.
2. **Entry-delay stress**: +1h → t 4.19; +4h → t 4.82. The edge does not live
   at the print; there is no stale-price artifact.
3. **Placebo**: same active days, same gross, top-100 basket instead of the
   selected names: Sharpe 0.72 vs 2.76 (active-day basis). ~90% of the return
   is name selection, not market timing.
4. **Worst-day forensics**: the worst days are real cascade events (LUNA week
   2022-05, 2021-12-04, 2022-06 Celsius); no data artifacts. The gross cap is
   what bounded them.
5. **Parameter plateaus** (all cells reported in the research note): enter/exit
   {5/2, 10/3, 15/4, 20/5} → Sharpe 1.92–2.64; universe {50/100/300} →
   2.26–2.61; knife-filter variants change little. No spike-fitting.
6. **Slippage sensitivity**: +2 bp/side beyond the measured fee moves bench
   Sharpe 2.57 → 2.53.
7. **Multiple testing**: the program bar is **t ≥ 2.5**
   ([`governance.md`](governance.md) §2, owner decision 2026-07-31, prospective).
   The base book's corrected full-sample t is 2.31 (§4) — under it. The bar does
   not control family-wise error, so a plateau and a failed placebo carry the
   weight a higher threshold used to.
8. **Funding-sign accounting** is covered by unit tests
   (`tests/research/backtest/test_financed_longs.py`): a long receives negative
   funding settlement-exactly; hysteresis state uses only past prints; the gross
   cap dilutes.

## 6. Risk profile — read this before sizing

- **Underwater behavior**: at 15% vol the longest underwater spell in the
  bench window is **204 days (2024-02-26 → 2024-09-17)**, 87% of days are
  below a prior peak, max DD 23.7%. For calibration: the deployed CONTINUOUS
  system's own longest spell is 215 days at its native scale — long spells
  are endemic to every book here; **sizing changes depth, never duration**.
  At half size (vt 7.5%) the same book is max DD 11.6%, +105% over the
  window, same Sharpe.
- **Concentration**: **2.17 names per active day**, out of the market 46% of
  days (measured on v4; v5 and v6 hold the same names on less gross). A
  single-name disaster costs up to its 10% cap ex-slippage. The book *will* hold
  names that go to zero (it held LUNA); the claim is that the funding collected
  across the book pays for them, as it has 2021-2026.
- **Regime dependence**: deployment frequency tracks the funding regime. In
  2021 the book was ~78% cash. If funding normalizes market-wide, the book
  goes quiet rather than long — returns flatten, they do not invert. Through the
  standard chart (`reports/equity_curves/research/lane2_carry_hold_v4/`)
  **76.2% of the log growth is 2025-26**: 2.21x on 2025-01-01 after three years,
  finishing at 28.83x, by year 2021 −0.6%, 2022 +23.5%, 2023 +48.7%, 2024
  +24.1%, 2025 +260.5%, 2026 +266.1% (207 days). That shape is the mechanism's,
  not one config's — v3 renders 76.7%.
- **Market beta**: +0.30 correlation to BTC on active days (long-only book).
  corr with the deployed CONTINUOUS: **−0.08** — additive, not overlapping.
- **Capacity**: the deep-negative-funding subset of top-100 names at demo-account
  scale. Fees are measured at small notional; no impact model. This is not a
  large-book claim.
  - **The one measured impact reading** (n=3 live entries, against the exact
    decision books captured at entry). A single **$25k** clip already exhausts
    most of the *visible top-50 levels*: `LAUSDT` 79% covered, `ESPUSDT` 74%,
    `VANRYUSDT` 100% (13 levels). Book-walk VWAP for the full clip was **+47.4 /
    +45.7 / +16.1 bp** — roughly **6× the 7.78 bp/side the scorer charges**.
    Realised execution was much better than that (effective spread **−29.2 /
    +3.2 / −2.3 bp**) because the producer chunks (5 / 2 / 2 clips over 0.4–2.4
    s) and the book refills between clips, beating the visible walk by 17–62 bp.
    Read it as: the fee model is fine, the *impact* model is still absent, and
    patience is what is currently paying for its absence. Nothing here supports
    scaling this book an order of magnitude without re-measuring.
  - Observed taker fee is **5.50 bp/side, exactly**, on all 346 live orders —
    the §3 assumption of 7.78 bp/side is conservative by 2.28 bp/side.

## 7. How it runs live

1. **Sleeve**: `carry` — target producer `liquidity_migration/strategy/carry_demo.py`
   (+ `carry_demo_daemon.py`) on both carry units, publishing absolute component
   targets as a target book the engine reads
   ([`rules/engine_targets.py`](../../liquidity_migration/rules/engine_targets.py)).
   The deployed decision logic is NOT a reimplementation: the producer calls the
   exact registered-scorer functions (`rules/carry_hold.py:carry_hold_weights`)
   on the live frame (`rules/carry_hold.py:prepare_decision`) over a stateless
   90-day replay window (longest-ever state spell: 19 days).
2. **Decision cadence**: once per UTC day at 00:00 close, computed from
   ~00:20 (kline availability lag, same offset as the rmom timer). Diff-based
   idempotent publication; quiet cycles between decisions. Data: 1h klines
   stream in over WebSocket into an in-memory store that serves the cycle's
   whole window, with REST covering gaps and restarts. Settled funding history
   and the hourly sweep stay REST — the venue has no stream for it. Same bars,
   same close keys.
3. **Protection**: declared `stop_loss_pct` 0.35 per target — wide enough that
   the funding-normalization exit is always the real exit rather than an
   account-level fallback, and the intraday stop grid in
   [`archive/2026-07-28-carry-hold-quant-review.md`](archive/2026-07-28-carry-hold-quant-review.md)
   §11 is why nothing tighter is declared — and **NO take-profit**, because the
   book's right tail is the P&L. That is measured, not assumed: **105 cells across nine
   families** on the registered v4 book — fixed take-profit, trail-from-peak,
   armed trail, half scale-out, take-profit-then-re-enter, and the adaptive
   kinds (fee-gated, vol-scaled, run-fraction give-back, ratchet floors,
   depth-relative funding exit, blow-off spike, carry-vs-gain, volume climax,
   stall) — and **not one beats the baseline** on mean bp/day; 2026 alone falls
   +74.1 → +18.9 bp/day at a +40% take-profit. The price leg is −1.45 bp/day
   against the crowd fee's +24.57, so the price run is a cost the book carries,
   not a profit it is failing to bank. The cells that do beat the baseline at
   midnight are decision-hour luck (0–6 of 24 clock phases) and lose to a
   **random** exit — better in 145 of 200 draws at hourly resolution, 300 of 300
   at minute resolution, where the true peaks are ~2x the hourly view but a
   volatility spike carries **no signal** (Spearman rho −0.013 against the
   forward return) and 16 of 17 spike rules lose. Hold the trigger fixed, vary
   only the delay, and the series is monotone, converging to the baseline from
   below. Ledger row and method:
   [`research_findings.md`](research_findings.md) §2; report in
   `~/SHARED_DATA/bybit_full_pit/reports/carry_hold_exit_grid_2026-08-07/`.
   Sizing: weight × owner equity × profile multiplier 0.5, per-name 0.10, gross
   cap 1.0, entry leverage 5, under the engine risk kernel's caps. **The equity
   in that product is the equity as of the decision, not the live mark** —
   sizing off the live mark makes the day's targets a function of the book's own
   unrealized P&L and rebalances the whole book every few minutes. Targets are
   constant between decisions, and the resize dead-band is 5% of standing
   notional: below that the tracking error is not worth the round trip.
4. **Live-vs-scored divergence, stated up front**: the research frame drops
   each symbol's terminal 24h (the registered frame caveat, ~+0.13 Sharpe
   in research's favor); the live sleeve cannot and does not dodge — it
   holds through bars the scorer never sees. Forward research rows and the
   live journal are two records; neither substitutes for the other.
   - **Entry price basis.** §3 scores entries at the decision-bar close
     (`execution_delay_ms=0`). The live sleeve cannot get that price: the
     00:00 UTC bar is only published at ~00:20, so orders go out ~23 minutes
     after the price the headline numbers use. Measured on the two on-time live
     entries, the gap between the modelled and realised entry price was
     **−363.8 bp (`ESPUSDT`) and +111.4 bp (`VANRYUSDT`)** — sign is noise at
     n=2, the dispersion is the point. The edge itself survives this (§5.2
     stress: +1h → t 4.19, +4h → t 4.82), but that means **the live sleeve runs
     the delayed-entry stress case, not the headline case**. Quote forward
     comparisons on that basis.
5. **What the live sleeve does that the scorer does not.** The scorer sets
   weights once per daily bar and lets quantity ride until the next one. The
   producer additionally re-sizes intraday whenever the accepted notional
   drifts more than 5% from target. That overlay is not in any registered
   number. It is bounded (dead-band, decision-anchored equity) rather than
   continuous, but it has not been removed, and whether a daily book should
   track notional intraday at all is an open owner decision, not a settled one.
6. **Known sharp edges**: (a) entry attempt keys are per-symbol-stable, so one
   terminal rejection suppresses that symbol's entries for the journal's life —
   the producer avoids the self-inflicted cases (expired-on-arrival validity,
   dust orders), and a bricked symbol surfaces in the cycles dataset's
   suppression counts. (b) The live bar keying is close-time (decision at 00:00
   close, computed 00:20) — knowledge-content identical to the research row, one
   grid-phase convention apart, inside the registered decision-clock caveat.
   (c) The cycles datasets (~1.4k rows/day) are written one day-bucketed part per
   calendar day, so a cycle append rewrites one day rather than the whole
   history.

## 8. What this document does not claim

Lane-1 numbers selected this rule; only the forward record grades it. Fees are
measured, impact is not. The 2025-26 contribution rides the structural funding
inversion; a normalization shrinks the opportunity set. The evidence is
single-venue: the Binance arm does not replicate on corrected accounting (§5.1).
Correlation +0.75 with `lane2_financed_leaders_v1` — the two harvest one
macro-premium in different phases and must not be presented as independent bets.
