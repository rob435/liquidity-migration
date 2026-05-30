# Forward-demo readiness — the two validated demo-candidates

**2026-05-30.** Part 1–3 produced two validated, operator-gated demo-candidates. This note bridges
the backtest results to *live deployment* honestly — including the same-code (#16) gap that the
residual-momentum gate carries and the age gate does not. **Nothing here is deployed; the promoted
profile is unchanged.** Moving the demo profile is an operator decision (hard line).

## Candidate A — the discrete AGE GATE (deploy-ready, low-friction)

**What:** `--liquidity-migration-pit-age-days-min=300` (drop symbols younger than ~300 d).
**Evidence:** ~doubles MAR cross-venue; robust to threshold (E2b), regime (E2, all-thirds-positive),
cost (E2c, 45 bps), and worst-case fills (E2d). Tier-2 demo-candidate. Mostly factor-neutralization,
not unique alpha (P2-1), but a robust risk-adjusted improvement.
**Live readiness: HIGH.** `pit_age_days` is a simple PIT feature the engine + live runner already
compute (the promoted profile already uses `pit-age-days-min=90`). Moving to 300 is a pure config
change — no new pipeline. **Lowest-friction forward demo.**

## Candidate B — the RESIDUAL-MOMENTUM GATE (strongest result; carries a live-pipeline prerequisite)

**What:** `--liquidity-migration-residual-momentum-max=<per-venue median>` (keep low residual-momentum
candidates) + the precomputed `<root>/residual_momentum.parquet` signal.
Bybit median +0.1377, binance +0.1148 (in-sample; see p3b verdict).
**Evidence:** the strongest Tier-2 result — DEMO-ELIGIBLE, return 2–3×, Sharpe doubled, DD halved,
all-thirds-positive, LOO-stable, bootstrap p5≫0. Tier-3 residual: binance certified (+1.10), bybit
residual-neutral full-window (+0.00, +2.18 recent) — not a clean cross-venue alpha cert.

**Live readiness: MEDIUM — needs the signal pipeline operationalized first (the honest gap).**
The gate reads `residual_momentum.parquet`, which is **precomputed offline** by
`scripts/precompute_residual_momentum.py` (build_factor_panel → fit_factor_returns → trailing
residual). For a forward demo this is a **same-code-illusion risk (#16)**: the live/demo runner does
*not* currently compute the 6-factor model, so to trade this gate forward you must EITHER
(a) re-run the precompute on a schedule (e.g. daily) so the signal extends to each new decision day
*before* that day's selection — PIT-safe because the signal uses only residuals strictly before the
signal day; OR (b) wire an incremental factor-model/residual computation into the live runner.
Until (a) or (b) exists, the gate is **backtest-validated but not live-wired** — do not treat a
config flag alone as a faithful forward test.
- The per-venue median threshold is in-sample; for live use, either freeze the in-sample medians
  (+0.1377 / +0.1148) or recompute the median on a trailing window (a small live design choice).
- The threshold should also be re-derived per venue as the universe evolves (the median drifts).

## Recommendation

1. **Forward-demo the AGE GATE now** (Candidate A) — lowest friction, robust, no pipeline gap; it is
   the cleanest way to start accumulating the real OOS (Tier-3) evidence the whole program defers to.
2. **In parallel, operationalize the residual-momentum signal pipeline** (option a: scheduled
   precompute extending `residual_momentum.parquet` daily, PIT-safe), then forward-demo Candidate B.
   It is the stronger result but must be live-wired faithfully first.
3. The forward demo is the real Tier-3 arbiter for both — especially for whether the residual-momentum
   gate's binance alpha and recent bybit residual persist OOS (the recency tilt is the key risk).

Both remain demo/paper only until the Tier-3 forward-demo gate (≥30 days + reconciliation +
residual-Sharpe) passes — `REAL_MONEY` stays off (hard line).
