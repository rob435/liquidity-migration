# The Whole System in Plain English

Every code name in this project gets one plain-English translation here.
Convention: plain words first, code name in parentheses when precision needs
it. This file is not a source of truth — the code, [`STATE.md`](../STATE.md),
and [`docs/strategy_program.md`](strategy_program.md) are. Numbers here carry
an as-of date and drift. As of 2026-07-31.

## 1. The system in five sentences

1. Two automated strategies trade crypto perpetual contracts on Bybit with
   **play money only**.
2. **CARRY**, the crowd-fee collector, buys small coins whose short-sellers are
   paying heavily to stay short, and holds while that payment lasts. Live on the
   play-money account since 2026-07-29. **LONG**, the breakout buyer, buys large
   heavily-traded coins after a breakout and holds up to three days.
3. Strategy programs never touch the exchange: they write down what they wish to
   hold, and one separate program (the account manager) places orders, applies
   every cap, and keeps the records. Every position gets a disaster stop stored
   at the exchange, so it still works if our server dies.
4. Everything runs on one rented Linux server; you get an hourly Telegram digest
   and trade notices, and a separate watcher that cannot trade sends warnings
   when something looks broken.
5. A simulation score counts as evidence only once the exact rule is in the code
   history and gets graded on days it never saw.

## 2. The crowd fee (`funding`), because CARRY lives on it

Perpetual contracts never expire. To keep each contract glued to the real coin's
price, the exchange makes the crowded side pay the other side at fixed
settlement times. Crowded longs pay shorts; crowded shorts pay longs; at zero
nobody pays. We always read the last fee **actually charged**, never a forecast.

**How often varies by coin, and it changed under us.** Bybit sets the gap per
symbol — 8 hours everywhere in 2021, but by 2025 about half of all settlements
were 4-hourly and a fifth hourly. Our entry test reads one settlement, so the
same threshold means a different *daily* rate on a 4-hour coin than on an
8-hour one; 73–80% of the coins CARRY has held since 2025 settle faster than 8
hours. Normalising the test to a daily rate was tried and made it worse — the
sharpness of one print is doing the work. This is also where a research bug
lived: the old scorer charged some settlements twice
([`docs/carry_hold.md`](carry_hold.md) §0).

## 3. CARRY — the crowd-fee collector (`lane2_carry_hold_v3`)

What it believes: when the crowd betting against a coin gets desperate, the fee
they pay to stay in that bet overshoots, and whoever holds the coin gets paid
for absorbing the panic. Once a day, just after midnight UTC:

| Step | Rule |
| --- | --- |
| Rank | 100 most-traded Bybit coins by real traded value over the past day |
| Qualify | last **settled** crowd fee deeper than 10 cents per $100 in that one settlement, paid by shorts to holders |
| Refuse the grind | no entry while the coin is down 5–30% over three days — there the shorts are simply right |
| Refuse dead coins | no entry while the daily swing is under 5% — a pinned price has no squeeze fuel |
| Size | up to 10% of the book per coin, scaled by how much the crowd is actually paying; whole book ≤ 1× the account |
| Hold | keep it while the fee stays deep; leave when it normalises, or the moment it snaps back more than 30 bp over two days |
| Seatbelt | 35% disaster stop at the exchange — deliberately far away; every tighter stop tested on 1,670 historical trades made it worse |

It takes no profit target. A few big squeezes pay for many small losers;
roughly 6 trades in 10 lose.

## 4. LONG — the breakout buyer (`LongV11aDivWeekendVol`)

Top-50 coins by 90-day turnover, 30 days minimum history, today among the 10
most traded; Bitcoin and Ethereum both above their 30-day average; a jump of at
least 2.5× the coin's typical move over 1, 3 or 7 days; a strong close; daily
swing under 12% of price; signal under 24 hours old. It waits up to six hours
for a 1% dip and buys that, or buys at the six-hour deadline.

Ten slots of about 10% each, shrunk for jumpy coins, capped at 30% per
position, scaled by how calm Bitcoin is, 1.5× on weekends, then the whole book
halved by LONG's own size dial (0.5). Exits: 1.5 typical-daily-swings below
entry, 4 above, out by 3 days, no re-buy for 7 days.

## 5. CONTINUOUS — retired 2026-07-29

The pump-fade strategy: short small coins that spiked, wait a day for the spike
to deflate. Retired on your instruction, replaced by CARRY. Not killed by its
own rules — its last honest simulation was healthy but modest (+11.06%, worst
dip −1.84%, smoothness 1.45). Its code, its Bitcoin trend check, and its
Bitcoin/Ethereum hedge job are all dormant. Restarting it is a fresh decision.

## 6. Demo, paper, real money

