# VPS Deep Audit — Critical Findings (2026-05-24)

**SSH-pulled live state from `root@5.223.42.109`.** All findings verified against the running VPS ledgers + journalctl.

## Headline

**The short sleeve has fired ZERO entries in 4 days of paper-demo operation.** Across 4,405 cycles spanning 2026-05-21 → 2026-05-24, `entries_executed` is 0 in every single cycle. The strategy is running, polling Bybit, building features, detecting events — but never trading. Equity flat at $9,885.40 since deploy.

This is a structural deployment defect, not a strategy defect. The backtest reproduces +14568% as expected (verified in the prior audit); the live system cannot reach the entry-readiness window with valid data.

## Critical bugs

### Bug 1 — universe is too narrow during the entry-readiness window

The deployment env sets `UNIVERSE_RANK_END=400` and `UNIVERSE_MAX_SYMBOLS=400`. The validator `_validate_demo_config` accepts this (≥ required 300). But the live universe count today between 00:00 and 03:15 UTC was **165–170 symbols** — well below the strategy's required `universe_rank_max(150) + rank_improvement_min(150) = 300` floor. The universe doesn't recover to 400 until much later (after the 01:00–01:15 entry-readiness window for daily signals has long closed).

Worse on days 22 and 23: universe stayed at 166–184 symbols for the entire day. No prior-week rank could reach 300 because the universe physically could not see that far down the ladder. **Events couldn't even be detected** — `events_pipeline.final` was 0 across all 4,287 cycles on days 21–23. The `_maybe_warn_universe_coverage_gap` warning didn't fire because the older universe-coverage telemetry was only added recently (the `coverage={}` field is empty on older days; populated only today).

Bybit demo endpoint currently returns 673 instruments and 675 tickers — there's no API constraint forcing the universe small. The shrinkage is upstream in the universe-build path (likely the symbol-filter / instrument-status / kline-availability gate prunes most of the 675 down to 168 in the first hour after a fresh start).

### Bug 2 — features lag the freshness window by 3+ hours

When the universe does recover today (later cycles, 400 symbols), event detection runs and produces 41 events per cycle. But every one is rejected as `stale`:

| time | events | skipped_stale | latest_feature_ts | comment |
|---|---|---|---|---|
| 00:00–04:38 UTC | empty | 0 | 2026-05-24 00:00 | features not built yet |
| 04:38 UTC | **41** | **41** | 2026-05-24 00:00 | first cycle with events, all 3h38m stale |
| 15:48 UTC | 41 | 41 | 2026-05-24 00:00 | still all stale 15h later |

The 41 events all have `signal_ts_ms = today 00:00 UTC`, so `entry_ready_ts = 01:00 UTC` (signal + entry_delay_hours=1). The configured `max_entry_lag_minutes = 15` means a signal must be entered within 15 minutes of its ready_ts. **Features don't exist until ~04:38 UTC** — 3h 38m after the entry-readiness window has closed.

Result: every cycle either (a) has no events to consider, or (b) has stale events that get rejected. **There is no time of day at which the strategy can both detect and act on a fresh signal.**

This makes the strategy's backtest performance **unreproducible in live deployment** under the current cadence. The backtest implicitly assumes signals fire at exact bar-close and entries fill 1h later regardless of feature-build latency; the live system enforces a 15-minute freshness window that the feature pipeline cannot meet.

### Bug 3 — long sleeve also fires zero entries

`long_native_event_demo` cycle log shows `entries=0/0 exits=0/0 open_long=0` cycle after cycle for the last 12+ hours, equity flat at $9,885.40. Same pattern as the short sleeve (the equity figure happens to coincide because both sleeves read wallet balance from the same demo account).

No deeper diagnosis attempted yet on the long sleeve specifically; pattern suggests same class of bug (feature/freshness mismatch).

## What is working

### Order placement safety + integrity
- `bybit.validate_order_submit_allowed` called from all 4 entry points (event_demo×2, ws_risk, long_native_event_demo) — refuses REAL_MONEY and requires CONFIRM_DEMO_ORDERS
- `submitted_unconfirmed` pattern correctly handles place_order exceptions (prevents orphans on lost responses)
- Runner script enforces SUBMIT_ORDERS=1 only with STRATEGY_PROFILE=promoted (test-locked)

