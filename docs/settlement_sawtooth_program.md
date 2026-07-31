# The settlement sawtooth — mechanism dossier and research program

**Status: Lane-1. Nothing here is registered, nothing is deployed, nothing is
graded.** Every number below was measured on already-seen data (Bybit and
Binance full-PIT cross-venue panel, 2021-01..2026-07) and therefore *selected*
whatever is interesting about it. `docs/strategy_program.md` remains the single
active research queue and points here; this file is the dossier that queue item
reads from. Evidence policy is `docs/governance.md`; the bar is `t >= 2.5`.

Opened 2026-07-31 out of the hunt for a larger carry edge. That hunt failed —
see §3 — and this is what it left behind: a large, cross-venue-replicated price
pattern around funding settlements that nothing in the program currently uses.

---

## 1. The measurement

Around a **deep** funding settlement, perpetual price runs up into the print and
falls after it. Conditioning on the print at `T` being deeper than −10 bp
(the carry family's entry depth), top-100 by trailing 24h quote turnover,
hourly bars, n = 30,742 settlements on Bybit:

| hours relative to the print | j−4 | j−3 | j−2 | j−1 | j+0 | j+1 | j+2 | j+3 | j+4 | j+5 | j+6 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| mean return, bp | +53.2 | +37.2 | +55.4 | +56.9 | +49.5 | **−44.5** | −16.9 | −14.0 | −7.9 | −27.8 | −9.5 |

`j+0` is the hour *ending* at the print. **The 5 hours ending at the print run
+252.3 bp; the 6 hours after it run −120.6 bp.**

The control is flat. Shallow settlements (|f| ≤ 10 bp, n = 685,296) read
−4.5 bp over the same 5 hours and −3.5 bp over the following 6 — that is, ~2 bp
per hour of nothing, against ~50 bp per hour on the deep events.

A second, looser conditioning — the *previous* print deep rather than the
current one — gives +167.6 / −105.4 over the same windows (n = 31,276). Same
phenomenon, weaker selection, smaller amplitude. Quote the +252.3 / −120.6
version and say which conditioning it is; the two must not be mixed.

### It is getting bigger, not smaller

| | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| n (deep prints) | 207 | 2,394 | 2,290 | 1,936 | 12,935 | 10,980 |
| pre-print 5h, bp | +45.4 | +94.0 | +160.9 | +220.9 | **+332.3** | +220.9 |
| post-print 6h, bp | +23.7 | +7.5 | −54.5 | −42.5 | −97.5 | **−206.1** |
| mean print, bp | −32.26 | −32.11 | −48.23 | −42.81 | −43.24 | −44.28 |

Two things in that table matter more than the amplitude. Deep prints became
**~6× more frequent** in 2025-26 while their mean depth barely moved — that
frequency shift, not deeper crowding, is what the whole carry family's 2025-26
dominance is made of. And the post-print fall has grown fastest of all, which
makes the pattern *more* dangerous to trade late, not more profitable.

---

## 2. What makes it credible, and what does not

### Passes: independent-venue replication

The same profile on Binance, computed with Binance's own price, funding and
settlement detector, n = 26,156:

| | pre-print 5h | post-print 6h |
| --- | ---: | ---: |
| Bybit | +252.3 | −120.6 |
| Binance | +145.5 | −82.7 |
| ratio | 0.58 | 0.69 |

Same sign, and both ratios sit inside the program's standing cross-venue kill
band of [0.5, 2.0]. This is not a Bybit data artifact, and it is not an artifact
of one venue's funding-timestamp convention.

### Fails: it does not mirror

If the pattern were purely the funding-harvest crowd — buy before the print to
collect, sell after — then deep **positive** funding (longs paying shorts)
should invert it: sell into the print, cover after. It does not.

| deep positive prints (> +10 bp) | pre-print 5h | post-print 6h | n |
| --- | ---: | ---: | ---: |
| Bybit | +45.2 | +15.0 | 6,722 |
| Binance | +363.5 | +36.4 | 2,482 |

Mildly *up* on both sides, on both venues. No inversion. **The harvest-crowd
story is therefore not established, and the doc should stop asserting it.**

At least two mechanisms survive this and predict the same deep-negative
observable:

1. **Harvest flow** — traders buy ahead of a deep print to collect and exit
   after. Predicts a mirror; the mirror is absent, so at best this is partial.
2. **Squeeze-and-fade** — crowded shorts get squeezed (price up), the squeeze
   exhausts, price gives it back, and funding stays negative throughout because
   the shorts stay crowded. The print is a *marker* of position in the cycle,
   not a cause. Predicts no mirror, because deep-positive funding in crypto
   arises in trending rallies rather than in symmetric crowding.

Distinguishing them is **H5** below, and it decides whether the pattern is
exploitable or merely descriptive. Until it is settled, treat the causal story
as open.

---

## 3. What is already closed — do not redo this

The hunt that produced the dossier tested and killed the obvious trades. Full
tables in the run note; the outcomes:

| tried | result | why it died |
| --- | --- | --- |
| Hold only the *h* hours ending at each settlement (`h`=1, exit at the print) | +10.67 bp/day, **Sharpe 2.96, t 6.75** | requires a zero-latency exit — see below |
| …with 1 hour of exit lag | −11.67 bp/day, Sharpe −2.14 | the −44.5 bp at `j+1` is the entire edge, handed back |
| …at 2× the measured cost stack | +1.51 bp/day, Sharpe 0.42 | 16 round trips/day; dies on any cost realism |
| …in 2026 alone, even at zero latency | −14.66 bp/day | the recent regime already does not support it |
| SHORT the post-print fall (lag 1-2h, hold 1-7h) | every cell negative, best −8.31 | as a short you *pay* the deep funding; it cancels the fall almost exactly |
| Hedge the carry book's price leg with short BTC | 2.5% of variance removed | the risk is idiosyncratic, not market |
| …with a short EW top-100 basket | Sharpe 1.13 → 1.11 at fixed beta, better in 6/24 phases | once the basket leg pays its own fees and funding the gain vanishes |
| …with a per-name short on Binance | neutral Sharpe 0.62 vs directional 1.24 | removes 94% of price variance but eats **74% of the funding** |

The last row is the program's bounding result and should be quoted whenever
someone proposes a market-neutral version of the carry book: on v4's held
name-days Bybit pays **+221.16 bp/name-day** and the Binance short costs
**−163.67**, leaving +57.48. The crowd fee is a property of the *coin*, not a
venue dislocation. **Holding the coin is the price of admission to the fee, and
there is no instrument that separates them.**

The decomposition behind that: v4 is +45.70 bp/day of funding and −2.70 bp/day
of price, and the price leg carries ~95% of the book's daily variance (sd 245 of
258 bp/day) while standing alone at Sharpe −0.11. A hypothetical perfect price
hedge scores **Sharpe 8.24**. That number is the size of the prize and the
reason this program exists; it is also, on present evidence, unpurchasable.

