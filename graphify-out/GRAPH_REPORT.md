# Graph Report - MODEL050426  (2026-05-02)

## Corpus Check
- 22 files · ~21,186 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 134 nodes · 253 edges · 9 communities detected
- Extraction: 76% EXTRACTED · 24% INFERRED · 0% AMBIGUOUS · INFERRED: 61 edges (avg confidence: 0.79)
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
- [[_COMMUNITY_Community 10|Community 10]]

## God Nodes (most connected - your core abstractions)
1. `download_market_data()` - 22 edges
2. `run_volume_alpha()` - 14 edges
3. `BybitMarketData` - 12 edges
4. `write_dataset()` - 10 edges
5. `trades_to_frame()` - 9 edges
6. `main()` - 8 edges
7. `main()` - 8 edges
8. `aggregate_signed_flow_1m()` - 7 edges
9. `build_volume_features()` - 7 edges
10. `load_config()` - 7 edges

## Surprising Connections (you probably didn't know these)
- `test_trade_parser_handles_websocket_aliases_and_string_booleans()` --calls--> `trades_to_frame()`  [INFERRED]
  tests/test_aggression_carry_ingestion.py → aggression_carry/ingestion.py
- `test_volume_alpha_isolated_daily_research_path()` --calls--> `generate_fixture_data()`  [INFERRED]
  tests/test_aggression_carry_volume_alpha.py → aggression_carry/ingestion.py
- `test_volume_alpha_controls_load_from_yaml()` --calls--> `load_config()`  [INFERRED]
  tests/test_aggression_carry_config.py → aggression_carry/config.py
- `test_volume_alpha_isolated_daily_research_path()` --calls--> `read_dataset()`  [INFERRED]
  tests/test_aggression_carry_volume_alpha.py → aggression_carry/storage.py
- `test_download_public_trade_archive_ignores_stale_fixed_temp_name()` --calls--> `download_public_trade_archive()`  [INFERRED]
  tests/test_aggression_carry_archive.py → aggression_carry/archive.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.15
Nodes (22): download_archive_bytes(), download_public_trade_archive(), read_public_trade_archive(), ResearchConfig, _archive_filename(), _archive_outputs_exist(), _dates_between(), download_market_data() (+14 more)

### Community 1 - "Community 1"
Cohesion: 0.19
Nodes (17): TradeFlowConfig, aggregate_signed_flow_1h(), aggregate_signed_flow_1m(), _first_present(), FixtureSpec, generate_fixture_data(), normalize_funding_history(), normalize_trade() (+9 more)

### Community 2 - "Community 2"
Cohesion: 0.2
Nodes (17): _add_cross_sectional_z(), attach_volume_forward_returns(), _best_base_portfolio(), build_volume_features(), compute_volume_metrics(), _cost_adjusted_spread(), _daily_bars(), _date_range() (+9 more)

### Community 3 - "Community 3"
Cohesion: 0.18
Nodes (10): build_parser(), main(), CostConfig, ExchangeConfig, load_config(), _merge_dataclass(), _merge_volume_alpha_config(), VolumeAlphaConfig (+2 more)

### Community 4 - "Community 4"
Cohesion: 0.25
Nodes (3): BybitDataError, BybitMarketData, RuntimeError

### Community 5 - "Community 5"
Cohesion: 0.33
Nodes (10): command_exists(), install_ao(), install_composio(), install_graphify(), install_skills(), main(), parse_args(), print_status() (+2 more)

### Community 6 - "Community 6"
Cohesion: 0.38
Nodes (8): dataset_path(), ensure_data_root(), read_dataset(), with_date_column(), write_dataset(), _write_part(), test_incremental_parquet_writes_merge_existing_partition(), test_incremental_parquet_writes_replace_duplicate_keys()

### Community 7 - "Community 7"
Cohesion: 0.4
Nodes (2): _ordinal_rank(), rank_correlation()

### Community 10 - "Community 10"
Cohesion: 1.0
Nodes (1): Bybit volume-alpha research package.  This package is the stripped-down rebuild

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 7`** (6 nodes): `ema()`, `_ordinal_rank()`, `math_utils.py`, `rank_correlation()`, `robust_z()`, `rolling_median()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 10`** (2 nodes): `__init__.py`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `download_market_data()` connect `Community 0` to `Community 1`, `Community 3`, `Community 4`, `Community 6`?**
  _High betweenness centrality (0.234) - this node is a cross-community bridge._
- **Why does `install_skills()` connect `Community 5` to `Community 4`?**
  _High betweenness centrality (0.137) - this node is a cross-community bridge._
- **Why does `run_volume_alpha()` connect `Community 2` to `Community 3`, `Community 4`, `Community 6`?**
  _High betweenness centrality (0.133) - this node is a cross-community bridge._
- **Are the 13 inferred relationships involving `download_market_data()` (e.g. with `main()` and `BybitMarketData`) actually correct?**
  _`download_market_data()` has 13 INFERRED edges - model-reasoned connections that need verification._
- **Are the 6 inferred relationships involving `run_volume_alpha()` (e.g. with `read_dataset()` and `RuntimeError`) actually correct?**
  _`run_volume_alpha()` has 6 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `run_volume_alpha()`) actually correct?**
  _`write_dataset()` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Are the 7 inferred relationships involving `trades_to_frame()` (e.g. with `read_public_trade_archive()` and `download_market_data()`) actually correct?**
  _`trades_to_frame()` has 7 INFERRED edges - model-reasoned connections that need verification._