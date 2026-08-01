# The settlement sawtooth — mechanism dossier and research program

**Status: CLOSED 2026-08-01. The mechanism is solved and it is arbitrage-free by
construction.** Kill criteria 2 and 4 both fired. Nothing here is registered,
nothing is deployed, nothing is graded. Every number was measured on
already-seen data (Bybit and Binance full-PIT cross-venue panel, 2021-01..2026-07,
plus 1-minute Bybit klines) and is therefore Lane-1. Evidence policy is
`docs/governance.md`; the bar is `t >= 2.5`.

> **Provenance correction, 2026-08-01.** The closure was first written against
> `~/SHARED_DATA/bybit_render_1m/klines_1m`. **That root does not exist.**
> `docs/data.md` records the `bybit_render_1m` and `binance_vision_alt` plans and
> their fetchers as removed 2026-07-21, with an explicit instruction not to
> recreate them from old documents; the cited validation receipt is dated
> 2026-07-20, one day before that removal. Two further roots the closure named —
> `bybit_full_pit/klines_5m` and `binance_vision_alt/klines_1m` — are also absent.
> The minute-resolution numbers were therefore not reproducible when written.
>
> They are being made reproducible rather than deleted. `scripts/data/download_bybit_klines_1m.py`
> fetches 1-minute klines into `bybit_full_pit/klines_1m` through the same
> `BybitMarketData.get_klines` path the 1h builder uses, and a run is in progress
> over the 568 symbols that carry a deep top-100 print (448,628 symbol-days,
> priority-ordered by deep-print count). **Until a minute-level claim below is
> re-derived from that root, treat it as UNVERIFIED and marked so.**
>
> The conclusion does not depend on it. The headline — ex-dividend adjustment —
> reproduces at hourly resolution on the cross-venue panel that does exist:
> pooled slope **1.1127** (t 62.3, n = 722,757) across prints from −450 to
> +250 bp; deep-negative print −43.01 → move −44.52 (ratio 1.035); deep-positive
> print +17.04 → move **+18.35** (ratio 1.077); shallow null. See §0.9.

Opened 2026-07-31 out of the hunt for a larger carry edge. Closed 2026-08-01.
One operational result survives — §0.3, H7 — and it is a scheduling finding, not
an alpha claim.

---

## 0. What closed it

### 0.1 The post-print fall is an ex-dividend adjustment

The perpetual drops by **exactly the funding paid, at the instant it is paid**,
and then holds the new level. Regressing the settlement-instant price move on
the print, pooled over deep-negative, deep-positive and shallow prints
(n = 47,004, print depths −166 bp to +16 bp):

> **move(0 → +1 min) = +1.163 bp + 1.0458 × print_bp,  t(slope) = 145.9**

An ex-dividend adjustment predicts slope +1.0000 and intercept 0. Holding
constant across a 14× range of print depth:

| print bucket, bp | n | mean print | move 0→+1m | move/print | net to a long |
| --- | ---: | ---: | ---: | ---: | ---: |
| < −100 | 1,890 | −166.13 | −168.90 | 1.017 | −2.77 |
| [−100, −60) | 2,055 | −76.93 | −76.92 | 1.000 | +0.01 |
| [−60, −40) | 2,062 | −48.83 | −51.71 | 1.059 | −2.88 |
| [−40, −25) | 3,228 | −31.46 | −31.51 | 1.002 | −0.05 |
| [−25, −15) | 4,621 | −19.32 | −20.28 | 1.049 | −0.95 |
| [−15, −10) | 4,710 | −12.23 | −12.87 | 1.052 | −0.63 |

"Net to a long" is the price move plus the funding that long receives. It is
zero everywhere. Over the whole deep-negative cohort the net is **−1.01 bp at
+1 min, −1.43 at +5, −0.64 at +20, −2.05 at +60** — all within noise.

**There is no edge in the post-print fall, by construction.** This also
retro-explains §3's dead end: shorting the fall failed because "as a short you
pay the deep funding; it cancels the fall almost exactly." That is the
mechanism, stated without knowing it.

### 0.2 The pre-print run is a spot move the print lags

Decomposing the deep-negative profile into index and basis (n = 30,090):

| | pre-print 5h | t | post-print 6h | t |
| --- | ---: | ---: | ---: | ---: |
| perp (`by_close`) | +264.1 | 36.83 | −112.9 | −18.60 |
| index (spot) | +263.1 | 34.92 | −117.2 | −18.67 |
| basis (perp − index) | +11.3 | 8.05 | **+9.8** | 8.64 |

