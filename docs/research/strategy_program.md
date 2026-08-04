# Strategy program — reset 2026-07-21

This is the single current authority for strategy evidence, direction, and next
work. `docs/research/governance.md` still owns evidence policy, `STATE.md` owns deployed
state, and code/tests own implemented behavior. Historical research is useful
only through the compact priors below; its old plans, queues, reports, and
one-off runners are retired.

## Current truth

- **Entries stopped crossing the spread (2026-08-04, owner instruction; the
  basic recipe from the overnight quote lab).** Both account owners now place
  an exposure-increasing entry as a GTC limit resting at the touch, chase a
  touch that moves away every 15s, and at 120s amend the price through the far
  touch so the remainder fills as a taker at a bounded price; a remainder the
  cross cannot clear within 20s is cancelled and the owner's convergence
  machinery re-plans it. Exits, resizes, and native stops are unchanged
  (taker). The numbers are the overnight lab's measured arm, not a guess:
  seg00 (Buy, reprice 15s, timeout 120s, 34 symbols, n=1,586 attempts) filled
  **70.4%** passively, median time-to-fill 41.6s, clean all-in cost median
  **1.9 bp/side** against the fleet's measured 7.78 bp taker basis
  (`docs/research/research_findings.md` §1). **This is an execution change
  point for both sleeves' forward records**: entry fills should turn
  maker-heavy and entry prices move from crossing to the touch. Change point =
  this commit; deploy receipt in `STATE.md`. **The full night fit is done
  (2026-08-04 morning, n=12,656 across all six arms plus a repeat): the
  recipe stays as shipped.** The Sell side quotes as well as the Buy side
  (the short entries carry makes are covered), the slower 30s/180s arm ties
  on cost while exceeding the owner's 120 s sibling-batch budget, and the
  10s/60s no-chase arm is rejected. Per-arm table and the three recorded
  decisions in `docs/research/research_findings.md` §1. **Same day, second
  execution change point (owner: "prepare for big sizing, up to 5,000 USDT
  notional"):** an entry larger than the displayed touch now arrives as a
  sequence of touch-sized quote windows instead of one resting order — the
  measured touch on the thin half of the universe holds only 23–181 USDT,
  so this is the difference between joining the queue and being the whole
  market. Forward-record effect: large entries take minutes instead of
  seconds, in exchange for staying maker-priced; ungraded until real
  entries at size produce receipts.
- **The significance bar is `t >= 2.5`** since 2026-07-31 (owner decision;
  authority `docs/research/governance.md` §2), replacing the family-wise ≈3.25/3.58. It is
  prospective — earlier verdicts stand as recorded. Because it no longer controls
  family-wise error, a survivor needs a reported plateau and a failed placebo
  beside the number.
- **`LongV12WideStop` is registered (2026-08-01) and wired as the LONG sleeve's
  deployed profile (2026-08-03; deployment receipt in `STATE.md`).** v12 changes
  exactly one thing — the stop opens to 3× ATR and decays back to 1.5× after 48h —
  after ablating all ~20 v11a quirks on the real engine. Paired daily difference
  **+0.48 bp/day, t 3.27, n 1927**; total 38.5% → 51.6%, daily Sharpe 1.24 → 1.49,
  worst dip −4.4% → −3.9%, better or equal in all six years, and *less* concentrated
  (best-20 share 78% → 62%). Detail: `docs/trading_logic.md`. The runtime change the
  registration called for is built: entries freeze a per-trade decay contract in their
  target metadata and `_plan_time_stop_exits` publishes a `decayed_stop_loss` zero
  target on a breached decayed stop; the wide half stays a venue-native resting stop.
  The wide initial stop alone (`fc_atr_stop_mult=3.0`, no decay) is t 1.84 — below the
  2.5 bar — and costs drawdown (−6.6% against v11a's −4.4%). The pair is what clears
  the bar, which is why the profile only ships with both halves wired.
  **No config-only cell clears it**: a 12-cell stop × hold sweep (stop 1.5/2/3/4 ×
  hold 1/2/3) tops out at that same t 1.84, and shortening the hold is *not* a
  substitute for the tightening — stop 3× at hold 2d is t −0.28, at hold 1d t −1.78.
  Cutting every trade at two days is worse than leaving them; the value is in cutting
  only the ones that are losing, which is what the decayed stop expresses. Negative results from the same sweep, all measured, all
  do-not-retest: **every funding gate on the LONG event fails** (16 cells, none beat
  1.24 — on the days LONG fires, median 3d funding is +9.0 bp and only 12.7% are
  ≤ 0, so carry's condition does not transfer); **every "sell into strength" rally
  exit fails** (trailing, breakeven ratchets, exit-on-lower-close: 15 cells, best
  1.17); **loss-only cooldown fails** (0.87); **concentrating on the best 1-2
  candidates a day fails** (t −2.38 / −2.00). CARRY and LONG v12 correlate **+0.012**
  across all 24 decision clocks — at equal risk the pair is 16.56 bp/day, Sharpe
  1.81, worst dip −24.2%, against carry alone at 14.46 / 1.13 / −45.6%.
- **`lane2_carry_hold_v4` is PROMOTED to the demo CARRY sleeve (2026-08-03,
  owner override).** v4 adds a crowding-persistence size multiplier and moves
  the toxic band's high edge to 0%; its claim is capital efficiency (same
  money, ~30% less capital) and not return — at its own capital the paired
  differential against v3 is t 0.47. Detail: `docs/research/carry_hold.md`
  §0.1. Promotion note (`governance.md` §3):
  - **Claim:** v3's book on ~30% less capital; capital-normalised differential
    vs v3 **+10.76 bp/day (t 3.23)** on seen data, own-capital +1.07 (t 0.47,
    not significant); Sharpe 1.41 → 1.64 is the scale-free statement.
  - **Config commit:** registered 2026-07-31
    (`configs/lane2_carry_hold_v4.json`); producer switch is this commit
    (`CARRY_PROFILE_NAME = carry_hold_v4_live_v1`; the journal strategy id
    stays `carry_hold_v3` — a frozen lineage key, documented at the constant).
  - **Forward record: 0 scored days.** The daily scorer has not run since v4
    entered `DEFAULT_CONFIGS` (ledger ends at panel day 2026-07-27), so this
    promotion rides on seen-data evidence and the owner's decision. v3 keeps
    scoring as the primary comparator; the v4−v3 paired differential is the
    experiment the forward record grades.
  - **Decision:** owner, 2026-08-03 ("promote v4 to demo and live now").
    Demo is done through the normal deploy flow; mainnet trades v4 whenever
    the owner arms `REAL_MONEY` (separate door, `governance.md` §6).
  - **Date:** 2026-08-03. Change point = the deploy receipt in `STATE.md`.
  Migration: the producer's stateless replay recomputes the desired book under
  v4 at the first post-deploy cycle, so the standing v3 book converges by
  ordinary exit-first diffs (persistence-cut names exit, the rest resize); no
  flatten, no stranded components.
- **The settlement sawtooth program is CLOSED (2026-08-01; kill criteria 2
  and 4 fired).** The step is arbitrage-free by construction — slope 1.0340 on
  365,691 settlements, net to a long zero at every depth — and every trade
  tried there is dead. Dossier archived verbatim:
  `archive/2026-08-01-settlement-sawtooth-program.md`. Two durable bounds
  survive it and must be quoted before anyone re-proposes either: **the carry
  book's price leg cannot be hedged** (a per-name Binance short removes 94% of
  the price variance but eats 74% of the funding — neutral Sharpe 0.62 against
  directional 1.24), and **the settlement-window trade needs a zero-latency
  exit** (Sharpe 2.96 at zero lag, −2.14 at one hour).
- **Settlement-instant timing is closed on the v4 book too (2026-08-03, owner
  request).** Entering just before the fee to collect it, shorting the post-fee
  crash (including a cadence-aware exit that never pays funding: −29.6 bp/event,
  t −4.1, negative in all six eras), and every entry/exit fill delay up to 12h
  are measured dead — `docs/research/research_findings.md` §2
  "Settlement-instant timing". One accounting fact survives: the scorer's
  funding-boundary convention understates carry configs by ~+0.5 bp/day at
  midnight, 24/24 phases (§4 there). The deployed ~00:20 fill stays as-is
  (H7: it saves ~42 bp per entry).
- The publishing profiles are `lane2_carry_hold_v4` (CARRY, since the
  2026-08-03 promotion) and `LongV12WideStop` (LONG, since the 2026-08-03
  rollout). `continuous_ensemble_v2` at revision
  `active_single_fund0_tp12_sl35_v1` (the single funding-gated cell — the profile
  id predates the 2026-07-26 replacement and no longer implies an ensemble) was
  retired from demo and paper on 2026-07-29 by owner override and its code is
  dormant. All of these are runtime configurations, not validated alpha claims;
  `deploy/sleeves.env` and `STATE.md` are the authority for what publishes.
- No researched replacement currently qualifies for implementation.
- Passive execution: the in-flow A/B is **retired** (it lived on the paper
  owner, retired 2026-08-03; the sample froze at 2 of 8 fills when CONTINUOUS
  went off 2026-07-29 — `docs/research/research_findings.md` §1); the measured
  floors stand. A fast instrument survives it:
  `scripts/research/probe_passive_fill_ab.py` (protocol in
  `liquidity_migration/research/execution/passive_fill_probe.py`, ITT
  accounting, written kill criteria) bounds the mechanism in hours — it
  answers whether the 5.40 bp passive floor is mechanically reachable, and
  only that. Blocked on demo credentials this box does not hold; run with the
  fleet stopped and flat.
- The account-kernel remediation was independent of this research reset and
  deployed with the 2026-07-25/26/27 rollouts of canonical `main`; `STATE.md`
  (Deployment) is the authority for what is installed.
- **The 2026-07-25 instrument-repair and program phases (1, 2A/2B, 5) are
  closed.** The anomaly program's conclusion is economic: the durable premium
  is compensation for liquidation risk this capital structure cannot survive,
  and no construction the repository can express clears the bar. CONTINUOUS
  declares a 35% stop its backtest also models (honest headline: Sharpe 1.87,
  +15.79%, max DD −2.85%). Full phase record verbatim in
  `archive/2026-08-03-strategy-program-change-log.md`; durable summary in
  `docs/research/research_findings.md`.
- **Dated change points 2026-07-26 .. 2026-08-03 are decanted verbatim to
  `archive/2026-08-03-strategy-program-change-log.md`** to keep this file
  small. New change points keep being recorded here, then decanted.

## Theses — measured, not registered

Ideas that have been built and measured but are **not** a deployed sleeve and,
in most cases, not a registered config either. Each entry says what it is, what
was measured, and the specific thing that keeps it out of the book — so a
promising-looking number is never re-discovered without its disqualifying
context attached. Confirmed dead ends belong in the do-not-retest ledger in
`research_findings.md` §2, not here; this is for things that *work* and still
are not run. Nothing here has a forward record: every number is Lane-1
simulation on data that also shaped the idea, under `governance.md`.

### 1. `momentum_1w` — the cross-sectional 1-week momentum leg

**What it is.** Rank the top-100 Bybit names by 168-hour return each day, buy the
top decile, short the bottom decile, equal-weight, hold 24h, rebalance. Market
neutral by construction. It is one leg of the registered
[`lane2_premium_momentum_blend_v1`](../../configs/lane2_premium_momentum_blend_v1.json)
and has **never been registered on its own**.

**Measured**, 2021-05 → 2026-07, costs charged at the blend's committed basis:

| framing | total | annualised | daily Sharpe | worst dip | MAR |
| --- | ---: | ---: | ---: | ---: | ---: |
| all 24 decision clocks, equal weight | +3934.6% | +103.9% | 1.23 | −78.3% | 1.33 |
| one clock (Sharpe per phase, then averaged) | — | — | **0.94** | −81.5% | — |

**Judge it at 0.94, not 1.23.** The two differ because averaging 24 imperfectly
correlated phase series before computing Sharpe removes variance that a single
deployed clock still carries. 1.23 is the equity of running 24 staggered books;
0.94 is what one book gets. The gap is diversification across decision *times*,
not alpha.

**Why it is not run.** The shape, not the total. The curve is flat for its first
two and a half years and essentially all of the 40× arrives after 2023-11, most
of it in the last 18 months. Monthly returns include +212.5% (2025-04), +141.8%
(2023-12), +101.4% (2026-05) against −49.8% (2026-06), −31.3% (2025-05) and
−29.3% (2024-10). It ran to 114× in mid-2026 and ended the sample near 40× —
**the −78% max drawdown is at the end of the record, not buried in its history.**
Annualised volatility is roughly 90%. A five-year record whose first half is flat
and whose second half is one regime, currently mid-drawdown, is not a book to
size into.

**What it is genuinely good at.** Being uncorrelated. It runs **+0.070** against
LONG v12 and **+0.277** against carry_hold v4. Side by side with LONG at equal
risk it takes the pair from Sharpe 1.48 to 1.65; the full blend takes it to 1.73.
That gain is real and it is the entire argument for the thing.

**Not measured:** the three-book portfolio (carry + LONG v12 + blend). Whether
the daily rebalance of ~20 long and ~20 short names survives a turnover-honest
cost model rather than the blend's flat per-day basis.

Chart and series: `~/SHARED_DATA/bybit_full_pit/reports/exploratory_momentum_1w/`,
rendered through the standard chart function and labelled EXPLORATORY.

---

### 2. `lane2_financed_leaders_v1` — momentum crossed with the financing condition

**What it is.** Registered
([config](../../configs/lane2_financed_leaders_v1.json)), long-only: the top decile
by 1-week return among names whose last settled funding is ≤ 0 bp/8h, with a BTC
30-day regime gate, vol-targeted. It is the object you get when you force the
merge of "buy winners" with "get paid to hold".

**Measured:** 14.32 bp/day, Sharpe 1.02, worst dip −40.3% — comparable to
carry_hold v4 standalone (14.46 / 1.13 / −45.6%).

**Why it is not run: it is substantially carry wearing a costume.** It correlates
**+0.544** with carry_hold v4. The funding condition dominates the momentum
condition, so combining the two does not produce a third bet — it recovers the
first one at extra complexity. Added to a book that already holds carry and the
blend it lifts Sharpe 1.52 → 1.56 while *cutting* return 17.89 → 16.53 bp/day.
Its only real contribution is drawdown, −43.3% → −33.3%.

Note the contrast with `momentum_1w`, which shares the momentum idea but not the
funding condition and correlates only +0.040 with the blend and +0.277 with
carry. **The funding condition is what creates the redundancy, not the momentum.**

---

### 3. The liquidity screen inside carry-hold

**What it is.** Restrict carry-hold's candidates to the most liquid quartile
(`pct < 0.25` by trailing turnover).

**Measured:** +15.23 bp/day against v4's +14.46 at constant capital, Sharpe
1.13 → 1.18, better in 24/24 clock phases. The underlying cross-sectional fact is
strong: the most-liquid 5% of crowded names earn **+354 bp/day** (t 3.39 on
disjoint sampling; +317.5, t 3.12 excluding ALPACA).

**Why it is not run.** The book is candidate-poor, not capital-poor. carry-hold
holds about 2.2 names at 9.4% gross with `gross_cap` unused at 1.0, so roughly
90% of the sleeve sits in cash. Removing a candidate from a 3-name book costs
more than the losers it avoids — the config's own `rejected_in_review` note says
so. Every conditioner measured this way is real in the cross-section and
unusable in the book. Ceiling across all of them: ~+15.5 bp/day, Sharpe 1.20.

**The trap this taught.** Gross-matching is the right filter for a rule that
*adds* exposure and the wrong one for a *screen* that removes it — rescaling a
shrinking book breaches `per_name_cap` and reports leverage as alpha. Screens
must be tested as runnable config, with max drawdown and cap-breach printed next
to bp/day.

---

### 4. Capital efficiency — the largest unexploited lever, and it is not alpha

Both deployed-shape sleeves are tiny. LONG deploys **2.7%** of account equity
averaged across all calendar days; carry_hold uses **9.4%**. Together the
two-sleeve book puts about 12% of the account to work.

`max_concurrent_positions` is a pure size dial for LONG, not a capacity
constraint: the book holds roughly one position at a time and the 10 slots never
bind (`skipped_capacity: 0` across the whole history). Halving it to 5 doubles
position size and takes total return 38.5% → **85.8%** at Sharpe 1.24 → 1.27 —
i.e. **at no measurable risk-adjusted cost**.

**Why it is not done.** It is an envelope and margin decision, not a research
one, and it is the same ask the owner already declined: `notional_multiplier`
1.0 was refused on 2026-07-28 because it needed roughly 4× the envelope. Equal
risk against carry would need 8.5×. Two of the three names in
`LongV11aDivWeekendVol` — the weekend 1.5× boost and the BTC-vol scalar — are
this same lever already in the profile, and both *cost* Sharpe when widened.

---

### 5. Open, unmeasured

- **Three-book portfolio**: carry + LONG v12 + the premium/momentum blend. The
  pairs are measured (carry+blend 1.52, LONG+blend 1.73) but never all three.
- **Premium divergence as a LONG entry filter.** `premium_diff_bp` is a free
  public ranking signal that needs no Binance account to *trade*, only to read.
  The blend's diversification comes from this leg rather than the momentum leg,
  and it has never been tested as a condition on LONG's event rather than as a
  separate book.
- **Per-symbol coordination between the two sleeves.** They collide on 11
  name-days in 5.5 years; the per-sleeve capital partition in `account_kernel.py`
  budgets each sleeve separately and does not see combined per-symbol exposure.
  Small, but it is the only genuine coupling between them.

## What survived the audit

| Evidence | Decision-useful conclusion | Decision |
| --- | --- | --- |
| Strategy Overhaul V2 | About 29 families and more than 150 configurations exhausted the existing hourly entry/exit/sizing surface. Fixed-capital barebones books were approximately -3.23% LONG and -20.23% CONTINUOUS after modeled costs and funding. Full account parity was not established. | Stop tuning descendants of that surface. |
| Historical sleeve curves | Some historical curves are positive, but LONG is materially dependent on a small take-profit tail and CONTINUOUS does not have complete live-runtime reconstruction. | Keep as descriptive controls, not promotion evidence. |
| Breadth study | CONTINUOUS increased from about 6.55 to 7.30 bets per open day, but per-bet volatility was about 1,000 bp and average dependence about 0.21. A 25 bp effect would need roughly 5.6 years at that information rate. | Breadth alone is not a research direction. Fix quantization only as an execution-validity issue. |
| Young-listing lifecycle | The 2021-24 unconditional short effect reversed in 2025-26. A day-0 long was negative or flat. The required listing-week 1-minute cost data had zero symbol/date overlap with the 27,398-row event panel. | Retire calendar-age rules and the proposed T-L v2. |
| Execution cost | The first 23 measured demo fills showed positive 15-second/1-minute realized spread against our taker flow. The in-flow maker-first A/B froze at 2 of 8 fills when CONTINUOUS retired 2026-07-29 and was itself retired with the paper fleet 2026-08-03 (`docs/research/research_findings.md` §1). | Continue measuring execution separately; do not confuse cost improvement with alpha. |
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
corrected here. The current tiered census is `docs/data.md`.

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

Promotion requires the five-line note in `docs/research/governance.md`, a recorded
change point, stable demo execution, and an explicit replacement/migration
diff. Promotion means demo only. Mainnet still requires a separate owner
instruction naming the deployment and risk boundary.

### P4 — directions remain open

Crowding Transfer is one starting family, not a gate around creativity.
Price-independent funding/premium carry, cross-sectional transfer, execution
reversion, regime-conditioned sleeve redesign, or a mechanism not anticipated
here may be better. Revisit an old family only with a new mechanism, new data,
or a corrected defect—not another threshold sweep wearing a new name. True
cross-exchange execution is a new capability and stays simulation-only
until both legs, atomic failure handling, collateral fragmentation,
liquidation, transfer, and venue-outage risk are modeled and deliberately
authorized.

## Live task queue

The measured position this list starts from: the significance bar is **t >= 2.5**
since 2026-07-31 (`docs/research/governance.md` 2, owner decision), replacing the
family-wise t = 3.25 derived from a ~44-mechanism count that was never
enumerable. At the measured 15.56 bp round trip the anomaly-program signals are
t 1.30-2.06 and still do not clear it. The one thing that does is the
`lane2_carry_hold_v4` crowding-persistence size, whose capital-normalised
differential against v3 is t 3.23 on seen data — registered 2026-07-31 and
accruing forward days, not validated. Execution work cannot create an edge (its
ceiling is Sharpe 0.69 -> ~1.17). Completed items below are retained as the
evidence trail.


- [ ] **Settlement sawtooth program — OPEN 2026-07-31, dossier at
      `docs/research/archive/2026-08-01-settlement-sawtooth-program.md`.** Seven ranked hypotheses; that file
      carries the tables, the falsification tests (cross-venue replication
      passes, the deep-positive mirror does *not*), the already-closed dead ends,
      and the kill criteria. Order of work: **H5** (which mechanism — harvest
      flow vs squeeze-and-fade; produces no trade but decides which other items
      are worth running), then **H7** (fill scheduling for the live CARRY sleeve
      — cheapest, operational, no alpha claim), then **H2/H3** (entry and exit
      timing for the deployed book — unblocked, and read the degenerate-cycle-
      phase trap in H2 before building). **P0 is a data task:** `tick_ohlc_1m/`
      is empty and no script in `scripts/data/` fetches sub-hourly bars, so H1 —
      the highest-value item — is *blocked*, not refuted. Report every result as
      a sweep over the 24 grid phases, never a single clock.
- [x] **Enumerate the "~44 mechanisms", or stop quoting a threshold derived from
      them — CLOSED 2026-07-31 by taking the second option.** The bar is now a
      fixed t >= 2.5 owned by `docs/research/governance.md` 2, so no threshold in this
      program rests on the unverifiable count any more. `bonferroni_t` and
      `PRIOR_MECHANISMS` survive as reference numbers printed beside the bar, not
      as the pass/fail rule. The original defect statement follows.
      ORIGINAL: Found 2026-07-30 while setting the bar for the idio screen: no
      artifact in the tree or in git history lists them. `scripts/research/screen_phase1.py`
      and four configs all *assert* the count, none enumerates it. Every Bonferroni threshold this programme quotes — the
      standing t = 3.25, and the 3.46 / 3.57 the idio screens derived from it —
      therefore rests on an unverifiable denominator, and whether a new grid
      overlaps something already inside the 44 cannot be checked. Either build
      the list (it is recoverable from the research docs and git history) or
      replace the count-based threshold with something auditable. This is a
      defect in the evidence standard itself, not in any one result.
- [x] **Idio charts — closed as a Sharpe upgrade for this book (2026-07-30).**
      `docs/research/archive/2026-07-30-idio-charts.md`. Pre-declared 48-cell grid over
      the Bybit full-PIT panel (2023-06-01..2026-06-30, 1,126 days, 880
      symbols): **0 cells profitable in their best direction and clearing
      t > 3.46** on measured turnover; max t anywhere is 1.90. Idio beats the
      information-matched (3-day-lagged) raw control on 2/6 features, 1/6
      era-stable. A demean-only control arm decomposes the null: de-marketing
      buys a median −0.032 Sharpe and factor-stripping on top +0.083 median /
      −0.057 mean — a decile long/short is already market-neutral, so
      residualising before ranking sells it something it has for free.
      COMMON4 explains only **6.0%** of daily cross-sectional dispersion.
      Corroborates two deleted June-2026 receipts recovered from git
      (`rmom-latency-falsification-2026-06-09`,
      `intraday-residual-scout-2026-06-10`): residualisation yields a real
      signal that does not pay this cost stack.
      **Mode (b) run and the declared kill condition FIRED:** the BTC-beta-hedged
      book (only `btc_beta` has a tradable instrument; the three rank factors do
      not) improves net Sharpe in 3/24 cells, median Δ −0.183, 0/24 profitable
      and clearing the bar. Inside that null: the decile books are *not*
      beta-neutral (|net_beta| 0.38–0.59 raw) and residualising genuinely
      de-betas them (0.26–0.39 idio) — the construction works, it just does not
      pay. **Momentum arms re-run on a momentum-free factor set** (`nomom3`,
      COMMON4 minus `xs_rank_ret_30d`, which had made those arms circular):
      idio beats the control in 1/6 rather than 2/6, so the defect was real and
      not load-bearing.
      Two reusable defects were found and added to the failure taxonomy as
      items **34** (log returns as a P&L target — a −34.76 bp/day variance drag
      that manufactured an apparent Sharpe 4.46) and **35** (full-rebalance cost
      models). Item 34 was then reintroduced by this same work via a column
      rename and caught by a negative R²; read it as a naming discipline.
      **The directional single-name book — the claim's strongest form — was also
      run and fails harder.** `pos = sign(60d per-symbol z-score)`, no
      cross-sectional information, so common-factor motion does not cancel:
      raw arms carry |net_beta| 0.82–0.84 (3× the decile book) and idio arms cut
      it to 0.22–0.40, so the mechanism works — but median Δ Sharpe
      (idio − control) is **−0.572** and 0/24 hedged cells clear |t| > 3.57.
      Residualising performs *worse* in the construction that theoretically
      favours it. That is what closes the programme rather than merely bounding
      it to one book. Across all three screens: **0 of 96 pre-declared cells are
      profitable and significant.**
      New: `liquidity_migration/research/panels/residual_price.py`,
      `liquidity_migration/research/panels/idio_features.py`, `scripts/data/build_idio_panel.py`,
      `scripts/research/screen_idio_charts.py`, `scripts/research/screen_idio_hedged.py`,
      `scripts/data/diagnose_idio_panel.py`.
- [x] Collapse old evidence into decision-useful priors.
- [x] Falsify simple young-listing continuation and mature turnover-decay rules.
- [x] Verify a viable long-history cross-venue premium/funding overlap.
- [x] Build the minimal P0 causal substrate and publish its coverage map.
      `liquidity_migration/research/panels/cross_venue_panel.py` +
      `scripts/data/build_cross_venue_panel.py`, built 2026-07-24 over the
      both-venue population from `2021-01-01`. Coverage lives in each shard's
      `manifest.json`; the two source defects it exposed (`open_interest_value`
      is contract units, `funding_event_kind` on 2 of 2,024 partitions) are in
      `docs/research/research_findings.md` §4.
- [x] Produce the P1 anomaly search with the full log, and consolidate it.
      `docs/research/archive/2026-07-24-anomaly-research.md` — 37 mechanisms tested identically.
      Survivors are cross-venue premium divergence and 1-week cross-sectional
      momentum, both concentrated in the *most* liquid names and effectively
      uncorrelated (+0.009). Funding carry broke in 2025-26 exactly when funding
      inverted. The 24h-display rollover is a confirmed mechanism that does not
      pay. The edge is non-monotone — essentially all of it is the short leg. Venue
      volume-share migration — the most direct test of the Crowding Transfer
      starting hypothesis below — is dead; the price dislocation pays, the flow
      migration does not. Scoring primitives are
      `liquidity_migration/research/panels/cross_section.py`.
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
      executable as `liquidity_migration/research/panels/lane2_blend.py`. Daily, top-100 Bybit,
      50/50 premium + 1-week momentum continuation, settlement-exact funding, 15% vol
      target; no dispersion gate, no Binance leg, no maturity filter. Per
      `docs/research/governance.md` the commit is the registration; it grades forward from
      that commit on days it never saw.
- [x] **2026-07-26 financed-longs program**: three Lane-2 registrations
      (`lane2_carry_hold_v1`, `lane2_financed_leaders_v1`,
      `lane2_financed_leaders_binance_v1`) against the regenerated CONTINUOUS
      sl35 benchmark (Sharpe 1.84, +15.85%) at measured costs. On the
      full-calendar basis the two Bybit books beat it on return AND Sharpe; the
      Binance replication arm beats on return only (Sharpe 1.66 vs 1.84) — see
      the registration block above, which has said so since the same-day
      correction. Module `liquidity_migration/research/backtest/financed_longs.py`, reproduction
      `scripts/research/screen_financed_longs.py` (reproduces the registered table
      directly since the 2026-07-27 M19 turnover fix), evidence
      `docs/research/archive/2026-07-26-financed-longs.md` with the 22-row
      negative-results ledger.
- [ ] Score the six registered financed-longs configs on each new completed UTC day
      (`lane2_carry_hold_v1/v2/v3`, `lane2_funding_spread_v1`,
      `lane2_financed_leaders_v1`, `lane2_financed_leaders_binance_v1`;
      `DEFAULT_CONFIGS` in the scorer is the list)
      (rolling forward record; the registration commit is the change point;
      since 2026-07-28 the scorer charges each settlement exactly once;
      `lane2_carry_hold_v2`/`v3` score beside v1 with the paired daily
      differentials v2−v1 and v3−v2 as the primary comparisons). Tooling:
      `scripts/research/score_financed_longs_forward.py` appends
      `reports/financed_longs_forward/ledger.csv` (append-first, idempotent,
      `forward_eligible` flagged). Panel refreshed through 2026-07-27 on
      2026-07-28; the daily sequence is research-refresh → panel 2026
      rebuild (`scripts/data/build_cross_venue_panel.py --start 2021-01-01`,
      full rebuild — the index is whole-file) → ledger append.
- [ ] Re-derive the settlement-exact surfaces on the corrected scorer:
      the `lane2_premium_momentum_blend_v1` table, the anomaly-research
      funding-leg numbers (leg-attribution reversal, dispersion-gate
      withdrawal), and financed-longs negative-ledger rows 1/2/13–17/20
      (2026-07-28 double-count correction).
- [ ] Score the venue-scoped CONTINUOUS admission variant beside the shipped
      shape on post-2026-07-27 days
      (`scripts/research/render_continuous_admission_variants.py admission --end-date …`;
      registered 2026-07-27, evidence
      `docs/research/archive/2026-07-27-continuous-ladder-mechanism.md` §5 — the 2025-carried
      era profile is the thing the forward record must test).
- [ ] Measure realised maker-fill probability in flow (target was 100 fills
      per arm; the retired paper-owner A/B froze at 2 of 8). This is the last
      unmeasured cost input. **Blocked, not pending:** it now needs a
      re-scoped in-flow arm on a live sleeve, or acceptance of the
      probe-only bound (`probe_passive_fill_ab.py`).
- [ ] Orthogonalise `basis` against `premium_diff` — they are one family and
      should not be double-counted.

No other strategy task list is active.
