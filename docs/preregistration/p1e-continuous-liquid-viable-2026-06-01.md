# Pre-registration — P1e/Avenue D (REVERSAL): the continuous fade is VIABLE on the LIQUID universe

**Date:** 2026-06-01 · **Stage:** EXPLORATORY (PIT-clean selection + additive portfolio proxy; NOT promotion evidence)
**Plan:** `docs/research_plan_continuous_fade.md` · **Reverses** the "weak book / CLOSED" verdict of `p1c`.
**Standard:** `docs/backtesting_errors_we_never_repeat.md`

## What happened (a second self-correction — in the optimistic direction)

`p1c` closed the program: "real signal, but a weak tradeable book — small capacity + impact-fragile." That
verdict **averaged over the illiquid tail** and was wrong. The decisive re-test (`p1i`, bucketing the fade by
entry liquidity) shows the edge is **monotonically STRONGER on MORE-liquid names** — the opposite of what the
capacity argument assumed — and CV1 (edge is venue-general) predicted exactly this.

## Evidence

**p1i — fade by entry-liquidity (FRESH D9, gross bps):**

| 6h fade FULL/E/R | <$50k/h | $500k-1M/h | >$1M/h |
|---|--:|--:|--:|
| bybit | +52/+47/+55 | +93/+76/+137 | **+118/+99/+175** |
| binance | +41/+15/+43 | +64/+49/+94 | **+93/+74/+148** |

Strongest on the most liquid names, all-weather. The LIQUID subset is large: bybit ≥$500k/h = 22% of entries
(median $1.4M/h → ~$1.4M book @1%); binance ≥$500k/h = **51%** (median $1.76M/h → ~$1.8M book). On liquid
names cost is also low (spread + taker ≈ 25-30 bps RT, not 100), so the 6h edge nets ~+65-90 bps.

**p1j — SANE additive portfolio proxy (liquid universe, realistic cost, concurrent-position sim; fixes p1h's
compounding bug):**

| cell | bybit MAR (ann%/DD%) | binance MAR (ann%/DD%) |
|---|--:|--:|
| 6h ≥$500k 30bps | 23 (78/3.4) | 26 (108/4.2) |
| 6h ≥$500k 50bps | 16 (60/3.6) | 12 (71/5.8) |
| 6h ≥$1M 30bps | 18 (55/3.1) | 30 (89/2.9) |
| 12h ≥$500k 30bps | **39 (123/3.2)** | **35 (182/5.2)** |
| 12h ≥$1M 30bps | 27 (85/3.1) | 37 (144/3.9) |

**Every cell all-weather** (early AND recent strongly positive, both venues), robust to cost (survives 50 bps),
liquidity threshold (≥$1M still MAR 17-37), and hold (6h & 12h). DD 3-6%; skip_frac ≈0 (book not capacity-bound
at this scale). Funding included (≈0, P0c).

## VERDICT — the continuous strategy is VIABLE on the liquid universe; the "weak book" close is RETRACTED

A continuous, any-hour short of **fresh rmom-gated composite-D9 entries restricted to liquid names
(≥$500k/h, ideally ≥$1M/h), short hold (6-12h)** is a **real, all-weather, cross-venue, cost-robust edge with
deployable capacity (~$1-3M+).** It captures the off-close fresh fades the daily strategy misses, strongest
exactly where capacity is highest. **The mission's thesis — a continuous, always-on, any-hour version IS
viable — is supported.** Both boss battles fall (BB1: a fresh D9 entry is the fade-started confirmation; BB2:
all-weather on liquid names). This **justifies the operator-gated engine-grade backtest + forward demo.**

## Honest bounds (calibration — I reversed twice; the engine/forward-demo is the real arbiter)

- EXPLORATORY: PIT-clean *selection* (within-ts composite rank + lag1 rmom + age), realized forward returns —
  but per-spell means with a simplified concurrency sim, **not a full engine MAR.**
- **The MAR magnitude (12-39) is partly de-concentrated SIZING** (2% weight × ~25 positions → DD 3-6%), not
  purely a better signal than the daily (MAR 3-6, reported at heavier concentration). A fair head-to-head needs
  **matched sizing**; expect the true edge over the daily to be the **off-close breadth + lower DD**, not a 5×
  return multiple.
- **Capacity ~$1-3M**, thinning as you scale (impact rises above 30 bps past ~$1.4M); a small-mid strategy.
- The liquid filter is applied at the trade stage on the full-universe D9; a **liquid-universe re-decile** is
  the cleaner construction (engine step).
- Substantially the STR/residual-reversal factor (rmom) → a continuous factor-harvest, not certified unique alpha.

## Matched-sizing addendum (p1k) — the calibration caveat, quantified: DAILY is MAR-optimal, continuous is return-max

Running the matched-sizing comparison (same liquid universe / 2% wt / max_active 25 / 30 bps; continuous
any-hour vs a daily-only 01:00 proxy, the SAME signal):

