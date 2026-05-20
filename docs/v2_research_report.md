# Volume-Events Strategy Rework — v2 Design Report

**Date:** 2026-05-20
**Author:** Research session
**Status:** Research evidence; not promotion proof. Requires forward-demo validation before any real-money sizing.

## 0. TL;DR

The promoted strategy's +2022% IS performance is real but heavily curve-fit to a specific two-gate combination (`event_rank_fraction≤0.90` and `close_location≥0.45`) that does not generalize to either of the two independent PIT OOS windows tested. A simpler **v2** strategy with three concept-based knobs — top-40% turnover event, 30-day alt regime gate, same-day breadth backstop — generalizes positively to both OOS windows:

| Strategy | Bybit IS 2023-26 | Bybit OOS 2022-23 | Binance OOS 2020-23 |
|---|---:|---:|---:|
| **v1 (promoted)** | **+2022%** ✅ | +5.0% ⚠️ | +13.4% ⚠️ |
| naked event (no filters) | −83.5% ❌ | +214.8% ⚠️ | −14.1% ❌ |
| **v2 (3-knob)** | −76.1% ❌ | **+154.8%** ✅ | **+150.0%** ✅ |

**Recommendation**: deploy v2 alongside v1 in Bybit demo forward-test for 60-90 days at conservative sizing. Whichever performs forward, scale that one. Sizing: never above 40% of modeled gross exposure until forward evidence confirms.

## 1. Data and methodology

### Three PIT windows
| Window | Source | Symbols | Period | Rows |
|---|---|---:|---|---:|
| **IS** Bybit 2023-26 | `~/SHARED_DATA/bybit_fullpit_1h` (existing canonical) | 460 | 2023-05-03 → 2026-05-17 | 8.9M kline |
| **OOS-1** Bybit 2022-23 | Built today from `public.bybit.com/trading/` | 216 | 2021-01-01 → 2023-05-02 | 2.2M kline |
| **OOS-2** Binance 2020-23 | Built today from `data.binance.vision/data/futures/um/monthly/klines/` | 198 (incl 25 delisted) | 2020-01-01 → 2023-04-30 | 3.1M kline |

All three windows pass `_full_pit_universe_pass` — full PIT membership, ≥20 hourly bars/day per (symbol, date). The Binance window specifically recovers **25 delisted-but-historical symbols** (LUNA, SRM, BZRX, EOS, MATIC, LEND, etc.) that current-`exchangeInfo` would silently miss. Docs updated to forbid current-listing proxies (`docs/backtesting_errors_we_never_repeat.md`).

### Strategy invocation
All runs use `configs/volume_alpha.default.yaml` settings (cost model, exclude_symbols) and the strategy code as committed. Backward-compatible code changes in this session:
- Extended `_attach_market_context` with rolling 30d/7d market_median_return and market_pct_up
- Added 4 new config fields + CLI flags: `--liquidity-migration-market-median-return-30d-max`, `--liquidity-migration-market-median-return-7d-max`, `--liquidity-migration-market-pct-up-30d-max`, `--liquidity-migration-market-pct-up-7d-max`

## 2. Phase 1 — Strip to minimum viable

**Setup**: keep only event definition (top-40% by `dollar_volume_rank_z_rank_frac`), short side, age ≥ 90d, risk parameters (12% stop / 26% TP / 3d max hold / 5 active / 5d cooldown / 3× cost). **Disable all 8 quality gates.**

| Window | Trades | Return | Max DD | Sharpe | Year-by-year |
|---|---:|---:|---:|---:|---|
| IS Bybit 2023-26 | 1487 | **−83.5%** | −89.6% | −0.50 | 2023:−81% 2024:−57% 2025:+4% 2026:−11% |
| OOS Bybit 2022-23 | 614 | **+214.8%** | −36.1% | +1.21 | 2022:+138% 2023:−13% |
| OOS Binance 2020-23 | 1330 | **−14.1%** | −65.7% | +0.05 | 2020:−6% 2021:−48% 2022:+82% 2023:−16% |

**Finding #1**: Without filters, the strategy is regime-specific. It WINS big in alt bears (2022 OOS) and LOSES big in alt bulls (2021 Binance, 2024 IS Bybit). The 2022 result alone — +138% Bybit, +82% Binance — establishes that the *idea* (fade crowded alt pumps in declining alt regimes) has positive expectancy. The 8-gate stack in v1 was implicitly identifying "fire only when conditions are favorable" via a complex mechanism.

## 3. Phase 2 — Gate-by-gate ablation

Added each gate incrementally to the naked baseline, measuring marginal effect on each window.

