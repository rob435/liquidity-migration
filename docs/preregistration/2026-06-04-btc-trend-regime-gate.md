# Pre-registration — BTC-trend regime gate on the deployed liquidity-migration fade

**Date:** 2026-06-04 · **Author:** self-directed alpha-hunt loop · **Stage:** OPERATOR-DIRECTED DEMO DEPLOY
(2026-06-04, demo/paper only) on EXPLORATORY in-sample evidence; the §Decision-rule binding Tier-2 run is
still PENDING and must precede any Tier-2 demo-candidate / promotion claim. See the on-engine + deploy
sections below.
**Standard:** `docs/backtesting_errors_we_never_repeat.md` · **Tiers:** `STATE.md`. **Branch:**
`research/continuous-halflife-fade` (off main; not pushed).

## What's changing
Add a **causal BTC-30-day-trend regime gate** to the deployed SHORT volume-events fade: take a fade entry
**only when BTC's trailing 30-day return is positive** (risk-on); skip the entry otherwise. One new gate; the
deployed selection/exits are otherwise unchanged. The signal is BTC's own trailing 30d return, lagged one day
(known strictly before the entry decision) — venue-agnostic (BTC is the same everywhere).

## Hypothesis (mechanism, frozen)
The liquidity-migration fade is an **idiosyncratic reversal that needs froth**: it shorts the confirmed giveback
of pumps, and pumps are more frequent / frothier / more reliably-fading in **risk-on (BTC-uptrend)** regimes.
In BTC-downtrends there are fewer/weaker pumps and the fade overlaps with general market weakness, so its
per-trade edge collapses while its drawdown does not. Therefore conditioning the fade on a BTC-uptrend regime
should keep most of the return while removing the low-edge, high-DD downtrend trades → materially higher MAR
and lower drawdown, on both venues. (This emerged from a broader hunt whose unifying finding is that alt extreme
movers *continue* — fat continuation tails — so cross-sectional alt factors aren't net-tradeable; the fade works
*because* it is selective, and this gate sharpens that selectivity by regime.)

## Predicted direction + magnitude (pre-committed, falsifiable)
- MAR improves on **both** venues vs the ungated deployed fade; pooled MAR Δ clearly > +0.10.
- Drawdown reduced on both venues (the gate removes the high-DD downtrend trades).
- BTC-downtrend trades are near-zero-edge on both venues (the discriminator).
- Falsifier: if gated MAR ≤ ungated on either venue, or the downtrend trades carry material positive edge, or
  the improvement is one-venue / one-era only → reject the regime-gate thesis.

## Exploratory evidence so far (EXPLORATORY — not promotion evidence; motivates the binding run)
Deployed daily fade (`run_volume_event_research`, promoted profile, full-PIT, real costs+funding, 2023-04+),
trades split by BTC-30d-trend at entry (causal):

| | ALL MAR (ret/DD) | BTC-uptrend MAR (ret/DD) | BTC-downtrend MAR (ret/DD) |
|---|---|---|---|
| bybit   | 1.61 (+75% / −16%) | **3.12** (+66% / −7%) | 0.19 (+9% / −16%) |
| binance | 0.52 (+24% / −16%) | **1.30** (+20% / −6%) | 0.10 (+4% / −15%) |

Era-stability: bybit gate improves BOTH eras (early 2.06→2.91, recent 1.69→**4.25**); binance gate FIXES the
known recent decay (recent MAR −0.10→**+0.38**, DD 16%→6%) at a modest early cost (3.14→2.44). Causal gate,
believable metrics (no 36/36-up / sub-5%-DD red flags). The earlier "binance fade negative" was a cruder
continuous immediate-entry proxy; the **deployed** binance fade is +24% and the gate improves it.

**Tier-2 fragility battery (on the deployed-fade ledgers) — REFINES the cross-venue claim:**
- **bybit ROBUST**: gated MAR positive in all 3 thirds (2.2 / 3.3 / 5.2), LOO-month sign-stable (+55%..+69%),
  moderate concentration (top-3 months = 43% of 31). A genuine, robust, deployable-grade bybit improvement.
- **binance FRAGILE**: gated edge **76% carried by top-3 of 28 months**, negative in third-1 → concentration-
  driven, NOT robust. So the robust benefit is **BYBIT-PRIMARY**; binance directionally agrees but fails
  fragility (consistent with binance = weaker/different venue). The headline cross-venue MAR-doubling overstated
  binance; the honest claim is "robust on bybit, fragile-positive on binance."

## Roots touched
- [x] `~/SHARED_DATA/bybit_full_pit` · [x] `~/SHARED_DATA/binance_full_pit` · [ ] forward demo (Tier-3, later).

