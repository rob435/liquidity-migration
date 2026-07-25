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

The audit found that **a short book in this universe** takes 22.4% of its losses
in 1% of trades, worst case −249,169 bp, and that the tail is ~95% idiosyncratic
and un-hedgeable at book level. The blend is a **different risk object**: market
neutral, daily, top-100 only, both legs liquid, 12.4% loss concentration.

> **Correction, 2026-07-24.** An earlier version of this paragraph described that
> statistic as a property of *the deployed book*. It is not. It is the payoff
> geometry of hypothetical short positions across the research universe. Measured
> directly (`scripts/equity_curves.sh`, full PIT), the deployed sleeves carry no
> such tail: LONG is **long-only** with max drawdown −4.11% and worst month
> −3.06%; CONTINUOUS has max drawdown −1.29% and **zero days worse than −1%**.
> Neither is decaying — both are stronger in the second half of their samples.
> The premise that this research was replacing a broken deployed book was wrong;
> see §10.

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
premium + momentum-continuation, settlement-exact funding, 15% volatility target,
**no dispersion gate**, **no Binance leg**, **no maturity filter**. Under
`docs/governance.md` the commit is the registration; from that commit forward it
grades itself on days it never saw.

---

## 10. The deployed sleeves are not spent (2026-07-24, third pass)

Prompted by a direct challenge — *is the current signal fully spent?* — the two
deployed profiles were measured with the repository-standard tooling
(`scripts/equity_curves.sh`, `~/SHARED_DATA/bybit_full_pit`, full PIT) rather
than assumed dead. They are not spent. Both are **improving**.

### LONG (`LongV11aDivWeekendVol`), long-only, 3× cost multiplier

2021-01-02 → 2026-07-17: **+40.73%**, Sharpe-like **1.60**, max drawdown
**−4.11%**, worst month −3.06%, 292 trades, win rate 51.4%, profit factor 1.83.
Gross +42.35%, cost −6.69%, funding −1.04%.

| 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| +4.16% | −0.76% | +11.50% | +12.80% | +4.30% | +3.83% |
| Sh 1.57 | −0.25 | 1.01 | 1.57 | 0.92 | **2.60** |

First half +15.64%, second half **+21.75%**.

### CONTINUOUS (`continuous_ensemble_v2`), 1× modeled

2023-03-13 → 2026-07-16: **+23.42%** (6.49%/yr), Sharpe **2.74**, max drawdown
**−1.29%**, MAR 5.41, worst day −0.93%, **zero days below −1%**.

| 2023 | 2024 | 2025 | 2026 |
| ---: | ---: | ---: | ---: |
| +3.58% | +4.91% | +7.22% | +5.93% |
| Sh 2.99 | 2.68 | 4.92 | **5.15** |

First half +6.97%, second half **+15.38%**.

### What this changes

**The replacement premise was wrong.** Against the Lane-2 blend registered in §9:

| | Sharpe | max DD | return |
| --- | ---: | ---: | ---: |
| Deployed LONG | 1.60 | 4.11% | ~6.4%/yr |
| Deployed CONTINUOUS | 2.74 | 1.29% | 6.49%/yr |
| Lane-2 blend, vol-targeted | 1.59 | 13.6% | 29%/yr |

The new blend is **not better risk-adjusted than either deployed sleeve**, and
CONTINUOUS beats it substantially. The blend's higher return is bought entirely
with risk, not edge.

The binding constraint is therefore **risk utilisation, not signal quality**.
Both sleeves run at 1–4% drawdown against an account that could tolerate far
more. Closing that gap is a cheaper source of return than any new book.

### Do not take these numbers at face value

Sharpe 2.74–5.15 with sub-1% drawdowns is *too good*, and should raise suspicion
rather than confidence:

- **`funding=partial` on all three CONTINUOUS components.** §9.1 established that
  funding treatment alone can halve or double a leg. A partially-modelled funding
  input is the single most likely source of flattery here, and it is the first
  thing to check.
- CONTINUOUS covers only 646 days from 2023-03-13, not the requested 2021 start.
- Both are **Lane-1**: these profiles were selected on this history and cannot
  grade themselves. The run labels are `exploratory` and `historical_equity`.
- Neither reconstruction is a literal daemon replay; capacity, netting, order
  lifecycle, and live state can differ (`docs/active_trading_logic.md`).
- The 4× CONTINUOUS chart is **presentation leverage only** — it models no margin
  or liquidation and must never be quoted as a modelled result.

The honest next step is to resolve `funding=partial` before treating either
sleeve's Sharpe as real, and to run `scripts/check_kill_criteria.py` against the
live journal, which needs VPS access.

---

## 11. The tail is structural, and it is fixable (2026-07-25)

Operator report: **demo reconciliation shows realised fees materially above the
modelled cost, and forward testing produced large losses.** That is ground truth
and it outranks every backtest in this document, including §10's.

### 11.1 Cost error alone does not explain a losing book

Decomposing the CONTINUOUS reconstruction (646 days, 1×):

| component | contribution |
| --- | ---: |
| gross | +25.07% |
| funding | −4.08% |
| hedge, net | +2.33% |
| **modelled trading cost** | **−2.19%** |
| net | +21.13% |

Modelled trading cost is **8.7% of gross** — implausibly cheap for a book that
turns over ~3.7 trades/day. But the sensitivity says something important:

| cost multiple vs model | 2× | 3× | 5× | 8× | **10.6×** | 15× |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| net | +18.9% | +16.8% | +12.4% | +5.8% | **+0.1%** | −9.5% |

Costs would have to be **10.6× the model** merely to reach break-even. So if
forward testing is *losing*, cost error is not a sufficient explanation on its
own. Either the signal does not survive out of sample, or the tail events do the
damage — and the operator's report of "massive losses" points at the tail.

### 11.2 Replacing the idiosyncratic short with a basket short halves the tail for free

Same signal throughout, **only the short leg's construction changes**. Top-100,
24h disjoint holds, settlement-exact funding, 4 bp maker, 1,952 days:

| variant | bp/day | Sharpe | worst day | worst 1% | loss conc. | max DD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A baseline — short the decile | 39.55 | 1.20 | −35.37% | −20.99% | 10.8% | 83.6% |
| **B — short an equal-notional basket** | 38.62 | **1.55** | **−17.50%** | **−12.12%** | **8.6%** | **64.4%** |
| C — short the decile minus "crowded" names | 37.82 | 1.13 | −43.63% | −23.91% | 12.0% | 86.5% |
| D — half decile, half basket | 39.09 | 1.40 | −25.82% | −15.26% | 9.4% | 73.2% |

**Variant B gives up 0.93 bp/day of mean — 2.4% of the return — and removes half
the tail.** Worst day −35.4% → −17.5%, worst 1% −21.0% → −12.1%, max drawdown
83.6% → 64.4%, Sharpe 1.20 → 1.55. D is the dial between them.

This is exactly what the audit's mechanism predicts. The tail is ~95%
idiosyncratic, so a short leg with **no idiosyncratic exposure** cannot carry it.
It also explains why the deployed LONG sleeve, being long-only, has a −4.11%
drawdown while a symmetric book does not.

**The intuitive fix fails.** Screening "crowded" names (top-quintile funding) out
of the short leg made the tail *worse* on every measure — worst day −43.6% and
the highest loss concentration in the table. Funding percentile is not a squeeze
predictor; removing those names removes diversification without removing hazard.

The generator is visible directly: the worst single name in the short leg on a
given day averages **−7.65%** and reaches **−90.13%**, with 94 days below −25%
and 13 below −50%. An equal-weight leg of ~10 names passes a tenth of that
straight through. A basket short has no such name.

### 11.3 Correction: the momentum leg is continuation, not reversal

`cross_section.long_short` defines `sign=+1` as "long the LOW end", so the
committed `sign=-1` on `momentum_1w` means **long recent winners, short recent
losers** — momentum *continuation*. Earlier drafts and the config comment called
it reversal, which is backwards. Verified directly: the reversal direction earns
−40.73 bp/day (Sharpe −1.23); continuation earns +39.55 (Sharpe 1.20).

This also softens the tail question for that leg specifically: it shorts recent
*losers*, which squeeze less violently than recent winners. Config, module, and
the strategy program are corrected.

### 11.4 What to do about CONTINUOUS

1. **Re-cost the book from the demo reconciliation**, not from a multiplier. The
   measured `execution_cost_model` decomposition (effective spread, impact,
   realised spread) already exists; the 2.19% figure should be replaced by
   realised fills before any further judgement of the sleeve.
2. **Convert the short leg to a basket/index short**, or move part-way with
   variant D. This is the highest-value structural change available: it targets
   the mechanism the audit identified, costs almost nothing in mean return, and
   is testable on the deployed component book before any deployment.
3. **Do not add a crowding screen** on funding percentile. Measured, it makes the
   tail worse.
