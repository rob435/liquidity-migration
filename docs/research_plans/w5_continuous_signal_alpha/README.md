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
- entry execution alpha;
- exit lifecycle alpha;
- sniper add-on alpha;
- sizing/risk-budget alpha;
- interaction and forward-demo validation.

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
- [handoff_prompt.md](handoff_prompt.md)

## Non-Negotiable Interpretation

No stage may be called promotion evidence. Historical stages can nominate a
demo/paper shadow only. Forward demo/paper remains the pristine OOS arbiter.

The first implementation target is Stage 0 plus Stage 1. Do not start with
exits, sniper variants, or sizing until the candidate tape and score-priority
accounting are proven reconstructable.
