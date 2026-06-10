# Pre-registration: hedge upgrade Stage-B — BTC+ETH two-factor hedge through the rebalance engine (2026-06-10)

**Gated by:** Stage-A PASS in `docs/preregistration/continuous-hedge-upgrade-2026-06-10.md`
(`btc_eth_2f` 6/6, selected). **Pattern:** WP3 Stage-B
(`continuous-hedge-engine-2026-06-09.md`). Design and bars frozen BEFORE any engine cell
was computed. Run label: `exploratory` → on PASS the 2f hedge becomes the in-sample
`candidate` (Tier-2 ceiling) REPLACING the single-BTC hedge as the recommended hedge form;
the live dry-run stays single-BTC until the operator signs off. REAL_MONEY=false; no
deploy change; no push without operator. Freeze-compatible for the same reason as Stage-A
(overlay-estimator replacement on the banked book; no book-variant adjudication).

## Code change (engine)

`liquidity_migration/continuous_rebalance.py`:
- `compute_hedge_betas_2f(raw_rets, h1, h2, idx, hedge_rule)` — bivariate OLS of per-unit
  book return on (leg1, leg2) over trailing `beta_window_days` LEDGER rows strictly before
  `idx` (minus `beta_extra_lag_days`), using only rows where BOTH leg returns are known;
  min `beta_min_obs` joint rows else (0,0); population (1/n) convention matching
  `compute_hedge_beta`; closed-form 2×2 normal equations; collinearity guard: if
  |corr(h1,h2)| > 0.995 in the window (or a leg has zero variance), fall back to
  single-factor beta on leg1 with b2=0. Semantics mirror the Stage-A driver.
- `apply_rebalance_rule(..., hedge_returns_2=None, hedge_funding_2=None)` — two-leg mode
  iff `hedge_returns_2` is provided: per-day legs `H_k = clip(-b_k, 0, cap)·scale`, joint
  proportional cap `H1+H2 ≤ cap·scale`; a leg's ratio is 0 on days its return is unknown;
  funding charged per leg; turnover per leg incl. flat-gap close/reopen + final-day close;
  cost = −(turnover1+turnover2)·cost_bps/1e4. Output keeps the existing TOTAL hedge
  columns (`hedge_ratio`, `hedge_return`, `hedge_funding_return`, `hedge_cost_return`) and
  appends `hedge_ratio_leg1`, `hedge_ratio_leg2`. DD-half state sees hedged equity;
  vol-target scale stays on raw book returns (unchanged semantics).
- Live twin: `ContinuousHedge2FState` + `compute_continuous_hedge_ratios_2f(state, rule,
  target_scale) -> (ratio1, ratio2)` — must reproduce the engine loop's per-leg ratios
  exactly (parity-tested), for the hedge-manager executor.
- **The unhedged and single-leg paths are NOT touched** — existing schema/values
  byte-identical (tested).

