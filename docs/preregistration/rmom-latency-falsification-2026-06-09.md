# Active Receipt: Rmom Latency Falsification

**Status:** binding caveat on continuous promotion.

## Verdict

Rmom is causal, not an off-by-one leak, but the usable effect is concentrated at
the freshest legal daily availability. Delaying it further kills the edge.

Consequences:

- Daily-rmom continuous evidence has no deployment-grade operational margin.
- Continuous cannot be promoted while relying on this daily rmom edge.
- Any revival needs a design that consumes the fast-decay signal with realistic
  latency and costs, or forward evidence that proves the current implementation
  survives live delay.

The old falsification harness depended on code deleted with the short-sleeve
erasure. Re-running requires restoring the historical comparator in a scratch
checkout, not reviving erased short code in main.
