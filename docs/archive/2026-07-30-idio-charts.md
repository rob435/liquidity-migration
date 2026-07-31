# Idiosyncratic charts — 2026-07-30

Lane-1 investigation of an outside claim: that chart signals which work on raw
price series work better on *idiosyncratic* ones — synthetic paths cumulated
from factor-model residual returns. Everything here is **seen data** —
selection, not evidence.

The claim, verbatim:

> "There's a whole shitton of useful stuff in idio charts, which is why the
> smart boys run their signals off em. If you want to have high sharpe signals
> you take the shit that works on time series charts and make idio ones."

## 0. Status — COMPLETE. The programme is closed.

| Section | State |
| --- | --- |
| §1 mechanism, §2 construction | complete |
| §3 theses | declared before any result was computed; graded in §6 |
| §4 diagnostics, §5 grid | complete |
| §6 verdicts | complete — **declared kill condition fired** |
| §6.4a mode (b) hedged book | run; hedging improves 3/24, median Δ −0.183 |
| §6.9 directional single-name book | run; median Δ −0.572, 0/24 clear the bar |

**Bottom line.** Chart signals on idiosyncratic paths do not beat the same
signals on an information-matched raw chart, in either construction this
repository can trade, at the measured cost basis. Zero of 96 pre-declared cells
are profitable and significant. The claim is not refuted in general — the
untested territory is named in §6.4 — but it is closed here.

## 1. What an idio chart is, stated abstractly

A factor model splits each day's return into a part explained by common
exposures (market beta, size/liquidity, momentum, volatility regime) and a
residual. Cumulating the residual gives a price path for the part of the asset
that did not move with everything else.

The claim's logic is that a chart pattern — a breakout, a range position, a
trend — is a statement about supply and demand *in that name*. On a raw chart
that statement is buried under common-factor motion, which in crypto perps is
large and shared. Strip the common part and the pattern is measured against a
cleaner background, so the same rule should carry more signal per unit of
noise.

The counter-argument, which this repository's own book makes structurally: a
**cross-sectional decile long/short already differences out common exposure**.
The long leg and the short leg share the market factor, so it largely cancels
in the spread. Residualising *before* ranking may therefore be substantially
redundant in exactly the book this repo trades. The claim should bite hardest
on *directional, single-name* signals — which are not tradable here without
holding the factor hedge.

That tension is what §3 tries to settle.

## 2. Construction, and what forced each choice

`liquidity_migration/residual_price.py` cumulates
`risk_model.fit_factor_returns` residuals into per-symbol paths.
`liquidity_migration/idio_features.py` computes chart features from any single
price column. Four decisions were forced rather than chosen:

**Log space.** `daily_feature_panel` produces *simple* returns, and `cumsum` on
simple returns is wrong precisely in the tails. `add_log_forward_return` takes
`log1p(fwd_ret_1d)`, which is exact — `fwd_ret_1d` is a simple return over the
same two closes — so the regression is fit on genuine log returns and the path
compounds correctly. The correction is not cosmetic: at ±50% daily residuals
the two conventions diverge by more than 2×, and `log1p(r) < r` always, so
reading a series as simple returns always yields a path at or below the log
reading.

**Calendar re-indexing, not a positional shift.** `residual_return[d]` is fit
to `first_bar_close(d+2)/first_bar_close(d+1) − 1`, so the move *completes* at
01:00 UTC on `d+2` and is first readable at a 00:00 UTC decision boundary on
`d+3`. Cumulating residuals against their own `ts_ms` back-dates every move by
two days and manufactures a look-ahead any breakout signal will monetise. The
shift is applied as an explicit `+3 days` calendar offset so a data gap cannot
stretch it.

**Point-in-time row existence.** An earlier draft spanned each symbol's
`[first, last]` residual day. That keeps every *value* causal while making row
*existence* depend on the future: a symbol dark at the read day gains rows for
the dark period only because it relisted afterwards, so any signal whose
universe is "symbols with a row today" selects on future listing status. A row
now exists at `t` only if a real residual falls in `[t − max_stale_days, t]`.

