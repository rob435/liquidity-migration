# Research summary — liquidity-migration strategy

**Updated 2026-05-29.** Single consolidated research record (replaces the deleted Round 1 +
Round 2 per-phase docs; originals in git history). Methodology standard:
[backtesting_errors_we_never_repeat.md](backtesting_errors_we_never_repeat.md).

## The core distinction: SELECTION signal vs EXECUTION signal

The strategy has two separable layers, and conflating them caused the wrong Round-2
conclusions:

1. **Selection (initial signal) — *which* names are candidates.** A liquidity-migration
   event: a mid-liquidity perp (rank 31–400, top-30 excluded) takes in price-insensitive
   flow — turnover ≥6×, ≥150-place rank climb, modest residual return ≥8%, strong close,
   top-10% of events excluded. This identifies a *candidate pool*. It is **not** an entry.
2. **Execution (entry signal) — *when* to short.** The thesis is the in-migrated flow
   **exhausts and fades**. You do **not** short at the top (the pump can continue). You wait
   for the fade to **confirm**, then enter — "**fade the fade**." The deployed execution
   (`promoted_quality_squeeze`, volume_events.py:1003–1036) already does this: track the
   post-event high, require a **pop**, then enter the short on the **giveback** from that
   high. Selection ≠ execution.

**Why this matters / the bias we removed.** The momentum-continuation finding (the extreme
post-event names *continue up* +26/+39 bps before fading) is **not** evidence the strategy
fails — it is the **reason the execution signal must wait for confirmation**. Immediate
entry shorts into that continuation and gets run over; a fade-confirmation entry sidesteps
it. The old "fade-the-pump short thesis fails" root-cause was an artifact of testing
immediate entry.

## Current status (honest)

The earlier **"Round 2 = documented null"** was substantially wrong, for two compounding
reasons:
- **Methodology (pessimistic execution model):** worst-case `bar_extreme` stop fills +
  `max_active=3` over-concentration + a ×3 (45 bps) cost. Fixed: default is now
  `bar_extreme_capped` (10%), and the validated config is `max_active=12`.
- **Selection/execution conflation:** the continuous test (C2) used *immediate* entry, never
  the fade-confirmation execution.

Under the realistic fill + sane concentration, the **daily** strategy is **positive on
both venues in-sample.** It remains in-sample; the forward demo (since 2026-05-22) is the
arbiter; nothing is promoted.

## E1 (2026-05-30): the EXECUTION signal is NOT load-bearing — the alpha is SELECTION

The E1 experiment (+ the E1b knob-engagement robustness probe) tested the thesis directly: on the *same*
selection pool, costs, and concentration (full-PIT, capped10, max_active=12, 15 bps,
both venues, 2023-04→2026-05), vary only `--entry-policy`: A `fixed_delay` (immediate
+1h) vs B `promoted_quality_squeeze` (fade-confirmation).

| venue | A immediate (fixed_delay) | B fade-confirm (quality_squeeze) | premium B−A |
|---|---:|---:|---:|
| bybit | **+67.3% / MAR 2.76 / Sh 1.07** | +71.2% / MAR 2.93 | +0.17 MAR (LOO-flips, recent-only) |
| binance | **+8.0% / MAR 0.28** (funding-missing) | +7.3% / MAR 0.25 | −0.03 MAR (sign-flip) |

**Verdict: SELECTION-DOMINANT.** The fade-confirmation execution adds nothing robust —
pooled MAR Δ ≈ +0.01 (Tier-2 `descriptive`), it sign-flips across venues, the bybit
premium is a fragile recent-month artifact (LOO flips the sign), and the high-power
paired micro-test on the genuinely time-divergent trades is pure noise (bybit t=+0.35,
binance t=−1.30). The E1b probe forced 6× more engagement (bybit giveback trades 69→267)
and the premium was unchanged → robust to engagement level. **The alpha is the SELECTION
pool + a plain +1h short.** Two reasons the execution layer is inert: (a) `promoted_quality_squeeze`
never *filters* the pool (A and B trade the identical candidates), and (b) at 1h granularity
most "givebacks" complete inside the entry bar, so the timing barely moves (only ~3–9% of
entries differ from immediate). **`promoted_quality_squeeze` ≈ immediate entry in practice.**

