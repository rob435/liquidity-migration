# WebSocket Kline Streaming — Handoff Plan

**Owner:** next agent / engineer
**Date drafted:** 2026-05-24
**Status:** plan only, not yet built

---

## Why this exists

The deployed cross-sectional momentum strategy operates on daily bars. The signal fires at daily-bar close (00:00 UTC); the backtest assumes entries fill at signal_ts + 1h. In live, the kline-fetch path is reactive REST inside the cycle:

```
00:00 UTC      yesterday's daily bar closes (last 1h kline appears on Bybit)
00:00-04:38    cycle's _download_recent_1h_klines REST-fetches the new bar
               for each of 400 universe symbols, then _build_demo_features
               computes the per-symbol features
04:38 UTC      features ready; events_pipeline reports 42 signals
04:38+         entries fire — but ready_ts (01:00) is now 218min in the past
```

So `MAX_ENTRY_LAG_MINUTES` had to be loosened to 360min just to let entries fire at all. The result is entries 3-4h late vs the backtest's T+1h fills, which trades away most of the post-pump reversion alpha. This is the strategy's architectural Achilles heel and the only fix is to stop fetching reactively.

**Outcome of this work:** Entries fire within ~60s of `ready_ts` (vs current ~4h). Tighten `MAX_ENTRY_LAG_MINUTES` back to ~15min after this lands.

---

## Architecture

Three new components + one integration point. Subscribe to **every** USDT-perp symbol on Bybit, not just the current top 400 — the universe selection (top-N by turnover) stays in `_build_demo_universe`, which then queries a fully-fresh in-memory kline store. This avoids the "universe-change blindness" failure mode where a symbol jumps into the top 400 today but has no klines because we weren't subscribed.

### Component 1 — `KlineStore` (new module: `liquidity_migration/kline_store.py`)

Thread-safe in-memory bars-by-symbol, bounded to N days, periodic disk flush.

```python
class KlineStore:
    """In-memory 1h klines per symbol. Thread-safe append + read.

    Bounded by retain_days (default 90). Background flush thread writes to
    parquet cache periodically so a restart doesn't lose history."""

    def __init__(self, *, cache_root: Path, retain_days: int = 90,
                 flush_interval_seconds: float = 30.0) -> None: ...

    # WS callback path — called from pybit's WS thread
    def add_bar(self, symbol: str, bar: dict[str, Any], *, confirmed: bool) -> None:
        """Add a 1h bar. Skip if not confirmed (still forming).
        Idempotent on (symbol, ts_ms) — overwrites if same key."""

    # Cycle read path — called from cycle thread
    def get_klines(self, symbols: list[str], *, start_ms: int, end_ms: int) -> pl.DataFrame:
        """Return klines in (symbol, ts_ms) rectangular form, matches the
        shape of _download_recent_1h_klines output exactly so the integration
        is a drop-in replacement."""

    def bootstrap_symbol(self, symbol: str, bars: pl.DataFrame) -> None:
        """One-shot historical fill from REST at startup."""

    def symbols_with_coverage_through(self, ts_ms: int) -> set[str]:
        """For cycle to know which symbols are usable (have data up to ts_ms).
        Cycle falls back to REST for symbols outside this set."""

    def stats(self) -> dict[str, int]:
        """rows, symbols, oldest_ts, newest_ts, bytes_in_memory."""
```

**Key invariants:**
- Single `threading.RLock` guards both adds and reads (single-writer model)
- Per-symbol deque or sorted list keyed by `ts_ms`; evict bars older than `retain_days` on every add
- Background flush thread serializes the whole store to `<cache_root>/.cache/ws_klines/store.parquet` every 30s
- On startup: read flush file to recover state, then live WS catches up. Bootstrap fills any remaining gap.

### Component 2 — `BybitKlineStreamPool` (extend `liquidity_migration/bybit.py`)

Multi-connection WebSocket pool. Splits subscription across connections, handles reconnect + resubscribe.

```python
@dataclass(slots=True)
class BybitKlineStreamPool:
    """Manages N pybit WS connections, each holding a slice of the symbol
    universe. On disconnect, reconnects and re-subscribes its slice."""

    category: str = "linear"
    testnet: bool = False
    demo: bool = False
    interval_minutes: int = 60
    topics_per_connection: int = 180  # conservative — Bybit allows ~200/conn

    def subscribe(self, symbols: list[str], callback: Callable[[dict], None]) -> None:
        """Partition symbols across ceil(len(symbols) / topics_per_connection)
        connections. Start a WS thread per connection. Callback gets each bar
        event (must inspect the `confirm` flag in the message)."""

    def update_subscriptions(self, new_symbols: set[str]) -> None:
        """Diff against current — subscribe to new symbols, unsubscribe
        from removed. Called by manager when universe refresh detects
        listings/delistings."""

    def close(self) -> None: ...

    def stats(self) -> dict[str, Any]:
        """connections, subscribed_topics, last_message_ts_per_conn,
        reconnect_count, dropped_messages."""
```

