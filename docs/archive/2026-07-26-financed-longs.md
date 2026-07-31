# Financed longs — the 2026-07-26 program

One-day Lane-1 program run under an owner goal: *change the repository's
signal/strategy as I see fit; find three alphas that beat the deployed
CONTINUOUS system on return and Sharpe after full realistic round-trip fees,
with no shortcuts.* This document is the complete evidence note per
`docs/governance.md` §4 — including every negative result, because the
negatives carry most of the information.

**Labelling.** Everything here is Lane-1 exploration on already-seen data
(cross-venue panel, 2021-01-01..2026-07-18). These runs selected the three
registered configurations and therefore cannot grade them. The registrations
(`configs/lane2_carry_hold_v1.json`, `configs/lane2_financed_leaders_v1.json`,
`configs/lane2_financed_leaders_binance_v1.json`) grade forward from their
commit.

---

## 0. 2026-07-28 correction — every sub-daily settlement was charged twice

The panel's funding-age column carries float epsilon (one hour after a
settlement `by_funding_age_h` reads `0.9999999999999999`, not 1.0), so this
program's `settlement_exact_funding` predicate `age < 1.0` marked two bars
per 8h/4h/2h settlement and charged each print **twice**; 1h-interval
symbols were counted once because the next print overwrites the epsilon bar.
Weights, entries, exits, price legs, and turnover costs are unchanged —
decisions read funding *levels* — but every funding P&L leg below is
inflated (~×1.5–2 blended, largest where funding was deepest). Fixed
2026-07-28 in `financed_longs.settlement_exact_funding` and
`lane2_blend.settlement_exact_funding` (age-reset detector; regression tests
on the real age shapes in `tests/test_financed_longs.py` and
`tests/test_lane2_blend.py`). The registrations and their commit dates are
untouched — the scorer is corrected, the receipts stand (M19 precedent).
Discovered from the owner's question about Bybit's shortened funding
intervals; those are real (2025 settlements: 52% 4h, 21% 1h, 7% 2h, ~20%
8h; 2021 was 100% 8h) and 73–80% of carry-hold's 2025-26 held name-days are
on sub-8h names, so the per-print −10/−3 bp thresholds mean different DAILY
carry per symbol. The corrected accounting charges each settlement exactly
once at any cadence. ~~A successor config should normalize to a
daily-equivalent rate~~ — **tested and refuted 2026-07-28**: the
daily-rate-entry variant collapses (Sharpe 0.62; the deep-daily-carry names
its threshold admits are chronic decliners whose shorts are right), while
the per-print gate's acuteness selection is load-bearing. See
`docs/archive/2026-07-28-carry-hold-quant-review.md`.

**Corrected verdicts** (`scripts/research/screen_financed_longs.py` on the
2026-07-28 panel; output `reports/financed_longs_corrected_2026-07-28/`):

| bench window 2023-03-13..2026-07-16, full calendar | Sharpe raw | Sharpe vt | mean bp/d | beats bench? |
| --- | ---: | ---: | ---: | --- |
| **benchmark (CONTINUOUS sl35)** | **1.84** | — | — | — |
| `lane2_carry_hold_v1` | **1.21** | 1.05 | 23.6 | **return only — NOT Sharpe** |
| `lane2_financed_leaders_v1` | **1.44** | 1.11 | 20.5 | **return only — NOT Sharpe** |
| `lane2_financed_leaders_binance_v1` | **1.01** | 0.67 | 13.8 | **return only — NOT Sharpe** |

- **The program goal (beat on return AND Sharpe) is met by none of the
  three.** The §2 headline table and its verdict column are superseded.
- carry-hold corrected attribution: **funding +7.2 units vs price −3.4**
  (2.1:1, not 3.4:1). The mechanism's sign survives; its size does not
  clear the bar.
- Corrected full-sample t: carry-hold **2.31**, financed-leaders **2.58** —
  below the ≈3.4 multiple-testing threshold this note applies. The §2
  Bonferroni claims are withdrawn.
