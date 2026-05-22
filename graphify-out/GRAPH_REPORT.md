# Graph Report - liquidity-migration  (2026-05-22)

## Corpus Check
- 66 files · ~126,868 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1753 nodes · 5595 edges · 25 communities detected
- Extraction: 54% EXTRACTED · 46% INFERRED · 0% AMBIGUOUS · INFERRED: 2568 edges (avg confidence: 0.65)
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
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]

## God Nodes (most connected - your core abstractions)
1. `ResearchConfig` - 307 edges
2. `EventDemoCycleConfig` - 241 edges
3. `VolumeEventResearchConfig` - 216 edges
4. `EventScenario` - 169 edges
5. `ExecutionEventRouter` - 168 edges
6. `EventRiskCycleConfig` - 151 edges
7. `EventWebSocketRiskEngine` - 120 edges
8. `read_dataset()` - 91 edges
9. `EventWebSocketRiskConfig` - 75 edges
10. `run_event_demo_cycle()` - 71 edges

## Surprising Connections (you probably didn't know these)
- `TradeLifecycleConfig` --uses--> `Correctness tests for the trade lifecycle module.  The lifecycle helpers convert`  [INFERRED]
  liquidity_migration/config.py → tests/test_liquidity_migration_trade_lifecycle.py
- `build_parser()` --calls--> `test_cli_strategy_tribunal_parses_research_controls()`  [INFERRED]
  liquidity_migration/cli.py → tests/test_liquidity_migration_strategy_tribunal.py
- `VolumeEventResearchConfig` --uses--> `Backtest a named demo strategy profile (promoted / demo_relaxed).  The volume-ev`  [INFERRED]
  liquidity_migration/volume_events.py → scripts/backtest_profile.py
- `plan_risk_exits()` --calls--> `test_plan_risk_exits_uses_live_position_price_for_stops()`  [INFERRED]
  liquidity_migration/event_demo.py → tests/test_liquidity_migration_event_demo.py
- `plan_stop_repairs()` --calls--> `test_plan_stop_repairs_detects_missing_exchange_stop()`  [INFERRED]
  liquidity_migration/event_demo.py → tests/test_liquidity_migration_event_demo.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (233): BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitWebSocketTradeClient, _close_ws_client(), ResearchConfig, _collect_private_snapshots(), _build_private_ws_stream(), EventDemoDaemon (+225 more)

### Community 1 - "Community 1"
Cohesion: 0.03
Nodes (151): _env_flag(), True when environment variable ``name`` is set to a truthy value., Return ``(api_key, api_secret, demo)`` from the .env DEMO / REAL_MONEY toggle., resolve_private_credentials(), _active_position_by_symbol(), _base36(), _bool(), _build_demo_universe() (+143 more)

### Community 2 - "Community 2"
Cohesion: 0.03
Nodes (181): HTMLParser, download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig (+173 more)

### Community 3 - "Community 3"
Cohesion: 0.03
Nodes (177): CostConfig, TradeLifecycleConfig, FixtureSpec, generate_fixture_data(), _price_bars_by_symbol(), _add_event_uniqueness_score(), _add_liquidity_migration_speed_features(), _add_rank_fraction() (+169 more)

### Community 4 - "Community 4"
Cohesion: 0.04
Nodes (77): BybitPublicTradeStream, _patch_pybit_daemon_ping_timer(), _build_demo_features(), _demo_feature_cache_fingerprint(), _demo_feature_cache_paths(), _execute_entries(), _read_demo_feature_cache(), _validate_demo_config() (+69 more)

### Community 5 - "Community 5"
Cohesion: 0.04
Nodes (82): BinanceDataError, BinanceUSDMData, _ceil_to_period(), _floor_to_period(), _recent_history_start(), Slugify `name` for use as a file or path component., safe_name(), _archive_filename() (+74 more)

