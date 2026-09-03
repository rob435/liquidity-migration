---
name: equity-curve
description: Produce and interpret the repository-standard equity curves for the LONG profile, and for a registered Lane-2 carry config through the same chart. Use scripts/research/equity_curves.sh or scripts/ops.sh equity for citable outputs, select the correct full-PIT venue root, distinguish modeled leverage from presentation-only chart leverage, and report run scope and limitations. A standard curve is descriptive evidence, not proof of live-runtime parity, promotion, or authorization.
---

# Equity Curve Generation & Interpretation

## 1. Purpose
Specify execution parameters, sleeve configurations, output schemas, and interpretation standards for producing repository-standard equity curves across LONG, CARRY, and COMBINED portfolios.

---

## 2. Spec Tables

### Equity Curve CLI Parameters (`scripts/research/equity_curves.sh`)

| Parameter | Type | Default | Description | Invariant |
| :--- | :--- | :--- | :--- | :--- |
| `--sleeves` | String | `long` | Target sleeve selection: `long`, `carry`, or `long,carry`. | Determines modeled execution engine. |
| `--root` | Path | `~/SHARED_DATA/bybit_full_pit` | Historical Point-in-Time data directory. | Must contain validated PIT manifest. |
| `--combined` | Flag | `false` | Synthesizes blended portfolio equity curve. | Requires both `long` and `carry` inputs. |
| `--combined-long-multiplier` | Float | `6.0` | Sizing multiplier applied to LONG equity. | Matches live funded deployment dial. |
| `--combined-carry-multiplier`| Float | `3.0` | Sizing multiplier applied to CARRY equity. | Matches live funded deployment dial. |
| `--combined-weight` | Float | Inverse-vol | Fixed capital allocation weight to CARRY (0.0 to 1.0). | When omitted, defaults to risk parity. |
| `--combined-scale` | Float | `1.0` | Visual presentation leverage multiplier on blend. | Visual display only; does not model cost. |
| `--research-config` | Path | None | Explicit registered Lane-2 config (e.g. `configs/lane2_*.json`). | Enforces registered research schema. |
| `--out` | Path | `reports/equity_curves/` | Destination root for generated plots and ledgers. | Output directory created automatically. |

### Sleeve Reconstruction Characteristics

| Sleeve | Underlying Model Engine | Base Authority Config | Deployed Execution Parity | Excluded Mechanics |
| :--- | :--- | :--- | :--- | :--- |
| **LONG** | Native Rust long research runner | Active LONG profile (`v12`) | High parity; matches native reducer. | Microsecond book queue priority. |
| **CARRY** | Cross-venue research runner | `configs/lane2_carry_hold_v7.json` | Moderate; registered rule shape. | Pre-settlement exit boost not modeled. |
| **COMBINED** | Daily equity CSV blender | Weighted blend of LONG + CARRY | High descriptive parity at daily bar. | Intraday cross-margin netting. |
| **EXODUS** | N/A (Demo only) | N/A | None; excluded from standard research curves. | No standardized research series. |

### Output Artifact Schema

| Artifact File | Format | Contents & Purpose |
| :--- | :--- | :--- |
| `equity_curve.png` | PNG | Standard 3-pane plot: cumulative return vs BTC, metric tiles, monthly returns table. |
| `summary.json` | JSON | Key statistics: Sharpe, max drawdown, CAGR, total trades, fee drag, win rate. |
| `daily_equity.csv` | CSV | Daily time series: date, balance, equity, return, drawdown, gross/net fees. |
| `trades.parquet` | Parquet | Granular trade-level execution log with causal timestamps and fill prices. |

---

## 3. Invariants

- **Must Never Create Lookalike Plots**: Ad hoc charts *must never* mimic the standard format (strategy-vs-BTC overlay, metric tiles, monthly table); always use `scripts/research/equity_curves.sh` for citable charts.
- **Must Distinguish Modeled vs Presentation Leverage**: Never report `--combined-scale` as modeled return; scaling without modeled financing drag is purely cosmetic.
- **Must Declare Reconstruction Gaps**: All reports *must* explicitly state differences between backtest assumptions and live execution (e.g., funding capture, queue latency, slippage).
- **Descriptive, Not Authorizing**: An equity curve is descriptive evidence of past rules; it *must never* be treated as automated authorization for live capital allocation.

---

## 4. Operational Recipes

### Generate Standard Single-Sleeve Curves
```bash
# Generate LONG equity curve using full-PIT Bybit data
bash scripts/research/equity_curves.sh --sleeves long --root ~/SHARED_DATA/bybit_full_pit

# Generate CARRY equity curve for registered Lane-2 v7 config
bash scripts/research/equity_curves.sh --sleeves carry --root ~/SHARED_DATA/bybit_full_pit
```

### Generate Deployed Combined Portfolio Curve
```bash
# Blended LONG (6x) + CARRY (3x) equal-risk portfolio
bash scripts/research/equity_curves.sh \
  --sleeves long,carry \
  --combined \
  --combined-long-multiplier 6.0 \
  --combined-carry-multiplier 3.0 \
  --root ~/SHARED_DATA/bybit_full_pit

# Via operator script
scripts/ops.sh equity --combined
```

### Inspect Output Summary Metrics
```bash
cat reports/equity_curves/combined/summary.json | jq '{sharpe: .sharpe, max_drawdown: .max_dd, cagr: .cagr}'
```
