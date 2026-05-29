# Pre-registration: latency / fully-event-driven optimization program

**Date:** 2026-05-29
**Author:** assistant (optimization-potential audit remediation), pending owner ratification
**Objective:** move the system toward **lowest-latency, fully event-driven** operation
("Jump-like") and off the daily-frequency / +1h-entry-delay design — the direction the
owner set on 2026-05-29.
**Integrity standard:** `docs/backtesting_errors_we_never_repeat.md` is binding. Any
change that touches the **signal/alpha path** is research-gated (OOS re-validation)
before it can influence real-money work.

## Where this came from

A full optimization-potential audit (8 dimensions, 75 findings → 69 stand after
adversarial verification) mapped every latency / event-driveness / compute / I/O /
concurrency / hard-coding / signal-cadence finding. The audit's load-bearing
conclusion:

> **The runtime is already event-driven and already well-optimized** (the adversarial
> pass refuted the naive wins — the per-cycle feature panel already has a content
> fingerprint cache, ws_risk order submission is already fire-and-forget, the state
> caches never go stale on a quiet account, the cycle file-lock does not block the risk
> daemon). **The binding constraint on reaction latency is the SIGNAL cadence, not the
> infra:** hourly bars → daily-close features → +1h entry delay means a new actionable
> *entry* appears at most once per UTC day, regardless of how fast the loop wakes.

So "lightning fast" splits cleanly into (A) **execution-latency** wins that pay off
TODAY (stop enforcement, independent of the entry cadence), and (B) the **signal-cadence
unlock** (Architecture B / C-phases) that is the only thing that makes *entries* fast —
and is a research program, not a mechanical edit.

## Shipped already (2026-05-29, this program)

- **Docs realigned (ADD-only)** to surface the Architecture-A→B direction at the
  entry points (STATE.md, system_status.md, event_demo_daemon.md, backtest-integrity +
  run-strategy skills) — without rewriting that Round 1/2 research was daily-frequency.
- **De-hard-coded the event-driven daemon knobs**: `min_cycle_interval_seconds`,
  `order_submit_mode`, `ws_trade_timeout_seconds`, `ws_gap_threshold_seconds` are now
  `event-demo-cycle` CLI flags (default None = unchanged) — the debounce floor and
  execution-path knobs are tunable without a code edit.
- **Tier A execution-latency wins (ALL shipped):** (1) Telegram HTTP send moved to a
  background sender thread (off the consumer thread); (2) `submit_exit` cross-process
  double-submit guard moved to in-memory `live_exit_order_symbols` (no parquet read on
  the stop path) — safe because it is an efficiency guard, not a safety one (a miss =
  a redundant reduce-only the venue caps/rejects, never a missed stop); (3) the
  `rest_reconcile` blocking positions+open-orders REST moved to an opt-in background
  prefetcher (`reconcile_prefetch_enabled`, **default off**) on its own HTTP session,
  with a UNION-with-WS apply (never drops a fresh WS position) + stale-fallback to the
  inline path. Enabling (3) on the live risk daemon is a reviewed deploy decision.
- **Tier C compute/IO:** KlineStore materialized-window cache + flush-skip-when-unchanged
  (cycle 0.7–1.2s → 0.3s). vectorize/incrementalize NOT done — see Tier C note below.
- **Tier B Architecture-B capability (default daily):** `aggregation_ms` (feature
  cadence) + `hold_hours`/`cooldown_hours` (sub-daily holds), byte-identical at the
  daily default.

## Sequenced plan (each tranche test-gated, committed, and — where it touches the live system — owner-reviewed)

### Tier A — execution-latency wins (REAL now, NOT gated by signal cadence)
The stop-enforcement daemon (`ws_risk`) is the genuinely latency-sensitive, fully
event-driven part. These reduce stop-reaction jitter/tail TODAY. **They touch the live
safety daemon → careful, well-tested, owner-reviewed work; do NOT rush.**

1. **Take `write_report` + Telegram off the consumer thread** (`ws_risk.py:1871`,
   HIGH). A dedicated single-writer reporting thread fed by an immutable snapshot
   queue; the consumer mutates state + submits orders only. Submit before report.
2. **Move `rest_reconcile`'s blocking REST chain off the consumer thread** (`ws_risk.py:1155`,
   HIGH). Background thread produces an immutable snapshot; consumer applies it as a
   fast in-memory swap. The stale-WS forced reconcile triggers the same path.
