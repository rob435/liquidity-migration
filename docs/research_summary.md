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
- `docs/preregistration/2026-06-14-w5-continuous-stage7-path-shape-neutralized.md` -
  W5 Stage 7 NULL-as-registered (both venues): the registered tercile-spread gate
  is noise-dominated on this heavy-tailed per-notional cross-section
  (per-symbol-random spread null SD 128/175 bps; a random per-trade score yields
  ±170 bps spreads), so it cannot decide — banked NULL on the registered gate,
  not overturned post-hoc, no admissibility claimed. The robust rank-IC was clean
  (path-shape 0.22/0.21 vs symbol-hash control 0.13/0.09). Methodology lesson: on
  this data use rank-IC + permutation null, not tercile spread. Code uncommitted.
- `docs/preregistration/2026-06-14-w5-continuous-stage7b-within-symbol-pathshape.md` -
  W5 Stage 7b ADMISSIBLE (both venues): within-symbol fixed-effects partial
  rank-IC over composite (1000-perm within-symbol null; degenerate symbol-hash
  control). After removing ALL symbol mix AND the composite, causal path-shape
  still predicts per-notional return — `pre_24h_return` 0.110/0.105 (p=0.001 both),
  `Q_combined` 0.115/0.114, `pre_24h_realized_vol` 0.097/0.066. Resolves the W4
  symbol-mix confound (within-symbol timing, not symbol selection). `exploratory`;
  feeds Stage 5 `Z2` sizing, which must still clear pooled MAR. A control-degeneracy
  failure exposed and fixed a global-residualization contamination bug; result held.
  Code uncommitted.
- `docs/preregistration/2026-06-14-w5-continuous-stage5-pathshape-sizing.md` -
  W5 Stage 5 Z2 path-shape sizing NULL (both venues): sizing the SAME trades by the
  causal within-symbol path-shape residual (gross-neutral, additive engine hook
  `size_mult_lookup`, 329 tests pass) raises return but worsens drawdown more
  (bybit MAR 4.748→3.993, pooled ΔMAR −0.33) and is **beaten by the Z6
  symbol-identity negative control** (+0.337) — the sizing effect is symbol
  dispersion, not path-shape; binance gross crept to 1.064 (residual leverage).
  **Path-shape is real (Stage 7b) but not harvestable** via entry priority (Stage 1
  NULL) or sizing (this NULL). `exploratory`; clean kill. Code uncommitted.
- `docs/preregistration/2026-06-14-w5-continuous-stage8-regime-hedge.md` -
  W5 Stage 8 regime-hedge (R4) NULL-but-promising (both venues): distinct from the
  E2-closed V1/V2 entry-gate family — keeps V0 entries, modulates only the BTC hedge
  intensity by a causal mean-1 BTC-vol percentile regime (additive engine hook
  `hedge_intensity`, default None → byte-identical, 343 tests pass; V0 reproduces the
  Stage 0 ensemble). R4 improves pooled MAR **+0.078 both venues** (bybit +0.108,
  binance +0.048), constant avg hedge, no DD increase, **beats the R5 hash control by
  +0.69** — the program's first both-venue control-beating signal — but misses the
  +0.1 bar and is cost-fragile (2× hedge cost → +0.008, binance flips neg). NULL on
  the bar (not moved); lead = a lower-turnover regime-hedge follow-up. Code
  uncommitted.
- `docs/preregistration/2026-06-14-w5-continuous-stage8b-lowturnover-regime-hedge.md` -
  W5 Stage 8b lower-turnover regime-hedge NULL (both venues): banded hysteretic
  BTC-vol intensity (0.7/1.0/1.3) cut hedge resizes ~25× (26–28 vs ~660) but NOT
  hedge cost (few big 0.6-step jumps), and the coarse/lagged response mistimed the
  hedge — pooled MAR **−0.005** (bybit flipped +0.108→−0.063, binance stable +0.052).
  Falsifies the lower-turnover fix; shows Stage 8's +0.078 was partly bybit form-luck
  (form-stable ~+0.05 binance). **Regime-hedge family banked: real but sub-threshold,
  not harvestable above +0.1 net of hedge cost in continuous OR banded form.** Next
  lever = a different stage (exit/entry-style/sniper). Code uncommitted.
