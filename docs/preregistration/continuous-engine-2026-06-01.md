# Pre-registration — the CONTINUOUS-fade ENGINE (execution-grade backtest)

**Date:** 2026-06-01 · **Stage:** EXPLORATORY (engine-grade execution, but modeled impact +
research-tuned selection → not yet `candidate`; never promotion evidence)
**Plan:** `docs/research_plan_continuous_fade.md` (Phase 2 / A3 / H3 — "the continuous backtest,
age+rmom gated, funding-to-exit, full-PIT"). **Standard:** `docs/backtesting_errors_we_never_repeat.md`.
**Operator-directed goal:** "get the continuous engine fully made so we can run proper realistic backtests."

## Why this run

Every continuous-fade result to date (P0→p1m) is an **EXPLORATORY proxy** — per-spell additive
PnL, mid-fill at the same close used to rank, a flat 15/30 bps cost with **no market impact**,
no compounding. The receipts flag exactly these gaps (`p1c`: the short-hold edge is
impact-fragile; `p1e`: "the MAR magnitude is partly de-concentrated sizing… a matched-sizing
engine + forward demo is the real arbiter"). This builds the engine that closes those gaps so the
continuous book can be measured under the SAME execution machinery as the deployed daily strategy.

## Hypothesis (frozen before the realistic run)

Under execution-grade fills + a size/ADV impact cost + compounding equity, the continuous
liquid-universe short (the p1j/p1k "VIABLE" cell) **remains positive and all-weather but is
haircut materially vs the proxy** — and the daily-cadence proxy of the same signal remains
MAR-superior (the p1k finding). Falsifier: it goes negative on a venue/era once honest costs +
the +1h entry are applied, OR the haircut is small (≤10%) — either is informative.

## The engine — `liquidity_migration/continuous_events.py`

Reproduces the EXPLORATORY **selection** exactly (so it is auditable against the proxy) but runs
it through the daily engine's validated execution core:

- **Selection (ported from `scripts/p1d_continuous_turnover._deciled_panel`, PIT-causal):** 5 trailing
  closed-bar features (`rv_168h, vov, dist_low, xsret7, xsret3`) → within-ts composite decile, on
  the **rmom-low half** (causal day-floor lag1 join of `residual_momentum.parquet`); short the top
  composite decile (D9); **fresh spell entry** (gap > 1h); **liquid gate** (signal-bar hourly
  `turnover_quote` ≥ threshold, default $500k/h).
- **Execution (REUSES `_simulate_indexed_trade` + `trade_lifecycle` verbatim — identical to the
  daily engine):** stop fills (`bar_extreme_capped` 10%), MAE/MFE, exit ladder, **funding-to-exit**
  (`_funding_lookup`/`_perp_funding_return`, settlement-window-correct), and metrics
  (`build_equity_curve` compounding equity, `_daily_sharpe`, DD, worst-day, underwater).
- **The three realism upgrades the proxy lacked (the point of the engine):**
  1. **Honest +1h entry.** Fill at the close of the bar AFTER the deciding bar
     (`entry_bar_end = signal_ts + (1+entry_delay_hours)·1h`, default delay 1). The proxy filled at
     the same close it ranked on (execution look-ahead). `delay=0` reproduces the proxy (validation only).
  2. **Real round-trip cost = `2·taker + 2·spread + 2·impact`**, where
     `impact_bps = impact_coef_bps · participation^impact_exponent`,
     `participation = position_notional / signal-bar hourly turnover`,
     `position_notional = (gross_exposure/max_active)·deploy_capital_usd`. This is the
     **capacity-aware** cost the p1c argument and the integrity gate demand — impact rises with size
     and falls with liquidity. Defaults: taker 5.5, spread 2.5, impact_coef 50, exponent 0.5,
     deploy $1M. A `flat_round_trip_bps` override exists for proxy-parity validation.
  3. **Compounding equity + true concurrency/cooldown** (heap of exit-times, `max_active` cap,
     per-symbol cooldown = hold), and the full artifact set.

## Pre-committed run grid + decision rule

- **Validation (delay=0, flat 30 bps, no stop):** reproduce the p1j/p1k `cont` per-trade count and
  daily-PnL series to within proxy tolerance (the selection/execution port is correct). Label: `invalid`
  for evidence (look-ahead entry) — diagnostic only.
- **Realistic (delay=1, impact cost, both venues, early/recent):** cells = hold {6,12} h × stop {none, 25%}.
  Primary metric **MAR** (ann/maxDD), Sharpe + DD + worst-day + early/recent secondary.
- **Decision:** report honestly. The continuous short is a real candidate sleeve only if it is
  **positive both venues both eras** after honest costs. MAR-vs-daily and the impact haircut are the
  headline. No promotion — forward demo (operator-gated) is the only Tier-3 arbiter; nothing here changes
  the live profile.

## Run labels & artifacts

Default label **EXPLORATORY** (engine-grade fills, but impact coefficients are *modeled* not
venue-calibrated, and the selection params come from a heavily multiple-tested research arc → not
`candidate` until impact is calibrated against real fills and the cell is not re-tuned on the window).
Artifacts per run: trade ledger CSV, equity-curve CSV + PNG, monthly CSV, split metrics, max DD,
worst-day, config hash, data-root identity, run-label — written under `<root>/reports/continuous_events/`.
Full-PIT roots only (the panel is built from full-PIT klines; the rmom join is PIT-causal lag1).

## Post-run results (2026-06-01)

**Engine built** (`liquidity_migration/continuous_events.py` + `continuous-events` CLI + 11 tests;
full suite 1044 pass, ruff clean). **Accounting note:** the headline equity is **additive /
fixed-capital** (consistent with the fixed-capital impact model and proxy-comparable). An early cut
reused the daily engine's *compounding* equity, which — paired with a fixed-$1M impact charge —
inflated returns absurdly (binance h12 "14,057%"); compounding implies a growing book that would
face growing impact the fixed model never charges, so additive is the honest choice here. A
compounding figure is kept as a reference field only.

**Port validation (delay=0 = same-close fill, flat 30 bps, additive — reproduces the proxy):**
bybit h12 → **12,981 trades** (proxy p1j: 13,006; 0.2% off = my stablecoin exclusion), **total
+390.9%** (proxy +388.2%; 0.7% off), ann ~124% (proxy ~123%). MAR 33.5 vs proxy 39 — the only gap is
maxDD 3.70% vs 3.15%, from the engine's settlement-correct funding-to-exit (numerical equivalence,
not a bug). **The selection + execution port is faithful.**

**Honest realistic grid (delay=1 = real +1h entry, size/ADV impact cost, funding-to-exit, additive,
both venues, fresh rmom-D9, liquid ≥$500k/h, 2% wt × max_active 25, $1M deploy):**

| venue | hold | stop | trades | MAR | ann% | total% | DD% | Sharpe | early% | recent% |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|
| bybit | 6h | — | 14,680 | 19.5 | 59 | 187 | 3.1 | 7.4 | +108 | +79 |
| bybit | **12h** | **—** | 12,981 | **27.7** | 103 | **326** | 3.7 | 8.4 | **+195** | **+131** |
| bybit | 12h | 25% | 12,981 | 20.0 | 99 | 314 | 5.0 | 8.1 | +190 | +124 |
| binance | 6h | — | 28,974 | 17.3 | 90 | 277 | 5.2 | 6.8 | +137 | +140 |
| binance | **12h** | **—** | 24,777 | **21.8** | 163 | **502** | 7.5 | 8.5 | **+268** | **+235** |
| binance | 12h | 25% | 24,779 | 20.9 | 156 | 481 | 7.5 | 8.1 | +263 | +218 |

**Findings:** (1) the continuous short **survives honest execution** (+1h entry + impact + funding)
**all-weather both venues both eras** — early AND recent strongly positive. (2) The honest +1h-entry
+ impact **haircut is a clean ~17%** vs the look-ahead/flat-30 validation (bybit h12 MAR 33.5→27.7,
total +391%→+326%). (3) **h12 > h6** (the longer hold clears cost better). (4) **A 25% stop HURTS**
both venues (whipsaw on the fade short) — engine-grade confirmation of the research's "the fade short
doesn't want a tight stop / more fade loses." **Verdict: EXPLORATORY engine PASS — the continuous
liquid short is real and execution-survivable.** Caveats below stand; MAR is still inflated by the 2%
de-concentrated sizing (DD 3–7%), impact is modeled, and a matched-sizing daily-cadence arm + forward
demo remain the real arbiters. Artifacts: `~/SHARED_DATA/cont_engine/` (per-cell ledgers, equity CSV/PNG,
`grid_summary_2026-06-01.json`, `continuous_honest_equity_2026-06-01.png`).

## Skeptic's audit (2026-06-01) — the headline numbers were too good; here is what's real

Operator (correctly) disbelieved the MAR 28–56 / Sharpe 8–13 / DD 2–7%. Three checks:

1. **Survivorship / full-PIT — PASS.** bybit root: 767 symbols, **187 delisted (24%)** + 110 short-lived;
   binance: 697, 27 delisted (4%) + 92 short-lived. The universe is genuinely point-in-time (it shorts
   names that later delisted), not a current-universe snapshot. (Coverage note: binance klines end
   2026-04-30, ~1mo before the configured end.)
2. **Concentration — corrects a wrong framing.** Actual **avg concurrency is ~4–8 names, NOT 25** — the
   max_active=25 cap almost never binds. So the low DD is not "25 independent names." (state-exit, MTM):
   | venue | max_active | concur | realized MAR/DD% | **MTM MAR / Sharpe / DD% / worstDay%** |
   |---|--:|--:|--:|--:|
   | bybit | 25 | 4.4 | 55 / 2.1 | **29 / 10.4 / 3.9 / 3.9** |
   | bybit | 5 | 3.3 | 65 / 5.9 | **49 / 10.2 / 7.9 / 7.7** |
   | bybit | 3 | 2.3 | 36 / 11.7 | **44 / 9.3 / 9.6 / 9.0** |
   | binance | 25 | 8.5 | 62 / 2.9 | **35 / 10.2 / 5.1 / 3.5** |
   | binance | 5 | 4.5 | 39 / 9.7 | **32 / 8.8 / 11.9 / 6.4** |
   | binance | 3 | 2.8 | 20 / 16.8 | **18 / 6.9 / 18.4 / 11.3** |
3. **Portfolio mark-to-market drawdown (correlated squeeze days) — the real correction.** Marking all
   OPEN positions to the daily close (`_portfolio_mtm_equity`) instead of booking realized-PnL-at-exit:
   **MTM DD is ~2× the realized DD and MTM MAR ≈ half the realized MAR** (bybit ma25 55→29, binance 62→35;
   DD 2–3%→4–5%). So the realized-at-exit metrics were optimistic on risk by ~2×; the honest risk-adjusted
   figure is **MTM MAR ~29–35, Sharpe ~10, DD ~4–5%** at the de-concentrated end, degrading hard (binance
   ma3: DD 18%, worst day 11%) as you concentrate.

**What is still not believable, and the decisive remaining test.** Even corrected, **Sharpe ~10 is too high**
for something deployable. It is NOT a data/look-ahead artifact (survivorship clean; features causal; +1h
entry strictly post-decision). It comes from a **consistent ~1%-per-7h fade edge** on extreme pumped alts,
sampled ~14×/day. The most likely inflator is the one the research already named (p1l): the short-only return
is **substantially the recent-alt-bear short-BETA tailwind** — being short alts through a 2-year downtrend.
The settling test is a **beta-neutral L/S** (long D0 / short D9), which strips market direction; plus the
daily-close MTM still misses intraday squeezes, impact is modeled, borrow/fill-availability is unmodeled, and
it is in-sample with no forward demo. Artifacts: `~/SHARED_DATA/cont_engine/full_sweep_2026-06-01.json`,
`continuous_mtm_concentration_2026-06-01.png`.

## Beta-neutral L/S (2026-06-01) — the short-beta hypothesis was WRONG; the edge is cross-sectional

Built the long leg (long bottom composite decile D0, symmetric to short D9 on the same rmom-low panel),
combined into a beta-neutral L/S book, measured each book's beta to the equal-weight alt-market daily return
(portfolio MTM, state-exit, +1h, impact):

| venue | book | trades | MTM MAR | MTM Sharpe | MTM DD% | total% | beta_mkt |
|---|--:|--:|--:|--:|--:|--:|--:|
| bybit | short-only | 17,072 | 29 | 10.4 | 3.9 | +363 | **−0.10** |
| bybit | long-only (D0) | 9,091 | −0.3 | −3.4 | 71 | −71 | +0.07 |
| bybit | **L/S** | 26,163 | 18 | 9.4 | 5.0 | **+292** | **−0.04** |
| binance | short-only | 34,969 | 35 | 10.2 | 5.1 | +549 | **−0.20** |
| binance | long-only (D0) | 20,098 | −0.3 | −4.2 | 152 | −152 | +0.14 |
| binance | **L/S** | 55,067 | 37 | 9.9 | 3.5 | **+398** | **−0.07** |

**The short book's market beta is only −0.10/−0.20 — modest, not dominant.** Neutralizing it (the L/S, beta
≈ −0.04/−0.07) **keeps ~75–80% of the return** (bybit +363→+292, binance +549→+398) at **Sharpe ~10**, and on
binance it even *lowers* DD (5.1→3.5%). So the prior hypothesis (mine and p1l's "substantially short-beta
tailwind") is **refuted**: most of the return is a **cross-sectional fade/reversal** edge (D9 fades relative to
D0), not "being short alts in a bear." Only ~20–25% was short-beta.

