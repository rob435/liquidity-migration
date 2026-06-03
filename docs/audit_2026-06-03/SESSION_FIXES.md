# Audit follow-through — fix session (2026-06-03)

Worked the 69-finding audit ledger to closure. All work is **uncommitted**; behavior-
changing fixes carry inline `DEPLOY NOTE:` comments and are the operator's to deploy.
Gate after every change: `ruff check liquidity_migration tests` + `mypy` + `pytest -q`.
**Final: 1189 tests pass, ruff + mypy clean.**

## Disposition totals
- **59 FIXED** (21 this session) · **7 DEFERRED** (documented) · **1 PARTIAL** (long-sleeve-9) · **6 FLAGGED** (pre-existing methodology notes)

## Fixed this session (each test-gated)
**Efficiency / cosmetic (no behavior change):** quality-dup-9 (shared `coerce_int`), quality-dup-8
(entry tick/qty constants), quality-dup-2 (MS constants → `_common`), EXEC-8 (per-cycle rank index,
O(N·trades)→O(1)), reconcile-core-5 (filtered single-trade lookups vs full-ledger materialization),
ws-dataplane-6 (bootstrap threshold scoped to new targets), ws-dataplane-7 (cached stats scan),
long-sleeve-9 (extracted `_compute_long_order_sizing`).

**Behavior-changing (DEPLOY NOTE + tests):**
- EXEC-2 — paper/demo stop-loss now books at the honest `bar_extreme_capped` fill (worse-of trigger
  vs realized adverse bar extreme), matching the backtest engine so demo↔backtest stop PnL agree.
- EXEC-6 — `_wait_for_execution_summary` aggregates all WS legs **and** gates the REST poll on
  `target_qty` before declaring a multi-fill order `partial`.
- continuous-6 — the `left_decile` selection exit now reads the **confirmed** decile (`entry_state`),
  the same signal that selected the entry, instead of the noisy live intra-hour decile. Fires the
  instant the system's signal drops the name; no time floor; protective price stops stay immediate.
  (Replaced a rejected `min_hold_hours` band-aid.)
- continuous-4 — circuit breaker compares a fee-consistent `net_return` (subtracts realized fees at
  read time) without mutating the stored field or the w24/n8 threshold.
- reconcile-core-2 — risk-exit qty capped to `min(trade.qty, position.size)` in shared-account mode
  so one sleeve's stop can't flatten a sibling's leg on the same symbol.
- reconcile-ledger-7 — `reconcile_demo_bybit` scopes account-wide Bybit truth to the ledger's
  `(symbol, side)` pairs (removes sibling-sleeve false orphans). Lost-row masking documented.
- ws-dataplane-1 — per-symbol ticker staleness filter so a stale per-symbol price can't reach
  stop/exit pricing while another symbol keeps the global cache fresh.
- ws-dataplane-8 — ticker WS self-reconnect watchdog (3× the stale-warn bound).
- reconcile-ledger-4 — listing-age now keys on the trading day (not the +1-day stamp), consistent
  with the membership flag, so the age300 gate is honest.
- data-download-1 — clamped Binance OI/taker markers keyed on the actually-covered range so the
  uncovered pre-30d prefix is no longer claimed complete and skipped forever.

**Test-only:** reconcile-core-7 (netted over-close surfacing).

## Adversarial re-audit (4 Opus skeptics, refute-mode)
Confirmed bulletproof: EXEC-2, ws-dataplane-1, ws-dataplane-8, reconcile-ledger-4, data-download-1,
continuous-4, continuous-6. Found + fixed 3 real gaps: EXEC-6 REST-poll hole, reconcile-core-2
comment/second-caller, reconcile-ledger-7 dangling test reference (the referenced test didn't exist
— now written) + masking docstring.

## Deferred (need a dedicated PR / operator decision — NOT silently dropped)
- **reconcile-core-4** — ✅ SUBSEQUENTLY DONE (the safe way): instead of the rejected background
  thread, the per-orphan `get_closed_pnl` calls were collapsed into ONE account-wide fetch per pass,
  single-threaded, paged in ≤7-day windows (Bybit clamp) with orderId dedup. Double-audited (rounds
  2+3); bulletproof. See "Fixed this session" / the re-audit section below.
- **reconcile-ledger-5** — month-partitioning the ledgers needs an on-disk migration + pre-registration.
- **reconcile-ledger-3** — same-(symbol,side) cross-sleeve fold needs an orderId→sleeve map (closed_pnl
  has no orderLinkId); the separable part is covered by reconcile-ledger-7.
- **quality-dup-5** — per-pass full ledger read is a correctness requirement on the shared account;
  the windowed-read replacement needs pre-registration.
- **long-sleeve-4 / -5 / -6** — PIT universe-rank, shared-account margin budget, and cross-process
  same-symbol exclusion are all multi-site / cross-sleeve redesigns (the long sleeve is "do not tune").
- **long-sleeve-9 (PARTIAL)** — pure sizing helper extracted; the I/O-ordering splits need a payload-
  equivalence harness, deferred rather than rushed on a deployed sleeve.

## Adversarial re-audit — converged to dry over 4 rounds
Every behavior-changing / hot-path / equivalence-claimed / research-correctness change was
independently refuted by a read-only Opus skeptic. Severity converged round over round:
- **Round 1** (10 live-path fixes): 3 concerns found + fixed (EXEC-6 REST-poll gate, reconcile-core-2
  comment+caller, reconcile-ledger-7 dangling-test + masking doc).
- **Round 2** (reconcile-core-4 + long-sleeve recovery): 1 **medium regression** found + fixed — the
  batched closed-PnL fetch widened the window past Bybit's ~7-day clamp, dropping a recent orphan's
  close when an old (≤21d-hold) orphan shared the pass → fixed by paging in disjoint ≤7-day windows.
- **Round 3** (EXEC-8, reconcile-core-5, ws-dataplane-7 + the F1 fix): EXEC-8/reconcile-core-5/
  ws-dataplane-7 **confirmed equivalent** (20k-trial fuzzing each); 2 low/unreachable F1 concerns
  closed (dropped the `now_ms` upper cap to match legacy's unbounded upper; dedup only by non-empty
  orderId so distinct orderId-less legs aren't merged).
- **Round 4** (research-correctness: causal shift / panel join-key / block bootstrap / MAR-nan /
  funding dedup / decompose keying): **CLEAN** — no look-ahead, off-by-one, wrong-shift, survivorship,
  or statistical bug; every test fails under the pre-fix code (non-vacuous). 2 non-regression edge
  cases (control-zero-DD MAR abstention; the legacy `entry-1d` decompose fallback's single-day
  assumption) documented in-code.

**Deploy caveat (already self-documented in STATE.md open-debts):** the residual-momentum causal
shift (shift1→shift3) + panel join-key fix re-base the rmom gate, so the rmom-gate MAR verdict and the
live `rmom_quantile=0.33` must be re-validated before that path is deployed.