**Close-only features.** An idio path is cumulated from daily *returns*: it has
a close and no open, high, or low. `close_location_1d` and
`range_extension_30d` therefore have **no idio analogue** without residualising
intraday, and are excluded rather than approximated. More importantly, the
existing `dist_from_30d_high` reads the daily `high`, so the raw arm is
recomputed through the same close-only code — otherwise the comparison would
partly measure high-versus-close.

### 2.1 The control that decides the experiment

The idio path is structurally **three days stale**. Scoring it against a raw
chart that sees today's close measures information age, not idiosyncrasy. The
grid therefore carries four price sources:

| source | information available |
| --- | --- |
| `raw` | raw close through `D` |
| `rawlag` | raw close through `D−3` — **the information-matched control** |
| `idio` | cumulated residual path (residuals through `D−3`) |
| `iz` | volatility-standardised residual path |

and two forward targets, because "higher Sharpe" is ambiguous about what is
harvestable:

| target | meaning |
| --- | --- |
| `fwd_ret_1d` | raw executable return — mode (a), no hedge held |
| `resid_fwd` | residual return — mode (b), requires holding the factor hedge, **cost not modelled** |

A signal that wins on `resid_fwd` and loses on `fwd_ret_1d` has found something
real and untradeable at this cost basis.

**Primary read: `idio` vs `rawlag` on `fwd_ret_1d`, same feature.**

### 2.2 Multiple testing

