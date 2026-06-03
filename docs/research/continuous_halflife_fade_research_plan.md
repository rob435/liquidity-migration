# Continuous-Fade v2 — Volatility-Half-Life-Timed Fade

**Research plan / pre-registration scaffold.** Working name: `continuous-halflife-fade`. Status: **PROPOSED — not yet run.** Author: lead-author synthesis. Date: 2026-06-03.

> This is the single design-of-record for the rebuilt continuous sleeve. It supersedes the retired always-in D9 continuous sleeve. Read alongside `STATE.md`, `docs/research_summary.md`, `docs/backtesting_errors_we_never_repeat.md`, and `docs/parameter_pre_registration.md`. The frozen parameter grid in §8 must be committed to `docs/preregistration/2026-06-03-continuous-halflife-fade.md` BEFORE the first non-exploratory run.

---

## 1. Motivation — what the dead v1 taught us

The previous continuous sleeve (rmom-gated, always-in D9-decile fade) was disabled after a deep audit found it died of **two independent, fatal failures** — both of which the new design must engineer out *before* the first backtest, not discover after.

**Failure 1 — a ~25h look-ahead leak.** The sleeve did **not** use the short system's filters at all. It built a separate cross-sectional composite-decile + rmom panel (`continuous_events.py:151-265`, `build_continuous_panel` / `compute_continuous_decile_panel`) and shorted the top decile (`config.decile=9`, `continuous_events.py:65`). That decile/rmom panel carried a `.shift(1)`-class join error: at the decision bar it used information that, in live trading, only arrives ~25h later (the rmom day-join keyed on stamp day instead of the trading day `date(ts_ms − 1ms)`). The effect was brutal and quantified: **gross "edge" of +319% collapsed to +21% gross / −68% net** the moment the join was made causal. The leak surface that killed it is exactly the surface a half-life estimator re-opens (a window fit on bars, easiest to write by peeking forward) — so §7 is heavy.

**Failure 2 — a structural cost trap.** Even had the signal been real, the design was uneconomic. Always-in: 24/25 slots filled, ~11.8k trades over the window, fixed timer per fresh D9 spell. Costs −65% + funding −25% buried even the gross. The binding constraint was never per-trade edge — it was `cost_per_trade × n_trades`: `36bps × ~11.8k` dwarfs any thin per-trade fade edge.

**The new thesis is the structural fix for both.** A continuous observer, **flat by default**, that:
1. **Screens candidates perpetually using the SAME filters as the short system** (the liquidity-migration volume-event selection — already proven causal and PIT-correct), and
2. For each candidate (a pump / liquidity-migration event), **estimates a causal half-life of post-event volatility decay**, and
3. **Enters the short ONLY WHEN volatility has "died down"** — the pump is exhausting / stabilizing — timing the fade by volatility decay, *never* by a fixed wait and *never* by any future data.

This attacks Failure 2 directly: an event converts to a trade only if vol actually decays in-window, so the trade count drops by an estimated **10–50×** (from ~11.8k to low-hundreds–~1-2k), and entries cluster at *post-pump stabilization* where the confirmed-fade edge lives (short the giveback, not the top). It inherits the short stack's defense against Failure 1 for the *selection* half. The only new leak surface is the half-life estimator, which §4 and §7 lock down.

The hardware constraint is gone: research runs on a **16-core Ryzen 5950X** with ample RAM/CPU — the full parameter grid (§8) is embarrassingly parallel and cheap; no 16GB-box restriction.

---

## 2. Hypothesis, prediction, falsifier

**Hypothesis (one paragraph, mechanism not metric).** A liquidity-migration pump injects transient volatility that decays at a *measurable, per-name rate*. Entering the fade at the top captures mean-reversion but also the highest residual upside variance (the old catch-the-top failure mode). Entering once realized volatility has decayed below a fixed fraction of the event-peak — i.e. once roughly one half-life has elapsed — waits for the move to **exhaust**, so the short is the confirmed giveback rather than the pop. The half-life additionally *ranks* candidates by how fast each is stabilizing, letting the observer skip events that never calm down within the holding horizon. Because entry is sparse and well-timed, the realistic ~26–36bps round-trip cost (§6) is clearable where the always-in design's was not.

**Predicted direction + magnitude (pre-committed, falsifiable):**
- **Net return positive on BOTH venues** (`bybit_full_pit` and `binance_full_pit`) after the full cost+funding model. (The old sleeve was net −68% on one venue; the bar here is sign-correct AND positive on both.)
- **Trade count an order of magnitude below ~11.8k** — target low-hundreds to ~1-2k over the full window. If the design still fires ~10k trades, "flat by default" is not implemented — that is itself a falsifier.
- **Cost drag materially below the old −65%; funding drag below −25%.**
- **Edge monotone-ish in decayed-RV** on an in-sample diagnostic: decayed-RV entries outperform still-hot / slow-decay entries. If decayed and non-decayed entries perform the same, the half-life adds nothing.

**Falsifiers (any → reject / `invalid`):**
- Edge exists only at `entry_delay_hours=0`, or collapses under a +1-bar delayed-copy latency test → look-ahead, the old disaster recurring.
- Net-negative on **either** venue after full costs+funding.
- **Sign flip** bybit vs binance → venue microstructure, not alpha.
- Trade count stays in the ~10k range / slots stay saturated → the old always-in cost trap rewritten.
- The half-life parameter has **no monotone relationship** to fade PnL on the in-sample diagnostic → the timing element is noise; any positive result is the underlying fade, not the thesis.

---

## 3. Candidate screen — reuse the short system's filters verbatim

The "what is a candidate right now" decision is the short sleeve's selection chain, run continuously. This is a **selection-only reuse**: no new alpha is mined at the screen layer; the new alpha is purely *when* we enter within the post-event window. The chain is a 3-layer pure-polars pipeline in `volume_events_filters.py`, already perpetual-capable and PIT-correct.

### 3.1 The selection chain (`_select_events` → `select_events_with_stage_counts`, `volume_events_filters.py:31-84`)

**Stage A — feature build.**
- `build_volume_features(klines)` (`volume_features.py:18`) — daily-bar aggregation (`_daily_bars`, requires ≥20 hourly bars/day, `:103`), cross-sectional robust-z volume scores (`_add_cross_sectional_z`, `:110`), and **`liquidity_rank`** = per-`ts_ms` ordinal rank of `log_turnover` descending (`_add_liquidity_rank`, `:132`) — THE liquidity gate's input.
- `_enriched_event_features(...)` (`volume_events_features.py:30`) — joins daily returns, funding, OI, signed-flow, basis; computes `residual_return_1d` (`:70`), `signal_day_close_location` (`:604`), `signal_day_last6h_*`, `up_volume_concentration`; per-`ts_ms` `*_rank_frac` columns (`:105`); PIT/age via `_attach_event_archive_membership` (`:484`).

