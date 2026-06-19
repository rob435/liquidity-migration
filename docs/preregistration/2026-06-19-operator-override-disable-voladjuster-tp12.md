# Operator Override 2026-06-19 — Disable Daily Volatility Adjuster + Promote TP12

Date: 2026-06-19
Type: **operator override** (directed by the owner), demo/paper only. `REAL_MONEY` stays false;
the Tier-3 real-money gate is unmet and unchanged. This receipt records the change, its rationale,
the honest evidence against parts of it, reversibility, and the owner-gated deploy steps still pending.

## What changed (code, system-wide)

1. **Daily volatility adjuster DISABLED** (the daily vol-target rebalance / "daily volatility curve",
   not the BTC-vol hedge regime). Mechanism: a reversible `enabled` flag on
   `ContinuousRebalanceRule` (default True keeps prior behavior byte-identical);
   `compute_continuous_rebalance_scale` returns a constant 1.0 when disabled (no vol-target, no
   drawdown-halving, no momentum gate). Set off in:
   - `continuous_forward_replay.FROZEN_FORWARD_CONFIG["rebalance"]["enabled"] = False` (backtest,
     promoted profile via `promoted.continuous_profile()`, forward replay).
   - `continuous_demo.apply_continuous_demo_profile` (`continuous_ensemble_v2`):
     `daily_rebalance_enabled=False` (live demo/paper daemon).
   The remaining rebalance params are retained verbatim so re-enabling is a one-line flip.
2. **Component take-profit promoted 0.10 -> 0.12 system-wide.** Live profile component tuples
   (`continuous_demo`) and the research runner `V2_COMPONENTS` (`scripts/continuous_v2_ab_research_runner.py`).
   The daemon computes `take_profit_price = price*(1-0.12)` live, so no component-ledger rebuild is
   needed for the live book.

Both changes alter `frozen_config_hash`, so the prior continuous forward ledger is **VOIDED** by the
config-hash pin (a clean reset, not a drift alarm).

## Rationale (operator)

Research-phase decision: the daily vol-target rebalance is a path-dependent overlay that confounds
signal/exit research (judging every change through rebalanced MAR conflates signal quality with the
overlay's lagged response). Disable it during the research phase so signal/exit work is evaluated at
constant gross, adopt the TP12 exit improvement found this session, and **rework the volatility
control once research is finished** (flip `enabled` back to True + retune).

## Honest evidence caveats (this override goes against parts of the research evidence)

- **TP12 is NOT a both-venue win.** It fails the two-venue promotion gate: lifecycle MAR
  +1.79/+2.23 on Bybit but **-3.66/-3.45 on Binance** (Binance drawdown doubles). It is
  Sharpe-negative on Binance even at constant gross (rebalance-off check), so it is not a vol-curve
  artifact. Promoted system-wide by operator override accepting the Binance degradation.
  Evidence: `2026-06-19-continuous-v2-f2-exit-tp-lifecycle-verdict.md`, `...-f2b-vol-tp-verdict.md`.
- **Disabling the adjuster removes the book's only daily risk control** and lowers MAR on both
  venues (it is value-adding: no-rebalance control MAR ~4.23 Bybit / ~5.30 Binance vs rebalanced
  5.66 / 8.19 — Binance's tight-drawdown edge was largely the rebalance). The v2 book already runs
  no-stop; with the adjuster off it has no daily risk control. This is acceptable ONLY as a
  reversible research-phase state, not a real-money configuration.

## Reversibility

Flip `enabled` back to True in `FROZEN_FORWARD_CONFIG["rebalance"]` and
`daily_rebalance_enabled=True` in the v2 profile (and revert TP to 0.10 if desired) to restore the
prior object. The retained rebalance params make the re-enable a one-line change per site.

## Owner-gated steps NOT performed here (no push/deploy without owner confirmation)

- Archive the live continuous forward state dir and start a fresh clock (config hash changed).
- Regenerate the hedge warmstart CSVs for the new object.
- Redeploy the VPS demo daemon (the pre-push gate + `vps-deploy` workflow). The code change defines
  the new promoted object; the live daemon picks it up only on redeploy.
- No `REAL_MONEY` change; demo/paper only.
