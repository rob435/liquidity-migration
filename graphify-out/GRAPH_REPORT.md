# Graph Report - MODEL050426  (2026-05-17)

## Corpus Check
- 31 files · ~50,883 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 601 nodes · 1605 edges · 11 communities detected
- Extraction: 70% EXTRACTED · 30% INFERRED · 0% AMBIGUOUS · INFERRED: 486 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 12|Community 12]]

## God Nodes (most connected - your core abstractions)
1. `run_event_demo_cycle()` - 45 edges
2. `EventWebSocketRiskEngine` - 41 edges
3. `write_dataset()` - 33 edges
4. `main()` - 32 edges
5. `read_dataset()` - 32 edges
6. `VolumeEventResearchConfig` - 32 edges
7. `run_event_risk_cycle()` - 30 edges
8. `_float()` - 29 edges
9. `run_volume_event_research()` - 28 edges
10. `BybitMarketData` - 27 edges

## Surprising Connections (you probably didn't know these)
- `_scenario_side()` --calls--> `test_selloff_exhaustion_side_hypotheses_are_directional()`  [INFERRED]
  aggression_carry/volume_events.py → tests/test_aggression_carry_volume_events.py
- `plan_risk_exits()` --calls--> `test_plan_risk_exits_uses_live_position_price_for_stops()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `plan_stop_repairs()` --calls--> `test_plan_stop_repairs_detects_missing_exchange_stop()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `build_ledger_position_pnl_snapshot()` --calls--> `test_ledger_position_snapshot_marks_short_pnl_from_current_price()`  [INFERRED]
  aggression_carry/event_demo.py → tests/test_aggression_carry_event_demo.py
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (92): _active_position_by_symbol(), _base36(), _build_demo_features(), _build_demo_universe(), build_event_risk_private_client(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot(), _build_private_client() (+84 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (95): TradeLifecycleConfig, build_equity_curve(), _exit_reason_rows(), _add_rank_fraction(), _apply_market_context_filters(), _attach_event_archive_membership(), _attach_market_context(), _bottom_cut_from_top_cut() (+87 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (78): _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _bybit_api_kline_url(), _date_from_ts_ms() (+70 more)

### Community 3 - "Community 3"
Cohesion: 0.07
Nodes (29): BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitWebSocketTradeClient, _close_ws_client(), _is_rate_limit(), ResearchConfig, EventRiskCycleConfig, _build_private_stream() (+21 more)

### Community 4 - "Community 4"
Cohesion: 0.06
Nodes (58): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _positive_int_env(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h(), _read_public_trade_archive_klines_1h_vectorized() (+50 more)

### Community 5 - "Community 5"
Cohesion: 0.09
Nodes (34): _bar_excursion(), _bar_exit_hits(), _daily_equity_values(), _date_boundary_ms(), _empty_baskets(), _filter_signal_window(), _filter_universe(), _funding_lookup() (+26 more)

### Community 6 - "Community 6"
Cohesion: 0.09
Nodes (27): build_parser(), EventDemoCycleConfig, _validate_demo_config(), EventScenario, test_cli_archive_hourly_api_kline_default_resumes_written_partitions(), test_cli_archive_hourly_kline_default_resumes_written_partitions(), test_cli_archive_kline_default_requires_dense_utc_day(), test_cli_download_data_default_open_interest_interval() (+19 more)

### Community 7 - "Community 7"
Cohesion: 0.11
Nodes (26): _csv_float(), _csv_int(), _csv_str(), _event_risk_payload_material(), main(), _print_event_risk_summary(), _universe_config_from_args(), CostConfig (+18 more)

### Community 8 - "Community 8"
Cohesion: 0.11
Nodes (13): BybitDataError, BybitPrivateClient, BybitPublicTradeStream, _leverage_text(), _price_bars_by_symbol(), RuntimeError, _contract(), _decimal_text() (+5 more)

### Community 10 - "Community 10"
Cohesion: 0.21
Nodes (1): BybitMarketData

### Community 12 - "Community 12"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit liquidity-migration research package.`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 10`** (17 nodes): `BybitMarketData`, `._get()`, `.get_funding_history()`, `.get_index_price_klines()`, `.get_instruments_info()`, `.get_klines()`, `.get_mark_price_klines()`, `.get_open_interest()`, `.get_orderbook()`, `.get_premium_index_klines()`, `._get_price_index_klines()`, `.get_recent_trades()`, `.get_tickers()`, `._paged_time_range()`, `._record_call()`, `.reset_stats()`, `.stats()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 12`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 7` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 6`, `Community 8`?**
  _High betweenness centrality (0.074) - this node is a cross-community bridge._
- **Why does `VolumeEventResearchConfig` connect `Community 1` to `Community 0`, `Community 3`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.067) - this node is a cross-community bridge._
- **Why does `run_event_demo_cycle()` connect `Community 0` to `Community 1`, `Community 2`, `Community 4`, `Community 6`, `Community 7`, `Community 8`, `Community 10`?**
  _High betweenness centrality (0.062) - this node is a cross-community bridge._
- **Are the 12 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 16 inferred relationships involving `EventWebSocketRiskEngine` (e.g. with `BybitPrivateClient` and `BybitPrivateWebSocketStream`) actually correct?**
  _`EventWebSocketRiskEngine` has 16 INFERRED edges - model-reasoned connections that need verification._
- **Are the 28 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `.write_report()`) actually correct?**
  _`write_dataset()` has 28 INFERRED edges - model-reasoned connections that need verification._
- **Are the 24 inferred relationships involving `main()` (e.g. with `load_config()` and `generate_fixture_data()`) actually correct?**
  _`main()` has 24 INFERRED edges - model-reasoned connections that need verification._