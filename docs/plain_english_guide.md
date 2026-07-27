# The Whole System in Plain English

This guide translates the entire project into normal words, for the owner.
Nothing in here is a second source of truth — the code, `STATE.md`, and the
research docs stay authoritative, and numbers quoted here carry their as-of
date (mostly 2026-07-27) and will drift. What this guide promises is that
every code name and piece of jargon used anywhere in this project is
explained here once, in plain English, so conversations about the system can
happen in plain English. Convention from now on: plain words first, the code
name in parentheses when precision needs it. This guide was adversarially
fact-checked against the source documents and code on 2026-07-27;
simplifications are deliberate, distortions are bugs — report them.

## 1. The whole system in ten sentences

1. This project runs two automated trading strategies against the crypto
   exchange Bybit, using **play money only** — a hard switch keeps real money
   off, and nothing can flip it except a separate, explicit instruction from
   you naming exactly what is authorized.
2. The first strategy (code name **CONTINUOUS**) bets against small coins
   that just spiked upward, expecting the spike to deflate within a day.
3. It only takes that bet when Bitcoin has been rising over the past month,
   and — since 2026-07-26 — only when the coin's crowd fee (see §3) shows
   the spike-buyers are at least not being paid to stay in; measured over
   history this cut the strategy's fee bill by more than half.
4. The second strategy (code name **LONG**) does roughly the opposite trade:
   it buys large, heavily-traded coins right after a strong breakout and
   rides the strength for up to three days.
5. The strategy programs never touch the exchange — they only write down
   what they wish to hold, and one separate program (the account manager)
   places the real orders, enforces every risk cap, and keeps the records.
6. Every position gets an automatic profit-taking exit, a loss exit stored
   on the exchange itself so it works even if our server dies (deliberately
   wide for the pump-fade strategy, the normal loss exit for the breakout
   strategy), and a hard time limit.
7. A small side position in Bitcoin and Ethereum (the hedge) is computed to
   lean against the pump-fade strategy's bets — though at the account's old
   size its orders were below the exchange's minimum and never actually
   placed; they become real once the account runs at its new 250k size.
8. Everything runs on one rented Linux server; the account manager sends
   you the hourly Telegram digest and trade notices, and a separate
   independent checker — which holds no exchange password and cannot trade
   or restart anything — sends warnings when something looks broken.
9. We do not trust our own simulations, because our own history proved they
   flatter — so a new idea's numbers only start counting once its exact
   rules are locked into the code history and it is graded on market days it
   has never seen.
10. Changing the live system takes a recorded decision — normally earned by
    that live track record, occasionally ordered by you ahead of it — and
    real money is a separate locked door this project has never opened.

## 2. The pump-fade strategy (code name: CONTINUOUS)

What it believes: when a small coin suddenly spikes upward in one hour, the
spike usually partly deflates over the next day — *if* the overall market is
healthy and *if* you are not standing in a crowd of trapped short sellers
about to be squeezed. (A **squeeze**: when a rising price forces the crowd
betting against a coin to buy back all at once, which pushes the price up
even harder and hurts everyone still betting against it.)

The checklist it runs every hour, in order:

1. **Find the spikes.** Rank every coin by a combined spike score; look at
   the top tenth. The trigger pattern (code name `turn3_pop3`) means:
   trading activity roughly three times normal plus a price jump of roughly
   3% in the hour.
2. **Wait one full hour.** Only act on a completed, confirmed move — never
   on an hour that is still in progress.
3. **Check the market.** Bitcoin must be in an uptrend over the past 30
   days, judged with yesterday's data (the Bitcoin gate). Tests show
   removing this check roughly halves the strategy's quality: fading pumps
   only pays in rising markets.
4. **Check the coin.** At least 500,000 dollars traded in the hour, at
   least 240 days of trading history on the exchange, and the coin's own
   trend — with the overall market's effect stripped out — must rank in the
   bottom quarter of all coins. We fade pumps in coins that were already
   weak before the spike, not pumps in genuinely strong coins.
