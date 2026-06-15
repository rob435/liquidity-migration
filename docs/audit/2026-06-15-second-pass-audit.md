# Second-Pass Full-System Audit — liquidity-migration

**Date:** 2026-06-15 · **Method:** multi-agent adversarial audit (deep, second-order) +
two guard-railed fix workflows + hand-applied package/deploy fixes.

This pass FOLLOWS the 2026-06-14 full-system audit (committed `7d39d61`, 200 findings
remediated). Its charter was deliberately second-order: find what the first pass *missed*
or *broke*, **verify the prior fixes are correct**, and apply only robust fixes — flagging
anything that shifts a promoted/research number or live-execution behavior for operator
sign-off (per AGENTS.md pre-registration governance).

## Method

1. **Audit fan-out** — 30 subsystem units (package + scripts + deploy + test-integrity),
   one+ agent each, every finding adversarially verified by an independent refute-by-default
   skeptic (61 agents total). 2 units re-run after socket failures (`bybit-core`,
   `continuous-addon-shadow`).
2. **Triage** — confirmed findings hand-classified into *fix_now* (correctness / robustness /
   crash / doc, numerically equivalent on the happy path) vs *flag_operator* (shifts a
   promoted/research-reported number, live order/sizing semantics, or a gate — needs
   pre-registration).
3. **Remediation** — methodology-critical package + deploy fixes applied by hand; independent
   mechanical script/telegram fixes applied via two file-disjoint fix workflows. Every fix
   carries a regression test that fails on the old code and pins the corrected edge while
   asserting the happy path is unchanged.

## Gate

- **Baseline (start):** ruff clean · pytest **1792 passed** · working tree clean.
- **After the safe-fix pass:** ruff clean · pytest **1914 passed** (+122 regression tests).
- **After the operator-override round (below):** ruff clean · pytest **1955 passed**.

> **UPDATE — operator override (2026-06-15).** The operator explicitly authorized fixing the
> flagged (number/behavior-changing) items and waived the pre-registration gate for this change.
> All flagged items below were then fixed **except `B1`** (bybit `_paged_time_range`), which was
> deliberately left strict — see its row. Each behavior-changing fix carries a regression test
> pinning the new behavior and a measured delta; the deltas are summarized in
> "Flagged items — fixed under operator override" below. Gate stays green (ruff clean, 1955
> passed). The change is staged on a branch for a reviewed merge (merge to `main` triggers the
> live-VPS auto-deploy).

## Verified severity (this pass)

0 critical · 2 high · 9 medium · 10 low confirmed (+6 from the two re-audits). 6 findings were
adversarially **refuted** and dropped.

---

## Fixed this pass (gate-green, working tree only)

### Methodology-correctness (package)
| id | file | what changed |
|---|---|---|
| `[0]` dyn-exit look-ahead | `continuous_dynexit_shadow.py` | dyn_tp scan now capped at the real exit time ("whichever first") — enforces the frozen pre-registration; a target touched **after** a real disaster-stop no longer counts as a dyn_tp win (+2 tests) |
| `[11]` panel cache key | `continuous_events.py` | cache key now folds in `exclude_symbols`; two configs differing only in the exclusion set no longer collide on one cached panel (empty-exclude/live filename byte-identical) (+2 tests) |
| `[12]` ridge embargo leak | `ridge_combiner.py` | embargo≥horizon+1 leak guard decoupled from the `fwd_ret_Nd` target name: explicit `forward_horizon` field, and a forward-returnish unparseable name is now a hard error instead of a silent skip (+4 tests) |
| `[16]` false cross-venue pass | `continuous_ensemble_rebalance_scout.py` | `*_both` flags now require `n_venues == len(VENUES)`; a single-venue run can no longer claim a both-venue pass (+test) |
| `[17]` warm-start gap | `regenerate_hedge_warmstart.py` | BTC/ETH daily returns now gap-guarded (emit only on calendar-consecutive days), matching the orchestrator twin; identical on contiguous data (+test) |
| `[10]` bookdepth transient/404 | `backfill_binance_bookdepth_vision.py` | transient fetch exhaustion now distinguished from a genuine 404 (sentinel + incomplete manifest + resume re-fetch) — no more silent permanent data gaps (+9 tests) |
| `[19]/[20]` funding holes | `backfill_binance_funding_vision.py` | in-progress month no longer cached as a permanent 404; fapi top-up now paginates past 1000 settlements (+tests) |
| signal-harness doc | `signal_harness.py` | docstrings corrected: cross-sectional ranks are average-tie fractional (matches the verified impl), not "dense" |
| `[14]` forward MAR comment | `continuous_forward_replay.py` | corrected the misleading claim that `years` matches `deployed_equity` — documents the deliberate no-`+1` divergence and warns against the number-shifting "fix" |