---

## 4. The blocking dependency: minute data

**The single most valuable experiment cannot be run with what the repo has.**

The `h`=1 / exit-at-print trade is worth Sharpe 2.96 at zero latency and
Sharpe −2.14 at one hour. The true answer lies between, and it is entirely
determined by how fast the −44.5 bp at `j+1` accrues. If it is front-loaded into
the first seconds, the trade is dead and this program's headline hypothesis dies
with it. If it accrues smoothly over the hour, an exit inside two minutes costs
~1.5 bp and the trade survives.

Current state:

- `~/SHARED_DATA/bybit_full_pit/tick_ohlc_1m/` **exists and is empty (0 files).**
- There is **no downloader** for it. `scripts/data/` has
  `build_full_pit_bybit.sh`, `build_full_pit_binance.sh`,
  `build_cross_venue_panel.py`, `build_idio_panel.py`,
  `build_candidate_tape.py`, `precompute_residual_momentum.py` — none fetches
  sub-hourly bars.
- `premium_index_1h`, `mark_price_1h`, `index_price_1h` exist at hourly
  resolution and are **not** substitutes; they answer a different question
  (how the rate forms, §H6) and not the execution question.

So P0 for this program is a data acquisition task, and until it lands H1 is
unanswerable rather than unpromising. Do not report H1 as "does not work"; report
it as blocked.

