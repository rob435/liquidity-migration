# Repository and strategy audit — 2026-07-24

Brutally honest read of where this project actually is, what the tail-risk
problem actually is, and what to do next. Every number below was measured
during the audit from local PIT data or the repository itself; none is copied
from an earlier document. Where this contradicts a current doc, the
contradiction is stated explicitly.

This document changes no runtime authority, deploys nothing, and does not open
the real-money boundary.

---

## 1. Bottom line

Three findings, in order of importance:

1. **The project builds execution infrastructure, not alpha.** 2.8% of package
   code is signal/risk logic. 49% is account/execution/ops plumbing. Every
   commit since the 2026-07-21 research reset — plus the entire uncommitted
   working tree — is deployment automation. The `P0` research substrate that
   `docs/strategy_program.md` names as the next action does not exist.

2. **The tail risk is payoff geometry, not a tuning problem.** A short book in
   this universe takes 22.4% of its total loss in 1% of its trades. Removing
   market beta relieves only 5% of that tail. No overlay, stop, or governor
   fixes this; only not being short the crowded name does.

3. **The carry has inverted, and it explains the tail.** Median funding still
   pays a short about +5.5%/yr, but *mean* funding now costs a short about
   -4.5%/yr. The entire gap is a fat negative-funding tail that arrived in
   2025-26 — and it fires on the same events as the price squeeze. A short book
   now pays twice on the same event.

The strategic implication: stop hardening the platform, build the smallest
honest cross-venue panel, and research the crowding state directly using the
two deep assets nobody here has touched — Bybit open interest (5.5 years) and
cross-venue funding/premium divergence (5.5 years).

---

## 2. Where the code mass is

Package: 72,671 lines across 111 modules. Tests: 64,444 lines, 2,023 test
functions. Measured by functional category:

| Category | Lines | Share |
| --- | ---: | ---: |
| Account / execution / ops infrastructure | 35,609 | 49.0% |
| Strategy runtime & demo harness | 22,278 | 30.7% |
| Data ingest / storage | 12,760 | 17.6% |
| **Alpha / signal / risk** | **2,012** | **2.8%** |

The entire alpha surface is six files: `daily_feature_panel.py` (946),
`sleeve_kill_criteria.py` (281), `risk_model.py` (281),
`residual_momentum.py` (210), `execution_cost_model.py` (201),
`momentum_signals.py` (93).

**There is no dead code.** An AST pass over every import in the repository
found zero unimported modules; four modules are imported only by tests and all
four are process entry points. The bloat is not dead lines — it is *scope*.
Deleting code is therefore not the main cleanup lever; stopping work is.

## 3. Where the effort goes

| Date | Commits | Content |
| --- | ---: | --- |
| 2026-07-20 | 48 | Tail-risk research program (P0–P2, overlays, forward recorder) |
| 2026-07-21 | 2 | Reset — deleted the entire tail-risk program and its docs |
| 2026-07-22 | 15 | Ops firefighting: rollout, reset, recovery, journal race, paper reductions |
| 2026-07-23/24 | 0 | Uncommitted: +1,293 lines of deployment automation |

A full day of research was produced and deleted the next day. Everything since
is plumbing. `docs/tail_risk_program.md`, referenced as the main focus on
2026-07-20, no longer exists — the reset retired it.

The four open items in `docs/strategy_program.md`'s live queue are all
unstarted. Confirmed by search: no cross-venue panel builder, no crowding
module, nothing named for the P0 substrate.

This is a feedback loop, not an accident. Ops work has fast, legible feedback
(tests pass, deploy is green, page resolves). Research has slow, ambiguous
feedback and usually returns a negative. The system reliably rewards the
former. **That is the thing to fix, and it is fixed by budget, not by
intention.**

---

## 4. The tail risk, measured

Bybit daily panel, 5-day forward returns, symbols with 20-day ADV > 12M USDT.
N = 131,039 symbol-days, 2022–2026.

Distribution: **skew +20.8, kurtosis 1,362.** Median -120 bp, mean +118 bp —
most coins bleed, a few explode.

| Percentile | 5-day return |
| --- | ---: |
| p1 | -3,652 bp |
| p50 | -120 bp |
| p99 | +8,051 bp |
| p99.9 | +23,821 bp |

Asymmetry between the two sides of that distribution:

| | worst 1% of trades, mean | worst case | share of all losses in worst 1% |
| --- | ---: | ---: | ---: |
| **Short** | **-15,301 bp** | -249,169 bp | **22.4%** |
| **Long** | -4,879 bp | -9,997 bp (floored) | 8.6% |

