# Parameter Pre-Registration: Pop15 + Pop25 Trade-Level Overlay Cap

Date: 2026-06-05

Status: PRE-REGISTERED before confirmatory rerun. Prior saved-artifact and
trade-cap scouts motivate this run but are not acceptance evidence.

## Hypothesis

The accepted `fresh_pop15` rescue V2 + Binance daily-throttle overlay can be
improved by adding the sparse `fresh_pop25` overlay only when ex-ante active
overlay notional capacity remains. The cap should preserve the `fresh_pop15`
recovery behavior while allowing high-conviction `fresh_pop25` exposure without
the non-tradable realized daily return cap used in the prior component scout.

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

- Primary trades are always accepted first.
- Add-on trades are scaled down at entry if active primary + accepted add-on
  notional would exceed the cap.
- Venue caps:
  - Bybit max active overlay notional: `0.20`
  - Binance max active overlay notional: `0.12`
- No realized-return cap.
- No post-hoc day filter.

Accounting:

- Rebuild overlay MTM from capped trade ledgers via `_portfolio_mtm_equity`.
- Combine with the primary book's daily effective sleeve return.
- Drawdown ratio gate compares against the raw deployed daily short baseline.
- Repeat with cost multiplier `2.0`, applied by doubling trade `cost_return`
  before MTM rebuild.

## Acceptance Rules

The candidate passes only if all of the following hold on both venues:

1. Base blend total return exceeds the primary book total return.
2. Base blend MAR exceeds the primary book MAR.
3. Base blend absolute max-drawdown ratio versus raw daily baseline is `<= 1.10`.
4. The same three gates pass with 2x overlay costs.
5. At least one add-on trade is accepted after the cap.
6. The cap is applied using only entry-time active notional, not realized
   returns.

## Planned Artifacts

- Base:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_trade_cap_prereg_2026-06-05`
- 2x cost:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_trade_cap_prereg_2026-06-05_cost200`
- Research note:
  `docs/research/hourly_event_trigger_pop15_pop25_blend_2026-06-05.md`

