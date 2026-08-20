# Research findings

The durable record of what this repository's strategy research established, replacing six dated documents.
Current interpretation: [docs/research/strategy_program.md](strategy_program.md). Evidence rules:
[AGENTS.md](../../AGENTS.md). Failure taxonomy: [docs/research/backtesting_errors_we_never_repeat.md](backtesting_errors_we_never_repeat.md).
Data tiers, roots, and PIT membership: [docs/data.md](../data.md).

**Reading these numbers.** All screening figures are Lane-1 selection on seen data and grade nothing. Any
pre-2026-07-28 figure whose funding leg came through the cross-venue panel is non-citable unless re-derived
on the corrected scorer (§3).

## 1. What currently looks real

**The one durable premium: funding-financed long-side liquidity provision** — long the names whose shorts are
paying. The only mechanism of ~45 screened that clears an honest cost bar, and positive in every era.

| construction | bp/day | t | Sharpe | worst 1% | max DD | neg eras |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Bybit top-100 decile carry | +34.09 | 3.05 | 1.34 | −18.61% | 77.9% | 0/6 |
| Bybit top-300 decile carry | +24.59 | **3.96** | 1.75 | −10.03% | 58.8% | 0/6 |

Top-300 clears the 144-cell Bonferroni line (t ≈ 3.58) on a plateau, 30/30 era-size cells positive. It is
uninvestable on drawdown, not significance: 58.8–275.7% max DD with −10% to −35% worst-1% days on a 2× gross
book is a liquidation sequence. Magnitudes stale post-fix; shape is not. **You are paid for holding the risk
nobody wants and not paid for the hedged version anybody can run** — the premium compensates idiosyncratic
liquidation risk with a named counterparty, which is why the easy construction was arbitraged out by 2022.

| registered config | bp/day | t | Sharpe full / bench | max DD | turnover |
| --- | ---: | ---: | ---: | ---: | ---: |
| [carry_hold_v4](../../configs/lane2_carry_hold_v4.json) | 22.19 | 3.61 | 1.64 / **1.88** | −24.5% | 0.119 |
| [carry_hold_v1](../../configs/lane2_carry_hold_v1.json) | 18.0 | 2.31 | 1.02 / **1.21** | −60.0% | 0.271 |
| [carry_hold_v2](../../configs/lane2_carry_hold_v2.json) | 16.7 | 2.47 | 1.09 / 1.35 | −48.6% | 0.198 |
| [carry_hold_v3](../../configs/lane2_carry_hold_v3.json) | 19.83 | 3.13 | 1.38 / 1.71 | −28.7% | 0.156 |
| funding_spread_v1 (**config DELETED 2026-08-19, operator override** — numbers kept as the record) | 5.08 | 2.92 | 1.34 / 1.61 | −16.7% | 0.087 |

- **v4 is scored over 1,756 days, not 1,894** — it does not trade early-2021, so its row above is not
  directly comparable with v1–v3's. On the shared spine v3 reads 21.12 bp/day and Sharpe 1.41. v4's own-capital
  differential vs v3 is **+1.07 bp/day at t 0.47, not significant**; its registered claim is capital efficiency
  (mean gross 0.0948 vs 0.1362) and the capital-normalised differential **+10.76, t 3.23**. At v3's capital
  v4's worst dip is 33.5%, *worse* than v3's; at its own capital it is 24.5%, better. Sharpe and MAR are
  scale-free and improve either way. v4's bench 1.88 is the first carry-hold render above the 1.84 benchmark —
  seen-data selection, so it grades nothing.
- v1's bench **1.21 (t 2.31)** is the citable figure and does **not** beat the CONTINUOUS benchmark (1.84, §3).
  Two CONTINUOUS Sharpes appear in this document and they are different renders: **1.84** is the retired
  sl35 research render used as the comparison benchmark; **1.45** is the shipped book's forward figure.
  Compare 1.21 against 1.84, not against 1.45.
- v2 is risk reallocation, not mean improvement — same P&L from a third less average capital (mean gross 0.24
  vs 0.37); paired differential vs v1 −1.0 bp, t −0.4. v3 adds three filters, each grounded in a measured loss
  cohort: moderate grind-down (trailing 3d return in [−30%, −5%) earns −114 bp/name-day at Sharpe −2.1), dead
  names (30d vol < 5%/day, −77 bp/nd), post-recovery holds (−54 bp/nd). Its registered forward experiment is
  the paired differential vs v2: +3.09 bp/day, t 1.64.
- funding_spread took the same premium market-neutrally (long the perp on the more-negative-funding venue,
  short the same symbol on the other): correlation with v3 **+0.09**, and decision-clock stable unlike the
  directional family. Basis risk concentrates where it trades — the cross-venue price difference runs 40 bp/day
  sd normally and 677 bp/day when |spread| > 50, which is why its standalone Sharpe was 1.3 and not 3.
  **Deleted 2026-08-19 with the financed-leaders pair, operator override** — everything that is not
  carry-hold or LONG left the repo; old forward-ledger rows remain as receipts.
- **Two-book portfolio** (Lane-1 selection on seen data — this grades nothing), PIT 60d vol-parity: bench Sharpe **2.34** (max DD −11.2%, MAR 5.07), 1.93–2.34 across
  clocks; full 2021-inclusive window 1.55–1.87. The Sharpe ≥ 2 target is met on the standard quote basis and
  not on the strictest basis. Both numbers are the claim.
- **Unconditional Sharpe 2 is not honestly reachable for the directional book.** ~95 cells plateau at 1.4–1.55.
  On the PIT deep-funding half of days it runs at conditional 2.15–2.35; on the shallow half it is EV-noise
  (1.4–5.0 bp/day). A conditional-2.25 stream active half the time pools to ~1.6 and no per-day scaling changes
  that — which is why the answer was a second P&L identity, not a threshold.

**Momentum.** `momentum_1w` is **continuation** (long recent winners), not reversal — the sign was backwards in
earlier drafts: continuation +39.55 bp/day (Sharpe 1.20) vs reversal −40.73 (−1.23). Best-behaved real signal
found, never cleared: +30.48 bp/day t 2.10 Bybit vs +13.64 t 0.99 Binance (ratio 0.45, outside the [0.5, 2.0]
kill band); best-tuned cell t 2.78 against t ≥ 3.25. It was the momentum leg of the
premium/momentum blend, **deleted 2026-08-19 by operator override** after the blend
lost the portfolio test (carry+LONG Sharpe 2.15 alone, 1.99 with the blend added);
the premium leg was already dead. Do not rebuild either leg as a book.

**CONTINUOUS** was retired from the forward routes by owner override 2026-07-29 (`CONTINUOUS_SLEEVE=off`);
no kill criterion tripped, so the frozen journal is a retirement artifact, not
a dead run. Citable baseline for the shipped shape: **+11.06% / max DD −1.84% / Sharpe 1.45 / MAR 1.80**, 655
trades, 2023-03-13 → 2026-07-16. Five load-bearing parameters, all in
`continuous_profile.py` and
`continuous_events.py` (both deleted from the tree in the 2026-08-14 cleanup; git history holds
them): trigger `turn3_pop3`, age 240d,
settled-funding floor 0.0, crowd-2, hold 24h, plus the BTC uptrend gate and BTC+ETH hedge.

- **The gate is half the strategy and the funding floor is an economic boundary, not a searched threshold.**
  Gate off → 0.93 with the funding bill tripled and drawdown doubled; gated 2.18. No funding filter 1.88 →
  ≥ 0 **2.18** → ≥ +1 bp 1.65, keeping 98% of return on 17% fewer trades at half the max DD. Pump-fading pays
  in uptrends only, and a pump with negative funding is a crowded short.
- **The retired 3-cell ensemble was amplitude weighting, not scale-in or TWAP** — 92.5% of base pumps also
  triggered the second rung in the same hour, median rung lag 0.0h, median entry-price difference 0.00%. Its
  Sharpe 1.84 decomposes into four non-signal channels: the funding bill it kept paying, gated beta from the
  hedge, an impact-slicing artifact (impact scales by weight^0.5 per slice, so splitting one pump across three
  books under-prices it), and calendar density (646 vs 554 active days). Do not rebuild it.
- **Only surviving lead — venue-scoped admission**: apply the funding floor only to both-venue symbols, admit
  Bybit-only contracts regardless of sign. Hedged +12.76% / −1.75% / MAR 2.09 against the shipped
  +11.39% / −1.84% / 1.78 in the same render, the only variant better on every axis — but the delta is 43
  trades over 28 symbols, carried entirely by 2025 (+1.31 pp) while losing 2024 (−0.22) and 2026 (−0.28).

**Short-leg construction.** A basket short halves the tail for 2.4% of the return: decile short 39.55 bp/day,
Sharpe 1.20, worst day −35.37%, max DD 83.6% vs basket 38.62 / 1.55 / −17.50% / 64.4%; half-and-half is the
dial, and the worst name in the decile leg averages −7.65%/day and reaches −90.13%. Standalone the basket short
is dead (§2), so this is a construction principle, not a strategy.

**Execution.** Fills price as taker: 7.78 bp/side notional-weighted, **15.56 bp round trip**, 3.89× the 4 bp
maker assumption the anomaly and blend work used (`cross_section.MEASURED_ROUND_TRIP_BP`, also `summary`'s
default `cost_bp`). `is_maker` is false on all 87 journal fills — a venue label, not an inference — and 88.3%
of exit notional left through native stop triggers (24 of 30 exit fills `CreateByStopLoss`), taker by
construction and unreachable by chase logic. Markouts show no adverse selection: effective spread 3.76 bp
against a 4.91 bp quoted spread, realized spread to the passive counterparty +25.7 bp at 1 m, leaving ~25–30
bp/side of maker-first headroom (23 fills, 1,120 USDT: small). Chase-then-cross is the right shape and the
passive fill rate is the binding constraint — arm B chases at 4.80 bp/side against a 5.50 taker control but
fills 2 of 8, implying 2.70 bp/side; raising the fill rate 25% → 80% moves a book 14.43 → 23.47 bp/day. The
passive floor is 5.40 bp round trip, so 4 bp was never reachable, and arm B's strict-crossing model grants no
queue credit, so measured rates are lower bounds
(the retired paper owner's `passive_execution.py`, removed 2026-08-03). Capacity is small: v3's held names have
median $33M trailing-24h turnover ($3.2M at p05), and p95 entry participation crosses 1% at a ~$1.1M book and
5% at ~$5.5M post-2025.

**The registered A/B's read thresholds, and why nothing is accruing.** H: for CONTINUOUS entries, a post-only
limit at the touch with a bounded chase-and-timeout fallback reaches ≥ 60% passive fill rate and improves
all-in cost by ≥ 10 bp/side against market-IOC, without degrading signal capture (fills inside the same
decision hour). Any read beyond mechanics needs ≥ 100 **fills** per arm — not the standalone demo probe's
≥ 100 **attempts** per arm (`REGISTERED_MIN_ATTEMPTS_PER_ARM` in
[passive_fill_probe.py](../../liquidity_migration/research/execution/passive_fill_probe.py), a separate instrument with its own
40% fill-rate kill); the two are easily confused. Kill rule: stop early if arm B's missed-fill opportunity
cost exceeds its measured cost saving over any 50-entry window, where **missed-fill opportunity cost** is the
signal P&L of entries whose passive order never filled, measured at the decision-hour close.

**The A/B is retired, not graded.** The in-flow instrument (arm B in the retired paper owner's
`passive_execution.py`) made an entry arm-eligible only when `sleeve == "continuous"`, and CONTINUOUS was
already off on both routes when the paper fleet itself was retired 2026-08-03, with the sample stuck at 2/8
fills. The measured numbers above (5.40 bp passive floor, 15.56 bp taker round trip) stand; grading the flow
would need a new in-flow experiment on a live sleeve.
[execution_cost_model.py](../../liquidity_migration/research/execution/execution_cost_model.py) has no arm grouping, so the cost
report does not split by arm.

**The quote lab measured the mechanism with real orders (2026-08-03 overnight, registered `b7ecca4`).**
On a second, separate demo account, the first completed arm (Buy, reprice 15s, timeout 120s, 34 symbols,
75 minutes, n=1,586 attempts) reached a **70.4% passive fill rate** (1,097 fills; 16.8% rejected
would-cross, 12.3% timed out), median time-to-fill 41.6s, clean intention-to-treat all-in cost **mean 5.15 /
median 1.91 bp/side** against the 7.78 bp taker basis. Fills accrue roughly linearly through the window
(39% of attempts by 60s, 63% by 120s), so the window length is set by runtime tolerance, not by the fill
curve flattening. Caveats recorded at registration: probe attempts sample ordinary moments, not the fleet's
entry moments, and quoting loses on tight-spread books — which is why the shipped entry path gates on at
least two ticks and ~1 bp of quoted spread. **Shipped 2026-08-04**: both account owners now rest entries at
the touch on exactly this arm's recipe with a bounded cross at the deadline (change point in
`strategy_program.md`); entry fills journal `is_maker` per fill, so the live maker share accrues as
receipts from the first night.

