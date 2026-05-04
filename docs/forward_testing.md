# Paper Forward Testing

This is the live-observation path for the daily-close fade alpha. It scans the
current Bybit public universe, records paper trades, and can send Telegram
notifications. The strategy forward path does not submit exchange orders.

## Contract

- public Bybit market data only
- no API keys for the strategy paper runner
- no private account/order endpoints inside `forward-run` or
  `forward-run-sleeves`
- no demo or live strategy order submission
- no old live runtime rebuild
- Telegram is notification-only

Demo exchange execution is intentionally separate from the strategy runner. It
tests plumbing more than alpha and can create false confidence from unrealistic
fills. The first useful forward test is still whether the system would have
selected good names from the full live universe without hindsight.

There is a separate `bybit-demo-probe` command for order-path checks. It can
authenticate to Bybit demo, submit one tiny far-from-touch post-only order, and
request immediate cancellation. It does not consume alpha candidates, open
strategy trades, manage fills, or alter the paper ledger.

There is also a separate `bybit-demo-sync` command for demo execution shadowing.
It reads `forward_paper_trades`, mirrors capped demo orders into its own
`demo_execution_orders` ledger, and reconciles open orders/positions. It is not
called by `forward-run` or `forward-run-sleeves`.

`bybit-demo-cycle` is the only scheduled demo-shadow command. It runs
`forward-run-sleeves` first, then syncs each sleeve into its own demo ledger.
It is still demo-only. It does not add real-money live execution.

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

Run all forward sleeves:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-run-sleeves
```

Sleeves:

```text
control_top_1_30: ranks 1-30, includes majors, 0.25x gross cap
core_31_150: ranks 31-150, top 5, current locked core logic
microcap_151_plus: ranks 151+, top 3, 0.50x gross cap, turnover floors,
                   and capacity-limited sizing for a $10k account
```

Each sleeve keeps an isolated ledger:

```text
data/forward-paper/forward_sleeves/control_top_1_30/
data/forward-paper/forward_sleeves/core_31_150/
data/forward-paper/forward_sleeves/microcap_151_plus/
```

This isolation matters. If the sleeves shared one ledger, same-day basket IDs
and same-symbol trade IDs could block or overwrite each other.

Write a report from the current paper ledger:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-report
```

Write the paper-vs-demo audit after demo cycles have run:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-audit
```

The audit joins each sleeve's `forward_paper_trades` to its
`demo_execution_orders` by paper trade ID. It reports paper expected trade,
demo entry/exit order state, fill status, entry/exit slippage, missed-trade
reason, sleeve attribution, and daily paper-vs-demo PnL. Accepted Bybit demo
order acknowledgements are not treated as fills; slippage and demo PnL require
reconciled filled quantity/value.

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

For the sleeve runner:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-run-sleeves \
  --telegram
```

The message includes scan status, candidate count, new paper trades, open
trades, closed trades, and the top candidates. The sleeve runner sends one
aggregate message instead of one message per sleeve. Missing env vars make
Telegram a no-op.

## Bybit Demo Probe

Use this only to verify API key, order create, and cancel plumbing. It is not a
performance test and it is not live trading.

Set demo credentials as environment variables. Do not put keys in the command:

```bash
export BYBIT_DEMO_API_KEY="..."
export BYBIT_DEMO_API_SECRET="..."
```

Dry-run the probe first. This uses public market data and writes the proposed
order without touching private endpoints:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  bybit-demo-probe \
  --symbol XRPUSDT \
  --side Sell \
  --notional 5
```

Actually place and immediately cancel one tiny demo order:

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

Safety rules:

- default target notional is 5 USDT and default max notional is 10 USDT
- default order is `PostOnly`
- default sell probe is placed far above the ask; default buy probe is placed
  far below the bid
- default behavior requests cancellation immediately after placement
- use `--leave-open` only if you are intentionally inspecting open-order state

Report output:

```text
data/forward-paper/reports/bybit_demo_probe_report.md
data/forward-paper/reports/bybit_demo_probe_report.json
```

## Bybit Demo Sync

This mirrors the paper ledger into tiny capped Bybit demo orders. It is the
next step after the probe, but it is still not live trading and it still does
not validate alpha by itself.

The command reads whichever `forward_paper_trades` ledger lives under
`--data-root`. To shadow only the core sleeve, point `--data-root` at the core
sleeve root:

```text
data/forward-paper/forward_sleeves/core_31_150
```

Dry-run first:

```bash
python -m aggression_carry \
  --data-root data/forward-paper/forward_sleeves/core_31_150 \
  --config configs/volume_alpha.default.yaml \
  bybit-demo-sync
```

Submit capped demo orders:

```bash
python -m aggression_carry \
  --data-root data/forward-paper/forward_sleeves/core_31_150 \
  --config configs/volume_alpha.default.yaml \
  bybit-demo-sync \
  --submit-orders \
  --i-understand-demo-sync
