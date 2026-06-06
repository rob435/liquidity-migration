# Parameter Pre-Registration: Binance-Only Pop25 Active-Primary PnL Gate

Date: 2026-06-05

Status: PRE-REGISTERED before implementation and run.

## Question

The all-venue active-primary PnL gate improves Binance risk-adjusted behavior
but slightly hurts Bybit return/MAR. Can we keep Bybit on the accepted
`fresh_pop25` blend while applying the PnL gate only to Binance, where the
drawdown boundary is the real problem?

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

Venue-specific PnL gate:

- Bybit: no active-primary PnL gate.
- Binance: if a same-symbol primary trade is active at the add-on entry,
  estimate the primary trade's gross unrealized return using the add-on entry
  price as the current mark. Skip the add-on when the worst same-symbol active
  primary unrealized return is `< 0`.
- If no same-symbol primary is active, allow the add-on.
- Causal: uses only the open primary entry price and the candidate add-on fill
  price known at entry.
- Primary trades are never skipped.
- No realized daily-return cap.
- No symbol blacklist.

## Acceptance Interpretation

Prefer this venue-specific PnL gate over the accepted blend only if:

1. Bybit matches the accepted blend.
2. Binance base and 2x-cost return/MAR still improve versus the primary book.
3. Binance base and 2x-cost drawdown ratio stay `<= 1.10`.
4. Binance MAR and drawdown headroom improve versus the accepted blend.
5. Binance return remains better than the conservative `binance=0.10` cap.

Reject if the Binance improvement disappears when Bybit is left unchanged or if
the result depends on non-causal path information.

## Planned Artifacts

- Base:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_binance_primary_pnl_gate_2026-06-05`
- 2x cost:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_binance_primary_pnl_gate_2026-06-05_cost200`
- Comparative audit:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_binance_primary_pnl_gate_audit_2026-06-05`
- Research note:
  `docs/research/hourly_event_trigger_pop15_pop25_blend_2026-06-05.md`
