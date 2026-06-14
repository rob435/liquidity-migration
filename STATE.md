# Research Program State

**Last updated:** 2026-06-13

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
  - Next: Stage 7 neutralized path-shape (also unlocks Stage 1 A2/A3/A4), then
    Stage 5 sizing. Stages 2/3/4/8 stay gated behind their own receipts.
- **Forward data stack:** P11 taker-flow full-universe completion is idle-time;
  P12 liquidation-proxy calibration waits on a mature forward liquidation tape
  (~2026-07-10). All remaining evidence paths are forward-only: demo fills →
  R4 calibration, dynexit shadow, forward-watch leads (≥100 trades/book).
  Forward signal clock: `scripts/continuous_forward_replay_orchestrator.py`
  (run at each data-root refresh; overlap drift = hard alarm).

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
