# Graph Report - MODEL050426  (2026-05-03)

## Corpus Check
- 34 files · ~119,405 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 435 nodes · 1127 edges · 12 communities detected
- Extraction: 75% EXTRACTED · 25% INFERRED · 0% AMBIGUOUS · INFERRED: 282 edges (avg confidence: 0.79)
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
- [[_COMMUNITY_Community 14|Community 14]]

## God Nodes (most connected - your core abstractions)
1. `read_dataset()` - 38 edges
2. `main()` - 31 edges
3. `run_volume_trade_backtest()` - 27 edges
4. `download_market_data()` - 24 edges
5. `run_daily_close_fade()` - 23 edges
6. `DailyCloseFadeConfig` - 22 edges
7. `CostConfig` - 22 edges
8. `write_dataset()` - 21 edges
9. `run_forward_once()` - 20 edges
10. `run_volume_grid()` - 18 edges

## Surprising Connections (you probably didn't know these)
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `run_volume_grid()` --calls--> `main()`  [INFERRED]
  aggression_carry/volume_backtest.py → scripts/run_volume_bucket_sweep.py
- `run_volume_alpha()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py
- `build_volume_features()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py
- `VolumeGridConfig` --calls--> `main()`  [INFERRED]
  aggression_carry/config.py → scripts/run_volume_bucket_sweep.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (83): VolumeBacktestConfig, VolumeGridConfig, generate_fixture_data(), _attribution_rows(), backtest_volume_trades(), _bar_at_close(), _bar_excursion(), _bar_exit_hits() (+75 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (56): download_archive_bytes(), download_public_trade_archive(), read_public_trade_archive(), ResearchConfig, TradeFlowConfig, _archive_filename(), _archive_outputs_exist(), _dates_between() (+48 more)

### Community 2 - "Community 2"
Cohesion: 0.1
Nodes (46): _apply_basket_stop_to_rows(), apply_close_fade_basket_stop(), _attach_archive_membership(), _attach_instrument_age(), backtest_daily_close_fade(), _baseline_liquidity_filter_expr(), _baseline_liquidity_rank(), build_close_fade_equity() (+38 more)

### Community 3 - "Community 3"
Cohesion: 0.1
Nodes (46): _as_utc(), _basket_already_opened(), _bool_value(), build_forward_scan_features(), build_forward_universe(), _concat(), _count_reason(), _count_status() (+38 more)

### Community 4 - "Community 4"
Cohesion: 0.19
Nodes (26): CostConfig, DailyCloseFadeConfig, run_daily_close_fade(), read_dataset(), test_daily_close_fade_basket_stop_exits_open_basket(), test_daily_close_fade_can_require_archive_membership(), test_daily_close_fade_capacity_caps_trade_weight(), test_daily_close_fade_excludes_young_pumps_and_writes_trade_ledger() (+18 more)

### Community 5 - "Community 5"
Cohesion: 0.13
Nodes (25): ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _download_one_archive_kline(), _download_result(), _empty_download_results(), _empty_manifest(), fetch_directory_html() (+17 more)

### Community 6 - "Community 6"
Cohesion: 0.14
Nodes (12): BybitDataError, BybitMarketData, _age_filter_label(), build_current_universe_table(), _empty_universe_table(), format_universe_report(), run_discover_universe(), _safe_name() (+4 more)

### Community 7 - "Community 7"
Cohesion: 0.15
Nodes (19): _ordinal_rank(), rank_correlation(), _add_cross_sectional_z(), _add_liquidity_rank(), attach_volume_forward_returns(), _best_base_portfolio(), build_volume_features(), compute_volume_metrics() (+11 more)

### Community 8 - "Community 8"
Cohesion: 0.22
Nodes (22): _add_forward_fade_args(), _add_forward_runtime_args(), _add_universe_backtest_args(), _apply_universe_backtest_args(), _backtest_config_from_args(), build_parser(), _close_fade_base_from_grid_args(), _close_fade_config_from_args() (+14 more)

### Community 9 - "Community 9"
Cohesion: 0.19
Nodes (19): DailyCloseFadeGridConfig, ExchangeConfig, ForwardTestConfig, load_config(), _merge_daily_close_fade_config(), _merge_daily_close_fade_grid_config(), _merge_dataclass(), _merge_forward_test_config() (+11 more)

### Community 10 - "Community 10"
Cohesion: 0.35
Nodes (9): _csv_bool(), _csv_float(), _csv_int(), _csv_str(), _format_summary(), main(), parse_args(), _parse_buckets() (+1 more)

### Community 14 - "Community 14"
Cohesion: 1.0
Nodes (1): Bybit volume-alpha research package.  This package is the stripped-down rebuild

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 14`** (2 nodes): `__init__.py`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `read_dataset()` connect `Community 4` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 5`, `Community 7`?**
  _High betweenness centrality (0.083) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 8` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 9`?**
  _High betweenness centrality (0.082) - this node is a cross-community bridge._
- **Why does `run_volume_grid()` connect `Community 0` to `Community 1`, `Community 2`, `Community 4`, `Community 7`, `Community 8`, `Community 10`?**
  _High betweenness centrality (0.047) - this node is a cross-community bridge._
- **Are the 36 inferred relationships involving `read_dataset()` (e.g. with `run_volume_trade_backtest()` and `run_volume_grid()`) actually correct?**
  _`read_dataset()` has 36 INFERRED edges - model-reasoned connections that need verification._
- **Are the 19 inferred relationships involving `main()` (e.g. with `load_config()` and `generate_fixture_data()`) actually correct?**
  _`main()` has 19 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `run_volume_trade_backtest()` (e.g. with `VolumeBacktestConfig` and `CostConfig`) actually correct?**
  _`run_volume_trade_backtest()` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 17 inferred relationships involving `download_market_data()` (e.g. with `main()` and `BybitMarketData`) actually correct?**
  _`download_market_data()` has 17 INFERRED edges - model-reasoned connections that need verification._