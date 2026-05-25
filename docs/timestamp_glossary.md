# Timestamp glossary

The trade ledger carries **several** timestamps that all sound interchangeable
("the time of the trade") but mean very different things. Conflating them
caused the 2026-05-25 WAVESUSDT premature-exit bug: the orderLinkId recovery
set `entry_ts_ms = signal_ts_ms`, which collapsed the venue fill time onto
the signal time and made the position look 1-6h older than it really was, so
both `event_decay` and `planned_exit_ts_ms` tripped early.

Read this before touching anything in the trade-row builder, the adoption
path, the cycle's exit logic, or reconciliation.

## The 5 timestamps

### `signal_ts_ms`
**The kline bar timestamp that triggered the entry.** Always the closing
boundary of a 1h bar (so always at `xx:00:00.000 UTC`). Same on demo and
paper for the same signal — it's part of the deterministic `trade_id`
(`{scenario_id}-{symbol}-{signal_ts_ms}`). Encoded into the order_link_id
as base36(signal_ts_ms // 1000).

  - Set by: the cycle when it builds an entry candidate from a detected event.
  - Read by: `trade_id`, `order_link_id`, reconciliation pairing.
  - Survives a VPS rebuild: yes (decoded back from orderLinkId).

### `entry_ready_ts_ms`
**The earliest moment the signal can be acted on**, typically
`signal_ts_ms + feature_build_lag` (~218 min in production — the strategy
needs the bar to close AND the feature pipeline to compute its rolling
windows). Used by the cycle's stale check:
`now - entry_ready_ts_ms > MAX_ENTRY_LAG_MINUTES` → reject as stale.

  - Set by: the feature pipeline.
  - Read by: stale-skip gate.

### `entry_ts_ms`
**The actual venue fill time** for live orders, or the cycle's submit time
for paper (which idealizes fills at signal price). This is what the cycle's
exit logic uses for hold-window calc:
`planned_exit_ts_ms = entry_ts_ms + hold_days * MS_PER_DAY`.

  - Set by: cycle's order-fill confirmation (live) or cycle's signal-time
    snapshot (paper).
  - Read by: `event_decay` rank-checks, `planned_exit_ts_ms`, time-stop
    logic.
  - **DO NOT** set this to `signal_ts_ms` in the adoption recovery path
    (that's the bug). Use `opened_at_ms` instead.

### `opened_at_ms`
**Bybit's reported `createdTime` for the position.** Closest server-side
timestamp to the actual fill. The recovery path's source of truth for
when the venue saw the order land.

  - Set by: adoption (from `position.createdTime`).
  - Read by: nothing in the cycle directly; mirror copy for audit.

### `planned_exit_ts_ms`
**When the cycle's hold-window scheduler will close the position.** Computed
as `entry_ts_ms + hold_days * MS_PER_DAY` for the strategy, or
`opened_at_ms + adopt_hold_days * MS_PER_DAY` for adoption.

  - Set by: trade-row builder + adoption.
  - Read by: cycle's time-stop check.

### `ts_ms` (on the trade row itself)
**The wall-clock moment the trade row was written / last updated.** Closer
to `now` than to any of the above. Only useful for ordering ledger writes
on the same machine.

  - Set by: every code path that builds or updates a trade row.
  - Read by: ledger-display tooling only.

## Invariants

These should hold for every trade row. If you write code that violates them,
the cycle's exit/staleness logic breaks.

  - `entry_ts_ms >= signal_ts_ms`  
    Always. The fill cannot precede the signal that caused it.
  - `planned_exit_ts_ms > entry_ts_ms`  
    Hold window is positive.
  - `signal_ts_ms % MS_PER_HOUR == 0`  
    Signal ts is a 1h-bar boundary.
  - `opened_at_ms >= signal_ts_ms` (when present)  
    Bybit can't have created the position before the signal.

The adoption-recovery test pins these explicitly:
`tests/test_liquidity_migration_ws_risk.py::test_ws_risk_recovers_strategy_trade_id_from_bot_order_link`.

## Common pitfalls

  - **Confusing signal_ts with entry_ts.** Signal_ts is the bar
    boundary (00, 60, 120 min). Entry_ts is the venue fill time which can
    be 1-6h later. The cycle's exit logic uses entry_ts; setting it to
    signal_ts trips exits prematurely.
  - **Using `now_ms` as `entry_ts_ms`.** When closing a trade you write
    `exit_ts_ms = now_ms` but **don't touch** `entry_ts_ms`. The entry
    time is permanent once the order fills.
  - **Decoding orderLinkId timestamps without re-multiplying by 1000.**
    The orderLinkId encodes `base36(signal_ts_ms // 1000)`. Reversing:
    `signal_ts_ms = int(ts36, 36) * 1000`.
