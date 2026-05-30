---
name: equity-curve
description: "Produce the long-only (long_native v11a) sleeve's equity curve and the official strategy-vs-BTC PNG, on Bybit or Binance. Use when asked for the long-only / long-native equity curve, to run that backtest, to get the equity-vs-BTC chart PNG, or to compare the long sleeve across venues. Covers the canonical invocation (there is NO CLI subcommand — drive it via scripts/long_native_sweep_fc_min_day.py), the correct per-venue full-PIT roots, the output artifacts, and run-label interpretation."
---

# Long-only sleeve equity curve + official PNG

The one command for the long-only (long_native v11a) equity curve + the official
strategy-vs-BTC PNG is `scripts/long_native_sweep_fc_min_day.py`. Use it instead of
hand-assembling a `long_native` run — there is **no CLI subcommand** (only the
forward demo `long-native-event-demo-cycle` is wired into `python -m liquidity_migration`).

```bash
.venv/bin/python scripts/long_native_sweep_fc_min_day.py \
  --data-root <ROOT> \
  --values <FC_MIN_DAY_RETURN> \
  --report-subdir long_native_v11a_rerun
```

The v11a sleeve (`liquidity_migration/long_native.py`, `run_long_native_research`)
is crypto-native and long-only — separate from the volume-events short sleeve.

- `--values` takes the `fc_min_day_return` value(s) to sweep; pass the canonical
  v11a default (defined in `_v11a_long_native_config()`) for the production curve.
  One value = one run; the script overrides only that param.
- `--config` (default `configs/volume_alpha.default.yaml`) supplies only the
  **cost model**; the strategy config is always v11a.
- Runtime ≈ 100–200 s per venue. Re-run instead of trusting a stale cached
  report whenever the user emphasizes fresh / current / "no bugs" data.

## Data roots — per venue (critical)

| Venue | Root | Why |
|---|---|---|
| Bybit | `~/SHARED_DATA/bybit_full_pit` | funding dataset named `funding` → funding modeled |
| Binance | `~/SHARED_DATA/binance_full_pit_strategy` | has `funding` (~129k rows) → funding partial/modeled |

**Do NOT use `~/SHARED_DATA/binance_full_pit` for this backtest.** Its funding is
stored as `binance_usdm_funding`, so `read_dataset(root,"funding")` returns 0 rows
→ `funding_mode=missing` → not comparable to the Bybit run. The `_strategy` root
has canonically-named datasets and is the proven path (prior long_native reports
live there).

## Outputs — `<ROOT>/reports/<subdir>/fc_min_day_015/`

- **`long_native_equity_btc.png`** — the official equity curve: strategy equity
  vs BTC buy-and-hold, $1-normalized, with a monthly-returns table. **Display it
  with the Read tool** (it renders the image). This is "the official equity curve
  maker" output. Note: BTC's multiple dominates the y-axis, so the strategy line
  can look flat — read the legend multiples, not the visual height.
- `long_native_equity.csv` — per-basket equity / drawdown / basket_return / date.
- `long_native_trades.csv`, `long_native_baskets.csv`, `long_native_monthly.csv`.
- `long_native_research_report.json` / `.md` — run_label, summary, splits,
  event_counts, config.

## Canonical v11a profile (for context when reporting)

- Universe / regime parameters (universe size, turnover lookback, BTC regime
  gate) come from `_v11a_long_native_config()` in `liquidity_migration/long_native.py`
  — read them there rather than trusting a copy here. Membership is PIT-recomputed
  daily, so the count of distinct symbols traded exceeds the universe size as it
  rotates over the years.
- In practice fires `fomo_chase` events; the docstring's capitulation_rebound /
  funding_squeeze / volume_resurrection patterns fire 0 under v11a.
- `require_full_pit_universe=False` → **the run does NOT raise on a PIT failure.
  You MUST read the run_label every time** (see below).

## Run label = the integrity verdict (check every run)

From `long_native._run_label`, best → worst:

- `full_pit_universe` — clean: full-PIT universe + funding modeled.
- `full_pit_universe_funding_partial` / `full_pit_universe_funding_missing` —
  universe clean (no survivorship), funding caveat (costs understated where
  funding is absent).
- `pit_membership_filtered_current_universe` — **full-PIT FAILED → current-universe
  survivorship-biased → throwaway**, never cite as evidence. Caused by a
  kline/manifest coverage gap (e.g. Bybit's early-2021 1h-kline gap: the manifest
  claims symbol-dates the 1h klines don't cover).
- `pit_required_missing_manifest` — archive manifest empty.

A PIT failure means a kline/manifest coverage gap; the run_label and report name
it. To refresh membership and re-check coverage, follow the **`pit-reconcile`**
skill (it drives `scripts/reconcile.sh`, which refreshes the archive manifest and
checks coverage). Fix Bybit kline gaps with `archive-download-klines-1h`; fix
Binance funding gaps by backfilling funding.

## Cross-venue read

Run both venues and compare total return / Sharpe-like / profit factor / max-DD.
Directional agreement across Bybit + Binance is the robustness signal; divergence
flags a regime/microstructure artefact or a data-coverage difference (e.g. one
venue funding-partial, the other funding-modeled; different history start).

## Pairs with

- `backtest-integrity` — apply before trusting any run; the label rules above
  ARE that standard for this sleeve.
- `research-report` — interpret the JSON/MD report and assign a run label.
- `pit-reconcile` — refresh PIT membership / diagnose manifest-vs-kline coverage
  gaps (the official fix for a PIT-failed run_label).
- `run-strategy` — the short/volume-events sleeve and the rest of the CLI.
