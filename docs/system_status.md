# System Status

Updated 2026-05-21.

The liquidity-migration short strategy is under **active research —
refinement phase** (see `README.md` and `docs/research_findings.md`).

## Research status

- The signal is statistically real but regime-conditional. The standalone
  strategy is not deployable as-is — it failed promotion in the
  `strategy-tribunal` (funding unmodeled; 2 of 3 pre-registered out-of-sample
  windows positive).
- Current focus: a regime / crash-detector overlay that keeps the strategy
  active only in regimes where the edge monetizes, plus funding-cost modeling.

## Deployment status

- Nothing is deployed — no live or demo trading, no active champion or
  challenger.
- The Bybit private client remains demo-only by design (`demo=False` is
  refused).
