# Parameter Pre-Registration: Pop25 Add-On Same-Symbol Loss Quarantine

Date: 2026-06-05

Status: PRE-REGISTERED before implementation and run.

## Question

The accepted Binance blend sits near the 2x-cost drawdown boundary. A tail audit
shows the April 2026 drawdown includes a same-symbol sequence where the primary
overlay loses on `RAVEUSDT` and the `fresh_pop25` add-on then re-enters the same
symbol. Can an add-on-only same-symbol loss quarantine reduce path risk without
the return destruction caused by blunt caps or delayed giveback entries?

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

Same-symbol loss quarantine:

- Applies only to add-on candidate trades.
- If any already-closed primary or accepted add-on trade in the same symbol has
  `net_return < 0` within the trailing `72` hours, skip the add-on candidate.
- Causal: a trade only enters the quarantine set after its `exit_ts_ms` is
  strictly `<=` the candidate add-on `entry_ts_ms`.
- Primary trades are never skipped by this rule.
- No realized daily-return cap.
- No symbol blacklist.

## Acceptance Interpretation

Prefer the quarantine variant over the accepted `fresh_pop25` blend only if:

1. Base and 2x-cost return/MAR still improve versus the primary book on both
   venues.
2. Base and 2x-cost drawdown ratio stay `<= 1.10`.
3. Binance 2x drawdown headroom improves versus the accepted blend.
4. Return give-up is smaller than the conservative `binance=0.10` cap and the
   confirmed-fade `pop25_gb1` variant.

Reject if it mostly removes winners, fails to improve Binance drawdown, or is
too sparse to matter.

## Planned Artifacts

- Base:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_symbol_loss_quarantine_2026-06-05`
- 2x cost:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_symbol_loss_quarantine_2026-06-05_cost200`
- Comparative audit:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_symbol_loss_quarantine_audit_2026-06-05`
- Research note:
  `docs/research/hourly_event_trigger_pop15_pop25_blend_2026-06-05.md`
