# Research findings

The durable record of what this repository's strategy research establishes, negative results included.
Evidence grading and promotion: [docs/research/governance.md](governance.md). Evidence rules:
[AGENTS.md](../../AGENTS.md). Failure taxonomy: [docs/research/backtesting_errors_we_never_repeat.md](backtesting_errors_we_never_repeat.md).
Data tiers, roots, and PIT membership: [docs/data.md](../data.md).

## 2026-09-02 — Eight exit ideas from an outside review, all tested: nothing beats its own control

**Question (owner).** An outside review reframed exits around continuation
value — "is holding still the best use of this risk?" — and proposed eight
mechanisms: (1) replace a held position when a blocked candidate is worth
more, (2) LONG horizons chosen by entry thesis, (3) renew a LONG hold on a
fresh signal, (4) expire LONG on the signal clock rather than the fill clock,
(5) a CARRY continuation band, (6) an Exodus microstructure cover, (7) a veto
of premature Exodus fires before entry, (8) maker-first execution of
scheduled exits. The owner asked for all eight to be tested, and asked how
the recorder's deep tier helps a book that trades crowded small names.
Artifacts (scripts, ledgers, results): `~/SHARED_DATA/bybit_full_pit/reports/exit_program_2026-09-02/`.

**LONG (ideas 2, 3, 4).** The registered v12 ledger rebuilt on the full PIT
root 2021-01→2026-08-29: 307 trades, +0.5278 book units, 253 clock exits,
54 stops; each trade labelled with its trigger legs (199 one-day, 166
three-day, 75 seven-day, 108 with two or more), its source strength, and its
entry route (243 retrace, 64 six-hour deadline). Per-trade overlays replay
each trade's hourly path under the registered stop geometry with a different
hard clock (the simulator reproduced all 307 recorded exits to the tick
before any variant ran), 45 bp round trip, settlement-exact funding:

| clock rule | total Δ vs v12 | trades changed | paired t | years worse (of 6) |
| --- | ---: | ---: | ---: | ---: |
| expiry at signal + 72h (idea 4) | −0.050 | 253 | −2.5 | 6 |
| unconditional 48h / 60h / 84h / 96h / 120h | −0.127 / −0.086 / −0.055 / −0.134 / −0.048 | 253–288 | −0.6 to −2.3 | 5–6 |
| thesis 48/72/96 (1d-only deadline entries 48h, multi-day or multi-leg 96h; idea 2) | −0.098 | 188 | −1.7 | 4 |
| thesis, the two halves alone (short-only 48 / long-only 96) | −0.029 / −0.069 | 27 / 161 | −1.5 / −1.3 | 4 / 4 |
| renewal to 96h on a fresh accepted signal at the day-2 / day-3 / either close (idea 3) | −0.060 / −0.042 / −0.061 | 83 / 58 / 94 | −1.5 / −1.9 / −1.5 | 5 / 4 / 5 |

