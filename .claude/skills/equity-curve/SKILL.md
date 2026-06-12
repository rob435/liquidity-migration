---
name: equity-curve
description: "Produce equity curves for the promoted LONG v11a sleeve from its EXACT deployed profile, and the official strategy-vs-BTC PNG. Use when asked for any sleeve's equity curve, to backtest the promoted/deployed profile, to get the equity chart PNG, or to compare sleeves/venues. The zero-friction path for both sleeves is scripts/equity_curves.sh (profiles from liquidity_migration/promoted.py); the long-only deep-dive is scripts/long_native_sweep_fc_min_day.py. Covers per-venue full-PIT roots, outputs, and run-label interpretation. (Continuous was de-promoted 2026-06-05 and is no longer part of the equity tool.)"
---

> **ERASURE NOTE (2026-06-11, operator order):** the daily SHORT sleeve was
> ERASED from the system — `volume-events` backtest, `event-demo-cycle`,
> `event_demo_daemon`, `short_profile`, `volume_events_cell.sh`, short deploy
> units and short reconcile commands NO LONGER EXIST. Ignore any instruction
> below that references them; long + continuous guidance still applies.

# Equity curves — promoted profiles, one command

**For ANY/ALL sleeves' deployed-profile equity curve, use the zero-friction tool:**

```bash
bash scripts/equity_curves.sh                 # the promoted LONG sleeve, last 3 years
bash scripts/equity_curves.sh --years 2       # shorter window (lighter on the 16 GB box)
```

It runs the promoted sleeve's EXACT deployed profile — sourced from the single source
of truth `liquidity_migration/promoted.py` (`long_profile`; `PROFILES == {"long"}`
since the 2026-06-11 short-sleeve erasure, pinned by `tests/test_promoted_profiles.py`)
— emits the equity PNG, and prints the `run_label` for the run (a biased/partial-PIT
result is flagged, never hidden). No flag archaeology, no "what's deployed?" guessing.
Use `--long-notional-multiplier N` to draw the long curve at a higher (e.g. 5x)
sizing — pure leverage on the same signal.

The rest of this skill is the **long-only deep-dive** — use it when you need more than the
one-command run.

---

# Long-only sleeve equity curve + official PNG (deep-dive)

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
| Bybit | `~/SHARED_DATA/bybit_full_pit` | funding dataset named `funding`, 764 symbols → funding modeled |
| Binance | `~/SHARED_DATA/binance_full_pit` | `binance_usdm_funding` rebuilt 2026-06-09: 697 symbols / ~2.23M rows with true settlement intervals → funding modeled |

**Funding now auto-resolves — no symlink/rename needed.** As of the run-diagnostics
refactor, `read_dataset(root,"funding")` transparently falls back to the
venue-specific dataset present on the root (`binance_usdm_funding`) when a canonical
`funding/` dir is absent (`storage.resolve_dataset_name`). So `binance_full_pit` is
funding-readable directly; you do **not** need `binance_full_pit_strategy` (it does
not exist on this box) or a hand symlink. The old ~51-symbol partial-coverage caveat
is GONE since the 2026-06-09 rebuild (receipt
`docs/preregistration/binance-funding-rebuild-2026-06-09.md`; pre-rebuild dataset
kept as `binance_usdm_funding.pre_rebuild_2026-06-09.bak`) — all future Binance
numbers use the full-coverage basis. A `FUNDING_PARTIAL` warning on a new run now
indicates a real gap worth investigating, not the known-old coverage hole.

Every run now prints a named **warnings block** (and the report JSON carries
`warnings[]` + a machine `tainted` bool) — read that instead of decoding `run_label`
by hand. `tainted: true` (e.g. `PIT_SURVIVORSHIP`) means survivorship/look-ahead
biased → not citable; data-gap warnings (`FUNDING_PARTIAL`, `WINDOW_CLIPPED_*`) are
non-blocking and tell you exactly what to backfill.

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
  gate) come from `_v11a_long_native_config()` in `liquidity_migration/long_native_event_demo.py`
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
skill. Fix Bybit kline gaps with `archive-download-klines-1h`; fix
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
- `run-strategy` — the rest of the CLI (data builders, audits, forward runners).
