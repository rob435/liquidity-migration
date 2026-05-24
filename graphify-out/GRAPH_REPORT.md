# Graph Report - liquidity-migration  (2026-05-24)

## Corpus Check
- 107 files · ~268,065 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 2424 nodes · 7992 edges · 79 communities detected
- Extraction: 47% EXTRACTED · 53% INFERRED · 0% AMBIGUOUS · INFERRED: 4218 edges (avg confidence: 0.63)
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
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 41|Community 41]]
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
- [[_COMMUNITY_Community 81|Community 81]]
- [[_COMMUNITY_Community 82|Community 82]]
- [[_COMMUNITY_Community 83|Community 83]]
- [[_COMMUNITY_Community 84|Community 84]]

## God Nodes (most connected - your core abstractions)
1. `ResearchConfig` - 443 edges
2. `EventDemoCycleConfig` - 326 edges
3. `VolumeEventResearchConfig` - 318 edges
4. `EventScenario` - 260 edges
5. `ExecutionEventRouter` - 239 edges
6. `EventRiskCycleConfig` - 234 edges
7. `EventWebSocketRiskEngine` - 144 edges
8. `TradeLifecycleConfig` - 141 edges
9. `read_dataset()` - 112 edges
10. `CostConfig` - 108 edges

## Surprising Connections (you probably didn't know these)
- `CrossSectionalMomentumConfig` --uses--> `The demo cycle summary printed to journald must surface the top-3     slowest st`  [INFERRED]
  liquidity_migration/cross_sectional_momentum.py → tests/test_liquidity_migration_cli.py
- `CrossSectionalMomentumConfig` --uses--> `Serial cycles (1 worker or none) must not print parallel_workers — keeps     the`  [INFERRED]
  liquidity_migration/cross_sectional_momentum.py → tests/test_liquidity_migration_cli.py
- `CrossSectionalMomentumConfig` --uses--> `End-to-end: download fixture, run the momentum CLI subcommand.      The 8-symbol`  [INFERRED]
  liquidity_migration/cross_sectional_momentum.py → tests/test_liquidity_migration_cli.py
- `ExecutionEventRouter` --uses--> `A WS event landing AFTER one caller's timeout must NOT be discarded —     a subs`  [INFERRED]
  liquidity_migration/execution_router.py → tests/test_liquidity_migration_execution_router.py
- `ExecutionEventRouter` --uses--> `On WS disconnect we drop in-flight buffered links so REST fallback     becomes t`  [INFERRED]
  liquidity_migration/execution_router.py → tests/test_liquidity_migration_execution_router.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.02
Nodes (364): BybitPrivateWebSocketStream, BybitPublicTickerStream, BybitWebSocketTradeClient, _close_ws_client(), ResearchConfig, _compute_pipeline_diagnostics(), _build_private_ws_stream(), EventDemoDaemon (+356 more)

### Community 1 - "Community 1"
Cohesion: 0.01
Nodes (354): date_boundary_ms(), date_ms(), finite_float(), pct(), Shared low-level helpers and constants for the liquidity_migration package.  Cen, Coerce `value` to a finite float, returning `default` if missing/invalid., Format a fraction as a 2-decimal percentage, or `invalid` if not finite., Parse an ISO date/datetime to epoch milliseconds (UTC), or None if empty.      A (+346 more)

### Community 2 - "Community 2"
Cohesion: 0.02
Nodes (204): Return ``(api_key, api_secret, demo)`` from the .env DEMO / REAL_MONEY toggle., resolve_private_credentials(), main(), _active_position_by_symbol(), _base36(), _bool(), _build_demo_universe(), build_event_risk_private_client() (+196 more)

### Community 3 - "Community 3"
Cohesion: 0.02
Nodes (212): HTMLParser, download_archive_bytes(), _download_archive_to_path(), download_public_trade_archive(), _archive_kline_skip_rows(), ArchiveHourlyKlineApiDownloadConfig, ArchiveHourlyKlineDownloadConfig, ArchiveKlineDownloadConfig (+204 more)

### Community 4 - "Community 4"
Cohesion: 0.03
Nodes (163): _cooldown_until(), _empty_skip_counts(), _realized_stop_exit_ts(), select_demo_entry_candidates(), _trade_id(), FixtureSpec, generate_fixture_data(), _side_return() (+155 more)

