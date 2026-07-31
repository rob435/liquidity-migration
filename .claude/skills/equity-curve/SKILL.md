---
name: equity-curve
description: Produce and interpret the repository-standard equity curves for the active LONG and CONTINUOUS profiles. Use scripts/equity_curves.sh or scripts/ops.sh equity for citable outputs, select the correct full-PIT venue root, distinguish modeled leverage from presentation-only chart leverage, and report run scope and limitations. A standard curve is descriptive evidence, not proof of live-runtime parity, promotion, or authorization.
---

# Produce equity curves

Read current options before constructing a command:

```bash
bash scripts/equity_curves.sh --help
scripts/ops.sh equity --help
```

Use the standard wrapper for outputs intended to be compared or cited:

```bash
bash scripts/equity_curves.sh --sleeves long
bash scripts/equity_curves.sh --sleeves continuous
bash scripts/equity_curves.sh --sleeves long,continuous
bash scripts/equity_curves.sh --root ~/SHARED_DATA/bybit_full_pit --venue bybit
bash scripts/equity_curves.sh --root ~/SHARED_DATA/binance_full_pit --venue binance
```

Derive the time boundary and venues from the user's question or experiment
contract. Do not assume a default window is OOS or that both venues are required.

## Understand the reconstruction

- `long` loads the active LONG profile and runs the long-native research
  engine.
- `continuous` reconstructs the active continuous component book and hedge
  through `scripts/continuous_deployed_equity_refresh.py`.
- Neither curve is automatically a literal daemon replay. Capacity, live state,
  netting, optional overlays, order lifecycle, and deploy environment can differ.
  Read `docs/active_trading_logic.md` and the emitted config before claiming
  parity.
- Runtime profile names confer no evidence status.

## Handle leverage correctly

- Use `--long-notional-multiplier` only to scale the same LONG signal when that
  is the intended comparison.
- Use `--continuous-backtest-leverage` to change modeled component exposure so
  fees, impact, funding, and hedge constraints are recomputed.
- Use `--continuous-chart-leverage` or `--chart-leverage` only for presentation.
  It does not model margin or liquidation and must be labelled as such.
- Use `--continuous-render-only` only when the expected existing CSV is present
  and its identity is known.

Do not describe a partial flag combination as “the live config.” Verify every
material runtime setting and lifecycle behavior.

## Read outputs honestly

Default outputs live under the selected root's `reports/equity_curves/` tree.
Inspect the generated Markdown/JSON, trade ledgers, CSV, config/run identity,
chart subtitle, and run label—not only the PNG.

Apply `backtest-integrity` and `research-report` before drawing a conclusion.
State:

- claim and window;
- venue/population and PIT provenance;
- modeled costs/funding and coverage;
- reconstruction gaps versus runtime;
- modeled versus presentation leverage;
- validity, study mode, and justified conclusion under `AGENTS.md`.

Ad hoc plots are allowed for diagnostics only when they are visually DISTINCT
from the standard layout, clearly labelled non-standard, and never compared as
standard outputs. **Never hand-build a chart that imitates the standard format**
(strategy-vs-BTC overlay, metric tiles, monthly table) in matplotlib or any
other tool — a lookalike is a second format even when labelled, and this exact
mistake was made and reverted on 2026-07-28. If a series needs the standard
format, the wrapper is the only path:

- Registered Lane-2 financed-longs configs render through the SAME standard
  chart via `--research-config configs/lane2_*.json` (repeatable; output under
  `<out>/research/<config_id>/`, labelled RESEARCH / simulation-on-seen-data;
  added 2026-07-28 with `liquidity_migration.financed_longs.research_equity_chart`).

If the wrapper still lacks an option for a recurring citable need, add a tested
option to the wrapper rather than creating a second format.