---

## 5. Open hypotheses, ranked

Ranked by expected value × tractability. Every one is Lane-1 until a config is
committed; a commit is the registration (`docs/governance.md`).

### H1 — Execution-speed harvest *(blocked on §4)*

Enter ~1 hour before a deep print, exit as close to the print as execution
allows. Measured at Sharpe 2.96 / t 6.75 with a zero-latency exit, on a clean
entry-depth plateau (−5/−10/−20/−40 bp → Sharpe 2.07/2.96/3.37/2.72) and with a
passing placebo (all settlements, no depth selection: −14.32 bp/day, Sharpe
−5.48 — the depth selection does the work).

**Needs:** 1-minute bars for the top-100 names. Then re-run the `h`×`k` grid at
`k` ∈ {1, 2, 5, 15, 30, 60} minutes.
**Kills it:** more than ~60% of the `j+1` move landing inside the first 2
minutes; or the 2026 slice staying negative at realistic `k`.

### H2 — Entry timing for the deployed carry book *(unblocked, highest tractability)*

The carry book enters *after* observing a deep print, so it buys into the
post-print fall. That is where its price drag comes from. If entry is delayed
past the fall, the price leg should improve without touching the signal.

**Trap already hit — read this before building.** On the 24h decision grid the
funding-cycle phase is *degenerate*: an 8h-cadence symbol decided at midnight is
always at age 0, so "enter later in the cycle" cannot be expressed by gating on
`by_funding_age_h` without silently excluding whole cadence classes. Bybit's
interval mix moved from 100% 8h in 2021 to 52% 4h / 21% 1h in 2025, so any
clock-based gate carries an era gradient. Build the gate on **age ÷ the symbol's
own inferred interval**, or move to a finer decision grid — not on raw age.

**Kills it:** no improvement in the price leg across a majority of the 24 grid
phases (§H4 of `docs/research_findings.md` on clock fragility applies here too).

### H3 — Exit timing for the deployed carry book *(unblocked)*

Symmetric to H2. v3/v4 exit when funding recovers above −3 bp, at whatever hour
the grid lands on. The sawtooth says the local price maximum is *at* the print.
An exit aligned to the next print rather than to the grid should capture
+50 bp/hour of drift that is currently taken at random.

**Kills it:** the improvement fails to survive the 24-phase sweep, or it is
smaller than the extra turnover cost it creates.

### H4 — Sawtooth amplitude as a conditioning or sizing variable *(unblocked)*

`lane2_carry_hold_v4` sizes on depth × crowding persistence. A name's own recent
sawtooth amplitude is a third, mechanically distinct axis: it measures how
violently the crowd trades that name around its prints. Test whether it composes
with the existing two the way persistence composed with depth, or replaces them.

**Kills it:** it is a proxy for realised volatility. Control against a plain
30-day vol arm before believing anything.

### H5 — Which mechanism is true *(unblocked, diagnostic — do this early)*

§2's open question. Harvest flow and squeeze-and-fade predict the same
deep-negative profile and differ elsewhere. Discriminating tests:

- Condition on open interest change across the print. Harvest flow implies OI
  *rises* into the print and falls after; squeeze-and-fade implies short OI
  *falls* through the squeeze. **Caveat: `open_interest` is
  survivorship-contaminated in this panel (recorded 2026-07-24); repair or
  bound that before trusting the read.**