**99.6% of the pre-print run and 103.8% of the post-print fall are in the
index.** The basis contributes ~+10 bp on *both* sides — same sign, so it is not
a sawtooth at all. Both candidate mechanisms in §2 were perp-flow stories; the
perpetual does essentially nothing relative to its own index.

And the "5-hour run" is the tail of a much longer move. Cumulative perp path
around the print, n = 30,090:

| window | perp | t | index | t |
| --- | ---: | ---: | ---: | ---: |
| prior 24h (j−24..j−13) | +426.7 | 40.64 | +456.0 | 41.58 |
| prior 12h (j−12..j−5) | +318.7 | 36.38 | +345.6 | 37.10 |
| pre-print 5h (j−4..j+0) | +264.1 | 36.83 | +263.1 | 34.92 |
| post-print 6h (j+1..j+6) | −112.9 | −18.60 | −117.2 | −18.67 |
| post-print 7-24h | −163.8 | −17.79 | −174.7 | −18.76 |

The cumulative path runs **+1,012 bp into the print** at roughly +40 bp/hour for
a full day, peaks at the print, and gives back about a quarter of it over the
following day. A deep negative print selects a coin in a violent spot rally
whose perpetual is at a persistent discount (basis level −41.7 bp at j−24,
−55.4 at the print, −25.5 at j+24). It is a marker, not a cause.

### 0.3 What survives: H7, and it is operational

> **⚠ UNVERIFIED at minute resolution.** Every number in this subsection came
> from the non-existent `bybit_render_1m` root (see the provenance correction).
> The *magnitude and direction* are independently corroborated at hourly
> resolution — the drop in the hour after a deep print is −44.52 bp, so a fill
> landing after it avoids about that much — and the operational advice therefore
> stands on evidence that does exist. The minute-by-minute profile below does
> not. **Re-derive from `bybit_full_pit/klines_1m` before citing the shape.**

The deployed CARRY sleeve decides at 00:00 UTC and fills at ~00:20 because of
kline availability. Measured on 1-minute bars (n = 18,566 deep prints inside the
1m window), entry price relative to the price at the print:

| fill at | +0m | +5m | +10m | +20m | +30m | +60m | +90m |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| entry price vs print, bp | 0.00 | −45.66 | −44.32 | **−44.87** | −47.98 | −46.28 | −62.70 |

**The 00:20 lag is not a cost. It saves ~45 bp per entry**, because the sleeve
fills *after* the ex-dividend drop rather than into it. Favourable in **24/24
grid phases** (−81.04 to −12.16, median −40.54) and in every era: 2023 −33.25
(t −6.08), 2024 −53.98 (t −9.02), 2025 −38.21 (t −13.49), 2026 −56.31 (t −14.08).

**Do not "fix" the kline-availability lag.** Driving the fill toward 00:00 would
hand back ~45 bp on every entry the sleeve makes. Nearly all of the saving is
present by minute 1 and it is flat out to +60 min, so the current schedule is
already capturing it. Extending the delay buys little and starts forgoing
funding (§0.4).

This is a cost-model and scheduling observation on the existing book. It is not
a proposal, it authorizes nothing, and no runtime change follows from it here.

### 0.4 H2 and H3 fail on the deployed book

**H2 (delay the entry).** Delaying the book's entry by one day on its own
decision grid costs **−336.20 bp per entry, t −6.15**, and is negative in
**24/24 grid phases** (−441.79 to −266.29, median −355.99). The premise was
backwards: the book already fills after the drop, and deep funding is transient,
so a day of delay forgoes the highest-carry day. Over a 7-day hold, funding
falls +612.6 → +494.5 bp per day of delay and the price leg gets *worse* too
(+86.5 → −100.7).

**H3 (align the exit to the print).** There is nothing to capture.
Unconditionally the hour ending at a print earns **−0.362 bp (t −1.55)** against
−0.271 bp (t −3.04) for every other hour, n = 706,555 vs 3,407,161. The
+50 bp/hour H3 wanted exists only inside the deep-print conditioning, and that
conditioning is not knowable in advance (§0.5).

### 0.5 H1 was never blocked, and it dies on causality

**On the minute data — both sides of this were wrong.**

