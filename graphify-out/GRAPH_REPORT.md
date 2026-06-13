# Graph Report - liquidity-migration  (2026-06-13)

## Corpus Check
- 175 files · ~374,628 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 4475 nodes · 13083 edges · 47 communities detected
- Extraction: 55% EXTRACTED · 45% INFERRED · 0% AMBIGUOUS · INFERRED: 5861 edges (avg confidence: 0.65)
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
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]

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
- `LongNativeDemoCycleConfig` --uses--> `Tests for B.4 long-side reconcile-paper-demo analyzer.`  [INFERRED]
  liquidity_migration/long_native_event_demo.py → tests/test_liquidity_migration_long_reconciliation.py
- `CostConfig` --uses--> `Live daemon entrypoints self-provision a missing ledger root (so a brand-new sle`  [INFERRED]
  liquidity_migration/config.py → tests/test_liquidity_migration_cli.py
- `CostConfig` --uses--> `The --paper-mode flag must surface on the parsed args so the long-paper     serv`  [INFERRED]
  liquidity_migration/config.py → tests/test_liquidity_migration_cli.py
- `CostConfig` --uses--> `Live long-demo defaults to paper_mode=False so its writes land in the     long_n`  [INFERRED]
  liquidity_migration/config.py → tests/test_liquidity_migration_cli.py
- `ContinuousHedgeConfig` --uses--> `Tests for the live BTC-beta hedge manager (WP3 live wiring, 2026-06-10).`  [INFERRED]
  liquidity_migration/continuous_hedge_manager.py → tests/test_continuous_hedge_manager.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.02
Nodes (333): BybitMarketData, _is_rate_limit(), active_primary_pnl_gate_allows_addon(), apply_continuous_demo_profile(), _btc_trend_gate_allows_entries(), build_confirmed_entry_state(), _build_continuous_rebalance_resize_rows(), build_live_continuous_state() (+325 more)

### Community 1 - "Community 1"
Cohesion: 0.01
Nodes (270): build_ws_trade_client(), BybitDataError, BybitPrivateClient, BybitPrivateWebSocketStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client(), _default_kline_websocket_factory() (+262 more)

### Community 2 - "Community 2"
Cohesion: 0.01
Nodes (228): BybitKlineStreamPool, BybitRestRateLimiter, _close_state(), _is_already_subscribed_error(), _KlineConnectionState, Thread-safe sliding-window rate limiter shared across BybitMarketData     instan, True for pybit's "You have already subscribed to this topic" error.      pybit's, Per-connection bookkeeping for the pool. (+220 more)

### Community 3 - "Community 3"
Cohesion: 0.02
Nodes (283): _cal_roll(), calendar_roll(), calendar_shift(), date_boundary_ms(), date_ms(), _date_range(), _date_symbol_set(), _exclude_symbols() (+275 more)

### Community 4 - "Community 4"
Cohesion: 0.02
Nodes (266): build_full_ledger(), _config_path(), forward_readiness_summary(), ForwardUpdateResult, frozen_config_hash(), frozen_hedge_rule(), frozen_rebalance_rule(), init_or_check_state() (+258 more)

### Community 5 - "Community 5"
Cohesion: 0.03
Nodes (252): BybitPublicTickerStream, ContinuousHedgeConfig, _build_demo_universe(), _bust_demo_instruments_cache(), _concat_recent_klines(), _dedupe_recent_klines(), _demo_feature_cache_fingerprint(), _demo_feature_cache_paths() (+244 more)

### Community 6 - "Community 6"
Cohesion: 0.02
Nodes (240): HTMLParser, ArchiveFileNotFoundError, download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig (+232 more)

### Community 7 - "Community 7"
Cohesion: 0.02
Nodes (218): _assert_download_completeness(), build_binance_oos(), discover(), fetch_month_klines(), list_symbol_months(), list_usdm_usdt_symbols(), main(), parse_month_csv() (+210 more)