5. **Check who is paying the crowd fee.** This is the 2026-07-26 upgrade
   (code name `single_fund0` / the funding admission): only fade the pump
   if the most recent already-charged crowd fee on that coin was zero or
   positive — meaning the spike-buyers are at least not being paid to stay
   in, and usually are paying us while we sit short. (If the fee reading is
   missing entirely — essentially never in practice — the coin is allowed
   through and the exception is recorded.) Fees re-price during the hold,
   so the strategy as a whole still pays a small net fee — but this rule
   cut that bill by more than half, and cutting it is where the whole
   improvement lives: paying to wait, in front of a possible squeeze, is
   how the old version got its worst losing streaks.
6. **Don't pile in.** If more than two coins qualify from the same signal
   hour, skip that whole batch (a market-wide move, not coin news —
   tested: those crowded batches lose). Never re-enter a coin within 24
   hours of the previous *entry* (since 24 hours is also the maximum hold,
   a position held to its time limit can re-qualify immediately). A
   separate safety ceiling allows at most 25 open bets and 5 new ones per
   pass, though in practice the batch rule binds first.
7. **Size the bet.** About 2% of the account as the baseline, halved for
   the jumpiest coins and up to doubled for the calmest (so roughly 1–4%).
   A separate overlay cuts bets to roughly a third when a Bitcoin risk
   score sits in an elevated middle band — note it does *not* cut at the
   very highest readings, and it only switches on after the system has 50
   accepted decisions of its own history.
8. **Exit.** Take profit automatically at 12%; a deliberately wide 35%
   disaster exit sits on the exchange itself; and whatever happens, close
   after 24 hours. The bet is specifically "spikes deflate within a day."

Honest history for exactly this shape (simulation, March 2023 → July 2026,
market moves hedged out): **+11.06% total, worst dip −1.84%, smoothness
score 1.45** (655 trades). The retired three-part version scored +15.85%
with a worst dip of −2.85%. So the new shape gives up return and smoothness
to cut the worst dip by about a third and improve the pain-adjusted score —
the trade you chose. Its live track record started on 2026-07-26 and is the
only evidence that will settle the choice.

## 3. The crowd fee (code name: funding), because everything hinges on it

The contracts we trade never expire (code name: perpetuals). To keep each
contract's price glued to the real coin's price, the exchange makes one side
pay the other a small fee every 8 hours: whichever side is more crowded
pays. If the longs (people betting up) are crowded, longs pay shorts; if
shorts are crowded, shorts pay longs. At exactly zero, nobody pays.

Two consequences run the whole project:

- **For the pump-fade strategy**: shorting a coin whose longs are paying
  means we *collect* a fee while waiting for the deflate. Shorting a coin
  whose shorts are paying means we *bleed* fees while standing in a crowd
  that can get squeezed. The 2026-07-26 change is simply: only take the
  first kind (fees move after entry, so the book still pays a small net
  fee overall — a bit over half less than before).
- **For research**: the one deep, durable payment stream found in this
  market (July 2026 program) is being paid to hold what the crowd is
  desperate to dump. Two long-side ideas built on reading these fees are
  on trial (buy coins whose shorts are paying heavily — `carry_hold`; buy
  recent winners only while holding them is free or paid —
  `financed_leaders`), plus one refinement of the pump-fade fee rule
  itself (§7).

We always read the last fee actually charged, never a forecast.

## 4. The breakout-buy strategy (code name: LONG)

What it believes: when a big, liquid coin breaks out hard and closes strong
while the whole market is rising, the strength tends to continue briefly.

Its checklist: only the top-50 coins by money traded over 90 days, at least
30 days of trading history, and today among the 10 most-traded; both Bitcoin
and Ethereum above their 30-day average price; a jump of at least 2.5× the
coin's typical move over 1, 3, or 7 days (or a flat 15% in one day when the
typical move can't be measured); the day closing strongly (top 30% of its
range for a one-day jump, top 40% for a multi-day one); the coin's typical
daily swing no more than 12% of price; signal fresh (under 24 hours). It
prefers not to buy the top: it waits up to six hours for a dip of at least
1% below the signal price and buys the dip — and if no dip comes by the
six-hour mark while the signal is still fresh, it buys then anyway.

