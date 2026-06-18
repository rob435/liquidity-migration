---
name: equity-curve
description: "Produce official equity curves for the promoted LONG v11a sleeve and the research-stage CONTINUOUS demo book. Use scripts/equity_curves.sh for long, continuous, or long-vs-continuous comparisons; LONG comes from liquidity_migration/promoted.py, CONTINUOUS delegates to scripts/continuous_deployed_equity_refresh.py and is a promoted-in-code profile by operator override (2026-06-15), demo/paper only - not a real-money or gate-pass claim. Covers per-venue full-PIT roots, outputs, and run-label interpretation."
---

> ERASURE NOTE (2026-06-11, operator order): the daily SHORT sleeve was erased
> from the system. Ignore old short/volume-events commands. The surviving curve
> surfaces are LONG v11a and the CONTINUOUS fade demo book.

# Equity Curves - One Runner

Use the official wrapper:

```bash
bash scripts/equity_curves.sh                              # LONG only, last 3 years
bash scripts/equity_curves.sh --sleeves long               # explicit LONG
bash scripts/equity_curves.sh --sleeves continuous         # continuous demo book
bash scripts/equity_curves.sh --sleeves long,continuous    # side-by-side run
bash scripts/equity_curves.sh --years 2                    # shorter window
bash scripts/equity_curves.sh --start 2023-06-01 --end 2026-06-12
bash scripts/equity_curves.sh --sleeves continuous --chart-leverage 2.5
```

Venue roots:

```bash
bash scripts/equity_curves.sh --root ~/SHARED_DATA/bybit_full_pit --venue bybit
bash scripts/equity_curves.sh --root ~/SHARED_DATA/binance_full_pit --venue binance
```

`--venue` is only needed for the continuous sleeve when the venue cannot be
inferred from `--root`.

## Sleeve Contract

- `long`: the promoted-in-code LONG v11a sleeve. The profile is sourced from
  `liquidity_migration/promoted.py` (`PROFILES == {"long", "continuous"}`) and run through
  `run_long_native_research`. Use `--long-notional-multiplier N` only to draw a
  pure-leverage curve on the same signal.
- `continuous`: the research/demo-stage continuous ensemble reconstruction:
  continuous_ensemble_v2 components plus the banked BTC+ETH 2f hedge, via
  `scripts/continuous_deployed_equity_refresh.py`. It is in `promoted.PROFILES`
  by an explicit operator override (2026-06-15), NOT a demo-arbiter gate pass: it
  is demo/paper ONLY (REAL_MONEY stays false, Tier-3 real-money gate unmet), not a
  real-money claim, and not promotion evidence. The forward demo/paper record is
  the arbiter. Receipt:
  `docs/preregistration/2026-06-15-operator-override-promote-continuous.md`.

Continuous-specific options:

```bash
bash scripts/equity_curves.sh --sleeves continuous --continuous-render-only
bash scripts/equity_curves.sh --sleeves continuous --continuous-chart-leverage 3
bash scripts/equity_curves.sh --sleeves continuous \
  --continuous-frozen-fallback ~/SHARED_DATA/continuous_deployed_equity_refresh_2026-06-12
```

`--continuous-render-only` requires an existing
`<out>/continuous/<venue>/continuous_equity.csv`; it re-renders charts and stats
without rerunning components. The frozen fallback root supplies the component
configs when the original one-off receipt directories are absent.

`--continuous-chart-leverage N` (alias `--chart-leverage N`) writes an extra
pure-leverage continuous chart next to the 1x chart. Default is `4`; pass `1`
to suppress the extra leveraged PNG. This is chart/report leverage only:
margin and liquidation are not modeled.

## Outputs

Default output root:

```text
<ROOT>/reports/equity_curves/
```

Key files:

- LONG: `long/**/long_native_equity_btc.png`,
  `long/**/long_native_equity.csv`, trades/baskets/monthly/report JSON+MD.
- CONTINUOUS: `continuous/<venue>/continuous_equity_btc.png` and
  `continuous/<venue>/continuous_equity.csv`.
- CONTINUOUS also writes `continuous_equity_btc_<N>x.png`,
  `continuous_equity_<N>x.csv`, and `continuous_monthly_<N>x.csv` for the
  requested chart leverage. The 1x outputs are
  `continuous_equity_btc.png`, `continuous_equity.csv`, and
  `continuous_monthly.csv`.

The monthly table on continuous charts shows real entry-month trade counts from
the component trade ledgers, deduped by `(entry_ts_ms, symbol, side)`, not equity
row counts. The runner summary prefers the unlevered continuous PNG when both 1x
and a leveraged chart exist.

## Data Roots

| Venue | Root | Notes |
|---|---|---|
| Bybit | `~/SHARED_DATA/bybit_full_pit` | funding dataset named `funding` |
| Binance | `~/SHARED_DATA/binance_full_pit` | funding dataset named `binance_usdm_funding`; storage fallback resolves it |

Use per-venue full-PIT roots for research curves. Do not point research runs at
live demo or paper ledger roots.

## Integrity Read

Always read the printed `run_label` and chart subtitle.

- LONG labels come from `long_native._run_label`. Clean best case is
  `full_pit_universe`; `pit_membership_filtered_current_universe` is not citable
  unless the runner proves delisted names were traded and explains the conservative
  label.
- CONTINUOUS is labelled `continuous_demo_paper_research_stage` by the equity
  runner. That means comparison/diagnostic curve, not promotion evidence. The
  continuous research window is spent; forward demo/paper is the decision surface.
- Any result with missing PIT, current-universe bias, missing cost artifacts, or
  unexplained synchronization is exploratory/invalid under `backtest-integrity`.

## Long Deep Dive

For a long-only parameter deep dive, use:

```bash
.venv/bin/python scripts/long_native_sweep_fc_min_day.py \
  --data-root <ROOT> \
  --values <FC_MIN_DAY_RETURN> \
  --report-subdir long_native_v11a_rerun
```

Use this only when you need the v11a sweep machinery. For normal equity curves
and long-vs-continuous comparisons, use `scripts/equity_curves.sh`.

## Pairs With

- `backtest-integrity`: apply before trusting or citing a curve.
- `research-report`: interpret generated JSON/MD reports.
- `pit-reconcile`: diagnose PIT manifest or demo/paper ledger issues.
- `run-strategy`: construct other CLI/data-root commands correctly.
