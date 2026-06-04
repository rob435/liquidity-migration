# event-demo-daemon (opt-in long-running entry path)

The legacy demo runner is a bash loop that wakes a fresh Python process every
`INTERVAL_SECONDS`, runs one cycle, and exits. Fill confirmation goes through
`get_trade_history` REST polling — typically the slowest stage in the entry
path.

`--daemon` mode keeps one Python process up, subscribes to the Bybit private
execution WebSocket once at startup, and routes every venue-pushed execution
event through `ExecutionEventRouter`. Cycle code's `_wait_for_execution_summary`
prefers the router's WS event over a REST poll. Expected per-fill confirmation
latency drops from ~100-300 ms (REST best-case) to <30 ms (WS push). REST
remains the fallback if WS hasn't delivered within the existing
`order_fill_confirm_seconds` budget.

## Running it

The runner script `scripts/run_bybit_demo_event_engine.sh` has a `USE_DAEMON`
toggle. When `USE_DAEMON=1`, it `exec`s a single long-running Python process
with `--daemon --interval-seconds $INTERVAL_SECONDS`. **Daemon mode is the
deployed default** — `deploy/systemd/liquidity-migration-bybit-demo.service`
ships with `Environment=USE_DAEMON=1`. The bash-loop runner remains as a
diagnostic fallback; to fall back, set `Environment=USE_DAEMON=0` on the
systemd unit and run `systemctl daemon-reload && systemctl restart
liquidity-migration-bybit-demo.service`.

All other env vars (`STRATEGY_PROFILE`, `INTERVAL_SECONDS`, `WORKERS`,
`SUBMIT_ORDERS`, etc.) work identically in both modes.

## Event-driven cycle (current default)

The daemon no longer runs on a fixed wall-clock timer. It is **WS-event-driven**:
`KlineStreamManager._on_bar` sets a cycle-wake `Event` on each new *confirmed*
kline-bar boundary (the ~566-symbol hour-close burst is coalesced into one wake
by gating on the boundary-advance high-water mark). The run loop waits on that
event with a small min-cycle debounce floor and an `interval_seconds` **heartbeat**
as the no-data fallback, plus a shutdown-wake. Telemetry distinguishes
`cycles_kline_triggered` (woke on a bar) from `cycles_timer_triggered` (heartbeat).
Set `--no-event-driven-cycle` to fall back to the pure timer loop. `interval_seconds`
is now the heartbeat ceiling, not the primary clock.

> **Cadence note (direction).** Bars are currently HOURLY and the signal acts on
> the daily-close roll, so a new actionable *entry* still appears at most once per
> UTC day regardless of how fast the loop wakes. The reaction-latency ceiling is set
> by the *signal cadence*, not the runtime. Moving to sub-hourly reaction is the
> Architecture-B / C-phase + R12 sniper work (rolling-window features, finer bars) —
> see STATE.md. The runtime layer is already event-driven and ready for it.

**SIGTERM drains promptly.** `request_shutdown()` also sets the cycle-wake event, so
a `systemctl stop` returns within the current cycle rather than blocking up to the
heartbeat; the in-flight cycle is allowed to finish so no `place_order` is interrupted.

## Safety boundaries

Read these carefully before flipping the systemd unit.

**REST fallback is always active.** Every place_order is followed by a wait
that first asks the router for a WS event (short blocking wait, ~50-200 ms
depending on the existing fast/slow poll window) and falls back to
`get_trade_history` polling if the router is empty. WS is never the only
source of truth.

**On WS disconnect we drop all buffered events.** Any in-flight order whose
fill happens during the disconnect window will be confirmed via REST on the
next iteration — same behavior as today, just slower for that one order.
Reconnection is delegated to `pybit` (auto-reconnects on its own thread).

**Single cycle failure does not kill the daemon.** Exceptions inside a cycle
are caught, logged, and the loop continues. Repeated failures will show up in
journal logs as `cycle failed: ...` lines and bump the `cycle_errors`
counter in the shutdown summary.

