# Graph Report - liquidity-migration  (2026-07-17)

## Corpus Check
- 112 files · ~275,659 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 3069 nodes · 11365 edges · 26 communities detected
- Extraction: 51% EXTRACTED · 49% INFERRED · 0% AMBIGUOUS · INFERRED: 5596 edges (avg confidence: 0.58)
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
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]

## God Nodes (most connected - your core abstractions)
1. `Clock` - 214 edges
2. `SystemClock` - 213 edges
3. `AccountExecutionKernel` - 168 edges
4. `SleeveAdapterKind` - 153 edges
5. `AccountEventType` - 147 edges
6. `InstrumentRules` - 126 edges
7. `BybitMarketData` - 121 edges
8. `AccountIntentInbox` - 113 edges
9. `AccountRiskPolicy` - 108 edges
10. `RequestedIntent` - 97 edges

## Surprising Connections (you probably didn't know these)
- `Empirically verify small-order rules against Bybit's demo order endpoint.` --uses--> `InstrumentRules`  [INFERRED]
  liquidity_migration\demo_rule_probe.py → liquidity_migration\account_kernel.py
- `The venue response did not bind to the exact probe order identity.` --uses--> `InstrumentRules`  [INFERRED]
  liquidity_migration\demo_rule_probe.py → liquidity_migration\account_kernel.py
- `Validate one symbol's terminal, identity-bound no-fill probe evidence.      Re` --uses--> `InstrumentRules`  [INFERRED]
  liquidity_migration\demo_rule_probe.py → liquidity_migration\account_kernel.py
- `Find the smallest demo-accepted PostOnly notional for one symbol.      Structu` --uses--> `InstrumentRules`  [INFERRED]
  liquidity_migration\demo_rule_probe.py → liquidity_migration\account_kernel.py
- `Validate independent membership against klines without rewriting either.` --uses--> `SymbolIdentityError`  [INFERRED]
  liquidity_migration\binance_vision.py → liquidity_migration\symbol_codec.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (345): Shared, venue-mutation-free account rules and risk-policy loaders., BybitAccountExecutionConsumer, PendingTerminalStatus, PrivateExecutionStreamSupervisor, Normalize Bybit private execution/order streams into account-kernel facts., Subscribe a candidate without changing the currently published stream., Atomically publish a ready candidate and retire its exact predecessor., Block health on a dead private stream and rebuild it without auth storms. (+337 more)

### Community 1 - "Community 1"
Cohesion: 0.02
Nodes (275): _base_reasons(), build_candidate_universe_artifact(), build_profile_universe_tables(), build_profile_universe_tables_from_frames(), continuous_profile_universe_inputs(), _decision_rows(), enforce_frozen_candidate_frames(), enforce_frozen_candidate_population() (+267 more)

### Community 2 - "Community 2"
Cohesion: 0.02
Nodes (189): FrozenCandidateUniverse, AccountTargetPublisher, RequestedIntent, CanonicalReductionEvent, ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveManifestConfig, Config for ``build_archive_trade_manifest``.      The manifest is always built (+181 more)

### Community 3 - "Community 3"
Cohesion: 0.02
Nodes (179): _connected(), health_detail(), AccountOwnerLease, AccountOwnerLeaseAlreadyHeldError, acquire_inherited_account_owner_lease(), _add_prepared_receipt_arguments(), _build_cli_parser(), canonical_demo_account_lease_path() (+171 more)

### Community 4 - "Community 4"
Cohesion: 0.03
Nodes (132): HTMLParser, _adapt_request_targets(), _atomic_replace(), _filename(), _prepared_request_intents(), _superseding_requests(), _archive_cache_is_complete(), ArchiveDownloadIncompleteError (+124 more)

### Community 5 - "Community 5"
Cohesion: 0.02
Nodes (141): Validate independent membership against klines without rewriting either., validate_pit_manifest_coverage(), _cal_roll(), coerce_int(), date_boundary_ms(), date_ms(), _date_range(), _date_symbol_set() (+133 more)

