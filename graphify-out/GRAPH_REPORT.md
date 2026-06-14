# Graph Report - liquidity-migration  (2026-06-14)

## Corpus Check
- 167 files · ~366,901 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 4432 nodes · 13000 edges · 55 communities detected
- Extraction: 55% EXTRACTED · 45% INFERRED · 0% AMBIGUOUS · INFERRED: 5825 edges (avg confidence: 0.65)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]

## God Nodes (most connected - your core abstractions)
1. `ResearchConfig` - 459 edges
2. `EventWebSocketRiskEngine` - 248 edges
3. `EventDemoCycleConfig` - 222 edges
4. `ContinuousDemoCycleConfig` - 189 edges
5. `EventWebSocketRiskConfig` - 185 edges
6. `BybitMarketData` - 173 edges
7. `KlineStore` - 157 edges
8. `TickerCache` - 146 edges
9. `ContinuousRebalanceResizePlan` - 131 edges
10. `read_dataset()` - 130 edges

## Surprising Connections (you probably didn't know these)
- `A sleeve is active unless its kill-switch toggle (deploy/sleeves.env, loaded int` --uses--> `BybitPrivateClient`  [INFERRED]
  scripts\check_demo_liveness.py → liquidity_migration\bybit.py
- `No cycle written within the freshness window -> the daemon is down/hung.` --uses--> `BybitPrivateClient`  [INFERRED]
  scripts\check_demo_liveness.py → liquidity_migration\bybit.py
- `SERVICES alert only on the TERMINAL systemd ``failed`` state; TIMERS alert` --uses--> `BybitPrivateClient`  [INFERRED]
  scripts\check_demo_liveness.py → liquidity_migration\bybit.py
- `For every open ledger trade, confirm the Bybit position carries a     server-si` --uses--> `BybitPrivateClient`  [INFERRED]
  scripts\check_demo_liveness.py → liquidity_migration\bybit.py
- `The continuous decile join drops EVERY symbol when residual_momentum.parquet lac` --uses--> `BybitPrivateClient`  [INFERRED]
  scripts\check_demo_liveness.py → liquidity_migration\bybit.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.01
Nodes (344): active_primary_pnl_gate_allows_addon(), apply_continuous_demo_profile(), _btc_trend_gate_allows_entries(), build_confirmed_entry_state(), _build_continuous_rebalance_resize_rows(), build_live_continuous_state(), _continuous_age_eligible_symbols(), continuous_dataset_names() (+336 more)

### Community 1 - "Community 1"
Cohesion: 0.01
Nodes (304): build_ws_trade_client(), BybitDataError, BybitMarketData, BybitPrivateClient, BybitRestRateLimiter, _env_flag(), _is_rate_limit(), _leverage_text() (+296 more)

### Community 2 - "Community 2"
Cohesion: 0.02
Nodes (265): BybitPrivateWebSocketStream, BybitPublicTickerStream, Subscribe to wallet balance pushes. Bybit pushes a per-account         snapshot, Socket-level liveness of the private stream. pybit's WebSocket subclasses, ContinuousHedgeConfig, HedgeDecision, HedgeDecision2F, _demo_kline_fetch_ranges() (+257 more)

### Community 3 - "Community 3"
Cohesion: 0.02
Nodes (265): _cal_roll(), calendar_roll(), calendar_shift(), date_boundary_ms(), date_ms(), _date_range(), _date_symbol_set(), _exclude_symbols() (+257 more)

### Community 4 - "Community 4"
Cohesion: 0.03
Nodes (244): ResearchConfig, Map a canonical dataset request to the variant actually present in ``root``., read_dataset(), resolve_dataset_name(), write_dataset(), _ensure_sleeve_column(), EventWebSocketRiskConfig, EventWebSocketRiskEngine (+236 more)