4. These are Lane-1 structural results on a cross-sectional proxy, not on the
   CONTINUOUS component book itself. The same comparison should be run on the
   deployed components before anything changes.

---

## 12. Realised cost, measured from the forward journal (2026-07-25)

The operator's report — realised fees materially above model — is now measured
rather than assumed. Source: the archived pre-reset demo journal
(`ledger-reset-20260722T213413Z-owner-authorized-full-reset-20260722.tar.gz`),
33,666 events, **85 fills**, 4,406.62 USDT notional, read through
`liquidity_migration.account_kernel` on the VPS and analysed read-only.

| | |
| --- | ---: |
| Notional-weighted fee | **7.78 bp/side** |
| Median fill | **11.00 bp/side** |
| Range | 5.50 – 11.00 bp/side |
| **Implied round trip** | **15.56 bp** |
| Research assumption | 4.00 bp (maker) |
| **Ratio** | **3.89×** |

The distribution is not noise: it sits exactly on Bybit's taker tiers (5.50 and
11.00 bp). **Fills are being priced as taker, not maker.** The research books
assume maker-first execution that is not happening.

### 12.1 What this does to the registered Lane-2 config

| cost basis | bp/day | Sharpe | vol-targeted Sharpe | compounded DD |
| --- | ---: | ---: | ---: | ---: |
| 4.00 bp (as registered) | 25.99 | 1.24 | 1.59 | 13.6% |
| 11.00 bp | 18.99 | 0.91 | 1.17 | 16.4% |
| **15.56 bp (measured)** | **14.43** | **0.69** | **0.89** | **19.1%** |

The config loses **44% of its mean and roughly half its Sharpe**, and its
drawdown gets *worse*. Break-even is 29.99 bp/day of round-trip cost. The config
is retained and its registration stands — but at the honest cost basis it is
marginal, not good. The measured basis is recorded in the config itself.

### 12.2 What it does to CONTINUOUS

Applying the same 3.89× to the modelled 2.19%: net over 646 days falls from
+21.13% to **+14.80%**, or 8.36%/yr. Still positive. **Fee error alone does not
explain forward losses** — consistent with §11.1, where break-even needed 10.6×.
The tail remains the better explanation, which is what the §11 experiment tests.

### 12.3 Kill criteria: no trip, but no sample either

`ops.sh kill-criteria` against the live journal returns **NO TRIP** — and that
verdict is empty. The 2026-07-22 reset restarted the record, so at 5.39 forward
days CONTINUOUS has **0 attributed round trips** and LONG has **1** (net −0.53
USDT). K2 and K3 do not evaluate until 2026-10-17. This is not evidence of
health; it is evidence of no data.

### 12.4 Attention/salience alphas: all dead at realistic cost

Seven salience features in the family of the 24h-display rollover, same harness,
top-100, disjoint 24h holds, settlement-exact funding, 2,021 days:

| feature | bp @ 4 bp | t | bp @ 15.56 bp | t |
| --- | ---: | ---: | ---: | ---: |
| displayed 24h gainers rank | 6.57 | 0.47 | −4.99 | −0.35 |
| most-traded (volume) rank | 14.14 | 1.62 | 2.58 | 0.30 |
| proximity to round price anchor | −0.88 | −0.12 | −12.44 | −1.70 |
| closeness to trailing 30d high | −13.06 | −1.04 | −24.62 | −1.95 |
| consecutive-move streak | −20.33 | −2.13 | −31.89 | −3.33 |
| 24h-display rollover (control) | 12.06 | 0.91 | 0.50 | 0.04 |
| crossed the ±10% UI highlight | −16.05 | −1.29 | −27.61 | −2.22 |

Two looked alive in their inverted direction (streak, ±10% threshold). Both
**collapse on a controlled sample**: restricted to contracts with a full 168h of
history — the same universe the registered momentum leg uses — streak falls to
−2.41 bp (t −0.26) and the threshold to −1.62 (t −0.13). The apparent effect
lived entirely in young, short-history contracts, which is the listing mean/median
trap of §5.3 in another costume. Their book returns correlate 0.33–0.44 with the
registered momentum leg, so they were never independent anyway.

**No salience feature survives.** The 24h-display rollover remains what it was: a
confirmed mechanism that does not pay. Even the registered 1-week momentum leg is
only t 1.65 (Sharpe 0.73) at the measured cost on this sample.

### 12.5 The honest summary

The measured cost basis is the most consequential number found in this document.
It does not merely trim results — it removes most of the research pipeline,
including a large part of the config registered a day earlier. The single
highest-value open question is therefore **not another signal**: it is whether
maker fills are achievable at scale, because that alone moves the round trip from
15.56 bp back toward 4 bp and restores everything above. That is precisely what
`passive_execution_experiment_2026-07-20.md` was built to answer, and reading it
is now the top priority.

---

## 13. The passive-execution A/B, read at last (2026-07-25)

Read from the archived pre-reset **paper** journal (231 files, 73 arm-tagged
fills), the instrument built to answer whether maker-first execution is
achievable. It has never been read until now.

| arm | fills | notional | fee/side |
| --- | ---: | ---: | ---: |
| A — market IOC (control) | 35 | 1,192.51 | **5.50 bp** |
| B — post-only chase | 8 | 265.25 | **4.80 bp** |

**Arm B's passive fill rate is 2 of 8 — 25%.** Fallbacks: 4 `chase`, 2
`timeout`. Repeg counts were `[0,0,0,9,9,0,0,0]`, i.e. two orders hit the repeg
ceiling and still missed.

Chase limits **work directionally** — 5.50 → 4.80 bp/side — but the arithmetic
says the fill rate, not the concept, is the binding constraint. Solving
`0.25·m + 0.75·5.50 = 4.80` gives an implied passive fill price of **2.70
bp/side**, close to Bybit's ~2 bp maker reference, which confirms the mechanism
is sound when it fills.

Projecting the blend of §12.1 (gross 29.99 bp/day) across fill rates:

| passive fill rate | bp/side | round trip | blend bp/day | ≈ Sharpe |
| ---: | ---: | ---: | ---: | ---: |
| 25% (observed) | 4.80 | 9.60 | 20.39 | 0.97 |
| 60% | 3.82 | 7.64 | 22.35 | 1.07 |
| **80%** | 3.26 | **6.52** | **23.47** | **1.12** |
| 100% (ceiling) | 2.70 | 5.40 | 24.59 | 1.17 |

Even a perfect passive book lands at **5.40 bp round trip, not 4.00** — the
research assumption was never reachable. But raising the fill rate from 25% to
80% recovers most of what §12 destroyed: the blend goes 14.43 → 23.47 bp/day and
Sharpe 0.69 → ~1.12.

**Two caveats that matter more than the numbers.** The sample is 8 arm-B fills
against a 100-per-arm target — this is a direction, not a result. And the demo
book pays 7.78 bp/side against paper arm A's uniform 5.50, because **native stop
triggers are market orders**. Chase limits can only ever improve *entries*; every
stop-driven exit is taker by construction. A book whose exits are native stops
has a hard floor that passive entry logic cannot reach.

## 14. Should the erased daily SHORT sleeve be restored?

The sleeve was purged on 2026-06-11 by operator order (`e03e9ab`), engine and
all. Its promoted profile was `drop_all_4 + age300 + ff6 +
btc_trend_gate=uptrend`. Reconstructed **approximately** on the cross-venue panel
— the original engine is deleted, so this is a proxy, not a replay — and judged
against the two things unavailable when it was promoted: measured cost, and tail
statistics.

| variant | days | bp/day @4bp | bp/day @15.56bp | t | Sharpe | worst day | loss conc. | maxDD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| short 4d drop < −10% | 1,675 | 1.29 | −10.27 | −0.65 | −0.30 | −78.5% | 11.6% | 99.7% |
| + age ≥ 300d | 1,300 | −3.34 | −14.90 | −0.81 | −0.43 | −84.5% | 14.4% | 99.8% |
| **+ BTC 30d uptrend (promoted shape)** | 651 | 41.09 | **29.53** | **1.30** | **0.98** | **−17.8%** | **7.7%** | 70.5% |
| + deeper drop < −20% | 327 | 66.93 | 55.37 | 1.17 | 1.24 | −43.5% | 11.6% | 79.8% |

Three things stand out.

**The BTC uptrend gate is the entire strategy.** Ungated, the book is dead
(+1.29 bp) and the age filter makes it *worse* (−3.34). Gated, it is +41.09. In
this reconstruction the age-300 filter is harmful, which contradicts the
historical "age gate is robust" finding — though the panel's first-appearance
date is a weaker proxy than true listing age, so that specific contradiction is
soft.

**Its tail is better than the symmetric books', not worse.** Worst day −17.8% and
loss concentration 7.7% beat the long/short momentum baseline of §11.2 (−35.4%,
10.8%). The gate keeps it out of the crashes, and it is only in the market 39% of
the time. That is the opposite of what I expected from a short book.