**But a beta-neutral Sharpe ~10 is still not deployable-believable, and the remaining doubt now has a precise
address** (none of which are data/look-ahead/survivorship/short-beta — those are ruled out):
1. **Intraday squeeze risk invisible to DAILY-close MTM.** The short leg shorts names that *just pumped*; they
   can squeeze another +50–100% INTRA-day before fading. Daily marks hide that path (and the liquidation/borrow
   reality). An **hourly MTM** on the 1h bars would deepen DD materially — the top untested hole.
2. **Borrow / short availability** on freshly-pumped illiquid alts is unmodeled — often you simply cannot short
   the most extreme (best) names, or only at punitive borrow.
3. **In-sample STR factor + heavy multiple-testing** → OOS decay; only the forward demo settles it.

Net: the edge is more real (cross-sectional, beta-neutral) than the skeptic — or I — expected, but the
headline Sharpe is still inflated, now most plausibly by intraday-tail blindness + borrow constraints + in-sample.
Artifacts: `~/SHARED_DATA/cont_engine/ls_betaneutral_2026-06-01.json`, `continuous_ls_betaneutral_2026-06-01.png`.

## Full verification audit (2026-06-01) — "check & verify everything"

Code audit + ledger forensics + two empirical leakage/risk tests. **The engine is correct; the headline
risk metrics were optimistic by ~3× and are corrected here.**

