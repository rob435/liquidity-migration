# Continuous Fade BTC Tail Skip With BTC Gate Off

Date: 2026-06-28

Run label: exploratory.

Real money: no. Demo/paper and offline research only.

## Hypothesis

The prior `skip_btc_tail_035` full replay showed a cleaner hard skip on Binance
but a small MAR/drawdown regression on Bybit. Separately, disabling the BTC
regime gate was rejected because drawdown and MAR deteriorated sharply. This
run tests the combined mechanism the operator asked about: replace the 35%
BTC-risk tail sizing with a hard skip while removing the BTC regime entry gate.

The falsifier is simple: if hard-skipping the BTC-risk tail cannot rescue the
BTC-gate-off replay on both venues, then the current BTC uptrend gate should
stay in place and the hard skip should not replace 35% sizing from this evidence.

## Arms

| Variant | Rule |
|---|---|
| `baseline_current` | Current frozen local target: BTC uptrend gate on, BTC-risk tail remains 35% sized. |
| `skip_btc_tail_035` | BTC uptrend gate on, skip entries when external size multiplier is `<=0.35`. |
| `skip_btc_tail_035_btc_gate_off` | BTC regime gate off, skip entries when external size multiplier is `<=0.35`. |

Existing comparator rows are reused from frozen full-replay artifacts. The new
arm must be generated through the same full component + BTC-risk + hedge replay
path, not a candidate-tape shortcut.

## Data And Engine

- Data roots: `~/SHARED_DATA/bybit_full_pit` and `~/SHARED_DATA/binance_full_pit`.
- Engine: full continuous component replay plus BTC-risk sizing plus two-factor hedge.
- Costs/funding/hedge: unchanged from the frozen continuous ensemble refresh.
- Artifact table: `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/tables/skip_portfolio_replay.csv`.

## Decision Rule

Reject the combined arm if either venue loses MAR versus `baseline_current`,
worsens max drawdown versus `baseline_current`, or only works by a large
collapse in component trades. If both venues improve return and MAR without a
drawdown penalty, label it a research candidate only; it still cannot approve
real money or size increases without forward demo/paper arbitration.

## Known Limits

This is not OOS. The validation window has already been used by multiple
diagnostics, so the run can falsify a weak mechanism but cannot certify alpha.