### Community 5 - "Community 5"
Cohesion: 0.02
Nodes (192): _active_position_by_symbol(), _bool(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot(), _build_private_client(), _column_values(), _combine_errors() (+184 more)

### Community 6 - "Community 6"
Cohesion: 0.02
Nodes (215): HTMLParser, ArchiveFileNotFoundError, download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig (+207 more)

### Community 7 - "Community 7"
Cohesion: 0.02
Nodes (194): build_full_ledger(), _config_path(), forward_readiness_summary(), ForwardUpdateResult, frozen_config_hash(), frozen_hedge_rule(), frozen_rebalance_rule(), init_or_check_state() (+186 more)

### Community 8 - "Community 8"
Cohesion: 0.02
Nodes (130): _default_continuous_kline_stream_manager_factory(), Read-only kline FOLLOWER — share one WS kline data plane across co-located sleev, _Bar, _empty_klines_frame(), KlineStore, _parse_ws_kline_event(), In-memory 1h kline store for the WS-driven kline-delivery path.  The cross-sec, One 1h bar. Stored per-symbol keyed by ``ts_ms`` in the store's dict. (+122 more)

### Community 9 - "Community 9"
Cohesion: 0.03
Nodes (144): _additive_equity(), _additive_summary(), _btc_trend_returns(), build_continuous_panel(), _continuous_rank_lookup(), ContinuousEventConfig, _daily_pnl_metrics(), _entry_event_expr() (+136 more)

### Community 10 - "Community 10"
Cohesion: 0.02
Nodes (146): build_factor_panel(), compute_btc_beta(), decompose_strategy_pnl(), fit_factor_returns(), R4 — risk-factor model for crypto-perp returns (Round 2).  Pre-reg: docs/resea, Build the per-(symbol, ts_ms, date) factor-exposure panel, full-PIT.      Read, Per-day cross-sectional OLS of realized return on factor exposures.      For e, Honest test of whether the factor model captures REAL forward-return variance. (+138 more)

### Community 11 - "Community 11"
Cohesion: 0.03
Nodes (124): account_key(), claim_symbol_reservation(), clamp_max_new_entries(), compute_im_used(), CrossSleeveAccountState, encode_control_row(), equal_split_budget(), _loads_budget() (+116 more)

### Community 12 - "Community 12"
Cohesion: 0.03
Nodes (105): BinanceDataError, BinanceUSDMData, _ceil_to_period(), _floor_to_period(), _recent_history_start(), Slugify `name` for use as a file or path component., safe_name(), _archive_filename() (+97 more)

### Community 13 - "Community 13"
Cohesion: 0.06
Nodes (113): coerce_int(), Coerce `value` to an int, returning `default` (0) on a missing/invalid value., _active_exposure_contributor_rows(), _active_exposure_rows(), _active_exposure_summary(), _active_trade_rows_at(), _anatomy_drift(), _anatomy_summary() (+105 more)

### Community 14 - "Community 14"
Cohesion: 0.03
Nodes (77): build_parser(), _add_archive_download_klines_1h_api_parser(), _add_archive_download_klines_1h_parser(), _add_archive_download_klines_parser(), _add_archive_manifest_parser(), _add_combined_book_report_parser(), _add_continuous_addon_shadow_audit_parser(), _add_continuous_event_demo_cycle_parser() (+69 more)

### Community 15 - "Community 15"
Cohesion: 0.04
Nodes (59): BybitKlineStreamPool, _close_state(), _close_ws_client(), _is_already_subscribed_error(), _KlineConnectionState, Close a pybit WS client with a hard timeout.      pybit's exit/close/stop meth, True for pybit's "You have already subscribed to this topic" error.      pybit, Per-connection bookkeeping for the pool. (+51 more)

### Community 16 - "Community 16"
Cohesion: 0.05
Nodes (76): coefficient_sign_consistency(), FoldCoefficients, _rank(), rank_ic(), Walk-forward ridge combiner: frozen causal features -> one forward-return score., Per-column mean/std from training rows. Zero-variance columns get std=1 so, Average-rank of a 1-D array (ties share their mean rank)., Spearman rank correlation (Pearson on average-ranks). NaN-safe-ish: returns (+68 more)

### Community 17 - "Community 17"
Cohesion: 0.06
Nodes (70): audit_continuous_rebalance_cycles(), _calendar_metrics(), _clean_trades(), _continuous_cycle_daily_returns(), _cycle_zero_daily_returns(), _daily_forward_returns(), _entry_slippage_bps(), _exit_slippage_bps() (+62 more)

### Community 18 - "Community 18"
Cohesion: 0.08
Nodes (42): BybitTradeRouter, BybitWebSocketTradeClient, Issue a WS call and block (with timeout) for the ack.          Returns the ack, Internal signal that the WS submission failed — the router decides     whether, Route order placement + cancellation through WS first, REST as fallback., _RouterWsFailed, Tests for BybitTradeRouter — WS-first / REST-fallback order submission.  The r, Bybit demo currently returns retCode != 0 for WS order entry; the     router mu (+34 more)

### Community 19 - "Community 19"
Cohesion: 0.04
Nodes (57): Pin the Strictness Manifesto decision-rule analyzer.  Specifically validates a, Construct a cell that passes EVERY gate; verify it's labelled candidate., A near-miss is inconclusive (Bybit passes, Binance falls short on sharpe Δ)., M6: a status!=ok cell must be returned as an explicit exclusion (a     falsifie, The full_pit_universe_pass column is parsed into CellMetrics so the     analyze, At window_days = 365.25 (one calendar year), annualized = total_return.     San, At window_days = 182.625 (half year), annualized = (1+r)^2 - 1., The actual Round 1 / Phase 0 / R1 baseline at 1125 days (Bybit):     total_retu (+49 more)

### Community 20 - "Community 20"
Cohesion: 0.04
Nodes (25): H4: the funding total must not be truncated to the first 200-row page —     fol, BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     cal, BybitPrivateClient must acquire the shared rate limiter before every     pybit, When _call retries on a failed pybit call, each attempt must hit the     limite, The multi-daemon demo connect-storm fix: a transient connect failure is     ret, All attempts fail -> raise so the caller falls back to REST (seatbelt)., Permanent errors (no creds) raise immediately with NO jitter/backoff —     keep, EXC-3: a definite (non-rate-limit) venue reject must raise IMMEDIATELY, not burn (+17 more)

### Community 21 - "Community 21"
Cohesion: 0.07
Nodes (49): finite_float(), parse_date(), pct(), Coerce `value` to a finite float, returning `default` if missing/invalid., Format a fraction as a 2-decimal percentage, or `invalid` if not finite., Parse an ISO date/datetime string to a UTC `date`, or None if empty., _covered_pairs(), _dataset_notes() (+41 more)

### Community 22 - "Community 22"
Cohesion: 0.1
Nodes (44): _format_book_line(), format_age_ms(), format_pct(), format_usd(), format_utc_time_ms(), _rate_limit_retry_seconds(), Seconds to wait before the single 429 retry, or None when the error is     not, True when the token + chat-id env vars are both present (a send can be     atte (+36 more)

### Community 23 - "Community 23"
Cohesion: 0.09
Nodes (38): _float_or_nan(), _parse_day(), _has_columns(), _btc_daily_close_series(), _chart_final_values(), _chart_font(), _date_axis_ticks(), _draw_monthly_return_table() (+30 more)

### Community 24 - "Community 24"
Cohesion: 0.1
Nodes (39): Alert, build_arg_parser(), _default_units_for_toggles(), evaluate_cycle_liveness(), evaluate_rmom_staleness(), evaluate_stop_protection(), evaluate_unit_states(), evaluate_ws_staleness() (+31 more)

### Community 25 - "Community 25"
Cohesion: 0.16
Nodes (32): _continuous_sniper_link_prefix(), _execute_sniper_placements(), reconcile_continuous_snipes(), _sniper_order_state(), _sniper_round_qty(), _sniper_trade_id(), _cfg(), _entry_row() (+24 more)

### Community 26 - "Community 26"
Cohesion: 0.09
Nodes (34): _assert_download_completeness(), build_binance_oos(), discover(), fetch_month_klines(), list_symbol_months(), list_usdm_usdt_symbols(), main(), parse_month_csv() (+26 more)

### Community 27 - "Community 27"
Cohesion: 0.1
Nodes (22): build_arg_parser(), bybit_linear_symbols(), connection_expired(), JsonlDayWriter, main(), parse_binance_event(), parse_bybit_event(), Live liquidation collectors — Bybit allLiquidation + Binance forceOrder (P3, 202 (+14 more)

### Community 28 - "Community 28"
Cohesion: 0.07
Nodes (17): Unit tests for the fast liveness/safety watchdog's pure decision logic., The collector unit can never reach systemd 'failed' (RestartSec spaces starts, The LONG sleeve runs on its own root with no rmom gate. gather_long_alerts must, Continuous-sleeve diagnosability: a zero universe / empty kline store is the sam, Paper evidence writes continuous_fade_paper_* datasets and submits no orders, so, The watchdog skips an intentionally-off sleeve. Explicit env always wins; the, REGRESSION (audit 2026-06-12): send_telegram_message returns False (no exception, REGRESSION (audit 2026-06-12 round 3): a stopped/disabled TIMER reports     'in (+9 more)

### Community 29 - "Community 29"
Cohesion: 0.13
Nodes (22): band_notionals(), build_arg_parser(), collect_cycle(), _get_json(), main(), Forward Bybit order-book depth collector — hourly band snapshots (DC1 follow-on), Fetch the live Trading universe; on failure keep ``previous``.      A transien, Cumulative quote notional to each ±band of mid; NULL beyond the book's span. (+14 more)

### Community 30 - "Community 30"
Cohesion: 0.17
Nodes (20): _cli(), _first_summary_line(), _have_rsync(), _load_dotenv(), main(), pull_sleeve(), _py(), _quote() (+12 more)

### Community 31 - "Community 31"
Cohesion: 0.18
Nodes (22): _bars(), _base_trade_paths(), _code_hash(), _dates(), _decision(), _effect_row(), _event_rows(), _features_for_trade() (+14 more)

### Community 32 - "Community 32"
Cohesion: 0.21
Nodes (18): _arm_report(), _bars(), _base_trade_paths(), _code_hash(), _dates(), _funding_events(), _git_head(), _ledger_with_equity() (+10 more)

### Community 33 - "Community 33"
Cohesion: 0.22
Nodes (16): _append(), compute_shadow_anchor(), Forward paper-shadow of the fade-completion dynamic exit (P5 follow-up, 2026-06-, One per-cycle shadow sweep (pure bookkeeping; no orders).      Arms fresh entr, Replay the JSONL into (armed_by_trade_id, exited_trade_ids)., anchor = clip(max(runup24h, ret1), lo, hi) at the signal bar, causal.      Use, _read_state(), update_dynexit_shadow() (+8 more)

### Community 34 - "Community 34"
Cohesion: 0.19
Nodes (16): CellMetrics, CellVerdict, compute_annualized_return(), compute_mar(), evaluate_cell(), evaluate_cell_investigation(), _index_by_cell(), main() (+8 more)

### Community 35 - "Community 35"
Cohesion: 0.12
Nodes (7): Pin the R1 robustness / Tier-2 demo-candidate analyzer's gate integrity.  Audi, A >=100% cumulative loss makes (1+total_return) non-positive; a fractional, re-audit rescan-rmom-funding-2: the non-circular moving-block bootstrap must, The max(n-block+1, 1) guard must not IndexError when n < block., test_annualize_and_engine_mar_stay_real_below_minus_100pct(), test_block_bootstrap_indices_handle_series_shorter_than_block(), test_block_bootstrap_indices_never_straddle_the_boundary()

### Community 36 - "Community 36"
Cohesion: 0.17
Nodes (12): _is_link_or_junction(), _make_source_root(), Pin the legacy-archive manifest side-copy builder.  The script lives at scripts/, The reports/ subdir is intentionally NOT link-mirrored — Phase 1 cells     land, Build a synthetic source data root mimicking ~/SHARED_DATA/bybit_full_pit., Treat both symlinks and Windows directory junctions as 'linked'.      On Windows, test_build_legacy_archive_manifest_dry_run_makes_no_changes(), test_build_legacy_archive_manifest_filters_source_tag() (+4 more)

### Community 37 - "Community 37"
Cohesion: 0.24
Nodes (14): _continuous_payload_from_summary(), _delisted_traded(), _find_png(), _headline(), _infer_venue_from_root(), _label(), main(), _pit_verdict() (+6 more)

### Community 38 - "Community 38"
Cohesion: 0.18
Nodes (11): ContinuousOverlayCandidate, ContinuousRebalanceCandidate, ContinuousStandaloneCandidate, long_profile(), THE single source of truth for what each sleeve runs in the live demo.  If you, Return cfg with start_date/end_date overridden when provided (both sleeve     c, The deployed LONG v11a profile (the `div` risk-engineering: universe 50,     ma, Research-stage continuous overlay candidate visible from the promoted tab. (+3 more)

### Community 39 - "Community 39"
Cohesion: 0.27
Nodes (10): backfill_symbol(), main(), _month_rows(), _months(), backfill_symbol(), _day_rows(), _days(), _fetch() (+2 more)

### Community 40 - "Community 40"
Cohesion: 0.27
Nodes (9): Functional tests for the per-sleeve kill-switch (deploy/lib_sleeves.sh + sleeves, Last-resort fallback (NEITHER sleeves.env present — a stripped checkout):     E, _run(), test_continuous_paper_split_keeps_demo_orders_off_runs_paper(), test_lib_fallback_defaults_every_sleeve_off(), test_loaded_toggles_continuous_on_long_off(), test_off_sleeve_is_disabled_on_sleeve_is_enabled(), test_verify_fails_when_on_sleeve_is_not_running() (+1 more)

### Community 41 - "Community 41"
Cohesion: 0.36
Nodes (9): analyze_venue(), block_boot_p(), build_factor(), load_daily_panel(), main(), _mask(), WP1a: does trailing alt-vs-BTC relative strength predict continuous-short squeez, One-sided percentile-bootstrap p for IC < 0 (fraction of reps with IC >= 0). (+1 more)

### Community 42 - "Community 42"
Cohesion: 0.53
Nodes (9): _load_module(), _payload(), ROUND 4 (pins the round-3 fix): the watchdog monitors this oneshot on     the p, test_collecting_when_both_legs_fresh_and_window_immature(), test_continuous_only_when_daily_leg_dead(), test_main_exits_nonzero_when_attempted_send_fails(), test_pass_after_common_window_matures(), test_stale_only_when_continuous_itself_lags_today() (+1 more)

### Community 43 - "Community 43"
Cohesion: 0.2
Nodes (1): Pin the single-source-of-truth promoted profiles (liquidity_migration.promoted).

### Community 44 - "Community 44"
Cohesion: 0.31
Nodes (5): _mk(), Tests for the PIT coverage / staleness diagnostics (FIX C)., test_coverage_fresh_manifest_is_not_stale(), test_coverage_missing_manifest(), test_coverage_stale_manifest_flagged()

### Community 45 - "Community 45"
Cohesion: 0.32
Nodes (6): _apply(), Causality tests for the residual-momentum precompute (scripts/precompute_residua, residual_momentum[D] = sum residual_return[D-9 .. D-3] (rolling 7, shift 3)., Look-ahead guard: residual_momentum[D] must be INVARIANT to residual_return at d, test_residual_momentum_does_not_read_future_residuals(), test_residual_momentum_is_causal_shift3()

### Community 46 - "Community 46"
Cohesion: 0.48
Nodes (6): backfill_symbol(), _day_rows(), _days(), _fetch(), _kline_spans(), main()

### Community 47 - "Community 47"
Cohesion: 0.48
Nodes (6): _fapi_topup(), _fetch(), _kline_spans(), main(), _month_rows(), _months()

### Community 49 - "Community 49"
Cohesion: 0.6
Nodes (5): load_entries(), load_klines(), load_rmom(), main(), _ts()

### Community 50 - "Community 50"
Cohesion: 0.6
Nodes (4): load_binance_events(), main(), needed_days_by_symbol(), Event-anchored Binance UM `metrics` backfill (P10 binance arm, data layer only).

### Community 51 - "Community 51"
Cohesion: 0.5
Nodes (2): test_monthly_trade_counts_dedupes_component_overlap(), _write_trades()

### Community 52 - "Community 52"
Cohesion: 0.67
Nodes (3): attribution(), main(), Per-exit-reason forward attribution for the continuous book (zero-DOF report).

### Community 54 - "Community 54"
Cohesion: 0.67
Nodes (1): Guards against circular-import regressions in the post-refactor module split.

### Community 55 - "Community 55"
Cohesion: 1.0
Nodes (1): The manifest cannot PIT-validate the most recent daily-close signal.

### Community 56 - "Community 56"
Cohesion: 1.0
Nodes (1): Extract (orderId, orderLinkId) — accept Bybit's camelCase or the         snake_

## Knowledge Gaps
- **382 isolated node(s):** `The requested archive URL returned HTTP 404.      Distinct from transient netw`, `Config for ``build_archive_trade_manifest``.      The manifest is always built`, `Fetch currently-listed Bybit v5 perpetual symbols + their launchTime (ms).`, `Build manifest rows from v5-Trading listings for ``(symbol, date)``     pairs m`, `Build the Bybit PIT trade manifest by merging two sources:      1. ``public.by` (+377 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 43`** (10 nodes): `test_promoted_profiles.py`, `Pin the single-source-of-truth promoted profiles (liquidity_migration.promoted).`, `test_continuous_merged_rebalance_candidate_is_not_promoted()`, `test_continuous_overlay_candidate_is_documented_but_not_promoted()`, `test_continuous_performance_frontier_is_not_promoted()`, `test_continuous_rebalance_candidate_is_not_promoted()`, `test_continuous_standalone_return_candidate_is_not_promoted()`, `test_long_profile_is_v11a()`, `test_registry_covers_the_long_sleeve_only()`, `test_windowing_sets_dates_on_all_sleeves()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 51`** (5 nodes): `test_continuous_deployed_equity_refresh.py`, `test_chart_leverages_always_include_one_x()`, `test_monthly_returns_with_trades_uses_trade_counts()`, `test_monthly_trade_counts_dedupes_component_overlap()`, `_write_trades()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 54`** (3 nodes): `test_package_import_integrity.py`, `Guards against circular-import regressions in the post-refactor module split.`, `test_split_sibling_imports_cold_in_fresh_process()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 55`** (1 nodes): `The manifest cannot PIT-validate the most recent daily-close signal.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 56`** (1 nodes): `Extract (orderId, orderLinkId) — accept Bybit's camelCase or the         snake_`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 6` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 8`, `Community 9`, `Community 12`, `Community 13`, `Community 14`, `Community 17`, `Community 21`, `Community 22`?**
  _High betweenness centrality (0.128) - this node is a cross-community bridge._
- **Why does `ResearchConfig` connect `Community 4` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 6`, `Community 12`?**
  _High betweenness centrality (0.090) - this node is a cross-community bridge._
- **Why does `ContinuousDemoCycleConfig` connect `Community 0` to `Community 1`, `Community 2`, `Community 4`, `Community 6`, `Community 7`, `Community 11`, `Community 14`, `Community 17`, `Community 25`?**
  _High betweenness centrality (0.087) - this node is a cross-community bridge._
- **Are the 457 inferred relationships involving `ResearchConfig` (e.g. with `ContinuousDemoCycleConfig` and `LivePanelCache`) actually correct?**
  _`ResearchConfig` has 457 INFERRED edges - model-reasoned connections that need verification._
- **Are the 170 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateWebSocketStream` and `BybitPublicTickerStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 170 INFERRED edges - model-reasoned connections that need verification._
- **Are the 221 inferred relationships involving `EventDemoCycleConfig` (e.g. with `ContinuousDemoCycleConfig` and `LivePanelCache`) actually correct?**
  _`EventDemoCycleConfig` has 221 INFERRED edges - model-reasoned connections that need verification._
- **Are the 183 inferred relationships involving `ContinuousDemoCycleConfig` (e.g. with `Coverage table + (when stale) a prominent WARNING that download-data does not` and `Resolve the data root for a CLI command: commands that don't use it get the path`) actually correct?**
  _`ContinuousDemoCycleConfig` has 183 INFERRED edges - model-reasoned connections that need verification._