- ~~It existed the whole time in `bybit_render_1m`.~~ **WITHDRAWN.** That root
  does not exist and is documented as removed 2026-07-21. Nor is
  `bybit_full_pit/klines_5m` a thing — the 5-minute klines are Binance's
  (`binance_full_pit/klines_5m`, 686 files), a venue mislabel.
- ~~`tick_ohlc_1m` is empty (0 files).~~ **ALSO WITHDRAWN, and this one was
  §4's.** It holds **5,648 parquet files over 814 date partitions,
  2023-03-29..2026-05-24**. The original check globbed `*.parquet`
  non-recursively against a `date=/symbol=/part.parquet` layout and read the
  zero as absence. `docs/data.md`'s coverage census already described this root
  correctly — Tier D, 401 symbols at a **median of 11 days each**, "event
  windows; no cross-sectional flow or microstructure study can be built on
  them" — and §4 did not read it before asserting a data gap.

The corrected position: minute data of the right shape was never in the tree,
but it was always *obtainable*. Bybit's v5 kline endpoint serves `interval="1"`
back to 2021 for linear perps (verified 2026-08-01, 1,440 bars/day complete),
and it is now being fetched into `bybit_full_pit/klines_1m` (§0.10). **H1 was
neither blocked nor already answered; it was one afternoon of fetching away, and
two consecutive passes over this question each got the tree wrong in a different
direction.**

With that data, **H1 fails its own declared kill criterion**: "more than ~60% of
the `j+1` move landing inside the first 2 minutes" — measured, **97.0%** (−44.89
of −46.28 bp; 97.7% inside the first *minute*). Price is flat in the last five
minutes before the print, gaps at the instant, and is flat again after.

But H1 was already dead for a prior reason. **The trade cannot be entered.** It
gates on the print at `T`, which Bybit computes from the premium index over the
interval ending at `T` and publishes *at* `T`, while the position must open at
`T−1h`. Reproducing the dossier's own trade and then replacing only the gate:

| arm | gate knowable at entry? | events | gross bp/day | net bp/day | Sharpe | t |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| A look-ahead — `print(T) < −10bp` | **no** | 30,090 | +40.33 | +24.77 | 2.97 | 6.25 |
| B previous settled print (corr 0.73) | yes | 30,601 | +2.94 | −12.62 | −1.91 | −4.02 |
| C premium at T−1h (corr 0.61) | yes | 73,683 | +11.66 | −3.90 | −0.80 | −1.77 |
| D forming rate, elapsed window (corr 0.73) | yes | 35,601 | +12.14 | −3.42 | −0.63 | −1.37 |

Every PIT replacement dies, and that is the durable result.

**But the attribution to look-ahead is withdrawn.** Arm A was matched to the
original trade by *reproducing its Sharpe* (2.97 / t 6.25 against 2.96 / 6.75),
which does not identify a construction — all four arms select ~30k events from
the same population and similar Sharpes are cheap. Read from source, the original
gate is `by_funding` at `T−h`, the last *settled* print as of the entry bar.
Measured directly, `corr(gate at T−1h, print at T) = +0.7277` — that is arm B's
gate, not arm A's. **The original trade was PIT-clean in its gate.**

What actually made it score was the exit: it exits *at* `T`, before the
ex-dividend hour, and the ex-dividend drop is the whole giveback. That is the
same infeasibility the hunt already recorded (Sharpe 2.96 at zero exit lag,
−2.14 at one hour) — now with the mechanism attached. H1 dies on **execution**,
not on look-ahead, and dies just as completely.

The look-ahead is directly measurable. Splitting arm A's deep prints by whether
the forming rate saw them coming:

| | n | mean event return | bp/day | Sharpe |
| --- | ---: | ---: | ---: | ---: |
| deep print the forming rate predicted | 16,239 | +40.41 (t 12.83) | +29.42 | 3.39 |
| deep print it did **not** predict | 1,141 | **+183.56** (t 8.31) | +209.43 | 4.76 |

The trades you could not have known about are 4.5× the ones you could.

Dropping the settlement anchor entirely and asking the PIT question directly —
gate on the forming rate at any hour `t`, hold `t+1..t+h`, 15.56 bp round trip
amortised over the hold — **all 36 cells of a 6 depth × 6 holding-period grid
are negative net.** Best cell +1.18 bp/day at t 0.09, negative in 2025 and 2026,
positive in 6 of 24 phases.

