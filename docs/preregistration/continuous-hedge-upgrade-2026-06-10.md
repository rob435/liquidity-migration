# Pre-registration: hedge upgrade Stage-A — can anything beat the banked 90d-OLS BTC hedge? (2026-06-10)

**Charter:** `docs/research_plan_alpha_hunt_2026-06-10.md` §4-C (operator-issued 2026-06-10).
**Status at registration:** design frozen BEFORE any treatment cell was computed. Working tree
@ `151c685` (clean except the charter doc itself).
**Run label:** `exploratory` (Stage-A overlay; a PASS gates a separately pre-registered Stage-B
engine integration). Tier-2 ceiling regardless — forward demo is the only Tier-3 arbiter.

## Context and the benchmark to beat

The banked hedge (WP3 2026-06-09, receipts `continuous-hedge-{overlay,engine}-2026-06-09.md`)
is a **single 90d-OLS BTC beta**, long-only, cap 2×scale, real funding, on the continuous
uptrend ensemble `winner_base` @ max4. Its banked Stage-A overlay deltas vs the unhedged
control at max4 were: ΔMAR **+0.34 / +1.07** (bybit/binance), ΔSharpe **+0.207 / +0.382**.
The charter calls this estimator "one noisy 90d OLS beta" and directs testing
shrinkage/multi-factor/tradeable-basket replacements. **The bar is the charter's: beat the
banked BTC hedge's variance reduction on BOTH venues without a sample-specific return crutch.**

## Redirect note (charter §4-A killed by feasibility probe, no shot burned)

§4-A (covariance-aware within-book sizing) was the ranked-first target. A pre-registration
feasibility probe (book breadth only, NO outcome metrics) found the ensemble book is **thin**:
median **1 peer** open at entry, 28–30% of entries have **zero** peers, only 38% have ≥2
(bybit 3184 trades / binance 2617, all four components, symbol-deduped). A within-book
correlation allocator is mechanically a no-op (m=1) for ~62% of trades and estimation noise
for most of the rest. The charter's "pile of correlated alt shorts" failure mode came from
*loosened* gates (rmom q25→q50 added correlated breadth) — re-loosening is a pre-registered
null and freeze-banned. §4-A as framed is **dead a priori at current book breadth**; recorded
in `docs/research_summary.md` so it is not re-mined. This receipt is the §4-C redirect.

## Freeze compatibility

The 2023-04→2026-05 continuous window is SPENT for book-variant adjudication (STATE.md
freeze). This experiment adjudicates **no book variant**: both sides of every comparison are
the SAME banked winner_base ledgers; the treatment is an overlay-estimator replacement on the
banked hedge leg, run as a single pre-registered shot (no tuning grids; the sensitivity rows
below are falsifiers/diagnostics, not selection axes). Charter 2026-06-10 explicitly
authorizes §4-C. The line still inherits the rmom latency-knife-edge caveat; nothing here is
a promotion case. REAL_MONEY=false; no deploy change; no push without operator.

## Frozen design

**Machinery:** `scripts/continuous_hedge_upgrade_driver.py`, conventions copied verbatim from
the WP3 Stage-A driver (`continuous_hedge_overlay_driver.py`): winner_base rebalanced ledgers
from `continuous_robustness_2026-06-09/scale_sensitivity/<venue>/winner_base/
w90_tv0.045_max{4,10}_ddh-0.04/equity.csv`; causal betas from trailing 90 LEDGER rows strictly
before row i (min 60 obs); H long-only, total cap 2.0×scale; REAL funding day-sums charged on
hedge legs; turnover costs incl. flat-gap close/reopen and final close. Binding scale =
**max4**; max10 reported only. Both venues, always.

**Arms (all computed in one pass; no arm added or modified after results are seen):**

1. `base_btc` — the banked estimator, re-run through this driver (parity arm).
2. `shrunk_btc` — β̂ᵢ = 0.5·β_OLS90ᵢ + 0.5·β̄ᵢ, where β̄ᵢ = expanding mean of all valid prior
   β_OLS90ⱼ (j<i); if fewer than 30 valid priors, β̂ᵢ = β_OLS90ᵢ. κ=0.5, min_anchor_obs=30,
   fixed a priori. Mechanism: cut estimator noise + hedge turnover.
3. `btc_eth_2f` — bivariate OLS of unit book return on (BTC, ETH) daily returns, trailing 90
   ledger rows; legs H_b=clip(−b_btc,0,2)·scale, H_e=clip(−b_eth,0,2)·scale; if H_b+H_e >
   2·scale, both scaled proportionally to sum 2·scale. Funding and turnover charged per leg.
   Collinearity guard: if |corr(btc,eth)| > 0.995 in the window, fall back to base_btc beta
   for that row. Mechanism: ETH is closer to the alt factor (alt_ew was the WP3 ceiling but
   untradeable); ETH is deep/tradeable — "the tradeable middle".
4. `basket5050` — single OLS-90 beta on the 0.5·BTC+0.5·ETH daily return; hedge fills the
   basket; funding = 0.5·(BTC+ETH) day-sums. (Known simplification: internal 50/50
   rebalancing turnover ignored — order 1e-7/day at 5bps, negligible; stated here so it is
   not discovered later.)

**Cells per arm:** cost {5bps primary, 10bps = 2× stress}; funding {real, off at primary};
beta windows {60,120,150} sign-stability at primary; lag-1 falsifier (betas from row i−1)
for every arm including base.