**What this corrects:** the 2026-05-29 retraction was right that the old null was a
worst-case-fills + over-concentration artifact (E1 confirms the strategy is strongly
positive — bybit +67% at honest 15 bps). But its framing that the strategy is a SELECTION
*and* an EXECUTION (fade-confirmation) signal — and that execution is the open lead — is
**not supported**. Execution timing is a non-lever at 1h; E3 (sniper) is therefore not
justified (its gate fails). **The open lead is SELECTION refinement.**

## Daily strategy — realistic re-baseline (full-PIT, `bar_extreme_capped` 10%, in-sample 2023-04→2026-05)

| config | venue | stop fill | cost | total ret | max DD | worst day | Sharpe |
|---|---|---|---:|---:|---:|---:|---:|
| baseline, max_active=3 (DEPLOYED) | bybit | `bar_extreme` | 45bps | −32% | −87% | −36% | 0.19 |
| baseline, **max_active=12** | bybit | **capped 10%** | 45bps | **+37.8%** | −27.5% | −4.8% | **0.70** |
| baseline, **max_active=12** | binance | **capped 10%** | 45bps | −4.7% (**gross +16.1%**) | −33.6% | −4.4% | −0.05 |

All rows use the `promoted_quality_squeeze` (fade-confirmation) execution. At the honest
15 bps cost the drag roughly thirds → bybit higher, **binance ~breakeven-to-positive**.
**Both venues are gross-positive.** The top row is the old worst-case (what the "null" was
built on). *(Binance funding is **missing** in these runs — `binance_full_pit` has no funding
dataset wired; label `funding-missing`. Correcting an earlier note: this does NOT understate
binance — E1 shows bybit short funding is a net **−6.2% drag** (modeled), not a credit, so
adding funding would if anything pull binance **down**. The cross-venue gap is real, not a
funding artifact.)*

## The open lead (post-E1): SELECTION refinement, not execution

E1 closed the execution question (above): timing is a non-lever, so E2/E3 pivot away
from execution. (The E1→E2→E3 plan ran its course — E1 triggered the contingency: E2 became a **selection
refinement** study and E3/sniper was dropped. The forward plan is now
[research_plan_intraday_kernel.md](research_plan_intraday_kernel.md).)

Two concrete selection leads:

- **E2 RESULT (2026-05-30): the age gate is a robust cross-venue refinement.** Testing an
  exhaustion-quality gate,
  the winner is **`pit-age-days-min=300` (drop symbols younger than 300 days)**: daily-DD MAR
  **bybit +2.93→+5.96, binance +0.25→+2.81** (return up, DD down on both), all-thirds-positive
  both venues, LOO-stable, bootstrap P(Δ>0) 83%/93%. It **improves the recent weak third on
  both venues** (bybit −2%→+25%, binance −26%→+4%) — refuting the age→early-regime confound and
  **explaining the recent edge-decay** (young-name shorts are systematic net losers — fresh
  listings squeeze; the signal works on seasoned names; verified cross-venue on the ledgers).
  `prior30-max-return-max=0.14` is a secondary cross-venue risk-reducer (halves DD).
  `universe-rank-max=110` (liquidity-tighten) was **rejected** — it hurt both venues (the
  `liquidity_rank` IC was a within-realized artifact the backtest caught). Note: OI / taker /
  funding features were **excluded** — they are NaN on `binance_full_pit`, so the earlier
  exploratory IC's binance derivative-feature cluster was an artifact. **Tier-2 demo-candidate,
  in-sample** — forward demo is the arbiter; deployment/profile change is the operator's call.
- **E2b confirmed the age effect is not a knife-edge**:
  dropping young names ~doubles MAR across age 200/300/400 on both venues, all-thirds-positive,
  recent-third improved both, LOO-stable, bootstrap P(Δ>0) 86–96% (binance age400 p5>0). bybit
  saturates ~age200 (≈MAR 6, flat to 400); binance monotone to 400. `age400` is the joint-best
  (bybit 6.91 / binance 5.64, ample trades); `age300` conservative; `prior30+age` an optional
  DD-reducer (mild bybit fragility). `age-alone` is the primary robust refinement.
