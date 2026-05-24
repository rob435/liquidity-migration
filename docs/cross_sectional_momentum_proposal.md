# Cross-Sectional Momentum Sleeve — Design Spec

**Status:** v1 design, code in flight. Adapted from the equities cross-sectional
momentum literature (Jegadeesh & Titman 1993; Asness, Moskowitz & Pedersen 2013;
Clenow 2015 *Stocks on the Move*) with crypto-specific event triggers replacing
calendar rebalance.

The existing event-driven short sleeve (`liquidity_migration/volume_events.py`)
captures regime-conditional liquidity-migration shorts. This sleeve is its
**long counterpart**: independent signal, opposite direction, longer holding
horizon, event-triggered.

## Why cross-sectional momentum exists in crypto

- Liu & Tsyvinski (2021) "Risks and Returns of Cryptocurrency" — significant
  cross-sectional momentum effect across coins, with stronger persistence in
  larger / higher-volume names.
- Liu, Tsyvinski & Wu (2022) — proposed 3-factor model includes a momentum
  factor.
- Slow information diffusion + retail attention cascades. Most pronounced in
  the **upper tier** of liquidity; degenerates into lottery-like reversal in
  the long tail (consistent with Asness 1997 / Hong-Lim-Stein 2000 in equities).

## Design constraints

1. **Cross-sectional, long-only.** A short counterpart to the existing short
   sleeve.
2. **Event-triggered.** No calendar rebalance. Entries fire on multi-condition
   events; exits fire on event triggers. Daily evaluation granularity, hourly
   entry delay.
3. **Liquid universe only.** Top-N by trailing volume. The strategy is
   explicitly NOT trying to ride microcap pumps.
4. **Regime-aware kill switch.** Momentum crashes (Daniel & Moskowitz 2016)
   are a known failure mode — equity 2009, crypto 2022. BTC trend gate.

## Universe (event: liquidity-tier promotion/demotion)

- Source: Bybit USDT-perp universe, PIT membership from
  `archive_trade_manifest`.
- Eligible set on day `d`: top **N=30** by 90-day median USD daily turnover,
  computed on trailing data only.
- Filters: ≥180 days of trading history; no >24h gaps in last 30 days; not in
  `DEFAULT_EXCLUDED_SYMBOLS` (stablecoin pairs).
- **Promotion event:** symbol enters top-30 → becomes a candidate for entry
  events.
- **Demotion event:** symbol drops out of top-30 → force-exit if held (one of
  the six exit events).

## Ranker (continuously computed, only acted on at events)

Default: **Clenow slope × R²** over 90 days.

```
returns_log = log(close).diff().dropna().tail(90)
slope = exp(linreg_slope(t, log_close)) ** 252 - 1   # annualized
r2 = pearson_correlation(t, log_close) ** 2
score = slope * r2
```

The R² factor penalizes coins that "got there by jumping" — pure noise without
a real trend. This is the canonical *Stocks on the Move* metric. Alternative
ranker available behind a flag: trailing 90-day Sharpe-like (`mean / vol`).

Cross-sectional rank within the eligible 30, normalized to `[0, 1]`
(0 = worst, 1 = best). Updated daily; never used directly except inside an
event check.

## Entry events (all must fire on the same daily bar)

1. **Coil-release.** 30-day realized vol (from daily log-returns) crosses
   above 90-day realized vol on the current day, after sitting below it for
   ≥7 consecutive prior days. Captures volatility-compression → expansion.
2. **Rank threshold.** Cross-sectional rank in the top quartile of the
   universe (rank ≥ 0.75).
3. **Breakout confirmation.** Daily close > prior 60-day high.
4. **Regime gate.** BTCUSDT close > BTCUSDT 200-day SMA. If off, no new
   entries; existing positions managed normally until exit events.
5. **Funding sanity.** Last 8h funding rate ≤ 95th percentile of trailing
   90-day funding distribution for that symbol. Avoids buying into peak
   leverage-long crowding.

