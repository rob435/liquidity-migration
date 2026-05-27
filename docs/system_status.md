# System Status

Updated 2026-05-27.

The liquidity-migration short strategy is in **committed paper forward testing**
on the Bybit demo account. The canonical live configuration is the `promoted`
profile with `liquidity_migration_close_location_min = 0.30`.

## Research-evidence status (2026-05-27 reset)

The per-venue full-PIT research roots and every backtest report under them
were deleted on 2026-05-27. Previous drafts of this doc carried specific
return / drawdown / Sharpe / sweep / tribunal numbers sourced from those
reports; those numbers have been removed rather than left as misleading
citations. See `docs/research_findings.md` for the rebuild + re-validation
sequence required before new numbers can be cited.

The deletion does NOT affect:
- the engine code (`liquidity_migration/volume_events.py`,
  `liquidity_migration/long_native.py`, `liquidity_migration/strategy_tribunal.py`)
- the live VPS demo (its ledgers live on the VPS under
  `/opt/liquidity-migration/data/`, independent of the deleted local roots)
- the deployed `promoted` profile + parameters (defined in code, not in reports)

## Deployment status

- The Bybit demo (paper) forward test runs the `promoted` profile at
  `close_location_min = 0.30` on the Singapore VPS, with the concurrent-position
  cap overridden to **3** (`MAX_ACTIVE_SYMBOLS=3`) — a concentrated variant of
  the 5-position canonical research config. The systemd unit pins
  `STRATEGY_PROFILE=promoted` and the runner refuses `SUBMIT_ORDERS=1` for any
  other profile, so `promoted` is the single order-submitting demo stack;
  `demo_relaxed` and the other candidates are shadow/dry-run only. This is a
  demo-only paper forward test — not Model-Court validated, not a real-money
  promotion.
- The demo cycle runs in **match-the-backtest mode** as of 2026-05-26
  (commit `78df65a`): `UNIVERSE_RANK_END=0` and `UNIVERSE_MAX_SYMBOLS=0`
  disable the live-ticker pre-filter so every active Bybit USDT-perp
  (~750 symbols) feeds into daily aggregation. The strategy's
  `universe_rank_max` then applies on the resulting daily-bar
  `liquidity_rank`, exactly the same way the backtest does. Without
  this widening the demo and backtest could pick different symbols on
  the same signal date because the rank denominator differed (observed
  2026-05-26 with DRIFTUSDT: same data, demo entered, backtest
  rejected at `rank_improvement_min=150` because the prior7 rank was
  computed within a 400-symbol vs 568-symbol universe). The validator
  (`_validate_demo_config` / `_required_universe_rank_end`) accepts
  `0/0` as the explicit unlimited-universe opt-in; partial misconfigs
  (one zero, one positive) still trip the universe-too-narrow guard.
  To revert to the legacy narrow-universe demo (top-400 by ticker
  turnover, smaller kline store, but demo ≠ backtest), set both env
  vars to 400 in the systemd unit and rebuild.
- The long sleeve (`liquidity-migration-bybit-long-demo.service`) runs the
  `MultiStratV1` / v11a profile at `NOTIONAL_MULTIPLIER=10`,
  `ENTRY_LEVERAGE=10`, `MAX_NEW_ENTRIES_PER_CYCLE=5`, `UNIVERSE_SIZE=10`.
  This is the 10× notional sleeve referenced in the deployment combined-equity
  analyses.
- No real-money trading is active: demo is the default, and `demo=False` is
  refused unless real-money mode is deliberately armed (see below).

## Execution-path resilience (2026-05-26)

The demo loop is WS-first with REST as the safety net at every layer. Recent
hardening pass shipped these crash-/drift-durability invariants — none of them
change strategy or backtest output, but each closes a specific way the live
ledger could diverge from Bybit:

- **Preflight order rows.** Every `place_order` that submits to Bybit (both
  cycles, both sleeves, including the wsrisk reduce-only exit path and the
  limit-chase fallback-market) flushes a `status="submitted",
  submit_mode="preflight"` row to the orders parquet *before* the venue call.
  If the cycle dies between submission and the end-of-cycle ledger flush, the
  `orderLinkId` is already on disk and the next cycle's
  `_reconcile_pending_order_fills` adopts the actual fill from
  `get_trade_history`. Without this, a crash-window fill would orphan on the
  venue and the ledger would carry no trail.
