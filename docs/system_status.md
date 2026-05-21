# System Status

Updated 2026-05-21.

The liquidity-migration short strategy is sound and effectively market-neutral
(see `docs/research_findings.md`).

## Research status

- A full point-in-time costed backtest, an 81-scenario parameter sweep, and an
  adversarial `strategy-tribunal` review return a **WATCH** verdict with no
  FAIL findings: 3/3 pre-registered windows positive, all six negative controls
  pass, 81/81 sweep scenarios promotable.
- The book carries almost no directional exposure (beta -0.03 to BTC, -0.07 to
  the equal-weight universe) and is positive in both up and down regimes. It
  needs neither a regime gate nor a hedge — both were tested and rejected on
  evidence.
- The remaining gap to a green tribunal PASS is the `execution_drift` check,
  which needs live demo execution data.

## Deployment status

- The demo, risk-watchdog, and dry-run paper services run on the Singapore VPS
  against the Bybit demo account. No real-money trading; the private client
  remains demo-only by design (`demo=False` is refused).
