# Pre-registration: Promote `div` risk-engineering to the deployed v11a long-FC profile

**Date:** 2026-05-30
**Author:** claude (for owner)
**Stage:** accepted

## What's changing
Add the `div` risk-engineering overlay to the deployed long-FC profile
`_v11a_long_native_config` (the `MultiStratV1` long demo profile):
`universe_size` 10→50, `max_concurrent_positions` 5→10, and de-risk-only
volatility targeting (`enable_vol_target=True`, `vol_target_annual=0.60`,
`vol_target_max_scale=1.0`, `vol_target_min_scale=0.30`). No signal change.

## Hypothesis
Portfolio construction, not a new signal (FC remains the alpha ceiling — ~12
alternative signals were exhausted as nulls). A wider universe + more
concurrent positions diversify idiosyncratic risk (lower portfolio vol per
unit edge → higher Sharpe). De-risk-only vol targeting (Moreira–Muir /
Barroso–Santa-Clara) scales the book DOWN when BTC realized vol is high — where
crypto drawdowns cluster — and never levers above 1.0, cutting the worst
peak-to-trough. Per-trade FC edge is unchanged; the gain is risk-adjusted
return from better packaging.

## Predicted direction + magnitude
From the research-lab finding (Sharpe figures are daily-aligned `sharpe_like`):
- Bybit: Sharpe 1.41→1.63, MAR 1.46→1.68, DD −4.7%→−3.0%, trades 146→288
- Binance: Sharpe 0.96→1.08, MAR 0.91→1.31, DD −5.7%→−3.2%, trades 205→405
- Failure mode / falsifier: if the confirmation run shows MAR Δ ≤ 0 on either
  venue, or a sign flip between venues, reject (cross-venue is the arbiter).

## Roots that will be touched
- [x] bybit_full_pit (confirmation backtest: reports/div_promo_verify)
- [x] binance_full_pit_strategy (confirmation backtest)
- [x] forward demo/paper (the deployed long sleeve trades the new profile after deploy)

## Decision rule (a priori)
Promote only if the confirmation run reproduces a MAR improvement on BOTH
venues (both Δ > 0, no sign flip), consistent with the validated `div` numbers.

## Run command
```bash
.venv/bin/python scripts/verify_div_promotion.py   # baseline vs +div, both venues
```

## Post-run results (2026-05-30, current code, equity-CSV metrics)
| venue   | config        | trades | Sharpe(basket) | MAR  | maxDD | label |
|---------|---------------|-------:|---------------:|-----:|------:|-------|
| bybit   | v11a baseline |    146 |           6.33 | 1.46 | −4.7% | full_pit_universe |
| bybit   | v11a + div    |    288 |           5.25 | **1.58** | −3.3% | full_pit_universe |
| binance | v11a baseline |    205 |           3.72 | 0.91 | −5.7% | full_pit_universe_funding_partial |
| binance | v11a + div    |    405 |           3.06 | **1.30** | −3.2% | full_pit_universe_funding_partial |

MAR up on BOTH venues (Bybit +8%, Binance +43%), DD lower on both, trades ~2×.
(Basket-frequency Sharpe is optimistic for sparse strategies and falls because
the wider book books P&L on more days; MAR — the repo's leverage-invariant
primary metric — is the verdict and it improves cross-venue.) Binance label is
`funding_partial` (its funding table is back-filled pre-2024); the effect is
robust regardless.

A separate experiment this session — adding a 12h-pump sleeve on top of `div` —
was **rejected**: additive on Binance (MAR 1.25→1.71) but a drag on Bybit
(MAR 1.53→1.14, monotonic), i.e. fails the cross-venue bar. `div` stands alone.

## Verdict
**accepted** — cross-venue MAR improvement reproduced on current code, DD lower
on both venues, mechanism grounded in portfolio theory (not signal mining).
Promoted into `_v11a_long_native_config`. Forward demo/paper is the arbiter;
the deploy date is the clean pre/post split for the `MultiStratV1` long ledger.
This remains EXPLORATORY for any real-money claim — the live gate is the forward
demo + cross-venue agreement, unchanged.
