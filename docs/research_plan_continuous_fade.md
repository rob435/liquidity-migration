# Research plan — the CONTINUOUS liquidity-migration fade (always-on, any-hour)

**Author:** claude (operator-directed, 2026-05-31). **One focus, many avenues.**
**Status:** OPEN — a fresh forward program. Nothing here is validated; every cell is a
pre-registration candidate. The deployed daily strategy (age300 + ff6 on demo) is the baseline
to BEAT, not to abandon.

> Read first: `docs/research_summary.md` (the dated record, esp. §c2b / §i1b / §I2 / §CV1 / §RD1),
> `docs/intraday_burst_synthesis.md`, and the `backtest-integrity` skill. This plan assumes them.

---

## 0. The one-paragraph thesis

The deployed strategy is a **daily** liquidity-migration short: detect a daily event (a coin that
climbs the liquidity-rank on a turnover surge — a pump/squeeze), then short the **multi-day fade**,
entering next-day at ~**01:00 UTC** (the daily close + a 1h leakage guard). It is all-weather
positive cross-venue. The 01:00 entry is not a magic hour — **it is a clock-PROXY for a STATE**:
"the event has happened, the multi-day fade is starting, and the crowd's funding spike has passed."
**A continuous system replaces the clock with direct measurement of that state and shorts at
whatever hour it becomes true.** That is the entire program. It is also how a real continuous book
thinks (condition on state, not the wall clock). Two prior attempts to do this failed — and a
cheap probe just told us *exactly why*, which narrows the design honestly.

---

## 1. The foundational exhibit — the entry-delay probe (2026-05-31, EXPLORATORY)

Before planning, we ran the one experiment that could kill the most seductive (and wrong) version
of "continuous." On the i1b/i2 **intraday burst-short** we swept the entry delay after the causal
exhaustion signal, with fair funding-to-exit accounting (bybit, EXTREME subset, hold 24h / stop 25%):

| entry delay | gross fade % | funding % | **NET %** | early | recent |
|---:|---:|---:|---:|---:|---:|
| +1h (0) | **+0.56** | −0.48 | **+0.09** | −0.08 | +0.35 |
| +2h | −0.23 | −0.48 | −0.71 | −0.92 | −0.38 |
| +6h | −0.98 | −0.41 | −1.39 | −1.40 | −1.37 |
| +12h | −2.62 | −0.34 | −2.96 | −2.65 | −3.46 |
| +24h | −4.32 | −0.26 | −4.57 | −3.85 | −5.72 |
| +48h | −6.60 | −0.12 | −6.72 | −5.49 | −8.69 |

Binance confirms the *shape* cross-venue (fade at +1h, gone by +2h):

| entry delay | gross fade % | funding % | **NET %** | early | recent |
|---:|---:|---:|---:|---:|---:|
| +1h (0) | **+0.57** | −0.01 | **+0.56** | +0.54 | +0.60 |
| +2h | −0.14 | −0.02 | −0.15 | −0.09 | −0.27 |
| +6h | −0.72 | −0.01 | −0.74 | −0.31 | −1.51 |
| +48h | −5.95 | −0.00 | −5.96 | −4.75 | −8.19 |

*(probe: a /tmp script reusing the FROZEN `scripts/i2_burst_backtest.py` selector unchanged — only
the fill timing varied; both venues run, same shape.)*

**What it proves.** Funding cost *does* decay with delay exactly as hypothesized (−0.48 → −0.12).
**But the intraday fade collapses far faster and inverts** (+0.56 → −0.23 by +2h → −6.6 by +48h):
the intraday giveback is a **~1-hour** phenomenon; wait longer and you short into the *rebound*.
The two curves never cross favorably.

**Two things to notice on binance.** (1) The fade-collapse is identical cross-venue → the reframe is
robust, not a bybit quirk. (2) Binance `funding% ≈ 0` while bybit's is −0.48 — **binance is
funding-blind** (the `binance_usdm_funding` dataset is sparse in this root), which is exactly why
binance's +1h net looks all-weather-positive (+0.56, early +0.54) where bybit's barely clears zero.
That optimism is an *artifact of missing funding*, not a real edge — a live exhibit for why **Avenue
F4 (wire binance funding)** gates every binance verdict.

