# Stage 5 - Sizing Alpha

## Question

Can risk allocation improve the book without changing entries?

Hard rule: same selected trades. If a sizing arm changes the entry set, it is
not sizing alpha.

## Control

`Z0_control_size`: frozen current sizing/rebalance/hedge object.

## Arms

- `Z1_score_monotone_size`: fixed monotone size by Stage 1 score bucket.
- `Z2_path_shape_size`: size by neutralized path-shape bucket.
- `Z3_vol_residual_size`: size by residual volatility after existing inverse-vol
  logic.
- `Z4_crowding_risk_budget`: downsize correlated/crowded baskets without
  dropping trades.
- `Z5_sniper_size_conditioned`: vary sniper size only, base size unchanged.
- `Z6_negative_control_size`: hash-bucket sizing.

## Constraints

- same entries;
- same exits;
- same max active positions;
- same global gross/risk cap;
- resize costs charged;
- funding preserved;
- capacity stress reported;
- no leverage increase hidden as alpha.

## Metrics

- return;
- MAR;
- max drawdown;
- worst day;
- turnover/resize cost;
- exposure concentration;
- component contribution;
- symbol concentration;
- beta/residual risk;
- capacity at 1x, 5x, 10x;
- R1 robustness.

## Pass Bar

Sizing advances only if:

- return positive both venues;
- pooled MAR delta `> +0.1`;
- no venue MAR delta `< -0.5`;
- drawdown and concentration do not worsen beyond preregistered tolerance;
- capacity is not worse;
- negative-control sizing is weaker.

## Falsifier

Reject as sizing alpha if the effect is just more leverage, hidden trade
selection, or a single venue/symbol bucket carrying the result.