- **E2c — the age gate is COST-ROBUST**:
  at 3× cost (45 bps) the baseline degrades (binance baseline goes **negative**, −4.7% / MAR −0.14),
  but the age-gated book stays strongly positive both venues (bybit age300 +71% / MAR 4.04; binance
  age300 +22% / MAR 1.74; age400 stronger). The gate removes losing trades → lower cost drag → the
  edge widens at higher cost. So the discrete age gate is robust to threshold (E2b), regime (E2),
  and cost (E2c) — a thoroughly-validated in-sample Tier-2 demo-candidate. Forward demo is the next gate.
- **E2d — the age gate is FILL-ROBUST + closes the loop on the original null**:
  under worst-case `bar_extreme` wick fills the baseline goes **negative on binance** (−6.8%/MAR −0.18)
  but the age-gated book stays strongly positive (bybit age300 +67%/MAR 3.85; binance age300 +27%/MAR 2.00).
  The original Round-2 "documented null" was worst-case fills + over-concentration on a universe that
  **included young names** (wildest wicks); the age gate (drop them) is the **structural remedy** — it
  makes the strategy survive even the brutal fill assumption. **In-sample validation is now exhaustive:
  threshold (E2b) + regime (E2) + cost (E2c) + stop-fill (E2d).** Forward demo (Tier-3, operator-gated)
  is the only remaining gate.
- **P2-1 (the sobering one): the edge is mostly FACTOR EXPOSURE, not unique alpha**.
  Decomposing the ledgers through the validated 6-factor risk model: the **baseline is pure factor
  exposure** (annualized residual Sharpe strongly *negative* under the robust common-4-factor model
  both venues — its raw Sharpe ≈1.1 is entirely priced premia from shorting high-vol/low-liquidity
  alts). The **age gate sharply reduces factor dependence** (residual jumps massively vs baseline),
  but the age-gated **residual alpha is borderline and not a clean cross-venue Tier-3 pass**: binance
  clears (+0.43–0.52) but bybit is marginal/model-sensitive (+0.27 full6 / −0.12 common4), and the
  √(trades/yr) annualization is optimistic (overlapping trades). So the strategy — even age-gated —
  is primarily a **factor-harvesting vehicle** (still a legitimate demo-candidate at MAR 3–6 / halved
  DD), but the "we found unique alpha" framing is **not** supported by the residual-Sharpe gate.
- **P2-2/3 (residual leaderboard) — the age gate's real value is FACTOR-NEUTRALIZATION**.
  Decomposing every candidate selection config: **no config clears Tier-3 residual ≥+0.3 cross-venue**
  (age residuals sign-flip bybit −/binance +). The biggest *return* lever `drop_all_4` (+295%) is
  **NOT alpha** — negative residual both venues = pure factor harvesting. But under *every* model the
  age gate moves the residual sharply up from the baseline's *inefficient factor bet* (−0.99/−0.42
  common4) toward **factor-neutral** (age400 −0.04/+0.26); **stricter age = more neutral**. So the age
  gate's robust contribution is **stripping factor exposure** (valuable for forward robustness — a
  near-neutral book resists factor-premium decay/crowding), not adding idiosyncratic alpha. Forward-demo
  the age gate as a **robust factor-neutralized short**, not a unique-alpha engine.
- **P3 — residual-momentum selection: a real refinement + a promising (uncertified) alpha lead**.
  Trailing factor-residual momentum (PIT, signal-close `lag1`) **strongly predicts which age300
  candidates are the best shorts, cross-venue** (IC −0.19 bybit / −0.35 binance — short the
  idiosyncratically-weak names), survives the strict-PIT lag, full coverage, holds recent. Selecting
  the low-rmom half lifts the per-trade residual Sharpe above the Tier-3 gate on both venues
  (+0.47/+1.25). **Caveat (honest):** `IC(rmom,residual)` is weak (−0.08/−0.03) while `IC(rmom,net)`
  is strong, so rmom predicts net mostly *through factor exposure*. Under **honest overlap-aware
  (weekly) annualization** (P3-3) the selected-subset residual Sharpe is **+0.28 bybit (marginal miss)
  / +1.14 binance (clear pass)** — i.e. **borderline Tier-3, not a clean cross-venue pass** — but the
  relative separation (selected vs discarded) is large/robust both venues and **very strong recently**
  (+1.9/+2.0). It's the program's **best alpha evidence**, sitting right at the Tier-3 threshold;
  certifying it needs an engine-integrated backtest (rmom as a PIT selection filter, overlap-aware
  annualization) — **operator-gated build** (the precheck justifies it).