- Condition on `positioning_lsr` (long/short account ratio) across the print.
- Test whether the pre-print run is larger for names with *higher* crowding
  persistence. Harvest flow predicts yes; squeeze-and-fade is agnostic.
- Ask why deep-positive prints do not mirror on **either** venue, given that
  deep-positive funding is 4-10× rarer. Rarity alone may explain the asymmetry.

This hypothesis produces no trade. It decides which of the others are worth
running, which is why it is early.

### H6 — Does the forming rate predict the print? *(unblocked)*

Bybit computes funding from the premium index over the interval, so the print at
`T` is close to deterministic given the premium path — and `premium_index_1h`
is in the tree, unused by this program. If the print is predictable an hour
ahead, entry no longer needs to wait for a settled deep print, which is the
lag H2 is trying to work around and is the most literal reading of "enter before
the fee is paid".

**Kills it:** the premium path adds nothing over the last settled print once the
cadence confound is neutralised.

### H7 — Fill scheduling for the live sleeve *(unblocked, operational)*

Independent of any new strategy: the deployed CARRY sleeve places orders at
~00:20 UTC (kline availability lag). If price systematically runs +50 bp/hour
into prints and falls after, *when* the sleeve fills is worth basis points on
every entry it already makes. This is a cost-model and scheduling question, not
an alpha claim, and it is the cheapest item here.

---

## 6. Kill criteria for the whole program

Declared before the work, per `docs/governance.md`. Any one of these closes it:

1. **The cross-venue replication breaks** on refreshed data — ratio outside
   [0.5, 2.0] or a sign flip on Binance.
2. **H5 resolves to squeeze-and-fade with no tradable conditioning**, i.e. the
   pattern is a description of what crashing coins do and carries no
   information beyond depth and volatility, both of which the carry family
   already uses.
3. **H1 resolves negative on real minute data** *and* H2/H3 fail to improve the
   deployed book across a majority of grid phases. That would mean the pattern
   is real, large, and inaccessible — a legitimate outcome, and the same shape
   as the hedging result in §3.
4. **The 2026 slice stays negative** for every hypothesis at a realistic cost
   stack. The program's whole record is a 2025-26 story; a mechanism that is
   already dead in the most recent era is not worth carrying forward.

---

## 7. How anything here gets graded

Unchanged from `docs/governance.md`, restated because this program is unusually
prone to two specific errors:

- **Clock fragility.** Every carry-family number this repo publishes sits on the
  midnight-UTC decision grid, which is the *best of 24 hourly phases* (v4:
  +9.84 to +22.19 bp/day, Sharpe 0.75 to 1.64, mean 1.13). Midnight is not
  structurally better — it ranks #12 of 24 in the first half of the record and
  #1 in the second, and the winning hour migrates by era (2023: phases 15-18;
  2024: phases 7-9; 2026: phases 0-1). **Report any result here as a sweep over
  phases, never as a single clock.** The phase spread is a free error bar on the
  backtest; use it.
- **Cadence confounds.** Anything measured on a clock rather than in a symbol's
  own settlement sequence reports Bybit's interval mix, which has a strong era
  gradient. This has already produced two discarded measurements
  (`docs/archive/2026-07-31-trend-filters-and-persistence.md` §3).

Costs at the measured stack: 7.78 bp per side, 15.56 bp round trip. Report every
grid cell and era split, put costs next to gross, and state which data shaped
the result and which graded it.

---

## Provenance

Measured 2026-07-31 in the scratchpad during the "find a bigger edge" hunt;
tables reproduced above rather than left in a run note because this dossier is
the thing the queue reads. The hunt's negative results are §3. The v4 config
carries the hedging bound and the clock-fragility disclosure that this work also
turned up. Related: [`docs/carry_hold.md`](carry_hold.md),
[`docs/research_findings.md`](research_findings.md),
[`docs/archive/2026-07-31-trend-filters-and-persistence.md`](archive/2026-07-31-trend-filters-and-persistence.md).
