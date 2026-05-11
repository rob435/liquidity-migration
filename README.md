# MODEL050426

Bybit demo-account trading system for the daily-close short fade.

## Current Objective

The main objective is to get the selected Stage 4 strategy working properly on the Bybit demo account, then use forward/demo evidence to decide the next change. The repo now treats demo execution, slice reconciliation, and exit parity with backtests as first-class production work.

The private Bybit client is still demo-only by design: `demo=False` is refused in code. Real-money trading requires a separate explicit implementation decision.

## Active Strategy

Selected strategy: `configs/volume_alpha.default.yaml` and `configs/daily_close_fade.lowcap_scam_tail_stage4_selected.yaml`.

Key parameters:

- Signal: 23:00 UTC
- Side: short only
- Ranking: `day_return`
- Selection: top 1 pump candidate, baseline liquidity rank 226+
- Entry: 60 equal 1-minute TWAP slices from 23:00 through 23:59 UTC
- Hold: 360 minutes after TWAP completion
- Stop: 8%, active from first fill
- Take profit: 10%, then time-decay floor to 4% over 120 minutes after profit protection activates
- Profit protection delay: 120 minutes after final TWAP slice
- Capacity: 0.05% same-day turnover and 0.10% baseline turnover caps
- Impact model: 3 bps per 1% turnover participation

Reference backtest snapshot: `docs/daily_close_fade_full_listing_scam_tail_stage4_20260510.md` reported 11.48% return, 5.51 Sharpe-like score, -3.17% max drawdown, and 74 trades under point-in-time Bybit archive membership.

## Demo Runtime

The demo runtime uses slice-level paper trades and slice-level Bybit demo orders. It does not collapse the 60-minute TWAP into one fill.

Default demo sizing is wallet-aware:

- `--use-wallet-balance`
- `--max-order-notional 0`
- `--max-total-new-notional 0`
- `--max-order-notional-pct-equity 0.10`
- `--max-total-new-notional-pct-equity 1.0`

For TWAP entries, the 10% cap applies to the whole paper trade per coin, then the entry is divided across the 60 slices.

Install the demo systemd units:

```bash
scripts/install_bybit_demo_systemd.sh
```

Manual commands:

```bash
python -m aggression_carry --config configs/volume_alpha.default.yaml --data-root data/forward-paper forward-run-sleeves --forward-mode scan --sleeves stage4_selected
python -m aggression_carry --config configs/volume_alpha.default.yaml --data-root data/forward-paper bybit-demo-cycle --submit-orders --i-understand-demo-sync --use-wallet-balance --max-order-notional 0 --max-total-new-notional 0 --max-order-notional-pct-equity 0.10 --demo-entry-sleeves stage4_selected --forward-mode open-from-scan --require-first-slice
python -m aggression_carry --config configs/volume_alpha.default.yaml --data-root data/forward-paper forward-audit --telegram
```

Emergency demo controls:

```bash
python -m aggression_carry --data-root data/forward-paper --config configs/volume_alpha.default.yaml bybit-demo-cancel-all --i-understand-demo-cancel-all
python -m aggression_carry --data-root data/forward-paper --config configs/volume_alpha.default.yaml bybit-demo-flatten --i-understand-demo-flatten
```

Pause new demo entries:

```bash
touch data/forward-paper/DEMO_PAUSED
```

Resume:

```bash
rm -f data/forward-paper/DEMO_PAUSED
```

## Useful Files

- `aggression_carry/daily_close_fade.py`: backtest strategy and exit model
- `aggression_carry/forward_test.py`: live scan, paper TWAP slices, and paper marking
- `aggression_carry/demo_execution.py`: Bybit demo order sync and reconciliation
- `aggression_carry/demo_cycle.py`: minute-loop orchestration
- `aggression_carry/forward_audit.py`: paper/demo slice audit
- `scripts/run_bybit_demo_engine.sh`: continuously runs demo cycle plus audit
- `scripts/run_forward_signal_with_audit.sh`: 23:00 signal scan plus audit
