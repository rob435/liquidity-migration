# Research Program State

**Last updated:** 2026-06-14

Read this first for live state and binding decision rules. Research conclusions
live in [docs/research_summary.md](docs/research_summary.md).

## First Read

1. `STATE.md` - what is running, what is open, and what rules bind us.
2. `docs/research_summary.md` - current research decisions, failure ledger, and
   revisit queue.

Git history is the archive. Do not keep closed one-off receipts in the hot path.

## Current Status

Liquidity-migration is research-stage. **Nothing is approved for real money**
(`REAL_MONEY=false`; demo/paper only). The daily SHORT sleeve was erased from
the system on 2026-06-11 by operator order; only continuous fade and long v11a
remain.

The VPS runs the continuous demo system. LONG is promoted-in-code for demo/paper
only but toggled off in `deploy/sleeves.env`. Continuous is live demo evidence,
not promoted and not paper-ready.

Several 2026-06-12 audit rounds found and fixed live execution bugs
(component-weight sizing, recovery/adoption identity, ws_risk realized PnL,
telegram visibility, deploy pinning, liveness checks, and ledger bucketing).
Details are in git history; the current behavior below is what matters.

## What's Running / Wired

- **Continuous demo book:** live default is `continuous_ensemble_v1`
  (`winner_base`: p3 .30 / p4p3 .20 / p4p5 .40 / tp14 .10,
  w90/tv0.045/max4/ddh-0.04, no momentum hurdle, rmom q25,
  BTC-uptrend gate). Demo fills are execution evidence only.
- **2f BTC+ETH hedge:** wired and armed. Warmstart CSVs were regenerated on
  2026-06-13; after a long flat spell, the first risk-increasing leg may still
  block on calendar-age staleness and page unless the operator requests
  ledger-aware staleness. Flat/no-action runs are healthy; failed/blocked
  armed runs page.
- **BTC-vol regime-hedge overlay (W5 Stage 8c, LIVE 2026-06-15):** the deployed
  hedge is now modulated by a causal, mean-1 BTC-volatility regime intensity
  `1 + 0.5·(2·pct − 1)` (trailing-30d BTC vol → trailing-250 percentile; bounded
  [0.5, 1.5]) — hedge MORE in turbulence, LESS in calm. Scales BOTH 2f legs.
  Single source: `liquidity_migration/continuous_regime.py`, read by the live
  hedge manager, the forward orchestrator, AND the deployed-equity report (one
  object). It is in `FROZEN_FORWARD_CONFIG['hedge']['regime']`, so it is part of
  `frozen_config_hash` — turning it on **voided the prior 2f forward ledger**
  (new hash `0668eb88c0d6…`). Operator action required on the VPS: archive the old
  2f state dir and start a fresh clock (receipt
  `docs/preregistration/2026-06-15-forward-btcvol-regime-hedge.md`). Research-stage
  forward-watch — evaluate as squeeze protection, NOT a promotion; Tier-3 unchanged.
- **Sniper:** wired and armed in demo (`CONTINUOUS_SNIPER=1`; code default off).
  No placements yet because the base book has had zero entries since the
  2026-06-09 rebuild. W4 Stage 2 (2026-06-13) rechecked the fixed live form
  historically and supports retaining forward watch only; no forward-fill or
  promotion claim exists.
- **Dynamic exit:** no-order forward paper shadow only. The in-sample result was
  a cross-venue null; the shadow is the only possible revival path.
- **Shared kline data plane:** paper shadow follows the demo root's flushed
  kline snapshot read-only (`KLINES_FOLLOW_ROOT`).
- **LONG:** `div` + volup125 + weekend 1.5x tilt, toggled off, awaiting operator
  leverage/capital decision.
- **VPS:** Hetzner demo host. A push to the deployment branch can auto-deploy;
  do not push without operator confirmation and the pre-push gate.

## Open Operator Decisions

1. ~~Hedge warmstarts~~ RESOLVED-AS-SCOPED 2026-06-13: producer built
   (`scripts/regenerate_hedge_warmstart.py`, semantics validated vs the
   shipped CSVs to ~1e-4) and CSVs regenerated at 200-day windows. Finding:
   the "staleness" is the FLAT BOOK, not a missing refresh — the book has no
   ledger days since 2026-05-23 (gate closed), so no fresher beta input can
   exist. Cadence = run the producer after each data-root refresh once the
   book trades again. REMAINING OPERATOR CHOICE: on the first post-flat
   entries the armed hedge will still block its first Buy leg + page
   (warmstart calendar-age > 3d by construction after any long flat spell);
   either accept that one page per regime reopen (status quo, conservative)
   or direct a change to ledger-aware staleness (small tested patch on
   request).
2. ~~Depth collector~~ DONE 2026-06-13: enabled on the VPS
   (`liquidity-migration-depth-collector` active; 581 symbols on first
   cycle).
