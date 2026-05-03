# Graph Report - MODEL050426  (2026-05-04)

## Corpus Check
- 34 files · ~120,393 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 443 nodes · 1151 edges · 12 communities detected
- Extraction: 75% EXTRACTED · 25% INFERRED · 0% AMBIGUOUS · INFERRED: 288 edges (avg confidence: 0.79)
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
1. `read_dataset()` - 40 edges
2. `main()` - 32 edges
3. `run_volume_trade_backtest()` - 27 edges
4. `download_market_data()` - 24 edges
5. `run_daily_close_fade()` - 23 edges
6. `DailyCloseFadeConfig` - 22 edges
7. `CostConfig` - 22 edges
8. `run_forward_once()` - 21 edges
9. `write_dataset()` - 21 edges
10. `run_volume_grid()` - 18 edges

## Surprising Connections (you probably didn't know these)
- `test_cli_parses_forward_sleeves_alias()` --calls--> `build_parser()`  [INFERRED]
  tests/test_aggression_carry_cli.py → aggression_carry/cli.py
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `run_volume_grid()` --calls--> `main()`  [INFERRED]
  aggression_carry/volume_backtest.py → scripts/run_volume_bucket_sweep.py
- `run_volume_alpha()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py
- `build_volume_features()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (84): VolumeBacktestConfig, VolumeGridConfig, generate_fixture_data(), _attribution_rows(), backtest_volume_trades(), _bar_at_close(), _bar_excursion(), _bar_exit_hits() (+76 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (63): _close_fade_position_weight(), _as_utc(), _basket_already_opened(), _bool_value(), build_forward_scan_features(), build_forward_universe(), _concat(), _count_reason() (+55 more)

### Community 2 - "Community 2"
Cohesion: 0.07
Nodes (55): download_archive_bytes(), download_public_trade_archive(), read_public_trade_archive(), ResearchConfig, TradeFlowConfig, _archive_filename(), _dates_between(), download_market_data() (+47 more)

### Community 3 - "Community 3"
Cohesion: 0.1
Nodes (44): _apply_basket_stop_to_rows(), apply_close_fade_basket_stop(), _attach_archive_membership(), _attach_instrument_age(), backtest_daily_close_fade(), _baseline_liquidity_filter_expr(), _baseline_liquidity_rank(), build_close_fade_equity() (+36 more)

### Community 4 - "Community 4"
Cohesion: 0.21
Nodes (28): CostConfig, DailyCloseFadeConfig, _replace_dataset(), run_daily_close_fade(), _archive_outputs_exist(), _partition_exists(), dataset_path(), ensure_data_root() (+20 more)

### Community 5 - "Community 5"
Cohesion: 0.13
Nodes (25): ArchiveKlineDownloadConfig, ArchiveManifestConfig, build_archive_trade_manifest(), _download_one_archive_kline(), _download_result(), _empty_download_results(), _empty_manifest(), fetch_directory_html() (+17 more)

### Community 6 - "Community 6"
Cohesion: 0.15
Nodes (19): _ordinal_rank(), rank_correlation(), _add_cross_sectional_z(), _add_liquidity_rank(), attach_volume_forward_returns(), _best_base_portfolio(), build_volume_features(), compute_volume_metrics() (+11 more)

### Community 7 - "Community 7"
Cohesion: 0.22
Nodes (22): _add_forward_fade_args(), _add_forward_runtime_args(), _add_universe_backtest_args(), _apply_universe_backtest_args(), _backtest_config_from_args(), build_parser(), _close_fade_base_from_grid_args(), _close_fade_config_from_args() (+14 more)

### Community 8 - "Community 8"
Cohesion: 0.19
Nodes (19): DailyCloseFadeGridConfig, ExchangeConfig, ForwardTestConfig, load_config(), _merge_daily_close_fade_config(), _merge_daily_close_fade_grid_config(), _merge_dataclass(), _merge_forward_test_config() (+11 more)

### Community 9 - "Community 9"
Cohesion: 0.27
Nodes (2): BybitDataError, BybitMarketData

### Community 10 - "Community 10"
Cohesion: 0.3
Nodes (10): _csv_bool(), _csv_float(), _csv_int(), _csv_str(), _format_summary(), main(), parse_args(), _parse_buckets() (+2 more)

### Community 14 - "Community 14"
Cohesion: 1.0
Nodes (1): Bybit volume-alpha research package.  This package is the stripped-down rebuild

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 9`** (13 nodes): `BybitDataError`, `BybitMarketData`, `._get()`, `.get_funding_history()`, `.get_instruments_info()`, `.get_klines()`, `.get_open_interest()`, `.get_orderbook()`, `.get_recent_trades()`, `.get_tickers()`, `._paged_time_range()`, `.__post_init__()`, `bybit.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 14`** (2 nodes): `__init__.py`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `read_dataset()` connect `Community 4` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 5`, `Community 6`?**
  _High betweenness centrality (0.087) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 7` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 8`?**
  _High betweenness centrality (0.085) - this node is a cross-community bridge._
- **Why does `run_volume_grid()` connect `Community 0` to `Community 3`, `Community 4`, `Community 6`, `Community 7`, `Community 10`?**
  _High betweenness centrality (0.045) - this node is a cross-community bridge._
- **Are the 38 inferred relationships involving `read_dataset()` (e.g. with `run_volume_trade_backtest()` and `run_volume_grid()`) actually correct?**
  _`read_dataset()` has 38 INFERRED edges - model-reasoned connections that need verification._
- **Are the 20 inferred relationships involving `main()` (e.g. with `load_config()` and `generate_fixture_data()`) actually correct?**
  _`main()` has 20 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `run_volume_trade_backtest()` (e.g. with `VolumeBacktestConfig` and `CostConfig`) actually correct?**
  _`run_volume_trade_backtest()` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 17 inferred relationships involving `download_market_data()` (e.g. with `main()` and `BybitMarketData`) actually correct?**
  _`download_market_data()` has 17 INFERRED edges - model-reasoned connections that need verification._