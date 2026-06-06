# Parameter Pre-Registration: Pop25 Add-On Same-Symbol Overlap Block

Date: 2026-06-05

Status: PRE-REGISTERED before implementation and run.

## Question

The accepted `fresh_pop25` add-on improves the combined book, but the Binance
2x-cost drawdown boundary is tied to squeeze clusters where the primary
`fresh_pop15` sleeve is already active. A closed-loss quarantine did not help
because the damaging add-on can arrive before the primary trade has closed. Can
an add-on-only same-symbol overlap block reduce duplicate symbol exposure
without broadly cutting the profitable add-on flow?

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

Same-symbol overlap block:

- Applies only to add-on candidate trades.
- If a primary or already-accepted add-on trade in the same symbol is active at
  the add-on candidate's entry timestamp, skip the add-on candidate.
- Causal: uses only open positions known at entry.
- Primary trades are never skipped.
- No realized daily-return cap.
- No symbol blacklist.

## Acceptance Interpretation

Prefer the overlap block over the accepted `fresh_pop25` blend only if:

1. Base and 2x-cost return/MAR still improve versus the primary book on both
   venues.
2. Base and 2x-cost drawdown ratio stay `<= 1.10`.
3. Binance drawdown headroom improves versus the accepted blend.
4. Return give-up is smaller than the conservative `binance=0.10` cap and the
   confirmed-fade `pop25_gb1` variant.

Reject if it mostly removes winners, fails to improve Binance drawdown, or is
too sparse to matter.

## Planned Artifacts

- Base:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_same_symbol_overlap_2026-06-05`
- 2x cost:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_same_symbol_overlap_2026-06-05_cost200`
- Comparative audit:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_same_symbol_overlap_audit_2026-06-05`
- Research note:
  `docs/research/hourly_event_trigger_pop15_pop25_blend_2026-06-05.md`
