# Graph Report - .  (2026-07-16)

## Corpus Check
- 116 files · ~195,199 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 2497 nodes · 9008 edges · 34 communities detected
- Extraction: 48% EXTRACTED · 52% INFERRED · 0% AMBIGUOUS · INFERRED: 4656 edges (avg confidence: 0.56)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Strategy Target Producers|Strategy Target Producers]]
- [[_COMMUNITY_Account Kernel Domain|Account Kernel Domain]]
- [[_COMMUNITY_Execution Models|Execution Models]]
- [[_COMMUNITY_Continuous Hedge Analytics|Continuous Hedge Analytics]]
- [[_COMMUNITY_Account Service and Storage|Account Service and Storage]]
- [[_COMMUNITY_Bybit Private Boundary|Bybit Private Boundary]]
- [[_COMMUNITY_Active Strategy Cycles|Active Strategy Cycles]]
- [[_COMMUNITY_Data Download and CLI|Data Download and CLI]]
- [[_COMMUNITY_Journal Serialization|Journal Serialization]]
- [[_COMMUNITY_Artifact and Candidate Integrity|Artifact and Candidate Integrity]]
- [[_COMMUNITY_Market Capture and Owner Health|Market Capture and Owner Health]]
- [[_COMMUNITY_Archive Ingestion|Archive Ingestion]]
- [[_COMMUNITY_Strategy State Protection|Strategy State Protection]]
- [[_COMMUNITY_Account Route Integrity|Account Route Integrity]]
- [[_COMMUNITY_Liveness and Alerts|Liveness and Alerts]]
- [[_COMMUNITY_BTC Risk Overlay|BTC Risk Overlay]]
- [[_COMMUNITY_Operations and Governance|Operations and Governance]]
- [[_COMMUNITY_Account Notifications|Account Notifications]]
- [[_COMMUNITY_Residual Momentum|Residual Momentum]]
- [[_COMMUNITY_Target Scheduling Capture|Target Scheduling Capture]]
- [[_COMMUNITY_Strategy Outcome Tape|Strategy Outcome Tape]]
- [[_COMMUNITY_Equity Curve Reporting|Equity Curve Reporting]]
- [[_COMMUNITY_Private Execution Stream|Private Execution Stream]]
- [[_COMMUNITY_Package Root|Package Root]]
- [[_COMMUNITY_PIT Archive Contracts|PIT Archive Contracts]]
- [[_COMMUNITY_Mainnet Safety|Mainnet Safety]]
- [[_COMMUNITY_Demo Account Identity|Demo Account Identity]]
- [[_COMMUNITY_Recent PIT Limitation|Recent PIT Limitation]]
- [[_COMMUNITY_Repository Overview|Repository Overview]]
- [[_COMMUNITY_Validation Workflow|Validation Workflow]]
- [[_COMMUNITY_Spent Data Governance|Spent Data Governance]]
- [[_COMMUNITY_Signal Membership|Signal Membership]]
- [[_COMMUNITY_Transaction Storage|Transaction Storage]]
- [[_COMMUNITY_Hash Chain Integrity|Hash Chain Integrity]]

## God Nodes (most connected - your core abstractions)
1. `Clock` - 200 edges
2. `SystemClock` - 199 edges
3. `AccountExecutionKernel` - 155 edges
4. `SleeveAdapterKind` - 150 edges
5. `InstrumentRules` - 123 edges
6. `AccountEventType` - 119 edges
7. `BybitMarketData` - 118 edges
8. `AccountIntentInbox` - 113 edges
9. `AccountRiskPolicy` - 106 edges
10. `RequestedIntent` - 96 edges

## Surprising Connections (you probably didn't know these)
- `Anchor a default data root at the repo dir (NOT the CWD).      A manual/cron inv` --uses--> `AccountEventType`  [INFERRED]
  scripts/check_demo_liveness.py → liquidity_migration/account_kernel.py
- `Read a sleeve toggle, failing safe to the supplied default.` --uses--> `AccountEventType`  [INFERRED]
  scripts/check_demo_liveness.py → liquidity_migration/account_kernel.py
- `Match the deploy predicate: either CONTINUOUS sleeve needs RMOM refresh.` --uses--> `AccountEventType`  [INFERRED]
  scripts/check_demo_liveness.py → liquidity_migration/account_kernel.py
- `No cycle written within the freshness window -> the daemon is down/hung.` --uses--> `AccountEventType`  [INFERRED]
  scripts/check_demo_liveness.py → liquidity_migration/account_kernel.py
