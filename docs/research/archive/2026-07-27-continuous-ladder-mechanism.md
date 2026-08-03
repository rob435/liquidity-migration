# CONTINUOUS ladder mechanism + single_fund0 refinement — 2026-07-27

Owner question: the retired 3-cell ensemble read like "a gradual scale-in /
TWAP" — why did it work, can that mechanism be replicated inside the new
single funding-gated cell, and which optimizations is the barebones
`single_fund0` missing?

**Scope and basis.** All work here is Lane-1 on seen data (2023-03-13 →
2026-07-16/17, the window that produced the sl35 and fund0 decisions); it
grades nothing and the forward record still arbitrates. The engine
(`run_continuous_equity_component`) is untouched; new variants use existing
config fields, plus two admission variants via an in-process patch of
`_funding_admission_filter` recorded in their `variant_meta.json`. Hedged
books use the exact deployed overlay stack (combine → winner rule → BTC+ETH
hedge → btcvol intensity) imported from
`scripts/research/continuous_deployed_equity_refresh.py`. Parity: the driver
reproduces the recorded deployed render exactly (+15.85% / −2.85% / fc 1.84
/ 2,372 trades) and research-V3 to rounding. Reproduction:
`scripts/research/render_continuous_admission_variants.py` (artifact dirs key on
`--end-date`). Artifacts:
`~/SHARED_DATA/bybit_full_pit/reports/continuous_ladder_mech_2026-07-17/`.

Sharpe bases are always labelled: **active** = active ledger days only (what
component reports print as `sharpe_like`); **fc** = full-calendar with flat
days as zeros (what the recorded hedged tables use). The redesign note's two
tables mixed these bases without saying so; converting active→fc by
`× sqrt(active_days / 1222)` reconciles every recorded number.

## 1. The TWAP story is wrong — the ladder was an amplitude weighting

Matching every retired base-cell trade (`turn3_pop3`) against sibling
entries in the stricter cells (same symbol, inside the same hold window):

- **92.5%** of base pumps also triggered `turn4_pop3` and **81.2%**
  triggered `turn4_pop5` **in the same hour** — median rung lag 0.0h
  (p75 = 0.0h), median entry-price difference 0.00%.
- There was no gradual scale-in and no price laddering. The nested triggers
  fire simultaneously; the ensemble was one entry whose size was 1.0 for
  pop5-grade pumps (81%), 5/9 for mid-grade (~11%), and 1/3 for marginal
  pop3-only pumps (~8%).

So the retired system was an **amplitude ladder** (size by signal strength
at entry), not a time ladder. Ungated, that weighting earned a small
premium: pop3-only pumps were worse trades (mean +1.1 bp, win 63.1%) than
pop5-grade pumps (+1.4 bp, win 68.8%).

## 2. Full attribution of the retired book's fc-Sharpe edge

Per-day attribution of the recorded hedged curves (bp/day on active days):

| book | gross | entry cost | funding | hedge px | hedge f/c | net | active days |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| deployed 3-cell (retired) | +2.858 | −0.339 | −0.539 | +0.363 | −0.050 | +2.292 | 646 |
| V0 single ungated | +2.847 | −0.434 | −0.590 | +0.404 | −0.060 | +2.167 | 640 |
| V3 single fund0 | +2.595 | −0.393 | −0.262 | +0.312 | −0.062 | +2.190 | 591 |
| fund0 3-cell ensemble | +2.382 | −0.304 | −0.261 | +0.245 | −0.051 | +2.010 | 595 |

Cumulative Sharpe ladders (active | fc), adding one layer at a time:

| book | gross only | +costs | +funding | +hedge = all |
| --- | --- | --- | --- | --- |
| V0 single ungated | 2.96 \| 2.13 | 2.51 \| 1.81 | 1.88 \| 1.36 | 2.24 \| 1.62 |
| V3 single fund0 | 2.95 \| 2.04 | 2.50 \| 1.73 | 2.18 \| 1.51 | 2.47 \| 1.71 |
| deployed 3-cell (retired) | — | — | 2.19 \| 1.59 | 2.54 \| 1.84 |

What this pins down:

1. **The funding admission selects nothing — it is a cost filter.** V0 and
   V3 have identical gross-only and cost-adjusted Sharpe (2.96/2.95,
   2.51/2.50 active). Its entire edge is the funding bill (−0.59 → −0.26
   bp/day), worth +0.30 active Sharpe and the drawdown halving.
2. **The hedge overlay is a return engine in this window, not insurance.**
   A tiny long-BTC/ETH leg (mean hedge ratio ~0.014, active ~90% of days)
   adds +2.3–2.6 pp and +0.25–0.35 fc Sharpe to every book, because the BTC
   gate concentrates activity in uptrends where the long leg drifts up.
   This is gated beta; a chop regime that whipsaws the gate takes it back.
