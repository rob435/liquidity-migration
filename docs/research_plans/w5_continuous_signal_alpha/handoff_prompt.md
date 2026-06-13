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

Task:
Start W5 Continuous Signal Alpha Program. The goal is score-based entry
priority, entry alpha, exit alpha, sniper alpha, and sizing alpha with full
artifact discipline. Do not run quick screens.

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
- No unregistered mining.
- No threshold movement after seeing output.
- No single-venue claims.
- No filters disguised as scoring. Score-entry arms must keep comparable breadth.
- Negative controls are mandatory.
- Every result needs event rows, ledgers, R1-compatible monthly returns where
  applicable, effect sizes, fragility diagnostics, and a clear falsifier.
- If a result fails, it blocks only that exact mechanism.

Be direct. Call out bad assumptions before running expensive work.
```
