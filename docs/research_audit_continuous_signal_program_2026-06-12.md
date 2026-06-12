# Continuous-strategy audit for the new signal-research program (2026-06-12)

**Label: AUDIT / research-governance document — no run, no result, no alpha claim.**
Scope: the continuous fade book as the substrate for new signal research (Wave 3+).
Sources: the deployed engine code (`continuous_demo.py`, `continuous_events.py`,
`promoted.py`), every binding receipt in `docs/preregistration/`,
`docs/research_summary.md`, STATE.md, and external literature (cited in §7).
Method: enumerate every variable, classify it by the evidence that binds it,
define the scoring standard for new variables, and adjudicate the proposed
ORDER-FLOW composite score against the record.

Bottom line up front:

1. **Almost the entire deployed stack is receipt-frozen and should be left
   alone** — not out of conservatism, but because each frozen knob carries a
   pre-registered falsifier that already killed its alternatives (§2).
2. **New signals have exactly two live attachment points**: per-event entry
   veto and the constrained E2 regime family. Size tilts at daily granularity
   are 0-for-4 with receipts; exits are closed permanently (§3).
3. **Score variables with the two-stage standard already implicit in the
   OI/P10 scouts** — formalized in §4 with the statistical rationale spelled
   out (it is the right standard; keep it).
4. **The ORDER-FLOW composite is a good instinct with one wrong default**:
   build it as an equal-weight rank composite used as an entry veto, with
   components admitted only after individual cross-venue passes — NOT as an
   IC-weighted score. IC-weighting on this sample size is fitting noise; the
   repo's own ridge-combiner (negative OOF IC) and walk-forward-allocator
   (equal-weight matches fitted OOS) receipts already demonstrated this, and
   the external literature agrees (§5).
5. Sequencing: E1 Stage 0 → P10 as registered (single feature, two-sided) →
   E2 → P12 calibration → only then a fresh composite receipt (§6).

---

## 1. The object being audited (deployed continuous book, 2026-06-12)

`continuous_ensemble_v1` (live demo default; research-stage, NOT promoted):

```text
selection   rmom q25 gate + max_ret168 D9 + liq ≥ $500k (signal-bar 1h turnover)
            + BTC 30d-uptrend gate + age floors (240d / 210d per component)
entry       confirmed-bar close + 1h delay (d1; validated ~2x MAR vs intra-hour)
components  p3   turn3_pop3  age240 TP10  w=0.30
            p4p3 turn4_pop3  age240 TP10  w=0.20
            p4p5 turn4_pop5  age240 TP10  w=0.40
            tp14 none        age210 TP14  w=0.10   (frozen receipt weights)
exits       venue-side TP (10/14%) + 24h max hold + breakeven(arm 10%)
            + failed-fade (6h/−4%/MFE<1%) + disaster stop 25% (+ 0.8 approach)
sizing      ~2% notional per name; crowd cap 2; cooldown 30m; D9-buffer hysteresis
risk engine w90 / tv0.045 / max4 / ddh−0.04 daily rebalance; NO momentum hurdle
overlays    2f BTC+ETH hedge (banked, armed, warmstart-blocked);
            sniper +8% quarter-size resting Sell (armed, Tier-2);
            circuit breaker 8 adverse covers / 24h (operator insurance);
            dynamic-exit forward shadow (paper-only)
```

Everything above maps to a receipt; the per-knob map with the binding evidence
is §2. The window 2023-04→2026-05 produced this object through a long
selection chain; the operator rescinded the window freeze 2026-06-12 for
pre-registered runs, but the statistical consequence of the chain stands: any
new pass measured on this window is an order statistic and must be shrunk;
forward demo/paper remains the only Tier-3 arbiter.

## 2. Variable inventory — what is frozen, what is open, and why

Classification rubric:

- **FROZEN** — receipt-bound. Touching it requires *falsification evidence*
  (a methodology bug or a forward-data contradiction), never "improvement"
  mined from the same window.
