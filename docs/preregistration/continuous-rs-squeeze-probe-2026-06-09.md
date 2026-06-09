# Pre-registration: WP1a — alt-vs-BTC relative-strength squeeze diagnostic (continuous short)

**Date:** 2026-06-09 (registered BEFORE the run)
**Run label:** `exploratory` (diagnostic; gates WP1b — never promotion evidence on its own)
**Program:** `docs/research_plan_continuous_regime_2026-06-09.md` WP1a
**Code state:** commit 5e1c960 + uncommitted working tree (operator hold on commits)

## Hypothesis

The continuous short's squeeze days are driven by alt-season (alts outperforming BTC,
i.e. BTC-dominance falling). Therefore trailing alt-vs-BTC relative strength (RS),
computed causally, predicts NEGATIVE forward strategy returns.

True BTC.D (a market-cap share, e.g. 58.56 on 2026-06-09) is not derivable from the perp
roots (no circulating-supply data). The tradeable proxy for its *change* is relative
performance: `RS_d = EW_alt_return_d − BTC_return_d`. We need the direction/trend of
dominance, not the level, so the proxy is the correct research object.

## Data (verified before registration)

- `~/SHARED_DATA/{venue}_full_pit/feature_panel_2026-05-27.parquet` — per-symbol daily
  `close` (last 1h close of UTC date) and `turnover_quote` (daily sum). Spot-verified
  float-exact vs raw `klines_1h` on 3 dates x 2 venues (6/6 exact). Coverage
  2021-01-01..2026-04-29; PIT-inclusive (721/696 symbols incl. delisted; membership per
  day = symbol had bars that day).
- Bybit tail 2026-04-30..2026-05-26 rebuilt identically from raw
  `klines_1h/date=*/symbol=*/` partitions (same aggregation: last close, sum turnover).
- Winner equity ledgers (benchmark book, reproduced bit-exact at commit 5e1c960):
  `~/SHARED_DATA/continuous_robustness_2026-06-09/scale_sensitivity/{venue}/winner_base/w90_tv0.045_max10_ddh-0.04/equity.csv`.
  Verified: `basket_return_d` == `equity_d/equity_{d-1} − 1` (max err ~1.7e-16); ledger
  contains IN-MARKET days only (bybit 633 rows 2023-04-05..2026-05-23; binance 569 rows
  2023-04-01..2026-04-29); `scale` in [1,10].

## Factor construction (fixed a-priori)

- Symbol day-d return: `close_d/close_{d-1} − 1`, valid only when both consecutive
  calendar dates have bars (no gap-spanning returns).
- Day-d membership: `turnover_quote(d-1) >= 500_000` AND symbol != BTCUSDT. Membership
  uses PRIOR-day turnover (known at the start of the return window) — causal, no
  survivorship (panel includes delisted names).
- `alt_ew_d` = plain EW mean of member day-d returns (as specified in plan §4).
- `rs_d = alt_ew_d − btc_d` where `btc_d` = BTCUSDT day-d return.
- Trailing predictor `X_w(d) = mean(rs_{d-w} .. rs_{d-1})`, `w ∈ {3,7,14,30}`. Every
  input is known by 00:00 UTC of day d (the last input is close_{d-1}, timestamped
  exactly 00:00 UTC day d; deployment would evaluate the gate at the first intraday
  entry decision >= minutes later).
- Latency-delayed copy: `X'_w(d) = X_w(d-1)` (one full extra day).

## Causality declaration (integrity gate)

`decision_ts` = 00:00 UTC of day d (start of the strategy-return day).
`data_available_ts` of every predictor input <= decision_ts. Outcome day d is the
equity ledger's day-d net return — strictly after decision_ts. No revised data; the
panel is a deterministic aggregate of immutable 1h klines.

## Outcome variables

- **Primary** `Y(d) = basket_return_d / scale_d` on in-market days (per-unit-exposure
  net return; removes the vol-target/scale confound).
- Secondary: raw `basket_return_d` (scaled), and the full-grid variant with flat days
  0-filled (reported, non-gating).