- Corrected carry-hold eras (bp/day, full calendar): 2021 +3.8 · 2022 +3.0 ·
  2023 +26.0 · 2024 +13.7 · 2025 +30.3 · 2026 +32.5. **The "positive
  through the 2022 bear" robustness claim is withdrawn** — the doubled
  prints were concentrated exactly in deep-funding capitulations.
  financed-leaders 2022 is now −4.0 bp/day.
- §3's negative ledger: rows whose verdicts rest on funding magnitude
  (1, 2, 13, 14, 15, 16, 17, 20) are numerically stale pending
  re-derivation; the structural findings (short-side failure, nothing
  intraday, replication discipline) are unaffected in kind. Cross-venue
  replication ratios carried the same defect on both venues — directionally
  intact, numerically stale.

**2026-07-28 addendum (carry-hold quant review).** Re-deriving the §2.1
validation battery on the corrected scorer **kills the carry-hold Binance
replication**: the same construction on Binance's own funding/prices/universe
is +2.7 bp/day, t 0.4, Sharpe 0.18, max DD −85% (registered claim: +25.0
bp/day, t 2.73, Sharpe 1.38, ratio 0.50). The doubled funding leg WAS the
replication. "First positive mechanism to survive cross-venue replication"
is withdrawn; carry-hold is single-venue (Bybit) evidence until shown
otherwise. The same review registered `lane2_carry_hold_v2` (depth-scaled
sizing, same state machine; v1 keeps scoring) — full tables in
`docs/archive/2026-07-28-carry-hold-quant-review.md`.

---

## 1. The benchmark, pinned from primary artifacts

The deployed CONTINUOUS sleeve's honest render was regenerated rather than
quoted: `scripts/research/equity_curves.py --sleeves continuous --start 2023-03-13
--end 2026-07-17`, profile revision `active_tp12_sl35_v1`, output
`~/SHARED_DATA/bybit_full_pit/reports/equity_curves_sl35_2026-07-26`
*(2026-07-28: renders the RETIRED 3-cell shape — no longer a citable
baseline; the dir stays in place only as a frozen input of
`scripts/research/render_continuous_admission_variants.py`. Current baselines:
`reports/equity_curves_2026-07-28/`.)*

> **Benchmark: Sharpe 1.84, total +15.85% (+4.49%/yr), max DD −2.85%, worst
> day −0.70%, window 2023-03-13 → 2026-07-16.**

**Benchmark change point, 2026-07-26 (later the same day):** the deployed
CONTINUOUS shape this benchmark renders was replaced at commit `1fe0e48`
(profile revision `active_single_fund0_tp12_sl35_v1`; operator override,
see `docs/strategy_program.md`). The new deployed shape's same-window render
is **Sharpe 1.45, +11.06%, max DD −1.84%**. Every comparison in this
document was made, and stays valid, against the pre-change 1.84 benchmark;
forward comparisons after the change point grade against the new one.

(The strategy program's documented 1.87 / +15.79% reflects the same render at
a slightly earlier data-root state; the regenerated numbers are used
throughout.)

Comparison basis for every challenger: same window, compounded daily equity,
settlement-exact funding, costs charged as **measured one-way turnover ×
7.78 bp/side** (the measured demo taker fee; `cross_section.MEASURED_ROUND_TRIP_BP`),
entries at the decision bar close (house convention; every surviving book was
also delay-stressed), disjoint 24h decision grid, era splits reported.

## 2. The three registered books

**Superseded 2026-07-28 — the funding leg in every row below is
double-counted; the corrected verdicts are in §0.**

**Full-calendar basis (corrected 2026-07-26, same day as registration).** The
first-pass series counted only days with positions; a strategy's capital is
committed on flat days too, and the benchmark's own Sharpe counts *its* flat
days (CONTINUOUS is in-market 38% of days). Flat days = 0 in the denominator
is therefore the only apples-to-apples basis. The correction leaves t-stats
and compounded returns unchanged and shrinks Sharpe by ≈√(active fraction);
it **changed one verdict** — the Binance arm no longer beats on Sharpe.

