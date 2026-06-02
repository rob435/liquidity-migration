# Continuous-fade strategy-alpha sweep — exit / entry / rebalance (2026-06-02)

**Type:** EXPLORATORY engine ablation (modeled impact; never promotion evidence — forward demo is the
only Tier-3 arbiter). **Tool:** `scripts/alpha_sweep.py` (load each venue's panel+klines ONCE, sweep
config-overrides in-memory). **Discipline:** every verdict read **cross-venue** (bybit + binance,
full-PIT, 2023-04-01→2026-05-28), promising cells **fine-gridded** (plateau-not-spike), metric =
portfolio **daily-MTM** MAR / max-DD / worst-day (`_portfolio_mtm_equity`), MAR-primary. Base config
mirrors the LIVE sleeve (state exit, max_hold 48, ff6, breakeven@10, age30, 25% stop, gross 0.5 /
max_active 25, rmom-low 0.50, liq ≥ $500k, +1h entry). Baselines: **bybit MAR 38.6 / DD 2.6% / ret 304%**;
**binance MAR 30.4 / DD 5.1% / ret 460%**.

## HEADLINE — one robust cross-venue improvement: tighten the rmom gate 0.50 → 0.33 (ENTRY alpha)

Keep only the lowest-residual-momentum **third** within each timestamp (vs the current half). Same
already-validated rmom squeeze-filter (RD1), used tighter — NOT a new mined signal.

| rmom_quantile | bybit MAR | bybit DD | binance MAR | binance DD |
|--|--|--|--|--|
| 0.50 (base) | 38.6 | 2.6% | 30.4 | 5.1% |
| 0.40 | 38.1 | 2.2% | 39.6 (+31%) | 3.4% |
| **0.33** | **42.9 (+11%)** | **1.8%** | **50.1 (+65%)** | **2.3%** |
| 0.25 | 50.0 (+29%) | 1.3% | 32.8 (+8%) | 2.9% |

**Robust:** up on BOTH venues at q33, DD down on both, neighbours also above base (bybit monotonic
q40<q33<q25; binance q40/q33/q25 all > base, peak q33) — a gradient/plateau, not a lone cell. Mechanism:
rmom is the squeeze filter; tightening removes the most squeeze-prone (high-rmom) shorts — exactly the
strategy's #1 risk. **It is selection quality, not thinning:** q33 keeps ~64-76% of return on ~56-71% of
trades while halving DD (the removed trades were return-light, DD-heavy). **Cost:** return −22% (bybit) /
−24% (binance) — MAR-primary (the binding metric) is strongly up. **Recommend:** set the live sleeve's
`rmom_quantile` 0.50 → **0.33** (one config value, same signal) and forward-demo it. q0.25 is even better
on bybit but falls back on binance → 0.33 is the cross-venue-robust choice.

## EXIT alpha — mfe_giveback trailing exit: a real but SMALLER win that does NOT stack with rmom33

`mfe_giveback` (arm a trailing exit at +5% MFE, cover when profit gives back to 30% of peak), on top of
breakeven@10. Fine grid: at trigger **t5** it beats base on BOTH venues across retain r20-r40 on bybit (a
plateau: 43.4/42.3/41.0) and at t5/r20-r30 on binance (30.6/30.7, marginal); t3 too tight, t8≈base.
Cross-venue cells beating base: **t5/r30** (bybit +9%, binance +1%, DD down both). **But the stack test
kills its incremental value:** rmom33 alone = binance 50.1; rmom33+mfe = binance **35.98** (mfe over-trims
the already-better selection). So mfe is a standalone improvement on the *current* config, **superseded by
rmom33** — do NOT adopt both. (bybit liked rmom33+mfe=50.6, but binance vetoes it.)

## DEAD / not-alpha (with the full evidence, so they're not re-run)

- **max_hold sweep (exit):** 48h ≈ peak on both venues (bybit peaks at 48; binance 36-48 flat) — no alpha.
- **liq-raise (entry):** VENUE-DIVERGENT — hurts bybit monotonically (38.6→32→20→16) but helps binance
  (its weak venue-unique illiquid names are the drag — CV1). Live = bybit → not adoptable.
- **turnover-surge entry gate (entry, "re-inject the liquidity-migration event"):** VENUE-DIVERGENT —
  hurts bybit at all k (38.6→37.7→…→21), helps binance only at extreme k5 (87% of trades cut). **Refutes
  the inheritance-doc hypothesis for bybit:** the continuous composite already selects well; requiring an
  explicit turnover surge just removes valid fades. Live = bybit → not adoptable.
- **max_active (rebalance):** NOT alpha — trade count is identical 18→50; it is purely `gross/max_active`
  per-name size (a leverage dial). The "MAR gain" at high max_active is only lower market-impact at smaller
  size while halving return; the operator already controls this via `gross_exposure`. (Monotonic both
  venues: smaller size → higher MAR, lower return.)
- **replace-weakest rotation (rebalance):** triage-rated LOW; not built (warm-start/PIT hazard, low
  expected value). The state exit already rotates capacity as names leave the decile.

## Meta-finding

The continuous fade's edge is largely captured. The one robust remaining lever is **tightening the
rmom squeeze-filter** (entry quality) — consistent with the whole arc ("the sophistication that helped was
risk machinery, not new signal") and the audit's #1 risk (the squeeze tail). Entry *breadth* filters
(liq/turnover) and *rebalance* sizing (max_active) are not alpha for the live (Bybit) sleeve; the only
other directional finding (mfe exit) is real-but-small and is superseded by the better selection.

## Disposition

- **APPLIED 2026-06-02 (operator-directed):** the live continuous sleeve ships `rmom_quantile=0.33`
  (`continuous_demo.ContinuousDemoCycleConfig`). One config value; same validated signal; cross-venue
  MAR↑/DD↓; ~−23% return (MAR-primary win). EXPLORATORY — the forward demo is the arbiter; revert to 0.50
  if the demo diverges. Engine `ContinuousEventConfig` default stays 0.50 (research baseline).
- **Not recommended:** mfe alongside rmom33 (over-trims binance); all DEAD items above.
- EXPLORATORY engine — the demo/paper forward run is the only arbiter. Artifacts:
  `/tmp/as_{phaseA,phaseB,phaseC,stack}_{bybit,binance}.json`; reproduce via the dispatch in
  `scripts/alpha_sweep.py`. No live profile changed by this research.
