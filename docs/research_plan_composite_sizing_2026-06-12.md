# Composite-sizing + regime-response program (2026-06-12, operator-directed)

Operator-directed program, 2026-06-12. Two experiments on the continuous
book: E1 converts the demonstrated within-selection IC into sizing; E2 tests a
small regime-response family against the current BTC gate. Window backtests
decide at the Tier-2 bar (STATE.md "Decision Rules"); forward demo/paper
remains the path to Tier-3 as usual.

Receipts: [2026-06-12-e1-composite-size-tilt.md](preregistration/2026-06-12-e1-composite-size-tilt.md),
[2026-06-12-e2-regime-response-family.md](preregistration/2026-06-12-e2-regime-response-family.md).

---

## 1. The exploratory findings this plan is built on (2026-06-12)

Run label: **EXPLORATORY** (raw 24h close-to-close forward returns; no stops/
TPs/costs/funding; fill at close(ts+1h); ~20 cells examined across the session,
so magnitudes are optimistic upper bounds). Populations are the engine's own
`_fresh_entries` on the cached PIT-causal panels (rmom q25, liq ≥ $500k),
bybit_full_pit, 2023-04→2026-05. Not citable as alpha; cited here only as the
motivation for the registered experiments and the closures in §5.

**F1 — within-selection IC exists.** Among fresh-D9 entry candidates, entry-time
rank predicts the 24h fade outcome. No-trigger population, monthly Spearman IC:
deployed single feature (`max_ret168`) mean +0.069, t=+5.2, 79% positive months;
the legacy 5-feature composite (`rv_168h, vov, dist_low, xsret7, xsret3`)
mean +0.099, t=+6.9, 89% positive months, Q5−Q1 +59bps.

**F2 — it survives the live regime, composite > single feature.** Uptrend-only:
5-feature composite mean IC +0.101, t=+5.7, 83% hit, mid-quintile (Q2-Q4) IC
+0.056 (improves in-regime); single feature holds rank IC but its top tail
inverts (Q5−Q1 flips to −19bps).

**F3 — no residual ordering inside the trigger arms.** Conditioned on the
ensemble triggers + age floors + uptrend, `max_ret168` rank loses significance
(t≈1.0-1.4, ~53% hit months, mid-quintile IC negative): the triggers consume
the same information. tp14 (no-trigger arm) keeps t=+2.6 but its extreme top
quintile inverts (−140bps). **Conclusion: tilt on the 5-feature composite
across the book, never on `max_ret168` within trigger arms; caps mandatory.**

**F4 — secondary-score probe (caveated).** Joining the stale 5-feature panel
onto live entries (only ~30% coverage — older rmom vintage ⇒ selection bias):
p3/p4p3 arms show monthly IC +0.11-0.13, 74% hit, mid-IC +0.06 where the own
feature had none. Encouraging but non-representative; Stage 0 of E1 exists to
re-measure this without the coverage confound.

**F5 — the BTC-trend response is NON-monotone and catastrophe is not in the
tails.** Mean 24h fade by prior-30d BTC return bucket (no-trigger entries):
`<-20%` +198bps (24 clustered episodes), `-20..-10%` +34, `-10..0%` +73,
`0..+10%` +47, `+10..+20%` +120, `>+20%` **−136bps** (29 episodes — currently
TRADED by the live uptrend gate). Worst-day and p5-day basket losses are
roughly uniform across buckets (−7% to −34% pre-stop): the trend value
localizes the MEAN, not the disasters. Disaster control stays with the
per-name stop + position cap. Funding is unmodeled and pushes against both
extremes (shorts collect funding in euphoria, pay in crashes) — the reason the
REGISTERED E2 run must model funding before any conclusion. 168 sign episodes, median 3 days;
tail buckets rest on 24-29 clustered episodes ⇒ enough to test a SMALL
pre-named family, nowhere near enough to fit a curve.

## 2. Ground rules

- Each stage runs once against the rule written in its receipt; both venues;
  2x-cost arm at Tier-2; fragility diagnostics reported, not used to rescue.
- A win here means an operator-gated change to the DEMO book's profile;
  real money stays behind the forward demo/paper Tier-3 gate as always.
- E2 stays a three-variant comparison — no extra variants or threshold tuning
  inside this program; a new idea gets a new pre-registration.

## 3. E1 — capped composite size tilt (within the existing gate)

*Hypothesis:* reallocating notional across already-selected entries by the
5-feature composite percentile (capped) converts the demonstrated
within-selection IC into MAR without new trades, new costs, or any change to
selection, exits, or the regime gate.

