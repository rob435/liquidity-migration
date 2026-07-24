# Anomaly research — 2026-07-24

Consolidated record of the anomaly sweep run on the cross-venue panel built the
same day. Supersedes the two working documents it replaces (`anomaly_atlas` and
`anomaly_leads_batch2`, both removed; recoverable from Git history). Thirty-seven
mechanisms tested under one harness. Two survived every screen.

**Labelling, per `docs/governance.md`.** All of this is **Lane-1 exploration on
already-seen data**. These runs selected these results and therefore cannot
grade them. Nothing here is a validated alpha claim, authorizes a deployment, or
touches the real-money boundary. A Lane-2 verdict needs a committed config and
scorer graded on days that postdate the commit.

Three claims in the working documents were **wrong and are corrected here**
(§6). Two of the three were my own leads, and both corrections weaken them.

---

## 1. Method

One harness, applied without variation:

- Universe: the both-venue panel, `[2021-01-01, 2026-07-18)`, hourly.
- Signal ranked cross-sectionally **within each hour**, so every read is
  market-neutral by construction.
- Equal-weight long the bottom decile, short the top decile, each leg averaged
  separately and then differenced. A negative number means the effect runs the
  other way; **no sign is flipped to flatter a result**.
- Return is **price plus funding**, not price alone (§4.1 — this reversed a
  conclusion).
- Reported per era, with the round-trip cost that would break the trade.
- Scored through `liquidity_migration.cross_section`.

Costs use Bybit reference fees: **taker ~5.5 bp/side (11 bp round trip), maker
~2 bp/side (4 bp round trip)**. Convention is `1u long + 1u short`, i.e. one
unit of capital at 2× gross; halve every figure for a gross-normalised read.

## 2. What survived

Top-100 by turnover, hold 24h, net of approximated funding and 4 bp maker,
1,881 non-overlapping daily observations:

| Book | bp/day gross | bp/day net | Sharpe net | ann % net | max DD | tail conc. | hit % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| premium_diff | 34.62 | 30.62 | **1.26** | 111.8 | 124.3% | 12.4% | 52.7 |
| momentum_1w | 19.50 | 15.50 | 0.48 | 56.6 | 149.7% | 10.6% | 49.9 |
| **50/50 blend** | 27.06 | **23.06** | 1.14 | 84.2 | **60.1%** | 12.4% | 51.9 |

Correlation between the two is **+0.009** — effectively independent. The blend
buys its risk reduction from ordinary diversification, not from a hedging
relationship: drawdown falls to 60.1% from 124.3% and 149.7%, while Sharpe sits
*between* the two legs rather than above both.

**The blend is therefore not strictly better than premium_diff alone.** Premium
has the higher Sharpe (1.26 vs 1.14) and higher return; the blend has roughly
half the drawdown. Which to prefer is a risk-appetite decision that the data
does not settle — but since this project's stated problem is tail behaviour
rather than mean return, the blend is the more relevant object.

> **Superseded by §9.1.** The table above uses pro-rated funding. Under
> settlement-exact accrual the *attribution reverses* — the premium leg roughly
> halves and momentum roughly doubles — while the blend total barely moves. The
> blend is the robust object; the individual legs are not.

Era split of the blend, net of maker cost:

| | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| bp/day | **−17.17** | 8.02 | 26.98 | 8.08 | 39.74 | 86.67 |
| Sharpe | **−0.92** | 0.76 | 2.10 | 0.53 | 1.44 | 2.53 |
| days | 224 | 365 | 365 | 366 | 365 | 196 |

2021 is materially negative. The effect strengthens monotonically thereafter,
which is either a real change in market structure or the setup for a sharp
disappointment; nothing here distinguishes those.

### Why this matters for the tail problem

The audit's core finding was that the deployed book takes 22.4% of its losses in
1% of trades, worst case −249,169 bp, and that the tail is ~95% idiosyncratic
and un-hedgeable at book level. The blend is a **different risk object**: market
neutral, daily, top-100 only, both legs liquid, 12.4% loss concentration. That
is not a hedge of the old tail — it is a book that does not take it. See caveat
§7.6: its own drawdowns are large.