**SIGTERM drains gracefully.** A `systemctl stop` flips a threading.Event the
loop consults between cycles, and `request_shutdown()` also sets the cycle-wake
event so the event-driven wait returns immediately rather than blocking up to the
heartbeat. The current cycle is allowed to finish so no place_order is interrupted
mid-flight. Worst case is roughly one `max_cycle_seconds` (the in-flight cycle), not
a full heartbeat interval.

**The risk service still runs as a separate process** (`liquidity-migration-bybit-risk`).
That side already had its own WS connection for executions and is unaffected
by this change. Both services authenticate with the same demo API key, so the
private REST rate budget is shared — the demo daemon uses
`BybitPrivateRateLimiter` with a conservative 15 req/s (env
`BYBIT_PRIVATE_REST_RATE_LIMIT_PER_SECOND`) to leave headroom.

## Design notes

- **WS-driven fill confirmation covers the risk engine's reduce-only exits.**
  The risk engine submits tracked exits through the WebSocket trade path
  (`ws_exit`) and confirms fills from its private execution + order streams
  (`on_execution_message` → `record_tracked_exit_stream_fill`); REST polling
  remains the fallback. Leftover positions with no ledger trade are *adopted*
  as tracked trades (`adopt_untracked_positions`) rather than flattened, so
  they are managed and exited through that same WS-confirmed path.
- **Cross-process router sharing is intentionally not implemented.** The demo
  and risk services each own a router and a WS subscription. They write
  disjoint order-link-id prefixes (`lm-en-*` for entries vs `lm-ux-*` /
  `lm-ex-*` for exits) and each consumes only its own events, and each already
  sees its own fills on its own subscription. Sharing a router across the two
  processes would add inter-process plumbing for no functional gain, so it is
  deliberately left out — not a pending TODO.
- **WS gap telemetry.** `EventDemoDaemon._record_ws_event` tracks inter-event
  gaps on the execution stream — pybit reconnects transparently, so a long
  silence followed by a resumed event is the only observable symptom of a
  dropped connection. Gaps beyond `ws_gap_threshold_seconds` (default 120s)
  are counted and logged; `ws_gap_count` and `ws_max_gap_seconds` appear in the
  shutdown summary and the `run()` stats dict. A long gap in a quiet market is
  normal, so the counter is a coarse signal, not a definitive disconnect count.
