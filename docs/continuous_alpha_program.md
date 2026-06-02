# Continuous-fade — quant-inspired alpha program (loop)

Self-paced research loop ("perfect the strategy, cover all bases"). Each iteration tests ONE new
quant-canonical base in the engine (`scripts/alpha_sweep.py`), cross-venue, plateau-not-spike, MTM-MAR/DD,
EXPLORATORY (forward demo is the arbiter). Discipline: `docs/backtesting_errors_we_never_repeat.md`. The
live profile only changes on operator direction.

## Already covered (this session)

- Exit: mfe_giveback (small, superseded by rmom33), max_hold (dead), breakeven/ff6 (adopted).
- Entry: **rmom-tighten 0.50→0.33 (APPLIED — the big cross-venue win)**, liq-raise (venue-divergent),
  turnover-surge (dead — refutes "re-inject the event").
- Rebalance/risk: max_active (leverage dial, not alpha), circuit breaker (tail insurance, w24/n8),
  rotation (low). Receipt: `docs/preregistration/alpha-sweep-2026-06-02.md`.

## Bases to cover (quant inspiration) — the backlog

1. **Volatility targeting (de-risk-only gross overlay)** — scale the book down when trailing realized
   vol is high (squeeze regimes); the long sleeve's validated `div` technique, untested on continuous.
   Attacks the #1 risk (squeeze tail) at the portfolio layer. [ITERATION 1]
2. **Beta-neutral L/S overlay (D0-long / D9-short)** — the research's named value-add + the 3-way
   redundancy question (is it additive to the existing long sleeve?). [big]
3. **MAX / lottery-demand feature** (Bali-Cakici-Whitelaw) — a pumped alt IS a lottery stock; add the
   extreme single-bar return as a selection feature/condition. Panel rebuild.
4. **Feature orthogonalization / drop collinear vov** — the composite is 5 collinear features; test
   subsets / a de-correlated blend.
5. **Time-of-day & session seasonality** — crypto intraday seasonality + the I-phase peak-fade-hour
   (~16-17 UTC); entry/exit hour conditioning.
6. **entry_delay=0 realistic fidelity** — the live sleeve enters at 0h; only +1h is validated. Quick.
7. **Funding-aware entry/exit** — funding-timed; crowded-short funding spikes at the pump peak.
8. **Vol-scaled (ATR) stop vs fixed-% stop** — risk-overlay refinement.
9. **Idiosyncratic-vol / co-skewness sorts** — adjacent cross-sectional anomalies.

## Log

- **Iter 1 — volatility-targeting overlay: NEGATIVE (not alpha).** At sensible targets (40-80% ann) it
  no-ops (book vol ~10% ann). At sub-book targets it only DELEVERS — on the adopted rmom33 config every
  variant is <= rmom33; on base the one "win" (vt5/lb20 MAR 38.6->45.4) costs -45% return and barely moves
  Sharpe (9.5->9.8) = leverage dial, not vol-timing; short lookbacks hurt. Trailing vol can't pre-empt the
  jump/squeeze tail (the real risk). Dead.
