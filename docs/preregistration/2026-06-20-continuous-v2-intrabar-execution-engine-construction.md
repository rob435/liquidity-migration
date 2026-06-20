# Construction Receipt: Continuous V2 Next-Level — Wave 2, Intrabar Execution Engine

Date: 2026-06-20
Author: Claude (operator-directed next-level research push)
Stage: construction (X1 engine done + validated; X2 ledger partial; X3 gated)
Parent plan: `docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md`
Run label: `exploratory` (engine construction; no alpha claim)

## Design — research overlay, not a deployed-engine edit

The deployed `trade_lifecycle._simulate_indexed_trade` resolves stop/TP on 1h OHLC
with a same-bar adverse-first rule (it can't see the intrabar path). Rather than
perturb that live/forward path, Wave 2 adds a **research overlay**
(`liquidity_migration/intrabar_engine.py`) that re-resolves a trade's exit on the
Wave-1 1m cache, **reusing the deployed engine's exact price/exit helpers**
(`_take_profit_price`, `_stop_price`, `_bar_exit_hits`, `_bar_excursion`,
`_side_return`, `_stop_fill_price`). So the ONLY difference vs the 1h engine is
path granularity — exactly the X1 requirement ("1m changes only path-dependent
trades").

## X1 — Intrabar path engine (DONE + validated)

`resolve_exit_1m(bars, *, entry_ts_ms, entry_price, side, take_profit_pct,
stop_loss_pct, planned_exit_ts_ms, stop_fill_mode, stop_slippage_cap_pct)`:

- HIGH/LOW exits (stop, TP) resolved by 1m **first-touch**; window
  `[entry_ts_ms, planned_exit_ts_ms)` matches the 1h engine's bar range.
- Stop AND TP in the SAME 1m bar → genuinely ambiguous → **adverse-first** (stop),
  flagged `ambiguous_same_bar`.
- No touch → max_hold at the window's last 1m close (== the 1h max-hold close);
  `data_end` if the 1m cache ends before planned exit.
- No-trade/densified-null minutes are skipped for touch and **counted**
  (`null_bars`) — they carry-forward close (high=low=close) and cannot create a
  spurious touch.
- `intrabar_resolution=1h` remains the deployed engine (baseline reproduction);
  this overlay is the `1m` mode. `trade` mode deferred (needs trade tapes + X3).

Close-based soft exits (mfe_giveback, breakeven, failed_fade, event/rank/hash)
stay on the registered hourly cadence in the deployed engine and are NOT
re-resolved here — this overlay is the path-dependent stop/TP layer the 1h bar
could not measure (Books A, E; entry/exit fill timing for X2).

### X1 acceptance — PASS

- Unit tests (`tests/test_intrabar_engine.py`, 7 passing): TP-first, stop-first,
  same-bar→adverse-first, max_hold, data_end, control-no-stop-ignores-high-spikes,
  null-minute skip/count.
- Real-data validation (re-resolve a 60-trade sample of the Phase-0 `V2_CONTROL`
  ledger on the 1m cache, TP12/no-stop): **bybit 61/61 reason agreement (100%),
  price match 60/61 (98.4%); binance 62/62 (100%) / 62/62 (100%)**. The 1m
  resolver reproduces the control's TP/max_hold economics, confirming it will
  change only path-dependent (stop) trades.
- Known immaterial nuance: a few Bybit max_hold exits differ ~0.05-0.08% in price
  (1h-root close vs the 1m-from-trades close — different aggregation source).
  Binance is exact (both 1h and 1m from Vision). This is a cross-source close
  rounding difference, not a path error; it does not affect TP or stop fills.

## X2 — Order/fill ledger (PARTIAL — schema in `ExitResolution`; driver pending)

`ExitResolution` already carries the exit fill (reason, ts, price, side_return,
mae/mfe, ambiguity, first_touch_ts). The full per-trade order/fill ledger
(decision / intended-order / fill / slippage / fee / funding / position rows with
`order_submit_ts`, `fill_ts`, `spread_bps`, `participation_bps`, `model_cost_bps`)
will be emitted by the Book-A/E driver that re-resolves all Phase-0 trades under
each policy. The entry side is currently the instantaneous entry-bar close (the
deployed convention); TWAP/maker entry fill modeling is Book C.

## X3 — Fill/cost calibration (GATED on demo/paper fill data)

Calibrating slippage/impact from real fills needs the VPS demo/paper fill ledgers
(`scripts/reconcile.sh` / `pit-reconcile`), which are not local to this research
box. Until then, A/B arms run with the deployed cost model + 1×/2×/3× cost stress;
no arm may become a candidate on zero-cost or uncalibrated market-order fills.
This is registered as a dependency, not skipped.

## Touched code

- NEW `liquidity_migration/intrabar_engine.py` (overlay; reuses trade_lifecycle helpers).
- NEW `tests/test_intrabar_engine.py` (7 tests).
- No change to the deployed forward-replay engine or frozen config.

## Stop conditions

- If `intrabar_resolution=1m` disagreed with the 1h control on reason for a
  material fraction of trades, stop (it would mean a resolver bug, not a path
  finding). Observed: 0% reason disagreement → proceed.

## No real-money / promotion claim

Engine construction only. `REAL_MONEY` stays false. Forward demo/paper remains
the arbiter.
