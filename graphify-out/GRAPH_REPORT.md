# Graph Report - MODEL050426  (2026-05-03)

## Corpus Check
- 27 files · ~55,922 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 252 nodes · 563 edges · 11 communities detected
- Extraction: 79% EXTRACTED · 21% INFERRED · 0% AMBIGUOUS · INFERRED: 119 edges (avg confidence: 0.79)
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
- [[_COMMUNITY_Community 12|Community 12]]

## God Nodes (most connected - your core abstractions)
1. `run_volume_trade_backtest()` - 25 edges
2. `download_market_data()` - 22 edges
3. `run_volume_grid()` - 17 edges
4. `backtest_volume_trades()` - 16 edges
5. `main()` - 15 edges
6. `run_volume_alpha()` - 14 edges
7. `BybitMarketData` - 13 edges
8. `VolumeBacktestConfig` - 12 edges
9. `write_dataset()` - 12 edges
10. `load_config()` - 11 edges

## Surprising Connections (you probably didn't know these)
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `main()` --calls--> `run_volume_grid()`  [INFERRED]
  scripts/run_volume_bucket_sweep.py → aggression_carry/volume_backtest.py
- `run_volume_alpha()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py
- `build_volume_features()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py
- `main()` --calls--> `VolumeGridConfig`  [INFERRED]
  scripts/run_volume_bucket_sweep.py → aggression_carry/config.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.07
Nodes (59): _attribution_rows(), backtest_volume_trades(), _bar_at_close(), _bar_excursion(), _bar_exit_hits(), _btc_monthly_returns(), _btc_regime(), build_equity_curve() (+51 more)

### Community 1 - "Community 1"
Cohesion: 0.13
Nodes (14): BybitDataError, BybitMarketData, _age_filter_label(), build_current_universe_table(), _empty_universe_table(), format_universe_report(), run_discover_universe(), _safe_name() (+6 more)

### Community 2 - "Community 2"
Cohesion: 0.18
Nodes (24): VolumeBacktestConfig, VolumeGridConfig, generate_fixture_data(), dataset_path(), ensure_data_root(), read_dataset(), with_date_column(), write_dataset() (+16 more)

### Community 3 - "Community 3"
Cohesion: 0.14
Nodes (23): download_archive_bytes(), download_public_trade_archive(), read_public_trade_archive(), ResearchConfig, _archive_filename(), _archive_outputs_exist(), _dates_between(), download_market_data() (+15 more)

### Community 4 - "Community 4"
Cohesion: 0.14
Nodes (20): CostConfig, _ordinal_rank(), rank_correlation(), _add_cross_sectional_z(), _add_liquidity_rank(), attach_volume_forward_returns(), _best_base_portfolio(), build_volume_features() (+12 more)

### Community 5 - "Community 5"
Cohesion: 0.2
Nodes (16): TradeFlowConfig, aggregate_signed_flow_1h(), aggregate_signed_flow_1m(), _first_present(), FixtureSpec, normalize_funding_history(), normalize_trade(), _parse_bool() (+8 more)

### Community 6 - "Community 6"
Cohesion: 0.21
Nodes (14): ExchangeConfig, load_config(), _merge_dataclass(), _merge_universe_config(), _merge_volume_alpha_config(), _merge_volume_backtest_config(), _merge_volume_grid_config(), _tuple_bool() (+6 more)

### Community 7 - "Community 7"
Cohesion: 0.35
Nodes (12): _add_universe_backtest_args(), _apply_universe_backtest_args(), _backtest_config_from_args(), build_parser(), _csv_bool(), _csv_float(), _csv_int(), _csv_str() (+4 more)

### Community 8 - "Community 8"
Cohesion: 0.35
Nodes (9): _csv_bool(), _csv_float(), _csv_int(), _csv_str(), _format_summary(), main(), parse_args(), _parse_buckets() (+1 more)

### Community 9 - "Community 9"
Cohesion: 0.44
Nodes (9): command_exists(), install_ao(), install_composio(), install_graphify(), install_skills(), main(), parse_args(), print_status() (+1 more)

### Community 12 - "Community 12"
Cohesion: 1.0
Nodes (1): Bybit volume-alpha research package.  This package is the stripped-down rebuild

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 12`** (2 nodes): `__init__.py`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `download_market_data()` connect `Community 3` to `Community 1`, `Community 2`, `Community 5`, `Community 7`?**
  _High betweenness centrality (0.085) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 8` to `Community 2`, `Community 6`?**
  _High betweenness centrality (0.077) - this node is a cross-community bridge._
- **Why does `run_volume_grid()` connect `Community 2` to `Community 0`, `Community 1`, `Community 4`, `Community 7`, `Community 8`?**
  _High betweenness centrality (0.076) - this node is a cross-community bridge._
- **Are the 10 inferred relationships involving `run_volume_trade_backtest()` (e.g. with `VolumeBacktestConfig` and `CostConfig`) actually correct?**
  _`run_volume_trade_backtest()` has 10 INFERRED edges - model-reasoned connections that need verification._
- **Are the 13 inferred relationships involving `download_market_data()` (e.g. with `main()` and `BybitMarketData`) actually correct?**
  _`download_market_data()` has 13 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `run_volume_grid()` (e.g. with `VolumeGridConfig` and `VolumeBacktestConfig`) actually correct?**
  _`run_volume_grid()` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `main()` (e.g. with `load_config()` and `generate_fixture_data()`) actually correct?**
  _`main()` has 9 INFERRED edges - model-reasoned connections that need verification._