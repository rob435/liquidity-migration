# Pre-registration: continuous v2 inverse-vol + max4 replay

**Date:** 2026-06-18
**Stage:** exploratory registered
**Scope:** CONTINUOUS demo/paper research only; no real-money claim.

## Question

Does the repaired v2 live-style lifecycle recover the old high-MAR deployed-equity
shape when we apply inverse-vol trade sizing while retaining the max4 daily
vol-target rebalance stack?

## Cells

Run on both `bybit_full_pit` and `binance_full_pit`:

1. `09_v2_uptrend_no_server_stop_breaker_invvol`
   - Same v2 entry book and exits as the proposed no-stop v2.
   - `BTC_TREND_GATE=uptrend`.
   - No `left_decile`, no `stop_approach`, no `failed_fade`, no `breakeven`,
     no server stop.
   - Adverse breaker retained.
   - Trade sizing changed from `flat` to `inverse_vol` with
     `target_vol_per_name=0.01`, `vol_weight_clamp=2.0`.
   - Apply the existing max4 daily vol-target rebalance rule
     `w90/tv0.045/max4/ddh-0.04`.

2. `10_v2_uptrend_no_server_stop_breaker_invvol_hedged`
   - Same as cell 1, plus the existing BTC+ETH 2f hedge and BTC-vol regime
     hedge overlay.

## Success metric

Primary: per-venue MAR and max drawdown versus the current saved v2 replay
`08_v2_uptrend_no_server_stop_breaker_hedged`.

Secondary: total return, Sharpe-like, worst day, and trade counts.

## Integrity notes

- This is still an internal exploratory replay, not promotion evidence.
- The full-live replay already applied the max4 daily vol-target rebalance
  layer; the new thing being tested here is inverse-vol trade sizing inside the
  live-style shared book.
- The old high-MAR chart used a different deployed-equity reconstruction
  harness. This run tests whether that sizing improvement survives in the
  live-style shared-book replay.
- Sniper PostOnly add-on is still omitted.
- No server stop means the cell is demo/paper only and not real-money-safe.

## Run command

```bash
env POLARS_MAX_THREADS=4 PYTHONIOENCODING=utf-8 \
  .venv/bin/python scripts/continuous_live_v2_redesign_runner.py \
    --start-date 2023-04-01 \
    --end-date 2026-06-18 \
    --only-rungs 09_v2_uptrend_no_server_stop_breaker_invvol 10_v2_uptrend_no_server_stop_breaker_invvol_hedged \
    --out-root backtest-runs/continuous_v2_invvol_max4_2026-06-18
```

## Verdict

Run completed.

Artifact root:
`backtest-runs/continuous_v2_invvol_max4_2026-06-18`.

Run command:

```bash
env POLARS_MAX_THREADS=4 PYTHONIOENCODING=utf-8 \
  .venv/bin/python scripts/continuous_live_v2_redesign_runner.py \
    --start-date 2023-04-01 \
    --end-date 2026-06-18 \
    --only-rungs 09_v2_uptrend_no_server_stop_breaker_invvol 10_v2_uptrend_no_server_stop_breaker_invvol_hedged \
    --out-root backtest-runs/continuous_v2_invvol_max4_2026-06-18
```

Results:

| Cell | Bybit return | Bybit DD | Bybit MAR | Bybit Sharpe-like | Binance return | Binance DD | Binance MAR | Binance Sharpe-like |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| invvol + max4 | +67.86% | -5.39% | 4.02 | 2.77 | +59.20% | -4.72% | 3.99 | 2.54 |
| invvol + max4 + hedge/regime | +87.92% | -5.39% | 5.09 | 3.32 | +75.37% | -4.70% | 4.99 | 3.04 |

Comparison versus prior flat v2 hedged cell
`08_v2_uptrend_no_server_stop_breaker_hedged`:

| Venue | Return delta | DD improvement | MAR delta | Sharpe-like delta |
| --- | ---: | ---: | ---: | ---: |
| Bybit | -36.05 pp | +4.97 pp | +1.36 | +0.53 |
| Binance | -21.96 pp | +3.04 pp | +1.08 | +0.43 |

Verdict: inverse-vol trade sizing survives in the live-style shared-book replay
and improves the objective we cared about: MAR/Sharpe and drawdown shape. It
does not maximize raw return. The best new cell is
`10_v2_uptrend_no_server_stop_breaker_invvol_hedged`: mean return +81.65%,
mean max drawdown -5.05%, both venues MAR ~5. This is still exploratory
registered, demo/paper only, and not real-money-safe because no server stop is
present.

## Promotion wiring

Accepted for official `continuous_ensemble_v2` demo/paper wiring on 2026-06-18:

- `apply_continuous_demo_profile()` resolves v2 to `sizing_mode="inverse_vol"`,
  `target_vol_per_name=0.01`, `vol_weight_clamp=2.0`, and the max4 daily
  rebalance stack `w90/tv0.045/max4/ddh=-0.04`.
- Demo and paper systemd units pin the same sizing and rebalance env vars.
- Live trade rows persist `entry_vol` and `vol_weight_multiplier`; daily
  rebalance preserves that stored multiplier when resizing open trades.
- `FROZEN_FORWARD_CONFIG` exposes the same recipe under `entry_sizing` so
  `promoted.continuous_profile()` is not ambiguous.
