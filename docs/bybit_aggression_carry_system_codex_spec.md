# Bybit Demo Trading System Spec

## Objective

The project focus is a profitable Bybit demo-account system built around the selected Stage 4 daily-close short fade. Work should improve one of four things:

- strategy/backtest parity
- forward signal quality
- demo order execution and reconciliation
- risk sizing, auditability, or operations

The current private Bybit client is demo-only and refuses `demo=False`. Real-money support is a separate implementation, not a hidden mode.

## Active Strategy

Canonical config: `configs/volume_alpha.default.yaml`.

Selected benchmark: Full PIT Bybit Scam-Tail Stage 4 top-result refinement, documented by `data/research_reports/backtests/top_result_equity_trades_vwap10_m03_ex00_20260515T130424Z/summary.json`.

Backtest reference:

- Return: 88.41%
- Sharpe-like: 2.75
- Max drawdown: -5.41%
- Trades: 705

Contract:

- Signal at 23:15 UTC.
- Short the top 1 pump candidate by `day_return`.
- Require PIT Bybit archive membership.
- Exclude major/high-liquidity symbols listed in the config.
- Require baseline liquidity rank 226+.
- Require intraday VWAP extension <= 10% and market median day return <= 3%.
- Enter with 20 equal 1-minute TWAP slices.
- Stop adding future TWAP slices if price moves 2% against the short during entry.
- Stop at 8%, active from first fill.
- Fixed TP at 10%.
- Time-decay TP from 10% down to 5% over 120 minutes after profit protection activates.
- Profit protection activates 120 minutes after the final TWAP slice.
- Max hold is 360 minutes after TWAP completion.

## Demo Execution

The demo runtime is sleeve-based. The active sleeve is `stage4_selected`.

Flow:

1. `forward-run-sleeves --forward-mode scan --sleeves stage4_selected` caches the 23:15 candidate.
2. The signal runner immediately calls `bybit-demo-cycle --forward-mode open-from-scan --require-first-slice` so the first TWAP slice does not depend on the background minute loop racing the scan.
3. `forward_paper_slices` defines the expected TWAP child orders.
4. `bybit-demo-sync` submits due demo entry slices and reduce-only exits.
5. `forward-audit` reconciles paper trades, child slices, demo orders, fills, misses, and PnL drift.

The runtime should preserve exit parity with the backtest. In particular, forward marking must include the Stage 4 time-decay take-profit path, not just fixed TP and max hold.

## Demo Risk

Default demo sizing should use current Bybit demo wallet equity:

```bash
--use-wallet-balance
--max-order-notional 0
--max-total-new-notional 0
--max-order-notional-pct-equity 0.10
--max-total-new-notional-pct-equity 1.0
```

`max-order-notional-pct-equity` is treated as the per-coin total trade cap. For TWAP entries the capped trade notional is divided across the scheduled child slices.

## Operations

Systemd installer:

```bash
scripts/install_bybit_demo_systemd.sh
```

Long-running engine:

```bash
scripts/run_bybit_demo_engine.sh
```

Signal scan:

```bash
scripts/run_forward_signal_with_audit.sh
```

Pause new demo entries by creating `data/forward-paper/DEMO_PAUSED`. Reduce-only exits and reconciliation should continue.

## Secondary Research

Volume-alpha work remains useful as separate research, but it is not the active demo trading stack. Treat it as a candidate source only after it clears standalone costs and has its own forward/demo evidence.
