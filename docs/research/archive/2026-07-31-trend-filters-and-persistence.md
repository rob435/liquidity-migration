# Trend filters, the four doors, and crowding persistence — 2026-07-31

One day, three questions, in the order they were asked. The first two failed. The
third produced `lane2_carry_hold_v4` and the strongest carry result the program
has. Lane-1 on already-seen data throughout: this *selected* everything below and
therefore grades none of it (`docs/governance.md`).

## 1. Does excluding coins in a downtrend improve the books? — no

Asked family-wide, not only of carry. `scripts/research/screen_trend_filters.py`
and `..._registered.py` (both removed after the run at the owner's direction;
recoverable from this commit's history) tested 38 filters — trailing 3/7/14/30d
return, price versus its own moving average, drop from a 30-day high, and each
of those minus Bitcoin's move — across three testbeds and 2,085 cells.

**What was true.** It is a genuine name screen, not disguised market timing: every
filter was run against a *size-matched random* arm that drops the same number of
names per bar, and the screen beat it. The designated dead control
(`premium_diff`) was left slightly *worse*, on both venues, which is what a
market-timing overlay would not do. The effect is long-side only — positive for
every long leg, negative for every short leg, both venues, both mechanisms. And
it is not a repackaged momentum tilt: regressed on both unfiltered momentum
books, the carry books' betas are ≈0 and the alpha survives.

**Why it does not matter.** On both registered Lane-2 books the screen is
worthless. Same signature on each: keeping *only* the falling names hurts in
35/35 and 38/38 filters, so the measures are right about which name is bad; the
screen beats a random screen of identical attrition in 34/38 and 31/38, so it
ranks correctly; and actually dropping those names is positive in only 3/38 and
15/38, best t 1.30 and 1.28.

**The reason is the scope statement, and it is the deliverable.** Both registered
books already exclude falling names at entry — carry-hold through its deep-print
gate, financed-leaders through a top-momentum-decile rule — and both hold three
or four names, so every further removal costs a position and buys nothing. A
downtrend screen pays where entry is unselective and stops paying once entry
already excludes falling names. That is why it is worth up to +53 bp/day on a
top-100 decile book that admits everything.

Also recorded: nothing cleared even the pre-2026-07-31 bar (best t 3.07 in 2,085
cells), the carry cells failed cross-venue replication (ratios 0.13–0.42 against
a [0.5, 2.0] kill band), and the base decile books are EV-noise on the corrected
scorer (carry +1.08 bp/day, t 0.10).

## 2. The four untested loser-identification doors — two closed, one taken

From `docs/research_findings.md` §5's own ranked list. Diagnostics are on
carry-hold's 5,660 held name-days, mean +69.6 bp/name-day.

**Door 1, cross-venue funding confirmation — closed.** The repo ranked this
first. Name-days where Binance funding is also deep earn +59.6 bp/nd against
+16.0 where Binance is ≥ 0, so the "consensus vs venue-local" direction is real.
But the venue-local cohort is 6.4% of the book and blocking it is worth
−0.10 bp/day (t −0.09). Requiring *sustained* Binance crowding is actively
harmful: −5.70 and −6.18 bp/day (t −2.9, −3.0). It removes the acute Bybit-local
liquidation events, which is where the premium is.

**Door 3, toxic-band high boundary to 0 — taken, below the bar.** The [−5%, 0)
cohort earns −34.4 bp/name-day on 10% of held name-days and v3 keeps it. Moving
the edge is a plateau (hi ∈ {−10, −5, −2, 0} within 0.8 bp/day of each other,
breaking at +2%) and zero is the less-fitted boundary. On its own: **+0.76 bp/day,
t 1.12 — below the bar**, included in v4 at the owner's direction and flagged in
the config.

**Door 4, turnover-rank decay — closed, and the first measurement was broken.**
Raw rank *number* drifts upward for every symbol as the panel grows from 84 to
552 listings, so a 7-day change in it counts new listings rather than decay.
Repaired with percentile rank: names slipping 5–15pp in a week earn −53.1 bp/nd,
but that is 8% of the book, and as a filter it is worse than v3's existing band.

**Door 2, suspend → hard exit — still open.** Not reachable without an
identity-shifting engine change: v3/v4 suspend a hold to zero weight inside the
band rather than ending the state.

## 3. Crowding persistence — the result, and it only works as a size

**The feature.** The share of a symbol's last 20 **settlements** that printed
deeper than the 10 bp entry threshold. Counted in the symbol's own settlement
sequence, never on a clock.

**Two measurements were thrown away before this one.** "Hours since the last deep
print" and "count of deep prints in 30 days" are both confounded: a 1h-interval
symbol settles eight times more often than an 8h one, and Bybit's mix went 100%
8h in 2021 to 52% 4h / 21% 1h in 2025, so the confound has an era gradient on
top. Both clock versions report cadence, not crowding.

**The interval-neutral version inverts the intuition.** The isolated deep print
is the loser:

| persistence | share of book | bp/name-day |
| --- | ---: | ---: |
| ≤10% (isolated) | 32.5% | **−16.7** |
| 10–30% | 28.8% | +99.2 |
| 30–50% | 16.3% | **+135.4** |
| 50–75% | 13.2% | +114.9 |
| >75% | 9.2% | +101.0 |

No contradiction with the program's established chronic-vs-acute finding: that
one is about many *shallow* prints summing up, this is about many *deep* ones.
One deep print is often a thin hour or a single liquidation that resolves; a name
printing deep repeatedly has a stuck crowd of shorts.

**As a filter it is a wash** — paired differential vs v3 −0.69, t −0.39. Same
wall as §1: on a 3-name book, removing candidates costs more than the losers
save.

**As a size multiplied with v2's depth ladder it is the result.** All 16 shape
cells positive, t +1.87 to +2.77, at the control's capital:

| sizing rule | gross held | bp/day | Sharpe | maxDD | MAR | turnover | vs v3 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| v3 | 0.198 | +19.83 | 1.38 | 28.7% | 2.52 | 0.156 | — |
| depth × step@10% → 0.25 | 0.164 | +24.34 | 1.51 | 28.7% | **3.10** | 0.155 | +4.52 (t 2.53) |
| depth × ramp/0.20 | 0.161 | +24.52 | 1.53 | 29.9% | 3.00 | 0.154 | +4.70 (**t 2.77**) |
| depth × step@10% → 0.00 | 0.152 | +26.34 | **1.54** | 31.3% | 3.07 | 0.156 | +6.51 (t 2.54) |

Turnover is unchanged — unlike every filter in §1 and §2, this does not work by
trading less. Replacing the depth ladder rather than multiplying it gives +0.16
to +1.89, all t < 1: the composition is the result, because depth and persistence
answer different questions.

**Placebos.** Sizing *up* the isolated prints: −14.44 bp/day (t −2.73). Handing
the identical distribution of position sizes to the wrong names: **−15.26
(t −2.71)** — the load-bearing control, since it holds size distribution and
gross constant, so the gain is about *which* names get the money. Null
persistence fails open and never fires on this book (0 of 3,314 held name-days),
so it is not a listing-age screen; the fails-open variant is identical.

## 4. What was registered, and the honest shape of it

`configs/lane2_carry_hold_v4.json` — the band edge and the persistence size
together. The config carries the full statement; the two things that must not be
lost:

- **At its own capital v4 is not a return improvement**: +1.07 bp/day, t 0.47.
  It does the same work on ~30% less capital and 24% less turnover.
- **The capital-normalised differential is +10.76 bp/day, t 3.23** — and running
  v4 at v3's leverage makes its worst dip *worse* (33.5% vs 28.7%). The return
  gain and the drawdown gain are not available at the same time.

Sharpe 1.41 → 1.64 is scale-free and true either way. MAR is not: 3.08 → 4.14 at
v4's own capital, 3.08 → 4.67 at v3's. All MARs here are compounded annualised
return ÷ max drawdown, the convention v3 registered and the standard chart
renders — simple annualisation of the daily mean gives 2.69 / 3.31 / 3.47 for the
same three rows and the two must not be mixed.

**The curve.** Rendered through the standard chart
(`reports/equity_curves/research/lane2_carry_hold_v4/`, window 2021-10-05 →
2026-07-26, native raw-book size, no presentation leverage): **76.2% of the log
growth is 2025-26.** The book sits at 2.21x on 2025-01-01 after three years and
finishes at 28.83x. By year: 2021 −0.6%, 2022 +23.5%, 2023 +48.7%, 2024 +24.1%,
2025 +260.5%, 2026 +266.1% (207 days). v3 is the same shape — 76.7% post-2025,
2.13x on 2025-01-01 — so this is the mechanism's regime dependence and not
something v4 introduced. Any forward expectation taken from the headline
+2,783% is an expectation about the 2025-26 crowd-fee regime persisting.

## 5. Two corrections found along the way

- **`lane2_carry_hold_v1` does not reproduce its own registered figures.** The
  module scores it at 17.38 bp/day / Sharpe 0.977 / turnover 0.273 against the
  registered 18.0 / 1.02 / 0.271. Verified as **pre-existing** — byte-identical
  before and after the v4 change — and consistent with the module-path versus
  review-script difference already recorded in `research_findings.md` §4. v2 and
  v3 reproduce exactly. Not repaired here; recorded so it is not rediscovered.
- **Vol-targeted drawdown ranked the persistence variants in the opposite order
  to the raw basis** during the review. The repo already treats raw as primary;
  this is a concrete instance of why, and the v4 config says so.
