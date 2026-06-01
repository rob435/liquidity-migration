# Pre-registration — P0: the CONTINUOUS rmom fade (age300+rmom rolling signal, all-weather?)

**Date:** 2026-05-31 · **Stage:** EXPLORATORY (look-ahead decile characterization; NOT promotion evidence)
**Plan:** `docs/research_plan_continuous_fade.md` (Phase 0 / §6 — the highest-information first run)
**Standard:** `docs/backtesting_errors_we_never_repeat.md` · `docs/parameter_pre_registration.md`
**Run on:** the local 34 GB box, `~/SHARED_DATA/{bybit,binance}_full_pit` (read-only; one venue at a time).

## Hypothesis (the §6 fork, frozen before the run)

The deployed daily strategy is all-weather; the c2b *continuous* (rolling-decile) short with the
**age300** gate flips full-window-positive but is **recent-only** (early 2023–25 short_only & beta-neutral
L/S NEGATIVE both venues; positive only in the 2025–26 alt-bear) — the BB2 trap. **H1:** the missing
ingredient is the **residual-momentum squeeze-filter** (P3b/RD1 — short idiosyncratically-WEAK names,
which on the daily book removes ~75% of recent stop-out squeezes). Applying it to the continuous panel
makes the rolling short **all-weather** (early positive both venues) → BB2 was a c2b universe-composition
artifact, the continuous edge is real → fast-track to the engine-grade rolling backtest (A3/H3).
**H0:** still early-negative → BB2 structural → pivot to Avenue C (regime gate / market-neutral) or file
the null ("the daily cadence is load-bearing").

## Method (pre-registered; three read-only runs on the same hourly panel)

The c2b object: hourly rolling features (`rv_168h, vov, dist_low, xsret7, xsret3`, all trailing/closed-bar
= PIT-causal) → within-ts composite decile; **D9 (top composite) = the short**, D0 = the long leg.
Read-outs: short_only_net (`−D9_fwd − 15bps`), beta-neutral L/S net (`D0_fwd − D9_fwd − 2×15bps`), per
hold horizon. Reproduces c2b's `baseline`/`age_ge_300` columns exactly (port-correctness check).

- **P0** (`scripts/p0_continuous_age_rmom_split.py`): variants `baseline`, `age_ge_300`, `rmom_lo`
  (within-ts LOW-residual-momentum half), `age_ge_300_rmom_lo`; full + EARLY/RECENT split (2025-06-01)
  at 24/72/168h. rmom joined **causally** from `<root>/residual_momentum.parquet` (daily lag1 PIT signal,
  day-floor join: `rmom[D]` uses residuals ≤ D−1, known at 00:00 D, so an hourly bar at t ≥ 00:00 D is
  causal).
- **P0b stress** (`scripts/p0b_continuous_rmom_stress.py`): (A) **entry-latency** — re-price the forward
  return at entry delay {0,1,3}h (the mandatory backtest-integrity gate: real reversal vs closing-print
  artifact); (B) **threshold monotonicity** — rmom bottom-{50,33,25}%; (C) **finer time-buckets** —
  2023 / 2024 / 2025H1 / recent (the c2b coarse-split trap check).
- **P0c funding** (`scripts/p0c_continuous_funding.py`): charge realized **funding-to-exit** over the
  [t, t+24h] hold (short receives +Σ funding_rate, gap-robust as-of cumsum), both venues, per bucket.

## Decision rule (pre-committed, from §6)

- **PASS (optimistic branch → A3/H3):** age+rmom (or rmom) makes the rolling short **all-weather** —
  EARLY short_only AND beta-neutral L/S positive on **BOTH** venues — and survives the +1h latency gate.
- **FALSIFIER:** still early-negative on either venue/readout → BB2 structural → Avenue C / file the null.
- **INVALID** if the rmom join leaks (non-causal), the c2b columns fail to reproduce, or the edge is a
  same-bar artifact (collapses at +1h).

## Post-run results (2026-05-31; artifacts in `~/SHARED_DATA/p0*_2026-05-31.{json,out}`)

**Port check:** `baseline`/`age_ge_300` reproduce the committed c2b JSON to ±0.5 bps (e.g. bybit age300
@168h split: early −44/−14, recent +194/+75 — matches c2b −44.4/−13.6, +193.9/+75.0). Trustworthy.

**The all-weather test — EARLY/RECENT short_only & L/S net @168h (bps):**

| variant | bybit EARLY s/ls | bybit RECENT s/ls | binance EARLY s/ls | binance RECENT s/ls |
|---|--:|--:|--:|--:|
| age_ge_300 | **−44 / −14** | +194 / +75 | **−18 / −12** | +161 / +104 |
| **rmom_lo** | **+82 / +92** | +181 / +43 | **+65 / +41** | +176 / +67 |
| age_ge_300_rmom_lo | +50 / +62 | +266 / +127 | +31 / **−5** | +278 / +191 |

age300-alone is recent-only (BB2 confirmed). **The PIT-clean rmom squeeze-filter flips it all-weather on
both venues, both readouts. The active gate is rmom, not age** — and rmom-alone is cleaner than the stack
(stacking age thins the cross-section; binance L/S −5). **BB1 also resolved:** the rmom-gated D9 mean-fwd
is NEGATIVE at every horizon incl. 1h (bybit −15/−41/−149/−146/−128 @1/3/24/72/168h) — these names *fade
from hour one*, so rmom **is** the causal "fade-has-started" confirmation (not catch-the-top; contrast
baseline D9 which RISES +2/+16/+27 → shorting into continuation = the c2b wrong sign).

