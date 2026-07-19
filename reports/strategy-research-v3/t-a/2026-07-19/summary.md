# T-A — BTC uptrend-gate ablation (exploratory, Lane 1)

**Status: EXPLORATORY.** Paired research renders of the standard CONTINUOUS
chain; no promotion claim, no profile change. The live profile changes only
through the normal post-epoch deploy flow.

## What ran

- Added a guarded, default-off `--research-disable-btc-gate` flag to the
  standard chain (`scripts/equity_curves.py` →
  `scripts/continuous_deployed_equity_refresh.py`), threaded to
  `ContinuousEventConfig.btc_trend_gate="off"` in the research render only.
  Guards: the flag requires an isolated output root and refuses the
  operational report root; runtime demo/paper producers and the hedge service
  never see it (they own separate gate configuration).
- Two renders on the full-PIT bybit root over the full active history
  (2023-04-05 → 2026-07-09, end 2026-07-10 exclusive): baseline (gate on) and
  ablation (gate off). Identical cost model, identical components, identical
  hedge rule. `residual_momentum.parquet` was rebuilt first with the canonical
  builder (the existing table predated the `is_provisional` schema).
- Paired diff on the union daily calendar (the gate-on arm is flat far more
  often; absent days are flat). Tail arm: the V2 156 common-loss dates
  (rederived from the frozen V2 curve and count-verified) intersected with the
  render window (93 days), plus 2024-08-06 explicitly. Era split at the
  calendar midpoint (2024-11-22).
- Note: the render window includes `[2025-01-01, 2026-07-06)` at the
  equity-render level only, which deployed-profile renders had already
  covered; label-level holdout data stays unread.

## Results (1x modeled; 4x charts are presentation-only leverage)

| Arm | Total | Ann. | MaxDD | Worst day | Entries | Per-entry net |
|---|---:|---:|---:|---:|---:|---:|
| Gate on | **+22.93%** | +6.52% | **−1.30%** | −0.93% | 2,300 | **1.00 bps** |
| Gate off | +21.94% | +6.26% | −6.35% | −1.88% | 4,019 | 0.55 bps |

- **Sample cost of the gate:** it blocks ~43% of entries (4,019 → 2,300). The
  blocked entries were not net-positive: removing the gate LOWERS total return
  by ~1.0pp while doubling entry count — gated-off entries are net-negative
  after costs at the portfolio level, contradicting the thesis premise.
- **Tail arm:** on the 93 in-window common-loss dates the gate-off arm has
  nearly twice the negative days (28 vs 15) with a worse worst tail day
  (−0.46% vs −0.25%); tail sums are small and positive for both (hedged book).
  On 2024-08-06 specifically both arms are mildly negative (baseline −0.081%,
  gate-off −0.047%) — that single named date does not separate the arms.
- **Era split (the honest wrinkle):** early era (→2024-11-22) favors removal
  on return (+10.59% vs +8.52%, maxDD −2.34% vs −1.30%); late era decisively
  favors the gate (+12.95% vs +10.08%, maxDD −1.20% vs −6.35%, worst day
  −0.66% vs −1.88%, worst gate-off day 2025-04-20). The gate's protection is
  concentrated in the recent era — the one closest to current conditions.

## Read

Under the draft's own decision rule — "removal must not win on mean while
losing on the tail" — **gate-off fails both arms pooled**: it loses on mean
(−1.0pp) and loses the tail on frequency. The thesis that the gate does not
pay for its sample cost is refuted on this render. The early-era return
advantage of removal is real and worth remembering if regime conditions
change, but it comes with strictly worse tail behavior in both eras.

## Limitations

- Descriptive historical equity renders, not runtime parity; funding coverage
  is `partial` for some component symbols in both arms identically.
- Windows research runs used a research-only compatibility shim
  (`scripts/research_v3/run_with_stub.py`): no-op POSIX file locks and
  non-crash-durable account writes, single-process only — the same boundary
  the V2 phase-3 portable account IO declared.
- Era split is a single midpoint cut; entries are not era-attributable from
  the component reports (full-period counts only).
- No capacity, sizing, or cross-sleeve interaction modeled beyond what the
  standard render already does.

## Next action

No prototype advances: gate-off does not survive the declared two-arm test.
If the early-era pattern motivates anything, it is a separately declared,
regime-conditional design — not removal of the gate.

Artifacts: `ta_paired_diff.csv`, `manifest.json` (render hashes, flag, tail
definition, era midpoint); render outputs under `render-baseline/` and
`render-gate-off/` (local; equity CSV hashes in the manifest).
