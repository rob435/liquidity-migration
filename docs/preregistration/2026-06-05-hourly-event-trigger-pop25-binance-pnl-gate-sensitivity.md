# Parameter Pre-Registration: Binance Pop25 PnL-Gate Sensitivity

Date: 2026-06-05

Status: PRE-REGISTERED before sensitivity runs.

## Question

The Binance-only active-primary PnL gate at threshold `0.00` improves Binance
MAR and drawdown headroom while preserving more return than the blunt
`binance=0.10` cap. Is `0.00` a narrow tuned point, or is there a nearby stable
risk-control region?

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

Sensitivity grid:

- Bybit: no active-primary PnL gate in all runs.
- Binance active-primary PnL gate thresholds:
  - `-0.01`
  - `0.00` (already run)
  - `+0.01`

For Binance, if a same-symbol primary trade is active at the add-on entry,
estimate the primary trade's gross unrealized return using the add-on entry
price as the current mark. Skip the add-on when the worst same-symbol active
primary unrealized return is below the threshold. If no same-symbol primary is
active, allow the add-on.

## Acceptance Interpretation

This is a sensitivity audit, not a fresh promotion gate.

Prefer `0.00` if:

1. It remains inside a locally sensible frontier between `-0.01` and `+0.01`.
2. It improves Binance MAR and drawdown headroom versus the accepted no-gate
   blend under base and 2x costs.
3. It preserves more return than the blunt `binance=0.10` cap.
4. It is not dominated by either neighbor on return, MAR, and DD ratio.

If `+0.01` gives materially better MAR/DD with acceptable return loss, treat it
as the defensive setting. If `-0.01` keeps most of the accepted return while
improving DD enough, treat it as the operating setting. If neighbors are erratic
or contradictory, keep the gate as research-stage only and require forward demo.

## Planned Artifacts

- `-0.01` base:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_binance_primary_pnl_gate_m001_2026-06-05`
- `-0.01` 2x:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_binance_primary_pnl_gate_m001_2026-06-05_cost200`
- `+0.01` base:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_binance_primary_pnl_gate_p001_2026-06-05`
- `+0.01` 2x:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_binance_primary_pnl_gate_p001_2026-06-05_cost200`
- Comparative audit:
  `C:\Users\user\SHARED_DATA\fresh_pop15_pop25_binance_primary_pnl_gate_sensitivity_audit_2026-06-05`
- Research note:
  `docs/research/hourly_event_trigger_pop15_pop25_blend_2026-06-05.md`
