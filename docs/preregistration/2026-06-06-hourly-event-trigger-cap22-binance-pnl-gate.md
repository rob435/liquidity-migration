# Parameter Pre-Registration: Cap22 + Binance PnL Gate

Date: 2026-06-06

Status: PRE-REGISTERED before the saved-artifact blend run.

## Question

Can we combine the two best local refinements found so far?

- Bybit cap frontier: use active overlay cap `0.22` instead of `0.20`.
- Binance risk mode: keep active overlay cap `0.12`, but apply the
  active-primary PnL gate at threshold `0.00`.

## Frozen Inputs

Primary overlay:

`C:\Users\user\SHARED_DATA\daily_plus_event_trigger_rescue_v2_binance_daily_throttle_2026-06-05`

Add-on overlay:

`C:\Users\user\SHARED_DATA\cont_event_trigger_fresh_pop25_low_churn_prereg_2026-06-05`

Cell:

`q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop25_imp50_cap1m`

Venue scales:

- Bybit: `1.8`
- Binance: `5.0`

Active overlay caps:

- Bybit: `0.22`
- Binance: `0.12`

Active-primary PnL gate:

- Bybit: off
- Binance: threshold `0.00`

## Harness

`scripts/blend_trade_level_overlay_cap.py`

Primary trades are accepted first. Add-on trades are scaled down at entry by
active overlay notional capacity. On Binance only, if a same-symbol primary
trade is active at the add-on entry and its estimated unrealized gross return
is negative, skip the add-on.

## Acceptance Interpretation

This is a research-stage saved-artifact combination test. It is not a
real-money promotion gate.

Compare against the current best trade-capped blend hierarchy:

- Bybit should match or improve the `0.22` frontier result.
- Binance should match the prior Binance-only PnL-gate risk-mode result.
- Base and 2x overlay-cost stress should both pass the drawdown ratio gate
  (`<= 1.10`) and improve return/MAR versus the primary-only book.

## Planned Artifacts

- Base:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_cap22_binance_pnl_gate_2026-06-06`
- 2x cost:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_cap22_binance_pnl_gate_2026-06-06_cost200`
- Research note:
  `docs/research/hourly_event_trigger_cap22_binance_pnl_gate_2026-06-06.md`