| Name | Exchange | Money | Credentials | What it proves |
| --- | --- | --- | --- | --- |
| **demo** | real Bybit demo realm | none | yes, demo-only | real order handling, fees, crowd fees, outages |
| **paper twin** | none | none | none | the software plumbing, and only that |
| **real money** | Bybit mainnet | yours | none held here | nothing yet — never used |

The paper twin is being changed so it no longer decides for itself: it
republishes demo's targets verbatim (`paper-target-mirror`) and only executes
them. Two producers reading the same files seconds apart disagreed 6% of the
time — once opening and closing a TLMUSDT position demo never asked for, for
−70.73 USDT. One fleet decides, both execute, so every remaining difference
between the two books is execution and nothing else.

The same change fixes two things the twin got wrong: its account balance was a
fixed number, so it could never resize a position (0 resizes in 1,776 cycles
against demo's 366), and it was never charged the crowd fees it was supposedly
collecting.

**None of this is live yet** — written and tested, not deployed. Until it is,
the twin still runs its own producer with a frozen balance.

Arming real money is your act alone. The mainnet sleeves are off in the
repository, which a host edit cannot reverse. The steps are now commands rather
than hand-work, written out in [`docs/real_money.md`](real_money.md);
`scripts/ops.sh real-money preflight` reports what is still missing and changes
nothing.

## 7. The money limits

The caps live in one file per account. Play money uses
[`configs/operational.demo.json`](../configs/operational.demo.json); the
un-armed mainnet shape is
[`configs/operational.mainnet.json`](../configs/operational.mainnet.json).

| Limit | Play money (250,000 reference) | Mainnet shape (2,500 reference) |
| --- | --- | --- |
| Whole book | 500,000 | 5,000 |
| Any one coin | 125,000 | 1,250 |
| Cash locked as collateral | 250,000 | 2,500 |
| Leverage ceiling | 2× | 2× |
| Daily loss halt | not set | 250 |
| Per-sleeve share | not set | CARRY 2,750 · LONG 2,000 · CONTINUOUS 50 |

Leverage does **not** make bets bigger. Bet size is a percentage of the account
balance; leverage only changes how much cash each bet locks up.

What stands between the account and a bad day:

- **The daily loss halt**
  ([`account_loss_guard.py`](../liquidity_migration/policy/account_loss_guard.py))
  measures the day's loss against the day's *opening* equity, not a high-water
  mark, so a profitable morning cannot ratchet the trigger up. It has three
  answers: trade normally; take no new risk when the equity reading is too stale
  to judge (open positions stay under their exchange stops — flattening blind on
  a dropped feed would fire constantly); or flatten and stop, which never clears
  by itself. The anchor survives a restart, so a crash-loop cannot hand the day
  a fresh loss budget.
- **The wallet-anchored envelope**
  ([`equity_anchored_envelope.py`](../liquidity_migration/policy/equity_anchored_envelope.py))
  makes every cap above a ratio of observed wallet equity, not a number someone
  remembered to update. Equity down shrinks the caps immediately; equity up
  waits for a move past a 5% dead band; equity unknown moves nothing.
- **The per-sleeve partition** (in
  [`account_kernel.py`](../liquidity_migration/account/account_kernel.py)) holds each
  sleeve to its own share, so one strategy cannot eat the book, and **the
  exchange-side stop**
  ([`venue_protection.py`](../liquidity_migration/venue/venue_protection.py)) goes on
  in the same request that opens the position.

## 8. Deploying, and the correctness pieces

Deploying is: install the code stopped, activate, verify — or run the guarded
`rollout` when the account is flat ([`docs/operations.md`](operations.md)). What
each service runs is fixed in one file
([`scripts/run_authorized_runtime.sh`](../scripts/run_authorized_runtime.sh)): a
unit names a job, the script owns the whole command line, nothing can append to
it. Which shape is installed is a one-line marker at
`/etc/liquidity-migration/profile`.

Beside the money limits in §7 sit three correctness pieces: one owner per
account
([`account_owner_lease.py`](../liquidity_migration/account/account_owner_lease.py)), the
constant cross-check against the exchange
([`account_reconcile.py`](../liquidity_migration/venue/account_reconcile.py)), and the
checks that run before anything deletes a directory
([`reset_path_safety.py`](../liquidity_migration/ops/reset_path_safety.py)).

**Standing rule:** no new safety features, guards, or gates on an agent's own
initiative. Propose them; you decide.

## 9. How we decide what is true

**A simulation score is an opinion. A score on days the rule could not have
seen is evidence.** The process
([`AGENTS.md`](../AGENTS.md)): explore freely on data we have already
seen (Lane 1); when an idea looks good, save its exact rules into the code
history — that save *is* the registration, there is no other paperwork; from
that day every new market day adds one honest point (Lane 2); promote or kill
with a five-line note.

Our own failures set that bar: a flagship 2.73 smoothness claim collapsed to
−2.54% once the live stop was replayed; two cost errors each reversed a
conclusion; five of six effects found on Bybit failed on Binance; one idea's
entire profit came from 2021; and a fee-counting bug double-charged every
crowd-fee payment in research. Details in
[`docs/backtesting_errors_we_never_repeat.md`](backtesting_errors_we_never_repeat.md).

Honest position today:

- After the double-count fix, the original buy-when-shorts-pay idea
  (`carry_hold`) scores a corrected benchmark smoothness of **1.21 (t 2.31)** and
  does **not** beat the CONTINUOUS benchmark. The older 2.57 / t 4.87 figures
  are withdrawn — do not repeat them.
- The deployed `lane2_carry_hold_v3` variant scores better on paper (1.38 raw,
  1.71 on the benchmark window) but was selected on the same data that measured
  it, so it is an opinion until forward days grade it. Its registration records
  the caveats: midnight is the luckiest of twelve decision hours tested, and it
  earns most when the market is fearful.
- LONG's forward record is demo-only and too small to prove anything beyond
  "the machinery works."

## 10. Dictionary

Plain phrase on the left; the code name appears when we need to point at a
file, a record, or a log line.

### Trading

| plain English | code name |
| --- | --- |
| bet that a price rises / falls | long / short |
| contract that never expires, tracking a coin's price | perpetual, perp |
| the crowd fee: every 8h the crowded side pays the other | funding |
| the last crowd fee actually charged, never a forecast | settled funding |
| rising price forcing short-sellers to buy back at once, pushing it higher | short squeeze |
| profit from being paid to hold, not from price moves | carry |
| money traded in a period | turnover |
| full market value of a position (price × quantity) | notional |
| cash locked up as a guarantee | margin, initial margin |
| borrowing that caps locked cash — it does NOT size bets here | leverage |
| one actual executed trade | fill |
| automatic exit at a preset gain / loss | take-profit / stop |
| exchange-stored loss exit that works if our server dies | native protection |
| typical size of a coin's daily swing | ATR |
| one hundredth of one percent | basis point, bp |

### Scores

| plain English | code name |
| --- | --- |
| smoothness score: return per unit of day-to-day wobble (1 decent, 2 very good) | Sharpe |
| worst peak-to-bottom dip along the way | max drawdown, maxDD |
| pain-adjusted score: yearly return ÷ worst dip | MAR |
| how-unlikely-is-this-luck score (≥ ~3 convincing here) | t-statistic |
| before / after trading costs | gross / net |
| result reported separately per period, to catch dead effects | era split |

### The machinery

| plain English | code name |
| --- | --- |
| strategy program that only writes wishes, holds no passwords | producer |
| the one program allowed to trade and keep records | account owner |
| a wish: "I want to hold this much of coin X" | target |
| the durable queue wishes go through | inbox |
| the decision core that merges wishes and applies caps | account kernel |
| the append-only master diary that outranks every report | journal |
| any read-only view derived from that diary | projection |
| constant cross-check of our records against the exchange's own answer | reconciliation, venue truth |
| one strategy's slice of the account, judged on its own | sleeve |
| one pass of a strategy's decision loop, and its record of why each coin was skipped | cycle, cycle receipt |
| refuse to act when something safety-critical is unknown | fail closed |
| holding nothing: no positions, orders, or pending wishes | flat |
| lock preventing two account managers running at once | lease |
| the alert-only health watcher that cannot trade | watchdog, demo-liveness, mainnet-liveness |
| the supervised deploy you trigger on GitHub | rollout |
| measured snapshot of Bybit demo's real order rules — that realm rejects orders its own stated minimum accepts | demo rule probe |
| recorded date-and-version marker when the live system changes | change point |
| deliberate fresh start of account records, old ones archived first | ledger reset |
| the switch that would allow real money (off, and yours alone) | REAL_MONEY |

### Research

| plain English | code name |
| --- | --- |
| simulation replaying a rule over past prices — an opinion | backtest, render |
| past data we already looked at while inventing ideas | seen data |
| explore-freely mode on seen data | Lane 1 |
| locked-in rules graded on days they never saw | Lane 2, forward scoring |
| saving a rule's exact definition into code history on a date | registration (commit = registration) |
| the day-by-day record on unseen days — the only real proof | forward record |
| putting a rule into the live system | promotion |
| you ordering a change before the evidence earned it | operator override |
| use only what was knowable at that moment | point-in-time, PIT |
| failed-idea list; retesting needs a new mechanism, new data, or a fixed defect | negative results ledger |

## 11. How we talk

- Plain words first; a code name once, in parentheses, when pointing at code.
- Every performance number arrives with three companions: return, worst dip,
  and whether it is simulation or forward record.
- "Simulation says" and "live record says" stay distinct sentences.
- A change that makes this file stale fixes this file in the same change.
