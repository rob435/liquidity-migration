# Pre-registration: promote the `drop_all_4` filter set to the deployed `promoted` profile

**Date:** 2026-05-30
**Author:** claude (for owner)
**Stage:** accepted — demo/paper forward-test candidate (never real money)

## What's changing

The deployed short-sleeve `promoted` profile (`event_demo._demo_event_config`)
adopts the R1 filter-audit cell **`drop_all_4`** + the de-concentration it was
explored at. Old → new:

| field | old (promoted) | new (drop_all_4) |
|---|---|---|
| `max_active_symbols` | 5 | **12** |
| `liquidity_migration_day_return_min` | 0.0 | **-1.0** (floor dropped) |
| `stop_pressure_stop_count` | 7 | **999** (veto dropped) |
| `realized_loss_pressure_loss_count` | 6 | **999** (veto dropped) |
| `universe_rank_max` | 150 | **99999** (upper bound dropped) |

`drop_all_4` = the four defensive vetoes/bounds the R1 filter audit flagged as
non-earning (recovered from `c28c512~1:scripts/r1_filter_audit_sweep.py`); the four
entry-*quality* filters (`event_rank_fraction_max`, `residual_return_min`,
`close_location_min`, `pit_age_days_min`) are retained. The systemd demo + paper
services' `MAX_ACTIVE_SYMBOLS` env is raised `3 → 12` to match (env overrides the
profile field). `strategy_id` is unchanged → the forward ledger stays continuous and
the **deploy date is the clean pre/post split**. The dropped `universe_rank_max`
makes the profile use the full universe, already provided by the deployed
match-the-backtest mode (`UNIVERSE_RANK_END=0 / UNIVERSE_MAX_SYMBOLS=0`).

## Hypothesis

The four dropped filters are defensive vetoes/bounds that removed more good trades
than bad — overhead, not protection. Dropping them and spreading risk over 12 names
(same gross exposure, thinner slices) is portfolio/selection engineering: keep the
candidate pool wide, let the position layer diversify idiosyncratic noise. Not a new
signal.

## Evidence (in-sample, full-PIT backtest root, 2023-04-01 → 2026-05-28)

The deleted research's surviving artifacts had drop_all_4 ahead on both venues
(bybit +53.8%, binance +5.7%) — but those runs used `stop_fill_mode='stop'`
(optimistic) + `max_active=3`. Re-running this profile's exact config under the
current engine (`bar_extreme_capped`, max_active=12, 45 bps):

| venue | total return | max DD | trades | profit factor |
|---|---:|---:|---:|---:|
| bybit | **+56.9%** | −25.2% | 820 | 1.14 |
| binance | **−12.2%** | −38.9% | 509 | 0.96 |

Read fairly: **Bybit is strongly positive; Binance is negative in-sample under the
conservative capped-stop fill.** Cross-venue disagreement is the known character of
this regime-conditional signal (STATE.md) — informative, not by itself
disqualifying. Two caveats soften the Binance read: (1) it's a single fill model —
the optimistic fill had Binance positive, so the truth sits in that band; (2)
Binance funding is not wired (`funding-missing`), so the modeled Binance PnL omits a
real cash flow (for a short, often a credit).

## Why demo/paper now

The three-tier framework is **permissive at Tier-2 by design** — "permissive where
being wrong is free (backtest → demo is paper)." Demo/paper is exactly where a
cross-venue-mixed, in-sample candidate earns or loses its keep: the forward
demo/paper ledger is the arbiter, and fresh forward data can't be overfit. This
deploys drop_all_4 to demo + paper to gather that forward evidence — the in-sample
backtest is the prior, not the verdict.

## Forward review (a priori)

Track the forward demo cross-venue. Bybit is the lead. If Binance stays net-negative
forward across the demo window, that's the signal to revert to the prior filtered
profile or gate Binance separately. No real-money step without a full Tier-3 pass
(STATE.md). The `strategy_id` is unchanged, so the deploy date cleanly splits the
forward ledger pre/post.

## Not changed

No real-money toggle (demo + paper only). No cost model, no entry-quality filters,
no signal logic. `demo_relaxed` and the long sleeve are untouched.
