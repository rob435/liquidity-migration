# Continuous Fade Scale-In Portfolio Replay

Date: 2026-06-28.

## Question

Does the positive conditional scale-in diagnostic survive a full-book replay
that recomputes component mark-to-market, BTC/ETH hedge sizing, drawdown, and
MAR?

## Pre-Registered Arms

Replay the frozen baseline plus three mechanical add-on arms selected from the
diagnostic grid:

- `mae05_add25`: add 25% of the parent trade notional after a 5% adverse move.
- `mae05_add50`: add 50% of the parent trade notional after a 5% adverse move.
- `mae10_add50`: add 50% of the parent trade notional after a 10% adverse move.

The first arm checks whether a smaller add-on keeps the same direction with less
heat. The second is the prior diagnostic best arm. The third tests whether a
more selective trigger reduces bad add-ons.

## Method

For every frozen component trade, create an explicit child short only if the
post-entry hourly path touches the adverse trigger before the parent exits. The
child fills at the trigger price, closes at its own TP12 or at the parent exit,
and uses the same component config cost model plus available funding. The
combined parent+child component ledger is then decomposed and passed through the
existing ensemble combiner and BTC/ETH hedge layer.

This is stronger than the by-trade diagnostic because it recomputes full-book
hedge/drawdown/MAR from component MTM. It is still not live-ready evidence: it
does not model order-book queue position, intrabar trigger ordering beyond a
bar-close-safe fill convention, margin coupling, or venue liquidation mechanics.

## Decision Rule

Label remains `exploratory` regardless of outcome. A live/paper change is
blocked unless the replay improves both venues on MAR and drawdown, does not
violate disaster-loss sizing, and is later validated by forward demo/paper.

Artifacts will be written under:

- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/scale_in_portfolio_replay.csv`
- `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/portfolio_replays/scale_in_grid/`

## Results

Command:

```bash
.venv/Scripts/python.exe scripts/continuous_scale_in_portfolio_replay.py
```

All arms increased full-book return, but every arm worsened MAR and drawdown
versus the frozen baseline.

| Venue | Arm | Child trades | Return | MAR | Max DD | Decision read |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Bybit | baseline | 0 | +26.64% | 7.33 | -1.13% | control |
| Bybit | `mae05_add25` | 1,274 | +31.17% | 6.75 | -1.43% | fails MAR/DD |
| Bybit | `mae05_add50` | 1,274 | +35.83% | 6.39 | -1.74% | fails MAR/DD |
| Bybit | `mae10_add50` | 764 | +33.11% | 6.27 | -1.64% | fails MAR/DD |
| Binance | baseline | 0 | +18.84% | 5.72 | -1.02% | control |
| Binance | `mae05_add25` | 1,123 | +21.66% | 5.34 | -1.25% | fails MAR/DD |
| Binance | `mae05_add50` | 1,123 | +24.53% | 4.96 | -1.53% | fails MAR/DD |
| Binance | `mae10_add50` | 682 | +23.54% | 5.36 | -1.36% | fails MAR/DD |

## Verdict

Rejected for deployment. The mechanism is real enough to lift return in this
overlay, but it buys that return with worse path risk. No arm clears the
pre-registered requirement to improve both venues on MAR and drawdown. Keep the
result labeled `exploratory`; do not add live/paper scale-in behavior from this
evidence.