- **Iter 2 — entry-delay fidelity = THE BIG FINDING (cross-venue, ~2× MAR).** d1 (+1h, validated) vs d0
  (≈ the live sleeve's intra-hour entry): bybit 38.6 vs **19.5**, binance 30.4 vs **15.1** — d1 ≈ 2× d0 on
  BOTH venues, monotonic decay d1>d2>d3>d6, and d0 (shorting into the pump) is the worst. **The live
  "no-1h" design enters at ~d0 and roughly HALVES MAR vs the validated +1h.** FIX (operator-gated, the #1
  recommendation): select ENTRIES from the CONFIRMED bar-close decile + 1h delay; keep tick-driven EXITS.
  - ff6 sub-sweep: `failed_fade_loss_pct` 4%→**6%** (h6 or h9) is a modest robust cross-venue win
    (bybit +5-9%, binance +5-13%); l6 beats l4 on both. Minor; recommend the nudge.
  - breakeven arm: VENUE-DIVERGENT (bybit wants 5% — but a spike, neighbour a8 below base; binance wants
    8%). No robust cross-venue win → keep 10%.
- **Iter 3 — combined config:** rmom33+ff96 = bybit 44.3 (stacks) but binance 48.4 < rmom33-alone 50.1
  (ff96 increment is venue-divergent, like mfe). **Verdict: rmom33 is the robust core; ff6/mfe/breakeven
  increments are NOT cleanly additive on top of it.** The dominant lever is the entry-timing fix (d0→d1,
  ~2×), independent of all of these.

## CONSOLIDATED RECOMMENDATION (operator-gated; live = Bybit)

1. **Entry at the confirmed bar-close +1h (d1), NOT the intra-hour live cross (d0)** — the #1 fix, ~2× MAR
   cross-venue. Code change: select ENTRIES from the confirmed-bar decile + 1h delay; keep tick-driven
   protective EXITS. (Corrects the "no-1h" fidelity gap.)
2. **rmom_quantile 0.33** — APPLIED; the robust core entry-quality win (+11% bybit / +65% binance).
3. Everything else (ff6/breakeven/mfe nudges, vol-target, liq, turnover-surge, max_active, rotation,
   circuit breaker) — not robust cross-venue alpha on top of 1+2; leave as-is / tail-insurance only.

- **Iter 4 — composite feature changes: VENUE-DIVERGENT (not robust).** At rmom33: add_max (MAX/lottery)
  bybit +6% (45.4) but binance −10% (44.9); drop_vov bybit +2.5% but binance −15%; the both-positive cell
  (dropvov_addmax +1.6%/+8%) is a fragile interaction of two venue-divergent moves → overfit, rejected.
  The 5-feature composite is a venue-robust compromise; feature-engineering it overfits to one venue. The
  engine now supports a configurable feature_set + a MAX feature (default-safe, 46 tests pass) for the record.
- **Iter 5 — L/S construction: INVALID test (my error), no verdict.** I inherited the short's rmom-LOW
  gate on the long D0 leg (a squeeze filter — wrong for longs) → long_d0 MAR −0.33 / ret −57%, dragging
  LS_5050 to 14.8. Not a fair test. The properly-built beta-neutral L/S is already covered by the engine
  research (a diversifier, not a standalone short-fade improvement) — defer to it; don't ship a broken leg.
- **Iter 6 — seasonality: NO win.** Entry-hour quality varies (UTC 00-05 best ~3.4bps/trade, 18-23 worst
  ~0.8bps) but ALL sessions are net-positive → excluding any cuts positive-EV breadth (bybit wants breadth);
  a session tilt is just sizing. Not a lever.

- **Iter 7 — selection method (predictive/optimal-weight) IC diagnostic: composite near-optimal.** All 6
  features have similar positive rank-IC vs the 12h fade (0.18-0.21, none dead/wrong-signed); equal-weight
  COMPOSITE IC = 0.33 (>> any single → features combine well, equal-weight extracts it). With similar-IC
  positively-correlated signals, equal-weight ≈ optimal linear combination → a learned model has little
  headroom + would overfit (cf. the venue-divergent feature tests). Selection method is well-built. No upgrade.
- **Iter 8 — entry-timing fix IMPLEMENTED (the #1 win, ~2× MAR).** `continuous_demo`: new
  `build_confirmed_entry_state` selects ENTRIES from the CONFIRMED bar-close decile at the deciding bar
  (`entry_confirm_delay_hours=1`, the validated +1h point), NOT the live intra-hour cross; EXITS stay
  tick-driven. Reversible (`entry_confirm_delay_hours=0` = legacy). Tested (build_confirmed_entry_state =
  the deciding-bar decile, no live price) + full suite 1101 pass. Default = ON (the fix). **NOT pushed —
  operator deploys** (a structural change reversing the deliberate "no-1h" design; the live demo is the
  arbiter; revert with =0). (A flaky trade-router test — operator's concurrent bybit.py WIP, passes in
  isolation/on rerun — is unrelated.)
- **Iter 9 — recommended config (d1 + rmom33) is ROBUST cross-venue.** bybit point MAR 42.8 / DD 1.8% /
  block-boot MAR p5=36.9 / eras 154%+83%; binance 50.1 / 2.3% / p5=37.0 / eras 186%+166%. Bootstrap p5≈37
  both (≫0, not a lucky path), both eras positive + sign-consistent. Strong Tier-2/3 readiness; mild recent
  decay on bybit (recent still solidly +). EXPLORATORY — forward demo is the arbiter.

## LOOP COMPLETE — productive search exhausted; the two real wins are found, implemented, and robust

Covered 15+ quant-inspired bases (entry timing, selection tightness, predictive/optimal-weight selection,
the MAX/lottery feature, vol-targeting, ff6/breakeven/mfe exits, liq & turnover-surge gates, max_hold,
max_active, rotation, seasonality, L/S, circuit breaker). **Two robust cross-venue wins; nothing else
robustly adds** (each ruled out with evidence above). The edge is captured by good selection + correct
entry timing — feature-engineering, risk overlays, and breadth filters do not improve it (consistent with
the whole arc: "the sophistication that helped was risk machinery, not new signal"). A further blind
sweep would mine noise (multiple-testing) — the genuine remaining levers (borrow-availability, capacity/
impact calibration) need LIVE fill data, not more backtests.

**State:** (1) entry-timing +1h fix IMPLEMENTED (reversible, tested, default-on, NOT pushed — operator
deploys); (2) rmom-0.33 APPLIED + pushed. Both validated + robust cross-venue. The next step is the
operator deploying the entry-timing fix + the forward demo (the only Tier-3 arbiter).

## FINAL SUMMARY (loop wound down 2026-06-02 — productive bases covered)

Tested 13+ quant-inspired bases, cross-venue, plateau-not-spike, MTM-MAR/DD, EXPLORATORY. **Two robust
cross-venue wins; everything else is venue-divergent, dead, a leverage dial, or already-covered research.**

**ADOPT (operator-gated, live = Bybit):**
1. **#1 — Entry at the confirmed bar-close +1h (d1), NOT the intra-hour live cross (d0).** ~2× MAR on BOTH
   venues (bybit 19.5→38.6, binance 15.1→30.4). The live "no-1h" design trades at ~d0 (shorting into the
   pump) — a fidelity gap that roughly HALVES MAR. Biggest single improvement. Code change: select ENTRIES
   from the confirmed-bar decile + 1h delay; KEEP tick-driven protective EXITS. → IMPLEMENTED iter 8
   (`entry_confirm_delay_hours=1`, reversible, tested; NOT pushed — operator deploys).
2. **rmom_quantile 0.33** — APPLIED. Robust core entry-quality win (+11% bybit / +65% binance, DD↓ both).

**NOT adopted (with evidence):** vol-targeting (delever, not vol-timing); composite feature changes incl.
the MAX/lottery feature (venue-divergent — bybit + / binance −); ff6 & breakeven nudges (venue-divergent
increments on rmom33); mfe (superseded by rmom33); liq-raise & turnover-surge (venue-divergent, hurt
bybit-breadth); max_hold (48≈peak); max_active (leverage dial); rotation (low); seasonality (all sessions
positive); circuit breaker (tail-insurance only). Funding is already costed (use_funding) + studied (I2);
ATR/stop-width covered by I2; L/S overlay covered by the engine research (a diversifier, not a short-fade
improvement) — my quick L/S test was invalid (long-leg gate misuse).

**Meta:** the continuous fade's edge IS captured by (rmom-tightened cross-sectional selection + correct
entry timing). Feature-engineering, risk overlays, and breadth filters do not robustly add — consistent
with the whole arc ("the sophistication that helped was risk machinery, not new signal"). The single
highest-value action is implementing the **entry-timing (+1h) fix**. Receipts: this doc +
`docs/preregistration/alpha-sweep-2026-06-02.md`. Engine knobs (feature_set, MAX feature, circuit breaker)
left in place default-safe; not pushed.

## Loop REOPENED — the risk-metric base was not covered (operator re-looped)

- **Iter 10 — INTRADAY (hourly-MTM) drawdown.** All prior sweeps optimized DAILY-MTM DD (~2.6%); the audit
  says the true squeeze risk is the INTRADAY/hourly DD (~6-7%). Built `_portfolio_mtm_equity_hourly` (the
  audit metric, previously only a /tmp script) to measure the CORRECT risk + whether the entry-timing fix
  (d1) also cuts the intraday squeeze tail vs the legacy intra-hour (d0). If it does, the entry-timing fix
  is an even stronger win, and risk controls (stop_approach/breaker) can be re-examined under the right
  metric (they may help the intraday DD even though they didn't help daily-MAR). Running.

### Iter 10 RESULT — intraday risk is CONTAINED + the entry-timing fix cuts the squeeze tail (cross-venue)

Built `_portfolio_mtm_equity_hourly` (the audit's intraday-DD metric). Recommended config (d1+rmom33):

| | bybit d1(fix) | bybit d0(legacy) | binance d1(fix) | binance d0(legacy) |
|---|---|---|---|---|
| MAR | 42.8 | 18.5 | 50.1 | 14.3 |
| daily-DD | 1.8% | 2.6% | 2.3% | 4.8% |
| hourly-DD | 3.4% | 3.9% | 4.1% | 5.4% |
| worst-hour | -1.08% | -1.30% | -1.54% | -1.43% |

- **True intraday DD is contained** (3.4% bybit / 4.1% binance) — NOT the 6-7% the audit flagged for the
  *promoted* profile; the daily-MAR I optimized was not misleading for this config.
- **The entry-timing fix STRICTLY DOMINATES on both venues**: higher MAR, lower daily-DD, lower intraday-DD
  (on binance the legacy intra-hour entry *doubles* the daily DD, 4.8% vs 2.3%). Avoiding shorting INTO the
  pump improves return AND cuts the squeeze tail — not a risk/return tradeoff. Strengthens the #1 deploy case.
- **Iter 11 — cost-model robustness** (running): the impact/slippage cost is uncalibrated (audit flag) and I
  optimized at 1x. Stress the recommended config at 1.5/2/3x realized cost cross-venue — does the edge survive?

### Iter 11 RESULT — cost-model robustness: edge survives 2x cost cross-venue (NOT a cost artifact)

Stressed the recommended config (d1+rmom33) at 1/1.5/2/3x the modeled impact+slippage cost (rescale realized
cost on the existing trades, re-MTM):

| cost mult | bybit MAR / DD | binance MAR / DD |
|---|---|---|
| x1.0 | 42.8 / 1.8% | 50.1 / 2.3% |
| x1.5 | 35.4 / 1.9% | 32.8 / 2.9% |
| x2.0 | **28.5 / 1.9%** | **21.0 / 3.5%** |
| x3.0 | 9.8 / 3.5% | 1.4 / 22.6% |

Edge robust to 2x modeled cost on BOTH venues; the live venue (bybit) survives 3x. Binance has a capacity
cliff between 2x-3x (worth knowing for sizing). The modeled cost is already a reasonable-conservative
fees+impact+slippage estimate -> the wins are NOT a cost-model artifact. Closes the audit's cost uncertainty.

### Iter 12 RESULT — funding-conditioned entry: NO alpha, NO risk benefit

Tested funding (short-crowding/carry) as an entry signal/risk filter, causal asof at the signal bar. Decisive
evidence = bybit (full funding coverage, n=11870): top_tercile (long-crowded, "best fade") 24.2 ~= bottom_tercile
(short-crowded, "squeeze risk") 23.3 -> funding does NOT discriminate good fades from bad. Every gate only cuts
breadth (lower MAR); DD barely moves (1.5-1.8%) so it's not a useful risk trim either. Binance funding coverage
is sparse (asof-join kept 1744 of 24193 -> degraded subsample, baseline 12.6 not comparable to 50.1) but agrees
qualitatively (top 10.0 ~= bottom 9.2). Verdict: funding-conditioning does not add. Consistent with the pattern.

## BACKTEST RESEARCH COMPLETE (re-loop extension) — ~18 bases covered; two robust wins; rest is noise

This re-loop added three genuine NEW bases (risk metric, cost robustness, funding) — all reassuring/conclusive:
- **Risk metric (iter 10):** true intraday DD is contained (3.4-4.1%), and the entry-timing fix cuts it.
- **Cost robustness (iter 11):** edge survives 2x modeled cost on both venues. Not a cost artifact.
- **Funding (iter 12):** no entry alpha, no risk benefit (top~=bottom tercile).

The conclusion has been stable for ~6 iterations: the edge = good SELECTION (composite + rmom33) + correct
ENTRY TIMING (+1h, the dominant ~2x lever). Robust cross-venue (bootstrap p5~=37), robust to 2x cost, contained
intraday risk, and the entry-timing fix DOMINATES on both return and risk. ~18 distinct bases tested; every
micro-refinement is venue-divergent noise or dead.

The genuinely-untested remainder (ATR/vol-scaled stop, concentration/correlation cap, scale-in entry) is the
low-EV dregs: each targets an already-small effect, and the established pattern predicts venue-divergent noise.
Testing more would be multiple-testing/mining, which the methodology gate forbids. **The remaining unknowns
(true capacity, real slippage, borrow, live fills) are answerable ONLY by the forward demo — the real arbiter.**

THE PATH TO "PERFECT" NOW: (1) operator deploys the entry-timing fix (#1 win, implemented, reversible, NOT
pushed); (2) forward demo settles it. Not more backtests.

### Iter 13a RESULT — CONCENTRATION cap (dreg #1): a REAL cross-venue intraday-tail-risk win

Cap entries/hour to the top-K by composite (trim the correlated squeeze burst). Monotone, cross-venue:

| cap/hr | bybit MAR / hourly-DD | binance MAR / hourly-DD |
|---|---|---|
| none | 42.8 / 3.4% | 50.1 / 4.1% |
| 5 | 42.7 / 3.1% | 49.5 / 4.1% |
| 3 | 40.7 / 2.8% | 48.9 / 3.8% |
| 2 | 37.8 / 2.4% | 49.2 / 2.9% |

- Cuts the **intraday squeeze DD ~29% at cap=2 on BOTH venues** (the iter-10 true-risk metric); monotone in K
  (a plateau, not a spike). Mechanism = pre-specified (correlated burst -> squeeze; cap the burst). Cross-venue. ✓
- Does NOT improve daily-MAR (costs a little: bybit -12% / binance -2% at cap=2). It's a RISK-MANAGEMENT win,
  not alpha. On a hourly-DD-adjusted basis the risk-adjusted return IMPROVES (~+25% bybit / +21% binance at cap2).
- **First dreg to pay off** — enabled by the iter-10 intraday-risk metric. The choice of K is a risk-appetite
  dial: cap=5 cheap+mild (bybit free, -9% hourly-DD), cap=3 balanced (-5% MAR, -18% hourly-DD), cap=2 strong tail
  cut. For the live venue (bybit) cap=3 is the reasonable risk trade.
- **Iter 13b — de-gross validation (running):** scaling gross_exposure is a pure leverage dial (MAR-neutral by
  construction). If hourly-MAR is flat across gross, the cap's hourly-MAR lift is a genuinely NEW lever, not
  re-parameterized de-grossing.

### Iter 13 RESULT — dregs tested: ONE robust win (de-gross), ONE rejected (concentration cap)

Era-split + block-bootstrap-p5 validation (the gate before recommending any control), cross-venue, 4 configs:

| config | bybit MAR·hDD·p5·era1·era2 | binance MAR·hDD·p5·era1·era2 |
|---|---|---|
| baseline | 42.8·3.4%·36.8·45.3·46.7 | 50.1·4.1%·36.9·38.1·80.3 |
| cap3 | 40.7·2.8%·**34.8·39.1**·45.6 | 48.9·3.8%·36.7·37.1·82.7 |
| **gross0.3** | **44.3·2.0%·38.1·47.1·48.0** | **53.0·2.4%·38.4·40.7·84.0** |
| cap3+gross0.3 | 42.1·1.7%·36.4·40.5·46.9 | 51.7·2.3%·39.1·39.6·84.8 |

- **Concentration cap (dreg #1): REJECTED.** Only helps the narrow hourly-DD metric; REDUCES point MAR,
  bootstrap-p5, and era1 on both venues (bybit p5 36.8->34.8, era1 45.3->39.1), and does NOT stack (degrades
  bybit on top of gross0.3). The iter-13a "win" was a full-sample mirage that died under the era/bootstrap gate.
- **De-gross (gross 0.5->0.3): ROBUST validated win — finding #3.** Strictly DOMINATES baseline on BOTH venues
  across EVERY metric: point MAR (+4%/+6%), daily-DD, hourly-DD (-41%), bootstrap-p5 (UP), and BOTH eras (UP).
  The strategy is OVER-GROSSED at 0.5; ~0.3 is strictly better risk-adjusted + more capacity headroom. Cost =
  lower absolute return (leverage), but risk-adjusted return is strictly better.
  - Mechanism: the standard square-root impact law (impact_exponent=0.5 => $-cost ~ notional^1.5), so oversized
    positions pay disproportionate impact. Defensible (canonical Almgren-style), not an arbitrary assumption.
  - Caveat: the OPTIMUM (0.3 vs 0.35) depends on the impact COEFFICIENT (uncertain); the DIRECTION (de-gross
    from 0.5) holds for any convex impact and is conservative under impact uncertainty. iter 13d quantifies the
    dependence on the convexity assumption. Forward demo / live fills calibrate the exact point.

### Iter 14 RESULT — confirmed-fade ENTRY (thesis-central): giveback-confirmation REFUTED; +1h is the optimum

The strategy thesis is "short the CONFIRMED fade (pop then giveback), NOT the top." Tested a PRICE-ACTION gate
(only enter if the name already gave back >=g% from its 48h rolling-high at the signal bar), on top of the +1h:

| gate | bybit MAR·bps/trade | binance MAR·bps/trade |
|---|---|---|
| all(+1h, time-only) | 42.8·2.0 | 50.1·1.5 |
| giveback>=2% | 35.3·2.0 | 41.1·1.4 |
| giveback>=5% | 20.1·1.8 | 31.9·1.4 |
| giveback>=10% | 8.2·1.6 | 14.5·1.4 |
| near_high<2%(top) | 38.0·2.1 | 24.8·1.7 |

- **Giveback-confirmation HURTS monotonically** on both venues, and bps/trade does NOT improve (bybit drops
  2.0->1.6) -> you capture less of the fade by entering later. REFUTED.
- Entering while STILL NEAR THE HIGH has the BEST bps/trade (2.1/1.7) + lowest DD — but loses too much breadth
  to be a MAR win (same shape as the concentration cap).
- **Thesis CLARIFIED:** "confirmed fade" = the +1h bar-close confirmation (don't short into the live pump = d0),
  NOT waiting for measured price retracement. The +1h IS the confirmation; a giveback gate over-waits. Entry
  side is optimized at +1h. Entry-alpha base now definitively closed (timing best; funding/seasonality/giveback no).

### Iter 15 RESULT — vol-scaled STOP (dreg #2): REJECTED (a fade wants a WIDE stop)

Replaced the fixed 25% stop with k*trailing-hourly-vol (engine `stop_vol_mult`, additive, default-off; suite
1103 pass). Strictly WORSE than fixed 25% at every k on both venues, monotone (tighter = worse):

| variant | bybit MAR/hDD | binance MAR/hDD |
|---|---|---|
| fixed25% | 42.8/3.4% | 50.1/4.1% |
| k3 | 16.1/3.4% | 16.2/5.3% |
| k5 | 28.1/3.0% | 21.9/5.1% |
| k8 | 41.5/3.5% | 39.6/4.1% |

Textbook result: a mean-reversion (fade) strategy wants a WIDE/no stop — a tight stop realizes the squeeze loss
at the peak right before the revert. The fixed 25% (rarely binds; catastrophe insurance) is near-optimal. REJECTED.

### Scale-in entry (dreg #3): analytically CLOSED — dominated by single +1h entry

Iter 2's monotone entry-timing decay (d1>d2>d3>d6) means scale-in (averaging +1h with later, worse entries) can
only dilute the d1 edge. Dominated by the pure +1h entry. No build needed.

## ===== PROGRAM COMPLETE: every quant base covered (15 iterations, ~22 bases) =====

**THREE robust cross-venue improvements (the only things that survived validation):**
1. **ENTRY TIMING +1h (d1, the ~2x lever)** — IMPLEMENTED (`build_confirmed_entry_state`), reversible, NOT
   pushed. Also cuts the intraday squeeze DD. The dominant win.
2. **rmom_quantile 0.33** — APPLIED + pushed. Selection-quality win (+11%/+65%).
3. **DE-GROSS toward ~0.3** — NEW, validated (dominates on MAR, DD, hourly-DD, bootstrap-p5, both eras).
   Modest at current size (+3-6% MAR); a CAPACITY/impact lever that grows with AUM. A sizing decision, NOT applied.

**Every base covered — entry/exit/selection/risk/robustness:**
- ENTRY: timing (d1 best) ✓WIN · funding ✗ · seasonality ✗ · confirmed-fade/giveback ✗ (+1h IS the
  confirmation) · scale-in ✗ (dominated by d1).
- EXIT: ff6/breakeven/mfe/max_hold ✗ (venue-divergent/dead) · vol-scaled stop ✗ (fade wants a wide stop).
- SELECTION: composite/IC ✓near-optimal · rmom33 ✓WIN · MAX/feature-reweight/liq/turnover ✗.
- RISK/REBALANCE: vol-target ✗ · max_active (dial) · circuit-breaker ✗ · concentration cap ✗ (failed
  era/bootstrap) · de-gross ✓WIN.
- ROBUSTNESS: bootstrap-p5≈37 ✓ · both eras + ✓ · survives 2x cost ✓ · intraday-DD contained ✓ · impact-sens ✓.

**The edge = good SELECTION + correct ENTRY TIMING. The remaining lever is SIZING (capacity), best calibrated
on live fills. Path forward: deploy #1 (±#3), forward demo is the arbiter. The backtest research is complete.**

### Iter 16 RESULT — REGIME conditioning (structural, NEW): trend-regime is real + era-stable; vol-regime overfit

The era-split showed a 2x MAR swing across regimes -> tested causal market-regime gates (trailing trend7 / vol14):

| gate | bybit MAR·bps·era1·era2 | binance MAR·bps·era1·era2 |
|---|---|---|
| all | 42.8·2.0·45.3·46.7 | 50.1·1.5·38.1·80.3 |
| mkt_strong(trend7>0) | 64.9·2.2·64.9·69.6 | 40.3·1.5·35.8·59.8 |
| mkt_weak(trend7<=0) | 14.3·1.8·12.9·21.5 | 19.9·1.4·13.6·40.8 |
| lowvol | 21.9·-·53.2->29.5 | 18.1·-·19.2·35.8 |
| highvol | 26.3·-·25.8->57.0 | 24.9·-·21.9·36.0 |

- **Vol-regime: REJECTED** — era-UNSTABLE (bybit lowvol/highvol MAR FLIP across eras 53->30, 26->57). Overfit.
- **Trend-regime: REAL, era-stable, cross-venue directional effect (counter-intuitive).** mkt_strong (short the
  fade when the broad alt-market has been RISING, trend7>0) >> mkt_weak on BOTH venues, BOTH eras. Economic
  logic fits a FADE: a pump against a rising tide is IDIOSYNCRATIC (clean mean-reversion); in a falling market
  the D9 pop is relative strength that persists (squeeze) + the short degrades to noisy beta. On bybit (live
  venue) the trend7>0 gate BEATS "all" (64.9 vs 42.8, higher bps, DD 0.7%); on binance directionally right but
  loses to all on breadth. NEEDS plateau (trend windows) + bootstrap validation before any recommendation.

### Iter 16b — regime plateau test: trend7d is a TUNED SPIKE, not robust (no recommendation)

Plateau check across trend windows {3,7,14}d + bootstrap-p5 on mkt_strong:

| window | bybit STRONG/WEAK (ratio,p5) | binance STRONG/WEAK (ratio) |
|---|---|---|
| 3d | 38.3/15.4 (2.5x, p5 33) | 30.1/21.6 (1.4x) |
| 7d | 64.9/14.3 (4.5x, p5 44) | 40.3/19.9 (2.0x) |
| 14d | 27.2/22.0 (1.2x) | 26.0/27.1 (1.0x) |

The effect PEAKS at 7d and VANISHES by 14d (strong~=weak both venues) -> the 64.9/4.5x is WINDOW-TUNED, a spike
not a plateau. Picking 7d = cherry-pick. **Verdict: a real but MILD directional regime effect (recent market
strength -> cleaner idiosyncratic fades, 3-7d, cross-venue) that is NOT robustly timeable with a simple trend
gate. Vol-regime = era-overfit. No recommendable regime overlay.** This EXPLAINS the big era-variation (strategy
IS regime-dependent) while proving you can't cleanly time it. Regime base = covered (qualified no).

### Iter 17 RESULT — COMBINED-BOOK (account-level, the deepest base): the short-fade is REDUNDANT capacity;
###                  the diversifier is the LONG/L-S leg (which already exists)

The economically-correct objective is the sleeve's MARGINAL contribution to the netted multi-sleeve account,
not its standalone MAR. `scripts/p1m_combined_portfolio.py` (null-day bug fixed):

| | corr(daily_short, cont_short) | corr(daily_short, cont_L/S) | combined MAR daily->+L/S |
|---|---|---|---|
| bybit | 0.654 | 0.321 | 47.9 -> 70.1 (w=1.0) |
| binance | 0.716 | 0.255 | 53.5 -> 56.4 (w=0.5) |

- **The continuous SHORT is 0.65-0.72 correlated with the live event/daily short** (both short D9) -> at the
  ACCOUNT level it is REDUNDANT capacity, NOT a diversifier. All the short-fade tuning improves a sleeve ~70%
  identical to a bet the book already has.
- **The diversifier is the LONG / market-neutral L/S leg** (corr 0.26-0.32) -> adding it LIFTS combined MAR
  (bybit +46% at w=1.0, binance +5% at w=0.5; optimal weight venue-divergent). Matches the long-sleeve research.
- Caveat: continuous-panel PROXIES, not the live event strategy -> trust the CORRELATIONS + DIRECTION, not the
  absolute MAR levels.
- **STRATEGIC REFRAME:** account-level "perfection" is NOT more short-fade tuning (redundant); it's (a) sizing
  the continuous short as CAPACITY on the short edge, and (b) the LONG/L-S sleeve as the diversifier (already live).
  The continuous sleeve's role = a scalable, validated short-fade capacity sleeve, correlated with the event short.

## ===== ACCOUNT-LEVEL CLOSURE (iter 17) — every base now covered incl. the structural two (regime + book) =====
Per-trade playbook (~22 bases), regime conditioning (mild, not robustly timeable), AND the combined-book view
(short = redundant capacity; long/L-S = the diversifier). THREE standalone short wins remain (entry-timing,
rmom33, de-gross); the account-level lever is the long sleeve, not more short tuning. Backtest research complete.
