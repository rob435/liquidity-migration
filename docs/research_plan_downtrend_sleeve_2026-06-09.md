# Research plan: downtrend sleeve + execution refinement program (2026-06-09)

**Mandate:** operator full authority — (1) a SEPARATE strategy for BTC-30d-downtrend
days (the current book is off then; capital idles), new logic from the ground up if
needed; (2) creative refinement of the combined system, explicitly including staged
"sniper" entries (enter where a stop would have fired); target: combined MAR +30% vs
the current deployable (hedged uptrend core @ max4: bybit 5.52 / binance 5.64, pooled
~5.58 → target pooled ≈ 7.25). **Guardrails unchanged:** pre-register every full-PIT
run; both venues; Tier-2 ceiling in-sample; failure is a first-class outcome — the
target is NOT a license to overfit; demo/paper only; no push without operator.

**Binding priors (do NOT re-mine):** raw downtrend pump-fading is weak (demoted
sliver); funding/vol/lottery/long-lowvol/trend factor shorts not net-tradeable
(alpha-hunt 2026-06-03); cross-sectional momentum null at daily horizon (Round-3);
trailing alt-RS unforecastable daily (WP1a). New leads must differ from these or
condition them on the regime in a way not previously tested.

## Work items

- **D1 — regime-conditional opportunity map (FIRST, cheap, pre-registered).** On the
  verified daily panel, both venues: which causal cross-sectional signals predict
  next-day EXCESS returns specifically on BTC-30d-DOWN days? Signals: ret_1d, ret_7d,
  ret_30d, rv_7d, turnover_delta_7d (primitives from closes/turnover). Outcome: day-d
  return minus EW-alt-market day-d return (and raw). Decile spreads + Spearman IC per
  regime per venue; block-bootstrap p. Lead bar (pre-stated): down-regime |IC| ≥ 0.03,
  p < 0.05, SAME sign both venues, economically interpretable. Output = a map, not a
  strategy claim (Tier-1).
- **D2 — build the best D1 lead through the continuous engine** (downtrend-gated
  config; both venues; funding-realistic — downtrend funding is often negative so
  SHORT carry is a real cost there). Pre-registered separately with the Tier-2 bar +
  the combined-book test (uptrend core + hedge + new sleeve at the rebalance layer).
- **S1 — staged/sniper entries on the PROVEN uptrend book.** Mechanism: the fade's
  stops sold squeeze wicks (why stops were removed); the mirror trade is ADDING into
  the wick. Test on the existing trade ledgers with conservative intrabar fill rules
  (limit fills only when the NEXT bars' high strictly exceeds the level; fill AT the
  level): split entries (e.g. 50% at trigger fill, 50% limit at +x%), x-grid
  pre-registered, both venues, TP/hold accounting recomputed from the blended entry.
  Bar: pooled MAR delta > +0.1, both venues positive, robust to x-perturbation and 2x
  cost, no Tier-1 trade-count violation. If Stage-A passes → engine-grade
  implementation (intrabar limit fills) before any claim enters the headline.
- **D3/S2 — combined book.** Whatever survives D2/S1 gets composed (uptrend+hedge +
  downtrend sleeve + staged entries) at the rebalance layer; the +30% MAR target is
  evaluated THERE, per venue and pooled, with the full stress battery (2x cost,
  sub-periods, leave-out, perturbation).

## Status log

- 2026-06-09: program opened. D1 pre-registered + running.
- 2026-06-09: D1 DONE — lead found: regime-conditional cross-sectional REVERSAL
  (down-regime D10−D1 −51/−24 bps on ret_7d, both venues, p~0; up-regime tails flip).
  Receipt `downtrend-opportunity-map-2026-06-09.md`.
- 2026-06-09: D2 Stage-A DONE — **FAIL** (binance 0/8 cells positive; funding −28..−43%
  + costs eat the real gross long-leg alpha; bybit-only +78% is the single-venue
  mirage). No off-menu rescues. Receipt `downtrend-reversal-ls-2026-06-09.md`.
  Honest answer so far: downtrend capital = hedge leg + cash, not a forced sleeve.
  → S1 (sniper/staged entries) is now the critical path for the +30% MAR target.
- 2026-06-09: S1 arc COMPLETE through 4 pre-registered amendments. Substitutive 50/50
  staging FAILS (forgone base size on the best trades) but the declared diagnostic
  found the real construction: ADDITIVE quarter-size snipe at the +8% wick. Bar-accurate
  result (own-TP, pro-rata funding, strict fills): pooled dMAR +0.72 at 1x
  (5.58 -> 6.30, +13%, both venues, DD-safe, per-fill alpha +2-3%) but the 2x-cost leg
  fails on binance (+0.18; pooled +0.30-0.375 < +0.5) incl. under maker economics.
  **Tier-1 lead, NOT banked.** Receipt `sniper-staged-entries-2026-06-09.md`.
- GOAL ASSESSMENT: +30% pooled MAR (target 7.25) NOT honestly reachable in-sample
  today: D2 dead, S1 lands +13% and misses one robustness leg. Honest remaining paths:
  (1) engine-grade snipe with maker/queue realism CALIBRATED from demo fills (R4 pull,
  operator), (2) WP4 residual-momentum standalone (operator-gated engine build, the
  documented big-edge path), (3) forward demo evidence. No further in-sample mining.
- 2026-06-09: D3 bounce-long (the attribution-driven revisit through D2's declared
  door): standalone bar PASSES on 2 cells (funding-receive thesis confirmed; bybit
  Sh 2.03 / binance 0.81) but the COMBINED bar fails catastrophically (sleeve DD
  -30..-44% vs the book's -5% budget; binance combined MAR 5.64 -> 1.5). Pre-registered
  close-out clause applied: **downtrend question CLOSED — hedge + cash is final.**
  Receipt `downtrend-bounce-long-2026-06-09.md`. PROGRAM TERMINAL STATE: pooled MAR
  banked 5.58; sniper Tier-1 +13% unbanked (2x-cost leg); +30% requires operator
  unblocks (R4 fill calibration, WP4 greenlight, forward evidence) — no honest
  in-sample path remains.
- 2026-06-09: WP4 Stage-A (rmom standalone L/S, run under the full-authority grant as
  the research greenlight): **FAIL both bars** (best Sharpe 1.09/0.88 < 1.0 both-venue
  bar; all combined deltas negative; -18..-37% DD risk-class mismatch; return is
  funding carry minus costs). Receipt `wp4-rmom-standalone-ls-2026-06-09.md`.
  **PROGRAM LESSON (3 independent confirmations: D2, D3, WP4): the edge is EVENT
  SELECTION + EXECUTION; daily cross-sectional books fail on DD-class + costs at our
  scale. New alpha needs new DATA (orderbook/L2, ticks, off-exchange), not new
  combinations of the daily/hourly data already mined.** Goal terminal state stands:
  banked 5.58, sniper Tier-1 6.30 unbanked, +30% operator-keyed.
