# Parameter Pre-Registration: Pop25 Add-On Active-Primary PnL Gate

Date: 2026-06-05

Status: PRE-REGISTERED before implementation and run.

## Question

The same-symbol overlap block showed that most `fresh_pop25` add-on edge is
intentional same-symbol sizing on top of an active `fresh_pop15` primary fade.
Banning overlap disables the add-on. Can we preserve the edge by allowing the
same-symbol add-on only when the active primary trade is not already losing at
the add-on entry?

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

Active-primary PnL gate:

- Applies only to add-on candidates.
- If a same-symbol primary trade is active at the add-on entry, estimate that
  primary trade's gross unrealized return using the add-on candidate's
  `entry_price` as the current mark.
- For a short primary, unrealized return is
  `entry_price_primary / current_price - 1`.
- Skip the add-on if the worst same-symbol active primary unrealized return is
  `< 0`.
- If no same-symbol primary is active, allow the add-on.
- Causal: uses only the open primary entry price and the candidate add-on fill
  price known at entry.
- Primary trades are never skipped.
- No realized daily-return cap.
- No symbol blacklist.

## Acceptance Interpretation

Prefer the PnL gate over the accepted `fresh_pop25` blend only if:

1. Base and 2x-cost return/MAR still improve versus the primary book on both
   venues.
2. Base and 2x-cost drawdown ratio stay `<= 1.10`.
3. Binance drawdown headroom improves versus the accepted blend.
4. Return give-up is smaller than the conservative `binance=0.10` cap and the
   confirmed-fade `pop25_gb1` variant.

Reject if it skips too many winners, fails to improve Binance drawdown, or
collapses the 2026 Binance rescue behavior.

## Planned Artifacts

- Base:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_primary_pnl_gate_2026-06-05`
- 2x cost:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_primary_pnl_gate_2026-06-05_cost200`
- Comparative audit:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_primary_pnl_gate_audit_2026-06-05`
- Research note:
  `docs/research/hourly_event_trigger_pop15_pop25_blend_2026-06-05.md`