3. **Part of the retired ensemble's recorded cost edge is a modeling
   artifact.** `combine_continuous_components` scales impact by
   weight^0.5 per slice, so splitting the same pump across three books
   discounts modeled impact (−0.339 vs −0.434 bp/day despite ~3× trades).
   The venue executes the aggregated notional, so ~+0.1 bp/day of the
   recorded edge would not survive aggregate-impact pricing.
4. **Density is the real remaining difference.** 646 vs 591/554 active
   days. On active days the retired and fund0 books are nearly the same
   quality (2.54 vs 2.46/2.47); zero-filled to the calendar, the day
   deficit is most of the fc gap. The funding admission buys its drawdown
   halving by sitting out the days the old book spent paying funding.

Density arithmetic: at the shipped cell's active-day quality (2.23 on
today's vintage), fc parity with 1.84 needs ≈832 active days — a +278-day
deficit no admissible lever below approaches. **The retired book's fc
Sharpe is unreachable inside the funding-gated shape; the shape trades
calendar density for drawdown/MAR, which is the side the operator chose.**

## 3. Under the funding admission the amplitude ladder is dead

The rung premium was largely a funding proxy; once the admission handles
funding directly, strictness only cuts sample:

- Ungated cells alone (active mtm Sharpe): turn3_pop3 1.88 < turn4_pop3
  2.00 < turn4_pop5 2.45 — monotone in strictness.
- Funding-gated cells alone: V3 2.18 > V10 2.13 > V9 2.00 — order inverts.
- Inside V3's own trades the amplitude split flips: pop5-grade +1.5 bp mean
  vs pop3-only +2.1 bp (win still favors strong, 69.4% vs 64.4%).
- Per-trade funding split in the ungated cells: the strict rungs' premium
  concentrates on funding<0 trades (turn4_pop5 +0.6 bp vs turn3_pop3
  −0.0 bp), the population the admission removes. (Hold-funding sign is a
  proxy for the settled-print admission; stated as such.)
- Hedged, every ladder reconstruction loses to the plain single cell
  (fc, V3 panel basis): V3 1.71 > inverted-weight ladder 1.68 >
  deployed-weight/equal-weight fund0 ensemble 1.66 > V9 1.51 > V10 1.49.

**Do not rebuild the ladder.** It was an implicit, inferior funding filter
plus an impact-slicing discount plus calendar density. The admission
replaces the selection cleanly; the discount was partly artifact; density
is the only real channel, and it must come from more admitted pumps.

## 4. Replication and knob grid (shipped admission basis)

All new cells run the shipped basis (engine `funding_min_at_entry=0.0`,
root funding, zero unknowns) with gate/age/TP/SL untouched. `fund0_base` is
the deployed shape re-run on today's data vintage; it reconciles the
recorded shipped render at +0.33 pp / +0.04 fc (11.39% vs 11.06%, same
−1.84% maxDD; RMOM/funding partitions moved between sessions). Every cell
run is reported; stats computed uniformly by the committed script (MAR =
annualized/|maxDD| over the common 1,222-day window, so recorded MARs from
other window conventions differ by ≤0.08).

| hedged book | trades | total | maxDD | MAR | Sh (act) | Sh (fc) | active days |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| deployed 3-cell (retired; parity) | 2,372 | 15.85% | −2.85% | 1.58 | 2.54 | 1.84 | 646 |
| research V3 (panel basis; parity) | 704 | 13.72% | −1.69% | 2.32 | 2.46 | 1.71 | 595 |
| **fund0_base (shipped shape)** | 641 | 11.39% | −1.84% | 1.78 | 2.23 | 1.49 | 554 |
| **venue-scoped admission** | 679 | **12.76%** | **−1.75%** | **2.09** | 2.35 | **1.61** | 578 |
| reject-unknown | 641 | 11.39% | −1.84% | 1.78 | 2.23 | 1.49 | 554 |
| TWAP 3-tranche (delay 1/2/3) | 1,923 | 10.55% | −1.51% | 2.01 | 2.20 | 1.50 | 567 |
| crowd-3 | 660 | 9.79% | −2.06% | 1.38 | 1.74 | 1.17 | 553 |
| crowd-off | 666 | 9.73% | −1.99% | 1.41 | 1.72 | 1.16 | 555 |
| hold-12 | 702 | 2.38% | −2.66% | 0.27 | 0.63 | 0.41 | 508 |
| hold-48 | 609 | 12.79% | −3.18% | 1.15 | 1.94 | 1.37 | 610 |

Component-level facts behind the table:

- **reject-unknown ≡ base trade-for-trade** (641 trades): on root funding
  there are no historical unknowns, and the 240-day age gate makes a
  runtime candidate without a settled print near-impossible. The
  unknown-admits follow-up recorded in `docs/strategy_program.md` is
  **empty — closed**.
- **TWAP tranches**: with identical admissions (641 each), later entries
  are strictly worse (delay1 +9.21% / mtm 1.89; delay2 +6.06% / 1.27;
  delay3 +8.07% / 1.79). The TWAP framing is refuted twice: the old
  ensemble never actually time-laddered (§1), and forcing a time ladder
  costs return. The 3-tranche book is a fair DD improver (−1.51%, MAR
  2.01) at −0.84 pp return and 3× the parameter surface — not adopted.
- **Crowding is blocking toxic density, not usable density**: base 1.89 >
  crowd-3 1.41 > crowd-off 1.31 (active mtm), monotone, with worse
  drawdowns. The crowd-2 gate is load-bearing.
- **hold-12 destroys the edge** (+1.94% additive, mtm 0.51): the fade needs
  the full 24h; halving the hold captures half the reversion at full cost.
  This conflates hold and cooldown (cooldown = hold in the engine); it
  refutes hold-12, not a pure cooldown-12 — but with crowd density toxic
  and tranches losing, the prior on "more same-symbol re-entries" is weak
  and does not justify the identity-shifting engine field a clean
  cooldown-only test needs. Deprioritized.
- **hold-48** buys active days (610) and a little return at more than
  double the funding exposure per trade: maxDD −3.18%, MAR 1.15. It sells
  the exact property the shape was chosen for.

## 5. Venue-scoped admission — the one surviving lead

**Rule**: apply the settled-funding floor only to symbols in the both-venue
universe (the 721 symbols of `~/SHARED_DATA/cross_venue_panel_v1`, itself
manifest-backed); Bybit-only contracts admit regardless of funding sign.
Everything else identical to the shipped shape.

Hedged: +12.76% / −1.75% / MAR 2.09 / fc 1.61 vs base +11.39% / −1.84% /
1.78 / 1.49 — the only variant that beats the shipped shape on every
aggregate axis. The delta is 43 extra trades across 28 symbols (+1.20 pp,
mean +2.8 bp, win 74.4%, funding cost −0.37 pp; max concentration 6 trades
in one name) plus 5 chain-effect trades (≈0). This is the population the
render reconciliation flagged prospectively: the funding thesis measurably
does not hold on Bybit-only contracts in this window — their
negative-funding pumps still collapsed.

**Era split, unspun** (the caveat that matters):

| year | extra trades | extra P&L | base fc Sh | scoped fc Sh |
| --- | ---: | ---: | ---: | ---: |
| 2023 | 2 | +0.39 pp | 2.28 | 2.47 |
| 2024 | 10 | −0.22 pp | 1.21 | 1.05 |
| 2025 | 24 | +1.31 pp | 2.75 | 3.35 |
| 2026 | 7 | −0.28 pp | −0.43 | −0.62 |

The aggregate win is carried by 2025 — exactly the era the anomaly research
recorded as the negative-funding explosion, when negative funding became
ubiquitous on small Bybit-only names rather than squeeze-diagnostic. The
mechanism is coherent (the signature worth avoiding is the *both-venue*
crowded short; on Bybit-only names there is no cross-venue arb flow
enforcing it), but the variant **loses in 2024 and 2026** and 43 seen-data
trades decide nothing. Also visible in the same table: the fund0 family
itself is slightly negative in 2026 YTD on seen data (96–98 active days) —
the sleeve's live forward record since the change point is the honest
instrument for that.

**Registration (Lane-2, commit = registration).** This commit registers the
venue-scoped formulation for forward grading: exact executable spec in
`scripts/research/render_continuous_admission_variants.py` (`fund0_venue_scoped`),
universe = the cross-venue panel symbol union, scorer = the same script
re-run with a later `--end-date` on days after this commit, compared against
`fund0_base` re-run identically. No runtime change: the deployed profile is
untouched, and the sleeve's forward record continues to grade the shipped
rule. Promotion would additionally need: a deliberate
`ContinuousEventConfig` admission-scope field (identity-shifting — its own
change point), a frozen committed both-venue registry with a refresh
policy (the panel union must not silently drift), and the five-line note
per `docs/governance.md`.

## 6. Negative results recorded

On the shipped basis, all Lane-1, all reported above: crowd-3, crowd-off,
hold-12, hold-48, TWAP 3-tranche, delay-2 and delay-3 single cells, and (on
the V3 panel basis) the fund0 amplitude ladders at deployed, equal, and
inverted weights, plus the gated strict rungs V9/V10 standalone. The
reject-unknown variant is a null (identical book), closing that follow-up.
Do not re-test these without a new mechanism, new data, or a corrected
defect.