- **WS state caches** (`liquidity_migration/ws_state_cache.py`).
  `PrivateStateCache` (positions / open orders / wallet) and `TickerCache`
  are fed by the same private + public WS streams the daemon opens for
  executions. Cycles read `_collect_private_snapshots` from the caches when
  fresh (default `state_cache_stale_seconds=120`, reconciled every 60s by
  the daemon's `_reconcile_loop`) and fall back to REST on a stale or empty
  cache. The fallback is silent and per-call, so the cycle never blocks on
  a degraded WS.
- **Schema-drift safety.** Each `on_*_event` only bumps the cache's
  `last_event_monotonic` when at least one row in the message applied
  successfully. A Bybit schema change that drops every row no longer leaves
  the cache reporting "fresh" forever — the existing stale check engages
  REST fallback automatically.
- **Ticker-stream startup recovery.** If the REST seed has no symbols at
  daemon startup, the ticker WS subscribe is skipped (can't subscribe to
  nothing). The reconcile loop retries `_open_ticker_stream()` after each
  successful re-seed, so the daemon recovers from a transient startup
  failure instead of permanently REST-falling-back.
- **Crash-durability preflight.** Both cycles write the order row to parquet
  with `submit_mode="preflight"` BEFORE calling `place_order`. A crash
  between submission and the end-of-cycle ledger flush still leaves the
  `orderLinkId` discoverable for the next cycle's
  `_reconcile_pending_order_fills` to adopt the actual fill. Applies to
  short entries + short exits + wsrisk reduce-only exits + long entries
  + long exits.
- **Orphan-close PnL backfill.** When the reconciler detects a Bybit
  position that has vanished but the ledger still says open, it queries
  `get_closed_pnl` and backfills `exit_price`, `gross_trade_return`,
  `net_return`, `exit_ts_ms`, and `exit_order_id` from the actual close.
  Stamped `submit_mode="orphan_reconciled"`. Falls back silently to the
  legacy zero-PnL row when the endpoint, network, or matching record is
  unavailable.
- **Kline-warmer alert.** Consecutive warmer failures (default ≥3 in a row)
  trigger a one-shot telegram so a sustained outage is operator-visible
  before cycles start REST-bursting on every bar close. Streak resets on
  the first success.

### Crash-/drift-durability hardening (2026-05-26/27)

Additional ledger-fidelity invariants (none change strategy or backtest output;
each closes a specific way the live ledger could diverge from Bybit; covered by
`tests/test_liquidity_migration_event_demo*.py` + `tests/test_liquidity_migration_ws_state_cache.py`):

- **Orphan-reconciler API-failure guard.** `_risk_reconcile_missing_positions`
  takes a `position_error` and bails out (no orphan-closes) when the upstream
  `get_positions` failed — a single transient REST failure must not
  false-positive orphan-close every open trade. The main cycle's
  `_reconcile_open_trades` already had this guard; the wsrisk path now matches it.
- **Ledger write ordering.** End-of-cycle parquet writes flush the orders
  dataset BEFORE the trades dataset (both cycles, both sleeves), so a crash
  between the two leaves the order ledger ahead of the trade ledger and the
  next cycle's `_reconcile_pending_order_fills` can re-apply the trade-close.
- **Sub-order split for venue-cap-bound entries/exits.** When the target qty
  exceeds Bybit's per-order `maxMktOrderQty`, the entry/exit splits into
  N = ceil(target/max) sequential sub-orders sharing the base `orderLinkId`
  (`-s0`/`-s1`/… suffixes) across all three exit engines; trade rows persist
  `max_market_order_qty` so the close path can read it. Preserves backtest
  notional fidelity on capacity-constrained alts instead of silently under-sizing.
- **Closed-PnL backfill on flat-position trade close.** A pending reduce-only
  exit with `avg_price=0` that later goes flat under its own stop falls back to
  the `_orphan_close_pnl_backfill` path (Bybit closed-PnL endpoint) instead of
  closing with `exit_price=0`.
- **Cross-process exit-submission lease.** `submit_exit` in ws_risk re-reads the
  orders parquet immediately before submitting, closing the window where the demo
  cycle and the ws-risk daemon could double-submit the same reduce-only exit.
- **Ledger uPnL matches Bybit position uPnL.** `build_ledger_position_pnl_snapshot`
  prefers the position payload's own `markPrice` for open symbols, so ledger uPnL
  matches Bybit's by construction (was ticker `mark_price`, drifted on illiquid alts).
- **Long-sleeve ticker-stream recovery.** The long-native daemon's `_reconcile_loop`
  mirrors the short daemon's recovery-open, retrying `_open_ticker_stream()` so a
  startup REST-seed failure doesn't permanently disable the long sleeve's WS feed.

## Shadow-testing checklist

Before flipping the systemd `ExecStart`:

1. Run the daemon manually on the VPS as a foreground process with `--daemon`
   and your usual flags. Verify journal output:
   ```
   event_demo_daemon starting data_root=... interval_seconds=60.0 ...
   event demo cycle mode=submit ... elapsed=Xs slowest=...
   ```
2. Force a fill (entry candidate present, or simulate a candidate that
   passes filters). Verify the `wait_for_execution_summary` returns in <50ms
   when the WS event arrives, by watching the `slowest=...` field in the
   cycle summary — the `entries` stage should be markedly faster than under
   the bash-loop runner.
3. Run for 1 hour. Check that the `router_stats` log line at shutdown shows
   `events_received > 0` and `waits_satisfied_by_ws > 0`. If WS isn't
   delivering events at all, the daemon will silently fall back to REST —
   functional but no speedup.
4. Confirm `systemctl stop` exits within a few seconds (drains current
   cycle, doesn't hit the 90s default kill timeout).
5. Only then update the systemd `ExecStart`.

## Resetting the demo/paper ledgers (post-overhaul clean slate)

After a strategy overhaul (e.g. the 2026-05-30 `drop_all_4` SHORT + `div` LONG
promotions) the accumulated forward ledgers belong to the *old* config. To
restart the forward demo/paper run — and the Tier-3 30-day clock — from a clean
slate, archive + wipe the trading ledgers with
`scripts/reset_demo_paper_ledgers.sh`. It only touches the
`event_demo_*` / `long_native_demo_*` / `long_native_paper_*` trade/order/cycle
datasets across the four roots (`data/bybit-demo-event`, `data/bybit-paper-event`,
`data/bybit-long-demo-event`, `data/bybit-long-paper-event`); the WS kline
stores and everything else are preserved so there is no slow re-bootstrap. Every
wiped dataset is tar.gz'd to `data/_archive/ledger-reset-<ts>.tar.gz` first, so
the reset is auditable and reversible.

This is a DATA operation on the VPS (116.202.15.128); CI does not run it. Run it
in a maintenance window, as root, from `/opt/liquidity-migration`:

```bash
# 0. (Recommended) FLATTEN any open positions on the Bybit DEMO account first
#    (Bybit demo UI or API). A ledger wipe with positions still open on Bybit
#    leaves them untracked (the fail-closed orphan logic won't manage a position
#    that has no ledger trade). Paper places no real orders, so it has none.
# 1. Stop the daemons + timers so nothing writes mid-reset:
systemctl stop \
  liquidity-migration-bybit-demo liquidity-migration-bybit-paper \
  liquidity-migration-bybit-risk liquidity-migration-bybit-long-demo \
  liquidity-migration-bybit-long-paper \
  liquidity-migration-demo-health.timer liquidity-migration-combined-book-report.timer
# 2. Preview, then run the reset:
scripts/reset_demo_paper_ledgers.sh --dry-run
scripts/reset_demo_paper_ledgers.sh
# 3. Restart — the daemons recreate the emptied datasets on their next cycle:
systemctl start \
  liquidity-migration-bybit-demo liquidity-migration-bybit-paper \
  liquidity-migration-bybit-risk liquidity-migration-bybit-long-demo \
  liquidity-migration-bybit-long-paper \
  liquidity-migration-demo-health.timer liquidity-migration-combined-book-report.timer
```

Deploy the code first (so the daemons restart on the fixed engine), then run the
reset — the order doesn't matter for correctness since the reset wipes whatever
has accumulated, but doing the reset last means the first post-reset cycles are
already on the fixed code.

## Continuous-fade sleeve (4th sleeve — sub-hourly, ticker-driven) — LIVE on demo (operator-directed 2026-06-01)

A separate forward-demo sleeve for the continuous liquidity-migration fade (`continuous_demo.py`,
`continuous_demo_daemon.py`, CLI `continuous-event-demo-cycle --daemon`, unit
`liquidity-migration-bybit-continuous-demo.service`). It is fully isolated from short/long: data root
`data/bybit-continuous-demo-event`, datasets `continuous_fade_demo_{trades,orders,cycles}`, orderLinkId
prefix `lm-en-c-` / `lm-ux-c-` (the extended `ws_risk` `decode_entry_order_link_id` routes its fills).
It reuses the same WS plumbing (kline pool + `TickerCache` + `PrivateStateCache` + `ExecutionEventRouter`)
via a thin subclass of the long daemon.

- **"No 1h":** the cross-sectional decile is recomputed off the live `TickerCache` price every
  `INTERVAL_SECONDS` (default 60s) heartbeat, so a name entering/leaving the top fade decile is acted on
  within ~60s — not gated on the hourly bar close. The trailing rolling features still come from the
  confirmed-1h store (a 168h vol needs hourly history); only the reaction is sub-hourly.
- **Signal == backtest:** the live decile uses the shared `compute_continuous_decile_panel`, proven
  bit-identical to the verified backtest (equivalence test). State-exit: short fresh rmom-gated D9 (liquid
  ≥$500k/h), cover when it leaves D9 or at max-hold; resting stop + the `ws_risk` intrabar path handle stops.
- **rmom dependency:** the signal gates on `data/bybit-continuous-demo-event/residual_momentum.parquet`.
  The `continuous-rmom-refresh.timer` rebuilds it daily (00:20 UTC) from the sleeve's own kline store.
  No rmom file ⇒ the daemon runs but emits **no** signal (fail-safe), so the cold-start is signal-quiet
  until the store has history and the first refresh runs.
- **Memory:** the kline manager is scoped to the top-250 by 24h turnover (the liquid cross-section it
  trades), not the full ~570 — the full store blew the long sleeve's 1G cap. `MemoryMax=4G`.

### Shared-account safety (three short-direction sleeves, one netted demo account)

Short, long, and continuous all trade ONE Bybit demo account (one-way / netted position mode). The
isolation that makes this safe:

- **One reconcile authority.** A single `ws_risk` service reads ALL THREE ledger roots (`DATA_ROOT` +
  `LONG_DATA_ROOT` + `CONTINUOUS_DATA_ROOT`), tags every row with its `sleeve`, and routes each
  write back to that sleeve's ledger (`_write_*_rows_routed`). So a continuous position is *tracked*
  (never flattened as untracked), and a continuous orphan (server-side disaster stop fired) is closed —
  with the real venue PnL backfilled from `get_closed_pnl` — into the **continuous** ledger, not short's.
  The continuous cycle, like the long cycle, does NOT run its own orphan-close (that would race the risk
  service); it only writes its own *planned* exits (left-decile / breakeven / failed-fade / max-hold).
- **Account-wide same-symbol exclusion (Rule A).** Each sleeve's cycle skips entry on any symbol that
  already has a live account position or pending entry order (account-wide, not sleeve-scoped), so two
  sleeves never both hold the same symbol — the netted account stays effectively per-sleeve-disjoint.
  Entries are also blocked entirely on a position-fetch error (no empty-fetch-as-flat false entry).
- **Hard-fail wiring.** `run_bybit_demo_ws_risk_engine.sh` refuses to start with
  `EXIT_UNTRACKED_POSITIONS=1` unless BOTH `LONG_DATA_ROOT` and `CONTINUOUS_DATA_ROOT` are set, and
  `deploy_vps_live.sh` verify asserts the risk unit carries both roots — so a stale unit can't silently
  leave a sleeve's positions exposed to flattening. The deploy also restarts the risk service BEFORE the
  continuous daemon, so the tracker is up before trading starts.

**Live (operator-directed go-live 2026-06-01):** the unit ships `SUBMIT_ORDERS=1` /
`CONFIRM_DEMO_ORDERS=1` — it submits demo orders on deploy. To pause without removing the sleeve, set
`SUBMIT_ORDERS=0` and `systemctl restart liquidity-migration-bybit-continuous-demo`. Cold start is
signal-quiet until the first `residual_momentum.parquet` exists (the `continuous-rmom-refresh` timer,
enabled `--now` on deploy, builds it). The refresh defaults `--end` to **tomorrow (UTC)** so it keeps
`residual_momentum[today]` fresh on every daily run — a stale table (e.g. the old hardcoded
`END=2026-05-28`) would silently empty the live decile (the `is_not_null` join drops every symbol). The
liveness watchdog pages on an rmom table whose max day is stale, and the cycle telemetry surfaces
`max_rmom_day_ts` (not just a `rmom_present` boolean). Add `data/bybit-continuous-demo-event` to
`reset_demo_paper_ledgers.sh` to include it in the Tier-3-clock reset set. Demo account ONLY — never
`REAL_MONEY`.
