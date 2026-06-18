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
