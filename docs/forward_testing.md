# Paper Forward Testing

This is the live-observation path for the daily-close fade alpha. It scans the
current Bybit public universe, records paper trades, and can send Telegram
notifications. It does not submit exchange orders.

## Contract

- public Bybit market data only
- no API keys for Bybit
- no private account/order endpoints
- no demo or live order submission
- no old live runtime rebuild
- Telegram is notification-only

Demo exchange execution is intentionally not implemented yet. It tests plumbing
more than alpha and can create false confidence from unrealistic fills. The
first useful forward test is whether the system would have selected good names
from the full live universe without hindsight.

## Signal

The paper tester uses the same daily-close fade rules as the backtester:

- scan live Bybit USDT linear perps
- exclude non-trading/prelisting contracts
- exclude configured majors by default
- exclude symbols younger than 10 days
- filter by live 24h turnover, spread, and optional open-interest value
- use 1m bars from 00:00 UTC through the configured signal minute
- rank baseline liquidity from prior daily quote turnover, defaulting to ranks
  31-150 so top-tier perps are ignored without a static symbol list
- rank day-to-date gainers by `vol_adjusted_day_return` by default
- require `pump_like` by default
- paper-short top 5
- enter after the configured entry delay
- record capacity-limited paper weight when turnover caps are enabled
- mark exits by max hold, 20% disaster stop, fixed TP if enabled, standard
  trailing stop if enabled, volatility-scaled trail if enabled, MFE giveback if
  enabled, or VWAP-reversion exit if enabled

The current paper-forward default has no fixed TP and uses the best current
adaptive-exit candidate: baseline liquidity ranks 31-150, 20% disaster stop,
`0.25x` daily-vol trail after the 15-minute stop delay, and 20% MFE giveback
after +1% favorable movement. Fixed TP and VWAP-reversion exits capped too much
right tail in the backtests.

For experimental microcap paper testing, override the liquidity bucket and
enable capacity caps instead of pretending every thin coin receives full size:

```bash
python -m aggression_carry \
  --data-root data/forward-paper-microcap \
  --config configs/volume_alpha.default.yaml \
  forward-run \
  --top-n 3 \
  --gross-exposure 0.5 \
  --liquidity-rank-min 151 \
  --liquidity-rank-max 0 \
  --min-baseline-turnover 250000 \
  --min-day-turnover 750000 \
  --min-last-60m-turnover 75000 \
  --account-equity 10000 \
  --max-position-weight 0.20 \
  --max-trade-notional-pct-day-turnover 0.002 \
  --max-trade-notional-pct-baseline-turnover 0.005 \
  --min-turnover-24h 750000 \
  --max-spread-bps 80
```

This is a separate sleeve. Do not merge it into the core 31-150 book until the
paper ledger shows fills, spreads, and exits are consistent with the backtest.

Short PnL is USDT-linear:

```text
short return = (entry_price - exit_price) / entry_price
```

## Commands

Single live scan:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-scan
```

Run one paper cycle:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-run
```

Write a report from the current paper ledger:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-report
```

Override the locked-in daily-close settings:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-run \
  --signal-time 22:15 \
  --top-n 5 \
  --hold-minutes 180 \
  --pump-filter pump \
  --liquidity-rank-min 31 \
  --liquidity-rank-max 150 \
  --stop-loss-pct 0.20 \
  --take-profit-pct 0 \
  --trailing-stop-pct 0 \
  --vol-trailing-stop-mult 0.25 \
  --vol-trailing-activation-mult 0 \
  --mfe-giveback-activation-pct 0.01 \
  --mfe-giveback-pct 0.20 \
  --min-turnover-24h 2000000 \
  --max-spread-bps 80
```

## Telegram

Set these environment variables:

```bash
export TELEGRAM_BOT_TOKEN="123456:abc..."
export TELEGRAM_CHAT_ID="123456789"
```

Then run:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-run \
  --telegram
```

The message includes scan status, candidate count, new paper trades, open
trades, closed trades, and the top candidates. Missing env vars make Telegram a
no-op.

## Scheduling

For now, run `forward-run` manually or from cron/Task Scheduler around the
signal and exit windows. The command is idempotent for a basket: it will not
open the same basket twice.

Useful schedule:

- 22:10 UTC: `forward-scan` preview
- 22:16 UTC: `forward-run` to enter paper trades after the 22:15 signal
- every 5-15 minutes until flat: `forward-run` to mark stops/exits
- after the window: `forward-report`

## Outputs

```text
data/forward-paper/reports/forward_scan_report.md
data/forward-paper/reports/forward_scan_candidates.csv
data/forward-paper/reports/forward_paper_report.md
data/forward-paper/reports/forward_paper_trades.csv
data/forward-paper/reports/forward_paper_baskets.csv
data/forward-paper/forward_scan_features
data/forward-paper/forward_paper_trades
data/forward-paper/forward_paper_baskets
```

## Evidence Standard

This is not final alpha proof. It is live selection evidence:

- Did the full live universe produce sensible candidates?
- Were candidates tradable by spread and turnover?
- Did the paper lifecycle match the backtest assumptions?
- Did exits happen for the stated reasons?

Historical proof still requires the archive point-in-time path in
`docs/walk_forward_universe.md`.
