# Graph Report - liquidity-migration  (2026-07-19)

## Corpus Check
- 257 files · ~0 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 6620 nodes · 24859 edges · 79 communities detected
- Extraction: 46% EXTRACTED · 54% INFERRED · 0% AMBIGUOUS · INFERRED: 13368 edges (avg confidence: 0.61)
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
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 66|Community 66]]
- [[_COMMUNITY_Community 67|Community 67]]
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 69|Community 69]]
- [[_COMMUNITY_Community 70|Community 70]]
- [[_COMMUNITY_Community 71|Community 71]]
- [[_COMMUNITY_Community 72|Community 72]]
- [[_COMMUNITY_Community 73|Community 73]]
- [[_COMMUNITY_Community 74|Community 74]]
- [[_COMMUNITY_Community 75|Community 75]]
- [[_COMMUNITY_Community 76|Community 76]]
- [[_COMMUNITY_Community 77|Community 77]]
- [[_COMMUNITY_Community 78|Community 78]]
- [[_COMMUNITY_Community 79|Community 79]]
- [[_COMMUNITY_Community 80|Community 80]]

## God Nodes (most connected - your core abstractions)
1. `AccountExecutionKernel` - 337 edges
2. `SleeveAdapterKind` - 304 edges
3. `Clock` - 273 edges
4. `SystemClock` - 272 edges
5. `InstrumentRules` - 268 edges
6. `AccountRiskPolicy` - 255 edges
7. `AccountEventType` - 247 edges
8. `ResearchConfig` - 239 edges
9. `RequestedIntent` - 209 edges
10. `MarketInputRef` - 202 edges

## Surprising Connections (you probably didn't know these)
- `Windows-only adapter for a frozen historical root.      The influencing file has` --uses--> `AccountEventType`  [INFERRED]
  scripts\run_strategy_overhaul_v2_repair.py → liquidity_migration\account_kernel.py
- `Hash the date partitions that can influence the registered RMOM output.` --uses--> `AccountEventType`  [INFERRED]
  scripts\run_strategy_overhaul_v2_repair.py → liquidity_migration\account_kernel.py
- `AccountEventType` --uses--> `Account owners are required continuously, not merely checked for ``failed``.`  [INFERRED]
  liquidity_migration\account_kernel.py → scripts\check_demo_liveness.py
- `AccountEventType` --uses--> `Check immutable-prior integrity, not meaningless wall-clock freshness.`  [INFERRED]
  liquidity_migration\account_kernel.py → scripts\check_demo_liveness.py
- `AccountEventType` --uses--> `The continuous decile join drops EVERY symbol when residual_momentum.parquet lac`  [INFERRED]
  liquidity_migration\account_kernel.py → scripts\check_demo_liveness.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.02
Nodes (513): DecisionFunnelObserver, PendingTerminalStatus, Normalize Bybit private execution/order streams into account-kernel facts., Subscribe a candidate without changing the currently published stream., Subscribe a candidate without changing the currently published stream., Atomically publish a ready candidate and retire its exact predecessor., Atomically publish a ready candidate and retire its exact predecessor., Block health on a dead private stream and rebuild it without auth storms. (+505 more)

### Community 1 - "Community 1"
Cohesion: 0.01
Nodes (462): _base_reasons(), build_candidate_universe_artifact(), build_profile_universe_tables(), build_profile_universe_tables_from_frames(), continuous_profile_universe_inputs(), _decision_rows(), enforce_frozen_candidate_frames(), enforce_frozen_candidate_population() (+454 more)

### Community 2 - "Community 2"
Cohesion: 0.02
Nodes (436): CandidatePopulationReconciliation, FrozenCandidateUniverse, AccountTargetPublisher, component_target_key(), publish_exit_first_target_requests(), requested_target(), unresolved_target_snapshot(), RequestedIntent (+428 more)

### Community 3 - "Community 3"
Cohesion: 0.01
Nodes (419): HTMLParser, _adapt_request_targets(), _atomic_replace(), _filename(), from_dict(), _prepared_request_intents(), _superseding_requests(), _archive_kline_skip_rows() (+411 more)

### Community 4 - "Community 4"
Cohesion: 0.01
Nodes (346): _active_stop(), _batch_target_proposals(), _clear_recent_entry_rejection_counters(), _component_attribution_pending(), _entry_attempt_key(), _entry_rejection_summary(), _entry_risk_decision_messages(), _entry_scope() (+338 more)

