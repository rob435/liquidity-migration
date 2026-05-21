# Position-Sizing Study — Pre-Registration

**Status:** pre-registered 2026-05-21. Locked *before* any sized backtest was run.

## Motivation

The `liquidity_migration` short strategy is validated to tribunal **WATCH** — 3/3
pre-registered windows positive, 81/81 sweep scenarios promotable, 6/6 negative
controls pass. The 81-scenario sweep varied *entry* parameters (threshold, hold,
stop, take-profit) around a **constant, equal-weight position size**: every trade
is sized `notional_weight = gross_exposure / max_active_symbols` (default 0.20),
identical for all trades (`volume_events.py:487`).

Position sizing has never been varied. It is the one remaining untested,
legitimate lever on this dataset, and this study tests it.

## Hypothesis (locked)

**H1 (primary):** Volatility-aware position sizing — weighting each short
inversely to a point-in-time estimate of the symbol's recent return volatility —
improves the **out-of-sample risk-adjusted return (Sharpe)** of the strategy
without changing the entry signal, entry timing, or exit logic.

**Direction is genuinely uncertain.** The strategy shorts the *weakest-liquidity*
names, which skew toward higher-volatility microcaps. Inverse-vol sizing
therefore systematically *underweights* exactly the names the edge concentrates
in. H1 may well be false. A flat or negative result is a valid, expected-possible
outcome and will be reported as such.

## Sizing rule (locked)

Each trade receives a multiplier `position_weight` applied to the base
`notional_weight`:

- **`equal`** (baseline / control): `position_weight = 1.0` for every trade.
  Reproduces current behavior byte-for-byte.
- **`inverse_vol`** (the H1 treatment): `position_weight_i = (1/σ_i) / E_i`, where
  - `σ_i` = the symbol's point-in-time volatility field for event *i* (primary:
    `prior7_return_volatility`, already computed PIT by the feature pipeline);
  - `E_i` = the expanding mean of `1/σ_j` over all prior events *j* reaching the
    sizing stage in execution order — strictly causal, no future data;
  - first event / missing or non-positive σ → `position_weight = 1.0` (neutral).
- **`signal_rank`** (secondary variant): the same construction with the event
  signal score in place of `1/σ` — concentrate into the strongest signals.

All weights are clamped to **[0.25, 4.0]** (`position_weight_clamp = 4.0`). The
clamp is fixed in advance and is **not** tuned. It guarantees the change is a
genuine reallocation, not stealth filtering: the highest-vol name still carries
≥ 0.25× weight.

The expanding-mean normalization holds the mean multiplier ≈ 1, so total gross
exposure stays ≈ constant in aggregate. This is a **reallocation of a fixed risk
budget, not a leverage change** — and Sharpe, the primary metric, is in any case
~invariant to a constant leverage multiplier.

## Metrics & decision rule (locked)

- **Primary metric:** Sharpe on the pre-registered **out-of-sample window** — the
  same windows and the same `strategy-tribunal` harness used for every other
  claim in this repo.
- **Secondary:** max drawdown, total return, positive-window count — all windows.
- **"Improvement" is declared real only if** OOS Sharpe rises **and** OOS max
  drawdown does not materially worsen **and** the direction survives the
  robustness checks below.
- **Robustness checks** (reported, never used to pick the headline):
  1. `inverse_vol` with a different PIT volatility proxy
     (`prior7_intraday_range_mean`);
  2. `signal_rank`.
- **What falsifies H1:** OOS Sharpe flat or down, or an improvement that does not
  survive the alternate-proxy check.
- A small improvement will be reported as small. The word "significant" will not
  be attached to any result that is not large *and* robust across both checks.

## Pre-registration revisions (made before any result)

The conversational version of this hypothesis specified a hand-computed 30-day
realized volatility. Revised — before running anything — to use the feature
pipeline's existing `prior7_return_volatility`: it is already point-in-time
validated, eliminating a class of look-ahead bugs a new self-computed vol path
would introduce. The robustness check was correspondingly changed from "30-day
window of the same measure" to "a different vol proxy"
(`prior7_intraday_range_mean`) — a stronger test. Both revisions are
methodological, made with zero results observed.

## Results

Run 2026-05-21 on the canonical full-PIT root (`bybit_fullpit_1h`, 460 symbols,
2023-05..2026-05), promoted scenario `liquidity_migration / reversal /
threshold 0.40 / hold 3d / stop 0.12 / TP 0.26 / cost 3.0x`.

### Baseline regression gate — PASS

`equal` mode reproduces the known baseline **exactly**: 448 trades, +2022.17%
total return, Sharpe 3.41, max drawdown -13.72%, 3/3 pre-registered windows
positive, OOS-window Sharpe 3.02. The `_PositionSizer` machinery is therefore a
confirmed no-op in `equal` mode — any difference in a sized run is attributable
to position sizing alone, not an implementation artifact.

