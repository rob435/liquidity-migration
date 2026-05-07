# Forward And Demo Testing

Forward/demo exists to test plumbing and execution drift. It is not alpha proof.

## Current Boundary

The active research strategy ranks at 22:00 UTC and uses a 22:01-23:00 TWAP
entry. Backtest, forward paper, and Bybit demo shadowing use slice-level
accounting.

Therefore:

```text
forward-run and bybit-demo-cycle must not fake TWAP as one fill.
```

No paper or demo fill may be assumed at the same timestamp used for ranking.
Bybit/archive 1m bars are minute-open timestamped, so the 22:00 rank uses only
bars whose end time is no later than 22:00. The last input 1m bar for the active
signal has open timestamp 21:59.

## Slice Lifecycle

```text
22:00 UTC: rank candidates from bars available at 22:00
22:01-23:00: submit/record 60 equal 1m entry slices
entry_price: running average fill price
first fill onward: 20% disaster stop on average entry
23:15 onward: vol trail and MFE giveback can flatten the whole symbol
max hold: 180m after final scheduled add
no same-symbol re-entry that day
```

The audit layer must report:

```text
expected slice
demo order
fill status
entry slippage
exit slippage
missed slice reason
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

Demo shadow sync:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  bybit-demo-cycle \
  --submit-orders \
  --i-understand-demo-sync \
  --demo-entry-sleeves rank_31_plus \
  --entry-leverage 1 \
  --forward-mode open-from-scan \
  --require-first-slice
```

The shadow sync mirrors only the currently due child slice. It must not run a
full-universe signal scan inside the one-minute order loop. The production/demo
timer uses cached 22:00 scan candidates and `--require-first-slice`, so paper
entries are opened only at the first scheduled 22:01 slice. If the 22:00 signal
scan is late, demo entry stays flat and audit reports the miss instead of
starting a different partial TWAP at 22:05 or 22:06. The default
`require_contiguous_twap` guard also refuses a later child slice when an earlier
slice has no demo order attempt.

By default, demo submission opens new entries only for the canonical
`rank_31_plus` sleeve: prior-liquidity ranks 31+ in one combined ranking, with
the top 30 excluded. Reduce-only exits remain allowed, but there are no
separate control/core/microcap demo sleeves in the runtime path. Demo entry
submission enforces 1x Bybit demo leverage before each new
entry symbol when the private client supports the venue leverage endpoint. This
changes demo margin state only; it does not change paper sizing, candidates,
stops, or backtest logic. Existing demo emergency commands remain useful:

```bash
python -m aggression_carry --data-root data/forward-paper --config configs/volume_alpha.default.yaml bybit-demo-cancel-all
python -m aggression_carry --data-root data/forward-paper --config configs/volume_alpha.default.yaml bybit-demo-flatten --i-understand-demo-flatten
```

## Forward/Demo Drift Loop

Use the live loop to measure execution drift, not alpha quality. The practical
cadence is:

```text
22:00 UTC: forward-run-sleeves ranks the traded sleeve and persists cached scan candidates.
22:01 UTC: the long-running demo engine opens paper trades from the cached scan only if this is the first TWAP slice.
22:01-23:00 UTC: the engine refreshes every wall-clock minute and mirrors only the due slice.
23:15-02:30 UTC: the same engine keeps reconciling entries, listens to public trades for fast vol/MFE exits,
                 and submits idempotent whole-symbol reduce-only exits through the shared demo ledger.
after each minute cycle: forward-audit writes paper-vs-demo drift reports.
```

The systemd installer uses `scripts/run_bybit_demo_engine.sh` as one long-running
runtime. It loops on wall-clock minute boundaries, runs `bybit-demo-cycle` with
fast protection enabled, then runs `forward-audit --telegram`. It does not run
the full signal scan inside the minute loop. The installer also creates a
separate `*-signal.timer` that runs `scripts/run_forward_signal_with_audit.sh`
at 22:00 UTC. That signal runner defaults to `FORWARD_SIGNAL_SLEEVES=rank_31_plus`,
the only canonical sleeve.

The default environment is rank-31-plus-only demo entry submission with
`DEMO_ENTRY_SLEEVES=rank_31_plus`, `DEMO_ENTRY_LEVERAGE=1`,
`DEMO_FORWARD_MODE=open-from-scan`, and
`FORWARD_SIGNAL_SLEEVES=rank_31_plus`. Demo child order target notional is
always the same paper/backtest child notional, normally `target_notional / 60`
during the TWAP. Bybit tick, quantity-step, and minimum-notional rules still
apply; if a minimum order would oversize the paper child, the demo row is
skipped instead of enlarged. There is no wallet-scaled or capped demo sizing
mode in the canonical runtime.

Performance boundary: the signal scan must finish before 22:01 or no demo entry
should be submitted. The current REST path uses concurrent public kline fetches,
but exact 22:01 reliability ultimately requires prewarming most daily/1m data
before 22:00 and doing only the final rank/open step at 22:00.

Start with dry-run ledgers. Only enable demo submission after dry-run output
shows the expected 60-slice schedule, isolated sleeve ledgers, and no order-link
collisions. Demo submission still requires explicit `--submit-orders` and
`--i-understand-demo-sync`; there is no real-money live trading path in this
repo.

The audit output to watch is:

```text
reports/forward_demo_audit_slices.csv
reports/forward_demo_audit_slice_daily.csv
reports/forward_demo_audit_daily.csv
reports/forward_demo_audit_report.md
```

`forward_demo_audit_slice_daily.csv` is the first health check. A trade-level
row can show at least one fill while many child slices are missing, so the
slice-level fill rate, missing/actionable slice count, open-slice count, and
average slice slippage are the canonical execution-drift metrics.

Useful replay command:

```bash
python -m aggression_carry \
  --data-root data/forward-paper \
  --config configs/volume_alpha.default.yaml \
  forward-audit \
  --now 2026-01-16T03:00:00+00:00
```

## Telegram

Telegram should stay quiet:

```text
position entries
position exits
end-of-day PnL
critical errors
```

## Bybit-Native Stops

Bybit V5 has a position trading-stop endpoint for full-position stop loss,
take-profit, and trailing stop. The demo client exposes this wrapper, but the
canonical forward/demo lifecycle does not yet promote native trailing as the
profit-protection source of truth.

The reason is important: the current backtest evaluates adaptive exits from 1m
bars after final add + 15m. The demo engine adds public-trade fast protection
for runtime safety, but it still uses the same vol-trailing plus MFE-giveback
thresholds and writes exits through the shared demo execution ledger. A Bybit
trailing stop is exchange-side and faster, but its trailing-distance/activation
semantics are not identical to this model. Promoting native trailing requires a
backtest change that models Bybit's exact trading-stop behavior first. Native
disaster stop syncing is the safer next candidate because a full-position stop
loss at 20% above current average short entry is much closer to the current
contract.

No scan spam, no routine heartbeat spam.