**What it kills (do NOT revisit — see §2 OUT):** "short the intraday burst, but enter later to
dodge funding." Dead. The fast intraday fade is **not** the daily edge.

**What it teaches (the spine of this plan):** the daily strategy's edge is a **SLOW, multi-day
mean-reversion** of liquidity-migrated names — a different object from the fast intraday burst.
The continuous system must target the **slow event**. Funding is *survivable* for a multi-day hold
(the daily strat proves it); funding-timing is **not** the boss battle.

---

## 2. Scope

**IN:** a continuous (any-hour, always-on) short on the **slow** liquidity-migration fade — the
c2b "rolling event" object — made all-weather and right-signed.

**OUT (settled; do not burn cycles here):**
- Funding-timed *delay* of the intraday burst-short — **killed by §1**.
- Shorting the *fast* intraday giveback at all — it's a ~1h move that inverts.
- Relaxing the PIT / all-weather / cross-venue / funding-to-exit bars (those are correctness gates,
  see §7). c2b died honestly on the all-weather bar; a continuous winner must clear it too.
- Re-deriving the daily strategy — it's deployed; it is the **control**.

---

## 3. The two boss battles (the organizing spine)

Every avenue below attacks one or both. A continuous winner must beat **both**:

- **BB1 — WRONG SIGN.** Raw c2b shorts *continuers*: the rolling signal reads as momentum while
  the pump is still running, so it shorts the wrong moment (inverse of the daily). The daily strat
  avoids this for free by **waiting for the daily close to confirm the pop happened.** Continuous
  needs a *causal confirmation that the slow fade has STARTED* — a state, not a clock. (Avenue B.)
- **BB2 — RECENT-ONLY.** Age-gated c2b flips positive on the full window but is **regime-conditional**
  (negative early 2023-25, positive only in the 2025-26 alt-bear). The daily strat is all-weather.
  *Why?* — and can a reformulation or regime-gate restore all-weather, or is the continuous edge
  intrinsically regime-conditional? (Avenue C.) **This is the harder battle and the likely make-or-break.**

If BB2 is unfixable, the honest fallback is a market-neutral L/S expression or a regime veto — or
the verdict that the daily cadence is load-bearing and continuous is a null (also a valuable result).

---

## 4. The avenues (workstream catalog)

Each avenue is a self-contained workstream; the phased roadmap (§5) sequences their experiments.

### A — Continuous slow-event detection (the candidate pool)
*Q: define "an event is happening" PIT-causally at any hour, reproducing the daily event's selection.*
- **A1 (cheap, read-only):** audit `_filter_liquidity_migration` (volume_events_filters.py) +
  `_liquidity_migration_event_is_candidate` → map every daily gate (rank-Δ≥150, turnover-ratio≥6,
  residual-return≥0.08, age≥300, market context…) to a causal rolling feature in `signal_harness.py`;
  flag any gate with no PIT-safe rolling equivalent or binance-missing (OI/taker). *Kill: >20% of the
  pass-rate has no rolling equivalent, or it's not causal.*
- **A2 (medium):** build `scripts/a1_rolling_event_detector.py` — fire the rolled gates hourly on a
  168h window; measure **precision** (does it fire *before* the daily 01:00 entry?) and fire-rate vs
  the daily event ledger, cross-venue, early/recent. *Kill: precision <60% either era, or fires >4h
  before the daily event (look-ahead), or fire-rate >95% (too loose).*
- **A3 (expensive, gated on A1+A2):** engine-grade rolling-event backtest (entry on first fire,
  age+rmom gated, hold 3d, full-PIT, funding-to-exit) → MAR vs the daily baseline, both venues, both eras.
- Open: exact causal lag of each rolling feature; is the recent-only an event-*clustering* artifact
  (same coin fires many hours/day → pseudo-concentration)?; binance OI/taker NULLs.

