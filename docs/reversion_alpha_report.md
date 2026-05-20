# Ground-Up Rebuild — Cross-Sectional Reversion Alpha

**Date:** 2026-05-20
**Status:** Research. Did NOT produce a deployable strategy. Value is diagnostic:
an honest, fit-free measurement of the true edge.

## What was asked

Rebuild the liquidity-migration short from the ground up as a cleanly separated
quant stack, rather than the v1 stack of ~10 hand-tuned, correlated gates.

## What was built

`liquidity_migration/reversion_alpha.py` — three cleanly separated layers
(15 passing simulator tests in `tests/test_liquidity_migration_reversion_alpha.py`):

1. **Alpha** — one `reversion_score` per (coin, day): the equal-weight mean of
   cross-sectional z-scores of four economically-motivated sub-signals
   (idiosyncratic 1d return, turnover abnormality, close-location, 7d rank jump).
   Equal-weight => no fitted coefficients => cannot overfit. Cross-sectional =>
   universe-size invariant (no absolute thresholds like v1's `rank_imp>=150`).
2. **Portfolio construction** — select the most extended names, size continuously
   by score, scale total gross continuously by the alt-regime.
3. **Execution** — capacity-aware hourly simulator: entry delay, conservative
   intrabar stop/TP/max-hold scan, round-trip cost, daily mark-to-market.

This is the right architecture. The honest problem is what it revealed.

## What it measured — the true edge is weak

Information coefficient (per-day cross-sectional Spearman of each signal vs the
3-day-forward short return), measured across three windows:

| Sub-signal | IS-train | Bybit OOS | Binance OOS |
|---|---:|---:|---:|
| z_rank_jump | +0.026 | +0.052 | +0.041 |
| z_residual_return | +0.029 | +0.034 | +0.026 |
| z_turnover_ratio | +0.014 | +0.030 | +0.019 |
| z_close_location | +0.001 | +0.004 | +0.003 |

Consistent across windows: `rank_jump` is the strongest signal, `residual_return`
real, `turnover_ratio` moderate, `close_location` **dead everywhere** (a third
independent confirmation, after the gate ablation and leave-one-out).

But the strongest single signal is **IC ≈ 0.05** and the composite ≈ 0.02-0.04
on Bybit — a *weak* signal. (The composite IC printed +0.176 on Binance; that is
~4x its best component, which is mathematically impossible for an equal-weight
mean — a bug in the throwaway IC diagnostic script. Flagged, excluded, not used.
The per-component ICs are consistent and trustworthy.)

## What it returned — mixed, ~50% win rates, fails Binance

v0 stack (4-signal composite, top decile, regime scaler, 28.8bps round trip):

| Window | Trades | Return | Max DD | Sharpe | Win rate |
|---|---:|---:|---:|---:|---:|
| IS-train Bybit 2023‑09→2024‑09 | 661 | −28.5% | −41.5% | −0.76 | 50.4% |
| IS-valid Bybit 2024‑09→2026‑05 | 1154 | +115.4% | −25.4% | +1.37 | 51.1% |
| OOS Bybit 2022‑04→2023‑05 | 681 | +33.7% | −41.2% | +0.76 | 52.6% |
| OOS Binance 2020‑09→2023‑05 | 1606 | −77.5% | −87.0% | −0.54 | 44.7% |

Dropping the dead `close_location` component (a legitimate train-window decision)
made every window *worse* — a component with ~0 forward-return IC still
contributes through interaction (likely affecting stop-out paths). IC measured
against the 3d close return does not fully capture strategy P&L.

## The honest verdict

1. **The rebuild did not beat v1 or v2.** Win rates sit at ~50% (v1 was 67%, v2
   ~57%). It fails Binance OOS badly (−77%).
2. **The true edge is weak: IC ≈ 0.05 for the best signal.** The cross-sectional
   top-vs-bottom spread is ~0.3% per 3 days *gross*. Round-trip cost is 28.8 bps.
   **Cost eats almost the entire edge** — which is exactly why win rates are ~50%
   and per-trade net is near zero.
3. **v1's +2022% was epoch-fitting, confirmed once more.** A clean, fit-free
   measurement of the same idea on the same data produces a weak signal. The big
   historical number came from the gate stack matching one favorable epoch.
4. **The iteration was stopped deliberately.** Tweaking signal subsets and
   re-selecting across the four windows would overfit to the validation set by
   hand — the exact error this whole effort exists to avoid.

## What would actually make this viable (structural, not tweaks)

The signal is real but small. A ~0.05-IC, ~0.3%/3d edge needs one of:

1. **Lower execution cost.** 28.8 bps round-trip is the binding constraint. The
   cost model already assumes a 60% maker fill probability; pushing real
   execution toward maker/passive fills (maker fee 2bps) roughly halves cost and
   turns a break-even signal net-positive. This is the highest-leverage change.
2. **Longer holding horizon.** At 3 days the fixed cost is large relative to the
   gross edge. A 5-10 day reversion horizon would raise gross-edge-per-trade
   against the same round-trip cost. Needs its own IC-vs-horizon study.
3. **Hard regime concentration.** v2's hard regime *gate* (only trade alt-bear)
   beat this rebuild's soft continuous regime *scaler* — the scaler still traded
   the 2021 alt bull and got the −77% on Binance. A hard gate is the right call.
4. **Better features.** `rank_jump` (IC ~0.05) is the best of the four. Finding
   features with materially higher IC — not re-combining these four — is the only
   path to a strong strategy. This is open-ended alpha research.

## Recommendation

`reversion_alpha.py` is a clean, tested foundation — keep it as the research
harness. But do **not** expect a v1-like strategy from this feature set: the
honest edge is IC ≈ 0.05, roughly break-even after realistic cost. The next real
work is (1) an execution-cost study and (2) an IC-vs-horizon study — both
structural. Until then, the forward-demo of v1 and v2 (per the v2 report) remains
the live path; this rebuild's contribution is knowing, honestly, how weak the
underlying edge is so nobody sizes off the +2022% mirage.

## Files

- `liquidity_migration/reversion_alpha.py` — the three-layer stack
- `tests/test_liquidity_migration_reversion_alpha.py` — 15 simulator tests
- Per-window ledgers: `reports/reversion_alpha_*` under each data root
