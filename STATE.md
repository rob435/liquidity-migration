# Research Program State

**Last updated:** 2026-06-09
This file is live/operational state plus binding decision rules. Research conclusions live in
[docs/research_summary.md](docs/research_summary.md).

## First Read

Read these two files only:

1. `STATE.md` - what is running and what rules bind us.
2. `docs/research_summary.md` - consolidated research findings and next direction.

Old one-off research receipts were consolidated and deleted. Git history is the archive.

## Current Status

Liquidity-migration is research-stage. Nothing is approved for real money.

The current short profile is the code-resolved promoted profile:
`drop_all_4 + age300 + ff6 + btc_trend_gate=uptrend`. It does **not** use rmom;
`liquidity_migration_residual_momentum_max=10.0` is the inactive sentinel. The
continuous work is research-only. The right continuous direction is no longer
"independent system replaces old candidate"; it is "old rebalance engine plus cleaner
independent entry/exit logic."

## What's Running

- **2026-06-09 operator re-shape: the VPS runs ONLY the continuous system.**
  SHORT/SHORT_PAPER/LONG sleeves toggled OFF (`deploy/sleeves.env`); their profiles
  stay promoted-in-code and redeployable. CONTINUOUS demo orders + paper shadow
  toggled ON by explicit operator instruction (demo account only; forward evidence +
  observed-fill cost calibration). Honest status: continuous remains research-stage
  and NOT promoted; demo fills are execution evidence, not alpha proof; the rmom
  latency knife-edge caveat stands.
- **CONTINUOUS (the only live sleeve, 2026-06-10):** demo orders ON + paper shadow.
  Base book = `continuous_rebalance_v1` (turn4_pop4 + w90/max4 rebalance + uptrend gate
  + rmom q25 — a validated continuous object), already submitting demo orders.
  **BTC-beta HEDGE: LIVE as a daily dry-run** (`continuous-hedge.timer`, 00:35 UTC) —
  computes the banked hedge size (trailing-90 beta -0.026 -> ~2.6% long-BTC, Buy plan
  verified on the box) and LOGS it; order submission is GATED (SUBMIT_HEDGE=0) pending
  one verified cycle + ws_risk adoption-schema confirmation. Module
  `continuous_hedge_manager.py` (8 tests), warm-start from the UN-hedged control book.
  SNIPER: Tier-2 demo candidate (Amendment 6) — live wiring (resting +8% limit per
  entry) is the NEXT build, not yet in the daemon. Full 4-component ensemble (vs the
  single turn4_pop4 live) also next. Honest: continuous is research-stage; demo fills =
  execution evidence, not alpha proof.
- SHORT (off-box, promoted-in-code): `drop_all_4 + age300 + ff6 + btc_trend_gate=uptrend`;
  rmom inactive at `10.0`. Receipts: `promote-age-ff6-demo-2026-05-31.md`,
  `drop-all-4-promotion.md`.
- LONG (off-box, promoted-in-code): `div` profile **+ volup125** (`vol_target_max_scale`
  1.0→1.25, operator-promoted 2026-06-09; receipt `long-volup-candidate-2026-06-09.md`;
  note: the live 10x multiplier interaction flagged by the risk audit must be resolved
  before the long sleeve is re-enabled on a box).
- **VPS:** Hetzner live host runs demo/paper services. Keep `REAL_MONEY=false`; never enable
  real money without explicit owner instruction.
  **2026-06-09 FULL REBUILD (operator):** fresh Ubuntu 24.04; all prior demo/paper ledger
  HISTORY was lost with no backup (operator confirmed nothing material was collected).
  Reprovisioned same day from local checkout @ 92db7e4 (rescue-mode SSH restore -> direct
  git push -> venv -> units): all sleeves per `deploy/sleeves.env` (short+long demo/paper
  ON, continuous orders OFF, continuous no-order paper collector ON). **All forward
  demo/paper Tier-3 clocks restart 2026-06-09.** New host key SHA256:TJRbvgB8... (workflow
  default updated; the GitHub Actions VARIABLE `VPS_ED25519_FINGERPRINT` may still be stale
  -> operator must update/delete it for CI deploys to pass). Root password + demo API keys
  + Telegram token passed through chat during recovery -> rotate when convenient.

