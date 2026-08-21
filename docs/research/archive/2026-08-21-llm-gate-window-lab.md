# LLM gate — the mechanical trigger, graded

Receipts for the gate's window set, its turnover depth, its exit geometry, and
its signal validity. The conclusions live in
[trading_logic.md](../../trading_logic.md) §LLM GATE; the tables are here.

## What was measured

The gate's trigger rebuilt on history with the LLM removed, so what is graded
is the mechanical part alone: top-N by trailing 24h turnover, a move clearing
`2.5 × sigma_daily_30d × sqrt(h/24)`, range location ≥ 0.70, BTC-and-ETH daily
regime on, ATR-14d in (0, 0.12]. The LLM's own contribution is not in these
numbers and is not gradeable backwards.

- **Data**: `bybit_full_pit/klines_1h`, 2021-01-01 → 2026-08-14, 1,019 symbols.
  The archive carries delisted names (136 present 2024-06-15 and gone by
  2026-06-15), and point-in-time membership filtering drops **zero** rows from a
  top-30-by-turnover universe — measured, so the universe needs no survivorship
  caveat.
- **Events**: ~70,000 triggers across five windows; 48,876 on the three kept.
- **Entry**: the OPEN of the bar *after* the trigger bar. The trigger bar's own
  close is not reachable — the ledger wakes at :05 and the producer acts inside
  a minute.
- **Exit**: the sleeve's own geometry — 3×ATR stop, 1.5×ATR from 48h, 72h max
  hold, first touch, stop taken first on an ambiguous bar.
- **Costs**: 15.0 bp round trip, `CostConfig.base_entry_exit_cost_bps`.
- **Bar-path ambiguity**: stop and target both touched inside one hourly bar on
  9–15 events of ~70,000. Minute bars were not needed.

## Windows — net bp per trade, by era

| year | 1h | 2h | 4h | 12h | 24h |
| --- | --- | --- | --- | --- | --- |
| 2021 | +58 | +56 | +24 | −44 | **−124** (t −3.2) |
| 2022 | **−175** (t −4.6) | **−106** (t −2.7) | +20 | +178 | +167 |
| 2023 | +91 | +90 | +93 | +104 | +40 |
| 2024 | +208 | +205 | +174 | +207 | +228 |
| 2025 | −22 | −37 | −35 | +58 | −4 |
| 2026 | +10 | +74 | +196 | +302 | +489 |
| **pooled** | +60 | +76 | +98 | **+150** | **+150** |

1h and 2h each carry a significantly negative year and are not run. 12h is the
only window with none.

## Turnover depth — the strongest single measurement

Pooled over the kept windows, entry at +1h:

| entry universe | n | net bp | win % | t |
| --- | --- | --- | --- | --- |
| rank ≤ 5 | 8,510 | **+433** | 49.0 | 10.9 |
| rank ≤ 10 | 18,493 | **+308** | 45.7 | 12.2 |
| rank ≤ 15 | 26,995 | +245 | 44.9 | 12.1 |
| rank ≤ 20 | 34,911 | +206 | 44.1 | 12.3 |
| rank ≤ 30 | 48,876 | +154 | 42.8 | 11.8 |

Monotone, and rank ≤ 15 beats rank ≤ 30 in **every** year including 2021, which
flips from −29 to +29. The entry scan runs at 10; the research scan stays at 30.

## Exit geometry — net bp per trade, pooled

| window | live 3×ATR | decay 1.5× @48h | TP 4×ATR | TP 3×ATR | TP 2×ATR | trail 3×ATR |
| --- | --- | --- | --- | --- | --- | --- |
| 4h | +98 | **+108** | +72 | +62 | +45 | +67 |
| 12h | +150 | **+168** | +116 | +107 | +86 | +94 |
| 24h | +150 | **+168** | +85 | +75 | +59 | +61 |

**Take-profit is negative at every multiple.** The 4×ATR target in the v12
profile came from a daily-signal backtest; on hourly triggers it cuts the right
tail these trades live on. Its absence from the live path is correct.

**The decayed stop is worth +13 to +19 bp a trade** and helps in 26 of 30
(year, window) cells, most in the recent era (+20 to +57 in 2025–26). It is
computed by the producer and never reaches the venue.

**Trailing was a one-year trap**: best of all variants on 2025 alone, worse than
the plain stop pooled.

## Signal decay — net bp by entry delay

Pooled over the kept windows, decayed-stop geometry:

| delay | +1h | +2h | +3h | +4h | +6h | +9h | +12h | +18h | +24h |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| net bp | 154 | 153 | 151 | 145 | 138 | 123 | 113 | 101 | 108 |

