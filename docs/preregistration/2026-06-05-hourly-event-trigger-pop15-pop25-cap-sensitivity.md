# Parameter Pre-Registration: Pop15 + Pop25 Cap Sensitivity

Date: 2026-06-05

Status: PRE-REGISTERED before the cap-sensitivity run.

## Question

The trade-level `fresh_pop15` + `fresh_pop25` capped blend passed with venue
caps `bybit=0.20`, `binance=0.12`. Are those caps sitting on a narrow tuned
point, or is there a nearby robust region?

## Frozen Inputs

Primary overlay:

- Base root:
  `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_rescue_v2_binance_daily_throttle_2026-06-05`
- 2x-cost root:
  `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_rescue_v2_binance_daily_throttle_2026-06-05_cost200`

Add-on overlay:

- Execution root:
  `C:\Users\user\SHARED_DATA\cont_event_trigger_fresh_pop25_low_churn_prereg_2026-06-05`
- Cell:
  `q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop25_imp50_cap1m`
- Venue scales: `bybit=1.8`, `binance=5.0`

Harness:

- `scripts/blend_trade_level_overlay_cap.py`
- Primary trades accepted first.
- Add-on trades scaled down at entry by active overlay notional capacity.
- No realized-return cap.

## Cap Grid

Run shared cap levels across both venues:

- `0.10`
- `0.12`
- `0.14`
- `0.18`
- `0.20`
- `0.22`

This is enough to read local sensitivity for:

- Bybit around accepted cap `0.20`: `0.18/0.20/0.22`
- Binance around accepted cap `0.12`: `0.10/0.12/0.14`

## Acceptance Interpretation

This run is diagnostic, not a new promotion gate. A cap is considered locally
robust if:

1. Base return and MAR improve versus the primary book.
2. Base drawdown ratio versus raw daily baseline is `<= 1.10`.
3. The same conditions pass under 2x overlay costs.

If only the exact accepted cap passes and both neighbors fail, treat the cap as
fragile and prefer a lower-return but more stable cap or forward-demo-only
status.

## Planned Artifacts

- Base grid:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_trade_cap_sensitivity_2026-06-05`
- 2x-cost grid:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_trade_cap_sensitivity_2026-06-05_cost200`
- Research note:
  `docs/research/hourly_event_trigger_pop15_pop25_blend_2026-06-05.md`

