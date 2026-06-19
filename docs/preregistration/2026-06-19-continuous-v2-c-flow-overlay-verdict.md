# Continuous V2 C-Book Flow Verdict — Screen + Hedge-Intensity Overlays (Binance-only)

Date: 2026-06-19

Parent plan: `docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md`
Amendment: `docs/preregistration/2026-06-19-continuous-v2-ab-amendment-binance-only-flow.md`
Construction: `docs/preregistration/2026-06-19-continuous-v2-c-flow-overlay-construction.md`

Scope: `claimed_venue_scope=binance_only_flow_exploratory`. Run label `exploratory`;
**no Tier-2 candidate pass is possible**. No real-money claim. Forward demo/paper
is the arbiter; nothing here is wired to demo/paper.

## What ran

- Feature almanac (Binance, flow features value-built):
  `backtest-runs/continuous_v2_feature_almanac_2026-06-19_cflow/`
- C0 residualized flow screen (Binance, with symbol/calendar/shuffle nulls):
  `backtest-runs/continuous_v2_feature_screens_2026-06-19_cflow/`
- C2/C3 hedge-intensity overlays + C7 hash control (Binance):
  `backtest-runs/continuous_v2_ab_cflow_2026-06-19/` (ab_table.csv, robustness.json/csv/report)

## C0 screen — flow is a trade-level signal, not a hedge-timing signal

Within-symbol (symbol-demeaned) rank-IC vs control short `net_return`, null-max ±0.029:

- `idiosyncratic_flow`: **−0.103** (buy flow → continuation → short loses) — strong, beats null.
- `taker_imbalance_1h`: −0.085; `taker_imbalance_24h`: +0.067; `flow_squeeze`: +0.086;
  `oi_change_24h`: +0.056 — all beat the null, economically signed (crowding/exhaustion → short wins).
- `flow_resid_return`: **+0.007 — BELOW the null-max.** Residualizing 24h taker imbalance
  against the run-up (`path_max_ret168`) destroys the signal: the flow information is
  largely collinear with the run-up, not orthogonal to it. The order-flow paper's
  residualize-against-lagged-returns translation does **not** add incremental within-symbol
  prediction for this fade book.

Daily book-return level (where a hedge overlay acts), month-demeaned IC, null-max ±0.030:
only `flow_squeeze` beats the null (+0.054); `market_flow` ≈ 0 (−0.003). And `flow_squeeze`'s
daily sign is positive — high-squeeze days are *good* book days — so "hedge more when squeeze
high" should drag, not protect.

## C2 / C3 / C7 hedge-intensity overlays (Binance, exploratory)

Control V2 (Binance): ret +84.02%, maxDD −3.27%, MAR 8.185.

| Arm | ret | maxDD | MAR | MAR Δ | boot P(MAR Δ>0) | boot MAR Δ p5 | min LOO Δ |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `C2_MARKET_FLOW_HEDGE_INTENSITY_BINANCE_ONLY` | +86.05% | −3.54% | 7.744 | −0.442 | 0.322 | −8.05 | +0.014 |
| `C7_FLOW_HASH_CONTROL_BINANCE_ONLY` (market-flow hash) | +84.76% | −3.49% | 7.721 | −0.465 | 0.199 | −3.31 | −0.001 |
| `C3_FLOW_SQUEEZE_HEDGE_INTENSITY_BINANCE_ONLY` | +85.17% | −3.28% | 8.257 | +0.072 | 0.963 | +0.03 | +0.009 |

## Verdict — flow hedge-overlay branch CLOSED (falsifier-backed negative)

- **C2 (market-flow hedge intensity): FALSIFIED.** Its MAR delta (−0.442) is statistically
  indistinguishable from the C7 calendar-hash control (−0.465): the *real* market-flow timing
  is no better than randomly permuted timing. Both worsen drawdown (−3.5% vs −3.27%) and lower
  MAR. The market-flow daily signal is noise for hedge timing (consistent with its ~0 daily IC).
- **The overlay mechanism has a ~0.45 MAR noise floor.** The C7 hash shows that *random*
  ±30% hedge-timing modulation moves MAR by ~−0.45. C3's +0.072 MAR delta sits firmly inside
  that noise band, so it is **not distinguishable from noise**. (A flow_squeeze-specific hash was
  not run; given the effect is <1% relative MAR, single-venue, and inside the demonstrated noise
  floor, it does not warrant the compute. C3 is parked, not accepted.)
- The mechanism falsifier from the construction receipt fired for C2 (matched by hash). C3 is
  negligible. Flow does not harvest through hedge-intensity timing on Binance.

## Why this is the expected result, not a surprise

The C0 screen already showed flow is a **trade-level** predictor (strong within-symbol IC on
`idiosyncratic_flow`/`taker_imbalance`) but **null/weak at the daily book level** where a hedge
overlay operates. Hedge-intensity timing is the wrong harvest mode for a trade-level signal.

## Next required step (still Binance-only exploratory)

The only flow harvest mode consistent with the evidence is **trade-level entry sizing**:
size down shorts with high causal `idiosyncratic_flow` (continuation risk). That is arm
`C1_FLOW_RESID_FEATURE_SIZING_BINANCE_ONLY` (sizing, not hedge overlay). It needs a dated
construction amendment fixing the exact mean-1 tilt and sign before running, and remains
Binance-only exploratory. `flow_resid_return` is **not** the feature to use (it failed the C0
null); use raw `idiosyncratic_flow`. A Bybit full-market taker-flow archive build is justified
**only** if C1 idiosyncratic-flow sizing shows a real, null-beating, time/liquidity-stable
Binance harvest — not by these hedge-overlay results.
