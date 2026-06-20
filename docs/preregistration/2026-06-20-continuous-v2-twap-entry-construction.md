# Construction + Verdict: Continuous V2 — Event-Driven TWAP/VWAP Entry

Date: 2026-06-20
Author: Claude (operator-directed next-level research push)
Run label: `exploratory`. Operator idea: "why can't we event-driven TWAP/VWAP in so we get a better average price?"
**Verdict: TWAP/VWAP/event-driven entry gets a WORSE average price for this fade — it shorts the immediate reversion at lower prices. Single-shot at the signal is already optimal on PRICE. TWAP's only real benefit (reduced market impact on large clips) is a capacity/cost question, not entry alpha.**

## Thesis tested

The adverse-trade characterization showed high-vol / big-run-up fades run hard against
the short before reverting. Idea: scale the short IN over the first minutes so as the
name keeps popping we short at higher (better) prices, improving the average entry —
especially on the high-vol trades.

## Method

`scripts/continuous_v2_twap_entry.py`, on the 1m path: single-shot vs `twap_K` (equal
slices over K min), `vwap_K` (volume-weighted), `rip_K` (event-driven "short the rip" —
heavier weight when price is above the single-entry). K ∈ {5,15,30,60}. Two measures:
(1) realized short return (flat round-trip cost, same for all methods, stressed 1×/2×/3×);
(2) PURE entry-price improvement = (twap_avg − single)/single, exit-independent — for a
short, >0 means a better (higher) average price. Broken out by pre-entry vol decile.

## Results (2026-06-20, both venues)

PURE entry-price improvement (the decisive, exit-independent measure):

| | bybit | binance |
|--|------:|--------:|
| twap mean | **−0.0012** | **−0.0003** |
| twap % of trades with a better price | **42%** | **45%** |
| rip (event-driven) mean | −0.0002 | +0.0010 |

By pre-entry vol decile, the entry-price improvement is NOT systematically positive for
high vol (bybit d10 −0.0006, binance d10 +0.0015 — ≈0, not the large positive the thesis
predicted; only the noisy d7 is positive on both). Realized-return Δ vs single is
negative almost everywhere (−0.001 to −0.006).

## Verdict — averaging in shorts the reversion at worse prices

- TWAP gets a WORSE average short price ~58% of the time. Mechanism: the fade enters
  AFTER the pop (post-signal, +1h delay) and its edge is the IMMEDIATE reversion — price
  falls right after entry, so spreading the entry over the next 5–60 min shorts at
  progressively LOWER prices. Single-shot captures the top of the pop.
- The high-vol trades do NOT rescue it ex-ante: you cannot distinguish the minority that
  keep ripping (where TWAP would help) from the majority that revert immediately (where
  it hurts). Conditioning on pre-entry vol — the only ex-ante signal — gives ≈0 entry
  improvement in the top decile.
- "Short the rip" (event-driven, weight toward higher prices) is the best variant and
  lifts the average vs plain TWAP, but only to ≈flat — it needs the price to actually rip
  after entry, the minority case.
- This is the same root cause as the closed E1 intrabar entry-timing result: delaying /
  spreading a fade entry gives up the edge.

## The one legitimate TWAP use (not alpha)

TWAP's real-world benefit is reduced MARKET IMPACT on large clips (sqrt-impact: smaller
slices move the price less). This study charged a flat cost equal across methods, so that
benefit is UNMODELED upside. But it is a CAPACITY/COST question (Book C / X3 cost
calibration, gated on demo-fill data) — at the book's current size the entry is small and
single-shot is fine. TWAP does not improve entry ALPHA; on the price path it slightly
harms it. If the book ever scales to where impact dominates, revisit TWAP as an
impact-reduction tool, not an alpha tool.

## Honesty / scope

- Per-trade screen on the 1m path; slices fill at the 1m close (mid-ish), market-taker
  assumption; flat round-trip cost stressed 1×/2×/3×. The realized-return measure has a
  minor exit-window confound (longer windows skip more early TP) — the PURE entry-price
  measure (exit-independent) is the decisive one and is also negative.

## No real-money / promotion claim

`REAL_MONEY` stays false. No entry change to the frozen object.
