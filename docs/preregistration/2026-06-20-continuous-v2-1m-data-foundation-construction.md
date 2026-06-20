# Construction Receipt: Continuous V2 Next-Level — Wave 1, 1m PIT Data Foundation

Date: 2026-06-20
Author: Claude (operator-directed next-level research push)
Stage: construction (data build + audit)
Parent plan: `docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md`
Run label: `exploratory` (data foundation; no result claim)

## Scoping decision (deliberate deviation from "full 1m root")

The plan's D1/D2 describe a *full-universe* 1m root (1440 rows/day for every
symbol, all history). That is tens of GB and dominated by symbol-days the
continuous book never trades. Phase 2's intrabar engine and Books A (stops/TPSL),
C (TWAP), E (dynamic TP) only need 1m path fidelity **within each trade's life
plus a pre-entry context day**. So Wave 1 builds a **trade-window-scoped** 1m
cache, sized directly from the Phase 0 trade ledgers:

| venue | trades | symbols | symbol-day partitions | est. download |
|-------|-------:|--------:|----------------------:|--------------:|
| bybit | 2367 | 351 | 2401 | ~141 MB |
| binance | 2149 | 334 | 2238 | ~131 MB |

(Partition set = for every trade, `[entry_date − 1 … exit_date]`; the −1 day
feeds pre-entry admission/exhaustion features for Book B.) This is tractable
and sufficient; a full-universe 1m root is **deferred** as a registered optional
extension if a future book ever needs universe-wide 1m (none of Books A–I do).

Coverage manifests (persisted, resumable):
`~/SHARED_DATA/continuous_v2_1m/coverage_needed_{bybit,binance}.parquet`.

## Sources (both reachable + checksum-valid from this box, verified 2026-06-19/20)

- **Bybit**: `public.bybit.com/trading/<SYM>/<SYM><DATE>.csv.gz` (actual executed
  trades) → `ingestion.aggregate_trade_klines_1m` → `ingestion.densify_trade_klines_1m`.
  PIT-safe (historical trades, no revision). V5 kline API is gap-fill/backstop only.
- **Binance**: `data.binance.vision/data/futures/um/daily/klines/<SYM>/1m/<SYM>-1m-<DATE>.zip`
  + `.CHECKSUM`, validated via the existing `binance_vision._fetch_expected_sha256`
  + `_verify_download` (sha256 gate); reuse the monthly-1h CSV parser shape (same
  columns at 1m). This is the Vision CDN archive, NOT FAPI REST — not region-gated.

## Output

`~/SHARED_DATA/continuous_v2_1m/<venue>/klines_1m/date=<D>/symbol=<S>/...parquet`
(separate cache root — NEVER mixed into the full-PIT roots, so a trade-window
cache can never be mistaken for a dense full-universe `klines_1m`).

## Data-quality ledger (D4)

`~/SHARED_DATA/continuous_v2_1m/audit_<date>/`:
- `coverage_by_symbol_date.csv` — per (symbol,date): rows_built, expected (1440 for
  a complete UTC day), complete flag, source.
- `gap_minutes.csv` — minutes missing on incomplete partitions.
- `missing_partitions.csv` — needed (symbol,date) with NO source data (delisted /
  pre-listing / archive gap), with reason. **Gaps are ledgered, never silently
  filled.**
- `source_identity.json` — source URLs, checksum mode, build command, git commit.

## Acceptance

- Every needed partition is either built (with row count + completeness flag) or
  recorded in `missing_partitions.csv` with a reason. No silent skips.
- Binance partitions are sha256-checksum-validated against the `.CHECKSUM` sidecar.
- Complete UTC days have 1440 1m rows; incomplete days have their gap minutes
  ledgered (lifecycle/listing gaps allowed and recorded, not errors).
- Open/incomplete current-day candles are excluded from decision use (Phase 2).
- A symbol-level + venue-level pass/fail summary is produced and cited before any
  intrabar A/B run.

## Build command (pre-registered)

```bash
.venv/bin/python scripts/continuous_v2_build_1m_trade_windows.py \
  --venues bybit,binance --resume \
  --ab-root backtest-runs/continuous_v2_phase0_freeze_2026-06-19
```

## Stop conditions

- If a venue's missing-partition fraction is high enough that intrabar tests would
  be dominated by data absence rather than mechanism, stop and write a verdict
  (distinguish "alpha failed" from "data did not exist").
- Checksum failures that cannot be resolved by retry are a hard stop for that
  partition (ledgered, never accepted unverified).

## No real-money / promotion claim

Data construction only. `REAL_MONEY` stays false.