The controls decide it. The thesis assignment dealt at random 200 times
averages −0.098 — identical to the real assignment (49.5% of random deals
beat it): the thesis carries no information. Random extensions matched to
the renewal counts average −0.033 / −0.028 / −0.041, and 72–83% of them beat
the real renewal: a fresh signal on a held name selects the trades that give
back *more*, not less. Seventy-two hours after the fill is the measured
optimum among unconditional horizons on both sides. Book-level reruns of the
registered pipeline (slots, cooldowns, the Rust reducer's own exit decisions)
confirm the sign and size:

| book-level | total net | daily Sharpe | worst dip |
| --- | ---: | ---: | ---: |
| v12 (fill + 72h) | +0.528 | 1.47 | −4.6% |
| signal clock | +0.484 | 1.40 | −5.1% |
| renewal at day 2 or 3 (99 renewals) | +0.460 | 1.19 | −4.3% |
| thesis 48/72/96 | +0.444 | 1.10 | −5.2% |
| unconditional 96h | +0.393 | 0.93 | −5.9% |

**Slot replacement (idea 1).** The mechanism needs a binding constraint. The
ten LONG slots refused **one** candidate in 5.7 years (724 candidates; 205
refused by cooldown, 211 already held), and the carry book's gross cap
binds on none of its bars (2026-08-07 program). There is nothing to replace
into; the idea is dead by counting, before any value model.

**CARRY continuation band (idea 5).** For every v7 held name-day whose
funding interval is at least 4h (1,206 name-days, 2023→2026-08; shorter
intervals have no valid running-rate proxy from hourly premium bars), every
hour from 1 to 23 is a state: the reconstructed running rate and its 2h
slope, hours to settlement, last print, trailing funding, the day's return so
far, 3h return, 4h open-interest change, vol, persistence, 3d return,
turnover growth — 23,523 states. A ridge model of the remaining-day net
(price plus settlements to the next decision), fit walk-forward by year,
reaches out-of-sample correlation 0.01 (2024), 0.10 (2025), −0.04 (2026),
0.04 pooled. The proposed policy — exit when the model's upper bound is below
zero two hours running — never fires at one sigma; at half a sigma it exits
0.2–1.5% of name-days for +44…+148 weighted bp per year at paired t ≈ 1.0;
the point estimate exits 45–86% of name-days and loses 300–1,500 weighted bp
per year, worse than a random exit at the same rate in 2026. The state has
nothing to say about the rest of the day; this extends the 2026-08-07 and
2026-08-24 closures to model-based continuation.

**Exodus pre-entry veto (idea 7) — not gradeable on the data we hold.** The
fire population was rebuilt from the v7 book (2023-01→2026-07-27, where 1m
bars end): the running rate one hour before each settlement from hourly
premium closes plus Bybit's interest clamp, fire when it reads −3 bp or
better, priced short S−10 → S+60 on 1m opens, 15.56 bp fees, the print paid
when deep. 70% of the settlements inside held days sit in intervals under 4h
and were excluded (no proxy); 560 fires remain. Against the venue's own
displayed rate at S−15 on the 44 tardis free days (168 settlement pairs at
≥4h): the proxy's fire verdict agrees 92% of the time, but 7 of its 49 fires
are false (the display was still deep) — and among the 48 displayed fires the
print stayed deep **once (2.1%)**, against 16% among the proxy's fires and 27%
in the full reconstruction. The premature label the idea wants to predict is
therefore mostly reconstruction error here, not a live phenomenon. The model
result is reported for completeness and reads accordingly: walk-forward ridge
on 18 pre-fire features, out-of-sample correlation with the event's net
−0.00 (n 403); vetoing predicted losers does not beat a random veto at the
same rate (24% of random vetoes beat it in 2025, 90% in 2026); the one
monotone in-sample pattern — fires that barely cleared −3 bp are premature
more often (27% → 9% as the margin rises to 4 bp) — is exactly what proxy
noise produces. Per-year mean net of the reconstructed events: 2023 +21,
2024 −20, 2025 −8, 2026 +6 bp, against the registration's +95 clean mean.
The venue's S−15 rate exists only in the live engine's WAL fires and in the
tape's ticker stream: the veto question grades forward, from those.

**Exodus microstructure cover (idea 6) and maker-first exits (idea 8).**
Both need tick data around real exits. The local tape is one hour of
2026-08-03 on 22 names: 22 scheduled sells per notional, posted at the ask
from T−30m and re-pegged, queue-aware, remainder crossed at T — fully passive
fills, and −21 bp mean against crossing at T (t −1.9) because that hour
drifted +23 bp; the 5-minute buy-to-cover variant +4.5 / +3.6 bp at $100 /
$1,000 (t ≈ 1.0, 78% / 53% filled). Eighty-eight attempts on one hour grade
nothing about either mechanism. The hourly market tape on Google Drive
(ticker funding at tick cadence for every listed name, full books on the
promoted crowded names, every trade and liquidation) is the data both need;
at the registration's ~15 fires a month, a first read of idea 6 is a
quarter away.

**State-driven exits: leave when the state that admitted the trade is gone.**
Every exit above, and every earlier program, keys on the trade's own P&L or
clock. This family keys on the market instead: at each daily decision after
the fill, leave at the next hourly close if the admitting state has gone.
Sixteen cells on the same 307-trade ledger, each against the registered exits
and a placebo that exits the same number of trades at a random alive stamp
(200 draws). Deltas are book units against v12's +0.528.

| Cell | Trades hit | Delta | t on hit | Years worse | Placebo ≥ real |
| --- | --- | --- | --- | --- | --- |
| BTC regime off | 25 | −0.019 | −0.95 | 4/6 | 62% |
| ETH regime off | 20 | +0.018 | 1.95 | 1/6 | 1% |
| either regime off | 38 | −0.003 | −0.12 | 3/6 | 21% |
| volume rank > 10 / 20 / 30 | 106 / 28 / 6 | −0.066 / −0.025 / +0.001 | −1.77 / −1.47 / 0.22 | 5 / 4 / 2 of 6 | 38% / 62% / 39% |
| out of universe | 0 | — | — | — | — |
| funding ≥ 5 / 10 / 20 bp | 42 / 13 / 4 | −0.007 / +0.019 / +0.005 | −0.22 / 1.53 / 1.01 | 2 / 0 / 0 of 6 | 19% / 0.5% / 20% |
| reverse shock, full / half trigger | 4 / 56 | −0.006 / −0.070 | −0.99 / −3.71 | 1 / 5 of 6 | 80% / 83% |
| close in the day's bottom quarter | 136 | −0.100 | −1.78 | 4/6 | 55% |
| regime off or rank > 30 or funding ≥ 20 bp | 44 | +0.005 | 0.23 | 2/6 | 9% |

Two cells beat their placebo, the first exit cells in this desk's record to
do so, and both fail the plateau checks. ETH regime off: three of its twenty
trades carry 73% of the gain; require the state to hold for two consecutive
stamps and the delta is −0.003, lag it one day and it is +0.001, and the same
rule on BTC loses. Funding ≥ 10 bp: three trades carry 105% of the gain
(1000BONKUSDT on 2024-03-01 alone is +0.011); thresholds of 6, 7, 8, and 9 bp
all lose, 10 to 14 bp win, 16 bp fades, the same shape whether the rate is
taken per settlement or normalised to eight hours. A rule that loses at 9 bp
and wins at 10 is a spike, not a plateau. The union of the two cells reaches
t 2.44 on 33 trades and inherits both concentrations. The state does carry
information about the remaining hold: after an ETH-off stamp the rest of the
trade returns −6.0 bp of weight against +8.7 otherwise (t −3.4), after a
funding ≥ 10 bp stamp −12.2 against +8.7 (t −2.6). Nothing is promoted: both
cells sit below the t 2.5 bar on 13 to 20 trades, and even taken at face
value they add 0.3 to 0.6 book-percent a year to a book that made 53 over
5.7 years. Scripts `state_exit_lab.py` and `state_exit_plateau.py`, results
under `long/state_exits/` in the program folder.

**On the recorder's deep tier (the owner's question).** The 81-name file is
LONG's entry universe plus the maker canary; the names CARRY and Exodus trade
are the crowded ones, which change daily and were in the wide tier (no
50-level book). The recorder now promotes any listed name whose funding rate
is at or below −10 bp into the deep tier for that day and the next
(`--deep-funding-bp`), so settlement-window books exist for exactly the names
the two money-making sleeves hold.