### Community 6 - "Community 6"
Cohesion: 0.11
Nodes (69): read_dataset(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, run_event_ws_risk(), BlockingPrivateStream, BlockingPublicStream, FakePrivateClient, FakePrivateStream (+61 more)

### Community 7 - "Community 7"
Cohesion: 0.04
Nodes (75): _audit_violations(), ChallengerSpec, champion_challenger_specs(), format_champion_challenger_manifest(), _parse_command_tokens(), Split a manifest command into its leading ``KEY=value`` env assignments     and, True if the command carries an order-submission flag as a real token:     either, run_champion_challenger_audit() (+67 more)

### Community 8 - "Community 8"
Cohesion: 0.06
Nodes (47): BybitDataError, BybitMarketData, BybitPrivateClient, BybitRestRateLimiter, _is_rate_limit(), _leverage_text(), Thread-safe sliding-window rate limiter shared across BybitMarketData     instan, UniverseConfig (+39 more)

### Community 9 - "Community 9"
Cohesion: 0.05
Nodes (73): date_boundary_ms(), date_ms(), finite_float(), parse_date(), pct(), Shared low-level helpers and constants for the liquidity_migration package.  Cen, Coerce `value` to a finite float, returning `default` if missing/invalid., Format a fraction as a 2-decimal percentage, or `invalid` if not finite. (+65 more)

### Community 10 - "Community 10"
Cohesion: 0.05
Nodes (73): _bar_excursion(), _bar_exit_hits(), build_equity_curve(), _daily_equity_values(), _date_boundary_ms(), _empty_baskets(), _exit_reason_rows(), _filter_signal_window() (+65 more)

### Community 11 - "Community 11"
Cohesion: 0.1
Nodes (51): _artifact_check(), _basket_returns(), _best_summary_row(), _block_bootstrap(), _boolish(), _cluster_report(), _comparison_family_frame(), _comparison_family_label() (+43 more)

### Community 12 - "Community 12"
Cohesion: 0.09
Nodes (50): compute_reversion_score(), construct_target_book(), _cross_sectional_z(), _daily_mtm(), _date_range(), _day_str(), _day_str_from_index(), _hourly_bars_by_symbol() (+42 more)

### Community 13 - "Community 13"
Cohesion: 0.08
Nodes (37): add_forward_short_returns(), _bar_arrays(), cross_sectional_ic(), ic_table(), ic_vs_horizon(), ICResult, _rankdata(), Information-coefficient diagnostics for the reversion-alpha signals.  WHY THIS M (+29 more)

### Community 14 - "Community 14"
Cohesion: 0.11
Nodes (28): build_binance_oos(), discover(), fetch_month_klines(), list_symbol_months(), list_usdm_usdt_symbols(), main(), parse_month_csv(), Point-in-time Binance USD-M OOS data acquisition from the public ``data.binance. (+20 more)

### Community 15 - "Community 15"
Cohesion: 0.22
Nodes (24): send_telegram_message(), TelegramConfig, FakeResponse, _install_urlopen(), Stand-in for the object returned by urllib.request.urlopen., Replace urlopen with a recording fake; never touches the network.      Returns a, _set_credentials(), test_2xx_status_codes_return_true() (+16 more)

### Community 16 - "Community 16"
Cohesion: 0.07
Nodes (6): BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     call, BybitPrivateClient must acquire the shared rate limiter before every     pybit H, When _call retries on a failed pybit call, each attempt must hit the     limiter, test_bybit_market_data_routes_get_through_rate_limiter(), test_bybit_private_client_rate_limiter_acquires_each_retry(), test_bybit_private_client_routes_call_through_rate_limiter()

### Community 17 - "Community 17"
Cohesion: 0.16
Nodes (19): _clean_trades(), _entry_slippage_bps(), _exit_slippage_bps(), _float(), format_reconciliation_report(), _int(), _normalized_side(), Reconcile the paper (dry-run) ledger against the demo ledger.  The paper runner (+11 more)

### Community 21 - "Community 21"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

### Community 23 - "Community 23"
Cohesion: 1.0
Nodes (1): Render a reconciliation result (from reconcile_paper_demo) as markdown.

### Community 24 - "Community 24"
Cohesion: 1.0
Nodes (1): Read the paper and demo trade ledgers, reconcile them, write a markdown     repo

### Community 25 - "Community 25"
Cohesion: 1.0
Nodes (1): Thread-safe sliding-window rate limiter shared across BybitMarketData     instan

### Community 26 - "Community 26"
Cohesion: 1.0
Nodes (1): BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     call

### Community 27 - "Community 27"
Cohesion: 1.0
Nodes (1): BybitPrivateClient must acquire the shared rate limiter before every     pybit H

### Community 28 - "Community 28"
Cohesion: 1.0
Nodes (1): When _call retries on a failed pybit call, each attempt must hit the     limiter

## Knowledge Gaps
- **80 isolated node(s):** `ExchangeConfig`, `True when environment variable ``name`` is set to a truthy value.`, `Return ``(api_key, api_secret, demo)`` from the .env DEMO / REAL_MONEY toggle.`, `Thread-safe sliding-window rate limiter shared across BybitMarketData     instan`, `Information-coefficient diagnostics for the reversion-alpha signals.  WHY THIS M` (+75 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 21`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 23`** (1 nodes): `Render a reconciliation result (from reconcile_paper_demo) as markdown.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 24`** (1 nodes): `Read the paper and demo trade ledgers, reconcile them, write a markdown     repo`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 25`** (1 nodes): `Thread-safe sliding-window rate limiter shared across BybitMarketData     instan`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 26`** (1 nodes): `BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     call`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 27`** (1 nodes): `BybitPrivateClient must acquire the shared rate limiter before every     pybit H`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 28`** (1 nodes): `When _call retries on a failed pybit call, each attempt must hit the     limiter`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 7` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 9`, `Community 11`, `Community 17`?**
  _High betweenness centrality (0.122) - this node is a cross-community bridge._
- **Why does `EventDemoCycleConfig` connect `Community 0` to `Community 1`, `Community 4`, `Community 6`, `Community 7`, `Community 8`?**
  _High betweenness centrality (0.115) - this node is a cross-community bridge._
- **Why does `VolumeEventResearchConfig` connect `Community 0` to `Community 1`, `Community 3`, `Community 4`, `Community 7`, `Community 8`?**
  _High betweenness centrality (0.086) - this node is a cross-community bridge._
- **Are the 305 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 305 INFERRED edges - model-reasoned connections that need verification._
- **Are the 238 inferred relationships involving `EventDemoCycleConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`EventDemoCycleConfig` has 238 INFERRED edges - model-reasoned connections that need verification._
- **Are the 213 inferred relationships involving `VolumeEventResearchConfig` (e.g. with `One-line `event demo cycle ...` summary used by both the legacy bash-loop     ru` and `EventDemoDaemon`) actually correct?**
  _`VolumeEventResearchConfig` has 213 INFERRED edges - model-reasoned connections that need verification._
- **Are the 167 inferred relationships involving `EventScenario` (e.g. with `CostConfig` and `TradeLifecycleConfig`) actually correct?**
  _`EventScenario` has 167 INFERRED edges - model-reasoned connections that need verification._