**Correctness — all PASS:**
- **Accounting identity:** `net == gross + cost + funding` exactly (max residual 0) on every ledger.
- **No same-bar look-ahead:** entry fills at `signal_ts + 2h` (delay=1), strictly after the decision is
  knowable (`signal_ts + 1h`); `exit_ts > entry_ts` always; cost_return < 0 always.
- **No look-ahead (empirical latency sweep, the decisive test):** the edge decays **smoothly &
  monotonically** with entry delay 1→2→3→6h on BOTH venues — win 59→56→54→51% (bybit) / 56→53→52→48%
  (binance), Sharpe 10.4→6.1 / 10.2→5.4, ~gone by +6h. A leakage artifact would cliff at one bar; this
  glides → a **real multi-hour mean-reversion**, confirming P0b at engine grade.
- **MTM telescopes:** portfolio-MTM total == additive total exactly (gross marks telescope).
- **Sharpe is not an exit-day artifact:** exit-day Sharpe == calendar-grid Sharpe (13.3≈13.2; the book
  trades ~every calendar day).
- **Survivorship:** roots are full-PIT (bybit 24% delisted; binance + short-lived) — shorts names that delist.
- **Regression:** ruff clean, **1046 tests pass.**

**Per-trade economics (delay=1, state-exit):** median gross +0.66% (bybit) / +0.49% (binance) per ~2h short
(mean hold 7h), win 56–59%, fat right-tail losses — a real but modest fade captured repeatedly.

