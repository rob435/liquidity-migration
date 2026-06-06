# Parameter Pre-Registration: Pop25 Rolling Add-On Cluster Cap

Date: 2026-06-05

Status: PRE-REGISTERED before the rolling add-on cap implementation run.

## Question

The accepted trade-level blend uses `bybit=0.20`, `binance=0.12` active
overlay notional caps. The conservative Binance cap `0.10` improves drawdown
headroom but gives up too much return and does not improve positive-PnL
concentration. Can a rolling add-on issuance cap reduce Binance event-cluster
risk while preserving more of the `0.12` rescue PnL?

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

Base trade-level caps:

- Bybit active overlay notional cap: `0.20`
- Binance active overlay notional cap: `0.12`
- Primary trades accepted first.
- Add-on trades scaled down at entry by active primary + accepted add-on
  notional capacity.

Rolling add-on cap:

- Apply only to Binance.
- Window: trailing `168` hours.
- Cap: accepted Binance add-on notional over the trailing window must not exceed
  `0.24`.
- The cap is causal: only add-on entries accepted before the candidate entry
  count against the trailing window.
- If both active and rolling caps bind, the smaller accepted fraction is used.
- No realized-return cap.
- No post-hoc symbol or date filter.

## Acceptance Interpretation

This is a concentration-risk diagnostic, not a real-money promotion gate.

Prefer the rolling cap over the accepted `0.12` candidate only if:

1. Base and 2x-cost return/MAR still improve versus the primary book.
2. Base and 2x-cost drawdown ratio stay `<= 1.10`.
3. Binance drawdown headroom improves versus the accepted `0.12` candidate.
4. Binance add-on top-10 positive share or temporal clustering improves without
   giving up as much return as the blunt `0.10` cap.

Reject it if it mostly removes profitable 2026 rescue clusters or leaves
concentration unchanged.

## Planned Artifacts

- Base:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_trade_cap_rolling_addon_2026-06-05`
- 2x cost:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_trade_cap_rolling_addon_2026-06-05_cost200`
- Comparative audit:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_trade_cap_rolling_addon_audit_2026-06-05`
- Research note:
  `docs/research/hourly_event_trigger_pop15_pop25_blend_2026-06-05.md`