## A-priori bars (binding cells: max4, W=90, 5 bps, real funding; all deltas vs `base_btc`)

- **c0 (parity precondition):** `base_btc` must reproduce the WP3 Stage-A receipt deltas vs
  the unhedged control within |ΔdMAR| ≤ 0.05 and |ΔdSharpe| ≤ 0.010 per venue. If c0 fails
  the run is INVALID (plumbing), all arm results are disregarded unseen, fix and re-run.
- **b1 (variance reduction):** ΔSharpe > 0 on BOTH venues AND pooled ΔSharpe > +0.05.
- **b2 (no MAR give-up):** per-venue ΔMAR ≥ −0.10 AND pooled ΔMAR ≥ 0.
- **b3 (DD guard):** max-DD not worse than base_btc by >0.5pp on either venue.
- **b4 (2× hedge cost):** at 10bps, arm-vs-base ΔSharpe > 0 on both venues.
- **b5 (no return crutch):** 2023–24 sub-period ΔSharpe ≥ 0 both venues AND funding-off
  ΔSharpe agrees in sign with funding-on (the win must not be a funding artifact).
- **b6 (latency falsifier):** lag-1 arm vs lag-1 base ΔSharpe > 0 both venues.

**PASS = all of b1–b6.** If >1 arm passes: select highest pooled ΔSharpe; tie → lower pooled
annualized hedge turnover. Pooled ΔMAR vs base is REPORTED context (a passer with pooled
ΔMAR > +0.1 is additionally Tier-2-shaped; one in [0, +0.1] is a variance-reduction upgrade
per the charter bar). Diagnostics reported, non-blocking: per-year Sharpe, mean/max H per
leg, annualized turnover, Δworst-day, max10 repeat.

**NULL interpretation (first-class):** no arm passes → the single 90d-OLS BTC beta is not
beatably noisy at this book's scale; the banked hedge stands as-is; §4-C is closed for these
three estimator families and the next agent should not re-mine them without a new mechanism.

**Pre-stated failure modes:** `shrunk_btc` fails if beta variation is real regime variation
(shrinkage misses shifts); `btc_eth_2f` fails if BTC-ETH collinearity (~0.8 daily corr) makes
split betas unstable → noisy legs + turnover; `basket5050` fails if the post-BTC-hedge
residual is not spanned by ETH (adds funding/cost drag only).

## Artifacts

Out root: `C:\Users\user\SHARED_DATA\continuous_hedge_upgrade_2026-06-10\` (cells.csv,
report.json incl. machine verdict). Verdict to be appended to this receipt + one-paragraph
roll-up in `docs/research_summary.md` + STATE.md pointer.

---

## VERDICT (run 2026-06-10, same day, design unchanged)

**c0 parity: PASS, exact** — `base_btc` reproduces the WP3 banked deltas to the reported
digit (ΔMAR +0.34/+1.07, ΔSharpe +0.207/+0.382 bybit/binance).

**`btc_eth_2f`: PASS 6/6 — SELECTED.** vs `base_btc` at max4/W90/5bps/real funding:

| venue | ret | ΔSharpe | ΔMAR | ΔDD(pp) | ΔSh 2×cost | ΔSh 23-24 | ΔSh fund-off | ΔSh lag1 | ΔSh W{60,120,150} |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| bybit | +101.6% | **+0.200** | +0.37 | −0.15 | +0.196 | +0.107 | +0.208 | +0.185 | +0.219/+0.105/+0.059 |
| binance | +76.9% | **+0.079** | +0.50 | +0.17 | +0.078 | +0.045 | +0.082 | +0.062 | +0.101/+0.025/+0.044 |

Pooled ΔSharpe **+0.140** (bar +0.05), pooled ΔMAR **+0.43** (> +0.1 → additionally
Tier-2-shaped). Absolute (max4): bybit +101.6%/MAR 5.73/Sh 2.824 vs base 92.6/5.36/2.624;
binance +76.9%/6.14/2.462 vs 73.7/5.64/2.383. Per-year Sharpe improves in 2023, 2024 AND
2025 on both venues (bybit 2025: 3.52→4.09) — the ETH leg adds value where the BTC-only
hedge was flat, consistent with the WP3 alt_ew-ceiling mechanism (ETH = the tradeable alt
proxy). Legs: mean H_btc/H_eth = 0.020/0.028 (bybit), 0.026/0.032 (binance); ann hedge
turnover 2.6/3.8×. max10 (reported): bybit MAR 7.73→8.89, binance 7.39→7.42.

**`shrunk_btc`: FAIL b1 only** (pooled ΔSharpe +0.036 < +0.05; per-venue +0.034/+0.037,
all other bars pass) — honest near-miss, descriptive only, dominated by `btc_eth_2f`.
**`basket5050`: FAIL** (b1, b2, b5 — bybit ΔMAR −0.15, binance 23-24 ΔSharpe −0.077).
Constraining the two legs to a fixed 50/50 destroys what the free two-factor split earns —
the information is in the *separate* ETH beta, not in more index exposure.

**Honest framing (same as WP3):** in-sample on the spent window; part of the raw return
gain is long-ETH bull-sample drift; the durable claim is variance/regime-robustness, now
including 2025. Tier-2 ceiling; inherits the rmom latency caveat; forward demo decides
anything live. **Next gate: Stage-B engine integration** (multi-leg `ContinuousHedgeRule`)
under a fresh pre-registration; the banked single-BTC hedge remains the live dry-run object
until Stage-B passes and the operator signs off.
