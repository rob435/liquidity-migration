# Pre-registration: continuous "winner" robustness / falsification battery

**Date:** 2026-06-09
**Author:** rob435 (operator) / research session
**Stage:** proposed
**Label:** EXPLORATORY falsification of an existing research lead — NOT a promotion. No
config here is promoted, deployed, or paper-readied by this run. In-sample walk-forward /
backtest never promotes past Tier-2; forward demo/paper is the only Tier-3 arbiter.

## Why
The current best continuous research lead reaches MAR 6–7.5 via a hand-weighted component
ensemble run at ~7.5× average leverage (`tv0.045/max10`) under a *modeled* (uncalibrated)
impact cost, selected on a single in-sample window with no internal OOS. This is the highest
overfit-prone object in the program and the exact axis it has been burned on before
(parameter mining; retracted Round-2 null; v1 continuous look-ahead; the 2026-06-09 ridge
combiner rejected at Tier-1 with negative OOF IC). Before any further refining we falsify it:
is the edge real, or is it selection + leverage?

The target metric is deliberately deferred ("decide after the stress-test") — these a-priori
rules judge **stability/survival**, and the leverage-sensitivity + haircut results set a
re-anchored target afterward.

## Object under test (reproduced bit-exact 2026-06-09 at commit `5e1c960`)
- **Base ensemble winner (cheap-path centre):**
  `winner_base = {turn3p3:0.30, turn4p3:0.20, turn4p5:0.40, age210tp14:0.10}`,
  rule `w90/tv0.045/max10/ddh-0.04`, no strategy-equity momentum.
  Reproduced: Bybit +226.22% / MAR 6.18 / −11.69% DD / avg-scale 7.47;
  Binance +142.48% / MAR 6.01 / −7.71% DD / avg-scale 7.98.
- **Canonical downtrend-inclusive winner (STATE.md):**
  uptrend `{turn3p3:0.30, turn4p3:0.20, turn4p5:0.30, age210tp14:0.20}` + 40% downtrend
  `dt_turn4p5` sleeve filtered `premium_24h_mean>=0`, rule `w70/tv0.045/max10/ddh-0.04`.
  Target: Bybit +265% / MAR 7.50; Binance +191% / MAR 6.84.

Component source ledgers are precomputed and present under `~/SHARED_DATA` (verified):
`continuous_merged_signal_raw_2026-06-07`, `independent_continuous_entry_filter_sweep_*`,
`independent_continuous_tp_hold_sweep_*`, downtrend raw `continuous_downtrend_regime_extension_2026-06-08`.

## Roots touched
- [x] bybit_full_pit / binance_full_pit — only via the **precomputed** component ledgers
  (cheap path) and the downtrend `--raw-root` reuse; no new trade selection is mined here.
- [ ] forward demo/paper — NOT touched.

## Battery (frozen before the run)

Driver: `scripts/continuous_ensemble_rebalance_scout.py` (cheap recombination of fixed
component ledgers + rebalance rule), `scripts/continuous_downtrend_regime_scout.py`
(downtrend-inclusive, `--raw-root` reuse), and a self-contained sub-period / bootstrap
analysis on the winner equity ledger. Both venues, identical params (no per-venue tuning).

1. **Weight perturbation (DOF / selection inflation).** Full 0.1-step simplex over
   `{turn3p3, turn4p3, turn4p5, age210tp14}` at the winner rule. Report the pooled-MAR
   distribution and where `winner_base` sits in it.
2. **Scale / leverage sensitivity.** Fixed winner weights; risk grid
   windows`{70,90}` × tv`{0.025,0.035,0.045}` × max`{4,6,8,10}`, ddh`-0.04`. Report
   MAR/return/DD/avg-scale surface.
3. **Cost stress.** Winner config at cost-multiplier `{1.0, 1.5, 2.0, 3.0}`.
4. **Sub-period / regime stability.** Thirds, per-year, leave-one-out-month on the winner's
   own equity ledger (candidate-only; daily-delta optional).