**Stage B — `_event_filter_base` (universe + PIT + market-context, `volume_events_filters.py:166`):**

| Gate | Code | Parameter (default) |
|---|---|---|
| Score finite | `:179` | score_col non-null & finite |
| Symbol exclusion | `:180`/`:970` | `exclude_symbols` (`DEFAULT_EXCLUDED_SYMBOLS`) |
| **PIT membership** | `:181-184` | `require_pit_membership=True` → keeps only `tradable_membership_flag` rows |
| Min turnover | `:185-186` | `universe_min_daily_turnover=0.0` (off) |
| Universe rank band (low) | `:188-189` | `universe_rank_min=31` |
| Universe rank band (high) | `:190-191` | `universe_rank_max=150` |
| Market context | `:192`/`:976` | `market_median_return_1d_*`, `market_pct_up_1d_*`, `btc_return_1d_*` (off by default) |

**Stage C — `_filter_liquidity_migration` (the event-detection gate, `volume_events_filters.py:378`).** Always-on:
- **Volume-event trigger:** `rank_col >= top_cut` AND `prior7_{rank_col} < top_cut` (`:509-510`) — top `threshold` now, not a week ago; `top_cut = 1.0 − scenario.threshold` (`:64`), `threshold=0.40` default.
- **Rank migration:** `prior7_liquidity_rank − liquidity_rank >= liquidity_migration_rank_improvement_min` (cast Int64 to avoid u32 underflow, `:495`), `direction="improvement"` (`:144-145`, `improvement_min=150`).

Conditional gates (active only when off-default): `turnover_ratio_min=6.0` (`:513`), `residual_return_min=0.08` (`:552`), `day_return_max=10.0`/`return_7d_*`/`prior30_max_*`, `close_location_min=0.30`/`max=1.0` (`:737`), `market_pct_up_max=0.65` with hot-coin OR-escape (`:706-716`), `pit_age_days_min=90` (`:756`; promoted profile raises to **300**, `event_demo.py:1628`), `residual_momentum_max=10.0` (off; the rmom selection gate, `:762`).

**Stage D — crowding filter (`_apply_liquidity_migration_crowding_filter`, `:86`):** `crowding_filter="union_pathology"` (`volume_events.py:203`); thresholds `crowding_*` (`:204-213`).

### 3.2 Use the deployed config

Build the candidate pool from **`_demo_event_config(profile="promoted")`** (`event_demo.py:1587-1633`; or `liquidity_migration/promoted.py`): `max_active_symbols=12`, `pit_age_days_min=300`, `universe_rank_max=99999` (off), plus the four dropped vetoes. This makes the continuous candidate pool **identical to the live short book's**.

### 3.3 Perpetual screening — reuse the LIVE per-cycle wrapper

Two existing patterns; **reuse the live one**:
- **Live (per-cycle):** `select_demo_entry_candidates()` (`event_demo_planning.py:96`) — the short daemon's exact "screen at now" call (`event_demo._cycle_fetch_and_prepare`, `:599`). Per cycle: `_kline_window(now_ms, lookback_days)` → `_build_demo_features` (`event_demo_data.py:586`) → `_select_events` → walk events in `_execution_ordered_events` order, apply `_entry_decision_for_event(..., now_ms=now_ms)` (`volume_events.py:966`). Passing `now_ms` means a candidate whose entry condition hasn't matured is marked `pending` (`event_demo_planning.py:151`) and re-evaluated next cycle — exactly the perpetual-observer contract.
- **Backtest:** `_select_events` builds the candidate frame across all `ts_ms`; iterate chronologically (the `for event in sorted_events` loop, `volume_events.py:694`). `continuous_events._run_trades` (`:327`) already has the concurrency/cooldown/causal walk skeleton — feed it `_select_events` output instead of `_fresh_entries(panel)` output.

### 3.4 The one live PIT adaptation you MUST get right

- **Backtest, hard gate:** keep `require_full_pit_universe=True` (`volume_events.py:137`; `_full_pit_universe_pass`, `volume_events_pit.py:97`) and `require_pit_membership=True`. Per-trade membership `tradable_membership_flag` keys on the **trading day = `date(ts_ms − 1ms)`** (`volume_events_features.py:522`), NOT the stamp day.
- **LIVE adaptation (critical):** the live daemon passes an **empty** `archive_manifest` (`event_demo_data.py:600`), sets `require_pit_membership=False`/`require_full_pit_universe=False` (`event_demo.py:1591-1592`), and substitutes the exchange's live `listing_age_days` into `symbol_age_days`/`pit_age_days` (`event_demo_data.py:601-615`). The continuous observer's live path **must do the same substitution**, or `pit_age_days_min=300` rejects everything (null `pit_age_days` fails `is_not_null()` at `volume_events_filters.py:757`).

### 3.5 What is event-time-specific and must be adapted

The selection chain is intrinsically point-in-time per `ts_ms` and needs **no change**. The event-time coupling lives in the layers AFTER selection:
- **`_select_events` keys on the DAILY grid** (one row per (symbol, trading-day), stamped `day_start + MS_PER_DAY`). A daily candidate is live for the whole following day; the hourly observer must dedupe per (symbol, event-day). **Decision: reuse the `_trade_id`-keyed dedupe** (`event_demo_planning.py:178-181`) — one entry attempt per migration event, re-screened each hour until the half-life entry fires or the candidate ages out. (Do not switch to sub-daily feature grids without a separate pre-registration.)
- **`signal_day_*` features are full-day-of-signal aggregates** — causal at the daily decision stamp but they describe the *event day*, not "now." **The half-life estimator must compute its OWN trailing-volatility features from post-event hourly bars** (`symbol_bars` via `_indexed_price_bars_by_symbol`, `volume_events.py:950`; `continuous_events._entry_vol`, `:316` is a ready trailing-hourly-σ helper). Reusing `signal_day_*` as the decay signal re-introduces the daily-grid coupling.

---

## 4. The half-life estimation — the centerpiece

### 4.0 Setup, notation, and the one rule

Bars are 1h (`klines_1h` / live `event_demo_klines_1h`). A candidate fires at event bar `t = 0`. Per bar `i`: close `P_i`, log-price `p_i = ln P_i`, log-return `r_i = p_i − p_{i−1}`, absolute return `a_i = |r_i|`, realized-vol proxy `RV_i`.