**Stress (P0b), headline at the 24h hold (the sweet spot):**
- **(A) Latency — PASSES.** 24h early short_only keeps **~88% at +1h** (bybit +118→+104, binance +99→+86),
  ~66% at +3h, positive early+recent both venues both readouts at +1h. A smooth gradient = real multi-hour
  reversal, **not** a closing-print mirage (which would collapse).
- **(B) Threshold — MONOTONE.** Tighter rmom (q .50→.33→.25) *strengthens* uniformly (bybit h24 early
  +118→+138→+150; binance +99→+112→+124). Robust selection signal, not a knife-edge at 50%.
- **(C) Time-buckets — uniform at h24, concentrated at h168.** **h24 is positive in EVERY bucket** both
  venues both readouts (bybit short 2023 +91 / 2024 +111 / 2025H1 +185 / recent +170; binance +75/+90/+161/+163).
  **But h168 early is carried by 2025H1** (bybit 2023 short −11; binance 2024 short −19 / L/S −23; 2025H1 a
  +399/+427 outlier) → at the weekly hold the c2b regime-concentration reappears.

**Funding (P0c, 24h hold, funding-to-exit) — a NON-ISSUE both venues.** exit-coverage bybit 100% /
binance 99.8%; funding leg **+0 to −5 bps every period** (slightly POSITIVE early: 2023/2024 +2/+4):

| h24 rmom_lo, short funding-incl (bps) | bybit | binance |
|---|--:|--:|
| 2023 / 2024 / 2025H1 / recent | +93 / +115 / +184 / +165 | +77 / +94 / +158 / +160 |

The continuous rmom short **inherits the daily strat's funding-robustness**: selecting idiosyncratically-weak
*post-fade* names enters *after* the funding crowd clears (the I-phase deep reconciliation, now confirmed
for the continuous case). **Data correction:** binance `binance_usdm_funding` has 99.8% panel coverage and
is genuinely ≈0 — NOT "funding-blind/missing" (§1 saw ≈0 and mis-attributed it to sparsity; the near-zero
is real, matching binance's near-zero mean rate). Avenue F4 ("wire binance funding") is largely already done.

## Verdict — **PASS, optimistic branch, with two reframes** (EXPLORATORY)

The continuous liquidity-migration fade is **REAL and all-weather** when gated by the PIT-clean
residual-momentum squeeze-filter: uniformly positive across 2023/2024/2025H1/recent, both venues, both
short-only and beta-neutral, monotone in gate strength, surviving +1h/+3h entry latency and realized
funding cross-venue. **Both boss battles fall to one gate:** BB2 (recent-only) was a c2b
universe-composition artifact, not structural; BB1 (wrong-sign) is resolved because rmom selects names
already fading. Two honest reframes of the plan:
1. **The active gate is rmom, not age** (age300-alone stays recent-only; on the daily book age is the
   robust gate and rmom the fragile one — the continuous application *inverts* this because rmom is a broad
   cross-sectional signal that the narrow daily event-pool thins into a recent slice).
2. **The robust object is a ~24h idiosyncratic reversal, NOT the hypothesized "slow multi-day fade"** — at
   168h the regime-concentration (2025H1) returns; 24h is the all-weather sweet spot.

## Honest bounds (necessary, not sufficient — what this is NOT)

- **EXPLORATORY decile characterization**, per-ts mean forward returns — **NOT a tradeable backtest.** Gaps
  to a real engine (Phase 2/A3/H3): concurrency/overlap, **true turnover/churn cost** (hourly reformation;
  per-ts char overcounts distinct trades — the next cheap question, Avenue D), position sizing, capacity,
  borrow/slippage in squeeze names. Magnitudes are signal-characterization, not portfolio MAR.
- **Overlap:** 24h holds re-formed hourly → ~24× autocorrelation; `n_ts` overstates statistical precision
  (the all-weather-across-independent-regime-buckets criterion is what carries the conclusion, not a t-stat
  off n_ts).
- **Substantially the STR / residual-reversal factor** (rmom = factor-residual momentum) → "mostly factor
  exposure" like the daily strat (P2-1); unlikely to be unique Tier-3 alpha. It is a deployable continuous
  **factor-harvest** — which is what the mission asked for, but must not be sold as unique alpha.
- The engine-grade hourly-fired rolling backtest is a **genuine build** (the engine detects events on the
  daily grid; `cooldown_hours`/`hold_hours` exist but hourly event-firing does not) — **operator-gated**.

## Next (cheap → expensive)

1. **Avenue D (cheap, next):** turnover/persistence — how long does a name stay in D9, what is the real
   re-entry/churn, does a debounce/cooldown recover daily-pool stability without killing the edge? This is
   THE gap between the per-ts char and a tradeable book, and the plan's wide-open discretization question.
2. **STR-residualization (cheap):** residualize the continuous short through `risk_model` — confirm/bound
   how much is the known residual-reversal factor vs a liquidity-migration increment.
3. **A3/H3 engine-grade rolling backtest (expensive, operator-gated):** true concurrency/exit/turnover +
   funding-to-exit + residual, 24h hold, both venues, early/recent — the only thing that turns this
   characterization into a MAR a forward demo could arbitrate.

Artifacts: `~/SHARED_DATA/p0_continuous_age_rmom_2026-05-31.{json,out}`,
`p0b_continuous_rmom_stress_2026-05-31.{json,out}`, `p0c_continuous_funding_2026-05-31.{json,out}`.
Scripts: `scripts/p0{,b,c}_continuous_*.py`. Label: **EXPLORATORY** (never promotion evidence).
