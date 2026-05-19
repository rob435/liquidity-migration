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
with `--daemon --interval-seconds $INTERVAL_SECONDS`, replacing the bash loop.
Default is the legacy bash-loop runner — flipping is a one-line env-var
change to the systemd unit:

```ini
# /etc/systemd/system/liquidity-migration-bybit-demo.service
[Service]
Environment=INTERVAL_SECONDS=60
Environment=USE_DAEMON=1                       # <-- add this line
ExecStart=/opt/liquidity-migration/scripts/run_bybit_demo_event_engine.sh
```

Then `systemctl daemon-reload && systemctl restart liquidity-migration-bybit-demo.service`.
Rollback is `Environment=USE_DAEMON=0` (or remove the line) and the same
two-command restart.

All other env vars (`STRATEGY_PROFILE`, `INTERVAL_SECONDS`, `WORKERS`,
`SUBMIT_ORDERS`, etc.) work identically in both modes.

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
loop consults between cycles. The current cycle is allowed to finish so
no place_order is interrupted mid-flight. Worst case is one full
`interval_seconds + max_cycle_seconds` to exit.

**The risk service still runs as a separate process** (`liquidity-migration-bybit-risk`).
That side already had its own WS connection for executions and is unaffected
by this change. Both services authenticate with the same demo API key, so the
private REST rate budget is shared — the demo daemon uses
`BybitPrivateRateLimiter` with a conservative 15 req/s (env
`BYBIT_PRIVATE_REST_RATE_LIMIT_PER_SECOND`) to leave headroom.

## What's NOT yet in this change

- **WS-driven fill confirmation for the risk engine's reduce-only exits.**
  The risk engine already has its own WS execution subscription for
  reconciliation but still submits orders via REST and polls REST for fill.
  Same wiring would apply but is a separate refactor.
- **Cross-process router sharing.** Each service has its own router; events
  for orders submitted by the demo are not visible to the risk service and
  vice versa. Acceptable today because the two sides write to separate
  order-link-id prefixes (`lm-en-*` vs `lm-ux-*`/`lm-ex-*`), so neither
  service would consume the other's WS events anyway.
- **WS reconnect telemetry.** We rely on pybit's auto-reconnect and don't
  surface a counter yet. If WS instability becomes a concern, add a hook in
  `EventDemoDaemon._handle_execution_message` to record gaps.

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