| arm | bybit MAR / ann% / DD% | binance MAR / ann% / DD% |
|---|--:|--:|
| cont_6h | 23 / 78 / 3.4 | 26 / 108 / 4.2 |
| cont_24h | 36 / **166** / 4.6 | 27 / **245** / 9.2 |
| **daily_24h (01:00 only)** | **42** / 92 / **2.2** | **36** / 146 / **4.0** |

**The daily-cadence entry has the HIGHEST MAR, Sharpe (10.3), and lowest DD** — the 01:00 close is the
highest-quality entry hour (consistent with the hod profile). Continuous (24h) earns **~1.7-1.8× more
absolute return** (off-close breadth) but at higher DD → **lower MAR.** So the p1e "VIABLE" headline holds
(continuous is a real, strong, tradeable strategy — the "weak book" close was wrong), **but tempered: it does
NOT beat the daily on risk-adjusted return** (MAR-primary, the program's metric). Continuous's value is
**absolute return / breadth / capacity utilization**, not a better risk-adjusted edge; the daily is the
MAR-optimal point, continuous-24h the return-max point on the same frontier.

## CALIBRATED FINAL VERDICT

The continuous liquidity-migration fade is **real, all-weather, cross-venue, and tradeable on liquid names**
(deployable ~$1-3M; the "weak book" close is retracted). **On MAR-primary the DAILY cadence (close-only) is
OPTIMAL** (the 01:00 close is the best entry hour: MAR 42/36, DD 2.2/4.0%); **continuous adds ~1.8× absolute
return via off-close breadth, at higher DD / lower MAR (36/27).** Net: continuous is a viable
**return-maximizing / capacity** alternative, NOT a risk-adjusted improvement over the daily. The "daily
favored" intuition is vindicated — for the right reason (entry-quality), not the retracted daily-cycle artifact
or the wrong cost argument. **Note (correcting an over-reach): the "combined book" = the continuous book**
(daily close + off-close = all entries) — which I measured at MAR 36/27, **below** daily-alone (42/36). The
off-close sleeve is the *same signal on the same names* → highly correlated → little diversification benefit,
so it adds breadth/return, not risk-adjusted edge. **Daily-alone (close-only) is the MAR-optimal harvest of
this signal.** A weight-optimized daily-core + small off-close overlay is the only continuous variant that
could marginally help (and only if the off-close sleeve diversifies enough — doubtful given the correlation).

## Market-neutral addendum (p1l/p1m) — continuous's candidate value-add is a DIVERSIFYING SLEEVE, not the short

The short-only books carry short-beta risk (flattered by the recent alt-bear). A **beta-neutral continuous L/S**
(long D0 / short D9, liquid) **slashes DD** vs short-only (bybit 23→19%, binance 38→18%) at comparable MAR
(p1l), and is **low-correlation with the daily short** (p1m: corr **0.32/0.26**, vs 0.65/0.72 for the
continuous short which shares the D9 leg). **Adding it to the daily short improves the combined book** (bybit
Sharpe 11.6→12.6 / MAR 47.9→69.9 @w=1.0; binance Sharpe 11.1→12.3 @w=0.5) — a diversifying sleeve like the long
sleeve. So the direct continuous *short* doesn't beat the daily, but a continuous market-neutral L/S **overlay**
improves the live book's risk-adjusted return. **HONEST TEMPERING (the redundancy gate):** the EXISTING long
sleeve already diversifies the short **better** (corr ~−0.03 vs the L/S's +0.3), so the decisive deployment
question is whether the continuous L/S is **additive to the long sleeve or redundant** — untested, needs a
clean 3-way engine backtest (deployed short + long + real continuous L/S). So it's a **genuine but
possibly-redundant** candidate sleeve. `scripts/p1l_market_neutral.py`, `scripts/p1m_combined_portfolio.py`.

## Next (operator-gated)

Engine-grade rolling backtest on the **liquid universe** (re-decile within liquid names; realistic
impact/cost model; true concurrency/exit; funding-to-exit; weight-optimized daily-core + off-close overlay;
**+ the market-neutral L/S overlay validated 3-way vs the deployed short AND the long sleeve** — additive or
redundant?;
forward demo). Justified, but **NOT urgent** given **daily-alone is already MAR-optimal** — the only upside is
absolute-return/capacity at lower MAR, or a marginal diversification gain from a small off-close overlay (the
off-close sleeve is highly correlated with the daily core, so the gain is likely small). Pre-register before
building. Artifacts: `~/SHARED_DATA/p1k_matched_comparison_2026-06-01.{out,json}`; script
`scripts/p1k_matched_comparison.py`.

## Lessons logged (both directions)

Never finalize a verdict on an aggregate that mixes a strong core with a weak tail — **decompose by the
constraint** (here, liquidity). Combined with the earlier lessons (don't finalize a null on a single hold
horizon; don't trust a proxy MAR that assumes mid-fills), the program flipped PASS→null→viable→weak→VIABLE; the
honest landing required testing each premature conclusion. Artifacts: `~/SHARED_DATA/p1{i,j}_*_2026-06-01.{out,json}`;
scripts `scripts/p1{i,j}_*.py`. Label: **EXPLORATORY**. Never promotion evidence.
