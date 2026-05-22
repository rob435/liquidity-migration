# Graph Report - liquidity-migration  (2026-05-22)

## Corpus Check
- 66 files · ~124,497 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1717 nodes · 5432 edges · 23 communities detected
- Extraction: 55% EXTRACTED · 45% INFERRED · 0% AMBIGUOUS · INFERRED: 2427 edges (avg confidence: 0.66)
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

## God Nodes (most connected - your core abstractions)
1. `ResearchConfig` - 287 edges
2. `EventDemoCycleConfig` - 228 edges
3. `VolumeEventResearchConfig` - 199 edges
4. `ExecutionEventRouter` - 158 edges
5. `EventScenario` - 152 edges
6. `EventRiskCycleConfig` - 138 edges
7. `EventWebSocketRiskEngine` - 120 edges
8. `read_dataset()` - 92 edges
9. `EventWebSocketRiskConfig` - 75 edges
10. `run_event_demo_cycle()` - 71 edges

## Surprising Connections (you probably didn't know these)
- `TradeLifecycleConfig` --uses--> `Correctness tests for the trade lifecycle module.  The lifecycle helpers convert`  [INFERRED]
  liquidity_migration/config.py → tests/test_liquidity_migration_trade_lifecycle.py
- `ExecutionEventRouter` --uses--> `A WS event landing AFTER one caller's timeout must NOT be discarded —     a subs`  [INFERRED]
  liquidity_migration/execution_router.py → tests/test_liquidity_migration_execution_router.py
- `ExecutionEventRouter` --uses--> `On WS disconnect we drop in-flight buffered links so REST fallback     becomes t`  [INFERRED]
  liquidity_migration/execution_router.py → tests/test_liquidity_migration_execution_router.py
- `ExecutionEventRouter` --uses--> `If a link keeps receiving events, eviction must not pick it as victim     even w`  [INFERRED]
  liquidity_migration/execution_router.py → tests/test_liquidity_migration_execution_router.py
- `ExecutionEventRouter` --uses--> `N producer threads writing events for N distinct links, M consumer     threads w`  [INFERRED]
  liquidity_migration/execution_router.py → tests/test_liquidity_migration_execution_router.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (234): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPrivateWebSocketStream, BybitRestRateLimiter, Thread-safe sliding-window rate limiter shared across BybitMarketData     instan, One-line `event demo cycle ...` summary used by both the legacy bash-loop     ru, ResearchConfig (+226 more)

