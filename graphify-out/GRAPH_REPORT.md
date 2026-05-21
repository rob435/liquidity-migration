# Graph Report - liquidity-migration  (2026-05-21)

## Corpus Check
- 62 files · ~120,603 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1662 nodes · 5254 edges · 18 communities detected
- Extraction: 57% EXTRACTED · 43% INFERRED · 0% AMBIGUOUS · INFERRED: 2283 edges (avg confidence: 0.67)
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
- [[_COMMUNITY_Community 20|Community 20]]

## God Nodes (most connected - your core abstractions)
1. `ResearchConfig` - 270 edges
2. `EventDemoCycleConfig` - 217 edges
3. `VolumeEventResearchConfig` - 179 edges
4. `ExecutionEventRouter` - 148 edges
5. `EventScenario` - 135 edges
6. `EventRiskCycleConfig` - 128 edges
7. `EventWebSocketRiskEngine` - 120 edges
8. `read_dataset()` - 92 edges
9. `EventWebSocketRiskConfig` - 75 edges
10. `run_event_demo_cycle()` - 71 edges

## Surprising Connections (you probably didn't know these)
- `build_parser()` --calls--> `test_cli_strategy_tribunal_parses_research_controls()`  [INFERRED]
  liquidity_migration/cli.py → tests/test_liquidity_migration_strategy_tribunal.py
- `_scenario_side()` --calls--> `test_selloff_exhaustion_side_hypotheses_are_directional()`  [INFERRED]
  liquidity_migration/volume_events.py → tests/test_liquidity_migration_volume_events.py
- `plan_risk_exits()` --calls--> `test_plan_risk_exits_uses_live_position_price_for_stops()`  [INFERRED]
  liquidity_migration/event_demo.py → tests/test_liquidity_migration_event_demo.py
- `plan_stop_repairs()` --calls--> `test_plan_stop_repairs_detects_missing_exchange_stop()`  [INFERRED]
  liquidity_migration/event_demo.py → tests/test_liquidity_migration_event_demo.py
- `build_ledger_position_pnl_snapshot()` --calls--> `test_ledger_position_snapshot_marks_short_pnl_from_current_price()`  [INFERRED]
  liquidity_migration/event_demo.py → tests/test_liquidity_migration_event_demo.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (246): BybitMarketData, BybitPrivateClient, BybitPrivateWebSocketStream, BybitRestRateLimiter, Thread-safe sliding-window rate limiter shared across BybitMarketData     instan, One-line `event demo cycle ...` summary used by both the legacy bash-loop     ru, ResearchConfig, UniverseConfig (+238 more)