A long cannot lose more than 100%. A short's loss is unbounded, and 49.1% of
all short losses land in the worst 5% of trades.

### Three results that kill the obvious fixes

**Liquidity screening does not help — it makes it worse.** Splitting by ADV
quartile:

| ADV quartile | median ADV | worst 1% short |
| --- | ---: | ---: |
| Q1 thinnest | 14.9M | -11,422 bp |
| Q2 | 24.6M | -13,219 bp |
| Q3 | 47.7M | -16,394 bp |
| **Q4 deepest** | **146.8M** | **-19,719 bp** |

The worst squeezes happen in the *most* liquid names, because those are the
names that attract crowded shorts. Trading only liquid symbols increases tail
exposure.

**The tail is idiosyncratic.** On a median day 0.80% of the universe is in the
top-1% tail; 44.7% of days have zero tail events. There is real clustering —
26.2% of tail events land on the worst 5% of days, and 2025-04-26 saw 12.9% of
the universe squeeze at once — but the clustering is the minority of the mass.

**Beta hedging is nearly useless here.** Removing the cross-sectional daily
mean return (a perfect, look-ahead market hedge) moves the worst-1% short loss
from -15,301 bp to -14,514 bp: **5% tail relief**, and that is the optimistic
in-sample bound.

This directly bears on the retired tail-risk program. Its headline result was
a BTC-risk intensity overlay buying 19–33% tail relief for ~3.8 pp/yr of
premium. Since beta removal alone gives 5%, that overlay was not hedging beta —
it was de-grossing during dangerous regimes, i.e. paying 3.8 pp/yr to trade
less. That is a position-sizing decision wearing a hedge's clothing, and it was
correctly retired.

**Conclusion: this tail cannot be hedged at the book level. It has to be
avoided at the name level.** That makes the crowding state — who is positioned
where, and is it unwinding — the only research direction that addresses the
user-visible problem.

## 5. The carry inversion

Bybit funding, 203 sampled days across 2021-01-01 → 2026-07-17, 269,642
settlement observations:

| Year | mean bp | median bp | % negative | p1 bp |
| --- | ---: | ---: | ---: | ---: |
| 2021 | +2.55 | +1.0 | 9.0% | -12.8 |
| 2022 | -0.57 | +1.0 | 22.2% | -22.9 |
| 2023 | +0.30 | +1.0 | 12.6% | -19.0 |
| 2024 | +0.66 | +1.0 | 8.8% | -13.8 |
| **2025** | **-0.75** | +0.5 | 16.2% | **-27.7** |
| **2026** | **-0.88** | +0.5 | **20.7%** | **-30.2** |

The median stayed positive while the mean went negative. That is a pure
tail-thickening: the p1 funding roughly doubled from -13.8 bp (2024) to
-30.2 bp (2026), and the negative share went from 8.8% to 20.7%.

Translated to an annualised short position (3 settlements/day):

- **Median coin: about +5.5%/yr paid *to* a short.**
- **Mean coin: about -4.5%/yr paid *by* a short.**
- The ~10 pp gap is entirely tail.

**The mechanism.** Extreme negative funding means shorts are crowded and paying
longs to hold. That is the same state that produces the squeeze. So in 2025-26 a
short-biased book pays the funding tail and the price tail on the *same event*,
simultaneously. This is a sufficient explanation for why a book that worked
historically now bleeds with fat tails, and it did not require a strategy bug.

It also names the trade. If the crowded-short state is identifiable ex ante from
funding, open interest, and cross-venue premium — all of which we hold for 5.5
years — then the interesting position is on the *other* side of it, with bounded
downside.

## 6. What the data actually supports

Corrected against `docs/data_roots.md` and `docs/strategy_program.md`, both of
which overstate the flow data.

**Tier A — deep, wide, both venues, ~5.5 years.** 1h klines, funding, premium
index, mark/index price.

| Dataset | Bybit | Binance | Common symbols |
| --- | --- | --- | ---: |
| klines_1h | 2021-01-01 → 2026-07-16 | 2020-01-01 → 2026-07-16 | **579** |
| premium_index_1h | 2021-01-01 → 2026-07-17 | 2019-12-24 → 2026-07-17 | **566** |
| funding | 2021-01-01 → 2026-07-17 | 2019-09-10 → 2026-07-17 | **466** |