**Risk metrics CORRECTED (the skeptic was right, ~3× too rosy):** drawdown understated at every prior step:
realized-at-exit **2–3%** → daily-MTM **4–5%** → **hourly (intraday) MTM 6–7%** (worst single hour −1.7%
bybit / −2.3% binance — intraday squeezes are real but the ~4–8-name diversification contains them). So the
honest risk-adjusted figure is **MAR ~16 (bybit) / ~23 (binance)** — not the 29–55 (additive/daily-MTM) and
certainly not the original compounding-inflated 55–62. Sharpe stays ~10.

**Net of the whole audit:** the engine has no bug, no leakage, exact accounting, and a genuine in-sample
multi-hour cross-sectional fade (beta-neutral, both venues). The headline numbers were inflated by accounting
(compounding) + understated drawdown (daily marks); corrected, MAR roughly thirds to ~16–23. The residual
Sharpe ~10 is still too high to deploy on faith and is **not** a backtest defect — the un-closable gaps are
**OOS persistence** (in-sample STR factor + heavily-searched selection), **borrow / short-availability** on
freshly-pumped alts (unmodeled — would strip the best entries), and **sub-hourly intra-bar squeezes** (the
MAE tail beyond the hourly close). Only the forward demo settles those. Artifacts:
`~/SHARED_DATA/cont_engine/_latency.log`, `/tmp/hourly_mtm.py` output, `_full.log`, `ls_betaneutral_*.json`.

## Honest bounds (what this engine still is NOT)

- Impact coefficients are a **model**, not calibrated from live fills → the cost level is an assumption
  (exposed as flags; sensitivity is part of the grid). Real borrow cost is not modeled (perp short, so
  funding is the carry — and it is ≈0 here per P0c).
- Realized-PnL-at-exit equity (no intra-hold portfolio MTM) — same caveat as the daily engine (DD is a
  lower bound). Selection deciles are within-ts cross-sectional ranks (PIT-safe: all inputs are closed
  trailing bars), but the universe-at-time is the full traded set, not a live-tradeable snapshot.
- Forward demo remains the only OOS arbiter. This is the prior, not the verdict.
