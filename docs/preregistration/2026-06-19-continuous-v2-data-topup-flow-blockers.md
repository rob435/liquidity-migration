# Continuous V2 Data Top-Up Receipt - Flow/OI Blockers

Date: 2026-06-19

Run label: `feature_almanac_data_proof`

Scope: respond to the A4/C2/C3 data blockers for open interest, taker flow,
market flow residualization, `flow_resid_return`, and `flow_squeeze`.

This is a data-admissibility receipt, not an alpha test and not promotion
evidence. It does not approve any new demo/paper or real-money arm.

## Work Performed

- Repaired `scripts/backfill_binance_metrics_vision.py` so Binance
  `data.binance.vision` metrics archives can resume by symbol tail instead of
  treating existing symbol parquet files as permanently complete.
- Backfilled Binance USD-M metrics archives into
  `~/SHARED_DATA/binance_full_pit/binance_usdm_metrics_5m`.
- Wired the continuous v2 feature almanac to read Binance metrics archives for
  open interest and taker long/short volume ratio.
- Built Binance `market_flow` from the full metrics symbol set and
  `idiosyncratic_flow` as symbol flow minus that market aggregate.
- Attempted a broad Bybit OI tail refresh through 2026-06-19; it was too slow
  for a single all-symbol pass but did refresh a partial tail and a BTCUSDT
  spot check.
- Re-ran the two-venue feature almanac:
  `backtest-runs/continuous_v2_feature_almanac_2026-06-19_flow_topup`.

## Evidence

Binance metrics backfill no-op after repair:

```text
symbols todo: 0 (of 776 with klines) symbol-days: 0
DONE: 764 symbols, 116,511,323 rows in .../binance_full_pit/binance_usdm_metrics_5m
```

Two-venue almanac coverage for the relevant blockers:

```text
bybit   oi_level            0.7298461047763336  false
bybit   oi_change_24h       0.7298461047763336  false
bybit   oi_acceleration     0.7298461047763336  false
bybit   taker_imbalance_1h  0.47316800893070726 false
bybit   taker_imbalance_6h  0.47316800893070726 false
bybit   taker_imbalance_24h 0.47316800893070726 false
bybit   market_flow         0.47316800893070726 false
bybit   idiosyncratic_flow  0.47316800893070726 false
bybit   flow_resid_return   0.0                 false
bybit   flow_squeeze        0.0                 false
binance oi_level            1.0                 true
binance oi_change_24h       1.0                 true
binance oi_acceleration     1.0                 true
binance taker_imbalance_1h  1.0                 true
binance taker_imbalance_6h  1.0                 true
binance taker_imbalance_24h 1.0                 true
binance market_flow         1.0                 true
binance idiosyncratic_flow  1.0                 true
binance flow_resid_return   0.0                 false
binance flow_squeeze        0.0                 false
```

Focused verification:

```text
pytest -q tests/test_continuous_v2_ab_research_runner.py tests/test_scripts_backfill_binance_metrics_vision.py
20 passed

ruff check scripts/continuous_v2_ab_research_runner.py scripts/backfill_binance_metrics_vision.py tests/test_continuous_v2_ab_research_runner.py tests/test_scripts_backfill_binance_metrics_vision.py
passed
```

## Verdict

Binance OI and taker-flow coverage are no longer the blocker for the foundation
almanac. Binance full-market flow and idiosyncratic flow are value-built and
admissible in the almanac through the current Binance root kline span.

The original A4/C2/C3 arms remain blocked for a serious two-venue decision.
Bybit OI is still partial, Bybit taker-flow remains event-scoped rather than a
full-market tape, and both venues still lack value-built `flow_resid_return`
and `flow_squeeze`.

## Next Required Work

The operator later amended the plan to allow Binance-only exploratory C-book
flow research without waiting for Bybit. Before citing C2/C3 as serious
two-venue evidence, build and audit a resumable Bybit full-market taker-flow
archive backfill from public trade archives, then define causal constructions
for `flow_resid_return` and `flow_squeeze` in a fresh pre-registration or
amendment.