**But it does not clear significance at honest cost.** At 15.56 bp it is t 1.30
— not significant — and the 651-day sample is a conditional subsample of a
profile that was itself selected on this history.

**Recommendation: do not restore the sleeve; extract the gate.** The evidence
does not support rebuilding a deleted engine on a t 1.30 reconstruction. It does
support the narrower claim that **a BTC-regime gate converts a dead short book
into a live one**, which is a conditioner testable on the books that already
exist — including the CONTINUOUS short leg the §11 experiment is about. That is
the cheap version of this idea, and it does not require resurrecting anything.

---

## 15. Force-chase and the stop question (2026-07-25)

Two proposals, measured rather than argued.

### 15.1 Forcing every entry passive destroys the book

A resting post-only order fills **at your limit**, so switching to passive
changes two things and only two: which intended entries you actually get, and
the fee. (A first pass booked the passive entry at the *next* close, which hands
the strategy a price it could never obtain and produced a nonsense Sharpe of
5.60. Corrected below.)

| variant | bp/day | t | Sharpe |
| --- | ---: | ---: | ---: |
| IMMEDIATE — every entry, taker 11.00 bp | 28.42 | 1.96 | 0.86 |
| **FORCED — passive-only, maker 5.40 bp** | **−90.57** | **−7.20** | **−3.19** |
| **HYBRID — chase-then-cross, 8.12 bp** | **31.30** | **2.16** | **0.95** |
| TODAY — measured demo cost 15.56 bp | 23.86 | 1.65 | 0.73 |

**Forcing passive entry is the most damaging change tested in this document.**
It discards 49% of intended entries, and in a momentum book the entries that
come back to you are precisely the ones that were about to go wrong. You save
5.6 bp of fee and lose roughly 119 bp/day of alpha.

**Chase-then-cross is the right answer and it already exists.** The hybrid beats
both immediate execution (31.30 vs 28.42) and today's cost basis (vs 23.86). The
lever is the passive **fill rate**, not removing the fallback — which is exactly
what arm B of `passive_execution_experiment_2026-07-20.md` already implements at
a 25% fill rate. Raise the rate; keep the cross.

### 15.2 Entries are only half the bill

Fee decomposition of the archived demo journal:

| fill class | fills | notional | fee | bp/side | share of fee bill |
| --- | ---: | ---: | ---: | ---: | ---: |
| entry / increase | 55 | 2,222.78 | 1.7239 | 7.76 | **50%** |
| reduce-only exit | 30 | 2,183.84 | 1.7051 | 7.81 | **50%** |

Entry-side execution work therefore addresses at most half the cost. Every fill
in this sample matched an owner-issued `order_command`, so these exits went
through the owner's order path rather than arriving purely as venue triggers —
but the sample is 85 fills and the metadata does not label stop-triggered fills
distinctly, so **the split between native-stop triggers and owner reduce-only
orders is not resolved here**. That split decides whether exits are chaseable at
all, and it should be measured before any exit-side execution work.

### 15.3 On removing native stops

The system does have stops: the journal carries 135 `protection` events with
`stop_price` and `take_profit_price`, and they demonstrably fire — the 2026-07-22
DEXEUSDT close and the eight native-stop closes on 2026-07-19 are all in the
record.

What it does **not** have is a *strategy-level* stop-loss: the native stop is an
operational seatbelt, not an exit rule with an alpha thesis. That is a real gap
and worth reworking. But the two are different objects and should not be traded
against each other:

- A **venue-native stop** is the only protection that survives owner-process
  death, a VPS outage, or a network partition. The 2026-07-21 incident was an
  ~8-minute unprotected interval, and the entire account-kernel remediation
  exists to close it. Removing native stops reopens exactly that failure.
- A **software stop** protects nothing when the software is the thing that failed.

The design that gets both, and does not require giving up either:

1. Keep the venue-native stop, set **wide**, as a disaster backstop only.
2. Add a strategy-level exit at a **tighter** level, executed chase-then-cross
   like arm B.
3. The native stop then rarely triggers, so its taker cost mostly disappears
   while the seatbelt stays installed.

`AGENTS.md` is explicit that capital-preservation controls are not traded for
alpha metrics. This design does not trade them; it demotes the native stop from
primary exit to backstop, which is what it should have been.

---

## 16. Phase 0 — the instruments, repaired (2026-07-25)

Executing `docs/roadmap_2026-07-25.md` §2. Three of the four tasks close; one is
blocked on access. Two published claims are withdrawn, and **Gate 0.3 is
explained** — the CONTINUOUS backtest and the forward record were never
measuring the same strategy.

Run identity: `scripts/equity_curves.py --sleeves continuous --start 2023-03-13
--end 2026-07-17`, root `~/SHARED_DATA/bybit_full_pit`, config
`configs/volume_alpha.default.yaml`, profile `continuous_ensemble_v2` revision
`active_tp12_code_v1`. Reproduces §10 exactly: **Sharpe 2.73, max DD −1.29%,
worst day −0.93%, +23.09%** over 2023-03-13→2026-07-09. All three components
report `funding=partial`. Lane-1 on seen data.

### 16.1 Task 0.1 — WITHDRAWN: the repo was never priced at 4 bp

The roadmap's premise was that "every historical conclusion in this repository
was priced wrong" at 4 bp. That is true of the cross-venue anomaly work and the
Lane-2 config, and **false of the deployed-sleeve equity curves**. Measured from
`configs/volume_alpha.default.yaml` and the engines:

| surface | modelled round trip | vs measured 15.56 bp |
| --- | ---: | ---: |
| engine base (`maker_fill_probability: 0.0`, taker 5.5 + slip 2.0, both legs) | 15.00 bp | 1.04× |
| LONG (`cost_multiplier` 3.0×) | 45.00 bp | **0.35× — 2.9× conservative** |
| CONTINUOUS, nominal `2×(taker 5.5 + spread 2.5)` | 16.00 bp | 0.97× |
| **CONTINUOUS, actually charged in the ledger (incl. impact)** | **24.12 bp** | **0.65× — 1.55× conservative** |
| `lane2_premium_momentum_blend_v1.json` | 4.00 bp | 3.89× |
| cross-venue anomaly harness (§9, §11.2, §12.4, §14, §15.1) | 4.00 bp | 3.89× |

The 24.12 bp is measured, not inferred: `−Σ cost_return / Σ|notional_weight| ×
1e4` over the 2,344 ensemble trades.

**Two claims are withdrawn.**

1. **§12.2 is wrong.** It applied the 3.89× fee ratio to CONTINUOUS and reported
   net falling +21.13% → +14.80%. CONTINUOUS was never priced at 4 bp; it is
   charged 24.12 bp, already **1.55× more than realised**. Correcting the cost
   basis moves CONTINUOUS *up*, not down. The 3.89× ratio belongs only to the
   4 bp surfaces.
2. **§11.1's "implausibly cheap" is wrong.** Modelled trading cost is 10.3% of
   gross here (−2.52% against +24.35%) not because the *price* is low — 24 bp is
   expensive — but because **turnover is low**: 10.44 units of capital
   round-tripped over 3.3 years. Cheapness was read off a ratio when the
   denominator was the anomaly.

**Change made.** `liquidity_migration.cross_section` now owns a single
`MEASURED_ROUND_TRIP_BP = 15.56` with its provenance, plus
`PASSIVE_FLOOR_ROUND_TRIP_BP = 5.40`. `cross_section.summary` defaults `cost_bp`
to the measured basis instead of `0.0`, so omitting the argument yields an honest
number rather than a gross one; a gross read must now be asked for explicitly.
`lane2_blend` charges the measured basis by default, reports `cost_basis_bp` in
every score row, and keeps `maker_round_trip_bp` reachable only by explicit
override so the as-registered figures stay reproducible. The rule itself is
untouched — this is a re-pricing, not a new registration.

### 16.2 Task 0.2 — RESOLVED: `funding=partial` is a label artifact

§10 called this "the single most likely source of flattery" and "the first thing
to check". It is not the source. `funding_mode` is set per trade by
`trade_lifecycle._perp_funding_return`, which flags `partial` when a trade's
window extends beyond the symbol's funding-history span — while **still charging
every settlement it does cover**. It is a coverage flag, not a modelling gap, and
`_funding_mode_summary` collapses the whole book to `partial` if a single trade
is.

| component | trades | notional-weighted modelled fraction | partial | missing |
| --- | ---: | ---: | ---: | ---: |
| turn3p3 | 843 | **99.82%** | 2 | 0 |
| turn4p3 | 795 | **99.82%** | 2 | 0 |
| turn4p5 | 706 | **99.79%** | 2 | 0 |

Two trades per component, carrying +0.000106 of funding return against a
component total of about −0.036. Funding is materially fully modelled and cannot
explain Sharpe 2.73. Ensemble funding is −3.60% of capital over the window, i.e.
a real drag that is being paid, not skipped.

