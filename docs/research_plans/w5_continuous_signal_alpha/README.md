# W5 Continuous Signal Alpha Program

**Date:** 2026-06-13
**Status:** draft plan, not a run receipt
**Book:** continuous fade, demo/paper only

This folder is the working plan for the next serious continuous research
program. It is not itself permission to touch the per-venue working roots.
Before any stage runs, write a dated preregistration or amendment under
`docs/preregistration/`.

## Objective

Rebuild the continuous signal research program around scored decisions instead
of ad hoc filters:

- composite score entry priority;
- neutralized path-shape scoring (the W4 Stage 3 "Stage 3b" the prior program
  promised but never ran);
- entry execution alpha;
- exit lifecycle alpha;
- sniper add-on alpha;
- sizing/risk-budget alpha;
- regime-response: beat the binary BTC-uptrend gate with a materially different
  mechanism;
- interaction and forward-demo validation.

### Objective discipline (read before you touch the regime stage)

The objective is **risk-adjusted return (pooled MAR) vs the frozen control on
both venues**, never trade count or "uptime." E2 (2026-06-12, receipt
`docs/preregistration/2026-06-12-e2-regime-response-family.md`) already
falsified the naive "trade more regimes" family: capping euphoria (V1) and
trading downtrends at quarter-size (V2) each cost ~20pp return and roughly
halved MAR on **both** venues. A book that trades in more regimes is an
*allowed outcome only if it pays* — it is never the target. Do not pre-register
a stage whose success metric is "the book is always on."

The current live object remains the control:

- `continuous_ensemble_v1`;
- p3 `.30`, p4p3 `.20`, p4p5 `.40`, tp14 `.10`;
- rmom q25;
- BTC uptrend gate;
- frozen rebalance/hedge object;
- demo/paper only.

## Current Constraints

Use only full-PIT roots:

- `~/SHARED_DATA/bybit_full_pit`
- `~/SHARED_DATA/binance_full_pit`

Current local data gate from W4:

- Bybit data end: `2026-06-02`;
- Binance data end: `2026-04-30`;
- common historical window for both venues: `2023-04-01 <= signal_ts < 2026-05-01`;
- forward replay has `forward_days=0`;
- forward composite, OI/depth/liquidation, and live sniper-fill claims are gated.

## Program Files

- [00_methodology_contract.md](00_methodology_contract.md)
- [01_stage0_candidate_tape.md](01_stage0_candidate_tape.md)
- [02_stage1_score_entry.md](02_stage1_score_entry.md)
- [03_stage2_entry_alpha.md](03_stage2_entry_alpha.md)
- [04_stage3_exit_alpha.md](04_stage3_exit_alpha.md)
- [05_stage4_sniper_alpha.md](05_stage4_sniper_alpha.md)
- [06_stage5_sizing_alpha.md](06_stage5_sizing_alpha.md)
- [07_stage6_interactions_forward.md](07_stage6_interactions_forward.md)
- [08_stage7_path_shape_neutralized.md](08_stage7_path_shape_neutralized.md)
- [09_stage8_regime_response.md](09_stage8_regime_response.md)
- [handoff_prompt.md](handoff_prompt.md)

### Run order vs file order

File numbers are storage order, not run order. The logical dependency is:

1. Stage 0 candidate tape (gate for everything).
2. Stage 1 score-entry **and** Stage 7 neutralized path-shape — path-shape is a
   candidate-scoring feature; its admissible output feeds Stage 1 priority,
   Stage 2 entry style, and Stage 5 sizing.
3. Stage 8 regime-response runs as a parallel track against the binary gate; a
   winner feeds Stage 6.
4. Stages 2-5 (entry / exit / sniper / sizing) on the proven candidate set.
5. Stage 6 interaction matrix and forward gate is always last, regardless of
   file number.

## Research Posture

Be relentless. The continuous book is not "done," and a NULL on one mechanism is
never a reason to stop the program — it is one falsified hypothesis out of an
open set. Generate new, mechanistically distinct hypotheses across entry, exit,
path-shape, regime, sizing, and interaction; run them at full artifact
discipline; kill the losers honestly; bank the survivors as forward-watch
candidates; and move to the next idea. Never give up on finding edge.

Relentless means breadth of *honest* hypotheses, not torturing one dataset.
Every gate in `00_methodology_contract.md` and
`docs/backtesting_errors_we_never_repeat.md` still binds. "Never give up" does
NOT license: moving a threshold after seeing output, reviving a closed mechanism
in its exact failed form, letting one venue carry a claim, or converting a
failed filter into a score by shrinking trade count. Persistence is in proposing
the *next distinct mechanism*, not in re-running a dead one until it passes.

## Non-Negotiable Interpretation

No stage may be called promotion evidence. Historical stages can nominate a
demo/paper shadow only. Forward demo/paper remains the pristine OOS arbiter.

The first implementation target is Stage 0 plus Stage 1. Do not start with
exits, sniper variants, sizing, path-shape, or regime work until the candidate
tape and score-priority accounting are proven reconstructable.
