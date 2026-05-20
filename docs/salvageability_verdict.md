# Liquidity-Migration Short — Salvageability Verdict

**Date:** 2026-05-20
**Status:** Final. Executes `docs/research_plan.md` end-to-end (WS-0 → WS-5).
**Audit trail:** `research/RESEARCH_LOG.md` (every test pre-registered before it ran).

---

## Verdict

**Kill it. Do not deploy the liquidity-migration short — not as a standalone
strategy, not as a sized overlay. Shelve it.**

This is not a hedge and not a maybe. After rebuilding the data, fixing the
harness, and running the full plan, the strategy fails the plan's own §6
viability criteria on **four independent, pre-registered axes**, and — more
importantly — there is now a clean *mechanical* explanation for *why* it fails,
why it has resisted every prior tuning effort, and why no further tuning will
save it. The honest expected edge, net of realistic cost, is **≈ zero with a
catastrophic left tail**. A correct "do not trade this" is the result.

The work was not wasted: it produced three real bug fixes, a validated
measurement tool, and — the genuine prize — an understanding of the structural
reason this idea cannot be made into a strategy.

---

## What was done

`~/SHARED_DATA` was empty; all three PIT roots were rebuilt from scratch
(reproducibly — `scripts/build_oos_roots.sh` for OOS; the IS root via the
documented archive-API path). All four backtest windows reproduce the numbers
in `docs/reversion_alpha_report.md` (WS-0c gate **passed** — IS-train −28.5%
exact, OOS windows exact, IS-valid +111% vs +115% within rebuild tolerance).

Three bugs were found and two fixed (`research/RESEARCH_LOG.md` WS-0a/0d):
- **`storage.py` stale-lock recovery** — `os.kill` winerror 87 not detected on
  Windows; fixed + regression-tested.
- **`reversion_alpha.simulate` entry-lag** — entered ~25h after the signal, not
  the documented ~1h (a date-convention mismatch); fixed + tested. Legacy
  behaviour is still exactly reproducible (`entry_delay_hours=24`).
- **`exclusive_file_lock` is not concurrency-safe on Windows** — diagnosed,
  worked around (single-process builds); left unfixed (load-bearing infra).

A proper IC diagnostic was built (`liquidity_migration/ic_diagnostic.py`, 11
tests) — the reported impossible "+0.176 composite IC" was a pooling bug; the
honest composite IC is ~0.02.

All of WS-1 (cost), WS-2 (IC × lag × horizon), WS-3 (regime gate), WS-4
(feature search), WS-5 (capacity) ran on all four windows. 350 repo tests green.

---

## The evidence

### Four-window backtest (corrected harness, entry_delay=1h, 28.8 bps)

| window | total return | under adverse-fill stress |
|---|--:|--:|
| IS-train (Bybit 2023-09→2024-09) | −29% | −70% |
| IS-valid (Bybit 2024-09→2026-05) | −16% | −93% |
| OOS-1 (Bybit 2022-04→2023-05) | +245% | **+3.5%** |
| OOS-2 (Binance 2020-09→2023-05) | −46% | −94% |

The pre-fix (legacy) harness gives a *different* split — IS-train −28.5%,
IS-valid **+111%**, Bybit OOS +34%, Binance OOS −77%. **Neither** entry choice
is positive on all four windows, or even on both OOS windows + IS-valid.

### Plan §6 viability criteria — every one fails

| criterion | result |
|---|---|
| Positive on both OOS windows AND IS-validation | **FAIL** — no configuration achieves it |
| Positive at realistically-achievable cost | **FAIL** — IS-train & Binance OOS negative even at an unachievable 10 bps |
| Still positive under adverse-fill stress | **FAIL** — −70% to −94% on three windows; +3.5% on the fourth |
| Composite alpha IC stable and ≥ 0.08 | **FAIL** — composite IC ~0.02; best single signal ~0.04–0.09 |

Four independent, pre-registered failures.

---

## The core finding — *why* it fails (IC ≠ P&L)

This is the night's real contribution and it explains everything.

The signal has **genuine cross-sectional rank skill**. On IS-train, sorting the
universe by the strongest feature (`signal_day_range_pct`, the signal-day
intraday range) into deciles, the highest-range decile — the names the strategy
shorts — has a forward-return **median of +3.0%** and a **57% win rate** (vs
+0.07% / 50% for the lowest decile), rising monotonically. That rank skill is
real and is exactly what the information coefficient (~0.04–0.09) measures.

**But a short strategy earns the mean, not the median or the win rate — and
the mean is destroyed by a catastrophic left tail.** The same highest-range
decile has a 5th-percentile forward return of **−47.6%**: roughly 1 trade in 20
is an "exhausted pump" that kept running and cost the short ~half its capital.
Those tail events outweigh the +3% median, so the **mean** forward return of
the shorted decile is **−1.6%** — *worse* than shorting the lowest-range decile.

Information coefficient is rank-based and **cannot see the tail**. It
systematically overstates tradeability. The mean-based decile spread — what a
strategy actually earns — is the honest measure, and net of cost it is **≈ 0 to
negative** on the unfriendly epochs.

This single mechanism explains the entire history of this strategy:

1. **Epoch dependence is mechanical, not mysterious.** In an alt-bear epoch
   pumps revert, the left tail is benign, the mean is positive (Bybit OOS,
   IS-valid-legacy look spectacular). In an alt-bull epoch pumps continue, the
   left tail is fatal, the mean is negative (Binance OOS, IS-train). v2 proved
   the epoch is not a tradeable signal — this is *why*: it is the short-side
   fat-tail asymmetry flipping the mean's sign.