## 3. How the surviving book should be built

Two conditioners change the design materially. Both are point-in-time.

### 3.1 Dispersion gate — REJECTED (see §9.2)

Trading only when cross-sectional dispersion of `premium_diff` exceeded its
trailing 60-day two-thirds quantile appeared to lift Sharpe 1.27 → 1.74 and halve
drawdown. **That result does not survive settlement-exact funding.** Re-measured,
the gate gives Sharpe 1.30 against 1.29 ungated and a *worse* compounded drawdown
(51.6% vs 46.1%). It was an artifact of the pro-rated funding approximation and
is excluded from the committed config.

### 3.2 The edge is non-monotone and lives in the short leg

Decile means (net bp per 24h), low premium → high:

| 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| +9.1 | −6.5 | −7.1 | −8.9 | −7.6 | −8.3 | −12.4 | −11.0 | −11.5 | **−31.6** |

Deciles 1-8 are a flat −7 to −12 band. Essentially all the spread is **decile 9**
(short the richest cross-venue premium); decile 0 contributes only +9.1. A plain
rank-decile book wastes most of its gross on a flat middle. A sharper
short-tilted cut should dominate — but that reintroduces short tail exposure and
must be sized against the audit's tail findings rather than assumed free.

### 3.3 Secondary conditioners

- **Volatility de-grossing.** The edge *degrades* in high-vol regimes (Sharpe
  1.46 calm / 1.66 mid / **0.78 wild**) — the opposite of the usual "dislocation
  pays in chaos" intuition, and consistent with §3.1: what pays is *dispersion*,
  not volatility. Gate on dispersion, de-gross on volatility.
- **Funding-clock timing.** Entering 1h before a funding settlement beats
  mid-cycle (39.12 vs 32.76 bp/trade). A ~19% improvement for a free scheduling
  choice — small, but it costs nothing to take.
- **Hold 24h, not longer.** See §6.2: the apparent gain from week-long holds was
  a sampling artifact.

## 4. The screens, and what each killed

### 4.1 Funding P&L reversed a conclusion

Ranking a **funding-sorted** signal on price return alone is not a partial
answer, it is the wrong sign. Longing the most-negative-funding decile means
*receiving* funding; crediting none of it inverts the verdict:

| Signal | price-only | + funding | funding contribution |
| --- | ---: | ---: | ---: |
| funding_level | −19.93 (t −11.8) | **+8.57 (t +5.1)** | +28.50 |
| funding_chg24 | −15.83 (t −9.5) | +2.59 (t +1.6) | +18.41 |

The carry trade is real: price moves **against** you by ~20 bp/day and the
funding collected (~28 bp/day) more than pays for it. That also corrects the
mechanism I expected — it is not squeeze-reversal capture, it is pure carry.

Funding is approximated as `rate × hours/8` (the 480-minute standard interval),
not settlement-exact. Any decision-grade version must replay actual settlements.

### 4.2 Stale-price / lag screen

A cross-venue signal built from two hourly closes reverts mechanically if either
print is stale or bid-ask bounced — a large, highly significant, completely
untradeable effect. Screen: delay the signal and re-run.

| Signal | lag 0 | lag +1h | lag +4h | Survives |
| --- | ---: | ---: | ---: | --- |
| premium_diff | 24.08 (14.7) | 24.48 (15.0) | 23.69 (14.6) | **yes — no decay at all** |
| basis | 23.11 (14.2) | 19.72 (12.3) | 17.55 (11.0) | yes, mild decay |
| mom_168h | −22.32 | −22.32 | −20.52 | yes |
| funding_level | 8.57 | 7.40 | 5.42 | yes |
| **funding_chg24** | 2.59 (1.6) | 0.59 (0.4) | −2.18 (−1.3) | **NO — killed** |
| **mark_index_gap** | +29.96 (2.6) | **−17.91 (−1.6)** | +23.80 (2.1) | **NO — incoherent** |

### 4.3 Liquidity / capacity screen