- `Alert on failed services and debounce inactive timers for one interval.      Cyc` --uses--> `AccountEventType`  [INFERRED]
  scripts/check_demo_liveness.py → liquidity_migration/account_kernel.py

## Hyperedges (group relationships)
- **Claim-Scoped Research Integrity** — governance_evidence_axes, governance_hard_validity, prereg_minimum_contract, failure_research_taxonomy, pit_membership_gate [EXTRACTED 1.00]
- **Operational Cutover Flow** — state_operational_cutover, systemd_deployment_lifecycle, operations_staged_deployment, operations_operational_authority, account_owner_topology [EXTRACTED 1.00]
- **Commit-Owned Runtime Model Boundaries** — systemd_hedge_prior_gate, account_hedge_model_prior, account_paper_integration_twin, research_uncalibrated_paper_evidence [INFERRED 0.90]

## Communities

### Community 0 - "Strategy Target Producers"
Cohesion: 0.02
Nodes (196): FrozenCandidateUniverse, AccountTargetPublisher, RequestedIntent, CanonicalReductionEvent, ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveManifestConfig, Config for ``build_archive_trade_manifest``.      The manifest is always built f (+188 more)

### Community 1 - "Account Kernel Domain"
Cohesion: 0.04
Nodes (253): require_registered_demo_rule_max_age_hours(), BybitAccountExecutionConsumer, PendingTerminalStatus, Normalize Bybit private execution/order streams into account-kernel facts., Resolve Bybit rows by either client id or the venue's durable order id.      Exc, One consumer thread owns all private-stream mutation of the kernel., ExitFirstPublication, publish_exit_first_target_requests() (+245 more)

### Community 2 - "Execution Models"
Cohesion: 0.02
Nodes (266): AccountRiskPolicy, SleeveAdapterKind, _cal_roll(), calendar_roll(), calendar_shift(), coerce_int(), date_boundary_ms(), date_ms() (+258 more)

### Community 3 - "Continuous Hedge Analytics"
Cohesion: 0.02
Nodes (177): component_target_key(), requested_target(), target_reservation_rows(), _float_or_nan(), _parse_day(), component_source_paths(), ContinuousComponentSource, load_continuous_component_source() (+169 more)

### Community 4 - "Account Service and Storage"
Cohesion: 0.02
Nodes (113): load_demo_rules(), load_demo_rules_bytes(), _load_json_bytes(), load_risk_policy(), load_risk_policy_bytes(), Shared, venue-mutation-free account rules and risk-policy loaders., _canonical_funding_events(), _finite_or_zero() (+105 more)

### Community 5 - "Bybit Private Boundary"
Cohesion: 0.03
Nodes (81): AccountOwnerLease, canonical_demo_account_lease_path(), _canonical_demo_lease_directory(), _credential_fingerprint(), DemoAccountMutationLease, from_api_key_info(), Process leases for account owners and Bybit demo mutation authority., Return the fixed host-global namespace; there is no environment override. (+73 more)

### Community 6 - "Active Strategy Cycles"
Cohesion: 0.03
Nodes (108): require_profile_binding(), account_target_request_id(), completed_expired_entry_attempt_keys(), unresolved_target_snapshot(), canonical_entry_attempts(), terminal_entry_attempt_keys(), _cmd_long_native_event_demo_cycle(), exact_duration_ms() (+100 more)

### Community 7 - "Data Download and CLI"
Cohesion: 0.03
Nodes (100): BinanceDataError, BinanceUSDMData, _ceil_to_period(), _floor_to_period(), _http_error_detail(), _raise_if_suspicious_empty_page(), Seconds to wait after a 429/418, from the Retry-After header.          Falls bac, Best-effort detail string from an HTTPError body (the Binance error JSON). (+92 more)

### Community 8 - "Journal Serialization"
Cohesion: 0.04
Nodes (82): account_journal_lock_path(), account_journal_path(), account_transactions_path(), AccountEventSpec, AccountJournal, AccountJournalIntegrityError, AccountKernelError, _aggregate_target_quantities() (+74 more)

### Community 9 - "Artifact and Candidate Integrity"
Cohesion: 0.04
Nodes (100): _base_reasons(), build_candidate_universe_artifact(), build_profile_universe_tables(), build_profile_universe_tables_from_frames(), continuous_profile_universe_inputs(), _decision_rows(), enforce_frozen_candidate_frames(), enforce_frozen_candidate_population() (+92 more)