Sizing: the account is treated as ten slots of about 10% each, shrunk for
jumpy coins, capped at 30% per position, scaled 0.30×–1.25× by how calm
Bitcoin is, made 1.5× bigger on weekends — then the whole book is halved by
LONG's own size dial (0.5; the pump-fade strategy's dial is 1.0 — the dial
is per-strategy, set in the same shared config file). Exits: sell
automatically 1.5 typical-daily-swings below the actual entry price (loss)
or 4 above (profit), always out by 3 days, and no re-buying the same coin
for 7 days.

Caveat you should keep in mind: LONG's historical result leans heavily on
its profit-target winners, and its live sample is still too small to prove
anything beyond "the machinery works."

## 5. How a bet actually gets placed (the machinery)

- **Strategy programs (code name: producers)** hold no exchange passwords.
  Each one writes a wish — "I want to hold this much of coin X" (a target) —
  into a durable queue.
- **The account manager (code name: account owner)** is the single program
  allowed to trade. It merges all wishes, applies the account caps, places
  real orders, attaches the exchange-side loss exit in the same request
  that opens any position, tracks confirmations of actual trades (fills),
  and writes everything to an append-only diary (the journal) that
  outranks every chart and message derived from it. It also sends the
  hourly Telegram digest and trade notices.
- **Risk caps as of 2026-07-27** (config file `configs/operational.demo.json`,
  scaled 25× when you funded the account toward 250,000): at most 2×
  leverage, 500,000 total position value, 125,000 in any one coin, 250,000
  of locked-up collateral. Leverage here does *not* make bets bigger — bet
  size is a percentage of the account balance; leverage only changes how
  much cash each bet locks up. The old smaller caps still bind on the
  server until you dispatch the next deployment.
- **The hedge** is a scheduled job that computes small Bitcoin/Ethereum
  buy-side wishes sized against the pump-fade strategy's open shorts, so a
  market-wide jump hurts less. At the old 10k account size its computed
  orders were below the exchange's minimum order size and were never
  placed; at the full 250k they finally become placeable — which is why
  refreshing its frozen sizing dataset is now queued.
- **Two parallel fleets**: the play-money exchange account (demo) tests
  real exchange behavior — real order handling, fees, crowd fees, outages —
  with zero money at risk; and a simulated copy (the paper twin) that
  never trades or logs in to the exchange (it may read public price data
  but holds no credentials and can place no orders), existing only to
  prove the software plumbing, every record stamped "integration only."
- **Watchdog**: an independent checker reads the fleet's health files and
  sends Telegram warnings when data goes stale or a service breaks. It
  holds no exchange password and cannot trade or restart anything.
- **Deployments (code name: rollout)** are triggered by you through
  GitHub's automation page, never by copying files: tests run, the account
  is proven empty, the whole fleet stops, one exact code version installs,
  a tamper-evident authorization file is issued, everything restarts in
  order. If anything can't be positively verified — fresh data, a valid
  protective exit, matching records — the system refuses to add risk
  (fail closed). That refusal has already saved the account once.

## 6. How we decide what is true (the evidence rules)

The one-sentence version: **a strategy's simulation score is an opinion;
its score on days it could not have seen is evidence.**

Why we are this strict — the burns are from our own July 2026 history:

1. Our flagship claim (a 2.73 smoothness score) was withdrawn when we found
   the simulation had no automatic loss exit while the live account had a
   tight ~2% one. Replaying the same trades with the real exit turned
   +18.24% into −2.54%.
2. Two separate bookkeeping errors in trading costs taught us to measure
   rather than assume: correcting the second reversed two conclusions at
   once — the headline signal turned out to lose money and the designated
   dead-on-arrival comparison turned out to be the strongest thing on the
   board — and measuring the first showed our initial alarm about it had
   itself overshot.
3. Five of six effects found on Bybit failed to reproduce on Binance (a
   second, larger exchange we use as an independent check).