The honest fix is to the *label*, not the model: a coarse `partial` that fires on
2 of 843 trades tells a reader the opposite of the truth. The notional-weighted
fraction already exists (`_funding_modeled_fraction`) and should be what gets
reported.

### 16.3 Task 0.3 — GATE EXPLAINED: the backtest models no stop; the deployment always has one

The gap is not cost, not funding, and not signal decay. It is that **the
reconstruction and the deployed daemon do not share an exit rule.**

`ContinuousEventConfig` has `take_profit_pct = 0.12` and **no stop-loss field**.
The ledger confirms it: `stop_price` is empty on every one of the 2,344 trades,
and the exit mix is only `max_hold` 72.2%, `take_profit` 27.7%, `data_end` 0.1%.
The return distribution is hard-capped on the upside at exactly +12.00% (p95, p99
and p99.9 are all +12.00%) and open on the downside — worst single trade
**−92.60%**, 60 trades below −25%, 15 below −50%.

The deployed demo is different. CONTINUOUS declares **no** `stop_loss_pct` to the
account (verified: `continuous_demo.py` sets only `take_profit_pct`), so per
`account_kernel`'s provisional-stop contract — *"the account fallback is used only
when no same-direction component declares a stop"* — the account-level
`--disaster-stop-fraction` seatbelt becomes CONTINUOUS's **de facto exit rule**.
`STATE.md`'s DEXEUSDT record fixes its size: short entry 12.659, intended stop
12.913 = **2.006%**.

Applying that stop to the *same* modelled trades, using each trade's recorded
`mae` (max adverse excursion) and filling at exactly the stop:

| stop | total return | Sharpe | max DD | worst day | t |
| --- | ---: | ---: | ---: | ---: | ---: |
| **none — what the backtest reports** | **+18.24%** | **2.50** | 1.50% | −0.99% | **4.56** |
| 12.0% | +4.42% | 0.70 | 3.36% | −0.58% | 1.27 |
| 8.0% | +1.58% | 0.29 | 3.27% | −0.45% | 0.52 |
| 5.0% | −0.47% | −0.10 | 3.68% | −0.34% | −0.19 |
| 3.0% | −1.90% | −0.49 | 4.17% | −0.27% | −0.90 |
| **2.0% — the deployed seatbelt** | **−2.54%** | **−0.75** | 2.95% | −0.24% | **−1.36** |

- **77.5% of all 2,344 trades** breach a 2% stop.
- **64.3% of the model's 649 take-profit winners first dipped 2% against the
  position.** The live stop converts a +12% winner into a −2% loser in 417 cases.
- The book's sign flips between a 5% and an 8% stop. Even a stop set equal to the
  take-profit distance (12%) leaves only Sharpe 0.70.

**The deployed CONTINUOUS sleeve is a losing book under its own risk controls, and
its backtest cannot see that** because the backtest has no stop. Sharpe 2.73 is
not a data error, a funding error, or a cost error — it is the Sharpe of a
strategy that is not the one running. This also explains the forward record
directly: the eight native-stop closes on 2026-07-19 (−9.49 USDT combined) are the
predicted behaviour, not an anomaly.

**LONG does not have this defect**, which is the control that makes the finding
credible rather than a harness bug. LONG declares `stop_loss_pct` to the account
(`long_native_event_demo.py:911`) *and* models the identical stop in its own
backtest (`long_native.py:1042-1047`, exit reason `stop_loss`). LONG's comparator
is fair; CONTINUOUS's is not.

**Gate 0.3 verdict.** The gap is explained, so the roadmap's stated fallback —
"every historical reconstruction in this repository is suspect and the program
restarts on forward data only" — is **not** triggered. The defect is specific,
located, and asymmetric: it is a missing exit rule in one sleeve's comparator, not
a systemic reconstruction failure.

#### Scale, measured while we were in here

Separately worth recording, because §10 framed it as "risk utilisation": the
reconstruction's nominal target is `gross_exposure 0.5` (`max_active 25` ×
`notional_weight 0.02`). Realised is far below it.

| | |
| --- | ---: |
| nominal gross target | 0.500 |
| mean realised gross exposure | **0.0075** |
| mean on days in market | 0.0196 (≈1 position) |
| max ever realised | 0.0800 |
| days in market | 325/849 = 38% |
| **realised / nominal** | **1.5%** |

The book holds roughly **one position at a time on 38% of days**, not 25. So max
DD −1.29% and worst day −0.93% are small because the *book* is small, not because
the strategy is safe — the cap is never near binding, and the constraint is
candidate supply. Any restatement of these numbers at a larger size must scale the
drawdown by the same factor it scales the return.

### 16.4 Task 0.4 — BLOCKED on access, but structurally constrained

§15.2 left open whether the 30 reduce-only exit fills were native-stop triggers or
owner-issued reduce-only orders — the split that decides whether exit-side cost is
addressable. **This box has no VPS access**: `ssh root@116.202.15.128` returns
`Permission denied (publickey)`, so the archived journal cannot be re-read and the
fill-level classification is not resolved here. Reported as blocked rather than
estimated.

Two things do constrain it from this side:

- §16.3 predicts native-stop triggers should **dominate** CONTINUOUS exits, since
  77.5% of trades breach the 2% seatbelt. Native-stop triggers are market orders,
  which is consistent with §12's measurement that fills price on Bybit's taker
  tiers.
- `STATE.md` independently records the shape: eight native-stop closes on
  2026-07-19, ONDOUSDT closed by native protection on 2026-07-18, DEXEUSDT closed
  by take profit on 2026-07-21.

To finish it, from a box with access: `scripts/ops.sh venue-accounting` for
read-only demo accounting evidence, then classify each `fill` event by whether a
`protection` event with a matching venue order id precedes it in the journal.

### 16.5 What Phase 0 changes

1. **Cost is not the problem it was reported to be.** Two of the three deployed
   surfaces are priced *conservatively* against realised fees. The 4 bp error is
   real but confined to the cross-venue anomaly reads and the Lane-2 config.
2. **`funding=partial` is closed** and was never load-bearing.
3. **CONTINUOUS's Sharpe 2.73 is withdrawn as evidence about the deployed
   sleeve.** It describes a no-stop variant. The deployed variant, measured on the
   same trades, is Sharpe −0.75.
4. **The highest-value open item is now a design question, not a search
   question**: CONTINUOUS needs a real strategy-level exit whose backtest and
   deployment agree. §15.3's proposal — wide native backstop plus a tighter
   strategy exit — is the right shape, and §16.3 gives it a target: the exit must
   be at least 5-8% away before the book has any positive expectancy at all, and
   the 2% seatbelt must stop being the exit rule.
5. Phase 1's re-screen should not include CONTINUOUS's reconstruction as a
   surviving mechanism until 4 is done. It is not a candidate; it is a
   miscomparison.

**Caveats.** `mae` is derived from hourly bar highs/lows while the native stop
triggers on **MarkPrice**, so the breach counts are close but not exact; the
conclusion is insensitive across the whole 2-8% range, so this does not change it.
Filling at exactly the stop is **optimistic** — a MarkPrice stop becomes a market
order, and in a short squeeze it slips further, so −2.54% is a ceiling not a floor.
Early stop-outs free capital the counterfactual does not redeploy, and shorter
holds would pay slightly less funding; both are second-order against a sign flip.
The 2.006% stop distance is inferred from one `STATE.md` record because
`deploy/sleeves.env` is unreadable from here. All of §16 is Lane-1 on seen data and
grades nothing.

---

## 17. Phase 1 — the honest re-screen, and 2A/2B (2026-07-25)

Executing `docs/roadmap_2026-07-25.md` §3–§4. One pass over the mechanisms that
had survived something, at the honest cost basis, threshold **t ≥ 3.25**.

**Gate 1 result: 0 of 12 cells clear t ≥ 3.25.** That is the roadmap's expected
outcome, so no further sweeps were run.

Substrate: `scripts/build_cross_venue_panel.py --start 2021-01-01 --end
2026-07-18`, **11,430,624 rows / 636 both-venue symbols** across six yearly
shards, panel commit `ec29aa9`, zero exclusions, `execution_delay_ms=0` on top of
the mandatory 1h bar-completion lag. Harness: `scripts/screen_phase1.py`
(19 tests). Top-100 by trailing-24h turnover on the venue being traded, 24h
disjoint holds, settlement-exact funding. Lane-1 on seen data.

### 17.1 A second cost error, independent of the 4 bp one

§16.1 corrected the fee *level*. This corrects the *quantity*.

`cross_section.long_short` returns a book that is **1 unit long + 1 unit short on
one unit of capital** — 2x gross, as the registered config states outright. Every
read in §9–§15 charged **one** round trip to that book. A full rebalance into a
disjoint name set round-trips **four units** of notional per period (close both
legs, open both legs), which at 7.78 bp/side is 31.12 bp — not 15.56.

