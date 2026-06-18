# Continuous Audit Log

This is the durable ledger for the **perpetual repo-wide audit loop** (operator
mandate 2026-06-18: "repo wide multi agent audits continuously … constantly
iterating and making this repo better"). Each loop iteration appends an entry
here. Read the most recent iteration first; it is the live state of the cleanup
campaign.

## How the loop works

Each iteration:
1. **Baseline** — `ruff check` + `pytest -q` must be green before and after.
2. **Audit** — a multi-agent workflow fans finders across module clusters, then
   adversarially verifies each finding by re-reading the code. Only confirmed,
   line-referenced correctness/logic/safety defects survive.
3. **Fix** — high-confidence, low-risk confirmed findings are fixed in-tree.
   Logic/strategy changes are written up in
   [`docs/strategy_improvements.md`](../strategy_improvements.md), never applied
   silently.
4. **Document** — findings, fixes, and deferrals recorded below; limitations
   captured in [`docs/limitations.md`](../limitations.md).
5. **Verify** — `ruff` + `pytest` green; commit locally.
6. **Schedule** — the loop re-arms and continues.

### Standing guardrails (self-imposed)

- **No `git push`.** Pushing auto-deploys to the live VPS demo host. Commits are
  local-only until the operator pushes. Every iteration says so explicitly.
- **No `REAL_MONEY=true`.** Never, without explicit owner instruction.
- **Methodology gates are not "cleanup" targets.** PIT / no-look-ahead /
  no-survivorship and the three-tier real-money promotion gate are correctness
  invariants — they get *tightened*, never loosened, by this loop.
- **Strategy changes are proposals, not silent edits.** Anything that changes
  which trades the system takes goes to `docs/strategy_improvements.md` for the
  operator, not into the live config.

---

## Iteration index

| # | Date | Confirmed | Fixed | Deferred | Notes |
|---|------|-----------|-------|----------|-------|
| 1 | 2026-06-18 | 22 | 13 | 9 | repo-wide audit (8 clusters, adversarial verify); 1 HIGH fixed; reconcile collapsed to one command; harness-cli cluster pending re-run (rate-limited) |
| 2 | 2026-06-18 | 14 | 11 | 3 | harness-cli (rerun) + 27 modules iter1 missed; 2 HIGH fixed (execId double-count, combined-signal null-poison); +7 regression tests |
| 3 | 2026-06-18 | 39 | 8 | 31 | remaining scripts + DEEP pass on 5 giant modules; 3 HIGH fixed (incl. a fix-interaction with iter-1); 1 reverted (test-pinned audit2b conflict); large LOW backlog queued for iter-4 |

---

## Iteration 1 — 2026-06-18

**Baseline:** `ruff check` clean; `pytest -q` → 1969 passed in ~44s.

**Starting state:** uncommitted in-progress work building the three-way
reconciliation (`scripts/reconcile_three_way.{py,sh}`, `storage.since_date`,
`long_native` window-read support, plus tests). Goal of this loop: make the
whole reconciliation a single seamless command and harden the codebase.

**Audit:** repo-wide multi-agent correctness audit across 8 module clusters
(continuous, long, ws-stream, data-io, event-demo, archive-recon, harness-cli,
scripts) with adversarial per-finding verification. 32 raw findings → **22
confirmed**, 10 rejected. Severity (post-verify): 1 high, 6 medium, 15 low.
The `harness-cli` finder (signal_harness, cli, cli_parsers, cross_sleeve) was
**rate-limited and did not run** — re-queued for iteration 2.

### Fixed this iteration (13, all with `ruff`+`pytest` green; +6 regression tests)

| Finding | File | Sev | Fix |
|---|---|---|---|
| continuous-1 | `continuous_demo.py` | **high** | Gate the daily-rebalance resize on `not (errors and submit_orders)` so live resizes never size off the $10k fallback equity during a wallet-read outage |
| event-demo-1 | `trade_lifecycle.py` | med | `_daily_sharpe` now forward-fills on TRUE calendar days (reuse `_daily_equity_values`); the old intraday-stamped grid distorted `sharpe_like`, which feeds the three-tier decision rule |
| scripts-1 | `r1_robustness.py` | med | `_engine_mar` guards `OverflowError` (degenerate zero-span window) → nan instead of crashing the multi-venue run |
| scripts-2 | `r1_robustness.py` | med | `_load_monthly` returns `None` on a malformed/truncated CSV; callers skip the cell instead of crashing |
| scripts-3 | `apply_decision_rule.py` | low | `compute_mar` guards `OverflowError` for a tiny-but-positive window |
| scripts-4 | `reconcile_three_way.py` | low | `_coverage_ends` resolves funding via `resolve_dataset_name` (sees `binance_usdm_funding` on a Binance root) |
| scripts-7 | `reconcile_three_way.py` | low | filtered-through marker so an interrupted filter step isn't silently skipped (was a `pit_membership_fail` trap) |
| long-2 | `long_native.py` | low | `cl24` flat-range guard (NaN→0.5), matching the daily close-location features |
| long-3 | `long_native_event_demo.py` | low | median-universe re-selection targets the latest CLOSED bar, not a future-stamped partial-day bar |
| ws-1 | `ws_risk.py` | low | `on_ticker_message` now asserts the consumer thread (WS-R-002), like every other handler |
| ws-2 | `ws_risk.py` | low | adopted-trade `createdTime` parsed via `int(_float(...))` so a float-formatted venue ms doesn't date it to "now" |
| event-demo-3 | `event_demo_exits.py` | low | exit weighted-avg price averages over PRICED qty (both split-exit paths), so an unpriced leg can't drag it toward zero |
| archive-recon-2 | `reconciliation.py` | low | `_fee_adjusted_return` skips an undirected row instead of defaulting side to "short" (avoids sign-flipping a long) |
| data-io-2 | `data_layer.py` | low | suppress estimated `bar_coverage` (the 24-factor cancels → not a real measurement) |
| data-io-3 | `bybit.py` | low | documented the accepted single-writer lock-free counter design (no hot-path lock) |

(15 rows: 13 distinct findings + the two extra `event_demo_exits` split-path and
`bybit` doc edits.) Regression tests added: calendar-day Sharpe, `_engine_mar`
overflow, `_load_monthly` malformed-CSV, `compute_mar` overflow, `_fee_adjusted_return`
undirected-row, median-universe future-bar.

### Seamless reconciliation (the "one script" mandate)

Collapsed the two-script split into a single front door:
`bash scripts/reconcile.sh` now runs the **full demo↔backtest↔paper three-way for
both sleeves by default** (the whole reconciliation in one run); `--quick` is the
fast paper↔demo execution check. `scripts/reconcile_three_way.sh` is a back-compat
alias. Updated `docs/pit_gate.md`, `docs/data_roots.md`, the `.claude` pit-reconcile
+ run-strategy skills. **Follow-up:** `.codex/skills/pit-reconcile` (Codex-owned)
still describes `reconcile.sh` as paper↔demo-only — needs a Codex-side sync.

### Deferred (9 — design decision / pre-registration / Codex-owned)

Written up with fix plans:
- **strategy_improvements.md:** event-demo-2 (universe-shrink fail-open, MED),
  long-1 (provisional calendar-ops, MED, needs pre-reg + numeric-equiv test),
  long-4 (live per-symbol cap parity, LOW).
- **limitations.md:** data-io-1 (binance funding interval), archive-recon-1
  (windowed-ledger >6mo open trade), event-demo-4 (orphan-close leg-set),
  continuous-2 (live_state wasted compute).
- **Codex sync:** `.codex/skills/pit-reconcile` description.

### Guardrail honored

All changes committed **locally only — no `git push`** (push auto-deploys to the
live VPS). `REAL_MONEY` untouched. No methodology/PIT gate loosened; the
filtered-through marker and the universe-shrink proposal *tighten* correctness.

---

## Iteration 2 — 2026-06-18

**Baseline:** `ruff` clean; `pytest -q` → 1975 passed (carried iter-1 fixes).

**Audit:** the rate-limited `harness-cli` cluster (rerun) + the 27 package modules
iteration 1 did not reach, across 7 clusters (harness-cli, risk-factor,
hedge-regime, collectors, ingestion-pit, reports-exec, core-config) with
adversarial per-finding verification. 26 raw → **14 confirmed** (2 high, 3 medium,
9 low), 12 rejected.

### Fixed this iteration (11; +7 regression tests; 1975 → 1982)

| Finding | File | Sev | Fix |
|---|---|---|---|
| reports-exec-1 | `execution_router.py` | **high** | Dedup WS execution rows by `execId` (per-link seen-set, cleared with the bucket) so a redelivered fill isn't double-counted into qty/fee/avg-price |
| harness-cli-1 | `signal_harness.py` | **high** | Combined signal: a missing feature Z contributes neutrally (`sum_horizontal`/`fill_null(0)`) instead of nulling the whole name out of both pools; all-null names still excluded |
| ingestion-pit-1 | `ingestion.py` | med | `normalize_funding_history` clamps a 0/negative `funding_interval_min` (was `480/0 = inf` / sign-flip) |
| core-config-1 | `config.py` | med | Strict bool coercer (`bool("false")` was `True`, silently flipping a quoted YAML bool) |
| harness-cli-2 | `signal_harness.py` | low | Sub-period sign-consistency: reject under-powered splits (NaN slices) + treat 0.0 sub-IC as failing |
| risk-factor-2 | `risk_model.py` | low | `fit_factor_returns` suppresses per-factor slopes on a rank-deficient day (lstsq min-norm garbage); residuals kept |
| risk-factor-3 | `universe.py` | low | Report renders missing fields as `n/a`, not `0` (`or 0` masked None) |
| collectors-1 | `depth_collector.py` | low | `band_notionals` rejects a crossed/locked book (was a bogus in-spread mid) |
| ingestion-pit-3 | `ingestion.py` | low | OHLC aggregation adds a `price` tie-break so id-less same-instant fills give deterministic open/close |
| reports-exec-2 | `volume_events_charts.py` | low | `_chart_final_values` no longer assumes sorted points (scan-all, max-day ≤ common_end) |
| core-config-2 | `config.py` | low | `DEFAULT_RESEARCH_DATA_ROOT` expanded at definition (no stray `~`) |

Regression tests added: execId dedup (+ clear reset), combined-signal partial-feature
keep/all-null-exclude, funding interval clamp, strict bool coercion, data-root
expansion, crossed-book rejection.

### Deferred (3 — methodology-gate / pre-registration / defensive)

- **strategy_improvements.md:** ingestion-pit-2 (full-PIT per-symbol kline-lag gate,
  MED) + ingestion-pit-4 (required/covered bar-count asymmetry, LOW) — methodology
  gate, needs pre-reg + fail-vs-flag policy; combined-signal magnitude-normalization
  refinement (a-vs-b).
- **limitations.md:** risk-factor-1 (`decompose_strategy_pnl` missing-factor→0.0
  defensive gap; active trigger refuted as unreachable today).

### Guardrail honored

Local commit only — **no `git push`**. `REAL_MONEY` untouched. No methodology/PIT
gate loosened (the deferred PIT-gate change would *tighten* it; left for pre-reg).

---

## Iteration 3 — 2026-06-18

**Baseline:** `ruff` clean; `pytest -q` → 1982 passed (carried iter-1/2 fixes).

**Audit:** the 19 remaining `scripts/` (excluding the operator's concurrent
`research_residualization_*` workstream) + a DEEP second pass on the five
2,000–3,700-line modules a single finder under-covered (continuous_demo, ws_risk,
long_native, continuous_addon_shadow, long_native_event_demo), across 9 clusters.
50 raw → **39 confirmed** (3 high, 9 medium, 27 low), 11 rejected.

This was a deliberately wide pass; given the volume, iteration 3 FIXED the 3 HIGH +
the clearly-safe MEDIUM + a few low-risk crash guards, DEFERRED the behavior/PnL/
methodology items to the docs, and QUEUED the safe LOW tail for iteration 4.

### Fixed this iteration (8; +3 regression tests; suite stays green at 1985)

| Finding | File | Sev | Fix |
|---|---|---|---|
| scripts-research | `continuous_demo_signal_check.py` | **high** | Replay the continuous engine with the DEPLOYED params (rmom_quantile 0.33→0.25, feature_set=`max_ret168`) + a `--feature-set` flag, so the reconcile's continuous model leg is a faithful live==engine check, not a different universe |
| deep-continuous_demo | `continuous_demo.py` | **high** | Daily-rebalance no longer permanently skipped for the day after a first-cycle wallet error — `rebalance_resize_checked` now reflects whether the resize actually ran (fixes an interaction with the iter-1 wallet-error gate) |
| deep-continuous_addon_shadow | `continuous_addon_shadow.py` | **high** | Single-root strategy-split mode now filters `addon_cycles` by `strategy_id`, so cycle-based gates see only the add-on strategy (trades/orders were already filtered) |
| scripts-equity | `continuous_deployed_equity.py` | med | `stats()` Sharpe uses sample std (ddof=1) + zero-variance guard (was population std, div-by-~0 risk) |
| scripts-equity | `continuous_deployed_equity_refresh.py` | med | Frozen-panel fast path clips the cached panel to `--end-date` (exclusive), no longer leaking rows past the boundary |
| deep-long_native | `long_native.py` | med | Provisional FC panel gated on `enable_fomo_chase` too — an invalid pairing can no longer fire entries that never confirm |
| scripts-research | `bybit_taker_flow_backfill.py` | low | Guard `min()/max()` over an empty symbol-date set (was a crash) |
| (revert) | `build_legacy_archive_manifest.py` | — | Link re-resolution REVERTED: it conflicts with the deliberate, test-pinned audit2b "assume-correct" decision → deferred to operator |

Regression tests added: deployed-equity Sharpe (flat→None, sample-std finite),
provisional test fixture now sets a valid `enable_fomo_chase` pairing.

### Deferred — behavior / methodology (→ `docs/strategy_improvements.md`)

HIGH-PRIORITY: **live vol-target sizing reads an incomplete current-day `btc_rv_30`
(look-ahead)** — causal fix changes live sizing, needs pre-reg. Plus: `fc_use_scaled_exit`
partial never booked; partial time-stop qty not reduced; resurrection gates
(in_universe bypass / avg_rank normalization); live BTC-trend gate fail-closed from
~45d klines; squeeze-breaker partial-fill miss; the link-idempotency conflict; and
re-confirms of iter-1 long-1/long-4.

### Deferred — PnL/fee accounting + data warts (→ `docs/limitations.md`)

WS multi-leg close: only final sub-order's fee booked; only final leg's gross return;
`reconcile_flat` drops partial-reduce PnL; aged-out pending exit drops a late fill;
validator omits `continuous_addon_data_root`; funding 480-hardcode on blank interval.

### Backlog — safe LOW fixes queued for iteration 4

Low-risk, mostly research/ops tooling; not fixed this round to keep the iteration
reviewable: `backfill_binance_{metrics,bookdepth,funding}` 429-abort handling +
metrics concat column-mismatch; `bybit_taker_flow --ohlc` empty-label; `alpha_sweep`
non-gap-aware regime `shift(1)` + forward-pad delay; addon double-count guard (orders/
cycles) + idle-cycle worst-acceptance + entry_order_attempts ts_ms=0; `continuous_demo`
component-trigger validation; `long_native` event_counts fomo_chase double-count +
stale_signal counter; `long_native_event_demo` dry-run exit-order avg_price=0;
`continuous_deployed_equity` missing-funding silent-zero; orchestrator zero-trades
hard-fail.

### Guardrail honored

Local commit only — **no `git push`**. `REAL_MONEY` untouched. A test-pinned prior
decision (audit2b) was NOT overridden — surfaced to the operator instead. The
deferred look-ahead fix *tightens* PIT (left for pre-reg, not silently changed).
