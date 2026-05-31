# Intraday extreme-burst short — full research synthesis (unbiased)

**Written 2026-05-31.** A single, balanced record of the entire intraday investigation
(K0 → I2k) and the funding analysis, written to avoid the over-claim/under-claim swings
that happened during the live research session. Numbers are from the full-PIT roots on the
5950X; the strategy is **research-stage** and **nothing here is promoted to real money**.
Methodology gate: `backtesting_errors_we_never_repeat.md`. Companion records: `research_summary.md`
(consolidated program record), `research_plan_intraday_kernel.md` (the phase-by-phase log),
`STATE.md` (live state).

---

## TL;DR — the honest verdict

The intraday "extreme-pump-burst" reversal is a **real, beta-neutral, cross-venue signal**
(established cleanly at I1b). As a **standalone short** under realistic costs it is **marginal
and recent-tilted**, not a clean edge:

- **Funding is the dominant cost** and it is highly sensitive to how it is accounted.
  The realistic (fair) estimate at the best configuration is **MAR ≈ +0.30 (bybit) / +0.49
  (binance)** at a 24h hold, 25% stop — *positive on both venues but thin*.
- The **equity curve** shows the profit is **recent-regime-weighted**: bybit is *underwater
  for ~3 years* (2022–2024, −13% trough) and only net-positive in the last ~6 months; binance
  is more two-sided but still modest (+5.6% over 5+ years).
- The candidate survives only after **extensive search** (stop × entry × funding-treatment ×
  hold), so it is **weak evidence, not validation**, and it sits **at the Stage-B proxy's
  resolution limit** (the verdict swings −0.54 → +0.30 → +3.08 depending on funding accounting).

**The robust, already-validated all-weather edge is the DAILY age + residual-momentum
strategy** — not the intraday short. The intraday short is, at best, a **higher-risk, marginal,
unvalidated add-on** that would need an **engine-grade backtest (I3)** to settle. That build is
**operator-gated**. The daily strategy stands on its own regardless of the intraday outcome.

---

## 1. How we got here — the timing axis closed, the selection axis opened

- **E1** (execution-premium test): shorting the *confirmed fade* vs immediate entry adds nothing
  robust — the alpha is **SELECTION**, not fill-timing. Fill-timing is a non-lever.