### Community 5 - "Community 5"
Cohesion: 0.01
Nodes (307): load_demo_rules(), load_demo_rules_bytes(), _load_json_bytes(), load_risk_policy(), load_risk_policy_bytes(), Shared, venue-mutation-free account rules and risk-policy loaders., require_registered_demo_rule_max_age_hours(), BybitAccountExecutionConsumer (+299 more)

### Community 6 - "Community 6"
Cohesion: 0.02
Nodes (368): AccountRiskPolicy, SleeveAdapterKind, calendar_shift(), A per-symbol positional ``value.shift(periods).over("symbol")`` that is NULL unl, CostConfig, TradeLifecycleConfig, _additive_equity(), _additive_summary() (+360 more)

### Community 7 - "Community 7"
Cohesion: 0.01
Nodes (198): DemoAccountMutationLease, Held canonical lease capability required for every demo mutation., api_key_allows_order_submit(), BybitPrivateClient, _env_flag(), BybitDataError, BybitRequestRejected, BybitSubmissionUncertain (+190 more)

### Community 8 - "Community 8"
Cohesion: 0.01
Nodes (280): _cal_roll(), calendar_roll(), coerce_int(), date_boundary_ms(), date_ms(), _date_range(), _date_symbol_set(), _exclude_symbols() (+272 more)

### Community 9 - "Community 9"
Cohesion: 0.02
Nodes (251): AccountOwnerLease, AccountOwnerLeaseAlreadyHeldError, acquire_inherited_account_owner_lease(), _add_prepared_receipt_arguments(), _build_cli_parser(), canonical_demo_account_lease_path(), _canonical_demo_expected_owner(), _canonical_demo_lease_directory() (+243 more)