The settlement anchor is real but too small to trade. Anchored minus unanchored,
same PIT gate, paired by symbol-day: **+7.92 bp at t 3.51** (gate < −10 bp) —
roughly **half** the 15.56 bp round trip. The best anchored net cell (< −40 bp)
is +10.82 bp/day at Sharpe 0.90, **t 1.64**, below the bar, with worse
neighbours on both sides (−2.10 at −20 bp, +1.84 at −60 bp) — a spike, not a
plateau. It decays monotonically by era (2022 +51.94 → 2023 +38.81 → 2024
+21.01 → 2025 −6.22 → **2026 −44.65**) and lives entirely in 8h-cadence names
(+29.52, t 3.23) against 1h names (−61.03, t −3.62), which is the era gradient
and the cadence confound being the same fact.

### 0.6 H5 resolved — to neither candidate

§2 offered harvest flow and squeeze-and-fade. Both are refuted.

- **The mirror the dossier called absent is present.** Confirmed at hourly
  resolution on the panel that exists: deep-positive prints (mean **+17.04 bp**,
  n = 6,722) move **+18.35 bp** in the hour after the print, t +3.85, ratio
  1.077 — price *rises* when longs pay, exactly as ex-dividend requires. The
  shallow control is a clean null (print +0.31, move −0.62).
  The "no inversion" finding in §2 was not an artefact of hourly bars: it was an
  artefact of comparing 5- and 6-hour window *sums*, which drown a ~20 bp
  settlement-hour step in hours of several-hundred-bp noise. The mirror was
  visible in §1's own `j+1` column all along.
  *(The closure's original figure here — +33.10 bp at 1-minute resolution — is
  withdrawn as internally inconsistent: its own regression, slope 1.0458 with
  intercept +1.163, predicts +18.36 for a +16.45 bp print, and the independent
  hourly measurement is +18.35. Re-derive from `klines_1m` when the fetch lands.)*
- **Open interest rises into the print and never unwinds.** Harvest flow's
  distinguishing prediction is an unwind after the print. Bybit: **+849.3 bp
  pre (t 44.51), +166.9 post (t 15.49)**. Binance's independent series: **+738.3
  bp pre (t 43.65), +8.4 post (t 1.03)** — flat. Nobody closes.
- **The long/short account ratio moves the wrong way for harvest flow.** It
  *falls* into the print (−0.004, t −2.53) and *rises* after (+0.017, t 13.77).
  Accounts get shorter into the rally, which is why funding is negative, and
  longer once the fee has been paid.

The OI caveat is repaired, not merely bounded. Binance's `metrics_5m` is not
survivorship-filtered — 83 of 764 symbols (10.9%) have series ending more than
60 days before the root end, so delisted names are present — and it gives the
same answer as the contaminated Bybit column. The bound also holds directly: the
OI-covered cohort's price profile (+272.3 / −123.9) is close to the full
population's (+264.1 / −113.0).

**The mechanism, stated plainly.** A coin rallies hard in spot for a day or
more. Its perpetual crowd leans short into the rally, so the perp trades at a
persistent discount and funding prints deeply negative. At the settlement
instant shorts pay longs, and the perp drops by what was paid — an ex-dividend
adjustment, slope 1.046. Then the spot move decays. Every part of that is either
a spot price move or a compensated cash transfer.

### 0.65 OPEN — the ex-dividend result does not obviously reconcile with the carry book

**This is a diagnostic, not a result, and it is the one thing here that should
not be filed away.** It is measured on the *signal*, not on the book.

If the perp hands back the funding at every settlement, a book that holds
through settlements should not be able to bank a funding leg. Decomposing a held
name-day — PIT entry condition (last settled rate < −10 bp, top-100) at the
midnight grid, 24 hours strictly forward, no filters, no sizing, no costs,
n = 4,450 name-days, 7.31 settlements each:

| component | bp/name-day |
| --- | ---: |
| funding received | **+154.72** |
| price in the ex-dividend hour (the hour *after* each print) | **−160.02** |
| price in the hour ending at each print | −9.98 |
| price in every other hour | +53.32 |
| total price | −116.68 |
| **funding + ex-dividend hour** | **−5.30** |
| **total** | **+38.05** |

Per settlement: **+21.16 bp received, −21.89 bp given back.** The fee is handed
back in full, within noise. What is left is the inter-settlement drift.

Swept over all 24 grid phases the pattern is uniform and the level is not:

| | min | median | max | phases > 0 |
| --- | ---: | ---: | ---: | ---: |
| funding | +150.66 | +158.50 | +165.43 | 24/24 |
| ex-dividend hour | −223.39 | −190.27 | −160.02 | 0/24 |
| funding + ex-dividend | −61.12 | −30.73 | **−5.30** | **0/24** |
| other hours (drift) | +19.57 | +37.56 | +53.88 | 24/24 |
| total | −69.07 | −15.85 | +42.72 | **8/24** |

Funding is flat across phases. **What midnight buys is a smaller ex-dividend
leak (−160.02 against −223.39 at the worst phase) and more drift (+53.32 against
+19.57)** — and midnight is the best of the 24 on total, which is the same
clock-fragility disclosure `lane2_carry_hold_v4` already carries, reached by a
different route and now with a mechanism attached. Era split at midnight:
total +139.43 (2023), +125.15 (2024), +15.21 (2025), **−71.08 (2026)**.

### 0.66 RESOLVED on the actual v4 book — the fee is a wash

Run 2026-08-01 on `lane2_carry_hold_v4` itself: its own weights, its own
universe, its own decision grid, and its own denominator. The 24 forward hours of
each held name-day are classified into **mutually exclusive** buckets — the
1h-cadence trap is that every hour there is both "the hour ending at a print" and
"the hour after a print", so the two classes must not both claim it.

Denominator note: the registered +45.70 / −2.70 is per *active* day (944);
these are per *book*-day (1,756). 45.70 × 944 / 1756 = 24.57, which reconciles.

| component, bp per book-day | midnight | min | median | max | phases > 0 |
| --- | ---: | ---: | ---: | ---: | ---: |
| funding received | +24.57 | +21.43 | +23.92 | +26.79 | 24/24 |
| price, ex-dividend hour only | −13.94 | −18.01 | −15.08 | −13.10 | **0/24** |
| price, hours that are both (1h cadence) | −5.78 | −15.31 | −9.84 | −5.78 | **0/24** |
| **fee net of giveback** | **+4.85** | **−6.46** | **−1.44** | **+4.85** | **5/24** |
| price, print hour only | +6.89 | +5.56 | +7.03 | +7.81 | 24/24 |
| price, neither (DRIFT) | +6.72 | +3.45 | +5.74 | +8.09 | 24/24 |
| engine `gross_bp` (compounded, authoritative) | +23.12 | +10.74 | +15.22 | +23.12 | 24/24 |

**The funding leg is handed back.** +24.57 received against −19.72 given up in
the hours the fee is paid, netting **+4.85 at the registered clock and −1.44 at
the median phase — positive in only 5 of 24 phases.** Meanwhile the two
components that *are* robust are the price move in the hour ending at the print
(+7.03 median, 24/24) and the drift between settlements (+5.74 median, 24/24);
together they carry essentially all of the engine's gross.

**So v4's registered attribution is arithmetically right and economically
misleading.** "+45.70 bp/day of funding, −2.70 of price" is a true statement
about two columns; it is not a true statement about where the money comes from.
The cash is received and the perpetual drops by the same amount in the hour it
is received. What the book actually earns is the run-up into the print and the
drift between prints in deeply-shorted names.

**Caveat, stated rather than buried.** The hourly decomposition sums simple
returns and the engine compounds a 24h return; the gap is −4.65 bp/book-day
(sum-of-hourly +18.46 against engine +23.12, ~20%). The engine is authoritative
for the level. The gap does not move the conclusion — funding +24.57 against a
−19.72 giveback is far outside it — but any restatement should carry it.

**Consequence, for the owner to accept or decline:** [`docs/carry_hold.md`](carry_hold.md)
and `lane2_carry_hold_v4`'s `claim` describe the mechanism as crowded shorts
paying longs. On this evidence that describes a cash flow returned at the moment
it is paid. The honest mechanism is *drift and pre-print run-up in names whose
perpetual sits at a persistent discount*, which is a different claim with a
different risk profile — and it bears directly on the price leg's
~95%-of-variance problem, since under this reading the price leg is not a drag on
the edge, it **is** the edge.

### 0.7 H4 collapses into depth

H4 proposed a name's own sawtooth amplitude as a third sizing axis. Given slope
1.046, amplitude *is* the print. Measured: corr(trailing-20 mean |tooth|,
trailing-20 mean |print|) = **+0.4486**, and amplitude is a *better* proxy for
30-day realised volatility (**+0.6271**) than depth is (+0.2661) — which is
precisely the control H4 was required to pass and it fails it. `lane2_carry_hold_v4`
already sizes on depth. Not a third axis; no separate registration.

