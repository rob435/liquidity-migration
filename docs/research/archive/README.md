# Archive — dated research runs

The underlying run notes, newest last. Each one is a single day's Lane-1
exploration on already-seen data: it *selected* a result and therefore cannot
grade it. Registered configs cite these by section for provenance.

Read [`docs/research/research_findings.md`](../research_findings.md) first — it is the
durable summary of what the evidence supports, including the negative results.
Come here for the full tables and the reasoning behind a specific number.

| Run | Question it answered |
| --- | --- |
| [2026-07-24 anomaly research](2026-07-24-anomaly-research.md) | 37 mechanisms under one harness; two survived every screen |
| [2026-07-26 financed longs](2026-07-26-financed-longs.md) | Can a long book beat the deployed CONTINUOUS system after full round-trip fees? Registered three configs; 22-row negative ledger |
| [2026-07-27 continuous ladder mechanism](2026-07-27-continuous-ladder-mechanism.md) | Why did the retired 3-cell ensemble work, and can `single_fund0` replicate it? |
| [2026-07-28 carry-hold quant review](2026-07-28-carry-hold-quant-review.md) | Six falsifiable theses against `lane2_carry_hold_v1` on the corrected settlement-exact scorer; registered v2 |
| [2026-07-30 idio charts](2026-07-30-idio-charts.md) | Do chart signals work better on idiosyncratic price paths than raw ones? |
| [2026-07-31 trend filters and persistence](2026-07-31-trend-filters-and-persistence.md) | Does excluding downtrending coins pay? No — a screen pays only where entry is unselective. Closed two of the four untested doors, and found crowding persistence, which works as a **size** and not a screen. Registered v4 |
| [2026-08-01 settlement sawtooth program](2026-08-01-settlement-sawtooth-program.md) | The price pattern around funding payments. CLOSED: the step is arbitrage-free by construction and every trade tried there is dead; two durable bounds (unhedgeable price leg, zero-latency exit) survive it |
| [2026-08-03 strategy program change log](2026-08-03-strategy-program-change-log.md) | Dated change points 2026-07-26..2026-08-03 and the 2026-07-25 phase record, decanted verbatim so the active program file stays small |
| [2026-08-21 LONG v13 rework](2026-08-21-long-v13-rework-program.md) | Can anything mechanical capture the early-entry edge on confirming pumps? 25 cells dead; v12 stands |
| [2026-08-21 LLM gate window lab](2026-08-21-llm-gate-window-lab.md) | Which of the gate's detection windows, turnover depths, and exits survive 5.5 years of hourly bars? Cut 1h/2h, cut rank 30 to 10, killed the take-profit, and found a scam pump is a liquidity fact rather than a shape one |

## What is not here

Superseded audits, preregistration receipts, roadmaps, and redesign notes were
removed rather than archived — they are recoverable from Git history, and a
note that cites one names the commit that deleted it. Nothing in this directory
carries operational authority: for live state read
[`STATE.md`](../../../STATE.md), and for the active research queue read
[`docs/research/strategy_program.md`](../strategy_program.md).