```

Safety rules:

- reads paper trades only; it does not run candidate selection
- default per-order cap is 10 USDT
- default max new orders per run is 5
- default total new notional cap per run is 50 USDT
- entry orders are `PostOnly`
- duplicate paper trades are de-duped by deterministic `orderLinkId`
- stale open entry orders are cancelled after 5 minutes by default
- set `--cancel-stale-minutes 0` to cancel any still-open entry order on the
  next sync; set it negative to disable stale cancellation
- if a paper trade closes and a demo position is detected, the exit order is
  reduce-only
- market exits are allowed by default only for reduce-only demo exits; use
  `--no-market-exit` to disable them

Reports:

```text
data/.../reports/bybit_demo_sync_report.md
data/.../reports/bybit_demo_sync_report.json
data/.../reports/bybit_demo_execution_orders.csv
data/.../demo_execution_orders/
```

Suggested schedule for demo shadowing the core sleeve:

- 22:16 UTC: `forward-run-sleeves --telegram`
- immediately after: `bybit-demo-sync --submit-orders --i-understand-demo-sync`
  using `data/forward-paper/forward_sleeves/core_31_150`
- every 5-15 minutes until flat: run both commands again so paper exits and
  demo reconciliation stay current

## Bybit Demo Cycle

The cycle command is the preferred demo-shadow runtime. It keeps sleeves
separate, prefixes demo order IDs by sleeve, and writes one cycle report plus
per-sleeve demo execution reports. The VPS `systemd` installer also runs
`forward-audit` after each cycle so the paper-vs-demo audit stays current.

Dry-run:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  bybit-demo-cycle \
  --telegram
```

Submit capped demo orders:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  bybit-demo-cycle \
  --submit-orders \
  --i-understand-demo-sync \
  --telegram
```

Cycle safety:

- demo-only private client; no live-money API path
- all sleeves shadowed separately: `control_top_1_30`, `core_31_150`,
  `microcap_151_plus`
- default caps are 10 USDT per order, 5 new orders per sleeve, and 50 USDT
  total new notional per sleeve per cycle
- reduce-only exits are prioritized before new entries
- `max_new_orders` is enforced before every private `place_order` call; exits
  get priority, while `max_total_new_notional` only caps new entry exposure
- dry-runs do not block later real demo submission
- `dry_run`, `skipped`, and `place_failed` states are retryable
- `data/forward-paper/DEMO_PAUSED` blocks new entries but still permits
  reduce-only exits and reconciliation
- a process lock at `data/forward-paper/.bybit_demo_cycle.lock` blocks
  overlapping timer runs
- the default active window is `22:05-02:30 UTC`; outside that window the
  cycle skips public scans/syncs unless paper or demo state is still active

Emergency demo commands:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  bybit-demo-cancel-all

python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  bybit-demo-flatten \
  --i-understand-demo-flatten
```

These emergency commands are demo-only and should be used when the demo ledger
or Bybit demo account is out of sync.

## VPS systemd Timer

Install the demo-only 5-minute timer from the VPS repo checkout:

```bash
scripts/install_bybit_demo_systemd.sh
```

The installer creates:

```text
/etc/model050426/bybit-demo.env
/etc/systemd/system/model050426-bybit-demo.service
/etc/systemd/system/model050426-bybit-demo.timer
```

Fill the env file on the VPS:

```bash
BYBIT_DEMO_API_KEY=...
BYBIT_DEMO_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Start and inspect:

```bash
sudo systemctl start model050426-bybit-demo.timer
systemctl list-timers model050426-bybit-demo.timer
journalctl -u model050426-bybit-demo.service -f
```

Pause and resume new entries:

```bash
touch data/forward-paper/DEMO_PAUSED
rm -f data/forward-paper/DEMO_PAUSED
```

The env file is intentionally outside the repo. Demo keys are throwaway, but
they still must not be committed or printed in logs.

## Scheduling

For now, run `forward-run` manually or from cron/Task Scheduler around the
signal and exit windows. The command is idempotent for a basket: it will not
open the same basket twice.

Useful schedule:

- 22:10 UTC: `forward-scan` preview
- 22:16 UTC: `forward-run` to enter paper trades after the 22:15 signal
- every 5-15 minutes until flat: `forward-run` to mark stops/exits
- after the window: `forward-report`
- after demo shadowing: `forward-audit`

For the three-sleeve observation run, replace `forward-run` with
`forward-run-sleeves` at the same times.

## Outputs

```text
data/forward-paper/reports/forward_scan_report.md
data/forward-paper/reports/forward_scan_candidates.csv
data/forward-paper/reports/forward_paper_report.md
data/forward-paper/reports/forward_paper_trades.csv
data/forward-paper/reports/forward_paper_baskets.csv
data/forward-paper/reports/forward_sleeves_report.md
data/forward-paper/reports/forward_sleeves_results.csv
data/forward-paper/reports/forward_demo_audit_report.md
data/forward-paper/reports/forward_demo_audit_trades.csv
data/forward-paper/reports/forward_demo_audit_daily.csv
data/forward-paper/reports/forward_sleeves/<sleeve>/forward_scan_report.md
data/forward-paper/reports/forward_sleeves/<sleeve>/forward_paper_report.md
data/forward-paper/forward_scan_features
data/forward-paper/forward_paper_trades
data/forward-paper/forward_paper_baskets
data/forward-paper/forward_sleeves/<sleeve>/forward_paper_trades
```

## Evidence Standard

This is not final alpha proof. It is live selection evidence:

- Did the full live universe produce sensible candidates?
- Were candidates tradable by spread and turnover?
- Did the paper lifecycle match the backtest assumptions?
- Did exits happen for the stated reasons?

Historical proof still requires the archive point-in-time path in
`docs/walk_forward_universe.md`.
