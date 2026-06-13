# Pre-registration: W4 Continuous Stage 0 - Data and Forward Clock

**Date:** 2026-06-13
**Author:** Codex
**Stage:** complete

## What's changing

Run a read-only data/PIT/ancillary audit for both full-PIT roots, then run the
existing frozen continuous forward-replay orchestrator only if the audit does
not expose a stop-work root problem.

## Hypothesis

The per-venue roots are present and cover the registered continuous research
window, while the forward replay state can be advanced or verified without
overlap drift. If not, downstream W4 stages must stop until the data problem is
fixed by documented maintenance.

## Predicted direction + magnitude

- No alpha metric is predicted in Stage 0.
- Expected root state: both roots exist; `klines_1h` and `archive_trade_manifest`
  have date partitions through at least the Stage 1 end boundary
  (`2026-06-10` end-exclusive).
- Expected forward state: overlap verification succeeds; appended days may be
  zero if the state is already current.

Failure mode if wrong: a stale/missing root, stale PIT manifest, missing
ancillary datasets needed by later stages, or forward replay drift blocks only
the stages that depend on that data.

## Roots that will be touched

- [x] bybit_full_pit
- [x] binance_full_pit
- [x] forward demo/paper (readiness state only; no orders)

## Decision rule (a priori)

Stage 1 may run only if both roots exist, both roots expose the registered
`2023-04-01` to `2026-06-10` window, and the cheap PIT coverage check does not
show a missing manifest. If the forward replay orchestrator reports overlap
drift, stop all forward-clock claims and do not append a replacement ledger in
place.

If Binance ancillary June top-ups or forward liquidation/depth data are stale,
that blocks the later liquidation/OI/depth stages only; it does not block Stage
1 stop/exit realism.

## Run command

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python scripts/w4_continuous_data_clock_audit.py \
  --venues bybit,binance \
  --out ~/SHARED_DATA/w4_continuous_stage0_data_clock_2026-06-13

POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python scripts/continuous_forward_replay_orchestrator.py \
  --venues bybit,binance \
  --forward-start 2026-06-10 \
  --state-dir ~/SHARED_DATA/continuous_forward_replay
```

## Post-run results

- Stage 0 audit artifacts:
  `~/SHARED_DATA/w4_continuous_stage0_data_clock_2026-06-13/`.
- Bybit root exists. `klines_1h` and `archive_trade_manifest` end at
  `2026-06-02`; the manifest is stale versus the latest signal trading day
  (`2026-06-12`) but covers the amended Stage 1 historical window.
- Binance root exists. `klines_1h` and `archive_trade_manifest` end at
  `2026-04-30`; the manifest is stale versus the latest signal trading day
  (`2026-06-12`) but covers the amended Stage 1 historical window.
- Forward replay orchestrator transcript:
  `~/SHARED_DATA/w4_continuous_stage0_data_clock_2026-06-13/forward_replay_orchestrator_2026-06-13.txt`.
  It verified overlap (`bybit=695` days, `binance=663` days), appended `0`
  days on both venues, and reported `forward_days=0` because both local roots
  end before the `2026-06-10` forward start.
- Bybit native basis/funding coverage is full enough for historical stop/exit
  work through the amended window; native OI is partial, so OI/depth/liquidation
  stages remain blocked/gated separately.
- Binance kline/funding coverage is sufficient through the amended common
  historical window; Binance OI/taker-flow are only recent-window proxy
  datasets and do not support a historical OI/depth/liquidation verdict.

## Verdict

PASS for Stage 1 only after amendment to the common available historical window
ending `2026-05-01` exclusive. FAIL for any claim that needs current forward
root freshness, June Binance ancillary data, or mature local forward
liquidation/depth captures.
