# Composite-sizing + regime-response program (2026-06-12, operator-directed)

**Authority:** operator instruction 2026-06-12 ("make a research plan and
incorporate the findings"; same day: "remove that [freeze] line from state.md
and go with the original plan, this is not even close to being spent"). The
2026-06-09 window freeze is RESCINDED (STATE.md "Current Research Direction"):
**pre-registered window backtests are decisive evidence at the Tier-1/Tier-2
bars; the forward demo/paper arbiter remains the only path to Tier-3 and is
never loosened.** This program does NOT displace the alpha-hunt charter queue
(P1 liquidation-proxy remains the highest-EV new-alpha item); it runs
alongside it and unparks charter §4-G/P6 (regime model).

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

## 2. Binding constraints

1. **Pre-registration discipline replaces the (rescinded) freeze.** Each stage
   below runs ONCE against its frozen a-priori rule; both venues always;
   2x-cost survival required at Tier-2; fragility diagnostics reported, never
   used to rescue a weak cell. Tier-3 (real money) remains forward-demo-only
   and is never loosened.
2. **Prior null in this class.** The charter banked "rising-OI pops fade
   better — **no sizing conversion survived**" and an OI-tilt null. E1 is the
   same conversion CLASS (information → sizing) with a different, demonstrated
   carrier (within-selection mid-quintile IC on the exact entry population,
   F1-F4). The prior says: expect failure, keep Stage 1 cheap and one-shot,
   and weigh the Tier-2 verdict accordingly; forward demo still accrues the
   live evidence after any adoption.
3. **Both venues** for any window run; binance funding gaps disclosed
   (funding-missing label where applicable).
4. **rmom latency debt stands**: nothing in this program creates a continuous
   promotion case while that debt is open. "Adoption" here means changing the
   DEMO book's profile (operator-gated), nothing more.
5. **Pre-registration is binding** (decision rules below are frozen; a wrong
   prediction is rejected, not re-purposed).
6. **No curve fitting on regime variables.** Three pre-named variants in E2,
   thresholds fixed a priori; episodes (not trades) are the effective sample.

## 3. E1 — capped composite size tilt (within the existing gate)

*Hypothesis:* reallocating notional across already-selected entries by the
5-feature composite percentile (capped) converts the demonstrated
within-selection IC into MAR without new trades, new costs, or any change to
selection, exits, or the regime gate.

*Tilt spec (frozen):* per-entry size multiplier `m = clip(0.5 + p, 0.5, 1.5)`
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

## 4. E2 — regime-response family (unparks §4-G, constrained)

*Hypothesis:* the live binary uptrend gate mis-handles both tails of the BTC
30d-trend distribution — it trades the mean-negative euphoria bucket
(>+20%, F5) and discards non-uptrend days whose top-ranked entries retain
positive ordering. A bounded, pre-named regime response may improve MAR.

*Variants (frozen; no fitting, no additional thresholds ever):*
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
  the fragility diagnostic — disclosed, not used to rescue or to veto. If
  both variants miss the bar: V0 stands, null receipt closes §4-G again.
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

## 6. Sequencing and discipline

1. E1 Stage 0 → Stage 1 → (pass) Stage 2 shadow deploys.
2. E2 Stage 1 starts only after E1's Stage 1 verdict is filed — one change,
   one verdict, never bundled.
3. Any NULL at any stage is a first-class deliverable: receipt verdict +
   research_summary entry + this plan's section updated, then stop.
4. Multiple-looks ledger: the ~20 exploratory cells of 2026-06-12 are spent;
   each registered stage below runs ONCE against its frozen rule.