| bench window 2023-03-13..2026-07-16, full calendar | Sharpe raw | Sharpe vt15 | return raw | return vt15 | maxDD vt | t | beats bench? |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| **benchmark (CONTINUOUS sl35)** | **1.84** | — | **+15.85%** | — | −2.85% | — | — |
| `lane2_carry_hold_v1` (Bybit) | **2.57** | 2.41 | +21,943% | +342% | 23.7% | 4.69 | **yes, both metrics, both bases** |
| `lane2_financed_leaders_v1` (Bybit) | **2.21** | 1.87 | +3,070% | +264% | 24.1% | 4.04 | **yes, both metrics, both bases** (vt margin thin) |
| `lane2_financed_leaders_binance_v1` | 1.66 | 1.30 | +993% | +165% | 30.4% | 3.03 | **return only — NOT Sharpe** |

Slippage sensitivity: +2 bp/side beyond the measured fee moves the Bybit
books to 2.53 / 2.18 — negligible, because turnover is ~0.35 units/day.
Active-days-only figures (2.76 / 2.87 / 2.12) are retained in the configs as
the per-deployed-capital view, labelled not-comparable-to-benchmark.

**Reproduction parity (2026-07-27).** Until today the full-calendar correction
above lived only in this note: `daily_scores` still iterated the bars present in
`weights`, so the documented reproduction command printed the *active-days-only*
view (2.76 / 2.87 / 2.12) and silently contradicted this table, and a flat
gate-flip day charged neither the exit into it nor the re-entry out of it
(2026-07-27 audit M19). `daily_scores` now iterates every decision bar between
the first and last weighted bar, so the script reproduces this table directly.
Re-run on today's panel: Sharpe raw **2.56 / 2.21 / 1.66**, t **4.69 / 4.04 /
3.03**, n = 1221 days for all three — matching the table within the drift from a
panel that has been refreshed since 2026-07-26. Every verdict in this note is
unchanged; the total turnover actually charged rose 1–3%, which is small here
because gate flips are infrequent.

Reproduce with `scripts/research/screen_financed_longs.py`. The raw compounded totals
assume full reinvestment at book scale and are shown for the accounting, not
as a capacity claim; the vol-targeted (15% ann, 3× cap, leverage-change
turnover charged) rows are the deployable presentation. Equity curves and the
daily series CSV:
`~/SHARED_DATA/bybit_full_pit/reports/financed_longs_2026-07-26/`.

### 2.x Time under water — the comparison the drawdown numbers hide

Same-risk comparison (every series on one full calendar; CONTINUOUS reindexed
with flat days = 0 reproduces its official Sharpe 1.84, which validates the
basis; scaling CONTINUOUS ×6.2 to the challengers' 15% vol is presentation
only — it exceeds the 2× account cap and models no margin):

| bench window, 15% vol basis | Sharpe | total | maxDD | %days UW | longest UW | spells ≥60d |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 50/50 challenger blend (vt15) | 2.41 | +308% | 22.0% | 86% | 204d | 3 |
| blend at half size (vt7.5) | 2.41 | +105% | 11.6% | 86% | 204d | 3 |
| CONTINUOUS ×6.2 (presentation) | 1.84 | +143% | 16.9% | 81% | **218d** | **6** |
| **PROGRAM: 50% CONTINUOUS + 25/25 challengers (executable)** | **2.66** | +120% | **10.8%** | 84% | 203d | **2** |

