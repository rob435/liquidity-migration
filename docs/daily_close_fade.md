# Daily-Close Fade

This is the active demo strategy. The canonical config is
`configs/volume_alpha.default.yaml`; old alternate daily-close config files were
removed to avoid two sources of truth.

## Current Contract

```text
Signal time: 23:15 UTC
Sleeve: stage4_selected
Side: short only
Selection: top 1 pump candidate by day_return
Membership: require PIT Bybit archive membership in backtests
Age: listed at least 10 days
Liquidity: prior 7d baseline turnover rank 226+
Quality gates: coin VWAP extension <= 10%, market median day return <= 3%
Entry: 20 equal 1-minute TWAP slices from 23:15 through 23:34
TWAP guard: stop adding slices after 2% adverse move during entry
Hard stop: 8%, active from first fill
Fixed take profit: 10%
Profit protection delay: 120 minutes after final TWAP slice
Time-decay take profit: decay from 10% to 5% over 120 minutes
Max hold: 360 minutes after TWAP completion
Risk sizing in demo: current Bybit demo wallet balance, max 10% per coin
```

Ranking at 23:15 must use only completed 1m bars through 23:14. Treating the
23:15 candle as available at the 23:15 decision time is lookahead.

## Execution Path

```text
forward-run-sleeves --forward-mode scan --sleeves stage4_selected
bybit-demo-cycle --forward-mode open-from-scan --require-first-slice
bybit-demo-sync submits due demo entries and reduce-only exits
forward-audit reconciles paper slices, demo orders, fills, misses, and drift
```

The systemd entry points are:

```text
scripts/install_bybit_demo_systemd.sh
scripts/run_forward_signal_with_audit.sh
scripts/run_bybit_demo_engine.sh
```

## Backtest Discipline

Daily-close reports must keep the audit section that records:

```text
run label
promotion allowed true/false
config hash
data-root fingerprint
decision/order/fill/exit lifecycle
capacity and market-impact assumptions
```

Current-universe runs are benchmarks only. A run without PIT membership,
capacity caps, market impact, split stability, and forward/demo reconciliation
does not support promotion.

The known legacy failure is warm-started adaptive protection: trailing/MFE state
must start only when profit protection activates, not during the TWAP or delay
window. The permanent rule is kept in
`docs/backtesting_errors_we_never_repeat.md`.
