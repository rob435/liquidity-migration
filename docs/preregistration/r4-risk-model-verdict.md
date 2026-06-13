# Active Receipt: R4 Risk-Factor Model Verdict

**Status:** active Tier-3 residual-Sharpe foundation.

## Final Factor Set

The retained model is the six-factor daily cross-sectional model used by
`liquidity_migration.risk_model.decompose_strategy_pnl`.

Binding conclusions:

- Factors passed the sanity bar on both venues.
- Pairwise correlations were acceptable.
- Variance capture beat the permutation null; the earlier in-sample-tautology
  framing was corrected and must not be repeated.
- Calendar-exact daily returns and the residual day grid were hardened.

## Current Use

For any demo-candidate cell, residualize strategy PnL with this model before
claiming Tier-3 residual Sharpe. This receipt does not promote any strategy by
itself.

Historical validation tables and old integrated-program labels are in git history.
