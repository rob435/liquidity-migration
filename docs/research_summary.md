# Research Summary - Liquidity Migration

**Updated:** 2026-06-12

This is the research decision surface, not an archive. Closed receipts and
one-off scripts belong in git history or run artifacts. Keep this file short
enough that a new agent can read it before making a decision.

## Non-Negotiable State

- Research-stage only. Nothing is approved for real money.
- Forward demo/paper is the arbiter. There is no clean internal pre-2023 OOS
  root to rescue a result.
- Both venues matter. A Bybit-only win is a warning, not a candidate.
- Full-PIT membership, causal features, cost/funding treatment, ledgers, and
  reconstructable run records are correctness gates.
- The daily SHORT sleeve was erased on 2026-06-11 by operator order. It is not
  disabled, dormant, or available for restart; git history is the archive.

## Current Objects

**Continuous fade book**

- Live on demo only as `continuous_ensemble_v1`: p3 .30 / p4p3 .20 / p4p5
  .40 / tp14 .10, w90/tv0.045/max4/ddh-0.04, no momentum hurdle, rmom q25,
  BTC-uptrend gate.
- Research-stage, not promoted. Demo fills are execution evidence, not alpha
  proof.
- Known caveat: rmom is causal but has almost no latency margin. The day-grid
  audit found no off-by-one; the effect is genuinely fast-decay.
- Live gate can correctly produce a flat book. If the gate is closed, no-entry
  silence is expected; if the gate is open and the book stays flat for two
  days, that is page-worthy.

**Continuous 2f hedge**

- BTC+ETH 2-factor hedge is banked as an in-sample candidate with a Tier-2
  ceiling only.
- Live path is wired/armed, but stale warmstart CSVs block risk-increasing legs.
  Operator must either regenerate `deploy/hedge_warmstart/*.csv` with a refresh
  cadence or disarm the timer.

**Continuous sniper**

- Tier-2 demo candidate. Armed in the demo unit, code default off.
- No placements yet because the base book has had zero entries since the 2026-06-09
  rebuild. That is signal-side until the gate is open.

**Dynamic exit**

- In-sample cross-venue result was null: Bybit looked good, Binance failed.
- Only the no-order forward paper shadow remains live as a possible revival
  path. The fixed TP/24h clock stands unless the forward shadow clears its
  frozen bar.

**Long-native v11a**

- Promoted-in-code for demo/paper only, toggled off on the live box.
- Current profile is `div` + volup125 + weekend 1.5x tilt.
- It remains subject to the operator's leverage/capital decision and the same
  forward demo/paper bar. No real-money claim is allowed.

## Active Binding Receipts

Keep only receipts that still bind an active deployment, candidate, or
methodology decision:

- `docs/preregistration/2026-06-12-e2-regime-response-family.md` - CLOSED
  2026-06-12 (NULL; V0 stands); kept while it documents the V1/V2
  `btc_trend_gate` engine modes that shipped with it.
- `docs/preregistration/continuous-capacity-impact-2026-06-09.md` - active R4
  fill-calibration/capacity receipt.
- `docs/preregistration/continuous-dynexit-forward-shadow-2026-06-10.md` -
  active forward-only dynamic-exit shadow bar.
- `docs/preregistration/continuous-forward-clock-spec-2026-06-09.md` - active
  forward evidence design/debt.
- `docs/preregistration/continuous-hedge-2f-engine-2026-06-10.md` - binding
  2f hedge candidate receipt.
- `docs/preregistration/continuous-walkforward-allocator-2026-06-09.md` -
  binding frozen-weight/no-adaptive-reweighting policy.
- `docs/preregistration/continuous-winner-robustness-2026-06-09.md` - binding
  frozen ensemble/winner_base evidence.
- `docs/preregistration/div-promotion.md` - binding long `div` profile receipt.
- `docs/preregistration/long-volup-candidate-2026-06-09.md` - binding long
  volup125 receipt.
- `docs/preregistration/long-provisional-entry-engine-2026-06-10.md` - active
  future-OOS-only PE2 re-judgment path.
- `docs/preregistration/r4-risk-model-verdict.md` - Tier-3 residual-Sharpe
  model foundation.
- `docs/preregistration/rmom-latency-falsification-2026-06-09.md` - binding
  rmom latency verdict.
- `docs/preregistration/sniper-staged-entries-2026-06-09.md` - binding sniper
  staged-entry candidate receipt.
- `docs/preregistration/trade-atlas-2026-06-11.md` - binding long weekend tilt
  plus forward-watch bars; trim if it starts duplicating closed short-era detail.
- `docs/preregistration/_template.md` - template.

Closed receipts not listed here have been folded into this summary. Do not add
them back unless they again bind an active decision.

## Failure Ledger

These are closed on the spent 2023-04 -> 2026-05 window. Do not re-mine them
without genuinely new data, a new lifecycle, or a fresh forward-only bar.

**Continuous**

- Rmom latency: causal/no leak, but a shift beyond the freshest legal daily
  availability kills the edge. This blocks any deployment-grade continuous
  claim that relies on daily rmom.
- Downtrend extension: demoted. The headline rested on a fragile narrow slice;
  down-regime capital is hedge/cash, not a new sleeve.
