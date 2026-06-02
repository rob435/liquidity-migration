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

**Cross-venue asymmetry — REFRAMED by CV1 (2026-05-30):** the headline gap (bybit MAR 2.76
vs binance 0.28) is **NOT an edge-quality asymmetry** — the per-trade edge is venue-general
(matched-event corr 0.89, binance ≈ bybit on shared coins). It is **breadth + universe
composition**: binance fires ~half the events and its venue-unique coins are weak marginals
(see the CV1 section). binance is funding-missing (optimistic). The recent per-trade mean
decay on BOTH venues (tail-driven) is **squeeze-driven and is fixed by the rmom gate** — see
the RD1 section (cuts ~75% of recent stop-outs; restores recent mean both venues).

## K0→K1a (2026-05-30): the intraday-detection kernel — ceiling PASS, but FALSIFIED at K1a

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
sufficient.** The realistic capture is a fraction of it, measured next in K1a.

**K1a (2026-05-30) — FALSIFIED: the ceiling is un-capturable; the kernel is dead.** The cheap
feasibility check (`scripts/k1a_intraday_first_crossing.py`, conditioned on daily-firers, the
*optimistic* test) found the first intraday hour the **same selector** fires and the realistic
uplift (fill at `h*`+1 vs the daily +1h entry): only **~40–85 bps median (~10–15% of the
ceiling)**, a **coin flip** (45–46% of trades negative, q25 ≈ −3% / q75 ≈ +5%), **negative on
binance early**, `corr(lead,uplift)≈0`. **Mechanism:** the selector can't *confirm* the event
(cumulative turnover ≥6×) until median `h*` = 15–16:00 UTC — ~9h after the morning peak — by
which point price has faded back to ≈ the daily entry; confirmation time (turnover
accumulation) is **decoupled from the price path**. So the ~9% ceiling is real but
un-capturable by faster detection of the *same* event. **This closes the timing axis:** E1
killed *fill* timing (within 1h); K1a kills *detection* timing (~9h, same selector). The alpha
is **purely SELECTION**, the daily cadence is fine, and **K1b/K2 are cancelled** — the program
reverts to the validated selection refinements (age gate + residual-momentum gate, forward-demo
-gated). A *new* intraday-native selector is a separate unchartered direction. Receipts:
`docs/preregistration/k0-intraday-fade-timing-2026-05-30.md` (ceiling),
`k1-intraday-detection-2026-05-30.md` (K1a falsifier).