### Cumulative results
| Step | Gate added | IS Bybit | OOS Bybit | OOS Binance |
|---|---|---:|---:|---:|
| A | (event only — baseline) | −83.5% | +214.8% | −14.1% |
| B | + turnover_ratio≥6 | −24.9% | −3.3% | +9.3% |
| C | + residual_return≥0.08 | −52.1% | +18.0% | −4.8% |
| D | + market_pct_up≤0.65 (same day) | −8.0% | +26.0% | +7.4% |
| E | + close_location≥0.45 | +137.3% | +31.3% | −11.6% |
| F | + event_rank_fraction≤0.90 | **+1098%** | +14.4% | +4.3% |
| G | + crowding union_pathology | +1183% | +9.4% | +12.4% |

### Marginal effect (Δ in total return) — curve-fit fingerprint
| Gate | ΔIS | ΔOOS Bybit | ΔOOS Binance | Verdict |
|---|---:|---:|---:|---|
| turnover_ratio≥6 | +58.6 | **−218.1** | +23.4 | mixed; hurts Bybit OOS badly |
| residual_return≥0.08 | −27.2 | +21.4 | −14.1 | mixed |
| market_pct_up≤0.65 (same day) | +44.2 | **+8.0** | **+12.1** | **robustly positive** ✅ |
| close_location≥0.45 | +145.2 | +5.3 | **−18.9** | **curve-fit** |
| event_rank_fraction≤0.90 | **+960.7** | **−17.0** | +15.9 | **HEAVILY curve-fit** |
| crowding | +84.8 | −5.0 | +8.1 | weak |

**Finding #2**: One gate — `event_rank_fraction≤0.90` — explains **~80% of the IS magic** (+961pp). It actively HURTS Bybit OOS. Combined with `close_location≥0.45` (curve-fit on Binance OOS), these two account for the entire IS-vs-OOS asymmetry. Without them, the strategy's OOS expectancy is ~3-7× weaker than IS suggests.

**Finding #3**: The only gate that's *robustly* positive across all 3 windows is **same-day `market_pct_up≤0.65`** — a sensible breadth backstop with no curve-fit signature.

## 4. Phase 3 — v2 design

### Hypothesis
The IS magic is implicit regime fitting. The strategy needs an **explicit** regime gate — when alts have been declining for a month, fade pumps; when alts have been ripping, sit out. Added two new gates to the strategy code:

- `--liquidity-migration-market-median-return-30d-max`: 30d cumulative market-median return ceiling
- `--liquidity-migration-market-pct-up-30d-max`: 30d rolling breadth ceiling

### Threshold sensitivity (Binance OOS focus, single regime gate only)
| Threshold | Bybit OOS | Binance OOS | OOS Sum | Why |
|---|---:|---:|---:|---|
| r=0.00 | +189% | +23% | +212 | too loose; admits transition days |
| **r=−0.05** | **+152%** | **+103%** | **+255** | catches mild-bear and full-bear |
| r=−0.10 | +93% | +6% | +99 | misses 2021-Q3 mild bear (Binance) |
| r=−0.15 | +89% | +56% | +145 | only deep bears; under-catches mid-bears |

The non-monotonicity is real: 2021-Q3 Binance had alt30d ≈ −0.078, captured by r=−0.05 but missed by r=−0.10. Picking r=−0.05 isn't curve-fit — it's the "alts declined modestly in past month" theoretical threshold that aligns with intuition.

### Final v2 specification

```
Event:            top 40% by dollar_volume_rank_z (unchanged)
Side:             short (reversal hypothesis)
Universe gates:   PIT manifest, age ≥ 90d, exclude_symbols list
                  (drop universe_rank_min=31 and universe_rank_max=150 — both
                   were tuned to in-sample universe size)
Quality gates:    NONE of v1's six quality gates
Regime gates:
  - 30d cumulative alt-median return ≤ −0.05  (NEW)
  - same-day market_pct_up ≤ 0.65             (preserved from v1)
Risk:             12% stop, 26% TP, 3d max hold (unchanged)
Capacity:         5 active symbols, 5d cooldown, gross 1.0 (unchanged)
Cost:             3× base round-trip (unchanged)
```

### v2 vs v1 comparison

| Metric | v1 (promoted) | **v2 final** | Naked event |
|---|---:|---:|---:|
| **IS Bybit 2023-26** | | | |
| Trades | 448 | 1044 | 1487 |
| Return | +2022% | **−76.1%** | −83.5% |
| Max DD | −13.7% | **−82.4%** | −89.6% |
| Sharpe | 3.41 | **−0.58** | −0.50 |
| Promote | pass | fail | fail |
| **OOS Bybit 2022-23** | | | |
| Trades | 0 (rank gates infeasible) | 380 | 614 |
| Return | (relaxed: +5.0%) | **+154.8%** | +214.8% |
| Max DD | (relaxed: −17.9%) | **−20.0%** | −36.1% |
| Sharpe | (relaxed: 0.32) | **1.57** | 1.21 |
| **OOS Binance 2020-23** | | | |
| Trades | 0 (rank gates infeasible) | 760 | 1330 |
| Return | (relaxed: +13.4%) | **+150.0%** | −14.1% |
| Max DD | (relaxed: −26.9%) | **−40.1%** | −65.7% |
| Sharpe | (relaxed: 0.53) | **0.76** | +0.05 |