### Community 10 - "Community 10"
Cohesion: 0.02
Nodes (203): build_kline_follower(), FollowerKlineStreamManager, Read-only kline FOLLOWER — share one WS kline data plane across co-located sleev, Warn ONCE per staleness episode when the leader snapshot stops moving —, Drop follower-side symbols that are no longer in the leader snapshot         (t, Re-read the leader snapshot iff its (mtime, size) changed. Returns         True, Build a read-only follower and reject a frozen self-follow route., ``KlineStreamManager`` drop-in that follows another root's flushed snapshot. (+195 more)

### Community 11 - "Community 11"
Cohesion: 0.02
Nodes (252): target_reservation_rows(), finite_float(), Coerce `value` to a finite float, returning `default` if missing/invalid., component_source_paths(), ContinuousComponentSource, load_continuous_component_source(), Load continuous component artifacts from an explicit generated root., _beta_window_joint_observation_count() (+244 more)

### Community 12 - "Community 12"
Cohesion: 0.02
Nodes (214): _require_publication(), _cmd_long_native_event_demo_cycle(), exact_duration_ms(), is_weekend_ms(), Return an exact integer millisecond duration.      Use this for timestamp look, True when the UTC day containing ``ts_ms`` is Saturday or Sunday.      Epoch d, account_id_for_environment(), execution_environment() (+206 more)

### Community 13 - "Community 13"
Cohesion: 0.02
Nodes (184): BinanceDataError, BinanceUSDMData, _ceil_to_period(), _floor_to_period(), _http_error_detail(), _raise_if_suspicious_empty_page(), Seconds to wait after a 429/418, from the Retry-After header.          Falls b, Best-effort detail string from an HTTPError body (the Binance error JSON). (+176 more)

### Community 14 - "Community 14"
Cohesion: 0.03
Nodes (120): require_profile_binding(), _build_demo_universe(), _bust_demo_instruments_cache(), _column_values(), _concat_recent_klines(), _dedupe_recent_klines(), _demo_instruments(), _demo_instruments_cache_paths() (+112 more)

### Community 15 - "Community 15"
Cohesion: 0.03
Nodes (105): _default_long_kline_stream_manager_factory(), _ensure_default_log_handler(), LongNativeDemoDaemon, _seed_long_public_ticker_cache(), _select_long_kline_stream_manager_factory(), _validate_long_daemon_startup(), _atomic_private_replace(), from_dict() (+97 more)

### Community 16 - "Community 16"
Cohesion: 0.07
Nodes (86): account_journal_lock_path(), account_journal_path(), account_transactions_path(), AccountEventSpec, AccountJournal, AccountJournalIntegrityError, _aggregate_target_quantities(), _append_jsonl_projection() (+78 more)

### Community 17 - "Community 17"
Cohesion: 0.05
Nodes (83): account_route_manifest_path(), AccountRouteConfigurationError, AccountRouteCutoverRequiredError, AccountRouteError, AccountRouteIntegrityError, AccountRouteMissingError, _atomic_create(), derive_account_route() (+75 more)

### Community 18 - "Community 18"
Cohesion: 0.09
Nodes (55): build_snapshot_plan(), _capture_one(), capture_snapshot(), contract_sha256(), extract_snapshot(), _file_signature(), _get_metadata(), _initialize_container() (+47 more)

### Community 19 - "Community 19"
Cohesion: 0.17
Nodes (49): AccountIntentInbox, CountingTwin, _inbox(), _market(), _policy(), _request(), _route(), _rules() (+41 more)

### Community 20 - "Community 20"
Cohesion: 0.11
Nodes (53): completed_expired_entry_attempt_keys(), _accepted_batches(), _batch_fill_summary(), _build_batch_fill_index(), canonical_account_projection(), canonical_adverse_reduction_events(), canonical_component_execution_anchors(), canonical_entry_attempts() (+45 more)

### Community 21 - "Community 21"
Cohesion: 0.1
Nodes (48): _absolute(), _archive_targets(), create_reset_archive(), _directory_flags(), _entry_identity(), _hash_descriptor(), main(), _mount_id_for_fd() (+40 more)

### Community 22 - "Community 22"
Cohesion: 0.09
Nodes (35): _commit_all(), _fixture(), _git_repository(), _issue(), _private(), test_authority_rejects_exchange_credentials_in_paper_route_environment(), test_authority_rejects_raw_research_mode_and_wrong_acknowledgement(), test_clean_checkout_compares_git_executable_mode() (+27 more)

### Community 23 - "Community 23"
Cohesion: 0.13
Nodes (37): _rate_limit_retry_seconds(), Seconds to wait before the single 429 retry, or None when the error is     not, send_telegram_message(), TelegramConfig, FakeResponse, _http_error(), _install_urlopen(), Stand-in for the object returned by urllib.request.urlopen. (+29 more)

### Community 24 - "Community 24"
Cohesion: 0.08
Nodes (25): btc_context_by_day(), _daily_closes_by_day(), _equal_optional_float(), ExpandingBtcRiskState, _finite(), _fsync_directory(), _fsync_file(), _hash_payload() (+17 more)

### Community 25 - "Community 25"
Cohesion: 0.09
Nodes (33): Enum, compare_numeric(), NumericComparison, NumericTolerance, NumericUnit, _ordered_float_bits(), Dimension-aware numeric comparison with finite-position and ULP evidence., Return binary64 representable-step distance for two finite values. (+25 more)

### Community 26 - "Community 26"
Cohesion: 0.11
Nodes (33): _admission_reason(), _article_effective_time(), _article_symbols(), ArticleEvidence, _artifact_identities(), _canonical_json(), _effective_times_ms(), _fetch() (+25 more)

### Community 27 - "Community 27"
Cohesion: 0.19
Nodes (26): AccountNotificationEngine, _setup_open(), _setup_risk_notifications(), _submit_entry_risk_decision(), test_entry_risk_dedupe_and_fallback_attempt_key_survive_restart(), test_entry_risk_first_rejection_sends_one_actionable_alert(), test_entry_risk_reason_change_sends_one_changed_alert(), test_expired_entry_signal_does_not_remain_an_unresolved_risk_block() (+18 more)

### Community 28 - "Community 28"
Cohesion: 0.17
Nodes (25): _environment(), _read(), test_activation_verifies_bound_state_before_start_and_cannot_reconfigure_it(), test_authorized_wrapper_owns_every_runtime_argv_and_verifies_before_exec(), test_demo_account_notification_reads_the_explicit_continuous_status_root(), test_demo_and_paper_strategy_units_use_one_validated_operational_profile(), test_dependency_contract_has_one_source_and_exact_runtime_pins(), test_deploy_permission_probe_uses_only_bound_demo_credentials() (+17 more)

### Community 29 - "Community 29"
Cohesion: 0.13
Nodes (18): _apply(), _assert_existing_keys_allclose(), _patch_residual_inputs(), Causality tests for the residual-momentum precompute (scripts/precompute_residua, Forward targets arrive late: a padded tail value may legitimately     change on, residual_momentum[D] = sum residual_return[D-9 .. D-3] (rolling 7, shift 3)., Look-ahead guard: residual_momentum[D] must be INVARIANT to residual_return at d, A later global end must not erase a delisted symbol's final signals.      With (+10 more)

### Community 30 - "Community 30"
Cohesion: 0.19
Nodes (20): _function_source(), test_execute_binds_clean_candidate_before_deployed_state_and_rechecks_before_clear(), test_failed_post_clear_handoff_stops_and_verifies_every_managed_unit(), test_leave_stopped_receipt_is_created_before_owner_leases_are_released(), test_paper_runner_and_reset_share_historical_owner_lease_path(), test_reset_and_deploy_share_host_maintenance_lock_with_legacy_bridge(), test_reset_clean_candidate_check_ignores_git_replace_refs(), test_reset_clears_account_epochs_in_place_without_retiring_lock_inodes() (+12 more)

### Community 31 - "Community 31"
Cohesion: 0.26
Nodes (19): _publish(), _stages(), test_cleanup_does_not_unlink_a_foreign_final_replacement(), test_existing_final_is_exclusive_and_unchanged(), test_group_readable_commit_requires_an_explicit_group(), test_late_final_collision_is_preserved(), test_mount_identity_change_is_detected_before_link(), test_parent_permissions_are_revalidated_before_mode_commit() (+11 more)

### Community 32 - "Community 32"
Cohesion: 0.22
Nodes (17): diagnose(), is_tainted(), Turn completed-run integrity signals into actionable warnings.  Each :class:`R, True when any warning marks the result survivorship/look-ahead biased., A compact, loud block for stdout. Empty list → a single clean line., Build the warning list for a completed run from its integrity signals.      Pu, render(), RunWarning (+9 more)

### Community 33 - "Community 33"
Cohesion: 0.18
Nodes (9): _drive_main(), _eq_df(), Tests for scripts/equity_curves.py (+ scripts/continuous_deployed_equity_refresh, Run ``main`` for the continuous sleeve and capture run_venue's kwargs., test_continuous_inherits_frozen_start_when_window_unset(), test_continuous_uses_explicit_start_when_given(), test_continuous_uses_rolling_start_when_years_given(), test_stats_no_drawdown_returns_none_mar() (+1 more)

### Community 34 - "Community 34"
Cohesion: 0.22
Nodes (6): Functional tests for sleeve topology helpers against a stateful fake systemctl., Last-resort fallback (NEITHER sleeves.env present — a stripped checkout):     E, _run(), test_lib_fallback_defaults_every_sleeve_off(), test_loaded_toggles_long_continuous_and_paper_on(), test_unknown_liquidity_migration_unit_is_cleaned_and_verified()

### Community 36 - "Community 36"
Cohesion: 0.67
Nodes (1): Guards against circular-import regressions in the active module split.  The pu

### Community 37 - "Community 37"
Cohesion: 1.0
Nodes (2): _skill_files(), test_codex_and_claude_project_skills_are_identical()

### Community 38 - "Community 38"
Cohesion: 1.0
Nodes (1): Build identity from the authenticated ``query-api`` response.          ``userI

### Community 40 - "Community 40"
Cohesion: 1.0
Nodes (1): Declarative spec for one univariate feature.      ``builder`` consumes the sha

### Community 41 - "Community 41"
Cohesion: 1.0
Nodes (1): Pre-aggregated daily inputs shared by all feature builders.      Constructing

### Community 42 - "Community 42"
Cohesion: 1.0
Nodes (1): Load ``dataset`` from ``data_root`` and filter to [start_ms, end_ms).      ``e

### Community 43 - "Community 43"
Cohesion: 1.0
Nodes (1): ISO date string → UTC midnight ms epoch. Tolerates 'YYYY-MM-DD' only.

### Community 44 - "Community 44"
Cohesion: 1.0
Nodes (1): Aggregate hourly OHLCV bars to one bar per (symbol, date).      Daily open  =

### Community 45 - "Community 45"
Cohesion: 1.0
Nodes (1): Per-(symbol, date): sum actual settlements and retain the last raw rate.

### Community 46 - "Community 46"
Cohesion: 1.0
Nodes (1): Per-(symbol, date): last OI observation of the day (end-of-day snapshot).

### Community 47 - "Community 47"
Cohesion: 1.0
Nodes (1): Per-(symbol, date): last hourly premium-index close (end-of-day snapshot).

### Community 48 - "Community 48"
Cohesion: 1.0
Nodes (1): Per-symbol daily simple return = close[D] / close[D-1] - 1.      Calendar-exac

### Community 49 - "Community 49"
Cohesion: 1.0
Nodes (1): Cross-sectional average-tie rank fraction in [0, 1] per ts_ms.      Uses ``ran

### Community 50 - "Community 50"
Cohesion: 1.0
Nodes (1): Cross-sectional Z-score per ts_ms: (x - mean_xs) / std_xs.

### Community 51 - "Community 51"
Cohesion: 1.0
Nodes (1): ``xs_rank_ret_Nd``: cross-sectional rank of N-day total return.      N=1 -> in

### Community 52 - "Community 52"
Cohesion: 1.0
Nodes (1): ``liquidity_rank``: cross-sectional rank by 7d trailing mean turnover.      Ma

### Community 53 - "Community 53"
Cohesion: 1.0
Nodes (1): ``liquidity_rank_delta_Nd``: continuous version of the event signal.      Posi

### Community 54 - "Community 54"
Cohesion: 1.0
Nodes (1): ``turnover_delta_Nd``: today's turnover normalised by the prior N-day mean.

### Community 55 - "Community 55"
Cohesion: 1.0
Nodes (1): ``funding_rate_z``: cross-sectional Z-score of today's funding rate.      Posi

### Community 56 - "Community 56"
Cohesion: 1.0
Nodes (1): ``funding_rate_delta_7d``: funding momentum.      Current-week funding sum min

### Community 57 - "Community 57"
Cohesion: 1.0
Nodes (1): ``oi_delta_7d``: 7-day OI change normalised by 30d ADV (in USD).      ``(oi_va

### Community 58 - "Community 58"
Cohesion: 1.0
Nodes (1): ``oi_to_adv``: OI USD / 30d ADV USD. Positioning intensity (turns).

### Community 59 - "Community 59"
Cohesion: 1.0
Nodes (1): ``premium_index_z``: cross-sectional Z of the EOD premium-index close.      Po

### Community 60 - "Community 60"
Cohesion: 1.0
Nodes (1): ``realized_vol_7d``: annualised stdev of daily returns over the last 7 days.

### Community 61 - "Community 61"
Cohesion: 1.0
Nodes (1): ``vol_of_vol_30d``: std over 30 days of the daily |return| series.      Captur

### Community 62 - "Community 62"
Cohesion: 1.0
Nodes (1): ``close_location_1d``: (close - low) / (high - low) for today's session.

### Community 63 - "Community 63"
Cohesion: 1.0
Nodes (1): ``range_extension_30d``: today's range divided by the 30d mean range.      >1

### Community 64 - "Community 64"
Cohesion: 1.0
Nodes (1): ``dist_from_30d_high``: (close - max(high over last 30d)) / max(high) — non-posi

### Community 65 - "Community 65"
Cohesion: 1.0
Nodes (1): ``dist_from_30d_low``: (close - min(low over last 30d)) / min(low) — non-negativ

### Community 66 - "Community 66"
Cohesion: 1.0
Nodes (1): Translate "all" or a list of feature names into a list of FeatureSpec.      Al

### Community 67 - "Community 67"
Cohesion: 1.0
Nodes (1): Per (symbol, decision-date D), compute fwd_ret_Nd from first-bar closes.

### Community 68 - "Community 68"
Cohesion: 1.0
Nodes (1): Detect which dataset-naming convention applies to ``data_root``.      The repo

### Community 69 - "Community 69"
Cohesion: 1.0
Nodes (1): Rolling-window OLS beta of each symbol's daily return on BTC's daily return.

### Community 70 - "Community 70"
Cohesion: 1.0
Nodes (1): Build the per-(symbol, ts_ms, date) factor-exposure panel, full-PIT.      Read

### Community 71 - "Community 71"
Cohesion: 1.0
Nodes (1): Per-day cross-sectional OLS of realized return on factor exposures.      For e

### Community 72 - "Community 72"
Cohesion: 1.0
Nodes (1): Per-symbol (first, last) kline `date` — the coin's traded lifespan in our archiv

### Community 73 - "Community 73"
Cohesion: 1.0
Nodes (1): Manifest pairs independently inferred from a currently-Trading listing.

### Community 74 - "Community 74"
Cohesion: 1.0
Nodes (1): Observed v5 listing starts, including a later reused-symbol incarnation.

### Community 75 - "Community 75"
Cohesion: 1.0
Nodes (1): Return relisting boundaries and traded bounds inside each incarnation.      Sy

### Community 76 - "Community 76"
Cohesion: 1.0
Nodes (1): Archive-manifest (date, symbol) pairs that the klines are REQUIRED to cover for

### Community 77 - "Community 77"
Cohesion: 1.0
Nodes (1): The (causal) residual-momentum polars expression. Factored out so the exact wind

### Community 78 - "Community 78"
Cohesion: 1.0
Nodes (1): Exclusive end = TOMORROW (UTC) so today's causal residual_momentum row is produc

### Community 79 - "Community 79"
Cohesion: 1.0
Nodes (1): Select the research or WS-driven kline dataset present in this root.

### Community 80 - "Community 80"
Cohesion: 1.0
Nodes (1): Apply the canonical shift-3 window and explicit provisional-state owner.

## Knowledge Gaps
- **392 isolated node(s):** `Bybit liquidity-migration research package.`, `Shared low-level helpers and constants for the liquidity_migration package.  C`, `Return an exact integer millisecond duration.      Use this for timestamp look`, `True when the UTC day containing ``ts_ms`` is Saturday or Sunday.      Epoch d`, `Coerce `value` to a finite float, returning `default` if missing/invalid.` (+387 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 36`** (3 nodes): `test_package_import_integrity.py`, `Guards against circular-import regressions in the active module split.  The pu`, `test_split_sibling_imports_cold_in_fresh_process()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 37`** (3 nodes): `test_project_skill_mirrors.py`, `_skill_files()`, `test_codex_and_claude_project_skills_are_identical()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 38`** (1 nodes): `Build identity from the authenticated ``query-api`` response.          ``userI`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 40`** (1 nodes): `Declarative spec for one univariate feature.      ``builder`` consumes the sha`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 41`** (1 nodes): `Pre-aggregated daily inputs shared by all feature builders.      Constructing`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 42`** (1 nodes): `Load ``dataset`` from ``data_root`` and filter to [start_ms, end_ms).      ``e`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 43`** (1 nodes): `ISO date string → UTC midnight ms epoch. Tolerates 'YYYY-MM-DD' only.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 44`** (1 nodes): `Aggregate hourly OHLCV bars to one bar per (symbol, date).      Daily open  =`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 45`** (1 nodes): `Per-(symbol, date): sum actual settlements and retain the last raw rate.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 46`** (1 nodes): `Per-(symbol, date): last OI observation of the day (end-of-day snapshot).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 47`** (1 nodes): `Per-(symbol, date): last hourly premium-index close (end-of-day snapshot).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 48`** (1 nodes): `Per-symbol daily simple return = close[D] / close[D-1] - 1.      Calendar-exac`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 49`** (1 nodes): `Cross-sectional average-tie rank fraction in [0, 1] per ts_ms.      Uses ``ran`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 50`** (1 nodes): `Cross-sectional Z-score per ts_ms: (x - mean_xs) / std_xs.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 51`** (1 nodes): ```xs_rank_ret_Nd``: cross-sectional rank of N-day total return.      N=1 -> in`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 52`** (1 nodes): ```liquidity_rank``: cross-sectional rank by 7d trailing mean turnover.      Ma`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 53`** (1 nodes): ```liquidity_rank_delta_Nd``: continuous version of the event signal.      Posi`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 54`** (1 nodes): ```turnover_delta_Nd``: today's turnover normalised by the prior N-day mean.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 55`** (1 nodes): ```funding_rate_z``: cross-sectional Z-score of today's funding rate.      Posi`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 56`** (1 nodes): ```funding_rate_delta_7d``: funding momentum.      Current-week funding sum min`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 57`** (1 nodes): ```oi_delta_7d``: 7-day OI change normalised by 30d ADV (in USD).      ``(oi_va`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 58`** (1 nodes): ```oi_to_adv``: OI USD / 30d ADV USD. Positioning intensity (turns).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 59`** (1 nodes): ```premium_index_z``: cross-sectional Z of the EOD premium-index close.      Po`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 60`** (1 nodes): ```realized_vol_7d``: annualised stdev of daily returns over the last 7 days.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 61`** (1 nodes): ```vol_of_vol_30d``: std over 30 days of the daily |return| series.      Captur`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 62`** (1 nodes): ```close_location_1d``: (close - low) / (high - low) for today's session.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 63`** (1 nodes): ```range_extension_30d``: today's range divided by the 30d mean range.      >1`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 64`** (1 nodes): ```dist_from_30d_high``: (close - max(high over last 30d)) / max(high) — non-posi`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 65`** (1 nodes): ```dist_from_30d_low``: (close - min(low over last 30d)) / min(low) — non-negativ`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 66`** (1 nodes): `Translate "all" or a list of feature names into a list of FeatureSpec.      Al`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 67`** (1 nodes): `Per (symbol, decision-date D), compute fwd_ret_Nd from first-bar closes.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 68`** (1 nodes): `Detect which dataset-naming convention applies to ``data_root``.      The repo`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 69`** (1 nodes): `Rolling-window OLS beta of each symbol's daily return on BTC's daily return.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 70`** (1 nodes): `Build the per-(symbol, ts_ms, date) factor-exposure panel, full-PIT.      Read`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 71`** (1 nodes): `Per-day cross-sectional OLS of realized return on factor exposures.      For e`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 72`** (1 nodes): `Per-symbol (first, last) kline `date` — the coin's traded lifespan in our archiv`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 73`** (1 nodes): `Manifest pairs independently inferred from a currently-Trading listing.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 74`** (1 nodes): `Observed v5 listing starts, including a later reused-symbol incarnation.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 75`** (1 nodes): `Return relisting boundaries and traded bounds inside each incarnation.      Sy`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 76`** (1 nodes): `Archive-manifest (date, symbol) pairs that the klines are REQUIRED to cover for`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 77`** (1 nodes): `The (causal) residual-momentum polars expression. Factored out so the exact wind`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 78`** (1 nodes): `Exclusive end = TOMORROW (UTC) so today's causal residual_momentum row is produc`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 79`** (1 nodes): `Select the research or WS-driven kline dataset present in this root.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 80`** (1 nodes): `Apply the canonical shift-3 window and explicit provisional-state owner.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `KlineStore` connect `Community 10` to `Community 2`, `Community 12`, `Community 14`?**
  _High betweenness centrality (0.048) - this node is a cross-community bridge._
- **Why does `ContinuousDemoCycleConfig` connect `Community 2` to `Community 0`, `Community 1`, `Community 3`, `Community 6`, `Community 10`, `Community 13`?**
  _High betweenness centrality (0.030) - this node is a cross-community bridge._
- **Why does `AccountExecutionKernel` connect `Community 0` to `Community 1`, `Community 2`, `Community 4`, `Community 5`, `Community 6`, `Community 12`, `Community 16`, `Community 19`, `Community 20`, `Community 27`?**
  _High betweenness centrality (0.027) - this node is a cross-community bridge._
- **Are the 746 inferred relationships involving `str` (e.g. with `exact_duration_ms()` and `safe_name()`) actually correct?**
  _`str` has 746 INFERRED edges - model-reasoned connections that need verification._
- **Are the 409 inferred relationships involving `ValueError` (e.g. with `exact_duration_ms()` and `date_ms()`) actually correct?**
  _`ValueError` has 409 INFERRED edges - model-reasoned connections that need verification._
- **Are the 307 inferred relationships involving `RuntimeError` (e.g. with `.require_healthy()` and `.require_recent_healthy()`) actually correct?**
  _`RuntimeError` has 307 INFERRED edges - model-reasoned connections that need verification._
- **Are the 317 inferred relationships involving `AccountExecutionKernel` (e.g. with `PendingTerminalStatus` and `BybitAccountExecutionConsumer`) actually correct?**
  _`AccountExecutionKernel` has 317 INFERRED edges - model-reasoned connections that need verification._