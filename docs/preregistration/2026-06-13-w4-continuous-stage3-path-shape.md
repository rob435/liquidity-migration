# Pre-registration: W4 Continuous Stage 3 - Composite / Path-Shape Measurement

**Date:** 2026-06-13
**Author:** Codex
**Stage:** complete

## What's changing

Measure whether fixed, causal pre-entry path-shape features around the frozen
continuous component entries contain stable information beyond the existing
composite. This stage does not change live weights, entries, exits, sizing, or
thresholds.

## Hypothesis

Some entry events may be distinguishable by the shape of the move into the
confirmed signal. If the effect is real, fixed pre-entry path features should
show same-sign cross-venue information in per-notional forward returns without
depending on a single component or a single time slice.

Failure mode if wrong: path-shape features are venue-specific, unstable across
time, dominated by post-entry adverse-path diagnostics, or no stronger than a
negative-control hash bucket. That blocks only these fixed measurements; it
does not close all composite/path-shape research.

## Population

- Window: `2023-04-01 <= signal_ts < 2026-05-01`.
- Roots:
  - `~/SHARED_DATA/bybit_full_pit`
  - `~/SHARED_DATA/binance_full_pit`
- Base entries:
  `~/SHARED_DATA/{venue}_full_pit/reports/w4_continuous_stage1_stop_exit_2026-06-13/00_frozen_no_stop/{component}/continuous_trades.csv`.
- Components: `turn3p3`, `turn4p3`, `turn4p5`, `age210tp14`.

## Registered causal features

All causal features are computed using bars at or before the confirmed
`entry_signal_ts_ms`, never post-entry path:

- `pre_6h_return`: signal close divided by close 6 hours before signal minus 1.
- `pre_24h_return`: signal close divided by close 24 hours before signal minus 1.
- `pre_6h_upper_extension`: max high over the prior 6 hours divided by signal
  close minus 1.
- `pre_24h_realized_vol`: standard deviation of hourly close returns over the
  prior 24 hours.
- `signal_bar_close_location`: `(close - low) / (high - low)` on the confirmed
  signal bar.

Registered diagnostics, not usable as causal filters in this stage:

- `post_6h_adverse`: max high after entry over the first 6 hours divided by
  entry price minus 1.
- `post_6h_favorable`: entry price divided by min low after entry over the
  first 6 hours minus 1.

Negative control:

- `symbol_hash_bucket`: deterministic symbol hash bucket scaled to `[0, 1]`.

## Measurement

For each venue and feature:

- Pearson IC and Spearman IC versus per-notional net return.
- Tercile spread: top-tercile mean per-notional return minus bottom-tercile
  mean, in bps.
- The same spread by chronological thirds.
- Component-level spread and IC.
- Coverage counts and missing-feature counts.

## Decision rule (a priori)

This stage can only nominate a path-shape feature for a later Stage 3b
intervention receipt. It cannot promote, deploy, or change the live book.

A causal feature is "Stage-3b admissible" only if:

- both venues have at least 500 covered events;
- Spearman IC has the same sign on both venues;
- top-bottom tercile spread has the same sign on both venues;
- absolute pooled top-bottom spread is at least 25 bps per notional;
- at least two of three chronological thirds have the pooled spread in the same
  direction; and
- the absolute pooled spread exceeds the negative-control absolute spread.

If no causal feature clears those bars, this exact fixed path-shape measurement
is rejected. Post-entry diagnostics may explain adverse path, but they cannot be
used to rescue a causal feature.

## Run command

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python scripts/w4_continuous_path_shape_measure.py \
  --venues bybit,binance \
  --start 2023-04-01 \
  --end 2026-05-01 \
  --out ~/SHARED_DATA/w4_continuous_stage3_path_shape_2026-06-13
```

## Required artifacts

- Per-entry event rows with fixed causal features, diagnostics, component,
  venue, and per-notional return.
- Per-feature venue/component effect-size CSVs.
- Chronological-third fragility rows.
- Summary JSON/CSV/Markdown with data-root identity, code hash, effect sizes,
  negative control, and registered falsifier.

## Post-run results

Artifacts:

- Stage summary:
  `~/SHARED_DATA/w4_continuous_stage3_path_shape_2026-06-13/stage3_summary.{json,md}`.
- Per-entry event rows:
  `~/SHARED_DATA/w4_continuous_stage3_path_shape_2026-06-13/stage3_path_shape_events.csv`,
  plus per-venue splits in the same directory.
- Effect-size rows:
  `~/SHARED_DATA/w4_continuous_stage3_path_shape_2026-06-13/stage3_feature_effects.csv`.
- Chronological-third fragility rows:
  `~/SHARED_DATA/w4_continuous_stage3_path_shape_2026-06-13/stage3_thirds.csv`.

Run identity:

- Git HEAD: `e7ce8c81ad076a055aa59d64362333024a78c7af`.
- Code hash:
  `cc306e8d40c08ce059e0402c5ff6fb05272f34e239608192df47367d504fb105`.
- Frozen forward config hash:
  `1fc760f14567a204d73f36d5ffb81243d40196338ec72f9e7b4f137f431f0017`.
- Full-PIT roots: `~/SHARED_DATA/bybit_full_pit`,
  `~/SHARED_DATA/binance_full_pit`.
- Window: `2023-04-01 <= signal_ts < 2026-05-01`.

Causal feature summary:

| Feature | Bybit Spearman | Binance Spearman | Bybit Spread Bps | Binance Spread Bps | Pooled Spread Bps | Registered Decision |
|---|---:|---:|---:|---:|---:|---|
| `pre_6h_return` | 0.1855 | 0.1869 | 86.46 | 179.71 | 134.97 | admissible |
| `pre_24h_return` | 0.2078 | 0.1890 | 174.63 | 85.37 | 188.79 | admissible |
| `pre_6h_upper_extension` | 0.0137 | 0.0514 | -42.04 | 24.87 | 17.25 | rejected |
| `pre_24h_realized_vol` | 0.2059 | 0.1867 | 83.02 | 97.64 | 111.00 | admissible |
| `signal_bar_close_location` | 0.0641 | 0.0544 | 12.80 | 33.32 | 15.85 | rejected |

Negative-control `symbol_hash_bucket` was not benign: pooled absolute spread
was `97.10` bps. The three admissible causal features cleared the registered
"stronger than negative control" bar, but this is a serious confounding warning.
A later Stage 3b intervention must neutralize or explicitly model symbol and
component mix before it can touch sizing or entry decisions.

Chronological-third pooled spreads:

- `pre_6h_return`: `-20.04`, `195.60`, `158.95` bps.
- `pre_24h_return`: `101.49`, `54.17`, `267.17` bps.
- `pre_24h_realized_vol`: `-26.58`, `22.33`, `242.23` bps.

Post-entry diagnostics behaved as expected but are non-causal:

- `post_6h_adverse` pooled spread `-845.39` bps.
- `post_6h_favorable` pooled spread `1091.15` bps.

These diagnostics explain path risk after entry; they cannot be used as
pre-entry filters in this stage.

## Verdict

STAGE-3B ADMISSIBLE ONLY for three fixed causal measurements:
`pre_6h_return`, `pre_24h_return`, and `pre_24h_realized_vol`.

The cleanest candidate is `pre_24h_return` because all chronological thirds
were positive. `pre_6h_return` and `pre_24h_realized_vol` passed the registered
two-of-three rule but had a negative first third, so they need stricter
neutralization in any follow-up.

No live book change is authorized. This is not paper-ready, not promoted, and
not real-money evidence.