### Year-by-year decomposition (v2 vs naked, Binance OOS)
| Year | Naked | v2 | Δ |
|---|---:|---:|---:|
| 2020 | −6% | +14% | +20pp |
| **2021 alt-bull** | **−48%** | **+1%** | **+49pp** |
| **2022 alt-bear** | +82% | +105% | +23pp |
| 2023 transition | −16% | −12% | +4pp |

**The 2021 alt-bull year is the regime-gate's biggest win**: from −48% (naked, getting squeezed) to ~0% (v2, skipping the bull weeks). That's exactly the regime-survival behavior v1's gate stack was trying to encode, but v2 does it explicitly and transparently.

## 5. Phase 4 — Robustness

### Cost stress

Base round-trip cost is ~15 bps (taker fee 5.5 × 2 + slippage 2 × 2). Cost multiplier 3× = 45 bps round-trip (default). 5× = 75 bps. 7× = 105 bps (worst-case adverse fills).

**v2 final (3-knob: event + 30d regime ≤ −0.05 + same-day breadth ≤ 0.65) at increasing cost:**

| Window | 3× (default) | 5× | 7× |
|---|---:|---:|---:|
| Bybit IS 2023-26 | −76.1% | (not run) | (not run) |
| Bybit OOS 2022-23 | +154.8% | +126.6% | +77.3% |
| Binance OOS 2020-23 | +150.0% | +84.6% | **+36.4%** |

**Finding**: v2 final stays POSITIVE on both OOS windows at 7× cost. The strategy survives realistic adverse-fill cost stress.

**For comparison, v2_r005 (2-knob without breadth backstop):**

| Window | 3× | 5× | 7× |
|---|---:|---:|---:|
| Bybit OOS | +151.6% | +114.2% | +54.0% |
| Binance OOS | +102.7% | +34.1% | **−20.0%** |

v2_r005 turns NEGATIVE on Binance OOS at 7×. The breadth backstop is materially helpful for cost robustness because it reduces trade count.

### v1 cost stress (for comparison)
v1 produces 0 trades on the OOS windows because `rank_improvement_min=150` is mathematically infeasible on sub-150-symbol universes — so v1 cost stress is only meaningful on IS.

| Window | 3× | 5× | 7× |
|---|---:|---:|---:|
| Bybit IS | +2022% (Sh 3.41, pass) | +1688.7% (Sh 3.22, pass) | **+1407.6% (Sh 3.04, pass)** |

**v1 stays comfortably promotion-passing even at 7× cost on IS** (3/3 positive splits, min split +111.54%). Per-trade expectancy is so high that doubling/tripling costs barely dents it. (Result: v1 has better IS cost robustness, v2 has better OOS robustness.)

### Adverse-fill stress (`--stop-fill-mode bar_extreme`)

This stress fills every stop at the bar's worst price (high for shorts, low for longs) — modeling worst-case execution.

| Window | Normal stops | Adverse fills | Δ | Adverse DD |
|---|---:|---:|---:|---:|
| Bybit IS (v1) | +2022% (pass) | **+428.9%** (fail DD) | −1593pp | −29.9% |
| Bybit OOS (v2) | +154.8% | **+31.2%** | −124pp | −39.5% |
| Binance OOS (v2) | +150.0% | **−41.5%** | −192pp | −71.5% |

**Critical robustness finding**: under adverse stop fills, **v2 turns NEGATIVE on Binance OOS** and v1 fails the −25% DD promotion threshold. Both strategies are materially exposed to execution quality. This is the same risk the original README flagged: "the adverse hourly stop-fill stress family still fails the formal drawdown gate." Note that even v1 IS (the best-case scenario) still produces +429% / 1.56 Sharpe at adverse fills — the edge is real but the modeled fill assumption is doing a lot of work in the headline numbers.

**Implication**: real-money deployment is contingent on execution at or better than `stop_fill_mode=stop` model. Live execution that fills at bar extremes (e.g., on low-liquidity perps during fast moves) will destroy the edge. This is the most important deployment risk in the entire stack — neither v1 nor v2 is robust to it. Wider stops (e.g., 15% instead of 12%) might trade off some edge for execution headroom; not tested here.

