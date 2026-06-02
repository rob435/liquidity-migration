# Full-system audit — 2026-06-02

> Auto-generated artifact from a 16-dimension multi-agent audit with per-finding adversarial
> verification. Method: 16 audit agents (one per subsystem) → an independent skeptic tried to
> *refute* every critical/high/medium finding → a synthesis lead deduped, ranked, and waved.
> Raw findings: **85** · confirmed after adversarial check: **78** · refuted: **7**.
> 49 agents · ~5.6M tokens · ~27 min. Baseline at audit time: `pytest -q` = 1088 passed.

This is a **working tracker**: as fixes land, check them off. Operator-gated items (Wave D)
are NOT autonomous — they move alpha / the promoted profile / the Tier-3 gate / the locked YAML
/ live ledgers, and need an operator decision per STATE.md non-negotiables.

## Progress log

**Iteration 1 (2026-06-02) — 11 findings fixed, all test-gated, suite 1088 → 1098, ruff clean.
Not committed (operator pushes; push auto-deploys).**

| # | Finding | What landed |
|---|---------|-------------|
| 1, 9 | Continuous order-submit safety gap | `_validate_continuous_demo_config` mirrors the long/short guard (confirm-flag + demo-only + paper/submit refusal), called at the top of `run_continuous_demo_cycle`. Run script already passes `--confirm-demo-orders`, so go-live is unaffected. +4 tests. |
| 2 | WS-silence watchdog can never fire | Added a **WS-only freshness clock** (`last_ws_event_monotonic` + `seconds_since_last_ws_event`) to both caches — bumped only by genuine WS pushes, never by seed/REST-reconcile. Both daemons' `_check_ws_health` now read it. Improves on the audit: returns `inf` until the first push, so the watchdog fires only for a *was-pushing-then-silent* stream — no false positive on a calm account. +2 deterministic tests. |
| 5 | Order-size splitter could exceed cap | Replaced the even-split (last sub absorbed all rounding slack) with a balanced step-unit distribution — every sub provably ≤ cap, still balanced. +exhaustive sweep test. |
| 6 | Mass-false-orphan-close only leaf-tested | Engine-level test: a transient `get_positions` failure leaves open trades open, writes no reconciliation rows, records the error (the worst past incident, pinned through `ws_risk`). |
| 10 | Reset script omits the live continuous sleeve | Added `continuous_fade_demo_*` (+paper) roots to `reset_demo_paper_ledgers.sh` PAIRS. +coverage test. (Running the reset stays an operator step.) |
| 11, 12 | Continuous live alpha/risk values unpinned | Golden test pins `rmom_quantile=0.33`, breaker w24/n8, 0.25 stop; deploy gate (`deploy_vps_live.sh`) now imports + asserts the continuous live config too. |
| 42 | `_merge_universe_config` silently dropped unknown YAML keys | Now raises like `_merge_dataclass`. |
| 49 | `decompose_strategy_pnl` early-return missing keys | Added `n_unresolved`/`resolved_fraction` so a Tier-3 gate reading them can't KeyError. |
| 59 | Stale deploy comment (claimed continuous = dry-run) | Corrected to reflect the 2026-06-01 go-live (`SUBMIT_ORDERS=1`, verify hard-fails otherwise). |

Deferred this iteration: #41 (filter range validation — the module is already very thorough; needs careful per-field check to avoid over-constraining). Remaining waves B/C/D/E below are next.

**Iteration 2 (2026-06-02) — 6 more findings fixed, all test-gated on files disjoint from the
concurrent alpha-research loop. Suite 1098 → 1101, ruff clean. Not committed.**

| # | Finding | What landed |
|---|---------|-------------|
| 44 | Dedup probe only ran for `timeout` | `place_order` now probes for `exception`/`malformed_ack` too (lost/garbled ack ⇒ same double-submit risk); `rejected` stays excluded (definitive venue reject). +test. |
| 45 | Probe treated Rejected/Cancelled history rows as "present" | `_probe_existing_order` now only counts active/filled statuses (`_PROBE_PRESENT_STATUSES`); a rejected row falls through to REST resubmit (Bybit dedup backstops a true race) instead of leaving the position unentered. +test, +updated history-recovery test. |
| 43 | `ws_exit` built split sub-links with a raw f-string | Now uses the shared `_split_order_link_id` (truncates the base, never the unique `-s{idx}` suffix) like the entry/exit paths. |
| 29 | Reconcile over-merged closures with missing timestamps | `_aggregate_bybit_closures` no longer clusters rows with `created_ts_ms<=0` (0−0 ≤ window merged distinct closures). |
| 40 | Archive manifest tail-fill used local-TZ `date.today()` | Switched to `datetime.now(tz=UTC).date()` (VPS is UTC+8, rolled a day early). |
| 35 | r1 bootstrap p-value divided by all samples incl. non-finite | New `frac_gt0` filters to finite (like `pct()`); applied to both `ann_delta_p_gt0` and `mar_delta_p_gt0`. |

Deferred iteration 2: #30 (untracked-exit sleeve routing — a short venue position is ambiguous
between the short/continuous sleeves, so a correct fix needs a dedicated `untracked` dataset;
LOW/analytics-only, deferred to avoid scope-creep into the shared-account routing the other loop
is adjacent to). #26/#27/#28 (chart $1-normalization + monthly-table compounding — the fix hinges
on equity-curve presentation semantics where a naive change makes reporting *less* honest; deferred
to a focused pass).

**Iteration 3 (2026-06-02) — 3 more findings, all test-gated on disjoint files. Suite 1101 → 1103,
ruff clean. Not committed.**

| # | Finding | What landed |
|---|---------|-------------|
| 14 | Unbounded per-order-link maps (orphan-on-OOM) | New `_prune_closed_order_state` (called from `write_report`, same cadence as the telemetry prune) evicts `orders`/`orders_by_link`/`executions_by_link`/`submitted_link_*` entries for CLOSED trades only, beyond a `telemetry_log_retention` grace. **An open trade's links are never evicted** (in-flight reconciliation protected); links without a resolvable trade are kept (can't prove closed). `orders_evicted` counter for observability. +test pinning open-link survival + all-four-map eviction. |
| 51 | Deploy reboot restart-order untested | Added enable+restart ordering assertions (risk before continuous) to the deploy-script test — the reboot-safety invariant the docs lean on. |
| 54 | `gather_alerts` stop-protection orchestration untested | Test wires a no-server-stop venue position → `unprotected:*` CRITICAL, and a failed venue probe → `stop_verify_unavailable` WARNING with no false "protected" — pinning the paging path that backstops the live ledger. |

Deferred iteration 3: #19 (wallet zero-equity sentinel — distinguishing "no equity field" from
"legit ≤0" needs a contract change to the shared `wallet_equity_usdt`; LOW and doesn't occur on a
funded demo, so the `>0.0` guard is left as-is). #56 (cross-sleeve symbol-fallback — wants a more
conservative routing rule, design-first). #3 (rmom look-ahead causality — operator-gated AND in the
concurrent alpha-loop's active hot-zone `continuous_events`/`risk_model`/`precompute_residual_momentum`;
surfaced for the operator rather than risk a same-file race).

**Cumulative: 20 findings closed over 3 iterations; suite 1088 → 1103; ruff clean; nothing committed.**

**Iteration 4 (2026-06-02) — 2 findings, plus 6 reasoned deferrals. Suite 1103, ruff clean. Not committed.**

| # | Finding | What landed |
|---|---------|-------------|
| 22 | `_on_bar` HWM compare-and-set unlocked | Wrapped the `_max_confirmed_ts_ms` compare-and-set in `self._lock` (it runs on N pooled WS threads); `Event.set()` stays outside the lock. Prevents a lost boundary-wake under the hourly multi-connection burst. |
| 55 (part) | `_completed_bar_close_location` duplicated the engine formula | event_demo now imports + delegates to `volume_events._bar_close_location` (no cycle — the import direction already exists); byte-identical, behavior-preserving. |

**Reasoned deferrals (the naive fix is net-negative or too risky for the live path — NOT skipped lightly):**
- **#23** (coverage-scan → incremental newest-ts cache): perf-only and negligible at ~750 symbols/ms-scale; a missed `_bars` mutation site would silently corrupt REST-fallback decisions. Not worth a non-measured optimization's correctness risk.
- **#7** (reconcile resurrects a position closed mid-fetch): a *bounded* one-cycle-delay race that self-heals; the correct fix spans cache + daemon seeder + merge with subtle WS-removal tracking — a focused, dedicated pass on the live reconcile path, not a multi-item turn.
- **#57** (flaky scheduler tests): test-quality on currently-passing tests; the proper fix is a live-daemon scheduler refactor — not worth that risk for latent flakiness.
- **#20** (leading-edge window gap): the naive `lo>start_ms` check forces every newly-listed symbol off the fast path every cycle (REST can't backfill pre-listing bars) — a net-negative; needs listing-time awareness.
- **#19** (wallet ≤0 equity): distinguishing "no equity field" from "legit ≤0" needs a contract change to shared `wallet_equity_usdt`; doesn't occur on a funded demo.
- **#31** (adopted-trade net_return=0): the equity isn't available in the adoption path without a new wallet fetch; LOW/analytics-only/post-restart-only.

## Live incident + durable fix — 2026-06-02 (post-deploy)

After the `d981ada` push deployed, the `check_demo_liveness` watchdog (the #54 monitoring this
audit added/tested) correctly paged: **continuous-sleeve rmom gate EMPTY (`max_rmom_day_ts=0`) →
silent zero-signal blackout.** Fail-safe (the sleeve makes NO entries, never wrong entries;
protective exits run off live price). Root cause: `deploy_vps_live.sh` did `systemctl enable --now`
the rmom **timer** (next fire 00:20 UTC) but never ran the refresh **service**, so a fresh deploy
started the daemon into an unseeded `residual_momentum.parquet`. NOT a code regression (the
`--end`-defaults-to-tomorrow fix is intact); a deploy-ordering gap.

- **Immediate remediation (operator, on the VPS):** `systemctl start liquidity-migration-continuous-rmom-refresh.service` then verify the parquet has a current row; the next 60s cycle clears the blackout.
- **Durable fix (implemented, test-gated, ready for next push — NOT pushed):** `deploy_vps_live.sh`
  now SEEDS the rmom gate (runs the oneshot refresh) on every deploy, before the continuous daemon
  restart, with a fail-safe WARN when the gate is empty (the first-deploy edge where the kline store
  is still bootstrapping). Regression test pins the seed + its ordering + the WARN.

