# Forward-record annotations — sizing and mechanics validity

Registered 2026-07-20 together with
`docs/preregistration/sleeve_kill_criteria_2026-07-20.md`. This document
defines when a forward-record day measures *economics* and when it only
measures *mechanics*, using the venue-quantization floor as the boundary.

## The measured problem (2026-07-20 snapshot)

Facts from the live demo book and canonical journal, read 2026-07-20:

- Account equity: 9,985.42 USDT; CONTINUOUS base sizing is 2% of equity per
  position (~199.7 USDT) before component, volatility, and BTC-risk weight
  multipliers.
- The weight stack routinely reduces the smallest admitted components to
  ~15% of base: the open TLMUSDT component on 2026-07-20 carried 28.75 USDT
  notional.
- Bybit linear venue minimums (~5 USDT notional, symbol qty steps) are then
  a **~17% floor** on such components. Entry/exit rounding, unexpressable
  dust residuals (the 2026-07-20 00:04 UTC ACEUSDT case: a 0.1-unit
  residual against a ~5 USDT minimum), and account-netted reduction
  attribution are all first-order effects at this size.
- Consequence: for the smallest components, realized P&L measures venue
  quantization roughly as much as it measures the signal, and results do
  not extrapolate linearly to realistic size.

## Annotation rule

- A component is **quantization-distorted** when its intended (pre-venue)
  notional is below **4× the venue minimum notional** for its symbol
  (≈ 20 USDT at the standard 5 USDT minimum).
- A sleeve's forward-record day is **mechanics-only** when
  quantization-distorted components carried more than **20% of that day's
  gross exposure**, or when the 14-day quantized-entry condition in the
  kill-criteria registration trips. Mechanics-only days validate plumbing
  (routing, accounting, protection, reconciliation) and do not count toward
  the kill-criteria evidence samples (K2/K3) or any promotion claim.
- Days before 2026-07-20 are annotated wholesale: the demo record to date
  is **mechanics-and-accounting evidence**, not sized economics. This is
  consistent with how `docs/research_summary.md` already labels the live
  curves ("does not prove live-runtime parity"; "tiny skewed forward
  sample").

## The fix direction (registered forward, not applied retroactively)

The corrective is a **per-component notional floor in strategy sizing**:
components whose intended notional falls below the floor are either dropped
with their weight re-normalized across surviving components, or lifted to
the floor, keeping gross unchanged. This is a deliberate strategy-config
change — it belongs to the breadth redesign
(`docs/breadth_redesign_2026-07-20.md`), lands as one registered config with
its own commit-dated forward record, and does not retroactively edit any
existing record.