- **P3b (BUILT + VALIDATED, operator-greenlit): the residual-momentum gate is a robust Tier-2
  DEMO-CANDIDATE**.
  Integrated the signal as a PIT selection filter in the engine (commit 17df8ba; default-inactive,
  tested, 1054 tests pass) and backtested it (gate at the per-venue median rmom, both venues, 15 bps):
  **return 2–3×, Sharpe doubled, DD halved** (bybit +98%→+210% / Sh 1.59→3.81 / DD −16%→−3.3%; binance
  +32%→+90% / Sh 0.96→2.93 / DD −11%→−4%), all-thirds-positive both, LOO-stable, bootstrap MAR-Δ p5≫0
  → **r1_robustness DEMO-ELIGIBLE** (the program's strongest, most robust Tier-2 result). Tier-3 residual
  (overlap-aware weekly): the gate factor-neutralizes both venues; **binance clears +0.3 (+1.10) = genuine
  factor-neutral alpha, but bybit is residual-neutral full-window (+0.00, +2.18 recent)** → NOT a clean
  cross-venue alpha certification. So the gate = **risk-reduction + factor-neutralization + venue-asymmetric/
  recent residual alpha** — a validated demo-candidate, not a fully-certified all-weather alpha engine.
  (MARs DD-inflated; robust diagnostics + residual Sharpe are the headline.) **Recommend forward-demoing it**
  (operator-gated profile change; the forward demo is the real Tier-3 arbiter for OOS persistence).
- **The continuous candidate signal (c2b, 2026-05-30, EXPLORATORY) — age gate flips it on the
  full window but the edge is RECENT-REGIME-ONLY.** The rolling features carry real cross-venue IC
  (`rv_168h` −0.13 @168h). c2's decile short was "not tradeable" because the top-composite decile
  *rallies* in the full panel (young-name M1 continuation). Applying `age≥300` flips the full-window
  average to cost-positive @168h (short-only +29/+35 bps; beta-neutral L/S +14/+22). **But the
  recent/early split refutes the robust reading**: even the beta-neutral L/S is **negative in the
  early 26 months** (−14/−12 bps) and positive only in the recent ~12 (2025–26 alt-bear, largely
  short-beta). So the continuous age-gated short is **regime-conditional, not all-weather** → the
  C0 build is **not justified** on this (contrast the discrete strategy, all-thirds-positive incl.
  early). Honest null for the strong "rescues the continuous architecture" claim.

**Cross-venue asymmetry is the standing caveat:** bybit MAR 2.76 vs binance 0.28 (and
binance is funding-missing, i.e. optimistic). The edge is also front-loaded (recent third
much weaker). Any selection refinement must narrow this gap / hold up recently, not just
reload the early bybit regime.

## K0 (2026-05-30): the intraday-detection kernel — upside-ceiling PASS

The forward plan ([research_plan_intraday_kernel.md](research_plan_intraday_kernel.md))
asks whether detecting the discrete event **intraday** (off the WS stream) instead of on
the daily-close roll captures more of the fade. **K0** is the read-only upside-ceiling
precheck (`scripts/k0_intraday_fade_timing_precheck.py`, EXPLORATORY): for every short in
the validated daily ledger, compare the realized +1h entry to the **event-day intraday
high** — the best price a faster detector could have shorted at.

**Result: PASS, decisively, both venues × both early/recent splits × two books**
(primary `fixed_delay`/age≥90 to isolate detection latency; secondary age≥300
`quality_squeeze`). Median `ceiling_uplift` **bybit ~993–1041 bps, binance ~862–881 bps**
(~8–11%), positive in every split (bybit EARLY 957–1026 / RECENT 1000–1071; binance EARLY
698–778 / RECENT 969–996) — clears the ~15 bps cost gate by ~50–65×. The missed edge is
**0.84–1.41× the entire realized fade.** So the daily-close (+1h) entry is **systematically
~8–11% late** vs the intraday peak, all-weather (not a recent-regime artifact — passes the
c2b guard).

**Decomposition (K0b, `scripts/k0b_fade_decomposition.py`):** ~**95–98% of the ceiling is
the within-day-D giveback** (peak→daily-close); the +1h overnight fill window is only
~19–64 bps (~2–6%). → the lever is **intraday DETECTION**, not faster fills (re-confirms E1
at the daily→intraday scale). **Caveat:** it's an optimistic *ceiling* (assumes shorting the
exact top, which is unknowable in real time and pre-event-confirmation) → **necessary, not
sufficient.** The realistic capture is a fraction of it, to be measured under the engine in
**K1** (rolling PIT-causal intraday detection vs the daily-close baseline — pre-register
first; K2 live-WS stays operator-gated). Receipt:
`docs/preregistration/k0-intraday-fade-timing-2026-05-30.md`.