### Robustness / crash / diagnostics
| id | file | what changed |
|---|---|---|
| `[8]` inverted MAR gate | `reconciliation.py` | a zero-drawdown continuous book (`mar=None`, the BEST drawdown case) no longer trips a false "MAR ≤ daily" alarm (extracted `_continuous_beats_daily_mar`; +3 tests) |
| `B2` rate-limit classify | `bybit.py` | `_is_rate_limit` keys off structured `retCode`/`retMsg`, not a `str()` of the whole payload (an orderId containing "10006" no longer false-positives as a throttle) (+tests) |
| `B3` WS submit guard | `bybit.py` | `BybitWebSocketTradeClient` now carries the REST client's demo-only `_assert_submit_allowed` guard (defense in depth; demo path unaffected, real-money WS fails closed to REST) (+3 tests) |
| `addon-F1` | `continuous_addon_shadow.py` | unmatched-attempt CSV emitter now applies the shadows-4 `trade_id` backstop the summary uses — no more phantom-orphan rows contradicting the gate |
| `addon-F2` | `continuous_addon_shadow.py` | a historical blend CSV missing `blend_source` now raises instead of silently counting every trade as both legs (drift gate fed garbage) |
| telegram | `telegram.py` | Retry-After `nan`→`sleep(nan)` ValueError, NaN `$nan`/`nan%` formatting, leaked 429 HTTPError fp, and a false docstring contract all fixed (+14 tests) |
| cli telegram crash | `cli.py` | combined-book report wraps the (by-contract propagating) telegram send so a transient outage exits non-zero cleanly instead of crashing the timer service |
| `[9]` FC sweep tag | `long_native_sweep_fc_min_day.py` | sweep run-dir tag derived from the exact value (no banker's-round collision/clobber under `--skip-existing`) (+test) |
| equity crashes | `equity_curves.py`, `continuous_deployed_equity.py` | Feb-29 start-date crash and no-drawdown `ZeroDivisionError` (`mar=None`) fixed |
| decision-rule crash | `apply_decision_rule.py`, `alpha_sweep.py` | `window_days=0` row no longer crashes the investigation rule; block-bootstrap final-observation off-by-one fixed |

### Deploy
| id | file | what changed |
|---|---|---|
| `[1]` paper rmom regression | `run_continuous_rmom_refresh.sh` | **fix-regression from `7d39d61`**: the paper unit dropped `KLINES_FOLLOW_ROOT` (streams its own klines) but the refresh still only rebuilt the demo root's rmom gate → the paper book read a gate nothing builds → **emitted zero entries forever** and the paper↔demo cost reconcile had nothing to pair. Refresh is now sleeve-aware and rebuilds each on sleeve's own root. Two stale tests that *locked in the regression* were corrected. **Requires a deploy to take effect.** |

### Tier-A second wave (file-disjoint safe fixes, verify-first + equivalence-gated)
| file | what changed |
|---|---|
| `binance_vision.py` | a valid-but-empty Vision month no longer counts as a hard download failure (`fetch_month_klines` returns `None` only on real failure) — stops a spurious "survivorship-biased" abort against `max_failure_ratio` |
| `cli.py` | `discover-universe` + four `archive-*` commands now print the on-disk **slug** path (`safe_name`), not the raw `--name` (5 wrong-path bugs) |
| `config.py` | `load_config` now warns on an unconsumed top-level YAML block (e.g. the dropped `trade_flow`); `_merge_dataclass` applies the numeric coercion `UniverseConfig` already had (kills a latent quoted-number string-arithmetic crash) |
| `downloaders.py` | taker-flow imbalance guards NaN/inf/negative volumes before the ratio |
| `ingestion.py` | densify multi-symbol recursion now seeds each symbol with **its own** prior close (was reusing one initial price for all — data integrity) |
| `kline_stream_manager.py` | `universe_refresh_errors` no longer double-counted on the default-fetcher empty path |
| `long_native_event_demo.py` | cycle telemetry `entries_parallel_workers` now reports the actual (sequential) worker count instead of phantom parallelism |
| `volume_events_charts.py` | `_strategy_equity_series` guards the `ts_ms` column before sorting on it (no `KeyError` on a date-only frame) |
| `build_full_pit_bybit.sh` | `N_SYMBOLS` counts an empty list as 0, not 1 (build-log accuracy) |
| `build_legacy_archive_manifest.py` | corrected a misleading "verified" comment to match the assume-correct behavior |
| `check_demo_liveness.py` | cooldown state-file no longer falls back to a CWD-relative path when both roots are skipped |
| `verify_full_pit_rebuild.sh` | gate-7 ruff fallback no longer masks a real lint failure |

Adversarial discipline held: the `kline_store.py` follower-freshness item was verified **not a
defect** (already guarded) and left untouched; the `binance_vision` Content-Length floor was
**flagged** (a structural fix contradicts a frozen test and risks dropping legitimately-small
months) — see below.

---

## Flagged items — FIXED under operator override (2026-06-15)

The operator authorized these. Each now has a regression test pinning the new behavior:

| id | file | fix + measured delta |
|---|---|---|
| `[3]` long IM guard | `long_native_event_demo.py` | worst-case per-position notional now includes the weekend 1.5× tilt + max vol-parity weight. Promoted-config 4× projection 0.50 → **0.75** (now correctly REJECTS the IM-ceiling breach); strictly more conservative |
| `[4]`/`[5]` orphan-close | `event_demo_exits.py` | leg selection: provably-ours (exit_order_id) → exact-size-to-qty match → latest-first. Fixes mis-attribution of a sibling's *earlier* close while preserving the sibling's-*later* case (b10) — both symmetric cases now correct |
| `[2]` decile null-vol | `signal_harness.py` | names with null/≤0 realized_vol excluded from the rank pool before decile-k → every selected name is sizable (no silent 0-gross side) |
| OI/premium gap-edge | `signal_harness.py` | daily OI/premium/funding aggregation key snapped to the 00:00 grid → joins the kline grid on gap-edge days (no silently-null features) |
| `[13]` oi_chg7 + guard | `long_native.py` | metrics frame day-snapped before `calendar_shift`+join (oi_chg7_m now populates); guard requires all read columns (no KeyError) |
| sniper window boundary | `long_native.py` | a fired sniper-retrace entry past `config.end_date` is now refused, like every other entry path |
| market gate | `continuous_events.py` | `_market_daily_returns` now gap-aware (`calendar_shift`); pad_back reserves ≥2 warmup days so the gate doesn't fail-open the first ~2 days |
| recon fee | `reconciliation.py` | `_fee_adjusted_return` notional-weights gross before subtracting fees (consistent basis) |
| report mark | `event_demo.py` | `build_position_pnl_snapshot` no longer marks at the liquidation price (markPrice→lastPrice→avgPrice, else null) |
| equity window | `equity_curves.py` | `--sleeves continuous` defaults to the frozen deployed start (rolling start only on explicit `--start`) |
| `[15]` sparse 1m | `downloaders.py` | CLI `archive_klines_1m` path now densifies + prior-close-seeds, matching the canonical PIT builder |
| addon gross/net | `continuous_addon_shadow.py` | cooldown skipped-trade return cost-corrected (fees/funding) on live rows |
| Content-Length floor | `binance_vision.py` | `_verify_download` requires a valid zip when both checksum + length are absent (frozen b14 test updated) |
| hedge telemetry | `scripts/run_continuous_hedge.py` | `_warmstart_last_date` uses max-not-last; `hedge_mode` reflects the engine's btc fallback. (the `use_2f` gate is a harmless coarse pre-filter — the engine already backstops thin joint windows) |
| ws_risk | `ws_risk.py` | telegram dedupe un-record moved off the sender thread to the consumer (no off-thread state mutation / file race); public-WS watchdog built-timestamp anchored at actual subscription |

### Still deferred (intentionally)

| id | file | why NOT changed even under override |
|---|---|---|
| `B1` paged young-symbol | `bybit.py` | `_paged_time_range` fails loud on an empty page after a full page. The "fix" (suppressing the raise for a young symbol) risks **silently truncating a real mid-range data hole** — a methodology-correctness violation worse than the rare loud false-positive (history exactly one page). Failing loud on ambiguity is the safe choice; loosen only with a dedicated receipt. |

## Original flagged record (pre-override)

The items below were the original "do not change unilaterally" set; all except `B1` are now fixed
above. Retained for the audit trail.

| id | file | why flagged |
|---|---|---|
| `[2]` decile under-deploy | `signal_harness.py` | a selected name with null/zero `realized_vol` is tagged short/long but sized 0 → the side silently under-deploys; fixing changes deployment sizing |
| `[3]` long IM guard | `long_native_event_demo.py` | projected-margin safety guard ignores the weekend 1.5× tilt + vol-parity weight → can approve a levered config that breaches the 50% IM ceiling; hardening changes the guard's accept/reject (LONG sleeve currently off) |
| `[4]`+`[5]` orphan-close mis-attribution | `event_demo_exits.py` | earliest-first leg capping can attribute a sibling sleeve's earlier same-side close to this trade → wrong reconstructed PnL on a shared account; needs the test `[5]` + a selection-heuristic change |
| `[13]` oi_chg7 un-snapped | `long_native.py` | `calendar_shift` on a sub-daily metrics grid silently nulls `oi_chg7_m` (join miss); fixing populates an off-by-default research feature (changes its numbers) |
| `[18]` equity window | `equity_curves.py` | `--sleeves continuous` overrides the frozen start to today−3y, diverging from the deployed-window reconstruction the tool claims |
| `addon-F3` gross/net | `continuous_addon_shadow.py` | cooldown "skipped trade return" reports gross-of-cost on the live ledger; the right cost basis (gross vs net live/backtest) is a known repo-wide judgment call |
| `B1` paged young-symbol | `bybit.py` | `_paged_time_range` raises a spurious failure for a newly-listed symbol whose history is exactly one page → wedges the symbol's fetch; the disambiguation touches PIT-root ingestion |
| `[15]` sparse 1m CLI path | `downloaders.py` | the legacy CLI `archive_klines_1m` path writes sparse (non-densified, no prior-close seed) 1m bars diverging from the canonical PIT builder |
| market gate warmup | `continuous_events.py` | `market_min_ret_1d` fails open the first ~2 days (no pad_back); `_market_daily_returns` uses a gap-blind `shift(1)` — both change a research gate computation (gate off by default) |
| OI/premium gap-edge | `signal_harness.py` | daily OI/premium aggregation can drop a day's features on a gap-edge; `ts_ms=min(intraday)` is fragile vs the 00:00 grid — research feature value changes |
| sniper window boundary | `long_native.py` | the sniper-retrace "fired" entry skips the research-window boundary check every other entry path enforces → can change which entries fire (research numbers) |
| metrics-block guard | `long_native.py` | the block guards on `global_lsr` but reads `oi`/`toptrader_lsr`/`taker_lsr` unconditionally — hardening risks moving a crash downstream; needs a coherent missing-column path |
| fee-adjusted return | `reconciliation.py` | `_fee_adjusted_return` uses raw gross return without notional weighting when `net_return` is absent → changes a reported return |
| report mark fallback | `event_demo.py` | `build_position_pnl_snapshot` falls back to liquidation price as the mark → wrong reported PnL snapshot |
| ws_risk watchdog/thread | `ws_risk.py` | public-WS watchdog `built` timestamp set at `__init__` (not at construction); telegram-sender thread mutates consumer-only state + races the dedupe file — delicate concurrency in the order-submitting daemon |
| hedge use_2f / staleness | `scripts/run_continuous_hedge.py` | `use_2f` gate counts full-series ETH obs (not the windowed/joint count the beta uses); `_warmstart_last_date` keeps last-in-file not max — both can change live hedge gating/paging |
| Content-Length floor | `binance_vision.py` | `_verify_download` is a no-op when both the `.CHECKSUM` sidecar and Content-Length are absent. Ready patch (require a valid non-empty zip) **contradicts the frozen `test_audit_fix_b14` line-114 assertion** and risks rejecting legitimately-small months → operator must approve updating that test (already backstopped by `parse_month_csv`'s `BadZipFile`) |

Plus a tail of lower-confidence `flag_operator` notes from the unverified-low set (funding-interval
hardcode 480, Bybit funding-interval never populated, cross-ledger gross/net `net_return`
convention divergence, store fast-path truncated-history return) — left for operator triage.

## Refuted (adversarially dropped)

- WS order-upsert tombstone "resurrection" (`ws_state_cache.py`) — guarded.
- `long_native` per-symbol weight cap "bypassed" by `notional_multiplier` — invariant holds.
- Binance Vision microsecond `open_time` "truncates OOS root at 2024-12" — not reproduced.
- Continuous paper unit "missing `CONTINUOUS_SNIPER=1`" — by design (paper is a base-book shadow).
- `set -e` masking in `build_full_pit_{binance,bybit}.sh` (×2) — guards fire correctly.

## Governance note

Methodology-correctness fixes (look-ahead, leakage, false cross-venue, data-integrity) were
treated as bugs and fixed. Anything that would move a promoted/frozen/research-reported number or
live order/sizing behavior was **flagged, not changed** — consistent with the AGENTS.md
parameter pre-registration rule and the strict Tier-3 promotion gate. The real-money toggle path
(`bybit.resolve_private_credentials` / `validate_order_submit_allowed`) was independently
re-verified as fail-safe and is unchanged.
