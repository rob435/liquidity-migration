# Pre-registration: BTC-vol regime-hedge live on demo + forward

**Date:** 2026-06-15
**Author:** rob435 (operator-approved 2026-06-15 — "no opt-in, get it implemented
and running in the demo and forward")
**Stage:** run-pending (code complete; the confirming run is the first fresh-clock
orchestrator run on the archived state dir + the first live hedge cycle that
stamps a non-1.0 `hedge_intensity`).

Finding: the consolidated historical research record (`docs/research_summary.md`)
identified BTC-vol hedge modulation as the only robust, both-venue, trade-keeping
continuous-book improvement worth live demo + forward-watch.

## What's changing
The frozen continuous hedge gains a **causal, mean-1 BTC-volatility regime
intensity** that scales the hedge leg(s): hedge MORE in turbulence, LESS in calm.
Operator decisions (2026-06-15): the intensity scales the **whole 2f hedge** (both
BTC and ETH legs); the prior 2f forward ledger is **archived and a fresh clock is
started now**.

`intensity(d) = 1 + λ·(2·pct − 1)`, λ=0.5, where `pct` = trailing-250 percentile
of the trailing-30d population stdev of BTC daily returns, taken against PRIOR
days' vols only. Bounded to [0.5, 1.5], mean-1 (a reallocation of hedge weight
across regimes, not a larger average hedge). Causal: `intensity[d]` reads BTC
returns strictly before day `d`, so the day-`d` hedge sized as
`beta(through d-1) · scale · intensity[d]` has no look-ahead
(`docs/backtesting_errors_we_never_repeat.md`).

## Exact files / knobs touched
- `liquidity_migration/continuous_regime.py` (NEW) — single source of truth:
  `btcvol_intensity_series(days, btc_rets, …)` (backtest/forward) and
  `latest_btcvol_intensity(btc_returns, …)` (live), plus `FROZEN_BTCVOL_REGIME`
  (`{kind: btcvol, lam: 0.5, vol_window: 30, pct_window: 250}`, `VOL_MIN_OBS=10`,
  `PCT_WARMUP=50`).