### Community 5 - "Community 5"
Cohesion: 0.05
Nodes (91): BybitDataError, BybitMarketData, BybitPrivateClient, BybitPublicTradeStream, BybitRestRateLimiter, _env_flag(), _is_rate_limit(), _leverage_text() (+83 more)

### Community 6 - "Community 6"
Cohesion: 0.09
Nodes (87): read_dataset(), EventWebSocketRiskConfig, EventWebSocketRiskEngine, run_event_ws_risk(), BlockingPrivateStream, BlockingPublicStream, Tests for the dual-sleeve extension of ws_risk.  Per owner: ws_risk extends to h, Critical invariant: a long-side order with `lm-en-l-*` prefix must     never lan (+79 more)

### Community 7 - "Community 7"
Cohesion: 0.03
Nodes (77): _add_archive_download_klines_1h_api_parser(), _add_archive_download_klines_1h_parser(), _add_archive_download_klines_parser(), _add_archive_manifest_parser(), _add_combined_book_report_parser(), _add_cross_sectional_momentum_parser(), _add_data_layer_audit_parser(), _add_discover_universe_parser() (+69 more)

### Community 8 - "Community 8"
Cohesion: 0.04
Nodes (82): BinanceDataError, BinanceUSDMData, _ceil_to_period(), _floor_to_period(), _recent_history_start(), Slugify `name` for use as a file or path component., safe_name(), _archive_filename() (+74 more)

### Community 9 - "Community 9"
Cohesion: 0.07
Nodes (62): audit_crowding_model(), classify_liquidity_migration_crowding(), _crowding_reason_expr(), _entry_hour_expr(), format_crowding_model_report(), _pct(), summarize_crowding_classes(), _with_numeric_columns() (+54 more)

### Community 10 - "Community 10"
Cohesion: 0.09
Nodes (54): add_btc_regime(), add_clenow_score(), add_coil_release(), add_cross_sectional_rank(), add_funding_overheat(), add_liquidity_tier(), add_prior_high(), add_realized_vol() (+46 more)

### Community 11 - "Community 11"
Cohesion: 0.08
Nodes (37): add_forward_short_returns(), _bar_arrays(), cross_sectional_ic(), ic_table(), ic_vs_horizon(), ICResult, _rankdata(), Information-coefficient diagnostics for the reversion-alpha signals.  WHY THIS M (+29 more)

