# Graph Report - MODEL050426  (2026-05-04)

## Corpus Check
- 62 files · ~80,287 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 864 nodes · 2227 edges · 26 communities detected
- Extraction: 77% EXTRACTED · 23% INFERRED · 0% AMBIGUOUS · INFERRED: 522 edges (avg confidence: 0.77)
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
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]

## God Nodes (most connected - your core abstractions)
1. `read_dataset()` - 48 edges
2. `main()` - 47 edges
3. `ResearchConfig` - 45 edges
4. `write_dataset()` - 33 edges
5. `DailyCloseFadeConfig` - 30 edges
6. `_FakeExecution` - 29 edges
7. `run_volume_trade_backtest()` - 27 edges
8. `CostConfig` - 26 edges
9. `run_bybit_demo_sync()` - 24 edges
10. `download_market_data()` - 24 edges

## Surprising Connections (you probably didn't know these)
- `trades_to_frame()` --calls--> `test_trade_parser_handles_websocket_aliases_and_string_booleans()`  [INFERRED]
  aggression_carry/ingestion.py → tests/test_aggression_carry_ingestion.py
- `run_volume_grid()` --calls--> `main()`  [INFERRED]
  aggression_carry/volume_backtest.py → scripts/run_volume_bucket_sweep.py
- `run_volume_grid()` --calls--> `main()`  [INFERRED]
  aggression_carry/volume_backtest.py → scripts/run_volume_grid_splits.py