4. One idea looked statistically convincing until split by year: all of its
   profit came from 2021.

So the process (code name: Progressive Evidence Model, in
`docs/governance.md`) is:

- **Explore freely** (Lane 1) on data we've already seen — unlimited
  experiments, honestly labeled as exploration. Numbers from here are
  ideas, not proof.
- **Lock in and grade forward** (Lane 2): when an idea looks promising, its
  exact rules are saved into the code history on a recorded date — that
  save *is* the registration; there is no other paperwork. From that day,
  every new market day adds one honest data point.
- **Promote or kill** with a five-line note: putting a rule into the live
  system normally requires its forward record to earn it; every deployed
  strategy also carries pre-written shut-off conditions (kill criteria)
  agreed before results existed, so a failing strategy cannot be argued
  back to life. You can order a change ahead of the evidence (an operator
  override — as you did on 2026-07-26); the record just has to say so.
- **Every simulation must**: use only information knowable at each
  simulated moment (point-in-time / causal), include realistic costs
  (measured, ~0.16% per round trip — one entry plus its matching exit),
  report each time period separately (era split), and log failed ideas in
  a list that blocks re-testing them unless someone brings a new
  mechanism, new data, or a fixed defect (the negative results ledger).

## 7. The story so far, and the answer about the "ladder"

- Until 2026-07-26 the pump-fade strategy ran as three copies of the same
  bet at different strictness levels, with money split between them. It
  looked like a gradual scale-in. It wasn't: checking every trade showed
  the three copies fired **in the same hour at the same price** more than
  four times out of five. It was really one bet, sized bigger when the
  spike was bigger.
- Its extra return came mostly from being in the market more — about 90
  extra days over three years, created by the extra bets (roughly 150–200)
  that the new crowd-fee rule refuses: the fee-bleeding kind. Those added
  return in good stretches and caused the deep losing streaks. You cannot
  keep the extra return and lose the streaks: they were the same trades. A
  small slice of the old edge — about one part in twenty-five — was also a
  simulation artifact: splitting one bet across three copies made its
  simulated price impact (the way a big order pushes the price against you
  as it fills) look cheaper than the one real combined order would have
  been.
- On 2026-07-27 we rebuilt the ladder on top of the new rule five different
  ways, plus a true gradual scale-in (a third now, a third an hour later, a
  third after that), plus the remaining dials (hold time, same-hour batch
  limits; re-entry spacing was only testable tangled together with hold
  time and stays unproven on its own). **No variant beat the simple single
  bet overall.** Every ladder rebuild lost outright. The true gradual
  scale-in did trim the worst dip a little further, but gave up return and
  tripled the number of settings to maintain, so it was not adopted. The
  other dials each sold the exact property (small worst-dip) the new shape
  was chosen for. Spike-size strictness had mostly been the crowd-fee rule
  in disguise; once you check the fee directly, checking spike size twice
  adds nothing.
- One refinement survived and is now on trial: the crowd-fee rule is too
  strict for coins that trade *only* on Bybit. When a coin trades on two
  exchanges, traders exploit any fee gap between them, which keeps its fee
  tied to real crowding; a Bybit-only coin has no such correction, so a
  negative fee there doesn't carry the same warning. Loosening the rule
  just for those coins improved every measure at once in simulation — but
  the gain is concentrated in 2025, so it was locked in (registered) on
  2026-07-27 and now has to earn its way on live days before it touches
  the running system.

## 8. Dictionary

Plain-first: in conversation we use the left-hand phrase; the code name
appears when we need to point at code, files, or records.

### Trading basics