### B — The confirmation/exhaustion gate (fixes BB1, wrong-sign)
*Q: what causal rolling signal marks "the peak has passed, the slow fade has STARTED"?*
- **B1 (cheap):** rolling-signal **inflection** — short only AFTER the rolling composite peaks/rolls
  over (not while it's rising). *Kill: doesn't flip the early-period sign.*
- **B2 (cheap):** **liquidity-rank fallback** as the exhaustion proxy — short when a name that climbed
  rank starts falling back. *Kill: rank-Δ IC vs forward fade <|0.1| or contemporaneous-not-leading.*
- **B3 (cheap):** **OI-unwind + funding-reversal** confirmation (i1a fingerprint: OI builds into the
  peak then rolls off; funding flips). bybit-only (OI). *Kill: >30% NaN or no early-period lift.*
- **B4 (medium):** multi-feature ensemble (close-location/wick rejection + momentum deceleration).
  *Kill: ensemble not distinguishable from best single gate (bootstrap p5 overlap).*
- **B5 (cheap):** causal-vs-look-ahead audit of every candidate gate (the make-or-break correctness check).

### C — Regime-conditionality / all-weather (fixes BB2, recent-only) — **the crux**
*Q: WHY is continuous recent-only when daily is all-weather? Entry timing, hold, universe, or discretization?*
- **C1 (cheap):** decompose — is the rolling signal momentum-like in bull / fade-like in bear (the
  sign literally flips by regime)? Bucket forward returns by BTC/market regime.
- **C2 (medium):** a BTC/market-regime gate (e.g. disable shorts when `market_pct_up_1d`>0.55) — does
  on/off switching make it all-weather? *Kill: regime-filtered MAR still early-negative either venue.*
- **C3 (medium):** regime-aware **hold adaptation** (the fade duration may itself be regime-dependent —
  longer holds in bull, shorter in bear).
- **C4 (expensive):** is the early-negative driven by the rolling window itself (a 168h trailing
  return is a momentum proxy in bull markets) vs the market? Residualize via `risk_model.py`.
- **C5 (cheap):** is it a **universe-composition artifact** — continuous on the full universe eats the
  worst young squeezers that the daily **age gate** removes? Run continuous WITH age300+rmom from the
  start. *If age+rmom alone makes rolling all-weather, BB2 is mostly solved and we jump to A3/H3.*
- Open: is the **long** side of the c2b L/S all-weather even if the short isn't (→ market-neutral
  expression)? Is recent-only just a high-N exposure-to-drawdown artifact of the dense signal?

### D — The discretization advantage (what the clock GIVES) — novel
*Q: what does daily-close aggregation + the +1h buffer buy that continuous loses, and can we synthesize it?*
- **D1 (cheap):** measure intraday **re-trigger / whipsaw** — does the same event fire many hours/day?
- **D2 (medium):** per-name **debounce / tighter cooldown** (cooldown_hours exists) → does it recover
  daily-pool stability without killing breadth?
- **D3 (medium):** **signal smoothing** (rolling-median) to replicate daily aggregation.
- **D4 (medium):** **K-bar confirmation hysteresis** (the daily +1h is a 2-bar confirm) — does requiring
  N persistent bars before firing sidestep the continuation squeeze?
- **D5 (cheap):** sub-daily cooldown sweep (24h…168h vs the daily 5d).
- Deep open: is the load-bearing thing the cooldown, or the **information boundary** itself (daily
  signals consume exactly 24h of aggregated turnover/market cycle)? If the latter, continuous can't
  fully replicate it and that *is* the answer.

