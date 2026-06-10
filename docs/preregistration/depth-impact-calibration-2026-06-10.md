# Pre-registration: DC1 — depth-based impact calibration (bookdepth layer, first use)

**Date:** 2026-06-10 (registered BEFORE computation). **Label:** measurement /
methodology-debt closure — NO alpha claims, no promotion bars. **Authority:** the
standing blanket directive; this serves the documented R4 impact-calibration debt
(blocked on VPS fills) with data instead of waiting.

## Question

Are the cost model's impact assumptions (research configs: ~50 bps impact at $1M
deploy; trust-region capacity frontier ~$5M → pooled MAR ~3.8) CONSERVATIVE or
OPTIMISTIC against measured order-book depth on the names the books actually
trade, at the hours they trade them?

## Data

`binance_full_pit/binance_usdm_bookdepth_1h` — 686 symbols, hourly, 2023-01 →
2026-05, cumulative notional within ±{0.2,1,2,3,4,5}% of mid (bid negative bands,
ask positive). Binance-only (Bybit has no depth history) — binance is the
deeper-book venue, so measured impact there LOWER-bounds nothing; stated as the
venue caveat up front.

## Protocol (fixed)

1. Books: the deployed gated SHORT profile's binance trade ledger (regenerated
   tonight from `promoted.py` via the canonical driver) and the accepted LONG
   baseline binance ledger (tonight's `long_regularity` run).
2. Per trade entry: the entry-side depth at the entry hour — SHORT entries consume
   BID notional within 1% (selling); LONG entries consume ASK within 1%. Exit side
   reported as a secondary column (the cover/sell side).
3. Outputs per book: median / p25 / p10 of $-notional-to-move-1% across entries;
   implied participation and impact-bps at the modeled per-trade sizes (SHORT:
   C/12 per trade at C=$1M; LONG: gross/10), using linear interpolation within the
   1% band (declared simplification); the deploy size C at which median and p10
   implied impact reach 50 bps; comparison against the cost model's assumption and
   the capacity receipt's frontier.
4. No tuning, no selection, no strategy change. The deliverable is a table +
   one-page read recorded in research_summary; any cost-model change it motivates
   gets its own future receipt.

## Artifacts

`C:/Users/user/SHARED_DATA/depth_impact_calibration_2026-06-10/` — report JSON.
Script: `scripts/depth_impact_calibration.py`.

## Read-out (filled in after the run) — measurement complete; the books invert

Join quality: 91.4% (short) / 98.5% (long) of entries matched to entry-hour depth.

| | deployed SHORT (binance) | accepted LONG (binance) |
|---|---|---|
| $-to-move-1% at entry, median / p10 | **$98k / $23k** | **$2.51M / $750k** |
| implied impact @ modeled per-trade size | **85 bps median, 366 p90** | 4.0 bps median, 13.3 p90 |
| deploy C at 50 bps median impact | **~$588k** | ~$12.5M |
| C at 50 bps on p10 names | ~$137k | ~$3.75M |

**Reads (recorded; any cost-model change needs its own receipt):**
1. For INSTANTANEOUS taker execution, the short book's ~50bps-at-$1M impact
   assumption is OPTIMISTIC on binance — the deeper venue — and bybit (thinner
   books, no depth history) is presumably worse. The turnover-based capacity
   receipt ($4.3M median @1% daily participation) measures patient flow; depth
   measures the instant book. Both now exist: taker-style capacity ~$0.5M,
   patient-style single-digit $M — execution style IS the capacity decision.
2. At personal scale (≤$100k book) the short's median implied impact is ~8.5 bps —
   comfortably inside the modeled costs; the 2×/3× stress margins are doing real
   work exactly where they should.
3. The LONG book is the structurally scalable product (depth capacity ~10–20×
   the short's); composition/scaling decisions should weight that.
4. This partially closes the R4-class impact debt with market data; the remaining
   R4 piece (realized fills vs model) still needs VPS fill accrual.