New tests (`tests/test_continuous_rebalance_hedge_2f.py`): single-leg/unhedged outputs
unchanged; per-leg causality (day-i perturbation of each leg's return and of book return);
joint-cap proportionality; collinearity fallback equals the single-leg result; warm-start
min joint obs; accounting identity incl. per-leg columns; gap close/reopen; live-twin
parity every day.

## Run design

Driver `scripts/continuous_hedge_2f_engine_driver.py`, modeled on
`continuous_hedge_engine_driver.py`: winner_base components via the scout pipeline
(`_load_source` → `combine_continuous_components`), BTC + ETH daily returns from the
verified panel builder, REAL per-day funding sums from the funding datasets, both venues.
Rules: book rule `w90/tv0.045/ddh-0.04/resize10`; hedge `W90/min60/cap2/5bps`. Binding
scale **max4**; max10 reported. Cells: `control` (unhedged), `hedged_btc` (banked
single-leg, byte-identical path), `hedged_2f`, plus for both hedged forms: hedge-cost
{10,20}bps, funding-off, beta windows {60,120,150}, lag-1, and 2×-BOOK-cost pairs
(control_book2x / hedged_btc_book2x / hedged_2f_book2x).

## A-priori bars (binding: max4, W90, 5bps, real funding; deltas = `hedged_2f` − `hedged_btc`)

- **s0a (control parity):** engine `control_max4` reproduces the banked controls (bybit
  +84%/MAR 5.02, binance +60%/4.57) bit-close (|Δret| ≤ 0.5pp, |ΔMAR| ≤ 0.05).
- **s0b (banked-hedge parity):** engine `hedged_btc_max4` reproduces the banked Stage-B
  numbers (bybit +93.18%/ΔMAR +0.50/ΔSharpe +0.233; binance +73.66%/+1.07/+0.382) within
  |Δret| ≤ 0.5pp, |ΔdMAR| ≤ 0.05, |ΔdSharpe| ≤ 0.01 (untouched code path — expect exact).
  s0 failure ⇒ run INVALID, fix plumbing, re-run; arm results disregarded.
- **s1:** ΔSharpe > 0 both venues AND pooled ΔSharpe > +0.05.
- **s2:** per-venue ΔMAR ≥ −0.10 AND pooled ΔMAR ≥ 0.
- **s3:** max-DD not worse by >0.5pp on either venue.
- **s4 (2× hedge cost):** at 10bps, ΔSharpe(2f−btc) > 0 both venues.
- **s5 (no crutch):** 2023-24 ΔSharpe ≥ 0 both venues AND funding-off ΔSharpe sign agrees
  with funding-on both venues.
- **s6 (latency):** lag-1 both forms, ΔSharpe(2f−btc) > 0 both venues.
- **s7 (2× book cost):** ΔSharpe(2f_book2x − btc_book2x) > 0 both venues and 2f_book2x
  return > 0 both venues.
- **s8 (window grid):** sign of ΔSharpe consistent across W{60,120,150} on both venues.

**PASS = s0–s8 all true.** NULL ⇒ the Stage-A overlay win does not survive the engine
lifecycle; the banked single-BTC hedge stands; write it up. Diagnostics reported,
non-blocking: per-year Sharpe, per-leg mean/max H, hedge funding/cost totals, max10.

## Artifacts

Out root: `C:\Users\user\SHARED_DATA\continuous_hedge_2f_engine_2026-06-10\` (cells.csv,
report.json + machine verdict). Verdict appended here + roll-up in
`docs/research_summary.md` + STATE.md pointer.

---

## VERDICT (run 2026-06-10, same day, design unchanged): **PASS s0–s8 — banked as in-sample `candidate` (Tier-2 ceiling)**

**s0 parity: exact.** Engine `control_max4` and `btc_max4` reproduce the banked WP3
Stage-B numbers to the reported digit on both venues (bybit control +84.01%/MAR 5.02,
hedged_btc +93.18%/ΔMAR +0.50/ΔSharpe +0.233; binance +60.00%/4.57 and
+73.66%/+1.07/+0.382). Code-path-untouched claim verified empirically.

**Binding cells (max4, W90, 5bps, real funding):**

| venue | cell | ret | MAR | DD | Sharpe | Sh 23-24 | per-year Sharpe |
|---|---|---:|---:|---:|---:|---:|---|
| bybit | control | +84.01% | 5.02 | −5.34% | 2.417 | 1.182 | 1.17 / 1.19 / 3.53 / 4.31 |
| bybit | hedged_btc (banked) | +93.18% | 5.52 | −5.39% | 2.650 | 1.618 | 1.71 / 1.55 / 3.52 / 4.48 |
| bybit | **hedged_2f** | **+102.99%** | **6.12** | −5.37% | **2.862** | **1.716** | 1.74 / **1.70** / **4.14** / 4.48 |
| binance | control | +60.00% | 4.57 | −4.26% | 2.001 | 1.722 | 1.72 / 1.73 / 1.93 / 3.30 |
| binance | hedged_btc (banked) | +73.66% | 5.64 | −4.24% | 2.383 | 2.349 | 2.61 / 2.15 / 2.18 / 3.36 |
| binance | **hedged_2f** | **+76.95%** | **6.17** | **−4.05%** | **2.463** | **2.394** | 2.67 / 2.18 / **2.38** / 3.37 |

Legs (mean H, max4): bybit BTC 0.020 / ETH 0.028; binance BTC 0.026 / ETH 0.032 — the
hedge is ~5-6% of equity total, ETH slightly the larger leg.

**Deltas 2f − btc (max4):** bybit ΔSharpe +0.212 / ΔMAR +0.60 / ΔDD +0.02pp / 23-24
+0.098 / 2×hedge-cost +0.220 / funding-off +0.242 / lag-1 +0.245 / 2×book +0.182 (ret
+82.9%) / W{60,120,150} +0.222/+0.042/+0.084. binance ΔSharpe +0.080 / ΔMAR +0.53 /
ΔDD +0.19pp / 23-24 +0.045 / 2×hedge-cost +0.078 / funding-off +0.083 / lag-1 +0.062 /
2×book +0.083 (ret +64.7%) / W +0.102/+0.041/+0.059. **Pooled ΔSharpe +0.146, pooled
ΔMAR +0.56.** All 8 conditions true; every falsifier keeps sign on both venues.

max10 (reported, non-binding): bybit 2f +352.82%/MAR 10.65/Sh 2.907 (vs btc 8.39/2.513);
binance 2f +201.22%/MAR 7.69/Sh 2.237 (vs btc 8.17/2.174 — binance max10 MAR slightly
lower as DD grows 7.56→8.50%; Sharpe still better; max4 is the binding anchor).

**Status:** the BTC+ETH two-factor hedge supersedes the single-BTC hedge as the
recommended hedge form for the continuous book (in-sample candidate, Tier-2 ceiling,
rmom caveat inherited; return-gain framing stays honest — part is bull-sample ETH
drift; the durable claim is variance/regime-robustness incl. 2025). Engine support is
merged (`compute_hedge_betas_2f`, two-leg `apply_rebalance_rule`,
`compute_continuous_hedge_ratios_2f` live twin; 10 new tests, existing 11 pass
unchanged). **Remaining (operator):** live hedge-manager wiring for the second leg
(the dry-run currently sizes single-BTC), forward-demo accumulation.