- `docs/preregistration/2026-06-14-w5-continuous-stage3-exit-alpha.md` -
  W5 Stage 3 exit alpha (mfe_giveback) NULL — decisively harmful (both venues). The
  trailing gain-lock exit (same entries; additive `hash_exit_prob` neg-control hook,
  551 tests pass) crashes return on a mean-reversion fade book — bybit 0.77→0.53,
  binance 0.64→0.26, pooled MAR **−2.29**, WORSE than the random hash control (−0.92).
  Lesson: a bounce is noise preceding continued reversion, so exiting early (avg hold
  20→16h) cuts winners before full reversion; the fixed-hold-to-TP control is
  near-optimal and *earlier* exits fight the thesis. Exit lever closed in the
  exit-earlier direction. Code uncommitted.
- `docs/preregistration/2026-06-14-w5-continuous-stage3b-hold-extension.md` -
  W5 Stage 3b hold-extension (24h→48h) NULL — exit lever closed BOTH directions. Longer
  hold captures more reversion on bybit (return 0.77→0.98) but worsens DD both venues;
  binance MAR collapses 5.255→1.715; pooled MAR **−1.708**, split-venue. (Confound:
  fixed-mode cooldown = hold_hours → entries −4%.) With Stage 3, the fixed 24h hold is
  near-optimal (reversion-vs-squeeze balance). Fails strict + looser bar. Code
  uncommitted.
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
  gate stands; down/euphoria treatment stays hedge + stops/caps. The V1/V2
  engine modes (`uptrend_capped`/`soft3` + the `btc_trend_euphoria_cap` /
  `btc_soft3_size_frac` knobs), the E2 receipt, and `scripts/e2_regime_family.py`
  were removed 2026-06-14 as dead knobs (git history is the archive).
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
- **W5 Stage 7 path-shape (2026-06-14): NULL-as-registered on a broken metric →
  Stage 7b ADMISSIBLE.** Stage 7's registered statistic (cross-venue pooled
  tercile spread) is noise-dominated on this heavy-tailed per-notional return
  cross-section — a 400-draw null gives per-symbol-random spread SD 128/175 bps and
  per-trade-random ±170 bps, so the 25-bps floor and spread-vs-control comparison
  are noise. Banked NULL on the registered gate (not overturned post-hoc). The
  robust rank-IC was clean (path-shape residual 0.22/0.21, ~4–5 SD over the
  per-symbol null, ~2× the symbol-hash control). **Methodology lesson banked: on
  continuous per-notional returns use rank-IC + permutation null, NOT tercile
  spread.** **Stage 7b (within-symbol fixed-effects partial rank-IC over composite,
  1000-perm within-symbol null) is ADMISSIBLE on both venues:** after removing ALL
  symbol mix AND the composite, causal path-shape still predicts return —
  `pre_24h_return` 0.110/0.105 (p=0.001 both), `Q_combined` 0.115/0.114, with a
  degenerate symbol-hash control. This is the "Stage 3b" W4 promised and resolves
  the W4 symbol-mix confound (within-symbol timing, not symbol selection).
  `exploratory` — admissibility ≠ MAR; it must clear pooled MAR in Stage 5 `Z2`
  sizing. A control-degeneracy check caught and fixed a global-residualization
  contamination bug (the fix is stricter; the result held). Receipts
  `docs/preregistration/2026-06-14-w5-continuous-stage7-path-shape-neutralized.md`
  and `docs/preregistration/2026-06-14-w5-continuous-stage7b-within-symbol-pathshape.md`.