**I-phase (2026-05-30, operator-directed REOPENING — K1a was too narrow).** K1a only
falsified running the *daily selector* hourly (its ≥6×-**daily**-turnover rule can't confirm
until ~15:00, after the fade). It did NOT test a **purpose-built intraday selector** firing on
*rate/flow* features at the peak — a genuinely different signal. Reopened to engineer one.
Data verified (correcting a wrong memory): cross-venue all-weather channels = klines
(price/volume/intrabar/velocity) + **premium_index (hourly) + funding (8h)** both venues +
market-context; **OI bybit-only**; taker-flow binance-recent-only (out). 1h grain.
**I1a (`scripts/i1a_fader_intraday_signature.py`):** faders show a clear cross-venue intraday
**exhaustion fingerprint** — peak ~16–17:00 UTC, peak-bar **upper-wick ~0.43** + mid-range
close (intrabar rejection), **turnover climax ~4.2–4.6× the day-mean at the peak** then rolloff,
price +20–22% then fades ~6–8% over 6h; **OI builds into + surges after the peak on bybit**
(+28%→+54%); premium ~0 (froth channel quiet). So the fingerprint EXISTS (necessary). **The
make-or-break is I1b:** do pumps that CONTINUE (don't fade) show the same fingerprint at their
intraday burst? If a PIT-causal feature separates faders from continuers → build the selector;
if not → honest null (this time of the *right* hypothesis). "Timing is dead" is therefore
**downgraded to: the same-selector intraday variant is dead; the purpose-built intraday selector
is UNDER ACTIVE INVESTIGATION.**

**I1b RESULT (2026-05-30) — separation CONFIRMED, beta-neutral, cross-venue, all-weather → build I2.**
Scanned ALL intraday rate-bursts (age≥300, liq-rank 31–400, intraday gain ≥8% + hourly vol-spike
≥5× the prior-7d avg hour, cooldown 3d, forward 48h) across BOTH venues — including pumps that
never became daily events (bybit 8968, binance 7912 bursts; `scripts/i1b_burst_separation.py`).
Tested which PIT-causal features separate faders from continuers, then **beta-neutralized**
(coin forward return − market forward return):
- **The separation SURVIVES beta-neutralization** (so it's idiosyncratic, NOT the rejected
  market-regime beta bet): `idio` (pump magnitude relative to market) ic_neutral **−0.28…−0.31**;
  velocity / vol-spike / acceleration **−0.11…−0.16**; on **BOTH venues × BOTH early/recent**. The
  intrabar **wick is noise** (ic≈0) — the I1a fingerprint's wick does NOT discriminate fade vs continue.
- **It's a SELECTION on pump-extremity, not "short every pump":** shorting ALL bursts is
  ~breakeven-to-negative beta-neutral (mean −0.6…−0.9% early); the **extreme subset** (top
  idio/velocity quintile) has beta-neutral short-PnL **+1.2–1.3% early / +4.4–4.7% recent** (gross 48h).
- **Mechanism (reconciles RD1):** the intraday burst entry catches the idiosyncratic short-term
  reversal of extreme pumps that the daily entry (next-day +1h) is too late for — by the daily close
  the faders have faded and only continuers remain (RD1's squeezes). So intraday detection DOES add
  value — but as a **NEW extreme-pump-reversal selector**, not the daily selector run sooner.
- **Verdict: the make-or-break PASSES.** Next = **I2**: backtest the extreme-pump-burst short under
  the realistic engine (15 & 45 bps, capped stops, max_active, both venues, early/recent, MAR-primary)
  AND residualize through `risk_model` (is it unique alpha vs the known short-term-reversal factor?).
  **Honest bounds:** gross-forward edge must survive costs+stops; the edge is in the extreme subset;
  it is plausibly short-term-reversal concentrated by the liquidity-migration framing — I2 settles it.

**I2 RESULT (2026-05-30, `scripts/i2_burst_backtest.py`) — signal REAL, naïve stopped strategy FAILS.**
Backtested the frozen extreme-burst short (12% stop, 48h max-hold, costs, +1h fill), both venues,
early/recent. **Without a stop** the extreme subset is net-positive at 15 bps — bybit +1.79% (E +0.74 /
R +3.47), binance +1.74% (E +1.10 / R +2.90) per trade — positive **both venues × both eras**, beating
all-bursts (~+0.4%): the I1b signal is **real, not an artifact**. **But with a realistic 12% stop it
collapses:** ~breakeven at 15 bps (bybit −0.01% / binance +0.05% ALL) and **negative at 45 bps**;
**33–41% of trades stop out** (−14% each), eating the edge; portfolio MAR ~0/negative, recent-skewed.
**Mechanism:** the median trade is +2.3%/wins 55%, but pump-shorts wiggle up ≥12% before fading often
enough that any risk-bounding stop is hit ~40% of the time. No-stop is positive but **undeployable**
(unbounded short tail risk). **This rediscovers WHY the strategy shorts the confirmed FADE, not the top
(E1)** — the burst-short is "catch the top." **Verdict: not a deployable edge as a naïve top-short.**
**Next = I2b:** apply fade-confirm execution intraday — use the burst to flag the candidate, short the
intraday **giveback**, not the burst — to dodge the continuation that causes the stop-outs. Receipt:
`docs/preregistration/i2-intraday-burst-selector-2026-05-30.md`.

**I2b + stop-width resolution (2026-05-30) — the WIDE STOP monetizes it → a real (unvalidated) lead.**
I2b (short the intraday *giveback*, not the burst) did NOT rescue it — confirm-rate ≈1.0 (almost every
pump gives back 3–8% within 48h, so it barely filters) and it stayed recent-only / early-negative both
venues. Wrong fix. The RIGHT lever is **stop WIDTH**: the daily strategy's **12% stop is far too tight**
for a catch-the-top short (38% stop-out). Stop-width frontier (EXTREME subset, both venues, per-trade
net45 + Stage-B-proxy portfolio MAR/DD):
- 12% → MAR −0.8 (fails, recent-only); 20% → ~1.45; **30% → MAR 3.2/2.79, DD 14.5/10.7%, ALL-WEATHER
  (EARLY net45 +0.15/+0.25 both venues)**; 50% → MAR 4.88/8.01, DD 16/9% (better, EARLY +0.44/+0.64);
  no-stop → best raw return but the **tail blows out** (DD 26/20%, a −20% squeeze-day).
- **Monotonic 12%→50% (not a fragile sweet-spot):** the tight stop killed a real edge; a wide (30–50%)
  stop captures it with **bounded** tail (worst-day ~−4%); only removing the stop entirely exposes the
  −20% day. So the 12% mismatch was the problem — the signal IS monetizable.

**Verdict: a REAL, promising, cross-venue, all-weather intraday lead** — the extreme-pump-burst short +
a wide (30–50%) stop, MAR ~3–8 / DD 9–16% net of 15–45 bps, both venues, both eras. **NOT validated:**
(a) **Stage-B PROXY** (entry-day P&L booking, approximate concurrency → needs an engine-grade backtest);
(b) **wide-stop FILL realism is the key risk** — a 30–50% stop can gap far worse in a squeeze (the 2% slip
is optimistic); (c) **funding UNMODELED** (plausibly a *credit* here — shorts receive funding during
pump-long-crowding — or a drag); (d) back-loaded / thin early; (e) **STR-factor uniqueness open** (is this
the known short-term-reversal factor?). **Next = I3 (pre-registered):** engine-grade backtest (true
exit-timing/concurrency + capped fills + funding) + `risk_model` residual + STR-factor test;
operator-gated. A lead worth building properly — **not** a deployable strategy yet. Receipt:
`docs/preregistration/i3-intraday-burst-engine-2026-05-30.md`.

**I-PHASE CONCLUSION (2026-05-31, operator-directed tight-stop constraint) — the standalone intraday
short is NOT safely all-weather deployable.** The operator (correctly) flagged the 30–50% stop as
fragile (optimistic fill modeling; operationally untenable) and capped it at ≤20–25%, directing
"more fade, not short the top" + entry refinements. Under a **tight stop (≤20%)**, every entry tested
is **early-negative / recent-only on both venues** — top-short (I2), % giveback 3–20% (I2c), and
**momentum-confirmed sustained fade** (down-bars=2, I2d): best cell (give-5%, stop-20%) is EARLY net45
**−0.46% bybit / −0.03% binance**, RECENT +1.3/+1.0. Entry timing does NOT fix it; the early-negative is
**structural/regime** — 2023–24 bull-market pump *continuations* squeeze any tight-stopped short, and a
tight stop crystallises them as −15–20% losses (the all-weather profit only appeared at the wide,
fragile stop). So at deployable risk it is the **c2b pattern: recent-only = a bear-regime bet, not
all-weather alpha.**

**The deep reconciliation (the real payoff of this arc):** the daily strategy's "late" next-day +1h
entry — which K0 measured as ~9% below the intraday peak — is **NOT a bug; it is WHY the strategy is
safely all-weather.** Entering *after* the pump has settled sidesteps the intraday squeeze that makes
the aggressive/early short unsafe. The K0 ~9% ceiling is real but **un-capturable safely**, because
capturing it means shorting into the squeeze zone (fat right tail). The daily fade-confirm-next-day
entry IS the safe harvest of this exact effect. This closes the whole arc: K0 (ceiling real) → K1a
(same-selector can't fire in time) → I1b (the signal is real, beta-neutral, all-weather *unstopped*) →
I2/c/d (but not safely monetizable at a tight stop — the squeeze tail). **Verdict: file the
intraday-standalone-short as a real signal that is NOT safely deployable under realistic risk; the
program's robust all-weather edge remains the daily age-gated + residual-momentum strategy.** I3
(engine-grade) is **deprioritised** — it would only sharpen a recent-only result. Scripts:
`i1b_burst_separation.py`, `i2_burst_backtest.py`, `i2b_burst_fade_confirm.py`. (Lower-probability
avenues if revisited: down-bars=3; a market-neutral L/S form — though c2b found the beta-neutral
continuous version was also recent-only.)
**STR-factor check (completeness, 2026-05-31):** multivariate OLS of the unstopped beta-neutral
forward return on rank-standardised burst features (both venues, i1b bursts) — **idiosyncratic
relative-return reversal (`idio`) dominates (β −0.35/−0.36)**, i.e. the signal is **substantially the
known short-term-reversal factor**; `vol_spike` (the liquidity-migration volume structure) **survives
but is small** (univariate IC −0.14 → partial β −0.05/−0.06, consistent cross-venue). So ~mostly STR +
a modest real volume increment — consistent with P2-1 ("mostly factor exposure").

**I-PHASE UPDATE (2026-05-31, the operator's full ≤25% cap) — a deployable CANDIDATE exists after all
(verdict revised).** The "not deployable at a tight stop" conclusion above tested stops ≤20%; the
operator's stated cap is ≤25% and I had not run the 22–25% boundary (premature null — second time this
arc I called it early). Running it: the **extreme-burst TOP-short (the burst entry, NOT a fade) flips
all-weather at ~25%** — per-trade net45 positive in **BOTH venues × BOTH eras** (EARLY +0.13 bybit /
+0.39 binance; RECENT +1.34/+0.51), Stage-B portfolio MAR net45 **3.1/2.2** (net15 5.6/4.3), **DD 11–13%**.
The early period is marginally negative at 20–22% and turns positive at ~25% (the boundary sits right at
the operator's cap). Note the fade entries all UNDERPERFORMED the top-short at the same stop — "more fade"
empirically loses; the lever is **stop width**, not entry. **Verdict revised: a deployable-CANDIDATE
config exists within the ≤25% constraint = the extreme-burst top-short at a ~25% stop.** Caveats (NOT
validated): Stage-B PROXY (entry-day P&L, approx concurrency, flat 2% stop-slip → engine-grade needed);
**back-loaded** (portfolio first calendar-third mildly negative −6%/−2%, bulk recent); 25% is the boundary
(fragile below) and a rough 25% adverse hold operationally; mostly short-term-reversal (idio-dominated).
**Next = engine-grade I3** (`docs/preregistration/i3-intraday-burst-engine-2026-05-30.md`: true
exit-timing/concurrency + `bar_extreme_capped` fills + FUNDING + risk_model residual, stop ≤25%) — now
**JUSTIFIED**; operator-gated go/no-go.

**I-PHASE FUNDING DE-RISK (2026-05-31) — funding eats ~85% of the edge; a MARGINAL candidate survives under
fair accounting → engine-grade I3 to settle (NOT closed). Full balanced write-up: `intraday_burst_synthesis.md`.**
Before the expensive I3 build, costed FUNDING on the proxy (short receives + / pays − over the hold, both venues
full-history, `--funding-ds`). **Note: this verdict revised twice in-session — first to "funding kills it/closed",
then corrected when the operator asked whether any variant beat baseline under optimistic funding and exposed a
pessimistic proxy bug (I2k: funding was over-charged on stopped trades).**
- **I2g/I2h — median survives, portfolio dies.** The funding *mean* first looked like a kill (−0.6…−1.5%/trade)
  but was **outlier-distorted** by high-frequency-funding coins (LRC −16% over 48 hourly settlements). The
  **median** trade's funding is ≈0 (median net45+funding still +3.4/+4.7 early/recent). But the funding-**included
  PORTFOLIO** dies — MAR **−0.91 bybit / −0.73 binance** — because 11–32% of trades are **crowded-short** coins
  (perps trade at a discount when everyone shorts a fresh pump → the short *pays*), a broad early drag at 2% book weight.
- **I2i — a PIT crowded-short FILTER (skip coins with negative trailing funding *at entry*) doesn't rescue it.**
  It helped (MAR → −0.46 bybit / −0.15 binance) but stayed negative & early-negative both venues, because the
  funding accrues **during** the hold (shorts crowd in *as* the pump fades) — un-predictable at entry.
- **I2j — SHORTER HOLD (12/24/48h) with funding-to-48h accounting** looked like a kill (every cell MAR-negative:
  12h −0.69/−0.23, 24h −0.54/−0.09, 48h −0.91/−0.73). **It was not — the accounting was wrong (see I2k).**
- **I2k — FAIR funding (charged to ACTUAL exit) REOPENS it.** The to-48h funding was summed over the full hold
  **even for stopped trades**, but a stopped trade exits early and stops paying — and the ~13% stopped trades are
  exactly the **crowded-short squeezes** with the worst funding, so the charge was **over-counted on the killing
  tail** (a pessimistic bug the operator's question surfaced). `--funding-to-exit` fixes it. At **24h hold, 25% stop**:

  | 24h hold | funding-BLIND | **FAIR (to-exit)** | pessimistic (to-48h) |
  |---|---|---|---|
  | bybit   | ret +38.7%, MAR 3.08 | **ret +4.3%, MAR 0.30** | −0.54 |
  | binance | ret +28.7%, MAR 2.76 | **ret +5.6%, MAR 0.49** | −0.09 |

**BALANCED VERDICT: real signal, MARGINAL standalone short, NOT closed.** Under fair funding the **24h-hold candidate
is positive on both venues** (MAR +0.30 bybit / +0.49 binance; binance all-weather, bybit positive-but-recent-tilted —
equity curve underwater ~3y then a recent pop; 48h hold ~breakeven). BUT funding eats **~80–89% of the funding-blind
edge** (3.08/2.76 → 0.30/0.49), and this thin survivor was found after **extensive search** → **weak evidence, not
validation.** The verdict **swings with funding-exit modeling** (−0.54 → +0.30 → +3.08) — it sits **at the Stage-B
proxy's resolution limit**, which cannot settle a MAR≈0.3 candidate. So **engine-grade I3** (true exit-timing/concurrency
+ `bar_extreme_capped` fills + funding-to-exit + risk_model residual, **24h hold, stop ≤25%**) is the right tool —
**operator-gated** (~85% funding-eaten + recent-tilted → a genuine coin-flip whether worth the ~5–7d build). **The DAILY
age+rmom strategy remains the robust, already-validated all-weather edge** (late entry sidesteps both squeeze and funding
crowding); the intraday short is at best a higher-risk, marginal, unvalidated add-on. Full write-up:
`docs/intraday_burst_synthesis.md`. `scripts/i2_burst_backtest.py` (`--funding-ds`, `--funding-filter-floor`,
`--funding-to-exit`, `--hold-h`).

## Continuous-fade Phase 0 (2026-05-31): the rmom squeeze-filter makes the CONTINUOUS short ALL-WEATHER

New forward program ([research_plan_continuous_fade.md](research_plan_continuous_fade.md)): build an
always-on, any-hour version of the daily strategy, or kill it. Phase 0 (§6, the highest-information run)
re-ran the c2b rolling-decile signal on full-PIT both venues with **age300 + residual-momentum** applied,
split early/recent — EXPLORATORY look-ahead decile characterization (per-ts mean fwd returns, NOT a
backtest). Receipt: `docs/preregistration/p0-continuous-rmom-2026-05-31.md`; artifacts
`~/SHARED_DATA/p0{,b,c}_*_2026-05-31.{json,out}`; scripts `scripts/p0{,b,c}_continuous_*.py`. The port
reproduces c2b's `baseline`/`age_ge_300` columns to ±0.5 bps (validates it).

- **age300-alone is recent-only (c2b/BB2 confirmed); the PIT-clean rmom gate flips it ALL-WEATHER.**
  EARLY/RECENT short_only & beta-neutral L/S @168h (bps): age300 bybit early **−44/−14**, binance early
  **−18/−12** (recent-only); **rmom_lo** bybit early **+82/+92**, binance early **+65/+41** (positive both
  venues both readouts). The **active gate is rmom, NOT age** — and rmom-alone beats the age+rmom stack
  (stacking thins the cross-section; binance L/S goes −5). This *inverts* the daily-book finding (age robust,
  rmom fragile/recent — [[age-rmom-ff6-overlap]]): rmom is a broad cross-sectional signal that the narrow
  daily event-pool thins into a recent slice; continuous application recovers its all-weather breadth.
- **BB1 (wrong-sign) also resolved by the same gate.** rmom-gated D9 mean-fwd is NEGATIVE at every horizon
  incl. 1h (bybit −15/−41/−149/−146/−128 @1/3/24/72/168h) → the names *fade from hour one*; rmom IS the
  causal "fade-has-started" confirmation. Baseline D9 instead RISES (+2/+16/+27) = shorting into continuation
  (the c2b wrong sign).
- **Two reframes of the plan.** (1) The robust object is a **~24h idiosyncratic reversal, NOT a "slow
  multi-day fade"** — at the 24h hold the short is positive in EVERY bucket (2023/2024/2025H1/recent, both
  venues, both readouts: bybit short +91/+111/+185/+170; binance +75/+90/+161/+163); at 168h the early period
  is carried by a single 2025H1 outlier (+399/+427) and is weak/negative in 2023–24 (the c2b concentration
  returns). 24h is the all-weather sweet spot. (2) rmom-not-age (above).
- **Robust on every cheap axis.** Entry-latency (the mandatory gate): 24h early keeps **~88% at +1h**
  (bybit +118→+104, binance +99→+86), positive both venues at +3h — a smooth gradient = real multi-hour
  reversal, not a closing-print artifact. Threshold: **monotone** strengthening as the gate tightens (q
  .50→.33→.25: bybit h24 early +118→+138→+150). **Funding: a non-issue both venues** (funding-to-exit leg
  +0 to −5 bps every period, slightly positive early) — the rmom short enters post-fade names *after* the
  funding crowd clears, inheriting the daily strat's funding-robustness. **Data correction:** binance
  `binance_usdm_funding` has **99.8% panel coverage and is genuinely ≈0** — NOT funding-blind/missing (§1
  saw ≈0 and mis-attributed it to sparsity); Avenue F4 is largely already done. [[binance-funding-missing]]
- **Honest bounds (necessary, not sufficient):** EXPLORATORY per-ts characterization, not a tradeable book —
  the gaps are concurrency/overlap, **true turnover/churn** (hourly reformation; the next cheap question =
  Avenue D), sizing, capacity. ~24× overlap overstates `n_ts` precision (the all-weather-across-regimes
  criterion carries the conclusion, not a t-stat). Substantially the **STR/residual-reversal factor** (rmom
  = factor-residual momentum) → "mostly factor exposure" like the daily (P2-1); a deployable continuous
  factor-harvest, NOT certified unique alpha. **Phase-0 sub-verdict: PASS at the signal level** — the edge is
  real & all-weather; but tradeability + the discretization question were still open (Avenue D, below).

**Continuous-fade Avenue D (2026-06-01): NULL for continuous-any-hour — the daily cadence is LOAD-BEARING.**
Receipt `docs/preregistration/p1-continuous-daily-cycle-2026-06-01.md`; `scripts/p1d_continuous_turnover.py`;
artifacts `~/SHARED_DATA/p1d_*_2026-05-31.{json,out}` + cached panels `<root>/_p1_continuous_panel.parquet`.
The decisive discretization test (does continuous beat/extend daily?):
- **Tradeable, not a churn artifact:** de-overlapped spell-entry (24h cooldown, 15bps) is all-weather both
  venues all buckets and *stronger* than the per-ts char (bybit +193 vs +144 / binance +184 vs +133, FULL).
  Persistence: D9 dwell ~6.7h, ~18 names concurrent, ~12.7k trades/yr (moderate, not pathological).
- **But the fade is a DAILY-CYCLE object.** Short edge by hour-of-day decays **monotonically ~25× from the
  daily close to 23:00**, identical both venues, equal n/hour (bybit hod0 +259 → hod23 +10; binance +245 →
  +3). The **decisive cross-tab (fresh-entry edge × hour-of-day)** shows this is NOT staleness: *fresh* D9
  entries also decay by hour (bybit hod0 +304 → hod23 −5) and **~4× of fresh entries occur at hod 0**
  (n≈13.6k vs ~2.5k/hr). The daily-00:00-UTC close dominates even fresh signals.
- **Verdict: the strong continuous thesis is FALSIFIED.** The 01:00 clock is not a proxy — it is the actual
  information-arrival time; the **deployed daily entry is near-optimal**, and intraday entries are lower-edge
  *noise*. A continuous any-hour book adds only low-edge breadth → **the engine-grade build (A3/H3) is NOT
  justified.** This is the plan's welcomed honest null ("daily cadence is load-bearing — saves months"),
  coherent with E1 (fill-timing not the lever), K1a (detection-timing not the lever), and the I-phase
  reconciliation (the daily late entry is the safe, funding-light harvest). **Byproducts:** (1) the deployed
  01:00 entry is **vindicated** as near-optimal; (2) rmom independently reconfirmed as the all-weather
  cross-sectional fade gate; (3) **residual DAILY lead** — the rmom-composite-decile daily selection
  (daily_0100, all-weather +250/+238) is a broader daily short than the event filter, worth a proper DAILY
  backtest comparison (MAR/DD/funding vs the deployed strategy) — a daily-selection study, NOT a continuous
  system. Net: **continuous-fade program CLOSED as an honest null; the program's robust edge remains the
  daily age+rmom strategy, whose entry timing is now shown near-optimal.**

**Continuous-fade Avenue D CORRECTED (2026-06-01, same day) — the NULL above is RETRACTED; CONTINUOUS IS
VIABLE.** The "daily cadence load-bearing" verdict was **premature — a 24h-HOLD-SPAN artifact** (a 24h hold
from a late entry-hour spans the next day's pump, manufacturing a close-peaked decay). Two decisive
follow-ups (`scripts/p1f_entry_vs_hold.py`, `p1g_fresh_intraday.py`; receipt
`docs/preregistration/p1b-continuous-intraday-fade-2026-06-01.md`) break the confound:
- The **per-hour fade RATE** does NOT peak at the close — it is a broad **+12–15 bps/h plateau 00:00–15:00
  UTC** (slightly midday-peaked), weakest-but-positive late-evening, **positive every hour both venues both
  eras.** For FRESH D9 spell-entries (the clean test): 6h/h close +12.9/+12.1, midday +15.4/+15.0, late
  +8.2/+6.5 bps/h (bybit/binance), all EARLY-positive. The SAME fresh entries fade comparably over 6h at all
  hours (close +88 vs midday +91) but only the close entry's 24h fade is large (+319 vs +129) — purely the
  longer post-close *runway*, not a daily lock.
- **Corrected verdict: the fade is a real, all-weather, all-day intraday process → continuous is viable.**
  ~82% of fresh fades occur OFF the daily close (entries the once-daily strategy misses) at comparable
  per-hour quality, ~17–19-name book. **Both boss battles genuinely beaten** (BB1: a fresh D9 entry IS the
  fade-started confirmation; BB2: all-weather). The mission thesis ("measure the state, short any hour") is
  **vindicated.** Right design: enter fresh rmom-gated D9 at any hour, **short ~6–12h hold** (not 24h).
- **Still EXPLORATORY (not a portfolio MAR).** The now-justified next step is a concurrency/capacity-aware
  portfolio proxy backtest (enter-any-hour, 6–12h hold, cost+funding+max_active, both venues, early/recent);
  capacity (concurrent illiquid-alt shorts) + turnover cost are the key open risks (funding ≈0 here, P0c).
  **Lesson logged: never finalize a null on a single hold horizon — decompose entry-timing from hold-span.**
  The earlier "deployed 01:00 entry vindicated / daily age+rmom is the robust edge" still holds for the
  DAILY book; what changed is that continuous is NOT dominated — it captures real all-weather off-close fade.

**Continuous-fade FINAL verdict (2026-06-01) — real all-weather SIGNAL, but a WEAK tradeable book; daily
wins on COST/CAPACITY (not the retracted artifact).** Receipt
`docs/preregistration/p1c-continuous-final-verdict-2026-06-01.md`. The portfolio proxy (`p1h`) is **INVALID**
(compounds a high-frequency look-ahead edge with no capacity/fill realism → absurd MAR 24,000+; only its
structure — DD 4-8%, both eras positive — is informative). Capacity is **small & shrinking** (D9 = mid-liq
alts; bybit median hourly turnover $101k, RECENT only $52k, 35% of entries <$50k/h; est. deployable book
~$0.1-0.5M bybit / $0.5-2.6M binance). **The sound economic reason the daily cadence wins** (NOT the retracted
daily-cycle lock): the continuous **6h-hold** gross edge (~+85 bps) is the same order as a realistic
round-trip cost on these names (spread + taker + impact ≈ 100+ bps) → **impact-fragile** (net @100bps ≈ −15);
the daily **24h-3d hold** captures ~+319 bps (close) that clears the same cost (net @100bps ≈ +219). The
15-bps mid-fill used in all the EXPLORATORY chars is the optimistic illusion; continuous also runs ~12× the
daily's turnover on the same illiquid names → strictly worse capacity. **Net: continuous is a real,
all-weather signal but NOT a worthwhile tradeable book** (small capacity + short-hold impact-fragility +
long-hold only adds lower-edge off-close breadth); **recommend NOT building the continuous engine.** The
robust, cost-survivable edge remains the **daily age+rmom strategy**, whose longer hold is now understood as
*load-bearing for cost robustness* on the illiquid alt space. **Continuous-fade program CLOSED** (operator
may revisit only with a realistic-impact engine backtest; low-priority given the capacity ceiling). Byproducts:
rmom reconfirmed all-weather; binance funding present+≈0; the deployed daily design validated as cost-robust;
lessons logged (single-hold-horizon null; never trust a proxy MAR that assumes mid-fills on illiquid names).

**Continuous-fade REVERSAL (2026-06-01) — the "weak book / CLOSED" verdict above is RETRACTED; continuous is
VIABLE on the LIQUID universe.** Receipt `docs/preregistration/p1e-continuous-liquid-viable-2026-06-01.md`. The
"weak book" close averaged over the illiquid tail and was wrong. `p1i` (fade bucketed by entry liquidity) shows
the edge is **monotonically STRONGER on MORE-liquid names** (6h fade <$50k/h +52/+41 → >$1M/h **+118/+93**
bybit/binance, all-weather) — exactly as CV1 (venue-general edge) predicted; the liquid subset is large (bybit
≥$500k/h 22%, binance 51%) with real capacity (~$1.4-1.8M @1%) and low cost (~25-30 bps RT). `p1j` (SANE
ADDITIVE portfolio proxy — fixing p1h's compounding bug — on the liquid universe at realistic cost): **MAR
12-39, all-weather every cell both venues** (early AND recent positive), DD 3-6%, robust to cost (survives
50bps), liquidity threshold (≥$1M still MAR 17-37), and hold (6h & 12h; 12h best, bybit MAR 39 / binance 35).
**Verdict: a continuous any-hour short of fresh rmom-D9 entries restricted to LIQUID names (≥$500k/h, ideally
≥$1M/h), short 6-12h hold, is a real, all-weather, cross-venue, cost-robust, deployable-capacity (~$1-3M) edge**
that captures off-close fades the daily misses — the mission's "continuous, any-hour" thesis is **supported**.
**Calibration (I reversed twice):** EXPLORATORY (PIT-clean selection, simplified concurrency, not an engine MAR);
the MAR magnitude is partly de-concentrated sizing (DD 3-6% from 2% weight × ~25 positions), so the true edge
over the daily is the off-close breadth + lower DD, not a 5× return — a matched-sizing engine + forward demo is
the real arbiter. **This JUSTIFIES the operator-gated engine-grade backtest on the liquid universe** (re-decile
within liquid names; realistic impact; matched-sizing vs the daily; forward demo). Lesson added: never finalize
a verdict on an aggregate that mixes a strong core with a weak tail — decompose by the binding constraint.

**Continuous-fade CALIBRATED FINAL (2026-06-01, p1k matched-sizing) — continuous is REAL & tradeable on liquid
names, but the DAILY cadence is MAR-optimal; continuous adds absolute-return breadth, not a risk-adjusted edge.**
Matched-sizing (same liquid universe / 2% wt / max_active 25 / 30 bps; continuous any-hour vs a daily-only
01:00 proxy of the SAME signal): **daily_24h has the HIGHEST MAR** (bybit 42 / binance 36), Sharpe (10.3), and
lowest DD (2.2/4.0%) — the 01:00 close is the highest-quality entry hour; **cont_24h earns ~1.7-1.8× more
absolute return** (ann 166/245% vs 92/146%) via off-close breadth but at higher DD (4.6/9.2%) → lower MAR
(36/27). So the p1e "VIABLE" stands (continuous is a real strong strategy — "weak book" was wrong) **but
tempered: by MAR-primary the daily wins.** The "daily favored" intuition is vindicated for the RIGHT reason
(entry-quality: the close is the best hour), not the retracted daily-cycle artifact or the wrong cost argument.
**Net (the honest landing after 6 pressure-tested flips): the continuous fade is real/all-weather/tradeable on
liquid names; DAILY-ALONE (close-only) is MAR-OPTIMAL; continuous is a viable return-maximizing/capacity
alternative (more absolute return, lower MAR).** Note (correcting an over-reach): the "combined book" = the
continuous book (all entries) = MAR 36/27, **below** daily-alone (42/36); the off-close sleeve is the same
signal on the same names → highly correlated → little diversification, so it adds breadth not risk-adjusted
edge. **Daily-alone is the MAR-optimal harvest of this signal**; the operator-gated engine (liquid re-decile,
realistic impact, forward demo) is justified but **NOT urgent** (the only upside is absolute-return/capacity at
lower MAR). Receipt: `docs/preregistration/p1e-continuous-liquid-viable-2026-06-01.md` (matched-sizing addendum).
**One genuine ADD (p1l, the mission's "market-neutral" pointer):** a beta-neutral continuous L/S (long D0 /
short D9 within the liquid universe) **slashes drawdown vs short-only** (same method: bybit DD 23→19%, binance
38→18%) by removing the short-beta tailwind, at lower return → **comparable MAR** (bybit 33→31, binance 14→20
— binance higher, DD-reduction dominates). So the short-only's return is **substantially the short-beta
tailwind** (recent alt-bear); the L/S is a **beta-neutral, low-DD, uncorrelated** alternative the *directional*
daily short fundamentally **cannot be** — a potential diversifying sleeve (role like the long sleeve), NOT a
MAR-beating replacement. (Absolute MARs here are concentrated-proxy-inflated — ~4-8 names/decile; only the
short-vs-L/S comparison is valid.) `scripts/p1l_market_neutral.py`.
**The CULMINATING finding (p1m, the actionable continuous deliverable): the continuous market-neutral L/S is a
genuine DIVERSIFYING SLEEVE for the daily short.** corr(daily_short, cont_LS) = **0.32/0.26** (bybit/binance) —
LOW, vs 0.65/0.72 for the continuous *short* (which shares the D9 leg); the beta-neutrality + D0 long leg
decorrelates it. **Adding it to the daily short improves the combined book** — bybit Sharpe 11.6→12.6 / MAR
47.9→69.9 (w=1.0, DD flat ~23.5%); binance Sharpe 11.1→12.3 / MAR best at w=0.5 (53.5→56.4). So while the
direct continuous *short* doesn't beat the daily, **a continuous market-neutral L/S OVERLAY does improve the
live book's risk-adjusted return** — the same role the long sleeve plays (short↔L/S corr ~0.3). **This is
continuous's real value-add.** Caveats: proxy MARs concentrated-inflated; both are continuous-panel proxies
(not the deployed event-filter strategy); proper validation (correlation to the DEPLOYED strategy, realistic
impact, de-concentrated MAR, forward demo) is operator-gated. `scripts/p1m_combined_portfolio.py`.
**HONEST TEMPERING (the redundancy question — the real deployment gate):** the EXISTING long sleeve already
diversifies the short, and BETTER (short↔long corr ~−0.02/−0.05 per [[long-sleeve-alpha-search-null]], vs the
continuous L/S's +0.32/+0.26). So the continuous L/S is NOT the only (or best) short-diversifier on hand — the
decisive question is whether it is **additive to the long sleeve or redundant** with it (if the two
short-diversifiers co-move, the continuous L/S adds little over what's deployed). That needs a clean 3-way
engine backtest (deployed short + long sleeve + a real continuous L/S, aligned daily returns + corr) —
**operator-gated** (the long sleeve is a separate ~23 GB volume-events run, not the continuous-panel proxy). So
the continuous market-neutral L/S is a **genuine but possibly-redundant** diversifier; its marginal value over
the long sleeve is the open, engine-gated deployment question. Net continuous-program deliverable: a real
all-weather signal + a candidate market-neutral diversifying sleeve whose incremental value awaits the 3-way test.

**Continuous-fade ENGINE built (2026-06-01, operator-directed) — the EXPLORATORY proxy is now an execution-grade
backtest; the short survives honest execution all-weather.** `liquidity_migration/continuous_events.py` +
`continuous-events` CLI + 11 tests (full suite 1044 pass). It reproduces the proxy SELECTION (rmom-low composite-D9,
liquid ≥$500k/h, fresh spell entry) but runs it through the daily engine's validated execution core
(`_simulate_indexed_trade`: stop fills, MAE/MFE, funding-to-exit) plus the three things the proxy lacked: an
**honest +1h entry** (proxy filled at the same close it ranked on = execution look-ahead), a **size/ADV market-impact
cost** (`2·taker + 2·spread + 2·impact`, impact = k·(notional/ADV)^0.5 — the p1c capacity argument), and
**fixed-capital additive** accounting (consistent with the fixed-capital impact charge; compounding + a fixed impact
charge is internally inconsistent for a capacity-capped book and inflates returns absurdly). **Port-validated:** at
delay=0 (proxy parity) bybit h12 = 12,981 trades / +390.9% vs the proxy's 13,006 / +388.2% (0.2%/0.7% — the diff is
stablecoin exclusion + settlement-correct funding). **Honest run (delay=1 +1h entry + impact, both venues, additive):
the continuous liquid short is all-weather both eras both venues** — bybit h12 MAR 27.7 / +326% (early +195 / recent
+131), binance h12 MAR 21.8 / +502% (early +268 / recent +235); h12>h6; a **25% stop HURTS** (engine-grade confirmation
that the fade short doesn't want a tight stop). The honest +1h+impact haircut vs the look-ahead proxy is a clean ~17%.
**Label EXPLORATORY** (impact coefficients are modeled-not-calibrated; MAR still inflated by 2% de-concentrated sizing
→ DD 3–7%; the matched-sizing daily-cadence arm + forward demo remain the arbiters — daily-alone was MAR-optimal in
the proxy, p1k). Receipt `docs/preregistration/continuous-engine-2026-06-01.md`; artifacts `~/SHARED_DATA/cont_engine/`.
**FULL VERIFICATION AUDIT (2026-06-01, operator-directed "check & verify everything"):** the engine is correct —
accounting exact (net=gross+cost+funding), NO look-ahead (latency sweep 1→6h decays smoothly both venues = real
multi-hour reversal, not leakage), MTM telescopes, survivorship clean, Sharpe not an exit-day artifact. Two
framings corrected: concurrency is ~4–8 names NOT 25; and the short is **NOT mostly short-beta** (beta −0.10/−0.20;
the beta-neutral L/S long-D0/short-D9 keeps ~75–80% of return at Sharpe ~10 → a genuine cross-sectional fade).
**Risk metrics were ~3× too rosy and are corrected:** DD 2–3% (realized-at-exit) → 4–5% (daily MTM) → **6–7%
(hourly/intraday MTM)**; honest **MAR ~16 bybit / ~23 binance, Sharpe ~10**, all-weather both venues — not the
compounding-inflated 55–62. Residual Sharpe ~10 is still too high to deploy and is NOT a bug: the un-closable gaps
are OOS persistence (in-sample STR + heavy search), borrow/short-availability on pumped alts, and sub-hourly
squeezes — only the forward demo settles them.

## CV1 (2026-05-30): the cross-venue asymmetry is BREADTH + composition, NOT edge-quality

Read-only decomposition of the age-gated ledgers both venues
(`scripts/cv1_cross_venue_decomposition.py`, EXPLORATORY). The standing "Bybit strong /
Binance weak" caveat is substantially a **misframe**:
- **The per-trade edge is venue-GENERAL.** On the 164 events that fire on the *same coin,
  same day* on both venues, outcomes correlate **0.89** and Binance is marginally *better*
  (matched median net +0.52% vs +0.40%, paired diff ≈ 0). Per-venue per-trade net is
  near-identical (bybit median +0.34% / win 63%; binance +0.27% / win 61%). The edge is
  **not** Bybit-specific.
- **The aggregate gap (age-gated sum +72% vs +29%) is BREADTH + composition.** Bybit fires
  ~1.9× more events (579 vs 307; 270 vs 182 symbols) — Binance USD-M has fewer mid-liquidity
  alt perps in the rank-31–400 band. And Binance's **venue-unique** coins (on Binance, not
  Bybit) are weak marginal shorts (mean **−0.05%**, sum −7.4%, win 57%) — less liquid (rank
  ~92 vs 69), weaker spike (turnover ~9× vs 14×), worst recently — whereas Bybit's
  venue-unique coins are fine (+0.30% median, +45% sum).
- **No clean single-feature filter removes the weak coins.** Per-trade net vs turnover-ratio
  is weak/non-monotone (corr +0.08 bybit / +0.04 binance; venue-best buckets differ), and
  rank-tightening was already rejected (E2). The weak binance-only coins can't be filtered
  without dropping good events.

**Implication:** the edge is **robust and replicates cross-venue on shared (high-quality)
events** — reassuring for the real-money robustness question; Binance is **breadth-limited**
(fewer/lower-quality unique events), not edge-weak. The MAR 2.76-vs-0.28 gap is the
aggregate-return + drawdown denominator driven by breadth, not a weaker signal. (The recent
per-trade mean decay is real on **both** venues — tail-driven — and remains the genuine
standing caveat.) JSON: `~/SHARED_DATA/cv1_cross_venue_2026-05-30.json`.

## RD1 (2026-05-30): the recent decay is SQUEEZE-driven — and the rmom gate fixes it

Following CV1's one genuine caveat (recent per-trade MEAN decay, both venues, tail-driven),
RD1 dissects the recent (≥2025-06-01) tail of the age-gated book
(`scripts/rd1_recent_decay_rmom.py`, EXPLORATORY):
- **Mechanism — idiosyncratic strength vs a weak market.** Recent losers are overwhelmingly
  **stop-outs** (bybit 81, binance 57 of recent losers) and cluster on days the **broad market
  is DOWN** (losers: `market_pct_up_1d` ~0.28–0.32, `market_median_return_1d`<0, BTC down;
  winners: `market_pct_up` ~0.44–0.47). A coin pumping *against* a weak market has **genuine
  idiosyncratic strength** and keeps running → squeezes the short. (Counterintuitively the short
  works BEST when the event rides a broad up-market that mean-reverts — the *opposite* of "don't
  short a rally"; a `market_pct_up` ceiling would remove the winners, not the losers.)
- **Fix — the residual-momentum gate (P3b) removes exactly these names.** rmom shorts
  idiosyncratically-WEAK candidates, filtering the strong-against-weak-market squeeze coins.
  Recent tail, age-gated baseline → rmom-gated: **stop-out losers cut ~75%** (bybit 81→19,
  binance 57→14), worst-decile drag halved, recent per-trade **mean ~5–17×** (bybit
  +0.08%→+0.39%, binance +0.02%→+0.35%), recent **sum** bybit +22.6%→+71.0% / binance
  +4.2%→+42.1%, **both venues.** Selective (median rises too; not just fewer trades).

**So** the recent decay (the standing caveat) is **squeeze-driven**, and the already-validated,
PIT-clean **rmom gate is the mechanistic fix** — it removes the idiosyncratic-strength squeezes
the age-gated book still takes. This both **explains WHY the rmom gate works** (it is a squeeze
filter, not just "return 2–3×") and **strengthens the case to forward-demo it** (it addresses the
very thing — recent decay — that most threatens a forward demo). Profile change is operator-gated.
JSON: `~/SHARED_DATA/rd1_{bybit,binance}_2026-05-30.json`.

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