### 0.8 H6 answered

The forming rate — the mean premium index over the elapsed part of the current
funding interval — predicts the print at **corr +0.7325**, against +0.6756 for
the previous settled print and +0.6149 for a single premium reading (n = 595,093).
As a gate it has 93.4% recall at 45.6% precision. It is the best PIT signal
available in the tree and it is what arm D above is built on. It does not rescue
any trade.

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

*(2026-08-01: independently reproduced at n = 30,090, +264.1 / −113.0; shallow
control −4.5 / −3.4 against the −4.5 / −3.5 below. The measurement is sound. It
is §0.1–0.2 that reinterpret it: the run is spot and the fall is the fee.)*

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

*(2026-08-01: the growing post-print fall is the growing fee. Mean print depth
is flat across the table while settlement frequency rose ~6×, so the fall per
day grows with the number of prints, not with any change in behaviour.)*

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

### ~~Fails: it does not mirror~~ — WITHDRAWN 2026-08-01

~~If the pattern were purely the funding-harvest crowd — buy before the print to
collect, sell after — then deep **positive** funding (longs paying shorts)
should invert it: sell into the print, cover after. It does not.~~

| deep positive prints (> +10 bp) | pre-print 5h | post-print 6h | n |
| --- | ---: | ---: | ---: |
| Bybit | +45.2 | +15.0 | 6,722 |
| Binance | +363.5 | +36.4 | 2,482 |

**This section was wrong, and it was the dossier's central puzzle.** The mirror
is there; hourly bars cannot see it. At 1-minute resolution deep-positive prints
move **+33.10 bp at the settlement instant (t 16.49)** against a mean print of
+16.45 bp — price rises when longs pay. See §0.6. The hourly windows above are
dominated by the spot move the print is selecting on, which is up in both
cohorts because deep funding of either sign selects names that are moving.

The two mechanisms this section proposed — harvest flow and squeeze-and-fade —
are **both refuted** (§0.6). Neither is a description of what happens, because
neither is about the index, and the index is where the move is.

---

## 3. What is already closed — do not redo this

The hunt that produced the dossier tested and killed the obvious trades. Full
tables in the run note; the outcomes:

| tried | result | why it died |
| --- | --- | --- |
| Hold only the *h* hours ending at each settlement (`h`=1, exit at the print) | +10.67 bp/day, **Sharpe 2.96, t 6.75** | **look-ahead — the gate is not knowable at entry (§0.5)** |
| …with 1 hour of exit lag | −11.67 bp/day, Sharpe −2.14 | the −44.5 bp at `j+1` is the entire edge, handed back |
| …at 2× the measured cost stack | +1.51 bp/day, Sharpe 0.42 | 16 round trips/day; dies on any cost realism |
| …in 2026 alone, even at zero latency | −14.66 bp/day | the recent regime already does not support it |
| SHORT the post-print fall (lag 1-2h, hold 1-7h) | every cell negative, best −8.31 | as a short you *pay* the deep funding; it cancels the fall almost exactly — **this is the ex-dividend mechanism (§0.1)** |
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
reason this program existed; it is also, on present evidence, unpurchasable.

---

## 4. ~~The blocking dependency: minute data~~ — WITHDRAWN 2026-08-01

**This section was factually wrong.** It checked one empty directory and
concluded the repository had no sub-hourly data. The data exists; see §0.5 for
the inventory and receipt. The experiment this section called "the single most
valuable experiment" has now been run, and it kills H1 rather than saving it:
97.0% of the `j+1` move lands in the first two minutes, against the ~60%
threshold this section itself declared fatal.

The original text is preserved below because the reasoning was right and only
the fact was wrong — and because "check the other roots before declaring a
dependency" is the lesson.

> The `h`=1 / exit-at-print trade is worth Sharpe 2.96 at zero latency and
> Sharpe −2.14 at one hour. The true answer lies between, and it is entirely
> determined by how fast the −44.5 bp at `j+1` accrues. If it is front-loaded into
> the first seconds, the trade is dead and this program's headline hypothesis dies
> with it. If it accrues smoothly over the hour, an exit inside two minutes costs
> ~1.5 bp and the trade survives.
>
> - `~/SHARED_DATA/bybit_full_pit/tick_ohlc_1m/` **exists and is empty (0 files).**
> - There is **no downloader** for it. [...]
> - `premium_index_1h`, `mark_price_1h`, `index_price_1h` exist at hourly
>   resolution and are **not** substitutes.