### E — Hold horizon & continuous exit for the slow fade
*Q: how long is the slow fade, and how to exit it continuously?*
- **E0 (cheap):** slow-fade horizon ceiling — from the deployed ledger, the per-trade peak-to-trough
  fade duration and where the optimal exit sits (confirm it's multi-day, not 24h). *Kill: median <12h
  → it IS intraday-dominant and this whole premise is wrong.*
- **E1 (medium):** generalize ff6 → a **giveback-target** exit (close when MFE decays to 40-60% of peak,
  or close retraces to ±5% of entry).
- **E2 (medium):** **momentum-reacceleration** exit (re-enter the fade if velocity turns back down).
- **E3 (medium):** **rank-recovery** dynamic hold (exit when the shorted name recovers liquidity rank).
- **E4 (expensive):** funding-aware dynamic hold (cap hold when a slow-fade short becomes crowded).

### F — Funding & cross-venue for the SLOW fade
*Q: funding for a multi-day hold is an accrual problem (not the §1 fast-decay one) — when do we pay vs receive?*
- **F1a (cheap):** funding accrual profile over multi-day holds (does the daily lateness buy a lower tail?).
- **F1b (medium):** an **entry-state funding gate** (`--funding-filter-floor` already exists) — short only
  when trailing funding ≥ FLOOR (not deeply-negative = not crowded). Does it convert c2b to all-weather?
- **F1c (medium):** cross-venue funding-state confirmation (both venues in positive funding regime at entry).
- **F2 (cheap):** decompose the deployed daily strat's realized funding drag (how much is the lateness worth?).
- **F4 (medium):** **wire binance funding** (`binance_usdm_funding`) — binance is currently funding-blind/
  optimistic (see §1); this changes every binance verdict. High-value infra unblock.

### G — Continuous book construction & capacity
*Q: when batch entry becomes continuous, how do sizing/de-concentration/capacity change, and what breaks?*
- **G1 (cheap):** per-name caps under overlapping same-symbol fires (max_active=12 assumes no re-entry-in-hold).
- **G2 (medium):** re-entry/overlap mechanics under continuous firing.
- **G3 (medium):** concentration drift under conviction-weighted sizing.
- **G4 (medium):** crowding under simultaneous entry (staggered vs batch) — the §1 funding lesson at book level.
- **G5 (medium):** all-weather stress of the *book* (not just the signal) on age/funding-regime splits.
- **G6 (cheap):** operational capacity — continuous firing could 4-24× the entry rate; venue/API limits.

### H — Validation architecture & guardrails
*Q: what gates and PIT discipline keep this out of the c2b recent-only trap, backtest→paper→demo→OOS?*
- **H1 (cheap):** rolling-feature PIT audit (0 entry-delay legal ONLY because the trailing window is causal).
- **H2 (medium):** rolling backtest of the i1b burst signal, 24h hold, fair funding (the §1 control, re-confirmed).
- **H3 (medium):** rolling backtest of age+rmom gates, rolling entry — the most likely first winner.
- **H4 (expensive):** same-code reconcile backtest↔paper↔demo (the #16 "unreconciled live drift" trap).
- **H5 (cheap):** pre-committed decision gates (Tier-2 backtest → paper reconcile → demo OOS → Tier-3
  residual Sharpe ≥ +0.3). Use `scripts/apply_decision_rule.py` / `r1_robustness.py`.

---

## 5. Phased roadmap (cheap → expensive; each phase gates the next)

**Phase 0 — Reproduce & diagnose (all CHEAP, read-only — do FIRST):**
`Reproduce c2b on full-PIT both venues` (the recent-only baseline we must beat) · **A1** (feature
audit) · **C1 + C5** (why is it recent-only — regime vs universe-composition) · **B5 + H1** (PIT
causality of every candidate feature) · **E0** (confirm the fade really is multi-day) · **F2** (the
daily strat's funding decomposition).
**Gate G0:** Do we understand BB2's root cause, is the feature set buildable + causal, and is the
fade confirmed multi-day? *If C5 shows age+rmom alone makes rolling all-weather → skip to Phase 2 / H3.*

**Phase 1 — The confirmation gate + the all-weather fix (MEDIUM):**
**B1-B4** (does a confirmation gate flip the sign — BB1) · **C2-C3** (regime gate / adaptive hold — BB2) ·
**A2** (build the detector, precision audit) · **D1-D5** (does discretization-replication recover all-weather) ·
**F1b** (funding entry-state gate).
**Gate G1:** Does a confirmation-gated rolling signal (a) fix the wrong sign AND (b) clear the
**all-weather both-venue both-era** bar? *If still recent-only after B+C+D+F1b → the continuous edge is
likely intrinsically regime-conditional; pivot to market-neutral L/S or a regime veto, or FILE THE NULL
("the daily cadence is load-bearing") — itself a publishable, cycle-saving result.*

**Phase 2 — Engine-grade rolling backtest (EXPENSIVE; only if G1 passes):**
**A3 / H2 / H3** (the continuous backtest, age+rmom gated, funding-to-exit, full-PIT) · **E1-E4**
(slow-fade exits) · **G1-G5** (book construction) · **F1c / F4** (cross-venue + binance funding).
**Gate G2 (Tier-2 demo-candidate):** all-weather both venues, pooled MAR ≥ daily − 0.5, funding-costed,
LOO/bootstrap-stable, cross-venue positive. Pre-register before running.

**Phase 3 — Forward demo (operator-gated, EXPENSIVE):**
**H4** (same-code reconcile backtest↔paper↔demo) · **H5** (pre-committed gates) · forward OOS →
**Tier-3** residual Sharpe ≥ +0.3. The forward demo is the only real arbiter; no internal OOS exists.

---

## 6. Where to start (for the next agent)

Do **Phase 0** first, and within it, **the single highest-information experiment is C5 cross-checked
against C1**: re-run the c2b rolling signal on full-PIT, both venues, with the **age300 + rmom** gates
applied from the start, split early/recent. Two outcomes, both valuable:
- If it's **all-weather** → BB2 was a c2b universe/decile artifact; the continuous edge is real and you
  fast-track to the engine-grade rolling backtest (A3/H3). This is the optimistic, high-payoff branch.
- If it's **still recent-only** → BB2 is structural; pivot to Avenue C's regime gate / market-neutral
  expression and Avenue D's discretization question. Either fixes it or files the (valuable) null.

This costs one cheap read-only run and tells you which world you're in.

---

## 7. Methodology guardrails (non-negotiable — these killed c2b honestly)

- **PIT-causal rolling features.** Entry-delay may be 0 ONLY because the trailing window is already
  causal; every feature must use closed bars (`shift(1)`), no current/future bar. Audit per `backtest-integrity`.
- **All-weather is the hard gate.** Early AND recent positive, on BOTH venues. c2b's recent-only is the
  exact trap; do not promote a recent-only candidate.
- **Funding-to-exit** fair accounting (charge funding to the actual exit, not a fixed window). bybit
  funding is real; **binance is funding-blind/optimistic** until F4 wires it.
- **Cross-venue** is the robustness axis (CV1: the per-trade edge is venue-general; the gap is breadth).
  bybit (funding-real) is the anchor.
- **Pre-registration** for every cell touching a working dataset (`docs/parameter_pre_registration.md`);
  EXPLORATORY runs may skip it but can NEVER be cited as promotion evidence.
- **Three-tier demo-arbiter** (STATE.md): backtest = prior, forward demo = Tier-3 arbiter, nothing to
  real money. Full-PIT roots mandatory; the 16 GB research box runs one full-PIT cell at a time.

---

## 8. Substrate index

- **Selector/engine:** `liquidity_migration/volume_events.py` (daily engine; has a rolling-window signal
  option + `cooldown_hours` sub-daily field), `volume_events_filters.py` (`_filter_liquidity_migration`),
  `volume_events_features.py` (rolling builders), `signal_harness.py` (feature builders).
- **Intraday substrate:** `scripts/i1b_burst_separation.py` (exhaustion features), `scripts/i2_burst_backtest.py`
  (FROZEN intraday engine; the §1 probe added `--entry-delay-h` in a /tmp copy — re-add upstream if extending).
- **Gates:** age (`pit_age_days_min`), rmom (`scripts/precompute_residual_momentum.py`,
  `liquidity_migration_residual_momentum_max`), ff6 (`_failed_fade_exit_hit` / live `_failed_fade_exit_since_entry`),
  funding-state (`--funding-filter-floor`).
- **Validation:** `scripts/r1_robustness.py`, `scripts/apply_decision_rule.py`, `scripts/reconcile.sh`,
  `risk_model.py` (Tier-3 residual), the `research-phase-runner` / `pit-reconcile` / `backtest-integrity` skills.
- **Record:** `docs/research_summary.md` (§c2b, §i1b, §I2/I2k, §CV1, §RD1, §E2/P3b), `docs/intraday_burst_synthesis.md`.

---

## 9. The bet, in one line

> The daily 01:00 entry is a clock-proxy for "the slow fade has started & the funding crowd has left."
> Measure that state directly (a causal confirmation gate) and the only thing standing between us and a
> continuous, any-hour book is whether the edge is all-weather or just a recent-regime bear bet — and
> Phase 0 tells us which in one cheap run.