5. **Cross-venue matched.** Inherent to every run above.
6. **Statistical haircut (report-only, sets re-anchored target).** Circular-block bootstrap
   on the winner daily basket return → Sharpe/MAR CI + left tail; plus a multiple-testing
   note estimating configs tried across the continuous campaign.
7. **Downtrend-inclusive confirmatory.** Reproduce the canonical winner and sweep downtrend
   scale `{0.3,0.4,0.5}` (reusing the raw root) to check it is not a sharp downtrend-scale peak.

## Decision rule (a priori)

**ROBUST** (edge is more than selection+leverage → continue refining; advance the candidate
toward forward-demo readiness, lower-leverage variant preferred) iff ALL hold:
- **Perturbation:** median pooled MAR of the 0.1-simplex neighbours ≥ **60%** of
  `winner_base` pooled MAR, AND ≥ **50%** of neighbours keep both-venue positive return.
  (Winner may be high-ranked but must NOT be a lone spike.)
- **Leverage:** at the LOW-leverage base rule (`tv0.025/max4`), both venues positive return
  AND pooled MAR ≥ **2.0**. (Edge survives without max leverage.)
- **Cost:** both venues positive return at 2× cost, pooled min-MAR ≥ **2.0** at 2×, no return
  sign-flip at 3×.
- **Sub-period:** no third with negative candidate return on either venue; ≥ **2 of 3** thirds
  positive both venues; no single LOO-month flips the headline sign.