- **CLOSED** — the alternatives have pre-registered NULLs. Re-running needs a
  new mechanism or new data plus a fresh receipt.
- **OPERATOR** — deliberate de-risking choices, not fitted parameters. They
  are not research surface at all; changing them is a risk-preference
  decision, not an experiment.
- **OPEN** — has a live pre-registration or an armed forward-watch condition.

### 2.1 FROZEN (leave alone)

| Variable | Value | Binding evidence |
|---|---|---|
| Component weights | .30/.20/.40/.10 | walk-forward allocator falsifier: causal chooser haircut 13.8%; equal-weight matches fixed winner OOS (pooled Sharpe 2.334 vs 2.317); adaptive re-weighting actively hurts. Weights are NOT the edge — frozen, no re-estimation (`continuous-walkforward-allocator-2026-06-09`) |
| Entry triggers (turn3_pop3 / turn4_pop3 / turn4_pop5) + D9 of `max_ret168` | as deployed | winner-robustness battery 5/5 PASS (all 286 simplex vectors both-venue positive); F3: the triggers *consume* the within-arm ordering information |
| rmom gate | q25 | 0.25 re-confirmed optimal; loosening "adds correlated breadth, blows out DD"; knife-edge latency debt means NO further rmom-derived claims without an intraday design |
| Entry timing | +1h confirmed-bar delay | d1 ~doubles MAR vs d0 cross-venue (alpha-sweep 2026-06-02) |
| Liquidity gate | ≥$500k | part of the validated selection chain; capacity work (DC1) confirms thin-name dependence is structural |
| Age floors | 240d / 210d | age gate robust cross-venue (daily + continuous); the one gate family with consistent wins |
| Exits: TP10/TP14 + 24h hold | as deployed | §4-D CLOSED PERMANENTLY: P5 dynamic exit = cleanest cross-venue mirage on record (bybit ΔMAR +1.74 / binance −2.10); multi-horizon: 24h is THE cross-venue horizon; graveyard: stops, trailing, giveback, breakeven-variants, rank-decay all null |
| Risk engine | w90/tv0.045/max4/ddh−0.04, no momentum hurdle | robustness battery (tv is a dead knob — scale pins at max); risk retargets rejected; hurdle: no-momentum was the winning arm, hard-off especially bad at 2x cost. Anchor leverage max4-6, never the recent-regime-flattered max10 |
| Per-name sizing | ~2%/name inverse-vol | cov-sizer DEAD A PRIORI: median trade has 1 open peer; allocator is a no-op for ~62% of trades. Do not re-mine at current breadth |
| Crowd cap | 2 | cap 3/4/off reintroduce tails (independent-branch sweep) |
| Hedge | 2f BTC+ETH, w90/min60/cap2 | Stage-A 6/6 + Stage-B s0-s8 engine-grade; shrunk-beta and 50/50-basket estimator families closed (`continuous-hedge-2f-engine-2026-06-10`) |
| Sniper | +8% wick, 0.25 size | fixed P0 beats adaptive/fitted variants OOS (stitched deltas +1.06/+0.60); Tier-2 by Amendment 6 |

### 2.2 CLOSED families (do not re-mine; the receipts are the tombstones)