3. Finish Binance FAPI ancillary June top-ups from the VPS or another permitted
   host; the dev box is region/network blocked.
4. Decide LONG leverage/capital. The sleeve is off until then.
5. PE2 long provisional-entry OOS re-judgment is armed only after both full-PIT
   roots extend at least 60 days past 2026-05-28 and have enough trades.
6. ~~Forward replay orchestrator~~ DONE 2026-06-13:
   `scripts/continuous_forward_replay_orchestrator.py` (spec sequence exactly;
   first accrual initialized 695/663 ledger days to the data ends; forward
   window opens at days ≥ 2026-06-10 as data extends). Run it at every
   data-root refresh; overlap drift = hard alarm.
7. Binance forward liquidation capture needs a permitted-region host. The current
   host idles harmlessly with zero Binance data.

## Current Research Direction

The full window is open for pre-registered research again, but the methodology
bar did not change: both venues, full PIT, causal features, cost/funding, and
pre-stated decision rules.

**Operator guidance 2026-06-15 (W5 acceptance bar):** willing to accept **any
improvement as long as it is robust and the book still takes trades** — i.e. a
robust sub-+0.1-MAR edge is acceptable for demo/paper adoption + forward-watch,
not only one clearing the strict +0.1 Tier-2 gate. "Robust" still means the full
discipline: both venues, beats the negative control, survives cost stress, stable
across chronological thirds / forms, not carried by one venue or bucket, and
"takes trades" (no degenerate filter to ~zero breadth). The strict **Tier-3
real-money gate is unchanged** (this loosening is for research adoption / forward
watch, never real money). Directive: **keep iterating, do not stop** — take as
long as needed, never give up. This reopens the Stage 8 regime-hedge (+0.078 both
venues, ~+0.05 form-stable) as a live candidate to firm into a robust form.

Active programs:

- **Wave 4 owner-erased 2026-06-13.** By explicit owner override, the local
  W4 plan, W4 receipts, W4 scripts, and W4 local artifacts were removed from
  the active workspace. Do not cite deleted W4 materials as active evidence.
  Replacement work should be a serious staged program: dated preregistration,
  both venues, full artifacts, effect sizes, fragility, and explicit decision
  gates. Important feature families are not closed by one small script; each
  mechanism/stage is judged on its own registered evidence.
- **W4 replacement program started 2026-06-13.** Program receipt:
  `docs/preregistration/2026-06-13-w4-continuous-program.md`. Stage 0 found
  both roots present but stale for current forward claims (`bybit` data end
  2026-06-02, `binance` data end 2026-04-30, forward replay `forward_days=0`);
  Stage 1 therefore used the amended common full-PIT window ending
  2026-05-01 exclusive. Stage 1 rejected the exact registered 25% capped
  disaster stop + failed-fade/breakeven overlay; uncapped stop fills flipped
  Binance negative. Stage 2 supported the fixed +8% quarter-size sniper add-on
  historically for forward watch only (R1 pooled MAR delta +0.14) but Binance
  bootstrap MAR was weak and live fills remain zero. Stage 3 nominated
  `pre_6h_return`, `pre_24h_return`, and `pre_24h_realized_vol` only for a
  future neutralized Stage 3b receipt; the 97 bps symbol-hash negative-control
  spread is a confounding warning, not a deploy signal. These do not close the
  broader feature families. Later W4 stages require their own dated receipt
  before touching full-PIT roots.