The edge decays slowly — 94% intact at +4h, 74% at +12h. **The one-hour validity
is not required by decay**; it costs about 2% of the edge and is justified
operationally instead: the ledger republishes hourly, so a file approaching an
hour old means a run was missed, and a missed run is not a signal.

## Scam pumps — what separates them

Quartiles of each candidate discriminator, kept windows, net bp:

| feature | q1 | q2 | q3 | q4 | verdict |
| --- | --- | --- | --- | --- | --- |
| turnover rank | +357 | +120 | +90 | +30 | **monotone, strongest** |
| turnover 24h (abs) | +43 | +86 | +208 | +278 | **monotone** |
| up-bar share | +149 | +92 | +149 | +284 | weak |
| depth past the bar | +121 | +131 | +172 | +191 | mild, win % falls |
| range location | +164 | +200 | +153 | +98 | mild, *downward* |
| one-bar share of the move | +187 | +185 | +77 | +167 | **no signal** |
| turnover concentration | +165 | +177 | +130 | +149 | **no signal** |
| prior fires, 90d | +120 | +154 | +247 | +96 | **no signal** |
| ATR % | +210 | +34 | +142 | +230 | **no signal** (U-shaped) |

**A scam pump is a liquidity fact, not a shape fact.** Every shape hypothesis
died: the vertical one-candle pump, the turnover spike, and the name that keeps
firing all fail to separate outcomes. What separates them is how big the book
is. That belongs in the universe rule, which is why the entry scan tightened to
rank 10 rather than the prompt gaining a shape heuristic.

Refuted at the same time: **the hour of day predicts nothing usable.** The
rubric's own prior — that triggers after 12:00 UTC confirm materially better
than 00–06 — is backwards on this population.

| block UTC | n | net bp | win % |
| --- | --- | --- | --- |
| 00–06 | 13,480 | +151 | 43.8 |
| 06–12 | 11,673 | +158 | 43.0 |
| 12–18 | 11,833 | **+112** | 40.9 |
| 18–24 | 11,890 | +195 | 43.4 |

## Additional exit conditions — twenty-one tested, none kept

Every overlay below sits on top of the sleeve's own geometry (3xATR narrowing
to 1.5x at 48h, 72h max hold) and can only cut a trade short. Graded on the
live configuration — windows 4/12/24, turnover rank <= 10 — over six years and
18,493 events. Overlays fire on a completed bar's close, which is what a 60 s
producer reading completed hourly bars can actually reach.

| overlay | vs baseline |
| --- | --- |
| funding spike 30bp/8h | +6.9 bp |
| funding paid 1.00% | +1.8 |
| **baseline (decay only)** | **0** |
| btc-rel −20% | −14.3 |
| funding paid 0.10% | −15.4 |
| stall 36h | −26.7 |
| btc-rel −10% | −51.2 |
| btc-rel −5% | −93.9 |
| stall 12h | −183.8 |
| mfe giveback 70% | −251.4 |
| premium flips negative | −265.9 |
| vol exhaust 0.5× | −270.1 |
| mfe giveback 50% | −291.5 |

**Nothing survives.** The two that finish above the baseline do so by under
7 bp and both are *worse* than it in 2021 and 2022; `funding paid 1.00%` is
identical to the baseline in four of six years because it almost never fires.

The pattern is the same one the take-profit showed: this strategy's return is a
right tail, and every rule that caps a winner destroys far more than it saves.
The MFE give-back ratchet — the most intuitively appealing rule on the list, and
one that scales with how far the trade ran rather than using a fixed distance —
is the second worst at −292 bp a trade.

**BTC-relative was a two-era mirage.** On 2021–22 alone, exiting when the name
lagged BTC by 5–10% led the whole table at +13 bp. Over six years both variants
are firmly negative (−94 and −51). It joins the trailing stop and the two
long/short ratios in the file of things that looked like alpha on a slice.

**And the stop is already at the right width.** Narrowing it costs
monotonically, and the widest is best in five of six years (tied in the sixth):

| stop | net bp | win % |
| --- | --- | --- |
| **3×ATR, narrowing to 1.5× at 48h** | **+308** | 45.7 |
| 2.5×ATR flat | +273 | 45.1 |
| 2×ATR flat | +224 | 43.8 |
| 1.5×ATR flat | +185 | 41.3 |

Which makes the decay contract's shape the point: **wide early, narrow late.**
A 1.5×ATR stop from entry is the worst variant tested, and the same 1.5×ATR
applied from 48 hours is worth +13 to +19 bp. The trade needs room while it is
still deciding, and pays to have it taken away once it has had two days to
work. v12's design is right and only its plumbing was broken.

**The BTC-relative exit was a tighter stop in a market-neutral costume.**
Against its own control — the same trigger distance with BTC ignored entirely:

| | net bp |
| --- | --- |
| btc-rel −10% | +257 |
| hard −10%, BTC ignored | +252 |
| btc-rel −5% | +214 |
| hard −5%, BTC ignored | +205 |

Five to nine basis points separate them out of 250. The market adjustment
contributes essentially nothing; what the rule was doing was stopping out
earlier, and stopping out earlier is what the width table above already prices.

**What this means for the exit system.** The decayed stop was the whole of the
available exit alpha, and it is now real at the venue. Nothing measured here
adds to it. A further exit idea should have to beat this table before it is
built.

## Order flow — one signal, and two that were one year in disguise

Bybit publishes no taker-side breakdown, so this comes from
`~/SHARED_DATA/binance_metrics_daily` (2022-04 → 2026-08, 649 symbols): the mean
of the day's 288 five-minute taker buy/sell volume ratios, plus top-trader and
all-account long/short ratios. Delisted names are present — 93 of the 128
symbols carrying rows before 2023 are gone by mid-2026 — so unlike Bybit's own
open interest this can be graded backwards. Binance data on Bybit positions,
covering 67% of events in 2022 rising to 91% in 2026, and every EOD row is
joined so it is knowable only from 00:00 the following day.

**Aggressive buying at entry separates, but not cleanly enough to be a rule.**
Split at a `taker_buy_sell_ratio_1d` of 1.07:

| year | mean below | mean above | win% below | win% above |
| --- | --- | --- | --- | --- |
| 2022 | +476 bp | +115 | 44.6 | **55.3** |
| 2023 | +310 | +65 | 48.8 | 43.0 |
| 2024 | +448 | +119 | 52.2 | 37.9 |
| 2025 | +262 | −305 | 47.9 | 38.9 |
| 2026 | +800 | **+1541** | 33.4 | 31.0 |
| **pooled** | **+410** | **+267** | **48.1** | **41.0** |

Quieter pumps are better on the mean in four of five years and win more often
in four of five — but they are *different* exception years, so neither claim
survives as universal. What is pooled-consistent is the hit rate and the median
trade: 48.1% against 41.0%, and −38 bp against −207 bp.

2026's reversal is the tail, not the centre. Both halves have a deeply negative
median (−484 and −565) and a win rate near 32%; the means come from a fat right
tail that favoured the aggressively-bought names in a melt-up. Trimming the top
1% takes the above-1.07 mean from 1541 to 788 and the below from 800 to 286 —
the reversal survives the trim, so it is a real regime effect and not one
monster trade.

**The two long/short ratios do not survive an era split**, and both look
superb without one. Top-trader long/short above 1.3 shows −448 bp a trade
pooled at t −11.0 — the largest-looking figure in the whole program:

| top-trader L/S > 1.3 | 2022 | 2023 | 2024 | 2025 |
| --- | --- | --- | --- | --- |
| below | +248 | +228 | +321 | +363 |
| above | **+282** | +71 | **+321** | **−731** |

Above is *better* in 2022 and identical in 2024. The whole effect is 2025.
All-account long/short behaves the same way (flat in 2022 and 2024, −751 in
2025). Neither is used. A pooled t-statistic of −11 built from one regime is
the exact failure this table exists to catch.

**Open interest rising hard is good, not fragile.** The top OI-growth quartile
(median +94% in a day) is the best of the four at +702 bp. The rubric's
leverage-chase step had this pointed the other way.

**As an exit, flow fails.** Best overlay was `top-trader L/S < 0.8` at +7.6 bp;
`taker ratio < 1.1` costs −199 bp. One cell (`taker ratio < 0.9`) returned
exactly the baseline because it never fires — 0.9 sits below the data's 10th
percentile. That is a vacuous condition, not a null result.

**What was incorporated.** `taker_buy_sell_ratio_1d` becomes a fact the rubric
receives, stated as a caution with its exception years named rather than as a
rule, and the leverage-chase prior is
corrected. It is deliberately *not* a hard gate: the events with no Binance
coverage average +298 bp, better than the covered ones, so filtering on
coverage would throw away good trades. The live fetch reproduces the archived
statistic — 6 of 8 sampled symbol-days agree within 0.005 — which matters
because the day's aggregate ratio is a different and lower number than the mean
of its five-minute ratios.

## What this does not establish

The LLM's judgement is not in any number here. Every figure is the mechanical
trigger's, so these grade the *population the model is shown*, not the model.
Whether a score ≥ 6 beats the unfiltered trigger is a forward question and the
live ledger is the only instrument for it.

One measured feature was discarded as broken rather than reported: listing age
was computed per load-chunk, so it indexed position-within-the-year, not age.
