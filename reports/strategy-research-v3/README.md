# Strategy Research V3 — exploratory execution (2026-07-19)

Lane-1 execution of `docs/preregistration/DRAFT_strategy_research_v3_2026-07-19.md`
on the Windows big PC. **Everything here is exploratory on the spent V2
discovery surface**: no alpha, robustness, candidate, deployment, or real-money
claim, and no change to the deployed runtime, profile, sizing, or the active
90-day epoch. The `[2025-01-01, 2026-07-06)` label-level V2 holdout was not
created, read, or partitioned; T-A touches that period only at the
equity-render level, which the deployed-profile renders had already covered.

## Layout

- `shared/<date>/` — verified input caches reused by every thesis (funding
  settlement panel, 1h kline slice, aux 1h panel) plus the validation manifest:
  the caches reproduce every modeled V2 ledger trade's funding exactly and the
  official `barebones_curve.parquet` to ≤ 4e-15 per day.
- `t-b/`, `t-c/`, `t-d/`, `t-a/` — one directory per thesis and date with
  `summary.md` (evidence-card-lite), all grid tables as CSV, and a
  `manifest.json` (inputs + SHA-256 hashes + code commit + declared grids).
  Heavy parquet intermediates stay local; their hashes are in the manifests.
- Code: `scripts/research_v3/` (read-only analysis entry points; the only
  runtime-adjacent change is the guarded, default-off
  `--research-disable-btc-gate` render flag in the standard equity chain).

## Outcomes at a glance (details and caveats in each summary.md)

| Thesis | Outcome |
|---|---|
| T-B funding floor | Floor rarely binds at the 12% TP shape; strictly-PIT variant not era-stable; advance-known-rate variant era-stable (+1.7 to +3.1pp) but PIT-validity needs venue-semantics verification. Drain-exit rule refuted (−5 to −11pp everywhere). |
| T-C pump deceleration | Premise refuted on this ledger: adverse paths concentrate in *decelerating*, not accelerating entries (era-stable). Delay rules neutral-to-worse; skip's gain is a mechanical trade-count effect. |
| T-D funding forecast | Predictability beyond persistence exists, concentrated in tails and 48–72h horizons (up to −17% tail MAE); declared Stage-2 bar (24h) not met, so the T-B floor substitution did not run. |
| T-A regime-gate ablation | Refuted: gate-off doubles entries but loses ~1.0pp return with ~5× the drawdown and twice the negative tail days; early era favors removal on return, late era decisively favors the gate. No prototype advances. |

Prototype advancement, if any, is a separate owner decision through the forward
rolling ledger (Lane 2) and the normal post-epoch deploy flow.

## Strategy Research V4 — exploratory execution (2026-07-19)

Lane-1 execution of `docs/preregistration/DRAFT_strategy_research_v4_2026-07-19.md`
in the same output root, same rules, plus the owner-directed
**double-verification rule**: a rule advances only if same-signed on the
barebones ledger, both T-A render books (gate on / gate off), and both eras.

- `shared/2026-07-19-render/` — render-window caches (1h klines and funding
  events for the T-A render books' symbols, 2023-03-26 → 2026-07-10).
- `t-e/` … `t-i/` — one directory per V4 thesis (same artifact conventions).
- Code: `scripts/research_v3/v4_shared.py`, `te_fresh_high.py`,
  `tg_funding_state.py`, `tf_mfe_giveback.py`, `th_expected_net.py`,
  `ti_regime_intensity.py` (T-I runs via `run_with_stub.py`).

## V4 outcomes at a glance (details and caveats in each summary.md)

**No thesis advanced a forward-ledger prototype.** The program-level finding:
entry-quality cuts discovered on the barebones surface (freshness, funding
state) show large era-stable gains there but invert sign on both deployed-shape
render books — they measure the barebones exit shape, not the entry.

| Thesis | Outcome |
|---|---|
| T-E fresh-high conditioning | At-high bucket era- and year-stable positive; skip rules gain up to +28pp on the ledger but would forfeit net-positive mass on both render books. Ranking signal, not a transferable filter. |
| T-G funding-state conditioner | All 9 cells positive in both eras on the ledger (best: forecast-qualified skip, +7.99pp); deep-neg entries are the render books' best bucket under both gate states. Shape artifact. Bybit timing semantics verified: "next-rate" rules not registrable post-2022-07 (`bybit_funding_timing.md`). |
| T-F MFE give-back ladder | Re-simulator engine-exact (16,745/16,745 exits reproduced); every cell fails — forfeited TP completions ≈ captured give-back, and the best cell inverts on the T-E axis. Adaptive exits dead on 1h and 1m granularities. |
| T-H expected-net ranker | Loses to the simple T-E∧T-G conditioner by an order of magnitude; 4/9 coefficients sign-flip across refits (declared refutation); mid-distribution ranking non-monotone. |
| T-I regime intensity | No member passes the registered MAR+tail rule. Linear intensity Pareto-dominates the binary gate on risk at equal net but MAR is ill-posed at negative net — recorded as a metric lesson. Two-sided refuted. |

## V5 — deployed-book conditioning search (2026-07-20)

`t-j/` moves the discovery surface to the deployed-shape render books
(`scripts/research_v3/tj_deployed_conditioning.py`, run via `run_with_stub.py`).
Three candidates, judged with era/component/permutation/tail/concentration
controls: the exit-geometry hypothesis is killed by anatomy (identical exit
shape; the deployed edge is selection), the deep-neg gate override is refuted
on barebones, and the freshness sizing tilt fails on the deployed book because
its selection already saturates the signal (at-high ≈ 62% of notional; tilt
inside permutation noise). Program-level answer: the deployed sleeve is at a
local optimum for every coarse 1h observable measured — which is why closures
are fast. One lead survives (blocked at-high entries, newest era only) and is
frozen as a Lane-2 forward-ledger prototype
(`t-j/2026-07-20/prototype_freshness_gate_override.json`); its evidence is
post-commit forward days only.