**THE RULE (causal-from-line-one).** Every quantity used to decide entry at bar `t` is a function ONLY of bars `≤ t` (whose close is known at the decision instant). The old sleeve died on a `shift(1)` leak. Three concrete prohibitions:
1. **No centered windows.** Rolling stats are trailing only (right-aligned, `min_periods` set). `rolling(window, center=True)` peeks forward — banned.
2. **No "peak" over the full episode.** `RV_peak(t) = max_{0≤i≤t} RV_i` (running max of bars seen *so far*), never the hindsight max over future bars. This is the single most seductive leak in the whole design.
3. **No de-meaning / standardizing with full-sample moments.** Any mean/std subtracted for a fit is a trailing estimate.

**Litmus / pre-registered unit test:** recompute any feature `f(t)` on data truncated at `t`, assert it is unchanged (`np.allclose`); recompute with bars `>t` appended, assert invariance. This is the cheapest defense against the old death.

**Fit-window subtlety.** An OU/AR(1) half-life on a *trailing fixed window of length L* ending at `t` is causal and stationary-ish. An OU half-life on the *since-event expanding window* `[0,t]` is also causal (bar 0 is in the past) but non-stationary (spans the regime break) → biased-long. Both allowed, different clocks (§4.1.4).

### 4.1 Method (a) — Ornstein-Uhlenbeck / AR(1) mean-reversion half-life

**Model.** `dx_t = κ(μ − x_t)dt + σ dW_t`, `κ>0`. `E[x_t − μ] = (x_0 − μ)e^{−κt}`. **Half-life `H = ln(2)/κ`.**

**Discrete AR(1) ↔ OU (the OLS → κ relation).** Sampled at `Δ=1`: `x_t = c + φ x_{t−1} + ε_t`, `φ = e^{−κΔ}`, `c = μ(1−φ)`. Fit by OLS of `x_t` on `x_{t−1}` (with intercept) over the trailing window:
```
κ̂ = −ln(φ̂)/Δ     (requires 0 < φ̂ < 1)
H  = ln(2)/κ̂ = −ln(2)·Δ / ln(φ̂)
```
Equivalent (Chan): regress the change `Δx_t` on the lagged level `x_{t−1}`: `Δx_t = α + β x_{t−1} + ε_t`, `β = φ − 1`, `κ̂ = −ln(1+β̂)/Δ`. **Use the exact `−ln(2)/ln(1+β̂)` form** — the small-`β` shortcut `H ≈ −ln(2)/β̂` breaks in exactly the fast-decay regime we care about.

**Validity guards (must be in code):** `φ̂ ∈ (0,1)` — else `φ̂≥1` (vol still trending/explosive) → **no entry**, `φ̂≤0` (oscillatory) → no entry. Report the OLS t-stat / SE on `β̂`; require `|t| ≥ t_min` (~2) before trusting `H`.

**What series `x` to fit (ranked):**
1. **`x = RV_i` de-meaned by a trailing mean (preferred)** — the thesis-correct target: timing the *volatility* decay. The OLS intercept handles de-meaning (keep it in the regression; do NOT subtract a full-window mean separately).
2. **`x = a_i = |r_i|`** — cheaper, heavy-tailed (OU normality violated) but a usable decay timescale; good cross-check.
3. **`x = p_i` (log-price)** — the *short sleeve's* price-reversion clock, a different (correlated) timescale than vol-decay. Secondary confirmer only.

**Fixed trailing `L` vs since-event:** **fixed trailing `L`** is the robust default (sweep `L ∈ {48,72,96,168}` h), reported per bar; optionally blend with the since-event estimate once `t ≥ L`. Since-event expanding is biased-long and guarded by min-bars.

**Small-sample caveats:** AR(1) OLS is biased toward 0 in `φ` (Kendall/Marriott `≈ −(1+3φ)/N`) → `H` biased **short** → enter too early; with `L=72`, `N≈71` the bias is ~3–5% in `φ` near `φ≈1`. Either bias-correct or treat it as a known conservative skew and let the entry threshold absorb it — do NOT silently trust the point estimate. Heavy tails on `a_i` understate the theoretical SE → prefer empirical/block-bootstrap CIs.

**Entry rule (a):** vol is "settled" when `Ĥ(t) ≤ H_max` (decay fast enough that the pump is spent):
```
ENTER iff  φ̂∈(0,1)  AND  |t_stat(β̂)|≥t_min  AND  Ĥ(t)≤H_max  AND  t≥t_min_bars
           (AND the liquidity-migration selection still holds)
```

### 4.2 Method (b) — exponential-decay fit of RV post-peak

**Model.** `RV_t = RV_∞ + (RV_peak − RV_∞)·exp(−(t−t_peak)/τ)`, `H = τ·ln(2)`. Dropping the baseline (`RV_∞≈0` for *excess* vol): `ln RV_t = ln RV_peak − (1/τ)(t−t_peak) + ε_t` — fit by **OLS on logs**, slope `= −1/τ̂`. The 3-param NLS form (`scipy.optimize.curve_fit`) is a fragile refinement on ~10–40 noisy points; the log-linear 2-param fit is the workhorse. `RV_∞` may be seeded from the symbol's pre-event 168h baseline (`rv_168h`, already computable from the panel).

**Causal computation & the look-ahead trap (this method is the MOST dangerous for leakage):** `t_peak` and `RV_peak` MUST be the **running argmax/max over bars `≤ t`**. As new bars arrive, if RV makes a new high, `t_peak` moves forward and the fit resets — correct and causal. Fit only on bars strictly after the running peak, `i ∈ (t_peak, t]`, with `≥ n_min` (e.g. 6) post-peak bars before the slope is trusted.

