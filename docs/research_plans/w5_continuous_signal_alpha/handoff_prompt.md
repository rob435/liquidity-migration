# Prompt For Next Agent

Use this prompt verbatim for the next agent.

```text
You are continuing the liquidity-migration continuous research program from the
current repo state. Do not use old chat memory.

First read, in order:
1. STATE.md
2. docs/research_summary.md
3. docs/data_roots.md
4. docs/backtesting_errors_we_never_repeat.md
5. .codex/skills/backtest-integrity/SKILL.md
6. .codex/skills/research-phase-runner/SKILL.md
7. .codex/skills/run-strategy/SKILL.md
8. docs/research_plans/w5_continuous_signal_alpha/README.md
9. docs/research_plans/w5_continuous_signal_alpha/00_methodology_contract.md
10. docs/research_plans/w5_continuous_signal_alpha/01_stage0_candidate_tape.md
11. docs/research_plans/w5_continuous_signal_alpha/02_stage1_score_entry.md

Important current state:
- The daily SHORT sleeve was erased and is not available.
- Continuous is research-stage demo/paper only, not promoted and not paper-ready.
- Never set REAL_MONEY=true.
- Do not push/deploy unless the operator explicitly asks.
- Use full-PIT roots only:
  - ~/SHARED_DATA/bybit_full_pit
  - ~/SHARED_DATA/binance_full_pit
- W4 replacement results are binding context:
  - Stage 0: roots exist but are stale for current forward claims; common local
    historical window ends 2026-05-01 exclusive.
  - Stage 1: exact 25% disaster stop + failed-fade/breakeven overlay rejected.
  - Stage 2: fixed +8% quarter-size sniper historically supported for forward
    watch only; live fills still zero.
  - Stage 3: pre_6h_return, pre_24h_return, and pre_24h_realized_vol are
    admissible only for a future neutralized test; the 97 bps symbol-hash
    negative-control spread is a confounding warning.
- E2 regime work is binding context
  (docs/preregistration/2026-06-12-e2-regime-response-family.md): the
  bounded-threshold "trade more regimes" family is NULL — V1 euphoria cap and
  V2 downtrend quarter-size each lost ~20pp return and roughly halved MAR on
  both venues. The binary BTC-uptrend gate stands as the control. W5 Stage 8 may
  only test a materially different regime mechanism that beats V0 on pooled MAR;
  do NOT re-run V1/V2 in any form, and do NOT make "always trading" or trade
  count a success metric. The objective is pooled MAR vs control on both venues,
  net of funding and costs.

Task:
Start W5 Continuous Signal Alpha Program. The goal is score-based entry
priority, neutralized path-shape scoring (Stage 7), entry alpha, exit alpha,
sniper alpha, sizing alpha, and a regime-response mechanism that beats the
binary gate (Stage 8) — all with full artifact discipline. Do not run quick
screens.

Posture: be relentless and never give up on finding edge. A NULL closes one
hypothesis, not the program — bank the honest verdict and queue the next
mechanistically distinct experiment. Relentless means breadth of honest
hypotheses, NOT re-running a dead mechanism, moving a threshold after seeing
output, or letting one venue carry a claim. Every gate in
00_methodology_contract.md and docs/backtesting_errors_we_never_repeat.md still
binds.

First implementation target:
1. Write a dated preregistration for W5 Stage 0 under docs/preregistration/.
2. Implement a Stage 0 candidate-tape builder that reconstructs the full
   eligible candidate set, not just executed trades.
3. Run both venues on the common full-PIT window:
   2023-04-01 <= signal_ts < 2026-05-01.
4. Prove selected entries reconcile to the frozen continuous control.
5. Emit candidate tape, selected/rejected rows, PIT/root identity, code/config
   hash, and a Stage 0 verdict.
6. Only if Stage 0 passes, write the Stage 1 score-entry preregistration.

Rules:
- No single-venue claims.
- Negative controls are mandatory.
- Every result needs event rows, ledgers, R1-compatible monthly returns where
  applicable, effect sizes, fragility diagnostics, and a clear falsifier.

run expensive work.
```
