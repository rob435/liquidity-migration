# Parameter Pre-Registration: Pop25 Confirmed-Fade Add-On

Date: 2026-06-05

Status: PRE-REGISTERED before the confirmed-fade add-on execution runs.

## Question

The accepted `fresh_pop25` add-on improves the combined book, but Binance
drawdown and add-on concentration remain the key weak points. Book-level caps
did not solve this cleanly: a blunt `0.10` cap gives up too much rescue PnL,
and a rolling 168h add-on cap fails drawdown. Can the add-on be improved by
waiting for a causal post-pop giveback before entry, so the strategy shorts the
confirmed fade rather than the pop itself?

## Frozen Inputs

Primary overlay:

- Base root:
  `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_rescue_v2_binance_daily_throttle_2026-06-05`
- 2x-cost root:
  `C:\Users\user\SHARED_DATA\daily_plus_event_trigger_rescue_v2_binance_daily_throttle_2026-06-05_cost200`
- Source trades: each venue's `filtered_overlay_trades.csv`

Confirmed-fade add-on execution:

- Venues: Bybit and Binance
- Data roots:
  - `C:\Users\user\SHARED_DATA\bybit_full_pit`
  - `C:\Users\user\SHARED_DATA\binance_full_pit`
- Window: `2023-04-01` through `2026-05-28`
- Signal side: short-only D9 overlay
- RMOM quantile: `0.25`
- Feature set: `max_ret168`
- BTC trend gate: `uptrend`
- Liquidity gate: hourly turnover >= `2,000,000`
- Entry delay: `1h`
- Hold: fixed `24h`
- Gross exposure in execution artifact: `0.5`
- Max active positions: `25`
- Funding and modeled impact: enabled, existing continuous engine defaults

Entry triggers:

- `pop25_gb1`: prior six-hour max one-hour pop >= `25%`, current hour <= `0`,
  current close at least `1%` below the prior six-hour high.
- `pop25_gb2`: same, but at least `2%` below the prior six-hour high.

Blend:

- Add-on venue scales: `bybit=1.8`, `binance=5.0`
- Active overlay notional caps: `bybit=0.20`, `binance=0.12`
- Primary trades accepted first.
- Add-on trades scaled down at entry by active primary + accepted add-on
  notional capacity.
- No realized-return cap.
- No rolling add-on cap.
- Repeat with 2x overlay costs.

## Acceptance Interpretation

This is an entry-quality diagnostic, not real-money promotion evidence.

Prefer a confirmed-fade add-on over `fresh_pop25` only if:

1. Base and 2x-cost return/MAR still improve versus the primary book on both
   venues.
2. Base and 2x-cost drawdown ratio stay `<= 1.10`.
3. Binance drawdown headroom improves versus the accepted `fresh_pop25` blend.
4. The return give-up versus `fresh_pop25` is smaller than the blunt
   `binance=0.10` cap, or concentration/drawdown improves enough to justify it.

Reject a trigger if it becomes too sparse, loses the 2026 Binance rescue
behavior, or only improves drawdown by removing most profitable add-on flow.

## Planned Artifacts

- Execution root:
  `C:\Users\user\SHARED_DATA\cont_event_trigger_pop25_giveback_addon_2026-06-05`
- Base blend root:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_giveback_trade_cap_2026-06-05`
- 2x-cost blend root:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_giveback_trade_cap_2026-06-05_cost200`
- Comparative audit:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_giveback_trade_cap_audit_2026-06-05`
- Research note:
  `docs/research/hourly_event_trigger_pop15_pop25_blend_2026-06-05.md`
