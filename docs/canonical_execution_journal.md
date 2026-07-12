# Canonical Execution Journal

## Authority

`<ledger-root>/canonical_journal/events.jsonl` is the execution authority for
historical, paper and demo lifecycles. Trade/order Parquet datasets, TCA Parquet,
Telegram summaries and dashboard inputs are projections. They may be deleted and
rebuilt; the journal may not be reset to alter operational state.

Every event has:

- an immutable UUID `event_id` and root-global monotonic `sequence`;
- `local_ts_ms` and `venue_ts_ms` (zero venue time means the venue did not
  provide one, not that the event occurred at epoch zero);
- monotonic `order_version` and `position_version`;
- mode, sleeve, strategy, trade, symbol, side and venue/order identifiers;
- a SHA-256 `prev_event_hash` / `event_hash` chain;
- optional immutable trade/order projection patches and TCA fields.

The journal append is serialized across processes, validates the existing hash
chain, rejects lifecycle skips and version regression, writes with `O_APPEND`,
and fsyncs the file and directory. Deterministic idempotency keys make duplicate
WebSocket deliveries and replay retries no-ops. Reusing an event ID with changed
content fails as attempted mutation.

## Lifecycle

All three operating modes use one reducer:

```text
decision
  -> risk_accepted
  -> submitted
  -> acknowledged
  -> fill
  -> protection_active
  -> exit_requested
  -> close_fill
  -> pnl_confirmed
```

Entry and close fills may repeat only with a strictly increasing position
version, representing partial fills. Supplemental facts—projection patches,
venue snapshots, rejections, WebSocket gaps, markouts, hedge delays and risk
shocks—cannot skip or replace a lifecycle transition.

`protection_active` means the configured exit/risk policy is active. It can be a
venue stop/TP or a deterministic strategy time/state exit; it does not claim a
venue stop exists when the selected profile is deliberately stopless.

## Venue Flat While P&L Lags

A private-WebSocket zero position, a proven side flip, Bybit's explicit
zero-position reduce-only rejection, or the operator reset workflow can prove
that a local leg is no longer live before Closed-PnL is attributable. The
journal then appends a `venue_snapshot` fact and projects the row as
`awaiting_pnl`:

- it is excluded from open exposure, stop evaluation and repeat close orders;
- it is excluded from recurring pending-orphan Telegram warnings;
- it remains eligible for Closed-PnL reconciliation;
- no close fill, price, fee or P&L is invented.

When attributable Closed-PnL arrives, normal replay appends
`exit_requested -> close_fill -> pnl_confirmed` and the projection becomes
closed.

## Transaction-Cost Analysis

Every venue execution—not merely each order-level aggregate—creates a TCA row
in `canonical_journal/tca.parquet`. Bybit `execId`, execution quantity, price,
fee and venue time are retained independently. Repeated REST/WS snapshots are
idempotent; cumulative partial-fill rows append only their positive quantity
delta.

Each row carries decision, submission and fill prices with source labels,
executed quote depth (labelled `executed_quote_notional`, not misrepresented as
a full order-book snapshot), submit-to-venue-execution latency with its source,
and 1/5/30-minute markout fields. Markouts have an explicit status:

- `pending`: the horizon has not yet been observed;
- `observed`: price and side-signed basis-point markout are present;
- `unavailable`: the source resolution cannot identify the horizon.

Positive markout basis points are favorable for both long and short fills.
Historical engines currently use hourly source bars, so their 1/5/30-minute
markouts are labelled unavailable instead of being fabricated. Demo/paper cycles
append the first causal price observation after each horizon and retain both the
target and observed timestamps.

## Rebuild And Verification

Use the operational command surface:

```bash
python -m liquidity_migration --data-root ROOT canonical-journal verify
python -m liquidity_migration --data-root ROOT canonical-journal rebuild
python -m liquidity_migration --data-root ROOT canonical-journal simulate-incidents
```

`rebuild` bootstraps pre-journal ledgers once, discovers registered projection
datasets from immutable events, and recreates them by replay. The VPS reset
script stops writers, proves the demo account flat, archives the generated
views and journal snapshot, retains the live journal, records the flat fact, and
rebuilds projections. It never deletes execution history to make a ledger look
flat.

## Deterministic Incident Contracts

`liquidity_migration.incident_simulation` executes these fixed-clock scenarios
through the production journal/reducer:

1. venue flat while ledger is open;
2. duplicate fills;
3. missing WebSocket events with REST recovery;
4. partial closes;
5. reduce-only rejection on a zero position;
6. changed minimum notional;
7. delayed hedge;
8. correlated squeeze at 10× leverage.

They test lifecycle/accounting behavior. They are not alpha, liquidation-price,
or market-impact evidence.