Entry executes 1 hour after the daily close that fired the events (mirrors the
short sleeve's `entry_delay_hours=1` convention — the signal close is not an
executable fill, integrity gate #2).

## Exit events (any one fires)

1. **Rank decay.** Cross-sectional rank drops below median (rank < 0.50).
2. **Trend break.** Daily close < 100-day SMA.
3. **Vol shock.** Realized 1-day return magnitude > 3× trailing 30-day median
   absolute return.
4. **Regime break.** BTC < 200-day SMA → entire sleeve liquidates.
5. **Universe demotion.** Symbol drops out of top-30 by 90d median turnover.
6. **Trailing ATR stop.** Daily close < (highest close since entry) − 4 ×
   30-day ATR.

All exit checks evaluated at daily close; exit executes at the same close
(intra-bar trailing-stop modeling is a v2 refinement, integrity gate #14).

## Sizing

- `max_concurrent_positions = 8`
- `notional_weight = gross_exposure / max_concurrent_positions = 0.125` per
  slot
- `position_weight = clamp(vol_floor / vol_estimate, 0.25, 4.0)` then
  mean-normalized to 1.0 across the active universe (vol-parity within slot
  budget — same `_PositionSizer` shape the short sleeve uses)
- `vol_estimate` = 90-day annualized realized vol of log-returns; floored at
  30% annualized to prevent the lowest-vol coin from absorbing the whole book.
- `max_position_weight` clamp at 25% of book (after vol-parity rescaling).

## Costs

- Reuse `CostConfig` (maker/taker/slippage blend).
- `cost_multiplier = 3.0` default (matches the short sleeve's conservatism).
- **Funding** modeled via `_perp_funding_return` from `trade_lifecycle.py`.
  Long perps pay funding most of the time in bull regimes; modeled cost is
  load-bearing.

## Data dependencies

| Dataset | Canonical research root | Bybit OOS pre-2023 | Binance OOS PIT |
|---|---|---|---|
| `klines_1h` | yes | yes | yes |
| `funding` | yes (2023-05+) | **missing** | **missing** |
| `archive_trade_manifest` | yes | yes | yes |

On OOS roots, funding cost is not modeled; reports flag
`funding_mode = "missing"` and the run label downgrades accordingly. No new
dataset needed for v1 — daily ranker, ATR, and SMA values are computed
in-memory from `klines_1h`.

## Run labels & promotion gates

Mirrors the short sleeve:

- `pit_required_missing_manifest` — archive empty, integrity broken.
- `pit_membership_filtered_current_universe` — manifest present but feature
  universe doesn't cover the manifest symbol set. Diagnostic only.
- `full_pit_universe` — manifest present, full coverage. Required for any
  promotion claim.

Promotion gates (must all hold):
- All 3 splits (`train_2023_2024`, `validation_2024_2025`, `oos_2025_2026`)
  positive net of costs and funding.
- `max_drawdown >= -25%`.
- Mean per-split Sharpe-like ≥ 0.50.
- `full_pit_universe_pass = True`.
- `funding_mode != "missing"` on the canonical root run.

## OOS plan (locked before tuning)

1. **First run** — canonical research root (`~/SHARED_DATA/bybit_fullpit_1h`),
   full window 2023-05-03 → 2026-05-18, default config. Label `exploratory`.
2. **Splits hold or not.** If all three splits positive, advance to (3).
   Otherwise document and stop — no parameter mining.
3. **Bybit OOS pre-2023** (`~/SHARED_DATA/bybit_oos_pre2023`). Funding
   missing — flagged. Label `exploratory_oos`.
4. **Binance OOS PIT** (`~/SHARED_DATA/binance_oos_pit`). Funding missing —
   flagged. Cross-venue validation.
5. **Candidate label** only if both OOS roots show the splits-positive
   property with the same default config. **No threshold tuning on OOS data
   (integrity gate #18).**

Limited tuning grid (3 ranker lookbacks × 2 ranker types), to keep degrees of
freedom small and avoid the multiple-testing trap (integrity gate #19).

## Integrity gate map

| Gate | Mitigation in v1 |
|---|---|
| #1 future universe | PIT membership from `archive_trade_manifest`; liquidity tier from trailing 90d only |
| #2 future info in signals | Ranker uses closed daily bars; entry delayed 1h |
| #3 instantaneous trading | 1h entry delay; daily close-to-close exit fills |
| #4 non-PIT data | Canonical / OOS roots only; `--allow-partial-pit` is biased-diagnostic only |
| #5 capacity | Per-position weight capped at 25%; no impact model declared as gap |
| #6/#7 fees/slippage | Reused `CostConfig`; `cost_multiplier = 3.0` default stress |
| #8 market impact | Not modeled in v1, declared in report |
| #9/#10 borrow/funding | Long-only avoids borrow; funding modeled on canonical, declared missing on OOS |
| #11 venue restrictions | Inherits via PIT manifest membership |
| #12 instrument lifecycle | Universe demotion exit; min listing history blocks prelist |
| #13 timestamp/resampling | Daily resample = UTC midnight close from 1h bars |
| #14 impossible OHLC paths | Trailing stop checked at close only (no intrabar fill assumption) |
| #15 warm-started state | High-water-close, entry_ts, cooldown persisted to ledger |
| #16 backtest ≠ forward | Demo daemon extended with a `strategy_tag` (deferred to v2) |
| #17 parameter mining | 3 lookbacks × 2 ranker types only; everything else fixed |
| #18 OOS reuse | OOS roots run **once** with the candidate config from canonical splits |
| #19 multiple testing | Small grid; promotion requires splits AND OOS, not best-of |
| #20 accounting | Reuses `summarize_baskets` / `build_equity_curve` / `summarize_trade_backtest` |
| #21 one basket = one bet | Max 8 concurrent positions; report shows held-symbol correlation |
| #22 venue mechanics | Funding settlement boundaries respected by `_funding_lookup` |
| #23 pretty-report bias | Full trade ledger + run_label + config hash written |
| #24 live drift | Reconciliation deferred until demo daemon integration |
| #25 all-or-nothing | Per-year runs supported via `--start`/`--end` |

## CLI shape

```
python -m liquidity_migration cross-sectional-momentum \
  --data-root ~/SHARED_DATA/bybit_fullpit_1h \
  --start 2023-05-03 --end 2026-05-18 \
  [--ranker clenow_slope_r2|sharpe_90d] \
  [--ranker-lookback-days 90] \
  [--liquidity-tier-size 30] \
  [--max-concurrent-positions 8] \
  [--cost-multiplier 3.0] \
  [--allow-partial-pit]   # biased-diagnostic only
```

## Package layout

```
liquidity_migration/
  momentum_signals.py          # universe filter, Clenow ranker, vol estimate, ATR, SMA
  momentum_events.py           # entry/exit event detectors
  cross_sectional_momentum.py  # main backtest entry, report, run_label
tests/
  test_liquidity_migration_momentum_signals.py
  test_liquidity_migration_momentum_events.py
  test_liquidity_migration_cross_sectional_momentum.py
```

Reused without modification: `data_layer.py`, `storage.py`, `archive_manifest.py`,
`config.py` (`CostConfig`), and the trade-ledger helpers in
`trade_lifecycle.py` (`summarize_baskets`, `build_equity_curve`,
`summarize_trade_backtest`, `_funding_lookup`, `_perp_funding_return`,
`_bar_excursion`, `_side_return`, `_stop_price`).

## What v1 does NOT do (declared gaps)

- Intrabar trailing stop fills — checked at close only.
- Market impact model — assumed zero.
- Live demo daemon integration — backtest only.
- BTC regime measure is borrowed from Clenow (200d SMA on BTC); a
  pre-registered alternative (e.g., 30d vol percentile) is logged here so we
  don't post-hoc swap to the better-looking one.
- No cross-strategy correlation gating with the short sleeve — that's a v2
  portfolio-overlay concern.

## Pre-registered v2 work (after v1 ships)

1. Intrabar trailing stop fill model (touch the close-since-entry high
   intrabar, check stop on the same bar).
2. Demo daemon multi-strategy support.
3. Portfolio overlay combining momentum-long + liquidity-migration-short with
   shared risk budget.
4. Funding backfill for OOS roots (separate data infra task).
