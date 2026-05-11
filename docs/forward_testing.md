# Forward And Demo Trading

Forward/demo is now the execution path for the selected Stage 4 daily-close short fade on the Bybit demo account.

## Runtime Contract

- 23:00 UTC: scan the live Bybit USDT perp universe, cache the selected Stage 4 candidate, and immediately hand it to the demo cycle for the first TWAP slice.
- 23:00-23:59 UTC: open the paper trade as 60 one-minute TWAP child slices and submit matching Bybit demo entry orders.
- 00:00 UTC onward: stop adding, keep marking the paper trade, reconcile demo orders, and submit reduce-only exits when the paper exit model closes.
- 02:00 UTC onward: the time-decay TP can start working after the 120-minute profit-protection delay.
- 06:00 UTC target: max-hold exit if no stop or TP fired.

The forward ledger writes `forward_paper_trades`; the TWAP child schedule writes `forward_paper_slices`; demo orders write `demo_execution_orders`. `forward-audit` joins all three so missed slices, slippage, fill status, and paper/demo drift are visible.

## Strategy Defaults

The canonical sleeve is `stage4_selected`. It inherits `configs/volume_alpha.default.yaml`:

- `signal_minute: 1380`
- `top_n: 1`
- `score: day_return`
- `liquidity_rank_min: 226`
- `entry_twap_minutes: 60`
- `hold_minutes: 360`
- `stop_loss_pct: 0.08`
- `take_profit_pct: 0.10`
- `time_decay_take_profit_floor_pct: 0.04`
- `time_decay_take_profit_minutes: 120`
- `profit_protection_delay_minutes: 120`

## Demo Sizing

Use wallet-aware sizing for demo order submission:

```bash
--use-wallet-balance \
--max-order-notional 0 \
--max-total-new-notional 0 \
--max-order-notional-pct-equity 0.10 \
--max-total-new-notional-pct-equity 1.0
```

For a 60-minute TWAP, `max-order-notional-pct-equity=0.10` caps the whole coin position at 10% of current Bybit demo wallet equity, then divides that notional across the 60 slices.

## Commands

Run the signal scan:

```bash
python -m aggression_carry --data-root data/forward-paper --config configs/volume_alpha.default.yaml forward-run-sleeves --forward-mode scan --sleeves stage4_selected
```

Run one demo cycle:

```bash
python -m aggression_carry --data-root data/forward-paper --config configs/volume_alpha.default.yaml bybit-demo-cycle --submit-orders --i-understand-demo-sync --use-wallet-balance --max-order-notional 0 --max-total-new-notional 0 --max-order-notional-pct-equity 0.10 --demo-entry-sleeves stage4_selected --forward-mode open-from-scan --require-first-slice
```

Run audit:

```bash
python -m aggression_carry --data-root data/forward-paper --config configs/volume_alpha.default.yaml forward-audit --telegram
```

Install the long-running demo service and 23:00 UTC signal timer. The timer runner performs scan, first-slice demo handoff, then audit; the always-on engine continues later TWAP slices and exits.

```bash
scripts/install_bybit_demo_systemd.sh
```

Emergency demo actions:

```bash
python -m aggression_carry --data-root data/forward-paper --config configs/volume_alpha.default.yaml bybit-demo-cancel-all --i-understand-demo-cancel-all
python -m aggression_carry --data-root data/forward-paper --config configs/volume_alpha.default.yaml bybit-demo-flatten --i-understand-demo-flatten
```