**The full night is fitted (2026-08-04: eight segments, n=12,656 attempts — all six arms plus a base-arm
repeat).** One uniform accounting throughout: a fill costs its observed price against the decision mid plus
the observed 2.0 bp maker fee; an unfilled or rejected attempt is priced as if we then paid up at the
end-of-window touch plus the 5.5 bp taker fee. Per arm (fill rate, all-in mean cost per side):

| arm (side, reprice, window) | fill % | time-to-fill (median) | all-in mean | taker basis |
| --- | --- | --- | --- | --- |
| Buy 15s/120s — shipped (runs 1, 2) | 69.2% / 71.2% | ~42s | 4.56 / 4.63 bp | 7.1 bp |
| Sell 15s/120s (runs 1, 2) | 62.2% / 67.3% | ~41s | 4.99 / 4.10 bp | 7.0 bp |
| Buy/Sell 30s/180s, chase 4 | 78.6% / 79.3% | ~42s | 4.68 / 4.41 bp | 7.1 bp |
| Buy/Sell 10s/60s, no chase | 53.3% / 45.0% | ~30s | 5.52 / 5.93 bp | 7.2 bp |

Three decisions follow. (1) **The Sell side works as well as the Buy side** — the mechanism is now
validated for the short entries carry actually makes. (2) **The shipped 15s/120s recipe stays.** The
30s/180s arm ties it on cost (the differences are inside one standard error at these sample sizes); its
higher passive share is paid for with worse per-fill prices (2.3 vs 1.8 bp) and more price movement against
the fill afterwards, and a 180 s window does not fit inside the account owner's 120 s sibling-batch
freshness budget — it is the measured-but-unadopted candidate if that budget is ever widened. The
10s/60s no-chase arm is rejected: fills collapse below 55%. (3) **No isolated reprice-interval read
exists** — the 30 s reprice only ever ran with the 180 s window, so nothing can be claimed about reprice
cadence alone. The live fleet should do slightly better than these numbers: where the lab rejected a
would-cross quote and waited out the window, the fleet sends a market order at decision time instead,
which on average beats paying up at the drifted end-of-window touch. First live maker-share grade: none
yet — carry's 2026-08-04 decision was legitimately cash (demo decided identically on a healthy
100-symbol universe) and LONG made no entries, so the first funded receipts wait for the first
non-empty book.

**Size capacity (2026-08-04, measured from the overnight order-book tape — no orders placed).** The
lab's clips were 5–10 USDT; the owner asked whether the mechanism holds at a few hundred to a thousand.
One hour of full depth tape across 22 lab symbols answers the scale question. At the liquid end
(BTCUSDT: ~217,000 USDT resting at the touch) a 1,000 USDT clip is invisible — but one-tick books are
gated to market orders anyway. On the illiquid half, where the quoting earns its ~2.5 bp, the ENTIRE
resting queue at the touch is 23–181 USDT (medians; 25th percentiles 6–94). So today's per-name entries
(~80–200 USDT at current equity) already join the level as roughly half of it, while a 500–1,000 USDT
clip would be 3–30× the whole displayed touch on most of these names: the order becomes the market,
the queue-wait bound ((queue + clip) ÷ same-side print flow) runs 2–26 minutes against the 120 s
window, and the bounded cross at the deadline would then have to walk a book whose first level holds
~50 USDT. Two conclusions and one refusal follow. (1) The shipped mechanism is correctly sized for the
current envelope and does not need retuning for it. (2) It does NOT scale much past ~1–2× today's
clips on the illiquid half — big entries there need slicing. **Built the same day at owner direction
("prepare for big sizing, up to 5,000 USDT notional"):** each quote window now rests at most the
quantity already displayed at the touch (floored at 100 USDT per window so a near-empty book cannot
stretch one entry into hundreds of windows), the command ends its window with the shortfall
un-ordered, and the owner's convergence machinery plans the next window — a retry that made progress
no longer spends the retry budget, so a 5,000 USDT entry arrives as a sequence of touch-sized windows
(~2.5 min each) instead of one order thirty times the queue. Deep books are untouched: when the touch
absorbs the whole command, nothing is capped. Dials: `--entry-clip-touch-fraction` (0 disables) and
`--entry-clip-min-notional-usdt`. **Live-tested on the demo account the same day** (two controlled
entries, 1,000 and 500 USDT, arriving as 10 and 5 clip-capped windows; three integration defects
found live and fixed — receipts and the fix list in `CHANGELOG.md`). The demo verifies the mechanics
only: fill rates, fees, and queue behavior at size stay ungraded until funded entries produce
receipts, and the deadline-cross path did not occur live during the test. (3) A large-size test on the demo account
would not be evidence: demo fills are simulated against real prints with no queue position, so a big
resting order fills far too easily — the only honest large-clip receipts are funded ones, or more
depth-tape measurement. Caveat: one overnight hour, 22 symbols; the numbers are a scale check, not a
fill model — reprice amends also lose queue priority, which this bound ignores and which bites harder
at size.

**The entry recipe was rebuilt in a standalone execution lab and upgraded (2026-08-04, quote-forge;
change point in `strategy_program.md`).** The lab replayed the full overnight order-book tape (34 symbols,
199,785 paired attempts) through seven candidate recipes with a queue-honest fill model: every recipe saw
the same decision instants, a resting order only fills when recorded trades clear the queue ahead of it or
the market trades through it, and an unfilled window ends by paying the far touch — so each attempt is one
all-in number. The winner stacks three measured mechanisms on the shipped recipe: place by the displayed
touch sizes (improve one tick inside when the book leans toward the entry, rest one tick behind when it
leans hard against — joining a touch that is about to trade buys the adverse fill), escalate with the clock
(never behind the touch past half the window, improve near the end), and cross early once the mid has run
against the entry by twice the half-spread-plus-taker-fee. Against the shipped join-and-reprice recipe:
**−0.36 bp per entry (t = −11.1), deadline crosses halved (6.9% vs 13.5%), median fill 15.5 s vs 19.8 s,
and the least price movement against the fill afterwards** — winning in every spread and lean bucket,
strongest exactly where carry trades (illiquid names, 2–8 tick spreads), flat and never negative on
liquid one-tick books. Three negative results are as load-bearing as the win: gluing the quote to every
touch move is *worse* than the shipped recipe (+0.09 bp, t +4.5 — every reprice rejoins the back of the
queue), a trade-flow toxicity brake is worse (+0.13, t +6.0), and one-dial retunes of the winner all tie
or lose, so the recipe ships on its measured defaults. A cadence check ran the winner with evaluations
throttled to the owner loop's pace: 89% of the edge survives at 3 s — which is why the fleet integration
is a policy change inside the existing quote manager, not new execution infrastructure. **Out-of-sample
check (13 unseen daytime hours, 15,391 paired attempts): the cost edge is a night-regime effect** — in the
faster, tighter daytime market it reads +0.04 bp (noise) — while the structural wins transfer intact
(deadline crosses 2.0% vs 5.6%, faster fills, better markouts) at zero cost. The fleet enters at the
00:20 UTC decision, i.e. in the regime where the −0.36 bp was measured; the daytime read bounds the
downside of the change at zero. A fitted short-horizon price model (imbalance, flow, drift, volatility,
BTC lead-lag) was also built and validated (out-of-sample IC 0.12 at 5 s): it independently confirms every
mechanism the recipe uses but does not beat it in paired replay, so it stays in the lab — with one shelved
discovery, a real BTC-to-illiquid-alts lead of a few seconds (t +3.8), for any future event-driven engine. Two lab-method
findings correct earlier numbers: the demo realm's matching engine holds internal liquidity its published
book does not show (post-only orders at the published touch die ~80% of the time there; the overnight
lab's 2,777 `rejected_would_cross` terminals were this, and its fill rates therefore *understate* the
fleet's GTC path), and maker fees are per-symbol (2.0 bp on most lab names, 4.0 bp observed on TLMUSDT),
so the flat 2.0 bp in the replay accounting is optimistic on the 4 bp names. Live receipts, tape, and the
lab itself: `~/Desktop/quote-forge` (FINDINGS.md, INTEGRATION.md); the demo probes there grade engine
mechanics only — the first honest queue-economics grade stays with the funded account's own `is_maker`
receipts.

**The LONG sleeve's stop was the one mispriced number in it** (registered 2026-08-01 as
`LongV12WideStop`, `long_v12_profile()`, commit `f04ccdc`; wired as the deployed LONG profile
2026-08-03 — mechanism in `docs/trading_logic.md`, receipt in `CHANGELOG.md`). All ~20 v11a quirks were ablated
one at a time on the real engine — the harness reproduces the stored report to within the eight extra days
of data (294 trades against 292). Every selectivity filter is load-bearing and loses Sharpe when loosened;
the stop was not.

A 1.5× ATR-14d stop is a two-week average applied to a name that moved 2.5σ *today*, so it sits inside the
noise of the move that triggered the entry — 67 of 294 trades stopped out. Opening it to 3× ATR for 48h and
then tightening back to 1.5× is worth **+0.48 bp/day, t 3.27, n 1927**.

| LONG profile | trades | total | daily Sharpe | worst dip | MAR | stop / target / time exits |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `LongV11aDivWeekendVol` (deployed) | 292 | +40.7% | 1.28 | −4.11% | 1.72 | 66 / 36 / 190 |
| [`LongV12WideStop`](../../liquidity_migration/rules/long_native.py) | 293 | **+52.2%** | **1.50** | **−3.32%** | **2.50** | 50 / 39 / 204 |

Better or equal in all six calendar years (2022 flips −0.9% → +0.8%) and *less* concentrated than v11a
(best-20 trades carry 62% of P&L against 78%), on flat gross (0.027 → 0.028) so it is not leverage.
**Deploying it needs a runtime change, not a profile flip**: the producer publishes `stop_loss_pct` once in
the entry-target metadata and cannot revise it, so the 48h tightening needs `_plan_time_stop_exits` extended
to fire on a breached decayed stop. The wide initial stop alone is config-only but t 1.84, below the bar.
Detail and the promotion note: [`trading_logic.md`](../trading_logic.md), [`strategy_program.md`](strategy_program.md).

**CARRY and LONG are the two-sleeve book because they are opposite sides of one crowd.** Daily book returns
correlate **+0.012** across all 24 decision clocks (+0.002 to +0.024), and they hold the same symbol on the
same day 11 times in 5.5 years — 1.04% of LONG's open name-days, 0.22% of CARRY's. CARRY is long what the
crowd is short and paying for; LONG buys what the crowd has just piled into. At equal risk the pair is
16.56 bp/day, Sharpe 1.81, worst dip −24.2%, against CARRY alone at 14.46 / 1.13 / −45.6%. Scaling LONG the
8.5× that equal risk implies is an envelope decision, not a research one.

## 2. Do-not-retest ledger

Rows marked *(stale)* have a superseded magnitude but a surviving direction (§3).

### LONG entry, exit and stop geometry — ~165 cells on the real engine, 2026-08-01

| mechanism | measured | verdict |
| --- | --- | --- |
| funding condition on the LONG event | 16 gates, none beat the 1.24 baseline: 3d funding ≤ 0 → 1.07 (55 trades), ≤ −10 bp → **−0.00** (34 trades), ≥ 0 → 1.04, ≥ 25 bp → 1.20, bottom decile → −0.07 | CARRY's condition does not transfer. On the days LONG fires the median 3d funding is **+9.0 bp** and only 12.7% are ≤ 0 — the two books are conditioned on opposite states of the same variable |
| "sell into strength" rally exits | 15 cells, best 1.17 against 1.24: trail 1.0–3.0× ATR → 0.52–0.93, breakeven at +1 ATR → 0.79 (hit rate collapses to 32.4%), exit on 1 lower close after +1 ATR → 1.00 at a 62.8% hit rate | every rule that reacts to a pullback shortens the hold and clips the winners. The give-back is at the bottom of the move, not the top. Supersedes the 2026-06 trailing test, which was confounded with hold-7 |
| loss-only cooldown (re-buy a winner) | 0.87 with the deployed stop, 1.00 with 3× ATR, 1.07 with a range stop | a name that just hit its target is not a fresh signal |
| concentrating on the best 1–2 candidates a day | t **−2.38** / **−2.00** vs baseline; best-1 puts 47% of P&L in five trades | the book's breadth is doing real work; the 10 slots never bind, so breadth was never a tested choice |
| shorter hold as a substitute for the decayed stop | stop 3× at hold 2d → t −0.28, at hold 1d → t −1.78 | cutting every trade at two days is worse than leaving them. The value is in cutting only what is losing |
| loosening the shape filters (close location, ATR ceiling) | t 1.33 full-sample and *worse* held-out (1.42 vs 1.52) | noise; the paired test caught what the Sharpe comparison suggested |
| dropping the weekend 1.5× size boost | −0.19 bp/day, t −1.45 | its apparent Sharpe gain was the mechanical effect of running smaller |