**Tier B — deep, wide, Bybit only.** `open_interest`, 2021-01-01 → 2026-07-17,
growing 6 → 636 symbols as listings accumulate. **This is the best asset in the
building and no research here has used it.**

**Tier C — wide but shallow, Binance only.** `open_interest` (637 symbols, 70
days from 2026-04-27) and `taker_flow_1h` (658 symbols, 67 days). Enough for a
recent diagnostic, not for era analysis.

**Two source defects found while building the P0 panel against these roots.**
Both were invisible until real data was read end to end, and both are the kind
of artifact the program asks to be surfaced rather than absorbed:

1. **`open_interest_value` is not a value.** Across the whole Bybit
   `open_interest` dataset it is a byte-for-byte duplicate of `open_interest`
   — 55,060 for BTCUSDT, i.e. contract units, not quote notional. A consumer
   reading it as USD would understate BTC open interest by roughly the price.
   The panel drops the column and derives `by_oi_notional = open_interest ×
   mark_close` instead.
2. **`funding_event_kind` exists on only 2 of 2,024 Bybit funding partitions**
   (from 2026-07-16). It distinguishes `settlement` from `predicted`. Filtering
   on it naively drops 99.9% of history; ignoring it admits *unsettled
   predicted rates* into recent decision rows, which is direct lookahead. The
   panel scans the two schema generations separately: settlements only where
   the column exists, all rows where it predates the column.

### Bybit open interest is survivorship-contaminated

Found by building the panel and auditing its own coverage flags — it is not
visible from a partition census, because the partitions exist and look healthy.

In the 2024 shard, OI coverage is bimodal: 244 of 347 symbols are ~100%
covered, 90 are **exactly zero**, 13 partial. That split is not random. Testing
each cohort against symbols still listed on 2026-07-16:

| 2024 cohort | n | still listed 2026 |
| --- | ---: | ---: |
| Symbols **with** OI history | 244 | **95.9%** |
| Symbols **without** OI history | 90 | **0.0%** |

Perfect separation. The OI dataset was backfilled only for contracts still
listed at backfill time, so **every delisted symbol has no OI history at all.**

Field coverage by survival cohort confirms the defect is specific to OI:

| 2024 cohort | n | price | funding | premium | Binance funding | **OI** |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Delisted | 100 | 0.999 | 0.999 | 0.998 | 0.928 | **0.100** |
| Survived | 247 | 0.998 | 0.999 | 0.998 | 0.914 | **0.998** |

**Consequence, and it reorders the plan below.** Any study that requires OI
silently restricts itself to survivors — and delisted alts are precisely the
population where short exposure pays. An OI-conditioned short study would be
biased against its own thesis; an OI-conditioned long study biased for it.
Either way the number would not mean what it appears to mean. Rules:

- Never treat OI availability as a filter in a return study.
- Funding, premium, and price are population-complete across both cohorts and
  are therefore the primary crowding measurements.
- OI is a corroborating variable on the survivor cohort only, and any
  OI-conditioned result must be replicated funding-only on the full population
  before it means anything.

**Tier D — not a research panel, despite the names.**
- `bybit/taker_flow_5m` and `tick_ohlc_1m`: 401 distinct symbols but a **median
  of 11 days each** (min 1, max 78), scattered 2023-2026. These are event
  windows, not a panel. `docs/data_roots.md` and `strategy_program.md` describe
  this as "available from 2023-03-29 but has gaps"; that materially overstates
  it. Any cross-sectional microstructure plan built on it will fail.
- `bybit/positioning_lsr`: **empty** (0 partitions).
- `binance_usdm_metrics_5m`: **empty** (0 partitions).

**Consequence for the plan:** cross-venue microstructure research is not
currently possible. Cross-venue *crowding-state* research on
price/funding/premium/basis plus Bybit OI is well supported, over 466–579
symbols and 5.5 years. Build for what exists.

## 7. Disk and repository weight

Working tree 2.5 GB; `.git` 461 MB; `~/SHARED_DATA` 46 GB.

Reclaimable, all derived or scratch, all regenerable:

| Item | Size | Note |
| --- | ---: | --- |
| `~/SHARED_DATA/_bybit_taker_flow_tmp` | **8.0 GB** | 404 symbol dirs, many empty; last written 2026-06-12. Concentrated in 1000BONK (1.9G), 1000PEPE (1.1G), REEF (549M). Raw source for the Tier-D event dataset. |
| `~/SHARED_DATA/*/_continuous_engine_panel_v*.parquet` | **3.7 GB** | 38 derived panels from the retired V2 program |
| `reports/research-refresh/` | **1.1 GB** | untracked scratch |
| `graphify-out/` | 78 MB | regenerable |
| `~/SHARED_DATA/` one-off run dirs (9) | 48 MB | `tail_no_tp_*`, `official_short_book_5x_*`, etc. |

