# Strategy program

The single current authority for strategy evidence, direction, and next work.
`docs/research/governance.md` owns evidence policy, `STATE.md` owns deployed
state, and code/tests own implemented behavior. Dated history lives in
`CHANGELOG.md` and `docs/research/archive/`.

## Current truth

### What is deployed

- **`lane2_carry_hold_v6` is the CARRY profile on both producers.** The demo
  producer's book trades through the live engine under v6; the mainnet producer
  publishes a v6 target book that the funded engine reads in shadow — promotion
  changes what is published, never what is armed (`governance.md` §6). v6 is
  v5's book with ONE shape change: the depth ladder bends, so the size
  multiplier is clip((|trail_fund_24h|/ref)^1.5, 0.25, 1.0) instead of the
  straight ratio. Same names, same days; mid-depth names get less size, the
  floor and cap do not move. Promotion note (`governance.md` §3):
  - **Claim:** v5's book on ~3.5% less average gross; capital-normalised
    differential vs v5 **+0.43 bp/day mean across all 24 clock phases**
    (midnight +0.63, t 2.86) on seen data; own-capital deliberately a wash
    (Sharpe 1.842 vs 1.841, dip −18.6% vs −18.7%). Inherits v5's flow + whale
    halvings, so this is also the first deployment of both.
  - **Config commit:** registered 2026-08-19 (`configs/lane2_carry_hold_v6.json`,
    commit `50156e80`); producer switch `CARRY_STRATEGY_PROFILE=v6` → profile
    `carry_hold_v6_live_v1`; the journal filing id stays the version-free
    `carry_hold`.
  - **Forward record: 0 scored days** — registered and promoted the same day, so
    this rides on seen-data evidence and the owner's decision. v4 and v5 keep
    scoring; the v6−v5 capital-normalised differential is the experiment the
    forward record grades.
  - **Decision:** owner, 2026-08-19 ("get v6 live and running. implement it
    into live and get it deployed", then "the real money side as well").
  - **Date:** 2026-08-19. Change point = the deploy receipt in `CHANGELOG.md`.

  Registration evidence: placebo 0/20, exponent plateau 1.25/1.5/2.0 all
  t ≥ 2.7, positive **24/24** clock phases, no materially negative year. v6 is
  the sole survivor of a ~40-cell response-shape hunt, and the config's
  selection-debt block lists every closed sibling — smoothed flow/whale steps
  (wash/worse), softened persistence kill (worse), inverse-vol sizing (worse),
  depth cap raises (a 2025-26 regime bet, worse Sharpe at matched capital), age
  taper (episodes are 1-2 days), and the depth-conditional flow drop that passed
  era + placebo but failed the clock sweep 14/24. Negative worth keeping: the
  measured dose-response says the book's per-unit payoff is flat below ~1.4× ref
  and jumps above, but chasing that jump by raising the cap is regime-local, so
  the bend harvests the stable part only.

  Engineering note: v6 is the first deployed rule that reads a second venue.
  The producer keeps a per-symbol-day cache of Binance top-trader position
  long/short EODs (public endpoint, no key) — the live twin of the panel's
  `bn_tt_ls` — and every feed failure fails OPEN under the registered 48h
  freshness clause, degrading v6 toward v6-minus-whale rather than blocking a
  decision.

- **The carry early exit is deployed and fires before the print settles (v7).**
  A held name is sold at the first read at or above the registered −3 bp exit
  threshold — the K=1 cascade, all day, no new parameters. v7 changes an
  execution clock only: it trades `lane2_carry_hold_v6` byte-identical (one
  config id, its forward grade unbroken) and moves the fire from the settled
  print (sell ~S+1 min) to the venue's running rate read inside the last 15
  minutes before a held name's next settlement. The venue locks that rate ~55 s
  before it pays (tardis ticks; the S−1 read matched the final print 230/230
  walk-forward days), so this is the same registered −3 bp test on the same
  number, read early.
  - Evidence: on the cascade's own 1,112 fires the sell-minute curve is
    monotone — S−10 is +21.3 bp/fire all-in (median +11.3, t 4.9); the deployed
    continuous 15-minute window nets **+19.0 all-era / +28.3 bp per fire in
    2025/26** after the measured premature drag (~4 bp/fire-day; every-minute
    walk-forward, 230 held→fire days), beating flat S−10 (+16.6/+23.5) and the
    shrinking-margin variant (+9.2) in a 13-cell sweep. The S−30 first read was
    the runner-up (+20.2/+31.7) and was rejected for doubled premature days on
    half-formed hourly averages. Book-level ≈ +2.4–3.1 bp/day in 2025/26, ~0
    before.
  - The gap the owner accepted: read at print time, the full-day cascade is
    tail-exposed both ways — fires are 100% fresh-settlement events, medians
    +49…+150 per fire with ~59% of fires positive 2023–26 (trimmed ~+2.5–5
    bp/day book-level), but 2024's mean went negative on adverse tails, 2022 is
    flat, and the mean never clears the t ≥ 2.5 bar (pooled t 2.3, 2026 t 1.5).
  - Kill switches: `CARRY_STRATEGY_PROFILE=v6` (settled-print clock only) or
    `CARRY_EARLY_EXIT=0` (registered midnight clock).
  - Change point = the v7 deploy receipt in `CHANGELOG.md`. Forward grade:
    realized engine exit fills against the same-day settled-print
    counterfactual. Full numbers:
    [research_findings.md §Settlement-instant timing](research_findings.md).

- **`LongV12WideStop` is the LONG sleeve's deployed profile** (registered
  2026-08-01, deployed 2026-08-03; deploy receipt in `CHANGELOG.md`). v12 changes
  exactly one thing against v11a — the stop opens to 3× ATR and decays back to
  1.5× after 48h. Paired daily difference **+0.48 bp/day, t 3.27, n 1927**;
  total 38.5% → 51.6%, daily Sharpe 1.24 → 1.49, worst dip −4.4% → −3.9%, better
  or equal in all six years, and *less* concentrated (best-20 share 78% → 62%).
  Mechanism detail: `docs/trading_logic.md`.
  - Both halves must be wired. The wide initial stop alone
    (`fc_atr_stop_mult=3.0`, no decay) is t 1.84 — below the 2.5 bar — and costs
    drawdown (−6.6% against v11a's −4.4%). The runtime carries the pair: entries
    freeze a per-trade decay contract in their target metadata and
    `_plan_time_stop_exits` publishes a `decayed_stop_loss` zero target on a
    breached decayed stop; the wide half stays a venue-native resting stop.
  - **No config-only cell clears the bar**: a 12-cell stop × hold sweep (stop
    1.5/2/3/4 × hold 1/2/3) tops out at that same t 1.84, and shortening the
    hold is *not* a substitute for the tightening — stop 3× at hold 2d is
    t −0.28, at hold 1d t −1.78. Cutting every trade at two days is worse than
    leaving them; the value is in cutting only the ones that are losing, which
    is what the decayed stop expresses.
  - Measured and do-not-retest, from the same sweep: **every funding gate on the
    LONG event fails** (16 cells, none beat 1.24 — on the days LONG fires,
    median 3d funding is +9.0 bp and only 12.7% are ≤ 0, so carry's condition
    does not transfer); **every "sell into strength" rally exit fails**
    (trailing, breakeven ratchets, exit-on-lower-close: 15 cells, best 1.17);
    **loss-only cooldown fails** (0.87); **concentrating on the best 1-2
    candidates a day fails** (t −2.38 / −2.00).
  - CARRY and LONG v12 correlate **+0.012** across all 24 decision clocks — at
    equal risk the pair is 16.56 bp/day, Sharpe 1.81, worst dip −24.2%, against
    carry alone at 14.46 / 1.13 / −45.6%.

- **`lane2_exodus_short_v1` is REGISTERED and DEPLOYED to the demo fleet as a
  third engine sleeve** (2026-08-20, owner: "build the exodus short as a
  standalone strat sleeve, but synergising"). When carry's v7 pre-settle exit
  fires, this sleeve takes over the abandoned position as a short and covers 60
  minutes after the settlement — the measured bottom of the post-settlement
  fall. Standalone at the engine (own `[[strategy]]` block, book, fill
  attribution), produced inside the carry process because the trigger IS carry's
  fire. Promotion note (`governance.md` §3):
  - **Claim:** the book's own fires leave the larger half of the move on the
    table; shorting through it earns **+95 bp mean / +50 median per clean
    event** net of the 15.56 bp round trip, **+6.1 bp/day book-weighted
    overlay** with the 18 real premature fires charged at their measured 7.8%
    rate — but 2024 is negative (−0.8) and 2021–23 flat: a priced regime trade
    on the 2025–26 farmer crowd.
  - **Config commit:** registered 2026-08-20
    (`configs/lane2_exodus_short_v1.json`, this commit); dial
    `EXODUS_SHORT_PROFILE=v1` on the demo carry unit, mainnet unset.
  - **Forward record: 0 scored days** — rides on seen-data evidence and the
    owner's decision. The first demo weeks measure the kline-vs-fill gap; fires
    are graded per event from the engine's own WAL fills.
  - **Decision:** owner, 2026-08-20. The stop question was settled by
    measurement before the config froze: every strategy-level stop from +30 bp
    to +1500 bp loses against the time-boxed cover, so the declared 0.35 stop is
    a disaster fence, carry's exact posture.
  - **Date:** 2026-08-20. Change point = the deploy receipt in `CHANGELOG.md`.

- **Entries rest at the touch; they do not cross the spread** (owner
  instruction, 2026-08-04). Both account owners place an exposure-increasing
  entry as a GTC limit resting at the touch, chase a touch that moves away every
  15s, and at 120s amend the price through the far touch so the remainder fills
  as a taker at a bounded price; a remainder the cross cannot clear within 20s
  is cancelled and the owner's convergence machinery re-plans it. Exits,
  resizes, and native stops are taker.
  - Measured arm behind the recipe — seg00 (Buy, reprice 15s, timeout 120s, 34
    symbols, n=1,586 attempts): **70.4%** filled passively, median time-to-fill
    41.6s, clean all-in cost median **1.9 bp/side** against the fleet's measured
    7.78 bp taker basis. The full night fit (n=12,656 across all six arms plus a
    repeat) keeps it as shipped: the Sell side quotes as well as the Buy side
    (the short entries carry makes are covered), the slower 30s/180s arm ties on
    cost while exceeding the owner's 120 s sibling-batch budget, and the
    10s/60s no-chase arm is rejected.
  - An entry larger than the displayed touch arrives as a sequence of
    touch-sized quote windows instead of one resting order (owner: "prepare for
    big sizing, up to 5,000 USDT notional") — the measured touch
    on the thin half of the universe holds only 23–181 USDT, so this is the
    difference between joining the queue and being the whole market. Large
    entries take minutes instead of seconds, in exchange for staying
    maker-priced; ungraded until real entries at size produce receipts.
  - The resting recipe places by the displayed touch sizes (improve into the
    spread when the book leans toward the entry, rest one tick behind when it
    leans hard against), escalates with the clock, and crosses early once the
    mid has run against the entry past twice the half-spread-plus-taker-fee.
    Selected on a 199,785-attempt queue-honest replay of the full overnight
    tape: **−0.36 bp/entry** against the touch-resting recipe above, t = −11.1,
    deadline crosses halved. The churn alternatives (reprice on every touch
    move, toxicity brake) measured *worse* and are recorded as negative results.
  - Out-of-sample honesty (13 unseen daytime hours): the cost edge is a
    night-regime effect — daytime it reads zero — while the halved deadline
    crosses and faster fills hold in both regimes; the fleet enters at 00:20
    UTC, in the measured regime.
  - **These are execution change points for both sleeves' forward records**
    (all 2026-08-04; deploy receipts in `CHANGELOG.md`): entry fills turn
    maker-heavy, entry prices move from crossing to the touch, entry cost per
    window should fall ~0.2–0.4 bp on overnight entries, and window-end taker
    crosses should roughly halve everywhere. Graded on funded `is_maker`/fill
    receipts as they accrue. Evidence:
    `docs/research/research_findings.md` §1 and `~/Desktop/quote-forge/FINDINGS.md`.

- The publishing profiles are `lane2_carry_hold_v6` (CARRY), `LongV12WideStop`
  (LONG), and `lane2_exodus_short_v1` (exodus short). All are runtime
  configurations, not validated alpha claims; `deploy/sleeves.env` and
  `STATE.md` are the authority for what publishes.

### Registered, not promoted

- **`lane2_carry_hold_v5` — registered 2026-08-19, research-only**
  (`configs/lane2_carry_hold_v5.json`; owner: "do an A/B test and fit it into
  our system"). v4's book plus two size halvings on axes outside the
  funding/price complex: stale turnover flow (growth ≤ +40%/3d) and Binance
  top-trader de-longing (ratio change ≤ −0.26/3d), composing with depth and
  persistence. The registered experiment is the capital-normalised daily
  differential vs v4: **+6.13 bp/day (t 3.30)** at midnight, positive 24/24
  clock phases (mean +3.10 — cite the mean), own-capital a wash (+0.18, t 0.11)
  — a capital-efficiency claim, v4-over-v3's shape. Scale-free: Sharpe
  1.62 → 1.84, worst dip 24.5% → 18.7% at own capital. Read the selection-debt
  block in the config before citing anything: both features came out of a
  ~60-cell one-day search, the era gain is 2025-26-concentrated, and neither
  component clears the bar alone. Data seam: the whale leg reads the public
  Binance metrics archive (`scripts/data/refresh_binance_metrics.py` → panel
  `--metrics-root`, `bn_tt_ls` columns; nulls fail open, 81% held-name-day
  coverage at registration). v4 keeps scoring untouched; the v5−v4 differential
  is what the forward record grades. Promoting it to a sleeve is a separate
  owner decision with its own note.

- **`lane2_carry_hold_v4` — registered 2026-07-31**
  (`configs/lane2_carry_hold_v4.json`), the CARRY profile from 2026-08-03 until
  v6 replaced it. v4 adds a crowding-persistence size multiplier and moves the
  toxic band's high edge to 0%; its claim is capital efficiency (v3's book on
  ~30% less capital) and not return — capital-normalised differential vs v3
  **+10.76 bp/day (t 3.23)** on seen data, own-capital +1.07 (t 0.47, not
  significant), with Sharpe 1.41 → 1.64 as the scale-free statement. Detail:
  `docs/research/carry_hold.md` §0.1. **The journal strategy id stays
  `carry_hold_v3`** — a frozen lineage key, documented at the constant. v3 keeps
  scoring as the primary comparator; the v4−v3 paired differential is what the
  forward record grades.

### Standing rules

- **The significance bar is `t >= 2.5`** (owner decision 2026-07-31; authority
  `docs/research/governance.md` §2). It is prospective — verdicts recorded
  before that date stand as recorded. It does not control family-wise error, so
  a survivor needs a reported plateau and a failed placebo beside the number.
- No researched replacement currently qualifies for implementation.
- Passive execution: the measured floors stand
  (`docs/research/research_findings.md` §1). The instrument that survives is
  `scripts/research/probe_passive_fill_ab.py` (protocol in
  `liquidity_migration/research/execution/passive_fill_probe.py`, ITT
  accounting, written kill criteria) — it bounds the mechanism in hours and
  answers whether the 5.40 bp passive floor is mechanically reachable, and only
  that. Blocked on demo credentials this box does not hold; run with the fleet
  stopped and flat.

### Closed, with receipts

- **Settlement sawtooth — CLOSED 2026-08-01** (kill criteria 2 and 4 fired). The
  step is arbitrage-free by construction — slope 1.0340 on 365,691 settlements,
  net to a long zero at every depth — and every trade tried there is dead. Two
  durable bounds survive it and must be quoted before anyone re-proposes either:
  **the carry book's price leg cannot be hedged** (a per-name Binance short
  removes 94% of the price variance but eats 74% of the funding — neutral Sharpe
  0.62 against directional 1.24), and **the settlement-window trade needs a
  zero-latency exit** (Sharpe 2.96 at zero lag, −2.14 at one hour). Dossier:
  [`archive/2026-08-01-settlement-sawtooth-program.md`](archive/2026-08-01-settlement-sawtooth-program.md).
- **Settlement-instant timing on the v4 book — CLOSED 2026-08-03.** Entering
  just before the fee to collect it, shorting the post-fee crash (including a
  cadence-aware exit that never pays funding: −29.6 bp/event, t −4.1, negative
  in all six eras), and every entry/exit fill delay up to 12h are measured dead.
  One accounting fact survives: the scorer's funding-boundary convention
  understates carry configs by ~+0.5 bp/day at midnight, 24/24 phases. The
  deployed ~00:20 entry fill stays as-is — it saves ~42 bp per entry. Receipts:
  `docs/research/research_findings.md` §2 "Settlement-instant timing" and §4.
- **Idio charts — CLOSED 2026-07-30** as a Sharpe upgrade for this book. Across
  the three pre-declared screens, **0 of 96 cells are profitable and
  significant**: residualisation yields a real signal that does not pay this
  cost stack, and COMMON4 explains only **6.0%** of daily cross-sectional
  dispersion. Two reusable defects went into the failure taxonomy as items
  **34** (log returns as a P&L target — a −34.76 bp/day variance drag that
  manufactured an apparent Sharpe 4.46) and **35** (full-rebalance cost models).
  Dossier:
  [`archive/2026-07-30-idio-charts.md`](archive/2026-07-30-idio-charts.md).
- **The anomaly search — 37 mechanisms under one harness (2026-07-24).**
  Survivors are cross-venue premium divergence and 1-week cross-sectional
  momentum, both concentrated in the *most* liquid names and effectively
  uncorrelated (+0.009). Funding carry broke in 2025-26 exactly when funding
  inverted. **Venue volume-share migration — the most direct test of the
  Crowding Transfer hypothesis below — is dead**: the price dislocation pays,
  the flow migration does not. The premium leg is Bybit-local (23.81 bp, t 2.06
  at 24h, against Binance's 11.42, t 1.01; adding a Binance leg dilutes to
  17.62), so **true cross-venue execution is not worth building for this
  signal**. Hold 24h: under disjoint sampling t peaks at 24h (3.48) and falls to
  1.18 at 168h. Under settlement-exact funding the leg attribution reverses
  (premium 33.63 → 16.55 bp, momentum 16.98 → 35.42 bp, blend unchanged at ~26),
  and the dispersion gate is withdrawn as an artifact of the funding
  approximation. The delisting-decay lead is withdrawn as a look-ahead label
  (220.8 bp/day): turnover collapse identifies dying contracts at **0.96× lift**
  and pays *more* on contracts that never died (+38.0 bp, t 4.26), so the
  residual is generic "short low-turnover". Dossier:
  [`archive/2026-07-24-anomaly-research.md`](archive/2026-07-24-anomaly-research.md).
- **The 2026-07-25 instrument-repair and program phases (1, 2A/2B, 5) — CLOSED.**
  The anomaly program's conclusion is economic: the durable premium is
  compensation for liquidation risk this capital structure cannot survive, and
  no construction the repository can express clears the bar. Full phase record
  and the dated change points through 2026-08-03:
  [`archive/2026-08-03-strategy-program-change-log.md`](archive/2026-08-03-strategy-program-change-log.md).
  New change points are recorded here, then decanted there.
- **The financed-longs program (2026-07-26) — its three registrations are
  deleted.** Registered tables and the 22-row negative-results ledger:
  [`archive/2026-07-26-financed-longs.md`](archive/2026-07-26-financed-longs.md).
- **CONTINUOUS and its venue-scoped admission variant — retired** (sleeve
  retired 2026-07-29 by owner override, code out of the tree 2026-08-14). It
  cannot serve as a control for anything. Evidence and design constraints:
  [`archive/2026-07-27-continuous-ladder-mechanism.md`](archive/2026-07-27-continuous-ladder-mechanism.md)
  §5.

## Theses — measured, not registered

Ideas that have been built and measured but are **not** a deployed sleeve and,
in most cases, not a registered config either. Each entry says what it is, what
was measured, and the specific thing that keeps it out of the book — so a
promising-looking number is never re-discovered without its disqualifying
context attached. Confirmed dead ends belong in the do-not-retest ledger in
`research_findings.md` §2, not here; this is for things that *work* and still
are not run. Nothing here has a forward record: every number is Lane-1
simulation on data that also shaped the idea, under `governance.md`.

### 1. Financed leaders and funding spread — deleted, do not rebuild

Both non-carry funding books are gone — configs (`lane2_financed_leaders_v1`,
`lane2_financed_leaders_binance_v1`, `lane2_funding_spread_v1`), their scorer
code, and their forward-ledger slots (owner: "kill everything that's not
carry-hold and LONG"). The reasons they were never run are the fence: financed
leaders was carry wearing a costume (+0.544 correlation to carry_hold v4, 14.32
bp/day Sharpe 1.02 — no third bet, just the first one at extra complexity), and
the funding spread never beat its costs at the measured 2-leg fee. The idio
screen family (panels, screens, its panel builder) went in the same wave; its
program had already closed 0/24 hedged cells. Dated dossiers in `archive/` and
the ledger rows in `research_findings.md` §2 keep the numbers; old ledger CSV
rows remain as receipts.

---

### 2. The liquidity screen inside carry-hold

**What it is.** Restrict carry-hold's candidates to the most liquid quartile
(`pct < 0.25` by trailing turnover).

**Measured:** +15.23 bp/day against v4's +14.46 at constant capital, Sharpe
1.13 → 1.18, better in 24/24 clock phases. The underlying cross-sectional fact is
strong: the most-liquid 5% of crowded names earn **+354 bp/day** (t 3.39 on
disjoint sampling; +317.5, t 3.12 excluding ALPACA).

**Why it is not run.** The book is candidate-poor, not capital-poor. carry-hold
holds about 2.2 names at 9.4% gross with `gross_cap` unused at 1.0, so roughly
90% of the sleeve sits in cash. Removing a candidate from a 3-name book costs
more than the losers it avoids — the config's own `rejected_in_review` note says
so. Every conditioner measured this way is real in the cross-section and
unusable in the book. Ceiling across all of them: ~+15.5 bp/day, Sharpe 1.20.

**The trap this taught.** Gross-matching is the right filter for a rule that
*adds* exposure and the wrong one for a *screen* that removes it — rescaling a
shrinking book breaches `per_name_cap` and reports leverage as alpha. Screens
must be tested as runnable config, with max drawdown and cap-breach printed next
to bp/day.

---

### 3. Capital efficiency — the largest unexploited lever, and it is not alpha

Both deployed-shape sleeves are tiny. LONG deploys **2.7%** of account equity
averaged across all calendar days; carry_hold uses **9.4%**. Together the
two-sleeve book puts about 12% of the account to work.

`max_concurrent_positions` is a pure size dial for LONG, not a capacity
constraint: the book holds roughly one position at a time and the 10 slots never
bind (`skipped_capacity: 0` across the whole history). Halving it to 5 doubles
position size and takes total return 38.5% → **85.8%** at Sharpe 1.24 → 1.27 —
i.e. **at no measurable risk-adjusted cost**.

**Why it is not done.** It is an envelope and margin decision, not a research
one, and it is the same ask the owner already declined: `notional_multiplier`
1.0 was refused on 2026-07-28 because it needed roughly 4× the envelope. Equal
risk against carry would need 8.5×. Two of the three names in
`LongV11aDivWeekendVol` — the weekend 1.5× boost and the BTC-vol scalar — are
this same lever already in the profile, and both *cost* Sharpe when widened.

---

### 4. Pre-settlement (23:00) entry — refuted, with both signals measured

**What it was.** The registered engine decides at the midnight bar, so a
position opens one hour AFTER the 00:00 settlement and never collects the entry
print itself — 291 of 1,833 v6-book entries forfeit a mean +41.8 bp print,
+12,161 bp raw over 4.9 years (~+0.3–0.5 bp/day at entry weights). The idea:
enter at 23:00 when the venue's RUNNING funding rate already qualifies,
collecting the print. Pool-level tardis numbers looked good (positive median in
all four eras).

**Why it is dead.** Simulated on the book's OWN eligible entry moments across
the 44 tardis days, only **1 of 16 had the signal at 23:00** (the pool ran ~50%;
P(≤1/16 | 0.44) ≈ 1e-4). The pool's early-warning story came from
chronically-deep 4h/8h tail names; the book's fresh entries are top-100
1h-interval names whose displayed running rate is **baseline-anchored** (+0.1 bp
reset) until late in the final hour — and for those symbols the tardis
`funding_rate` field often never converges to the settled print at all
(last-minute −2.4 vs settled −82; +0.1 vs −193), which also taints every
pool-level capture estimate. The forfeited-print accounting stands; there is
simply no reliable one-hour-ahead signal to collect it.

The raw mark−index premium (the primitive, which cannot be baseline-anchored)
was then measured on both sides. Sensitivity is real — the (22:00, 23:00] mean
premium at ≤ −10 bp fires on **75%** of book-eligible deep prints, against 6%
for the displayed rate — but precision is fatal: scanned over ALL 4,398 top-100
tardis name-days it fires on 12% of them and only **4% of fires confirm** (19
captures, 511 false positives). True positives pay +188 bp mean; the blend is
era-unstable false-positive price drift (2023/24 "profit" with zero confirms)
and runs **−33 bp per fire in 2026**, the only era with real captures.
Tightening the threshold from here would be mining n = 19. **This door is closed
with both signals measured.**

---

### 5. The two-leg exit clock — both legs deployed

The book's names drift down after the 00:00 settlement, and on exit days they
leak price all evening; the 00:20 sell sat at the bottom of both. Leg A is the
deployed early-exit clock (the settled-print fire, generalized by the v7
pre-settlement read). Leg B is folded into the strategy itself (owner swept
the dial away 2026-08-23): a held name the upcoming midnight decision zeroes —
universe rank, persistence cut, suspend — sells at the first post-midnight
cycle off the swept print and WS-served bars instead of on the 00:20 REST-era
margin; a dirty build degrades that day to the old clock.
Measured on every held name-day 2021–2026 with all-in accounting:

- **Leg A (fee recoveries):** selling at the recovered print beats the 00:20
  fill by +21.3 bp per fire (t 4.9); deployed as v7.
- **Leg B (universe/persistence drops):** those exits leak **+74/+43 bp pooled
  between 23:55 and the 00:20 fill (t 3.5)** — but only +18/+15 in 2026 alone
  (t 0.8), so the live edge is the era-weak tail of the measurement. Selling
  all exits at minute ~3 beat 00:20 by +24 bp pooled (+12 in 2026),
  weight-summed **+0.6 (2026) to +3.8 (2025) bp/day, positive all six years**
  — measured before v7 existed, so the fee-recovery share has since left the
  residual population.

The known cost: the early freeze samples its ticker snapshot minutes before
the authoritative rebuild, so a name kept by the 00:20 computation can be sold
once and re-bought (~15.56 bp round trip) — the same documented residual the
pre-deadline freeze-ahead already carries for the whole book. Entries never
move early (filling into the post-payment dump costs −46 bp/entry; the entry
clock is measured-optimal where it is). Full grids, including the refuted
adaptive entry-sniping arms:
`research_findings.md` §Settlement-instant timing.

---

### 6. Two-book portfolio — measured 2026-08-19

On the 1,747 shared days (2021-10-05..2026-07-17; LONG leg = the on-disk
2026-07-24 mark-to-market build; equal-risk = inverse full-window vol,
in-sample): carry↔LONG correlation is **+0.002** and ~0 in every era, and
**carry_v6+LONG at equal risk is Sharpe 2.15, worst dip 3.6%**. The equal-risk
pair is 89% LONG by capital because LONG runs ~27 bp/day vol against carry's
~225 — converting Sharpe 2.15 into money is the envelope/leverage decision the
owner declined on 2026-07-28 (`notional_multiplier` 1.0 needed ~4× the
envelope), not a research output. Scratch: session artifact
`three_book_portfolio.py`.

A third book — the premium/momentum blend `lane2_premium_momentum_blend_v1` —
was tested the same day, LOWERED the portfolio (1.99 against 2.15 without it),
and was **deleted by operator override**: config, module, tests, and its
phase-1 screen harness. Do not rebuild it; the receipt is in
`research_findings.md` §2 and the dated archive dossiers.

---

### 7. Premium divergence as a LONG entry filter — measured null at available power

Joined PIT `premium_diff_bp` onto all 292 LONG trades (97% coverage): quintile
means +9.4/+11.0/+8.7/+14.8/+16.4 bp per trade — a ~7 bp spread in the WRONG
direction (Bybit-rich entries mildly better), far inside noise at n≈57 per cell,
and the book fires too rarely (~1 trade/week, essentially all one pattern) for
any per-era read. Not worth a config; re-open only if LONG's event rate grows
several-fold.

---

### 8. LONG v13 rework — closed, v12 stands; one forward experiment survives

25 cells over the full 2021→2026-08 window through the registered kernel
accounting (exit re-anchoring, hold extension, information exits, the
price-volume alignment factor, intraday rolling-24h entries, gated hybrids,
fallthrough removal): nothing clears the t ≥ 2.5 bar and most lose. The
surviving fact is a decomposition — the intraday entry earns **+16 bp/trade
(t 3.76) on the pumps that go on to confirm the daily close** and loses it
all on the pumps that do not; no mechanical trigger-time gate separates the
two well enough. Full tables:
[archive/2026-08-21-long-v13-rework-program.md](archive/2026-08-21-long-v13-rework-program.md).

**Exit conditions on alternative data — closed 2026-08-21 (Lane 1).** The one
untouched exit surface — mid-hold exits keyed on Bybit open interest, perp
premium, settled-funding state, BTC shocks — was run through the registered
kernel with a trade-for-trade harness identity check: OI exits are null
(concurrent with price, not leading; ρ +0.35 over the hold converts into a
worst-cell −0.21 bp/day t −1.65 rule), premium-collapse and funding-flip
exits are significantly **harmful** (t −2.39 / −2.54), BTC shocks never cross
a 3-day hold. **Divergences and the settlement clock — closed the same day**
(atlas on all 18,986 held bars of the registered book): selling at any of 9
divergence constructions yields 62–171 bp less per fire than holding to the
deployed exit, the least-bad one (new-high on fading volume) still loses
through the kernel (−1.06 bp/day t −2.66), cross-venue premium negativity is
significantly worse than neighboring bars, and held pump names RISE through
their settlements (+22.7 bp/4h) so carry's pre-settlement exit clock transfers
as harm (−0.28 t −2.27). The deployed v12 exit stack stands as measured-optimal
across ~200 price/turnover cells plus alternative-data states, divergences,
and the settlement clock. Receipts:
`~/SHARED_DATA/bybit_full_pit/reports/long_exit_altdata_2026-08-21/` and
`.../long_exit_divergences_2026-08-21/`, ledger rows in `research_findings.md`.

**Active forward experiment — the driver-judgment ledger**
(`scripts/research/llm_driver_ledger.py`). Nominates live movers from public
tickers, enriches each with the facts a judgment needs (funding, perp
premium and its 24h path, open-interest change over 24h and 48h, BTC/ETH
beta context, distance from the 30d high, volume vs the coin's own 90d norm,
vol-adjusted depth, listing age),
then has a language model walk a fixed step-rubric — identity, beta check,
leverage-vs-organic flow, structure, driver hypothesis, this repo's measured
priors — and journal every step's answer BEFORE the outcome exists. Grading
prints the 72h forward return by prompt version, row type, and judged driver
kind, so a failed grade localizes to the step that failed; a rubric change is
a new prompt version and grades separately. Beside the mover rows, an hourly
scan journals fresh trigger events on the 1/2/4/12/24h rolling windows (each
window's bar is the daily 2.5σ trigger scaled by √time; only the 24h window
has ever been measured) with the trigger-hour price and a would-enter at
score ≥ 6. Since 2026-08-21 those judged events ARE LONG entries: each
score ≥ 6 event is published to the LONG sleeve's candidates file and enters
through that sleeve's own sizing, exits, and stops — one strategy, one book;
mechanics in `docs/trading_logic.md` §LLM GATE. The forward record grades
real fills beside the shadow rows; the registered lane-1 prior is that no
mechanical gate on these events cleared the bar, so this rides entirely on
the judged discriminator and the owner's decision. The fact set is v6 as of
2026-08-21 (leverage-flow paths added; the rubric states plainly that the
desk measured no mechanical edge in them). Forward-only by
construction: a model judged on historical pumps already knows how they
ended. Armed by `DEEPSEEK_API_KEY` (or `LLM_API_KEY`; any OpenAI-compatible
endpoint via `LLM_BASE_URL`/`LLM_MODEL`); without a key it still journals
nominations.

### 9. Genuinely open

- **Per-symbol coordination between the two sleeves.** They collide on 11
  name-days in 5.5 years, and nothing sizes them against each other on a name —
  the caps are account-wide and per-symbol exposure is not one of them. Small,
  but it is the only genuine coupling between them.

## Priors from the 2026-07-21 reset

What the audit left standing, and the reset research read behind it. All of it is
Lane-1 work on already-seen local data: it shaped the new plan and cannot grade
it. The old reserved V2 label tape was not opened and is not earmarked for
the new program — a descendant would inherit too much design exposure, while a
genuinely new strategy is better graded on post-commit days.

| Evidence | Decision-useful conclusion | Decision |
| --- | --- | --- |
| Strategy Overhaul V2 | About 29 families and more than 150 configurations exhausted the existing hourly entry/exit/sizing surface. Fixed-capital barebones books were approximately -3.23% LONG and -20.23% CONTINUOUS after modeled costs and funding. Full account parity was not established. | Stop tuning descendants of that surface. |
| Historical sleeve curves | Some are positive, but LONG is materially dependent on a small take-profit tail and CONTINUOUS has no complete live-runtime reconstruction. | Keep as descriptive controls, not promotion evidence. |
| Breadth study | CONTINUOUS increased from about 6.55 to 7.30 bets per open day, but per-bet volatility was about 1,000 bp and average dependence about 0.21. A 25 bp effect would need roughly 5.6 years at that information rate. | Breadth alone is not a research direction. Fix quantization only as an execution-validity issue. |
| Young-listing lifecycle | The 2021-24 unconditional short effect reversed in 2025-26. A day-0 long was negative or flat. The required listing-week 1-minute cost data had zero symbol/date overlap with the 27,398-row event panel. | Retire calendar-age rules and the proposed T-L v2. |
| Execution cost | The first 23 measured demo fills showed positive 15-second/1-minute realized spread against our taker flow. The in-flow maker-first A/B froze at 2 of 8 fills when CONTINUOUS retired and was itself retired with the paper fleet (`docs/research/research_findings.md` §1). | Continue measuring execution separately; do not confuse cost improvement with alpha. |
| Cross-venue follow-ups merged 2026-07-21 | A Bybit turnover-collapse listing short looked strong by era (+247/+246/+510 bp at day 2) but failed in every Binance era (-415/-41/-290 bp). Hedged extreme-funding carry was negative across every declared arm on both venues. Naive pump-event longs were negative in 23 of 24 venue/era cells; D9 and BTC-uptrend short-path differences were only about +26 to +62 bp and uncertain. | Preserve venue divergence, the post-2025 negative-funding explosion, and the small D9/uptrend directional effect as anomaly leads. Retire the fixed admission bars, bulk reports, and one-off runners. |
| Book-level overlay follow-ups | A monotone BTC-risk intensity bought roughly 19-33% tail relief for about 3.8 percentage points/year of net premium on the deployed-shape render. A realized daily loss budget helped mainly on the negative barebones surface, while a cluster cap never bound the deployed-shape book. | Priced, regime-dependent insurance diagnostics, not automatic governors. Retire the staged hardcoded implementations; revisit through open anomaly research if new evidence warrants it. |

**Young listings and mature-symbol turnover decay: dead, in compact form.**
Young listings: six pre-declared event-day-2 rules, honest costs and funding,
block bootstrap — the turnover-decay short was positive in aggregate on nine
2021-22 observations with every era-specific interval crossing zero (a mechanism
lead, not a candidate), and persistent-attention continuation was directly
refuted (n=98, CI −1,341 to −105 bp). Mature symbols: falsified on the canonical
daily panel (889 symbols, 2022-01..2026-07) — pooled means near zero with severe
era dependence, and the screen omitted funding, so it was optimistic for shorts
even so. Price extension, listing age, and turnover retention are context, not a
standalone signal.

## Starting hypothesis, not mandated direction: Crowding Transfer

The first promising question changes the object being predicted. Instead of
asking whether a coin that pumped will continue or reverse, it asks whether
leveraged demand is moving into or out of **Bybit relative to the broader
market**. This is a place to start, not a prescribed destination. Research may
falsify it, split it into narrower mechanisms, or replace it with a more
interesting anomaly discovered in the data.

### Mechanism

1. Ask whether causal Bybit-minus-Binance premium, settled funding, or price
   basis describes local crowding better as a level, change, acceleration, or
   disagreement among measures.
2. Ask whether open-interest, taker-flow, turnover, or price transitions lead
   or lag that crowding, and whether the answer changes by regime.
3. Test whether any apparent effect survives removal of BTC/ETH beta and common
   cross-sectional moves; do not assume the correct trade is a naked short.
4. Study long and short asymmetries independently. Do not force symmetry or a
   matched pair when the data suggests only one side is interesting.
5. Treat response horizon and normalization behavior as research surfaces.
   Exchange-native disaster protection remains an account safety layer, not an
   alpha parameter to mine.

If supported, this family would differ from both active sleeves: the signal
would be a cross-venue state change, the portfolio could be beta-controlled,
and the trade would not require calendar age, a pump threshold, or broad market
direction.

### Feasibility already checked

Measured 2026-07-24. The current tiered census is `docs/data.md`.

- Bybit hourly premium, funding, index, mark, and open-interest partitions span
  `2021-01-01` through `2026-07-17`. **Bybit open interest is the deepest
  unused asset: 2,024 daily partitions growing 6 → 636 symbols.**
- Binance hourly premium and funding span late 2019 through `2026-07-17`.
- Common-symbol counts on the latest partition: 579 klines, 566 premium, 466
  funding; 466 symbols carry both-venue kline + funding + premium together.
- **Bybit `taker_flow_5m` and `tick_ohlc_1m` are not panels.** They hold 401
  distinct symbols but a *median of 11 days each* (min 1, max 78), scattered
  `2023-03-29` through `2026-05-24` — event windows, not cross-sectional
  coverage. `positioning_lsr` and `binance_usdm_metrics_5m` are empty.
- Binance OI and taker flow are wide but shallow: ~637/658 symbols over only
  70/67 days from `2026-04-27`.
- **Consequence:** cross-venue microstructure research is not currently
  possible. Long-history work must use price, premium, funding, basis, and
  Bybit OI. Design for that surface rather than assuming flow data exists.

## Proper work plan

### Research selection policy — no hardcoded performance gates

Lane-1 research has no universal Sharpe, return, trade-count, cost, era-sign,
or configuration-count hurdle. Those are properties to measure, not laws. An
anomaly is interesting when it is surprising, economically interpretable,
stable somewhere important, sharply regime-specific, useful for explaining a
known failure, or revealing of a data/execution artifact. Negative, inverted,
and conditional effects count as discoveries.

Choose follow-ups by expected information gain, mechanism plausibility,
effect-size shape, uncertainty, concentration, executable economics, and how
different the idea is from spent work. Record the judgment. Do not turn it into
a numeric pass/fail formula after the fact.

The hard boundaries are evidence physics: causal availability, honest
population/PIT scope, missingness, executable fills/costs/funding for a
performance claim, reconstructable accounting, and provenance. A violation
changes what the result can mean; it does not make the diagnostic useless.

### P0 — the causal research substrate

The substrate is `liquidity_migration/research/panels/cross_venue_panel.py` plus
`scripts/data/build_cross_venue_panel.py`, built over the both-venue population
from `2021-01-01`; coverage lives in each shard's `manifest.json`, and the two
source defects it exposed (`open_interest_value` is contract units,
`funding_event_kind` on 2 of 2,024 partitions) are in
`docs/research/research_findings.md` §4. Anything added to it holds the same
rules, and the answer to a new field is a live research question, not another
family of bespoke report scripts:

- Exact symbol mapping with collisions and contract differences rejected.
- Decision time, source publication/availability time, a claim-appropriate
  execution delay, and no backward fill across missing venue data.
- Bybit/Binance price, mark, index, premium, settled funding, turnover, and the
  available OI/taker fields; every field carries a coverage flag.
- Manifest with Git/config/data hashes, date and population bounds, coverage by
  venue/year, and all exclusions.
- If common-population coverage or timing cannot support a proposed claim,
  narrow or relabel that claim and preserve the gap as an anomaly. A root name
  is not evidence.

### P1 — anomaly atlas

Explore freely on already-seen data and keep an honest search log. Start with,
but do not limit the work to:

1. venue lead/lag, premium/funding/basis disagreement, and convergence paths;
2. price/OI/taker-flow divergences and transitions rather than static levels;
3. capital transfer between symbols, clusters, and venues;
4. funding-clock, time-of-week, volatility, liquidity, and market-regime
   asymmetries;
5. anomalies in what the active sleeves admit, reject, miss, or lose money on;
6. sign-inverted, time-shifted, and venue-local controls that expose artifacts;
7. unexpected data gaps, contract-lifecycle behavior, or microstructure effects
   that may be more valuable than the intended signal.

For every useful read, show the complete tested surface and enough time,
symbol, cluster, and regime decomposition to reveal instability. Put gross
next to actual or claim-appropriate stressed costs and funding. Report effect
size, uncertainty, concentration, turnover, capacity, common-factor exposure,
and missingness as continuous evidence rather than reducing them to pass/fail.

Maintain a compact anomaly catalog: observation, why it is interesting,
plausible mechanism, data touched, strongest artifact explanation, economic
shape, and the next discriminating test. Follow as many leads as remain
decision-useful; retire only duplicated plumbing and questions that no longer
teach anything.

### P2 — deepen the most informative anomalies

For leads that imply a tradable claim:

- try to disprove the proposed mechanism with timing, venue-local, sign,
  universe, and common-factor controls;
- compare sensible unhedged and hedged expressions without assuming one is
  preferred;
- replay through the account journal and venue rules when the claim reaches
  portfolio P&L;
- attribute gross, funding, fees, spread, impact, hedge P&L, residual beta,
  missed trades, and tail concentration;
- separate an unavailable live feature or optimistic cross-venue fill from a
  genuinely executable paper design.

Several anomalies may remain alive. The output is a better map of the market,
not an artificially forced winner.

### P3 — rolling forward grade

When a formulation becomes worth grading, commit its exact config and scorer
before the first new day; that commit is the registration. Append one row per
new day. Grade only post-commit decisions and keep mechanics-only days
separate. Multiple distinct formulations may accumulate their own honest
records. The existing LONG and CARRY sleeves remain the controls and are not
modified to help a challenger.

Promotion requires the five-line note in `docs/research/governance.md`, a recorded
change point, stable demo execution, and an explicit replacement/migration
diff. Promotion means demo only. Real money stays behind its own switch, set
by the owner's own hand (`docs/research/governance.md` §6).

### P4 — directions remain open

Crowding Transfer is one starting family, not a gate around creativity.
Price-independent funding/premium carry, cross-sectional transfer, execution
reversion, regime-conditioned sleeve redesign, or a mechanism not anticipated
here may be better. Revisit an old family only with a new mechanism, new data,
or a corrected defect—not another threshold sweep wearing a new name. True
cross-exchange execution is a new capability and stays simulation-only
until both legs, atomic failure handling, collateral fragmentation,
liquidation, transfer, and venue-outage risk are modeled and deliberately
authorized.

## Live task queue

The measured position this list starts from: at the measured 15.56 bp round trip
the anomaly-program signals are t 1.30-2.06 and do not clear the t ≥ 2.5 bar.
Execution work cannot create an edge — its ceiling is Sharpe 0.69 → ~1.17.

- [ ] Score the registered carry-hold configs on each new completed UTC day
      (`lane2_carry_hold_v1..v6`; `DEFAULT_CONFIGS` in the scorer is the list).
      This is the rolling forward record; the registration commit is the change
      point; the scorer charges each settlement exactly once; the paired daily
      differentials v2−v1, v3−v2, v4−v3, v5−v4 and v6−v5 are the primary
      comparisons. Tooling:
      `scripts/research/score_financed_longs_forward.py` appends
      `~/SHARED_DATA/bybit_full_pit/reports/financed_longs_forward/ledger.csv`
      (append-first, idempotent, `forward_eligible` flagged; the path is under
      the data root, not the repo's `reports/`). The daily sequence is
      research-refresh → panel 2026 rebuild
      (`scripts/data/build_cross_venue_panel.py --start 2021-01-01`, full
      rebuild — the index is whole-file) → ledger append, and
      `scripts/research/daily_evidence_run.sh` runs all three, writing
      `daily_run_status.json` beside the ledger. It refuses a dirty checkout
      (the provenance rule) and fails closed with the failing step named.
      **It is run by hand and nothing schedules it** until the new box arrives —
      the sequence once stopped for three weeks unnoticed and the promoted
      config accrued zero scored forward days, so a silent gap here is the
      failure mode to watch.
- [ ] Re-derive the settlement-exact surfaces on the corrected scorer: the
      anomaly-research funding-leg numbers (leg-attribution reversal,
      dispersion-gate withdrawal) and financed-longs negative-ledger rows
      1/2/13–17/20 (2026-07-28 double-count correction).
- [ ] Measure realised maker-fill probability in flow (target was 100 fills
      per arm; the retired paper-owner A/B froze at 2 of 8). This is the last
      unmeasured cost input. **Blocked, not pending:** it now needs a
      re-scoped in-flow arm on a live sleeve, or acceptance of the
      probe-only bound (`probe_passive_fill_ab.py`).
- [ ] Orthogonalise `basis` against `premium_diff` — they are one family and
      should not be double-counted.

No other strategy task list is active.