## Statistics

Per venue, per window: Spearman IC(X_w, Y); circular block bootstrap (block=10d,
n=2000, seed=20260609) one-sided p for IC<0; quintile means of Y bucketed by X_14;
squeeze diagnostic: mean percentile-rank of X_14 on worst-decile Y days vs all days.

## A-priori GO bar (to WP1b) — all three on BOTH venues

(a) Spearman IC <= −0.08 for at least one of w ∈ {7,14}, bootstrap p < 0.05;
(b) sign consistency: >= 3 of 4 windows have IC < 0;
(c) top-RS-quintile (by X_14) mean Y < 0.

**MARGINAL** (proceed to WP1b, flagged): both venues IC <= −0.05 with (b) and (c) passing.
**NO-GO** otherwise → fall back to WP2 + WP3 per plan.

Anomaly stop: if the delayed copy X' sign-flips to materially positive on both venues
while the base passes, stop and debug grid alignment before any GO claim (kline
availability latency is seconds, so a hard collapse indicates a bug, not microstructure).

## Sensitivities (reported, non-gating)

EW winsorized at per-day 1%/99%; median alt return; turnover-weighted mean; ex-ETH;
delayed copy X'. Sub-period split (2023, 2024, 2025+) reported for the headline window.

## Artifacts

`~/SHARED_DATA/continuous_rs_probe_2026-06-09/` — factor CSV per venue, report JSON,
this receipt updated with the verdict AFTER the run. Script:
`scripts/continuous_rs_squeeze_probe.py`.

## Verdict (filled in after the run, same day)

**NO-GO — unambiguous.** Trailing alt-vs-BTC relative strength does NOT predict forward
squeezes. Against the a-priori bar (IC <= −0.08, p < 0.05, both venues): every primary
IC is near zero and mostly POSITIVE:

| window | bybit IC (p) | binance IC (p) |
|---|---|---|
| 3d | +0.004 (0.55) | +0.055 (0.93) |
| 7d | +0.061 (0.94) | +0.044 (0.88) |
| 14d | +0.023 (0.71) | +0.007 (0.59) |
| 30d | +0.022 (0.71) | −0.010 (0.42) |

All sensitivities (winsorized/median/turnover-weighted/ex-ETH/delayed/full-grid/raw-Y)
agree. Quintiles are an inverted-U (both extremes slightly worse than middle), not a
monotone decline; squeeze days sit at the 51st RS-percentile (chance). The MARGINAL
bar (−0.05) also fails. No anomaly stop (delayed copy consistent with base).

**Why (supplementary diagnostics, same artifacts):** the mechanism is REAL but
CONTEMPORANEOUS and unforecastable —

- Same-day Spearman(rs_d, y_d) = **−0.26 bybit / −0.30 binance** (and same-day raw
  alt-market return even stronger: −0.30/−0.36). The book bleeds exactly when alts
  outperform BTC that day. Factor construction validated by this.
- Alt-RS has ~zero daily persistence: AR1(rs) = +0.02/+0.03; Spearman(X_7, rs_d) =
  +0.01/+0.03; Spearman(X_14, rs_d) = +0.005/+0.03. Alt-season at daily granularity
  is a martingale — there is no trend to gate on.

**Consequences (per the pre-registered fallback):** WP1b (all three trailing-RS gate
forms) is DEAD a-priori — you cannot time an unforecastable factor. The correct
treatment of the exposure is a HEDGE (WP3), not a gate. Mechanism-driven addition to
WP3 design: compare the BTC-beta hedge against an alt-basket hedge — the book's
contemporaneous correlation to the EW-alt market (−0.30/−0.36) is stronger than to
BTC (−0.22/−0.27). Falls back to WP2 + WP3 per plan.

Artifacts: `~/SHARED_DATA/continuous_rs_probe_2026-06-09/{report.json, {venue}_rs_factor.csv}`;
script `scripts/continuous_rs_squeeze_probe.py`. Negative result is first-class; this
receipt is the record.
