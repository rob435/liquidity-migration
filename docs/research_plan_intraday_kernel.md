# Research plan — the intraday-detection kernel (fast WS reaction to the discrete event)

**Created 2026-05-30.** Supersedes the prior selection-execution (E1→E2→E3) and part-2
research plans — that round concluded (E1 falsified the execution premium, E3/sniper
dropped) and its verdicts are consolidated in `research_summary.md`. Methodology gate:
`backtesting_errors_we_never_repeat.md`. Pre-registration: `parameter_pre_registration.md`.
Decision rule: the three-tier demo-arbiter in `STATE.md`. **All runs are on the 5950X
full-PIT roots** (the 16 GB box can't hold a full cell, ~23 GB; `SWEEP_MAX_WORKERS=1`).

## The hypothesis

The discrete liquidity-migration **event** is the all-weather selector — it is
threshold-triggered and sparse, and (unlike the rank-all continuous decile, which tested
regime-conditional and is rejected — the c2b verdict, see `research_summary.md`) it is
positive in the **early** period too. Today it fires on the **daily-close roll**: one
actionable entry per UTC day per name.

> **Kernel hypothesis:** detecting the event **intraday**, off the live WS stream (rolling
> event features), and entering immediately captures more of the fade — improving the
> strategy **robustly, on both venues, including the early period.**

Keep everything proven — the discrete event selector + the **age gate** (`pit-age-days-min≈300`)
+ the **residual-momentum gate**. Change exactly one thing: **detection latency** (daily-close
→ intraday). Execution stays **immediate** (E1: timing-*within*-a-bar is a non-lever — the
lever under test is *detection* latency, not *fill* latency).

## Explicitly OUT of scope (already settled — do not revisit)

- **Rank-all continuous decile (C0 / Architecture B):** regime-conditional, rejected. The
  kernel keeps the sparse event trigger, NOT a dense always-on rank.
- **Fade-confirmation execution (`promoted_quality_squeeze`) + sniper fill-timing (E3):**
  non-lever (E1). The lever is detection latency, not how fast the fill lands.

## Phases (cheap → expensive; each phase gates the next)

### K0 — Upside-ceiling characterization (CHEAP, read-only, run FIRST)
Read-only analysis on the existing validated daily-event ledger (both venues). For each event
name, reconstruct the intraday price path between the intraday pump-peak and the daily-close
detection bar, and measure **how much of the eventual giveback is already gone by the time the
daily-close entry fires.**
- **Output:** the distribution of "fade already captured by daily-close entry" vs "fade still
  available," both venues, split **early (2023-04→2025-06) / recent (2025-06+)**.
- **Gate → K1:** a material chunk of the giveback is systematically lost to daily-close
  lateness, on **both venues including the early period.**
- **Falsifier (STOP):** if the daily-close entry is not systematically late (most of the fade
  is still ahead) → detection latency is a non-lever (consistent with E1) → do not build.
  File the negative verdict; the discrete daily strategy + age gate stands.
- **Cost:** a precheck script (read-only, in the `c1/c2/c2b` style). No engine build.

### K1 — Intraday-detection backtest (MEDIUM, only if K0 passes)
Build the rolling intraday event-feature pipeline (compute the event features on a trailing
window so the event can fire intraday, **PIT-causal — no within-bar look-ahead**) and backtest
the intraday-detected, age+momentum-gated strategy under the realistic engine (capped fills,
15 & 45 bps, full-PIT) **vs the daily-close baseline**, both venues.
- **Decision:** `scripts/r1_robustness.py` Tier-2 verdict + fragility (bootstrap p5, LOO,
  sub-period thirds). Pre-register before running.
- **Falsifier (STOP):** the lift is regime-conditional (recent-only) like c2b, OR there is no
  robust cross-venue MAR Δ over the daily-close baseline → do not build the live engine.

### K2 — WS live-detection engine + forward demo (EXPENSIVE; explicit operator go ONLY)
The ~5–7 day build: production WS intraday detection running the **same code path** the K1
backtest used (same-code #16), PIT-safe, then forward-demo it. **Hard line:** not started
without an explicit operator go AND K0+K1 passing. The forward demo is the Tier-3 arbiter.

## Guards (the lessons baked in)

- **Both venues + early/recent split on every phase** — the c2b trap: a recent-bear-only edge
  is a regime bet, not an edge.
- **Keep the proven selector** — don't swap the sparse event for a dense rank; don't relax the
  age / residual-momentum gates.
- **PIT / causality** — intraday features must be causal at the detection instant; no
  within-bar look-ahead (backtesting-errors #2 / #13).
- **Same-code** — the forward demo must run the identical detection code as the K1 backtest (#16).
- **Don't block the ready thing** — forward-demo the already-validated discrete + age gate in
  parallel; this kernel is exploration, not a dependency.

## Status

- **K0: DONE — PASS (2026-05-30).** The daily-close (+1h) entry is a median **~8–11% below
  the event-day intraday peak**, positive on **both venues × both early/recent splits ×
  both books** (fixed_delay age≥90 and age≥300 quality_squeeze) — clears the ~15 bps gate by
  ~50–65×. Decomposition (K0b): **~95–98% of that gap is the within-day-D giveback**, the +1h
  fill window only ~2–6% → the lever is **intraday detection**, not faster fills (consistent
  with E1). It is an optimistic *ceiling* (exact-top short) → necessary, not sufficient.
  Receipt: `docs/preregistration/k0-intraday-fade-timing-2026-05-30.md`.
- **K1: DONE — FAILED (K1a, 2026-05-30). The kernel is FALSIFIED; K1b + K2 cancelled.**
  K1a (cheap feasibility, `scripts/k1a_intraday_first_crossing.py`) found the first intraday
  hour the *same selector* fires and the realistic uplift (fill at `h*`+1 vs the daily +1h
  entry). Result: only **~40–85 bps median (~10–15% of the K0 ceiling)**, a **coin flip**
  (45–46% negative, q25 ≈ −3% / q75 ≈ +5%), **negative on binance early**, `corr(lead,uplift)
  ≈0`. **Mechanism:** the selector cannot *confirm* the event (cumulative turnover ≥6×) until
  median `h*` = 15–16:00 UTC — ~9h after the morning peak — by which point price has faded back
  to ≈ the daily entry; confirmation time is decoupled from the price path. The K0 ceiling is
  real but **un-capturable by faster detection of the same event.** Receipt:
  `docs/preregistration/k1-intraday-detection-2026-05-30.md`.
- **K2:** CANCELLED (gated on K1, which failed).

## Outcome — the timing axis is closed; the alpha is SELECTION

E1 killed *fill* timing (within 1h); **K1a kills *detection* timing** (across ~9h, same
selector). Neither faster fills nor faster detection of the same event is a lever — the
daily-close cadence leaves no capturable money on the table because the event cannot be
confirmed any earlier. **The program reverts to its validated SELECTION refinements** — the
age gate (Tier-2, all-weather) + the residual-momentum gate (P3b, demo-eligible) — under
forward demo (the operator-gated arbiter).

## I-phase — purpose-built intraday selector (operator-directed reopening, 2026-05-30)

The operator (correctly) flagged that K1a was too narrow: it tested the *daily selector run
hourly*, not a **purpose-built intraday signal** firing on rate/flow features at the peak — a
genuinely different signal needing custom engineering. Reopened.

- **Channels (verified data):** cross-venue all-weather = klines (price/volume/intrabar/velocity)
  + premium_index (hourly) + funding (8h) both venues + market-context; OI bybit-only;
  taker-flow binance-recent-only (out). 1h grain.
- **I1a (DONE):** faders carry a clear cross-venue intraday **exhaustion fingerprint** — peak
  ~16–17 UTC, turnover climax ~4.2–4.6× day-mean at the peak then rolloff, peak-bar upper-wick
  ~0.43 + mid-range close (rejection), price +20–22% then fades ~6–8%/6h; OI builds into + surges
  after the peak (bybit); premium quiet. Fingerprint EXISTS (necessary). `scripts/i1a_fader_intraday_signature.py`.
- **I1b (DONE — PASS).** Scanned ALL intraday rate-bursts (age≥300, rank 31–400, gain≥8% +
  vol-spike≥5×, cooldown 3d, fwd 48h) over BOTH venues incl. non-events (bybit 8968 / binance 7912),
  labeled forward fade-vs-continue, tested separation + **beta-neutralized**. Result: separation
  SURVIVES beta-neutralization (idiosyncratic, not market-regime beta), robust **cross-venue +
  early/recent** — `idio` (pump size vs market) ic_neutral −0.28…−0.31, velocity/vol-spike/accel
  −0.11…−0.16, wick = noise. Edge is a SELECTION on pump-extremity (extreme quintile beta-neutral
  short +1.2–1.3% early / +4.4–4.7% recent gross 48h; all-bursts ~breakeven). `scripts/i1b_burst_separation.py`.
- **I2 (DONE) — REAL, promising, cross-venue, all-weather lead with a WIDE stop; NOT validated.**
  Backtested the extreme-burst short (`scripts/i2_burst_backtest.py`). The daily 12% stop is too tight
  (38% stop-out → fails, recent-only); fade-confirm (I2b) didn't rescue (no selectivity). The **stop-width
  frontier** is monotonic 12%→50%: **30% → MAR 3.2/2.79 DD 14.5/10.7% all-weather both venues**; 50% →
  MAR 4.9/8.0 DD 16/9%; no-stop best-return but tail blows out (DD 26/20%). A wide (30–50%) stop monetizes
  the signal with bounded tail. Caveats (not validated): Stage-B proxy; wide-stop gap/fill realism;
  funding unmodeled; back-loaded; STR-factor open. Receipt: `docs/preregistration/i2-intraday-burst-selector-2026-05-30.md`.
- **I2c/I2d (2026-05-31) — operator capped the stop at ≤20–25% ("more fade, not the top"; entry
  refinements). Under a TIGHT stop, the standalone short does NOT hold up.** Every entry — top-short,
  % giveback 3–20% (I2c), momentum-confirmed sustained fade (down-bars=2, I2d) — is **early-negative /
  recent-only both venues** (best give-5%/stop-20%: EARLY net45 −0.46 bybit / −0.03 binance). The
  early-negative is structural/regime (bull-market continuations squeeze a tight-stopped short); entry
  timing doesn't fix it. The 30–50% all-weather result was a fragile-fill + untenable-stop artifact.
  `scripts/i2b_burst_fade_confirm.py`.
- **I-PHASE CONCLUDED — verdict:** the intraday extreme-burst signal is **real** (I1b: beta-neutral,
  cross-venue, all-weather *unstopped*) but **NOT safely deployable as a standalone short under
  realistic risk** (recent-only at a tight stop). **Deep reconciliation:** the daily strategy's "late"
  next-day entry is WHY it is safely all-weather — it sidesteps the intraday squeeze; the K0 ~9% ceiling
  is real but un-capturable *safely*. The daily fade-confirm IS the safe harvest of this effect. **Robust
  all-weather edge stays the daily age-gated + residual-momentum strategy** regardless of the below.
- **I-PHASE VERDICT REVISED (2026-05-31, the operator's full ≤25% cap — I'd only tested ≤20% above).**
  Also tested volume-decline-vs-climax + failed-retest/no-new-high (I2e/I2f) — both confirm≈1.0, no help,
  early-negative (entry can't fix the POST-entry re-pump squeeze). BUT the lever is **stop width**: the
  **extreme-burst TOP-short flips all-weather at ~25%** (the operator's cap) — per-trade net45 EARLY +0.13
  bybit / +0.39 binance, RECENT +1.34/+0.51; portfolio MAR net45 3.1/2.2 (net15 5.6/4.3), DD 11–13% (20–22%
  marginal). Fade entries underperform the top-short at the same stop ("more fade" empirically loses).
  **A DEPLOYABLE-CANDIDATE exists within ≤25% = the extreme-burst top-short at a 25% stop** — NOT validated
  (Stage-B proxy; back-loaded first-third −6%/−2%; 25%=boundary + rough adverse hold; mostly STR). **I3
  (engine-grade: true exit-timing/concurrency + bar_extreme_capped fills + FUNDING + risk_model residual,
  stop ≤25%) is now JUSTIFIED — operator-gated go/no-go.** `scripts/i2_burst_backtest.py`.
- **FUNDING DE-RISK (I2g→I2j, 2026-05-31) — funding KILLS the standalone short; I3 NOT recommended.**
  Before the expensive engine build, costed realized funding on the PROXY (the 2nd-biggest caveat after
  fills). Short receives + / pays − funding over the hold (`--funding-ds`, both venues full-history).
  - **I2g/I2h — median survives, portfolio dies.** The funding *mean* looked like a kill (−0.6…−1.5%/trade)
    but was **outlier-distorted** by hourly-funding coins (e.g. LRC −16% over 48 settlements); the **median**
    trade's funding is ~0 (median net45+funding still +3.4/+4.7). BUT the funding-**included PORTFOLIO** dies
    — MAR **−0.91 bybit / −0.73 binance** — because 11–32% of trades sit in **crowded-short** coins (perp
    discount when everyone shorts a fresh pump) paying >1% funding; at 2% book weight that's a broad early drag.
  - **I2i — crowded-short FILTER doesn't rescue it.** A PIT filter skipping coins with negative trailing
    funding *at entry* (floor 0 / −0.0003) helped (MAR → −0.46 bybit / −0.15 binance) but stayed **negative,
    early-negative both venues** — because the funding accrues **during** the hold (shorts crowd in *as* the
    pump fades), which an entry-time filter cannot predict.
  - **I2j — SHORTER HOLD doesn't rescue it.** Funding ∝ time-short, and the fade is front-loaded (I1a), so
    12h/24h holds were the last genuine lever. Result: cutting the hold cuts the funding drag **and the gross
    edge proportionally** (median net45+funding 48h +3.4/+4.7 → 12h +2.3/+3.2) — **every** hold×venue cell is
    MAR-**negative**: 12h −0.69/−0.23, 24h −0.54/−0.09, 48h −0.91/−0.73. No free lunch.
  - **ROBUST FINAL VERDICT — the standalone intraday burst-short is NOT deployable.** TWO independent realistic
    costs each kill it: (1) the **tight-stop squeeze** (early-negative below ~22%, boundary even funding-blind
    at 25%); (2) the **crowded-short funding drag** during the hold (funding-included portfolio negative at
    *every* hold, both venues, filter-resistant). The signal (I1b) is real but the short *execution* is too
    expensive: you pay the crowd (funding) **and** get squeezed by continuations (stop) — the thin edge can't
    cover both. Caveat (stated both ways): the proxy slightly **over**counts funding for the ~15% stopped
    trades → the precise engine number is marginally less harsh, but cannot flip an early-negative, MAR-negative
    portfolio all-weather. **I3 is therefore NOT recommended** (expensive build, ~nil chance of flipping the
    sign). **The robust validated all-weather edge stays the DAILY age+rmom strategy** — whose late next-day
    entry is now understood to sidestep BOTH the intraday squeeze AND the worst of the crowded-short funding.
    The intraday arc is **closed**. `scripts/i2_burst_backtest.py` (`--funding-ds`, `--funding-filter-floor`).