| plain English | code name / jargon |
| --- | --- |
| bet that a price rises / falls | long / short |
| an open bet you are currently holding | position |
| contract that never expires, tracking a coin's price | perpetual, perp |
| the crowd fee: every 8h the crowded side pays the other | funding, funding rate |
| the last crowd fee actually charged (never a forecast) | settled funding |
| rising price forcing short sellers to buy back at once, pushing it higher | short squeeze |
| profit from being paid to hold, not from price moves | carry |
| a sharp fast price jump | pump |
| betting against a sharp move | fade |
| a crypto coin built to always be worth one US dollar; all amounts are quoted in it | USDT |
| money traded in a period | turnover |
| full market value of a position (price × quantity) | notional |
| cash locked up as a guarantee for a position | margin, initial margin |
| holding more than your cash by borrowing (caps how much cash is locked, does NOT size bets here) | leverage |
| one actual executed trade on the exchange | fill |
| average price actually paid, weighted by size | fill VWAP |
| the way your own order pushes the price against you as it fills | price impact |
| one entry plus its matching exit, as a unit of cost | round trip |
| automatic exit at a preset gain | take-profit, TP |
| automatic exit at a preset loss | stop, stop-loss, SL |
| exchange-stored loss exit that works if our server dies | native protection |
| order that can only shrink a position, never grow it | reduce-only |
| the exchange's live list of standing buy/sell offers | order book, L2 |
| one hour of price data | bar |
| typical size of a coin's daily swing | ATR |
| how bumpy a coin's price moves are | volatility, vol |

### Scores and measurements

| plain English | code name / jargon |
| --- | --- |
| smoothness score: return per unit of day-to-day wobble (1 decent, 2 very good) | Sharpe ratio |
| worst peak-to-bottom dip along the way | max drawdown, maxDD |
| pain-adjusted score: yearly return ÷ worst dip | MAR |
| how-unlikely-is-this-luck score (≥ ~3 is convincing here) | t-statistic, t |
| one hundredth of one percent | basis point, bp |
| before / after trading costs | gross / net |
| chart of account value over time | equity curve |
| smoothness score counting only days the strategy was in the market | active-day Sharpe |
| smoothness score counting every calendar day, flat days as zero | full-calendar (fc) Sharpe |
| result reported separately per time period, to catch dead effects | era split |

### The pump-fade strategy (CONTINUOUS)

| plain English | code name / jargon |
| --- | --- |
| the pump-fade strategy | CONTINUOUS, continuous_ensemble_v2 |
| the current single-rule version (since 2026-07-26) | single_fund0, active_single_fund0_tp12_sl35_v1 |
| the spike-detection pattern (3× activity + ~3% jump) | trigger, turn3_pop3 |
| the combined spike score coins are ranked by | composite |
| the top tenth of that ranking | decile 9 |
| a coin's own trend with the market's effect removed | residual momentum, RMOM |
| only trade when Bitcoin rose over the past 30 days | BTC gate, uptrend gate |
| the entry checks a candidate must pass | admission |
| only fade a pump whose buyers aren't being paid to stay | funding admission, funding floor, fund0 |
| candidate allowed in because its fee data was missing (essentially never happens) | unknown admit |
| skip the whole batch when >2 coins signal in the same hour | crowding, crowd-2 gate |
| no re-entry within 24h of the previous entry | cooldown |
| at most 25 open bets / 5 new per pass | capacity, reservations |
| one-hour wait for the bar to complete before entering | confirmation delay, entry delay |
| bets sized 1–4% of account: smaller for jumpy coins, larger for calm ones | inverse-vol sizing |
| bets cut to ~a third in an elevated (not extreme) Bitcoin-risk band, after 50 decisions of history | BTC-risk overlay, CTRL_BTC_RISK_70_90_35 |
| close every bet after 24 hours | max hold |
| one self-contained sub-rule with its own trade list | component, cell |
| several sub-rules run side by side with size shares | ensemble, weights |
| the retired three-copy version | the 3-cell ensemble |
| the small Bitcoin/Ethereum side position leaning against the shorts | hedge, hedge overlay |
| the frozen dataset the hedge sizes itself from | hedge model prior, warmstart |
| the step-by-step count of why candidates were skipped | funnel |

### The breakout-buy strategy (LONG)