### Community 10 - "Market Capture and Owner Health"
Cohesion: 0.04
Nodes (61): account_owner_health_path(), _atomic_replace(), format_convergence_health(), from_dict(), Durable liveness evidence for a single account-execution owner.  The health proj, Atomically publish the latest owner observation under ``root``., Read and strictly validate the latest durable owner observation., Require fresh health bound to the verified canonical journal head. (+53 more)

### Community 11 - "Archive Ingestion"
Cohesion: 0.05
Nodes (75): HTMLParser, _archive_cache_is_complete(), ArchiveDownloadIncompleteError, ArchiveFileNotFoundError, _content_length(), download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive() (+67 more)

### Community 12 - "Strategy State Protection"
Cohesion: 0.08
Nodes (50): _accepted_batches(), _batch_fill_summary(), _BatchFillSummary, canonical_adverse_reduction_events(), canonical_component_execution_anchors(), canonical_reduction_events(), _canonical_reduction_events_from_events(), canonical_strategy_trade_rows() (+42 more)

### Community 13 - "Account Route Integrity"
Cohesion: 0.07
Nodes (43): account_route_manifest_path(), AccountRouteConfigurationError, AccountRouteCutoverRequiredError, AccountRouteError, AccountRouteIntegrityError, AccountRouteMismatchError, AccountRouteMissingError, _atomic_create() (+35 more)

### Community 14 - "Liveness and Alerts"
Cohesion: 0.08
Nodes (47): _rate_limit_retry_seconds(), Seconds to wait before the single 429 retry, or None when the error is     not a, send_telegram_message(), TelegramConfig, Alert, build_arg_parser(), _continuous_rmom_refresh_on(), _default_root() (+39 more)

### Community 15 - "BTC Risk Overlay"
Cohesion: 0.08
Nodes (25): btc_context_by_day(), _daily_closes_by_day(), _equal_optional_float(), ExpandingBtcRiskState, _finite(), _fsync_directory(), _fsync_file(), _hash_payload() (+17 more)

### Community 16 - "Operations and Governance"
Cohesion: 0.06
Nodes (41): Immutable Historical Hedge Model Prior, Sole Account-Owner Topology, integration_only_uncalibrated Paper Twin, continuous_ensemble_v2, LongV11aDivWeekendVol, Repository Source-Authority Hierarchy, Operating Constitution, Purpose-Based Repository Navigation (+33 more)

### Community 17 - "Account Notifications"
Cohesion: 0.15
Nodes (25): AccountNotificationState, _active_stop(), _batch_target_proposals(), _clear_recent_entry_rejection_counters(), _component_attribution_pending(), _entry_attempt_key(), _entry_rejection_summary(), _entry_risk_decision_messages() (+17 more)

### Community 18 - "Residual Momentum"
Cohesion: 0.14
Nodes (21): compute_btc_beta(), fit_factor_returns(), Causal factor panel and residual-return estimation for RMOM refresh., Per-day cross-sectional OLS of realized return on factor exposures.      For eac, Rolling-window OLS beta of each symbol's daily return on BTC's daily return., _append_signal(), _append_trailing_pad(), _assert_append_overlap_matches() (+13 more)

### Community 19 - "Target Scheduling Capture"
Cohesion: 0.2
Nodes (15): _append_private_line(), capture_event_from_cycle(), capture_event_id(), _capture_hash(), _capture_one_request(), CapturedTargetRequest, _decision_keys_from_requests(), from_dict() (+7 more)

### Community 20 - "Strategy Outcome Tape"
Cohesion: 0.19
Nodes (12): _append_private_line(), _decision_keys(), _event_id(), from_dict(), load_strategy_event_decision_tape(), load_strategy_event_decision_tape_bytes(), _next_tape_hash(), Hash-chained strategy decision outcomes keyed to durable input events.  The stra (+4 more)

### Community 21 - "Equity Curve Reporting"
Cohesion: 0.23
Nodes (15): _continuous_payload_from_summary(), _delisted_traded(), _find_png(), _headline(), _infer_venue_from_root(), _label(), main(), _pit_verdict() (+7 more)

### Community 22 - "Private Execution Stream"
Cohesion: 0.38
Nodes (6): _command_id_for_row(), _float(), _rows(), _terminal(), _terminal_status(), _timestamp_ns()

### Community 23 - "Package Root"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

### Community 24 - "PIT Archive Contracts"
Cohesion: 1.0
Nodes (2): Research Full-PIT Roots, Archive Trade Manifest Contract

### Community 25 - "Mainnet Safety"
Cohesion: 1.0
Nodes (2): Runtime Safety Boundary, Mainnet Readiness Boundary

