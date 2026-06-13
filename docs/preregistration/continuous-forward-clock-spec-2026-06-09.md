# Active Receipt: Continuous Forward Clock

**Status:** active methodology/deployment gap.

## Current Gap

The live demo book collects execution evidence, but the signal-forward clock for
the frozen four-component ensemble is incomplete until an orchestrator reruns the
four frozen component configs and feeds `continuous_forward_replay`.

Correct API sequence:

1. Build a full component ledger for each frozen component.
2. `build_forward_ledger(...)`
3. `update_forward_ledger(state_dir, venue, full_ledger)`
4. `forward_readiness_summary(state_dir, venue, forward_start_ms=...)`

The missing piece is the runner/orchestrator, not another decision rule.

## Honest Framing

Signal replay can support forward signal evidence. It is not a substitute for
order-level execution evidence, realized fills, slippage, funding, and daily
reconciliation.

Old single-component/decile history is in git history. Current truth is
`continuous_ensemble_v1`.