### Community 1 - "Community 1"
Cohesion: 0.02
Nodes (204): _active_position_by_symbol(), _base36(), _bool(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot() (+196 more)

### Community 2 - "Community 2"
Cohesion: 0.02
Nodes (181): CostConfig, TradeLifecycleConfig, _cooldown_until(), _empty_skip_counts(), _realized_stop_exit_ts(), select_demo_entry_candidates(), _trade_id(), FixtureSpec (+173 more)

### Community 3 - "Community 3"
Cohesion: 0.03
Nodes (173): HTMLParser, download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig (+165 more)

### Community 4 - "Community 4"
Cohesion: 0.04
Nodes (80): BinanceDataError, BinanceUSDMData, _ceil_to_period(), _floor_to_period(), _recent_history_start(), _archive_filename(), _archive_outputs_exist(), _dates_between() (+72 more)

### Community 5 - "Community 5"
Cohesion: 0.11
Nodes (69): read_dataset(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, run_event_ws_risk(), BlockingPrivateStream, BlockingPublicStream, FakePrivateClient, FakePrivateStream (+61 more)

### Community 6 - "Community 6"
Cohesion: 0.05
Nodes (71): _audit_violations(), ChallengerSpec, champion_challenger_specs(), format_champion_challenger_manifest(), run_champion_challenger_audit(), _add_archive_download_klines_1h_api_parser(), _add_archive_download_klines_1h_parser(), _add_archive_download_klines_parser() (+63 more)

### Community 7 - "Community 7"
Cohesion: 0.05
Nodes (65): date_boundary_ms(), date_ms(), finite_float(), parse_date(), pct(), Shared low-level helpers and constants for the liquidity_migration package.  Cen, Coerce `value` to a finite float, returning `default` if missing/invalid., Format a fraction as a 2-decimal percentage, or `invalid` if not finite. (+57 more)

### Community 8 - "Community 8"
Cohesion: 0.04
Nodes (37): BybitPublicTickerStream, BybitPublicTradeStream, BybitWebSocketTradeClient, _close_ws_client(), _env_flag(), _is_rate_limit(), _leverage_text(), _patch_pybit_daemon_ping_timer() (+29 more)

### Community 9 - "Community 9"
Cohesion: 0.06
Nodes (64): _bar_excursion(), _bar_exit_hits(), build_equity_curve(), _daily_equity_values(), _date_boundary_ms(), _empty_baskets(), _filter_signal_window(), _filter_universe() (+56 more)

### Community 10 - "Community 10"
Cohesion: 0.07
Nodes (58): compute_reversion_score(), construct_target_book(), _cross_sectional_z(), _daily_mtm(), _date_range(), _day_str(), _day_str_from_index(), _hourly_bars_by_symbol() (+50 more)

### Community 11 - "Community 11"
Cohesion: 0.1
Nodes (51): _artifact_check(), _basket_returns(), _best_summary_row(), _block_bootstrap(), _boolish(), _cluster_report(), _comparison_family_frame(), _comparison_family_label() (+43 more)

### Community 12 - "Community 12"
Cohesion: 0.08
Nodes (37): add_forward_short_returns(), _bar_arrays(), cross_sectional_ic(), ic_table(), ic_vs_horizon(), ICResult, _rankdata(), Information-coefficient diagnostics for the reversion-alpha signals.  WHY THIS M (+29 more)

### Community 13 - "Community 13"
Cohesion: 0.11
Nodes (28): build_binance_oos(), discover(), fetch_month_klines(), list_symbol_months(), list_usdm_usdt_symbols(), main(), parse_month_csv(), Point-in-time Binance USD-M OOS data acquisition from the public ``data.binance. (+20 more)

### Community 14 - "Community 14"
Cohesion: 0.22
Nodes (24): send_telegram_message(), TelegramConfig, FakeResponse, _install_urlopen(), Stand-in for the object returned by urllib.request.urlopen., Replace urlopen with a recording fake; never touches the network.      Returns a, _set_credentials(), test_2xx_status_codes_return_true() (+16 more)

### Community 15 - "Community 15"
Cohesion: 0.07
Nodes (6): BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     call, BybitPrivateClient must acquire the shared rate limiter before every     pybit H, When _call retries on a failed pybit call, each attempt must hit the     limiter, test_bybit_market_data_routes_get_through_rate_limiter(), test_bybit_private_client_rate_limiter_acquires_each_retry(), test_bybit_private_client_routes_call_through_rate_limiter()

### Community 16 - "Community 16"
Cohesion: 0.16
Nodes (19): _clean_trades(), _entry_slippage_bps(), _exit_slippage_bps(), _float(), format_reconciliation_report(), _int(), _normalized_side(), Reconcile the paper (dry-run) ledger against the demo ledger.  The paper runner (+11 more)

### Community 17 - "Community 17"
Cohesion: 0.16
Nodes (18): _exec_event(), On WS disconnect we drop in-flight buffered links so REST fallback     becomes t, If a link keeps receiving events, eviction must not pick it as victim     even w, N producer threads writing events for N distinct links, M consumer     threads w, A WS event landing AFTER one caller's timeout must NOT be discarded —     a subs, test_router_accumulates_partial_fills_in_order(), test_router_caps_buffered_links_with_fifo_eviction(), test_router_clear_all_supports_ws_reconnect() (+10 more)

### Community 21 - "Community 21"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

### Community 23 - "Community 23"
Cohesion: 1.0
Nodes (1): Thread-safe sliding-window rate limiter shared across BybitMarketData     instan

### Community 24 - "Community 24"
Cohesion: 1.0
Nodes (1): BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     call

### Community 25 - "Community 25"
Cohesion: 1.0
Nodes (1): BybitPrivateClient must acquire the shared rate limiter before every     pybit H

### Community 26 - "Community 26"
Cohesion: 1.0
Nodes (1): When _call retries on a failed pybit call, each attempt must hit the     limiter

## Knowledge Gaps
- **74 isolated node(s):** `ExchangeConfig`, `True when environment variable ``name`` is set to a truthy value.`, `Return ``(api_key, api_secret, demo)`` from the .env DEMO / REAL_MONEY toggle.`, `Thread-safe sliding-window rate limiter shared across BybitMarketData     instan`, `Information-coefficient diagnostics for the reversion-alpha signals.  WHY THIS M` (+69 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 21`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 23`** (1 nodes): `Thread-safe sliding-window rate limiter shared across BybitMarketData     instan`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 24`** (1 nodes): `BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     call`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 25`** (1 nodes): `BybitPrivateClient must acquire the shared rate limiter before every     pybit H`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 26`** (1 nodes): `When _call retries on a failed pybit call, each attempt must hit the     limiter`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 6` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 7`, `Community 8`, `Community 11`, `Community 16`?**
  _High betweenness centrality (0.111) - this node is a cross-community bridge._
- **Why does `EventDemoCycleConfig` connect `Community 0` to `Community 8`, `Community 1`, `Community 5`, `Community 6`?**
  _High betweenness centrality (0.100) - this node is a cross-community bridge._
- **Why does `VolumeEventResearchConfig` connect `Community 0` to `Community 8`, `Community 1`, `Community 2`, `Community 6`?**
  _High betweenness centrality (0.089) - this node is a cross-community bridge._
- **Are the 285 inferred relationships involving `ResearchConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`ResearchConfig` has 285 INFERRED edges - model-reasoned connections that need verification._
- **Are the 225 inferred relationships involving `EventDemoCycleConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`EventDemoCycleConfig` has 225 INFERRED edges - model-reasoned connections that need verification._
- **Are the 196 inferred relationships involving `VolumeEventResearchConfig` (e.g. with `One-line `event demo cycle ...` summary used by both the legacy bash-loop     ru` and `EventDemoDaemon`) actually correct?**
  _`VolumeEventResearchConfig` has 196 INFERRED edges - model-reasoned connections that need verification._
- **Are the 147 inferred relationships involving `ExecutionEventRouter` (e.g. with `EventDemoDaemon` and `Long-running demo entry/exit daemon with WS-driven fill confirmation.  The legac`) actually correct?**
  _`ExecutionEventRouter` has 147 INFERRED edges - model-reasoned connections that need verification._