Three facts worth keeping: (1) long underwater spells are endemic to every
book here **including the deployed benchmark** — CONTINUOUS's longest spell is
215 days (2024-11-28 → 2025-07-01) at its native scale; sizing controls
drawdown *depth*, never *duration*. (2) At equal risk the challengers have
*fewer* long spells than the benchmark (3 vs 6 ≥60d). (3) The challengers
correlate **−0.08 with CONTINUOUS**, so the executable 50/25/25 program has a
higher Sharpe than any component (2.66), maxDD 10.8%, and only 2 spells ≥60d
— diversification across the sleeves, not replacement, is what shortens the
stomach-ache. Chart: `financed_longs_same_risk.png` in the reports dir.

**The drawdown trade-off is not hidden**: the benchmark's −2.85% max DD
reflects a book whose realized gross exposure is ~1.5% of nominal
(anomaly research §16.3). The challengers run 0.3–1.0 gross and carry
proportionally larger drawdowns. The owner's stated criteria were return and
Sharpe; on MAR the benchmark (1.66) is beaten by financed_leaders-vt
(180/5.5 ≈ 32 over the window) as well.

### 2.1 Carry-hold — the capitulation phase

Enter LONG a top-100 name when its settled funding prints below −10 bp/8h;
hold while it stays below −3 bp; 0.10 per-name cap, 1.0 gross cap. ~3.8 names
on active days, one-way turnover 0.35 units/day (≈2.7 bp/day of cost).

- **Mechanism, with a named counterparty**: crowded shorts pay longs to hold
  the other side. Cumulative attribution 2021-2026: **funding +13.06 units of
  capital, price −3.86** — the book earns collected funding net of price
  bleed, ratio 3.4:1. It is a carry payment, not a price anomaly.
- Eras (bp/day): 2021 +25.4 · 2022 **+19.1** · 2023 +54.3 · 2024 +32.9 ·
  2025 +77.6 · 2026 +71.5. Positive in the 2022 bear because deployment is
  rare in grind-down regimes — deep-negative funding is itself the signal
  that the compensation is on.