- **W5 Stage 5 path-shape sizing (2026-06-14): NULL — path-shape lever exhausted.**
  Sizing the SAME trades by the causal within-symbol path-shape residual
  (gross-neutral; additive engine hook `size_mult_lookup`, default None →
  byte-identical, 329 continuous tests pass; Z0 reproduces the Stage 0 ensemble
  exactly) raises return on both venues but worsens drawdown more (bybit MAR
  4.748→3.993; pooled ΔMAR **−0.33**), and is **beaten by the Z6 symbol-identity
  negative control** (pooled ΔMAR +0.337) — the only positive sizing perturbation is
  symbol-level return dispersion (a random per-symbol tilt catches high-return
  symbols as well as path-shape), not the within-symbol signal. binance realized
  gross crept to 1.064 (residual leverage from feature uptrend + per-component
  dup-weighting). The positive test-fold (OOS) ΔMAR (+0.33/+0.60) is non-decisive
  (doesn't hold full-window, doesn't beat Z6). **Conclusion: the within-symbol
  path-shape signal is statistically real (Stage 7b, IC ~0.10) but NOT harvestable
  as risk-adjusted return via entry priority (Stage 1 NULL) or sizing (this NULL)** —
  the IC is too small and too entangled with drawdown. Do not re-run path-shape
  priority/sizing; remaining path-shape uses (entry-style/exit) are lower-prior.
  Receipt `docs/preregistration/2026-06-14-w5-continuous-stage5-pathshape-sizing.md`.
- **W5 Stage 8 regime-hedge (R4) (2026-06-14): NULL on the bar, but the program's
  strongest lead.** Mechanistically distinct from the E2-closed V1/V2 entry-gate
  family (which lost ~20pp / halved MAR): R4 keeps the V0 binary-uptrend gate and
  every trade identical, and modulates ONLY the BTC hedge intensity by a causal,
  mean-1, drift-robust BTC-vol percentile regime (hedge more in turbulence, less in
  calm) — the lever E2 itself pointed to ("drawdown treatment remains: hedge").
  Additive engine hook `hedge_intensity` in `continuous_rebalance.apply_rebalance_rule`
  (default None → byte-identical; 343 tests pass; V0 reproduces the Stage 0 ensemble
  exactly). R4 raises pooled MAR **+0.078 on BOTH venues** (bybit 4.748→4.856, binance
  5.255→5.303) at constant average hedge (mean intensity ~0.985, no DD increase) and
  **beats the R5 hash-regime control by +0.69 pooled** (R5 −0.614), with the gain
  concentrated in high-BTC-vol regimes (the hypothesized mechanism, not one bucket).
  **This is the first both-venue, control-beating positive MAR signal in the W5
  program.** It nonetheless **misses the +0.1 Tier-2 bar** (+0.078) and is
  **cost-fragile**: at 2× hedge cost the pooled delta collapses to +0.008 and binance
  flips negative — the percentile-daily intensity churns the hedge, so turnover cost
  eats most of the timing benefit. Per registration the bar is NOT moved → NULL. The
  cost fragility motivates one distinct follow-up: a **lower-turnover regime-hedge**
  (smoothed/banded/regime-change-triggered intensity) under a fresh receipt with
  locked params (not a λ/threshold re-tune). Receipt
  `docs/preregistration/2026-06-14-w5-continuous-stage8-regime-hedge.md`.
- **W5 Stage 8b lower-turnover regime-hedge (2026-06-14): NULL — closes the
  regime-hedge family.** The banded hysteretic intensity (the registered fix for
  Stage 8's cost fragility) cut hedge resizes ~25× but NOT hedge cost (few big
  0.6-step jumps) and mistimed the hedge: pooled MAR **−0.005**, bybit flipping
  +0.108→−0.063 while binance held +0.052. Two lessons: (1) the turnover-cost
  fragility is in resize MAGNITUDE not count, so fewer-but-bigger resizes don't help;
  (2) Stage 8's +0.078 was partly bybit form-luck — the form-stable benefit is ~+0.05
  (binance), below the bar. **Regime-hedge family (BTC-vol hedge intensity) banked as
  real-but-sub-threshold across two distinct forms; do not re-parameterize.** A
  different regime *signal* (multifactor/dispersion) is a separate lower-prior idea.
  Next W5 lever = a different stage (Stage 3 exit / Stage 2 entry-style / Stage 4
  sniper). Receipt
  `docs/preregistration/2026-06-14-w5-continuous-stage8b-lowturnover-regime-hedge.md`.