Total ≈ **13 GB**, about 26% of `SHARED_DATA`.

`.git` at 461 MB is dominated by history-only blobs: `graphify-out` 70.7 MB
(graph.json committed ~20 times at 1.3–1.6 MB), `reports` 51.3 MB,
`aggression_carry` 16.8 MB. All are now gitignored, so this does not grow.
**Do not rewrite history:** the deployment machinery pins exact commit SHAs
(`a9ac75d…`), and STATE.md, receipts, and verification hashes reference them. A
rewrite invalidates the entire receipt chain to reclaim ~140 MB. Not worth it.

Retired research code that can be deleted outright (5 scripts + their tests,
**5,373 lines**): `analyze_strategy_overhaul_v2.py` (V2 program retired),
`breadth_power.py` (0 references), `freeze_bybit_lifecycle_census.py`
(0 references), `build_candidate_tape.py`, `build_prospective_feature_bundle.py`.
Note `build_candidate_tape.py` is cited by `docs/preregistration/INDEX.md`;
deleting it makes that receipt non-reproducible, which is a deliberate
trade-off, not an oversight.

Also: `scripts/archived/` is empty and `scripts/research_v3/` contains only
`__pycache__`.

---

## 8. The plan

### Phase 0 — Stop the bleeding (1–2 days)

The cleanup that matters most is a budget rule, not a deletion.

1. **Land the in-flight deployment work and then freeze the ops surface.** The
   uncommitted 1,293 lines are finished; commit them. After that, no new
   account/execution/deploy/reset feature unless it is fixing something
   currently broken in demo. Not "hardening", not "acceleration", not
   "automation". The platform is years ahead of the alpha.
2. **Reclaim ~13 GB** per §7 (requires explicit approval; see the note on
   `_bybit_taker_flow_tmp`, which is raw source and should be confirmed
   unneeded before deletion).
3. **Delete the 5,373 lines of retired research plumbing** and the two empty
   script dirs.
4. **Correct the stale docs in the same change**: the Tier-D flow-data claims in
   `docs/data_roots.md` and `docs/strategy_program.md`, and the dangling
   `docs/tail_risk_program.md` reference.

### Phase 1 — Build the P0 substrate, small and once

One module, one builder, hard line-count discipline. Target **≤800 lines
including tests**. If it grows past that, the scope is wrong.

- Cross-venue hourly panel over the **466 symbols** with both-venue
  kline + funding + premium, 2021-01 → present.
- Fields: bybit/binance close, mark, index, premium, settled funding, turnover;
  **bybit open interest** (Tier B). One coverage flag per field per row.
- Exact symbol mapping; collisions and contract mismatches rejected, not
  silently mapped.
- Decision time, publication time, explicit execution delay, no backward fill.
- One manifest: git SHA, config hash, date and population bounds, coverage by
  venue-year, all exclusions.

Deliverable: the panel, focused synthetic timing/mapping tests, one manifest.
Nothing else. Resist adding fields until a live question needs them.

**Built 2026-07-24.** `liquidity_migration/cross_venue_panel.py` +
`scripts/build_cross_venue_panel.py`, 27 focused tests. Sharded by calendar
year to bound memory; each shard carries its own manifest and SHA-256, with a
top-level `index.json`. Output at `~/SHARED_DATA/cross_venue_panel_v1`
(598 MB, 11,576,400 rows, `[2021-01-01, 2026-07-18)`).

Published coverage map — the fraction of rows carrying each field:

| Year | Rows | Symbols | by price | by funding | by premium | **by OI** | bn price | bn funding |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2021 | 192,864 | 84 | 0.997 | 0.996 | 0.997 | 0.857 | 0.997 | 0.997 |
| 2022 | 1,107,816 | 143 | 1.000 | 0.999 | 1.000 | **0.740** | 0.968 | 0.983 |
| 2023 | 1,657,680 | 234 | 1.000 | 1.000 | 1.000 | **0.734** | 0.943 | 0.943 |
| 2024 | 2,526,888 | 347 | 1.000 | 1.000 | 1.000 | **0.749** | 0.936 | 0.940 |
| 2025 | 3,738,696 | 552 | 1.000 | 0.999 | 0.999 | **0.841** | 0.976 | 0.976 |
| 2026 | 2,352,456 | 613 | 1.000 | 1.000 | 0.999 | 0.980 | 0.997 | 0.997 |

