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

## Results (build complete 2026-06-20)

Gates-off trade counts vs the uptrend-gated V2_CONTROL:

| venue | gates-off | V2_CONTROL | extra |
|-------|----------:|-----------:|------:|
| bybit | 3972 | 2367 | +1605 (+68%) |
| binance | 3685 | 2149 | +1536 (+71%) |

The BTC-uptrend gate removes ~40% of potential fades. Using the extra trades to test
**whether the gate earns its keep** (split gates-off fades by BTC 30d trend at entry):

| venue | regime | n | mean gross | mean MAE | blow-up |
|-------|--------|--:|-----------:|---------:|--------:|
| bybit | uptrend | 2249 | +0.0247 | −0.103 | 9.9% |
| bybit | downtrend | 1723 | +0.0122 | −0.122 | 10.4% |
| binance | uptrend | 2050 | +0.0205 | −0.103 | 9.3% |
| binance | downtrend | 1635 | +0.0034 | −0.131 | 11.4% |

**The gate is validated.** BTC-uptrend fades have 2× (Bybit) to 6× (Binance) the per-trade
edge of downtrend fades, with smaller MAE and lower blow-up. Nuance: downtrend MEDIAN gross
is actually higher, but the MEAN collapses — BTC downtrends are stress regimes where the
fade's LEFT TAIL fattens (high-vol names get run over instead of reverting). So the
characterization's "scary high-vol = best return" holds in calm/uptrend but INVERTS in
stress — which is exactly what the BTC-uptrend gate removes. Disabling the gate for research
gave us the trades to learn WHY the gate works.

### Adverse-characterization confirmation (richer dataset, 1m-feature subset)

Re-ran `continuous_v2_adverse_characterization.py` on the richer dataset. The high-vol /
big-run-up adverse finding is CONFIRMED and slightly stronger (binance run_up_120 IC vs
MAE −0.238, rv_30 −0.227; vs −0.223 / −0.219 on the control). The top-vol-decile gross is
LOWER on the ungated data (binance d10 +0.0015 vs control +0.014) — consistent with the
downtrend left-tail mechanism above.

**Coverage caveat (honest):** the 1m-feature characterization on the richer dataset only
resolved the trades whose windows are in the existing 1m cache (~2150/venue, built for the
V2_CONTROL windows) — the extra ~1550 gates-off trades are NOT in the 1m cache, so the
1m-FEATURE stats are on the cache-covered subset. The BTC-regime split above uses the FULL
gates-off ledger (trade-level mae/gross, no 1m features needed) so it is the complete-sample
result. Extending the 1m cache to the gates-off windows is a registered follow-up if the
full-sample 1m features are needed.

## No real-money / promotion claim

`REAL_MONEY` stays false. Research dataset only.