The usual way a crypto cross-sectional result dies is that all of it lives in
the illiquid tail where a modelled 4 bp maker cost is fiction:

| Signal | Q1 thin | Q4 deep | top-50 | top-100 | top-400 |
| --- | ---: | ---: | ---: | ---: | ---: |
| premium_diff | −0.40 | **+19.65** | **+21.69** | +17.19 | +7.21 |
| momentum_1w | +4.84 | **+28.02** | **+23.86** | +15.61 | +5.95 |
| funding_carry | +4.99 | +1.69 (t 1.0) | +5.20 | +3.16 | +7.90 |

The two survivors behave **opposite to the usual failure mode** — strongest in
the *most* liquid names, weakening as the universe widens. That is capacity you
can actually use.

**Funding carry fails here**: it lives in thin names and dies in deep ones. Its
era split explains why — 12.6, 14.0, 10.5, 12.8, **−4.2, −52.1** bp/day for
2021→2026. It broke precisely when funding turned structurally negative in
2025-26 (the inversion measured in
`docs/audit/2026-07-24-repo-and-strategy-audit.md` §5). Internally consistent,
and a reason not to build on it now.

## 5. Negative results worth keeping

### 5.1 The 24h-display rollover — confirmed mechanism, negative trade

Tested from an external idea (robot james, "a truly idiotic crypto trade"):
exchanges show a trailing 24h % change; when a large candle rolls **out** of that
window the displayed number jumps with no new information, and retail chases the
display. A lag scan discriminates: generic reversion predicts a smooth profile,
the display artifact predicts a spike at the rolloff lag only.

| lag | 6 | 12 | 18 | 21 | 22 | **23** | 24 | 25 | 26 | 30 | 48 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| bp/hr | −0.77 | −0.35 | −0.25 | −0.60 | −0.59 | **+0.90** | −0.47 | −1.27 | −0.20 | +0.06 | +0.36 |
| t | −2.0 | −0.9 | −0.7 | −1.6 | −1.6 | **+2.45** | −1.3 | −3.5 | −0.6 | +0.2 | +1.0 |

An isolated spike at exactly lag 23, surrounded by negatives, that **scales
monotonically with the rolled-off candle's size** (1.14 bp for candles under 1%,
27.79 bp for candles over 10%) — what the mechanism predicts and coincidence
does not. **But it is not monetizable**: all the alpha is in hour 1 (3.06 bp at
h=1, 1.52 at h=3, −0.25 at h=6), so no hold amortises the round trip; the
sharpest 1% cut yields 3.82 bp/hour against a ~4 bp maker round trip; and it is
dead in 2023 (t 0.39) and 2024 (t 0.12). Logged as validation that the panel and
harness detect a known-shape effect where it should be.

### 5.2 Mark/index dislocation has been arbitraged out

`mark_index_gap` looked like a winner at 29.96 bp/day, Sharpe 1.15, t 2.61. It
fails the lag screen incoherently (§4.2 — a real effect decays smoothly; a sign
flip and recovery is noise) and decays by era to nothing: 76.2 → 36.2 → 18.7 →
35.9 → 16.6 → **0.4** bp/day for 2021→2026. That profile is what an
arbitraged-away inefficiency looks like. Useful as a control: the panel *can*
see a mark/index effect, and there is none left to trade.

### 5.3 Listing debut is a mean/median trap

First 24h return by contract age, all contracts:

| Age | 0-3 days | 3-7 days | 7-30 days | 30-90 days | 90+ (base) |
| --- | ---: | ---: | ---: | ---: | ---: |
| mean bp | **+21.2** | −16.4 | −2.3 | −10.2 | −3.7 |
| median bp | **−72.3** | −85.4 | −49.1 | −38.8 | −18.5 |

Positive mean, strongly negative median — most bleed, a few moonshot. That is the
exact payoff shape the audit warned about: shorting it harvests the median and
pays for the tail. It also explains why this repository's earlier listing-short
work (T-L) kept flipping sign by era — the result depends entirely on whether
the sample caught a moonshot.

### 5.4 Twelve mechanisms are dead

