# Full-System Audit — liquidity-migration
**Date:** 2026-06-14  ·  **Method:** two-wave multi-agent audit (54 + 26 = 80 agents; every finding adversarially verified, refuted findings dropped).
**Scope:** every package module, scripts/, deploy/, tests/. Wave 1 = live-execution / PIT / engine / metrics / data / infra (26 units). Wave 2 = research-methodology / forward-collector / storage / archive-integrity / untested-script surface the wave-1 completeness critic flagged as unowned (12 units).
**Verified findings:** 200 — 15 high, 67 medium, 118 low. None survived at *critical* (the look-ahead/safety items are dormant or guarded today).
**Remediated this pass:** 19 distinct findings fixed in the working tree (uncommitted) + 12 new regression tests. Gate: ruff clean, pytest 1360 passed (was 1348). Nothing committed or pushed.

> **UPDATE — full remediation (2026-06-14).** Following a "fix every finding" directive, **all 200 findings are now fixed or flagged.** ~190 root-cause fixes landed (across the package, scripts, and deploy) via three file-disjoint multi-agent workflows (complete → integrate) plus manual integration, all uncommitted in the working tree. **Gate: ruff clean, pytest 1814 passed** (1348 baseline → 466 net-new regression tests). The integration triage also fixed a real bug in the archive-integrity-4 fix (it validated the `.tmp` temp suffix, silently skipping the gzip drain). **Flagged for pre-registration / operator sign-off** (shift a promoted/reported number or are a design decision, so NOT changed unilaterally): `cost-funding-4` (marked-notional funding), `metrics-3` (Sharpe-convention consolidation), the gap-blind→calendar feature re-base (`research-methodology-2`/`pit-signals-3`/`-4`, re-bases gapped-symbol rmom/ridge), `long-sleeve-1` (weekend tilt before re-enabling LONG), the forward-clock hedge identity (`forward-replay-1`/`sizing-rebalance-2`, btc_only vs live 2f), the additive→compounded continuous comparator (`metrics-2`/`reconciliation-2`), and `cross-sleeve-4` (IM-budget wiring needs a sleeve-weighted allocation receipt). The per-finding tables below are the original audit record; status markers predate the full remediation.

## Legend
- ✅ **fixed** — landed this pass, gate green.
- 🔧 **ready** — safe, specced, low blast radius; apply in a follow-up batch.
- 🚩 **operator** — changes a promoted/frozen numeric, live-execution behaviour, or is large; needs sign-off (+ pre-registration where it touches a working dataset).
- 📋 **review** — moderate, worth a closer look.