- `liquidity_migration/continuous_forward_replay.py`
  - `FROZEN_FORWARD_CONFIG['hedge']` gains `"regime": FROZEN_BTCVOL_REGIME` — a
    new HASHED key (the single behavioural knob). `frozen_hedge_regime()` exposes
    it; `forward_readiness_summary` stamps `"hedge_regime"`.
  - `build_full_ledger(...)` already threads `hedge_intensity` into
    `apply_rebalance_rule` (merged 85e92b6); the two-leg branch's `hedge_scale =
    scale · intensity` scales both legs.
- `scripts/continuous_forward_replay_orchestrator.py` — `venue_update` computes
  `btcvol_intensity_series(all_days, rets, …)` from the BTC return series and
  passes `hedge_intensity=` to `build_full_ledger`.
- `liquidity_migration/continuous_hedge_manager.py` — `compute_hedge_decision_2f`
  (and single-leg `compute_hedge_decision`) multiply `target_scale` by
  `latest_btcvol_intensity(btc_returns)` read from the same hash-pinned
  `frozen_hedge_regime()`; intensity is stamped into the decision diagnostics.
- `scripts/continuous_deployed_equity.py` / `continuous_deployed_equity_refresh.py`
  — the reported deployed equity applies the same intensity
  (`deployed_hedge_intensity`) so the curve cannot silently diverge from the live
  + forward hedge object.

One source of truth: all three consumers (forward ledger, live demo orders,
deployed-equity report) read the regime from `frozen_hedge_regime()` /
`FROZEN_BTCVOL_REGIME`, so they apply one identical hedge object
(errors-we-never-repeat #16).

## Hypothesis
Historical screening found the fade book's edge is diffuse and profits when broadly deployed; every
selection/sizing/exit lever failed to robustly harvest. The only robust both-venue
improvement is an overlay that keeps the whole book and hedges the squeeze tail —
the BTC-vol regime-hedge. Characterized honestly, it is a **modest,
sub-period-variable tail-insurance overlay**, not a smooth alpha: pooled ΔMAR
+0.05–0.08 at 1× hedge cost, both venues positive, λ-robust {0.25,0.5,0.75}, beats
the random-regime (hash) control by +0.6–0.8, keeps all trades, gross-neutral. Lone
fragility: thin binance cost headroom (≈break-even at 1.2× hedge cost, −0.011 at
1.5×).

**Known extrapolation (recorded honestly):** the +0.05–0.08 was validated on a
single-BTC-leg control; per operator decision the live object scales the **whole
2f (BTC+ETH)** hedge by the BTC-vol regime. BTC vol is used as a market-turbulence
proxy for both legs; the ETH-leg modulation is NOT separately validated and is part
of what forward-watch evaluates.

## Predicted direction + magnitude
Object/clock change, not a sweep:
- `frozen_config_hash()` becomes
  `0668eb88c0d657478517c02d4994c0e48ddd5da7449897cdd92ecd153913d158`
  (was the regime-free 2f hash). `init_or_check_state` therefore **refuses** the
  existing 2f state dir — the prior 2f forward ledger is VOIDED by the config-hash
  pin. This is the intended clean reset, **not a drift alarm**.
- `hedge_intensity=None` / all-1.0 reduces `build_full_ledger` and
  `apply_rebalance_rule` byte-for-byte to the plain 2f hedge (tested), so the only
  behavioural change is the regime modulation itself.
- The live decision now stamps `hedge_intensity` (1.0 during the ≥50-obs warm-up,
  then in [0.5, 1.5]); the forward summary stamps `hedge_regime`.
- Falsification: a fresh clock whose summaries do not stamp the regime, or a live
  decision whose `ratio = beta·scale·intensity` does not match the forward ledger
  for the same day (live↔backtest parity is asserted in
  `tests/test_continuous_regime.py`).

## Roots that will be touched
- [ ] bybit_full_pit — read-only; the orchestrator recomputes the four frozen
  component cells to the data end. No candidate selection, no parameter sweep.
- [ ] binance_full_pit — same, read-only.
- [x] forward demo/paper (always, by being live) — the fresh regime-hedge forward
  state dir + the live demo hedge cycles are the artifacts this produces.

## Decision rule (a priori)
Forward-watch adoption (operator bar 2026-06-15: any ROBUST improvement that still
takes trades), evaluated as **squeeze protection, not a smooth MAR gain**:
1. Accepted as wired correctly iff, after the operator archives the old 2f state
   dir and the orchestrator runs fresh: (a) `init_or_check_state` pins the new
   hash; (b) both venues' summaries stamp `hedge_regime` (kind=btcvol, λ=0.5); and
   (c) the live decision's `hedge_intensity` reproduces the forward ledger's
   per-day hedge ratio (same-object parity).
2. Forward go/no-go (operator-gated, over a meaningful squeeze sample): intensity
   fires up in high-BTC-vol regimes; realized demo drawdown in squeeze episodes is
   reduced; hedge turnover cost stays within model (watch binance, the
   thin-headroom venue). No promotion to real money.
3. Overlap drift on the FRESH clock remains a hard alarm; the config-hash void of
   the old clock is the one sanctioned discontinuity.

**Tier-3 real-money gate UNCHANGED and unmet.** The fresh clock restarts at
`forward_days=0`. Do NOT set `REAL_MONEY=true`.

## Run command
```bash
# Operator step 1 (VPS): archive the regime-free 2f forward clock (clean reset).
mv ~/SHARED_DATA/continuous_forward_replay \
   ~/SHARED_DATA/continuous_forward_replay.2f_no_regime.archived-2026-06-15

# Operator step 2 (VPS): start the fresh regime-hedge clock at the next refresh.
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
    scripts/continuous_forward_replay_orchestrator.py \
    --venues bybit,binance --forward-start 2026-06-10

# Gate before any push (CLAUDE.md / STATE.md non-negotiable):
.venv/bin/python -m ruff check liquidity_migration tests scripts
.venv/bin/python -m pytest -q
```
The live demo hedge (`liquidity-migration-continuous-hedge.timer`) picks up the
overlay automatically once the new code deploys — no separate live step.

## Post-run results
(fill in after the confirming run; include the fresh state-dir path, both venues'
`readiness` blocks showing `hedge_regime`, and the first live `hedge_intensity`.)

- `frozen_config_hash` (regime):
  `0668eb88c0d657478517c02d4994c0e48ddd5da7449897cdd92ecd153913d158`
- code landed at commit: _pending merge SHA_
- bybit fresh clock: `forward_days` / `ledger_days` / `hedge_regime` = _pending_
- binance fresh clock: `forward_days` / `ledger_days` / `hedge_regime` = _pending_
- Old 2f state dir archived to: _path pending_
- First live non-1.0 `hedge_intensity` observed: _pending_

## Verdict
accepted | rejected | inconclusive — pending the confirming run.

Expected verdict on a clean run: **accepted as wired** (the demo book and forward
ledger track one regime-hedge object; the config-hash void of the old clock is the
intended reset). Forward-watch then judges the overlay as squeeze protection over a
real squeeze sample. No promotion or Tier claim is made or cleared here.
