# Graph Report - liquidity-migration  (2026-07-18)

## Corpus Check
- 113 files · ~287,298 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 3114 nodes · 11512 edges · 26 communities detected
- Extraction: 51% EXTRACTED · 49% INFERRED · 0% AMBIGUOUS · INFERRED: 5648 edges (avg confidence: 0.58)
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
4. `SleeveAdapterKind` - 154 edges
5. `AccountEventType` - 148 edges
6. `InstrumentRules` - 126 edges
7. `BybitMarketData` - 121 edges
8. `AccountIntentInbox` - 113 edges
9. `AccountRiskPolicy` - 110 edges
10. `RequestedIntent` - 97 edges

## Surprising Connections (you probably didn't know these)
- `InstrumentRules` --uses--> `Empirically verify small-order rules against Bybit's demo order endpoint.`  [INFERRED]
  liquidity_migration\account_kernel.py → liquidity_migration\demo_rule_probe.py
- `InstrumentRules` --uses--> `The venue response did not bind to the exact probe order identity.`  [INFERRED]
  liquidity_migration\account_kernel.py → liquidity_migration\demo_rule_probe.py
- `InstrumentRules` --uses--> `Validate one symbol's terminal, identity-bound no-fill probe evidence.      Re`  [INFERRED]
  liquidity_migration\account_kernel.py → liquidity_migration\demo_rule_probe.py
- `InstrumentRules` --uses--> `Find the smallest demo-accepted PostOnly notional for one symbol.      Structu`  [INFERRED]
  liquidity_migration\account_kernel.py → liquidity_migration\demo_rule_probe.py
- `Source-reopening receipt for the demo/paper account epoch reset.  The shell re` --uses--> `StableFileSnapshot`  [INFERRED]
  liquidity_migration\account_reset_receipt.py → liquidity_migration\artifact_snapshot.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.02
Nodes (226): FrozenCandidateUniverse, AccountTargetPublisher, RequestedIntent, CanonicalReductionEvent, ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveManifestConfig, Config for ``build_archive_trade_manifest``.      The manifest is always built (+218 more)

### Community 1 - "Community 1"
Cohesion: 0.04
Nodes (252): PendingTerminalStatus, Normalize Bybit private execution/order streams into account-kernel facts., Subscribe a candidate without changing the currently published stream., Atomically publish a ready candidate and retire its exact predecessor., Block health on a dead private stream and rebuild it without auth storms., Resolve Bybit rows by either client id or the venue's durable order id.      E, One consumer thread owns all private-stream mutation of the kernel., account_target_request_id() (+244 more)

### Community 2 - "Community 2"
Cohesion: 0.02
Nodes (193): _archive_bundle(), _BoundedForwardReader, build_account_reset_receipt(), _delete_created_receipt(), _directory_open_flags(), _entry_metadata(), _entry_mount_id(), _file_identity() (+185 more)

### Community 3 - "Community 3"
Cohesion: 0.03
Nodes (123): load_demo_rules(), load_demo_rules_bytes(), _load_json_bytes(), load_risk_policy(), load_risk_policy_bytes(), Shared, venue-mutation-free account rules and risk-policy loaders., require_registered_demo_rule_max_age_hours(), BybitAccountExecutionConsumer (+115 more)

### Community 4 - "Community 4"
Cohesion: 0.03
Nodes (166): AccountOwnerLeaseAlreadyHeldError, acquire_inherited_account_owner_lease(), _add_prepared_receipt_arguments(), _build_cli_parser(), canonical_demo_account_lease_path(), _canonical_demo_expected_owner(), _canonical_demo_lease_directory(), _credential_fingerprint() (+158 more)

### Community 5 - "Community 5"
Cohesion: 0.02
Nodes (162): _base_reasons(), build_candidate_universe_artifact(), build_profile_universe_tables(), build_profile_universe_tables_from_frames(), continuous_profile_universe_inputs(), _decision_rows(), enforce_frozen_candidate_frames(), enforce_frozen_candidate_population() (+154 more)