### Community 6 - "Community 6"
Cohesion: 0.03
Nodes (143): target_reservation_rows(), component_source_paths(), ContinuousComponentSource, load_continuous_component_source(), Load continuous component artifacts from an explicit generated root., ContinuousEventConfig, _beta_window_joint_observation_count(), compute_hedge_decision_2f() (+135 more)

### Community 7 - "Community 7"
Cohesion: 0.04
Nodes (78): DemoAccountMutationLease, Held canonical lease capability required for every demo mutation., _account_topic(), api_key_allows_order_submit(), BybitPrivateClient, BybitPrivateWebSocketStream, _env_flag(), BybitDataError (+70 more)

### Community 8 - "Community 8"
Cohesion: 0.03
Nodes (123): calendar_roll(), calendar_shift(), A per-symbol positional ``value.shift(periods).over("symbol")`` that is NULL unl, Calendar-aware rolling window over an integer timestamp grid.      Row-based `, _additive_equity(), _additive_summary(), _assert_rmom_covers_window(), _btc_trend_returns() (+115 more)

### Community 9 - "Community 9"
Cohesion: 0.04
Nodes (106): require_profile_binding(), _cmd_long_native_event_demo_cycle(), exact_duration_ms(), Return an exact integer millisecond duration.      Use this for timestamp look, _apply_btc_risk_sizing(), _btc_risk_policy(), _btc_risk_sizing_payload_fields(), _btc_rows_from_klines() (+98 more)

### Community 10 - "Community 10"
Cohesion: 0.04
Nodes (107): _assert_download_completeness(), build_binance_oos(), _days_between(), discover(), _download_daily_tail_to_staging(), fetch_daily_klines(), _fetch_expected_sha256(), fetch_month_klines() (+99 more)

### Community 11 - "Community 11"
Cohesion: 0.04
Nodes (78): BinanceDataError, BinanceUSDMData, _ceil_to_period(), _floor_to_period(), _http_error_detail(), _raise_if_suspicious_empty_page(), Seconds to wait after a 429/418, from the Retry-After header.          Falls b, Best-effort detail string from an HTTPError body (the Binance error JSON). (+70 more)

### Community 12 - "Community 12"
Cohesion: 0.05
Nodes (68): account_journal_lock_path(), account_journal_path(), account_transactions_path(), AccountEventSpec, AccountJournal, AccountJournalIntegrityError, AccountKernelError, _aggregate_target_quantities() (+60 more)

### Community 13 - "Community 13"
Cohesion: 0.05
Nodes (54): account_owner_health_path(), _atomic_replace(), format_convergence_health(), from_dict(), Durable liveness evidence for a single account-execution owner.  The health pr, Atomically publish the latest owner observation under ``root``., Read and strictly validate the latest durable owner observation., Require fresh health bound to the verified canonical journal head. (+46 more)

### Community 14 - "Community 14"
Cohesion: 0.06
Nodes (72): read_dataset(), _atomic_private_replace(), from_dict(), Durable completion evidence for one strategy-producer service generation.  Str, Atomically publish the latest fully completed producer cycle., Read and strictly validate one private completion projection., Latest fully evidenced cycle completed by one systemd invocation., Replace one private projection without exposing a torn target. (+64 more)

### Community 15 - "Community 15"
Cohesion: 0.08
Nodes (47): continuous_source_decile_panel(), cross_sectional_decile(), canonical_payload(), decision_funnel_source_key(), finalize_funnel_row(), FunnelJsonlWriter, gate_state(), _immutable_source_material() (+39 more)

### Community 16 - "Community 16"
Cohesion: 0.08
Nodes (44): account_route_manifest_path(), AccountRouteConfigurationError, AccountRouteCutoverRequiredError, AccountRouteError, AccountRouteIntegrityError, AccountRouteMismatchError, AccountRouteMissingError, _atomic_create() (+36 more)

### Community 17 - "Community 17"
Cohesion: 0.11
Nodes (45): _add_refresh_arguments(), _artifact_files(), _as_mapping(), _backtest_step(), _binance_ancillary_step(), _binance_tail_steps(), build_parser(), _bybit_ancillary_step() (+37 more)

### Community 18 - "Community 18"
Cohesion: 0.1
Nodes (31): _command_id_for_row(), _float(), _rows(), _terminal(), _terminal_status(), _timestamp_ns(), bybit_private_execution_metadata(), active() (+23 more)

### Community 19 - "Community 19"
Cohesion: 0.08
Nodes (25): btc_context_by_day(), _daily_closes_by_day(), _equal_optional_float(), ExpandingBtcRiskState, _finite(), _fsync_directory(), _fsync_file(), _hash_payload() (+17 more)

### Community 20 - "Community 20"
Cohesion: 0.14
Nodes (35): capture_record_id(), _book_metrics(), build_execution_diagnostics(), build_trade_diagnostic_manifest(), _depth(), _event_metadata(), _fee_summary(), _finite() (+27 more)

### Community 21 - "Community 21"
Cohesion: 0.16
Nodes (34): AccountEpochClearResult, _apply_missing_root(), _apply_plan(), _build_plan(), clear_account_epoch_roots_preserving_locks(), _Directory, _directory_open_flags(), _entry_metadata() (+26 more)

### Community 22 - "Community 22"
Cohesion: 0.11
Nodes (28): AccountEntryRecord, AccountEvidence, _backtest_row(), BacktestEntryRecord, _command_id(), _commit_candidates(), compare_three_way_entries(), EntryKey (+20 more)

### Community 23 - "Community 23"
Cohesion: 0.15
Nodes (24): _active_stop(), _batch_target_proposals(), _clear_recent_entry_rejection_counters(), _component_attribution_pending(), _entry_attempt_key(), _entry_rejection_summary(), _entry_risk_decision_messages(), _entry_scope() (+16 more)

### Community 24 - "Community 24"
Cohesion: 0.21
Nodes (16): build_parser(), build_report(), compare_locked_versions(), dependency_lock_report(), format_human(), _git_output(), git_report(), graphify_report() (+8 more)

### Community 25 - "Community 25"
Cohesion: 1.0
Nodes (1): Build identity from the authenticated ``query-api`` response.          ``userI

## Knowledge Gaps
- **277 isolated node(s):** `_Removal`, `_PreservedLock`, `In-place account epoch clearing that preserves persistent mutex inodes.`, `Return Linux's mount identity, or ``None`` where fdinfo is unavailable by design`, `Open every absolute component with openat/O_NOFOLLOW and bind each identity.` (+272 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 25`** (1 nodes): `Build identity from the authenticated ``query-api`` response.          ``userI`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SystemClock` connect `Community 0` to `Community 1`, `Community 2`, `Community 12`, `Community 13`, `Community 16`?**
  _High betweenness centrality (0.044) - this node is a cross-community bridge._