## Useful findings worth keeping

1. **Concentration is the deployed config's main risk.** `max_active` 3→12 cuts worst-day
   −36%→−4.8% and DD −87%→−27.5%. The demo runs 3; research-validated is 12. **Move it.**
2. **Stop-fill assumption** dominated the old verdict: `bar_extreme` (worst-case wick) vs a
   10% cap swung the deployed curve −32% → +479% (concentration-amplified). Default is now
   `bar_extreme_capped` 10% (realistic bad-case), calibratable from demo fills.
3. **Component winners** (daily): `risk_equal` 2% sizing (de-concentrates, cuts DD),
   `ff6_4pct` failed-fade exit (best loss-cutter), `drop_all_4` filter set.
4. **Pre-2023 is structurally untradeable** (bybit had 7–182 symbols; rank-31–400 + ≥150
   rank-climb needs the 400+ universe that only existed from ~mid-2024). There is **no
   internal OOS root** — pristine OOS is the forward demo (see [data_roots.md](data_roots.md)).
5. **Long sleeve = a low-correlation OVERLAY on the short book, not standalone alpha.**
   The v11a long (FC) sleeve's FC signal is the quality ceiling (exhaustive 7-wave search
   couldn't beat it on both venues; [[long-sleeve-alpha-search-null]]). Its value is
   diversification: short↔long correlation ≈ −0.02/−0.05 (Bybit/Binance), so an additive
   overlay (`combined = short + w·long`, w ≈ 0.5–1.0) lifts the combined book's risk-adjusted
   return — cross-venue **MAR +17% (Bybit) / +38% (Binance)** at w=1.0, drawdown flat-to-better,
   paying off precisely on the short book's worst days. The robust part is the
   variance-reduction (structural regime complementarity); the standalone-return *magnitudes*
   are 2023–2026 in-sample-optimistic (FC fails pre-2023 OOS), so don't over-size (w≥2 just
   bets the fragile long edge). Already deployed on demo; forward demo is the OOS arbiter.

## Methodology lessons

Engine hardening (2026-05-29) toward honesty was correct in direction: optimistic→honest
stop fills, 100% taker, calendar-exact returns, real promotion gates, full-PIT survivorship.
**The over-correction** was making worst-case `bar_extreme` the *default* (too brutal on 1h
alt wicks — real stop slip median +2.3%, but it assumed wick-tops to +89%). Fixed to a 10%
cap. **The deeper lesson:** never test a strategy with a single hard-coded execution; the
selection signal and the entry signal must be evaluated separately, or you measure the
execution's flaws and blame the signal.

## Was "Round 2 = null" right?

**No.** It was a worst-case execution model *and* a selection/execution conflation. The
daily strategy (with fade-confirmation execution) is positive on both venues in-sample. The
continuous selection signal is real and never got an execution layer. **The strategy is not
dead.** Open work: (a) move the demo to `max_active=12` + capped fills; (b) the
`fixed_delay` vs `quality_squeeze` test to quantify the execution signal's contribution;
(c) apply + refine the execution (sniper) on the continuous candidate pool; (d) forward-demo
confirmation is the arbiter. Any of these is a fresh, dated pre-registration.

## Provenance

The E1/E2/P2/P3 + c2b per-phase pre-registration receipts (2026-05-29/30) were consolidated into this record and removed 2026-05-30 (originals in git history), as were the earlier Round 1 + Round 2 plans and per-phase verdicts (phase0–6, R1–R13, C0–C3) — all consolidated
here and deleted 2026-05-29; originals in git history. Engine/methodology change receipts
are in the git commit log. Backtest artifacts live under the data roots
([data_roots.md](data_roots.md)).