3. **Remove the synchronous parquet read from `submit_exit`** (`ws_risk.py:862`, MEDIUM)
   — WITHOUT re-opening the P1-2 cross-process double-submit window. The audit's "use
   in-memory state" is NOT directly safe: `on_order_message` only tracks ws_risk's OWN
   links (`submitted_link_to_trade_id`), so the demo process's reduce-only orders are
   invisible to in-memory state. **Correct fix:** maintain an in-memory
   `pending_reduce_only_symbols` set updated from the shared-account WS order stream
   for ALL account orders (not just our links), so the guard is both faster (no disk)
   and fresher (WS push vs 30s reconcile + per-exit glob). Requires careful add/remove
   on every order status transition + thorough tests before it replaces the disk guard.

### Tier C — compute / I/O (identical-output refactors; some standalone, some cadence-prereq)
**Hard rule:** these are pure performance and must produce **bit-identical** signal
output — gate each behind an old-vs-new equivalence test on a realistic panel before
shipping (error #2/#13 territory if a refactor subtly changes the alpha).

1. **Incrementalize the 45-day daily panel** — **NOT done (WON'T, same reason as #2).**
   Recomputing only the accreting tail day requires the rolling windows at the
   day-boundary to match a full rebuild bit-for-bit, which hits the same float-order
   issue as #2. Cache-served today (zero live gain). Revisit only if the cadence moves
   sub-hourly (then the rebuild cost matters and an OOS-validated re-derivation is the
   vehicle, not a silent refactor).
2. **Vectorize `build_volume_features`** — **NOT done (WON'T): cannot be bit-identical.**
   numpy computes the rolling sums by **cumsum-difference** (`cs[i]-cs[i-w]`); polars
   `rolling_sum` uses a **sliding-window sum** — different floating-point summation
   order, so the outputs differ at the last bit. That is a silent alpha change, and the
   function is **cache-served** in the live daemon (cycle 0.3s, `features:0.0s`), so the
   gain is zero. Shipping it would *violate* the bit-identical rule above. Correctly
   left as the numpy implementation.
3. **Cache the materialized klines window frame in `KlineStore`** — ✅ **SHIPPED** (keyed
   by sorted-symbols+window+mutation-version; ~138ms/wake; provably correct, equivalence
   tested).
4. **Incremental store flush** — ✅ **SHIPPED** as flush-skip-when-unchanged (the whole
   re-serialize is skipped when the mutation version is unchanged).

### Tier B — the signal-cadence unlock (Architecture B / C-phases) — RESEARCH-GATED
This is the only path that makes *entries* fast. It is **already pre-registered** as
the C-phases in `integrated-strategy-program.md` (C0 continuous-signal engine ~5-7 days,
C1 univariate IC, C2 R9 variant, C3 stress) + R12 sniper. It is **NOT a mechanical edit
and must NOT be shipped to the live signal without OOS re-validation** — changing the
feature aggregation from daily-close to a rolling window changes the alpha.

Concrete cadence locks to parameterize (default = daily, so live alpha is unchanged
until the research clears):
- `_daily_bars` day-stamping + 20-hour completeness filter (`volume_features.py:75-95`)
  → a `rolling_window` aggregator stamped at bar-close (C0).
- Bar interval (`_kline_window`/`_floor_hour_ms`, `event_demo.py:2344`) → `bar_interval_minutes`.
- Aggregation granularity (`signal_harness.py:133`, day-unit rolling windows) → `aggregation_ms`.
- `hold_days`/`cooldown_days` calendar-day units → hour units.
- `entry_delay_hours=1` → 0 *only for already-causal rolling features* (R12e; the +1h
  guard stays non-negotiable for daily features).

## Decision rule

- Tier A and Tier C are **infra/perf**: they reduce latency and jitter and create the
  headroom a faster cadence needs, but (per the audit) their *PnL* payoff is gated by
  Tier B. Ship them on their own merits (faster/lower-jitter stop enforcement; cheaper
  cycles), test-gated, with Tier A owner-reviewed (safety daemon).
- Tier B is **alpha**: gated by the C-phase OOS validation in the integrated plan. The
  parameterization defaults to daily; flipping the live signal cadence is a separate,
  research-gated decision. Do not cite a faster-cadence backtest as promotion evidence
  until it clears R10/R11 OOS on Architecture B.