The OI column is the survivorship defect in §6 seen from the other side: it
rises toward 1.0 in 2026 only because recent listings have not yet had time to
delist. Read it as a bias gradient, not as improving data quality.

Line budget: the P0 target was ≤800 lines including tests; actual is 1,144
(623 implementation, 521 tests). Recorded rather than rationalised — the extra
tests cover the three real-data defects above, but the budget was still missed.

### Phase 2 — Anomaly atlas, aimed at the crowding state

Ranked by expected information gain given what §4–§6 established. All Lane-1 on
seen data; none of it grades anything.

**A1 (first). Crowded-short reversal — the direct inverse of the failing
book.** §5 shows extreme negative funding identifies the crowded state ex ante,
and §6 shows funding is population-complete across survivors *and* delisted
names. Test the forward 1d/3d/5d return of a *long* conditioned on
cross-sectional funding percentile, full 5.5 years. Bounded downside by
construction, survivorship-clean by measurement. Note the distinction from
retired work: the T-M study tested extreme-funding *carry* (collect the
funding), which failed. This is a *reversal* trade (capture the squeeze);
different object, different payoff. Verify that distinction against the T-M
receipt before claiming novelty.

**A2 (second, and constrained). Open-interest state transitions.** ΔPrice × ΔOI
has four quadrants: new longs, short covering, new shorts, long liquidation. OI
is the direct positioning measurement that funding only proxies. This was
ranked first until the coverage audit in §6 showed OI is survivor-only — so it
is now a corroborating study on the survivor cohort, never a population claim,
and every OI-conditioned result must be replicated funding-only on the full
population before it counts.

**A5 (highest expected value, because it pays off even if all alpha fails).
A PIT squeeze-hazard score.** §4 proved the tail is idiosyncratic and
un-hedgeable at the book level, so the useful artifact is a per-symbol,
point-in-time hazard estimate built from funding percentile, OI trajectory, and
cross-venue premium divergence. It improves *any* book — including the two live
sleeves — by vetoing entries into names about to squeeze, and it can be graded
forward immediately because it makes a falsifiable daily prediction. This is the
one item that directly answers the stated tail-risk problem.

**A3. Cross-venue crowding transfer** — the existing `strategy_program.md`
hypothesis, now better motivated. 566 common premium symbols, 5.5 years. Treat
Bybit-minus-Binance premium/funding divergence as a *state variable feeding A5*,
not as a standalone return predictor. Venue divergence was already preserved as
an anomaly lead by the 2026-07-21 reset.

**A4. Tail-conditioned carry.** §5 says median short carry is +5.5%/yr and mean
is -4.5%/yr, with the gap entirely tail. So: does a cross-sectional funding-carry
book that *excludes* the crowded state (bottom-decile funding, OI spike, premium
divergence) recover the median? This is a portfolio-construction question, cheap
once A1–A3 features exist, and it is the natural way to monetise the hazard model
rather than only defending with it.

For each: report the full tested surface, era splits, effect size, uncertainty,
concentration, turnover, capacity, and missingness. Put gross next to
claim-appropriate stressed costs and funding. Keep the search log honest.

### Phase 3 — Rolling forward grade

Unchanged from `docs/governance.md`. When a formulation is worth grading, commit
its exact config and scorer before the first new day; that commit is the
registration. The live LONG/CONTINUOUS sleeves stay as controls and are not
modified to help a challenger. Promotion is a five-line note plus a recorded
change point, and means demo only. Mainnet remains a separate owner door.

---

## 9. Guardrails

- **Research/ops budget.** Ops work is capped until the Phase-1 panel exists.
  Track the ratio; if a week produces more ops lines than research lines without
  a live demo failure driving it, the loop from §3 has reasserted itself.
- **Line budget on new research code.** Phase 1 is ≤800 lines. The last research
  program generated 48 commits of bespoke runners in one day and was deleted the
  next. Reusable panel, disposable analyses — never the reverse.
- **Payoff geometry is a design constraint.** §4 makes unbounded short exposure
  to an illiquid-or-liquid alt a structural hazard. Any candidate that requires
  naked shorts in the cross-section must justify the tail explicitly, and
  liquidity screening does not count as a justification.
- **No history rewrite.** §7.
- **Unchanged:** offline/demo/paper default, `REAL_MONEY` stays disabled, and no
  result in this document authorizes a deployment.
