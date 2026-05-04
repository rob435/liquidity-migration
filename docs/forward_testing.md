# Forward And Demo Testing

Forward/demo exists to test plumbing and execution drift. It is not alpha proof.

## Current Boundary

The active research strategy uses a 22:00-23:00 TWAP entry. Backtest support
exists. Forward/demo slice execution does **not** exist yet.

Therefore:

```text
forward-run and bybit-demo-cycle must not fake TWAP as one fill.
```

The current code blocks new paper entries when `entry_twap_minutes > 0`. This
is intentional and safer than pretending the demo account traded a TWAP.

## Next Implementation Target

Add slice-level paper/demo execution:

```text
22:00 UTC: rank candidates from data available at 22:00
22:00-22:59: submit/record equal 1m entry slices
entry_price: running average fill price
first fill onward: 20% disaster stop on average entry
23:15 onward: vol trail and MFE giveback can flatten the whole symbol
max hold: 180m after final add
no same-symbol re-entry that day
```

The audit layer must report:

```text
expected slice
demo order
fill status
missed slice reason
average entry slippage
exit slippage
symbol/day PnL
sleeve attribution
```

## Existing Commands

Public-data scan:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-scan
```

Paper run:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-run
```

Demo probe:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  bybit-demo-probe \
  --symbol XRPUSDT \
  --side Sell \
  --notional 5 \
  --place-order \
  --i-understand-demo-order
```

Demo shadow cycle:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  bybit-demo-cycle \
  --submit-orders \
  --i-understand-demo-sync
```

With the current TWAP config, the shadow cycle should not open new entries until
slice execution is implemented. Existing demo emergency commands remain useful:

```bash
python -m aggression_carry --data-root data/forward-paper --config configs/volume_alpha.default.yaml bybit-demo-cancel-all
python -m aggression_carry --data-root data/forward-paper --config configs/volume_alpha.default.yaml bybit-demo-flatten --i-understand-demo-flatten
```

## Telegram

Telegram should stay quiet:

```text
position entries
position exits
end-of-day PnL
critical errors
```

No scan spam, no routine heartbeat spam.