- `run_volume_alpha()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py
- `build_volume_features()` --calls--> `test_volume_alpha_isolated_daily_research_path()`  [INFERRED]
  aggression_carry/volume_alpha.py → tests/test_aggression_carry_volume_alpha.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.04
Nodes (88): download_archive_bytes(), download_public_trade_archive(), ArchiveManifestConfig, build_archive_trade_manifest(), _download_one_archive_kline(), _download_result(), _empty_download_results(), _empty_manifest() (+80 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (87): CostConfig, DailyCloseFadeConfig, DailyCloseFadeGridConfig, _apply_basket_stop_to_rows(), apply_close_fade_basket_stop(), _attach_archive_membership(), _attach_instrument_age(), backtest_daily_close_fade() (+79 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (86): VolumeBacktestConfig, VolumeGridConfig, generate_fixture_data(), _attribution_rows(), backtest_volume_trades(), _bar_at_close(), _bar_excursion(), _bar_exit_hits() (+78 more)

### Community 3 - "Community 3"
Cohesion: 0.07
Nodes (59): ForwardTestConfig, _as_utc(), _basket_already_opened(), _bool_value(), build_forward_scan_features(), build_forward_universe(), _concat(), _count_reason() (+51 more)

### Community 4 - "Community 4"
Cohesion: 0.09
Nodes (51): _as_utc(), build_demo_sync_orders(), _build_limit_order(), _build_probe_order(), cancel_stale_demo_orders(), _candidate_order_row(), _cap_candidate_order_rows(), _capped_order_qty() (+43 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (39): _as_utc(), _compact_token(), _demo_sync_compat_context(), _demo_sync_supports_entry_pause(), DemoCycleConfig, _existing_active_state(), _failed_sleeve_result(), format_demo_cycle_message() (+31 more)

### Community 6 - "Community 6"
Cohesion: 0.13
Nodes (32): BybitMarketData, ResearchConfig, DemoCancelAllConfig, DemoFlattenConfig, DemoProbeConfig, DemoSyncConfig, run_bybit_demo_sync(), _FakeExecution (+24 more)

### Community 7 - "Community 7"
Cohesion: 0.13
Nodes (36): _audit_has_telegram_signal(), _avg_fill_price(), build_forward_demo_audit_rows(), build_forward_demo_daily_summary(), _demo_realized_pnl(), _entry_slippage_bps(), _exit_slippage_bps(), _fill_status() (+28 more)

### Community 8 - "Community 8"
Cohesion: 0.14
Nodes (33): _add_forward_fade_args(), _add_forward_runtime_args(), _add_universe_backtest_args(), _apply_universe_backtest_args(), _backtest_config_from_args(), build_parser(), _close_fade_base_from_diagnostics_args(), _close_fade_base_from_grid_args() (+25 more)

### Community 9 - "Community 9"
Cohesion: 0.11
Nodes (33): build_volume_promotion_table(), _empty_promotion_table(), format_volume_promotion_report(), main(), _num(), _number(), parse_args(), _pct() (+25 more)

### Community 10 - "Community 10"
Cohesion: 0.15
Nodes (27): ArchiveKlineDownloadConfig, build_archive_pit_coverage(), _coverage_aggs(), _coverage_rates(), _coverage_status(), _coverage_thresholds_pass(), _csv_symbols(), _filtered_manifest() (+19 more)

### Community 11 - "Community 11"
Cohesion: 0.14
Nodes (20): summarize_close_fade_diagnostic_ic(), _ordinal_rank(), rank_correlation(), _add_cross_sectional_z(), _add_liquidity_rank(), attach_volume_forward_returns(), _best_base_portfolio(), build_volume_features() (+12 more)

### Community 12 - "Community 12"
Cohesion: 0.15
Nodes (21): ExchangeConfig, load_config(), _merge_daily_close_fade_config(), _merge_daily_close_fade_grid_config(), _merge_dataclass(), _merge_forward_test_config(), _merge_universe_config(), _merge_volume_alpha_config() (+13 more)

### Community 13 - "Community 13"
Cohesion: 0.17
Nodes (23): _base_config(), build_prior_daily_ema_regime(), build_prior_daily_ema_regime_from_klines(), _calendar_daily_returns(), _csv_float(), _csv_int(), _csv_str(), _date_range() (+15 more)

### Community 14 - "Community 14"
Cohesion: 0.15
Nodes (6): BybitDataError, BybitPrivateClient, _evaluate_grid_variant_worker(), _demo_cycle_lock(), _demo_executor(), RuntimeError

### Community 15 - "Community 15"
Cohesion: 0.28
Nodes (16): _base_config(), _csv_bool(), _csv_float(), _csv_int(), _csv_str(), _date_ms(), format_volume_grid_split_summary(), _grid_config() (+8 more)

### Community 16 - "Community 16"
Cohesion: 0.3
Nodes (14): _base_config(), _csv_float(), _csv_int(), _csv_signal_minutes(), _csv_str(), format_grid_split_summary(), _format_signal_minute(), _grid_config() (+6 more)

### Community 17 - "Community 17"
Cohesion: 0.29
Nodes (12): build_promotion_table(), _empty_promotion_table(), format_promotion_report(), _format_signal_minute(), main(), _num(), _number(), parse_args() (+4 more)

### Community 18 - "Community 18"
Cohesion: 0.32
Nodes (11): _artifact_key(), _artifact_path_list(), artifact_row(), build_research_manifest(), format_research_manifest(), _git(), git_metadata(), main() (+3 more)

### Community 19 - "Community 19"
Cohesion: 0.44
Nodes (9): evaluate_pit_coverage(), evaluate_promotion(), format_readiness_report(), main(), _missing_check(), _number(), overall_status(), parse_args() (+1 more)

### Community 21 - "Community 21"
Cohesion: 0.47
Nodes (4): _bar(), _result(), test_prior_daily_ema_regime_uses_previous_completed_daily_close(), test_regime_stability_prefers_split_survival_over_big_single_window()

### Community 22 - "Community 22"
Cohesion: 0.83
Nodes (3): _diagnostic(), _grid(), test_promotion_requires_raw_and_exit_split_survival()

### Community 23 - "Community 23"
Cohesion: 0.67
Nodes (2): _scenario(), test_split_summary_prefers_scenarios_that_survive_every_split()

### Community 24 - "Community 24"
Cohesion: 0.67
Nodes (2): test_volume_grid_split_summary_prefers_stable_variants(), _variant()

### Community 27 - "Community 27"
Cohesion: 1.0
Nodes (2): _candidate(), test_volume_promotion_requires_split_survival_and_drawdown()

### Community 28 - "Community 28"
Cohesion: 1.0
Nodes (2): test_grid_split_summary_prefers_stable_variants(), _variant()

## Knowledge Gaps
- **2 isolated node(s):** `ExchangeConfig`, `Bybit volume-alpha research package.  This package is the stripped-down rebuild`
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 23`** (4 nodes): `test_daily_close_fade_split_diagnostics_script.py`, `_scenario()`, `test_parse_splits_requires_ordered_windows()`, `test_split_summary_prefers_scenarios_that_survive_every_split()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 24`** (4 nodes): `test_volume_grid_splits_script.py`, `test_parse_splits_requires_ordered_windows()`, `test_volume_grid_split_summary_prefers_stable_variants()`, `_variant()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 27`** (3 nodes): `_candidate()`, `test_volume_promotion_script.py`, `test_volume_promotion_requires_split_survival_and_drawdown()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 28`** (3 nodes): `test_daily_close_fade_grid_splits_script.py`, `test_grid_split_summary_prefers_stable_variants()`, `_variant()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `main()` connect `Community 8` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 10`, `Community 11`, `Community 12`, `Community 14`?**
  _High betweenness centrality (0.112) - this node is a cross-community bridge._
- **Why does `load_config()` connect `Community 12` to `Community 6`, `Community 8`, `Community 9`, `Community 13`, `Community 14`, `Community 15`, `Community 16`?**
  _High betweenness centrality (0.077) - this node is a cross-community bridge._
- **Why does `read_dataset()` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 5`, `Community 6`, `Community 7`, `Community 10`, `Community 11`?**
  _High betweenness centrality (0.075) - this node is a cross-community bridge._
- **Are the 46 inferred relationships involving `read_dataset()` (e.g. with `run_volume_trade_backtest()` and `run_volume_grid()`) actually correct?**
  _`read_dataset()` has 46 INFERRED edges - model-reasoned connections that need verification._
- **Are the 32 inferred relationships involving `main()` (e.g. with `load_config()` and `generate_fixture_data()`) actually correct?**
  _`main()` has 32 INFERRED edges - model-reasoned connections that need verification._
- **Are the 43 inferred relationships involving `ResearchConfig` (e.g. with `DemoCycleConfig` and `DemoProbeConfig`) actually correct?**
  _`ResearchConfig` has 43 INFERRED edges - model-reasoned connections that need verification._
- **Are the 28 inferred relationships involving `write_dataset()` (e.g. with `generate_fixture_data()` and `_replace_dataset()`) actually correct?**
  _`write_dataset()` has 28 INFERRED edges - model-reasoned connections that need verification._