### Cross-sectional and cross-venue screens

| mechanism | measured | verdict |
| --- | --- | --- |
| premium_diff cross-venue divergence | −7.31 bp/day t −0.69 Bybit, −17.87 t −1.76 Binance at its own 3.23-unit turnover; eras −57.9, +6.9, +0.0, −14.3, +7.4, −2.8 | dead across its whole parameter space — a 24-setting sweep returns no positive cell and worsens monotonically as the cut loosens (t −3.02 at cut 0.45) |
| 50/50 premium+momentum blend | +16.00 t 1.80 Bybit, +2.33 t 0.28 Binance; registered anyway 2026-07-24, then measured in the 2026-08-19 portfolio test: adding it to carry+LONG LOWERS equal-risk Sharpe 2.15 → 1.99 (its own Sharpe 0.69, correlation +0.21 to carry) | **sub-bar, and DELETED 2026-08-19 by operator override** — config `lane2_premium_momentum_blend_v1.json`, module `lane2_blend.py`, tests, and `screen_phase1.py` all removed. Do not rebuild; the dated dossiers in `archive/` keep the full tables |
| premium momentum (the change, not the level) | 3.70 bp/day t 0.35 | the effect is a convergence force, not a trend |
| mark/index dislocation | looked like 29.96 bp/day t 2.61; flips sign incoherently under lag (+29.96 → −17.91 at +1h → +23.80 at +4h), decays 76.2 → 0.4 by era | arbitraged out |
| `funding_chg24` | 2.59 (t 1.6) → 0.59 → −2.18 at lag 0/+1h/+4h | stale-price artifact |
| venue volume-share shift | −10.83 bp/day t −1.31 | dead — the most direct test of flow migrating between venues |
| cross-venue convergence pair on premium_diff | 534k flips, −417 bp/day | the per-name signal flaps at hourly scale |
| cross-venue funding-differential pair, 07-26 build | 47k flips, churn −35 bp/day *(stale)* | dead. **Not** `lane2_funding_spread_v1`, which uses a different band and a trailing settled-spread basis |
| cross-venue discount accumulation | bench vt 1.21, corr 0.45 to carry-hold *(stale)* | sub-bar; kept only as the best-independence lead |
| amihud illiquidity, cross-venue funding dispersion, jump frequency, funding-vs-premium dislocation, vol term structure, basis instability, close location in range, stale-quote ratio | all \|t\| < 2 | dead |
| OI/turnover ratio; OI/price divergence | 4.27 t 0.35; 3.80 t 0.34 | dead, and on a contaminated panel (§4) |
| OI-flow crowding (7d/1d growth, 4 shapes) | shorting crowded longs costs −28.8 bp/day | dead and inverted |

### Short-side constructions

| mechanism | measured | verdict |
| --- | --- | --- |
| equal-notional basket short, standalone | −5.07 bp/day t −0.70 Bybit, −15.85 t −2.19 Binance | dead on both venues |
| carry SHORT leg | −7.5 bp/day *(stale)* | dead, and a tail carrier |
| MAX / lottery weekly short | −109 bp/week | inverted — a squeeze furnace |
| bear-regime mirror (crowded leaders, gate off) | −4.6 bp/day | dead |
| crowding screen on the short leg | worst day −43.63% vs −35.37% unscreened, worst 1% −23.91%, loss concentration 12.0% | makes the tail **worse** — funding percentile is not a squeeze predictor; it removes diversification without removing hazard |
| BTC-gated conditional short book | −9.73 → +24.88 bp/day Bybit, −10.95 → +20.4 Binance (sign replicates), but t 1.08 / 0.90; flat across seven drop thresholds, peak t 1.30 | do not restore the erased daily SHORT sleeve; extract the gate as a momentum-book component |

**Structural:** every short-side construction in this market fails — the marginal desperate flow is short-side
and the payment is always on the long side of it. A sign result, unaffected by the funding correction.

### Time-series, horizon and clock

| mechanism | measured | verdict |
| --- | --- | --- |
| delta-neutral carry (short perp / long spot index proxy) | 168h hold +47.83 bp/period, t 4.33, Sharpe 1.85; the 24h pair's worst 1% is −0.78% and max DD 4.5% vs −18.61%/77.9% unhedged. Eras +245, −18, +10, +58, −27, −13 | the whole result is 2021, the panel's thinnest era (84 both-venue symbols vs 552 in 2025). Killed on era stability, not significance. No Binance index column, so no replication arm existed |
| weekly hold / t rising with horizon | disjoint resampling t = −5.69 (1h), 2.82 (6h), **3.48 (24h)**, 1.91 (72h), 1.18 (168h) | overlap artifact; 24h is the correct hold and the 73.1%/yr weekly figure does not survive |
| delisting decay | published 220.8 bp/day t 9.94; the PIT turnover-collapse trigger identifies dying contracts at 12.7% vs a 13.3% base rate (0.96× lift); on contracts that never died the same trigger pays **more**, +38.0 bp/day t 4.26 | look-ahead, withdrawn. Residual is generic "short low-turnover falling coins" |
| listing debut short | first 24h return at age 0–3 days: mean +21.2 bp, median −72.3 bp | mean/median trap; explains why listing-short work kept flipping sign by era |
| 24h-display rollover | isolated spike at exactly lag 23 (+0.90 bp/hr t 2.45) scaling with the rolled-off candle, but all alpha is in hour 1, the sharpest 1% cut yields 3.82 bp/hr, dead in 2023 (t 0.39) and 2024 (t 0.12) | a confirmed mechanism that does not pay |
| 7 salience/attention features | all fail at 15.56 bp; the two apparent survivors collapse to t −0.26 / −0.13 when restricted to contracts with a full 168h of history | the effect lived in young contracts — the listing trap again |
| funding-stamp clock | conditioned on the concurrent rate; the PIT version is negative and all intraday windows sit below costs | look-ahead, caught and killed |
| hourly print clock for carry-hold | −94 bp/day in the 2026 era | knife-catching; the daily clock's staleness doubles as a survived-to-the-bar filter |
| BTC→alt lead-lag (24h, 72h) | −25.4 bp/day | dead and inverted — alts mean-revert against BTC's prior move |
| beta catch-up gap | −18.3 bp/day | dead in both directions |
| per-coin time-series momentum ensemble | −0.3 bp/day | dead |
| majors 3d dip-buy | −4 to +5 bp/day | dead |
| weekend basket long | t 0.18 | dead |
| volume-impact fade/follow | Sharpe 0.15 / 0.18; fade-all control −23 bp/day | flat |

**Structural:** nothing intraday survives the 15.56 bp round trip on this panel.

### Carry-hold levers — ~95 cells, all reported in `~/SHARED_DATA/bybit_full_pit/reports/carry_hold_quant_review_2026-07-28/`

