# Active Receipt: Continuous Winner Robustness

**Status:** binding evidence base for `continuous_ensemble_v1`.

## Frozen Winner

`winner_base` weights:

- p3: 0.30
- p4p3: 0.20
- p4p5: 0.40
- tp14: 0.10

Portfolio rule: w90/tv0.045/max4/ddh-0.04, no momentum hurdle, rmom q25,
BTC-uptrend gate.

## Binding Conclusions

- The four-component uptrend ensemble is the frozen continuous demo object.
- Adaptive re-weighting did not justify replacing frozen weights.
- The single-component `continuous_rebalance_v1` remains resolvable only for old
  ledgers.
- The downtrend extension was demoted; current live default is the uptrend core.
- Continuous remains research-stage and below Tier-3 until forward demo/paper,
  residual, stress, and capacity bars are met.

Historical simplex grids, downtrend experiments, funding checks, and amendments
are in git history and summarized in `docs/research_summary.md`.