### Community 8 - "Community 8"
Cohesion: 0.04
Nodes (192): ResearchConfig, _order_link_id(), read_dataset(), _ensure_sleeve_column(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, run_event_ws_risk(), _validate_trade_row_invariants() (+184 more)

### Community 9 - "Community 9"
Cohesion: 0.03
Nodes (121): _active_position_by_symbol(), _bool(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot(), _build_private_client(), _column_values(), _decimal_text() (+113 more)

### Community 10 - "Community 10"
Cohesion: 0.02
Nodes (154): build_factor_panel(), compute_btc_beta(), decompose_strategy_pnl(), fit_factor_returns(), R4 — risk-factor model for crypto-perp returns (Round 2).  Pre-reg: docs/researc, Build the per-(symbol, ts_ms, date) factor-exposure panel, full-PIT.      Reads, Per-day cross-sectional OLS of realized return on factor exposures.      For eac, Honest test of whether the factor model captures REAL forward-return variance. (+146 more)

### Community 11 - "Community 11"
Cohesion: 0.03
Nodes (138): _additive_equity(), _additive_summary(), _btc_trend_returns(), build_continuous_panel(), _continuous_rank_lookup(), ContinuousEventConfig, _daily_pnl_metrics(), _entry_event_expr() (+130 more)

### Community 12 - "Community 12"
Cohesion: 0.03
Nodes (144): UniverseConfig, Resolve the shared account/control root from a sleeve's own data root (mirrors, shared_account_root(), _combine_errors(), _contract_lookup(), _demo_private_rest_rate_limit_per_second(), Bybit per-account private REST budget for place_order et al is roughly     20 re, _max_int() (+136 more)

### Community 13 - "Community 13"
Cohesion: 0.03
Nodes (103): BinanceDataError, BinanceUSDMData, _ceil_to_period(), _floor_to_period(), _recent_history_start(), Slugify `name` for use as a file or path component., safe_name(), _archive_filename() (+95 more)

### Community 14 - "Community 14"
Cohesion: 0.06
Nodes (113): coerce_int(), Coerce `value` to an int, returning `default` (0) on a missing/invalid value., _active_exposure_contributor_rows(), _active_exposure_rows(), _active_exposure_summary(), _active_trade_rows_at(), _anatomy_drift(), _anatomy_summary() (+105 more)

### Community 15 - "Community 15"
Cohesion: 0.03
Nodes (77): build_parser(), _add_archive_download_klines_1h_api_parser(), _add_archive_download_klines_1h_parser(), _add_archive_download_klines_parser(), _add_archive_manifest_parser(), _add_combined_book_report_parser(), _add_continuous_addon_shadow_audit_parser(), _add_continuous_event_demo_cycle_parser() (+69 more)

### Community 16 - "Community 16"
Cohesion: 0.05
Nodes (76): coefficient_sign_consistency(), FoldCoefficients, _rank(), rank_ic(), Walk-forward ridge combiner: frozen causal features -> one forward-return score., Per-column mean/std from training rows. Zero-variance columns get std=1 so     t, Average-rank of a 1-D array (ties share their mean rank)., Spearman rank correlation (Pearson on average-ranks). NaN-safe-ish: returns (+68 more)

### Community 17 - "Community 17"
Cohesion: 0.07
Nodes (62): audit_continuous_rebalance_cycles(), _calendar_metrics(), _clean_trades(), _continuous_cycle_daily_returns(), _cycle_zero_daily_returns(), _daily_forward_returns(), _entry_slippage_bps(), _exit_slippage_bps() (+54 more)

### Community 18 - "Community 18"
Cohesion: 0.04
Nodes (57): Pin the Strictness Manifesto decision-rule analyzer.  Specifically validates aga, Construct a cell that passes EVERY gate; verify it's labelled candidate., A near-miss is inconclusive (Bybit passes, Binance falls short on sharpe Δ)., M6: a status!=ok cell must be returned as an explicit exclusion (a     falsifier, The full_pit_universe_pass column is parsed into CellMetrics so the     analyzer, At window_days = 365.25 (one calendar year), annualized = total_return.     Sani, At window_days = 182.625 (half year), annualized = (1+r)^2 - 1., The actual Round 1 / Phase 0 / R1 baseline at 1125 days (Bybit):     total_retur (+49 more)

### Community 19 - "Community 19"
Cohesion: 0.09
Nodes (39): BybitTradeRouter, Issue a WS call and block (with timeout) for the ack.          Returns the ack's, Route order placement + cancellation through WS first, REST as fallback.      Ex, Tests for BybitTradeRouter — WS-first / REST-fallback order submission.  The rou, Bybit demo currently returns retCode != 0 for WS order entry; the     router mus, The double-submit race: WS submit reached Bybit but the ack     network-delayed, Probe checks open-orders first, then history. If neither has the     order, REST, A Rejected/Cancelled history row means the WS submit did NOT take effect,     so (+31 more)

### Community 20 - "Community 20"
Cohesion: 0.04
Nodes (25): H4: the funding total must not be truncated to the first 200-row page —     foll, BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     call, BybitPrivateClient must acquire the shared rate limiter before every     pybit H, When _call retries on a failed pybit call, each attempt must hit the     limiter, The multi-daemon demo connect-storm fix: a transient connect failure is     retr, All attempts fail -> raise so the caller falls back to REST (seatbelt)., Permanent errors (no creds) raise immediately with NO jitter/backoff —     keeps, EXC-3: a definite (non-rate-limit) venue reject must raise IMMEDIATELY, not burn (+17 more)

### Community 21 - "Community 21"
Cohesion: 0.12
Nodes (38): _rate_limit_retry_seconds(), Seconds to wait before the single 429 retry, or None when the error is     not a, True when the token + chat-id env vars are both present (a send can be     attem, send_telegram_message(), telegram_configured(), TelegramConfig, _continuous_age_days(), _coverage_gap_days() (+30 more)

### Community 22 - "Community 22"
Cohesion: 0.09
Nodes (38): _float_or_nan(), _parse_day(), _has_columns(), _btc_daily_close_series(), _chart_final_values(), _chart_font(), _date_axis_ticks(), _draw_monthly_return_table() (+30 more)

### Community 23 - "Community 23"
Cohesion: 0.09
Nodes (40): _bars(), _base_trade_paths(), _code_hash(), _dates(), _decision(), _effect_row(), _event_rows(), _features_for_trade() (+32 more)

### Community 24 - "Community 24"
Cohesion: 0.22
Nodes (25): reconcile_continuous_snipes(), _cfg(), _entry_row(), FakeClient, _orders_df(), Sniper live-wiring tests (S1 Amendment 6 → demo cycle, 2026-06-10).  Covers: pla, REGRESSION (audit 2026-06-12): a PartiallyFilled order missing from a torn     o, REGRESSION (audit 2026-06-11, empty-fetch-as-deletion class): an order absent (+17 more)

### Community 25 - "Community 25"
Cohesion: 0.1
Nodes (22): build_arg_parser(), bybit_linear_symbols(), connection_expired(), JsonlDayWriter, main(), parse_binance_event(), parse_bybit_event(), Live liquidation collectors — Bybit allLiquidation + Binance forceOrder (P3, 202 (+14 more)

### Community 26 - "Community 26"
Cohesion: 0.13
Nodes (22): band_notionals(), build_arg_parser(), collect_cycle(), _get_json(), main(), Forward Bybit order-book depth collector — hourly band snapshots (DC1 follow-on), Fetch the live Trading universe; on failure keep ``previous``.      A transient, Cumulative quote notional to each ±band of mid; NULL beyond the book's span. (+14 more)

### Community 27 - "Community 27"
Cohesion: 0.17
Nodes (20): _cli(), _first_summary_line(), _have_rsync(), _load_dotenv(), main(), pull_sleeve(), _py(), _quote() (+12 more)

### Community 28 - "Community 28"
Cohesion: 0.17
Nodes (21): _annualize(), _block_bootstrap(), _compound(), _concentration(), _engine_mar(), _leave_one_out(), _load_json_metrics(), _load_monthly() (+13 more)

### Community 29 - "Community 29"
Cohesion: 0.22
Nodes (16): _append(), compute_shadow_anchor(), Forward paper-shadow of the fade-completion dynamic exit (P5 follow-up, 2026-06-, One per-cycle shadow sweep (pure bookkeeping; no orders).      Arms fresh entrie, Replay the JSONL into (armed_by_trade_id, exited_trade_ids)., anchor = clip(max(runup24h, ret1), lo, hi) at the signal bar, causal.      Uses, _read_state(), update_dynexit_shadow() (+8 more)

### Community 30 - "Community 30"
Cohesion: 0.17
Nodes (12): _is_link_or_junction(), _make_source_root(), Pin the legacy-archive manifest side-copy builder.  The script lives at scripts/, The reports/ subdir is intentionally NOT link-mirrored — Phase 1 cells     land, Build a synthetic source data root mimicking ~/SHARED_DATA/bybit_full_pit., Treat both symlinks and Windows directory junctions as 'linked'.      On Windows, test_build_legacy_archive_manifest_dry_run_makes_no_changes(), test_build_legacy_archive_manifest_filters_source_tag() (+4 more)

### Community 31 - "Community 31"
Cohesion: 0.12
Nodes (7): Pin the R1 robustness / Tier-2 demo-candidate analyzer's gate integrity.  Audit, A >=100% cumulative loss makes (1+total_return) non-positive; a fractional     p, re-audit rescan-rmom-funding-2: the non-circular moving-block bootstrap must, The max(n-block+1, 1) guard must not IndexError when n < block., test_annualize_and_engine_mar_stay_real_below_minus_100pct(), test_block_bootstrap_indices_handle_series_shorter_than_block(), test_block_bootstrap_indices_never_straddle_the_boundary()

### Community 32 - "Community 32"
Cohesion: 0.19
Nodes (16): CellMetrics, CellVerdict, compute_annualized_return(), compute_mar(), evaluate_cell(), evaluate_cell_investigation(), _index_by_cell(), main() (+8 more)

### Community 33 - "Community 33"
Cohesion: 0.27
Nodes (10): backfill_symbol(), _day_rows(), _days(), _fetch(), _kline_spans(), main(), backfill_symbol(), main() (+2 more)

### Community 34 - "Community 34"
Cohesion: 0.27
Nodes (9): Functional tests for the per-sleeve kill-switch (deploy/lib_sleeves.sh + sleeves, Last-resort fallback (NEITHER sleeves.env present — a stripped checkout):     EV, _run(), test_continuous_paper_split_keeps_demo_orders_off_runs_paper(), test_lib_fallback_defaults_every_sleeve_off(), test_loaded_toggles_continuous_on_long_off(), test_off_sleeve_is_disabled_on_sleeve_is_enabled(), test_verify_fails_when_on_sleeve_is_not_running() (+1 more)

### Community 35 - "Community 35"
Cohesion: 0.2
Nodes (1): Pin the single-source-of-truth promoted profiles (liquidity_migration.promoted).

### Community 36 - "Community 36"
Cohesion: 0.53
Nodes (9): _load_module(), _payload(), ROUND 4 (pins the round-3 fix): the watchdog monitors this oneshot on     the pr, test_collecting_when_both_legs_fresh_and_window_immature(), test_continuous_only_when_daily_leg_dead(), test_main_exits_nonzero_when_attempted_send_fails(), test_pass_after_common_window_matures(), test_stale_only_when_continuous_itself_lags_today() (+1 more)

### Community 37 - "Community 37"
Cohesion: 0.36
Nodes (9): analyze_venue(), block_boot_p(), build_factor(), load_daily_panel(), main(), _mask(), WP1a: does trailing alt-vs-BTC relative strength predict continuous-short squeez, One-sided percentile-bootstrap p for IC < 0 (fraction of reps with IC >= 0). (+1 more)

### Community 38 - "Community 38"
Cohesion: 0.31
Nodes (5): _mk(), Tests for the PIT coverage / staleness diagnostics (FIX C)., test_coverage_fresh_manifest_is_not_stale(), test_coverage_missing_manifest(), test_coverage_stale_manifest_flagged()

### Community 39 - "Community 39"
Cohesion: 0.32
Nodes (6): _apply(), Causality tests for the residual-momentum precompute (scripts/precompute_residua, residual_momentum[D] = sum residual_return[D-9 .. D-3] (rolling 7, shift 3)., Look-ahead guard: residual_momentum[D] must be INVARIANT to residual_return at d, test_residual_momentum_does_not_read_future_residuals(), test_residual_momentum_is_causal_shift3()

### Community 40 - "Community 40"
Cohesion: 0.36
Nodes (7): _concurrency_timeline(), _equity_from_trades(), main(), Per trade: min(low)/max(high) over (entry_ts_ms, exit_ts_ms] from klines_1h., Compound equity from per-trade equity-relative net returns, booked at exit     (, Sweep entry/exit events; track concurrent positions, summed notional_weight,, _read_trade_window_lows()

### Community 41 - "Community 41"
Cohesion: 0.48
Nodes (6): backfill_symbol(), _day_rows(), _days(), _fetch(), _kline_spans(), main()

### Community 42 - "Community 42"
Cohesion: 0.6
Nodes (5): load_entries(), load_klines(), load_rmom(), main(), _ts()

### Community 43 - "Community 43"
Cohesion: 0.6
Nodes (4): load_binance_events(), main(), needed_days_by_symbol(), Event-anchored Binance UM `metrics` backfill (P10 binance arm, data layer only).

### Community 45 - "Community 45"
Cohesion: 0.67
Nodes (1): Guards against circular-import regressions in the post-refactor module split.  T

### Community 46 - "Community 46"
Cohesion: 1.0
Nodes (1): The manifest cannot PIT-validate the most recent daily-close signal.

### Community 47 - "Community 47"
Cohesion: 1.0
Nodes (1): Extract (orderId, orderLinkId) — accept Bybit's camelCase or the         snake_c

## Knowledge Gaps
- **389 isolated node(s):** `Shared chart writers. Originally a slice of the volume_events hub (the daily SHO`, `Write the strategy-vs-BTC equity PNG. ``png_name`` lets other sleeves     (e.g.`, `Insert +0.00%/count-0 rows for months absent between the first and last row. A m`, `Rows are {month, return, count}; `count` is real trades when `monthly` carries t`, `Forward-fill an equity series to one point per calendar day so no-trade     gaps` (+384 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 35`** (10 nodes): `test_promoted_profiles.py`, `Pin the single-source-of-truth promoted profiles (liquidity_migration.promoted).`, `test_continuous_merged_rebalance_candidate_is_not_promoted()`, `test_continuous_overlay_candidate_is_documented_but_not_promoted()`, `test_continuous_performance_frontier_is_not_promoted()`, `test_continuous_rebalance_candidate_is_not_promoted()`, `test_continuous_standalone_return_candidate_is_not_promoted()`, `test_long_profile_is_v11a()`, `test_registry_covers_the_long_sleeve_only()`, `test_windowing_sets_dates_on_all_sleeves()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 45`** (3 nodes): `test_package_import_integrity.py`, `Guards against circular-import regressions in the post-refactor module split.  T`, `test_split_sibling_imports_cold_in_fresh_process()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 46`** (1 nodes): `The manifest cannot PIT-validate the most recent daily-close signal.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 47`** (1 nodes): `Extract (orderId, orderLinkId) — accept Bybit's camelCase or the         snake_c`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ResearchConfig` connect `Community 8` to `Community 0`, `Community 1`, `Community 3`, `Community 5`, `Community 6`, `Community 9`, `Community 12`, `Community 13`?**
  _High betweenness centrality (0.154) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 6` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 5`, `Community 8`, `Community 9`, `Community 11`, `Community 12`, `Community 13`, `Community 14`, `Community 15`, `Community 17`, `Community 21`?**
  _High betweenness centrality (0.104) - this node is a cross-community bridge._
- **Why does `ContinuousDemoCycleConfig` connect `Community 0` to `Community 2`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 8`, `Community 15`, `Community 17`, `Community 24`?**
  _High betweenness centrality (0.084) - this node is a cross-community bridge._
- **Are the 457 inferred relationships involving `ResearchConfig` (e.g. with `LongNativeDemoDaemon` and `Long-side daemon — mirror of event_demo_daemon for the v11a sleeve.  Keeps a sin`) actually correct?**
  _`ResearchConfig` has 457 INFERRED edges - model-reasoned connections that need verification._
- **Are the 170 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateWebSocketStream` and `BybitPublicTickerStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 170 INFERRED edges - model-reasoned connections that need verification._
- **Are the 221 inferred relationships involving `EventDemoCycleConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`EventDemoCycleConfig` has 221 INFERRED edges - model-reasoned connections that need verification._
- **Are the 183 inferred relationships involving `ContinuousDemoCycleConfig` (e.g. with `ContinuousDemoDaemon` and `Continuous-fade demo daemon — a SEPARATE sub-hourly sleeve reusing the live WS a`) actually correct?**
  _`ContinuousDemoCycleConfig` has 183 INFERRED edges - model-reasoned connections that need verification._