- **Why does `run_venue()` connect `Community 6` to `Community 1`?**
  _High betweenness centrality (0.035) - this node is a cross-community bridge._
- **Why does `Clock` connect `Community 0` to `Community 1`, `Community 2`, `Community 12`, `Community 13`?**
  _High betweenness centrality (0.034) - this node is a cross-community bridge._
- **Are the 349 inferred relationships involving `ValueError` (e.g. with `_symbol()` and `_symbol_type()`) actually correct?**
  _`ValueError` has 349 INFERRED edges - model-reasoned connections that need verification._
- **Are the 221 inferred relationships involving `RuntimeError` (e.g. with `enforce_frozen_candidate_population()` and `require_profile_binding()`) actually correct?**
  _`RuntimeError` has 221 INFERRED edges - model-reasoned connections that need verification._
- **Are the 209 inferred relationships involving `Clock` (e.g. with `PendingTerminalStatus` and `BybitAccountExecutionConsumer`) actually correct?**
  _`Clock` has 209 INFERRED edges - model-reasoned connections that need verification._
- **Are the 209 inferred relationships involving `SystemClock` (e.g. with `PendingTerminalStatus` and `BybitAccountExecutionConsumer`) actually correct?**
  _`SystemClock` has 209 INFERRED edges - model-reasoned connections that need verification._