- **W5 Stage 3 exit alpha (mfe_giveback) (2026-06-14): NULL — decisively harmful.**
  A causal MFE-giveback trailing gain-lock (trigger 0.05 / retain 0.50, SAME entries —
  counts identical; additive `hash_exit_prob` neg-control hook, default 0, 551 tests
  pass; X0 reproduces the Stage 0 ensemble) crashes return on both venues (bybit
  0.77→0.53, binance 0.64→0.26), pooled MAR **−2.29**, and is **worse than the random
  hash-exit control** (−0.92). Mechanism: this is a mean-reversion fade book, so a
  bounce (50% giveback of the peak favorable move) is noise that precedes *continued*
  reversion; exiting on it (avg hold 20→16h) cuts the winner before the fade completes
  to its fixed TP. **Earlier exits fight the thesis; the fixed-hold-to-TP control is
  near-optimal.** Hold-shortening path/signal-decay exits (e.g. X1 rank-decay) are
  expected to fail the same way; only a *longer*-hold score-conditioned time cap (X3)
  is a distinct untested exit direction, lower-prior. Receipt
  `docs/preregistration/2026-06-14-w5-continuous-stage3-exit-alpha.md`.
- **W5 CROSS-LEVER CONCLUSION (2026-06-14, updated 2026-06-15):** the frozen continuous
  control (MAR ~4.75 bybit / ~5.26 binance) resisted MOST tested levers vs the strict +0.1
  Tier-2 bar — entry priority (Stage 1: no room), path-shape priority+sizing (Stage 7b real
  IC~0.10 but Stage 1/5 not harvestable, beaten by symbol-identity), regime book-sizing
  (Stage 9 harmful), exits (Stage 3/3b: both directions harmful — 24h hold near-optimal), and
  entry SELECTION (Stage 4 liquidity sniper: looked like a both-venue +0.407 at k=10% but the
  decile-robustness follow-up Stage 4d FALSIFIED it — venue-split, bybit-noise; see the Stage
  4/4d entries). **Net: no tested lever yields a robust both-venue MAR harvest beyond the
  regime-hedge.** The lone validated improvement remains the BTC-vol regime-hedge. **UPDATE (operator loosened the bar 2026-06-15 to "any robust
  improvement that takes trades"): the Stage 8/8c regime-hedge is the W5 candidate** — at
  the realistic 1× hedge cost it is a robust, both-venue, control-beating (+0.6–0.8 over
  hash), trade-keeping +MAR improvement (pooled ~+0.05–0.08, λ-robust across
  {0.25,0.5,0.75}), bybit robustly positive at all costs (+0.075→+0.108). Its one fragility
  is binance cost headroom (breaks even ~1.2×, −0.011 at 1.5×), failing the predeclared
  1.5×-cost stress marginally. Proposed as a **demo/paper forward-watch candidate** (Tier-3
  real-money gate unchanged). **Effort to firm the binance headroom is EXHAUSTED and the
  candidate is consolidated:** Stage 9 (regime book-sizing) NULL & harmful (the fade book
  *profits* in high vol — hedge the tail, don't shrink the book; −0.633, worse than random);
  Stage 8d/8e/10 (regime-signal search) show **BTC-vol is the unique both-venue-positive
  signal** — book-DD is binance-only, dispersion is bybit-only, book-vol/blend negative; and
  EMA-smoothing the intensity was ruled out without a run (intensity is mean-1, so there is
  no intensity-churn cost to cut — Stage 8 R4 hedge cost ≈ V0; the 1.5× break is base hedge
  economics).
- **W5 Stage 4 LIQUIDITY SNIPER (2026-06-15): [SUPERSEDED by Stage 4d — DOWNGRADED to
  venue-split, NOT robust both-venue]** ~~ROBUST — the program's strongest result and
  first clearly harvestable selection alpha, clearing the strict +0.1 bar on BOTH venues.~~
  The k=10% result below did NOT survive decile robustness (Stage 4d): on bybit a random drop
  matches it at k=5% and beats it +4.16 MAR at k=20%, and the single-seed random control had
  huge MAR variance (4.6→7.6) — so the bybit "edge" was favorable noise; only binance shows a
  consistent (modest) liquidity effect. Net venue-split. Original (superseded) write-up:
  A within-symbol IC screen found `turnover_quote` (liquidity) is the unique untested
  both-venue selection signal (partial rank-IC over composite +0.081 bybit / +0.134 binance,
  p=0.001, symbol-hash control degenerate). The engine sniper **drops the least-liquid decile**
  of fades (causal trailing-180d turnover percentile, `size_mult=0` via the Stage 5 hook →
  de-levers those slots, kept trades byte-identical, BTC hedge resizes) vs a per-day
  count-matched random-drop control. **Pooled 1× ΔMAR +0.407 (bybit 4.748→4.907 +0.159,
  binance 5.255→5.910 +0.655)**, and DECISIVELY beats the random-drop control at 1× (+0.329 /
  +1.000) and 2× cost (+0.094 / +0.831) — liquidity SELECTION, not de-leveraging. DD flat
  (bybit) to improved (binance −3.97%→−3.52%); keeps ~90% of trades; dropped set non-degenerate
  (104/108 symbols, top-share <0.04, all thirds); both venues +; not carried by one third
  (binance 3/3, bybit 2/3). **`candidate`** (demo/paper forward-watch; Tier-3 real-money gate
  UNCHANGED) — the second W5 candidate and stronger than the regime-hedge. Honest caveats:
  binance gain is drawdown-driven, bybit return-gain concentrates in the latter 2/3; part of
  the edge is execution-cost avoidance (cost model charges impact ∝ notional/ADV, so dropping
  low-ADV fades cuts the highest modeled-impact trades) — real but partly model-dependent, so
  forward demo fills validate. Next: a sniper+regime-hedge COMBINATION receipt (do the gains
  stack?) and a drop-decile robustness check (k∈{5,15,20}%). Receipt
  `docs/preregistration/2026-06-15-w5-continuous-stage4-sniper.md`.
- **W5 Stage 4c SNIPER × REGIME-HEDGE COMBINATION (2026-06-15): [SUPERSEDED by Stage 4d —
  inherits the sniper's bybit fragility]** ~~STACKS SUPER-ADDITIVELY — the strongest, most-robust
  W5 result and the program's deliverable.~~ The combination uses the k=10% sniper pieces, which
  Stage 4d showed are not k-robust on bybit; the bybit combo gain is mostly the (real) BTC-vol
  regime-hedge plus fragile sniper noise. The robust both-venue piece is the regime-hedge alone.
  Original (superseded) write-up: Cheap component reuse
  (Stage 4 `T1_turnover_drop` pieces + BTC-vol hedge intensity λ=0.5; no engine — C0/S
  reproduce Stage 4, H reproduces Stage 8c). The combination (SH) beats the frozen control,
  the sniper alone, AND the regime-hedge alone on **both venues at every hedge cost**: pooled
  ΔMAR vs control **+0.569 (1×) → +0.588 (2×)** (bybit +0.340→+0.348, binance +0.797→+0.828),
  with a positive interaction on both venues (+0.073 / +0.093 — super-additive). **It resolves
  the regime-hedge's one weakness — thin binance cost headroom:** the hedge alone went negative
  on binance at 1.5× (Stage 8c), but the combo is +0.797→+0.828 across 1×→2× because the
  sniper's selection is itself cost-robust (it drops the highest-impact trades, so its benefit
  RISES with cost, +0.407→+0.424 pooled). Mechanistically complementary — sniper = book
  quality (cut low-liquidity/low-gross-EV fades), hedge = tail risk (protect the squeeze in
  high vol). **Recommended demo/paper forward-watch package: sniper(k=10%) + BTC-vol
  regime-hedge(λ=0.5)**, pooled ~+0.57 MAR both venues, cost-robust, ~90% trades kept; Tier-3
  real-money gate UNCHANGED (forward demo validates the sniper's execution-cost component;
  a fresh-receipt engine confirmation of the combo precedes any forward-watch). Receipt
  `docs/preregistration/2026-06-15-w5-continuous-stage4c-sniper-hedge-combo.md`.
- **W5 Stage 4d LIQUIDITY-SNIPER DECILE ROBUSTNESS (2026-06-15): the Stage 4/4c headlines DO
  NOT survive — DOWNGRADE.** Bracketing the pre-registered k=10% with k=5% and k=20% (each with
  its own count-matched random-drop control) shows the sniper's MAR improvement is NOT robust
  to the drop fraction. Turnover-drop ΔMAR vs the frozen control — bybit: +0.707 / (+0.159) /
  **−1.312** across k=5/10/20%; binance: −0.024 / (+0.655) / +0.030. Two lessons: (1) **the
  single-seed random-drop control is inadequate at these drop sizes** — bybit random MAR swings
  5.499 (k5) → 4.577 (k10) → **7.592** (k20) purely by which drawdown-causing trades the one
  random draw happens to hit, so the k=10% "beats random +0.33" on bybit was within the control
  noise; a multi-seed null is required. (2) **The sniper is VENUE-SPLIT** — bybit has no robust
  liquidity effect (random matches/beats it at k=5/20, strongly negative at k=20), while binance
  shows a consistent but modest effect (turnover-drop beats random at all k, ≥ control, peaking
  ~k=10%). So the Stage 4 "+0.407 both-venue" and Stage 4c "+0.569 combo" were a favorable k=10%
  cut on bybit; the **BTC-vol regime-hedge (Stage 8c) reverts to the sole validated both-venue
  candidate.** The within-symbol liquidity IC is real (esp. binance) but, like path-shape
  (Stage 5/7b), does not translate to a robust both-venue MAR harvest. Receipt
  `docs/preregistration/2026-06-15-w5-continuous-stage4d-decile-robustness.md`.
- **W5 Stage 2 entry-style + hedge-instrument NULLs / PROGRAM CONVERGENCE (2026-06-15).** Two
  entry-style ideas closed with NO engine spend (Stage 4d discipline): the deceleration filter
  has a negative prior (a 6h-magnitude filter; Stage 7b showed bigger pumps fade better, so it
  drops the best fades), and entry-funding screened NULL (within-symbol partial funding IC over
  composite weak/insignificant — bybit +0.032 p=0.111, binance +0.051 p=0.028). A hedge-instrument
  probe (ETH vs BTC, component reuse) is NULL — ETH is a worse hedge leg on both venues (regime
  ΔMAR bybit −0.166, binance −0.403; ETH adds leg-variance/tracking-error). **Net: ~16 distinct W5
  mechanisms tested; the BTC-vol regime-hedge (Stage 8c) is the SOLE robust both-venue edge and the
  recommended demo/paper forward-watch deliverable** (λ=0.5; Tier-3 real-money gate unchanged). The
  continuous control's entry/exit/sizing is near-optimal; only the tail-hedge overlay harvests; a
  real binance-only within-symbol liquidity effect is a forward-watch note, not a both-venue
  candidate. Receipt `docs/preregistration/2026-06-15-w5-continuous-stage2-funding.md`.
- **W5 PROGRAM CONVERGED (2026-06-15) — final.** Two more closes: (1) the **aggregate-funding
  hedge regime** is NULL (pooled ΔMAR −0.016/−0.010/−0.004, fails the control on bybit, worse
  than BTC-vol both venues) → the hedge-signal space is closed and **BTC-vol is the unique
  both-venue hedge regime across all six tested signals** (BTC-vol, book-vol, book-DD,
  dispersion, multifactor, aggregate-funding); receipt
  `docs/preregistration/2026-06-15-w5-continuous-stage8g-funding-regime-hedge.md`. (2) the exit
  lever is exhausted — the frozen control ALREADY exits on 24h-hold OR a take-profit (~25–28% of
  trades exit via TP), so "adding a take-profit" is re-parameterizing a calibrated knob, not a
  distinct lever. **~18 distinct mechanisms tested; the sole robust both-venue edge is the
  BTC-vol regime-hedge (Stage 8c, λ=0.5)** — characterized (Stage 8f) as a MODEST,
  sub-period-variable tail-insurance overlay (return-additive, beats the random-regime control,
  pooled +0.05–0.08 MAR, not a smooth uniform edge). Root cause of every NULL: the book's edge is
  diffuse and it profits when broadly deployed in dislocations, so selecting/shrinking/derisking
  the entry set forgoes that diffuse profit; only the tail-hedge overlay (keep the book, hedge the
  squeeze) harvests. **Recommendation: green-light demo/paper forward-watch of the regime-hedge
  (operator-gated; Tier-3 real-money gate unchanged) or supply a new research direction; in-sample
  search is complete.**
- **W5 Stage 10b dispersion hedge — gross-corrected, BINANCE-real (corrects a Stage 10 bug);
  bybit-primary steer (2026-06-15).** Operator steer ("we trade on bybit; bybit-robust + not
  losing on binance — worth it?") prompted re-examining the Stage 10 dispersion NULL. It was
  confounded by a gross-neutrality BUG (omitted prior-month normalization, ~15% over-hedge):
  clean, dispersion is binance **+0.293** (the Stage 10 "−0.273" was the bug — a sign flip) and
  a ROBUST binance hedge regime (beats random-regime hash + constant-level controls at every
  λ/cost, monotone in λ +0.14→+0.52, sub-period-OK), while bybit +0.04 is NOISE (fails the hash
  control at λ0.25, negative in 2/3 thirds — the sniper fragility). So dispersion is venue-split
  **binance-real / bybit-noise**; the BTC-vol×dispersion stack is binance +0.368. **Bybit-primary
  verdict:** the bar must stay bybit-ROBUST (not bybit-lucky-in-one-config); applied rigorously,
  neither dispersion nor the sniper qualifies (their robust edge is on binance), so **BTC-vol
  regime-hedge remains the one robust bybit edge.** If a binance sleeve also runs, dispersion/stack
  is a real per-venue binance hedge (forward-watch option, not a both-venue candidate). Receipt
  `docs/preregistration/2026-06-15-w5-continuous-stage10b-dispersion-clean.md`.
- **W6 orderflow squeeze (OI-buildup) — A1 sizing + A4 hedge-regime BOTH NULL (2026-06-15).**
  The exploratory screen found a real within-symbol IC (`oi_chg_24h` bybit +0.0665 p=0.002),
  but neither harvestable mode converts it. **A1 (mean-1 gross-neutral SIZING up of high-squeeze
  fades):** return rises monotonically with tilt (0.77→0.80) but drawdown grows faster → bybit
  ΔMAR −0.40/−0.26 at k0.5, gross-neutral confirmed (no leverage artifact); the W5 diffuse-edge
  root cause on the orderflow axis. **A4 (aggregate book-squeeze × the live BTC-vol hedge
  intensity):** λ0.5 +0.057 vs the BTC-vol baseline is WITHIN the 8-seed shuffle-control noise
  (−0.34…+0.10, 2/8 beat it), not λ-robust (λ0.25 −0.087), cost-fragile (1.5× → −0.07) — extends
  Stage 8d/8e/8g (BTC-vol is the unique hedge regime). binance squeeze data-gated (OI ~6wk).
  **Lesson reinforced: a real orderflow IC does NOT robustly harvest as sizing OR as a hedge
  regime; the multi-seed shuffle control (not single-seed) is what kills the A4 favorable cut.**
  Receipts `2026-06-15-w6-squeeze-proxy-sizing.md`, `2026-06-15-w6-squeeze-hedge-intensity.md`.
  **A5 (per-day gross scaler by squeeze-breadth) skipped by cheap diagnostic** — squeeze-breadth
  → daily return is real-but-weak (breadth Spearman +0.094 p=0.038), the same edge A1 proved
  doesn't survive DD; a gross-up faces the same mechanism on a weaker signal (low prior).
  **Data-gating:** B1 (intrabar entry-price timing) is blocked locally — `tick_ohlc_1m`/all
  sub-hourly klines are 0 partitions; only `taker_flow_5m` exists. Next lead = squeeze-conditioned
  crowding ADMISSION (the one diffuse-edge-friendly entry-set direction: ADD high-squeeze fades
  the crowding gate rejects — screen rejected-entry outcomes first, then an engine hook).
- **W6 crowding-ADMISSION screen — POSITIVE bybit prior, the program's first non-NULL lead
  (2026-06-15).** Hypothetical causal fade model VALIDATED against the ledger (return corr
  0.996/0.999, entry-price MAPE 0.0). The 1166 crowding-rejected bybit fades (rejected by
  `entry_crowding_max_fresh=2`, the 3rd+ fresh candidate per signal-hour, with concurrency
  slots FREE) are **profitable: mean +37bp/fade net of 15bp cost** (high-squeeze tercile +109bp,
  low-squeeze +78bp). So the crowding gate is REJECTING PROFITABLE FADES — the diffuse-edge
  logic predicts admitting them ADDS book profit (this ADDS deployment, unlike the failed
  select/size/time levers). Squeeze CONDITIONING is weak (within-symbol IC +0.022, p=0.25, NS;
  control degenerate) → the value is blanket admission, squeeze a weak tilt. binance squeeze
  data-gated (OI ~6wk). **Next = the engine config sweep on `entry_crowding_max_fresh` tested
  on MAR (leverage-invariant → a MAR rise = genuine breadth quality, not leverage), with a
  constant-leverage control, cost stress, thirds, bybit-robust bar.** Receipt
  `2026-06-15-w6-crowding-admission.md`; screen `scripts/w6_crowding_admission_screen.py`.
- **W6 crowding-ADMISSION engine sweep — NULL (2026-06-15), closing Track A.** Despite the
  positive screen, admitting the rejected fades (entry_crowding_max_fresh 2→4→999) LOWERS MAR
  (ΔMAR −0.14/−0.075): return rises (0.77→0.80) and trades +8% but maxDD grows faster, and at
  MATCHED gross admission is far worse than uniformly leveraging the existing book (constant-
  leverage control +0.168 vs admission −0.075) — the admitted fades are tail-correlated
  (concurrent-squeeze days). **The crowding cap (max_fresh=2) is near-optimal.** SHARPENED ROOT
  CAUSE (W5+W6): the book's edge is diffuse but its TAIL is correlated-concurrent, so every lever
  that adds/sizes book exposure (sizing A1, gross A5, admission) concentrates the tail → MAR
  falls; only the BTC-vol HEDGE harvests (tail protection, no added exposure). **W6 Track-A
  convergence: the orderflow squeeze signal is a real IC that harvests in no mode; in-sample
  search on local data is complete. Paths forward = forward-watch the live BTC-vol hedge + data
  accrual (binance OI tape E4, sub-hourly price/tick for B1, depth for B2/B4) + operator steer.
  Do not manufacture low-prior in-sample experiments (Stage-4 false-positive risk).** Receipt
  `2026-06-15-w6-crowding-admission.md`.
- **W6 Track B (cost/execution alpha) — data accrued (bybit 5m, 3yr, 610 symbols) + B1/exhaustion
  entry-timing NULL (2026-06-16).** Operator authorized a bulk sub-hourly download; bybit
  `klines_5m` now full-coverage in `bybit_full_pit/klines_5m`. **B1 entry-PRICE timing** (chase-limit
  sell-higher) is a clean NULL once measured correctly: the first screen's "t=-10 catastrophic" was a
  FILL-CONVENTION BUG (caught by the convergence audit) — it benchmarked against close at signal+1h,
  but the engine fills at signal+**2h**. Corrected on local 5m with the real +2h baseline
  (`w6_entry_timing_corrected.py`): chase-limit is −7 to −11 bp/trade but INSIGNIFICANT (|t|<0.9) at
  all offsets — no edge, the +2h fill already captures the pump. **Exhaustion-conditioned
  entry** falsified the opposite way: intra-window continuation `cont_ret` IC **+0.094 p=0.002**
  (HIGHER continuation → BETTER fade; high-cont win 72% vs 59% low-cont) — re-confirms Stage-7b
  "bigger pumps fade better" (path-shape family, real-but-unharvestable), and shows the runaway
  losers (−19% mae) are NOT identifiable from intra-window price action at the fill. **Conclusion:
  the fixed +1h delayed fill is near-optimal execution; the entry-price-improvement and
  exhaustion-selection angles are closed.** Funding-carry also NULL (funding_return ~1e-5/trade;
  edge is pure price-fade). Scripts `w6_entry_timing_screen.py`, `w6_exhaustion_entry_screen.py`.
- **W6 funding-data finding (system-wide, 2026-06-15).** The audit guard
  `_assert_funding_one_per_settlement` false-positives on ~89 bybit symbols that genuinely
  settle sub-8h (verified vs the authoritative endpoint), blocking all bybit engine backtests;
  data + charge are correct, `funding_interval_min` is mislabeled 480. Corrected research-scoped
  guard installed; proper fix operator-gated. Doc
  `docs/audit/2026-06-15-funding-interval-mislabel-guard-falsepositive.md`.
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

- **E2 regime response:** NULL in the registered V1/V2 form (receipt + V1/V2
  engine modes removed 2026-06-14; git history is the archive). V0 (the binary
  uptrend gate) stands until a materially different, preregistered regime
  mechanism clears both-venue evidence — a new mechanism gets its own receipt.
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