## Top risks (address before any real-money consideration)
- market_min_ret_1d entry gate reads the entry day's full close-to-close return (same-day look-ahead) in the continuous research/sweep engine (pit-signals-1, pit-engine-1, event-demo-core-1). Off by default and exploratory-only, but if a sweep enables it the result is fake alpha that cannot reproduce in forward demo — corrupting any Tier-3 comparison built on it.
- Deployed LONG weekend 1.5x size tilt is applied in the backtest but NOT in the live demo sizing path (long-sleeve-1), so ~2/7 of entry days are mis-sized and the forward-demo arbiter measures a different strategy than the promoted profile. Latent only because the sleeve is currently toggled off.
- Gap-blind positional shifts in xs_rank_ret_Nd and the funding/oi/liquidity deltas (pit-signals-3) mislabel return horizons on gapped symbols and feed both the ridge OOF score and the residual_momentum factor residualization — a research-path correctness defect in the signals that drive promotion decisions.
- Cross-sleeve over-close: a trade with zero/empty ledger qty bypasses the per-sleeve cap and reduce-only-flattens the full netted position (ws-risk-3); combined with cross-sleeve-1 (continuous_addon never clamped) and hedge-2 (venue-net-blind hedge resize), one sleeve can liquidate or over-hedge against a sibling on the shared account — money-relevant once promoted.
- Failed private-WS rebuild latches private_stream=None permanently with no retry and no operator signal (ws-risk-1), and the stale-WS watchdog is kept fresh by ticker traffic so it cannot detect the dead private stream (ws-risk-5) — silently losing the low-latency stop/TP/close feed for the daemon's lifetime.
- The continuous live-demo reconcile runs with --paper-only (reconciliation-1, uncertain), and reconcile.sh always exits 0 (reconciliation-3), so the demo<->paper divergence check the runbook tells operators to trust is skipped and no machine-checkable tripwire fires on the live book.
- VPS auto-deploy on push to main has no server-side full-suite CI (deploy-ci-2); only a manually-installed local pre-push hook gates a live deploy, so a regression outside the two smoke-test files deploys straight to the order-submitting demo daemons undetected.
- Forward-evidence MAR/drawdown is computed additively while the engine and STATE compound (metrics-2/reconciliation-2), giving a ~13-30% wrong headline on the binding standalone continuous read, and a no-drawdown window emits invalid JSON Infinity (metrics-1) that can break the audit pipeline or be misread as infinitely good.
- forward-replay-1 (HIGH): The Tier-3-facing forward signal clock accrues evidence for a BTC-only hedge object while the live demo book executes the banked BTC+ETH 2f object. A forward-readiness PASS is not evidence for the strategy actually running — directly corrupts the promotion read and is the exact same-object mismatch (errors #16) the Tier-3 gate exists to prevent.
- deploy-env-timers-1 (HIGH): Flipping CONTINUOUS_SLEEVE=off (the documented retirement action) orphans the hedge's open, stopless BTC/ETH long — timer disabled, risk service contractually forbidden from exiting it, watchdog stops monitoring it. A silent permanently-open unmonitored directional position; on any future REAL_MONEY promotion this is unmanaged live exposure after a retirement believed safe. The single finding most threatening to money.
- alpha-scripts-1 (HIGH): The ensemble scout's cross-venue MAR flag and min_mar tiebreak skip NULL MAR, so a single-venue or near-zero-DD combo is flagged both-venues-passing. This is on the path that selected the LIVE demo book's config — it claims cross-venue validation that did not happen.
- research-methodology-1 (HIGH): RidgeCombiner never validates embargo_days >= forward_horizon+1; with the house-default fwd_ret_3d and default embargo=2 the newest training target resolves inside the test window — textbook in-fold target leakage that inflates the Tier-1 OOF rank-IC gate. Latent today only because the scout accidentally left target_col at fwd_ret_1d.
- research-methodology-2 (HIGH): N-day feature builders use gap-blind positional shift/rolling on a sparse daily panel, silently ballooning the effective calendar lookback across delist/relist gaps. Feeds the ridge combiner OOF IC, the univariate IC survival rule, and the inverse-vol sizing denominator — corrupts the magnitude of research numbers that gate promotion (resampling-leakage class, errors #13).
- archive-integrity-1 (HIGH): Downloaded archive bytes are never size/checksum/structure-verified before being parsed into the canonical full_pit root; a truncation ending on a CSV record boundary becomes a silently-thin kline day that the resume guard treats as covered and never re-fetches — contaminating the working-dataset evidence under every cited number (errors #4/#12).
- liquidation-collector-1 (HIGH): A mid-burst Bybit subscribe send() failure (no try/except, websocket-client swallows the callback exception) leaves the symbol-universe tail unsubscribed for up to 24h, streaming zero liquidations, while the whole-root-mtime watchdog stays green — permanent silent loss of explicitly unbuyable forward data.
- storage-concurrency-2 (HIGH): The order-submitting ws_risk daemon's singleton lock (stale_seconds=0) self-wedges permanently if a fast restart reuses the crashed daemon's PID; engine.run() never starts, so all stops/take-profits/max-hold exits/IM-reconcile halt with open positions unmanaged while systemd still reports the unit active — total and silent when it hits.

## ✅ Fixed this pass (verified, working tree only)
| id | sev | what changed |
|---|---|---|
| `cli-config-1` | high | dest renamed to audit_data_root; global --data-root no longer clobbered (+test) |
| `pit-signals-1` | high | market_min_ret_1d gate lagged to the prior completed day (causal) (+poison-future test) |
| `test-gaps-1` | high | calendar_shift/calendar_roll gap behaviour now pinned by tests |
| `test-gaps-2` | high | volume_events_pit pre-listing/post-delisting/mid-gap survivorship now pinned by tests |
| `liquidation-collector-1` | high | subscribe burst send() wrapped; closes to force full resubscribe on failure |
| `deploy-env-timers-1` | high | PARTIAL: watchdog now pages CRITICAL on an orphaned hedge leg + sleeves.env doc fixed (+test); the deploy_vps_live.sh auto-trim half still flagged |
| `research-methodology-1` | high | RidgeCombinerConfig hard-errors when embargo_days < forward_horizon+1 (+test) |
| `metrics-1` | medium | forward_mar emits JSON null (not Infinity) on a no-drawdown window |
| `forward-replay-4` | medium | documented sign-equivalence of tier3_mar_positive to total>0 (no behaviour change needed) |
| `pit-engine-1` | medium | same market gate causal fix |
| `pit-data-2` | medium | _max_partition_date requires a real parquet; empty partition no longer reads as fresh (+test) |
| `pit-data-3` | medium | corrupt/zero-byte partition parquet degrades per-file instead of aborting the coverage audit |
| `decision-rule-1` | medium | NaN other-venue now fails the majority gate (descriptive, not investigation_positive) (+test) |
| `code-quality-1` | medium | dead _attach_residual_momentum + stale doc ref removed/fixed |
| `forward-replay-3` | medium | same fix as metrics-1 (non-finite MAR -> null) |
| `event-demo-core-1` | medium | same market gate causal fix |
| `research-methodology-3` | medium | embargo<->horizon causality invariant now pinned by test |
| `liquidation-collector-2` | medium | failed subscribe acks (success=false) now logged at WARNING instead of dropped |
| `shadows-1` | medium | dynexit anchor requires both 24h and 1h refs (no ret1-only arm) per its own contract (+test) |
| `storage-concurrency-3` | medium | read-path cross-bucket dedup sort now maintain_order=True (matches write path) |
| `pit-signals-2` | low | dead _attach_residual_momentum removed |

## High-severity findings (all)
| id | unit | status | conf | file | title |
|---|---|---|---|---|---|
| `cli-config-1` | cli-config | ✅ fixed | 0.92 | `liquidity_migration/cli_parsers.py, liquidity_` | Global --data-root is silently shadowed by continuous-rebalance-cycle-audit's own --data-root default (wrong-root / |
| `pit-signals-1` | pit-signals | ✅ fixed | 0.9 | `liquidity_migration/continuous_events.py` | market_min_ret_1d entry gate reads the entry day's full-day (future) close — look-ahead / fake alpha |
| `ws-risk-1` | ws-risk | 🚩 operator | 0.9 | `liquidity_migration/ws_risk.py` | Failed private-WS rebuild leaves private_stream=None permanently; real-time stop feed silently lost until restart |
| `long-sleeve-1` | long-sleeve | 🚩 operator | 0.9 | `liquidity_migration/long_native_event_demo.py` | Deployed weekend 1.5x size tilt is applied in the backtest but NOT in the live demo sizing path — live book trades  |
| `test-gaps-1` | test-gaps | ✅ fixed | 0.9 | `liquidity_migration/_common.py` | calendar_shift / calendar_roll (the gap-aware no-look-ahead primitives) have ZERO direct tests |
| `test-gaps-2` | test-gaps | ✅ fixed | 0.9 | `liquidity_migration/volume_events_pit.py` | volume_events_pit.py PIT/survivorship gate is live-called but completely untested (tests deleted with SHORT erasure |
| `research-methodology-2` | research-methodology | 🚩 operator | 0.85 | `liquidity_migration/signal_harness.py` | N-day feature builders use gap-blind positional shift(n) / positional rolling windows — mislabels horizon on delist |
| `alpha-scripts-1` | alpha-scripts | 🔧 ready | 0.85 | `scripts/continuous_ensemble_rebalance_scout.py` | Ensemble scout pooled cross-venue target flags & min_mar tiebreak silently ignore NULL MAR (single-venue/degenerate |
| `liquidation-collector-1` | liquidation-collector | ✅ fixed | 0.85 | `liquidity_migration/liquidation_collector.py` | Bybit subscribe burst has no error handling; a mid-burst send() failure silently leaves the symbol-universe tail un |
| `deploy-env-timers-1` | deploy-env-timers | ✅ fixed | 0.85 | `scripts/deploy_vps_live.sh, scripts/check_demo` | Flipping CONTINUOUS_SLEEVE=off orphans the hedge's open BTC/ETH long: timer disabled, no risk-service stop, and wat |
| `research-methodology-1` | research-methodology | ✅ fixed | 0.8 | `liquidity_migration/ridge_combiner.py` | RidgeCombinerConfig never validates embargo_days >= forward_horizon+1; fwd_ret_3d + default embargo=2 leaks the tes |
| `archive-integrity-1` | archive-integrity | 📋 review | 0.8 | `liquidity_migration/archive.py` | Downloaded archive bytes are never size/checksum-verified before being parsed into a canonical full_pit root |
| `forward-replay-1` | forward-replay | 🚩 operator | 0.8 | `liquidity_migration/continuous_forward_replay.` | Forward signal clock replays BTC-only hedge while the live deployed book runs the BTC+ETH 2f hedge (forward evidenc |
| `storage-concurrency-2` | storage-concurrency | 🚩 operator | 0.7 | `liquidity_migration/storage.py` | Singleton ws_risk cycle lock (stale_seconds=0) self-wedges permanently when a fast restart reuses the crashed daemo |
| `ws-risk-3` | ws-risk | 🚩 operator | 0.68 | `liquidity_migration/event_demo_exits.py` | Cross-sleeve over-close: a trade with zero/empty ledger qty bypasses the per-sleeve cap and reduce-only-flattens th |

## Medium-severity findings (all)
| id | unit | status | risk | conf | title |
|---|---|---|---|---|---|
| `alpha-scripts-3` | alpha-scripts | 🔧 ready | safe | 0.95 | alpha_sweep `rotate` experiment is a silent no-op: every cell runs BASE, manufacturing a misleading flat/null resul |
| `cross-sleeve-1` | cross-sleeve | 🔧 ready | safe | 0.92 | VALID_SLEEVES omits 'continuous_addon' — equal-split budget silently mis-allocates and never clamps the addon sleev |
| `metrics-1` | metrics | ✅ fixed | safe | 0.92 | forward_readiness_summary emits MAR=inf on zero-drawdown window → invalid JSON (Infinity) in the Tier-3-facing repo |
| `w4-w5-stages-1` | w4-w5-stages | 🔧 ready | safe | 0.92 | W5 Stage 0 W4-overlap falsifier gate passes VACUOUSLY when the W4 control artifacts are absent |
| `forward-replay-4` | forward-replay | ✅ fixed | safe | 0.92 | tier3_mar_positive Tier-3 gate field is mislabeled — it checks total return > 0, not MAR > 0 |
| `pit-signals-3` | pit-signals | 🚩 operator | moderate | 0.9 | xs_rank_ret_Nd (a frozen ridge feature AND a COMMON4 risk factor) uses positional shift(n), mislabeling the return  |
| `pit-engine-1` | pit-engine | ✅ fixed | risky | 0.9 | market_min_ret_1d entry gate reads the entry day's FULL close-to-close market return (same-day look-ahead) |
| `pit-data-2` | pit-data | ✅ fixed | safe | 0.9 | coverage_status reports a stale manifest as FRESH when a date= partition dir exists but holds no parquet |
| `pit-data-3` | pit-data | ✅ fixed | safe | 0.9 | Data-layer coverage audit crashes on a single corrupt/zero-byte partition parquet |
| `decision-rule-1` | decision-rule | ✅ fixed | safe | 0.9 | NaN-MAR venue + positive other venue → false investigation_positive (Tier-1 gate hole; contradicts sibling r1 gate  |
| `reconciliation-3` | reconciliation | 🔧 ready | safe | 0.9 | reconcile.sh always exits 0 — no machine-checkable divergence gate; failing reconcile legs are swallowed |
| `deploy-ci-1` | deploy-ci | 🔧 ready | safe | 0.9 | Watchdog unconditionally expects continuous-forward-report.timer active, but deploy disables it when both continuou |
| `code-quality-1` | code-quality | ✅ fixed | safe | 0.9 | Dead PIT-join function with a docstring citing a nonexistent pinning test (misleads about look-ahead coverage) |
| `w4-w5-stages-2` | w4-w5-stages | 🔧 ready | safe | 0.9 | Stage 1 'A0 must reproduce Stage 0 exactly' wiring-sanity check is recorded but never enforced as a gate |
| `w4-w5-stages-4` | w4-w5-stages | 🔧 ready | safe | 0.9 | No regression test covers the W5 engine hooks (candidate_sink emission / _apply_entry_order reordering) |
| `forward-replay-3` | forward-replay | ✅ fixed | safe | 0.9 | forward_mar = float('inf') serialized as non-strict JSON 'Infinity' token in orchestrator and audit output |
| `ws-risk-2` | ws-risk | 🚩 operator | moderate | 0.88 | Failed public-ticker rebuild leaves public_stream=None permanently; intrabar price feed degrades to 30s REST mark |
| `pit-engine-2` | pit-engine | 🚩 operator | moderate | 0.85 | Backtest age gate infers listing age from the loaded kline-window start; diverges from the live demo's authoritativ |
| `pit-data-5` | pit-data | 📋 review | moderate | 0.85 | Stale residual_momentum.parquet silently drops entire dates from the backtest continuous panel (no date-coverage va |
| `ws-risk-4` | ws-risk | 🔧 ready | safe | 0.85 | cap_qty_to_trade omits continuous_addon_root: an addon-only deployment loses cross-sleeve isolation |
| `kill-switch-1` | kill-switch | 🔧 ready | safe | 0.85 | Watchdog unconditionally pages on the forward-report timer that the deploy disables when continuous is retired -> p |
| `ratelimit-rest-1` | ratelimit-rest | 📋 review | moderate | 0.85 | Kline bootstrap under-rate-limits: one acquire per symbol but get_klines makes multiple paginated HTTP calls with n |
| `sizing-rebalance-1` | sizing-rebalance | 🚩 operator | moderate | 0.85 | Hedge-sizing live twin diverges from the backtest engine when the latest hedge-instrument return is missing (untest |
| `sizing-rebalance-2` | sizing-rebalance | 🚩 operator | risky | 0.85 | Forward-evidence ledger hedges single-leg BTC while the live demo book runs the 2f BTC+ETH hedge |
| `event-demo-core-1` | event-demo-core | ✅ fixed | moderate | 0.85 | Look-ahead leak in continuous market-context entry gate (market_min_ret_1d uses the full entry-day return) |
| `event-demo-core-2` | event-demo-core | 🔧 ready | safe | 0.85 | Risk-exit path books a fabricated 0-price close when no exit price is resolvable (missing the cycle path's BUG-5 gu |
| `cli-config-2` | cli-config | 📋 review | moderate | 0.85 | CostConfig dataclass default (maker_fill_probability=0.60) contradicts the committed config (0.0); load_config() wi |
| `cli-config-3` | cli-config | 🔧 ready | safe | 0.85 | event-risk-cycle --loop swallows the fatal order-safety config error and spins forever |
| `cli-config-4` | cli-config | 🔧 ready | safe | 0.85 | Unknown/typo'd --datasets names are silently dropped on both venues (no requested-vs-served completeness check) |
| `deploy-ci-2` | deploy-ci | 🚩 operator | safe | 0.85 | Auto-deploy on push to main has no GitHub-side full test/lint CI; only a manually-installed local pre-push hook gat |
| `deploy-ci-3` | deploy-ci | 🔧 ready | safe | 0.85 | Deploy enables+restarts the always-on liquidation collector but never verifies it is active — a broken collector st |
| `research-methodology-3` | research-methodology | ✅ fixed | safe | 0.85 | No test pins the embargo<->horizon causality invariant for the ridge combiner |
| `forward-replay-2` | forward-replay | 📋 review | moderate | 0.85 | forward_days reports calendar SPAN not observed rows; coverage gaps inflate the day count and split MAR numerator/d |
| `test-gaps-5` | test-gaps | 🔧 ready | safe | 0.82 | Order-submission parser safety default (store_true ⇒ off) is never asserted |
| `liquidation-collector-2` | liquidation-collector | ✅ fixed | safe | 0.82 | Bybit subscribe {"success":false} acks are silently discarded — a rejected chunk loses those symbols with no signal |
| `cost-funding-2` | cost-funding | 🚩 operator | moderate | 0.8 | Forward-ledger reconciliation return omits funding entirely (_fee_adjusted_return subtracts fees but not funding) |
| `ws-risk-5` | ws-risk | 🚩 operator | moderate | 0.8 | Stale-WS watchdog is kept fresh by ticker traffic, so it cannot detect a dead private stream |
| `hedge-1` | hedge | 🚩 operator | safe | 0.8 | 2f single-leg fallback gate counts joint obs over the full series, not the trailing beta window — 2f hedge can sile |
| `sizing-rebalance-3` | sizing-rebalance | 🚩 operator | moderate | 0.8 | Resize-day raw-return marking weights the day's move by post-resize qty against yesterday's mark, biasing the persi |
| `metrics-2` | metrics | 🚩 operator | moderate | 0.8 | Additive equity/drawdown in reconciliation._calendar_metrics diverges materially from the compounded engine/STATE M |
| `test-gaps-3` | test-gaps | 🚩 operator | moderate | 0.8 | signal_harness N-day feature builders use gap-blind positional shift with no gap test (inconsistent with ret_1d/for |
| `alpha-scripts-2` | alpha-scripts | 🚩 operator | moderate | 0.8 | Ensemble scout selects deployed config by full-sample pooled MAR over a large weight-grid x risk-rule space with no |
| `liquidation-collector-3` | liquidation-collector | 📋 review | moderate | 0.8 | Binance connected-but-zero-streaming leg cannot be alarmed; freshness watchdog only checks file mtime |
| `archive-integrity-2` | archive-integrity | 🔧 ready | safe | 0.8 | Binance Vision .CHECKSUM (sha256) sidecar is never fetched or verified for monthly kline archives |
| `backfill-writers-2` | backfill-writers | 🔧 ready | safe | 0.8 | Transient all-day download failure for a symbol is written as a permanent empty .touch() marker that resume treats  |
| `shadows-1` | shadows | ✅ fixed | safe | 0.8 | Dynexit anchor arms on a partial feature set (ret1 only) when the 24h-ago bar is missing, contradicting docstring a |
| `backfill-writers-1` | backfill-writers | 📋 review | moderate | 0.78 | Event-anchored metrics backfill silently overwrites full-history per-symbol parquet (no merge, coverage not recorde |
| `forward-replay-5` | forward-replay | 📋 review | moderate | 0.78 | Legitimate late-arriving historical partition triggers a permanent, unmonitored forward-clock stall |
| `cost-funding-1` | cost-funding | 🚩 operator | moderate | 0.75 | CostConfig default maker_fill_probability=0.60 undercosts vs the 100%-taker live runner; only the YAML saves the ci |
| `depth-collector-2` | depth-collector | 📋 review | moderate | 0.75 | Single-threaded blocking REST cycle has no per-cycle deadline; a venue-latency spike stretches one hourly cycle to  |
| `telegram-alert-1` | telegram-alert | 📋 review | moderate | 0.72 | Flapping CRITICAL re-alert suppressed within cooldown after a dropped 'resolved' note (liveness false-negative) |
| `test-gaps-4` | test-gaps | 🔧 ready | safe | 0.72 | Live continuous rmom day-floor join-alignment off-by-one is not pinned (fixture uses day-constant rmom values) |
| `hedge-3` | hedge | 🚩 operator | moderate | 0.7 | Runner books every market order as filled_qty=qty at an implied (not actual) fill price with no post-submit fill re |
| `reconciliation-2` | reconciliation | 🚩 operator | moderate | 0.7 | Forward comparator equity/drawdown/MAR uses additive return summation, not the engine's compounding equity — materi |
| `reports-charts-1` | reports-charts | 📋 review | moderate | 0.7 | Failed wallet read shows the $10,000 fallback as real equity on the operator Telegram message (no wallet_error surf |
| `archive-integrity-3` | archive-integrity | 📋 review | moderate | 0.7 | st_size>0 cache guard re-serves a partial/corrupt cached archive on every subsequent run |
| `shadows-2` | shadows | 📋 review | moderate | 0.68 | Torn JSONL write of an 'arm' row is unrecoverable — the trade is never re-armed, silently undercounting shadow cove |
| `ws-risk-6` | ws-risk | 🚩 operator | moderate | 0.65 | Partial reduce-only fills never book realized PnL on the closed chunk and never count toward the adverse-exit circu |
| `event-demo-core-3` | event-demo-core | 📋 review | moderate | 0.65 | Orphan-close matcher can attribute a sibling sleeve's same-side close to this trade on the shared netted demo accou |
| `exec-router-2` | exec-router | 🚩 operator | moderate | 0.6 | Duplicate-orderLinkId (110089) reject after WS-then-REST race leaves a live position recorded as error/untracked |
| `hedge-2` | hedge | 🚩 operator | moderate | 0.6 | Cross-sleeve netting collision: hedge BTC/ETH longs net against fade-book BTC/ETH shorts on the shared one-way acco |
| `ingestion-1` | ingestion | 📋 review | moderate | 0.6 | Empty mid-pagination batch silently truncates a fetch, then the downloader marks the full range complete -> permane |
| `code-quality-4` | code-quality | 🔧 ready | safe | 0.6 | _fee_adjusted_return double-subtracts fees if ever fed a backtest ledger (net_return semantics overloaded, no sourc |
| `storage-concurrency-3` | storage-concurrency | ✅ fixed | safe | 0.6 | Read-path cross-bucket dedup uses an UNSTABLE sort, contradicting the load-bearing maintain_order=True in _write_pa |
| `ratelimit-rest-3` | ratelimit-rest | 🔧 ready | safe | 0.55 | get_instruments_info has an unbounded while-True cursor loop (no max_pages guard) - hang risk in the universe-refre |
| `long-sleeve-2` | long-sleeve | 🚩 operator | moderate | 0.55 | Live FC volume-rank gate (today_volume_rank<=10) is ranked over the 120-symbol live superset, not the full backtest |
| `reconciliation-1` | reconciliation | 🚩 operator | moderate | 0.5 | Continuous live-demo reconcile runs with --paper-only, silently skipping the demo↔paper divergence check the runboo |

## Low-severity findings (by unit)

**alpha-scripts** (3)
- `alpha-scripts-5` 🔧 ready — Duplicate `turnsurge` branch makes the lookback-parameterized version dead code; surge lookback is permanently hard-wired to 168
- `alpha-scripts-4` 🔧 ready — alpha_sweep klines forward-pad fixed at BASE.max_hold_hours (48h) truncates long-hold trades in the `maxhold` sweep (up to 168h)
- `alpha-scripts-6` 🔧 ready — Gate-style diagnostics (funding, fadeconfirm) pick gate thresholds on the full sample and report only pooled MAR with no split-s

**archive-integrity** (1)
- `archive-integrity-4` 🔧 ready — No test asserts a truncated/short archive body is rejected, leaving the silent-corruption path uncovered

**backfill-writers** (4)
- `backfill-writers-5` 🔧 ready — regenerate_hedge_warmstart --validate never gates the overwrite; a regressed/short regeneration is written unconditionally
- `backfill-writers-6` 🔧 ready — Funding-rebuild month cache writes are non-atomic; an interrupted write leaves a truncated zip that crashes every rerun until ma
- `backfill-writers-4` 🔧 ready — Funding-rebuild cross-source dedup uses unstable unique(keep="first") so vision-vs-fapi precedence at month boundary is not guar
- `backfill-writers-3` 🔧 ready — Funding fapi top-up fetches only the first 1000 settlements from since_ms; a symbol with zero vision rows but a long active span

**cli-config** (3)
- `cli-config-6` 🔧 ready — download-data --end inclusive/exclusive semantics undocumented at the CLI (silently end-exclusive)
- `cli-config-5` 📋 review — continuous-events --end default is a fixed past date (2026-05-28), silently truncating recent data when run without --end
- `cli-config-7` 🔧 ready — discover-universe --include-excluded vs --exclude-defaults precedence is silent (include always wins) when both are passed

**code-quality** (6)
- `code-quality-2` 🔧 ready — Dead private function _filter_universe (unreferenced)
- `code-quality-3` 🔧 ready — Dead private function _pending_order_refs (unreferenced)
- `code-quality-5` 🔧 ready — Five near-duplicate finite-float helpers; consolidation stopped halfway
- `code-quality-6` 🔧 ready — Duplicated Sharpe/MAR/DD metric block in continuous_events (copy-paste, will drift)
- `code-quality-8` 🔧 ready — Silent broad-except fallback on the 1h-kline fast paths hides data-correctness faults (observability gap)
- `code-quality-9` 📋 review — cli.main is an 860-line, 21-branch command dispatcher with repeated inline symbol-parsing

**cost-funding** (3)
- `cost-funding-3` 📋 review — Symbols absent from the funding dataset get zero funding charge while the book still passes the gate as 'partial'
- `cost-funding-4` 🚩 operator — Funding charged on entry notional, not marked notional at each settlement (documented approximation)
- `cost-funding-5` 📋 review — Research/live funding lookup never passes the authoritative per-symbol settlement interval; relies on exact-stamp dedup

**cross-sleeve** (5)
- `cross-sleeve-2` 📋 review — closed_trade_ids GC path is never exercised in production — ws_risk omits the argument, so closed-trade reservations linger to T
- `cross-sleeve-3` 🔧 ready — candidate reservation trade_id differs from the executed (component-suffixed) trade_id in the live ensemble path
- `cross-sleeve-4` 🔧 ready — seed_margin_budget has no production caller (dead operator path) and is fail-loud unlike every sibling
- `cross-sleeve-5` 🔧 ready — Empty-dict budget {} round-trips to None (no-clamp) — write/read asymmetry in _loads_budget
- `cross-sleeve-6` 📋 review — partition_claimable acquires the control-row lock and rewrites the whole parquet once PER candidate (N lock cycles/cycle)

**decision-rule** (4)
- `decision-rule-2` 🔧 ready — legacy preset labels a cell 'candidate' with a 0-trade Binance venue (min_trades_binance=0)
- `decision-rule-5` 🔧 ready — _thirds produces degenerate/duplicated labels and a misleading all-cell-thirds>0 result for series shorter than 3 months
- `decision-rule-3` 🔧 ready — No dedup on duplicate monthly-ledger rows → a repeated month is double-counted in compound/thirds/bootstrap
- `decision-rule-4` 📋 review — Headline Tier-2 MAR delta uses the cell's full reported window while fragility diagnostics use the cell∩control month intersecti

**deploy-ci** (3)
- `deploy-ci-4` 📋 review — combined-book-report.service still wires --short-data-root data/bybit-demo-event for the erased daily-short sleeve
- `deploy-ci-5` 🔧 ready — deploy/.env stores live demo Bybit API secret and Telegram bot token in plaintext in the repo tree
- `deploy-ci-6` 📋 review — Deploy/verify source bybit-demo.env but never assert it lacks REAL_MONEY=true (defense-in-depth gap on the highest-stakes toggle

**deploy-env-timers** (2)
- `deploy-env-timers-2` 🔧 ready — deploy/.env stores live demo API secret + Telegram bot token world-readable (0644) on the local box
- `deploy-env-timers-3` 📋 review — Documented-valid CONTINUOUS_SLEEVE=off + CONTINUOUS_PAPER_SLEEVE=on leaves the paper shadow following a frozen demo kline store 

**depth-collector** (2)
- `depth-collector-3` 🔧 ready — No retention/rotation for data/depth: unbounded append-only JSONL growth additive with the liquidation collector on a 4 GB box
- `depth-collector-4` 🔧 ready — Crash mid-write can leave a truncated final JSONL line; append-only writer does no per-line flush/fsync or atomic-line guarantee

**exec-router** (4)
- `exec-router-3` 🔧 ready — Unenforced 'component tag must not start with a' constraint silently mis-routes a fill to the addon sleeve on rebuild
- `exec-router-4` 📋 review — Strict ws mode (rest_fallback=False) has no idempotency probe on WS timeout — same orphan risk, no mitigation
- `exec-router-5` 🔧 ready — _probe_existing_order parses createdTime with bare int() in the 'never make things worse' fallback path
- `exec-router-6` 📋 review — _continuous_suborder_link_id collapses trade_id into a 3-char (46656-value) crc32 hash — same-symbol/second collision re-cross-w

**forward-replay** (2)
- `forward-replay-6` 🔧 ready — Forward Sharpe annualized by sqrt(365.25) on gap-collapsed rows overstates Sharpe on a non-contiguous ledger
- `forward-replay-7` 🔧 ready — Drift check uses pure absolute tolerance (abs_tol=1e-9, rel_tol=0) on cumulative equity, contrary to the repo's np.allclose equi

**hedge** (2)
- `hedge-4` 🔧 ready — Warm-start is 22 days stale vs a 3-day cap: the armed daily hedge currently blocks-and-pages every run while the book sits unhed
- `hedge-5` 📋 review — Stale-warmstart block also blocks risk-reducing reduce-only legs when any add leg is present in the same run

**ingestion** (5)
- `ingestion-2` 🔧 ready — Bybit open_interest normalizer fabricates 0.0 for a missing field (data-download-7 fixed only on the Binance side)
- `ingestion-3` 🔧 ready — binance_vision.discover aborts the whole OOS build on a single transient per-symbol S3 listing failure
- `ingestion-6` 🔧 ready — funding_interval_min of literal 0 produces funding_rate_8h_equiv = inf (the `or 8` guard only catches None/empty, not 0)
- `ingestion-4` 📋 review — build_binance_oos appends klines_1h (append=True) leaving stale partitions if a rerun discovers a narrower universe
- `ingestion-5` 🔧 ready — Binance error codes returned as HTTP 4xx are retried needlessly (only HTTP-200 JSON error codes short-circuit)

**kill-switch** (3)
- `kill-switch-3` 🔧 ready — Fake systemctl in the kill-switch test marks units active on `enable` (no --now), hiding the on-sleeve enable+restart dependency
- `kill-switch-4` 🔧 ready — Liveness watchdog default risk/liquidation/depth roots are relative and depend on systemd WorkingDirectory; a manual/cron invoca
- `kill-switch-2` 🔧 ready — rmom-refresh timer is enabled in paper-only mode but only monitored when the DEMO sleeve is on -> dead refresh timer goes unwatc

**liquidation-collector** (3)
- `liquidation-collector-5` 🔧 ready — on_message drops the triggering liquidation frame when the connection-age rollover fires
- `liquidation-collector-4` 🔧 ready — Disk-full mid-flush can leave a torn final JSONL line, corrupting a day file for content readers
- `liquidation-collector-6` 🔧 ready — Cross-venue side and price conventions are stored raw and unnormalized, with no documented schema for the eventual consumer

**long-sleeve** (2)
- `long-sleeve-5` 🔧 ready — Weekend-tilt weekday computation is duplicated inline in two backtest paths instead of using the shared is_weekend_ms helper
- `long-sleeve-3` 🔧 ready — Live trade-row net_return omits venue fees and funding (gross-of-cost), unlike the backtest net_return

**metrics** (4)
- `metrics-5` 🔧 ready — tier3_mar_positive is named for MAR but actually tests total return
- `metrics-4` 🔧 ready — worst_day_return compounds same-day baskets while build_equity_curve sums them
- `metrics-6` 🔧 ready — forward_readiness_summary span/year math off-by-one vs the other annualizers (minor)
- `metrics-3` 📋 review — Three inconsistent Sharpe conventions (ddof and calendar-fill) across the metric helpers

**pit-data** (4)
- `pit-data-1` 📋 review — require_pit_membership is a dead PIT config flag with no enforcement path
- `pit-data-7` 🔧 ready — Scrape-based 1m/1h download paths do not bail on v5-listing sentinel rows (contradicts the manifest docstring) and burn the retr
- `pit-data-4` 🚩 operator — Narrow/single-symbol manifest rebuild destructively REPLACES whole date partitions (drops other symbols PIT-wide)
- `pit-data-6` 📋 review — resolve_dataset_name can silently substitute Binance funding/OI for a canonical 'funding'/'open_interest' request on a mixed roo

**pit-engine** (2)
- `pit-engine-3` 🔧 ready — LivePanelCache confirmed-bar fingerprint is sum-based and can miss a value-preserving backfill, serving a stale carry
- `pit-engine-4` 🚩 operator — State-mode exit fills at the same bar close where 'left-D9' becomes known (exit-side instantaneous fill, asymmetric with the +1h

**pit-signals** (3)
- `pit-signals-2` ✅ fixed — Dead _attach_residual_momentum helper is mis-documented for the surviving panel convention; rewiring would silently one-day-stal
- `pit-signals-5` 🔧 ready — cross_sectional_decile percentile/decile denominator divides by (len-1); a singleton cross-section yields NaN and silently drops
- `pit-signals-4` 🚩 operator — compute_btc_beta rolling window is row-based, not calendar-based — gapped symbols get a stale beta over >window calendar days

**ratelimit-rest** (4)
- `ratelimit-rest-2` 🔧 ready — Single BybitMarketData shared across 16 bootstrap threads mutates stat counters without a lock (lost-update race)
- `ratelimit-rest-5` 🔧 ready — BybitRestRateLimiter.acquire double-counts throttle stats and busy-spins on the wait<=0 window-boundary path
- `ratelimit-rest-6` 🔧 ready — Binance offline _get ignores Retry-After / 418 IP-ban; burns fixed retries fast then drops the symbol
- `ratelimit-rest-4` 🔧 ready — Per-sleeve rate limiters are independent instances with no per-IP coordination; docstring overstates starvation protection

**realmoney-safety** (3)
- `realmoney-safety-2` 🔧 ready — Hedge submit/equity paths hardcode demo=True instead of threading the resolved flag
- `realmoney-safety-3` 🔧 ready — _env_flag does not reject ambiguous/typo'd REAL_MONEY values loudly
- `realmoney-safety-1` 📋 review — Order-submit account guard is decoupled from client construction (defense-in-depth gap)

**reconciliation** (2)
- `reconciliation-4` 🔧 ready — audit_continuous_rebalance_cycles is O(n²) in rebalance rows and re-materializes the frame many times per call
- `reconciliation-5` 🔧 ready — _first_summary_line falls back to '(no output)' on a crashed/empty leg, masking the failure in the unified headline

**reports-charts** (3)
- `reports-charts-3` 📋 review — Levered (4x) equity-vs-BTC chart clips the y-axis floor to 0, hiding equity blowing through zero
- `reports-charts-4` 🔧 ready — Monthly table header reads 'Trades' with all-zero counts when a monthly frame lacks a trades column
- `reports-charts-5` 🔧 ready — Legend Strategy-vs-BTC final-value comparison can be over different windows when BTC data is shorter than the (flat-extended) st

**research-methodology** (1)
- `research-methodology-4` 🔧 ready — Tied bottom signals collapse to flat, silently shrinking the short book below the intended top-decile count

**shadows** (2)
- `shadows-4` 🔧 ready — Latent reconciliation key mismatch: trades missing signal_ts_ms key off entry_ts_ms while their orders key off signal_ts_ms, pro
- `shadows-3` 🔧 ready — Addon-shadow audit has no guard that primary and addon sources differ; identical defaults make a misconfig silently double-count

**sniper** (8)
- `sniper-1` 📋 review — Snipe placement bypasses venue min-qty / min-notional / max-order-qty sizing that the base entry enforces
- `sniper-2` 🔧 ready — Filled demo snipes have no paper twin, structurally inflating demo_only in reconcile_paper_demo
- `sniper-6` 🔧 ready — Snipe fill row uses Bybit updatedTime as entry_ts_ms — wrong fill time for PartiallyFilledCanceled
- `sniper-5` 📋 review — Crash between snipe place_order and the immediate order-row flush orphans a never-filling resting snipe that nothing cancels
- `sniper-3` 📋 review — Sniper order rows carry no updated_at_ms — ledger dedup falls back to glob/concat order, not recency
- `sniper-4` 📋 review — recover_snipe_trade_id_from_link silently falls back to a component-less id on a crc32%46656 collision, mis-pairing after rebuil
- `sniper-7` 🔧 ready — Snipe fill net_return is zeroed when the base trade row has no positive equity_usdt
- `sniper-8` 🔧 ready — Snipe link falls back to now_ms when base entry signal_ts_ms is 0, breaking trade_id recovery for that snipe

**storage-concurrency** (2)
- `storage-concurrency-4` 🔧 ready — Orphaned .tmp part files leak on process crash between write_parquet and rename (unbounded disk growth on the live VPS)
- `storage-concurrency-5` 🔧 ready — Parent directory is never fsync'd after rename, so the atomic publish is not actually power-loss durable despite the comment

**telegram-alert** (4)
- `telegram-alert-2` 🔧 ready — Blocking Telegram send still inside the ledger lock in the legacy event_demo.run_event_risk_cycle path
- `telegram-alert-3` 🔧 ready — 429 retry helper raises uncaught AttributeError when HTTPError.headers is None
- `telegram-alert-4` 🔧 ready — 429 rate-limit retry branch of send_telegram_message has zero test coverage
- `telegram-alert-5` 📋 review — Monitored systemd timers can fire a transient CRITICAL false-positive during a deploy

**test-gaps** (3)
- `test-gaps-6` 🔧 ready — Dead code momentum_signals._attach_residual_momentum cites a pinning test that does not exist; orphaned with stale PIT docstring
- `test-gaps-7` 🔧 ready — Untracked-position exit close-side sign is asserted only for a SHORT; the LONG (Buy⇒Sell) direction is never asserted
- `test-gaps-8` 🔧 ready — _split_order_link_id truncation/collision guard (the documented pathological-symbol case) is untested

**universe-pit** (5)
- `universe-pit-2` 🔧 ready — No test asserts dated-delivery (LinearFutures) or missing-contractType contracts are excluded
- `universe-pit-4` 🔧 ready — Stale comment references erased SHORT strategy's prior7_liquidity_rank in still-live continuous universe builder
- `universe-pit-1` 🔧 ready — Perp-only contract_type filter is conditional and silently no-ops if the column is absent
- `universe-pit-3` 🔧 ready — instruments x tickers inner-join silently drops symbols present on only one side
- `universe-pit-5` 📋 review — Symbols with null launch_time_ms pass the universe in unlimited (match-backtest) mode with no age gate

**w4-w5-stages** (3)
- `w4-w5-stages-5` 🔧 ready — Stage-0 prereg rejection-reason taxonomy lists btc_trend before btc_trend_unknown; engine emits the reverse
- `w4-w5-stages-3` 🔧 ready — A0/Stage-0 equity reconciliation treats NaN diffs (incl. NaN-vs-finite) as 'close'
- `w4-w5-stages-6` 🔧 ready — W4 Stage-3 path-shape IC/spread is measured in-sample on executed trades only (survivorship) — correctly fenced, but the fence i

**ws-pool** (6)
- `ws-pool-4` 🔧 ready — on_bar callback closure binds self._on_bar at creation time; a later subscribe() with a different callback silently keeps routin
- `ws-pool-1` 📋 review — Universe-refresh bootstrap ignores shutdown: stop() can leave a detached REST worker pool running for up to bootstrap_timeout_se
- `ws-pool-2` 🔧 ready — newest_ts_ms / _global_max_ts_ms is never recomputed after keep_only_symbols trims the max-holding symbol, so it can over-report
- `ws-pool-3` 📋 review — pybit ping-timer monkeypatch is redundant for the installed pybit 5.16.0 and couples _close_ws_client to a patched attribute
- `ws-pool-5` 🔧 ready — stop() can run the final flush_to_disk concurrently with an un-joined flush thread when stop_flush_thread's join times out
- `ws-pool-6` 🔧 ready — Follower snapshot signature TOCTOU: _snapshot_age_seconds and the recover read can observe different generations of the leader f

**ws-risk** (2)
- `ws-risk-8` 🔧 ready — Cold-start adoption can make a blocking get_wallet_balance REST call on the latency-critical consumer thread
- `ws-risk-7` 🔧 ready — _prune_closed_order_state can evict an OPEN sibling-sleeve order's link when that sleeve's ledger read failed this pass

## Systemic patterns
### Wave 1
- **Same-day look-ahead in regime/context gates: the market_min_ret_1d gate reads the entry day's full close-to-close return while the sibling BTC-trend gate is car** — Lag the market gate to the prior completed day (or rebuild _market_daily_returns to exclude the current day, mirroring _btc_trend_returns), and add a poison-future PIT test that fails if the gate ever
- **Gap-blind positional shift/row-rolling instead of calendar-exact windows: multiple N-day feature builders and the beta window use shift(n)/rolling(window_size=n** — Route every fixed-horizon feature through _common.calendar_shift / calendar_roll (numerically identical on contiguous data, null/shrink across gaps); gate with np.allclose on the contiguous case. This
- **Live execution path silently diverges from the backtest/forward-evidence path that validates it (same-code-illusion). Multiple sleeves run a different profile/h** — Establish a live-vs-backtest parity harness as a first-class gate: for every sleeve, assert the deployed profile (weekend tilt, hedge mode, rank universe, age source, cost convention) is byte-for-byte
- **Cross-sleeve isolation on the shared one-way netted account is incomplete: the budget/cap/netting logic omits or mis-routes the continuous_addon sleeve and the ** — Treat continuous_addon as a first-class sleeve everywhere (add to VALID_SLEEVES, cap_qty_to_trade predicate = len(sleeve_routes)>1); never fall back to the raw netted position.size when cap_qty_to_tra
- **Latched-None on WS reconnect failure / single-call bootstrap permanently disables a safety stream with no retry and no operator signal.** — Build the replacement stream into a local and assign only on success; keep re-arming on subsequent on_idle passes instead of latching None. Track a separate private-event monotonic clock so the stale-
- **Order-submission idempotency gaps: WS-then-REST race, strict-WS, and crash windows can leave a live venue position with no ledger row (orphan), and duplicate-or** — Treat 110089/duplicate-link as idempotent success (re-probe and return the existing order); run _probe_existing_order even when rest_fallback=False; persist a pre-place intent row before place_order s
- **Two-or-more accounting conventions for the same headline metric (additive vs compounded equity/MAR/DD; ddof/calendar-fill Sharpe; gross-vs-net 'net_return'), pr** — Factor a single shared metrics helper (compounded equity, drawdown relative to peak, ddof=1, calendar-filled Sharpe, one annualization constant) and route reconciliation, continuous_events, trade_life
- **Diagnostic/observability layers fail by silently reporting OK (or crashing) rather than surfacing the fault they exist to catch: stale/empty data read as fresh,** — Make diagnostics fail loud: count a partition only if it holds parquet; wrap per-file parquet reads to flag corrupt partitions; assert rmom coverage reaches the kline window max on the backtest path (
- **Critical no-look-ahead / no-survivorship primitives and safety defaults have ZERO direct test coverage, so a regression that reintroduces fake alpha or arms ord** — Add focused regression tests for each non-negotiable invariant: gap behavior of calendar_shift/calendar_roll; volume_events_pit pre-listing/post-delisting exclusion and mid-gap requirement; per-day-va
- **Watchdog/monitoring config drifts from deploy config, producing guaranteed false CRITICAL pages or coverage gaps the moment a supported toggle is exercised — al** — Drive monitored-unit selection off the exact same sleeve predicates the deploy uses (shared helper mirroring continuous_rmom_refresh_on); separate resolved-note retry state from alert cooldown state; 
- **REST rate-limiting under-counts and the per-IP budget is not coordinated; unbounded cursor loops and missing Retry-After handling create hang/wasted-retry risk ** — Inject one shared rate limiter into each REST client so the limiter sees every paginated _get; give each bootstrap worker its own client (or lock the counters); add a max_pages bound + non-advancing-c

### Wave 2
- **Gates and falsifiers fail OPEN (vacuous PASS) when their inputs are absent or degenerate — the strongest cross-checks silently validate nothing**
- **Calendar time is silently collapsed to positional/row time, mislabeling horizons and inflating annualized metrics on a sparse panel**
- **Cross-source/cross-file dedup uses an UNSTABLE sort, re-opening the exact double-book/precedence class a sibling writer already hardened against**
- **Always-on collectors lose unbuyable forward data silently — failures are swallowed and the freshness watchdog only checks whole-root mtime**
- **In-sample selection over a large grid is presented as a verdict with no train/holdout, era split, or multiple-testing correction — including on the path that picked the LIVE demo config**
- **Append-only JSONL/parquet writers are non-atomic and unbounded — torn final lines, orphaned temps, no retention, no parent-dir fsync**
- **Critical correctness invariants are guarded only by docstring/prose convention, never enforced in code or pinned by a negative test**

## 🚩 Next-level changes (high-impact, for your decision)

### [transformational / large] Build a live-vs-backtest parity harness as a binding gate. For each sleeve, assert the deployed profile equals the profile the forward arbiter measures across every behavior-affecting dimension (weeke
Five+ confirmed same-code-illusion divergences (long-sleeve-1/2, pit-engine-2, sizing-rebalance-1/2, hedge-1) mean the Tier-3 forward arbiter is measuring a different strategy than the one running. This silently corrupts the single most important promotion decision and cannot be caught by existing tests. A parity gate converts the entire class from latent to impossible.

### [high / medium] Replace every positional shift(n)/rolling(window_size=n) fixed-horizon feature with the existing calendar_shift/calendar_roll primitives, and add the missing direct gap tests for those primitives plus
Gap-blindness (pit-signals-3/4, test-gaps-3) is a research-path fake-alpha class that feeds the ridge OOF score and residual_momentum, and the no-look-ahead/no-survivorship primitives that prevent it have zero coverage (test-gaps-1/2/4). One coordinated change eliminates the defect and pins the invariants so the whole 78-file suite finally guards the repo's central non-negotiab

### [high / medium] Centralize PnL/metric accounting into one shared, compounded, calendar-correct helper (equity, drawdown-vs-peak, ddof=1 calendar-filled Sharpe, single annualization constant, explicit gross/net cost c
The operator-facing forward report shows two different MARs/Sharpes/DDs for the same book (metrics-2/3/4/6, reconciliation-2) and emits invalid JSON Infinity on a no-drawdown window (metrics-1, persisted to disk in the data-clock audit). MAR is the named primary Tier-3 arbiter; an inconsistent or unparseable headline metric directly threatens decision validity.

### [high / medium] Harden the order/stream safety layer: treat duplicate-orderLinkId as idempotent success, probe-on-timeout in strict-WS, persist pre-place intent rows, add resting-order adoption, never latch a failed 
Idempotency/orphan gaps (exec-router-2/4, sniper-5) and latched-None stream failures (ws-risk-1/2/5) leave live positions unmanaged or the fast-stop feed silently dead — the exact failure modes that become money-relevant the instant the real-money toggle is used. Re-asserting demo mode at the signing layer (realmoney-safety-1/2) makes the highest-stakes invariant defense-in-dep

### [high / medium] Make continuous_addon a first-class sleeve across all cross-sleeve isolation logic and eliminate hedge/fade venue-netting drift: add it to VALID_SLEEVES and the cap_qty_to_trade predicate, forbid fall
The shared one-way netted account means an isolation hole (cross-sleeve-1, ws-risk-3/4, hedge-2, event-demo-core-3) lets one sleeve's stop or hedge resize flatten or mis-attribute a sibling's leg — a documented non-self-healing cross-sleeve over-close. Latent today because budget is off and BTC/ETH overlap is data-dependent, but it is the core risk once multiple sleeves run liv

### [high / small] Add a GitHub-side CI job (push + PR) running ruff + full pytest and gate the VPS auto-deploy on its success, instead of relying on a manually-installed local pre-push hook and a two-file on-VPS smoke 
deploy-ci-2: any test failure outside the two smoke files (exit-logic, reconciliation, cost/funding regression) deploys straight to the order-submitting live demo daemons with no server-side full-suite check, and --no-verify / a web edit / an uninstalled hook bypass the only gate entirely. This is the exact path a future fake-alpha or exit bug rides into the live book undetecte

### [medium / small] Make all diagnostics fail loud rather than silently OK/abort: count partitions only when they hold parquet, flag (not crash on) corrupt parquet in the coverage audit, assert backtest-path rmom coverag
pit-data-2/3/5, reconciliation-3/5 mean the layers that exist specifically to catch silent drift (stale manifest, corrupt data, truncated panel, divergent reconcile) themselves fail silently — an operator sees a green check or '✅ done' while a real fault hides. Restoring loud failure is cheap and directly protects research-evidence integrity.

### [medium / small] Sweep the dead code and stale safety-doc references: delete the orphaned _attach_residual_momentum / _filter_universe / _pending_order_refs, remove or wire the inert require_pit_membership flag, fix t
Dead code that advertises nonexistent PIT test coverage (code-quality-1, test-gaps-6) and an inert PIT flag (pit-data-1) are methodology traps in exactly the no-look-ahead area where the team has been burned; duplicated helpers (code-quality-5/6) are drift factories. Removing them shrinks the audit surface and prevents a future engineer from trusting phantom guarantees.

### [transformational / medium] Resolve the forward-evidence object-identity mismatch: make the forward signal clock track the SAME object the live demo book executes (BTC+ETH 2f hedge), or hard-stamp hedge_mode and forbid conflatio
The forward signal clock is the Tier-3-facing forward-evidence accrual, but it replays a BTC-only hedge while the live order book runs the banked BTC+ETH 2f object (forward-replay-1). A forward-readiness PASS on the BTC-only clock is not evidence for the strategy actually running; a promotion read could act on a number generated by a different hedge. This is precisely the same-

### [transformational / medium] Close the off-sleeve orphan-hedge safety hole: keep the hedge timer enabled (or page CRITICAL) while the hedge addon ledger holds an open, stopless leg, and amend deploy/sleeves.env off-semantics. Wir
Retiring the continuous sleeve is the documented, expected operator action, and sleeves.env promises open positions exit naturally with no orphan/flatten risk. That promise is FALSE for the hedge long specifically (deploy-env-timers-1): the risk service is contractually forbidden from exiting it (stop=0/tp=0/planned_exit=0), the timer is its only manager and is disabled --now o

### [high / medium] Build a shared fail-closed gate/coverage primitive and route every falsifier, reconciliation, and selection receipt through it (expected-count assertion, n_checked/n_expected stamping, fail-on-empty),
Five independent confirmed findings (w4-w5-stages-1/2, alpha-scripts-1, archive-integrity-1/2) are the same fail-open defect: a guard that PASSES when nothing was checked. These are the gates that decide whether later stages run, whether a cross-venue claim is real, and whether a canonical root is contaminated. A single primitive plus a 'gates must fail closed' CI invariant eli

### [high / medium] Add a byte-ingestion integrity layer for all archive/vision downloads: Content-Length assertion, .CHECKSUM (sha256) verification where published, post-download structural decompress check before atomi
The canonical full_pit roots are cited as working-dataset/OOS-validation evidence, yet a mid-stream truncation that ends on a CSV record boundary becomes a silently-thin kline day that the resume guard treats as covered and never re-fetches (archive-integrity-1), the Binance Vision .CHECKSUM is never verified (archive-integrity-2), and the size-only cache guard makes corruption

### [high / large] Adopt one calendar-grid utility for sparse-panel time series and route the N-day feature builders, forward_days/MAR, and forward Sharpe through it; pin with relist/gap fixtures. Mirror continuous_depl
Two findings (research-methodology-2, forward-replay-2/6) show calendar time collapsed to positional/row time on a sparse panel: feature horizons silently balloon across delist/relist gaps (feeding the ridge combiner OOF IC, the univariate IC survival rule, and the inverse-vol sizing denominator), and the forward clock's days/MAR/Sharpe mix gap-collapsed and calendar bases. The

### [high / small] Make the embargo<->forward-horizon causality invariant a hard RidgeCombinerConfig.__post_init__ error (parse N from target_col, require embargo_days >= N+1) and pin it with a multi-day-target leak tes
Walk-forward OOF rank-IC is the Tier-1 gate that decides whether the ridge combiner proceeds to engine-sizing wiring, and its entire causality argument is enforced by convention only (research-methodology-1/3). Today safety depends on the single accidental fact that the scout left target_col at fwd_ret_1d; the house-default horizon is fwd_ret_3d, so one config edit produces tex

### [high / medium] Harden the always-on collectors as a unit: fail-loud subscription error handling (wrap ws.send, close+reconnect on failure; surface success:false acks), per-cycle wall-clock budget + duration telemetr
Liquidation and depth history are explicitly unbuyable forward data, yet a mid-burst subscribe failure can leave the symbol tail unsubscribed for 24h (liquidation-collector-1/2), a region-quiet Binance leg cannot be alarmed (liquidation-collector-3), latency spikes silently degrade depth cadence (depth-collector-2), and transient backfill outages freeze as permanent empty marke

### [medium / medium] Establish a single durable-write convention (temp+replace+parent-dir fsync for rewrites; per-line+flush for append logs; startup .tmp sweep; tolerant readers) plus disk-space monitoring and retention,
Six findings span the same write-durability/resource gap: torn JSONL trailing lines (depth-collector-4, liquidation-collector-4), orphaned .tmp parts and missing parent-dir fsync on the order-recording path (storage-concurrency-4/5), no retention on two unbounded writers sharing a 4 GB box (depth-collector-3), and a non-atomic funding cache that wedges reruns (backfill-writers-

### [medium / medium] Stand up a split-stability selection harness (early/recent era + block-bootstrap p5, ranking on the worse case) as the required path for any config/threshold selection, retrofit the ensemble scout and
The live demo book's weights and risk rule were chosen from a full-sample pooled-MAR ranking over a large weight x risk grid with no holdout (alpha-scripts-2), and sibling diagnostics pick thresholds in-sample (alpha-scripts-6) — house-rules #17/#18/#19 on the path feeding the promotion read. The demo-forward arbiter is the saving grace, but research evidence quality is materia

### [medium / small] Add the missing critical-invariant tests and code guards: W5 same-code reconciliation (selected tape == executed trades, fcfs==no-reorder, composite preserves trade SET when capacity non-binding, reje
The W5 candidate_sink/_apply_entry_order machinery is the same-code audit foundation of the entire W5 program (errors #16) yet has zero positive tests (w4-w5-stages-4); the perpetuals-only PIT/lifecycle invariant (errors #12) has no negative test and a soft column guard (universe-pit-1/2); the Stage-1 wiring-sanity precondition is computed but unenforced (w4-w5-stages-2). These

## Still unaudited (completeness critics)
### Areas neither wave covered
- liquidity_migration/continuous_hedge_manager.py — the once-daily BTC/ETH 2f hedge sizing + submission path (HedgeDecision/HedgeDecision2F, build_hedge_tracking_row with stop/TP/planned-exit all zeroed
- liquidity_migration/continuous_rebalance.py — 15 functions including plan_continuous_hedge_resize, compute_continuous_hedge_ratio(s_2f); the pure sizing twin the hedge manager depends on. Tests exist 
- deploy/.env secret hygiene — live Bybit DEMO API key/secret AND a Telegram bot token are stored in PLAINTEXT in deploy/.env (gitignored, so not leaked to git, but on-disk and presumably shipped to the
- deploy/lib_sleeves.sh + deploy/sleeves.env kill-switch — the single source of truth for which sleeves run; deploy enables 'on' and `disable --now`s 'off'. The off-sleeve safety (open positions ride to
- scripts/reconcile.py + reconciliation.py demo↔paper slippage reconciler — the operator's daily integrity check. Tests exist for reconciliation modules, but the orchestration in scripts/reconcile.py (r
- cross_sleeve.py (520 lines) — combined-book reporting across sleeves, driven by the combined-book-report systemd timer. Has a test file but the cross-sleeve netting/aggregation correctness vs the per-
- requirements.txt vs pyproject.toml dependency-source-of-truth — requirements.txt is a 'legacy convenience' mirror that 'previously drifted silently'; nothing installs from it (VPS uses `pip install -e
- scripts/equity_curves.py continuous reconstruction — it rebuilds continuous from DEFAULT_CONTINUOUS_FROZEN_FALLBACK (~/SHARED_DATA/continuous_deployed_equity_refresh_2026-06-12) rather than the live d

### Recommended follow-ups
- Add a test suite for .claude/mcp/research_server.py: (a) assert its hardcoded DATA_ROOTS matches the roots enumerated in docs/data_roots.md (catch the missing continuous-hedge-event root and any futur
- Treat the deploy gate as a first-class audit target: either add a GitHub Actions job that runs `ruff + pytest` on push to main BEFORE vps-deploy.yml deploys (server-side enforcement), or make vps-depl
- Run a doc/skill consistency pass: fix the equity-curve SKILL.md 'de-promoted 2026-06-05 / not part of the equity tool' claim to match STATE.md + scripts/equity_curves.py (continuous IS chartable and l
- Add a pre-push or CI step that runs `graphify update .` (AST-only, no API cost) and fails if GRAPH_REPORT.md is dirty, so the mandated architecture entry point can't lag the code; document that graph.
- Backfill direct unit tests for the execution-critical untested modules, prioritizing order_link_id.py (encode/decode round-trip across short/long/continuous/addon prefixes, the ca-vs-c collision const
- Pin the cross-module hedge safety invariant with a test: a row from build_hedge_tracking_row (stop/TP/planned-exit = 0, sleeve='continuous_addon') must be ADOPTED by ws_risk.plan_risk_exits and NEVER 
- Verify (and document the verification of) that scripts/equity_curves.py's frozen continuous fallback root still reproduces the live continuous_ensemble_v1 weights, or switch it to derive from liquidit
- Audit deploy/.env handling: confirm the Telegram bot token scope is notify-only, establish a rotation note for the in-repo-tree plaintext demo credentials, and add a test asserting telegram.py exposes

---
*Full evidence + proposed fix for every id: the two workflow result JSONs in this session's task outputs.*
