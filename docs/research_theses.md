# Research theses — measured, not registered

Ideas that have been built and measured but are **not** a deployed sleeve and, in
most cases, not a registered config either. Each entry says what it is, what was
measured, and the specific thing that keeps it out of the book.

This file exists so a promising-looking number is never re-discovered without its
disqualifying context attached. It is not a queue — the active queue is
[`strategy_program.md`](strategy_program.md). Confirmed dead ends belong in the
do-not-retest ledger in [`research_findings.md`](research_findings.md) §2, not
here; this is for things that *work* and still are not run.

Nothing here has a forward record. Every number is Lane-1 simulation on data that
also shaped the idea, under [`governance.md`](governance.md).

---

## 1. `momentum_1w` — the cross-sectional 1-week momentum leg

**What it is.** Rank the top-100 Bybit names by 168-hour return each day, buy the
top decile, short the bottom decile, equal-weight, hold 24h, rebalance. Market
neutral by construction. It is one leg of the registered
[`lane2_premium_momentum_blend_v1`](../configs/lane2_premium_momentum_blend_v1.json)
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

## 2. `lane2_financed_leaders_v1` — momentum crossed with the financing condition

**What it is.** Registered
([config](../configs/lane2_financed_leaders_v1.json)), long-only: the top decile
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

## 3. The liquidity screen inside carry-hold

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

## 4. Capital efficiency — the largest unexploited lever, and it is not alpha

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

## 5. Open, unmeasured

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
