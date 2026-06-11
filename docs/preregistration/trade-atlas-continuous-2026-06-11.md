# Pre-registration: TC1 — trade-outcome atlas on the CONTINUOUS winner_base book

**Date:** 2026-06-11 (registered BEFORE computation). **Label:** `exploratory`
hypothesis-GENERATOR — by construction nothing here is promotion evidence.
**Authority:** direct operator instruction this session ("i wanted to try the
trade atlas on the continuous sleeve", following "get the backtests done and get
the best values as we are taking this live"). Same posture as TA1
(`trade-atlas-2026-06-11.md`): the operator directs an immediate
backtest-and-apply path; label discipline stays intact — every number produced
here is an IN-SAMPLE-DERIVED-GATE descriptive on the spent window, NEVER
promotion evidence. The forward-watch graduation bar (below) is the only
evidence path. The spent-window freeze (2026-06-09) is overridden for this
exercise by the same operator authority that overrode TA1's forward-only path.

## Dataset (fixed)

The four FROZEN winner_base component trade ledgers per venue (the exact
components the live `continuous_ensemble_v1` combines, weights .30/.20/.40/.10):

- `p3` = `continuous_merged_signal_raw_2026-06-07/{venue}/merged_signal`
- `p4p3` = `independent_continuous_entry_filter_sweep_exploratory_2026-06-07/{venue}/age240_turn4pop3_crowd2`
- `p4p5` = same root, `age240_turn4pop5_crowd2`
- `tp14` = `independent_continuous_tp_hold_sweep_exploratory_2026-06-07/{venue}/age210_tp14_hold24_invvol10_crowd2`

3,184 bybit + 2,617 binance component-trades (2023-04 → 2026-05/04 vintage).
Components overlap on entries; the PRIMARY analysis set is the POOLED PER-VENUE
BOOK deduped by `(symbol, entry_signal_ts_ms)`, keeping the highest-weight
component's copy (p4p5 > p3 > p4p3 > tp14). Outcome = per-trade `net_return`
(primary: win = net_return > 0; secondary: top vs bottom net quartile).

## Feature menu (FIXED — 9 features; consistency, not p-hacking, is the filter)

Context at ENTRY, all causal (TA1's entry-day-return error is explicitly
designed out — market context uses the COMPLETED PRIOR UTC day only):

1. Weekend entry (Sat/Sun by entry_ts UTC) vs weekday.
2. UTC session bucket (Asia 00–08 / EU 08–16 / US 16–24 by entry hour) — the
   continuous book enters all day, so this is non-degenerate here (unlike TA1).
3. Repeat-name: same symbol entered in the pooled book within the prior 30 days
   (entry-to-entry).
4. Concurrent open positions in the pooled book at entry.
5. Book trailing realized PnL at entry: sum of the prior 10 CLOSED trades' net.
6. BTC trailing-30d return magnitude at entry (panel closes, lagged 1 day; the
   book is already uptrend-gated, so this is regime DEPTH).
7. Prior-UTC-day EW alt-market return (daily panel closes; completed day only).
8. Symbol prior-UTC-day turnover (log10 `turnover_quote`; liquidity tier).
9. Signal `rank` at entry (existing ledger column).

## Protocol (fixed)

Per venue × feature on the deduped pooled book: winners-vs-losers delta
(continuous: median diff + bootstrap 90% CI, 1000 reps, seed 20260611;
categorical: win-rate per bucket + binomial CI). A feature SURVIVES iff its
effect direction is consistent across BOTH venues AND at least one venue's CI
excludes zero AND a one-sentence mechanism can be stated.

## Application path (operator-directed, declared up front)

Survivors are converted to per-trade multiplier gates and backtested through the
PARITY-VERIFIED engine path (the `continuous_participation_cap_driver`
trade-day-split machinery → `combine_continuous_components` with the frozen
winner weights → `apply_rebalance_rule` with the deployed w90/tv0.045/max4/
ddh-0.04 rule), both venues, unhedged book (the banked 2f hedge composes
independently). Gate forms declared now:

- repeat-name → cooldown: m=0 for entries within 30d of the same symbol's prior
  entry (entry-to-entry, pooled-book definition, applied per component).
- weekend / session / other categorical survivor → 1.5× size tilt (m=1.5) on the
  favourable bucket (TA1's chosen application form).
- continuous-feature survivor → declared as a threshold filter (m=0 below/above
  the pooled-book median split) BEFORE the gate run; no per-venue tuning.

Parity gate: the all-m=1 rebuild must reproduce the official combine path
(corr ≥ 0.999 on daily returns, total return within 1pp) or the run is invalid.
Decision rule for "best values": MAR-primary (CAGR/|maxDD|), Sharpe secondary,
both venues must not degrade materially (mirrors the Tier-2 shape; these remain
descriptives regardless). Best cell gets the equity curve + trade-count report.

## Forward-watch graduation (the only evidence path)

Identical to TA1: when ≥100 forward demo trades have accumulated on the
continuous book, recompute each armed lead's effect on forward trades only; a
lead graduates to a pre-registered gate proposal iff its forward direction
matches the atlas AND the pooled forward effect is ≥2σ. Until then nothing here
may be cited as evidence.

## Artifacts

`C:/Users/user/SHARED_DATA/trade_atlas_continuous_2026-06-11/` — atlas parquet +
report JSON + gate-backtest outputs. Script: `scripts/trade_atlas_continuous.py`.

## Findings (filled in after the run) — three trade-level survivors, ZERO book-level conversions; deployed config already optimal

**Stage A** (999 bybit / 852 binance pooled deduped trades, WR 68.9% / 64.4%):

- FAILED consistency: `weekend` (sign flips: −0.5pp bybit / +1.7pp binance) and
  `repeat30d` (−0.5pp / +1.4pp) — the two TA1 daily-book leads DO NOT EXIST on
  the continuous book. `concurrent`, `trail10_pnl`, `btc30_mag`, `rank`: null.
- SURVIVORS (direction-consistent both venues, ≥1 CI excl zero): **session
  US-penalty** (US-entry WR −2.6pp bybit / −6.8pp binance vs rest; binance excl
  zero), **mkt_prevday** (winners enter after an up prior-UTC-day market;
  +0.13pp / +0.97pp median W−L; binance excl), **log_turn_prev** (winners skew
  lower prior-day turnover; −0.12 / −0.09 log10; bybit excl).

**Stage B** (parity PASS: rebuilt baseline corr 0.99995/0.99985, ret diff
0.00pp; declared forms, pooled thresholds mkt_med=+1.04%, turn_med=7.126):

| venue | cell | trades on | ret% | dd% | MAR | Sharpe |
|---|---|---|---|---|---|---|
| bybit | baseline | 3184/3184 | 84.0 | −5.3 | **5.02** | 2.42 |
| bybit | TC_session15 | 3184 (tilt) | 107.7 | −8.6 | 4.01 | 2.24 |
| bybit | TC_mktup | 1627/3184 | 48.2 | −4.5 | 3.43 | 2.12 |
| bybit | TC_lowturn | 2419/3184 | 54.2 | −7.1 | 2.44 | 1.99 |
| bybit | TC_combo | 1627/3184 | 66.0 | −6.4 | 3.29 | 2.16 |
| binance | baseline | 2617/2617 | 60.0 | −4.3 | **4.53** | 2.00 |
| binance | TC_session15 | 2617 (tilt) | 86.2 | −6.2 | 4.52 | 2.05 |
| binance | TC_mktup | 1309/2617 | 40.2 | −2.9 | 4.54 | 2.02 |
| binance | TC_lowturn | 1081/2617 | 25.1 | −2.9 | 2.84 | 1.45 |
| binance | TC_combo | 1309/2617 | 61.0 | −4.0 | 4.92 | 2.12 |

**Verdict: NO gate adopted — the deployed `continuous_ensemble_v1` is the best
cell on both venues.** The session tilt is pure leverage (return up, MAR/Sharpe
down on bybit); the two filters cut the breadth the book's MAR lives on; the
combo is a textbook cross-venue mirage (bybit −1.73 MAR, binance +0.39). This is
the FOURTH no-sizing-conversion receipt on this book (OI tilt, down-only sizing,
participation cap, atlas gates): trade-level win-rate edges do not convert
because the w90/tv0.045/max4/ddh-0.04 rule already normalizes the book and its
MAR is breadth-carried. Do not re-mine sizing/filter forms on this window.

**Best backtest (the deployed object, ensemble + banked 2f hedge, real funding,
`continuous_deployed_equity_2026-06-10`):** bybit 1x +103.0%, DD −5.4%, MAR
6.12, Sharpe 2.86 (999 unique entries / 3,184 component-trades); binance 1x
+77.0%, DD −4.1%, MAR 6.17, Sharpe 2.46 (852 / 2,617). At the deployed 4x
anchor: +1,399.8% / DD −20.7% bybit; +779.9% / −15.5% binance.

**Forward-watch (the only evidence path, identical bar to TA1):** the session
US-penalty is ARMED as a forward observable — at ≥100 forward demo trades on the
continuous book, recompute the US-vs-rest WR delta on forward trades only;
graduates to a gate proposal iff direction matches and pooled effect ≥2σ.
`mkt_prevday` and `log_turn_prev` are recorded but NOT armed (their gate forms
already failed conversion at book level). Nothing here is promotion evidence.