But deciles overlap, and a name held through pays nothing, so the right answer is
measured rather than assumed. Measured per mechanism, and cross-checked against
decile persistence:

| mechanism | names retained period-to-period | predicted turnover | measured | charged |
| --- | ---: | ---: | ---: | ---: |
| momentum_1w | 63.4% | 1.46 | **1.52** | 11.9 bp |
| funding carry | 51.9% | 1.92 | **2.16** | 16.8 bp |
| premium_diff | 26.6% | 2.94 | **3.23** | 25.2 bp |

(predicted = 4 × (1 − retained); the small gap is leg-size drift.)

**The flat charge was not uniformly wrong — it was wrong in opposite directions.**
It *overcharged* the slow-rotating momentum book (11.9 actual vs 15.56 charged)
and *undercharged* the fast-rotating premium book (25.2 vs 15.56) by a third. A
uniform cost model systematically flatters signals that churn and penalises
signals that persist, which is a reranking, not a rescaling.

### 17.2 Phase 1 — every cell

Ungated, honest cost basis. `repo_1x` is the historical convention shown for
audit; `honest` is the number that means something.

| venue | mechanism | n | repo_1x | honest | charged | survives |
| --- | --- | ---: | ---: | ---: | ---: | :--: |
| bybit | premium_diff | 1849 | +2.29 bp t 0.22 | **−7.31 bp t −0.69** | 25.2 | no |
| bybit | momentum_1w continuation | 1875 | +26.78 t 1.84 | **+30.48 t +2.10** | 11.9 | no |
| bybit | blend 50/50 (registered) | 1849 | +16.80 t 1.89 | **+16.00 t +1.80** | 16.4 | no |
| bybit | funding carry (dead control) | 1875 | +35.29 t 3.16 | **+34.09 t +3.05** | 16.8 | no |
| bybit | basket short (§11.2 B) | 1982 | −0.27 t −0.04 | −5.07 t −0.70 | 20.4 | no |
| bybit | short 4d drop < −10% | 1621 | −9.73 t −0.54 | −9.73 t −0.54 | 15.6 | no |
| binance | premium_diff | 1850 | −8.16 t −0.80 | −17.87 t −1.76 | 25.3 | no |
| binance | momentum_1w continuation | 1876 | +9.91 t 0.72 | +13.64 t +0.99 | 11.8 | no |
| binance | blend 50/50 (registered) | 1850 | +3.11 t 0.37 | +2.33 t +0.28 | 16.3 | no |
| binance | funding carry (dead control) | 1876 | +9.24 t 0.84 | +7.42 t +0.67 | 17.4 | no |
| binance | basket short (§11.2 B) | 1982 | −10.99 t −1.52 | −15.85 t −2.19 | 20.4 | no |
| binance | short 4d drop < −10% | 1599 | −10.95 t −0.63 | −10.95 t −0.63 | 15.6 | no |

**Two published conclusions invert once turnover is charged correctly.**

1. **premium_diff — the program's headline signal — is negative.** §9.4 reported
   t 2.06 at 24h and the config's known-weaknesses list called it "marginal".
   Charged its actual 3.23-unit turnover it earns **−7.31 bp/day, t −0.69**. It is
   the fastest-rotating book in the set (26.6% name retention), so the flat charge
   was hiding a third of its cost. Its era splits show the effect was never there
   after 2021: −57.9, +6.9, +0.0, −14.3, +7.4, −2.8 bp/day.
2. **funding carry — catalogued as the known-dead control — is the strongest cell
   in the screen**: +34.09 bp/day, **t 3.05**, Sharpe 1.34, and positive in **all
   six eras** (+21.7, +14.7, +24.2, +22.5, +72.9, +53.0). It still does not clear
   3.25, and it fails 2A below. But a control outscoring every real signal is a
   finding about the screen, not a footnote.

   Scope note, so this is not overclaimed: the earlier "carry is dead" verdict was
   reached on a **hedged extreme-funding** construction
   (`docs/strategy_program.md`), which is a different book from this unhedged
   top-100 decile long/short. This does not withdraw that result; it measures a
   simpler construction the program had not priced this way. And the shape is the
   textbook carry shape — worst 1% −18.6%, max drawdown 77.9%, tail concentration
   12.6% — collect small, lose large. Sharpe 1.34 with that tail is not a free lunch.

3. Momentum continuation improves rather than degrades: t 1.65 in §12.4 becomes
   **t 2.10**, because the honest charge for it is *lower* than the flat one. Its
   era pattern strengthens monotonically after 2022 (+49.4, +11.4, +54.1, +75.7).

### 17.3 2A — cross-venue replication: 5 of 6 killed

Kill rule as registered: opposite sign on either venue, or an effect ratio outside
[0.5, 2.0]. Universe overlap between arms is **79.9% Jaccard** (152,715 shared
name-periods of 172,102 / 171,659), so divergence is not a universe artifact —
each arm ranks turnover on the venue it trades, and that is now a number.

| mechanism | bybit | binance | ratio | same sign | verdict |
| --- | ---: | ---: | ---: | :--: | --- |
| premium_diff | −7.31 bp | −17.87 bp | 2.44 | yes | **KILLED** |
| momentum_1w continuation | +30.48 | +13.64 | 0.45 | yes | **KILLED** |
| blend 50/50 (registered) | +16.00 | +2.33 | 0.15 | yes | **KILLED** |
| funding carry | +34.09 | +7.42 | 0.22 | yes | **KILLED** |
| basket short (§11.2 B) | −5.07 | −15.85 | 3.12 | yes | **KILLED** |
| short 4d drop < −10% | −9.73 | −10.95 | 1.13 | yes | replicates |

**Every mechanism with a positive Bybit effect fails replication.** Bybit's effect
is 2.2× to 6.9× Binance's on momentum, blend, and carry. The only mechanism that
replicates is the one that is *dead on both venues* — it replicates being worthless.

This is consistent with §9.4's "the edge is Bybit-local", but the framing must
change. §9.4 read venue-locality as a convenience: *"the true cross-venue
execution capability this repo does not have is not worth building."* Under the
roadmap's escape #2 — multiply the sample by replication instead of waiting — the
same fact is a **failure**. A signal that exists on one venue and not its largest
competitor has not been corroborated; it has been contradicted by the closest
available independent sample. Escape #2 is closed for every mechanism here.

### 17.4 2B — regime conditioning: no evidence it generalises

The BTC 30-day uptrend gate, applied to each book on each venue. Kill rule: fewer
than half the books improved is a kill.

Gate improved Sharpe on **6 of 12 books (50%)** — exactly at the boundary, so not
killed and not supported. But the pattern is not random, and that is more
informative than the count:

| helped by the gate | hurt by the gate |
| --- | --- |
| momentum_1w (both venues) | premium_diff (both venues) |
| blend 50/50 (both venues) | funding carry (both venues) |
| short 4d drop (both venues) | basket short (both venues) |

The split is perfectly consistent across venues — every mechanism is helped on
both or hurt on both. So the gate is doing something real and directional, but it
is **not a general conditioner**: it helps momentum-shaped books and harms
carry/premium-shaped ones. Applying it as a book-level overlay would be a coin
flip on which sleeve it landed on.

**§14's headline lead survives in direction and dies in significance.** §14
reported the gate taking the reconstructed short book from +1.29 to +41.09 bp/day
at 4 bp. At the honest basis it goes **−9.73 → +24.88 bp/day on Bybit** and
**−10.95 → +20.4 on Binance** — same sign, same mechanism, replicated across
venues, and still only **t 1.08 / t 0.90**. The single most promising lead in the
program is real enough to survive a venue change and far too weak to act on.

The sample cost is the reason, exactly as roadmap §8 predicts: the gate keeps
780 of 1,621 periods (48%), so it needs a ×1.44 effect merely to hold t constant.
It delivers roughly that and no more.

### 17.5 Gate 1 verdict and what it changes

- **0 of 12 cells clear t ≥ 3.25.** Per roadmap §3, no further sweeps.
- **2A closes the replication escape.** Five of six mechanisms killed; the
  survivor is dead on both venues. This was the cheapest of the three escapes and
  it produced a clean negative.
- **2B is not a kill but is not support either.** The gate is venue-consistent and
  mechanism-specific, which makes it a candidate *component* of a momentum book,
  not a portfolio overlay.
- **The two cost corrections have now reranked the program twice.** §16.1 fixed
  the level; §17.1 fixed the quantity. Between them, the headline signal
  (premium_diff) is negative and the designated dead control (carry) is the
  strongest cell. Any conclusion in §9–§15 that compared two mechanisms at a
  single flat cost is suspect on those grounds alone, and the ones that ranked
  fast-rotating against slow-rotating books are the ones to re-read first.
- **The most interesting unpriced object is now funding carry on Bybit**: t 3.05,
  positive in six of six eras, and killed only by cross-venue replication. It
  should be treated as a Lane-1 lead with a known tail problem and a known
  venue-locality problem, not as a candidate.