- **W5 continuous signal alpha program.** Plan folder:
  `docs/research_plans/w5_continuous_signal_alpha/`. Score-based entry priority,
  entry/exit/sniper/sizing alpha, neutralized path-shape (Stage 7),
  regime-response vs the binary gate (Stage 8), interaction + forward gates.
  - **Stage 0 PASS 2026-06-14** (receipt
    `docs/preregistration/2026-06-14-w5-continuous-stage0-candidate-tape.md`).
    Built a per-cycle candidate tape (selected + rejected-but-eligible, with the
    exact engine reason) emitted from the same decision code as the live engine
    (additive `candidate_sink` in `continuous_events`, default off → 107 tests
    unchanged). Both venues, window `2023-04-01 <= signal_ts < 2026-05-01`:
    bybit 15362 candidates / 3223 selected, binance 16794 / 2966. PIT pass,
    selected↔ledger exact, month reconcile, and W4-control overlap exact on all
    8 cells; ensemble-hedged control rebuilt (bybit ret 0.714 / MAR 4.40,
    binance 0.675 / MAR 5.53). No alpha claim — it is the reconstructability
    gate. Artifacts `~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14/`.
    Stage-0 code is uncommitted pending operator approval.
  - **Stage 1 NULL (structural) 2026-06-14** (receipt
    `docs/preregistration/2026-06-14-w5-continuous-stage1-score-entry.md`).
    Same-breadth score-as-entry-priority (A1 composite, A5 symbol-hash neg
    control) vs A0 control via an additive within-`signal_ts` `entry_order` knob
    (fcfs reproduces the control byte-for-byte; `equity_allclose_1e-9` both
    venues). Result: **0 replacements** across 3y × 4 components × 2 venues —
    A1/A5 ledgers are byte-identical to A0. The per-component crowding gate
    (max_fresh=2) + max_active=25 leave no within-cycle contention to reorder, so
    entry priority is a mechanical no-op for the frozen control. Banked as a
    clean kill; the score-as-information lever moves to Stage 7 (path-shape) and
    Stage 5 (score-weighted sizing at constant breadth). A breadth-changing
    score-conditioned crowding admission is a separate Stage 8 idea, not Stage 1.
  - **Stage 7 NULL-as-registered (metric corrected) 2026-06-14** (receipt
    `docs/preregistration/2026-06-14-w5-continuous-stage7-path-shape-neutralized.md`).
    The registered tercile-spread gate is noise-dominated on this heavy-tailed
    per-notional cross-section (per-symbol-random spread null SD 128/175 bps), so
    it cannot decide; the robust rank-IC was clean (path-shape 0.22/0.21 vs
    symbol-hash control 0.13/0.09). Banked NULL on the registered gate (not
    overturned post-hoc); no admissibility claimed. Methodology lesson: on this
    data use rank-IC + permutation null, NOT tercile spread (a random per-trade
    score yields ±170 bps spreads).
  - **Stage 7b ADMISSIBLE 2026-06-14** (receipt
    `docs/preregistration/2026-06-14-w5-continuous-stage7b-within-symbol-pathshape.md`).
    Within-symbol fixed-effects partial rank-IC over composite (1000-perm null;
    degenerate symbol-hash control on both venues). After removing ALL symbol mix
    AND the composite, causal path-shape still predicts per-notional return on both
    venues: `pre_24h_return` IC 0.110/0.105 (p=0.001 both), `Q_combined` 0.115/0.114
    (p=0.001 both), `pre_24h_realized_vol` 0.097/0.066. Resolves the W4 symbol-mix
    confound — genuine within-symbol timing, not symbol selection. `exploratory`;
    admissibility ≠ MAR. (A contamination bug — global composite residualization
    inflating a constant-within-symbol control — was caught by the control's failed
    degeneracy check and fixed to the within-symbol partial; the result held under
    both forms.)
  - **Stage 5 Z2 path-shape sizing NULL 2026-06-14** (receipt
    `docs/preregistration/2026-06-14-w5-continuous-stage5-pathshape-sizing.md`).
    Additive engine hook `size_mult_lookup` (default None → byte-identical; 329
    continuous tests pass); sizes the SAME trades by the causal within-symbol
    path-shape residual, gross-neutral (prior-month normalizer). Z0 reproduces the
    Stage 0 ensemble exactly. Result: Z2 raises return both venues but worsens
    drawdown more (bybit MAR 4.748→3.993, pooled ΔMAR −0.33), and is **beaten by the
    Z6 symbol-identity negative control** (+0.337) — the only positive sizing effect
    is symbol-level dispersion, not path-shape; binance gross also crept out of band
    (1.064). **Path-shape lever now exhausted**: real (Stage 7b) but not harvestable
    via entry *priority* (Stage 1 NULL) or *sizing* (this NULL). Banked clean kill;
    admissibility ≠ MAR.
  - **Stage 8 regime-hedge (R4) NULL-but-promising 2026-06-14** (receipt
    `docs/preregistration/2026-06-14-w5-continuous-stage8-regime-hedge.md`).
    Mechanistically distinct from the E2-closed V1/V2 entry-gate family: keeps V0
    entries identical, modulates only the BTC hedge intensity by a causal, mean-1,
    drift-robust BTC-vol percentile regime (hedge more in turbulence, less in calm).
    Additive engine hook `hedge_intensity` in `continuous_rebalance` (default None →
    byte-identical; 343 tests pass; V0 reproduces the Stage 0 ensemble). Result: R4
    improves pooled MAR **+0.078 on BOTH venues** (bybit +0.108, binance +0.048), at
    constant average hedge, no DD increase, and **decisively beats the R5 hash
    control** (−0.614; +0.69 spread), gain concentrated in high-vol regimes as
    hypothesized. **The program's first both-venue, control-beating positive
    signal.** But it misses the +0.1 Tier-2 bar (+0.078) and is cost-fragile (2×
    hedge cost → +0.008 pooled, binance flips negative — hedge turnover eats the
    benefit). NULL on the registered bar (not moved); the directional signal is the
    lead.
  - **Stage 8b lower-turnover regime-hedge NULL 2026-06-14** (receipt
    `docs/preregistration/2026-06-14-w5-continuous-stage8b-lowturnover-regime-hedge.md`).
    Banded hysteretic BTC-vol intensity (0.7/1.0/1.3) cut hedge resizes ~25× (26–28
    vs ~660) but NOT hedge cost (few big 0.6-step jumps), and the coarse/lagged
    response mistimed the hedge: pooled MAR **−0.005** (bybit flipped +0.108→−0.063,
    binance stable +0.052). Falsifies the lower-turnover fix and shows Stage 8's
    +0.078 was partly bybit form-luck (form-stable benefit ~+0.05 on binance). **W5
    regime-hedge family banked: real but sub-threshold (~+0.05), not harvestable
    above +0.1 net of hedge cost in either continuous or banded form — do not
    re-parameterize this lever.**
  - **Stage 3 exit alpha (mfe_giveback) NULL — decisively harmful 2026-06-14**
    (receipt `docs/preregistration/2026-06-14-w5-continuous-stage3-exit-alpha.md`).
    Additive engine hook `hash_exit_prob` (config.py + continuous_events + trade_lifecycle,
    default 0 → byte-identical, 551 tests pass) for the neg control. MFE-giveback
    trailing gain-lock (trigger 0.05/retain 0.50, same entries — counts identical) on
    a mean-reversion fade book exits on the first bounce, cutting winners before full
    reversion: return crashes (bybit 0.77→0.53, binance 0.64→0.26), pooled MAR
    **−2.29**, and is WORSE than the random hash-exit control (−0.92). Lesson: *earlier*
    exits fight the reversion thesis; the fixed-hold-to-TP exit is near-optimal. Exit
    lever closed in the exit-earlier direction.
  - **W5 cross-lever conclusion:** the continuous control (MAR ~4.75/5.26) is
    **near-optimal across every tested lever** — entry priority (no room), path-shape
    priority+sizing (real but not harvestable), regime-hedge (real but sub-+0.1), and
    exits (earlier exits harmful). Best forward-watch candidate = the Stage 8
    regime-hedge sub-threshold lead (~+0.05).
  - **Stage 3b hold-extension NULL — exit lever closed both directions 2026-06-14**
    (receipt `docs/preregistration/2026-06-14-w5-continuous-stage3b-hold-extension.md`).
    Hold 24→48h captures more reversion on bybit (return 0.77→0.98) but worsens DD
    both venues; binance MAR collapses 5.255→1.715; pooled MAR **−1.708**, split-venue.
    (Confound: fixed-mode cooldown = hold_hours, so entries dropped ~4%.) With Stage 3
    (earlier exits harmful), the **fixed 24h hold is near-optimal**. Fails strict AND
    looser bar.
  - **Stage 8c regime-hedge robustness grid 2026-06-15** (receipt
    `docs/preregistration/2026-06-15-w5-continuous-stage8c-regime-hedge-robustness.md`).
    λ×cost grid of the continuous BTC-vol regime-hedge. **λ-robust** (pooled MAR delta
    positive for all λ∈{0.25,0.5,0.75} at 1× cost: +0.042/+0.078/+0.078, and at 1.5×),
    **beats the hash control by +0.6–0.8 at every cost**, bybit robustly positive at ALL
    costs (+0.075→+0.108), binance positive at realistic 1× (+0.049) but thin cost
    headroom (breaks even ~1.2×, −0.011 at 1.5×). Per the locked criterion NOT robust
    (binance fails the 1.5×-cost stress, marginally). **At realistic 1× hedge cost it is
    a robust, both-venue, control-beating, trade-keeping +MAR improvement (~+0.05–0.08
    pooled) — the strongest W5 candidate**, qualifying under the operator's bar as a
    demo/paper forward-watch candidate (λ=0.5–0.75, BTC-vol). Tier-3 real-money gate
    unchanged.
  - **Stage 8d/8e regime-hedge signal exploration 2026-06-15** (receipts
    `…stage8d-regime-hedge-signals.md`, `…stage8e-regime-hedge-blend.md`). Tested
    book-vol / book-drawdown / multifactor / BTC-vol+book-DD-blend hedge-regime signals
    vs the BTC-vol baseline. Findings: book-vol counterproductive; **book-DD robustly
    fixes binance at ALL costs (+0.15→+0.27) but breaks bybit** (opposite-sign by venue,
    unusable alone); the 50/50 blend DILUTES (binance −0.061, worse than either). **BTC-vol
    (Stage 8c) remains THE regime-hedge candidate** (robust λ, beats hash +0.6–0.8,
    both-venue +at realistic cost, binance thin). Signal exploration thorough; no clean
    both-venue-robust signal beyond BTC-vol.
  - **Stage 9 regime-conditioned sizing NULL 2026-06-15** (receipt
    `docs/preregistration/2026-06-15-w5-continuous-stage9-regime-sizing.md`). Sizing the
    book DOWN in high-BTC-vol regimes (constant breadth, mean-1, gross-neutral) HURTS both
    venues (pooled MAR **−0.633**, bybit −1.19) and is **worse than a random hash-regime
    size control** (−0.075). Key insight: the fade book PROFITS in high-vol regimes (alt
    dislocations + funding), so shrinking it there forgoes profit — explains why the
    regime-HEDGE works (keep the book + hedge the squeeze tail) but sizing-down fails.
    Regime book-sizing closed; hedge+size-down combination unappealing. **BTC-vol
    regime-hedge (Stage 8c) remains THE candidate.**
  - **Stage 10 dispersion-hedge NULL 2026-06-15 — binance sign CORRECTED by Stage 10b** (receipts
    `…stage10-dispersion-hedge.md`, `…stage10b-dispersion-clean.md`). Stage 10 reported
    dispersion "venue-split, binance −0.273" — but that was a GROSS-NEUTRALITY BUG (omitted
    prior-month normalization → ~15% over-hedge). **Gross-corrected (Stage 10b): dispersion is
    binance +0.293 (sign flip) and a ROBUST binance hedge regime** (beats hash + constant-level
    controls at every λ/cost, monotone in λ, sub-period-OK), while bybit +0.04 is NOISE (fails
    the hash control at λ0.25, negative in 2/3 thirds). So dispersion is **venue-split:
    binance-real / bybit-noise** (NOT bybit-good/binance-bad). Still not a both-venue candidate;
    for a binance sleeve it is a real hedge, and the BTC-vol×dispersion stack is binance +0.368.
    (Among regime signals: BTC-vol is bybit-robust/binance-thin; dispersion is
    binance-robust/bybit-noise — COMPLEMENTARY across venues.)
  - **Hedge lever closed; EMA-smoothing ruled out without a run.** The last hedge idea
    (EMA-smoothed intensity to cut binance turnover cost) does not address the actual cost
    driver: the intensity is **mean-1**, so Stage 8 already showed R4's binance hedge cost
    (−0.0040) ≈ V0's (−0.0041) at 1× — there is no meaningful intensity-churn cost to smooth
    away. The binance 1.5×-stress break is base hedge economics (the extra high-vol hedging
    is *where the benefit is*, so smoothing cuts cost and benefit together). So smoothing
    cannot firm the binance stress headroom; not run. **The BTC-vol regime-hedge (Stage 8c)
    is the FINAL in-sample regime candidate and the standing W5 deliverable** — it qualifies
    under the operator's bar (robust at realistic 1× hedge cost, both venues +, beats hash
    +0.6–0.8, keeps all trades, λ-robust); the 1.5×-cost binance thinness is the lone
    caveat. Its natural next phase is **forward-watch (operator-gated — touches the live
    demo; do NOT set up without operator direction)**.
  - **Stage 4 LIQUIDITY SNIPER — DOWNGRADED to venue-split by decile robustness (Stage 4d);
    NOT a robust both-venue improvement** (receipts `…stage4-sniper.md`, `…stage4c-sniper-hedge-combo.md`,
    `…stage4d-decile-robustness.md`). A within-symbol IC screen found `turnover_quote` is the
    unique untested both-venue selection signal (partial rank-IC +0.081 bybit / +0.134 binance,
    p=0.001). The engine sniper (drop least-liquid decile, `size_mult=0`) at **k=10%** looked
    excellent — pooled +0.407, beats a per-day random-drop control, and (Stage 4c) stacked with
    the regime-hedge to a pooled +0.569 combo. **BUT the decile-robustness follow-up (Stage 4d,
    k∈{5,20}%) FALSIFIED it:** the effect does not survive the drop fraction. On **bybit** the
    turnover-drop has no reliable edge — a random drop matches it at k=5% and BEATS it by +4.16
    MAR at k=20%; turnover-drop vs T0 swings +0.707/+0.159/−1.312 across k. The **single-seed
    random control was inadequate** (bybit random MAR swings 4.6→7.6 by drawdown-event luck),
    so the k=10% "beats random" on bybit was favorable noise. Only **binance** shows a
    consistent (modest) liquidity effect (beats random at all k, peak ~k=10%). **Net: the
    sniper is VENUE-SPLIT (binance-real, bybit-noise)** — the k=10% both-venue headline was a
    favorable cut. A real IC that does not translate to a robust both-venue MAR harvest (cf.
    Stage 5/7b path-shape). The Stage 4/4c "+0.407 / +0.569 robust both-venue" claims are
    SUPERSEDED — do not cite them.
  - **STANDING CANDIDATE reverts to the BTC-vol regime-hedge (Stage 8c)** — the only validated
    both-venue improvement (robust at realistic 1× cost: both venues +, beats hash +0.6–0.8,
    λ-robust, keeps trades; thin binance 1.5×-cost headroom the lone caveat). The Stage 4c
    combo's bybit gain was mostly THIS hedge (real) plus fragile sniper noise; the sniper adds
    genuine value on binance only. Cross-lever conclusion: entry priority (Stage 1), sizing
    (Stage 5/9), exits (Stage 3/3b), most hedge signals, AND now liquidity selection (Stage 4,
    bybit) do NOT yield a robust both-venue MAR harvest beyond the regime-hedge. Methodology
    lesson banked: **a single-seed random-drop control is inadequate at these drop sizes (huge
    MAR variance); use a multi-seed null.**
  - **Stage 2 ENTRY-STYLE NULL 2026-06-15** (receipt `docs/preregistration/2026-06-15-w5-continuous-stage2-funding.md`).
    Two entry-style ideas both closed with NO engine spend (Stage 4d discipline — check
    evidence/screen before the 2.3h sweep): (a) the **deceleration filter** (`entry_decel_*`)
    has a NEGATIVE prior — it is a 6h-magnitude filter and Stage 7b showed bigger pumps fade
    BETTER, so it would drop the best fades; not run. (b) **Entry-funding selection** screened
    NULL — within-symbol partial funding IC over composite is weak and insignificant (bybit
    +0.032 p=0.111, binance +0.051 p=0.028; signs consistent but ~half the path-shape/liquidity
    ICs). The entry-style lever (priority, magnitude/path-shape, liquidity, decel, funding) is
    thoroughly NULL — entry-set modifications do not robustly harvest beyond the control.
  - **Hedge-INSTRUMENT probe (ETH vs BTC) NULL 2026-06-15** (exploratory component-reuse, no
    receipt — recorded here). Swapping the hedge leg from BTC to ETH (same causal rolling-beta
    rule + BTC-vol regime intensity) is WORSE on both venues: ETH-vs-BTC regime-hedge ΔMAR
    bybit −0.166, binance −0.403; ETH frozen hedge vs control −0.082 / −0.925. ETH is more
    volatile and co-moves with the alt book, adding hedge-leg variance/tracking-error. **BTC is
    the well-chosen hedge instrument**; a BTC+ETH 2-factor is low-prior (ETH alone clearly worse).
  - **PROGRAM CONVERGENCE ASSESSMENT (2026-06-15):** the W5 lever space is now thoroughly
    explored (~16 distinct mechanisms) and **the BTC-vol regime-hedge (Stage 8c) is the one
    robust both-venue edge / the deliverable.** Everything else is NULL/harmful/venue-split:
    entry priority (Stage 1), magnitude/path-shape (Stage 5/7b), liquidity (Stage 4: venue-split),
    decel (neg prior), funding (Stage 2 NULL); sizing (Stage 5/9 harmful); exits both directions
    (Stage 3/3b harmful); hedge signal (Stage 8d/8e/10: BTC-vol unique) and instrument (ETH worse).
    The continuous control's entry/exit/sizing is near-optimal; only the tail-hedge overlay
    harvests. Real single-venue residual: a binance within-symbol liquidity effect (forward-watch
    note, not a both-venue candidate). **Recommended deliverable: the BTC-vol regime-hedge
    (λ=0.5), demo/paper forward-watch (operator-gated); Tier-3 real-money gate UNCHANGED.**
  - **Correlation-aware concurrency cap NULL 2026-06-15** (cheap diagnostic, no engine). Tested
    whether the squeeze tail is driven by many concurrent correlated shorts: daily concurrency
    correlates POSITIVELY with daily return (Spearman +0.155 bybit / +0.145 binance), and the
    BEST days are MORE concurrent than the worst (bybit 14.7 vs 13.7; binance 16.7 vs 13.2). So
    broad-deployment days are net winners — a concurrency cap would cut the best days more than
    the worst → harmful (same root cause as Stage 9: the book profits broadly; hedge the tail,
    don't shrink the book). Killed cheaply.
  - **PROGRAM CONVERGED (2026-06-15) — ~17 distinct mechanisms; the regime-hedge is the
    deliverable, and a single root cause explains every NULL.** The continuous fade book's edge
    is DIFFUSE and the book PROFITS when broadly deployed in dislocations (Stage 9 + the
    concurrency diagnostic), so every lever that selects/shrinks/derisks the entry set forgoes
    that diffuse profit and fails to robustly harvest (entry priority/path-shape/liquidity/decel/
    funding; sizing; concurrency cap; exits both ways). The ONLY robust improvement is the
    tail-HEDGE overlay that keeps the book and protects the squeeze tail. **Deliverable: BTC-vol
    regime-hedge (Stage 8c, λ=0.5)** — robust both-venue at realistic 1× cost, λ-robust, beats
    hash control, keeps all trades. Real binance-only within-symbol liquidity IC is a
    forward-watch note (already characterized by the Stage 4 permutation screen; not a both-venue
    candidate, not harvestable as robust MAR per Stage 4d).
  - **Stage 8f regime-hedge sub-period validation 2026-06-15** (cheap component reuse;
    `~/SHARED_DATA/w5_continuous_stage8f_hedge_subperiod_2026-06-15/`). Sliced the deliverable
    into chronological thirds + years. **Honest refinement: the regime-hedge is MODEST,
    sub-period-VARIABLE tail insurance — not a smooth uniform edge.** In every sub-period it
    ADDS or holds RETURN (e.g. bybit third3 0.3634→0.3709, binance Y2024 0.1270→0.1317); the
    MAR dips that appear (bybit third3 ΔMAR −0.377, binance Y2024/25 −0.10/−0.10) are small
    maxDD increases (the long-BTC hedge leg loses in calm windows where BTC falls without a book
    squeeze), amplified by the MAR ratio in high-MAR windows. So it pays off in squeeze episodes
    and costs a little maxDD in calm windows — genuine tail insurance, pooled +0.05–0.08 MAR,
    return-additive, beats the random-regime control decisively (Stage 8c — the signal is real),
    but NOT uniformly positive per bucket. Binance is thirds-positive (+0.028/+0.056/+0.013);
    bybit is positive in thirds 1–2 and slightly DD-costly in third 3.
  - **FINAL W5 DELIVERABLE (honest):** the BTC-vol regime-hedge (λ=0.5) is a **modest both-venue
    tail-insurance overlay** — the one real edge in ~17 mechanisms — appropriate for demo/paper
    forward-watch evaluated on squeeze protection (not smooth MAR). Recommend operator
    green-light forward-watch (operator-gated; Tier-3 real-money gate UNCHANGED). Do NOT oversell
    it as a robust uniform +MAR edge.
  - **Stage 8g aggregate-funding hedge regime NULL 2026-06-15** (receipt
    `docs/preregistration/2026-06-15-w5-continuous-stage8g-funding-regime-hedge.md`). Market-wide
    funding (crowding) as a hedge regime: pooled ΔMAR −0.016/−0.010/−0.004 (negative), fails to
    beat the frozen control on bybit (4.686 < 4.748), worse than BTC-vol both venues. **Closes
    the hedge-signal space: BTC-vol is the UNIQUE both-venue hedge regime across all six tested
    signals** (BTC-vol, book-vol, book-DD, dispersion, multifactor, aggregate-funding).
  - **Exit lever fully closed — the book ALREADY has a take-profit.** Diagnostic of the T0 trade
    ledgers: exits are `max_hold` (24h timer) OR `take_profit` (~28% bybit / ~25% binance trades
    already exit via TP). So the frozen control is a 24h-hold-or-TP exit (what Stage 3/3b found
    near-optimal); "adding a take-profit" would be re-parameterizing an existing, calibrated knob
    — not a distinct lever. The exit lever (giveback Stage 3 harmful, extension Stage 3b harmful,
    24h+TP near-optimal) is exhausted.
  - **W5 PROGRAM FULLY CONVERGED (2026-06-15) — ~18 mechanisms; no distinct untested lever
    remains with a reasonable prior.** The continuous fade book's edge is diffuse and the book
    profits when broadly deployed in dislocations; every entry-set / sizing / concurrency / exit
    modification fails to one root cause, and the only robust improvement is the tail-HEDGE
    overlay. **DELIVERABLE: the BTC-vol regime-hedge (λ=0.5)** — a modest, sub-period-variable
    both-venue tail-insurance overlay (pooled +0.05–0.08 MAR, return-additive, beats the
    random-regime control; not a smooth uniform edge — frame it as squeeze protection). Real
    binance-only within-symbol liquidity IC = a forward-watch note, not a candidate.
  - **OPERATOR STEER 2026-06-15 (bybit-primary):** "we are trading on bybit … if it's bybit
    robust and not completely losing on binance, [is it] worth it?" Reasonable acceptance-bar
    refinement IF bybit is the traded venue: binance becomes a robustness CHECK, not a co-equal
    requirement. BUT the bar must stay **bybit-ROBUST** (robust across the free param + sub-period,
    not bybit-positive-in-one-config — that was the sniper trap), and the key distinction is
    binance NEUTRAL/thin (OK — didn't contradict) vs binance OPPOSITE-SIGN (red flag, venue
    artifact). Applied rigorously, this does NOT rescue the sniper or dispersion — both have their
    robust edge on BINANCE and are NOISE on bybit. **The only bybit-ROBUST signal remains the
    BTC-vol regime-hedge (+0.108, robust λ/cost).**
  - **DECISION (2026-06-15): option (a) TAKEN — the BTC-vol regime-hedge is now LIVE on demo +
    forward** (whole 2f hedge, fresh clock; see "What's Running" + receipt
    `docs/preregistration/2026-06-15-forward-btcvol-regime-hedge.md`). Forward-watch evaluates it as
    squeeze protection; Tier-3 real-money gate UNCHANGED. Options (b)/(c) below remain open for a
    future direction.
  - **RECOMMENDATION / next = OPERATOR INPUT.** In-sample search is complete; further grinding is
    diminishing returns and risks false positives (Stage 4 lesson). Recommend the operator: (a)
    green-light demo/paper FORWARD-WATCH of the **BTC-vol regime-hedge on bybit** (operator-gated;
    Tier-3 real-money gate UNCHANGED), evaluated on squeeze protection [DONE 2026-06-15]; (b) IF a binance sleeve
    also runs, a **per-venue hedge** — BTC-vol on bybit + **dispersion (Stage 10b) on binance**
    (binance +0.293, robust; or the BTC-vol×dispersion stack +0.368) — is a defensible
    forward-watch option (each independently robust on its venue; venue-specific tuning, not a
    both-venue signal); or (c) supply a new research direction / data source. The loop should
    consolidate, not manufacture low-prior experiments. All W5 code (stage
    0/1/2/3/3b/4/4c/4d/5/7/7b/8/8b/8c/8d/8e/8f/8g/9/10/10b scripts + 3 engine hooks) + receipts
    were COMMITTED + merged to main (85e92b6, 2026-06-15); the Stage 8c deliverable is now promoted
    live (see "What's Running").
- **Forward data stack:** P11 taker-flow full-universe completion is idle-time;
  P12 liquidation-proxy calibration waits on a mature forward liquidation tape
  (~2026-07-10). All remaining evidence paths are forward-only: demo fills →
  R4 calibration, dynexit shadow, forward-watch leads (≥100 trades/book).
  Forward signal clock: `scripts/continuous_forward_replay_orchestrator.py`
  (run at each data-root refresh; overlap drift = hard alarm). As of 2026-06-15 it
  accrues the BTC-vol regime-hedge object (config hash `0668eb88c0d6…`); the prior
  regime-free 2f clock is voided — archive it and start fresh (receipt above).

Prior same-day results (2026-06-12) and current status:

- E1 composite size tilt ended at Stage-0 NO-GO (+0.15bp/trade bybit). That
  exact size-tilt mechanism is not evidence for deployment; a different
  composite mechanism needs a fresh staged preregistration.
- P10 event-level taker-flow conditioning failed in its registered form. A later
  flow/liquidation/depth design is admissible only as a new registered stage
  with richer artifacts and both-venue evidence.
- E2 regime family NULL — V1/V2 destroy MAR vs the live gate (pooled −1.96 /
  −2.52); the binary uptrend gate stands until a new registered regime
  mechanism proves otherwise.
- Daily-granularity sizing conversion on the continuous book is not active.
  Reopening it requires a materially different mechanism and a new
  preregistration; do not treat the old failed tilt as a live candidate.

## Decision Rules

Forward demo/paper is the arbiter. MAR is primary, Sharpe secondary.

### Tier 1 - Investigation

- MAR delta positive on a majority of venues, or one venue positive with the
  other not badly worse.
- No return sign-flip vs control.
- At least 30 Bybit / 20 Binance trades unless explicitly labeled a tiny scout.

### Tier 2 - Demo Candidate

- Positive return on both venues.
- Pooled MAR delta > +0.1.
- Neither venue worse than MAR delta -0.5.
- Trade counts clear Tier 1.
- Fragility diagnostics reported, never used to rescue a weak cell.

### Tier 3 - Real Money

Strict and currently unmet:

- At least 30 days forward demo/paper.
- Forward MAR > 0 on both venues.
- Drawdown < 50%.
- Daily reconciliation.
- Bootstrap pooled MAR-delta left tail >= 0.
- Residual Sharpe >= +0.3.
- Stress pass and capacity >= 10x deployment size.
- No internal pre-2023 OOS exists.

## Open Methodology Debts

- **Rmom latency:** causal but knife-edge. No continuous promotion case until a
  design proves the effect can be harvested with operational margin.
- **Impact/capacity:** R4 realized-fill calibration waits on live fills and depth
  collector data.
- **Forward evidence:** continuous forward window is immature; the replay
  orchestrator is built and must run at each data-root refresh. Overlap drift
  is a hard alarm.
- **Funding/data freshness:** Binance June ancillary top-up remains blocked from
  the dev box.

## Helpers

- Reconcile: `bash scripts/reconcile.sh`
- Tier-2 robustness: `python scripts/r1_robustness.py --sweep-tag <TAG>`
- Continuous readiness: `python -m liquidity_migration continuous-forward-readiness --paper-only`
- Hedge dry-run: `.venv/bin/python scripts/run_continuous_hedge.py --venue bybit`
- Vision backfills: `scripts/backfill_binance_{funding,metrics,bookdepth}_vision.py`

## Non-Negotiables

1. Never set `REAL_MONEY=true` without explicit owner instruction.
2. Never present continuous as promoted or paper-ready.
3. Both venues matter; single-venue wins are not enough.
4. Full-PIT, causal features, ledgers, and cost modeling are correctness gates.
5. Do not loosen Tier 3 to rescue a result.
6. Pre-push gate before any push: ruff plus pytest.
7. Do not commit or push without operator confirmation.

## How To Update

Keep this file short. Research results go in `docs/research_summary.md`.
`docs/preregistration/` keeps only receipts that still bind an active
deployment, candidate, or methodology decision.