- Daily-granularity sizing conversion: closed after OI tilt, OI downsize,
  participation caps, continuous atlas gates, and E1 all failed to convert
  feature ordering into deployable MAR. Stop calling this "one more tilt away".
  E1's numbers for the record (receipt folded here; artifacts
  `~/SHARED_DATA/e1_stage0_2026-06-12/`): bybit mid-quintile monthly IC
  +0.051 but only 54% positive months (bar 65%), sign-flips under the
  pooled-cut sensitivity; registered tilt formula worth +0.15bp/trade bybit
  (t 0.02) / −2.3bp binance vs a 15-20bp/trade base book.
- Dynamic exit: in-sample null across venues. Bybit-only continuation was a
  mirage until forward shadow proves otherwise.
- Event-level taker flow: P10 failed both ex-ante mechanisms on this window.
  Flow composition did not separate winners from losers; the informative
  squeeze-proxy leg is OI/liquidation context, not taker-flow composition.
  Numbers for the record (receipt folded here; artifacts
  `~/SHARED_DATA/continuous_taker_flow_scout_2026-06-12/`): IC(flow_support_6h)
  +0.006 bybit (p=.84, cov 88%) / −0.014 binance (p=.60, cov 99.9%), signs
  disagree, flow-tercile spread −0.5bp/trade both venues; events were the
  locally reproduced component ledgers (parity p3 858/857, binance 722 exact).
- BTC-regime response family (E2): NULL. V1 euphoria cap and V2 soft 3-state
  both destroy MAR vs the live binary gate (pooled −1.96 / −2.52; worse DD,
  both venues, both cost arms). The euphoria bucket's raw negative mean did
  not survive the engine: those trades are net book contributors once
  funding, exits, and rebalance dynamics are modeled. The binary uptrend
  gate stands; down/euphoria treatment stays hedge + stops/caps.
- Naive passive-at-touch entries: null. Maker savings were not enough to pay
  for adverse continuation tails. Sniper-style deeper resting ladders are the
  only passive form still alive.
- Ridge combiner: rejected. Bybit out-of-fold IC was negative and Binance was
  unmeasurable without the OI rebuild.
- Hard stops, MFE/giveback exits, breakeven variations, rank-decay exits, and
  broader crowding caps did not improve the book enough to keep.

**Long**

- Long regularity/densification: closed. Extra unconfirmed events were negative;
  daily-close confirmation is the FC signal.
- PE2 provisional trigger-hour entry failed the in-window cross-venue bar by a
  small amount, especially on Binance. It is not adopted.
- Long-only leverage beyond the validated profile is an operator risk decision,
  not alpha evidence.

**Cross-book / exploratory**

- Intraday residual reversal: physics confirmed, economics failed by an order of
  magnitude at taker costs. Maker/depth evidence would need a fresh receipt.
- Downtrend bounce and hedged bounce products: killed by drawdown class and
  operator instruction. Do not revive.
- Funding-at-entry and most atlas features were null. The useful harvest is only
  the forward-watch queue below.
- Old daily SHORT evidence is historical only. It may teach methodology lessons;
  it cannot justify a current sleeve.

## Revisit Queue

These are not promotion evidence. They are the only things worth keeping in
view because the prior negative read may have been too pessimistic or because
they can be judged forward without spending the window again.

- **E2 regime response:** CLOSED 2026-06-12 — NULL (receipt
  `2026-06-12-e2-regime-response-family.md`). V0 stands; no regime variant
  may be revisited without genuinely new data or a forward-only bar.
- **PE2 long provisional entry:** failed in-window, but the engine exists
  default-off and has a pre-registered future-OOS re-judgment path once both
  full-PIT roots extend at least 60 days past 2026-05-28 and trade counts clear.
- **Dynamic-exit forward shadow:** only live forward evidence can decide whether
  the Bybit continuation profile was real or just venue/window luck.
- **Forward-watch atlas leads:** repeat-name penalty, weekend bonus for long
  books, and continuous US-session penalty. Recompute only on forward trades
  when the pre-stated sample thresholds are met.
- **Liquidation / squeeze proxy:** historical raw liquidation data cannot be
  bought cleanly. The forward liquidation tape plus OI and depth layers are the
  credible path.
- **R4 realized-fill / depth calibration:** capacity and maker economics remain
  unsettled until demo fills and depth collector data mature.
- **Intraday residual maker design:** only worth revisiting after R4/depth shows
  the cost bar can plausibly clear.

## Methodology Debts

- Continuous forward window is immature; clocks restarted with the 2026-06-09
  rebuild/data refresh.
- Continuous forward replay orchestrator still needs to rerun the four frozen
  component configs into `continuous_forward_replay`; until then, the forward
  signal clock is incomplete.
- Binance FAPI ancillary June top-ups were blocked from the dev box. Finish on
  the VPS or another permitted host.
- Bybit forward depth collector is built but not operator-enabled; every month
  disabled loses live capacity data.

## Repo Policy

- `STATE.md` is the operational state and decision rules.
- This file is the research decision surface.
- `docs/preregistration/` keeps only active/binding receipts.
- Exploratory failures get one concise entry here, then the receipt/script is
  deleted unless it remains active.
- If a result is missing PIT, funding, cost, ledger, or a clean run record, label
  it exploratory at best. Do not launder it into a candidate.
