# Parameter Pre-Registration: Pop15 + Pop25 Conservative Trade Cap

Date: 2026-06-05

Status: PRE-REGISTERED before the conservative-cap artifact run.

## Question

The accepted trade-level `fresh_pop15` + `fresh_pop25` blend uses venue caps
`bybit=0.20`, `binance=0.12`. Cap sensitivity showed Bybit has nearby
headroom, while Binance `0.12` is close to the upper passing boundary and
`0.14` fails drawdown. Is a more conservative operating cap
`bybit=0.20`, `binance=0.10` a better candidate for forward-demo monitoring?

## Frozen Inputs

Primary overlay:

- Base root:
  `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_rescue_v2_binance_daily_throttle_2026-06-05`
- 2x-cost root:
  `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_rescue_v2_binance_daily_throttle_2026-06-05_cost200`
- Source trades: each venue's `filtered_overlay_trades.csv`

Add-on overlay:

- Execution root:
  `C:\Users\user\SHARED_DATA\cont_event_trigger_fresh_pop25_low_churn_prereg_2026-06-05`
- Cell:
  `q25_liq2000k_featmax_ret168_btcuptrend_h24_fixed_evtfresh_pop25_imp50_cap1m`
- Construction: `short_only`
- Venue scales: `bybit=1.8`, `binance=5.0`

Trade-level cap:

- Primary trades are accepted first.
- Add-on trades are scaled down at entry if active primary + accepted add-on
  notional would exceed the cap.
- Venue caps:
  - Bybit max active overlay notional: `0.20`
  - Binance max active overlay notional: `0.10`
- No realized-return cap.
- No post-hoc day filter.

## Acceptance Interpretation

This is an operating-risk comparison against the already accepted
`bybit=0.20`, `binance=0.12` research candidate, not a replacement promotion
gate.

Prefer the conservative cap for forward-demo monitoring if:

1. It still improves total return and MAR versus the primary book on both
   venues.
2. It passes the drawdown-ratio gate (`<= 1.10`) under base and 2x overlay
   costs.
3. Binance has visibly better drawdown headroom than cap `0.12`.
4. The return/MAR give-up versus cap `0.12` is acceptable relative to the
   drawdown and concentration improvement.

Reject it as unnecessary if the cap `0.12` result has materially better
return/MAR with no meaningful drawdown, stress, or concentration penalty.

## Planned Artifacts

- Base:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_trade_cap_conservative_2026-06-05`
- 2x cost:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_trade_cap_conservative_2026-06-05_cost200`
- Research note:
  `docs/research/hourly_event_trigger_pop15_pop25_blend_2026-06-05.md`
