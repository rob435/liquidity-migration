# Continuous sleeve — sub-cycle reactivity (Tiers 1–4)

**Date:** 2026-06-01 · operator-directed. **Status:** code-complete, suite green (ruff + pytest);
EXPLORATORY forward-demo sleeve only — never real money. **Not deployed** until the operator pushes
(push auto-deploys to the VPS).

The continuous-fade demo sleeve (`continuous_demo.py` + `continuous_demo_daemon.py`) recomputed its
whole signal once per 60s heartbeat (or per confirmed-bar wake) and only reacted to held-name risk on
that same cadence. For a short that can squeeze over minutes inside a multi-hour fade, 60s is slow on
the *protective* side and wasteful on the *signal* side (the heavy features don't move intra-hour).
These four tiers make the sleeve react on the timescale that actually matters, cheaply.

All knobs live on `ContinuousDemoCycleConfig`; everything degrades safely to the prior behaviour when
disabled. The server-side 25% disaster stop is unchanged and remains the ultimate backstop.

## Tier 1 — tick-driven protective exits + anti-thrash (the high-value, low-risk piece)

**Protective exits on every ticker tick.** A dedicated fast-loop thread
(`ContinuousDemoDaemon._protective_exit_loop`) wakes on each ticker tick (and on a heartbeat floor),
reads the held continuous shorts from the ledger, prices them off the WS `TickerCache`, and covers any
that hit a protective trigger — **no panel/decile recompute**. Logic is the state-free
`plan_protective_exits` (the cycle's protective subset minus the `left_decile` state exit):

- `breakeven`  — reached ≥ `breakeven_arm_pct` favorable then gave it back to entry (protect a winner);
- `stop_approach` *(new)* — live loss reached `stop_approach_frac × stop_loss_pct` of the way to the
  disaster stop (default `0.8 × 0.25` ⇒ cover at −20%). Fires on a tick **before** Bybit's market stop
  gaps in — it reduces gap-through-stop slippage; it is an execution-safety tightening, **not** a new
  selection signal. Set `stop_approach_frac=0` to rely solely on the server stop;
- `failed_fade` — held ≥ `failed_fade_hours`, never reached `failed_fade_min_mfe_pct`, now down
  `> failed_fade_loss_pct` (the daily-validated ff6 loss-cut);
- `max_hold`    — the force-exit cap.

MFE is reconstructed from the WS-store kline lows over `[entry, now]` **plus the live price** (so an
intra-hour dip counts immediately). The fast loop is **serialised with the main cycle by a daemon
mutex** (it non-blocking-skips while a cycle runs) and shares the continuous cycle **file-lock**, so it
never races the main cycle or `ws_risk` on the ledger and never double-submits. **Two independent
in-flight guards** stop a double reduce-only: the WS open-orders snapshot (`live_exit_syms`) AND a
WS-lag-independent ledger guard — a held trade whose row already carries an `exit_order_link_id` has a
cover in flight and is skipped (its retry is deferred to the slower main cycle, whose snapshot has a
REST fallback). It is throttled by `protective_exit_min_interval_seconds` (the 2 s floor), reads only
the WS caches/store (**never REST**), and consumes its wake nudge immediately before the check so a
tick landing mid-check re-arms it (no dropped wake; the check reads the live cache regardless).

**Anti-thrash (worth doing regardless of speed):**

- **Hysteresis** (`exit_decile_buffer`, default `1`). Enter on the top decile (`decile`), but cover on
  `left_decile` only once the name is *clearly* out — its decile fell below `decile − exit_decile_buffer`
  (default: hold through a D8 wobble, cover at D7 or below). `buffer=0` reproduces the legacy
  cover-the-instant-it-leaves-D9 behaviour. Stops a name flickering on the D9 boundary from churning fees.
- **Re-entry cooldown** (`reentry_cooldown_minutes`, default `30`). After covering a name, do not re-open
  it for N minutes even if it is in D9. Computed statelessly from the ledger's recent closed exits, so it
  survives daemon restarts.

## Tier 2 — within-hour static-feature cache (`LivePanelCache`, the structural enabler)

`rv_168h, vov, dist_low, xsret7/3` are trailing windows over *confirmed* hourly bars — they don't change
between bar closes; only the in-progress bar's live-price term moves intra-hour. `LivePanelCache`
computes the heavy per-symbol confirmed-bar carry **once per bar close** (`_refresh`) and, on each wake,
computes only the current bar's features from the carry + the live price and re-ranks the cross-section
(`_live_panel`). Per-wake cost drops from a full O(N×history) recompute to a cheap O(N) content-signature
check (+ carry-update + O(N log N) re-rank). The carry is invalidated by a **content signature** (row
count + max ts + close/turnover sums), so it re-refreshes not just on the hour roll-over but whenever a
confirmed bar is **backfilled or re-delivered** with corrected values mid-hour (Bybit re-pushes a
just-closed bar after late trades; `KlineStore.add_bar` overwrites it in place) — without that the carry
would silently go stale within the hour.

**Equivalence (the repo's `np.allclose` gate).** The cross-sectional half is the *shared*
`cross_sectional_decile` (extracted from `compute_continuous_decile_panel` alongside
`per_symbol_timeseries_features` — a behaviour-preserving split), and the per-symbol current-bar feature
values reproduce exactly what the full pipeline produces for the appended synthetic bar. Verified
(`tests/test_liquidity_migration_continuous_demo.py`): `state(...)` is **np.allclose on `composite`** and
**exact on the operative trading sets — D9 entry membership and the hysteresis hold-band (decile ≥ 8)** —
vs `build_live_continuous_state(...)`, across mature timestamps including young-listing and gapped
symbols, with correct intra-hour reuse (one `refresh` per hour, cheap re-rank per wake). Mid-decile
integer assignments can differ only at sub-ULP composite ties (numpy `std` vs polars `rolling_std` differ
by ~1 ULP) — exactly the "last-bit float-order differences carry no alpha" case AGENTS.md sanctions, and
they never touch entries or the hold-band. The **full recompute remains the always-available fallback**
in the cycle (a cache exception is caught and logged), and `live_panel_cache_enabled=False` disables the
cache entirely — so a cache bug degrades to "slower", never "wrong".

## Tier 3 — debounced ticker-batch entry wake (OFF by default; validate before enabling)

`ticker_batch_wake_threshold` (default **0 = off**): after that many ticker rows accumulate, the daemon
sets `_bar_event` to request a full cycle (entries re-evaluated), still floored by the
min-cycle-interval. Tier 2 is what makes this affordable. It is **off by default on purpose**: for a fade
that plays out over hours, 60 s→1 s entry timing is likely noise (and the backtest engine resolves entry
latency only in *hours*, `entry_delay_hours` — it cannot adjudicate sub-hour timing). Enable only if the
forward demo shows entries are systematically late; then it is one config flag.

## Tier 4 — execution-event-driven state refresh

When the private execution WS confirms a **continuous-sleeve** fill (decoded by the `lm-en-c-` / `lm-ux-c-`
orderLinkId prefix), the daemon nudges both the fast protective loop (`_tick_event`) and a prompt full
cycle (`_bar_event`) so held-set/capacity refresh immediately rather than at the next 60 s tick. Other
sleeves' fills are ignored. Positions themselves are already WS-driven via `PrivateStateCache`; this
closes the last gap (ledger-derived held-set/capacity).

## Safety / shared-account invariants (unchanged)

- `ws_risk` remains the single reconcile authority; the fast loop only submits the same reduce-only covers
  the main cycle already does, under the same file-lock, tagged `sleeve="continuous"`.
- Orphan-close (disaster-stop fired while the daemon was down) is still deferred to `ws_risk`, which
  backfills into the continuous ledger.
- Demo + paper only. `REAL_MONEY` is untouched. The forward demo is the only OOS arbiter for the
  EXPLORATORY continuous signal.

## Validation status (be honest)

- **Tier 2** — `np.allclose` equivalence proven; a pure speedup. ✅
- **Tier 1 breakeven / failed_fade** — already engine-validated + adopted
  (`docs/continuous_sleeve_inheritance.md`); running them on ticks is faster execution, not a new signal.
- **Tier 1 stop_approach / hysteresis / re-entry cooldown** — operator-directed reactivity/anti-thrash
  changes that *do* alter live exit/entry timing and are **not** engine-ablation-validated. They are
  risk-reducing / churn-reducing by construction (cut a deeper loser sooner; hold through a one-decile
  wobble; don't re-short within 30 min). The forward demo is the arbiter; all three are configurable and
  can be turned off (`stop_approach_frac=0`, `exit_decile_buffer=0`, `reentry_cooldown_minutes=0`).
- **Tier 3** — mechanism implemented, **off by default**; enabling is gated on forward-demo evidence
  that entry latency matters.