## Current Research Direction

### Daily Short

Currently used profile: `drop_all_4 + age300 + ff6 + btc_trend_gate=uptrend`.

- **btc_trend_gate=uptrend is Tier-2 VALIDATED (2026-06-09): DEMO-ELIGIBLE.**
  by MARΔ +1.52 / bn MARΔ −0.12 / pooled +0.70, full-PIT both venues. The case is
  carried by Bybit (MAR 1.38→2.89, bootstrap P(Δ>0)=98%); Binance is a wash
  (P(Δ>0)=59%, final third negative) — reported, non-blocking. Receipt:
  `docs/preregistration/btc-gate-tier2-validation-2026-06-09.md`.
- Age gate around 300d is robust.
- Rmom is not in the promoted short. Historical rmom work is research-only and not a
  current run instruction.
- Execution timing is not the main lever.
- 2026-06-09 risk/data findings (details in research_summary): the book is one
  BTC-regime trade (37% of days carry ~82% of book P&L); live LONG 10x multiplier is
  not supported by 1x evidence (peak gross leverage 11.6x, wipe at −8.6% uniform day);
  Binance funding coverage hole (51 symbols) makes its results ~30% optimistic.

### Long

- **volup125 candidate ACCEPTED 2026-06-09** (pre-registered): `vol_target_max_scale`
  1.0→1.25, +24% relative return both venues at unchanged ret/DD/Sharpe, identical
  trade set. NOT deployed — needs operator sign-off and must ride with the
  long-sleeve leverage-cap decision (live 10x multiplier interaction). Receipt:
  `docs/preregistration/long-volup-candidate-2026-06-09.md`.