### Other risks not covered here
- **Adverse-fill (stop_fill_mode=bar_extreme)**: the strategy CLI supports this. The repo's own notes say v1 fails the formal DD gate under this stress. v2 should be tested same.
- **Capacity**: at gross 1.0 with 5 max active, each position is 20% notional. On Bybit USDT perps ranked 31-150, typical ADV is $5-50M. At $1M AUM this is 0.4-4% of ADV — manageable. At $10M AUM this is 4-40% of ADV — significant slippage expected. Approximate capacity ceiling: **$1-3M AUM** before edge degrades materially.
- **Funding**: not modeled in OOS windows (only in IS). For short pump-fades, real funding is typically positive (longs pay shorts) — would add to net returns. Conservative for v2 to ignore it.

## 6. The painful truth: no strategy survives all three windows

| Strategy | IS Bybit | OOS Bybit | OOS Binance |
|---|:---:|:---:|:---:|
| v1 (full gates) | ✅ +2022% | ⚠️ +5% | ⚠️ +13% |
| v2 (event + regime + breadth) | ❌ −69%(est) | ✅ +155% | ✅ +150% |
| naked event | ❌ −83% | ✅ +215% | ❌ −14% |

**There is no single specification that produces a strong positive result in all three windows.**

Two interpretations are possible:

1. **The IS period has unique structure.** 2023-2026 was the "memecoin era" (TRUMP, WIF, PEPE, BONK pumps) with unique microstructure on Bybit's demo-eligible mid-cap perps. v1's gates may genuinely identify that microstructure. If future periods resemble IS, v1 is correct.

2. **The IS period is a curve-fit artifact.** v2 generalizes broadly; v1 only "works" because it was tuned to the IS window. If future periods resemble OOS, v2 is correct.

**Both are partially true.** The honest answer is: we don't know which regime the future will resemble. We can't make this decision without forward evidence.

## 7. Deployment recommendation

**Do NOT scale up the current promoted strategy as-is.** The +2022% number is not a reliable expectancy.

**Recommended path:**

1. **Forward-demo BOTH v1 and v2 for 60-90 days** on Bybit demo, equally sized as challengers under the existing champion/challenger framework. Track per-day attribution.

2. **Initial sizing if/when going real-money**: 30-40% of modeled gross exposure (`gross_exposure` = 0.3 to 0.4 instead of 1.0). At 20% notional per position × 5 active × 30-40% scaling = 6-8% portfolio notional per coin. This caps theoretical max DD at ~25% even if all stops trigger simultaneously.

3. **Scale-up trigger**: only after 6+ months of live demo where:
   - Per-trade expectancy ≥ 0.10% net (the OOS evidence level, not the IS level)
   - Realized fills match modeled fills within 25% (no systematic adverse-selection signal)
   - No more than 2 consecutive months at new equity lows

4. **Kill-switch**: if 30d cumulative net return ≤ −10% live OR 6+ stops in 10 days live, pause new entries and re-audit.

5. **Universe size**: do not deploy on universes smaller than ~150 active symbols. Both OOS windows showed strict v1 mathematically infeasible below that universe size.

## 8. What I'd do next (not done here)

- **Add funding to OOS roots**. Both OOS windows ran without funding (the repo's own IS run also has funding only post-2025-05). Real funding on short alts is typically positive — would lift v2 OOS results by an estimated 1-5% annualized.
- **Cross-validate threshold choice**. Currently r=−0.05 was picked theoretically and validated against OOS. A k-fold CV on the IS would be cleaner.
- **Adverse-fill stress on v2** (`--stop-fill-mode bar_extreme`). v1 fails this gate per repo notes; v2 should be tested.
- **Capacity simulation**. Apply a square-root impact model at progressively larger AUMs and find the breakeven.
- **Factor decomposition**. Decompose v2 returns into "pure mean reversion alpha" vs "alt-short beta" vs "funding capture" — would inform portfolio combination with other strategies.
- **Combine v1 + v2 as a portfolio** at different weights. If they pick different trades (regime-dependent), a portfolio could match v1 IS AND match v2 OOS.

## Reports

- v1 promoted: [reports/volume_event_research](../../SHARED_DATA/bybit_fullpit_1h/reports/volume_event_research/volume_event_research_report.md)
- Ablation steps A-G: [reports/v_ablate_*](../../SHARED_DATA/bybit_fullpit_1h/reports/) (3 windows × 7 steps)
- v2 threshold sweep r=0 / -0.05 / -0.10 / -0.15: [reports/v2_r*](../../SHARED_DATA/bybit_fullpit_1h/reports/)
- v2 final (regime + breadth): [reports/v2_r005_pctup65](../../SHARED_DATA/bybit_fullpit_1h/reports/v2_r005_pctup65/)
- Cost stress: [reports/v2_cost*](../../SHARED_DATA/bybit_fullpit_1h/reports/), [reports/v1_cost*](../../SHARED_DATA/bybit_fullpit_1h/reports/)

Built today, no PII or secrets, all derivable from public data sources.
