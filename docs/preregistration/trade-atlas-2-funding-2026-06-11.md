# Pre-registration: TA2 — funding & cross-book entry context (atlas follow-on)

**Date:** 2026-06-11 (registered BEFORE computation). **Label:** `exploratory`
hypothesis-generator; application path identical to TA1 (forward/OOS only, never a
spent-window promotion). **Multiple-testing posture stated:** this is the SECOND
atlas on the same 952-trade dataset; the menu is 3 features (vs TA1's 11), the
survival bar is unchanged (ALL-ledger direction consistency + ≥1 CI excluding zero
+ mechanism), and the compounding window-erosion debt is acknowledged — TA2 is the
LAST atlas on this window regardless of outcome.

## Fixed menu (3 features, all causal at entry)

1. **`funding_at_entry`** — the name's last settled funding rate strictly before
   entry (8h-rate; both venues' funding datasets). Mechanism: funding is the price
   of crowding; a deeply negative rate at SHORT entry = the short side is already
   crowded (squeeze fuel + carry cost); a positive rate at LONG entry = paying to
   hold the FOMO name.
2. **`funding_pctile_30d`** — that rate's percentile within the name's own
   trailing 30d funding distribution (separates "this name is always negative"
   from "unusually crowded right now").
3. **`otherbook_recent`** — the OTHER deployed book traded this symbol within the
   prior 30 days (long candidate recently faded by the short book, or vice
   versa). Mechanism: a name that just completed a fade cycle may behave
   differently as a FOMO long, and vice versa.

## Protocol

Identical to TA1: per-ledger winners-vs-losers (continuous: median diff +
bootstrap 90% CI, seed 20260611; categorical: bucket win rates). Survivors → an
armed forward-watch entry appended to the TA1 receipt's observable list (same
graduation bar: ≥100 forward trades/book, matching direction, pooled ≥2σ).

## Artifacts

`C:/Users/user/SHARED_DATA/trade_atlas_2026-06-11/` (extends TA1's dir:
`atlas2.parquet`, `report2.json`). Script: `scripts/trade_atlas2.py`.

## Findings (filled in after the run) — all three features NULL; one structural discovery

- `funding_at_entry`: W−L median diff ≈ 0 in ALL four books (CIs within ±4e-5
  8h-rate). The crowding price does not separate winners from losers at entry.
- `funding_pctile_30d`: sign-flips across books (−0.08/+0.41/+0.02/+0.01), every
  CI spans zero. Null.
- `otherbook_recent`: DEGENERATE — **zero same-name overlaps within 30d across
  all 952 trades**. The deployed SHORT and accepted LONG books are NAME-DISJOINT
  at the monthly scale over the whole window: the corr ≈ −0.03 diversification is
  STRUCTURAL (different names at different times), not a statistical accident.
  Recorded as a combined-book thesis upgrade; not a tradeable feature.

**Per the pre-registration: this was the LAST atlas on the 2023-04→2026-05
window.** Cumulative atlas harvest (TA1+TA2, 14 pre-registered features): two
forward-armed leads (repeat-name penalty, weekend bonus — TA1 receipt), one
causality catch (entry-day market return), one structural discovery
(name-disjoint books), all else null. The window's trade-outcome space is
receipted closed.