| plain English | code name / jargon |
| --- | --- |
| the breakout-buy strategy | LONG, LongV11aDivWeekendVol |
| jump of ≥2.5× the coin's typical move | sigma trigger |
| closing near the day's high | close location |
| wait up to 6h for a ≥1% dip and buy it (buy at the deadline if none comes) | retrace entry |
| ten position slots of ~10% of the account each | slot sizing |
| positions 1.5× bigger on weekends | weekend multiplier |
| LONG's own halve-the-book size dial (per-strategy; pump-fade's is 1.0) | notional multiplier 0.5 |
| the largest total set of positions the strategy could ever ask to hold, checked in advance | worst-case registered envelope |
| the fixed list of coins the strategy may ever trade | frozen candidate list |
| coin scheduled by the exchange for removal | delisting, delivery time |

### The machinery

| plain English | code name / jargon |
| --- | --- |
| strategy program that only writes wishes, holds no passwords | producer |
| the one program that trades and keeps the records | account owner, account-execution service |
| a wish: "I want to hold this much of coin X" | target |
| the durable queue wishes go through | inbox |
| the decision core that merges wishes and enforces caps | account kernel |
| the append-only master diary that outranks every report | journal |
| any read-only view derived from the diary | projection |
| constant cross-check of our records against the exchange's | reconciliation |
| the exchange itself / the exchange's own signed answer | venue / venue truth |
| play-money exchange account (real exchange, fake money) | demo |
| simulated copy that never trades or logs in (public prices only) | paper twin |
| one strategy's slice of the account, judged on its own | sleeve |
| one pass of a strategy's decision loop | cycle |
| record of exactly why each coin was skipped this pass | cycle receipt |
| refuse to act when anything safety-critical is unverified | fail closed |
| holding nothing: no positions, orders, or pending wishes | flat, flatness |
| lock preventing two account managers running at once | lease |
| one supervised always-on program on the server | systemd unit |
| scheduled short job (hedge, data refresh, health check) | timer |
| the independent alert-only health checker | watchdog, liveness checker |
| the supervised deployment procedure you trigger on GitHub | rollout |
| tamper-evident file naming the approved code and settings | operational authority |
| verified snapshot of the exchange's per-coin trading rules (7-day life) | demo-rule receipt |
| recorded date-and-version marker when the live system changes | change point |
| deliberate fresh start of the account records (old ones archived first) | ledger reset |
| the switch that would allow real money (pinned off) | REAL_MONEY |

### Research and evidence

| plain English | code name / jargon |
| --- | --- |
| simulation replaying a rule over past prices (an opinion, not proof) | backtest, render |
| past data we've already looked at while inventing ideas | seen data |
| explore-freely mode on seen data | Lane 1 |
| locked-in rules graded on days they never saw | Lane 2, forward scoring |
| saving a rule's exact definition into code history on a date | registration (commit = registration) |
| the day-by-day record on unseen days — the only real proof | forward record |
| putting a rule into the live system (earned or overridden) | promotion |
| the five facts recorded for any promotion or shutdown | five-line note |
| pre-written shut-off conditions for a live strategy | kill criteria |
| you ordering a change before the evidence earned it | operator override |
| use only what was knowable at that moment | point-in-time, PIT, causal |
| failed-idea list; retesting needs a new mechanism, new data, or a fixed defect | negative results ledger |
| a number that failed an honesty check, kept for ideas only | diagnostic |
| bookkeeping of which data shaped which idea | provenance |
| the buy-when-shorts-pay-heavily idea on trial | carry_hold, lane2_carry_hold_v1 |
| the buy-winners-only-while-holding-is-free idea on trial | financed_leaders |
| the loosen-fee-rule-for-Bybit-only-coins refinement on trial | venue-scoped admission |
| market moves cancelled out of a performance number | hedged |

## 9. How we talk from now on

- Plain words first; a code name appears once in parentheses when we need
  to point at something in the code or records.
- Any performance number comes with its three companions: return, worst
  dip, and whether it is simulation (opinion) or forward record (evidence).
- "Simulation says" and "live record says" are always kept distinct.
- Anything in this guide that a future change makes stale should be fixed
  in the same change — this file is part of the system now.
