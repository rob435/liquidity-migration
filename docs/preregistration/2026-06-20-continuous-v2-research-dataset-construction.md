# Construction Receipt: Continuous V2 — Richer Research Dataset (gates off)

Date: 2026-06-20
Author: Claude (operator-directed next-level research push)
Run label: `exploratory_research_dataset`. **NOT the V2_CONTROL strategy, NOT a candidate, NOT promotion evidence.**

## Purpose (operator direction)

"For research we want to take all the trades we can get — disable the inverse-vol and the
BTC trend gate while researching so we get a better dataset." For mechanism research
(adverse-trade characterization, TWAP entry, dynamic TP) more real fade trades + a clean
equal-weight basis give better statistical power.

## What changed vs the frozen control

`scripts/continuous_v2_research_dataset.py` builds the 3 v2 fade components with TWO gates
relaxed; everything else matches the frozen control so the trades ARE v2 fades:

- `btc_trend_gate = "off"` (control: "uptrend") → include BTC-downtrend entries the live
  strategy skips → MORE trades, full behavior across regimes.
- `sizing_mode = "flat"` (control: "inverse_vol") → equal weight, so the trade rule is
  studied BEFORE position sizing ("perfect the trade first"); inverse-vol sizing is a
  portfolio-construction step applied later.

Unchanged: short, decile 9, rmom q0.25, +1h entry delay, TP 12%, 24h/48h hold, $500k
liquidity floor (kept — a sanity floor, not an alpha gate; removing it would add
untradeable names), funding. Window 2023-04-01 → 2026-06-12, both venues.

## Guardrails

- Output under `backtest-runs/continuous_v2_research_dataset_2026-06-20/`,
  `research_trades_<venue>.csv`. It must NEVER be cited as the V2_CONTROL ledger or as
  strategy/promotion evidence — it intentionally includes trades the strategy would not
  take (BTC-downtrend) and removes the live sizing.
- Use it for: adverse-trade characterization (more power, all regimes), entry-execution
  (TWAP) and dynamic-TP screens (equal-weight). Any per-venue or two-venue CLAIM still
  reduces to the gated V2_CONTROL object + forward demo/paper.

## Use

Re-run the characterization / screens on it via the `--trades` / `--trades-glob` hooks:

```bash
.venv/bin/python scripts/continuous_v2_adverse_characterization.py \
  --trades-glob backtest-runs/continuous_v2_research_dataset_2026-06-20/research_trades_VENUE.csv
```

## Status

Build running in background (gates-off panel is larger than the control). Results
(trade counts per venue, regime split) appended on completion; the gated-vs-ungated
trade-count delta itself quantifies how many trades the BTC-uptrend gate removes.

## No real-money / promotion claim

`REAL_MONEY` stays false. Research dataset only.
