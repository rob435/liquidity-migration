# Research findings

The durable record of what this repository's strategy research establishes, negative results included.
Evidence grading and promotion: [docs/research/governance.md](governance.md). Evidence rules:
[AGENTS.md](../../AGENTS.md). Failure taxonomy: [docs/research/backtesting_errors_we_never_repeat.md](backtesting_errors_we_never_repeat.md).
Data tiers, roots, and PIT membership: [docs/data.md](../data.md).

## 2026-08-30 — The gate's 4/12/24h triggers, unjudged: paid per trade, capped by the book

**Question (owner).** How does the LLM gate's mechanical trigger family
perform with no judgment at all — and doesn't its hourly clock give the
sleeve far more than the daily system's ~306 trades?

**Method.** The gate's own filter set rebuilt on hourly bars 2021→2026-08-28
(`mechanical_gate_study.py`, session a7ea30ba): top-10 trailing-24h
turnover, 4/12/24h log return ≥ 2.5 σ_daily × √(h/24), window range
location ≥ 0.70, BTC-and-ETH regime on, ATR-14d ≤ 12%, 31 daily bars of
history, one event per symbol-day, 24h re-trigger suppression, the live
path's 7-day per-symbol cooldown after each taken trade. Entry at the next
hourly open; v12's exact exit geometry; 45 bp round trip. Per-trade
economics only — no slots, no vol-parity sizing, no retrace execution.

**Result.** 894 trades (≈3× the daily system's count), **+159 bp/trade net,
t +3.6, 50% win**. The graded cuts reconfirm: rank 1–5 +161, 6–10 +123,
11–30 +48 bp/trade; the 12h window is the richest (+335), the 4h the
thinnest (+102). Per year: positive 2021–2024 and 2026, **negative 2025
(−71 bp/trade)** — a junk year the daily filter did not have.

**Why this does not mean "run it unjudged."** Per-trade parity with the
daily system (~+160 both) plus 3× the count is not additive at book level:
the two streams catch the same pumps (the hourly one ~12h earlier — the
+16 bp/trade shared-pump edge, t 3.76, from the v13 decomposition), so the
*incremental* hourly trades are the non-confirming residue, and the v13
kernel measured the full swap at **−0.37 to −1.44 bp/day vs v12** with real
capacity and sizing. Ten slots mean extra events displace better trades
rather than add. The discriminator between early-confirming and junk is
not in the panel (depth/hour gates reached 62–89% confirm rates and still
failed book-level) — which is the gate's whole reason to exist, and why its
LLM leg is graded forward-only in its ledger rather than backtested.

**The freshness veto, mechanical proxy.** Events whose name already
triggered on ≥2 distinct earlier days within 4: +95 bp/trade (t 1.3, n
414) against +160 (t 4.5, n 1,138) for fresh — the veto refuses the weaker
cohort, agreeing in four of six years; 2024 inverts (stale +245 beat fresh
+166). Right-sized as the ledger's forward A/B rather than a conviction.

**Boundary.** Lane-1 on seen data; next-open entries, flat costs, no book
interaction; the turnover rank is rebuilt from kline turnover rather than
venue tickers. Trade counts are events, not fills.

## 2026-08-30 — v13 exit hunt, round two: three new data sources, nothing binds

**Question (owner).** After the give-back program below closed the
price-path exit families, the owner asked for a genuinely new v13 exit on
genuinely new data — sentiment, positioning, liquidations — plus a
book-level re-grade of pyramiding. Same overlay harness and costs as the
give-back program; same 306-trade 2021→2026-08 v12 ledger.

**Data surveyed.** The crypto Fear & Greed index (alternative.me, 3,129
daily readings, full window); Binance top-trader long/short ratio
(`tt_ls_eod`, the input carry v6's registered whale halving reads;
coverage 2024-04+, 52 of the 306 trades); the precomputed market-adjusted
(residual) momentum panel; Tardis liquidations — **first-of-month free
days only** (44 days over 2023-26), which cannot ground a trade-level exit
rule and was not forced to.

| family | best cell | worst cell | verdict |
| --- | ---: | ---: | --- |
| exit on Fear & Greed ≥ 75/80/85 mid-hold | +0.002 (≥85, fires 7×) | −0.116, t −3.3 (≥75) | dead — cuts winners like every price-path trail |
| hold 2d not 3d when entry-day F&G ≥ 75 | — | −0.033, t −1.5 | dead |
| exit on top-trader ratio drop ≥ 0.20/0.26/0.35 from entry | — | — | non-binding: fires once in 52 covered trades; a 3-day hold is too short for daily positioning to move that far |
| exit when residual momentum < 0 / < −0.10 mid-hold | −0.011 (−0.10, fires 7×) | −0.101, t −3.0 (< 0) | dead — "the move is no longer idiosyncratic" still sells the tail |

**Pyramiding, book-level (the owner's preferred framing).** v12 + add
0.5w at +1×ATR: total +0.600, daily Sharpe 3.15, worst dip −4.9%. v12
scaled ×1.35 to the identical average gross: total **+0.658, Sharpe
3.31**, dip −5.5%. At equal risk budget plain size beats the conditional
add on return and Sharpe (the add's slightly smaller dip is a ~3% MAR
difference, inside noise). Third refutation of conditional adds, now on
the fair test.

**Standing count.** Across the v13 program (6 exit families), the
give-back overlays (5), and this round (4): fifteen mechanism families and
~40 cells against v12's exits, every one dead or non-binding. v12's exit
geometry is measured optimal against everything this desk has tried.
Re-opening exits now requires a new *mechanism class*, not a new
parameterization. Lane-1 on seen data; overlay caveats as below. Artifact:
session scratchpad `v13_new_exits_study.py` (session a7ea30ba).

## 2026-08-30 — Carry re-entry after harvest: profitable, no cooldown

**Question (owner).** The 2026-08-25..29 drawdown's biggest single line was
carry re-buying AGIUSDT days after a +62 USDT harvest and losing −68. Is
re-entering a just-harvested name a structural loss (the closed 118-cell
exit program tested exits, never re-entry cooldowns)?

**Method.** The registered v7 book rebuilt on the cross-venue panel
(`carry_reentry_study.py`, session a7ea30ba); 2,091 held name-days cut
into 1,262 trips (contiguous held runs per symbol); trips split by the gap
to the same name's previous trip and by that trip's outcome.

**Result — the opposite of the week's impression.** Fresh entries (no
prior trip within 14d): +36.2 bp/trip, t +3.7. Re-entries within 7d:
+29.6 bp/trip, t +2.3. The exact AGI shape — re-entry within 7d of a
> +200 bp winning trip — averages **+37.4 bp/trip** (n 44). Per year,
re-entries beat fresh entries outright in 2022 (+29.8 vs +7.7), 2023
(+49.4 vs +22.0), and 2024 (+48.1 vs +6.1), roughly tie 2025, and lag in
2026 (+3.7 vs +49.9 — weaker, still positive). A re-entry cooldown would
have deleted a cohort profitable in every meaningful year to soften one
weak one. **No cooldown.** AGI was one bad draw from a good cohort.

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