**Caveats:** double-pump events → `t_peak` jumps, fit restarts, `H` spikes (correct: don't fade into a second pump; the entry rule must tolerate non-monotone `H` and arguably refuse entry while peaks are still being made). Log-OLS underweights large RV (minimizes relative error); WLS or 3-param NLS if early fast decay matters. Carry the slope SE; gate on it.

**Entry rule (b):** (i) half-life gate: enter when `Ĥ = τ̂ ln2 ≤ H_max` AND slope significantly negative (`t_stat ≤ −t_min`); or (ii) level gate: enter when `RV_t ≤ c·RV_peak` with `τ̂` confirming decay (bridges to method (e)).

### 4.3 Method (c) — ACF decay of |returns| (cross-check only)

`ρ̂(k) = Σ(a_i − ā)(a_{i−k} − ā) / Σ(a_i − ā)²`, trailing window, de-meaned. Geometric decay ⇒ `H = ln2/(−ln ρ̂(1))` (≡ method (a) on `a_i` via lag-1); more robustly fit `ln ρ̂(k) = −λk` over `k=1…K`, `H = ln2/λ̂`. Caveat: crypto `|return|` ACF is often **long-memory/hyperbolic**, not geometric — `H` here is a coarse persistence gauge, not a precise clock. Cap `K ≪ L` (e.g. `L/4`). **Best as a confirmer of (a), not a primary trigger** (noisiest of the parametric set).

### 4.4 Method (d) — EWMA / GARCH persistence

**EWMA (RiskMetrics) — the clean causal RV generator.** `σ²_t = λ σ²_{t−1} + (1−λ) r²_{t−1}`, strictly causal (uses `r_{t−1}`). Shock half-life `H = ln(2)/(−ln λ)`. `λ` is fixed/swept, so `H` is constant — EWMA's value is a **smooth causal `σ_t` series** feeding (e)/(a)/(b), not per-symbol `H`. Seed `σ²_0` with the trailing-window variance; **never** the full-sample variance.

**GARCH(1,1) — symbol characterizer, NOT event timer.** `σ²_t = ω + α r²_{t−1} + β σ²_{t−1}`; persistence `α+β`; vol half-life `H = ln(2)/(−ln(α+β))`. MLE (`arch_model(r, vol='Garch', p=1, q=1)`), refit on a rolling cadence. **Under-identified on a 72–168h window** — `α+β` pins near 1 (IGARCH) → `H→∞`. Use only a **long trailing window** (30–90 days of 1h bars) to estimate *baseline* persistence as a **reject filter** ("this symbol's vol never settles within our horizon → skip"). The causal `h`-step forecast `σ²_{t+h} − σ²_∞ = (α+β)^h(σ²_t − σ²_∞)`.

### 4.5 Method (e) — non-parametric "RV below c·running-peak" (the DEFAULT)

```
RV_peak(t) = max_{0≤i≤t} RV_i              (RUNNING max, causal)
ENTER at bar t  iff  RV_t ≤ c·RV_peak(t)  AND  (t − t_peak(t)) ≥ d_min
```
`c ∈ (0,1)` = the "died down" fraction (0.5 = vol halved); `d_min` = min bars since peak (avoids a one-bar dip).

**Why it's the safest baseline:** no model, no fit, no SE, no convergence failure — two params. The only leak surface is `RV_peak`, nailed to the running max by §4.0 rule 2; if the truncation unit-test passes, it is **provably causal**. It is *implicitly* a half-life rule: if `RV_t = c·RV_peak` under `exp(−t/τ)` decay, then `t − t_peak = τ ln(1/c)`, so **`c = 0.5` ⇔ "wait one half-life."** Choosing `c` *is* choosing how many half-lives to wait, without ever estimating `H`.

**Caveats:** a deep-but-brief dip can trip on noise → fix with `d_min` and/or a **`k`-consecutive-bar debounce** (sweep it). Peak can keep rising (double pump) → entry correctly deferred.

### 4.6 RV proxy choices (apply to all methods, held fixed within a run)

- **Parkinson (preferred — OHLC in the kline store):** `RV_i = (1/(4 ln2))·ln(H_i/L_i)²` — lower-variance per-bar vol, strictly within-bar, fully causal. (Section C's `0.361·ln(H/L)²` is the same constant, `1/(4 ln2) ≈ 0.361`.)
- **Garman-Klass:** OHLC, lower variance still.
- **Rolling std of returns** over a short sub-window `w` (e.g. 6h): `RV_i = std(r_{i−w+1..i})` — trailing, simple.
- **EWMA `σ_t`** from §4.4 — smooth, causal.

### 4.7 Recommended default + 2 fallbacks

**DEFAULT — Method (e), non-parametric running-peak RV trigger, Parkinson (OHLC) proxy.** Lowest-leakage, lowest-degrees-of-freedom, most robust; it *is* a half-life rule in disguise; no fit to fail/overfit; minimizes exactly the look-ahead surface that killed v1. A real edge should survive the simplest trigger before adding parametric machinery.
```
ENTER iff  RV_t ≤ c·RV_peak(t)  AND  (t − t_peak) ≥ d_min  AND  [k-bar debounce]  AND selection holds.
Start: c = 0.5, d_min = 6h, debounce k = 2, RV = Parkinson.
```

**FALLBACK 1 — Method (a), AR(1)/OU half-life on a trailing-window RV series.** The principled "estimate `κ`, wait one half-life" version; produces the `ln2/κ` number directly. Gate: `φ̂∈(0,1)`, `|t(β̂)|≥2`, `Ĥ(t)≤H_max`, `t≥t_min_bars`. Use first as a *confirmer of* (e) (enter only when both agree), then standalone.

**FALLBACK 2 — Method (b), exponential log-OLS decay of post-(running)peak RV.** Half-life tied to the observed peak. Gate on significantly-negative slope AND `τ̂ ln2 ≤ H_max`.

Methods (c) and (d-GARCH) are **cross-checks / symbol characterizers**: (c) noisiest; (d-GARCH) under-identified on short windows — long-window baseline persistence as a *reject* filter only. EWMA (d) earns its keep as the clean causal RV generator feeding the others.

**Headline pre-registered candidate: (e) as the gate, (a) as the confirmer** — enter only when RV has fallen below `c·peak` AND the trailing OU half-life is short and significant. Two cheap, near-orthogonal evidences that the pump is spent.

> Live-parity constraint (binding at design time): every estimator must run **identically** in `continuous_events.py` (backtest) and `continuous_demo.py` (live, via `LivePanelCache`). The live cache assembles a single current-ts frame from cached per-symbol carry; an estimator that cannot be expressed as a trailing per-symbol window cannot reproduce the byte-equivalent live value — **reject it at design time** (no point estimating something the daemon can't compute).

---

## 5. Entry / exit / sizing rules

### 5.1 Entry — replace `_fresh_entries` with `_halflife_entries`

Each candidate opens an observation window `[event_ts, event_ts + max_observe_hours]` (e.g. 48h); it is *watched*, not yet a trade. Walk bars `i` from `event_bar+1`; enter the short at the **first bar `i`** where ALL hold (every term uses `≤ bar i` data only):

1. **Vol decayed:** trailing-k RV `rv_k(i) ≤ vol_decay_frac · rv_peak` (e.g. 0.5) — the core "pump exhausting/stabilizing" gate (= method (e)).
2. **Elapsed ≥ min fraction of the half-life:** `(i − event_bar) ≥ halflife_min_mult · Ĥ` — guards against a one-bar dip on a still-running pump (uses the fallback estimator's `Ĥ`; for the pure-(e) default this is `d_min`).
3. **Price stalled, not still ripping (causal decel):** reuse the existing `entry_decel_lookback_h`/`entry_decel_max_ret` gate in `_run_trades` (`continuous_events.py:408-413`): `close[i]/close[i−lookback]−1 ≤ entry_decel_max_ret`. The confirmed-fade check (pop then giveback, not the top).
4. **Liquid:** signal-bar `turnover_quote ≥ liq_turnover_min` (`continuous_events.py:285`).
5. **One entry per event** (`triggered` flag; no re-arming within the event window — replaces the fresh-spell dedup).

The entry row `(symbol, sig_ts = i's bar_end, composite, turnover_quote)` flows into `_run_trades` exactly as today, through `_simulate_indexed_trade` with **`entry_delay_hours=1`** (honest +1h fill; `entry_delay_hours=0` is look-ahead, proxy-parity only). **No change to the trade simulator.** Sparsity here is the whole anti-cost mechanism.

### 5.2 Exit — fade-target / time-stop / vol-reaccelerate (all via existing `TradeLifecycleConfig`)

Wire in `_run_trades` (`continuous_events.py:342-352, 426-434`); no new exit-walk code except the one optional vol-reaccel branch:
- **Fade target (take-profit):** set `TradeLifecycleConfig.take_profit_pct` (currently hard-zeroed at `continuous_events.py:345`); `_simulate_indexed_trade` already honors `take_profit_price` (`volume_events.py:1396, 1434-1438`). Scale to the pump amplitude: `take_profit_pct = tp_frac · (close[event_bar]/pre_event_close − 1)` (small `TradeLifecycleConfig` extension to accept per-trade TP).
- **Time-stop:** `planned_exit_ts_ms = entry_bar_end + hold_ms`, `hold_hours = round(time_stop_mult · Ĥ)` — the decay timescale sets the hold. `_simulate_indexed_trade`'s `max_hold`/`data_end` fallback (`volume_events.py:1501-1506`) force-closes. Uses `EventScenario.hold_hours` (`volume_events.py:293`) / `_scenario_hold_ms` (`:307`).
- **Vol-reaccelerate stop (the one genuinely new exit predicate):** cleanest path — vol-scaled disaster stop via existing `stop_vol_mult` (`continuous_events.py:427-429`): `trade_stop = clamp(stop_vol_mult · entry_vol, 5%, 50%)`, combined with `failed_fade_hours`/`failed_fade_loss_pct` (`:346-348`, already wired). For a true "current realized vol > entry vol × m" predicate, add ONE branch in `_simulate_indexed_trade`'s bar loop (`volume_events.py:1405-1500`) computing trailing-k vol from `close_arr[idx−k:idx]`, breaking with `exit_reason="vol_reaccelerate"` — causal (uses bars `≤ idx`), the only candidate new line in the hot loop.

Live (`continuous_demo.py`): the tick-driven protective path (`_protective_exit_reason`, `:505`; `plan_protective_exits`, `:587`) already covers stop_approach/failed_fade/breakeven/max_hold. Add the vol-reaccel check there and **replace the `left_decile` state exit** (`:577`) — under the new thesis there is no "in-decile = hold" state, so the exit set is target/time/vol-reaccel (all price+time driven) and the daemon gets *simpler* (no `entry_state` decile snapshot needed for exits).

### 5.3 Sizing — keep two modes; `inverse_vol` default

- **Flat 2%/name:** `notional_weight = gross_exposure/max_active = 0.5/25` (`continuous_events.py:124`). Matches the deployed proxy.
- **inverse_vol (recommended default):** `nw = base_nw · clamp(target_vol_per_name / entry_vol, 1/clamp, clamp)` (`:421-423`), `entry_vol` from `_entry_vol` (`:316`). Because we enter *after* vol has decayed, entry-bar vol is lower and more stable, so inverse-vol is better-behaved than on the always-in sleeve — the decayed entry and the sizing rule reinforce.
- **`max_active` is now slack, not binding.** Sparse triggers rarely hit the 25-slot cap; `skipped_capacity` (`:396`) should drop near zero — a built-in smell-test that the new entry is actually sparse.

---

## 6. Backtest design — engine reuse + cost model + per-trade edge needed

**Architecture decision: keep the entire execution core and ~70% of `continuous_events.py`; replace only the entry-timing layer.** The engine never died — the selection did.

**Keep verbatim (execution + accounting + cost + funding):**
- `_simulate_indexed_trade` — `volume_events.py:1354` (bisect exit-walk `:1405-1500`; funding `:1509`; cost `:1518`; net `:1520`)
- `_indexed_price_bars_by_symbol` — `volume_events.py:950` → `_price_bars_by_symbol` — `trade_lifecycle.py:508` (the `ts_ms/bar_end_ts_ms/open/high/low/close` numpy arrays the half-life reads)
- `_round_trip_bps` (size/ADV impact) — `continuous_events.py:289`
- `_perp_funding_return` — `trade_lifecycle.py:480`; `_funding_lookup` — `:411`
- `_entry_vol` — `continuous_events.py:316`
- `_additive_equity` `:465`, `_portfolio_mtm_equity` `:548`, `_split_metrics` `:595`, report/PNG path `:636-756`
- `_panel_cache_stale` `:137` (the stuck-curve guard — keep, and extend, see §8/§7)

**Replace:**
- `_fresh_entries` (`:268`) → new `build_event_candidates` (uses the §3 selection chain) + `_halflife_entries` (§5.1).
- planned-exit selection in `_run_trades` (`:430-434`, state/fixed) → fade-target + time-stop + vol-reaccel (§5.2).

**New module:** `liquidity_migration/continuous_halflife.py` — `estimate_vol_halflife(close, high, low, event_bar, t, *, min_bars=6) -> float | None`, `build_event_candidates`, `_halflife_entries`, and `HalflifeFadeConfig` (extends `ContinuousEventConfig`, `continuous_events.py:57`, with `halflife_estimator`, `vol_decay_frac`, `halflife_min_mult`, `time_stop_mult`, `tp_frac`, `vol_reaccel_mult`, `max_observe_hours`).

### 6.1 Cost model (keep verbatim — `_round_trip_bps`, `continuous_events.py:289-302`)
```
round_trip_bps = 2·(taker_fee + spread) + 2·impact
impact_bps     = impact_coef_bps · participation^0.5
participation  = (notional_weight · deploy_capital) / signal_bar_hourly_turnover
```
Deployed defaults (`continuous_events.py:84-87`): `taker=5.5bps, spread=2.5bps, impact_coef=50bps, exp=0.5`; `notional = (0.5/25)·$1M = $20k`/name.
- **Fixed leg:** `2·(5.5+2.5) = 16 bps`.
- **Impact at the liquid floor** ($500k/h): `participation = 20000/500000 = 0.04`, `impact = 50·√0.04 = 10bps`, round-trip impact `= 20bps` → **total ≈ 36bps**.
- **Typical liquid alt** ($2M/h): `participation=0.01`, `impact=5bps` → **total ≈ 26bps**. Sweeping the gate up to $1M drops worst-case round-trip impact to ~14bps.
- **Funding-to-exit:** shorts *receive* funding on average in alt pumps (positive carry); the integrity gate requires modeling both ways. At +0.01%/8h over a ~HL-scaled ~12h hold ≈ +1.5bps credit (immaterial); budget a possible **−5 to −10bps** debit in a negative-funding regime so the smell test is conservative.

### 6.2 Per-trade edge needed
A trade must clear **~40bps of adverse gross move** at the liquid floor (36 cost + ~4 funding buffer), ~30bps for a typical name, just to break even. A real post-pump fade gives back a meaningful chunk of the pump — a 1–3% pump retracing 30–50% yields **30–150bps** of favorable short move — so there is comfortable headroom IF the entry is well-timed. The binding constraint is `win-rate × avg-win` vs `cost × n_trades`. The always-in sleeve lost because `36bps × 11.8k` dwarfed a thin per-trade edge; sparse half-life entry wins by cutting `n_trades` ~10–50× while raising avg per-trade gross (entering at stabilization, not the top). **Pre-registered acceptance: median net per-trade > 0, believable hit-rate (~45–55%), losing months present.** A sweep showing >60% win-rate or no losing month is rejected as too-good.

---

## 7. Causal / no-look-ahead guarantees

The general gates in `docs/backtesting_errors_we_never_repeat.md` apply (declare `decision_ts`, `data_available_ts`, `order_submit_ts`, `fill_window`, `exit_activation_ts`, `state_initialization_ts`; every feature causal at `decision_ts`; full PIT universe). On top of that, the half-life-specific obligations:

**The one inviolable rule.** The half-life estimate and the trigger that consumes it must be computable from a **strictly closed window of bars ending at or before the deciding bar `t`**, order submitted at `t + entry_delay_hours` (`entry_delay_hours=1`; `=0` is the look-ahead proxy and is `invalid`). Every quantity feeding the decision at `t` is a function only of `close[..t]` (selection features already PIT). No window may include bar `t+1` or beyond. No estimator may be "the half-life of the decay that *did* happen" — that is the future.

**Decision-timestamp manifest for THIS design (declared in the run record):**
- `event_ts` — the liquidity-migration event fires (§3 filters, PIT at event time).
- `observe_window` — bars `[event_ts..t]` of RV used to *fit* the half-life (post-event but pre-decision; entirely in the past relative to `t`).
- `decision_ts = t` — the "vol died down" trigger evaluates; inputs are the fit from `[event_ts..t]` and trailing RV on `close[..t]` only.
- `order_submit_ts = t + entry_delay_hours` (≥1 bar).
- `fill_window` / fill model — bar after the deciding close, costed (§6).
- `exit_activation_ts`, `state_initialization_ts` — exit/stop/cooldown initialized exactly as `continuous_demo.py` would at activation (warm-start ban).

**Enumerated half-life-specific look-ahead traps — each gets a unit test that fails loudly:**
1. **Centered / forward-extended fit window.** Fit window must END at or before `decision_ts`. Test: assert the estimator's input slice has `max_index ≤ t` for every ledger entry.
2. **"Half-life of the realized decay" as a label.** The half-life must be an *extrapolation from past bars*; the trigger is "projected/observed RV at `t` has fallen below threshold," never "RV reached its eventual floor." Test: estimator truncated at `t` returns the same value as on the full series truncated at `t`.
3. **EWM / rolling seeded with future bars.** Causal only if `min_samples` satisfied from the left and the frame is sorted ascending by `ts_ms` *per symbol*. Test: recompute every RV feature reversed-then-reversed and assert identity; assert RV at `t` invariant to appending bars `>t`.
4. **The 25h-class data-availability lag (the exact old leak).** A half-life/RV join on *stamp day* instead of *trading day = `date(ts_ms − 1ms)`* re-opens it. Test: the **delayed-copy test** — re-run with every half-life/RV input shifted +1 extra bar; the edge must degrade *gracefully*, not collapse. A signal that survives only at 0-bar latency is the old artifact.
5. **Event-window vs universe-vol contamination.** RV must be the symbol's *own* trailing RV (`.over("symbol")`), never a cross-sectional stat that smears in other symbols' future. Test: per-symbol feature independence (keep the half-life in `_per_symbol_features`, never the within-ts cross-section step).
6. **Exit/stop state warm-start.** Decay-exits, max-hold, cooldown, `entry_pause_after_adverse_exits` initialize at activation, not pre-warmed. Test: state-initialization parity against `continuous_demo`.
7. **OHLC intrabar path on the entry bar.** If entry and a same-bar stop could both fire, use the conservative rule. The trigger fires on a *closed* bar; the fill is the next bar — keep them time-separated.

**Backtest↔demo lifecycle parity.** The estimator must produce the byte-equivalent value in backtest and live (`LivePanelCache`); if it cannot be mapped onto the live trailing-per-symbol carry model, it is not deployable — reject at design time (restates the §4.7 live-parity constraint).

---

## 8. Validation, pre-registration, success/kill criteria

### 8.1 Pre-registration (per `docs/parameter_pre_registration.md`)

A receipt is committed to `docs/preregistration/2026-06-03-continuous-halflife-fade.md` **before** the first non-exploratory run, **in the same PR as the estimator code**. It declares: what's changing, the §2 hypothesis, the §2 predicted direction + magnitude (falsifiable), the failure modes, roots touched (`bybit_full_pit`, `binance_full_pit`, forward demo), the BINDING decision rule (§8.4), the **full half-life candidate set frozen up front** (H1 log-linear OLS = method (b); H2 EWM-decay = method (d); H3 model-free `c·peak` crossing = method (e); plus method (a) OU as the §4.7 confirmer), the frozen trigger grid below, and the exact run command. Honesty rules from the standard are verbatim: a wrong predicted direction cannot be silently repurposed; the decision rule is binding; the verdict is committed and not rewritten. Because the estimator search is inherently multiple-testing, the **full candidate set and grid are frozen before any validation-window run**, and **every cell's full distribution is reported, not just the winner**.

### 8.2 Parameters to sweep (FROZEN in the pre-reg before any run)

| Param | Method | Grid (start) | Notes |
|---|---|---|---|
| `RV` proxy | all | {Parkinson, GK, rolling-std(6h), EWMA} | one choice, fixed within a run |
| `c` (died-down fraction) | (e),(b) | {0.3, 0.4, 0.5, 0.6, 0.7} | ⇔ # half-lives waited |
| `d_min` (bars since peak) | (e) | {3, 6, 12, 24} h | floor on elapsed time |
| debounce `k` | (e) | {1, 2, 3} consecutive bars | noise guard |
| `L` (trailing fit window) | (a),(c),(d-EWMA seed) | {48, 72, 96, 168} h | stationarity vs responsiveness |
| `H_max` (max half-life accepted) | (a),(b) | {6, 12, 24, 48} h | "fast enough decay" |
| `t_min` (slope/β significance) | (a),(b) | {1.5, 2.0, 2.5} | reject noise fits |
| `t_min_bars` (min bars post-event before entry) | all | {3, 6, 12} h | no entry on tiny samples |
| `λ` (EWMA) | (d) | {0.94, 0.97, 0.99} | ⇔ `H ≈ {11, 23, 69}` bars |
| GARCH baseline window | (d) | {30, 60, 90} d | reject-filter only |
| since-event vs trailing | (a) | {trailing, blend} | expanding biased-long, guarded |
| `vol_decay_frac` | entry | {0.4, 0.5, 0.6} | core gate |
| `halflife_min_mult`, `time_stop_mult`, `tp_frac`, `vol_reaccel_mult` | entry/exit | (pre-list) | scale-aware to `Ĥ` |
| `liq_turnover_min` | screen | {500k, 1M, 2M} | impact vs sample |
| sizing | sizing | {flat, inverse_vol} | §5.3 |
| max hold / time-stop | exit | {24, 48, 72} h | costs/funding bite with hold |

### 8.3 Validation plan (full grid on the 5950X — embarrassingly parallel over cells)

1. **Causality unit test FIRST** (before any sweep): assert `estimate_vol_halflife` and `_halflife_entries` give *identical* output whether or not future bars are present in the array — the regression test for the class of leak that killed v1. Mirror the existing `np.allclose` discipline (`tests/test_liquidity_migration_continuous_demo.py`).
2. **In-sample vs OOS split.** **No internal pre-2023 OOS root exists** (universe too small). OOS = a time-ordered train/test split on the per-venue full-PIT roots PLUS forward demo as the true pristine OOS. Pick the estimator + grid winner ONLY on the train segment; confirm on the held-out later segment, untouched until the winner is locked (once it influences a threshold it is no longer OOS).
3. **Cross-venue agreement.** Run `bybit_full_pit` and `binance_full_pit` simultaneously by default. The roots share most top-20 perps (not independent), but a **sign flip is informative** → venue microstructure, not alpha → reject. Required: net-positive and same-signed on both.
4. **Cost-stress ladder.** Base / 1.5× / 2× fees+slippage; funding as a path-dependent cost (not a friendly constant — the old −25% was real). **A result positive only at base costs is not a candidate.** If a root lacks a funding dataset, label `fee/slippage stressed but funding-missing` and do NOT cite it as net-edge proof.
5. **Report through the existing path** (`continuous_events.py:636-756`): `_additive_summary` + `_portfolio_mtm_equity` (correlated-DD-aware curve) + early/recent split + the BTC PNG, all marked EXPLORATORY. Apply `scripts/r1_robustness.py` (Tier-2: thirds, monthly-delta concentration top-k, leave-one-month-out sign flip, block-bootstrap CI on annualized-return Δ and MAR Δ). Forward demo (`continuous_demo.py` paper cycle, Singapore VPS) is the arbiter, reconciled via `scripts/reconcile.sh` (the `pit-reconcile` skill).
6. **The explicit smell-test (a GATE, not decoration).** A real regime-conditional fade MUST exhibit **losing months**, **a believable drawdown** (well into double digits is normal for a regime-conditional short book, not sub-5%), and **believable trade economics** (hundreds-to-low-thousands, NOT ~11.8k; per-trade edge surviving the cost ladder).

> **RED-FLAG protocol.** A 36/36-up, sub-5%-DD, monotone-equity result is a reason to **STOP and hunt for the leak**, not celebrate. The old +319% gross looked exactly that good. On any "too good" result: re-run the +1-bar delayed-copy test, bump `entry_delay_hours`, clear caches and rebuild (§8.5), re-check the day-floor join. The prior after a clean-looking result is "I haven't found the leak yet," not "it's real." `apply_decision_rule` (legacy strict-Sharpe bar) is the suspicious-result tripwire; `r1_robustness` thirds + LOO-month sign-flip catch the one-month-carries-everything illusion.

### 8.4 Success criteria — Tier-2 demo-candidate (matches `scripts/r1_robustness.py` `_tier2_verdict`)

ACCEPT to demo-candidate only if ALL hold:
- `full_pit_universe_pass = True` on **both** venues (a non-full-PIT control invalidates the verdict for that venue).
- **Net return positive on BOTH venues** after the full cost+funding model (`by_ret > 0 AND bn_ret > 0`).
- **Pooled MAR Δ > +0.10** vs the pre-declared no-half-life control (enter-immediately-on-event baseline), AND neither venue worse than **−0.5 MAR**.
- Trade-count floor: **≥30 bybit / ≥20 binance** trades, AND order-of-magnitude fewer than ~11.8k (thesis fidelity).
- Edge survives the cost-stress ladder (still net-positive both venues at ≥1.5× costs).
- Smell-test passes (losing months, believable DD, no monotone-up curve).
- R1 robustness: positive in a majority of thirds; LOO-month does NOT flip the sign; bootstrap MAR-Δ p5 not deeply negative; top-3-month concentration not carrying ~all the edge.
- Fragility diagnostics (concentration, LOO sign-flip) **reported but non-blocking at Tier 2**; **blocking at Tier 3**.

**Tier-3 (real money) stays strict and OUT OF SCOPE here** — funding fully costed, fragility diagnostics blocking, forward-demo arbiter pass, cross-venue agreement. No real-money / promotion claim on backtest evidence alone (AGENTS.md). The account stays on demo; `REAL_MONEY` is not toggled.

**Kill criteria (any one → `invalid`/`rejected`, stop):** edge only at `entry_delay_hours=0` or collapses under +1-bar latency (look-ahead); net-negative on either venue; sign flip across venues; "flat by default" not realized (trade count ~10k / slots saturated); half-life parameter has no monotone relationship to fade PnL (timing adds nothing — the pre-reg hypothesis is falsified); a "too good" result with the leak hunt not exhausted (blocked until explained); cache-staleness / non-full-PIT / partial-PIT in the run record (`invalid`, re-run); a verdict computed on an exploratory (non-pre-registered) run (may not be cited as evidence).

### 8.5 Cache-freshness discipline

A stale panel cache already produced an invalid verdict here (the `_panel_cache_stale` docstring records the 2026-06-03 observation: the deciled-panel cache is keyed only on `rmom_quantile`, so a klines/rmom refresh left it serving a panel truncated to the OLD data end). **The half-life feature is computed inside this same cached panel, so it inherits the staleness risk directly.** Mandatory before every validation/re-validation run:
1. **Rebuild panels / clear engine caches first.** Delete `_continuous_engine_panel_rmom*.parquet` (and any new half-life cache file) before re-validating. The `_panel_cache_stale` mtime guard keys only on the rmom file's mtime, so **any change to the half-life estimator code/inputs that doesn't touch the rmom file is served stale.** → **Extend the cache key/staleness check to cover the half-life estimator's config + code hash, or hard-clear the cache on every estimator change** (pre-register this).
2. **Rebuild klines/rmom before backtesting recent windows** via the `reconcile.sh` auto-provision path (pull → refresh manifest → download recent klines → recompute rmom), not hand-assembled.
3. **Verify the run label** (`full_pit_universe_pass=True`, `run_label=full_pit_universe`) and data-root identity before trusting any number (tooling silently defaulted to `--allow-partial-pit` until 2026-05-28).
4. A verdict on a cache whose mtime predates the latest rmom/klines refresh is **`invalid` by definition** — re-run.

---

## 9. Open questions & risks

1. **Which half-life estimator wins, and is the timing element real?** The headline risk is that a positive result is the *underlying fade* (selection + decel gate) rather than the half-life timing. The monotone-in-decayed-RV diagnostic and the no-half-life control (enter-immediately-on-event) are the discriminators; if the half-life parameter has no monotone PnL relationship, the thesis is falsified even with positive returns.
2. **Daily-grid coupling.** Candidates fire once per (symbol, event-day) on the daily grid; the hourly observer re-screens within the day. Sub-daily feature grids exist (`build_volume_features(aggregation_ms=...)`) but switching grids needs a separate pre-reg.
3. **Double-pump events** make `H` non-monotone (method (b) restarts on a new running peak); the entry rule must tolerate this and arguably refuse entry while peaks are still being made.
4. **Small-sample AR(1) bias** (`φ̂` biased low → `H` biased short → enter early); the swept `H_max`/`vol_decay_frac` threshold must absorb it.
5. **GARCH under-identification** on short windows — relegated to a long-window reject filter only.
6. **Funding regime risk.** Shorts usually receive carry in alt pumps, but a negative-funding regime is budgeted as a −5 to −10bps debit; the path-dependent funding model must reflect both.
7. **Cross-venue non-independence** — the roots share names, so agreement is weak evidence but a sign flip is a strong reject signal.
8. **Capacity at the liquid floor** — the worst-case ~36bps round-trip at $500k/h; sweeping `liq_turnover_min` up trades sample size for lower impact.
9. **Live-parity feasibility** of the chosen estimator on `LivePanelCache` — a design-time gate; an estimator that can't reproduce the byte-equivalent live value is dead on arrival.

---

## 10. Concrete implementation steps (in order, runnable on the 5950X)

1. **New module `liquidity_migration/continuous_halflife.py`.** Implement `estimate_vol_halflife(close, high, low, event_bar, t, *, min_bars=6) -> float | None` for methods (e)-default, (a), (b) (and (d) EWMA RV generator); `build_event_candidates(data_root, config)` (calls the §3 `_select_events` chain, caches like `build_continuous_panel` with `_panel_cache_stale` invalidation extended per §8.5); `_halflife_entries(candidates, symbol_bars, config)` (§5.1); `HalflifeFadeConfig` extending `ContinuousEventConfig`.
2. **Wire entries/exits into the kept engine.** Feed `_halflife_entries` output into `_run_trades`; replace planned-exit selection with fade-target (`take_profit_pct`) + time-stop (`hold_hours←Ĥ`) + vol-reaccel (`stop_vol_mult`, or the one new `_simulate_indexed_trade` branch, §5.2). No change to `_simulate_indexed_trade` except the optional vol-reaccel branch.
3. **Causality unit tests (gate everything else).** Truncation-invariance test (`max_index ≤ t`); append-future-bars invariance; reversed-series identity; per-symbol independence; +1-bar delayed-copy degradation test; backtest↔`continuous_demo` state-init parity. All must pass before any sweep.
4. **Pre-registration receipt** committed to `docs/preregistration/2026-06-03-continuous-halflife-fade.md` (§8.1), same PR as the estimator code, with the §8.2 grid and §2 falsifiable predictions frozen.
5. **Cache rebuild** per §8.5 (clear engine caches, rebuild klines/rmom via `reconcile.sh`, verify `full_pit_universe_pass=True`).
6. **Full sweep, both venues** — estimator × `vol_decay_frac` × `halflife_min_mult` × `time_stop_mult` × `tp_frac` × `stop_vol_mult` × `liq_turnover_min` × sizing, on `bybit_full_pit` and `binance_full_pit`, train segment only. 16 cores → parallel over cells. All runs marked EXPLORATORY until pre-reg verdict.
7. **Report + Tier-2 analysis** through the existing path (`_additive_summary`, `_portfolio_mtm_equity`, split, BTC PNG); run `scripts/r1_robustness.py` per the `research-phase-runner` skill; apply the cost-stress ladder; run the smell-test gate and RED-FLAG protocol on any "too good" cell.
8. **Lock the winner on the held-out OOS segment** (untouched until now), confirm cross-venue sign + the §8.4 thresholds, write the binding verdict into the pre-reg receipt, update STATE.md.
9. **Live wiring (only if Tier-2 passes).** In `continuous_demo.py`: half-life trigger replaces `select_continuous_entries` (`:434`) / `build_confirmed_entry_state` (`:216`); exits replace `left_decile` (`plan_continuous_exits` `:530`, `:577`) with target/time/vol-reaccel reusing `_protective_exit_reason` (`:505`) + `plan_protective_exits` (`:587`); apply the live PIT/`listing_age_days` substitution (§3.4). The daemon gets simpler (no decile state snapshot for exits). Forward demo is the only Tier-3 arbiter; the account stays on demo.

**Punchline:** the engine, cost model, funding, and accounting are all sound and stay. The only thing that died — the always-in, leak-driven D9 entry — is exactly the layer the half-life thesis replaces, and the replacement's *sparsity* (clearing ~26–36bps round-trip) plus its *causal running-peak trigger* (closing the leak surface) are what make the rebuild economically and methodologically viable.
