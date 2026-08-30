# Research findings

The durable record of what this repository's strategy research establishes, negative results included.
Evidence grading and promotion: [docs/research/governance.md](governance.md). Evidence rules:
[AGENTS.md](../../AGENTS.md). Failure taxonomy: [docs/research/backtesting_errors_we_never_repeat.md](backtesting_errors_we_never_repeat.md).
Data tiers, roots, and PIT membership: [docs/data.md](../data.md).

## 2026-08-30 — LONG give-back and the exit overlays: measured, all lose

**Question (owner).** The demo curve fell 1,757 → 1,655 USDT over 2026-08-25..29
(−5.8%). The owner's read: LONG catches the initial pump, then holds through
the retrace and gives it back — can a retrace-harvesting exit, or an inverse
(short) overlay, do better?

**What the week actually was (venue receipts, signed reads of the demo
account's closed-PnL and transaction log).** Realized closed-PnL over the five
days was −102.6 USDT: carry −119 (−76 after its +42.5 funding income), LONG
−44, exodus +17. LONG's two losers were different faults: AAVE (−20) was an
LLM-gate entry scored 7 on its third consecutive mover-day after a +45%
three-day run, bought within 2.5% of the top (peak excursion +1.6%, trough
−12%) — a chase, and the gate has no freshness-versus-move-age rule; ENA
(−24) peaked +17.7% mid-hold and exited −9.7% at the clock — the structural
give-back. The registered v12 rule itself had a *profitable* model week: its
2026-08-20..22 entries took profits and its own ENA trade exited +0.15%
(entry 0.1508, exit 0.15631) where the live book realized −9.7% — the live
loss came from later, higher entries and a ~21h-later exit clock, not from
the rule.

**The give-back, quantified (Lane 1).** Fresh v12 run 2021-01-01→2026-08-30
on the full-PIT root, 306 trades, +48.3% net: median trade peaks +8.6% and
gives back 6.8 points; the sum of weight-scaled give-back (+1.05 book units)
is 2.2× the sleeve's entire net (+0.48). Half of trades reach ≥1.5×ATR of
favorable excursion. The give-back is real and large.

**And no overlay harvests it.** Per-trade exit overlays on the recorded
trades, hourly intra-hold paths from klines_1h, 45 bp round trip (the v13
lab's 3× cost model), funding pro-rated; recorded exit kept when a variant
never triggers. Every cell loses vs recorded v12, most in every calendar year:

| family | cells | total Δ vs v12 | per-trade t |
| --- | --- | ---: | ---: |
| chandelier trail (k=1..2×ATR off the running high, armed at 0/+1/+2 ATR) | 6 | −0.063 to −0.386 | −2.9 to −5.8 |
| breakeven ratchet (stop→entry once +1/+1.5/+2 ATR) | 3 | −0.060 to −0.200 | −2.6 to −4.6 |
| half scale-out at +1.5/+2/+3 ATR | 3 | −0.020 to −0.101 | −1.4 to −3.7 |
| short at v12's exit, 24/48/72h hold, 2×ATR stop | 3 | −0.016 to +0.035 | −0.2 to +0.4 |

The short overlay's pooled ≈0 hides the era split: +0.15..+0.16 of its total
sits in 2022 alone and it loses in 2023, 2024, 2025, and 2026 separately —
it is short-beta in a bear year, not an edge on these names. Post-exit these
names keep drifting up, the same "crowding continues" wall as the v13 PV
factor. Pyramiding into strength (+0.5w once +1×ATR) tested +0.094, t +3.5 —
and died against its control: unconditional extra size from entry earns the
same per unit of capital-time (0.041 vs 0.038), so the condition carries no
information; that lever is the multiplier, already at 6.0.

**Structure.** 34 take-profit exits carry +0.38 of the +0.57 gross; the
131 three-day-clock exits hold +0.19 gross against +0.46 of give-back. The
give-back is the price of the tail, and cutting it costs more than it saves —
consistent with, and extending, the v13 exit grid (six families, all dead).

**Non-conclusions and boundary.** Overlays re-simulate exits on recorded
trades: earlier exits do not re-open slots, and funding inside a shortened
hold is pro-rated, so these are Lane-1 numbers on seen data, not a graded
config. The trade ledger does not record which trigger leg (1d/3d/7d) fired,
so "stale-move entries underperform inside v12" is not answerable from the
ledger as written. Since the 2026-08-24 merge, gate-sourced entries are
indistinguishable from native entries in every persisted record — the gate's
forward record cannot currently be separated from v12's. Artifacts: session
scratchpad `exit_overlay_study.py` + `exit_overlay_results.parquet`, v12 run
`fullbt/long/long_native_trades.csv` (session a7ea30ba); venue history pulled
2026-08-30 via signed read-only closed-pnl/transaction-log queries.

## 2026-08-30 — Directional toxic-flow protection: first forward sample

**Forward evidence.** The first funded run after registration produced 10
feature-bearing `maker_canary` fills on AGIUSDT. All 10 were maker fills. At
the stop point their notional-weighted all-in arrival cost was +8.81 bp and
their signed one-minute markout was -14.52 bp: the price moved against the
fill. The surrounding-state receipts averaged flow score 0.43, same-side
depth ratio 0.09, 92.34 USDT nearby same-side depth, 11.73 bp spread, 1.26 bp
short movement and 30.11 USDT estimated queue ahead.

**Boundary and decision.** This sample did not reach the registered 30-fill or
60-minute boundary. It stopped at 10 fills because the final 10 AGI of
inventory was below the ordinary close minimum, exposing an execution fault.
The rule stays registered but quoting is disabled. Ten fills cannot establish
economics, and the operational stop makes this a diagnostic rather than a
completed grade. The full historical quoter log is descriptive only: 31 fills,
92% maker share, 11 closed trips, no winning trips and -0.16 USDT net; it mixes
earlier rules with the registered one and therefore cannot grade this config.

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