2. **Adverse-fill fragility is the same problem.** The −47% tail events are
   pumps continuing; a stop is meant to cap them, but on a high-volatility name
   the stop fills far through (bar_extreme: −70% to −94%). The fat tail and the
   adverse-fill catastrophe are one phenomenon.
3. **Every "improvement" raises IC but not P&L.** A fresher entry and the
   stronger `signal_day_range_pct` feature both *increase* rank skill (IC) — and
   both *increase* exposure to the first-24h continuation tail. The entry-lag
   fix is +211 pp on Bybit OOS and **−127 pp on IS-valid** for exactly this
   reason: fresher entry walks straight into the danger window the 25h lag
   accidentally skipped.
4. **It is why v1 over-fit.** v1's +2022% was its gate stack accidentally
   selecting a tail-benign sub-period. The headline +245% in this rebuild is the
   identical thing — ~70% of it is the May–June 2022 alt crash (alt-beta in a
   bear), and it collapses to +3.5% under adverse fills.

You cannot tune your way out of this. It is the structural nature of shorting
volume-pumped alts: rank skill is real, the short-side mean is tail-killed, and
the tail's sign tracks an untradeable regime.

---

## Why each lever failed

- **WS-1 cost.** Cost is not the binding lever. Binance OOS and IS-train are
  negative even at an unachievable 10 bps round-trip. Bybit OOS has huge cost
  headroom — but that is the friendly epoch, where the tail is benign.
- **WS-2 horizon / entry-lag.** A 10–14 day hold beats the inherited 3-day hold
  on the cost/edge ratio (a fixed cost amortised over a bigger move) — confirmed
  — but even the best horizon leaves the unfriendly epochs negative. Entry-lag
  is a Pareto knob, not a free lever (see above).
- **WS-3 regime gate.** The hard bear-only gate (v2's a-priori −0.05 threshold)
  helps the bull-ish windows (IS-train −29%→−17%, Binance OOS −46%→−26%) but
  *hurts* IS-valid (−16%→−34%). It is itself a window-trading knob, and v2
  already proved the regime is not reliably tradeable.
- **WS-4 features.** The strongest feature found, `signal_day_range_pct`
  (IC ~0.07–0.12, a-priori "blow-off exhaustion" rationale), is genuinely
  better than the current 4-feature composite — but it is *still* tail-killed:
  its market-neutral decile spread is positive on the bear epochs and negative
  on IS-train, and the IC≠P&L gap is widest exactly for this feature because it
  selects the highest-volatility names. The plan's marquee features — funding
  rate, open interest, taker imbalance — could not be tested (the rebuilt roots
  hold klines only); this is the one genuine untested card (see below).
- **WS-5 capacity.** Not the binding constraint. Traded names are liquid
  (median signal-day volume **$106M**); square-root impact at $1M AUM is only
  ~0.2–0.4%/trade. Capacity is N/A because the *edge*, not liquidity, is
  missing — on the unfriendly windows the gross edge is ≤ 0.

---

## What is real, and what to keep

- **The bug fixes** (entry-lag, stale-lock) and the **IC diagnostic tool** are
  genuine repo improvements — keep them regardless of this verdict.
- **The honest knowledge:** the liquidity-migration signal is a real
  cross-sectional *rank* signal (IC ~0.04–0.09) that does **not** survive as
  net *mean* P&L on the short side because of the continuation tail. Nobody
  should ever again size off a headline backtest number for this idea.
- **`reversion_alpha.py`** remains a clean, tested research harness.

## Recommendation

1. **Do not deploy the liquidity-migration short.** Not standalone, not as a
   "tiny diversifying overlay" — a ≈-break-even strategy with a −47% left tail
   is not diversification, it is hidden tail risk that will surface in the next
   alt-bull.
2. **Stop tuning it.** The failure is structural (short-side continuation
   tail), not parametric. Cost, horizon, regime gate, entry-lag, and a stronger
   feature were all tested; each trades one window for another on a Pareto
   frontier with no interior win. More knobs will only manufacture another
   epoch-fit number.
3. **The one honest follow-up, with low expectations.** The fat tail is a
   *short-side* problem. An expression without an uncapped short-side tail —
   genuinely market-neutral (decile spread), or options-defined-risk — could in
   principle harvest the rank skill. But the measured market-neutral edge is
   thin (~+0.3%/trade where positive) and *still* epoch-dependent in sign, so
   the expected value of that follow-up is low. It is worth pursuing **only**
   if a materially stronger, tail-aware signal is found first. Funding rate
   (untested here for lack of data) is the single remaining candidate worth one
   focused look — but the prior is weak: a higher IC does not help unless it
   specifically predicts *which* pumps continue, and nothing in the four
   economically-motivated features tested does.
4. **Forward demo:** the existing v1/v2 demo stack may continue as observation
   only, with the now-firmly-established understanding that the honest forward
   expectation is ≈ break-even with fat left tails — not the headline numbers.

## Honest uncertainties

- **`bar_extreme` adverse-fill is the pessimistic bound** (every stop fills at
  the bar high). Reality lies between it and the optimistic `stop` model. The
  point is not the exact figure — it is that the result swings from +245% to
  +3.5% on Bybit OOS depending on a fill assumption, and a strategy that
  un-sizeable is not deployable. The verdict does not rest on `bar_extreme`
  alone; it rests on the cost grid, the IC≠P&L finding, and the four-window
  failure, each independently sufficient.
- **Funding / open-interest / taker-imbalance features are untested** — a real
  gap, owned. A focused follow-up could acquire that data. The prior that it
  changes the verdict is low, for the structural reason in §Recommendation 3.
- **The cross-sectional rank skill is real.** This verdict is "the short-only
  strategy is not viable and cannot be tuned into viability," not "there is no
  signal." The signal exists; it is mis-packaged and tail-dominated.
