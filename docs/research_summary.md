# Research Summary - Liquidity Migration

**Updated:** 2026-06-13

This is the research decision surface, not an archive. Obsolete receipts and
run-specific helper scripts belong in git history or run artifacts. Keep this file short
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
- Live path is wired/armed. Warmstart CSVs were regenerated and validated on
  2026-06-13; the remaining risk is calendar-age staleness after a flat spell,
  so the first post-flat risk-increasing leg may still block and page unless
  the operator asks for ledger-aware staleness.

**Continuous sniper**

- Tier-2 demo candidate. Armed in the demo unit, code default off.
- No placements yet because the base book has had zero entries since the 2026-06-09
  rebuild. That is signal-side until the gate is open.
- W4 Stage 2 recheck (2026-06-13) supports only the fixed live form for forward
  watch: +8% quarter-size PostOnly add-on, 25% stop, exit with base lifecycle.
  Historical fill rates were 37.2% bybit / 33.4% binance; R1 pooled MAR delta
  +0.14, but Binance bootstrap MAR was weak. No promotion or forward-fill claim.

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
- `docs/preregistration/2026-06-13-w4-continuous-program.md` - active staged
  Wave-4 replacement program; deleted prior W4 materials remain non-evidence.
- `docs/preregistration/2026-06-13-w4-continuous-stage0-data-clock.md` -
  current W4 data/forward-clock gate.
- `docs/preregistration/2026-06-14-w5-continuous-stage0-candidate-tape.md` -
  W5 Stage 0 PASS (both venues): per-cycle candidate tape (selected +
  rejected-but-eligible) reconstructed from the live decision code, reconciles
  exactly to the frozen control and the W4 overlap; PIT pass; ensemble-hedged
  control rebuilt. Reconstructability gate for the W5 score/entry/exit/sniper/
  sizing/path-shape/regime stages — no alpha claim. Stage-0 code uncommitted.
- `docs/preregistration/2026-06-14-w5-continuous-stage1-score-entry.md` -
  W5 Stage 1 NULL (structural, both venues): same-breadth score-as-entry-priority
  is a mechanical no-op (0 replacements across 3y × 4 components × 2 venues; A1/A5
  ledgers byte-identical to A0). The crowding gate already resolves within-cycle
  contention. Score lever moves to Stage 7 (path-shape) + Stage 5 (sizing).
  Code uncommitted.
- `docs/preregistration/2026-06-13-w4-continuous-stage1-stop-exit-realism.md` -
  CLOSED 2026-06-13; exact registered stop/protective-exit overlays rejected.
- `docs/preregistration/2026-06-13-w4-continuous-stage2-sniper-fill-validity.md` -
  CLOSED 2026-06-13; fixed sniper historically supported for forward watch only.
- `docs/preregistration/2026-06-13-w4-continuous-stage3-path-shape.md` -
  CLOSED 2026-06-13; path-shape measurements admissible only for a neutralized
  follow-up receipt.
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
  Flow composition did not separate winners from losers in that registered
  form; the informative squeeze-proxy leg is OI/liquidation/depth context, not
  a reason to promote raw taker-flow composition.
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
- **Wave 4 owner-erased 2026-06-13:** by explicit owner override, the W4 plan,
  receipts, scripts, and local artifacts were removed from the active
  workspace. Deleted W4 materials must not be cited as active evidence. Any
  replacement Wave-4-style work should be a serious staged program with dated
  preregistration, both venues, full artifacts, effect sizes, fragility, and
  explicit decision gates. Important feature families are not closed by one
  small script; each mechanism/stage is judged on its own registered evidence.
- **W5 Stage 1 score-as-entry-priority (2026-06-14): structural NULL.** Same-
  breadth within-cycle entry priority (composite, and a symbol-hash negative
  control) vs the frozen control produced **0 replacements** across 3y × 4
  components × 2 venues — A1/A5 ledgers are byte-identical to A0 (fcfs reproduces
  the control to 1e-9). The per-component crowding gate (max_fresh=2) plus
  max_active=25 leave no within-cycle contention to reorder, so entry priority is
  a mechanical no-op. Do not re-run within-cycle entry-priority on this control.
  The score-as-information lever moves to Stage 7 (neutralized path-shape) and
  Stage 5 (score-weighted sizing at constant breadth); a breadth-changing
  score-conditioned crowding admission is a distinct Stage 8 idea. Receipt
  `docs/preregistration/2026-06-14-w5-continuous-stage1-score-entry.md`.
- **W4 replacement Stage 1 stop/exit realism (2026-06-13):** exact registered
  capped 25% disaster stop plus failed-fade/breakeven overlay rejected on the
  amended full-PIT common window (`2023-04-01 <= signal_ts < 2026-05-01`).
  Primary `02_stop_ff6_be10` vs frozen control: bybit return 0.026 vs 0.714,
  MAR delta -4.31, DD worse -9.9% vs -5.3%; binance return 0.044 vs 0.675,
  MAR delta -5.33, DD worse -7.0% vs -4.0%. R1 pooled MAR delta -3.97 and
  bootstrap P(delta > 0)=0%; uncapped stop-fill falsifier flips Binance
  negative. This blocks only that exact stop/protective-exit mechanism, not the
  broader exit-realism family.

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

- **E2 regime response:** NULL in the registered V1/V2 form (receipt
  `2026-06-12-e2-regime-response-family.md`). V0 stands until a materially
  different, preregistered regime mechanism clears both-venue evidence.
- **PE2 long provisional entry:** failed in-window, but the engine exists
  default-off and has a pre-registered future-OOS re-judgment path once both
  full-PIT roots extend at least 60 days past 2026-05-28 and trade counts clear.
- **Dynamic-exit forward shadow:** only live forward evidence can decide whether
  the Bybit continuation profile was real or just venue/window luck.
- **W4 Stage 3 path-shape:** `pre_6h_return`, `pre_24h_return`, and
  `pre_24h_realized_vol` cleared the fixed two-venue admissibility screen for a
  later neutralized Stage 3b receipt. Do not use them directly: the
  `symbol_hash_bucket` negative control had a large 97 bps pooled spread, so
  symbol/component/time mix must be neutralized before any intervention test.
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
- Continuous forward replay orchestrator is built and initialized; it must run
  at each data-root refresh, and overlap drift is a hard alarm.
- W4 replacement program is active. Stage 0 says current local roots are stale
  for June forward claims, Binance ends at 2026-04-30, and forward replay has
  `forward_days=0`; later W4 stages need fresh dated preregistration before
  touching full-PIT roots.
- W5 continuous signal alpha draft plan lives at
  `docs/research_plans/w5_continuous_signal_alpha/`. It is the working plan for
  same-breadth score-entry, entry, exit, sniper, sizing, interaction, and
  forward gates. It is not itself a preregistration.
- W4 Stage 2 sniper result does not remove the live-fill debt: current evidence
  is historical bar-validity only until demo sniper placements/fills exist.
- Binance FAPI ancillary June top-ups were blocked from the dev box. Finish on
  the VPS or another permitted host.
- Bybit forward depth collector is enabled on the VPS; depth still needs time
  to mature before R4/capacity conclusions.

## Repo Policy

- `STATE.md` is the operational state and decision rules.
- This file is the research decision surface.
- `docs/preregistration/` keeps only active/binding receipts.
- Exploratory failures get one concise entry here, then the receipt/script is
  deleted unless it remains active.
- If a result is missing PIT, funding, cost, ledger, or a clean run record, label
  it exploratory at best. Do not launder it into a candidate.