Tested identically, top-100, hold 24h, net of funding and maker. None reached
|t| ≥ 2:

| Signal | bp/day | t | | Signal | bp/day | t |
| --- | ---: | ---: | --- | --- | ---: | ---: |
| amihud illiquidity | −18.37 | −1.30 | | premium momentum (Δ, not level) | 3.70 | 0.35 |
| venue volume-share shift | −10.83 | −1.31 | | basis instability | 7.32 | 0.71 |
| funding dispersion (cross-venue) | −10.44 | −1.03 | | close location in range | 1.13 | 0.11 |
| jump frequency | −10.18 | −0.94 | | OI/turnover ratio *(survivor)* | 4.27 | 0.35 |
| funding-vs-premium dislocation | −8.74 | −0.83 | | OI/price divergence *(survivor)* | 3.80 | 0.34 |
| vol term structure | −7.29 | −0.66 | | stale-quote ratio | −4.12 | −0.60 |

Two deserve a note. **Venue volume-share shift** was the most direct possible
test of the "crowding transfer" hypothesis that `docs/strategy_program.md` names
as its starting family — flow migrating between venues — and it is dead. The
*price* dislocation between venues pays; the *flow* migration does not. And
**premium momentum** (the change) is dead while premium *level* is the strongest
survivor, which argues the effect is a convergence force, not a trend.

### 5.5 What needs data we do not have

- **Liquidation feed.** Every practitioner source on cascades keys off forced
  liquidation prints. We hold none. Highest-value missing dataset.
- **Multi-venue funding dispersion.** Published work spans 26 exchanges; we have
  two. Two venues give a difference, not a distribution — and §5.4 shows the
  two-venue version is dead.
- **Delisting announcements.** See §6.1.
- **Sub-hourly data.** The rolloff mechanism is real but decays inside an hour.
  Unreachable with hourly bars; local tick datasets are event windows, not
  panels (`docs/audit/2026-07-24-repo-and-strategy-audit.md` §6, Tier D).

## 6. Corrections to the superseded working documents

### 6.1 Delisting decay is not reachable — the lead is withdrawn

The working document reported shorting contracts 3-14 days before their final
bar at 220.8 bp/day, t 9.94, and called it the largest raw effect found. That
label is look-ahead: at trade time you do not know a contract's final bar.

Testing whether any point-in-time trigger reaches the same effect:

| Check | Result |
| --- | ---: |
| Rows with turnover < 30% of trailing 30d median that are dying contracts | 12.7% |
| Base rate of dying contracts | 13.3% |
| **Lift** | **0.96×** |

A turnover collapse is **worse than random** at identifying a contract about to
be delisted. The control settles it: restricting to contracts that **never
died**, so no delisting can possibly be involved, the same trigger pays
**+38.0 bp/day (t 4.26)** — *stronger* than on the full population. Whatever the
residual is, it is generic "short low-turnover falling coins", not a delisting
effect. **The lead is withdrawn**, and no announcement-lead-time check will
rescue it, because the PIT proxy never found the delistings in the first place.

### 6.2 The horizon result was an overlap artifact — hold 24h, not a week

The working document reported the premium edge's t-stat *rising* with horizon
(0.44 at 1h → 5.90 at 168h), read that as slow convergence, and recommended a
weekly book. Those reads sampled entries **daily** while holding up to 168 hours,
so consecutive observations shared up to 167 hours of the same return. Overlap
does not bias the mean; it badly inflates the t-stat.

Re-sampling so holding windows are **disjoint** (entries every h hours):

| Hold | 1h | 6h | 24h | 72h | 168h |
| --- | ---: | ---: | ---: | ---: | ---: |
| t (overlapping, as published) | 0.44 | 0.87 | 3.26 | 4.10 | **5.90** |
| **t (disjoint, honest)** | **−5.69** | 2.82 | **3.48** | 1.91 | 1.18 |

The t-stat **peaks at 24h and declines**. The rise to 5.90 was entirely overlap.
The "hold it for a week at one-seventh the turnover" recommendation does not
survive, and the 73.1%/yr weekly figure should not be quoted. **24h is the right
holding period**, which is what §2 and §3.1 already use.

