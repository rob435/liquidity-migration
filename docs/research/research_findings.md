# Research findings

The durable record of what this repository's strategy research establishes, negative results included.
Evidence grading and promotion: [docs/research/governance.md](governance.md). Evidence rules:
[AGENTS.md](../../AGENTS.md). Failure taxonomy: [docs/research/backtesting_errors_we_never_repeat.md](backtesting_errors_we_never_repeat.md).
Data tiers, roots, and PIT membership: [docs/data.md](../data.md).

## 2026-08-29 — Directional toxic-flow protection: Lane-1 selection

**Question.** Does scaling public aggressive trades by the displayed dollars
they can consume identify the side of a two-sided quote most likely to be
picked off? `scripts/research/compare_toxic_quoter.py` replayed 57,364 paired
quote opportunities across 34 Bybit names on 2026-08-03 and 2026-08-04. Every
arm saw the same causal L50 and public-trade stream. Displayed queue stood ahead
of the simulated order, only public trades advanced it, and fills were marked
15 seconds later. The artifact is
`reports/toxic-quoter-forward-20260829.json` (gitignored), SHA-256
`9c27d5f64ce23e5da095d2e922501f26792d1b6de37be9a4db3ed561bc75c1d1`.

**Grid.** Net is basis points per quote opportunity with an available mark,
after the four-basis-point round-trip maker-fee assumption. Improvement and
its t score are paired against the fee-corrected no-flow control. The
`current` row retains the installed two-basis-point fee setting and is not a
like-for-like economic control.

| Arm | Fill rate | Net bp/quote | Paired improvement | Paired t |
| --- | ---: | ---: | ---: | ---: |
| current, old symmetric response and 2 bp fee | 3.78% | -0.169 | +0.078 | 10.77 |
| fee-corrected, no flow response | 3.66% | -0.248 | 0 | — |
| one-sided widen 1 bp/score | 3.35% | -0.227 | +0.020 | 5.98 |
| one-sided widen 2 bp/score | 3.08% | -0.209 | +0.038 | 8.19 |
| one-sided widen 4 bp/score | 2.60% | **-0.171** | **+0.076** | **11.75** |
| widen 2, pull attacked side at score 1 | 2.58% | -0.182 | +0.065 | 8.41 |
| widen 2, pull attacked side at score 2 | 2.96% | -0.207 | +0.040 | 7.33 |

The selected four-basis-point response improves both dates: +0.058 bp/quote
(t 6.09) on 2026-08-03 and +0.089 (t 10.12) on 2026-08-04. Its 15-second mark
coverage is 99.67%. The hard-pull score-1 arm is the runner-up: it avoids more
bad fills but gives up enough good ones to finish 0.011 bp/quote behind the
selected widening rule.

**Decision and evidence boundary.** Register the four-basis-point one-sided
widening rule as `lane2_toxic_flow_quoter_v1` and grade its tiny funded canary
only on fills after that file's commit. This result establishes that the
feature selects less-toxic quotes on these two seen dates. It does **not**
establish a profitable maker: every full-fee arm is negative. The same tape
both shaped and graded the grid, independent quote opportunities omit the
inventory path, and public depth cannot reveal exact venue queue position.
The forward recorder supplies unseen L50, trades, mark/index price, the crowd
fee (funding), open interest and liquidation events so later changes can be
shaped without reusing these two grading days.

## 2026-08-26 — Cross-venue funding gap: Lane-1 exploration

The "trade the venue where funding is most negative (Bybit as reference)"
idea was probed on seen data only — it grades nothing. The registered v7
carry-hold book's held name-days (n=1,957, both-venue reads fresh) were
split by `funding_diff_bp` (= bybit minus binance settled funding, bp/day).
Script: `scripts/research/demo_funding_gap_diagnostic.py`; artifact:
`reports/demo_funding_gap_diagnostic.json` (gitignored).

**Result.** The gap is predictive of the book's own per-name-day net, in the
direction the idea expects — but it is *the Bybit book's* edge, not a
separately capturable cross-venue alpha:

- baseline (all held): net +16.5 bp/name-day, t +3.76
- Bybit much deeper (gap < −40 bp/day): net +51.5, t +1.95
- venues agree (−10..+10): net +6.8, t +1.93
- Bybit negative **and** Binance negative: net +17.1, t +3.77 (n=1861, 95% of
  the book) — vs Bybit negative and Binance non-negative: net +4.9, t +0.30

**Interpretation.** The deep-gap cohort is the book's best subset, and the
edge concentrates where the venues *diverge* — that is consistent with
"route to the venue that pays most." But the caveat that killed
`lane2_funding_spread_v1` still stands: Binance funding alone is near-noise
(t 0.4 / Sharpe 0.18 on its own), so the gap's predictive content is
**Bybit-depth carrying the load, not a second venue you could trade into**.
The deep-gap era split is thin and one-sided: 2022 is sharply negative
(−96.6 bp/name-day, n=14) and 2024-26 carry it — so this is a regime
feature of the structural funding inversion, not a stable property.

**Non-conclusion.** This is Lane-1 selection evidence, not a graded result.
It does not establish a separately capturable cross-venue edge, and it does
not reverse the `funding_spread_v1` deletion reasoning (that config deleted
2026-08-19; cross-venue replication on corrected accounting measured t 0.4).
The one live question it leaves: whether "*both* venues negative" (a
convergence signal) predicts anything *in addition* to Bybit being negative
— the n=1861 vs n=96 split above suggests it mostly does not.