It was front-loaded into the first seconds.

---

## 5. Hypotheses — final verdicts

| | hypothesis | verdict |
| --- | --- | --- |
| **H1** | Execution-speed harvest | **DEAD.** Twice: the entry gate is not knowable at entry (§0.5), and 97.0% of the move lands inside two minutes, against its own ~60% kill threshold. |
| **H2** | Entry timing for the deployed book | **DEAD.** −336.20 bp per entry (t −6.15), 0/24 phases positive (§0.4). |
| **H3** | Exit timing for the deployed book | **DEAD.** No unconditional pre-print drift to capture: −0.362 bp (t −1.55) (§0.4). |
| **H4** | Sawtooth amplitude as a sizing axis | **DEAD.** It is the print, and it fails the realised-vol control it was required to pass (§0.7). |
| **H5** | Which mechanism | **RESOLVED, to neither candidate.** Ex-dividend adjustment plus selection on a spot rally (§0.1, §0.2, §0.6). |
| **H6** | Does the forming rate predict the print | **ANSWERED.** corr +0.7325, the best PIT signal in the tree; it rescues nothing (§0.8). |
| **H7** | Fill scheduling for the live sleeve | **ANSWERED, and it is the one useful output.** The ~00:20 lag saves ~45 bp per entry, 24/24 phases. Do not reduce it (§0.3). |

---

## 6. Kill criteria — which fired

Declared before the work, per `docs/governance.md`.

1. **Cross-venue replication breaks.** Did not fire. Replication holds (§2), and
   the Binance metrics series independently corroborates the mechanism (§0.6).
2. **H5 resolves with no tradable conditioning.** **FIRED**, and in a stronger
   form than written. The pattern is not merely uninformative beyond depth and
   volatility — its post-print half is a compensated cash transfer that is
   arbitrage-free by construction, and its pre-print half is a spot move.
3. **H1 negative on real minute data *and* H2/H3 fail.** **FIRED, both limbs.**
   H1 fails its own 2-minute criterion at 97.0%, and H2 and H3 both fail across
   a majority — in H2's case all — of the 24 grid phases.
4. **The 2026 slice stays negative.** **FIRED.** Every arm run here is negative
   in 2026 at a realistic cost stack: the PIT anchored trade −44.65 bp/day, the
   PIT unanchored grid −55.57 at its best cell, arm D −9.39.

Three of four fired. The program is closed.

---

## 7. How anything here gets graded

Unchanged from `docs/governance.md`, restated because this program was unusually
prone to three specific errors — the third of which it committed.

- **Clock fragility.** Every carry-family number this repo publishes sits on the
  midnight-UTC decision grid, which is the *best of 24 hourly phases* (v4:
  +9.84 to +22.19 bp/day, Sharpe 0.75 to 1.64, mean 1.13). Midnight is not
  structurally better — it ranks #12 of 24 in the first half of the record and
  #1 in the second, and the winning hour migrates by era. **Report any result
  here as a sweep over phases, never as a single clock.** Every verdict in §5
  carries its 24-phase sweep.
- **Cadence confounds.** Anything measured on a clock rather than in a symbol's
  own settlement sequence reports Bybit's interval mix, which has a strong era
  gradient. §0.5's anchored trade shows the confound in its final form: the
  apparent edge lives in 8h-cadence names and dies in 1h names, which is the
  same fact as its decay from 2022 to 2026.