*Tilt spec:* per-entry size multiplier `m = clip(0.5 + p, 0.5, 1.5)`
where `p` = percentile of the 5-feature composite within the entry's signal-ts
cross-section (the panel's own rank normalization). `E[m] ≈ 1` ⇒ gross-neutral
in expectation. Applied multiplicatively on top of the validated config's
existing sizing; everything else byte-identical.

- **Stage 0 — coverage-clean confirmation (GO/NO-GO).** Rebuild the 5-feature
  panel against the CURRENT rmom vintage (both venues), re-run the
  within-selection diagnostic once. **GO iff** bybit uptrend no-trigger
  mid-quintile monthly IC mean ≥ +0.04 with ≥65% positive months, AND binance
  not sign-opposed. Anything less: program ends, null receipt filed.
- **Stage 1 — DECISIVE window run (Tier-2 bar).** Full engine A/B (flat vs
  tilted), both venues, funding on where the root carries it, identical
  entries/exits, plus the 2x-cost stress arm. **Win iff** (Tier-2): positive
  total return both venues, pooled MAR-Δ > +0.1 vs flat, neither venue
  MAR-Δ < −0.5, survives 2x cost; fragility diagnostics reported. Anything
  less: rejected, null receipt filed.
- **Stage 2 — adoption + forward arbiter.** A Stage-1 win is proposed to the
  operator as a demo-profile change (demo + paper twin together). Forward
  demo/paper then accrues the live verdict as usual — Tier-3 consideration
  stays forward-only and is unaffected by this plan. Optional belt-and-braces:
  the dynexit-shadow-pattern tilted-vs-flat bookkeeping may run alongside for
  attribution, but it is no longer the gating evidence.

## 4. E2 — regime-response family

*Hypothesis:* the live binary uptrend gate mis-handles both tails of the BTC
30d-trend distribution — it trades the mean-negative euphoria bucket
(>+20%, F5) and discards non-uptrend days whose top-ranked entries retain
positive ordering. A bounded, pre-named regime response may improve MAR.

*Variants (fixed up front):*
- **V0** — baseline: current gate (trend > 0 ⇒ on).
- **V1** — euphoria cap: on iff `0 < trend ≤ +0.20`.
- **V2** — soft 3-state: `trend > +0.20` ⇒ off; `0 < trend ≤ +0.20` ⇒ full
  size; `trend ≤ 0` ⇒ quarter-size, top-composite-quintile entries only.

The `+0.20` threshold comes from the exploratory bucket edge (disclosed); it is
fixed a priori and will not be tuned.

- **Stage 1 — DECISIVE window run (Tier-2 bar vs V0).** Full engine, all
  three variants, both venues, **funding mandatory** (it pushes against both
  tails; a fundingless regime result is meaningless), plus the 2x-cost arm.
  **A variant wins iff** (Tier-2 vs V0): positive total return both venues,
  pooled MAR-Δ > +0.1, neither venue MAR-Δ < −0.5, survives 2x cost. The
  episode counts (~29 euphoria / 24 deep-crash, clustered) are REPORTED as
  the fragility diagnostic — disclosed, not used to rescue. If
  both variants miss the bar: V0 stands, null receipt filed.
- **Stage 2 — adoption + forward arbiter.** The winning variant (if any) is
  proposed to the operator as a demo-profile change; forward demo/paper
  accrues the live verdict; Tier-3 stays forward-only. If both V1 and V2 pass
  Stage 1, the higher pooled MAR-Δ is proposed (no second look, no blending).

## 5. Closed by the 2026-06-12 diagnostics — do NOT re-mine

- **Linear/monotone BTC-trend score (the "-5..+5" sizing curve):** falsified —
  the response is non-monotone at both ends (F5).
- **"Cut regime tails to avoid catastrophe":** falsified — catastrophe days are
  uniform across trend buckets; tails differ in mean, not disaster density (F5).
- **Per-component sizing tilts on `max_ret168`:** no residual ordering after
  trigger conditioning; mid-quintile ICs negative (F3).
- **Unconditional all-days trading (drop the gate, no replacement):** no
  monthly-consistent non-uptrend edge (5-feature population: +10bps/mo,
  t=0.35, 45% hit), squeeze-tail and funding unmodeled. Only the constrained
  E2 family may revisit non-uptrend exposure.

## 6. Sequencing

1. E1 Stage 0 → Stage 1 → (pass) Stage 2 adoption proposal.
2. E2 Stage 1 starts after E1's Stage-1 verdict — one change, one verdict.
3. A NULL at any stage is a first-class result: receipt verdict +
   research_summary entry, then stop.