- **Continuous size tilts at daily granularity — 0 for 4**: OI tanh tilt
  (pooled ΔMAR −1.07), down-only OI de-size (pooled −0.305), participation
  cap dominance (B3 fail), TC1 atlas-gate conversion (4th receipt: "the
  rebalance rule already normalizes the book; MAR is breadth-carried").
- **Exit family** (P5 + the graveyard) — closed permanently on this window.
- **Hard derivative filters** (funding / premium / basis on the merged
  stream) — all lost MAR vs unfiltered.
- **Fitted multi-feature combiners**: ridge within-pool combiner — bybit
  pooled out-of-fold rank-IC **−0.04** (anti-predictive with stable
  coefficients), Tier-1 fail.
- **Daily squeeze timing** (WP1a: alt-RS is a daily martingale), downtrend
  sleeves/bounce products (operator-terminal), standalone cross-sectional
  books (D2/D3/WP4 — wrong drawdown class), passive-at-touch entries (P7),
  intraday taker-cost reversal harvest (IR1: signal real, economics fail by
  ~an order of magnitude), densification of the long sleeve (LR/PE1/PE2).
- **2026-06-12 diagnostics closures**: linear/monotone regime scores;
  regime-tail catastrophe trimming; per-component `max_ret168` tilts;
  unconditional all-days trading.

### 2.3 OPERATOR knobs (insurance, not alpha — out of research scope)

Disaster stop 0.25 + approach 0.8; circuit breaker w24/n8 (documented to cost
~20% bybit return in-sample as deliberate tail insurance); sleeve toggles;
demo/paper wiring; max4 leverage anchor; live cadence/WS/cache knobs (governed
by tests and parity checks, not backtests).

### 2.4 OPEN — the only live research surface

| Item | State | Gate |
|---|---|---|
| E1 capped composite size tilt | pre-registered, Stage 0 pending | Stage-0 GO/NO-GO (mid-quintile IC ≥ +0.04, ≥65% positive months, binance not opposed) then Tier-2 |
| E2 regime family V1/V2 | pre-registered, after E1 verdict | Tier-2 vs V0, funding mandatory |
| P10 `flow_support_6h` scout | pre-registered; **blocked on the taker_flow_5m build** (layer empty on this box) | two-sided \|IC\| ≥ 0.08, p<0.05, both venues, same sign, ≥60% coverage |
| P11 full-universe taker-flow layer | idle-time data build | none (data only) |
| P12 liquidation-proxy calibration | blocked ~30d (forward `allLiquidation` tape maturing, 27k events/2d) | measurement receipt only |
| Forward-watch leads (repeat-name, weekend, US-session) | armed | ≥100 forward trades/book, direction match + pooled ≥2σ |
| R4 impact calibration | blocked on fills (zero book entries since 06-09 rebuild) | execution realism, then maker-economics door |

Note on E1, stated for honesty: it is the *fifth* attempt to convert
event-level ordering information into sizing on this book. The base rate is
0/4. What makes it worth running anyway: the information source is different
(within-selection composite ordering vs OI/atlas features), the t-stats are
the strongest yet seen in this family (t≈+6-7 vs the OI scout's marginal
economics), the Stage-0 falsifier is nearly free, and the tilt is gross-
neutral by construction (the OI tilt died partly on a leverage artifact the
±5% gross guard now catches). Expected outcome remains NULL; the program is
correctly priced as a cheap lottery ticket with a clean kill switch.

## 3. Where a new signal can attach — base rates from the record

| Attachment point | Track record on this book | Verdict for new signals |
|---|---|---|
| **Per-event entry veto / gate (binary)** | BTC-uptrend gate DEMO-ELIGIBLE; age gates robust; liq gate load-bearing. (Cross-listing gate failed — venue-asymmetric.) | **The preferred form.** P10's pre-stated Stage-2 is exactly this |
| **Constrained regime family** | E2 pending; binary V0 validated on bybit as risk transform | Allowed ONLY through the E2-style pre-named family — no curve fitting (F5: response is non-monotone; 24-29 episode tails) |
| Continuous size tilt | 0/4, receipts | presumptively dead at daily granularity; E1 is the operator-authorized last word |
| Exits | closed permanently | dead absent fundamentally new data |
| Execution layer | sniper banked; passive-at-touch null | R4-gated; engineering not signal research |
| Hedge overlay | 2f banked; estimator families closed | only a structurally new factor would re-open |
| New standalone book | D2/D3/WP4/downtrend: wrong drawdown class | dead |

The convergent conclusion: **new-signal research = event-level information
scouts that, on a PASS, convert to entry vetoes (or feed the one constrained
regime family)**. This is also exactly what the charter's §2 lesson predicts:
the edge is event selection + execution; new alpha comes from new data.

## 4. The variable-scoring standard

Two stages, strictly separated, because the record proves information ≠
money (OI Stage-1 PASS → both conversions NULL; IR1 signal confirmed →
economics fail ~10x).

### Stage 1 — information scorecard (per candidate variable)

Run on the frozen winner_base component ledgers (the scouts' event loader),
outcome = ledger `net_return`. Machinery exists: `spearman` +
`partial_spearman` + per-year + lag falsifiers in
`scripts/continuous_taker_flow_scout.py` (P10 is the reference design).

1. **Primary IC**: event-level Spearman |IC| ≥ 0.08 with p < 0.05, on BOTH
   venues, SAME sign. *Rationale*: with n ≈ 850-1000 events/venue,
   SE(Spearman) ≈ 1/√(n−1) ≈ 0.033, so 0.08 ≈ 2.4σ per venue; demanding the
   same sign on a second venue (partially independent universe — ~50% shared
   names) is the closest thing to replication this program has. Declare the
   test TWO-SIDED whenever priors conflict (P10's sign-conflict note is the
   model).
2. **Stability**: per-year ICs reported; a pooled IC carried by one year
   (the OI scout's bybit 2026-carry) is a fail-flag. Where the variable
   supports it, monthly IC series → mean, t (≈ mean/std×√38 on this window),
   % positive months, ICIR. Healthy reference points from the record:
   the 5-feature composite's t=+6.9, 89% positive months is strong; the OI
   scout's flat-by-year binance arm is acceptable; anything 2026-carried
   is not.
3. **Latency/decay**: lag-1 copy keeps the sign on both venues. *The rmom
   lesson is the binding precedent*: an effect alive only at the freshest
   legal staleness (dead at +1 day) supports no deployment claim for a
   daily-cadence system. Every new variable must show its IC at the
   staleness the live path actually guarantees, plus one.
4. **Incrementality (orthogonality)**: (a) not-a-proxy: |spearman(candidate,
   trigger score)| < 0.3 per venue; (b) partial IC rank-residualized on the
   features the book already conditions on (`max_ret168`, ΔOI_6h where
   covered, rmom) keeps the primary's sign. A variable that re-discovers the
   trigger is worth zero. Do NOT pre-orthogonalize components against each
   other before combining (literature: orthogonalized combinations
   underperform — the correlation structure carries information); measure
   partial IC for the incrementality CLAIM only.
5. **Conditionality**: measure the IC *within the selection* (on the book's
   entries), not only on the universe. F3 is the precedent: `max_ret168` has
   universe-level IC that vanishes inside the trigger arms because the
   triggers consume it.
6. **Coverage/survivorship**: ≥60% event coverage per venue or the run is
   coverage-limited and not citable either way; covered-vs-uncovered outcome
   means reported (the bybit-OI survivor-only asymmetry is the precedent;
   the tick-tape flow layer exists precisely because it repairs this).
7. **Economic order-of-magnitude (pre-Stage-2 sanity)**: report the Q5−Q1
   (or tercile) spread in bps against the 45bps round-trip taker reality.
   The OI scout's +0.08 IC produced ~1bp/trade tercile spreads — real
   information, unconvertible at daily granularity. If the spread is under
   ~2x costs for the intended form, do not bother designing a Stage-2.
8. **Multiple-testing posture**: ONE primary feature, ONE primary window per
   receipt; confirmations are fixed copies (24h window, lag-1), never a
   search; a FAIL retires the family on this window. Independent external
   priors assembled BEFORE touching in-repo data (the
   `research_notes_external_priors` pattern) — this is the program's
   protection against new-dataset mining, keep it mandatory.

### Stage 2 — conversion scorecard (per use-form, separate fresh receipt)

- Form chosen from §3's live attachment points (veto/gate ≫ anything else).
- Full engine A/B, both venues, funding ON, 2x-cost arm, ±5% gross guard.
- Tier-2 bar exactly as STATE.md: positive return both venues, pooled MAR-Δ
  > +0.1, neither venue < −0.5, trade counts, fragility reported never used
  to rescue.
- Asymmetry prior from P8: removing/vetoing risk is more likely to survive
  than adding/up-sizing exposure (up-sizing crowded events adds squeeze
  variance faster than mean edge).
- A window pass is an *adoption proposal to the operator for the demo book*;
  Tier-3 stays forward-only, never loosened.

### Scoring the PORTFOLIO of research itself

One more Jane-Street habit the program already half-has — make it explicit:
before each wave, rank candidate experiments by
`P(pass) × ΔMAR-if-true × (1 − correlation with what's already banked)
÷ cost-to-run`, and let pre-registered NULLs update P(pass) for the whole
family (that is what "0/4 sizing conversions" means operationally). The
charter's EV-ranked hunting grounds did this informally; keep doing it in
writing.

## 5. The ORDER-FLOW composite — adjudication

**The proposal**: a composite ORDER FLOW score built from the positive ICs
found in research, to condition the continuous book.

### What the record + literature actually support

The instinct is half right, and the half that is right matters:

- *Composites beat single features* — in-repo: the 5-feature composite IC
  +0.099/t+6.9 vs `max_ret168`'s +0.069 with an inverting top tail (F1/F2).
  In the literature this is just IC diversification (averaging weakly
  correlated unbiased signals raises ICIR).
- *The order-flow family is the right hunting ground* — the charter's §2
  lesson (new alpha = new data), the external-priors note (taker imbalance
  is the dominant short-horizon crypto feature family), and the OI Stage-1
  PASS all point here.

### What the record forbids

1. **IC-weighting the components is fitting, and fitted weights have
   failed here twice, out-of-fold and out-of-window.** The ridge combiner —
   literally "weights estimated from in-window predictiveness" — produced
   *negative* OOF rank-IC (−0.04) with stable coefficients. The walk-forward
   allocator falsifier showed equal weights match the fitted winner OOS and
   adaptive re-weighting hurts. Externally: estimation error dominates weight
   optimization until sample sizes this program will never have (the 1/N
   literature's calibration: ~3000 months for 25 assets); IC-weighted
   composites win only in huge-N equity settings with explicit constraints,
   and even there "pure Max IR amplifies estimation error out-of-sample".
   With 2-3 components and ~38 monthly cross-sections, IC weights ≈ equal
   weights + noise. **Equal-weight rank composite, weights frozen a priori,
   is the only defensible default.**
2. **The components are not yet eligible.** Today the "positive ICs found in
   the research" for *order flow* are: ΔOI_6h (+0.08, Stage-1 PASS, bybit
   survivor-caveated) and... nothing else. `flow_support_6h` is UNMEASURED
   (P10 pending, sign genuinely unknown — the priors conflict two ways);
   the liquidation proxy is uncalibrated (P12, ~30d). The 5-feature
   composite is price/vol, not order flow, and is already spoken for by E1.
   A composite of one measured component is not a composite.
3. **The use-form must respect the conversion base rates.** A composite
   *size tilt* would be the sixth attempt at a 0-for-4 (soon 0-for-5 or
   1-for-5 after E1) family. The pre-stated P10 conversion is a per-event
   **entry veto** of the worst class — that is the form with the live track
   record.
4. **Do not bolt the composite onto P10.** The receipt's multiple-testing
   posture (one feature, one window, no alternative weightings) is what
   makes a P10 result interpretable. The composite is a separate, later
   receipt or it is mining.

### The legitimate design (pre-named here so it cannot drift later)

**`flow_veto_v1` — registered only if its components individually pass:**

- **Admission rule**: a component enters the composite only with an
  individual Stage-1 cross-venue PASS on this book (today: ΔOI_6h is in,
  pending its survivor caveat being repaired by the flow layer;
  `flow_support_6h` joins iff P10 passes; the liquidation proxy joins iff
  P12 calibrates it as honest and a Stage-1 scout passes).
- **Construction**: within-event-cohort cross-sectional ranks, equal-weight
  mean, sign-aligned so higher = better expected fade. No fitted weights, no
  tanh, no z-score re-scaling beyond ranks (rank-space is what every banked
  IC was measured in).
- **Use-form**: binary entry VETO of the pre-named worst class (bottom
  quintile of the composite, threshold fixed in the receipt before the run),
  judged at full Tier-2 + ±5% gross guard + funding + 2x cost. Explicitly
  NOT a size tilt and NOT a new selection signal.
- **One shot.** A FAIL retires event-level flow conditioning of this book on
  this window (forward data may revisit).
- **Honesty clauses inherited**: window-spent shrinkage applies to any pass;
  per-year ICs reported; the composite's partial IC vs the trigger score and
  vs each component reported (the composite must beat its own best component
  on incremental grounds, else ship the single veto).

### What would change this verdict

If the program ever has (a) ≥3 individually-passed flow components, (b)
≥12-18 months of *forward* event cross-sections to estimate weights on data
the window never touched, and (c) evidence the equal-weight composite's
Stage-2 margin is materially limited by component quality dispersion — then
an IC-weighted variant (shrunk 50% toward equal, weights from forward data
only) becomes a registrable question. None of those conditions exist today.

## 6. Recommended sequencing (one change, one verdict — unchanged discipline)

1. **E1 Stage 0** (cheap, already registered; GO/NO-GO falsifier first).
   Its result is also the program's live read on whether within-selection
   ordering converts to economics at all.
2. **Finish the `taker_flow_5m` event-anchored build** (the layer is empty
   on this box; P10 is blocked until it lands), then **run P10 exactly as
   registered**. Resist every temptation to widen it.
3. **E2** after E1's verdict (the plan's own sequencing).
4. **P12** when the forward liquidation tape hits ~30d (≈ 2026-07-10).
5. **Then and only then**: if ≥2 flow components have individual passes,
   register `flow_veto_v1` per §5. If only one passes, ship the single-
   component veto Stage-2 instead and skip the composite entirely.
6. Throughout: the forward clocks (demo fills, R4 calibration, forward-watch
   leads at ≥100 trades) are the highest-value evidence per unit of
   researcher degrees-of-freedom — they spend none.

## 7. External sources consulted (2026-06-12)

- FactSet, [A Practical Approach to Weighting Signals](https://insight.factset.com/a-practical-approach-to-weighting-signals)
  — Max-IR vs equal-weight vs risk parity; "pure Max IR tends to amplify
  estimation error out-of-sample unless explicit constraints are imposed."
- DeMiguel, Garlappi & Uppal (2009), [Optimal Versus Naive Diversification:
  How Inefficient is the 1/N Portfolio Strategy?](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1376199)
  — estimation windows of ~3000+ months needed to beat 1/N at N=25.
- Bailey & López de Prado, [The Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
  ([overview](https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio)) — the
  selection-bias/multiple-testing haircut formalism behind this repo's
  "window pass = order statistic, shrink it" rule.
- ML4Trading, [Reading the Information Coefficient: stability, ICIR, horizon
  decay](https://ml4trading.io/primer/reading-the-information-coefficient-stability-icir-and-horizon-decay/)
  — the IC/ICIR/decay conventions §4 formalizes.
- arXiv 2105.10306, [Turnover-Adjusted Information Ratio](https://arxiv.org/pdf/2105.10306)
  — costs/turnover belong inside the signal score, not after it.
- arXiv 2602.00776, [Explainable Patterns in Cryptocurrency
  Microstructure](https://arxiv.org/html/2602.00776v1) — taker-flow
  imbalance dominance + extreme-flow concavity (already in the repo's
  priors note; re-confirmed current).
- Repo-internal: every receipt and summary section cited inline above;
  `docs/research_notes_external_priors_2026-06-12.md` for the
  taker-flow sign-conflict priors.