- **Cross-venue:** the qualitative verdict (positive return; MAR ordering) agrees on both
  venues (STATE.md non-negotiable #3).

**FRAGILE / selection-inflated** (do NOT keep adding hand-tuned components; re-anchor to a
lower-leverage, more parsimonious object and set the target from the leverage curve) iff ANY
of: perturbation lone-spike, leverage-only edge, dies by 2× cost, or carried by one
month/one venue.

The haircut (item 6) is report-only but: flag additional caution if the bootstrap MAR
left tail < 0 or the multiple-testing-deflated Sharpe < ~0.5.

## Post-run results

**Run commit:** `5e1c960`. **Date:** 2026-06-09. Artifacts under
`~/SHARED_DATA/continuous_robustness_2026-06-09/{perturbation,scale_sensitivity,cost_*}` and
the smoke `~/SHARED_DATA/continuous_robustness_smoke_2026-06-09`. Centre = base ensemble
winner `{turn3p3:0.30,turn4p3:0.20,turn4p5:0.40,age210tp14:0.10}` @ `w90/tv0.045/max10/ddh-0.04`,
reproduced bit-exact (bybit +226.2%/MAR 6.18, binance +142.5%/MAR 6.01).

1. **Weight perturbation** — full 0.1 simplex, 286 vectors @ winner rule. **100%** both-venue
   positive return; **100%** both-venue MAR>0; 100% pooled-MAR ≥3.0; 96.5% ≥4.0. Pooled-MAR
   (venue-mean) distribution p0=3.60 / p50=5.48 / p100=7.12. Winner at 89.5th pct;
   median/winner = **0.90** (bar ≥0.60). Not a spike — a high plateau. **PASS.**
2. **Scale/leverage** — winner weights × {w70,90}×{tv.025,.035,.045}×{max4,6,8,10}. `tv` is a
   **dead knob** (scale pinned at `max_scale` cap → identical rows across tv). De-levered to
   max4 (avg ~3.5×): bybit +84%/MAR 5.0/−5.3%DD, binance +60%/MAR 4.6/−4.3%DD → pooled MAR
   ~4.8 (bar ≥2.0). Edge is NOT pure leverage. **PASS.**
3. **Cost stress** — winner @ cost-mult {1,1.5,2,3}×: 2× → bybit +151%/MAR 4.02, binance
   +94%/MAR 3.24 (pooled min-MAR 3.24, bar ≥2.0); 3× → bybit +104%/MAR 2.38, binance
   +59%/MAR 1.87 (both still positive, no sign-flip). Cost-mult scales the *modeled impact*
   term only (funding drag flat ~−28%/−20%). **PASS.**
4. **Sub-period (candidate ledger)** — bybit: 3/3 thirds + 4/4 years positive, 27/35 green
   months (worst −6.4%), LOO-month full return floors at +184%. binance: 3/3 thirds + 4/4
   years positive, 24/34 green (worst −5.1%), LOO floor +109%. Bootstrap (2000×, 20d blocks):
   MAR p5 **>0 both venues**; ann. Sharpe 3.05/2.62 (p5 1.81/1.49). **PASS** — caveat:
   strength is recent-tilted (2025 carries the high MAR; mid-2024 thin-positive).
5. **Cross-venue matched** — every run identical params both venues; verdict agrees. **PASS.**
6. **Haircut (report-only)** — bootstrap MAR left-tail >0 both venues (no caution flag). The
   286-vector plateau IS the weight-axis multiple-testing answer: the winner is not a lucky
   max over noisy trials (worst weight choice still pooled MAR 3.6). Residual deflation risk
   is the recent-tilt + modeled-impact, not the weight search.
7. **Downtrend-inclusive confirmatory** — NOT re-run here. Downtrend-scale sensitivity was
   already explored in-campaign (the 0.4/70d point was *selected* by the scale/window
   interpolation; STATE.md), and the dt sleeve is a +17%/+34% risk-shaping add-on, not the
   core edge. Deferred for a clean pre-registered re-run if desired
   (`continuous_downtrend_regime_scout.py --raw-root continuous_downtrend_regime_extension_2026-06-08
   --downtrend-scales 0.0,0.3,0.4,0.5 --filter-ids premium_24h_ge0
   --uptrend-weight-specs u_base=turn3p3:0.3,turn4p3:0.2,turn4p5:0.3,age210tp14:0.2
   --windows 70 --target-daily-vols 0.045 --max-scales 10 --drawdown-halves -0.04`).

## Verdict

**ROBUST (in-sample) — NOT selection-inflated; advance, but de-levered and recent-tilt-discounted.**
All five a-priori gates pass decisively. The hypothesis I most suspected — that the
hand-weighted ensemble at ~7.5× leverage was parameter-mined overfit — is **refuted**: the
entire weight simplex is a both-venue-positive plateau (286/286, MAR 3.6–7.1), the edge
survives de-leveraging to ~3.5× (pooled MAR ~4.8), survives 3× modeled-impact cost, and is
positive across every third/year on both venues with a bootstrap MAR left-tail >0. The
weighting, the leverage, and the cost model are **not** where fragility lives.

The battery did, however, surface the *real* residual risks (which more weight/component
tuning cannot address): (1) **recent-tilt** — the MAR-6 headline is partly a 2025-regime
number; 2023–24 is only thin-positive (ann. MAR ~1.4–2.7). (2) **Modeled, uncalibrated
impact** at avg 7.5× scale / $1M deploy — survives 3× stress but the true fill impact is the
binding unknown only a real calibration or forward demo can settle. (3) **Funding drag**
(~−28%/−20% of return) rests on the open Binance funding-interval debt (4h vs 8h). (4) **No
forward OOS** — this is in-sample and cannot promote past Tier-2.

**Re-anchored target (resolves the deferred Q2):** drop the max-leverage framing. Run the
candidate at **max4–6 (avg ~3.5–5×)** — keeps both venues MAR ~4.6–5.9 with **half** the
drawdown (−5% vs −12%) and far lower real-impact exposure — and set the forward expectation
from the **weaker 2023–24 sub-period (ann. MAR ~1.5–2.5)**, not the 2025 number. The next
robustness gates are the **funding-interval correctness fix**, a **real impact calibration**,
and **forward-demo accumulation** — not another weight/component sweep.

**Addendum 2026-06-10:** residual risk (3) is CLOSED: funding accrual was verified
against raw datasets (`continuous-funding-debt-closure-2026-06-09.md`) and the
Binance funding dataset was rebuilt from 51 to 697 symbols
(`binance-funding-rebuild-2026-06-09.md`). Status of the three gates as of
2026-06-10: funding fix DONE; impact calibration BLOCKED on fresh fill accrual
(R4, `continuous-capacity-impact-2026-06-09.md`); forward-demo accumulation
started (demo orders ON, replay collector seeded). The 2023-04→2026-05 window is
FROZEN — no further in-window sweeps.
