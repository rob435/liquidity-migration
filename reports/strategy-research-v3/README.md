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
