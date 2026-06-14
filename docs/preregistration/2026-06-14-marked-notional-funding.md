# Pre-registration: Marked-notional funding (SPEC — focused follow-up)

**Date:** 2026-06-14
**Author:** Claude (cost/funding audit follow-up; operator-approved 2026-06-14)
**Stage:** proposed — **SPEC ONLY, NOT YET IMPLEMENTED**
**Finding:** `cost-funding-4`
**Status note:** This receipt is **pre-registered but pending implementation**. No
code change exists in the working tree for it yet, and no confirming run has been
performed. It is registered now (ahead of the change) because it will alter a
shared per-venue backtest number for both promotion engines, and AGENTS.md
requires the receipt to land in the same PR as the code change. The change is
recommended as a focused, carefully-tested follow-up — *not* folded into any
broader edit — precisely because it touches the shared funding term feeding
`net_return` on both sleeves.

## What's changing

Weight each in-window funding settlement's rate by the position's **marked**
notional (`close[settlement_ts] / entry_price`) instead of the current flat
**entry-notional** weight, inside the shared
`liquidity_migration/trade_lifecycle._perp_funding_return`, and wire **both**
callers (long-native `_finalize_trade` and the continuous `_simulate_indexed_trade`
path) consistently.

## Exact files / knobs touched

- `liquidity_migration/trade_lifecycle.py`
  - `_perp_funding_return` (def at line 488). Today it computes the in-window
    settlement sum as:

    ```python
    signed = sum(series["events_rate"][lo:hi])          # line 512
    return (float(-signed) if side == "long" else float(signed)), mode, hi - lo
    ```

    i.e. every in-window settlement rate carries an implicit flat entry-notional
    weight (the caller later multiplies the whole sum by one constant
    `effective_weight`). The marked version replaces the flat sum with a
    per-settlement marked sum:

    ```python
    signed = sum(rate_i * close_at(ts_i) / entry_price for each settlement ts_i in [lo:hi])
    ```

  - **New optional parameters** on `_perp_funding_return`: an optional
    settlement-price lookup (`close_at(ts)`) **and** `entry_price`, both defaulting
    to `None`. When either is absent the function falls back to the current
    `sum(events_rate[lo:hi])` flat-weight behavior **byte-for-byte** — this is the
    equivalence anchor (see Decision rule).
- `liquidity_migration/long_native.py`
  - `_finalize_trade` (def at line 2221), which calls `_perp_funding_return` at
    line 2225. It already holds `entry_price` (line 2223) and the price path is
    available via `bars_by_symbol` at the call site (`bars` resolved at line 2195;
    `exit_price` from `bars["close"][exit_idx]` at line 2205). Thread the
    per-symbol close lookup + `entry_price` through.
- `liquidity_migration/trade_lifecycle.py` continuous path
  - `_simulate_indexed_trade` (def at line 689), which calls `_perp_funding_return`
    at line 844. It already holds the indexed `close_arr` (line 714) and
    `entry_price = float(close_arr[entry_bar])` (line 716). Thread the same
    close lookup + `entry_price` through.
- The standing `cost-funding-4` comment block at `trade_lifecycle.py:852-861`
  (which documents exactly this approximation as a deliberate, flagged
  simplification) is updated/removed when the change lands.

No data builders, no per-venue dataset *writes* (reads only), no live order path.

## Hypothesis

Real perp funding is `rate * position_notional` settled at each funding stamp,
and the position's notional marks with price over the hold. The current flat
entry-notional weight therefore **under- or over-charges** funding as the position
marks away from its entry over the hold: a position that has moved in-the-money
is settling funding on a larger notional than entry, and vice versa. The flat
weight is exact only at `t = entry` and drifts with price thereafter. The error is
not a sign bug — it is a fidelity gap that scales with how far and how long the
mark drifts from entry.

## Predicted direction + magnitude

- This is a **second-order** correction to the funding term, hence to
  `net_return`, on **both** sleeves. The funding term itself is small relative to
  gross + cost, so the headline (MAR / total return) shift is expected to be
  small.