## Second-pass deep audit (numerical / accounting / data-layer) — 2026-06-02

A focused 5-agent second pass on the high-stakes areas the broad pass disclosed as gaps
(engine PnL/MTM/funding math, metric consistency, reconciliation in full, data layer),
adversarially verified: **21 raw → 20 confirmed (1 refuted).** It found real issues the
broad sweep's spot-checks missed.

**Iteration 5 — 6 fixed (test-gated, stable files). Suite 1103 → 1106, ruff clean. Not committed.**

| # | Finding | What landed |
|---|---------|-------------|
| 1 | Archive→klines streaming builder derived open/close from CSV ROW ORDER (no ts sort), unlike both siblings | **VERIFIED the real full-PIT archives are time-ASCENDING → open/close were already correct; NO prior-result corruption.** Hardened the streaming builder to track open/close by trade-ts (byte-identical on ascending data, robust if Bybit's format ever flips) + a descending-CSV regression test. |
| 3 | r1 `_annualize`/`_engine_mar` returned a COMPLEX number for ≤−100% cumulative return → crashed the Tier-2 verdict via `math.isfinite` | Floor growth≤0 to −1.0 (mirrors `apply_decision_rule`); no-op for all valid cells, fixes the crash. +test. |
| 6 | `get_closed_pnl` unpaginated (single page ≤200) while the funding sibling paginates → orphan-backfill could miss a re-entered symbol's closing record | Added the same `nextPageCursor` loop; single-page behaviour unchanged. +pagination test. |
| 57 | Daemon scheduler timing tests flaky-by-construction (per-sample wall-clock bounds) — **confirmed failing under full-suite+concurrent-loop load** | Assert on the MEDIAN period (catches systematic drift, tolerates load spikes). Pass 3× consecutively now. |
| 19, 20 | `_funding_lookup` double `to_dicts()`; dead `_cs_count_partial` windowed cum_sum | Behavior-identical cleanups. |

**⚠️ OPERATOR-GATED pass-2 findings (real, but move a research/live number — NOT autonomous):**
- **#2 (HIGH) — Binance funding undercount ~50% on 4h-funding alts.** `downloaders.py:729` hardcodes `funding_interval_min=480` for every symbol, but `/fapi/v1/fundingRate` returns one row per ACTUAL settlement; `trade_lifecycle._funding_lookup` then collapses 4h settlements into 8h buckets and drops one. **Binance is the cross-venue OOS arbiter and funding eats ~85% of the edge — this systematically INFLATES the Binance MAR the promotion gate trusts.** Fix: don't collapse Binance funding (one row = one charge), or derive the true interval from settlement spacing. Needs an operator decision + a re-run (it moves every Binance backtest).
- **#9 (low) — Bybit funding interval** defaults to 8h (endpoint omits the field); same class, smaller. Source it from `get_instruments_info`.
- **#4 (med) — pit_age_days overstated by 1 day** (computed from the stamp date = trading_day+1). The live `age300` SELECTION gate admits boundary symbols one day too young. Fix keys age on `trading_day_expr` both sides — but it shifts the promoted profile's live selection.
- **#8/#10/#12 — per-basket cumprod in split/monthly tables** (volume_events `_summarize_basket_split` feeds the promotion gate; long_native split + monthly). Same class as pass-1 #32/#26; moving them changes reported gate metrics.
- **#11 — feature day-keying inconsistency** (funding/OI use day-end keying; flow/basis/premium use floor) — unifying it moves features → backtest.

**Iteration 6 — 3 more (test-gated). Suite 1106 → 1108, ruff clean. Not committed.**

| # | Finding | What landed |
|---|---------|-------------|
| 17 | Binance proxy downloader silently skipped failed symbols (survivorship risk) | Accumulate failures + `_assert_download_completeness` (mirrors binance_vision) with a 5% tolerance + a persisted failed-jobs artifact; refuses to build a holey root. +test. |
| 18 | `normalize_trade` synthetic id collided for idless split fills → dedup-collapse undercounts volume | Append the caller's row index to the synthetic id (real venue ids unaffected; default-safe for direct callers). +test (idless prints survive; true execId dups still dedup). |
| 5/15/16 | Reconciliation merge averaging/timestamp basis | Documented the deliberate basis (priced-only VWAP; gap already None-guards the fully-unpriced case; first-leg timestamp). No behavior change — the "fixes" would discard info or be cosmetic on an offline report. |

**Still-open (low, next iterations if the loop continues):** #7 (recon funding time-scope — involved, offline report), #13 (funding partial-flag interior gaps — moves a diagnostic label only).

**Cumulative across both passes: 30 findings fixed + several documented; suite 1088 → 1108; ruff clean; nothing committed.** The autonomous-fixable backlog is essentially exhausted — what remains is operator-gated (the Binance-funding-interval #2 is the standout), low-value offline-report nits, or god-file modularization (deferred while the concurrent alpha loop edits adjacent files).

**State of the audit:** the critical/high/medium tail is largely addressed (iterations 1–3: order-submit safety, WS watchdog, orphan-close, order-size cap, OOM guard, double-submit probe). The remaining backlog is predominantly **LOW/nit with real subtleties**, **operator-gated** (the rmom look-ahead #3, decompose day-grid #25, age300 definition #8 — these move alpha/the promotion gate and need an operator decision + the proposed causality/parity tests), or **god-file modularization** (#60–#63 — high-churn; risky to do while the concurrent alpha-loop is editing adjacent sleeve files). Autonomous high-value work is reaching diminishing returns; the highest-leverage next step is **operator review of the 22 landed fixes + a decision on the operator-gated methodology items.**

## Executive summary

A full-system audit of the liquidity-migration demo/paper trading stack surfaced ~50 distinct confirmed findings after dedup. I independently re-verified the six highest-stakes items against source: (1) the continuous live sleeve never calls validate_order_submit_allowed and has no paper_mode+submit_orders guard (security gap on the only order-submitting path missing the repo's money-safety invariant); (2) the WS-silence operator watchdog can never fire because the reconcile loop re-seeds last_event_monotonic via replace_with_rest_snapshot immediately before _check_ws_health reads it; (3) a residual-momentum SELECTION signal that feeds the live continuous sleeve carries a plausible look-ahead (a strictly-forward fwd_ret_1d residual keyed to the decision day, with .shift(1) not provably clearing the day-grid offset); (4) the long sleeve can fire FC entries on an incomplete forming UTC daily bar from ~20:00 UTC onward (live != validated backtest); (5) the max-order-size splitter can emit a final sub-qty above the cap on coarse steps, re-causing the rejection it exists to prevent; (6) the empty-fetch orphan-close guard exists in seed() but is only unit-tested, never exercised through the ws_risk engine. No fixed-and-regressed bug was re-reported. The single most important methodology item (the rmom look-ahead) is OPERATOR-GATED: confirming/fixing it moves both the backtest and the live continuous signal, so it must go through a causality test + operator decision, not an autonomous edit. The continuous sleeve dominates the high-severity tail — it is the newest live sleeve and several of its safety rails (order-submit validation, deploy-config pinning, ledger-reset coverage, watchdog) lag the three older sleeves.

## Severity (confirmed, post-dedup)

| sev | count |
|-----|-------|
| high | 4 |
| medium | 10 |
| low | 50 |
| nit | 8 |

## Confirmed findings (ranked)

Legend: ⛔ = operator-gated (do not autonomously edit).

### #1 — [HIGH / security] Continuous live sleeve never calls validate_order_submit_allowed; --confirm-demo-orders is a dead flag and REAL_MONEY is not refused at the Python layer (and --paper-mode is allowed with --submit-orders)

- **Files:** `liquidity_migration/continuous_demo.py:117-118,878 (run_continuous_demo_cycle); liquidity_migration/cli.py:2038,2041,2649-2650; cf. bybit.py:95 validate_order_submit_allowed, called in event_demo.py:1829/1838, ws_risk.py:2208, long_native_event_demo.py:325`
- **Fix:** Add a _validate_continuous_demo_config (mirror _validate_demo_cycle_config / long_native_event_demo.py:319-325) called at the top of run_continuous_demo_cycle AND the daemon submit path, BEFORE building the private client: (a) validate_order_submit_allowed(submit_orders=config.submit_orders, confirm_demo_orders=config.confirm_demo_orders); (b) raise if config.paper_mode and config.submit_orders; (c) raise if config.paper_mode and not config.record_dry_run. Add tests for each raise.
- **Effort:** S · **Risk:** Low — adds a guard that only fails fast on misconfigured/unconfirmed/real-money submission; cannot regress a correctly-configured demo run. Verify the systemd unit ships --confirm-demo-orders (or set confirm default appropriately) so the new gate does not block the live go-live.

### #2 — [HIGH / observability] WS-silence operator watchdog can never fire: reconcile re-seeds last_event_monotonic via replace_with_rest_snapshot immediately before _check_ws_health reads it

- **Files:** `liquidity_migration/event_demo_daemon.py:857,874,887,902 (_reconcile_loop -> _invoke_state_cache_seeder then _check_ws_health); liquidity_migration/ws_state_cache.py:181,197 (seed bumps last_event_monotonic; replace_with_rest_snapshot calls seed); identical in long_native_event_demo_daemon.py`
- **Fix:** Track WS-event freshness independently of REST re-seeds: add a ws_only_last_event_monotonic that only on_position/order/wallet/ticker_event bump (seed()/replace_with_rest_snapshot do NOT touch it), and have _check_ws_health read it; OR record a per-channel last-WS-event monotonic in the daemon's _handle_*_message handlers and base the watchdog on that. Keep is_stale() (cycle REST-fallback) on the seed-inclusive clock.
- **Effort:** M · **Risk:** Low-medium — observability-only (no trade behavior change), but touches the shared cache stats struct used by two daemons; gate with a unit test that asserts the watchdog fires after a simulated WS silence even when a REST reconcile interleaves.

### #3 — [HIGH / look_ahead] ⛔ Residual-momentum SELECTION signal feeding the live continuous sleeve carries a plausible look-ahead (forward fwd_ret_1d residual keyed to decision day; .shift(1) may not clear the day-grid offset)

- **Files:** `liquidity_migration/risk_model.py:160-162,169,209-221 (fit_factor_returns: ts is the decision-day key, y=fwd_ret_1d strictly-forward); scripts/precompute_residual_momentum.py:67-74 (rolling_sum(7).shift(1)); liquidity_migration/continuous_events.py:213-214 + the live continuous decile join`
- **Fix:** Add a causality test FIRST (build two panels differing only in a future residual_return and assert residual_momentum[D] is unchanged). If a leak is confirmed, re-key the residual to its EXIT day (when the d+1->d+2 return is actually known) before building momentum, OR increase the lag (shift(2) for a day-end decision; appropriate lag for the within-day continuous decision) so the rolling sum only includes residuals whose exit precedes the decision.
- **Effort:** M · **Risk:** High blast radius: this MOVES the backtest AND the live continuous signal (alpha-class). Do NOT autonomously edit. Run the causality test, present evidence + the exact lag fix to the operator, and treat any change as a profile/signal change behind the operator gate.

### #4 — [HIGH / correctness] Long sleeve can fire FC entries on an incomplete (forming) UTC daily bar from ~20:00 UTC — live signal diverges from the validated backtest

- **Files:** `liquidity_migration/long_native_event_demo.py:111 (SIGNAL_FRESHNESS_MS=24h),854-857 (_select_long_entry_candidates eligible_ts: freshness-only, no completed-day lower bound); liquidity_migration/long_native.py:772/816 (daily_bars day-end ts = floor(ts,day)+MS_PER_DAY = next midnight, in the future for a forming day)`
- **Fix:** Bound eligible_ts to fully-closed days: filter to ts <= floor_to_day(now_ms) (day-end ts at/before the current UTC midnight), OR build daily bars with a completed-day guard (require the day-end ts <= now_ms, or 24 hourly bars). Mirror the short sleeve's completed-event discipline. Add a regression test feeding a forming-day bar and asserting no candidate.
- **Effort:** S · **Risk:** Medium — changes live entry timing for the long sleeve, so it is a live behavior change (not numerically behavior-preserving). Test-gate against a known forming-day fixture; confirm it does not regress the legitimate yesterday-still-in-6h-window entries the freshness filter intends to catch.

### #5 — [MEDIUM / correctness] Max-order-size splitter can emit a final sub-qty above the per-order cap on coarse qty_step, re-causing the maxMktOrderQty rejection it was built to prevent

- **Files:** `liquidity_migration/event_demo.py:1933-1947 (_split_qty_for_max_order_size: per_sub floored down by up to one step each, last absorbs (n_subs-1) steps of slack and can exceed cap)`
- **Fix:** Build subs greedily: repeatedly emit min(remaining, floor(cap/step)*step) until remaining<=0; OR after flooring per_sub recompute n_subs=ceil(target/per_sub) and clamp last. Add a test with a coarse step (target=29999, cap=10000, step=100) asserting every sub <= cap.
- **Effort:** S · **Risk:** Low — the new form provably keeps every sub <= cap; pure local function with an easy exhaustive test. Live-path execution change but strictly safer (prevents a rejection -> under-fill).

### #6 — [MEDIUM / test_gap] Empty-fetch / transient get_positions failure (the C1 mass-false-orphan-close class) is only unit-tested on the leaf guard, never exercised through the ws_risk engine

- **Files:** `tests/test_liquidity_migration_ws_risk.py:81 (FakePrivateClient.get_positions has no failure mode); guarded by ws_state_cache.py:165-173 seed() + _risk_reconcile_missing_positions require_evidence path`
- **Fix:** Add a fail_positions flag to FakePrivateClient.get_positions (raise RuntimeError). Test: engine with two open trades, get_positions raising; call bootstrap() and rest_reconcile() separately; assert both trades stay status='open', no reconciliation rows written, last_position_error set.
- **Effort:** S · **Risk:** Low — test-only; pins the highest-consequence past-incident class (memory audit-2026-05-29-critical-orphan-close) against regression.

### #7 — [MEDIUM / concurrency] Periodic REST reconcile can resurrect a position closed during the fetch window (stale REST snapshot clobbers fresher WS removal)

- **Files:** `liquidity_migration/ws_state_cache.py:165-181 seed() (unconditional whole-map replace), 183-204 replace_with_rest_snapshot`
- **Fix:** Capture a monotonic ts when the REST fetch STARTS, pass it into replace_with_rest_snapshot, and in seed() skip removing/overwriting a per-symbol position whose cached row was written by a WS event newer than the fetch start (store a per-row applied-at monotonic in _upsert_position_locked). Simpler bound: shorten the reconcile interval and record on_position_event symbols that just closed to suppress re-add.
- **Effort:** M · **Risk:** Medium — touches the live reconcile/cache merge; a re-added closed position could drive a spurious cover or block re-entry. Test with an interleaved size==0 WS push during a reconcile.

### #8 — [MEDIUM / correctness] ⛔ age300 SELECTION gate compares pit_age_days from two different definitions live (exchange launch time) vs backtest (first PIT-archive appearance)

- **Files:** `liquidity_migration/event_demo_data.py:600-615 (_build_demo_features: pit_age_days from listing_age_days = (snapshot_ts_ms - launch_time_ms)); liquidity_migration/volume_events_features.py:537-543 (_attach_event_archive_membership: pit_age_days from first_manifest_date)`
- **Fix:** (a) document the definitional difference in _build_demo_features + the promotion receipt so it is not mistaken for full live==backtest fidelity, OR (b) if the age300 edge is sensitive, derive the live pit_age from the same archive_trade_manifest the backtest uses (falling back to listing_age only when the manifest lacks the symbol). Add a parity assertion on a known signal.
- **Effort:** M · **Risk:** Medium — option (b) shifts which symbols pass the live age gate (signal change); option (a) is documentation-only. Recommend (a) now + escalate (b) to operator since it touches the promoted profile's behavior.

### #9 — [MEDIUM / correctness] continuous sleeve allows --paper-mode together with --submit-orders: real demo orders submitted while writing to the paper (shadow) ledger

- **Files:** `liquidity_migration/cli.py:2041,2650; liquidity_migration/continuous_demo.py:153-155 (paper_mode only switches dataset names),629`
- **Fix:** Covered by the rank-1 _validate_continuous_demo_config (raise on paper_mode and submit_orders, and on paper_mode and not record_dry_run). Listed separately because it is a distinct corruption mode (paper shadow contamination) the same guard closes.
- **Effort:** S · **Risk:** Low — fold into the rank-1 guard; cannot regress a valid run.

### #10 — [MEDIUM / correctness] reset_demo_paper_ledgers.sh omits the now-live continuous-fade demo root — a strategy-overhaul reset leaves stale continuous trades, contaminating the forward-demo clean split

- **Files:** `scripts/reset_demo_paper_ledgers.sh:54-67 (PAIRS list lacks data/bybit-continuous-demo-event:continuous_fade_demo_*)`
- **Fix:** Add continuous_fade_demo_trades/orders/cycles (and paper equivalents if/when deployed) to PAIRS. Add a regression test asserting the reset PAIRS list covers every live ORDER-SUBMITTING DATA_ROOT registered in storage.DATASETS.
- **Effort:** S · **Risk:** Low for the script edit. Note: running the reset itself is an OPERATOR step (it wipes live demo ledgers) — only the script + test change is autonomous.

### #11 — [MEDIUM / test_gap] Live continuous-sleeve rmom_quantile=0.33 (the single applied alpha lever) is not pinned by any golden test, and the deploy gate does not pin the continuous live config

- **Files:** `liquidity_migration/continuous_demo.py:52 (rmom_quantile=0.33); scripts/deploy_vps_live.sh:49-78 (in-deploy assertion block pins only the SHORT promoted config); scripts/verify_vps_live.sh`
- **Fix:** Add a golden test: assert ContinuousDemoCycleConfig().rmom_quantile == 0.33 (cite the 2026-06-02 alpha-sweep receipt), plus entry_pause_after_adverse_exits==8, entry_pause_window_minutes==1440, stop_loss_pct==0.25. Extend deploy_vps_live.sh + verify_vps_live.sh Python assertion block to import the continuous live config and assert the same values, mirroring the short-sleeve pins.
- **Effort:** S · **Risk:** Low — test/assertion only; protects an operator-directed live value from a silent revert toward the engine default (0.50).

### #12 — [MEDIUM / test_gap] Deploy pre-restart smoke test does not pin the continuous sleeve's live config — a regression to those defaults silently changes live demo trading on the next deploy

- **Files:** `scripts/deploy_vps_live.sh:49-78`
- **Fix:** Same change as rank 11 (extend the deploy assertion block). Listed separately because it is the deploy-gate surface vs the package golden test; both should land together.
- **Effort:** S · **Risk:** Low — assertion-only.

### #13 — [MEDIUM / observability] Rejection-trace diagnostic omits ~42 of the filter's gates, mislabeling production-dropped rows as 'passing'

- **Files:** `liquidity_migration/volume_events_filters.py:773-968 (_explain_liquidity_migration_rejections gate registry vs _filter_liquidity_migration active predicate set)`
- **Fix:** Either (a) extend the registry to every config-conditional gate (residual_momentum, taker_imbalance, funding, market-regime, etc.) and add a test asserting registry gate-set == filter active-predicate set, OR (b) downgrade the docstring/contract and add a synthetic '_uncovered_gate' that fails any row passing all registered gates so an operator never reads a falsely-clean trace.
- **Effort:** M · **Risk:** Low — diagnostic tooling, not the trade path; option (b) is the safe minimum.

### #14 — [MEDIUM / performance] Unbounded per-order-link state in ws_risk (state.orders, orders_by_link, executions_by_link, submitted_link_*) is never pruned — the documented orphan-on-OOM failure mode

- **Files:** `liquidity_migration/ws_risk.py:1945-1958 (_prune_state_logs caps only telemetry lists) vs 820-825 (state.orders/orders_by_link), 696 (executions_by_link), 982-983/1111-1112 (submitted_link_*)`
- **Fix:** Evict terminal/closed orders from state.orders + orders_by_link once their trade is closed (or cap-oldest-terminal like the telemetry lists), and drop matching executions_by_link / submitted_link_* in clear_submitted_symbol / on trade close. Add a test mirroring test_prune_state_logs_caps_telemetry for the order/link maps.
- **Effort:** M · **Risk:** Medium — ws_risk is the single live reconcile authority; eviction must not drop a link still needed for in-flight reconciliation. Gate on the existing ws_risk tests + a new eviction test asserting open links survive.

### #15 — [LOW / correctness] Partial REST ticker response wipes the complete WS-accumulated ticker set on reconcile (no shrink guard, unlike positions/universe)

- **Files:** `liquidity_migration/ws_state_cache.py:454-468 (TickerCache.seed/replace_with_rest_snapshot); long_native_event_demo_daemon.py:944-945 (unconditional replace)`
- **Fix:** Add a shrink guard to the ticker reconcile: if incoming REST ticker count is materially below the cached symbol_count, log and skip the replace (let WS deltas + next reconcile recover) — mirror _universe_shrink_floor in event_demo._resolve_universe_and_tickers.
- **Effort:** S · **Risk:** Low — defensive; prevents a transient coverage shrink that the 2026-05-24 incident class showed is real.

### #16 — [LOW / correctness] Fast protective-exit loop reads the WS ticker cache with no staleness gate (asymmetric to the main cycle's REST fallback)

- **Files:** `liquidity_migration/continuous_demo.py:1058-1077 (_live_prices_from_ticker_cache) consumed by run_continuous_protective_exit_cycle:1116`
- **Fix:** Gate the fast loop on freshness: skip the cycle (or per-symbol covers) when ticker_cache.is_stale(...) or a per-symbol last-update age exceeds a small bound; thread a per-symbol update timestamp through TickerCache.get so stop_approach/failed_fade only fire on fresh prices. Rely on the next main cycle (REST fallback) + the server stop for stale names.
- **Effort:** M · **Risk:** Low-medium — changes when fast-loop exits fire; the wide server-side disaster stop is the backstop. Test with a stale per-symbol ticker.

### #17 — [LOW / correctness] Live cooldown ignores config.cooldown_hours that the backtest honors (latent live!=backtest)

- **Files:** `liquidity_migration/event_demo.py:2542-2553 (_cooldown_until uses cooldown_days only) vs volume_events.py:825-830`
- **Fix:** Mirror the backtest: cooldown_ms = config.cooldown_hours*MS_PER_HOUR if not None else config.cooldown_days*MS_PER_DAY. OR add a _validate_demo_config raise if a profile sets cooldown_hours until the daily runner supports it.
- **Effort:** S · **Risk:** Low — currently benign (promoted/relaxed leave cooldown_hours=None); the validate-raise option is fully behavior-preserving today.

### #18 — [LOW / correctness] Live planned_exit_ts_ms ignores scenario.hold_hours that the backtest honors (latent live!=backtest)

- **Files:** `liquidity_migration/event_demo_planning.py:170 (select_demo_entry_candidates) and 268 (plan_demo_exits) use scenario.hold_days*MS_PER_DAY vs volume_events.py:307-312 _scenario_hold_ms`
- **Fix:** Route both live call sites through the shared _scenario_hold_ms(scenario) helper. OR add a _selected_scenario/_validate_demo_config guard rejecting hold_hours until the daily runner supports it.
- **Effort:** S · **Risk:** Low — benign today (promoted uses hold_days=3); validate-raise option is behavior-preserving.

### #19 — [LOW / correctness] Wallet WS update silently ignored (and clock still bumped) when computed equity <= 0, pinning equity at last positive value

- **Files:** `liquidity_migration/ws_state_cache.py:409-421 (_apply_wallet_update_locked: if equity>0.0), 287-288 (on_wallet_event bumps last_event_monotonic regardless)`
- **Fix:** Use a sentinel (equity is not None) rather than equity>0.0 so a real low/zero equity can propagate; OR do not treat a no-op wallet row (equity field absent) as a successful equity refresh for staleness. Distinguish 'no equity field' from 'equity legitimately low'.
- **Effort:** S · **Risk:** Low — equity feeds entry sizing; a stuck-high equity over-sizes. Edge-path; test with a coin-only delta and a near-zero totalEquity.

### #20 — [LOW / correctness] Leading-edge window gap not detected by _window_incomplete_symbols; a store-covered symbol missing its earliest hours stays on the fast path

- **Files:** `liquidity_migration/event_demo_data.py:364-385 (_window_incomplete_symbols computes completeness over the symbol's own [lo,hi], not the requested [start_ms,end_ms])`
- **Fix:** Compare against the requested window: flag incomplete when lo>start_ms OR hi<end_ms OR the contiguity count check fails, requiring coverage of the full [start_ms,end_ms] grid.
- **Effort:** S · **Risk:** Low — a leading hole serves a short feature window; would trigger an extra store-fill path. Behavior-affecting on reconnect-gap edge.

### #21 — [LOW / correctness] ⛔ continuous-rmom-refresh runs precompute on the demo root holding only ~45 days of klines, under-warming the 60-day BTC-beta / 30-day xs-rank factors

- **Files:** `scripts/run_bybit_continuous_demo_event_engine.sh:32 (LOOKBACK_DAYS=45); deploy/systemd/liquidity-migration-continuous-rmom-refresh.service; risk_model.py:114 build_factor_panel pads only 90d`
- **Fix:** Set WS_KLINES_LOOKBACK_DAYS / the rmom kline history on the continuous demo root to comfortably exceed the 90d warm-up (e.g. 150+ days), OR document the live rmom rank is warm-up-limited vs the research root and add a telemetry field recording the panel's earliest factor-valid day.
- **Effort:** M · **Risk:** Medium — increasing lookback raises memory/bootstrap time on the VPS; signal-quality (live!=backtest rmom ranks) concern. Operator should size the lookback vs box limits.

### #22 — [LOW / concurrency] _on_bar advances the cycle-wake high-water mark from concurrent WS connection threads without the manager lock

- **Files:** `liquidity_migration/kline_stream_manager.py:363-365 (_on_bar read-modify-write of _max_confirmed_ts_ms outside self._lock; pool opens multiple connections)`
- **Fix:** Guard the _max_confirmed_ts_ms compare-and-set under self._lock (RLock already present), keep Event.set() outside; OR document the field as advisory and a duplicate wake as acceptable.
- **Effort:** S · **Risk:** Low — worst case is a redundant idempotent cycle wake; behavior-preserving lock add.

### #23 — [LOW / performance] symbols_with_coverage_through does an O(symbols x bars) max() scan under the store lock every cycle

- **Files:** `liquidity_migration/kline_store.py:472-486`
- **Fix:** Maintain a per-symbol newest-ts cache updated incrementally in _insert_bar/bootstrap_symbol/_evict_old_locked/recover (same pattern as _global_max_ts_ms), making the method O(symbols) dict lookups.
- **Effort:** M · **Risk:** Low — perf-only; behavior-preserving if the incremental cache is correct. Test newest-ts invariance against the old scan over a fixture.

### #24 — [LOW / survivorship] Symbol-restricted archive-manifest rebuild overwrites whole date partitions -> survivorship hole (drops delisted symbols)

- **Files:** `liquidity_migration/archive_manifest.py:352-356 (requested-symbol restriction),383 (v5 supplement never symbol-filtered),452 (write_dataset append=False, partition_by date); storage.py:331-360 (_write_dataset_unlocked partition-local overwrite)`
- **Fix:** Either make run_archive_manifest a true full replace (rmtree the dataset dir under the lock, require a full-universe build — mirror binance_vision.rewrite_manifest_to_coverage), OR when config.symbols is non-empty refuse to write over an existing multi-symbol manifest / merge the symbol's rows into existing per-date frames. Document append=False as partition-local, not whole-dataset replace.
- **Effort:** M · **Risk:** Medium — touches the PIT-membership data the methodology gate depends on. Severity tempered to low because the documented backfill workflow (archive-manifest --symbols X) is operator-invoked and STATE.md's reconcile flow rebuilds full-universe, but the footgun is real. Test a symbol-restricted rebuild preserves other dates' symbols.

### #25 — [LOW / look_ahead] ⛔ decompose_strategy_pnl reads factor loadings from the day AFTER the decision (day-start vs day-end grid off-by-one)

- **Files:** `liquidity_migration/risk_model.py:368-369 (grid=(ets//MS_PER_DAY)*MS_PER_DAY then load_map lookup; engine entry_ts ~= 01:00 of D+1)`
- **Fix:** Snap to the panel's decision-day key, not the floored entry day: derive the decision-day ts from the trade's signal day (floor entry_ts to its day then subtract one MS_PER_DAY for the day-end engine convention, or carry an explicit signal_day_ts column). Add a regression test.
- **Effort:** M · **Risk:** Medium — feeds the Tier-3 residual-Sharpe gate (real-money gate input). Not a live-trade path, but mis-dated exposures bias the gate metric. Verify against a fixture before/after; operator-relevant since it touches the promotion gate's trustworthiness.

### #26 — [LOW / accounting] Monthly-return table uses per-basket compounding (cross-term) that the equity line on the same chart deliberately excludes — the two disagree for same-day exits

- **Files:** `liquidity_migration/volume_events_charts.py:47-62 (_monthly_returns product),283-296,358 vs trade_lifecycle.py:64-74 build_equity_curve (sums same-day basket returns)`
- **Fix:** Compute the monthly table from build_equity_curve's daily additive basis (sum same-day, then (sum+1).product()-1 across days within month), or share one helper between the line and the table. Keep trade/basket counts as-is.
- **Effort:** S · **Risk:** Low — report-only; aligns two on-chart conventions. Test that the table reconciles with the equity line on a same-day-exit fixture.

### #27 — [LOW / correctness] Strategy line is not normalised to $1 at start, contradicting the chart subtitle (and the 'Nx' legend is anchored differently from BTC)

- **Files:** `liquidity_migration/volume_events_charts.py:78-86,136,477-486 (strategy used raw; only BTC divided by base[0]); trade_lifecycle.py:74 (first equity row = 1+r0)`
- **Fix:** Normalise the strategy series like BTC (divide by strategy[0] or prepend a synthetic 1.0 anchor) before plotting and before the legend multiple, so both lines start at exactly 1.0x and match the subtitle.
- **Effort:** S · **Risk:** Low — report-only; cosmetic-but-misleading. Pair with rank 26 and the chart test (rank 28).

### #28 — [LOW / test_gap] Chart equity/monthly-table tests seed an artificial $1.0 first row that production never emits, masking the normalisation and compounding mismatches

- **Files:** `tests/test_liquidity_migration_volume_events.py:967-997,1010-1019 (hand-seeded equity=1.0/basket_return=0.0 leading row; assertions check only PNG existence + series length)`
- **Fix:** Add a test building equity via build_equity_curve from real baskets (first row = 1+r0) asserting the plotted strategy series starts at 1.0x after the rank-27 fix; add a test asserting the monthly table reconciles with the equity line for a multi-same-day-exit fixture.
- **Effort:** S · **Risk:** Low — test-only; unmasks ranks 26-27.

### #29 — [LOW / correctness] _aggregate_bybit_closures over-merges distinct closures when Bybit createdTime is 0/missing

- **Files:** `liquidity_migration/reconciliation.py:810-843 (created_ts_ms=0 when both createdTime/updatedTime absent; clusters within 120s window)`
- **Fix:** Do not cluster rows with created_ts_ms<=0 (treat a missing timestamp as its own singleton cluster), or use a per-order id / closedSize disambiguator before merging on a zero timestamp.
- **Effort:** S · **Risk:** Low — offline audit report only, cannot corrupt trades; affects operator-facing reconcile fidelity.

### #30 — [LOW / accounting] Untracked-position exit orders always route to the SHORT orders ledger regardless of which sleeve's position is being flattened

- **Files:** `liquidity_migration/ws_risk.py:1598-1618 (exit_untracked_positions order dict has no sleeve key) -> _sleeve_of:353-360 (defaults to 'short')`
- **Fix:** Tag the untracked-exit order's sleeve from the venue position side before _write_order_rows_routed, or route untracked-exit orders to a dedicated 'untracked' bucket so they don't pollute short-sleeve order analytics.
- **Effort:** S · **Risk:** Low — analytics pollution only; the position is still force-closed correctly.

### #31 — [LOW / accounting] Adopted-position orphan close records net_return=0 because equity_usdt is never set on adopted trade rows

- **Files:** `liquidity_migration/event_demo_exits.py:1532-1538 (_orphan_close_pnl_backfill uses notional_weight=_safe_ratio(notional,equity)); ws_risk.py:1389-1448 (_build_adopted_trade rows omit equity_usdt); event_demo.py:2896-2900 (_safe_ratio returns 0.0 on missing denom)`
- **Fix:** Populate equity_usdt on adopted trade rows from the latest equity snapshot (mirror cycle entries), or compute net_return directly from realized PnL/equity at close when equity_usdt is absent.
- **Effort:** S · **Risk:** Low — net-return aggregation under-counts adopted (post-restart) trades; gross is correct.

### #32 — [LOW / accounting] Per-split drawdown gate uses spurious per-basket cumprod, diverging from the whole-period daily-grid drawdown the engine itself warns against

- **Files:** `liquidity_migration/volume_events.py:1753-1763 (_summarize_basket_split: np.cumprod over per-basket basket_return); cf. trade_lifecycle.py:57-63 build_equity_curve docstring`
- **Fix:** Compute the split drawdown from build_equity_curve(part) (the daily-grid curve), mirroring how sharpe is already computed in the same function: eq=build_equity_curve(part); max_drawdown=float(eq['drawdown'].min()).
- **Effort:** S · **Risk:** Low — reporting metric; not numerically behavior-preserving (changes the reported split DD) so test-gate against a fixture and confirm it matches the whole-period gate convention.

### #33 — [LOW / accounting] worst_day_return compounds concurrent same-day baskets multiplicatively while the equity curve sums them additively

- **Files:** `liquidity_migration/trade_lifecycle.py:280 (_worst_volume_day_return uses (basket_return+1).product()-1)`
- **Fix:** Sum same-day basket returns to match build_equity_curve: group_by(exit_date).agg(pl.col('basket_return').sum()).min().
- **Effort:** S · **Risk:** Low — reporting metric; changes the reported worst-day number (not behavior-preserving). Test-gate.

### #34 — [LOW / correctness] Annualized-return / MAR explode on degenerate single-day equity spans via a 1e-9 years floor

- **Files:** `liquidity_migration/continuous_events.py:474-488 (_additive_summary years=max(span,1e-9)),513-517 (_daily_pnl_metrics)`
- **Fix:** Floor years at a meaningful minimum (max(span_days,1)/365.25 or require span_days>=2 before annualizing) and return mar=None / annualized_return=0.0 for single-day spans, mirroring _daily_sharpe's span_days>=2 guard.
- **Effort:** S · **Risk:** Low — affects degenerate single-day reports only; metric robustness.

### #35 — [LOW / correctness] r1_robustness bootstrap mar_delta_p_gt0 divides finite-positive count by the full sample count — NaN MAR samples deflate the reported P(MAR delta>0)

- **Files:** `scripts/r1_robustness.py:222 (numerator excludes non-finite, denominator includes them)`
- **Fix:** finite=[d for d in mar_deltas if isfinite(d)]; mar_delta_p_gt0 = sum(d>0 for d in finite)/len(finite) if finite else nan.
- **Effort:** S · **Risk:** Low — research diagnostic; understates MAR fragility (makes it look stronger). Affects Tier-2 reporting, not a live path.

### #36 — [LOW / accounting] verify_div_promotion Sharpe uses per-exit returns (not calendar-day-gridded) and ddof=0, diverging from the engine's honest daily Sharpe

- **Files:** `scripts/verify_div_promotion.py:42`
- **Fix:** Reuse trade_lifecycle._daily_sharpe on a built equity curve (calendar-day forward-fill + ddof=1) and standardize on 365.25 days/yr, so the verification Sharpe matches the canonical engine metric.
- **Effort:** S · **Risk:** Low — verification helper only; inflated inter-exit Sharpe vs engine.

### #37 — [LOW / correctness] write_dataset(append=False, partition_by date) leaves orphan date partitions on a shrinking manifest rebuild

- **Files:** `liquidity_migration/storage.py:331-360 (_write_dataset_unlocked: only partitions present in df are written)`
- **Fix:** Document append=False as partition-local overwrite (NOT whole-dataset replace) and add an explicit replace=True that rmtree's the dataset dir under exclusive_file_lock before writing (fold binance_vision's manual rmtree dance into storage so every replace path is lock-protected).
- **Effort:** M · **Risk:** Medium — shared storage primitive; the replace=True path must stay atomic vs concurrent readers. Underpins ranks 24 and 38.

### #38 — [LOW / concurrency] rewrite_manifest_to_coverage rmtree's the manifest outside the dataset lock -> concurrent readers can see an empty/partial manifest

- **Files:** `liquidity_migration/binance_vision.py:207-210`
- **Fix:** Move the destructive replace inside the lock via the storage replace=True mode (rank 37) instead of an unlocked shutil.rmtree followed by a separately-locked write.
- **Effort:** S · **Risk:** Low-medium — race window between rmtree and the locked write; depends on rank 37's replace mode.

### #39 — [LOW / observability] read_dataset_columns only self-heals SchemaError; a genuinely corrupt parquet partition propagates and halts the live cycle

- **Files:** `liquidity_migration/storage.py:398-409`
- **Fix:** In the fallback catch (SchemaError, ComputeError, ArrowError) and when a specific pl.read_parquet fails, log the exact path before re-raising so the operator can quarantine/rebuild that one partition. Fail loud but identified; do not silently skip rows.
- **Effort:** S · **Risk:** Low — improves operator recoverability; keep the fail-loud contract.

### #40 — [LOW / correctness] v5 manifest tail-fill caps coverage on date.today() (local TZ) instead of UTC when --end is omitted

- **Files:** `liquidity_migration/archive_manifest.py:236,243 (synthesize_v5_listing_manifest_rows uses date.today())`
- **Fix:** Use datetime.now(tz=UTC).date() so the no-end branch matches the codebase's UTC day convention (the VPS is UTC+8, so local date rolls 8h early).
- **Effort:** S · **Risk:** Low — one-day boundary on a no-end invocation; the reconcile flow passes explicit ends.

### #41 — [LOW / config] Inactive-by-default liquidity_migration filter knobs lack range validation, allowing silently-empty or always-pass gates

- **Files:** `liquidity_migration/volume_events_validation.py:135-279 (_validate_liquidity_migration_config: up_volume_concentration_min, signal_last6h_turnover_share_max, residual_momentum_max, market_*_max unvalidated)`
- **Fix:** Add domain checks: 0<=up_volume_concentration_min<=1, 0<=signal_last6h_turnover_share_max<=1, 0<=market_pct_up_*<=1 (if probability), residual_momentum_max finite — making a misconfigured sweep fail loudly instead of silently zeroing the cross-section.
- **Effort:** S · **Risk:** Low — pure pre-run validation; fully behavior-preserving for valid configs.

### #42 — [LOW / config] _merge_universe_config silently ignores unknown YAML keys (asymmetric with _merge_dataclass which raises)

- **Files:** `liquidity_migration/config.py:170-180`
- **Fix:** Route universe through _merge_dataclass for uniform unknown-key rejection, or add the same unknown-key guard at the top of _merge_universe_config.
- **Effort:** S · **Risk:** Low — limited to the scouting/benchmark universe block; fixes a fail-loud inconsistency.

### #43 — [LOW / correctness] ws_risk.ws_exit builds split sub-order links without the 36-char-safe helper used by the entry path

- **Files:** `liquidity_migration/ws_risk.py:1095 (sub_link = f'{base_link}-s{idx}' without _split_order_link_id); cf. event_demo.py:2784`
- **Fix:** Use _split_order_link_id(base_link, idx) so the unique -s{idx} suffix always survives truncation. Add a test that ws_exit sub-links are unique and <=36 chars for a long base.
- **Effort:** S · **Risk:** Low — benign today (wx base ~24 chars) but a latent orderLinkId collision on a longer base. Behavior-preserving for current link lengths.

### #44 — [LOW / concurrency] WS-timeout double-submit probe does not run for malformed_ack/exception failure kinds (only timeout)

- **Files:** `liquidity_migration/bybit.py:910-921 (place_order probe gated on kind=='timeout'),999-1016 (_ws_call_sync raises 'exception'/'malformed_ack')`
- **Fix:** Extend the probe to kind in {timeout, exception, malformed_ack} (any unknown-venue-state kind); keep skipping only kind=='rejected'. Relies on Bybit per-orderLinkId dedup as the backstop.
- **Effort:** S · **Risk:** Low-medium — touches the live order resubmit path; the probe is read-only so extending it is safe, but test the new kinds route correctly.

### #45 — [LOW / correctness] WS-timeout probe treats a Rejected/Cancelled order in history as 'order present' and suppresses the REST resubmit

- **Files:** `liquidity_migration/bybit.py:1030-1073 (_probe_existing_order: no orderStatus filter; returns any matching-link row)`
- **Fix:** Filter the probe to OPEN/active or filled statuses (New, PartiallyFilled, Filled, Untriggered); ignore Rejected/Cancelled/Deactivated so a rejected WS submit falls through to REST (which re-rejects cleanly) rather than being reported as recovered.
- **Effort:** S · **Risk:** Low-medium — live order path; changes when a resubmit happens. Test with a rejected-row history.

### #46 — [LOW / accounting] ⛔ Circuit breaker counts left_decile profit covers that closed marginally negative as 'adverse', over-pausing entries

- **Files:** `liquidity_migration/continuous_demo.py:599-604 (_recent_adverse_exit_count: net_return<0 counts any exit_reason)`
- **Fix:** Count only exit_reason in {stop_approach, failed_fade} (explicit loss-cuts), or require net_return below a small negative threshold, so break-even state-exits don't inflate the adverse tally. Document the chosen definition.
- **Effort:** S · **Risk:** Low — changes when the (operator-enabled) breaker pauses entries; the breaker only ever pauses, never adds risk. Live behavior change — coordinate with the operator's breaker config.

### #47 — [LOW / config] LivePanelCache uses construction-time rmom_quantile/exclude, not the per-call config — silent drift surface

- **Files:** `liquidity_migration/continuous_demo.py:235-237,389 (cross_sectional_decile uses self._rmom_quantile) vs build_live_continuous_state:201 (config.rmom_quantile)`
- **Fix:** Read rmom_quantile/exclude from the config passed to state()/_live_panel/_refresh, OR assert self._rmom_quantile==config.rmom_quantile at the top of state() so a mismatch fails loudly.
- **Effort:** S · **Risk:** Low — daemon constructs both from the same frozen config today; the assert is behavior-preserving and guards a future fallback-vs-cache divergence.

### #48 — [LOW / correctness] LivePanelCache vov diverges from the full recompute when the synthetic bar's rv_168h is null (latent, unreachable on realistic data)

- **Files:** `liquidity_migration/continuous_demo.py:367-373 (rv_cur None => vov_cur None, but polars rolling_std emits vov at a null position with >=168 valid in-window)`
- **Fix:** Compute vov_cur from the rv window regardless of rv_cur being None (build from the non-null rv tail), OR add an assertion/comment pinning the 'rv_cur None => vov None is equivalent' invariant so a window-size change is caught.
- **Effort:** S · **Risk:** Low — currently unreachable (a symbol with 168 valid rv cannot have null current rv); the comment/assert is the safe minimum.

### #49 — [LOW / correctness] decompose_strategy_pnl empty/no-factor early return omits documented keys (n_unresolved, resolved_fraction)

- **Files:** `liquidity_migration/risk_model.py:350-351`
- **Fix:** Add 'n_unresolved': 0, 'resolved_fraction': 0.0 to the early-return dict so callers reading the documented trustworthiness fields don't KeyError on degenerate inputs.
- **Effort:** S · **Risk:** Low — fully behavior-preserving on the success path; fixes a return-shape inconsistency.

### #50 — [LOW / concurrency] Continuous protective-exit thread can still submit reduce-only orders during the base daemon's shutdown teardown

- **Files:** `liquidity_migration/continuous_demo_daemon.py:207-212 (run: _stop_protective_exit_monitor in finally, after super().run() teardown clears the router)`
- **Fix:** Stop the protective-exit monitor BEFORE delegating to super().run()'s teardown (join the protective thread at the start of shutdown; it already wakes on _tick_event) so no fast-loop submission overlaps base WS teardown.
- **Effort:** S · **Risk:** Low — narrow shutdown-window race; the trade router is not closed by _close_ws, so impact is bounded. Test the shutdown ordering.

### #51 — [LOW / test_gap] deploy_vps_live.sh restart order (risk before continuous) is untested

- **Files:** `tests/test_runtime_scripts.py:~441-443 (presence-only assertions); deploy_vps_live.sh:134(risk),142(continuous)`
- **Fix:** Assert text.find('restart liquidity-migration-bybit-risk') < text.find('restart liquidity-migration-bybit-continuous-demo') (and the same for enable lines).
- **Effort:** S · **Risk:** Low — test-only; pins the reboot-safety invariant the docs lean on.

### #52 — [LOW / test_gap] Live continuous-decile blackout (stale rmom -> empty cross-section) is not directly tested; parity test covers only the fresh case

- **Files:** `tests/test_liquidity_migration_continuous_demo.py:50-64 (only the fresh-rmom decile parity asserted)`
- **Fix:** Add a test: rmom table whose newest row is yesterday/absent for today; assert build_live_continuous_state returns empty/clearly-flagged and the cycle telemetry surfaces max_rmom_day_ts/rmom_stale_days so the blackout is observable. Complements the 2026-06-02 fix.
- **Effort:** S · **Risk:** Low — test-only; pins the recently-fixed CRITICAL blackout class.

### #53 — [LOW / test_gap] rmom silent-blackout root-cause fix (precompute --end default + .shift(1) causality) has NO test

- **Files:** `scripts/precompute_residual_momentum.py:54-57 (_default_end),70 (.shift(1))`
- **Fix:** Tests: (a) _default_end()==(utcnow.date()+1d).isoformat() with a frozen clock; (b) tiny synthetic root -> precompute writes a row for today's trading day; (c) the causality test from rank 3 (residual_momentum[D] independent of D's own residual_return).
- **Effort:** S · **Risk:** Low — test-only; part (c) is the verification harness for rank 3.

### #54 — [LOW / test_gap] Safety watchdog orchestration (gather_alerts / gather_continuous_alerts) is untested — only leaf deciders are

- **Files:** `scripts/check_demo_liveness.py:347 (gather_alerts),386 (gather_continuous_alerts)`
- **Fix:** Integration tests over a tmp data root: seed cycles + an open trade, monkeypatch _venue_positions to return a wrong/missing stopLoss and assert the 'unprotected:*' CRITICAL; a second case where _venue_positions returns perr and assert 'stop_verify_unavailable' WARNING fires.
- **Effort:** M · **Risk:** Low — test-only; covers the stop-protection paging path that backstops the live ledger.

### #55 — [LOW / test_gap] No automated parity test for the live==backtest failed-fade exit (claimed 'verified' in a comment only); close-location helper duplicated

- **Files:** `liquidity_migration/event_demo.py:2623 (_failed_fade_exit_since_entry) vs volume_events.py:1539 (_failed_fade_exit_hit); event_demo.py:2671 (_completed_bar_close_location) duplicates volume_events.py:1172`
- **Fix:** Parity test: drive _simulate_indexed_trade (backtest) and _failed_fade_exit_since_entry (live) with matched bar sequences/config and assert agreement on whether/when/at-what-price ff6 fires. Separately dedupe _completed_bar_close_location by importing _bar_close_location (behavior-preserving).
- **Effort:** M · **Risk:** Low — test + a behavior-preserving dedupe; pins the ff6 exit now in the live promoted profile.

### #56 — [LOW / test_gap] _tag_sleeve_from_trades symbol-fallback can mis-route when a trade_id miss coincides with a cross-sleeve same-symbol position; untested

- **Files:** `liquidity_migration/ws_risk.py:1014 (first-wins symbol_index),1025-1026 (fallback returns 'short')`
- **Fix:** Add a test with the same symbol under both 'short' and 'continuous' sleeves and a row whose trade_id is absent + sleeve blank; assert the route. Prefer matching the open trade_id (or refuse to guess and log) over first-wins by symbol.
- **Effort:** S · **Risk:** Low — narrow path (Rule A account-wide same-symbol exclusion keeps sleeves disjoint today); a test + a more conservative fallback.

### #57 — [LOW / test_gap] Daemon scheduler tests assert tight wall-clock windows on real threads (flaky-by-construction)

- **Files:** `tests/test_liquidity_migration_event_demo_daemon.py:562-566,591,599-601 (0.18<period<0.34 around 0.25s on a real thread)`
- **Fix:** Make the scheduler injectable (fake monotonic clock + controllable sleep, or expose next-fire as a pure function) and assert the computed schedule deterministically; if real timing must stay, widen bounds / assert only the qualitative overrun property.
- **Effort:** M · **Risk:** Low — CI-stability only; no production change.

### #58 — [LOW / config] continuous-events CLI exposes none of the inherited-from-daily ablation knobs the config defines; default end_date hardcoded to 2026-05-28

- **Files:** `liquidity_migration/continuous_events.py:62 (end_date='2026-05-28', surfaced via cli.py:1976); cli.py:1969-2012,2888-2901 (ablation knobs not exposed)`
- **Fix:** Default end_date to None resolved to tomorrow-UTC at run time (mirror precompute_residual_momentum._default_end) OR warn when the resolved end is in the past; leave the scripts' explicit pin untouched. Add the deployed-relevant ablation flags (or a parser-help note that continuous-events is the baseline proxy-parity command).
- **Effort:** S · **Risk:** Low — research CLI surface; the same stale-hardcoded-date class STATE.md flags as a recurring footgun, but driving scripts pin the window explicitly.

### #59 — [LOW / observability] Contradictory/stale comment in deploy_vps_live.sh: says continuous sleeve ships SUBMIT_ORDERS=0=dry-run while the verify asserts SUBMIT_ORDERS=1 (and the unit is live)

- **Files:** `scripts/deploy_vps_live.sh:121-123 (comment) vs 211 (asserts Environment=SUBMIT_ORDERS=1)`
- **Fix:** Update the comment to reflect the 2026-06-01 go-live: ships SUBMIT_ORDERS=1; the verify hard-fails the deploy if it is not submitting; pause via SUBMIT_ORDERS=0.
- **Effort:** S · **Risk:** Low — comment-only; prevents an operator misreading the live state.

### #60 — [LOW / modularization] Generic trade/position/coercion primitives marooned in the 3096-line event_demo.py force 7 modules to import the hub as a utility grab-bag

- **Files:** `liquidity_migration/event_demo.py (~2477-2979: _float/_bool/_decimal_text/_quantity_text/_safe_ratio/_active_position_by_symbol/_price_lookup_from_positions/_open_trades/_empty_trades/_upsert_rows/_safe_raw_positions/_safe_open_orders/...); ws_risk.py imports 40 symbols from it`
- **Fix:** Extract position/ledger primitives into liquidity_migration/trade_primitives.py and the pure scalar coercions into _common (or a small module); re-import in event_demo. Behavior-preserving code movement gated by the existing suite.
- **Effort:** L · **Risk:** Low if purely mechanical and test-gated, but high churn touching the live reconcile authority's imports — do as one isolated, reviewed move.

### #61 — [LOW / modularization] EventWebSocketRiskEngine is a 60-method, 1880-line god-class; the orphan-adoption subsystem is a clean extraction seam

- **Files:** `liquidity_migration/ws_risk.py:245-2125 (class); adoption cluster 1273-1638`
- **Fix:** Extract the adoption subsystem (adopt_untracked_positions, _build_adopted_trade, _recover_entry_link_metadata, _adopt_strategy_id_for_sleeve, exit_untracked_positions, reconcile_untracked_exit_orders) into ws_risk_adoption.py as free functions taking the engine/state explicitly, called from thin methods; preserve the consumer-thread-only mutation contract.
- **Effort:** L · **Risk:** Medium — money-critical state machine; behavior-preserving move gated by the ws_risk tests, but review carefully. Do after the quick wins.

### #62 — [LOW / modularization] cli.main() is an 858-line flat command dispatch with 200+ line inline arg->config builders

- **Files:** `liquidity_migration/cli.py:2202-3060 (main; volume-events handler 2676-2886 ~210 lines; 30 if args.command branches)`
- **Fix:** Follow the _run_signal_harness precedent: extract each handler into _cmd_<name>(args, data_root)->int and dispatch from a {command: handler} dict; at minimum extract _volume_event_config_from_args / _continuous_config_from_args (mirror the existing _universe_config_from_args).
- **Effort:** L · **Risk:** Low if mechanical and test-gated; reduces drift between the arg map and the ~120-field config.

### #63 — [NIT / modularization] 14 detect_pattern_* predicates form a self-contained ~470-line cluster that should be its own module

- **Files:** `liquidity_migration/long_native.py:876-1321 (detect_pattern_* + _fc_exit_params/_coin_track_record_scale/_fc_alpha_score/_classify_entry)`
- **Fix:** Move into liquidity_migration/long_native_patterns.py taking LongNativeConfig as a typing import; import back into long_native. Pure code movement.
- **Effort:** M · **Risk:** Low — high-cohesion pure predicates; test-gated mechanical move.

### #64 — [LOW / duplication] Two _safe_ratio functions with DIVERGENT semantics (0.0 vs NaN) — a dedup trap

- **Files:** `liquidity_migration/event_demo.py:2896 (returns 0.0, !=0 guard) vs volume_events.py:1847 (returns NaN, bottom<=0 guard; feeds the turnover_ratio filter)`
- **Fix:** Do NOT merge. Rename to intent: event_demo -> _ratio_or_zero, volume_events -> _ratio_or_nan; add a one-line comment at each site noting the deliberate semantic difference so a future cleanup doesn't collapse them.
- **Effort:** S · **Risk:** Low — rename + comment; collapsing them WOULD change which events pass the engine threshold, so the value of this finding is preventing a future wrong dedup.

### #65 — [LOW / duplication] Three separate _empty_trades() definitions (event_demo, long_native, trade_lifecycle) with different schemas reached by different imports

- **Files:** `liquidity_migration/event_demo.py:2979; long_native.py:1804; trade_lifecycle.py:559`
- **Fix:** Do NOT blindly merge (schemas differ). Rename for clarity: long_native -> _empty_long_trades, demo-ledger -> _empty_demo_trades, keep trade_lifecycle._empty_trades as the canonical backtest one. Diff the column sets first; if any two are identical, delete one and import the other.
- **Effort:** S · **Risk:** Low — rename/clarify; prevents a wrong-schema merge.

### #66 — [NIT / duplication] long_native._safe_float duplicates _common.finite_float (79 call sites)

- **Files:** `liquidity_migration/long_native.py:1938 vs _common.py:24`
- **Fix:** Replace _safe_float with `from ._common import finite_float; _safe_float = finite_float` (keep the local name). Note finite_float rejects +/-inf (returns None) which _safe_float passes through — verify no long feature relies on inf surviving (a return/funding inf is already a data bug).
- **Effort:** S · **Risk:** Low — verify the inf-handling difference is inert before aliasing.

### #67 — [NIT / duplication] Three date-string->epoch-ms parsers (downloaders.parse_date_ms, _common.date_ms, volume_events._date_ms thin wrapper)

- **Files:** `liquidity_migration/downloaders.py:54; _common.py:72; volume_events.py:2211`
- **Fix:** Replace downloaders.parse_date_ms body with date_ms() (keep its non-raising-on-empty contract if any caller needs it — CLI passes required args). Delete volume_events._date_ms/_pct pass-throughs and point their callers at _common directly.
- **Effort:** S · **Risk:** Low — consolidates to one UTC-aware parser; verify the empty-string contract.

### #68 — [NIT / config] universe_current partitioned by snapshot_date but dedup-keyed by (snapshot_ts_ms, symbol) — benign today but fragile

- **Files:** `liquidity_migration/universe.py:55 vs storage.DATASET_KEYS['universe_current']`
- **Fix:** Either partition by snapshot_date AND key dedup on (snapshot_date, symbol) so a same-day re-snapshot replaces cleanly, or document universe_current as intentionally one-snapshot-per-write and keep append=False. No functional change today.
- **Effort:** S · **Risk:** Low — tighten before any append=True usage.

### #69 — [NIT / dead_code] ws_klines_* config kwargs use defensive getattr fallbacks for flags always added to the parser (dead defensive code with a hardcoded True default)

- **Files:** `liquidity_migration/cli.py:2422-2428,2608-2614`
- **Fix:** Replace getattr(args,'ws_klines_enabled',True) with direct args.ws_klines_enabled (flags are always present), or make the fallback demo_defaults.ws_klines_enabled instead of a literal True.
- **Effort:** S · **Risk:** Low — removes a dead branch that could silently re-assert True if the config default ever flips.

### #70 — [NIT / dead_code] EventDemoDaemon._next_cycle_at is frozen in event-driven mode; the kline-warmer 'room until next cycle' guard reads a stale value

- **Files:** `liquidity_migration/event_demo_daemon.py:490 (set once),640/652 (timer-only update),987 (warmer reads)`
- **Fix:** Set _next_cycle_at = time.monotonic()+self._max_idle_seconds at the top of _wait_for_next_cycle_event so the warmer's room estimate stays meaningful, or assert/comment that the warmer is mutually exclusive with event-driven mode (it currently is).
- **Effort:** S · **Risk:** Low — latent/harmless (warmer disabled under the WS manager); clarity fix.

### #71 — [NIT / dead_code] ⛔ Dead config surface: TradeFlowConfig and exchange.name/settle_coin are loaded and YAML-settable but never consumed

- **Files:** `liquidity_migration/config.py:43-47 (TradeFlowConfig),37/39 (ExchangeConfig.name/settle_coin); volume_alpha.default.yaml trade_flow block`
- **Fix:** Remove TradeFlowConfig + the YAML trade_flow block and the unused ExchangeConfig fields, OR add an inline comment marking them currently-unconsumed so no one assumes exclude_block_trades/exclude_rpi_trades take effect. (Note: editing volume_alpha.default.yaml is operator-gated per STATE.md non-negotiable #4.)
- **Effort:** S · **Risk:** Low for the config.py comment; the YAML edit needs operator sign-off.

### #72 — [NIT / config] Continuous CLI config construction ignores the reactivity/risk tuning knobs — env cannot configure stop_approach, hysteresis, cooldown, circuit breaker, or panel cache

- **Files:** `liquidity_migration/cli.py:2641-2651 (ContinuousDemoCycleConfig construction omits protective_exit_*, stop_approach_frac, exit_decile_buffer, entry_pause_*, live_panel_cache_enabled, ticker_batch_wake_*)`
- **Fix:** Add CLI args + run-script env mappings for at least entry_pause_after_adverse_exits/-window-minutes, stop_approach_frac, exit_decile_buffer, protective_exit_enabled and thread them into ContinuousDemoCycleConfig so operators can adjust the in-sample-only risk machinery without a code change.
- **Effort:** M · **Risk:** Low — additive plumbing; lets the operator tune live risk levers safely.

## Recommended roadmap

### Wave A — safe quick wins (behavior-preserving cleanups, dead code, pure validation, comment fixes)

- **Items:** ranks 41 (filter range validation), 42 (universe unknown-key guard), 49 (decompose early-return keys), 59 (deploy comment), 69 (dead getattr fallbacks), 70 (_next_cycle_at clarity), 64-68 (rename/comment the divergent duplicates; alias finite_float; consolidate date parsers), 47 (LivePanelCache config assert), 48 (vov invariant assert/comment), 17 & 18 (validate-raise option only — reject cooldown_hours/hold_hours until supported)
- **Why:** Zero or provably-inert behavior change; each is small and individually test-gateable. Clears the noise and fixes fail-loud inconsistencies the codebase already values, without touching alpha or the live trade path.

### Wave B — test gaps that pin live-critical behavior (test-only, no production change)

- **Items:** ranks 6 (ws_risk empty-fetch orphan-close), 52 & 53 (continuous rmom blackout + precompute causality/default), 54 (gather_alerts stop-protection orchestration), 51 (deploy restart order), 11 & 12 (golden + deploy-gate pins for rmom_quantile=0.33 and continuous live config), 55 (ff6 live==backtest parity + close-location dedupe), 56 (cross-sleeve symbol-fallback), 57 (deterministic scheduler tests), 28 (chart equity/table tests)
- **Why:** These lock down the highest-consequence past-incident classes (mass false orphan-close, silent rmom blackout) and the operator-directed live values against silent regression. Doing tests before fixes also produces the verification harness Wave C needs.

### Wave C — verified correctness fixes on live/reporting paths (test-gated)

- **Items:** ranks 1 (+9: continuous order-submit + paper_mode guard), 2 (WS-silence watchdog), 4 (long incomplete-bar entry), 5 (order-size splitter), 7 (reconcile resurrect), 10 (reset script coverage), 13 (rejection-trace), 14 (ws_risk state pruning), 15 (ticker shrink guard), 16 (fast-loop staleness), 19 (wallet zero-equity), 20 (leading-edge gap), 22 (HWM lock), 30/31 (untracked/adopted accounting), 29 (closure over-merge), 32-40 (split-DD, worst-day, MAR floor, r1 p-value, verify_div Sharpe, storage replace mode, manifest lock + corrupt-partition logging, UTC today), 43-45 (ws_exit link helper, probe kinds, probe status filter), 50 (shutdown ordering)
- **Why:** Each is a concrete correctness/observability fix with a clear local test. Rank 1 and rank 2 are the urgent pair (security gap on the live order path + a dead operator watchdog). Sequence rank 37 (storage replace mode) before 24 & 38 (they depend on it).

### Wave D — OPERATOR-GATED (move the backtest/live signal, or are profile/deploy/data decisions — NOT autonomous)

- **Items:** rank 3 (residual-momentum look-ahead — run the causality test, present evidence + exact lag fix; any change is a signal/profile change), rank 25 (decompose day-grid off-by-one — feeds the Tier-3 real-money gate metric), rank 8 (age300 live vs backtest pit_age definition — option (b) shifts the promoted profile's live selection), rank 21 (continuous rmom lookback vs VPS memory), rank 46 (circuit-breaker adverse definition — changes the operator-enabled live breaker), rank 71 (YAML trade_flow removal — volume_alpha.default.yaml is operator-locked). Also: running scripts/reset_demo_paper_ledgers.sh (rank 10's actual reset) and any deploy/push are operator steps.
- **Why:** These touch alpha, the promotion gate, the live profile, the locked config, or live demo ledgers. STATE.md non-negotiables forbid autonomous profile/real-money/config changes; the methodology items must go through a test + an operator decision, not a silent edit.

### Wave E — god-file modularization (behavior-preserving moves, do last, one at a time)

- **Items:** rank 60 (extract trade_primitives.py from event_demo.py), rank 62 (cli.main per-command handlers + arg->config extractors), rank 63 (long_native_patterns.py), rank 61 (ws_risk adoption subsystem — most caution, money-critical state machine)
- **Why:** High churn touching imports across many modules; gate each on the full suite and review individually. Sequence the ws_risk extraction last and most carefully since ws_risk is the single live reconcile authority.

## Quick wins (do first)

- rank 41 — add domain/range validation for the inactive-by-default liquidity_migration filter knobs (volume_events_validation.py) so a misconfigured sweep fails loudly instead of silently zeroing the cross-section. Pure pre-run check, behavior-preserving.
- rank 49 — add n_unresolved/resolved_fraction to decompose_strategy_pnl's empty early-return (risk_model.py:350-351). One-line fix; prevents a KeyError on exactly the degenerate inputs a trustworthiness gate inspects.
- rank 59 — correct the stale deploy_vps_live.sh comment claiming the continuous sleeve is dry-run when the deploy hard-asserts SUBMIT_ORDERS=1. Comment-only; prevents an operator misreading live state.
- rank 6 — add a fail_positions path to FakePrivateClient and a ws_risk engine test asserting open trades survive a transient get_positions failure (the C1 mass-false-orphan-close class). Test-only, pins the worst past incident.
- rank 11 + 12 — golden test asserting ContinuousDemoCycleConfig().rmom_quantile==0.33 (and the breaker/stop values) + extend the deploy/verify assertion blocks to pin the continuous live config. Protects the single operator-directed alpha lever from a silent revert.
- rank 5 — fix _split_qty_for_max_order_size to build subs greedily so every sub <= cap, with an exhaustive coarse-step test. Small, provably-safer live-execution fix (prevents a rejection -> under-fill).
- rank 1 — add _validate_continuous_demo_config calling validate_order_submit_allowed + the paper_mode guards (closes ranks 1 and 9 together). Small, fail-fast-only; restores the repo's money-safety invariant on the one sleeve missing it (verify the live unit passes --confirm-demo-orders first).
- rank 10 — add the three continuous_fade_demo_* datasets to reset_demo_paper_ledgers.sh PAIRS + a coverage regression test. Small; the script edit is safe (running the reset stays an operator step).

## Coverage gaps (honest disclosure — NOT audited / needs a live run)

- No live/runtime confirmation possible: the box OOMs above 16GB so no backtest or full continuous-events run was executed. The residual-momentum look-ahead (rank 3), the day-grid off-by-one in decompose_strategy_pnl (rank 25), and the age300 live!=backtest divergence (rank 8) are reasoned from code + day-grid conventions, not from a reproduced numerical diff. Each needs the proposed causality/parity test run before the fix is trusted.
- WS/threading races (ranks 2,7,22,50) were reasoned from source ordering, not observed under load. The seed()-then-_check_ws_health re-seed (rank 2) is mechanically certain; the reconcile-resurrects-closed-position race (rank 7) depends on real REST-fetch latency vs WS push timing that only a live or fault-injected run can confirm.
- pybit WS internals and Bybit ack/dedup semantics (ranks 44,45) were not exercised against the real venue — the probe behavior on malformed_ack/exception/rejected is inferred from _ws_call_sync raising and the place_order gate; the Bybit per-orderLinkId dedup that backstops a double-submit is assumed correct, not verified.
- The exact day-grid alignment of signal_ts_ms (day-end 00:00 of D+1) vs the live continuous within-day decision vs fwd_ret_1d completion was traced through build_factor_panel/build_feature_panel partially; the precise correct lag (shift(1) vs shift(2) for the residual-momentum, and the snap-to-decision-day for decompose) needs a focused PIT trace + the causality test, not the static read I did.
- Several modules were only spot-checked, not read in full: binance_vision.py (only the rewrite_manifest path), downloaders.py, pit_coverage.py, reconciliation.py beyond _aggregate_bybit_closures, and the full continuous_events.py accounting/MTM (STATE.md says it was audited 2026-06-01; I did not re-verify the telescoping/funding-to-exit math).
- Test-coverage findings (ranks 6,28,51-57) identify missing tests; I confirmed the tests are absent but did not write them, so the underlying behaviors they would pin remain unverified at runtime (notably the gather_alerts stop-protection orchestration and the live-vs-backtest ff6 parity).
- REFUTED items were taken on the verifiers' word and not independently re-checked — in particular 'continuous protective fast-loop consumes ticker-cache prices with no staleness gate' appears in BOTH the refuted list and as a confirmed finding (rank 16, verified=true); I kept the confirmed cite (continuous_demo.py:1058-1077) but the contradiction suggests the staleness behavior deserves a definitive live trace.
- Whether the live continuous systemd unit actually passes --confirm-demo-orders is unknown (cannot read the VPS units); the rank-1 fix must be checked against the deployed unit so the new gate does not block the operator-directed go-live.

## Refuted findings (adversarial check killed these — do NOT re-chase)

- **Continuous engine forward klines padding ignores max_hold_hours, truncating state-mode trades near the window end into premature data_end exits** — REFUTED. I read continuous_events.py in full and traced the cited code. The cited lines are accurate: pad_fwd = (hold_hours + entry_delay_hours + 4)*MS_PER_HOUR (line 626), and the state-mode planned_exit = min(spell_end + delay_ms, entry_bar_end + max_hold_ms) (line 404). exit_mode='state' is a rea
- **Split entry whose FIRST sub-order fails to submit but a later sub fills leaves an unprotected, trade-unledgered live position** — The finding's central mechanism is factually wrong. It claims that when the FIRST sub-order's place_order fails but a later sub fills, `submit_mode` stays "error", so the fill-aggregation block at line 548 (`if submit_mode == "submitted":`) never runs, leaving `filled_qty == 0.0`, the stop-repair bl
- **Shared REST rate limiter holds its lock across time.sleep, serializing all concurrent workers behind one throttled caller** — I read BybitRestRateLimiter.acquire (bybit.py:132-148) in full and traced every call site (event_demo_data.py:455-473, kline_stream_manager.py:416-531, plus the private-client paths in bybit.py:663-681).

STRUCTURAL claim is TRUE: time.sleep(wait) at line 143 executes inside `with self._lock:` (open
- **Single shared last_event_monotonic lets a live wallet/order stream mask a dead position stream in is_stale()** — FACTUAL CLAIM IS ACCURATE BUT THE HARM IS NOT. I confirmed the literal code: on_position_event (ws_state_cache.py:231), on_order_event (266), on_wallet_event (288) all write the single self._stats.last_event_monotonic, and is_stale() (342-345) / seconds_since_last_event() (336-340) key solely on tha
- **Continuous protective fast-loop consumes ticker-cache prices with no staleness gate** — I traced the cited code in full and the call sites across the repo.

WHAT THE FINDING GETS RIGHT (verified):
- continuous_demo.py:1058-1077 `_live_prices_from_ticker_cache` indeed never calls `is_stale`/`seconds_since_last_event`; it just reads `ticker_cache.get(sym)`.
- The main cycle's reader even
- **Continuous (and short/long) trading units have no systemd ordering dependency on the risk service — on reboot they start in parallel and can trade before the risk tracker is up** — The FACTUAL core is true and I independently reconstructed it: no systemd unit declares After/Wants on the risk service (grep across deploy/systemd/*.service shows only `network-online.target` everywhere); the continuous unit ships SUBMIT_ORDERS=1 + MAX_NEW_ENTRIES_PER_CYCLE=5 (liquidity-migration-b
- **PIT-universe survivorship-gate cluster lives in volume_events.py but is shared backtest infra imported by long_native.py** — I independently verified the factual claims and they are accurate: the cited functions all exist in volume_events.py at/near the cited lines (read directly), and long_native.py:50-59 does import `_full_pit_universe_error`, `_full_pit_universe_pass`, and `_pit_manifest_metadata` from volume_events ex
