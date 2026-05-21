# System Status

Updated 2026-05-21.

The liquidity-migration short strategy is in **committed paper forward testing**
on the Bybit demo account. The canonical configuration is the `promoted` profile
with `liquidity_migration_close_location_min = 0.30`.

## Canonical setting — close_location_min = 0.30

As of 2026-05-21, **`0.30` is the canonical close-location setting** for research
and for the forward test (previously 0.45). It was chosen from an exploratory
closing-bar sweep on the full-PIT IS root (2023-2026):

- 0.30 vs 0.45: more trades (510 vs 448) and higher total return (2637% vs
  2022%), at the cost of deeper drawdown (-18.0% vs -13.7%) and marginally lower
  walk-forward split Sharpe (3.51 vs 3.62).

This is a trade-count / return vs drawdown choice, not a strict improvement, and
it is **not yet tribunal-validated** — the prior tribunal WATCH verdict and
81-scenario sweep below were run on the 0.45 configuration. Validating 0.30
through `strategy-tribunal`, and the forward test itself, is the open work.

## Research status (prior baseline — close 0.45)

- A full point-in-time costed backtest, an 81-scenario parameter sweep, and an
  adversarial `strategy-tribunal` review returned a **WATCH** verdict with no
  FAIL findings: 3/3 pre-registered windows positive, six negative controls
  pass, 81/81 sweep scenarios promotable. This evidence is for close 0.45.
- The book carries almost no directional exposure (beta -0.03 to BTC, -0.07 to
  the equal-weight universe); it needs neither a regime gate nor a hedge.
- Caveat: true-OOS validation on the dedicated pre-2023 Bybit and Binance roots
  showed the edge does not clearly generalise before 2023 (walk-forward split
  Sharpe ~0). The edge is IS-era / regime-conditional — see
  `~/SHARED_DATA/bybit_fullpit_1h/reports/signed_flow_research_verdict.md`.

## Deployment status

- The VPS demo currently still runs the `demo_relaxed` profile. Switching the
  forward test to the canonical `promoted` + close-0.30 strategy is **pending**:
  the runner (`scripts/run_bybit_demo_event_engine.sh`) hard-restricts demo order
  submission to the `demo_relaxed` profile (the champion/challenger guard), so
  running `promoted` as a submitting demo requires changing that guard first.
- No real-money trading; the private client remains demo-only by design
  (`demo=False` is refused).
