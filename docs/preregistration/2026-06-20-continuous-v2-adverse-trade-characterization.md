# Characterization: What Predicts a Fade Trade Going Hard Against Us

Date: 2026-06-20
Author: Claude (operator-directed next-level research push)
Run label: `exploratory`. Operator question: "is there a trend among the trades that went highly against our direction — like high volatility?"

## Method

`scripts/continuous_v2_adverse_characterization.py`: for each V2_CONTROL fade trade,
compute strictly causal pre-entry 1m features (Book B loader, window [entry−120m, entry))
and relate them to the trade's MAX ADVERSE EXCURSION (MAE) and realized gross. Spearman
IC, blow-up bucket (MAE ≤ −25%), and an rv_30 (pre-entry vol) decile table. Both venues.

## Result — YES, high volatility and big run-up predict adverse moves (both venues)

| feature | IC vs MAE (bybit/binance) | IC vs realized GROSS | reading |
|---------|--------------------------:|---------------------:|---------|
| **rv_30** (pre-entry 1m vol) | **−0.21 / −0.22** | **+0.24 / +0.22** | high vol → worse MAE BUT better return |
| **run_up_120** (size of pop faded) | **−0.21 / −0.22** | +0.17 / +0.17 | bigger pop → worse MAE BUT better return |
| dist_from_hi | −0.05 / −0.04 | +0.02 / +0.05 | weak |
| upper_wick | −0.01 / −0.04 | +0.15 / +0.17 | weak on MAE, good on return |
| ret_last15 (into strength) | −0.02 / −0.07 | +0.07 / +0.05 | weak |

rv_30 decile table (low→high pre-entry vol), Bybit / Binance:

| decile | mean MAE | blow-up rate (MAE≤−25%) | mean gross |
|-------:|---------:|------------------------:|-----------:|
| 1 (low vol) | −0.06 / −0.05 | 0.4% / 0.0% | +0.004 / +0.010 |
| 5 | −0.09 / −0.11 | 10% / 13% | +0.030 / +0.008 |
| 10 (high vol) | −0.18 / −0.25 | **19% / 21%** | **+0.042 / +0.014** |

## The key insight — the scary trades ARE the best trades

`rv_30` and `run_up_120` have **opposite-signed IC on MAE vs realized return**: high
pre-entry vol / big run-up → much WORSE intrabar drawdown (−0.21 IC on MAE, ~20% blow-up
rate in the top decile) but BETTER realized gross (+0.24 IC). High-vol/high-run-up fades
run HARD against the short, then revert HARD to a bigger profit. This single fact ties
the whole program together:

- **Why stops fail (Books A / disaster):** the worst-MAE trades have the best gross —
  a stop exits at the −50% spike right before the big reversion. Confirmed: stopping
  kills the best trades.
- **Why the inverse-vol sizing is the right tail control:** it sizes DOWN exactly the
  high-vol blow-up-prone names, bounding the tail without touching the trade rule.
- **Why event-driven TWAP-in is worth testing:** the adverse run is PREDICTABLE (high
  vol, big run-up) and it is a run-then-revert pattern. Scaling the short IN as the name
  pops higher should improve the average entry price and capture more of the reversion —
  precisely on the trades that currently look scariest. This is the next study.

## Honesty / scope

- Causal pre-entry features; IC is a characterization, not a tradable claim (Book B
  already showed these ICs are real but diffuse — admission/sizing on them is venue-split).
- This run is on the frozen V2_CONTROL ledger; it will be re-run on the richer
  (gates-off, equal-weight) research dataset for more statistical power once that build
  completes.

## No real-money / promotion claim

`REAL_MONEY` stays false. Characterization only; no trade decision changed.