### Community 12 - "Community 12"
Cohesion: 0.17
Nodes (29): detect_entry_events(), exit_reason_for_position(), MomentumEventsConfig, All entry conditions firing on the same daily bar.      Returned rows are the (d, First exit reason that fires given today's per-symbol feature row.      Order is, _safe_float(), _features_df(), _features_row() (+21 more)

### Community 13 - "Community 13"
Cohesion: 0.11
Nodes (28): build_binance_oos(), discover(), fetch_month_klines(), list_symbol_months(), list_usdm_usdt_symbols(), main(), parse_month_csv(), Point-in-time Binance USD-M OOS data acquisition from the public ``data.binance. (+20 more)

### Community 14 - "Community 14"
Cohesion: 0.07
Nodes (6): BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     call, BybitPrivateClient must acquire the shared rate limiter before every     pybit H, When _call retries on a failed pybit call, each attempt must hit the     limiter, test_bybit_market_data_routes_get_through_rate_limiter(), test_bybit_private_client_rate_limiter_acquires_each_retry(), test_bybit_private_client_routes_call_through_rate_limiter()

### Community 15 - "Community 15"
Cohesion: 0.22
Nodes (24): send_telegram_message(), TelegramConfig, FakeResponse, _install_urlopen(), Stand-in for the object returned by urllib.request.urlopen., Replace urlopen with a recording fake; never touches the network.      Returns a, _set_credentials(), test_2xx_status_codes_return_true() (+16 more)

### Community 16 - "Community 16"
Cohesion: 0.16
Nodes (19): _clean_trades(), _entry_slippage_bps(), _exit_slippage_bps(), _float(), format_reconciliation_report(), _int(), _normalized_side(), Reconcile the paper (dry-run) ledger against the demo ledger.  The paper runner (+11 more)

### Community 17 - "Community 17"
Cohesion: 0.21
Nodes (19): _coverage_rows(), _crowded_edge(), _entry_date_expr(), _feature_edge(), _feature_verdict(), FeatureSpec, _filter_split(), _finite_float() (+11 more)

### Community 18 - "Community 18"
Cohesion: 0.16
Nodes (18): _exec_event(), On WS disconnect we drop in-flight buffered links so REST fallback     becomes t, If a link keeps receiving events, eviction must not pick it as victim     even w, N producer threads writing events for N distinct links, M consumer     threads w, A WS event landing AFTER one caller's timeout must NOT be discarded —     a subs, test_router_accumulates_partial_fills_in_order(), test_router_caps_buffered_links_with_fifo_eviction(), test_router_clear_all_supports_ws_reconnect() (+10 more)

### Community 20 - "Community 20"
Cohesion: 0.29
Nodes (10): _daily_basket_returns(), format_portfolio_hedge_report(), _path_metrics(), _pct(), run_portfolio_hedge_report(), _short_bad_dates(), _split_returns(), _worst_rolling_return() (+2 more)

### Community 21 - "Community 21"
Cohesion: 0.38
Nodes (11): _build_demo_features(), _demo_feature_cache_fingerprint(), _demo_feature_cache_paths(), _read_demo_feature_cache(), _write_demo_feature_cache(), _feature_cache_klines(), _feature_cache_universe(), test_build_demo_features_cache_misses_when_a_bar_is_appended() (+3 more)

### Community 22 - "Community 22"
Cohesion: 0.33
Nodes (8): axis_panel(), daily(), _font(), main(), 3-panel chart: short alone, long alone, combined book — keep production short, o, Stitch v11a IS+OOS aligned to date list., stitch_v11a(), to_returns()

### Community 23 - "Community 23"
Cohesion: 0.48
Nodes (6): daily(), _font(), main(), Side-by-side: uni10 baseline vs v4c (uni10 sigma+3d+7d) vs v4g (uni50 sigma+3d+n, stitch(), to_returns()

### Community 24 - "Community 24"
Cohesion: 0.48
Nodes (6): daily(), _font(), main(), v11a sniper vs v4c — standalone equity + combined book., stitch(), to_returns()

### Community 25 - "Community 25"
Cohesion: 0.48
Nodes (6): daily(), _font(), main(), Side-by-side: uni10 baseline vs v3a_uni10 vs v3a_uni50 vs v3g_uni50 — standalone, stitch(), to_returns()

### Community 26 - "Community 26"
Cohesion: 0.48
Nodes (6): _dd_to_y(), _eq_to_y(), main(), Render equity curves (in-sample + 2 OOS roots) for the momentum factor.  Uses Pi, _try_font(), _ts_to_x()

### Community 27 - "Community 27"
Cohesion: 0.53
Nodes (5): daily(), _font(), main(), v4c_uni10 (sigma-relative + 3d + 7d triggers) — standalone + combined book at le, to_returns()

### Community 28 - "Community 28"
Cohesion: 0.53
Nodes (5): _font(), main(), Equity curve for uni10_only FC pattern — stitched IS+OOS Bybit., rets(), to_daily()

### Community 29 - "Community 29"
Cohesion: 0.6
Nodes (4): main(), Equity curves: short alone vs short + long FC at various leverage levels., to_daily(), _try_font()

### Community 30 - "Community 30"
Cohesion: 0.6
Nodes (4): daily(), _font(), main(), v4c equity curves across three windows: Bybit IS, Bybit OOS, Binance OOS — singl

### Community 31 - "Community 31"
Cohesion: 0.6
Nodes (4): daily(), _font(), main(), Overlay v11a long sleeve at 10× leverage on top of the new Sharpe-4 short (q50-h

### Community 32 - "Community 32"
Cohesion: 0.67
Nodes (3): _font(), main(), Efficient frontier: trade count vs Sharpe across all FC variants tested in v4–v1

### Community 33 - "Community 33"
Cohesion: 0.67
Nodes (3): main(), Equity curve: short + leveraged long FC over stitched OOS+IS Bybit timeline (202, _try_font()

### Community 34 - "Community 34"
Cohesion: 0.67
Nodes (3): main(), Two-panel chart: LO_skip0 standalone + short × LO_skip0 combined book at various, _try_font()

### Community 35 - "Community 35"
Cohesion: 0.67
Nodes (3): main(), Two-panel chart: uni10_only standalone + short × uni10_only combined book at var, _try_font()

### Community 36 - "Community 36"
Cohesion: 0.67
Nodes (3): main(), Render equity curve for the FC FOMO chase pattern (Sharpe 1.5 honest)., _try_font()

### Community 37 - "Community 37"
Cohesion: 0.67
Nodes (3): _font(), main(), Final Jane Street efficient frontier: FC + sniper across universe sizes and vari

### Community 39 - "Community 39"
Cohesion: 0.67
Nodes (1): Aggregate all universe/rank sweep results into one ranked table.

### Community 41 - "Community 41"
Cohesion: 1.0
Nodes (1): Bybit liquidity-migration research package.

### Community 45 - "Community 45"
Cohesion: 1.0
Nodes (1): Eagerly read a dataset, optionally projecting only ``columns``.      ``columns=N

### Community 46 - "Community 46"
Cohesion: 1.0
Nodes (1): True when environment variable ``name`` is set to a truthy value.

### Community 47 - "Community 47"
Cohesion: 1.0
Nodes (1): Return ``(api_key, api_secret, demo)`` from the .env DEMO / REAL_MONEY toggle.

### Community 48 - "Community 48"
Cohesion: 1.0
Nodes (1): Thread-safe sliding-window rate limiter shared across BybitMarketData     instan

### Community 49 - "Community 49"
Cohesion: 1.0
Nodes (1): One-level subdirectory names under an S3 prefix (paginated).

### Community 50 - "Community 50"
Cohesion: 1.0
Nodes (1): All object keys under an S3 prefix (paginated).

### Community 51 - "Community 51"
Cohesion: 1.0
Nodes (1): Every USDT-quoted USD-M perp symbol that ever appears in the monthly archive.

### Community 52 - "Community 52"
Cohesion: 1.0
Nodes (1): Sorted YYYY-MM list of 1h-kline months available for a symbol, capped at max_mon

### Community 53 - "Community 53"
Cohesion: 1.0
Nodes (1): Map every USDT symbol that has 1h klines on/before max_month to its month list.

### Community 54 - "Community 54"
Cohesion: 1.0
Nodes (1): Parse a Binance Vision monthly 1h kline zip payload into kline rows.      Vision

### Community 55 - "Community 55"
Cohesion: 1.0
Nodes (1): Download and parse one monthly 1h kline file. Returns [] on hard failure.

### Community 56 - "Community 56"
Cohesion: 1.0
Nodes (1): Rewrite ``archive_trade_manifest`` so it lists only (symbol, date) pairs     tha

### Community 57 - "Community 57"
Cohesion: 1.0
Nodes (1): Build a Bybit-shaped PIT data root from the Binance Vision archive.      end_dat

### Community 58 - "Community 58"
Cohesion: 1.0
Nodes (1): Split a manifest command into its leading ``KEY=value`` env assignments     and

### Community 59 - "Community 59"
Cohesion: 1.0
Nodes (1): True if the command carries an order-submission flag as a real token:     either

### Community 60 - "Community 60"
Cohesion: 1.0
Nodes (1): Ground-up rebuild of the liquidity-migration short strategy as a cleanly separat

### Community 61 - "Community 61"
Cohesion: 1.0
Nodes (1): Daily tradable set: PIT-mature, non-stable, with a valid daily return.

### Community 62 - "Community 62"
Cohesion: 1.0
Nodes (1): Standardise raw_col within each date. Nulls stay null and are excluded     from

### Community 63 - "Community 63"
Cohesion: 1.0
Nodes (1): LAYER 1. Return the tradable panel with a `reversion_score` column.      The sco

### Community 64 - "Community 64"
Cohesion: 1.0
Nodes (1): Gross scaler driven by the 30d alt regime.      Default: a continuous ramp from

### Community 65 - "Community 65"
Cohesion: 1.0
Nodes (1): LAYER 2. Per signal day, produce a ranked desired book.      Output columns: dat

### Community 66 - "Community 66"
Cohesion: 1.0
Nodes (1): Conservative intrabar exit scan for a SHORT. Returns (exit_idx, exit_price, reas

### Community 67 - "Community 67"
Cohesion: 1.0
Nodes (1): UTC calendar date of an epoch-ms timestamp. Cached per day index — the     panel

### Community 68 - "Community 68"
Cohesion: 1.0
Nodes (1): Build the per-symbol bar structures `simulate` needs, once. Reusing this     acr

### Community 69 - "Community 69"
Cohesion: 1.0
Nodes (1): LAYER 3. Walk the hourly grid, admit entries under capacity + cooldown,     run

### Community 70 - "Community 70"
Cohesion: 1.0
Nodes (1): Correct overlapping-position accounting: each calendar day, sum every     open p

### Community 71 - "Community 71"
Cohesion: 1.0
Nodes (1): End-to-end: load PIT data, run the three layers, return results.

### Community 72 - "Community 72"
Cohesion: 1.0
Nodes (1): BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     call

### Community 73 - "Community 73"
Cohesion: 1.0
Nodes (1): BybitPrivateClient must acquire the shared rate limiter before every     pybit H

### Community 74 - "Community 74"
Cohesion: 1.0
Nodes (1): When _call retries on a failed pybit call, each attempt must hit the     limiter

### Community 75 - "Community 75"
Cohesion: 1.0
Nodes (1): The demo cycle summary printed to journald must surface the top-3     slowest st

### Community 76 - "Community 76"
Cohesion: 1.0
Nodes (1): Serial cycles (1 worker or none) must not print parallel_workers — keeps     the

### Community 77 - "Community 77"
Cohesion: 1.0
Nodes (1): Correctness tests for the reversion_alpha three-layer stack.  The execution simu

### Community 78 - "Community 78"
Cohesion: 1.0
Nodes (1): Build flat-ish hourly bars; price_fn(i) returns the close for bar i.

### Community 79 - "Community 79"
Cohesion: 1.0
Nodes (1): Render a reconciliation result (from reconcile_paper_demo) as markdown.

### Community 80 - "Community 80"
Cohesion: 1.0
Nodes (1): Read the paper and demo trade ledgers, reconcile them, write a markdown     repo

### Community 81 - "Community 81"
Cohesion: 1.0
Nodes (1): Thread-safe sliding-window rate limiter shared across BybitMarketData     instan

### Community 82 - "Community 82"
Cohesion: 1.0
Nodes (1): BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     call

### Community 83 - "Community 83"
Cohesion: 1.0
Nodes (1): BybitPrivateClient must acquire the shared rate limiter before every     pybit H

### Community 84 - "Community 84"
Cohesion: 1.0
Nodes (1): When _call retries on a failed pybit call, each attempt must hit the     limiter

## Knowledge Gaps
- **136 isolated node(s):** `ExchangeConfig`, `All entry conditions firing on the same daily bar.      Returned rows are the (d`, `First exit reason that fires given today's per-symbol feature row.      Order is`, `Drop pybit's 10006 (rate limit) retry chatter.      pybit's _handle_retryable_er`, `True when environment variable ``name`` is set to a truthy value.` (+131 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 39`** (3 nodes): `main()`, `analyze_sweep.py`, `Aggregate all universe/rank sweep results into one ranked table.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 41`** (2 nodes): `__init__.py`, `Bybit liquidity-migration research package.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 45`** (1 nodes): `Eagerly read a dataset, optionally projecting only ``columns``.      ``columns=N`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 46`** (1 nodes): `True when environment variable ``name`` is set to a truthy value.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 47`** (1 nodes): `Return ``(api_key, api_secret, demo)`` from the .env DEMO / REAL_MONEY toggle.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 48`** (1 nodes): `Thread-safe sliding-window rate limiter shared across BybitMarketData     instan`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 49`** (1 nodes): `One-level subdirectory names under an S3 prefix (paginated).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 50`** (1 nodes): `All object keys under an S3 prefix (paginated).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 51`** (1 nodes): `Every USDT-quoted USD-M perp symbol that ever appears in the monthly archive.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 52`** (1 nodes): `Sorted YYYY-MM list of 1h-kline months available for a symbol, capped at max_mon`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 53`** (1 nodes): `Map every USDT symbol that has 1h klines on/before max_month to its month list.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 54`** (1 nodes): `Parse a Binance Vision monthly 1h kline zip payload into kline rows.      Vision`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 55`** (1 nodes): `Download and parse one monthly 1h kline file. Returns [] on hard failure.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 56`** (1 nodes): `Rewrite ``archive_trade_manifest`` so it lists only (symbol, date) pairs     tha`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 57`** (1 nodes): `Build a Bybit-shaped PIT data root from the Binance Vision archive.      end_dat`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 58`** (1 nodes): `Split a manifest command into its leading ``KEY=value`` env assignments     and`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 59`** (1 nodes): `True if the command carries an order-submission flag as a real token:     either`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 60`** (1 nodes): `Ground-up rebuild of the liquidity-migration short strategy as a cleanly separat`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 61`** (1 nodes): `Daily tradable set: PIT-mature, non-stable, with a valid daily return.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 62`** (1 nodes): `Standardise raw_col within each date. Nulls stay null and are excluded     from`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 63`** (1 nodes): `LAYER 1. Return the tradable panel with a `reversion_score` column.      The sco`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 64`** (1 nodes): `Gross scaler driven by the 30d alt regime.      Default: a continuous ramp from`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 65`** (1 nodes): `LAYER 2. Per signal day, produce a ranked desired book.      Output columns: dat`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 66`** (1 nodes): `Conservative intrabar exit scan for a SHORT. Returns (exit_idx, exit_price, reas`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 67`** (1 nodes): `UTC calendar date of an epoch-ms timestamp. Cached per day index — the     panel`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 68`** (1 nodes): `Build the per-symbol bar structures `simulate` needs, once. Reusing this     acr`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 69`** (1 nodes): `LAYER 3. Walk the hourly grid, admit entries under capacity + cooldown,     run`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 70`** (1 nodes): `Correct overlapping-position accounting: each calendar day, sum every     open p`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 71`** (1 nodes): `End-to-end: load PIT data, run the three layers, return results.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 72`** (1 nodes): `BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     call`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 73`** (1 nodes): `BybitPrivateClient must acquire the shared rate limiter before every     pybit H`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 74`** (1 nodes): `When _call retries on a failed pybit call, each attempt must hit the     limiter`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 75`** (1 nodes): `The demo cycle summary printed to journald must surface the top-3     slowest st`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 76`** (1 nodes): `Serial cycles (1 worker or none) must not print parallel_workers — keeps     the`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 77`** (1 nodes): `Correctness tests for the reversion_alpha three-layer stack.  The execution simu`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 78`** (1 nodes): `Build flat-ish hourly bars; price_fn(i) returns the close for bar i.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 79`** (1 nodes): `Render a reconciliation result (from reconcile_paper_demo) as markdown.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 80`** (1 nodes): `Read the paper and demo trade ledgers, reconcile them, write a markdown     repo`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 81`** (1 nodes): `Thread-safe sliding-window rate limiter shared across BybitMarketData     instan`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 82`** (1 nodes): `BybitMarketData must call rate_limiter.acquire() before each pybit HTTP     call`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 83`** (1 nodes): `BybitPrivateClient must acquire the shared rate limiter before every     pybit H`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 84`** (1 nodes): `When _call retries on a failed pybit call, each attempt must hit the     limiter`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `VolumeEventResearchConfig` connect `Community 0` to `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 7`?**
  _High betweenness centrality (0.101) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 2` to `Community 0`, `Community 1`, `Community 3`, `Community 4`, `Community 6`, `Community 7`, `Community 8`, `Community 9`, `Community 15`, `Community 16`, `Community 17`, `Community 20`?**
  _High betweenness centrality (0.097) - this node is a cross-community bridge._
- **Why does `EventDemoCycleConfig` connect `Community 0` to `Community 2`, `Community 3`, `Community 5`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.088) - this node is a cross-community bridge._
- **Are the 441 inferred relationships involving `ResearchConfig` (e.g. with `LongNativeDemoDaemon` and `Long-side daemon — mirror of event_demo_daemon for the v11a sleeve.  Keeps a sin`) actually correct?**
  _`ResearchConfig` has 441 INFERRED edges - model-reasoned connections that need verification._
- **Are the 323 inferred relationships involving `EventDemoCycleConfig` (e.g. with `EventWebSocketRiskConfig` and `WebSocketRiskState`) actually correct?**
  _`EventDemoCycleConfig` has 323 INFERRED edges - model-reasoned connections that need verification._
- **Are the 315 inferred relationships involving `VolumeEventResearchConfig` (e.g. with `One-line `event demo cycle ...` summary used by both the legacy bash-loop     ru` and `Daily/weekly aggregate report covering both sleeves.      Reads the short ledger`) actually correct?**
  _`VolumeEventResearchConfig` has 315 INFERRED edges - model-reasoned connections that need verification._
- **Are the 258 inferred relationships involving `EventScenario` (e.g. with `CostConfig` and `TradeLifecycleConfig`) actually correct?**
  _`EventScenario` has 258 INFERRED edges - model-reasoned connections that need verification._