- Effect **scales with hold length and price drift** over the hold: negligible for
  short holds with little drift, larger for long holds in trending names.
- Direction is **not signed a priori** at the headline level — marked weighting can
  raise or lower the funding charge per trade depending on the mark path and side;
  the *mechanism* is the fix, not a predicted P&L improvement.
- **Promotion-relevant:** because it moves `net_return` on both promotion engines,
  it can move a Tier-1/Tier-2 MAR-delta cell. It must not be assumed to be in the
  noise without the confirming both-venue backtest below.
- **Failure mode if hypothesis wrong / falsifier:** if the both-venue confirming
  run shows the funding-term and headline shift is *not* small, or moves a
  promotion verdict in a direction the marked-notional mechanism does not explain
  (e.g. a large MAR jump uncorrelated with hold length / drift), the change is
  rejected and re-examined — that pattern would indicate a wiring/units bug, not a
  fidelity gain.

## Roots that will be touched

- [x] bybit_full_pit (per-venue working dataset) — **read-only** confirming
  backtest; writes go to a `reports/<tag>/` subdir, not the dataset.
- [x] binance_full_pit (per-venue working dataset) — same, cross-venue by default.
- [ ] forward demo/paper — untouched; no orders, no live path change.

## Decision rule (a priori)

**Equivalence anchor (mandatory):** with the new price-lookup / `entry_price`
arguments **absent (default `None`)**, `_perp_funding_return` must reproduce
today's `sum(events_rate[lo:hi])` output **exactly** for both sleeves — bit-for-bit
on the default path. This guarantees the change is purely additive and that the
marked weighting is opt-in.

**Acceptance:** accept the marked-notional weighting only if a focused PR carries
**(a)** a unit test that pins the marked per-settlement weighting against a hand-
computed `rate_i * close(ts_i)/entry_price` fixture (and that pins the default-None
path to the current flat behavior), **and (b)** a confirmatory both-venue backtest
(bybit + binance full PIT) on both sleeves showing the funding-term delta and the
headline (MAR-primary, Sharpe-secondary, per STATE.md Decision Rules) shift is
**small and as-expected** — i.e. correlated with hold length and price drift, same
sign-of-mechanism on both venues.

**Reject** if it materially changes a promotion verdict (a Tier-1 / Tier-2 MAR-delta
cell flips, or a candidate's standing moves) **in an unexplained way** — i.e. a
shift not accounted for by the marked-notional mechanism, or a cross-venue sign
flip in the funding-term correction. Per STATE.md, Tier-3 is not loosened to rescue
any result, and the cross-venue rule (a sign flip between venues is informative and
usually flags a venue-specific artifact) applies to the funding-term delta itself.

## Run command

```bash
# SPEC — not yet runnable; no implementation exists.
# When implemented, the confirming run is the standard both-venue equity backtest
# on both sleeves over the full-PIT roots, comparing the marked-notional build
# against the entry-notional baseline (default-None path) at the SAME commit:
#
#   bash scripts/equity_curves.sh            # promoted LONG v11a, both venues
#   .venv/bin/python -m pytest -q tests/...  # the new marked-weighting unit test
#
# plus a both-venue continuous diagnostic backtest over the same window, with the
# funding-term delta and per-trade hold-length / drift attribution reported into a
# reports/<tag>/ subdir of each per-venue root. Exact CLI to be pinned in the
# implementing PR's run-pending update to this receipt.
```

## Post-run results

(none — SPEC only; not yet implemented and not yet run)

## Verdict

**Pending — pre-registered, implementation deferred.** Recommended as a focused,
carefully-tested follow-up PR (shared-function change feeding `net_return` on both
the long-native `_finalize_trade` and continuous `_simulate_indexed_trade`
engines). Operator-approved on 2026-06-14 as a flagged audit item. To be filled in
when the implementing PR lands with the equivalence-anchored default path, the
marked-weighting unit test, and the confirmatory both-venue backtest.