**Caveats.** `execution_delay_ms=0` means entries are booked at the close whose
completion produced the signal — standard here and consistent with §9–§15, but
mildly optimistic; a delay sweep was not run because it would be a new sweep.
Costs are charged as measured turnover × 7.78 bp/side with no impact or
partial-fill term, so they remain a floor. The 15.56 bp basis is a demo-account
measurement at small notional and is not a capacity statement. Turnover is
measured on the traded venue's own universe, so the two 2A arms differ on ~20% of
name-periods. All of §17 is Lane-1 on data these mechanisms have already seen and
grades nothing (`docs/governance.md` §1).

---

## 18. Phase 5A — tuned for *t*. One mechanism clears, and it is uninvestable (2026-07-25)

`docs/roadmap_2026-07-25.md` §8. One-dimensional strictness sweeps on the
already-selected books, selecting on **t** rather than mean, at each cell's own
measured turnover. Harness `scripts/tune_phase5.py`. 8 cells × 3 parameters ×
3 signals × 2 venues = **144 cells swept, all reported**. Lane-1 on seen data.

### 18.1 The roadmap's arithmetic is confirmed empirically

Roadmap §8 predicted that loosening a filter can *raise* t by buying sample. It
does, and the BTC gate on the momentum book is the clean demonstration:

| gate | n | mean bp | t | Sharpe |
| --- | ---: | ---: | ---: | ---: |
| off | 1875 | +30.48 | +2.10 | +0.93 |
| > −0.20 | 1724 | +36.23 | +2.44 | +1.12 |
| > −0.10 | 1441 | +39.35 | +2.52 | +1.27 |
| **> −0.05** | 1247 | +46.99 | **+2.78** | +1.50 |
| > 0.00 *(the registered "uptrend")* | 952 | +46.78 | +2.50 | +1.55 |
| > +0.05 | 690 | +53.79 | +2.52 | +1.83 |
| > +0.10 | 493 | +40.14 | +1.61 | +1.38 |
| > +0.20 | 240 | +7.46 | +0.20 | +0.25 |

Shape: **PLATEAU** (5/8 cells within 20% of peak, sd 0.78) — a real parameter,
not a fitted one. The registered `> 0.00` threshold is *not* the t-maximising
setting: relaxing it to `> −0.05` keeps 66% of periods instead of 51% and lifts t
from 2.50 to 2.78, while Sharpe *falls* from 1.55 to 1.50. That is precisely the
trade roadmap §8 describes — and it means every gate in this program that was set
to maximise Sharpe was set against the evidence it was trying to produce.

Still short of 3.25.

### 18.2 premium_diff cannot be tuned into existence

All three sweeps return **"no positive cell"**. Not one of 24 settings is
positive, and it gets monotonically *worse* as the cut loosens (cut 0.45 →
t −3.02). This is not a strictness problem; the signal is absent. The §16.1/§17.2
withdrawal stands and is now robust to the whole parameter space.

### 18.3 The §14 lead cannot be tuned either

The BTC-gated conditional short — "the most promising single lead in the program"
— across the entire drop-threshold curve, gate on:

| drop | −30% | −20% | −15% | −10% | −7% | −5% | −3% |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| t | +0.37 | +0.64 | **+1.30** | +1.08 | +0.88 | +1.23 | +0.87 |

Peak t 1.30, exactly where §14 left it, and flat across seven thresholds (sd
0.30). There is no setting at which this book becomes evidence. Closed.

### 18.4 Funding carry clears 3.25 on a broad plateau — on Bybit

The one mechanism that survives everything §16–§17 threw at the program.

Universe-size curve, Bybit, cut 0.10, charged at measured turnover:

| top_n | n | mean bp | t | Sharpe | worst 1% | max DD | negative eras |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 30 | 1875 | +84.33 | +3.60 | +1.59 | −35.17% | **275.7%** | 0/6 |
| 50 | 1875 | +57.74 | +3.27 | +1.44 | −27.38% | **166.0%** | 0/6 |
| 75 | 1875 | +37.80 | +2.91 | +1.28 | −21.44% | 98.7% | 0/6 |
| 100 | 1875 | +34.09 | +3.05 | +1.34 | −18.61% | 77.9% | 0/6 |
| 300 | 1875 | +24.59 | **+3.96** | +1.75 | −10.03% | 58.8% | 0/6 |

Shape: **PLATEAU** (6/8 cells within 20% of peak, sd 0.36) — the tightest curve
in this document. **Positive in all six eras at every universe size**: 30 of 30
era/size cells positive. Bonferroni over the full 144-cell grid needs t ≈ 3.58;
top-300's 3.96 clears it and top-30's 3.60 is at the line.

**This is the only mechanism in the program that clears an honest threshold after
an honest cost basis, a corrected turnover charge, and a multiple-testing
correction over its own grid. And it is not investable.**

#### Why not: the drawdown exceeds capital

Max drawdown runs **58.8% to 275.7%**, and the worst single 1% of days is
**−10% to −35%**. A 275.7% drawdown on a 2x-gross book is not a drawdown, it is
several liquidations. Sharpe 1.59 is a meaningless statistic next to it. The tail
does not come out with any knob swept here: even the most diversified cell
(top-300, the *best* t) still carries 58.8% max drawdown and a −10% worst-1% day.

#### Why not, second reason: the two criteria disagree

| top_n | bybit | binance | ratio | 2A verdict |
| ---: | ---: | ---: | ---: | --- |
| 30 | +84.33 | +58.51 | 0.69 | replicates |
| 50 | +57.74 | +40.28 | 0.70 | replicates |
| 75 | +37.80 | +19.71 | 0.52 | replicates |
| 100 | +34.09 | +7.42 | 0.22 | **KILLED** |
| 300 | +24.59 | +8.41 | 0.34 | **KILLED** |

The cell that maximises t (top-300) **fails** replication. The cells that
replicate (top-30/50/75) are the ones with 99–276% drawdowns. A single clean
phenomenon would not split its criteria like this. Binance also carries a
negative 2024 at *every* universe size (−25, −12, −20, −15, −8 bp/day) that Bybit
does not — one venue's worst year is the other's flat year, on largely the same
names.

#### The replication caveat that weakens escape #2 generally

Universe overlap between the two arms is **79.9% Jaccard**. The two venues are
therefore *not* two partly-independent samples of the same phenomenon — they are
largely **the same names**, priced with different funding rates and fees. So a
cross-venue agreement is closer to a robustness check on funding data than to the
√2 effective-t gain roadmap §4 assumed for 2A.

This does not rescue the mechanisms 2A killed — failing a weak test is still
failing. But it means **2A was never able to deliver the sample multiplication it
was designed for**, and escape #2 should be marked closed for a structural
reason, not just an empirical one. Genuine sample multiplication needs a venue
with a different name population, or a different asset class, not Bybit's
neighbour trading the same 500 perps.

### 18.5 Verdict on carry: a real risk premium, fairly priced

Carry passes the roadmap's own "who is on the other side" test better than
anything else here, and the answer explains the result completely. Leveraged longs
pay funding; a short-the-high-funding book collects it. The compensation is real,
persistent, and visible in every era on both venues — because it is **payment for
taking liquidation risk**, not a mispricing. And the payment appears to be roughly
fair: you collect tens of bp per day and periodically give back 10–35% in one day.

That is a risk premium, not an edge. Harvesting it needs capital structure and a
liquidation-survival mechanism this program does not have, and the roadmap's
threshold was never the binding constraint on it — the drawdown was.

**Not promoted, not registered, not a candidate.** Recorded as the program's only
mechanism with a genuine effect and a named economic counterparty, and as the
strongest argument yet for Phase 3: the missing input that would make carry
tradeable is a **liquidation feed**, which is exactly the dataset §5 ranks first.

### 18.6 What 5A changes

1. Gates in this program were tuned on Sharpe and are therefore **mis-set for
   evidence**. The momentum BTC gate should be `> −0.05`, not `> 0.00`, on t
   grounds — a 1-line change worth 0.28 of t.
2. premium_diff and the §14 conditional short are closed across their whole
   parameter spaces, not just at their registered settings.
3. Funding carry is the one live object, and its problem is tail/capital, not
   significance. It is the first mechanism here whose next step is **risk
   engineering rather than more measurement**.
4. 2A's design limit is now known (79.9% name overlap). Any future replication
   claim on this panel must report the overlap, or it is overstating its evidence.

**Caveats.** 144 cells were swept and all are reported; the plateau/spike
classification is a heuristic, not a test. Costs remain a floor — measured
turnover × 7.78 bp/side, no impact or partial-fill term — and carry at top-30
concentrates into the most liquid names where that assumption is most defensible
and capacity is smallest. Drawdowns above 100% are arithmetic on an unlevered
2x-gross book and indicate the construction is inadmissible, not that a real
account lost 275%. All Lane-1 on seen data; grades nothing.

---

