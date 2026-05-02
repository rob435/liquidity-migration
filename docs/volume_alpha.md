# Volume Alpha Research Path

This is the isolated test for the podcast volume hypothesis. It is separate from
the aggression-carry composite.

## Hypothesis

Long coins with stronger volume profiles and short coins with weaker volume
profiles.

This does not test taker aggression, carry, quality, OI impulse, or a blended
score. It tests one alpha family only.

The first implementation separates two things that should not be confused:

- volume expansion: increasing daily/3-day volume versus prior volume
- dollar-volume rank: high absolute turnover versus low absolute turnover

## Command

```bash
/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m aggression_carry --data-root data/agc-bybit-3m --config configs/aggression_carry.default.yaml volume-alpha
```

## Outputs

```text
data/agc-bybit-3m/reports/volume_alpha_report.md
data/agc-bybit-3m/reports/volume_alpha_report.json
data/agc-bybit-3m/volume_alpha_features
data/agc-bybit-3m/volume_alpha_metrics
data/agc-bybit-3m/volume_alpha_portfolios
```

## Current Scope

- Uses Bybit 1h klines aggregated into UTC daily turnover.
- Builds daily volume-only signals.
- Tests 1d, 3d, and 7d forward returns.
- Runs equal-weight long/short portfolios at 20%, 30%, and 50% quantiles.
- Tests base, 2x, and 3x cost assumptions.

## Interpretation Rule

Do not combine this alpha with other signals until it clears costs standalone.
If the standalone volume alpha fails, the composite should not receive a volume
component just because it sounds plausible.

The current corrected 3-month Bybit run says the expansion variants fail, while
`dollar_volume_rank` is the only useful variant so far. Treat that as a lead to
refine, not as proof of a production strategy.