### Untracked-position cleanup did work once
The only orders in the demo ledger (4 rows from 2026-05-21) are `lm-ux-*` reduce-only Buy orders with `exit_reason="untracked_position"`. ws_risk's `reconcile_untracked_exit_orders` detected positions on Bybit not in the local ledger (leftover from a prior deploy) and cleanly closed them out. The mechanism works. ATUSDT, HOMEUSDT, MUSDT, ONTUSDT all closed with `status=filled`, `filled_qty == target_qty`. No partial fills, no orphans.

### Dual-sleeve ws_risk routing
The recent `fe61446` fix is live and operating. Sleeve column is tagged on writes, routed to the correct ledger root. WS execution route is unavailable on demo endpoint (`ws_order_unavailable: "Bybit demo WebSocket Trade order entry is unavailable; using REST fallback for demo reduce-only exits."`) — this is a known Bybit demo constraint, not a bug. Fallback to REST works.

### Partial fills are tracked
`ws_risk` handles "partiallyfilled", "partiallyfilledcanceled", "partiallyfilledcancelled" statuses explicitly. Sets `order["status"] = "filled" if fully_filled else "partial" if filled_qty > 0.0 else previous_status`. Tracks `partial_exit_qty` on the order row. Generates `ws_*_partial_fill` report-reason. Untested live because the system has had no entries to partially fill.

### Timezone consistency
All cycle timestamps, feature timestamps, kline timestamps are UTC ms. Verified across event_demo_cycles and the live `journalctl` output. No tz drift detected.

### Test suite still passes
550/550 tests green locally; VPS git HEAD matches local (`ff61a77`).

## Reconciliation result

Ran `reconcile-paper-demo` on pulled VPS ledgers:
- paper trades: 0
- demo trades: 0
- paired: 0

Nothing to reconcile because neither service has fired an entry. Tool runs cleanly; the underlying data is the issue.

## Action items (in priority order)

1. **Fix universe build path** so the first cycle returns 400 symbols, not 168. Without this, no event can be detected in the freshness window. The 168 → 400 progression suggests the universe is being lazily extended by kline-fetch availability. The fix is probably: pre-warm the kline cache for the universe BEFORE the first cycle runs, or relax the kline-availability gate inside the universe builder.

2. **Resolve the freshness vs feature-build deadlock**. Two viable paths:
   - **Increase `MAX_ENTRY_LAG_MINUTES`** to 360+ (6h+) so signals stay live until features can be computed. Simple, but the backtest assumed near-instant fills so this changes the implicit live model. Need to validate that 6h-stale entries still hit the backtest's profit profile.
   - **Build features faster.** Pre-fetch klines incrementally during the day so by 00:01 UTC the new daily bar has full coverage. The current architecture fetches reactively in the cycle, so the feature row count grows over hours.

3. **Add a self-watchdog alert** — `journalctl -u liquidity-migration-bybit-demo --since="24 hours ago" | grep entries_executed | awk '{sum+=$N} END {if(sum==0) exit 1}'` — fail-loud if the strategy hasn't fired an entry in 24h. Right now the system silently does nothing.

4. **Investigate long sleeve** with the same lens. Same `entries=0/0` pattern suggests the same class of bug.

5. **Once entries do fire**, re-run `reconcile-paper-demo` to measure live slippage. Cannot do that until #1 and #2 are fixed.

## What's still un-audited

- Live WS reconnection behavior (code reviewed; no live disconnect to test against)
- Real qty/price rounding at exchange boundary (no real entries fired to inspect)
- Bybit instrument lifecycle (which symbols dropped between days 21–23) — likely related to Bug 1, didn't trace deeper

## Bottom line

The previous static audit gave a clean verdict because it verified that the **code is correct**. The live VPS state shows the **deployment is broken** — the strategy has produced zero trading activity for 4 days because the universe is too small in the freshness window and the features arrive 3+ hours after entries become stale. The backtest is reproducible but unrunnable in this configuration.
