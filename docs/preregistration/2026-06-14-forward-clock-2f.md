# Pre-registration: Forward clock tracks the live BTC+ETH 2f object

**Date:** 2026-06-14
**Author:** rob435 (operator-approved 2026-06-14 — audit flagged-item answer)
**Stage:** run-pending (code complete + operator-approved; confirming run = the
fresh 2f clock started at the next data-root refresh)

Finding: **forward-replay-1** (with **sizing-rebalance-2**). The code change is
done and uncommitted in the working tree; this receipt is filed before the
confirming run (the first orchestrator run on a fresh, archived state dir).

## What's changing
`liquidity_migration/continuous_forward_replay.FROZEN_FORWARD_CONFIG['hedge']`
now carries `instrument2="ETHUSDT"` (so `frozen_hedge_mode()` returns `"2f"`),
and `scripts/continuous_forward_replay_orchestrator.py` now loads the ETH second
leg (`btc_inputs(..., "ETHUSDT")`) and passes it to `build_full_ledger` as
`hedge_returns_2` / `hedge_funding_2` — so the no-order forward signal clock
accrues the **same** BTC+ETH 2f hedge object the live demo book executes,
instead of a BTC-only hedge.

## Exact files / knobs touched
- `liquidity_migration/continuous_forward_replay.py`
  - `FROZEN_FORWARD_CONFIG['hedge']` gains `"instrument2": "ETHUSDT"` (single
    new key; `"instrument": "BTCUSDT"` unchanged).
  - New derived (NOT hashed) helpers `frozen_hedge_mode()` → `"2f"` /
    `"btc_only"` and `frozen_hedge_instruments()` → `["BTCUSDT", "ETHUSDT"]`,
    keyed off the presence of `instrument2`.
  - `build_full_ledger(...)` gains optional `hedge_returns_2` / `hedge_funding_2`
    params, threaded straight into `apply_rebalance_rule` (which already supports
    the two-leg path — `continuous_rebalance.apply_rebalance_rule`, params
    `hedge_returns_2`/`hedge_funding_2`, bivariate causal betas
    `compute_hedge_betas_2f`, joint proportional cap `hedge_cap*scale`).
  - `forward_readiness_summary(...)` already stamps `"hedge_mode"` and
    `"hedge_instruments"` from those helpers into every summary.
- `scripts/continuous_forward_replay_orchestrator.py`
  - `btc_inputs(venue, days, symbol="BTCUSDT")` generalised to any hedge symbol
    (returns + real funding day-sums for `symbol`).
  - `venue_update(...)` now calls `btc_inputs(venue, all_days, "BTCUSDT")` AND
    `btc_inputs(venue, all_days, "ETHUSDT")` and calls
    `build_full_ledger(pieces, rets, fund, rets2, fund2)`.

(The same working-tree edit also lands sizing-rebalance-2 / metrics-3..6 and
forward-replay-2/5/6 — the calendar-basis MAR/Sharpe annualizer, the
`allow_history_revision` self-heal opt-in, and the per-venue
stall-isolation/non-zero-exit observability. Those are documented at their own
findings; this receipt binds only the 2f object-identity change to the frozen
config + orchestrator. The single behavioural knob that changes the accrued
ledger object is `instrument2="ETHUSDT"`.)

## Hypothesis
The live demo book runs the banked BTC+ETH **2f** hedge
(`deploy/systemd/liquidity-migration-continuous-hedge.service`, `HEDGE_MODE=2f`,
banked 2026-06-10), but the forward signal clock was accruing a **BTC-only**
hedge object. A forward-readiness PASS on a BTC-only object is therefore NOT
evidence for the deployed strategy — it validates a different object
(errors-we-never-repeat #16, same-code/same-object illusion: backtest, paper,
and demo must map to the same decision/hedge object). Wiring the ETH second leg
into the frozen forward object makes the clock track the same object the live
book executes, so a future readiness PASS validates the deployed strategy.