### Community 1 - "Community 1"
Cohesion: 0.04
Nodes (135): _active_position_by_symbol(), _base36(), _bool(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot(), _build_private_client() (+127 more)

### Community 2 - "Community 2"
Cohesion: 0.03
Nodes (159): CostConfig, FixtureSpec, generate_fixture_data(), _exit_reason_rows(), _price_bars_by_symbol(), _add_event_uniqueness_score(), _add_liquidity_migration_speed_features(), _add_rank_fraction() (+151 more)

### Community 3 - "Community 3"
Cohesion: 0.03
Nodes (151): HTMLParser, download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig (+143 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (91): dataset_lock_path(), ensure_data_root(), exclusive_file_lock(), _lock_owner_is_dead(), _lock_payload_is_invalid(), read_dataset(), with_date_column(), write_dataset() (+83 more)

### Community 5 - "Community 5"
Cohesion: 0.04
Nodes (80): BinanceDataError, BinanceUSDMData, _ceil_to_period(), _floor_to_period(), _recent_history_start(), _archive_filename(), _archive_outputs_exist(), _dates_between() (+72 more)

### Community 6 - "Community 6"
Cohesion: 0.04
Nodes (69): BybitPublicTradeStream, _patch_pybit_daemon_ping_timer(), _build_demo_features(), _demo_feature_cache_fingerprint(), _demo_feature_cache_paths(), _read_demo_feature_cache(), _validate_demo_config(), _write_demo_feature_cache() (+61 more)

### Community 7 - "Community 7"
Cohesion: 0.05
Nodes (75): date_boundary_ms(), date_ms(), finite_float(), parse_date(), pct(), Shared low-level helpers and constants for the liquidity_migration package.  Cen, Coerce `value` to a finite float, returning `default` if missing/invalid., Format a fraction as a 2-decimal percentage, or `invalid` if not finite. (+67 more)

### Community 8 - "Community 8"
Cohesion: 0.04
Nodes (76): _audit_violations(), ChallengerSpec, champion_challenger_specs(), format_champion_challenger_manifest(), run_champion_challenger_audit(), _add_archive_download_klines_1h_api_parser(), _add_archive_download_klines_1h_parser(), _add_archive_download_klines_parser() (+68 more)

### Community 9 - "Community 9"
Cohesion: 0.05
Nodes (76): ExchangeConfig, load_config(), _merge_dataclass(), _merge_universe_config(), TradeLifecycleConfig, _tuple_str(), _bar_excursion(), _bar_exit_hits() (+68 more)

### Community 10 - "Community 10"
Cohesion: 0.07
Nodes (59): compute_reversion_score(), construct_target_book(), _cross_sectional_z(), _daily_mtm(), _date_range(), _day_str(), _day_str_from_index(), _hourly_bars_by_symbol() (+51 more)

### Community 11 - "Community 11"
Cohesion: 0.1
Nodes (51): _artifact_check(), _basket_returns(), _best_summary_row(), _block_bootstrap(), _boolish(), _cluster_report(), _comparison_family_frame(), _comparison_family_label() (+43 more)

### Community 12 - "Community 12"
Cohesion: 0.07
Nodes (18): BybitDataError, BybitPublicTickerStream, BybitWebSocketTradeClient, _close_ws_client(), _is_rate_limit(), _leverage_text(), _ack_order_link(), _build_private_stream() (+10 more)

### Community 13 - "Community 13"
Cohesion: 0.11
Nodes (29): add_forward_short_returns(), _bar_arrays(), cross_sectional_ic(), ic_table(), ic_vs_horizon(), ICResult, _rankdata(), Information-coefficient diagnostics for the reversion-alpha signals.  WHY THIS M (+21 more)

### Community 14 - "Community 14"
Cohesion: 0.11
Nodes (28): build_binance_oos(), discover(), fetch_month_klines(), list_symbol_months(), list_usdm_usdt_symbols(), main(), parse_month_csv(), Point-in-time Binance USD-M OOS data acquisition from the public ``data.binance. (+20 more)

### Community 15 - "Community 15"
Cohesion: 0.22
Nodes (24): send_telegram_message(), TelegramConfig, FakeResponse, _install_urlopen(), Stand-in for the object returned by urllib.request.urlopen., Replace urlopen with a recording fake; never touches the network.      Returns a, _set_credentials(), test_2xx_status_codes_return_true() (+16 more)

### Community 16 - "Community 16"
Cohesion: 0.07
Nodes (6): BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     call, BybitPrivateClient must acquire the shared rate limiter before every     pybit H, When _call retries on a failed pybit call, each attempt must hit the     limiter, test_bybit_market_data_routes_get_through_rate_limiter(), test_bybit_private_client_rate_limiter_acquires_each_retry(), test_bybit_private_client_routes_call_through_rate_limiter()

### Community 20 - "Community 20"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **63 isolated node(s):** `ExchangeConfig`, `Thread-safe sliding-window rate limiter shared across BybitMarketData     instan`, `Information-coefficient diagnostics for the reversion-alpha signals.  WHY THIS M`, `Average ranks (1-based), tie-aware — equivalent to scipy.stats.rankdata.`, `Spearman rank correlation of two equal-length vectors.      Returns NaN when eit` (+58 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 20`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 8` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 9`, `Community 11`?**
  _High betweenness centrality (0.109) - this node is a cross-community bridge._
- **Why does `EventDemoCycleConfig` connect `Community 0` to `Community 1`, `Community 4`, `Community 6`, `Community 8`, `Community 12`?**
  _High betweenness centrality (0.106) - this node is a cross-community bridge._
- **Why does `VolumeEventResearchConfig` connect `Community 0` to `Community 1`, `Community 2`, `Community 6`, `Community 8`, `Community 9`?**
  _High betweenness centrality (0.090) - this node is a cross-community bridge._
- **Are the 268 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 268 INFERRED edges - model-reasoned connections that need verification._
- **Are the 214 inferred relationships involving `EventDemoCycleConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`EventDemoCycleConfig` has 214 INFERRED edges - model-reasoned connections that need verification._
- **Are the 176 inferred relationships involving `VolumeEventResearchConfig` (e.g. with `One-line `event demo cycle ...` summary used by both the legacy bash-loop     ru` and `EventDemoDaemon`) actually correct?**
  _`VolumeEventResearchConfig` has 176 INFERRED edges - model-reasoned connections that need verification._
- **Are the 137 inferred relationships involving `ExecutionEventRouter` (e.g. with `EventDemoDaemon` and `Long-running demo entry/exit daemon with WS-driven fill confirmation.  The legac`) actually correct?**
  _`ExecutionEventRouter` has 137 INFERRED edges - model-reasoned connections that need verification._