### 6.3 The pooled-leg harness bug

The first version of the §2 numbers pooled both legs into a single mean and used
an uncentred percentile (`rank/len` runs 1/n..1 rather than being symmetric
about 0.5), which hands the two tails different name counts and quietly makes a
supposedly neutral book directional. Re-run through the corrected, tested
`cross_section` primitives, three things changed: the pair correlation is
**+0.009, not −0.285** (uncorrelated, not negatively correlated); magnitudes
roughly doubled because the spread convention no longer halves the result; and
**2021 is negative**, so the earlier "positive in all six years" claim was wrong.

§4's screening tables were produced with the same pooled convention. The bug
shifts magnitudes and can shift a marginal t-stat; it does not plausibly reverse
the large, consistent effects it was used to rank, but surviving candidates
should be re-scored through `cross_section` before any Lane-2 commit.

## 7. Caveats

1. **Lane-1 on seen data.** These runs chose the survivors, so they cannot grade
   them. Commit a config and scorer, then grade forward.
2. **Funding is approximated**, not settlement-exact (§4.1).
3. **Cost model is a flat 4 bp maker round trip** with no slippage, impact,
   partial fills, or maker non-fill risk. A maker-first book that misses fills
   has different economics — which is exactly what this repository's existing
   passive-execution A/B was built to measure. Use it before believing the net
   numbers.
4. **2026 carries an outsized share** (41.99 bp/day, Sharpe 2.54) on 196 days.
   Could be regime, could be luck.
5. **premium_diff supplies the return; momentum supplies the diversification.**
   If premium_diff is a market-structure inefficiency, it can close — and its
   2021 sign was negative.
6. **Drawdowns are large and the metric is arithmetic.** `max_drawdown_pct` is
   computed on a cumulative-sum equity path, not a compounded one, so the >100%
   single-leg figures are not literal — but they do mean that at 2× gross those
   books would have been liquidated at some point. Any serious version needs
   compounded accounting and a volatility target before these Sharpe numbers
   mean anything about a survivable book.
7. No margin, liquidation, borrow, or venue-outage modelling. Both legs are
   assumed executable on Bybit.
8. `basis` was not carried into §2 despite scoring similarly to premium_diff; it
   is highly correlated and the two should be tested as one family, not
   double-counted.
9. OI-derived signals run on a **survivorship-contaminated** panel (95.9% of
   with-OI symbols are still listed versus 0.0% without) and are marked
   *(survivor)* wherever they appear. They are not population claims.

## 8. Next discriminating tests

1. Build the **daily, dispersion-gated, short-tilted** premium book properly and
   replay it through the account journal with settlement-exact funding and the
   measured execution-cost model, not the flat 4 bp.
2. Decompose premium_diff: is it Binance leading price discovery, or Bybit-local
   flow reverting? Those imply different capacity and different decay.
3. Test whether entries survive a **maker-fill probability model** — the alpha is
   ~23 bp/day at 2× gross; a 30% non-fill rate on the good side would matter.
4. Rebuild `basis` and `premium_diff` as one orthogonalised family (§7.8).
5. Only then consider a Lane-2 commit.

---

## 9. Follow-up, 2026-07-24 (second pass)

Four checks run after the first draft. Three overturn a published claim.

### 9.1 Settlement-exact funding reverses the leg attribution

Funding was approximated as `rate × hours/8`, pro-rating whatever rate is current
across the whole hold. Real funding is a discrete cash event at 00/08/16 UTC.
Recomputed so funding accrues **only at settlements falling inside the hold**:

| Leg | pro-rated (published) | settlement-exact | change |
| --- | ---: | ---: | --- |
| premium_diff | 33.63 bp, Sharpe 1.36 | **16.55 bp, Sharpe 0.66** | roughly halved |
| momentum_1w | 16.98 bp, Sharpe 0.52 | **35.42 bp, Sharpe 1.08** | roughly doubled |
| **blend** | 25.31 bp, Sharpe 1.23 | **25.99 bp, Sharpe 1.24** | unchanged |