### Community 6 - "Community 6"
Cohesion: 0.04
Nodes (163): AccountRiskPolicy, SleeveAdapterKind, _cal_roll(), calendar_roll(), calendar_shift(), A per-symbol positional ``value.shift(periods).over("symbol")`` that is NULL unl, Calendar-aware rolling window over an integer timestamp grid.      Row-based `, BAC-1: calendar-aware rolling window over a per-symbol (or market) daily ts_ms g (+155 more)

### Community 7 - "Community 7"
Cohesion: 0.03
Nodes (131): HTMLParser, _atomic_replace(), _filename(), from_dict(), _superseding_requests(), _archive_cache_is_complete(), ArchiveDownloadIncompleteError, ArchiveFileNotFoundError (+123 more)

### Community 8 - "Community 8"
Cohesion: 0.03
Nodes (145): component_target_key(), target_reservation_rows(), component_source_paths(), ContinuousComponentSource, load_continuous_component_source(), Load continuous component artifacts from an explicit generated root., _beta_window_joint_observation_count(), compute_hedge_decision_2f() (+137 more)

### Community 9 - "Community 9"
Cohesion: 0.03
Nodes (136): Validate independent membership against klines without rewriting either., validate_pit_manifest_coverage(), coerce_int(), date_boundary_ms(), date_ms(), _date_range(), _date_symbol_set(), _exclude_symbols() (+128 more)

### Community 10 - "Community 10"
Cohesion: 0.04
Nodes (94): account_journal_lock_path(), account_journal_path(), account_transactions_path(), AccountEventSpec, AccountJournal, AccountJournalIntegrityError, AccountKernelError, _aggregate_target_quantities() (+86 more)

### Community 11 - "Community 11"
Cohesion: 0.04
Nodes (70): DemoAccountMutationLease, Held canonical lease capability required for every demo mutation., api_key_allows_order_submit(), BybitPrivateClient, _env_flag(), BybitDataError, BybitRequestRejected, BybitSubmissionUncertain (+62 more)

### Community 12 - "Community 12"
Cohesion: 0.04
Nodes (107): _assert_download_completeness(), build_binance_oos(), _days_between(), discover(), _download_daily_tail_to_staging(), fetch_daily_klines(), _fetch_expected_sha256(), fetch_month_klines() (+99 more)

### Community 13 - "Community 13"
Cohesion: 0.05
Nodes (89): exact_duration_ms(), Return an exact integer millisecond duration.      Use this for timestamp look, _round_trip_bps(), canonical_payload(), decision_funnel_source_key(), finalize_funnel_row(), FunnelJsonlWriter, gate_state() (+81 more)

### Community 14 - "Community 14"
Cohesion: 0.06
Nodes (69): AccountEntryRecord, AccountEvidence, _backtest_row(), BacktestEntryRecord, _command_id(), _commit_candidates(), compare_three_way_entries(), EntryKey (+61 more)

### Community 15 - "Community 15"
Cohesion: 0.06
Nodes (72): read_dataset(), _atomic_private_replace(), from_dict(), Durable completion evidence for one strategy-producer service generation.  Str, Atomically publish the latest fully completed producer cycle., Read and strictly validate one private completion projection., Latest fully evidenced cycle completed by one systemd invocation., Replace one private projection without exposing a torn target. (+64 more)

### Community 16 - "Community 16"
Cohesion: 0.07
Nodes (54): completed_expired_entry_attempt_keys(), _accepted_batches(), _batch_fill_summary(), canonical_adverse_reduction_events(), canonical_component_execution_anchors(), canonical_entry_attempts(), canonical_reduction_events(), _canonical_reduction_events_from_events() (+46 more)

### Community 17 - "Community 17"
Cohesion: 0.08
Nodes (42): account_route_manifest_path(), AccountRouteConfigurationError, AccountRouteCutoverRequiredError, AccountRouteError, AccountRouteIntegrityError, AccountRouteMismatchError, AccountRouteMissingError, _atomic_create() (+34 more)

### Community 18 - "Community 18"
Cohesion: 0.06
Nodes (20): _Bar, _empty_klines_frame(), KlineStore, _parse_ws_kline_event(), In-memory 1h kline store for the WS-driven kline-delivery path.  The cross-sec, One 1h bar. Stored per-symbol keyed by ``ts_ms`` in the store's dict., Parse a single bar dict from pybit's WS kline payload.      pybit forwards the, Thread-safe in-memory 1h klines per symbol with periodic disk flush.      Appe (+12 more)

### Community 19 - "Community 19"
Cohesion: 0.08
Nodes (25): btc_context_by_day(), _daily_closes_by_day(), _equal_optional_float(), ExpandingBtcRiskState, _finite(), _fsync_directory(), _fsync_file(), _hash_payload() (+17 more)

### Community 20 - "Community 20"
Cohesion: 0.12
Nodes (31): Return one canonical non-zero systemd invocation identifier., validate_systemd_invocation_id(), _absolute_directory(), AccountOwnerReadiness, latest_capture_receive_ts_ns(), latest_market_readiness(), latest_market_receive_ts_ns(), main() (+23 more)

### Community 21 - "Community 21"
Cohesion: 0.16
Nodes (34): AccountEpochClearResult, _apply_missing_root(), _apply_plan(), _build_plan(), clear_account_epoch_roots_preserving_locks(), _Directory, _directory_open_flags(), _entry_metadata() (+26 more)

### Community 22 - "Community 22"
Cohesion: 0.15
Nodes (24): _active_stop(), _batch_target_proposals(), _clear_recent_entry_rejection_counters(), _component_attribution_pending(), _entry_attempt_key(), _entry_rejection_summary(), _entry_risk_decision_messages(), _entry_scope() (+16 more)

### Community 23 - "Community 23"
Cohesion: 0.19
Nodes (18): _append_signal(), _append_trailing_pad(), _assert_append_overlap_matches(), _compute_signal(), _default_end(), main(), _ms_to_date_str(), precompute() (+10 more)

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

- **Why does `SystemClock` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 6`, `Community 10`, `Community 20`?**
  _High betweenness centrality (0.043) - this node is a cross-community bridge._
- **Why does `SleeveAdapterKind` connect `Community 6` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 5`, `Community 7`, `Community 8`, `Community 16`?**
  _High betweenness centrality (0.034) - this node is a cross-community bridge._
- **Why does `Clock` connect `Community 1` to `Community 0`, `Community 3`, `Community 6`, `Community 10`, `Community 20`?**
  _High betweenness centrality (0.034) - this node is a cross-community bridge._
- **Are the 350 inferred relationships involving `ValueError` (e.g. with `_symbol()` and `_symbol_type()`) actually correct?**
  _`ValueError` has 350 INFERRED edges - model-reasoned connections that need verification._
- **Are the 231 inferred relationships involving `RuntimeError` (e.g. with `enforce_frozen_candidate_population()` and `require_profile_binding()`) actually correct?**
  _`RuntimeError` has 231 INFERRED edges - model-reasoned connections that need verification._
- **Are the 209 inferred relationships involving `Clock` (e.g. with `PendingTerminalStatus` and `BybitAccountExecutionConsumer`) actually correct?**
  _`Clock` has 209 INFERRED edges - model-reasoned connections that need verification._
- **Are the 209 inferred relationships involving `SystemClock` (e.g. with `PendingTerminalStatus` and `BybitAccountExecutionConsumer`) actually correct?**
  _`SystemClock` has 209 INFERRED edges - model-reasoned connections that need verification._