| lever | measured | verdict |
| --- | --- | --- |
| daily-equivalent funding-rate entry | the "missed" cohort earns 0.6 bp/nd t 0.02 vs 72.2 (t 3.21) held, −45 bp/nd in 2026; grid cell 11.6 bp/d Sharpe 0.62 | **inverted.** Distributed carry marks chronic decliners; a single deep print marks acute crowding. The per-print gate is load-bearing |
| intraday MAE stops −10/−15/−20/−25/−30% | every cell worse on mean, Sharpe, max DD and MAR; DD goes −31.1% → −47.9…−63.6%; at −15% the stop wins the median stopped trade and loses the mean (−23.3 bp; p10 −153, p90 +132) | **the stop family is closed.** Excising recoveries converts a drawdown into a realized hole. Stop-and-re-enter is bounded between the grid and baseline-minus-fees |
| tighter trailing-rate exits | 17.9/17.6/17.5 bp/d, turnover unchanged | binary exits lose re-entry participation and save no turnover |
| **profit-taking exits, 29 cells on the registered v4 scorer** (2026-08-07, owner question: live trades run far our way and give it back; control reproduces v4's 22.19 bp/d, Sharpe 1.644, DD −24.46%, and a never-triggering arm is an identity to 5e-13 bp) | **no cell beats the baseline on mean bp/day, in any family.** Fixed take-profit +20/30/40/50/75/100% → 8.11/10.70/13.14/14.23/18.23/20.37; trail 15/20/30/40% from peak → 14.43/16.77/18.25/20.00; trail armed only after +20/+40% → 18.55–20.99; scale out *half* → 14.12–17.05; take profit then re-enter next decision → 9.18–13.15; then re-buy only on a dip → 9.39–13.97. Tight cells are significantly negative paired (t −3.27 to −2.19). Max DD gets *worse* in most cells (−24.5% → −25.6…−30.3%) | **the profit-taking family is closed, and closing it is now direct rather than by analogy to the stop grid.** Monotone in looseness with no interior optimum: the only cells that approach the baseline are the ones that almost never fire. Era split is the stop grid's signature — arms gain in the years the book barely earns (2023 +11.6 → +15.0) and dismantle the paying ones (**2026 +74.1 → +18.9 at take-profit +40%**, 2025 +40.9 → +26.6). Mechanism: the price leg is **−1.45 bp/day against the crowd fee's +24.57** — the book's price move is a *cost* it carries to collect the fee, and its distribution is right-skewed, so capping the upside removes the payers and leaves the losers. Trades peaking ≥ +40% are 5.1% of the book and carry ~175% of its P&L; trades peaking under +10% are 80% of it and contribute −164%. Report: `~/SHARED_DATA/bybit_full_pit/reports/carry_hold_exit_grid_2026-08-07/` |
| **intraday funding exit** (same program; exit the hour the print recovers instead of waiting for the next 00:00 decision) | 14.48 bp/d at ≥ −3 bp, 15.70 at ≥ 0 bp, 14.25 at ≥ −10 bp, against 22.19 | **the daily clock's lag is a feature, not slack.** Same finding as the hourly *entry* clock: waiting to the next decision doubles as a survived-to-the-bar filter. 2026 falls +74.1 → +13.0 |
| **adaptive / "sell the top" exits — wave 2, 34 more cells in seven families** (2026-08-07, owner follow-up: the static rules all sold a name while the crowd was still paying it, so each family fixes one named defect). **A** fee-gated TP (sell the extension only once the trailing rate has recovered); **B** vol-scaled TP on the name's own σ; **C** give back a fraction of the *run*, not of price; **D** ratchet floors that never sell on the way up; **E** exit on a fraction of the name's *own* entry depth rather than a flat −3 bp; **F** blow-off spike; **H** unrealised gain vs the fee still expected; **I** volume climax; **J** stall (in profit, no new high for N hours) | 8 cells beat the baseline at midnight — **and every one is decision-hour luck.** Swept over all 24 hourly grid phases: **D ratchet 6/24** (mean −1.79 bp/d), **J stall 3/24** (mean −3.05), **E depth-relative 0/24** (mean −7.75, t ≈ −2 in most phases). The rest lose outright: B 9.75–16.34, C 7.20–10.15, F 10.09–14.84, H 10.37–18.47, I 8.26–17.20 against 22.19. **A is a null, not a win** — it fires 4–25 times in 1,756 days, because once the fee dies the daily exit is already imminent | **the exit family is closed at every level of cleverness, and the placebo is the reason to stop.** For the best cell in the program (J stall 12h/+25%, Sharpe 1.644 → 1.789, +23.41 bp/d at midnight, t 1.00), exiting at a **random hour in randomly chosen spells** scores a median **24.01 bp/d — better than the rule — and 145 of 200 random draws beat it** (p 0.73; same-spell random hour p 0.16). There is no top being detected; an early exit is worth what any other early exit is worth. D's entire edge is 13 trades in five years (7 paid, 6 did not). Report: `~/SHARED_DATA/bybit_full_pit/reports/carry_hold_exit_grid_2026-08-07/` |
| **delta/retracement exit at the DAILY clock — 16 cells** (2026-08-20, owner: "instead of a fixed value we do delta — if funding recovers by 30% then exit"; harness reproduces `lane2_carry_hold_v6`'s weights byte-equal and its registered 21.82 bp/d / Sharpe 1.842 / −18.6% before any variant is graded; only the exit test varies) | replace the −3 bp line with exit-at-r%-recovered from the ENTRY print (r = 30/50/70/90%): 20.78/21.35/21.10/20.78 bp/d; from the TROUGH print of the hold: 20.86/21.26/21.09/19.90 — paired vs deployed **−0.19…−1.14 bp/d, t −0.42…−1.08, no positive cell**, and 2026 falls 62.2 → 53.7–59.8 everywhere. Absolute-line context: exit at 0/−1/−5 bp are a wash (paired t ≤ +0.69), −10 bp (hysteresis width zero) loses −1.14. "−3 bp OR retrace" is numerically IDENTICAL to the pure retrace cell — on held names the retracement always fires first; "−3 bp AND retrace ≥ 50/70%" is IDENTICAL to the deployed baseline to the last decision — the added condition never binds | **the fixed −3 bp line already IS a deep retracement exit, so a delta exit can only ever act earlier — on names still paying.** Entries need < −10 bp and troughs run far deeper, so by the time a print crosses −3 bp it has essentially always recovered ≥ 70% from the trough (the AND-cells' exact equality is the proof). Acting earlier means selling the deep names first — a trough of −80 bp recovered 30% is −56 bp, still rich — i.e. dumping exactly what the depth ladder sizes up. Same mechanism that killed wave-2 family E intraday (0/24 phases, −7.75 bp/d); now confirmed at the daily clock, where it loses less but still everywhere. The 0/−1/−3/−5 plateau also says the line's exact level is not load-bearing. **Delta exits are closed at both clocks.** The rule's existing velocity form (`exit_on_trail_recovery_bp_2d` = 30 bp over 2d) stays — it answers "is the squeeze unwinding fast", not "how far has it come back". Scratch: `exit_delta_sweep.py` |
| **pure-percentage state machine — no absolute bp constant, 9 cells** (2026-08-20, owner follow-up: "get rid of the 10 and 3 and go only off percentages"; same harness, same sizing/filters, only the entry/exit gates vary) | rank hysteresis (enter in the deepest p% of the day's top-100 prints, exit outside q%): 1/5% → **8.85 bp/d (−12.34 paired, t −3.01)**, 2/10% → 12.95 (t −2.19), 5/15% → 19.39 (t −0.92, still negative); rank entry + trough retracement exit → 10.31–18.56, t −1.28…−2.81; own-history entry (print ≥ 2×/3× the name's trailing 30-print median magnitude) + retrace exit → **12.51/12.31 (t −2.5)**, and 2025 collapses 48.3 → 19.8. Every cell loses; the least-bad cells are the ones whose thresholds happen to approximate the absolute rule | **"deep" is an income statement, not a beauty contest.** The fee pays an absolute number of bp/day; whether other names pay less is irrelevant to what this one pays. A percentile gate holds its quota mechanically — the "deepest 1%" exists on every day, including days when the deepest print is −2 bp — so rank rules buy shallow junk in dead regimes, and the tight ones simultaneously break real holds mid-squeeze when the top rank churns (872 name-days vs the baseline's 2,082). Own-history multiples fail the same way from the other side: a name whose typical funding is ~0 "triples" on noise. Third independent confirmation of the same law: the absolute deep print is load-bearing (daily-equivalent entry inverted 2026-08-07; z-score/vol-normalisation refuted three times; now percentile/own-relative gates). Scratch: `pct_rule_sweep.py` |
| **entry-line sweep — is −10 bp lucky? — 9 cells** (2026-08-20, follow-on from the exit plateau; same byte-equal harness, only the entry constant varies, `crowd_persistence` kept at its prepared −10 reference in every cell) | enter at −6/−8: 19.41/19.83 bp/d (−0.98/−0.53 paired, more name-days, worse DD −20.2/−20.3%); **−12: 21.56, a wash (−0.26, t −0.42)**; −15/−20/−25: 19.63/19.49/18.56 (−2.20/−2.33/−3.68, t to −2.00) with 2025 collapsing 48.3 → ~37 while 2026 holds; joint −12/−5 also a wash (−0.16) | **both absolute lines sit on plateaus** — exit 0/−1/−3/−5 (previous row) and now entry −10..−12 — so neither constant is decision-hour-style luck. Shallower entries buy junk (the −10 gate is doing real filtering work: +320 name-days at −8 LOSE money net), deeper entries forfeit the mid-depth payers that carried 2025. The rule's two numbers are structurally placed, not fitted points. Scratch: `entry_line_sweep.py` |
| **the exodus short — REGISTERED same day as `lane2_exodus_short_v1`, deployed to demo** (2026-08-20, "keep exploring" → "build it as a standalone strat sleeve, but synergising"; the post-settlement half of the v7 slide. Events = the cascade's own 1,112 fires, 1m kline opens, all px vs the S+1 base; scratch `exodus_short_study.py` / `exodus_short_placebo.py`, events parquet kept) | after the deployed S+1 sell the name KEEPS falling: −11.6 bp by S+5, −34.3 by S+20, **−104.1 by S+60** (2025/26: −127.3), bouncing after (+120m −83). Short S−10→S+60 gross: **+111.8 bp/event, median +66.3, t +9.0, 66% win, p10 −144.7** (n 1,112); net of the 15.56 bp round trip and the funding print a short pays through S (~0 — the print is dead at fire time, that IS the fire condition): **+110.7 all-era / +139.3 in 2025/26**. Cover sweep is a plateau (S+30/45/60/90 all strong), not a spike. **Controls**: same name same day at S−8h is DEAD (−7.4, t −0.5 — dying names do not bleed all day); S−4h is +50.5 (t +4.1 — the unwind starts hours early, the settlement window concentrates it 2.2×). **Era: the entire edge is modern** — 2021–24 +20/−12/−4 (all t<1), 2025 +111.6 (t 6.9), 2026 +188.8 (t 7.0) — the same farmer-crowding signature as the v7 entry/exit curves | **the first genuinely new mechanism candidate since the timing program: the book exits to FLAT at the fire and leaves the larger half of the move on the table.** Framed as an execution extension, this is exit-to-short with a timed S+60 cover on the same v7 fire read — same signal, same clock, one more order pair. NOT registered; risks priced so far: (1) **premature fires are now priced on the 18 real walk-forward events** (identities from the tardis 230-day walk-forward, 1m klines fetched per event): short S−10→S+60 on a still-alive squeeze runs **mean −96 bp gross, median −17, worst −945** (SOMIUSDT 2025-10-01 ripped 9.4% in the hour) — 8 of 18 actually paid, the mean is three tail events; blended at the measured 18/230 rate the drag is **8.7 bp per fire-day** against the clean fires' +139, so the blend stays ≈ +119 bp/fire-day in 2025/26, and the tail is exactly what a venue-native stop truncates (scratch `premature_events.parquet`); (2) held-book selection — fires are persistence-filtered held names; the all-name opportunity set needs hourly funding history not yet assembled; (3) kline-open executability into a falling market is a bound, not a fill model; (4) the standing midnight-circularity rule — a settlement-window finding must pass the full robustness battery before promotion. The 2021–24 flatness marks it a regime trade: expect nothing outside squeeze eras. Owner decision to take it further; the per-event distribution and both controls are in the parquet. **Book-level backtest (same day, owner: "run a full backtest"):** all 1,112 fires priced at book weight, exit-to-short at S−10 / cover S+60, 15.56 bp RT + funding paid + the premature share applied at the walk-forward rate (92.2% clean / 7.8% at −111.5): overlay **+6.2 bp/day over the record** (2023 +0.2, **2024 −0.8** — the honest losing year, 2025 +7.8, 2026 +18.3), clean-event net mean +95 / median +50; compounded on the v7 estimate the 3-year curve goes **25.7x → 50.4x**. Estimate on kline bounds, not fills; scratch `exodus_short_backtest.py`, chart `exodus_short_curve.png`. **Registered and built the same day** (`configs/lane2_exodus_short_v1.json`, `rules/exodus_short.py`, its own engine sleeve fed from the carry producer's fire site): entry at the fire (engine closes entries at S+5 via book validity), cover S+60 hard clock, notional = carry's abandoned position frozen at fire. **The stop question was settled by measurement before freezing the config**: on 1m wicks for all 1,130 event windows, every stop from +30 bp to +1500 bp loses — +300 whipsaws 12.9% of clean events (overlay 6.13→3.94 bp/d), +150 whipsaws 31.2% (median +1.2), +1000 still whipsaws 1.5%, costs 0.37 bp/d, and would not have fired on the worst observed event (SOMI peaked +940) — so the registered stop is a 0.35 disaster fence, carry's exact posture, and the account loss guard owns the tail (scratch `exodus_stop_sweep.py`, OHLC cache kept). **Three more stop DESIGNS measured same day, all losers too (scratch `exodus_stop_designs.py`, 10 cells):** delayed-arm (level armed only from the settlement) is IDENTICAL to armed-from-entry at +1000 (whip 1.5%, overlay 5.76) — proof the whipsaw lives INSIDE the post-settlement fall (dead-cat bounces mid-slide), not in the entry-to-S window; trailing stops are the worst family (+500 trail whipsaws 12.4%, overlay 4.09 — the bounce structure exits before the second leg down); vol-scaled fences are the least bad (5x hourly vol: whip 0.8%, overlay 5.93, -0.20 vs none) but do NOTHING for the tail — the premature squeezes are high-vol names whose scaled fences sit too far out to ever fire (prem worst unchanged -960). Across all 4 families / 18 cells no stop beats none: the path to the S+60 bottom routinely wicks +150..+300 against the short mid-way, so any fence tight enough to matter is a fence the winning trades hit first. Forward evidence starts at the registration commit; the first demo weeks measure the kline-vs-fill gap before any capital question |
| **the inverse program — short positive funding** (2026-08-20, owner: "we short positive funding too"; two levels tested, accounting verified short-safe: `net_return` is linear in funding, turnover on \|Δw\|, gross cap on \|w\|) | **(a) the daily mirror book is DEAD**: short top-100 names at settled print ≥ +10 bp, cover < +3 bp, depth ladder on \|trailing funding\|, mirrored toxic band (block while ret_3d in (0, +30%]), vol floor — best cell **+0.64 bp/day, Sharpe 0.20** (vs the long book's 21.82/1.84), and **2026 is negative in every cell** (−1.9…−9.4; six cells, band on/off, +10/+15/+20/+30 enters). **(b) the mirror exodus** (1,033 positive-print deaths 2024+ from the settlement-level funding root, top-100 days, 1m klines per event, deduped to ≥12h per symbol): in 2025+ price POPS after the death — the paid shorts cover — mean **+41.2 bp at S+20 (t 3.6), +56.7 at S+60**, and the S−4h control shows the opposite sign (−82 bp, the deflating pump), S−8h flat; **but the medians collapse** (+11.5 at S+20 → +5 at S+45 → −1.3 at S+120, win rate 52–54%) and **2024 inverts outright** (−30 bp at S+10, t −4.6) | **the asymmetry is structural, not an accident of thresholds.** Positive funding marks names still ripping — the fee a short collects is devoured by the price leg (that is *why* longs pay), so the daily mirror earns nothing and loses most in the squeeze era that feeds our long book. The event-level pop is real in the mean but tail-driven — a lottery-ticket profile (net of 15.56 bp fees the S+20 cell is mean +26 / median −4), the same shape this program has repeatedly declined — and era-flipped in 2024. **Priority stays on the exodus SHORT** (mean AND median positive, 66% win, 2.5× the per-event size); re-open the mirror long only if the short ships and its live record earns trust. Scratch: `mirror_short_book.py`, `mirror_exodus_study.py`, events parquet kept |
| **OI × Δfunding quadrant** (2026-08-07, owner: "OI rising + funding dropping means shorts opened" — mechanically right, and the correct pairing: the earlier test crossed OI change with the funding *level*, this crosses it with the funding *change*) | **The conditional read is confirmed.** In the book's own entry population (last settled print < −10 bp), forward 24h net by quadrant: shorts opening (OI ↑, fee deepening) **+119.9 bp t 2.55** (n 1,534); longs opening +119.3; longs capitulating +55.2; **shorts covering (OI ↓, fee easing) +17.9** (n 573) — **spread +102 bp/day**. Mirror confirms: in crowded-LONG names the shorts-opening quadrant earns **−128.0 bp (t −1.82)**. **But it converts into nothing.** As an entry filter: block-covering 21.14, require-fee-deepening 20.29, require-OI-rising 14.73, require-shorts-opening 14.02, all against 22.19 (t −0.99 to −2.13). As a *size* (the shape v4's persistence had to take) it stops losing but never wins — best cell daily-updated opening ×1.5 / covering ×0.5 at 22.30, **t +0.15** | **every quadrant is positive** — the worst still earns +17.9 bp/day — so the split separates degrees of good, not good from bad, and the book **never binds its gross cap** (0 of 944 bars), so a screen frees capital with nothing to do. Same wall persistence hit; persistence escaped by becoming a size because its bad bucket was genuinely −16.7 bp/name-day, and this one has no negative bucket to escape into. **Two further reasons not to bank it:** the spread is **not era-stable** (2022 −10.6, 2023 +102.9, 2024 +285.8, **2025 −66.1**, 2026 +245.2 — two of five negative), and the OI cohort is **biased in this finding's favour** — §4 says a long study conditioned on OI is biased *for* its thesis, and this is a long study. Clean forward-collected point-in-time OI is the only thing that settles it. Report: `~/SHARED_DATA/bybit_full_pit/reports/carry_hold_exit_grid_2026-08-07/oi_funding_quadrant.md` |
| **funding-factor construction hypothesis — 5 claims** (2026-08-07, owner-supplied: per-coin z-score then cross-sectional rank; OI cross-validation; price conditioning; OI-weighted cross-venue synthesis + dispersion; fast decay unsuitable for daily). Cross-venue panel, 174k name-days, 658 symbols, 2,026 daily bars, top-100, forward 24h net of settled funding and of 7.78 bp/side | **H1 z-score: diagnosis right, prescription wrong.** Net L-S: raw **+4.1** bp/d > de-meaned +1.4 > **z-score −5.0** (Sharpe 0.30 / 0.12 / −0.53). The *decomposition* confirms the insight — a coin's own rolling MEAN funding traded alone is **−12.9 / −9.6 / −13.7 bp/d** at 30/90/180d, i.e. the chronic baseline is not tradeable crowding. What breaks it is dividing by the coin's own funding vol, worth −8 bp/d gross. **H2 OI:** crowded longs rising/flat/falling OI = −1.4 / −14.9 / **+19.1** bp — hypothesis direction but non-monotone, no t > 1.5, contaminated cohort; crowded shorts are +27.7 / −4.5 / +29.9, wrong shape. **H3 price: splits by side.** Crowded longs, price down 0–10% → **−17.6 bp (t −1.79)**, the worst bucket, as claimed. Crowded SHORTS invert: down >10% −16.0, down 0–10% −14.0, flat/up +23.2, **up >10% +70.3 (t +3.06)**. **H4:** synthesis is worse than single-venue (Bybit 6.3 > equal-weight 4.1 > turnover-weighted 3.8 > Binance 0.7 gross); dispersion in its winning direction is +11.6 net, t 1.83, and is **entirely 2025** (−3/+1/+11/−1/**+48**/−1). **H5 decay: inverted** — bp per day held is 4h **−7.0**, 12h +7.3, **24h +11.8**, 72h +2.7, 168h −4.0 | **volatility standardisation is now refuted three independent times in this repo** (per-name vol normalisation; idio-charts T6, 5/6 features; here) — the premium *scales* with vol, so dividing it out discards the signal. De-meaning alone is defensible and roughly neutral. **H3 independently reproduces the registered toxic band** from the cross-sectional side: for the side this book trades you want price already rising, and the losing zone is exactly the band's [−30%, 0). **H5 confirms the daily clock is the optimum, not a compromise** — matching the hourly print clock's −94 bp/day and the staleness-as-survivorship-filter result. Framing all five: the cross-sectional daily long-short is **marginal after costs** (raw net +4.1 bp/d, t 0.66, negative in 2023 and 2024), which is why the deployed book is a long-only hysteresis machine on a deep absolute print rather than a rank factor — it sidesteps the turnover and the short leg. Report: `~/SHARED_DATA/bybit_full_pit/reports/carry_hold_exit_grid_2026-08-07/funding_factor_hypothesis.md` |
| **ENTRY and SIZE from 1-minute microstructure — wave 5** (2026-08-07; the exit closed, these were the two levers left. Features on the 4,320 minutes strictly BEFORE the entry bar, all 1,249 trades with a full window, 0 missing parts: jumpiness = share of motion in the top 1% of minutes, variance ratio 60m/1m, upper/lower wick asymmetry, turnover concentration, up-minute share, biggest single-minute move, 1m realised vol) | **Nothing predicts what the book earns.** On 1,249 *independent* trades no minute-scale feature beats the daily control (trailing-3d rho −0.0707 t −2.50; 1m vol −0.0631; variance ratio −0.0575; jumpiness −0.0336) and every quintile table is non-monotone. **The give-back looks predictable and is not** — 1m vol rho +0.2624 against give-back, but +0.1941 against the *peak*, and only **+0.0531** against give-back ÷ peak: the wave-4 variance artifact again. **20 entry screens as config, 0 winners**; removing trades removes money in proportion (keep-bottom-quintile cells run 9.35 / −0.14 bp/day). **6 sizing variants, 0 winners**: risk parity is harmful (17.52 and 15.64 bp/day, t −2.40 / −2.71), pro-vol −1.34, per-name cap 0.10 enforced with 0 breaches | **entry and size are closed alongside the exit.** One real result, and it is a confirmation from data the filter was never built on: the *calmest* entries are worthless — bottom quintile by jumpiness earns **−0.14 bp/day**, by 1m vol 0.86, by biggest-1m-move 0.90 — and dropping them is a wash (t +0.08) because **v3/v4's 5%/day minimum-vol entry floor already does it**. Risk parity failing again confirms that **the premium scales with volatility**. The remaining lever is how much capital to run, which is an owner decision, not a research one. Report: `~/SHARED_DATA/bybit_full_pit/reports/carry_hold_exit_grid_2026-08-07/entry_and_size.md` |
| "sell the runner to fund a fresher name" (rotation) | **retired analytically, no test run**: the gross cap binds on 0 of 944 weighted bars, mean gross 0.176 against a cap of 1.0 | the book is never capital-constrained, so a sale funds nothing — it goes to cash. Also note the median carry trade lasts **one day** (mean 1.64, p90 3), so there is normally exactly one decision point and the only way to sell a top is intraday |
| **flexible entry on settled prints, second pass — the parked print-clock follow-up** (2026-08-19, owner: "we don't have to enter at a specific time every day". The 2026-07-28 kill was parked on "needs a persistence mechanism"; v4–v6 supplied one, so this re-ran it properly: an hourly engine on the full panel, exits held at the registered midnight discipline, entries varied. Engine validated by reproducing the knife-catching receipt on its bare arm, 2026 era −106 vs the recorded −94) | Same-frame arms (mean bp/day on position days / Sharpe): midnight anchor **25.9 / 1.95**; print-hour entry behind the FULL v6 stack 17.0 / 1.28; fixed survival delays of 4/8/12/16h after the print 16–21 / 1.2–1.6, non-monotone; second-consecutive-deep-print 15.6 / 1.16. Decomposition: instant entries on prints settling at **00:00 alone earn 28.8 / 2.24** — the best arm anywhere; the same instant entry at 08:00 prints earns 12.8 / 1.10; midnight entries excluding fresh prints (the survived stale ones) 18.2 / 1.57 | **entry-hour flexibility on settled prints is closed, now with the mechanism.** The modern filters do not rescue early entry — they are all daily-resolution signals and cannot see an intraday knife forming — and survival *time* is not the mechanism either (no delay interpolates to the anchor). "Midnight" was never a magic hour: it is instant entry at the day's main (00:00) settlement, where the money is, **plus** a free survived-to-the-bar filter on off-hour prints. Any deviation costs 7–10 bp/day. Scratch: session artifacts `hourly_entry_engine.py` |
| **the interval's anatomy, from tardis** (2026-08-19; 44 free first-of-month days 2023-01..2026-08 of Bybit derivative_ticker, 1,313 deep episodes; Bybit's ticker publishes the RUNNING funding rate continuously, the signal the settled-print panel never sees) | The running rate is below −10 bp for essentially the WHOLE interval before a deep print (stay-below share median 1.00) — the venue announces the print hours ahead. Price rises **+93 bp mean into** the deep settlement and falls after it (−17/−131/−287 bp at 1/4/8h, cross-section); after a RECOVERY print price keeps rising (+20 bp/1h, +98 bp/4h mean, right-skewed). **The one measured opening: the engine enters AT the settlement bar and never collects the entry print itself.** On the v6 book, 291 of 1,833 entries forfeit a mean +41.8 bp print (+12,161 bp raw over 4.9y, ~98% of it 2025–26) ≈ +0.3–0.5 bp/day at entry weights. The S−1h (23:00) entry hour on running-rate knowledge is worth a **positive median in all four eras** (+17 to +39 bp per qualified entry; print income alone median +18–23 bp every era); means are fat-tailed (2024 mean −21 on n 58 despite a +39 median) | **both closed clocks now have a mechanical explanation**: early entry buys the average knife (post-print drift is negative), early exit forfeits the continuing squeeze (post-recovery drift is positive) — the daily lag is load-bearing on both sides. The pre-settlement entry is a DESIGNED, NOT BUILT candidate: live-implementable (the engine's own ticker feed carries the running rate), backtestable only on tardis first-of-month days, small (+0.3–0.5 bp/day), era-concentrated where the 4h/1h interval mix lives. Its future is a Mac-mini capture backtest or an owner-decided live A/B — not a panel registration. Scratch: `tardis_mechanics_44d.py`, `forfeited_prints.py` |
| **intraday conditioning at the print/entry: Binance 5-min positioning and Bybit liquidations** (2026-08-19; 33,443 top-150 deep prints since 2023 with 5-min OI / top-trader L/S / taker-flow features at the print, 92% coverage; the same features joined to the v6 book's own 1,770 entries; liquidation mix over the 4h before 1,313 deep prints) | Pool level, real-looking splits: weak taker buying into the print → −432 bp next-24h vs −181 at the median bucket; whales de-longing intraday → −329 vs −220. **On the book's own entries both collapse**: taker terciles +169/+351/+18 (a hump whose shape flips era to era), whale terciles +123/+204/+113 (same). OI 4h/24h: sign flips between 2025 and 2026 even at pool level. Liquidation mix: sell-heavy is the LEAST bad pooled (−16 vs −144 mixed) but 2025 and 2026 disagree on the sign; the only stable-ish cut — top-decile cascade notional before the print → +11 vs −87 — is one unstable slice | **pool-level knife signals do not transfer to the filtered book** — they are correlated with exactly what the v3–v6 stack already removes, and conditional on passing the stack the residual splits are era-noise. Closed for entry-conditioning. Worth keeping: the unconditional deep print is followed by −300 to −600 bp in 2025–26 — the book's entire edge is selection, which is why every "enter more, enter earlier" idea keeps failing. Scratch: `trackc_features.py`, `trackc_book_entries.py`, `liq_knife_study.py` |
| **UPSIDE volatility and move shape — wave 4** (2026-08-07, owner: "I mean UPSIDE volatility"; wave 3's `rv15` was symmetric, so a spike there could be a crash — a real flaw). Asymmetric and shape features on the 1m path: upside semi-deviation, upside/downside vol ratio, rolling skew, upside jump count, upper-wick share, log-price curvature (parabolic), up-minute share, up-streak, stretch above the 12h EMA, vol-of-vol. Target is **remaining upside** — `(max close from here to trade end)/close − 1` — because that is what "is this the top" actually asks | **A variance artifact fakes a strong signal and had to be removed**: raw rho against remaining upside is vol-of-vol +0.2594, gain +0.1740, stretch +0.1302, upper-wick +0.1156 — all *positive*, i.e. wrong-way, but large, because a wilder path has a higher future max by variance alone. Scaling the target by `trailing vol × √(minutes left)` collapses them (vol-of-vol +0.2594 → **−0.1018**, gain +0.1740 → +0.0285). **Detectability as AUC (0.50 = nothing, 0.65 = usable): best in the entire feature space is 0.542**, and it is vol-of-vol — a volatility-regime measure, not an exhaustion pattern. The owner's specific hypothesis, upside semi-vol, scores **0.480 — below a coin flip**, leaning the continuation way. **As rules: 13 cells, 0 winners**, t down to −3.09; the classic blow-off signature (parabolic curvature) is among the worst rules ever tested here, taking 2026 from 74.1 → **6.7 bp/day** | **upside acceleration does not mark exhaustion in this population — it marks the middle of a move that keeps going.** A hypothesis of mine failed here and is recorded rather than dressed up: I expected the spike to be the moment the crowd pays most, so that selling into it forfeits the fee. Not supported — the trailing fee is flat and non-monotone across upside-spike quintiles (−308.9 / −295.2 / −278.3 / −266.9 / −282.2 bp/day) and rho(spike, fee) = **+0.048**, if anything shallower. Running total **118 cells, ten families, two resolutions, three placebos, two 24-phase clock sweeps, two signal studies — nothing survives**. Report: `~/SHARED_DATA/bybit_full_pit/reports/carry_hold_exit_grid_2026-08-07/m1_upside_findings.md` |
| **volatility-spike exits at 1-MINUTE resolution — wave 3** (2026-08-07, owner: "we have 1m data, we can do sharper things"). `bybit_full_pit/klines_1m`, fill checked as `docs/data.md` §F demands: **100% of the book — 347 symbols, 2,050 held name-days, 1,249 spells, 2,952,000 held minutes, 0 missing parts**, with highs so a wick is visible | **First, the prize is ~2× the hourly view**: on true 1m highs, 43.0% of trades peak ≥ +10% (hourly 20.2%), 10.6% ≥ +40% (5.1%), mean peak **+19.83%** against a mean final of −0.73% — a mean give-back of 20.56 pp. Runs ≥ +20% peak a median **1,263 min (≈21h)** after entry, only 3% inside the first hour. **Then, as a SIGNAL, the spike is empty**: 205,272 backward-looking observations, forward return to trade end is flat and non-monotone across 15m/12h vol ratio (−0.46 / −0.57 / −0.46 / −1.08 / **+0.75** / −2.20), Spearman **rho −0.013**; volume rho −0.046, also non-monotone; only plain extension ranks anything (rho −0.096; gain ≥ +100% → −9.02% to trade end). **As RULES, 16 of 17 lose** (vol spike, volume spike, both, spike-gated-on-extension, single-minute range) | **the spike is the middle of the move, not the end** — after a trailing-60m return ≥ +20% the next 60m is **+0.99%** and the next 240m **+0.78%**, while the run to trade end is −5.33%. Selling into it sells early. The one survivor (vol spike 3× once up >100%, 18 fires, t 0.49) loses its placebo **300/300**: a *random* minute in the same trades scores a median 26.39 bp/d against the rule's 23.05, so the spike is the **worst** available timing, not a neutral one. **The closing shape of the whole program**: hold the trigger fixed and vary only the delay before selling — after a >50% run, exit immediately 11.91, +6h 15.08, +12h 19.13, +24h 22.75, +48h 22.32, never **22.19**. Monotone in delay, converging to the baseline from below. Every cell that ever beat the baseline had nearly stopped acting. Report: `~/SHARED_DATA/bybit_full_pit/reports/carry_hold_exit_grid_2026-08-07/m1_findings.md` |
| time-based / spell-age exits | funding per held-day flat across spell days (135/128/135/117/135 bp) | no decay, so no justification |
| breadth tilt (≤2/≤3/≤5 names) | 15.3/14.1/11.6 bp/d vs baseline 18.0 | the drawdown window is itself high-breadth; the tilt shrinks recoveries without protecting the crash |
| risk-off / BTC trend gate | PIT 30d-trend<0 days earn **more** (38.2 bp, Sharpe 1.69) than trend≥0 (6.4 bp, 0.38), in every era pair | would destroy the edge — the book is a fear-premium collector |
| beta hedge | relieves ~5% of the tail | the residual risk is the price of the premium |
| print-depth sizing | Sharpe 1.04/1.02/0.98 vs the trailing-rate ladder's 1.06 → 1.16 | one print is a noisier premium estimate than the 24h sum |
| vol-target overlay | v1 0.64 vs raw 1.02; v2 0.52 vs 1.11; worst vt day −24.6% | harmful on corrected accounting — raw is primary; vt kept for recipe comparability only |
| regime-scaling overlays | deep-regime variance dominates the pool at any scaling | pooled-variance arithmetic defeats them |
| per-name vol normalization | Sharpe falls | the premium scales *with* vol |
| shallower −5 bp entries, band-only-when-shallow, spell-loss floors, X1/band boundary grids | flat plateaus or worse | no cell beat the baseline |
| **deeper entry bars, −20/−30/−50 bp** (2026-08-06, 8 cells on the registered scorer; control reproduces v3's 19.83 bp/d and Sharpe 1.38 exactly) | monotonically worse and never close: −20 bp → 16.7 bp/d Sharpe 1.25 DD −41.6%; −30 bp → 12.4 / 1.03 / −53.7%; "enter −30 bp else exit" → 11.2 / 0.99 / −48.7%, paired **−8.60 bp/d t −2.46** over 1,894 days, negative in all six eras; −50 bp → 11.9 / 1.14 | **the −10…−30 bp band is where the book earns.** Raising the bar drops flat days 36% → 62% and concentrates what is left in cascades, so the worst dip nearly doubles on a *smaller* book. Depth preference already enters at the size level through the trailing-rate ladder |
| removing the hysteresis band (enter = exit) at any depth | −1.16 bp/d at −10 bp, −5.25 at −20 bp, −8.60 at −30 bp, all t ≤ −0.8 | the band pays for itself: a round trip costs ~15.6 bp and the gap is what stops the book re-buying the name it just sold |
| per-name cap 0.15/0.20 | Sharpe pinned ~1.5, MAR rises to ~4 | scales mean and vol together — a sizing decision for an owner, not a Sharpe fix |
| depth-sizing the spread book | Sharpe falls | a deeper spread carries more basis volatility |

**Why entry screening cannot separate them:** the worst decile at entry has a deeper print (−34 vs −22 bp),
deeper trail, higher vol and is *more* pumped (ret3d +27% vs +14%) — identical to the biggest winners, and the
ret3d ≥ +50% bucket has median −6.4% but mean +8.2% and the largest book contribution of any bucket. Blocking
the fingerprint deletes the strategy. Losses are single events, not processes: 98% of losers take ≥50% of the
loss in one daily candle, 88% hit maximum adverse excursion within 2 days, and conditional on being ≥10%
underwater after day 1 the remainder averages **+1.26%**.

### Settlement-instant timing — the v4 book and the deep-print population, 2026-08-03

Owner-requested follow-up to the closed sawtooth program
([archive/2026-08-01-settlement-sawtooth-program.md](archive/2026-08-01-settlement-sawtooth-program.md)):
can entries be timed around the moment the crowd fee pays? Scratchpad harness reproduced
registered v4 bit-identically (1,756 days, 22.1939 bp/day) before any arm ran.

| mechanism | measured | verdict |
| --- | --- | --- |
| long just before the fee pays, hold minutes, collect | archive, full 1-minute coverage (662,678 settlements): the price steps down by 1.03× the print in the first minute it pays (97% of the step inside 2 min); net to a long −3.0 to +0.8 bp at every depth | dead by construction — the fee is refunded in the price as it arrives. There is no slow decay to dodge: price is flat into the instant and steps at it |
| PIT short of the post-fee crash | entry at S+1h (print published at S, fully knowable), holds 1/2/4/6h: **−14.4 to −26.3 bp/event** (t −3.2 to −6.0, n 30.7k); cadence-aware exit at the last close before the next settlement, so the short never pays funding: **−29.6 bp/event, t −4.1** (n 15.9k), negative in all six eras. The lag-0 reference (+13.1, t +5.0) is the ex-dividend step itself and needs a fill at the pre-drop price at the very instant the print publishes | dead at every executable cell. By S+1h the crash is over and the drift runs against the short. Medians are positive (+36 bp at 6h) while means are deeply negative — squeeze skew; do not re-read the median as a lead. Confirms the archived §3 kill with the funding-crossing confound removed |
| v4 fill delays (uniform +1/2/4/8/12h; entry-only +1/2/4/8h; exit-only +1/2/4/8h) | same decisions and weights, only the execution clock moved; 24-phase sweep: entry-only median −0.26 to −1.27 bp/day (≤9/24 phases positive), exit-only median −0.88 to −1.71 (≤8/24), uniform −0.95 to −5.99 at phase 0 | the registered clock — decide at the settlement close, fill just after — is already the optimum this family offers. The +1h/+4h delays that were free at v1 registration (§4) are **not** free on v4's shape |
| **sub-hour entry-fill minute** (2026-08-19, owner: "why 00:20 and not 00:00 — the farmers dump after the fee pays"). All 1,255 v6-book entry events 2021–2026, Bybit's own 1m klines (0 fetch failures, delisted names included), fill = 1m open, 22-minute grid vs the deployed ~00:20; tardis minute marks agree from minute 1 on shared events (the m=0 mark is a settlement-instant marking artifact — trust the trade print there) | filling AT 00:00 costs **−46 bp/entry mean** vs 00:20 (median −48, t −3.7 splits); deep entries (print ≤ −25 bp) −60, and **2026 deep −90 (t −3.1, median −90)** — the farmer dump is real, front-loaded, and growing. Everything else is dead: minutes 25–59 flat-to-negative (means −6 to −22, 2026 ±5 noise); ≥+55min negative funding-adjusted, matching the row above; the one seductive cell — deep fills at 00:05–00:10, +49/+54 mean t≈2.4–2.8 — is **2025-only** and flips to −8/−3 (t −0.3) in 2026, where the dump now takes the full ~10–20 min to complete | the 00:20 clock was born as a kline-availability margin and landed, by luck, right where the post-payout dump has finished in every era. Filling earlier costs the dump; filling later buys nothing. **No change to the fill clock is supported**; re-test the 00:05–00:10 deep cell only if a future era shows the dump completing early again. Scratch: session artifacts `entry_fill_minute_curve.py` |
| **adaptive entry sniping + the EXIT-side sub-hour curve** (2026-08-19, owner: "don't use fixed values — make it predictive"). Same 1,255 entries plus all 1,255 exit events, full 1m OHLC incl. the pre-settlement hour; three parameter-light snipe rules (3-min sign-flip, retrace-¼-of-drop, 3-min stall), every rule shown next to the ORACLE (best price in minutes 3–25) and the deployed fixed-20 | **Entries: adaptive rules are early-fill in disguise** — median fill minute 4–6, +15 to +25 bp pre-2026, **−10 to −15 in 2026** (the dump completes later now); the oracle's +175 bp mean is path noise no causal rule touches (best rule captures <8% of it). Dump size IS forecastable at 00:00 (pre-settlement squeeze terciles → +31/+40/+68 bp dump) but waiting to 00:20 already collects it, so the forecast buys nothing on the entry side. **Exits: monotone and era-consistent the other way** — selling at minute 0/1/3 beats the 00:20 fill by **+45/+31/+24 bp/exit** pooled (t 5.3/4.0/3.5; 10%-trimmed +43/+28/+19; 2026 alone +31/+24/+12), decaying to zero by minute ~25 (which reproduces the closed ≥+1h exit-delay grid). Weight-summed: **+0.6 (2026) to +3.8 (2025) bp/day** at a minute-3 fill, positive all six years | the mechanism is one drift with an asymmetric optimum: these names fall after the 00:00 settlement, so the long book should buy AFTER the fall (it already does) and sell BEFORE it (it currently sells at 00:20 too). No reversal detector needed — both optima are corner solutions. Superseded the same evening by the two-leg proposal in the row below (the evening leg roughly triples this one). Scratch: `adaptive_fill_rules.py`, `fetch_fill_ohlc.py` |
| **the evening exit — tonight's exit is knowable at 23:00** (2026-08-19, owner: "more exits, intraday, be creative"). The modern book is ~100% hourly settlers at the decision level (2,073 of 2,082 held name-days), so the last SETTLED print visible at 23:00 is a causal forecast of the midnight recovery exit. All 2,082 held name-days, all-in accounting (skipped final print booked, false fires charged the re-buy round trip + 15.56 bp fees) | forecast: **sensitivity 56%, precision 98%** (706 true fires, 15 false in five years — hourly prints are sticky at the −3 bp boundary). Economics of "sell at 23:00 when the visible print has recovered": **all-in +49.0 bp per fire, t +4.2** (median +45); monotone decay on the same names — 23:30 +35, 23:55 +25, 00:00 +21, 00:03 +15, 00:20 = 0; era all-in +92/+37/+51/+55 for 2023/24/25/26, 2022 a wash on the mean (median +16). Earlier fire hours (16:00–22:00) have more false fires and t ≤ 1.6 — 23:00 is the sweet spot and the ex-ante natural one (max information, min directional exposure). Mechanism honesty: UNFIRED exits drift the same (+53 bp 23:00→00:20, look-ahead descriptive) — the drift is universal on exit days; the print is the permission slip for the ~56% that are causally knowable. **Combined policy** (fired → sell 23:00; remaining exits → sell 00:03): weight-summed **+0.71/+1.97/+0.94/+5.26/+2.56 bp/day for 2022–2026**, positive all six years — several times the v6−v5 improvement | **PROPOSED, owner to decide — the two-leg exit clock**: leg A fires at ~23:00 on the recovered visible print (settled prints only, never the displayed rate); leg B moves the remaining exits to ~00:02 off the swept midnight print + WS-closed kline. Membership logic untouched — both legs sell only names the registered rule is exiting anyway; a false fire (15 in 5y) re-buys at the deployed 00:20. Degrades gracefully if the venue's interval mix ever reverts toward 8h (fires stop, deployed clock resumes). Seen-data; a clock change is a strategy change with its own change point, graded live from the engine's fill records. Selection note: 5 fire hours probed, 23:00 reported. **The rolling cascade (owner: "why not every hour?") is measured and UNRESOLVABLE at available power**: fire at the first hour (16:00–23:00) where the fee has been dead K consecutive prints — K=1/3/6 give all-in +65/+26/+41 bp per fire at t 1.9/1.1/1.6, non-monotone in K, 2026 alone t ≤ 1.1. The means are always positive (medians up to +114) but hours of extra price exposure on ~200%-vol names swamp the drift: per-fire noise is ~10× the edge, so nothing earlier than 23:00 is provable on 2,082 name-days. Not proposed; revisit only with a live A/B off the fills WAL if the owner wants the aggressive version. Scratch: `evening_exit_forecast.py`, `evening_exit_economics.py` |
| **the pre-settlement fire read — sell before the fee pays** (2026-08-19 night, owner: "why not exit before funding is paid — front-run the farmers"). The deployed early exit sells ~1 minute AFTER the dying print settles, into the farmer exodus. Bybit publishes the running fee rate for the UPCOMING settlement continuously; measured from tardis derivative_ticker (44 first-of-month days, 932 symbol-days, 2,613 held-context intervals, plus a raw-tick check), economics on the deployed cascade's own 1,112 fire events (1m klines 2021–26), rule walk-forward on 230 held→fire days | the venue **locks the rate ~55 s before it pays** — no tick changes later, and the S−1 read matched the final print in 230/230 walk-forward days. Fire classification (≥ −3 bp) at S−1/−2/−5/−10: sensitivity 100/98.8/96.0/92.4%, premature 0/0.44/1.05/2.37% per check (4h/8h settlers cleaner than 1h); walk-forward premature days 0/1.7/2.2/3.9%, drag +0/+1.5/+1.5/+2.3 bp per fire-day (5 events in 230 days: 4 boundary grazes forfeiting 3–16 bp, 1 last-minutes whipsaw forfeiting 237 bp). Sell-minute curve on the real fires, all-in vs the deployed S+1 sell (forfeited final print booked; it costs ~nothing, mean −0.07 bp — the fee is already dead when we leave): **+6.6/+8.5/+12.8/+21.3 bp per fire at S−1/−2/−5/−10** (medians +1.9/+1.7/+3.6/+11.3, t to +4.9) — medians track means, so unlike the cascade's own tail-driven uplift this edge is broad-based. Era at S−5: 2025 +18.0 (t 3.0), 2026 +19.2 (t 3.2), 2021–24 flat — the same modern farmer-dump signature as the entry side. Book-level raw: **+1.6/+2.0 bp/day (2025/26) at S−5, +2.4/+3.1 at S−10**, ~0 before 2025. Side discovery: exit name-days with NO intraday fire (338; 322 are universe/persistence drops, not fee recoveries) leak **+74 mean/+43 median bp between 23:55 and 00:20** (t 3.5) — collapsing names falling out of the top-100 — but 2026 is only +18/+15 (t 0.8) and firing them needs a 23:55 shadow decision on the day's 99.7%-complete turnover data | **BUILT AND DEPLOYED AS v7 the same night** (owner picked S−10, then "keep improving"; the deploy receipt in `CHANGELOG.md` is the change point). The shipped rule came from a 13-cell design sweep on the same walk-forward: fire at the FIRST wake inside the last 15 minutes whose running read clears the registered −3 bp line, margin zero, settled-print fallback. Sweep receipts (net/fire all-era → 2025/26, premature days of 230): shrinking-margin ladder +9.2 → the safety margins defer fires past the steep part of the slide, REJECTED; flat S−10 +16.6 → +23.5, 9; discrete ladder 15-10-5-2-1 +19.4 → +28.8, 18; every-minute 15-window (the shipped form, what a 60 s-wake producer naturally does) **+19.0 → +28.3, 21**; every-minute 30-window +20.2 → +31.7, 27 — runner-up, rejected: doubled premature churn on half-formed hourly averages for a gain inside drag noise (re-open only if a month of live fires shows premature rate well under the tardis estimate). The earlier evening-exit row said "never the displayed rate" — that was the rate an HOUR out, still moving; minutes out it is the settled print visible early. Execution-clock change only: v7 = `carry_hold_v7_live_v1` trading `lane2_carry_hold_v6` byte-identical (one config id, forward grade unbroken), one public tickers batch inside the window, existing mask path, `CARRY_STRATEGY_PROFILE=v6` and `CARRY_EARLY_EXIT=0` as the two rollback dials. Re-verify the ~55 s rate-lock on the first live week (knowability sample: first-of-month tardis days, 2025–26-heavy). The universe-drop leg is NOT built (weak in 2026, needs a 23:55 shadow decision). Scratch: `presettle_exit_knowability.py`, `presettle_exit_economics.py`, `presettle_rule_walkforward.py`, `sniper_staged_margins.py`, `sniper_ladder_sweep.py`, `midnight_leg_presettle.py`, `presettle_book_impact.py` |

### Financed-longs mechanism ledger

| mechanism | measured | verdict |
| --- | --- | --- |
| cross-sectional carry long/short | +29.1 bp/d t 2.43 *(stale)* | reproduces the house cell; not new alpha |
| crash reversion conditioned on an OI purge | mid/no-purge cells revert equally (t 4.2 vs 2.8); purge cells unstable 2022/2026 | the purge does not discriminate; the external prior did not transfer |
| crash-reversal portfolio, all variants | best bench Sharpe 0.98 | sub-bar |
| crash + negative-funding absorption | best bench 1.58, cross-venue ratio 0.28–0.53, carry-exit variant −40 bp/d in 2026 *(stale)* | sub-bar and Bybit-local |
| gated momentum long *without* the financing condition | bench 1.57 and −61 bp/d in 2022 unfiltered; funding ≤ +1 bp → 1.79; ≤ 0 → 2.87 *(stale)* | the financing condition **is** the alpha — monotone along the economic axis, not a fitted spike |
| post-squeeze drift | Sharpe 0.41 *(stale)*; reconfirmed twice — shallowest trailing-rate decile −67 bp/nd, v3 recovery exit −54 bp/nd | the payment stops when the crowding stops |
| aggregate-funding regime basket | states pay (+24–31 bp/d) but are rare; Sharpe 0.44 *(stale)* | sub-bar standalone |
| long-side composites of sub-bar parts | best honest composite 1.80 | composing sub-bar parts stays sub-bar |

### CONTINUOUS shape

| cell | measured | verdict |
| --- | --- | --- |
| amplitude ladder under the funding admission | ungated cells are monotone in rung strictness (1.88 < 2.00 < 2.45) but funding-gated the order **inverts** (2.18 > 2.13 > 2.00); every reweighting loses hedged | the strict rungs' premium lived on funding<0 trades — the population the admission removes |
| crowd-3 / crowd-off | fc 1.17 / 1.16 vs crowd-2 | monotone worse, deeper drawdowns |
| hold-12 / hold-48 | +2.38% total at fc 0.41; hold-48 buys 610 active days at max DD −3.18%, MAR 1.15 | both strictly worse |
| TWAP 3-tranche (delay 1/2/3h) | −0.84 pp for a fair DD improvement at 3× the parameter surface | rejected; delay-2 and delay-3 single cells worse than delay-1 |
| looser trigger `turn2_pop2`, alone or combined | 1.27 / 0.97 / 1.68 | dead |
| funding floor at +1 bp | 1.65 vs 2.18 at 0.0 | past the economic boundary |
| gate off / loosened to >−0.05 | 0.93 / 1.35, and 1.31 even with the funding filter | not loosenable |
| reject-unknown admission | 641 trades, +11.39%, −1.84%, fc 1.49 — identical to the shipped book on every line | exact null; on root funding there are no historical unknowns |
| cooldown as an independent parameter | `cooldown_ms = hold_ms` in the CONTINUOUS backtest engine (deleted 2026-08-14 — not the Rust execution engine), so the hold-12 test conflated the two | deprioritized then, moot now: the scorer is git-history only |
| force every entry passive | −90.57 bp/day, t −7.20, Sharpe −3.19; discards 49% of intended entries | the most damaging change tested — in a momentum book the entries that come back to you are the ones about to go wrong. Immediate taker +28.42 t 1.96; hybrid chase-then-cross +31.30 t 2.16 |

## 3. Corrections that changed a conclusion

**Funding double-count (fixed 2026-07-28, commit 3540f9a).** `by_funding_age_h` carries float epsilon — one
hour after a settlement it reads 0.9999999999999999, not 1.0 — so the old `age < 1.0` predicate matched two
bars per 8h/4h/2h settlement and charged every such print **twice**; 1h-interval symbols escaped because the
next print overwrote the epsilon bar. Now an age-reset test, `(age < 0.5) | (age < age.shift(1).over("symbol"))`,
in [carry_hold.py](../../liquidity_migration/rules/carry_hold.py) (and mirrored in the since-deleted
`lane2_blend.py`), with regression tests on real age shapes. Weights, entries, exits, price legs and turnover costs are unaffected —
decisions read funding *levels*. Only funding P&L inflated, ~×1.5–2 blended and worst where funding was deepest.

| withdrawn | replacement |
| --- | --- |
| carry_hold_v1 and financed_leaders_v1 beat the CONTINUOUS benchmark on return **and** Sharpe (2.57 t 4.87 / 2.21 t 4.01) | bench Sharpe 1.21 / 1.44 / 1.01 against a 1.84 benchmark — **return only.** None of the three meets the program goal |
| carry-hold attribution funding +13.06 vs price −3.86 (3.4:1) | funding +7.19, price −3.40, fees −0.40 (2.1:1). The sign survives; the size does not clear the bar |
| eras +25.4 / +19.1 / +54.3 / +32.9 / +77.6 / +71.5 bp/day, "positive through the 2022 bear" | +3.8 / +3.0 / +26.0 / +13.8 / +30.3 / +32.5 — the doubled prints concentrated in deep-funding capitulations; financed-leaders' 2022 is now −4.0 |
| both books clear the ~t 3.4 multiple-testing bar | t 2.31 and 2.58 — both below |
| carry-hold replicates on Binance (+25.0 bp/day t 2.73 Sharpe 1.38) | +2.7 bp/day, t 0.4, Sharpe 0.18, max DD −85%. **The doubled funding leg *was* the replication.** Carry-hold is single-venue Bybit evidence |

**CONTINUOUS's Sharpe 2.73 described a strategy that was not running.** The backtest modelled no stop
(`stop_price` empty on all 2,344 trades) while the deployment always had the account's 2.006% disaster
fallback. Applying that stop via recorded MAE: −2.54%, Sharpe −0.75, t −1.36, against +18.24% / Sharpe 2.50
with no stop. 77.5% of trades breach a 2% stop; 64.3% of the model's 649 take-profit winners first dipped 2%
against the position; the sign flips between a 5% and an 8% stop. LONG had no such gap — it declares
`stop_loss_pct` and models the identical stop — which is the control that makes this a located defect rather
than a harness bug. Repair: a declared 35% component stop modelled on both sides, honest reconstruction Sharpe
**1.87**, exit mix `stop_loss` 114/2,344 = 4.9% exactly as predicted. 35% is the widest trigger whose worst
modelled fill still sits inside the ~48% 2× liquidation distance.

**Two cost claims withdrawn.** The deployed sleeves were never priced at 4 bp: LONG models a 45.00 bp round
trip (2.9× conservative) and CONTINUOUS was actually charged **24.12 bp** in the ledger (1.55× conservative;
`−Σ cost_return / Σ|notional_weight| × 1e4` over 2,344 trades). So CONTINUOUS net does not fall +21.13% →
+14.80%, and its cost was not "implausibly cheap" — the price is 24 bp, and the anomaly was turnover (10.44
units over 3.3 years). The 3.89× ratio belongs only to the 4 bp research surfaces. Relatedly, **a flat
per-book charge is a reranking, not a rescaling**: measured turnover is 1.52 units for momentum_1w (11.9 bp
charged), 2.16 for funding carry (16.8) and 3.23 for premium_diff (25.2), so the uniform 15.56 bp overcharged
momentum and undercharged premium by a third — which flipped premium_diff negative and left the designated
dead control as the strongest cell in the screen.

**The dispersion gate was an artifact** of the pro-rated funding approximation. Published as Sharpe 1.27 →
1.74 with halved drawdown; re-measured, gated 1.30 vs ungated 1.29 vs *inverse*-gated 1.29 — indistinguishable
— with a worse compounded drawdown (51.6% vs 46.1%) while sitting out 64% of the time. Not in the committed
config; the rejection rests on three identical Sharpes, not on an exact number.

**The 95%-idiosyncratic un-hedgeable tail described hypothetical shorts across the research universe, not the
deployed book.** On full PIT, LONG is long-only with max drawdown −4.11% and CONTINUOUS −1.29%. The premise
that this research was replacing a broken deployed book was wrong.

## 4. Data contamination and measurement facts

- **Bybit open interest is survivorship-contaminated** — backfilled only for contracts still listed at
  backfill time: 95.9% of with-OI symbols were still listed on 2026-07-16 against **0.0%** of without-OI
  symbols. Specific to OI (delisted-cohort coverage: price 0.999, funding 0.999, premium 0.998, OI 0.100).
  Delisted alts are exactly where short exposure pays, so an OI-conditioned short study is biased against its
  own thesis and a long study for it. Never use OI availability as a filter in a return study. The panel's
  rising OI coverage (0.857 → 0.980, 2021→2026) is a bias gradient, not improving quality.
- **`open_interest_value` is not a value** — byte-for-byte identical to `open_interest` across the dataset,
  i.e. contract units. Dropped in [cross_venue_panel.py](../../liquidity_migration/research/panels/cross_venue_panel.py) with
  the reason inline; still latent in `daily_feature_panel.py`, which prefers it for `oi_delta_7d` and
  `oi_to_adv` (neither has a consumer outside that module). Derive notional as `open_interest × mark_close`.
- **`funding_event_kind` exists on only 2 of 2,024 Bybit funding partitions** (added 2026-07-16). Filtering on
  it naively drops 99.9% of history; ignoring it admits unsettled *predicted* rates into recent decision rows
  — direct look-ahead. Scan the two schema generations separately.
- **Bybit funding intervals really did shorten**, so a per-print threshold means a different *daily* carry per
  symbol: 2025 settlements are 52% 4h, 21% 1h, 7% 2h, ~20% 8h against 100% 8h in 2021, and 73–80% of
  carry-hold's 2025-26 held name-days are on sub-8h names.
- **Bybit funding inverted in 2025-26** — the median still pays a short (~+5.5%/yr) but the mean now costs one
  (~−4.5%/yr), a ~10 pp gap that is entirely a negative-funding tail firing on the same events as the price
  squeeze. A short book pays twice on one event (269,642 settlements over 203 sampled days).
- **Short payoff geometry is structural.** A short book takes 22.4% of its total loss in the worst 1% of
  trades and 49.1% in the worst 5%, against 8.6% for a long. Liquidity screening makes it worse — worst-1%
  short loss by ADV quartile runs −11,422 bp (thinnest) to −19,719 bp (deepest), because the most liquid names
  attract the most crowded shorts. Removing market beta relieves ~5%, and that is the optimistic in-sample
  bound, so the tail has to be avoided at the name level.
- **Cross-venue replication cannot multiply the sample here.** Universe overlap is 79.9% Jaccard (152,715
  shared name-periods), so agreement is a robustness check on funding data, not independent evidence:
  empirically 5 of 6 mechanisms with a positive Bybit effect failed replication and the only replicator was
  dead on both venues. Real sample multiplication needs a different name population or asset class.
- **Decision-clock fragility.** The identical carry-hold construction over 12 daily-grid offsets spans Sharpe
  0.30–1.52, and midnight — the clock every registered financed-longs number uses — is the best cell;
  settlement-aligned offsets score 0.73/1.04, refuting a freshness explanation. The honest level is the
  8-offset ensemble, ~1.2 full / ~1.5 bench. The *filters'* improvement is clock-robust, which is why the
  registered experiment is a paired differential. Separately, entry staleness beyond the daily cadence is
  expensive: +1h and +4h delays were free at v1 registration (t 4.19 / 4.82), +24h costs ~40% of mean.
- **The scorer's funding boundary is conservative at 1h-cadence entries** (measured 2026-08-03). The
  funding window `(t, t+24]` excludes a settlement whose instant coincides with the entry bar's close, while
  the price window `close(t) → close(t+24)` contains that settlement's ex-dividend drop; the missed entry
  print is deep by construction and the exit-boundary print collected instead is shallow. Swapping the window
  to `[t, t+24)` on v4 is worth **+0.47 bp/day at midnight, +0.44 to +2.57 across all 24 phases (24/24
  positive, mean paired t 8.8)**, entirely 2025-26 — the 1h-cadence era. Registered carry numbers are
  understated by roughly this much. The convention stays: it is shared by every registered financed-longs
  record, and it is not tradable — the live book fills after the instant and has no such boundary.
- **Terminal-day dodge.** `prepare()`'s forward-return requirement exits every name 24h before its final panel
  bar — an implicit look-ahead worth roughly +0.13 Sharpe that flips 2022's sign. Every financed-longs number
  shares it, so cross-config comparisons stay fair and absolute levels are optimistic by that amount.
- **Registered numbers are module-path.** The review scripts compute the trailing rate after `prepare()`'s row
  filter, the module before: 50 of 1,883 gap-adjacent days differ, Sharpe 1.13 vs 1.11 on the chosen cell,
  orderings unaffected.
- **`lane2_carry_hold_v1` does not reproduce its own registered figures, and v2/v3 do.** `score_carry_hold`
  gives v1 **17.38 bp/day / Sharpe 0.977 / turnover 0.273** against the registered 18.0 / 1.02 / 0.271, a ~3.5%
  gap. v2 (16.736 / 1.085 / 0.198) and v3 (19.826 / 1.376 / 0.156) reproduce to three decimals. Confirmed
  **pre-existing** on 2026-07-31 by scoring both sides of the v4 change — the numbers are byte-identical before
  and after — and consistent with the module-path item above. Not repaired; recorded so it is not rediscovered
  as a new defect, and so v1's row in §1 is read as approximate.
- **Vol-targeted drawdown is not a comparable statistic across sizing variants.** During the v4 review it
  ranked the persistence arms in the *opposite* order to the raw basis (the arm with the best vt drawdown,
  31.9%, had the worst raw one, 39.3%). The repo already treats raw as primary and vt as recipe-comparability
  only; this is the concrete instance of why. Any drawdown compared across arms holding different average
  capital must also state the capital, since drawdown scales with it.
- **Two Sharpe bases in the CONTINUOUS work, not comparable**: "active" = active ledger days only (what
  component reports print); "fc" = full calendar with flat days as zeros. Convert with × sqrt(active_days/1222).
- **CONTINUOUS's small drawdown reflected a small book**: mean realized gross 0.0075 against a nominal target
  of 0.500 (1.5%), max ever 0.0800, in market 325/849 days. The cap never bound; the constraint was candidate
  supply. Any restatement at larger size must scale drawdown by the same factor it scales return. Relatedly the
  BTC hedge produces zero fills by design at this size — binding constraint `min_qty` 0.001 BTC (~65 USDT), not
  a notional minimum; it first becomes executable at short gross ≈ 2.5–4.9k USDT, and the sub-step intent is
  preserved so the kernel emits an explicit `qty_step_mismatch` rejection rather than silently rounding.
- **Harness errors that produced impossible numbers before publication** (they belong on the failure list, but
  are recorded here so they are not lost): an uncentred pooled-leg percentile — `rank/len` runs 1/n..1 rather
  than symmetric about 0.5 — handed the two tails different name counts and made a supposedly neutral book
  directional, and on re-run pair correlation is +0.009 not −0.285 (the legs are independent, not hedging),
  magnitudes roughly doubled, and 2021 is negative, so the earlier "positive in all six years" claim was wrong;
  `shift(-24)` on a disjoint-sampled frame differenced a 24-*day* spot move against a 24-*hour* perp move
  (+696 bp/day with a 3,049% drawdown simultaneously); booking a passive entry at the *next* close produced
  Sharpe 5.60 from a price no resting order could obtain; and a first venue comparison charged funding to Bybit
  only, comparing a net leg against a gross one.

## 5. Still open

- **Re-derive the stale magnitudes.** Every *(stale)* row above needs the corrected scorer before any
  magnitude is trusted. Directions are already safe to use.
- **Forward records.** `scripts/research/score_financed_longs_forward.py` appends one row per config-day (plus
  three paired differentials — `carry_hold_v2_minus_v1`, `v3_minus_v2`, and `v4_minus_v3`, the last being the
  experiment the 2026-08-03 v4 promotion rides on) to
  `~/SHARED_DATA/bybit_full_pit/reports/financed_longs_forward/ledger.csv`. Live-runtime parity — order
  lifecycle, venue stops, partial fills — is modelled nowhere. **The daily sequence stopped 2026-07-28 and sat
  idle for three weeks** (backfilled 2026-08-19); a forward record that stops accruing the moment nobody runs
  it by hand was the standing argument for automating it — done 2026-08-19 by owner order:
  `scripts/research/daily_evidence_run.sh` under launchd (`com.liquidity-migration.daily-evidence`,
  14:30 local), status in `daily_run_status.json` beside the ledger, dirty-checkout refusal kept.
- **Score the venue-scoped CONTINUOUS admission variant — RETIRED 2026-08-19, owner decision.** Its tooling
  (`render_continuous_admission_variants.py`, `ContinuousEventConfig`) left the tree with the CONTINUOUS
  sleeve on 2026-08-14; the owner chose retirement over restoration. Design constraints for anyone who ever
  reopens it are in the 2026-07-27 archive dossier and git history.
- **Loser-identification doors — two closed, one taken, one still open (2026-07-31).**
  - **Closed: same-symbol cross-venue funding confirmation at entry.** The diagnostic pointed the right way
    (name-days where Binance funding is also deep earn +59.6 bp/nd vs +16.0 where it is ≥ 0) but the
    venue-local cohort is 6.4% of the book and blocking it is worth −0.10 bp/day (t −0.09). Requiring
    *sustained* Binance crowding is actively harmful, −5.70 and −6.18 bp/day (t −2.9, −3.0): it strips the
    acute Bybit-local liquidation events the premium is paid for.
  - **Closed: turnover-rank-decay dropout.** The first measurement was broken — raw rank *number* drifts up
    for every symbol as the panel grows from 84 to 552 listings, so a 7-day change in it counts new listings.
    Repaired with a percentile rank, names slipping 5–15pp in a week do earn −53.1 bp/nd, but that is 8% of
    the book and as a filter it is worse than the band v3 already has.
  - **Taken: toxic-band high boundary extended to 0** — in `lane2_carry_hold_v4`, at t 1.12, i.e. below the
    bar, at the owner's direction and flagged as such in the config.
  - **Still open: suspend → hard exit.** Untestable without an identity-shifting engine change; v3/v4 suspend
    a hold to zero weight inside the band rather than ending the state.
  - Two cohorts remain known structurally lossy and candidate exclusions: suspension-touched trades (−6.5%,
    n=168) and positions open at series end (−4.0%, n=70, −16% of book over 5.5y).
    **Permanently closed:** OI (contaminated) and spot basis (data the roots lack).
- **Crowding persistence is the surviving new feature, and it only works as a size.** Share of a symbol's last
  20 settlements printing deeper than the 10 bp entry threshold, counted in settlement sequence and not on a
  clock. As an entry *filter* it is a wash against v3's band (paired differential −0.69, t −0.39) — on a
  3-name book removing candidates costs more than the losers save. As a *size multiplier* composed with v2's
  depth ladder it is the strongest carry result the program has: 16 of 16 shape cells positive at t 1.87–2.77,
  and the combined v4 differential is +10.76 bp/day (t 3.23) at v3's capital. Replacing the depth ladder rather
  than multiplying it gives +0.16 to +1.89, all t < 1 — the composition is the result. Registered as
  [lane2_carry_hold_v4](../../configs/lane2_carry_hold_v4.json); full run note in
  [docs/research/archive/2026-07-31-trend-filters-and-persistence.md](archive/2026-07-31-trend-filters-and-persistence.md).
- **Missing datasets, ranked.** (1) A liquidation feed — its purpose changed: it is the input that would let
  the one durable premium be *sized and survived*, not a squeeze predictor, and external cascade work records
  a hazard no backtest here models, that venues activate auto-deleveraging and can force-close a winning short.
  (2) Multi-venue funding beyond two venues: two venues give a difference, not a distribution, and the
  two-venue version is dead. (3) Sub-hourly bars — the rolloff mechanism decays inside an hour. Spot klines
  were downgraded to a completeness item because the index proxy already answered the delta-neutral question.
- **If Bybit's interval mix shifts materially again** (e.g. majority-1h), rerun the acuteness-vs-chronic
  tables — that split is microstructure-shaped and could invert.
- **The coarse funding-coverage label — subject deleted 2026-08-14.** `trade_lifecycle.py` exposes
  `funding_modeled_fraction` and `long_native.py` consumes it; the third file this row used to name,
  `continuous_events.py` (which collapsed the whole book to "partial" from per-trade modes — a flag that fired
  on 2 of 843 trades at 99.82% notional-weighted coverage), left the tree with the CONTINUOUS sleeve. The
  defect no longer has a subject; the two surviving consumers are correct.
- **Structural target never built.** One shared causal feature library, few signals each with a stated
  mechanism, one portfolio construction layer, one execution layer, parameters set by economics. Sizing and
  the BTC hedge remain per-sleeve.
- **The bar is t ≥ 2.5 as of 2026-07-31**, owner decision, authority
  [docs/research/governance.md](governance.md) §2. It replaces the family-wise ≈3.25/3.58 and is prospective — pre-2026-07-31
  verdicts stand as recorded and are not restated. It also closes a defect the program had open against itself:
  the ~44-mechanism denominator behind the old threshold was never enumerable in the tree or in git history, so
  every number derived from it rested on an unverifiable count. A fixed bar does not. What it costs: t 2.5 is
  p ≈ 0.012, so across ~45 screened mechanisms roughly **one false positive is expected** against roughly one in
  twenty at 3.25. A plateau and a failed placebo now carry the weight the threshold used to.
- **The screen's own budget**, for whoever revisits the threshold: ~45 mechanisms in the anomaly program plus
  ~18 families (~45 constructions) in the financed-longs program; phase 1 returned 0 of 12 cells at the
  then-current t ≥ 3.25 and the strongest cell was the designated dead control. Gates here were tuned on Sharpe
  and are therefore mis-set for producing evidence, because loosening a filter buys sample: the momentum BTC
  gate at > 0.00 gives n 952, t 2.50, Sharpe 1.55, while > −0.05 gives n 1,247, **t 2.78**, Sharpe 1.50.
- Registered experiment definitions live in code, not prose — the passive A/B arm parameters
  lived in the retired paper owner's `passive_execution.py` (removed 2026-08-03 with the fleet;
  in Git history). Its read thresholds, sample target and kill rule are recorded in §1, with
  the reason the arm never finished accruing.