### Community 26 - "Demo Account Identity"
Cohesion: 1.0
Nodes (1): Build identity from the authenticated ``query-api`` response.          ``userID`

### Community 27 - "Recent PIT Limitation"
Cohesion: 1.0
Nodes (1): The manifest cannot PIT-validate the most recent daily-close signal.

### Community 28 - "Repository Overview"
Cohesion: 1.0
Nodes (1): Liquidity Migration Repository

### Community 29 - "Validation Workflow"
Cohesion: 1.0
Nodes (1): Repository Validation Commands

### Community 30 - "Spent Data Governance"
Cohesion: 1.0
Nodes (1): Post-Exposure Spent-Data Rule

### Community 31 - "Signal Membership"
Cohesion: 1.0
Nodes (1): Daily Signal Membership Convention

### Community 32 - "Transaction Storage"
Cohesion: 1.0
Nodes (1): Atomic Transaction Storage and JSONL Reset Boundary

### Community 33 - "Hash Chain Integrity"
Cohesion: 1.0
Nodes (1): Hash-Chained Event and State Integrity

## Knowledge Gaps
- **263 isolated node(s):** `Shared Bybit transport errors and response classification.  This module is delib`, `The venue returned a definite negative response; no mutation was accepted.`, `A state-changing request may have reached the venue, but its response was lost.`, `Classify Bybit rate-limit payloads without scanning unrelated fields.`, `Causal BTC-risk entry-size overlay for the continuous demo book.  ``CTRL_BTC_RIS` (+258 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Package Root`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `PIT Archive Contracts`** (2 nodes): `Research Full-PIT Roots`, `Archive Trade Manifest Contract`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Mainnet Safety`** (2 nodes): `Runtime Safety Boundary`, `Mainnet Readiness Boundary`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Demo Account Identity`** (1 nodes): `Build identity from the authenticated ``query-api`` response.          ``userID``
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Recent PIT Limitation`** (1 nodes): `The manifest cannot PIT-validate the most recent daily-close signal.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Repository Overview`** (1 nodes): `Liquidity Migration Repository`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Validation Workflow`** (1 nodes): `Repository Validation Commands`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Spent Data Governance`** (1 nodes): `Post-Exposure Spent-Data Rule`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Signal Membership`** (1 nodes): `Daily Signal Membership Convention`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Transaction Storage`** (1 nodes): `Atomic Transaction Storage and JSONL Reset Boundary`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Hash Chain Integrity`** (1 nodes): `Hash-Chained Event and State Integrity`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Clock` connect `Account Kernel Domain` to `Strategy Target Producers`, `Execution Models`, `Journal Serialization`, `Market Capture and Owner Health`, `Account Notifications`?**
  _High betweenness centrality (0.078) - this node is a cross-community bridge._
- **Why does `SystemClock` connect `Account Kernel Domain` to `Strategy Target Producers`, `Execution Models`, `Journal Serialization`, `Market Capture and Owner Health`, `Account Route Integrity`, `Account Notifications`?**
  _High betweenness centrality (0.070) - this node is a cross-community bridge._
- **Why does `AccountEventType` connect `Account Kernel Domain` to `Strategy Target Producers`, `Execution Models`, `Journal Serialization`, `Strategy State Protection`, `Liveness and Alerts`, `Account Notifications`?**
  _High betweenness centrality (0.047) - this node is a cross-community bridge._
- **Are the 195 inferred relationships involving `Clock` (e.g. with `LongNativeDemoDaemon` and `Long-running strategy/target producer for the v11a sleeve.  Demo and paper route`) actually correct?**
  _`Clock` has 195 INFERRED edges - model-reasoned connections that need verification._
- **Are the 195 inferred relationships involving `SystemClock` (e.g. with `LongNativeDemoDaemon` and `Long-running strategy/target producer for the v11a sleeve.  Demo and paper route`) actually correct?**
  _`SystemClock` has 195 INFERRED edges - model-reasoned connections that need verification._
- **Are the 136 inferred relationships involving `AccountExecutionKernel` (e.g. with `FixedCapitalSnapshotProvider` and `Run the paper integration owner with an explicitly uncalibrated execution twin.`) actually correct?**
  _`AccountExecutionKernel` has 136 INFERRED edges - model-reasoned connections that need verification._
- **Are the 144 inferred relationships involving `SleeveAdapterKind` (e.g. with `LongNativeDemoCycleConfig` and `LONG strategy target producer - forward counterpart to long_native research.  Mi`) actually correct?**
  _`SleeveAdapterKind` has 144 INFERRED edges - model-reasoned connections that need verification._