- **K0** (read-only ceiling): the daily-close (+1h) entry sits a median **~8–11% below** the
  event-day intraday peak — on both venues, both eras. So there *is* an intraday giveback the
  daily entry misses. Necessary, not sufficient (it's an optimistic exact-top ceiling).
- **K1a** (cheap feasibility): running the **same daily selector hourly** does **not** capture
  that ceiling — the selector can't *confirm* the event (cumulative-turnover rule) until median
  ~15:00 UTC, ~9h after the morning peak, by which point price has faded back to ≈ the daily
  entry. The ceiling is real but **un-capturable by faster detection of the same event**.
- **Operator reopening:** K1a tested the wrong thing — the daily selector run hourly, not a
  **purpose-built intraday signal**. Correct call. That reopening is the I-phase.

## 2. The signal is real — I1a / I1b

- **I1a:** faders carry a cross-venue intraday **exhaustion fingerprint** (turnover climax ~4–4.6×
  at the peak then rolloff, upper-wick rejection). Necessary but not a strategy.
- **I1b (make-or-break, PASS):** scanned **all** intraday rate-bursts (age ≥ 300, liq-rank 31–400,
  gain ≥ 8% + hourly turnover ≥ 5× the prior-7d hour, cooldown 3d, fwd 48h) across both venues,
  **including pumps that never became daily events** (bybit ~8,968 / binance ~7,912). Labeled
  forward fade-vs-continue, tested feature separation, then **beta-neutralized** (coin fwd −
  market fwd) to rule out a market-regime bet.
  - **Result:** separation **survives beta-neutralization**, cross-venue + both eras. `idio`
    (pump size vs market) IC_neutral **−0.28…−0.31**; velocity / vol-spike / acceleration
    **−0.11…−0.16**; intrabar wick ≈ noise. It is a **SELECTION on pump-extremity**: the extreme
    quintile is beta-neutral short **+1.2–1.3% (early) / +4.4–4.7% (recent)** gross 48h; shorting
    *all* bursts is ~breakeven. **Conclusion: a genuine idiosyncratic mean-reversion signal.**
  - **Factor honesty:** the signal is **substantially the known short-term-reversal (STR) factor**
    (idio β dominates the multivariate; vol-spike adds a small real increment). Not necessarily
    *unique* alpha.

## 3. The standalone short under realistic risk — I2 / I2c–f

- The deployed daily **12% stop is too tight** for a catch-the-top short — ~38% stop-out, fails,
  recent-only. This *rediscovers why the daily strategy shorts the confirmed fade, not the top.*
- **Fade entries underperform** the top-short at the same stop (% giveback 3–20%, momentum
  down-bars, volume-decline-vs-climax, failed-retest — all confirm ≈ 1.0, no selectivity, and
  stay early-negative). Entry refinement can't fix a *post-entry* re-pump squeeze. "More fade"
  empirically loses; **the lever is stop width**, not entry.
- **Funding-blind**, the extreme-burst **top-short flips all-weather at ~25%** (the operator's cap):
  per-trade net45 positive both venues/eras; Stage-B portfolio **MAR 3.1/2.2**, DD 11–13%.
  *This looked like a deployable candidate — but it ignored funding.*

## 4. Funding — the dominant cost (I2g → I2k)

**How funding is computed** (`i2_burst_backtest.py`, `--funding-ds`): realized per-venue funding
(`funding` / `binance_usdm_funding`), each row a settlement `(ts, symbol, rate)`. Per coin,
cumulative-sum the rates; a trade's funding = `cf(exit) − cf(entry)` = the sum of settlements in
`(entry, exit]` (binary search on settlement times). **Sign (for a short):** `pnl += rate` each
settlement — **positive rate (longs pay shorts) helps; negative rate (crowded short, perp at a
discount) hurts.** It is realized funding applied PIT, not a model.

| step | finding |
|---|---|
| **I2g/I2h** | The funding **mean** (−0.6…−1.5%/trade) first looked like a kill but was **outlier-distorted** by hourly-funding coins (e.g. LRC −16% over 48 hourly settlements). The **median** trade's funding ≈ 0. But the funding-**included PORTFOLIO** (to-48h accounting) was MAR −0.91/−0.73 — dragged by ~11–32% **crowded-short** coins paying >1% funding. |
| **I2i** | A PIT **crowded-short filter** (skip coins with negative trailing funding *at entry*) helped (→ −0.46/−0.15) but **did not rescue** it — funding accrues *during* the hold, un-filterable at entry. |
| **I2j** | **Shorter holds** (12/24/48h) under to-48h accounting were all MAR-negative — cutting the hold cut funding *and* edge. **24h was the least-bad.** |
| **I2k** | **Fair funding (charged to the actual exit, `--funding-to-exit`)** — the to-48h accounting **over-charged stopped trades** (a stopped trade exits early and stops paying, and the ~13% stopped trades are exactly the worst crowded-short coins). Fixing it **reopened the candidate.** |

**The bracket at the best config (24h hold, 25% stop, EXTREME):**

| | funding-BLIND (upper bound) | **FAIR (funding-to-exit)** | pessimistic (funding-to-48h) |
|---|---|---|---|
| bybit | ret +38.7%, MAR 3.08, DD 12.5% | **ret +4.3%, MAR 0.30, DD 14.4%** | MAR −0.54 |
| binance | ret +28.7%, MAR 2.76, DD 10.4% | **ret +5.6%, MAR 0.49, DD 11.5%** | MAR −0.09 |

**Funding eats ~80–89% of the funding-blind edge.** The realistic number is the FAIR column:
positive on both venues, but thin. The 48h hold stays ~breakeven under fair funding (bybit −0.1 /
binance −0.24) → **24h is the right hold** (front-loaded fade, less funding).

## 5. The equity curve reality (24h, fair funding)

The summary MAR hides the shape. Reconstructed from the saved run (validated against the engine's
own portfolio numbers):

- **bybit** — bleeds from 1.0 to a **−13% trough (~2024-03)**, grinds back through 2024–2025, and
  only turns net-positive in the **last ~6 months**. The +4.3% is essentially a **2026 phenomenon**;
  for 2022–2025 this was a losing strategy. (early-third P&L −9.3%.)
- **binance** — more two-sided (up 2022, bleeds 2023→early-2024, climbs 2024-11+ to +13% then back
  to +5.6%). Genuinely more all-weather (thirds +3.2/+0.5/+3.0) but **modest and lumpy**.

**Read:** marginal and recent-weighted. Not a curve you would deploy capital on as-is, especially
on bybit.

## 6. Supporting context (CV1 / RD1)

- **CV1:** the bybit ≫ binance gap is **breadth + universe composition, not a weaker per-trade
  edge** (matched same-coin/day events corr 0.89). The edge is venue-general.
- **RD1:** recent decay is **squeeze-driven** (stop-outs on coins pumping against a weak market);
  the **residual-momentum gate** cuts ~75% of recent stop-outs — which is *why* it works, and why
  the daily strategy's late entry (sidestepping the squeeze **and** the funding crowding) is safe.

## 7. Balanced verdict

**The bull case.** A real, beta-neutral, cross-venue, idiosyncratic mean-reversion signal (I1b);
fair-funding positive on both venues at 24h/25% (MAR +0.30/+0.49); binance reasonably all-weather;
the mechanism is understood (extreme pumps mean-revert; the daily entry is too late to catch it).

**The bear case.** Funding eats ~80–89% of the edge; the survivor is **thin and recent-tilted**
(bybit underwater ~3 years); it was found after **extensive multiple-testing**; it is a **Stage-B
proxy** (P&L booked at entry-day, approximate concurrency, flat 2% stop-slip); 25% is the **stop
boundary** (fragile below) and a rough adverse hold operationally; it is **mostly the STR factor**;
and the verdict **swings with funding accounting** (−0.54 → +0.30 → +3.08), i.e. the proxy cannot
resolve a MAR ≈ 0.3 candidate.

**What is genuinely unresolved:** whether the fair-funding +0.30/+0.49 is real or a proxy artifact.
Only a true engine (exact exit-timing/concurrency, realistic fills, funding-to-exit) can settle it.

## 8. Recommendation + the decision in front of us

1. **Primary recommendation:** treat the **daily age + residual-momentum strategy** as the edge and
   **forward-demo it** (the operator-gated Tier-3 arbiter). It is the robust, validated, all-weather
   result and does not depend on the intraday outcome.
2. **On the intraday short:** it is a *maybe*, not a *deploy*. If the intraday angle is worth pursuing,
   the right next step is **engine-grade I3** — true exit-timing/concurrency + `bar_extreme_capped`
   fills + funding-to-exit + risk_model residual, **24h hold, stop ≤ 25%** — to convert the marginal
   proxy result into a real number. This is an **expensive (~5–7d) build and is operator-gated**; given
   the edge is ~85% funding-eaten and recent-tilted, it is a genuine coin-flip whether it's worth it.
3. **Do not** deploy the intraday short on the proxy result, and **do not** put it on real money.

## 9. What would change the verdict

- **Engine-grade I3** showing the fair-funding positivity holds with true exit-timing/concurrency and
  realistic fills → upgrade from "maybe" toward a demo-candidate (still forward-demo gated).
- **Out-of-sample / forward** confirmation that the edge is not purely the recent regime (the equity
  curve's biggest weakness).
- A genuinely different expression that **avoids paying the crowd** — e.g. the symmetric **long of
  extreme dumps** (oversold bounces), where a short-crowded market pays the *long* funding. This is a
  **new research program**, out of the current liquidity-migration-short scope, and not started without
  an explicit operator direction.

---

*Reproduce:* `python scripts/i2_burst_backtest.py --venue {bybit|binance} --root <root> --stop-pct 0.25
--hold-h 24 --funding-ds {funding|binance_usdm_funding} --funding-to-exit --output-json <out>`.
Funding-blind = drop `--funding-ds`; pessimistic = drop `--funding-to-exit`.