The grid is pre-declared in code: `idio_features.CHART_FEATURES` (6) ×
`build_idio_panel.PRICE_SOURCES` (4) × targets (2) = **48 cells**. Added to the
programme's 44 prior mechanisms, the Bonferroni family is 92 and the threshold
is **|t| > 3.46** (normal approximation; the same approximation reproduces the
programme's standing 3.25 for 44). Every cell is reported, including failures.
Direction is an *output* — each cell is scored once at `sign=+1` and the signed
t reported — so scoring both signs cannot silently double the family.

## 3. Theses — declared before any result was computed

Written and committed before the panel finished building. Each is falsifiable
and each has a number attached.

| # | Thesis | Prediction | Verdict |
| --- | --- | --- | --- |
| T1 | The factor model leaves most variance in the residual | `1 − R²` > 0.6, i.e. COMMON4 explains < 40% of daily cross-sectional variance | *pending* |
| T2 | Idio and raw chart signals are highly rank-correlated within day | mean Spearman ρ > 0.7 on momentum features; decile overlap > 0.5 | *pending* |
| T3 | **Idio does NOT beat the information-matched control on tradable returns** in a cross-sectional decile book, because the decile spread already differences out common exposure | ≤ 3/6 features favour `idio` over `rawlag` on `fwd_ret_1d`; no cell clears \|t\| > 3.46 after cost | *pending* |
| T4 | Idio looks better on `resid_fwd` than on `fwd_ret_1d` — near-tautologically, since the signal is built from those residuals | visible gap; to be read as a warning, not a win | *pending* |
| T5 | Three-day staleness costs more than idiosyncrasy gains | `raw` > `rawlag` ≳ `idio` on most features | *pending* |
| T6 | Volatility standardisation helps cross-sectional comparability | `iz` ≥ `idio` on most features | *pending* |

T3 is the thesis that matters. If it is **refuted** — if idio genuinely beats
the information-matched control on tradable returns — the quant's claim
survives its least favourable test and deserves a Lane-2 config. If it is
confirmed, the honest conclusion is that the claim is about *directional*
signals and does not transfer to this book without paying for the hedge.

## 4. Diagnostics

Panel: Bybit full-PIT, 2023-06-01..2026-06-30, 1,126 days, 880 symbols,
485,854 rows; screened to the top 150 by daily turnover. COMMON4 factor set.

### 4.1 The factor model explains almost nothing — T1 confirmed far past prediction

| statistic | daily cross-sectional R² |
| --- | --- |
| mean | **0.060** |
| median | **0.041** |
| p10 / p90 | 0.004 / 0.142 |
| share of days R² < 0.10 | **80.8%** |
| share of days R² > 0.50 | **0.0%** |

T1 predicted R² < 0.40. The measured value is ~0.06, so the residual keeps
**~94% of daily cross-sectional dispersion**.

Read this carefully, because the obvious reading is wrong. Cross-sectional
variance on a single day already excludes the market's common move — the
regression intercept absorbs it. So this is not "the market factor explains 6%
of returns". It is: *after removing the day's average move, differences in
beta, 30-day momentum, volatility rank and liquidity rank explain only 6% of
why names diverge from each other*. The remaining 94% is name-specific.

That number bounds the whole programme. The daily idio return is nearly the
daily demeaned raw return. What separates the two *paths* is the cumulated
explained component, which is persistent where the residual is noisy — so the
divergence grows with the window even though the daily R² is tiny.

### 4.2 Signal agreement — the arms can differ, but only on some features

Within-day Spearman ρ and long-decile overlap, `rawlag` vs `idio`:

| feature | mean ρ | decile overlap |
| --- | --- | --- |
| ma_ratio_7_30 | 0.925 | 0.865 |
| ret_30d | 0.901 | 0.822 |
| vol_of_vol_30d | 0.796 | 0.801 |
| ret_7d | 0.753 | 0.698 |
| range_pos_30d | 0.711 | **0.398** |
| dist_from_30d_high | 0.706 | **0.396** |

T2 predicted ρ > 0.7 (holds for all six) and decile overlap > 0.5 (fails for
the two rolling-extreme features). **T2 is half-confirmed, and the half that
fails is the interesting one.**

The structural finding: **residualising changes where a name sits in its range
far more than it changes its momentum ranking.** Momentum arms hold 70–87% the
same names; the extreme-based arms share only ~40%. If idio charts are going to
add anything, it is through range position and distance-from-high, not through
momentum — and that is a testable, mechanism-level statement rather than a
Sharpe comparison.

One control check worth noting: `raw` vs `rawlag` on `ret_7d` correlates at only
0.490. A three-day lag on a seven-day window replaces 3/7 of the window, so the
information-matched control is doing real work rather than being a formality.

### 4.3 An artifact found and killed — the log-return target

The first screen showed `vol_of_vol_30d` predicting the residual target at
**Sharpe 4.46, t 7.83**, which would have been the headline result. It is not
real. Scored against the *raw log return* with no residualisation whatsoever:

| source | vs simple return | vs **log** return | vs log residual |
| --- | --- | --- | --- |
| raw | 20.5 bp, Sharpe 0.79 | **110.8 bp, Sharpe 4.38** | 86.3 bp, Sharpe 4.46 |
| rawlag | 19.7 bp, Sharpe 0.80 | 88.4 bp, Sharpe 3.71 | 64.0 bp, Sharpe 3.44 |
| idio | 13.3 bp, Sharpe 0.57 | 80.2 bp, Sharpe 3.60 | 59.9 bp, Sharpe 3.22 |

The effect is fully present before any residualisation and vanishes against the
simple return. It is **volatility drag**: `E[log(1+r)] ≈ E[r] − Var(r)/2`, worth
a measured **−34.76 bp/day** on this universe and strongly cross-sectional, so
any volatility-correlated signal scores a large spurious spread against a log
target.

The design error was mine. Log returns are required for *fitting* and for
*cumulating the path* — a `cumsum` of simple returns does not compound. They are
wrong as a *P&L target*, because a trader earns the arithmetic return. The panel
now carries both: `resid_fwd_log` for path construction and `resid_fwd` (residual
of the simple return) as the mode-(b) P&L target.

Generalisation worth keeping: **any strategy scored on a log-return target is
paying itself a variance-drag premium for shorting volatile names.**

## 5. Declared grid — all cells

Full 48-cell table: `data/idio_panel/bybit/cells_common4.parquet`. Costs are
charged on **measured turnover** (0.081–0.284 one-way per day), not on the flat
per-period round trip — see §5.1. Direction is an output: a decile spread is
symmetric, so `dir = −1` means the profitable book is the reverse of the
as-scored one, and cost is paid either way.

### 5.1 The cost convention was doing most of the killing

`cross_section.summary` charges its `cost_bp` once per period and its own
docstring says this assumes the book fully rebalances each period. Measured
turnover on these books is **0.081 to 0.284**, so the flat charge overstated
cost by 3.5× to 12× and drove essentially every cell negative. The first pass of
this screen reported "27/48 cells clearing |t| > 3.46" — that was an artifact of
reporting the significance of *losing* series, and is withdrawn.

### 5.2 Tradable target (`fwd_ret_1d`), turnover-adjusted, best direction

Net bp/day, t, and Sharpe. Every feature except `vol_of_vol_30d` is a
**reversal**: names near their 30-day high underperform.

| feature | raw | rawlag | idio | iz |
| --- | --- | --- | --- | --- |
| dist_from_30d_high | 22.6 / 1.52 / 0.87 | **26.9 / 1.90 / 1.08** | 22.3 / 1.73 / 1.00 | 15.5 / 1.19 / 0.69 |
| range_pos_30d | 23.4 / 1.80 / 1.03 | 15.8 / 1.29 / 0.74 | 4.3 / 0.38 / 0.22 | 8.2 / 0.69 / 0.40 |
| ret_30d | 16.2 / 1.12 / 0.64 | 22.8 / 1.54 / 0.88 | 19.0 / 1.27 / 0.73 | 9.9 / 0.75 / 0.43 |
| ma_ratio_7_30 | 20.9 / 1.33 / 0.76 | 16.2 / 1.10 / 0.62 | 22.6 / 1.51 / 0.87 | 2.2 / 0.16 / 0.09 |
| vol_of_vol_30d | 18.0 / 1.21 / 0.69 | 16.9 / 1.21 / 0.69 | 10.3 / 0.77 / 0.44 | 7.1 / 0.60 / 0.35 |
| ret_7d | 9.3 / 0.55 / 0.31 | 2.9 / 0.19 / 0.11 | 3.2 / 0.21 / 0.12 | −5.5 / −0.40 / −0.23 |

**Cells profitable in their best direction AND clearing t > 3.46: 0 / 48.**
Highest t in the entire grid is 1.90.

### 5.3 Primary read — idio vs the information-matched control

Turnover-adjusted Sharpe on `fwd_ret_1d`:

| feature | rawlag | idio | idio − rawlag |
| --- | --- | --- | --- |
| ma_ratio_7_30 | 0.625 | 0.872 | **+0.247** |
| ret_7d | 0.110 | 0.124 | +0.014 |
| dist_from_30d_high | 1.082 | 1.000 | −0.083 |
| ret_30d | 0.877 | 0.734 | −0.143 |
| vol_of_vol_30d | 0.686 | 0.444 | −0.242 |
| range_pos_30d | 0.737 | 0.218 | **−0.519** |

**idio beats the information-matched control in 2 of 6 features.**

### 5.4 Era split

Idio wins 2/6 in the first half and 4/6 in the second; which features flip is
not stable. **`ma_ratio_7_30` is the only feature favouring idio in both eras**
(0.942 vs 0.908; 0.870 vs 0.359). With six features, one surviving a two-era
consistency check is roughly what chance produces (~1.5 expected), and its
full-sample t is 1.51. It is a candidate to remember, not a result.

### 5.5 The decisive decomposition — a demean-only control arm

The grid above compares idio against raw. That conflates two separate
operations: **de-marketing** (removing the day's common move) and **factor
stripping** (removing beta/momentum/vol/liquidity exposure on top). A third arm
separates them: the raw log return minus that day's cross-sectional mean over
the *same* regression cross-section, cumulated through the identical `+3d`
calendar re-index and run through the identical feature code.

`corr(demean_path_return, residual_return) = 0.971`.

Net-of-measured-turnover Sharpe on `fwd_ret_1d`, top-150, cut 0.10:

| feature | rawlag | demean | idio | idio − demean | demean − rawlag |
| --- | --- | --- | --- | --- | --- |
| ret_7d | 0.110 | 0.025 | 0.124 | +0.099 | −0.085 |
| ret_30d | 0.877 | 0.640 | 0.734 | +0.095 | −0.238 |
| dist_from_30d_high | 1.082 | 1.103 | 1.000 | −0.103 | +0.021 |
| range_pos_30d | 0.737 | 0.823 | 0.218 | **−0.605** | +0.086 |
| ma_ratio_7_30 | 0.625 | 0.769 | 0.872 | +0.102 | +0.145 |
| vol_of_vol_30d | 0.686 | 0.373 | 0.444 | +0.071 | −0.313 |

| step | median | mean | wins |
| --- | --- | --- | --- |
| de-marketing (`demean − rawlag`) | **−0.032** | −0.064 | 3/6 |
| factor stripping (`idio − demean`) | +0.083 | **−0.057** | 4/6 |

**Both steps are empty.** De-marketing a chart before ranking it
cross-sectionally buys nothing — which is what it should do, because a decile
long/short is already market-neutral by construction, so the de-marketing has
been done for free at the book level. Stripping the four factors on top of that
buys nothing either: positive median, negative mean, and a −0.605 worst cell.

This is a stronger statement than the raw-vs-idio comparison alone, because it
identifies *where* the claim fails rather than only that it fails.

## 6. Verdicts

| # | Thesis | Verdict | Key numbers |
| --- | --- | --- | --- |
| T1 | Factor model leaves most variance in the residual | **CONFIRMED, far past prediction** | R² mean 0.060 / median 0.041; predicted < 0.40. Residual keeps ~94% of cross-sectional dispersion. |
| T2 | Idio and raw signals are highly rank-correlated | **HALF-CONFIRMED** | ρ > 0.7 on all six ✓. Decile overlap > 0.5 fails on the two rolling-extreme features (0.396, 0.398). |
| T3 | **Idio does not beat the information-matched control on tradable returns** | **CONFIRMED** | idio wins 2/6; 0/48 cells profitable and clearing t > 3.46; max t 1.90. |
| T4 | Idio looks better on the residual target — a warning, not a win | **CONFIRMED** | idio improves on `resid_fwd` vs `fwd_ret_1d` in 6/6 features. Exactly why `resid_fwd` is not evidence of tradability. |
| T5 | Staleness costs more than idiosyncrasy gains (`raw` > `rawlag` ≳ `idio`) | **REFUTED** | `raw` > `rawlag` on only 3/6; median `raw − rawlag` ≈ 0.00 Sharpe. Three days of staleness is approximately free at the 7–30d chart-feature horizon. |
| T6 | Volatility standardisation helps | **REFUTED** | `iz` is worse than `idio` on 5/6 features, often badly (ma_ratio 0.09 vs 0.87). |

### 6.1 The escape hatch is closed

The natural defence of a null result like T3 is "the two arms hold nearly the
same book, so of course they score the same". §4.2 rules that out. The
rolling-extreme features share only ~40% of their long decile between arms —
they genuinely hold different books — and idio still does not win. Worse, on
`range_pos_30d`, the feature where residualising changes the book *most*
(overlap 0.398), idio is the **worst** arm in the grid (0.22 vs rawlag 0.74).

Where residualisation changes the book most, it makes the book worse.

### 6.2 What this does and does not say

**Does not refute the quant.** The claim is most plausible for *directional,
single-name* signals, where common-factor motion genuinely swamps the
name-specific pattern. This repository does not trade that; it trades a
cross-sectional decile spread in which the long and short legs already share
and cancel most common exposure. Residualising before ranking is largely
redundant *here*, which is exactly what §4.1's R² of 0.06 and this grid show.

**Does say** that porting chart signals onto idio paths is not a free Sharpe
upgrade for this book, and that the mode-(b) construction which would make the
claim bite needs a hedge whose cost is not modelled anywhere in this document.

### 6.3 The one durable, transferable finding

Not about idio charts at all: **any strategy scored on a log-return target pays
itself a variance-drag premium for shorting volatile names.** Measured here at
−34.76 bp/day, it manufactured an apparent Sharpe 4.46 that survived removal of
the residualisation entirely. Log returns are correct for fitting and for
cumulating a path; they are wrong as a P&L target. This is worth adding to
`docs/backtesting_errors_we_never_repeat.md`.

### 6.4a Mode (b) — the hedged book. RUN, and the kill condition fires.

`scripts/screen_idio_hedged.py`, same panel, same universe, same cost basis.

**What is actually hedgeable.** Three of COMMON4's four exposures are
cross-sectional ranks with no tradable instrument — nothing pays "liquidity
rank". Only `btc_beta` has one. So mode (b) here is a **BTC-beta-neutral** book,
not a fully factor-neutral one, and the residual return is correspondingly *not*
fully harvestable even in principle.

Book: the same decile spread plus a BTC leg sized to `net_beta = mean(β|long) −
mean(β|short)`, rebalanced daily, charged on `|Δnet_beta|` at the measured round
trip, with the spread leg charged on its own turnover as in §5.

| | result |
| --- | --- |
| hedging improves net Sharpe | **3 / 24 cells** |
| median Δ Sharpe | **−0.183** |
| mean Δ Sharpe | −0.168 |
| best hedged cell | `raw vol_of_vol_30d`, Sharpe 0.85, t 1.49 |
| hedged cells profitable and clearing t > 3.46 | **0 / 24** |

**Declared kill condition met. The idio-chart programme is closed for this
repository.**

Two findings inside the null worth keeping:

**The books are not beta-neutral, and residualising genuinely de-betas them.**
Mean `|net_beta|` is 0.38–0.59 on the raw arms and **0.26–0.39 on the idio
arms**. The prior expectation that a decile long/short is already beta-neutral
is wrong, and the idio construction does mechanically what it claims — it just
does not convert that into Sharpe.

**Hedging loses return, not merely cost.** `idio dist_from_30d_high` goes
22.32 → 18.02 bp. The beta tilt *paid* over 2023-06..2026-06, so removing it
removed return. That part is period-specific and should not be generalised; the
hedge's own turnover (0.12–0.30/day) is structural, so the tilt would have to be
actively harmful before the hedge earned its keep. `vol_of_vol_30d` is the one
consistent exception (+0.150 raw, +0.236 rawlag) and is also the book with the
largest beta mismatch, which is the mechanism working as expected.

### 6.4 What remains open

The programme is closed as a *Sharpe upgrade for this book*. Three narrower
questions are genuinely untested and are cheap now that the panel and scripts
exist:

1. **The momentum arms on a momentum-free factor set** — §6.6's defect. Built as
   `--factor-set nomom3` (COMMON4 minus `xs_rank_ret_30d`); see §6.8.
2. ~~A directional single-name construction.~~ **Tested — see §6.9.** It
   fails harder than the cross-sectional book (median Δ Sharpe −0.572, 0/24
   clearing the bar), which is what turns "closed for this book" into
   "closed".
3. **Intraday residualisation.** `close_location_1d` and `range_extension_30d`
   have no idio analogue on a daily grid. IR1 (2026-06-10) already found the
   intraday residual signal real and an order of magnitude short on economics,
   which is a strong prior against.

## 6.5 Prior art this screen should have read first

Two pre-registered residual experiments were run in June 2026, closed, and then
**deleted from the docs tree**. Both are recoverable only from git history, and
neither is cited by any current document. Verified by reading the receipts out
of git, not from a summary.

**`docs/preregistration/rmom-latency-falsification-2026-06-09.md`** (deleted in
`f7dadd6`). Verdict, verbatim:

> "Rmom is causal, not an off-by-one leak, but the usable effect is
> concentrated at the freshest legal daily availability. Delaying it further
> kills the edge."
>
> "Daily-rmom continuous evidence has no deployment-grade operational margin."

**`docs/preregistration/intraday-residual-scout-2026-06-10.md`** (also
deleted). Its commit message (`115c423`) records the verdict directly:
"IR1 residual-reversal scout — physics CONFIRMED, economics fail by an order of
magnitude; the intraday-class proposal closes."

The house had therefore already established, twice, that **residualisation
produces a real signal that does not pay this cost stack** — the same measured
cost basis this screen charges. That is the same conclusion §6 reaches by a
different route, which is corroboration, but it should have been the starting
prior rather than an independent rediscovery.

An apparent tension worth naming rather than smoothing: the 2026-06-09 receipt
says delay kills the rmom edge, while T5 here measures three days of staleness
as approximately free. These are different objects — that receipt concerns the
availability lag of a short-window residual-momentum scalar, this concerns a
three-day lag on 7–30 day chart shapes — but the two should not be quoted
together as if they agreed.

**The correction that matters most:** an earlier draft of this work described
`residual_momentum` as a deployed idio signal whose live record was evidence for
the claim. It is not deployed. `STATE.md` records CONTINUOUS retired by owner
override on **2026-07-29**, with the rmom and hedge timers off; the replacement
CARRY sleeve uses no residuals. RMOM's contribution was also never ablated —
all eleven arms of the 2026-07-26 redesign sweep hold `rmom_quantile` fixed at
0.25, so the idio gate rode through the entire redesign as an inherited
constant.

## 6.6 A design flaw in this screen's momentum arms

`COMMON4_FACTOR_COLUMNS` includes **`xs_rank_ret_30d`**
(`liquidity_migration/risk_model.py:41-46`). The residual is therefore
orthogonal to 30-day cross-sectional momentum *by construction*, on every day,
before any chart feature is computed.

So `idio_ret_30d` and `idio_ma_ratio_7_30` measure momentum-shape on a series
from which momentum has been explicitly regressed out. That is not the object
the quant's claim describes, and those two arms are **not a fair test** of it.
The rolling-extreme features (`dist_from_30d_high`, `range_pos_30d`) are not in
the regressor set and are unaffected — and they are also the features where §4.2
found the books genuinely diverge, so the conclusion in §6.1 rests on the arms
that remain valid.

A corrected momentum arm would residualise on a factor set excluding
`xs_rank_ret_30d`. That is a real gap in this screen, not a quibble.

## 6.9 The directional book — the claim's strongest form, tested

Every result above is a cross-sectional decile spread, which is the
construction *least* favourable to the claim: the long and short legs share the
common factor, so it cancels in the spread whether or not you residualise. A
null there is weak evidence about the claim and strong evidence only about that
book. §6.4 listed the directional single-name construction as untested. It is
now tested — `scripts/screen_idio_directional.py`.

Rule, fixed in advance and uniform across all six features and all four sources:
`pos[i,t] = sign(z[i,t])` where `z` is the 60-day trailing, strictly-prior
z-score of the feature *within that symbol*. No cross-sectional information
enters. A time-series z-score is the least arbitrary way to turn a level feature
into a directional call without inventing a per-feature threshold.

**The premise holds.** Mean `|net_beta|` in this book is **0.82–0.84** on the raw
arms — roughly 3× the decile book's — so common-factor motion genuinely does not
cancel here. And the idio arms cut it to **0.22–0.40**: residualising does
mechanically what it claims.

**The claim still fails, and by more.**

| | cross-sectional (§5) | directional |
| --- | --- | --- |
| idio beats information-matched control | 2 / 6 | 2 / 6 |
| median Δ Sharpe (idio − rawlag) | −0.14 | **−0.572** |
| hedged cells profitable and clearing the bar | 0 / 24 | **0 / 24** |
| largest \|t\| in the grid | 1.90 | 2.56 |

Family is now 44 prior + 48 cross-sectional + 48 directional = 140, so the
threshold restates to **|t| > 3.57**.

Nearly every directional cell is negative after cost. The one cell where idio
beats its control decisively is `vol_of_vol_30d` (+1.87 Sharpe, idio 0.86 vs
rawlag −1.01) — at t 1.47, which is not evidence.

**This is the finding that closes the programme rather than merely bounding it.**
The decile-spread null could be explained away as "wrong book". This cannot: in
the book where common-factor contamination is real and large, residualising the
chart makes the result *worse*, not better.

One honest limit: `sign(60d z-score)` is a single pre-declared directional rule,
not a tuned strategy. A tuned rule might do better — but tuning is precisely
what the multiple-testing frame forbids, and the pre-declaration is what makes
this a test rather than a search.

## 6.8 The corrected momentum arms — defect real, conclusion unchanged

§6.6 identified a genuine flaw: `xs_rank_ret_30d` is a COMMON4 regressor, so the
idio momentum arms measured momentum on a series with momentum regressed out.
`--factor-set nomom3` (btc_beta, realized_vol_rank, liquidity_rank) removes the
circularity. Same panel, same span, same screen.

| | common4 | nomom3 |
| --- | --- | --- |
| factor R² (simple / log) | 0.057 / 0.060 | 0.045 / 0.047 |
| idio beats `rawlag` | 2 / 6 | **1 / 6** |
| `ma_ratio_7_30` (idio − rawlag) | +0.247 | +0.095 |
| `ret_30d` (idio − rawlag) | −0.143 | **−0.244** |
| cells profitable and clearing t > 3.46 | 0 / 48 | **0 / 48** |

Removing the momentum regressor makes the idio arms **worse**, not better. The
defect was real and is not load-bearing: correcting it strengthens §6's
conclusion rather than threatening it. `ma_ratio_7_30` remains the only feature
favouring idio and its margin shrinks by more than half.

This also disposes of the last plausible rescue. The momentum arms were the one
place where a mechanical artifact of the experiment's design could have been
hiding a real effect; it was not.

### 6.8a A repeat of taxonomy item 34, committed by this author, in this session

Renaming `resid_fwd` to the simple-return residual (§4.3) left
`scripts/diagnose_idio_panel.py` comparing a *simple* residual against a *log*
total. The first `nomom3` diagnostic therefore reported a **negative R²**
(mean −0.053). That is not a finding, it is the same units mismatch this
document had just added to the failure taxonomy an hour earlier, reintroduced by
renaming a column without updating its consumer.

`factor_r2` now takes an explicit `scale` and pairs `resid_fwd`↔`fwd_ret_1d` or
`resid_fwd_log`↔`fwd_logret_1d`, refusing to mix. Reported R² values are
unaffected — the earlier 0.060 was computed before the rename, on matched log
scales, and reproduces exactly.

The transferable lesson is narrower and more useful than "be careful": **a
renamed column silently changes the meaning of every downstream consumer that
still compiles.** Taxonomy item 34 should be read as a naming discipline, not
only an arithmetic one.

## 6.7 Unresolved

- **The "~44 prior mechanisms" count is not auditable.** No artifact in the tree
  or in git history enumerates it; every reference (`scripts/screen_phase1.py`,
  four configs) asserts a number. The `|t| > 3.46` threshold this
  document quotes therefore inherits an unverifiable denominator, and whether
  any of these 48 cells overlaps something already inside that 44 is unknown.
- **Turnover measured two ways disagrees by ~2.3×.** `realized_turnover` gives
  0.081–0.284/day; an independent decile-retention estimate gave 0.78–2.26. The
  conversion formulas match, so the disagreement is in the retention estimate.
  Nothing clears the bar at either cost basis, so no conclusion turns on it, but
  it is unexplained.
- **Decile-overlap figures differ between two computations** (0.396–0.865 here
  versus 0.225–0.691 independently). Both agree the rolling-extreme features
  overlap least; the levels differ.

## 7. Scope and limitations

- **This screens the claim in the cross-sectional decile book this repository
  trades**, not in general. The construction most favourable to the claim — a
  directional single-name signal on a hedged residual — is mode (b) and carries
  a hedge cost this panel does not model.
- `resid_fwd` results are **not tradable numbers**. They are the ceiling a
  hedged implementation could approach before hedge costs.
- Costs are charged as a flat round trip per daily rebalance at
  `cross_section.MEASURED_ROUND_TRIP_BP` (15.56 bp), doubled for the 2× gross
  book. No turnover modelling: a signal that rebalances less is under-credited
  and one that rebalances more is over-credited.
- The factor model is the repository's existing one. "Idio" means "residual to
  COMMON4"; a different factor set defines a different residual, which is why
  the `full6` arm exists separately.

## 8. Reproduction

```bash
POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/build_idio_panel.py \
    --root ~/SHARED_DATA/bybit_full_pit --start 2023-06-01 --end 2026-07-01 \
    --out data/idio_panel/bybit
.venv/bin/python -u scripts/diagnose_idio_panel.py --panel data/idio_panel/bybit/panel_common4.parquet
.venv/bin/python -u scripts/screen_idio_charts.py --panel data/idio_panel/bybit/panel_common4.parquet --era-split
```