- **Knowability.** *(added 2026-08-01, and the error that produced this
  program's headline.)* A gate that conditions on a value the venue publishes at
  time `T` cannot open a position at `T−1h`. The settled-funding convention is
  PIT throughout; the *current* print is not, and neither is anything derived
  from it. Test every gate by asking what was published, and when. The cheap
  diagnostic: split the gated events by whether a PIT predictor saw them coming.
  If the unpredictable ones carry the return, the result is look-ahead — here
  they carried 4.5×.

Costs at the measured stack: 7.78 bp per side, 15.56 bp round trip. Report every
grid cell and era split, put costs next to gross, and state which data shaped
the result and which graded it.

---

## Provenance

Opened 2026-07-31 in the scratchpad during the "find a bigger edge" hunt; §1–§3
are that work and its negative results. Closed 2026-08-01 in a second scratchpad
run on the same panel plus `~/SHARED_DATA/bybit_render_1m/klines_1m` and
`binance_full_pit/binance_usdm_metrics_5m`, neither of which the first run knew
was there. All Lane-1, all on seen data; nothing registered, nothing deployed,
no config touched. The §1 measurement was reproduced before anything was built
on it.

Related: [`docs/carry_hold.md`](carry_hold.md),
[`docs/research_findings.md`](research_findings.md),
[`docs/governance.md`](governance.md),
[`docs/backtesting_errors_we_never_repeat.md`](backtesting_errors_we_never_repeat.md),
[`docs/archive/2026-07-31-trend-filters-and-persistence.md`](archive/2026-07-31-trend-filters-and-persistence.md).

---

## 0.9 Independent hourly re-derivation (2026-08-01)

Run because the closure's minute-resolution numbers pointed at a root that does
not exist. These use only the cross-venue panel, which does, and they are what
the closure now rests on until `klines_1m` lands.

Regressing the hourly return *after* each settlement on the print, pooled over
every depth, top-100, n = 722,757:

> **move(0 → +1h) = −0.780 bp + 1.1127 × print_bp,  t(slope) = 62.3**

against the ex-dividend prediction of slope 1.0000. By bucket:

| print bucket, bp | n | mean print | move 0→+1h | move/print | net to a long |
| --- | ---: | ---: | ---: | ---: | ---: |
| < −100 | 2,997 | −167.18 | −193.61 | 1.158 | −26.43 |
| [−100, −60) | 3,191 | −77.67 | −80.32 | 1.034 | −2.65 |
| [−60, −40) | 3,214 | −48.74 | −54.66 | 1.122 | −5.93 |
| [−40, −25) | 5,219 | −31.40 | −28.56 | 0.910 | +2.84 |
| [−25, −15) | 7,899 | −19.30 | −15.24 | 0.789 | +4.06 |
| [−15, −10) | 8,221 | −12.22 | −10.59 | 0.867 | +1.63 |
| shallow \|f\| ≤ 10 | 685,294 | +0.31 | −0.62 | — | −0.93 |
| **> +10 (positive)** | 6,722 | **+17.04** | **+18.35** | **1.077** | +1.31 |

The hourly slope is noisier and slightly steeper than the minute-level 1.0458 —
expected, since an hour of price noise is added to a one-instant step — and the
deepest bucket overshoots. The qualitative result is unchanged and the mirror is
unambiguous.

**What this does *not* re-derive, and what still needs `klines_1m`:**

- §0.3's H7 result — that the CARRY sleeve's ~00:20 fill saves ~45 bp — is
  **UNVERIFIED at minute resolution**. Its magnitude is corroborated (the hourly
  drop is −44.52 bp for deep prints, so a fill landing after it avoids about
  that much) and its direction is safe, but the claim that ~97% of the move
  lands inside two minutes, and the +5m/+10m/+20m/+30m/+60m/+90m profile, are
  not reproducible from the panel. **Do not act on the minute-level shape until
  it is re-derived.** The operational advice it implies — do not reduce the
  kline-availability lag — is supported by the hourly evidence on its own.
- §0.5's "97.0% of the `j+1` move lands inside the first 2 minutes", which is
  H1's declared kill criterion. H1 is dead on execution regardless (§0.5), so
  this changes no verdict.

## 0.10 Fetching the missing root

`scripts/data/download_bybit_klines_1m.py` writes
`bybit_full_pit/klines_1m/date=<d>/symbol=<s>/part.parquet` with the same schema
as `klines_1h`, via `BybitMarketData.get_klines(..., "1", ...)` — the same client
path the 1h builder uses, `category=linear`. Resumable at (symbol, date), flushed
every 14 days, so an interrupted run keeps what it has.

Verified 2026-08-01: the v5 endpoint serves 1-minute klines back to at least
2021-06 for BTCUSDT, 1,440 bars/day complete.

Scope of the first run: the **568 symbols that carry at least one deep top-100
print**, each over its own panel lifetime — 448,628 symbol-days, ordered by
deep-print count so a partial run is still usable (top 100 symbols = 61.9% of
deep prints, top 300 = 93.5%).

This is a dataset inside `bybit_full_pit`. It is **not** a revival of the retired
`bybit_render_1m` plan, which `docs/data.md` forbids recreating.