- **Validation**: Binance replication ratio 0.50 (inside the 2A band; the
  program's first-ever positive cross-venue survivor); entry delay +1h t 4.19,
  +4h t 4.82; placebo (same days, same gross, basket instead of names)
  Sharpe 0.72 vs 2.76 → the return is name selection; parameter plateaus
  everywhere (thresholds {5/2..20/5}, caps, top-N {50,100,300} all
  Sharpe 1.9–2.6); worst-day forensics identify real events (LUNA week),
  handled by the gross cap diluting cascades instead of levering into them.

### 2.2 Financed leaders — the squeeze phase

The top 1-week-momentum decile of the top-100, admitted only while the name's
own funding is ≤ 0 (shorts finance the move) and BTC's prior-30d return is
above −0.05 (the house 2B gate at the §18.6 t-chosen threshold). ~3 names on
active days, active ~47% of days.

- **The financing condition is the alpha**: unfiltered gated momentum longs
  earn bench Sharpe 1.57 and bleed −61 bp/day in 2022; funding ≤ +1 bp gives
  1.79; funding ≤ 0 gives **2.87** with 2022 flat (−3.9). Monotone along the
  economic axis — not a fitted spike. Rationale from an independent measured
  fact: crowded-long leaders (funding > 0) keep rallying but carry
  uncompensated crash risk (the D3a quadrant lost −29 bp/day when shorted
  *and* crashed hardest when held).
- **Validation**: Binance-native replication ratio 0.65, bench 2.12/2.21
  (registered as the third config); delay +1h → 2.65, +4h → **3.08**; a full
  24h delay decays to 1.17, so the live implementation must trade the
  day-of leader list — disclosed in the config.

### 2.3 The Binance arm — replication as a first-class object

`lane2_financed_leaders_binance_v1` is the same rule on Binance's own
turnover ranks, funding stream, and BTC gate. It beats the benchmark on both
criteria (2.12 raw / +993%) and its role is explicitly evidentiary: it is the
replication arm that makes financed-leaders the first mechanism family here
to survive 2A, registered so the forward record grades both arms
symmetrically. **corr(+0.82) with the Bybit arm** — a second-venue expression
of the same premium, not portfolio breadth. No Binance execution exists in
this repository; the config inherits the Bybit fee measurement
(conservative vs Binance's taker schedule) and accrues research evidence only.

### 2.4 What the three are, honestly

One macro-premium, two phases, two venues. **The market pays longs while the
short side is paying funding** — during capitulation (carry-hold) and during
financed rallies (financed-leaders). corr(carry-hold, financed-leaders) =
+0.75 with 41% name-day overlap; corr(financed-leaders, Binance arm) = +0.82.
These are three deployable books and three registrations, not three
independent premia, and any portfolio construction over them must use those
correlations, not assume diversification.

Multiple-testing position: this program tested ~18 new mechanism families
(~45 constructions) on top of the repository's ~45 prior mechanisms;
Bonferroni at α=0.05 is ≈ t 3.4. On the full-calendar basis (these three
full-sample t-values were the last figures still quoted on the active-days-only
basis; corrected 2026-07-27 with the M19 turnover fix) carry-hold (t 4.87) and
financed-leaders (t 4.01) clear it; the Binance arm (t 2.77) is a replication,
not an independent discovery, and is not claimed against the threshold. Adding
the flat days moves these t-values by <0.04 — the day count and the standard
error grow together — so no significance verdict changes; what the full-calendar
basis materially changes is Sharpe and mean bp/day, as §2 records.

## 3. Everything that died today (the no-retread ledger)

Tested identically (measured turnover × 7.78 bp/side, settlement-exact
funding, disjoint sampling, era splits). Sub-bar means real but below the
benchmark's Sharpe 1.84 on the bench window.

| # | Mechanism | Verdict | Key number |
| --- | --- | --- | --- |
| 1 | Cross-sectional carry L/S (validation of house cell) | reproduced | +29.1 bp/d t 2.43 |
| 2 | Carry short leg (short high-funding names) | **dead** | −7.5 bp/d, tail carrier |
| 3 | Crash reversion + OI purge (external prior) | **conditioning fails** | purge does not discriminate; mid/no-purge revert equally (t 4.2 vs 2.8); purge cells unstable 2022/2026 |
| 4 | Crash-reversal portfolio (B2, all variants) | sub-bar | best bench Sharpe 0.98 (panic-only) |
| 5 | Per-coin TS momentum ensemble (4 horizons, eq-risk) | **dead** | net −0.3 bp/d |
| 6 | BTC→alt lead-lag (TS, 24h and 72h) | **dead, inverted** | −25.4 bp/d — alts mean-revert vs BTC's prior move |
| 7 | Beta catch-up gap (CS, long laggards) | **dead both directions** | −18.3 bp/d net |
| 8 | OI-flow crowding (7d/1d OI growth, 4 shapes) | **dead/inverted** | crowded longs keep going (short them: −28.8 bp/d) |
| 9 | Funding-stamp clock (pre/post-settlement windows) | **artifact caught** | the "pre-stamp drift" conditioned on the concurrent rate (look-ahead); PIT version negative; all intraday windows < costs |
| 10 | Volume-impact fade/follow (participation z) | flat | fade 0.15, follow 0.18; fade-all control −23 bp/d |
| 11 | MAX/lottery weekly (short high-MAX) | **dead, inverted** | −109 bp/week — squeeze furnace |
| 12 | Majors 3d dip-buy | **dead** | −4 to +5 bp/d |
| 13 | Cross-venue funding-differential pair (hysteresis) | **dead** | signal flaps (47k flips); churn −35 bp/d |
| 14 | Post-squeeze drift (long after funding normalizes) | **dead** | the payment stops when the crowding stops (Sh 0.41) |
| 15 | Aggregate-funding regime basket (euphoria short / capitulation long) | sub-bar standalone | states pay (+24-31 bp/d short-euphoria) but rare; Sh 0.44 |
| 16 | Crash+negFunding absorption (H1, all holds/exits/gates) | sub-bar, Bybit-local | best bench 1.58; 2A ratio 0.28-0.53; carry-exit variant −40 bp/d in 2026 |
| 17 | Gated momentum long without the financing condition | sub-bar | bench 1.57, −61 bp/d in 2022 |
| 18 | Bear-regime mirror (short crowded leaders, gate off) | **dead** | −4.6 bp/d |
| 19 | Weekend basket long | **dead** | t 0.18 |
| 20 | Cross-venue discount accumulation (TS premium_diff long) | sub-bar lead | bench vt 1.21 at the robust setting; corr 0.45 to carry-hold — best independence, kept as a lead |
| 21 | Cross-venue convergence pair on premium_diff | **dead** | per-name signal flaps at hourly scale (534k flips); −417 bp/d |
| 22 | Long-side composites of sub-bar parts (H1+H2g±de-gross) | sub-bar | best honest composite 1.80 — 0.04 short; weights not tuned to cross |

Structural findings worth more than the individual results:

1. **Every short-side construction fails** — carry shorts, MAX shorts,
   crowded-leader shorts (both regimes), post-stamp shorts, basket shorts.
   In this market the marginal desperate flow is short-side; the payment is
   always on the long side of it.
2. **Nothing intraday survives 15.56 bp round trips.** The only "clock"
   effect found was a conditioning artifact, caught by the prior-stamp PIT
   re-run.
3. **The market pays exactly one deep premium** (funding-financed long-side
   liquidity provision), harvestable in multiple phases; everything else on
   this panel is either dead, inverted, or sub-bar.
4. **The 2A replication escape works when the mechanism is real**: after the
   entire prior program produced zero cross-venue survivors, both books here
   replicate (ratios 0.50 / 0.65).

## 4. What this does not show

- Nothing here is a forward result. All numbers are Lane-1 on data that
  selected the rules; the forward record after the registration commits is
  the evidence, per `docs/governance.md`.
- Costs are the measured demo taker fee at small notional with no impact,
  partial-fill, or capacity term. Both books hold ~3-4 names; capacity is the
  deep-negative-funding / financed-leader subset of the top-100, adequate for
  the current risk envelope, not for a larger book.
- The 2026 eras of both books ride the structural funding inversion
  (`docs/research_findings.md` §4). A normalization of
  funding shrinks deployment frequency and edge together — the books go to
  cash rather than short, but the forward record would flatten.
- The deployed CONTINUOUS sleeve is not modified, demoted, or replaced by
  this work. Promotion of any of these configs requires its own forward
  record and the five-line note, and real money remains a separate owner
  door.
- Live-runtime parity (order lifecycle, venue stops, partial fills) is not
  modeled; these are panel books. An implementation would go through the
  normal account-kernel path with its own execution evidence.

## 5. Artifacts

- Registered configs: `configs/lane2_carry_hold_v1.json`,
  `configs/lane2_financed_leaders_v1.json`,
  `configs/lane2_financed_leaders_binance_v1.json` (the commit is the
  registration).
- Executable: `liquidity_migration/research/backtest/financed_longs.py`; reproduction:
  `scripts/research/screen_financed_longs.py`; tests: `tests/test_financed_longs.py`.
- Benchmark render: `~/SHARED_DATA/bybit_full_pit/reports/equity_curves_sl35_2026-07-26/`
  *(2026-07-28: superseded as a baseline — retained in place only as a
  frozen input of the registered admission-variant scorer)*.
- Panel: `~/SHARED_DATA/cross_venue_panel_v1` (six shards, manifest per shard,
  panel commit `a9ac75d`).
- External sources consulted (mechanism priors, not parameters): Robot Wealth
  carry/stat-arb notes, SSRN 6579278 (Oct-2025 cascade autopsy), SSRN 5576424
  (funding predictability), MDPI two-tier funding-market study.
