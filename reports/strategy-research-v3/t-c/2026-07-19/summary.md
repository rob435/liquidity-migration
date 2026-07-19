# T-C — Pump-deceleration entry timing (exploratory, Lane 1)

**Status: EXPLORATORY.** Counterfactual post-processing of the spent V2 discovery
surface. No alpha, robustness, candidate, or promotion claim.

## What ran

- Input: frozen V2 barebones CONTINUOUS ledger (16,745 shorts) + the shared
  1h kline slice from the full-PIT root (6,452,755 bars, validated to reproduce
  the official daily curve exactly).
- PIT features at the entry bar close (definitions in `manifest.json`):
  `r_1h`, `mom_delta` (momentum of momentum), `hours_since_high_168h`.
  States: accelerating (`r_1h>0 & mom_delta>0`), decelerating (`r_1h>0 &
  mom_delta<=0`), pullback (`r_1h<=0`). All 16,745 entries classified (0 unknown).
- Diagnostic: MAE / deep-MAE / exit mix / net by state × era.
- Counterfactuals: skip-accelerating; delay-until-deceleration (wait 4h / 12h,
  entry re-priced from the kline at the delayed bar, same exit logic: TP 12%
  below the new entry, 24h max hold, funding re-joined per settlement).
- The barebones sleeve has **no stop-loss exits** (the live stop-out cluster that
  motivated this thesis is a deployed-profile behavior); deep MAE (< −10% / −15%)
  is the declared stop proxy. Era split at 2023-02-22.

## Results

**Diagnostic — the mechanism's premise fails on this ledger.** Adverse paths do
NOT concentrate in accelerating entries; decelerating entries are the worst
bucket in every era:

| State (full) | Trades | Mean net (bps/trade) | Mean MAE | MAE<−10% | TP rate |
|---|---:|---:|---:|---:|---:|
| accelerating | 4,789 | −0.06 | −7.7% | 23.9% | 15.6% |
| decelerating | 2,325 | **−0.62** | **−9.9%** | **31.4%** | 16.1% |
| pullback | 9,631 | −0.03 | −8.3% | 25.2% | 15.9% |

The ordering (decelerating worst, accelerating least-bad) is identical in the
early and late eras.

**Counterfactuals (baseline: −20.23% net, −38.74% maxDD):**

- `skip accelerating`: −17.34% (+2.89pp; early +1.36pp, late +1.53pp),
  maxDD −31.9%. But per-trade net **worsens** (−0.145 vs −0.121 bps full;
  −0.288 vs −0.216 late) and deep-MAE share **rises** (26.4% vs 25.7%): the
  improvement is a mechanical 28.6% trade-count reduction on a negative-mean
  sleeve, not a path-quality gain. Removed trades were near-net-flat in
  aggregate (winners +155.2%, losers −158.1% of capital, sum −2.89%).
- `delay 4h` / `delay 12h`: −20.85% / −20.99% (worse than baseline in both
  eras; 4,788/4,789 accelerating entries find a deceleration bar within 4h, so
  the rules converge). Re-priced delayed entries lose slightly more than the
  originals; MAE distribution essentially unchanged (−8.33% vs −8.36% mean).

## Read

At 1h feature granularity on the barebones shape, the deceleration-timing thesis
is **not supported**: the diagnostic contradicts the premise, and the only
"improving" rule improves totals for reasons unrelated to its mechanism while
making the surviving book worse per trade. This does not refute the live 2026-07-19
stop-out observation itself — that occurred on the deployed profile with
different exit geometry and finer granularity — but this ledger provides no
support for a deceleration entry gate.

## Limitations

- 1h bars; sub-hourly acceleration structure invisible.
- No stop-losses in the barebones shape; stop-outs proxied by deep MAE only.
- Delayed entries keep the original modeled cost; capacity/admission unmodeled.
- Spent discovery surface; nothing here is out-of-sample.

## Next action

No prototype advances from this thesis. If pursued at all, it needs the deployed
profile's exit geometry and finer-grained data, as a separate declared design.

Artifacts: `tc_bucket_diagnostic.csv`, `tc_grid.csv`, `tc_trade_features.parquet`
(local; hash in `manifest.json`), manifest with feature definitions and grids.