- Structural nulls (don't re-run): FC breadth, hold-extension/trailing/scaled exits,
  pyramiding, majors TSMOM/Donchian overlay (dilutes MAR at every weight).
- The long "step function" equity curve is an exit-booking artifact; daily-MTM
  rendering (`scripts/long_tsmom_overlay.py` engine) shows MAR 2.38 / DD −8.5%.

### Continuous

**FROZEN 2026-06-09 — the 2023-04→2026-05 window is SPENT for continuous
adjudication.** The window has adjudicated hundreds of accept/reject decisions
(the 06-07/06-08 sweeps alone left ~25 artifact roots); further variant
selection on it only degrades the believability of the numbers it already
produced. Binding until forward evidence exists:

- No further accept/reject sweeps, weight tweaks, filter frontiers, or
  risk-rule retargets on this window. (The 40%/70d downtrend-extended ensemble was
  the frozen winner when this freeze was written; a same-day parallel session's
  pre-registered fragility receipt DEMOTED it — see the 2026-06-09 re-anchor below.
  The freeze and the demotion are compatible: the demotion was a completed
  pre-registered decision, and the freeze governs everything from here on.)
- **2026-06-09 downgrade:** the rmom latency falsification FAILED (see Methodology
  Debts) — every continuous candidate rides rmom q25, so the whole line now rests on
  a boundary-concentrated feature. Day-grid alignment audit is the blocking
  prerequisite before any continuous promotion case.
- Forward no-order paper evidence is the only admissible new continuous
  evidence (`continuous-forward-readiness` / `continuous-vs-daily-forward`).
- EXEMPT from the freeze: methodology-falsification and causality audits (e.g.
  the rmom latency-delay test) — tests that can only kill the line, not
  improve it — and bug-fix re-runs of the frozen winner.

The strongest old continuous object is still the decomposed daily-rebalance candidate:

```text
q25_liq500k_btcup_turn4_pop4_decomp_rebalance_w90_tv25_max4_dd4_trend180_hurdle2
```

Keep what works from it:

- decomposed daily rebalance accounting;
- 90d realized-vol targeting;
- 2.5% target daily vol;
- max 4x scale;
- -4% drawdown half-scale;
- 10 bps resize cost;
- optional 180d strategy-equity momentum hurdle.

The merged test is complete. Keep the better independent trade logic:

- age >= 240d;
- `turn3_pop3` entry trigger;
- crowd cap 2;
- TP10;
- 24h hold;
- no hard stop, no rank-decay exit, no giveback exit by default.

Current cleaner cross-venue continuous research candidate:

```text
q25_liq500k_btcup_turn3_pop3_age240_tp10_crowd2_decomp_rebalance_w90_tv25_max4_dd4
```

Use it **without** strategy-equity momentum. Soft 0.25x, soft 0.5x, and the old hard-off
180d/+2% hurdle all hurt the merged signal; hard-off was especially weak under 2x costs.
Details are consolidated in `docs/research_summary.md`; the per-run continuous receipts
were deleted because continuous is not promoted or paper-ready.

The 2026-06-08 derivatives-positioning frontier rejected causal funding,
premium-index, and mark-index-basis hard filters for this merged stream. The
filters had near-complete coverage but reduced MAR versus the unfiltered control.
The closest return retarget was the unfiltered high-scale rule, not a filter:
Bybit +137.46% / MAR 4.39, Binance +112.77% / MAR 4.70, worst DD -10.00%.
That still fails the +120% both-venue and MAR 6 target.

**2026-06-09 re-anchor (WP2):** the downtrend-extended ensemble (7.50/6.84 headline)
is **DEMOTED** — its `premium_24h_ge0` downtrend sleeve is fragile/overfit (85/91
trades on ~10 active days; `dt_scale=0.4` sits at a cliff). The canonical continuous
research object is the parsimonious **uptrend ensemble winner_base**
`{turn3p3:0.30, turn4p3:0.20, turn4p5:0.40, age210tp14:0.10}` @ w90 rebalance, quoted
at **max4-6 leverage** (max4: bybit +84%/MAR 5.0, binance +60%/MAR 4.6; the max10
6.18/6.01 headline is recent-regime-flattered; `tv` is a dead knob — scale pins at
`max_scale`). It passed a pre-registered 5/5 falsification battery. Receipts:
`docs/preregistration/continuous-winner-robustness-2026-06-09.md`,
`docs/preregistration/continuous-demote-downtrend-extension-2026-06-09.md`.

**Regime program (docs/research_plan_continuous_regime_2026-06-09.md):** WP1a ran
2026-06-09 — trailing alt-vs-BTC relative strength does NOT predict squeezes (IC
~+0.01..+0.06 both venues vs pre-registered <= -0.08 bar; NO-GO; WP1b gate forms
dead). Mechanism confirmed contemporaneous: same-day RS vs book return -0.26/-0.30,
but alt-RS is a daily martingale (AR1 ~+0.03) — the exposure can be HEDGED, not
timed. Receipt: `docs/preregistration/continuous-rs-squeeze-probe-2026-06-09.md`.
WP3 ran same day in two pre-registered stages and is BANKED (in-sample candidate,
Tier-2 ceiling): Stage-A instrument comparison (PASS 6/6; BTC selected over alt_top10
and the non-tradeable alt_ew ceiling; real funding charged) then Stage-B through the
engine — hedge leg now lives inside `apply_rebalance_rule`
(`ContinuousHedgeRule(w90/min60/cap2)`, causal beta, DD-state on hedged equity;
unhedged path unchanged; 8 new tests). Stage-B PASS 8/8 at binding max4: ΔMAR
+0.50/+1.07, ΔSharpe +0.23/+0.38, 2023-24 Sharpe +0.44/+0.63; survives 2x/4x hedge
cost, funding-off, window grid, 1-day beta latency, and 2x BOOK cost (where it helps
MORE: ΔMAR +0.89/+1.03). Durable claim = regime-robustness (recent-tilt flattens;
2025 unchanged); part of raw return gain is bull-sample-specific. Receipts:
`docs/preregistration/continuous-hedge-{overlay,engine}-2026-06-09.md`.

**Live-readiness program (operator granted full authority 2026-06-09):**
`docs/research_plan_continuous_live_readiness_2026-06-09.md` — ALL autonomous items
done 2026-06-09: R0 funding debt CLOSED (verified vs raw datasets). R1 weight-overfit
DEAD (causal haircut 13.8%; equal-weight matches winner OOS; live policy = frozen
weights, no re-estimation). R2 hedge executor functions + backtest<->live parity
tests. R3 no-order forward replay collector built (config-hash pin, drift alarm,
idempotent) and SEEDED on real data (rebuilds the banked ledger exactly; clock at 0
days awaiting fresh roots). R5 capacity (pre-stated bar): combined Tier-3-safe
deployment ~$200-300k — a SMALL-BOOK strategy; turnover-capped sizing is the unrun
capacity lever. R4 impact calibration BLOCKED on operator (needs the VPS demo fill
ledgers; `bash scripts/reconcile.sh` pull). Operator decisions pending: data-root
refresh (starts the clock), R4 ledger pull, commit/push of the working tree, any demo
enablement. Guardrails unchanged: REAL_MONEY=false, Tier-3 strict, no push without
operator.
All June 7-9 continuous run receipts are consolidated in `docs/research_summary.md`;
the durable artifacts remain under `C:\Users\user\SHARED_DATA\...`.

### Ridge Combiner — REJECTED 2026-06-09 (scout falsified it cheaply)

The pre-registered walk-forward ridge scout ran: bybit pooled OUT-OF-FOLD rank-IC
**−0.04** (anti-predictive; coefficients stable but no signal); binance arm
unmeasurable (0 folds — OI/funding feature coverage holes). Tier-1 gate (positive IC
both venues) fails → engine-sizing wiring does NOT proceed. Re-run requires the
Binance funding rebuild + an OI backfill AND a freshly pre-registered feature set.
Receipt: `docs/preregistration/ridge-combiner-2026-06-09.md`.

**Cross-session governance note (2026-06-09):** two parallel sessions worked this
day without awareness of each other. This session's continuous results (hedge
banking, live-readiness, sniper/downtrend arcs below) were run before the freeze
above was visible; their receipts stand as pre-registered in-sample records, but
they ride rmom-q25-gated components and therefore INHERIT the rmom latency-knife-edge
caveat (see Methodology Debts) — no continuous promotion case proceeds until that is
resolved, and the window freeze binds all future continuous adjudication, including
any revival of this session's Tier-1 leads.