- **Orphan-close PnL backfill.** When `_risk_reconcile_missing_positions`
  detects a Bybit position that has vanished but the ledger still says open,
  it queries `get_closed_pnl` (filtered by symbol + close-side +
  created-after-entry) and backfills `exit_price`, `gross_trade_return`,
  `net_return`, `exit_ts_ms`, and `exit_order_id` from the actual close,
  stamped with `submit_mode="orphan_reconciled"`. Falls back silently to the
  legacy zero-PnL row on any failure — backfill can never block the close.
- **Orphan-reconciler API-failure guard.** `_risk_reconcile_missing_positions`
  takes a `position_error` argument and bails out (no orphan-closes) when the
  upstream `get_positions` failed. Before this, a single transient REST
  failure made `position_by_symbol={}` and false-positive orphan-closed every
  open trade. The main demo cycle's `_reconcile_open_trades` already had this
  guard; the wsrisk path now matches it.
- **Cache schema-drift safety.** `PrivateStateCache` (positions/orders/wallet)
  and `TickerCache` now only bump `last_event_monotonic` when at least one row
  in a WS message was applied successfully. If a Bybit schema change causes
  every row to drop, the cache goes stale on its existing timer and the cycle
  falls back to REST — preventing the "silently-stuck cache, cycle reads
  forever-fresh state" failure mode.
- **Ticker-stream startup recovery.** A REST seed failure used to permanently
  disable the WS ticker feed (the stream is skipped when the cache is empty
  and never retried). The reconcile loop now retries
  `_open_ticker_stream()` after each successful re-seed, so the daemon
  recovers automatically instead of REST-falling-back for its full lifetime.
- **Kline-warmer alert.** The hourly kline cache warmer now tracks consecutive
  failures and sends a one-shot telegram when the streak hits 3, so a
  sustained outage is operator-visible before cycles start REST-bursting on
  every bar close. Streak resets on the first success, alert rearms.
- **Ledger write ordering.** End-of-cycle parquet writes flush the orders
  dataset BEFORE the trades dataset (both cycles, both sleeves). A crash
  between the two leaves the order ledger ahead of the trade ledger so the
  next-cycle `_reconcile_pending_order_fills` adopts the order and
  re-applies the trade-close. The reverse ordering would leave the trade
  marked closed with the order detail (fill price, order_id) permanently
  missing.
- **Long sleeve ticker-stream recovery.** The long-native daemon's
  `_reconcile_loop` now mirrors the short daemon's recovery-open: if the
  ticker stream is unset and the cache has populated symbols, the loop
  retries `_open_ticker_stream()`. Without this, a single REST seed failure
  at startup would permanently disable the long sleeve's WS ticker feed.
- **Sub-order split for venue-cap-bound entries (2026-05-27).** When the
  strategy's target entry qty exceeds Bybit's per-order `maxMktOrderQty`,
  the cycle now splits the entry into N = ceil(target/max) sequential
  sub-orders (each ≤ max, sharing the base `orderLinkId` with `-s0`,
  `-s1`, … suffixes). Previously the qty was capped-and-reduced, which
  silently under-sized live trades vs the backtest assumption of full
  target notional — observed live as REQUSDT entering at 53% of target
  notional. Stops/TP attach to the first sub only (Bybit stops are
  position-level, so one set covers the aggregated position). Aggregate
  fills land in a single trade row with volume-weighted entry_price;
  each sub gets its own order row for ledger audit. The split achieves
  backtest fidelity on capacity-constrained alts without losing trades
  to venue rejection.

These are mechanical / engineering hardening — none touch the signal, the
universe, or the parameters. Backtests and the `promoted` profile are
unchanged. The relevant test suites (`tests/test_liquidity_migration_event_demo.py`,
`tests/test_liquidity_migration_ws_state_cache.py`) cover each contract.

## Real-money path (built, demo by default)

A real-money (mainnet) execution path exists in the code. Which account the
private clients use is a plain `.env` toggle read by
`bybit.resolve_private_credentials()`:

- `REAL_MONEY=true` — mainnet keys (`BYBIT_REAL_API_KEY` /
  `BYBIT_REAL_API_SECRET`), real-money endpoint.
- `DEMO=true` or unset — demo keys (`BYBIT_DEMO_API_KEY` /
  `BYBIT_DEMO_API_SECRET`), demo endpoint.

Demo is the default: with neither flag set the clients stay on the demo
account, so the VPS demo is unaffected. `DEMO` and `REAL_MONEY` are mutually
exclusive — setting both true raises.

**The strategy is NOT validated for real money** — the historical tribunal
verdict was WATCH (not PASS) on supporting reports that have since been
deleted, the edge is IS-era / regime-conditional, and there is no live-fill
track record. The repository ships with the toggle on demo.