**What the program says about the approach.** On the research panel the
registered book earns from the crowd fee: v7 carry +17.2 bp/day mean over
2021-10→2026-08 (raw Sharpe 1.59, +2,193% compounded on the raw daily
series), LONG +64.5% marked daily over 5.7 years at gross 1.0 (Sharpe 1.47),
Exodus a 2025–26 overlay. Every exit family this desk has tried — now
twenty-odd mechanism families and roughly a hundred cells including this
program and its sixteen state-driven cells — loses to the registered clocks
or to its own placebo, or wins on too few trades to survive a plateau check. The lever
that remains is entry and size, not exit. One population caveat carries
forward: the carry panel is the both-venue intersection, so Bybit-only names
such as AGIUSDT (last week's biggest carry loser) are outside every
historical carry and Exodus number.

**Boundary.** Lane-1 on seen data throughout. Overlays re-simulate exits on
recorded trades; the book-level reruns are the registered pipeline at hourly
resolution with next-open entries and flat costs. The Exodus population is a
proxy population with a measured 14% false-fire rate. The execution test is
one hour of one day.

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

**The rank barrier, priced (the HNTUSDT question).** HNT 2026-08-30 is the
case: judged a 7 as a rank-11 mover at 01:05 UTC (0.4215) but the trigger
scan stops at rank 10, so entry came at 09:05, rank 9, 0.6204 — the barrier
cost 8 hours and +47%, on a name already +265% off its 08-16 base (a
12-day grind at $0.1–2M daily turnover that no 90-day-turnover universe
can see, then $56M arrived in one day). Lowering the barrier mechanically
does not pay: rank 11–30 triggers under the live cooldown earn +58
bp/trade (t 2.0, win 44%, 2026 −32), and the pool decomposes into a
lottery — **9% graduate to top-10 within 3 days (+1,805 bp/trade, 89%
win); the other 91% average −126 bp/trade (40% win)**. Conditioning on
graduation is hindsight; nothing in the panel separates the 9% in advance.
Two more cohort facts: in the top-10 set, entries after a >100% 7-day
run-up average **−155 bp/trade (35% win)** — HNT's entry cohort is
mechanically the worst one — and below rank 10 quality drops by roughly
two-thirds at every run-up level. Both say the same thing: what would make
a wider or later funnel work is judgment, not a looser rule. The zero-risk
next step, if wanted: extend the trigger scan's *journaling* to rank ≤ 30
(publication kept at ≤ 10) so the ledger accrues a judged rank-11–30
forward record before any entry barrier moves.

**Boundary.** Lane-1 on seen data; next-open entries, flat costs, no book
interaction; the turnover rank is rebuilt from kline turnover rather than
venue tickers. Trade counts are events, not fills. The graduating-cohort
split conditions on the future by construction and is a diagnostic, never
an entry rule.

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
`lane2_funding_spread_v1` still stands: the corrected v2-era Binance transfer
is near-noise (t 0.4 / Sharpe 0.18 on its own), so the gap's predictive content is
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

## 2026-08-24 — Carry-hold on Binance: positive seen-data replication

**Question.** Does the registered v6 carry rule transfer when only the venue
view changes from Bybit to Binance? The run uses the panel's `BINANCE_VIEW`
over 1,756 days, limits the universe to names present on both venues, and
charges Bybit's conservative 7.78 bp per side. The Bybit control in the same
run reproduces its registered +21.82 bp/day.

**Result.** Binance earns **+10.1 bp/day with raw Sharpe 0.97** over the
4.80-year panel. Binance / Bybit-control net by era is 2021 −0.9 / −0.8 bp/day
(62 thin days), 2022 +4.4 / +4.9, 2023 +6.2 / +9.6, 2024 +2.1 / +4.8,
2025 +17.5 / +48.3, and 2026 +29.2 / +62.0. Every full year is positive,
the pooled Binance result is 46% of the Bybit control, and the positive
calendar-year ratios range from 36% to 90%. Both concentrate their largest
returns in 2025–26. This improves on the corrected v2-era Binance read of +2.7
bp/day and Sharpe 0.18 because v4–v6's sizing rules also help the Binance view.

**Boundary.** The rule and sizing were shaped on Bybit, so this is Lane-1
replication on seen data, not a registered Binance result. The symbols are the
same across both views, so adding Binance diversifies venue exposure rather
than the underlying coin book. The adapter has no live lifecycle evidence;
offline code and a practice realm do not authorize funded trading. The result
supports collecting a Binance forward record, not a production claim.

## 2026-08-30 — v12 mechanics on the forward tape: one reconstructed proxy does not fire

**Question.** Can the recorded public tape exercise the LONG kernel's entry
and stop assumptions? The diagnostic pins the kernel to commit
`9e100d8e7de4ac586526d605dd983e2d36f1533d` and reads 1,862,470 receive-ordered
rows from 17 completed BTCUSDT segments. Both observation windows are
bracketed; there are no sequence gaps, skipped segments, invalid timestamps,
or timestamp regressions. One still-open segment is excluded.

**Result.** Zero registered model trade rows are graded. One signal reconstructed
from the tape closes at 78,137.5, which puts its 1% resting entry at 77,356.125.
Neither the conservative trade-through bound nor the optimistic touch bound
reaches it inside six hours, and the 74,713 stop does not fire. The kernel's
next-hour-open price is 78,253.1, or 115.95 bp worse than the unfilled limit,
but that difference is not execution slippage: there is no tape fill to compare
with it. PENDLEUSDT has completed tape rows but no diagnostic input row, so it
grades nothing.

An explicitly artificial exercise at a 78,000 entry and 77,950 stop checks the
grader itself. That row fills 5.73 hours before the kernel, puts the kernel open
32.45 bp above the limit, and walks 1,000 USDT through displayed bids at 0.975
bp below the stop. Those three numbers belong only to the exercise; they do not
validate v12's actual levels.

**Boundary.** This is a Lane-1 mechanics diagnostic, not an alpha or live-fill
result. The proxy's signal close comes from the tape rather than a registered
kernel trade, and a displayed book omits latency, cancellations, hidden size,
impact, and replenishment. The local report is
`reports/v12-mechanics-tape-20260830-audit/`; summary / per-trade / provenance
SHA-256 are `89d2153710ca4cf29131a7a69c7e60903c1e8707724208fec71587413a16b7f4`,
`a542bca3294f1bd244a24713753ba477f14f64374557fb7754eaba73dd7b7b8d`, and
`881cb64931f82236b492d555c3624e127c3491b531910b7e6a807b5799e177e0`.
The explicit evidence-kind comparison is
`973ef7964aa05407b0e9b5cbfd09760c1701e2b94097a00d970c0423fc27fa76`.
The private tape input is retained under
`SHARED_DATA/research_evidence/2026-08-30-parity-tape/` as a mode-0600 archive
with SHA-256
`c7c868057f3a5d80554d5c4b64f322b17f6c59e6ed42fb68ed2e43a72f0419ab`;
it is deliberately not in Git.

## 2026-08-30 — LONG live versus model: the drawdown week is not net-gradeable

**Question.** Why did the demo LONG path differ from the registered v12 model
over `[2026-08-23, 2026-08-30)`? The supplied model ledger is labeled with
source commit `9e100d8e7de4ac586526d605dd983e2d36f1533d`; the checker hashes the ledger but
cannot independently prove that origin. It then joins producer transitions,
8,997 retained cycle payloads, the engine's long-sleeve journal, 936 Bybit
closed-PnL rows, and 2,301 unique transaction rows. Seven exact duplicate
transaction IDs are removed before accounting.

**Result.** One ENAUSDT model/live pair is structural, not a net observation:
the signals are one day apart, the model exits at its take-profit while live
waits for a time stop, and the engine entry journal has rotated away. The
nearest venue close belongs to a distinct 1,587-unit position opened after the
prior position went flat. Its one −0.00496098 USDT settlement exactly explains
the venue-position closed-PnL residual, but no engine or producer order links
that position to the paired sleeve trade. The earlier −2,786.5 bp comparison is
withdrawn; the paired price-and-fee gap is **not gradeable**.

The unmatched cohorts stay visible. PUMPFUNUSDT is a state/path divergence,
not a missed live execution: the live book entered on the prior day's signal,
remained open through the model's signal and entry, and closed 89.481 seconds
after the model entry. The hypothetical model path loses 1,361.0 bp. The
gate-only AAVEUSDT trade loses 918.52 bp from price and fees; its engine round
trip exactly matches the flat-to-flat venue position, so six explicit
settlements add −4.27 bp and put its all-in result at **−922.79 bp**. There
are no unexplained live-only trades in the window.

**Boundary.** This is a post-seen Lane-1 reconciliation, not a forward grade.
The model replay is tainted: its full point-in-time membership check falls back
to the current universe with 274 required date-symbol rows missing. ENA lacks
the order/accounting links needed for a live net, and one structural pair cannot
establish parity. The report is
`reports/long_live_vs_model_2026-08-23_2026-08-30/`; its final hashes live in
`long_live_vs_model_provenance.json`. The private live inputs are retained
under `SHARED_DATA/research_evidence/2026-08-30-parity-tape/` as a mode-0600
archive with SHA-256
`4088e1f9b018578cbf37a9893758e1acbc0f73b82bbd8b8df5091b0a2e69bd97`;
they are deliberately not in Git, so reconstruction also needs that local
archive. The ignored parity and tape report directories are preserved together
in `generated-reports.tar.zst` with SHA-256
`9e5c3feb893eb4c6ed638a0e79095d906ff942a3c21b9e7163c1647b03de299c`.