The published construction reproduces exactly at "no maturity filter + pro-rated
funding", so this is a like-for-like comparison, not a different book.

This makes sense mechanically: funding is derived from the premium index, so a
premium-sorted book *is* implicitly a funding-sorted book — precisely the trap §4.1
warned about, which then caught the premium signal itself. **§2's claim that
"premium_diff supplies the return; momentum supplies the diversification" is
backwards.** The blend total is robust to the funding treatment; the legs are not.

### 9.2 The dispersion gate does not survive

| Book | n | bp/day | Sharpe | compounded DD | in market |
| --- | ---: | ---: | ---: | ---: | ---: |
| ungated | 1,822 | 26.95 | 1.29 | 46.1% | 100% |
| gated (dispersion ≥ trailing 60d ⅔ quantile) | 664 | 31.56 | **1.30** | **51.6%** | 36% |
| inverse (low dispersion only) | 1,158 | 24.31 | 1.29 | 44.0% | 64% |

All three Sharpes are identical to two decimals and the gate's drawdown is
*worse*. The reported "1.27 → 1.74, drawdown halved" was an artifact of the
funding approximation. **Rejected.**

### 9.3 The book is survivable — the earlier liquidation concern was misplaced

Caveat §7.6 warned that >100% arithmetic drawdowns implied liquidation at 2×
gross. That applied to the **single legs**, not the blend, whose arithmetic
drawdown was always ~55–60%. On a compounded path with settlement-exact funding:

| Configuration | Sharpe | arith DD | compounded DD | worst day |
| --- | ---: | ---: | ---: | ---: |
| raw (2× gross) | 1.24 | 54.7% | 46.1% | −29.17% |
| gross-normalised (1×) | 1.24 | 27.3% | 24.7% | −29.17% |
| **vol-target 15% ann, cap 3×** | **1.59** | 14.4% | **13.6%** | — |

**The book is never wiped out**: worst single day −29.17%, no day below −50%.
Compounded drawdown is *lower* than arithmetic because gains compound. Volatility
targeting is the single best risk improvement available — Sharpe 1.24 → 1.59 and
drawdown 46% → 14% — and unlike the dispersion gate it survives scrutiny.

### 9.4 The edge is Bybit-local; no cross-venue capability is needed

Same premium decile book, each venue net of **its own** settlement-exact funding:

| Hold | Bybit (net) | Binance (net) | 50/50 both |
| --- | ---: | ---: | ---: |
| 4h | 4.96 (t 1.06) | −1.49 (t −0.33) | 1.73 (t 0.38) |
| 12h | 4.13 (t 0.48) | −4.81 (t −0.58) | −0.34 (t −0.04) |
| **24h** | **23.81 (t 2.06)** | 11.42 (t 1.01) | 17.62 (t 1.55) |
| 72h | 17.43 (t 0.83) | −3.92 (t −0.19) | 6.76 (t 0.33) |

**Bybit carries the return; Binance net of its own funding earns nothing
significant.** Adding a Binance leg *dilutes* the book (17.62 < 23.81). So the
answer to "is Binance leading price discovery, or is Bybit-local flow
reverting?" is **the latter** — and the practical consequence is that the true
cross-venue execution capability this repo does not have is **not worth
building** for this signal.

A first pass appeared to show the opposite (Binance 39.40 vs Bybit 23.81), but
that charged funding to Bybit only. Comparing a net leg against a gross one is
not a comparison.

Two sobering notes: the Bybit premium leg is marginal at t 2.06 and unstable
across horizons (1.06, 0.48, 2.06, 0.83), and 24h is the only horizon where it
clears t = 2.

### 9.5 What this changed

The committed configuration is
`configs/lane2_premium_momentum_blend_v1.json`, executable as
`liquidity_migration/lane2_blend.py`: daily, top-100 Bybit, 50/50
premium + momentum-reversal, settlement-exact funding, 15% volatility target,
**no dispersion gate**, **no Binance leg**, **no maturity filter**. Under
`docs/governance.md` the commit is the registration; from that commit forward it
grades itself on days it never saw.