## Decision rule (a priori, BINDING for the confirmation run)
Run the gated vs ungated deployed fade through the standard pipeline with `scripts/r1_robustness.py` (Tier-2):
ACCEPT to demo-candidate iff — net positive both venues; **pooled MAR Δ (gated − ungated) > +0.10** with neither
venue worse than −0.5; ≥30 bybit / ≥20 binance gated trades; survives the cost-stress ladder; majority-of-thirds
positive and LOO-month sign-stable; smell-test clean. Else reject. The gate is a SELECTION change → it must be
**pre-registered and confirmed on a fresh binding run before any deploy claim**; the deployed profile is not
changed without operator sign-off (AGENTS.md: profile change is a hard line) + forward demo (Tier-3).

## Run command (confirmation, to commit with the gate code)
```bash
# add a causal `btc_trend_gate` (off by default) to the volume-events selection, then:
.venv/bin/python -m liquidity_migration volume-events <promoted flags> --btc-trend-gate uptrend  # both roots
.venv/bin/python scripts/r1_robustness.py --sweep-tag btc_trend_gate   # Tier-2 verdict
```

## On-engine confirmation (2026-06-04, EXPLORATORY) + OPERATOR-DIRECTED demo deploy

The gate was implemented as a causal, off-by-default `btc_trend_gate` on the deployed SHORT selection
(`btc_return_30d` = BTC trailing-30d return, lagged 1d via `_cal_roll(shifted=True)`, in
`_attach_market_context`; the regime filter in `_apply_market_context_filters`; CLI `--btc-trend-gate`;
config validation; 11 unit tests incl. the look-ahead/truncation-invariance falsifier). The **exact
deployed promoted profile** (`promoted.short_profile`) was run on Bybit ± the gate over the in-sample
window 2023-04-01→2026-05-28 (`scripts/btc_trend_gate_run.py`):

| gate | trades | return | max DD | ret/\|DD\| | Sharpe | delisted traded |
|---|---|---|---|---|---|---|
| off (deployed) | 596 | +84.7% | −15.6% | 5.42 | 1.43 | 85 |
| **uptrend (gated)** | 369 | +81.0% | **−7.2%** | **11.33** | 1.75 | 68 |
| downtrend (control) | 228 | +2.8% | −15.2% | 0.18 | 0.18 | 50 |

The partition is near-exact (369+228≈596). Uptrend trades keep 96% of the return at half the drawdown;
the downtrend control is near-zero-edge (+2.8% / 0.18) and carries essentially all the drawdown — the
discriminator falsifier does NOT fire (downtrend ret/|DD| 0.18 ≈ the pre-reg's predicted 0.19). The ~2.1×
ret/|DD| lift matches the exploratory MAR 1.61→3.12. Ledger believable (63% win, ±1% per-trade fades,
losing months present, exits mostly `event_decay`). **Labels (honest):** EXPLORATORY, **in-sample** (same
window the gate was proposed on), `full_pit_universe_pass=False` on the run box but **delisted-inclusive
(no survivorship — 68 delisted names traded)**; `ret/|DD|` is a quick read, **not** the Tier-2 MAR.

**OPERATOR-DIRECTED demo deploy (2026-06-04).** The owner/operator directed promotion of `uptrend` to the
deployed SHORT profile (`_demo_event_config(profile="promoted")`) **ahead of** the binding Tier-2 run —
demo/paper only, no real money, `REAL_MONEY` not toggled; Bybit is the deployed venue and the gate's robust
venue. Same operator-directed-ahead-of-validation pattern as the BAC-1 deploy. `strategy_id` kept → the
deploy date is the clean pre/post forward split.

## STILL PENDING (this deploy does NOT discharge them)
- **Binding Tier-2 battery** (`scripts/r1_robustness.py`: cost-stress ladder, thirds, LOO-month, bootstrap
  MAR-Δ) — the a-priori decision rule above. NOT yet run. Until it passes, the deploy is operator-directed
  forward-demo scouting, **not** a Tier-2 demo-candidate verdict.
- **Binance / cross-venue.** Not run here; the pre-reg fragility battery flagged binance as concentration-
  FRAGILE. The honest cross-venue claim remains "robust on Bybit, fragile-positive on Binance."
- **Forward demo (Tier-3)** is the only real-money arbiter; out of scope. The account stays on demo.

## Verdict
**Operator-directed demo deploy, on-engine EXPLORATORY confirmation only.** The gate does what the pre-reg
claimed on Bybit (mechanism verified via the discriminator), but the binding Tier-2 confirmation is PENDING
and no real-money/promotion-grade claim is made on this evidence.
