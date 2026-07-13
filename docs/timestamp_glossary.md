# Timestamp glossary

The current execution authority is the account journal, not a mutable sleeve
trade row. Keep event time, strategy-decision time and compatibility-projection
time separate: they answer different questions and cannot be substituted for
one another.

## Canonical account-event clocks

Every canonical event has:

- `wall_ts_ns`: the owner's local wall clock when the event was appended;
- `monotonic_ns`: the owner's monotonic clock for local sequencing and latency;
- `exchange_ts_ns`: the venue timestamp when the venue supplied one, otherwise
  zero;
- a root-global `sequence`, which is the durable ordering authority.

Fill and acknowledgement payloads also retain their specific venue and local
receive timestamps. Use those fields for execution latency and TCA. Never infer
a venue fill time from a strategy projection or from file-write time.

## Strategy and compatibility timestamps

Sleeve target metadata retains strategy-decision timestamps. The canonical
strategy read model joins those targets to execution anchors reconstructed from
the account journal. Target clocks and fill clocks remain separate even when a
legacy-shaped projection carries both.

### `signal_ts_ms`

The closed kline boundary that caused the strategy decision. It is part of the
stable component identity and survives into target metadata. For the current
hourly decision paths it is aligned to an hour boundary.

- Set by: the strategy candidate builder.
- Read by: component identity, re-entry/cooldown logic and reconciliation.
- Execution meaning: none; it is not a submit, acknowledgement or fill time.

### `entry_ready_ts_ms`

The earliest causal time at which a signal may be acted on under its entry
policy. Fixed-delay entries use `signal_ts_ms + entry_delay`; sniper/retrace
entries use their first qualifying boundary or deadline.

- Set by: the strategy scheduler/candidate builder.
- Read by: stale-signal and chronological scheduling checks.
- Execution meaning: eligibility, not a fill.

### `entry_ts_ms`

On the current `canonical_strategy_trade_rows` read model this is the local
receive time of the first journal-confirmed fill attributable to the component.
It remains null before a fill and when an aggregate same-symbol fill cannot be
attributed safely. It is never replaced with target acceptance time.

Archived pre-account-kernel sleeve roots used this name for an actual fill time
or, in paper mode, a submit-time idealization. Do not combine those legacy rows
with current target projections without labelling the semantic change.

### `opened_at_ms`

On the current strategy read model this mirrors `entry_ts_ms`: the first
attributable fill's local receive time. Archived direct-execution roots used it
for Bybit's reported `createdTime`. Venue fill time remains separately preserved
on the canonical execution event.

### `entry_target_ts_ms`

The wall time of the first accepted non-zero component target. This is the
planning/admission clock that older projections incorrectly exposed as
`entry_ts_ms`. It may precede a fill and never starts protection or max hold.

### `max_hold_duration_ms`

The strategy-decided hold duration published with an entry target. New target
producers publish a duration, not an absolute decision-time deadline. Historical
target metadata may be interpreted only as a labelled duration delta.

### `planned_exit_ts_ms`

On the current strategy read model this is derived as first attributable fill
time plus `max_hold_duration_ms`. It stays null before a fill or when attribution
is ambiguous. The sleeve may publish a zero target after this boundary; the
field does not assert when the account owner will fill the resulting aggregate
order.

`target_planned_exit_ts_ms` preserves any legacy absolute deadline from target
metadata for audit only. It is not a lifecycle clock.

### `ts_ms`

The wall-clock write/update time on legacy-shaped Parquet rows. It is useful for
ordering local projection writes only. It is not an exchange timestamp and it
is not authoritative over the journal sequence.

## Invariants

- `signal_ts_ms` must not be later than the strategy decision that cites it.
- `entry_target_ts_ms` must not precede the signal decision that produced it.
- `entry_ts_ms` and `opened_at_ms` remain null until an attributable fill and,
  when present, identify the same first-fill clock.
- `planned_exit_ts_ms` equals fill time plus the declared duration whenever both
  are available; it is not derived from target acceptance.
- Component stop/take-profit prices are derived from confirmed fill VWAP, never
  from a decision reference price.
- Venue latency, fill price, fee, close and P&L must come from canonical
  acknowledgement/fill/P&L events, never from the planning timestamps above.
- A zero `exchange_ts_ns` means that the venue supplied no timestamp; it does
  not mean Unix epoch.

`tests/test_account_strategy_state.py` pins the current strategy-projection
semantics. Account event ordering and fill clocks are covered by the account
kernel, execution-stream and reconciliation tests.

## Legacy warning

The 2026-05-25 WAVESUSDT incident came from a retired adoption path that decoded
an order-link signal timestamp and wrote it as `entry_ts_ms`, making the position
appear hours older than the venue fill. Archived rows produced by that path are
historical evidence, not a recovery mechanism. Current account commands use
canonical command identities, and venue fill time comes from the account event
stream.