**Connection management:**
- For ~673 symbols at 180/connection → 4 WS connections
- Each connection runs on its own thread (pybit's `WebSocket()` handles this)
- pybit's `kline_stream(interval, symbol=list, callback)` accepts a list — pass each connection its slice
- On disconnect (pybit emits via callback or thread death), tear down the connection and rebuild with the same slice
- Track `last_message_ts` per connection; if stale > 30s, log warning and force reconnect

**Bybit V5 WebSocket constraints to respect:**
- Public connections: 500 per IP per 5min (rate limit on connection attempts only)
- Args per subscription message: max 10 (but multiple subscribe messages allowed per connection)
- Topic format: `kline.{interval}.{SYMBOL}` — e.g. `kline.60.BTCUSDT`
- The `confirm` field on each event is the gate: only act on `confirm=True` (bar fully closed)

### Component 3 — `KlineStreamManager` (new module: `liquidity_migration/kline_stream_manager.py`)

The orchestrator. Owns the store + pool + bootstrap + universe-refresh.

```python
class KlineStreamManager:
    """Wires KlineStore + BybitKlineStreamPool.

    Lifecycle:
      1. start() loads the current USDT-perp universe via REST (instruments
         + tickers), bootstraps 45 days of history per symbol via parallel
         REST, then subscribes the WS pool.
      2. Background universe-refresh thread polls instruments every 1h —
         subscribes to new listings, unsubscribes from delisted symbols.
      3. WS callbacks land in the pool, which forwards to the store via
         add_bar(symbol, bar, confirmed=msg['confirm']).
      4. Cycle's _download_recent_1h_klines queries the store; falls back
         to REST only for symbols not yet bootstrapped.
      5. stop() closes the pool and stops the background threads cleanly."""

    def __init__(
        self, *,
        market_data: BybitMarketData,
        cache_root: Path,
        lookback_days: int = 45,
        bootstrap_workers: int = 16,
        universe_refresh_interval_seconds: float = 3600.0,
    ) -> None: ...

    def start(self) -> None:
        """Block until bootstrap completes for >= 95% of symbols.
        After that, return — WS keeps catching up in the background."""

    def stop(self) -> None: ...

    def store(self) -> KlineStore: ...

    def stats(self) -> dict[str, Any]: ...
```

**Bootstrap behavior:**
- Listing every USDT-perp via `get_instruments_info` (filter `status == "Trading"`, `settle_coin == "USDT"`, `quoteCoin == "USDT"`)
- For each symbol, parallel REST fetch the last `lookback_days * 24` 1h klines
- Use `ThreadPoolExecutor(max_workers=bootstrap_workers)`, respect existing `BybitRestRateLimiter` (`max_requests=18, per_seconds=1.0`)
- Bootstrap should take ~8-15 minutes for 673 symbols × 1 REST call each
- Skip bootstrap for symbols already present in the recovered flush file with coverage through `now - 1h`

**Universe refresh:**
- Every 1h, re-fetch instruments + tickers
- Compute new set of `Trading + USDT + USDT` symbols
- Call `pool.update_subscriptions(new_set)`
- Bootstrap any newly-added symbol (one REST call each — fast)

### Integration point — modify `_download_recent_1h_klines` in `event_demo.py`

Currently this function is REST-only. Change it to:

```python
def _download_recent_1h_klines(symbols, *, start_ms, end_ms, config,
                               workers, market_client, cache_root,
                               kline_store=None):  # NEW arg, optional
    if kline_store is not None:
        # Try the store first.
        cached = kline_store.get_klines(symbols, start_ms=start_ms, end_ms=end_ms)
        covered = kline_store.symbols_with_coverage_through(end_ms)
        missing = [s for s in symbols if s not in covered]
        if not missing:
            return cached, _stats_from_store(cached)
        # REST fallback only for symbols not yet in store.
        rest, rest_stats = _legacy_rest_download(missing, ...)
        return pl.concat([cached, rest]), _merge_stats(...)
    return _legacy_rest_download(symbols, ...)
```

Pass `kline_store` through from the daemon. Cycle code path is otherwise unchanged.

### Daemon integration

Both `event_demo_daemon.py` and `long_native_event_demo_daemon.py`:
- On startup, instantiate `KlineStreamManager` and call `.start()` (blocks until bootstrap >= 95% complete)
- Pass the manager's `.store()` into each cycle call
- On shutdown, call `manager.stop()` before the daemon exits
- Add stats to the cycle's telemetry: `kline_store_symbols`, `kline_store_rows`, `kline_store_newest_ts_lag_seconds`

Existing `warm_demo_kline_cache` becomes redundant; remove it from the daemon's warmer loop once WS is proven stable.

---

## Memory + capacity budget

- 673 symbols × 24 bars/day × 90 days × ~50 bytes/bar ≈ **73MB**
- 4 WS connections, each idle-low CPU, occasional bar dispatch
- Bootstrap traffic: 673 × 1 REST call ≈ 8-15 minutes at the existing rate-limiter cap
- Cycle latency post-bootstrap: ~10-50ms (in-memory dict lookup + dataframe construction) vs current 200-5000ms REST fetch
- VPS already has 4GB RAM and current services peak at ~320MB — plenty of headroom

---

## Failure modes + mitigations

| Failure | Detection | Mitigation |
|---|---|---|
| WS connection drops | `last_message_ts` stale > 30s | Tear down + reconnect that pool slice; resubscribe topics |
| All connections down | All `last_message_ts` stale > 60s | Log ERROR, fall back to REST in cycle (existing path), keep retrying WS |
| Bootstrap fails for a symbol | REST returns empty | Skip that symbol from store; cycle's REST fallback handles it; retry on universe refresh |
| Store grows unbounded | `retain_days` eviction broken | Add periodic assertion in flush thread that newest-minus-oldest <= retain_days × MS_PER_DAY |
| Confirmed bar arrives twice | `add_bar` idempotency | Store keyed by (symbol, ts_ms); overwrites are no-ops on identical data |
| WS subscription rate-limit (500 connects / 5min / IP) | pybit raises on connect | Pool builds connections sequentially with 100ms spacing; rebuild on reconnect waits 5s |
| New symbol listed mid-day | Universe refresh thread | Hourly refresh diff catches it; symbol available after one bootstrap REST call |

---

## Implementation milestones

Each milestone is independently deployable. Don't ship anything until tests pass.

1. **`KlineStore` + tests** (2h) — pure logic, no I/O, no WS. Tests cover thread-safety, eviction, idempotent adds, get-by-range, bootstrap, persistence round-trip.
2. **`BybitKlineStreamPool` basics + tests** (3h) — single connection, subscribe to N symbols, callback fires on bar. Use pybit's WebSocket; test with a mock `WebSocket` that injects bars.
3. **Pool partitioning + reconnect + tests** (2h) — split across multiple connections, detect stale connection, rebuild. Test the reconnect path with a connection-drop simulator.
4. **`KlineStreamManager` + tests** (2h) — bootstrap (with mocked REST), start/stop, universe refresh diff. Bootstrap-completion threshold gate.
5. **Integration: `_download_recent_1h_klines` accepts optional store** (1h) — drop-in change, tests verify store-hit fast path and REST fallback.
6. **Daemon wiring** (1h) — both demo daemons start manager on init, pass store to cycles, expose store stats in cycle telemetry.
7. **Local end-to-end test** (1h) — run manager against live Bybit, watch store fill, verify cycle latency drops.
8. **Deploy to VPS + verify** (1h) — `MAX_ENTRY_LAG_MINUTES` stays at 360 for the first day; once verified, tighten to 60 or 30.
9. **Cleanup**: remove `warm_demo_kline_cache` from daemon's warmer loop; delete unused REST cache paths if any.

**Total estimate:** 13-15 hours of focused work for one experienced engineer.

---

## Configuration surface

Add to `EventDemoCycleConfig` and `LongNativeDemoCycleConfig`:

```python
ws_klines_enabled: bool = True            # master switch
ws_klines_bootstrap_workers: int = 16
ws_klines_lookback_days: int = 45
ws_klines_universe_refresh_seconds: float = 3600.0
ws_klines_topics_per_connection: int = 180
ws_klines_stale_warning_seconds: float = 60.0
```

Add env vars to the bash runners (`run_bybit_demo_event_engine.sh`, `run_bybit_long_demo_event_engine.sh`):

```bash
WS_KLINES_ENABLED="${WS_KLINES_ENABLED:-1}"
# ... pass through as --ws-klines-* CLI flags
```

Add `Environment=WS_KLINES_ENABLED=1` to all four service files. Keep a kill-switch (set to 0) so we can quickly revert to REST-only if the WS path breaks in production.

---

## Verification checklist

After deploy, verify in order:

- [ ] `journalctl` shows `ws_klines bootstrap complete: 673/673 symbols, 1.4M bars, took 12m`
- [ ] Cycle log shows `kline_store_symbols=673 kline_store_newest_ts_lag_seconds<30`
- [ ] At next 00:00 UTC: cycle at 00:01 shows `events_pipeline.final > 0` (features ready immediately, not 4h late)
- [ ] At 01:01 UTC: cycle fires entry on the fresh signal — `entries_executed >= 1`
- [ ] Watchdog at 02:15 UTC reports healthy entries in last 1h
- [ ] Tighten `MAX_ENTRY_LAG_MINUTES` to 60, redeploy, verify entries still fire next day
- [ ] After a week of clean operation, tighten further to 15 (matches original strict gate)

---

## Tests required

`tests/test_liquidity_migration_kline_store.py`:
- `test_add_bar_idempotent_on_same_ts`
- `test_add_bar_rejects_unconfirmed`
- `test_eviction_drops_bars_past_retain_days`
- `test_get_klines_returns_correct_window`
- `test_get_klines_empty_for_missing_symbol`
- `test_symbols_with_coverage_through`
- `test_concurrent_add_and_get_thread_safety` (pytest with 2 threads + barrier)
- `test_flush_and_recover_round_trip`

`tests/test_liquidity_migration_kline_stream_pool.py` (with mocked pybit WebSocket):
- `test_subscribe_partitions_across_connections`
- `test_callback_fires_on_confirmed_bar`
- `test_callback_skips_unconfirmed_bar`
- `test_reconnect_resubscribes_slice`
- `test_update_subscriptions_diff_adds_and_removes`

`tests/test_liquidity_migration_kline_stream_manager.py`:
- `test_bootstrap_blocks_until_threshold_reached`
- `test_universe_refresh_subscribes_new_listings`
- `test_universe_refresh_unsubscribes_delistings`
- `test_store_recovery_skips_already_covered_symbols`

`tests/test_liquidity_migration_event_demo.py` (extend):
- `test_download_recent_1h_klines_uses_store_fast_path`
- `test_download_recent_1h_klines_falls_back_to_rest_for_uncovered_symbols`

`tests/test_runtime_scripts.py` (extend):
- `test_services_enable_ws_klines`
- `test_bash_runners_wire_ws_klines_env`

---

## What this does NOT change

- The strategy logic in `volume_events.py` / `long_native.py` — same signal, same filters, same entry policy
- The order-submission path in `event_demo.py` — same `validate_order_submit_allowed`, same `submitted_unconfirmed` pattern
- The risk engine `ws_risk.py` — same dual-sleeve routing
- The reconciliation tool — same trade-id pairing
- The backtest — still uses the historical klines from the canonical data root

It is purely a kline-delivery upgrade: REST-pull-on-demand → WS-push-as-bars-close.

---

## Risk + rollback

- Kill-switch via `WS_KLINES_ENABLED=0` env → services revert to REST-fetched klines (the existing code path is preserved during integration)
- If bootstrap fails on a fresh deploy, daemon falls back to REST automatically; cycle works just like before (just slower)
- If WS connections degrade, cycle's REST fallback kicks in for stale symbols
- Existing parquet cache (`event_demo_klines_1h/`) is unchanged; the WS store has its own cache file

The change is additive. The existing REST path stays as the failsafe.

---

## File-by-file delta summary

| File | Change |
|---|---|
| `liquidity_migration/kline_store.py` | NEW (~250 lines) |
| `liquidity_migration/kline_stream_manager.py` | NEW (~200 lines) |
| `liquidity_migration/bybit.py` | +`BybitKlineStreamPool` (~150 lines) |
| `liquidity_migration/event_demo.py` | `_download_recent_1h_klines` accepts optional store (~20 lines diff) |
| `liquidity_migration/event_demo_daemon.py` | Instantiate manager on start, pass store to cycle (~20 lines diff) |
| `liquidity_migration/long_native_event_demo_daemon.py` | Same (~20 lines diff) |
| `scripts/run_bybit_demo_event_engine.sh` | Add `WS_KLINES_*` env passthrough (~10 lines) |
| `scripts/run_bybit_long_demo_event_engine.sh` | Same (~10 lines) |
| `deploy/systemd/*demo*.service` and `*paper*.service` | Add `Environment=WS_KLINES_ENABLED=1` (one line each, 4 files) |
| `tests/test_liquidity_migration_kline_store.py` | NEW (~200 lines) |
| `tests/test_liquidity_migration_kline_stream_pool.py` | NEW (~150 lines) |
| `tests/test_liquidity_migration_kline_stream_manager.py` | NEW (~150 lines) |
| `tests/test_liquidity_migration_event_demo.py` | +2 tests (~50 lines) |
| `tests/test_runtime_scripts.py` | +2 tests (~30 lines) |

Net: ~1,500 lines added, ~50 modified.

---

## Once landed, follow-up work

1. **Tighten `MAX_ENTRY_LAG_MINUTES` from 360 → 60 → 15** over a week of clean operation
2. **Delete the daemon's `warm_demo_kline_cache` loop** — obsolete once WS is the source of truth
3. **Consider WS tickers too** — `BybitPublicTickerStream` already exists; could feed price snapshots into the same store and eliminate the per-cycle `get_tickers` REST call
4. **Migrate `long_native` features to consume the store** — same drop-in pattern as the short side
