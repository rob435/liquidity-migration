# Graph Report - MODEL050426  (2026-05-16)

## Corpus Check
- 28 files · ~37,954 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 454 nodes · 1106 edges · 10 communities detected
- Extraction: 71% EXTRACTED · 29% INFERRED · 0% AMBIGUOUS · INFERRED: 321 edges (avg confidence: 0.79)
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
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 11|Community 11]]

## God Nodes (most connected - your core abstractions)
1. `run_event_demo_cycle()` - 44 edges
2. `write_dataset()` - 29 edges
3. `VolumeEventResearchConfig` - 28 edges
4. `run_volume_event_research()` - 28 edges
5. `main()` - 25 edges
6. `BybitMarketData` - 24 edges
7. `read_dataset()` - 24 edges
8. `download_market_data()` - 22 edges
9. `_run_event_scenario()` - 21 edges
10. `_float()` - 18 edges

## Surprising Connections (you probably didn't know these)
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `generate_fixture_data()` --calls--> `test_volume_event_research_writes_reports_on_fixture()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_events.py
- `generate_fixture_data()` --calls--> `test_volume_event_research_requires_full_pit_by_default()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_events.py
- `generate_fixture_data()` --calls--> `test_volume_features_build_daily_liquidity_ranks()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_volume_features.py
- `load_config()` --calls--> `test_active_system_config_loads_from_yaml()`  [INFERRED]
  aggression_carry/config.py → tests/test_aggression_carry_config.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (87): TradeLifecycleConfig, _build_demo_features(), build_equity_curve(), _exit_reason_rows(), _funding_lookup(), _price_bars_by_symbol(), _add_rank_fraction(), _apply_market_context_filters() (+79 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (74): _base36(), build_ledger_position_pnl_snapshot(), build_position_pnl_snapshot(), _column_values(), _contract_lookup(), _cooldown_until(), _decimal_text(), _demo_event_config() (+66 more)

### Community 2 - "Community 2"
Cohesion: 0.07
Nodes (61): ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig, ArchiveManifestConfig, format_archive_manifest_report(), _rows_by_date(), run_archive_klines_download(), run_archive_manifest() (+53 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (8): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPublicTradeStream, _is_rate_limit(), _leverage_text(), _build_private_client(), RuntimeError

### Community 4 - "Community 4"
Cohesion: 0.09
Nodes (33): build_parser(), _csv_float(), _csv_int(), _csv_str(), main(), _universe_config_from_args(), CostConfig, ExchangeConfig (+25 more)

### Community 5 - "Community 5"
Cohesion: 0.12
Nodes (36): _archive_kline_skip_rows(), build_archive_trade_manifest(), _bybit_api_kline_url(), _date_from_ts_ms(), _delete_local_archive(), _download_api_hourly_group(), _download_archive_hourly_group(), _download_one_archive_hourly_kline() (+28 more)

### Community 6 - "Community 6"
Cohesion: 0.1
Nodes (30): _bar_excursion(), _bar_exit_hits(), _daily_equity_values(), _date_boundary_ms(), _empty_baskets(), _filter_signal_window(), _filter_universe(), _funding_mode_summary() (+22 more)

### Community 7 - "Community 7"
Cohesion: 0.15
Nodes (25): TradeFlowConfig, aggregate_signed_flow_1h(), aggregate_signed_flow_1m(), aggregate_trade_klines_1h(), aggregate_trade_klines_1m(), densify_trade_klines_1h(), densify_trade_klines_1m(), _first_present() (+17 more)

### Community 9 - "Community 9"
Cohesion: 0.22
Nodes (13): download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _download_and_read_hourly_archive(), _positive_int_env(), _public_trade_text_handle(), read_public_trade_archive(), read_public_trade_archive_klines_1h() (+5 more)

### Community 11 - "Community 11"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit liquidity-migration research package.`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 11`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `run_event_demo_cycle()` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`?**
  _High betweenness centrality (0.097) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 4` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 5`, `Community 7`?**
  _High betweenness centrality (0.075) - this node is a cross-community bridge._
- **Why does `VolumeEventResearchConfig` connect `Community 0` to `Community 1`, `Community 4`?**
  _High betweenness centrality (0.072) - this node is a cross-community bridge._
- **Are the 12 inferred relationships involving `run_event_demo_cycle()` (e.g. with `main()` and `VolumeEventResearchConfig`) actually correct?**
  _`run_event_demo_cycle()` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 24 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `run_archive_manifest()`) actually correct?**
  _`write_dataset()` has 24 INFERRED edges - model-reasoned connections that need verification._
- **Are the 26 inferred relationships involving `VolumeEventResearchConfig` (e.g. with `CostConfig` and `TradeLifecycleConfig`) actually correct?**
  _`VolumeEventResearchConfig` has 26 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `run_volume_event_research()` (e.g. with `main()` and `CostConfig`) actually correct?**
  _`run_volume_event_research()` has 9 INFERRED edges - model-reasoned connections that need verification._