### H1 (inverse-vol sizing) — REJECTED

`inverse_vol` on `prior7_return_volatility` vs the `equal` baseline:

| metric | equal | inverse_vol |
|---|---:|---:|
| trades | 448 | 448 |
| total return | 2022.17% | 1159.77% |
| Sharpe | 3.41 | 2.92 |
| max drawdown | -13.72% | -18.42% |
| avg split Sharpe | 3.62 | 3.07 |
| positive windows | 3/3 | 3/3 |
| train / val / OOS Sharpe | 4.67 / 3.18 / 3.02 | 3.63 / 3.32 / 2.27 |
| train / val / OOS return | 126% / 225% / 183% | 72% / 209% / 127% |

The pre-registered primary metric — **OOS-window Sharpe — fell from 3.02 to
2.27**, and max drawdown **worsened** from -13.72% to -18.42%. The pre-registered
decision rule required OOS Sharpe to rise *and* drawdown not to materially
worsen; both failed. **H1 is rejected.**

This was a genuine reallocation, not a no-op: `position_weight` ranged
0.25-3.03 (mean 0.94, 444/448 distinct). The degradation is not a leverage
artifact — Sharpe is leverage-invariant and it dropped ~25%. The mechanism is
the one anticipated in the Hypothesis section: this strategy shorts the
weakest-liquidity, highest-volatility names, and inverse-vol sizing
systematically underweights exactly the names the edge concentrates in.
Down-weighting volatility here means down-weighting the edge.

### All four modes

| metric | equal | inverse_vol (return-vol) | inverse_vol (intraday-range) | signal_rank |
|---|---:|---:|---:|---:|
| total return | 2022.17% | 1159.77% | 1605.80% | 2217.58% |
| Sharpe | **3.41** | 2.92 | 3.15 | 3.11 |
| max drawdown | **-13.72%** | -18.42% | -20.09% | -18.74% |
| avg split Sharpe | **3.62** | 3.07 | 3.38 | 3.32 |
| positive windows | 3/3 | 3/3 | 3/3 | 3/3 |
| **OOS-window Sharpe** | **3.02** | 2.27 | 2.71 | 2.72 |
| OOS-window return | 183.27% | 126.99% | 183.79% | 168.61% |

- **`inverse_vol` on `prior7_intraday_range_mean`** — a different volatility
  proxy degrades the book the same way: OOS Sharpe 3.02 -> 2.71, drawdown
  -13.72% -> -20.09%. H1's rejection is robust to the choice of vol measure.
- **`signal_rank`** (concentrate into the strongest signals — a separate
  hypothesis, not a vol measure) — posted a higher *total* return (2217.58% vs
  2022.17%), but that is a leverage/concentration mirage: Sharpe 3.41 -> 3.11,
  max drawdown -13.72% -> -18.74%, and the pre-registered primary metric
  **OOS-window Sharpe fell 3.02 -> 2.72**. Higher raw return at worse
  risk-adjusted return and deeper drawdown is not an improvement — equal
  weighting can simply be levered to the same return at a shallower drawdown.

### Verdict — position sizing does not improve the strategy

All three sizing variants were measured against the verified `equal` baseline,
and **every one degrades the strategy**. Equal weighting is best — or tied
best — on every risk-adjusted metric: highest Sharpe (3.41), highest avg-split
Sharpe (3.62), highest OOS-window Sharpe (3.02), shallowest max drawdown
(-13.72%). No variant beat it on the pre-registered primary metric; all three
deepened drawdown. The pre-registered decision rule (OOS Sharpe must rise *and*
drawdown not worsen) is failed by all three.

The result is mechanistically coherent. This strategy's edge is cross-sectional:
it shorts the weakest-liquidity names, which skew small and high-volatility.
Both volatility-aware schemes pull capital *away* from those names — away from
the edge. Concentrating into the strongest signals raises raw return by taking
more concentration risk, but worsens every risk-adjusted measure. The strategy's
existing **equal-weight construction is already the best sizing scheme of the
four tested**; re-allocating the fixed risk budget by volatility or by signal
strength only adds drag or risk.

This closes position sizing — the last untested legitimate axis on this dataset.
Entry parameters (81-scenario sweep), event family (26-variant scan), hedge
overlay, regime gate, and capacity were all tested before; position sizing is
now tested too. None improves the strategy. The `liquidity_migration` short
stands at its genuine, evidence-established frontier on this engine and data.

### Label

`candidate`-grade evidence: full point-in-time universe, costed, split-stable,
ledger-backed; the `equal` run reproduces the audited baseline exactly, and the
sizing rule was a single pre-registered transformation with no tuning. The
verdict it supports is negative — position sizing is not an improvement.
Artifacts: `reports/position_sizing/{equal,inverse_vol,inverse_vol_range,signal_rank}/`.