**Downtrend-sleeve + sniper program (operator goal 2026-06-09, +30% pooled MAR):**
`docs/research_plan_downtrend_sleeve_2026-06-09.md`. D1 found real down-regime
cross-sectional reversal (D10-D1 -51/-24 bps); D2 reversal L/S FAILED Stage-A
(binance 0/8; funding -28..-43% + costs eat it) -> downtrend capital stays hedge+cash.
S1 sniper arc (4 pre-registered amendments + Amendment 6): additive quarter-size
snipe at the +8% wick = pooled MAR 5.58 -> 6.30 at 1x (+13%, both venues, per-fill
alpha +2-3%). **Tier-2 DEMO CANDIDATE by operator decision 2026-06-10**: it passes the
binding Tier-2 bar even at 2x cost (pooled +0.375 > +0.1, both venues positive); the
program's stricter +0.5-at-2x banking overlay remains failed-and-reported as a
fragility diagnostic (non-blocking for demo under the house framework). Real-cost
calibration from demo fills (Amendment 5) decides the Tier-3-facing margin question. D3 bounce-long (declared-door revisit): standalone bar passes
(funding-receive confirmed; bybit Sh 2.03/binance 0.81) but the COMBINED bar fails
catastrophically (sleeve DD -30..-44% vs the book's -5% budget) -> **downtrend
question CLOSED: hedge + cash is final** (a high-vol bounce book would be a separate
product with its own risk budget). +30% goal NOT honestly reachable in-sample;
remaining paths: engine-grade snipe + R4 calibration, WP4 residual-momentum build
(operator-gated), forward demo. Receipts:
downtrend-{opportunity-map,reversal-ls,bounce-long}-2026-06-09.md,
sniper-staged-entries-2026-06-09.md.

## Binding Decision Rules

Forward demo/paper is the arbiter. MAR is primary; Sharpe is secondary.

### Tier 1 - Investigation

- MAR delta positive on majority venues, or one venue positive with the other not badly worse.
- No return sign-flip versus control.
- At least 30 Bybit trades and 20 Binance trades, unless explicitly labeled a tiny scout.

### Tier 2 - Demo Candidate

- Positive return on both venues.
- Pooled MAR delta > +0.1.
- Neither venue worse than MAR delta -0.5.
- Trade counts clear Tier 1.
- Fragility diagnostics are reported, not used to rescue weak cells.

### Tier 3 - Real Money

Strict and not loosened:

- At least 30 days forward demo/paper evidence.
- Forward MAR > 0 both venues.
- Drawdown < 50%.
- Daily paper/demo reconciliation.
- Bootstrap pooled MAR-delta left tail >= 0.
- Residual Sharpe >= +0.3.
- Stress pass and capacity >= 10x deployment size.

No internal pre-2023 OOS substitute exists.

## Methodology Debts

These can still move numbers:

- ~~Binance funding coverage~~ **RESOLVED 2026-06-09**: dataset rebuilt from
  data.binance.vision (51→697 symbols, true settlement intervals); Binance gate cells
  re-measured (−4.5% abs baseline, inside the predicted band); gate verdict HOLDS
  (pooled +0.73). Pre-rebuild Binance numbers are ~3-6% abs optimistic. Receipt:
  `docs/preregistration/binance-funding-rebuild-2026-06-09.md`.
- ~~Live age vs PIT backtest age~~ **CLOSED 2026-06-09 (quantified-acceptable)**:
  median divergence 0d (564/569 within ±3d); rare outliers are all conservative
  (live launchTime resets on relaunch → live skips). 1 symbol flips the 300d gate
  today (FHEUSDT), in the safe direction.
- **Residual-momentum: latency falsification FAILED (2026-06-09).** The rmom edge is a
  knife-edge at shift3 only (+1d delay → pooled MAR 1.13→0.10, Bybit sign-flip); rmom
  supports no deployment-grade claim until debts here + day-grid alignment are resolved
  at the data layer. Receipt: `docs/preregistration/rmom-latency-falsification-2026-06-09.md`.
- ~~Factor/residual day-grid alignment~~ **CLOSED 2026-06-09: audited end-to-end,
  GRID CORRECT** (empirical bit-exact recompute; all consumer joins causal with
  ≥24h margin). The rmom knife-edge is genuine fast decay, not leakage; a continuous
  revival now needs an intraday-class execution design, not a data fix.
- ~~Binance funding interval ACCRUAL (continuous path)~~ **CLOSED 2026-06-09** —
  distinct from the coverage rebuild above: per-event accrual/sign/scaling verified
  end-to-end vs raw datasets (40/40 trades to 5e-20). Receipt:
  `docs/preregistration/continuous-funding-debt-closure-2026-06-09.md`.
- Continuous forward window is immature; current local evidence is not enough.
- Impact calibration at deployed size (live-readiness R4).

Risk-model receipt kept: `docs/preregistration/r4-risk-model-verdict.md`.
PIT membership receipt kept: `docs/preregistration/pit-membership-trading-day-fix.md`.

## Helpers

- Reconcile all sleeves: `bash scripts/reconcile.sh`
- Run daily research cell: `scripts/volume_events_cell.sh --cell-id X --overrides 'KEY=VAL,...'`
- Tier-2 robustness: `python scripts/r1_robustness.py --sweep-tag <TAG>`
- Legacy strict analyzer: `python scripts/apply_decision_rule.py SUMMARY.csv --control 00_baseline`
- Continuous readiness diagnostic: `python -m liquidity_migration continuous-forward-readiness --paper-only`
- Continuous vs daily forward comparator: `python -m liquidity_migration continuous-vs-daily-forward`
- Ridge combiner falsification scout: `python scripts/ridge_combiner_scout.py` (receipt: `docs/preregistration/ridge-combiner-2026-06-09.md`)

## Non-Negotiables

1. Never set `REAL_MONEY=true` without explicit owner instruction.
2. Never present continuous as promoted or paper-ready.
3. Both venues matter; single-venue Bybit wins are not enough.
4. Full-PIT, causal features, ledgers, and cost modeling are correctness gates.
5. Do not loosen Tier 3 to rescue a result.
6. Pre-push gate before any push: ruff plus pytest.
7. Do not commit or push without operator confirmation.

## How To Update

Keep this file short. Put research results in `docs/research_summary.md`. Keep
`docs/preregistration/` small and only for receipts that still bind an active
deployment, candidate, or methodology decision.