## Predicted direction + magnitude
This is an **object-identity correction**, not a sweep, so the predicted "effect"
is structural, not a Sharpe-delta hunt:
- `frozen_config_hash()` changes (the JSON payload now includes
  `instrument2`), so `init_or_check_state` **refuses** the existing `btc_only`
  state dir (`RuntimeError`: "forward state config hash mismatch ... archive the
  state dir and start a new clock"). Predicted: prior BTC-only forward ledger is
  VOIDED by the config-hash pin.
- Operator action: archive the old `btc_only` state dir and start a **fresh** 2f
  clock at the next data-root refresh. This is a **clean reset, not a drift
  alarm** — the hash pin is doing exactly its job.
- After the reset, every `forward_readiness_summary` stamps `hedge_mode="2f"`,
  `hedge_instruments=["BTCUSDT","ETHUSDT"]`, and accrues `basket_return`/`equity`
  against the two-leg object.
- Numeric direction of the readiness numbers vs the old BTC-only clock: not a
  promotion criterion and not predicted here — the two clocks measure different
  objects and are not comparable. The fresh clock starts at `forward_days=0` and
  re-accrues.
- Failure mode that would falsify the change: a fresh 2f clock whose summaries
  do NOT stamp `hedge_mode="2f"`, or whose accrual does not route the ETH leg
  through `apply_rebalance_rule` (i.e. `hedge_returns_2` ignored) — that would
  mean the live and forward objects still differ and #16 is not resolved.

## Roots that will be touched
- [ ] bybit_full_pit (per-venue working dataset) — read-only, no parameter
  sweep; the orchestrator recomputes the four frozen component cells to the
  root's current data end. No new candidate selection.
- [ ] binance_full_pit (per-venue working dataset) — same, read-only.
- [x] forward demo/paper (always, by virtue of being live) — the fresh 2f
  forward replay state dir is the artifact this change produces.

This is not a parameter sweep over a candidate; it is a frozen-object identity
fix to make the no-order forward collector match the deployed hedge. No
threshold, filter, timing, or universe rule is being tuned on these roots.

## Decision rule (a priori)
Object-identity acceptance (binding), not a Tier gate:
1. The change is **accepted as a correctness fix** iff, after the operator
   archives the `btc_only` state dir and the orchestrator runs on a fresh state
   dir: (a) `init_or_check_state` accepts the fresh dir and pins the new
   `frozen_config_hash`; (b) the build wires the ETH leg via `hedge_returns_2`/
   `hedge_funding_2` (two-leg `apply_rebalance_rule` path); and (c) every
   `forward_readiness_summary` for both venues stamps `hedge_mode="2f"` and
   `hedge_instruments=["BTCUSDT","ETHUSDT"]`.
2. **Overlap drift on the FRESH 2f clock remains a hard alarm.** Once the new
   clock has stored days, any re-verification drift on a stored
   `basket_return`/`equity` (outside the explicit operator-gated
   `allow_history_revision` re-base path) is a same-code regression →
   `RuntimeError`, nothing appended, orchestrator exits non-zero. The reset is
   the *one* sanctioned discontinuity (config-hash voiding the old object); after
   that the same-code/no-drift contract is strict again.
3. The config-hash mismatch on the OLD `btc_only` dir is **expected and not a
   failure** — it must NOT be "fixed" by editing the hash or reusing the old dir.
   Mixing a 2f ledger into the `btc_only` dir is forbidden by design.

Tier interaction (STATE.md "Decision Rules"): this change does not by itself
clear any Tier — it makes a future forward-readiness PASS *admissible* as
evidence for the deployed strategy. Tier-3 (≥30 forward demo/paper days, forward
MAR > 0 both venues, dd < 50%, daily reconciliation, bootstrap left tail ≥ 0,
residual Sharpe ≥ +0.3, stress + 10× capacity) stays strict and unmet; the fresh
2f clock starts at `forward_days=0` and must re-accrue real observed days
(`tier3_days_gate_30` gates on `ledger_days >= 30`, observed rows, not calendar
span).

## Run command
```bash
# Operator step 1: archive the stale BTC-only forward clock (clean reset).
mv ~/SHARED_DATA/continuous_forward_replay \
   ~/SHARED_DATA/continuous_forward_replay.btc_only.archived-2026-06-14

# Operator step 2: start the fresh 2f clock at the next data-root refresh.
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
    scripts/continuous_forward_replay_orchestrator.py \
    --venues bybit,binance --forward-start 2026-06-10

# Gate before any push (CLAUDE.md / STATE.md non-negotiable):
.venv/bin/python -m ruff check liquidity_migration tests scripts
.venv/bin/python -m pytest -q
```

## Post-run results
(fill in after the confirming run; include the fresh state-dir path, both
venues' `readiness` blocks showing `hedge_mode="2f"`, and the commit SHA at
which the `FROZEN_FORWARD_CONFIG` + orchestrator diffs landed.)

- `frozen_config_hash` (2f): _to record at run time_
- bybit fresh clock: `forward_days` / `ledger_days` / `hedge_mode` = _pending_
- binance fresh clock: `forward_days` / `ledger_days` / `hedge_mode` = _pending_
- Old `btc_only` state dir archived to: _path pending_

## Verdict
accepted | rejected | inconclusive — pending the confirming run.

Expected verdict on a clean run: **accepted as a same-object correctness fix**
(errors-we-never-repeat #16) — the forward clock now tracks the BTC+ETH 2f
object the live demo book executes, so a future readiness PASS validates the
deployed strategy rather than a BTC-only proxy. The config-hash void of the old
clock is the intended clean reset, not a drift alarm; drift on the fresh 2f
clock remains a hard alarm. No promotion or Tier claim is made or cleared by
this change.
