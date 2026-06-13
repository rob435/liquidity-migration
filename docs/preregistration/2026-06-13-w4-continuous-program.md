# Pre-registration: Continuous W4 Replacement Program

**Date:** 2026-06-13
**Author:** Codex
**Stage:** active

## Scope

This replaces the owner-erased local W4 work. Deleted W4 plans, receipts,
scripts, and artifacts are not evidence for this program.

The program studies the research-stage continuous book only. It does not approve
paper readiness, promotion, deployment, or real-money trading. Both venues are
mandatory and all root reads use only:

- `~/SHARED_DATA/bybit_full_pit`
- `~/SHARED_DATA/binance_full_pit`

Forward demo/paper remains the only pristine OOS surface.

## Stage Map

### Stage 0 - Data, PIT, and Forward Clock Prerequisites

Receipt: `docs/preregistration/2026-06-13-w4-continuous-stage0-data-clock.md`

Question: are the full-PIT roots, PIT membership partitions, ancillary datasets,
and frozen continuous forward-replay state current enough to run serious
research without fabricating forward evidence?

Artifacts:

- `~/SHARED_DATA/w4_continuous_stage0_data_clock_2026-06-13/`
- `~/SHARED_DATA/continuous_forward_replay/` if the registered forward replay
  maintenance run appends days without drift.

Gate: downstream stages stop if either venue is missing the registered window,
PIT partitions, or forward-replay overlap verification fails.

### Stage 1 - Honest Stop / Exit Realism

Receipt: `docs/preregistration/2026-06-13-w4-continuous-stage1-stop-exit-realism.md`

Question: how much of the frozen continuous ensemble survives when the live
disaster stop and protective exits are modeled explicitly, and how fragile is
that conclusion to adverse stop-fill severity?

Artifacts:

- `~/SHARED_DATA/w4_continuous_stage1_stop_exit_2026-06-13/`
- `~/SHARED_DATA/{bybit,binance}_full_pit/reports/w4_continuous_stage1_stop_exit_2026-06-13/`

Gate: this stage can support only the exact registered stop/protective-exit
mechanism. A failure does not close the whole exit-realism family.

### Stage 2 - Sniper Fill Validity and Adverse Path

Receipt: `docs/preregistration/2026-06-13-w4-continuous-stage2-sniper-fill-validity.md`

Question: for the fixed quarter-size `entry * 1.08` PostOnly sniper add-on, do
historical bars and forward fills support executable resting fills without
unacceptable adverse continuation?

Minimum artifacts: base-entry rows, eligible sniper order rows, fill-validity
rows, adverse path after touch/fill, cancel/expiry rows, per-venue ledgers, and
live-fill reconciliation when forward fills exist.

Gate: no adaptive sniper refits and no fitted wick changes in this stage. If
forward fills remain zero, the stage reports historical fill validity only and
stops at a "not enough forward fills" gate for forward-validity claims.

### Stage 3 - Composite / Path-Shape Information

Receipt: `docs/preregistration/2026-06-13-w4-continuous-stage3-path-shape.md`

Question: do causal path-shape measurements around the existing component
signals add information beyond the frozen composite without becoming another
single-venue sizing tilt?

Minimum artifacts: event rows with registered path features, feature coverage,
cross-venue effect sizes in bps/trade and return units, per-month IC/spread,
component and ensemble ledgers, fragility diagnostics, and negative controls.

Gate: no single-venue claim and no post-result threshold movement.

### Stage 4 - Forward Composite Measurement

Receipt/amendment to be written only after the forward sample gate is met.

Question: do forward demo/paper entries confirm the historical composite/path
diagnostics?

Gate: do not run a verdict before the registered forward sample thresholds are
met. Zero-entry or tiny-entry forward windows are a data/status result, not
evidence for or against alpha.

### Stage 5 - Liquidation / OI / Depth Context

Receipt to be written before execution.

Question: can OI, funding, taker-flow, depth, and forward liquidation context
explain squeeze/adverse-path risk in a way that is measurable on both venues?

Gate: historical liquidation raw data is not symmetric across venues. Any
liquidation-only historical claim is blocked unless a same-quality cross-venue
source exists. Forward liquidation and depth tapes are judged only after enough
forward observations mature.

### Stage 6 - Data Maintenance and Forward-Clock Operations

Receipt/amendment to be written before any operation that touches the full-PIT
working roots beyond read-only audits.

Allowed work: documented full-PIT rebuild/verify scripts, Binance ancillary
top-ups from a permitted host, residual-momentum refreshes, and the frozen
continuous forward replay orchestrator. Results are operational prerequisites,
not alpha.

## Program-Level Falsifiers

- Missing or partial PIT roots on either venue.
- A result that works on only one venue.
- A result whose conclusion depends on lowering a threshold after seeing the
  output.
- A result without reconstructable population, root identity, config/code hash,
  per-venue ledgers or event rows, effect sizes, fragility diagnostics, and a
  written falsifier.
- Any attempt to treat demo execution, signal replay, or historical root runs as
  real-money approval.

## Status

Stage 0 completed on 2026-06-13. It passed Stage 1 only on the amended common
historical window ending `2026-05-01` exclusive; it blocked current forward
claims and historical OI/depth/liquidation verdicts until data matures.

Stage 1 completed on 2026-06-13. The registered stop/protective-exit overlays
were rejected in their exact tested form. That result does not close the whole
exit-realism family.

Stage 2 completed on 2026-06-13. The fixed live sniper form is historically
supported for forward watch only; it remains gated by zero live sniper fills and
does not become paper-ready or promoted evidence.

Stage 3 completed on 2026-06-13. Three causal path-shape measurements
(`pre_6h_return`, `pre_24h_return`, `pre_24h_realized_vol`) are admissible only
for a future neutralized Stage 3b receipt. The high symbol-hash negative-control
spread is a confounding warning, so no live change is authorized.

Later stages require their own dated receipt or amendment before touching the
per-venue roots.