## 19. Phase 5C — one external hypothesis, tested to destruction (2026-07-25)

`docs/roadmap_2026-07-25.md` §8, 5C. Few hypotheses, chosen for mechanism
plausibility, tested deeply. One was imported and it turned out to explain a
result this program had already produced — and then to fail for the reason its own
author gave.

### 19.1 The source predicted §18.4 in advance

Robot Wealth, *"The Art and Science of Trading Carry"*
(<https://robotwealth.com/the-art-and-science-of-trading-carry/>), verbatim:

> "An obvious one is shorting perpetual futures trading at a premium and longing
> the spot to hedge the risk, thus collecting the funding."

> "A messier variation is to create a long-short basket of perpetuals trading at a
> discount or premium, respectively. This trade will see **a much higher return
> variance than the spot-perpetual version because the basket components will
> dislocate and do all sorts of weird idiosyncratic things**."

§18.4's funding-carry book **is** that messier variation, and its measured failure
mode is exactly the predicted one: 166–276% max drawdown produced by idiosyncratic
single-name dislocation. An outside practitioner, writing independently, diagnosed
in one sentence what this program spent ~44 mechanisms arriving at. That alone
justifies roadmap §5C as a standing practice.

**Mechanism and counterparty.** Funding is paid by leveraged directional longs to
stay long. A short-perp/long-spot pair holds no price view; it is paid for
supplying the leverage those longs demand. The premium is sticky because it is
autocorrelated, so funding accrues faster than the basis mean-reverts. This is the
only mechanism in the program with a named counterparty and a reason to persist.

### 19.2 H1 as tested, and one bug worth recording

The repository holds **no spot dataset**. But the panel carries the venue's own
**index price** — a basket of major spot exchanges — so the spot leg was proxied by
`by_index_close`. Per name over the hold:

    pair return = funding_received − (perp_return − index_return)

i.e. keep the funding unless the basis moves against you by more. Costs charged
asymmetrically and honestly: perp at the measured **7.78 bp/side**, spot at
**10.0 bp/side** (Bybit/Binance spot taker ≈0.10%, *worse* than perp), so a pair
round-trips **35.56 bp**.

**A bug caught before publication, recorded because it is the house failure mode.**
The first pass reported +696 bp/day, Sharpe 4.09, and a 3,049% drawdown — mutually
contradictory numbers. Cause: `shift(-24)` was applied to the *disjoint-sampled*
frame, where 24 rows is 24 **days**, so a 24-day spot move was differenced against
a 24-hour perp move. The spot leg must be built on the hourly frame before
sampling. Pinned in `attach_pair_return`'s docstring and by a contiguity guard.

### 19.3 The hedge works — decisively, on the tail

Same signal, same universe, same cost discipline; only the short leg's hedge
changes. This is the cleanest structural result in the document.

| construction | mean | t | Sharpe | worst 1% | max DD |
| --- | ---: | ---: | ---: | ---: | ---: |
| perp-only basket (§18.4 control) | +34.09 bp/day | +3.05 | +1.34 | **−18.61%** | **77.9%** |
| delta-neutral pair, gross, 24h | +17.76 bp/day | — | +9.99 | **−0.78%** | **4.5%** |

**Worst-1% improves 24×; max drawdown improves 17×.** Robot Wealth's prediction is
confirmed quantitatively. The idiosyncratic dislocation that makes the perp-only
carry uninvestable is entirely removable by hedging each name with its own spot.

At a 24h rebalance the pair is nonetheless a *losing* book — 35.56 bp of cost
against 17.76 bp of gross — but that is a construction artifact, not the mechanism:
a carry pair is held while funding is positive, not churned daily.

| hold | n | gross/period | net/period | net bp/day | t | Sharpe | max DD |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 24h | 2007 | +17.76 | −17.80 | −17.80 | −23.49 | −10.02 | 419.8% |
| 72h | 668 | +42.28 | +6.72 | +2.24 | +1.98 | +0.85 | 73.6% |
| **168h** | 285 | +83.39 | **+47.83** | +6.83 | **+4.33** | +1.85 | 26.5% |
| 336h | 142 | +153.54 | +117.98 | +8.43 | +4.13 | +1.77 | 16.2% |

At a 7-day hold this beats the perp-only carry on **every** dimension — higher t
(4.33 vs 3.05), higher Sharpe (1.85 vs 1.34), 3× smaller drawdown, 3× smaller
tail — and clears t ≥ 3.25 even after admitting itself as mechanism 45.

### 19.4 And then the era split kills it

Governance §4: a pooled number that hides decay is a wrong answer.

| hold / universe | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 | neg eras |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 168h top-100 | **+245** | −18 | +10 | +58 | −27 | −13 | 3/6 |
| 168h top-300 | **+245** | −13 | +19 | +51 | −9 | +1 | 2/6 |
| 336h top-100 | **+498** | −1 | +53 | +135 | +14 | −119 | 2/6 |
| 336h top-300 | **+498** | +6 | +66 | +121 | +14 | −0 | 1/6 |

**The entire result is 2021.** Strip that era and the book is flat-to-negative in
three of the remaining five. The pooled t 4.33 is single-era contamination, not
evidence. 2021 is also the panel's thinnest, least efficient era — 84 both-venue
symbols against 552 in 2025.

The source said this too, and it was the one caveat not to skip:

> "This was a fantastic trade for a while, but it's gotten harder as more people
> chase it."

**H1 is killed.** Not on significance — it cleared — but on era stability, which
is the check that significance cannot substitute for.

### 19.5 The real conclusion, and it is an economic one

Put §18.4 and §19 side by side:

| | perp-only carry | delta-neutral carry |
| --- | --- | --- |
| effect persistence | **positive in 6 of 6 eras** | **2021 only** |
| tail | uninvestable (−18.6% worst 1%, 78% DD) | benign (−0.78%, 4.5% DD) |
| who wants this risk | nobody | everybody |

The two results are the same fact from opposite sides. **In this market you are
paid for holding the risk nobody wants, and not paid for the hedged version anybody
can run.** The delta-neutral pair is easy, popular, and arbitraged out by 2022. The
perp-only basket still pays because it carries idiosyncratic liquidation risk that
is genuinely unpleasant to hold — the compensation is real, persistent, and roughly
fair for what it is.

That reframes the whole program's search. There is no free lunch left in the
constructions this repository can express, and the one durable premium is a payment
for tail risk that the current capital structure cannot survive.

### 19.6 What this changes about Phase 3

Roadmap §5 ranks the missing datasets: (1) liquidation feed, (2) multi-venue
funding, (3) sub-hourly bars. Two amendments, both evidence-driven:

- **Spot klines are not on that list and should be**, because they are the input
  that makes the §19 test executable rather than proxied — and they are free from
  the same public archives the repo already uses for perps. That said, §19.4 has
  already used the proxy to establish the answer is *no*, so this is now a
  low-priority completeness item rather than a lead. **Proxy first, procure second**
  turned out to be the right order and saved the purchase.
- **The liquidation feed's priority is confirmed and its purpose has changed.** It
  is no longer "a squeeze-hazard model" in the abstract. §19.5 identifies the one
  durable premium as payment for liquidation risk, so a liquidation feed is the
  input that would let that risk be *sized and survived* rather than merely
  observed. The cascade literature supports this: minute-level work on the
  2025-10-10/11 event found futures led the crash with volume 22× baseline seven
  minutes before the trough, and that the liquidation-trigger mark price undershot
  both spot and futures, creating a reflexive loop
  (<https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6579278>). It also records a
  hazard no backtest here can see: venues activated **auto-deleveraging**, which
  can force-close a winning short.

Corroborating external negative, worth keeping as a prior: a 26-exchange,
35.7M-observation study found 17% of observations show ≥20 bp arbitrage spreads
but **only 40% of the best opportunities are profitable after costs**
(<https://www.mdpi.com/2227-7390/14/2/346>). That independently matches §17.3 and
this program's cost discipline.

**Caveats.** The spot leg is a synthetic index that cannot be bought; real spot
adds tracking error, per-exchange fees, and possible borrow, so every H1 number is
a mechanism read, not a tradeable one. **2A was not run for H1** — the panel has no
Binance index column, so no cross-venue replication exists for it; the kill rests
on era stability alone. n is 285 at 168h and 142 at 336h. H1 counts against the
multiple-testing budget as mechanism 45. Auto-deleveraging and borrow are unmodelled
everywhere. Lane-1 on seen data; grades nothing.

**Sources:** [Robot Wealth — The Art and Science of Trading
Carry](https://robotwealth.com/the-art-and-science-of-trading-carry/) ·
[Anatomy of a Crypto Cascade: Minute-Level Evidence from the October 2025
Crash](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6579278) ·
[Two-Regime Liquidity Recovery After a Perpetual Futures Liquidation
Cascade](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6636998) ·
[The Two-Tiered Structure of Cryptocurrency Funding Rate
Markets](https://www.mdpi.com/2227-7390/14/2/346) ·
[Robot Wealth — Ideas for Crypto Stat Arb
Features](https://robotwealth.com/ideas-for-crypto-stat-arb-features/)


---

## 20. Post-roadmap execution: the two kept items, built (2026-07-25)

The roadmap closed with no validated edge and two owner-directed follow-ups:
fix the CONTINUOUS exit rule (§16.5 item 4) and measure passive execution.
Both were built this session. Everything here is engineering plus Lane-1
measurement on seen data; nothing grades anything.

### 20.1 The CONTINUOUS exit rule: declared 35% backstop, modeled on both sides

§16.3 established the defect: the backtest models no stop, the deployment
always has one (the account's 2.006% disaster fallback), and the deployed
variant of the same trades is Sharpe −0.75. The fix has two halves that must
agree — a declared stop the account places at the venue, and the identical
stop modeled in the reconstruction.

**Choosing the level.** The §16.3 counterfactual was extended upward
(`scripts/continuous_stop_counterfactual.py`, same MAE method, same caveats,
deployed component weights — the published "none" row reproduces exactly):

| stop | breached | TP→stop | total | Sharpe | t | maxDD | worst day |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| none | 0% | 0 | +18.24% | +2.50 | +4.56 | 1.50% | −0.99% |
| 40% | 3.6% | 6 | +15.56% | +2.12 | +3.87 | 1.88% | −0.68% |
| **35%** | **4.9%** | **8** | **+14.42%** | **+1.93** | **+3.53** | **2.09%** | −0.90% |
| 30% | 7.0% | 8 | +12.43% | +1.66 | +3.03 | 2.52% | −0.82% |
| 25% | 10.4% | 14 | +11.13% | +1.52 | +2.77 | 3.30% | −0.71% |
| 20% | 14.4% | 39 | +8.92% | +1.24 | +2.27 | 2.82% | −0.74% |
| 12% | 27.6% | 100 | +4.42% | +0.70 | +1.27 | 3.36% | −0.58% |
| 2% (old fallback) | 77.5% | 417 | −2.54% | −0.75 | −1.36 | 2.95% | −0.24% |

The curve is monotone: **every binding stop costs expectancy**, because
adverse excursion is this strategy's entry thesis temporarily winning (§16.3:
64.3% of TP winners first dip 2%). So the declared level is not tuned for
return — it is the widest level that still caps catastrophe inside the
mechanics: at 2× leverage liquidation sits near ~48% adverse, and **35%**
keeps a ~13 pp slippage buffer below it while capping the worst modeled trade
(−92.6%) at −35% and binding on only 4.9% of trades. 30–40% is a judgment
band; every value in it preserves the sign and most of the t. The choice
trades ~0.6 Sharpe against the no-stop ideal for a real venue-placed backstop
— and reclaims the difference between +14.4% and the −2.5% the 2% fallback
was silently producing.

**Implementation.** `ContinuousComponentProfile` gains `stop_loss_pct = 0.35`
(all three components); the runtime tuple, demo producer candidate, and target
metadata carry it, so `venue_protection`/`account_kernel` place the declared
stop instead of the disaster fallback (both already consumed
`metadata.stop_loss_pct` — LONG's path, now shared). Startup validation
rejects a component without a declared stop in (0, 1): the 2% fallback can
never silently become CONTINUOUS's exit rule again. A candidate that lacks
the field publishes no stop and falls to the tighter account fallback —
fail-closed. On the model side `ContinuousEventConfig.stop_loss_pct` feeds
the shared lifecycle's existing stop machinery (`bar_extreme_capped` fills,
10% slippage cap — *more* conservative than the counterfactual's exact-stop
fills). Profile revision bumped to `active_tp12_sl35_v1`; that bump is the
recorded change point. The equity-refresh parity gate now asserts the modeled
stop equals the profile stop, and it caught its own fixture in testing.

**Render — the two methods converge.** The full engine re-run with the
declared stop (`equity_curves_sl35`, identical window/root/args to the Phase 0
render, run label `continuous_ensemble_v2_active_tp12_sl35_v1_historical_equity`):

| | Phase 0 (no stop, withdrawn) | sl35 render | counterfactual predicted |
| --- | ---: | ---: | ---: |
| Sharpe | 2.73 | **1.87** | 1.93 |
| total return | — | +15.79% | +14.42% |
| max DD | −1.29% | −2.85% | 2.09% (unhedged blend) |
| worst day | −0.93% | −0.70% | — |

Exit mix: `max_hold` 1,586 / `take_profit` 641 / **`stop_loss` 114 = 4.9%** —
exactly the counterfactual's predicted breach count — with `stop_price`
populated on all 2,344 rows. The MAE counterfactual on the old ledger and the
full intrabar engine agree within 3% of Sharpe despite independent methods and
the engine's harsher fills.

One fill-mechanics subtlety, caught while validating the worst trades: the
10% slippage cap is multiplicative **on the stop price**, so a short's worst
modeled fill is entry × 1.35 × 1.10 = **−48.5%**, and four squeeze trades
(MAE −49% to −57%) fill exactly there. That lands essentially at the ~48%
2×-leverage liquidation distance — the declared trigger sits 13 pp inside
liquidation precisely so modeled slippage can consume that buffer. It also
sharpens the 35-vs-40 choice: a 40% trigger's capped worst fill (−54.9%)
would sit *beyond* liquidation, meaning the venue would liquidate first and
the modeled fill would be a fiction. 35% is the widest level whose worst
modeled outcome is still real.

**The honest headline for the deployed CONTINUOUS variant is now Sharpe 1.87**
(hedged reconstruction, 2023-03→2026-07), replacing the withdrawn 2.73. t =
1.87 × √3.33 ≈ 3.4 on seen data — a reconstruction of a runtime configuration,
not a validated alpha claim, and it grades nothing (Lane-1).

**What still does not agree.** The deployed daemon holds until TP/max-hold
with the venue stop as backstop; the reconstruction now models the same. But
exit *fills* remain unmeasured (§16.4, blocked on VPS access), and the stop
this section declares has never fired live. The first live stop_loss exits in
the forward record are the natural check that the venue placement matches the
model.

### 20.2 Passive execution: a fast probe joined to the registered experiment

Discovery first: the roadmap's "measure passive execution" already has a
registered in-flow A/B
(`docs/preregistration/passive_execution_experiment_2026-07-20.md`, arm B
shipped in `liquidity_migration/passive_execution.py` on the paper owner).
That instrument is the right grader — hash-assigned arms on *real* entries at
*signal* times — and it is slow for a measured reason: §16.3's realized-scale
finding (~1 position on 38% of days) makes 100 fills/arm months of accrual.

What was missing is a fast bound on the mechanism. Built this session:
`scripts/probe_passive_fill_ab.py` + `liquidity_migration/passive_fill_probe.py`
(protocol pre-declared in the module docstring; 22 unit tests):

- standalone, operator-run, demo-only, min-notional, sequential, mutation-lease
  guarded, flat-account preflight — the `probe_bybit_demo_rules.py` skeleton;
- deterministic hash allocation to taker / post-only arms;
- **intention-to-treat cost**: an unfilled post-only attempt is charged the
  taker fallback at the terminal quote, so drift-while-waiting and would-cross
  rejects are inside the metric, not excluded as inconvenient;
- maker fills with unresolved fees return "unresolved," never a fabricated
  rebate; adverse selection read at fill +30 s;
- written kill criteria: post-only fill rate < 40% in 60 s → passive is
  infeasible for this flow; ITT difference ≥ 0 at 100 resolved attempts/arm →
  no passive edge; otherwise re-price the ledger at the measured passive cost
  with a recorded change point.

Scope boundary, stated in the contract amendment: probe attempts sample
ordinary market states; CONTINUOUS entries sample pumps. The probe bounds
whether the 5.40 bp floor is *mechanically reachable*; only the in-flow A/B
grades the flow. The probe's 60 s window contains arm B's 20 s chase window,
so fill-rate-at-20s is derivable and the instruments stay comparable.

**Blocked here:** this box holds no demo credentials. The first run needs a
box with `BYBIT_DEMO_API_KEY`/`BYBIT_DEMO_API_SECRET`, the fleet stopped and
flat: `python scripts/probe_passive_fill_ab.py --symbols <liquid set>
--demo-rules-file <verified rules> --output <receipt> --confirm-demo-probe`.

### 20.3 Bookkeeping

`scripts/run_with_stub.py` gained three research-only Windows patches
(btc-risk fsync, account-route directory fsync, `rename_noreplace` via
Windows-native no-replace rename) so the contract tests were runnable here;
the account-route **mode-0600 enforcement is deliberately not stubbed**, so
tests that require it stay Windows-red (platform baseline, listed in §16
lineage). Affected suites: 134 passed locally; the 7 remaining failures are
that baseline plus fresh-interpreter import checks, all failing identically
at HEAD.
