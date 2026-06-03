# Continuation state (write before compaction, 2026-06-03)

## Where we are
All work is on branch **`audit/fixes-2026-06-03`** (main untouched at `68dd185`, NOTHING pushed —
push to main auto-deploys, so the operator decides release). **Full suite: 1221 passing, ruff + mypy
clean.** All edits uncommitted-to-main but committed ON the branch (13 commits).

The operator set a session goal "work on everything listed above" (a Stop hook) = the 5 items below.
All are DONE except the margin-budget adversarial verify (in flight).

## The 5 goal items — status
1. **commit** (protect work) — DONE (`008d371` + all subsequent).
2. **ledger month-partition** (reconcile-ledger-5/quality-dup-5) — DONE: `9c0f4d4`. storage.py buckets
   demo/paper ledgers by month on an immutable key (trades→entry_ts_ms, orders→ts_ms); backward-compatible
   read; ws_risk windowed read (6mo+legacy); `scripts/migrate_ledger_buckets.py` + LEDGER_PARTITION_RUNBOOK.md.
3. **long-sleeve-4** (90d-median universe) — DONE: `017f41b`. NOT deployable until operator bumps the long
   daemon RSS cap + forward-demo re-validation (pre-reg: docs/preregistration/long-sleeve-4-median-universe-2026-06-03.md).
4. **margin-budget + reservation registry** (long-sleeve-5/6) — BUILT functional, all layers:
   `5f246ad` (foundation: liquidity_migration/cross_sleeve.py + storage cross_sleeve_account_state dataset),
   `74bc4a3` (ws_risk owner: compute_im_used + write_account_state each pass), `b58236f` (long sleeve),
   `4598e1a` (short + continuous), `3ebefe6` (pre-reg). NO-OP by default (no budget seeded; all reads
   fail-open). Operator seeds via `cross_sleeve.seed_margin_budget(<short root>, {"short":0.35,"long":0.45,
   "continuous":0.20}, now_ms=...)` — and per the last discussion, the SUM can be < 1.0 to leave a
   liquidation buffer (small-deposit+leverage setup). Module + 12 tests incl. 2-thread lock-contention.
5. **rmom re-validation** — step 1 DONE (`a67b869`): `scripts/rmom_shift_diagnostic.py` measured **51%
   bottom-third churn** shift1→shift3 (Jaccard 0.489, 65.5% retained) on bybit_full_pit → re-validation is
   REQUIRED. Steps 2-4 (re-run gated backtest + MAR + recalibrate rmom_quantile=0.33) need backtest
   hardware = operator. Runbook: docs/audit_2026-06-03/rmom_revalidation_runbook.md.

## IN-FLIGHT verify — DONE (round 5, 2026-06-03)
The read-only adversarial verifier (agentId `a41edcc76e85a234e`) completed. It confirmed the cross-sleeve
build's no-op-default, fail-open, under-lock RMW, single-row dedup, account_key consistency, stop-ordering,
and IM attribution all sound under REAL multi-process contention, and found **one real defect**:
- **CS-1 (medium-high, FIXED in `eb0534a`)** — single-row dedup keeps `max(updated_at_ms)`; ws_risk's
  fresh-`_now_ms()` IM row could win over a sleeve's stale-`cycle_now_ms` claim row and silently erase a
  just-granted reservation (claim returned True) → defeats the same-minute-race guard. Fixed with
  monotonic `updated_at_ms = max(prior+1, now_ms)` in all 3 writers (last-committer-under-lock wins).
- **CS-7 (low, FIXED)** continuous `trade_id=""` → reservation only TTL-GC'd; now derives the id pre-claim.
- **CS-8 (low/soft, HARDENED)** leverage-implied IM floor, only when leverage is reliably known.
- **CS-3 (test, ADDED)** real 3-process spawn contention test (the thread test missed the O_EXCL path).
- CS-4 (ws_risk live-integration test harness) = remaining low-severity test-adequacy follow-up.

Task #19 is COMPLETE. Full suite **1224 passing**, ruff + mypy clean. Details in SESSION_FIXES.md
("round 5"). All 5 `/goal` items are built + verified on the branch; nothing pushed (deploy = operator).

## Key invariants for the cross-sleeve build (don't regress)
- ALL control-row writes (ws_risk write_account_state, sleeve claim_symbol_reservation, operator
  seed_margin_budget) are under-lock read-modify-write → serialized → no lost updates.
- clamp_max_new_entries is SHRINK-ONLY (never upsizes, never touches a sibling). Reservation claim
  FAILS OPEN. dry-run/paper never reserves. ws_risk refresh is self-swallowing + runs AFTER stops.
- DO NOT push to main / deploy without operator. DO NOT touch deploy/systemd/* (deploy-gated). The
  short-continuous workflow patch wanted to edit a .service file — excluded.
