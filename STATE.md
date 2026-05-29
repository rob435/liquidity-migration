# Research-program state

**Last updated:** 2026-05-29 (R1+R13+R5+R2+R3 COMPLETE — H2 closed (bearish=0 trades); R9 = bullish stack only; R4 next)

> If you are a Claude session opening this repo for the first time, read this
> file FIRST. It tells you in 60 seconds what's been done, what's running,
> and what's next.

## TL;DR

- Strategy: Bybit (+Binance) liquidity-migration short, **research-stage**.
  Live demo + paper run a frozen "promoted" profile; not deployed for real money.
- **Round 1 (7 phases, 2026-05-27): COMPLETE — documented null result.**
  H1, H2, H3, H5, H7 falsified; H4 not testable; H6 partially confirmed
  (5 features with stable cross-venue IC). Strategy unchanged.
  See [Round 1 verdict](docs/preregistration/round1/program-verdict.md).
- **Round 2 (11 sub-phases + R12 sniper + C-phases continuous, planned 2026-05-29):
  pre-registered, code work next.** Integrated strategy program: per-filter
  hypothesis audit + per-feature standalone tests + bearish-stack honest test +
  JS-style risk model + 1/realized-vol sizing + per-name cost model + stress
  test suite + capacity analysis + integrated strategy assembly + R10 promotion
  + R11 OOS. See [Round 2 plan](docs/preregistration/round2/integrated-strategy-program.md).
- **LEAD R1 CANDIDATE — CONFIRMED full-PIT (2026-05-29):** `R1_drop_all_4`
  (drops `day_return`, `stop_pressure`, `realized_loss`, `rank_max`) is
  **DEMO-ELIGIBLE** at R1 (Tier-2): pooled MAR Δ **+0.45** — Bybit **+1.30**
  (engine-DD MAR 3.84→5.14, ret +2.26×→+2.95×, DD −11.9%→−10.6%), Binance
  **−0.40** (MAR 1.16→0.75, DD −13.9%→−20.7%). Returns +ve both venues; trades
  816/509. **Re-baseline cascade TRIGGERED** — R2–R11 now compare vs the
  drop_all_4 stack. It is a **bybit-driven win with a real binance cost**
  (bootstrap binance MAR Δ p5 −2.07, P(Δ>0)=28% — likely a true degradation):
  demo candidate only. **Tier-3 real-money gate FAILS** (needs bootstrap pooled
  MAR-Δ p5 ≥ 0) — far from real money; frozen promoted profile unchanged.
  Verdict: [r1-per-filter-audit-verdict.md](docs/preregistration/round2/r1-per-filter-audit-verdict.md).
- **NEW optimization objective:** **(Return / Drawdown) tied as primary
  (i.e. MAR ratio), Sharpe as secondary tie-breaker.** This is a deliberate
  change from Round 1's implicit Sharpe-primary.
- **NEW decision-rule structure (AMENDED 2026-05-28):** three-tier,
  demo-arbiter. Investigation (carry forward) → Demo-candidate (LOOSENED:
  positive both venues + pooled MAR Δ > +0.1, fragility reported not
  blocking) → Real-money (STRICT: OOS + ≥30d demo + bootstrap + residual
  Sharpe + stress + capacity). Permissive where being wrong is free,
  strict where it's expensive; forward demo is the arbiter.
- Default action if Round 2 also produces a null: **do nothing.** Strategy
  stays in current state. Forward demo + paper continue.

## What's done

### Round 1 (2026-05-27 → 2026-05-28, complete)

| Phase | Verdict | Receipt |
|---|---|---|
| 0 (filter LOO) | REJECTED — 3 falsifiers (`crowding`, `event_rank_frac`, `turnover_ratio`) confirmed load-bearing | [verdict](docs/preregistration/round1/phase0-verdict.md) |
| 1 (universe diagnostic) | H1 FALSIFIED — universe widening hurts Sharpe but doesn't drive DD | [verdict](docs/preregistration/round1/phase1-verdict.md) |
| 2 (rank direction grid) | REJECTED — H2/H3 falsified-by-construction (filter stack excludes bearish names); Bybit-favorite cells sign-flip on Binance | [verdict](docs/preregistration/round1/phase2-verdict.md) |
| 3, 4 | NOT TRIGGERED — no Phase 2 candidate | — |
| 5 (signal harness IC) | 5 features survive at fwd_ret_3d — `vol_of_vol_30d`, `realized_vol_7d`, `dist_from_30d_low`, `xs_rank_ret_7d`, `xs_rank_ret_3d` (all negative IC = short-side) | [verdict](docs/preregistration/round1/phase5-verdict.md) |
| 6 (combined portfolio) | REJECTED — H7 FALSIFIED, every combined cell worse than event-driven | [verdict](docs/preregistration/round1/phase6-verdict.md) |
| 7 (OOS gate) | NOT TRIGGERED — no finalist from any phase | — |
| Program | DOCUMENTED NULL — strategy unchanged | [program verdict](docs/preregistration/round1/program-verdict.md) |

### Round 2 (2026-05-29, in setup)

| Sub-phase | Purpose | Status |
|---|---|---|
| R0 | Doc cleanup (delete unused Phase 7 pre-reg, update STATE.md) | complete (5dff927) |
| R1 | Per-filter hypothesis audit (softer criterion) | **COMPLETE (full-PIT, 2026-05-29).** 14/14 cells `full_pit_universe`. `drop_all_4` DEMO-ELIGIBLE (pooled MAR Δ +0.45) → re-baseline cascade TRIGGERED. Tag `r1_filter_audit_max12_2026-05-28`; ran `SWEEP_MAX_WORKERS=1` (23 GB/cell, 32 GB box — 8 OOMs). [verdict](docs/preregistration/round2/r1-per-filter-audit-verdict.md) |
| R2 | Per-feature standalone decile-sort + correlation matrix | not started |
| R3 | Bearish stack honest test (H2 retried) | not started — needs ~3h code (R3 filter flag additions) |
| R4 | Risk-factor model construction (JS-style, 8 factors) | not started — needs ~3 days code |
| R5 | 1/realized-vol position sizing | not started — needs ~1 day code |
| R6 | Per-name per-bar cost model | not started — needs ~2 days code |
| R7 | Stress test suite (named historical events) | not started (depends on R4+R6) |
| R8 | Capacity analysis (per-cell AUM ceiling) | not started (depends on R6) |
| R9 | Integrated strategy assembly | not started |
| R10 | Promotion-bar validation sweep | not started |
| R11 | Pre-2023 OOS gate (mandatory final) | not started |
| R12 | **Sniper entry execution layer** — sub-1h fill optimization on top of daily signal: 1m kline ingestion (R12a), simulator (R12b), univariate test of 5 sniper flavors (R12c), R9 integration (R12d), entry-delay reduction sweep (R12e), sniper stress test (R12f). Missed fills counted as $0-P&L. | not started — ~3-4 days code (R12a + R12b) |
| C0 | **Continuous-signal engine** — rolling-feature registry + K-minute step backtest engine + regression validation (continuous at 1d step + 24h window = bit-identical to daily backtest). The foundation for Architecture B. | not started — ~5-7 days code |
| C1 | **Continuous-signal univariate IC test** — Phase-5-equivalent on rolling-feature versions of the 5 IC survivors, at forward horizons {1h, 3h, 24h, 72h, 168h}. | not started — depends on C0 |
| C2 | **Continuous-signal R9 variant** — Architecture B's integrated-strategy assembly. 7 cells × 2 venues. | not started — depends on C0 + C1 |
| C3 | **Continuous-signal stress test** — R7 named-event replay applied to C2 promotion-eligible cells; flags WS-feed-fragile cells. | not started — conditional on C2 |

**Two signal architectures in scope.** Round 2 runs **Architecture A (daily, R-phases)** and **Architecture B (continuous, C-phases)** in parallel. They share R1-R8 + R10 + R11 infrastructure but differ in feature definitions and backtest framework. R10/R11 evaluate the best cell from each architecture independently; both, one, or neither may pass.

**No hard end-date on Round 2.** "Weeks if needed" per operator instruction.
With R12 + C-phases the total program estimate is **~2.5-3 weeks wall time**,
with ~10 days being code work (R4 risk model + R6 cost model +
R12a/b sniper + C0 continuous engine).

## What's running

- **Live demo** (Singapore VPS 5.223.42.109): event_demo_daemon +
  ws_risk_daemon + long_native_event_demo_daemon under systemd. Frozen
  promoted profile. Ledgers in `data/bybit-demo-event/`.
- **Paper shadow** (same VPS, same profile, no order submission):
  `data/bybit-paper-event/`.
- **NO research runs currently in-flight.** R1 + R13 + R5 + R2 + R3 COMPLETE
  (2026-05-29, all full-PIT). **R9 carry-forward stack (bullish event-driven
  only):** `drop_all_4` entries + `ff6_4pct` failed-fade exit + dollar-equal
  sizing + 1 composite IC factor (R2: 5 IC features → PC1 ≈ 82–88%, ρ 0.72–0.92
  → diversification-adjusted IC weighting). **R3 closed H2** — the
  bearish/deterioration mirror gives 0 trades both venues (the `turnover≥6`
  volume-spike trigger is structurally a pump detector); no bearish line, no
  market-neutral. Verdicts:
  [R13](docs/preregistration/round2/r13-exit-rule-verdict.md) ·
  [R5](docs/preregistration/round2/r5-position-sizing-verdict.md) ·
  [R2](docs/preregistration/round2/r2-per-feature-standalone-verdict.md) ·
  [R3](docs/preregistration/round2/r3-bearish-stack-verdict.md).
  - **R4 risk-factor model IN PROGRESS** (foundational code — gates R7 / R9
    factor-caps / Tier-3 residual-Sharpe; map in memory `r4-risk-model-implementation-map`).
    Chunk 1 committed: `liquidity_migration/risk_model.py` with `build_factor_panel`
    (reuses signal_harness daily-agg) + `compute_btc_beta` + 3 unit tests. **Next
    R4 chunks:** remaining 6 factors (xs-mom 3d/30d, realized-vol rank, funding-Z,
    liquidity tier, alt-season, mark-index premium) → `fit_factor_returns`
    (per-day cross-sectional OLS) → `compute_residual_returns` /
    `decompose_strategy_pnl` → `risk-model` CLI → validation run on full_pit roots.
  - **Then** R6 cost model, R12 sniper, C0–C3, R9 assembly → R10 → R11 OOS.
    limit-chase EXIT enable is safe (market fallback) but live-path — test-gated,
    post-backtest-validation.
- **5950X full-PIT op note:** one `volume-events` cell peaks ~23 GB → run sweeps
  at `SWEEP_MAX_WORKERS=1` (NOT the plan's 8, which OOMs); clear
  `<root>/.locks/*.lock` after any OOM/kill or a clean cell hangs ~6 h on
  orphaned locks.

## What's broken

Nothing known. Pre-push gate clean as of last check:
- `.venv/bin/python -m ruff check liquidity_migration tests` → clean
- `.venv/bin/python -m pytest -q` → all pass

## Decision rules currently binding (Round 2) — AMENDED 2026-05-28 (three-tier, demo-arbiter)

Principle: permissive where being wrong is free (backtest→demo), strict where
it's expensive (demo→real money). Forward demo/paper is the arbiter. Full text
+ rationale in the Round 2 pre-reg, "Strictness Manifesto v2 — AMENDED 2026-05-28".

### Tier 1 — Investigation (R1-R8 sub-phases) — unchanged
- MAR Δ > 0 on majority venues (2/2 OR 1/2 with other ≥ -0.5 MAR)
- No return sign-flip vs control; ≥30 Bybit / ≥20 Binance trades
- Falsifier: MAR Δ ≤ -1.0 either venue OR return negative OR DD > 70% OR <10 trades/sub-period

### Tier 2 — Demo-candidate (→ R11 OOS + forward demo) — LOOSENED
- Return positive on **both** venues (direction guard)
- **Pooled** MAR Δ > +0.1 (mean of the two venue MAR deltas — NOT symmetric)
- Neither venue worse than MAR Δ ≥ -0.5
- ≥30 Bybit / ≥20 Binance trades total
- Fragility diagnostics (bootstrap p5, LOO, sign-consistency, residual Sharpe) REPORTED, non-blocking — set demo order, not eligibility

### Tier 3 — Real-money (demo → mainnet) — STRICT, not loosened
- R11 pre-2023 OOS pass: MAR > 0 both venues all 3 sub-periods; DD < 50%; sign-consistent; ≥20 Bybit / ≥15 Binance trades/sub-period
- ≥30 days forward demo + daily paper-shadow reconciliation
- Block-bootstrap pooled MAR-Δ p5 ≥ 0 (seed=0, block=3mo, n=5000)
- Residual Sharpe ≥ +0.3 (after R4)
- R7 stress pass + R8 capacity ≥ 10× deployment size

Multiple-testing control: the demo treadmill itself (fresh forward data can't
be overfit). Only finite surface capped: **max 5 cells consume the pre-2023
OOS / quarter**; forward demo uncapped.

`scripts/r1_robustness.py` emits the **Tier 2 demo-candidate verdict**
(pooled MAR Δ > +0.1, engine-DD MAR) + the fragility diagnostics (bootstrap
p5, LOO, thirds) from the per-cell ledgers. `scripts/apply_decision_rule.py
--rule manifesto` remains the old strict Sharpe bar (legacy reference only).

## Helpers (when you need them)

- **CLI baseline wrapper:** `scripts/volume_events_cell.sh --cell-id X --overrides 'KEY=VAL,…'`
  — fills in the 30+ baseline flags so a cell only specifies what differs.
- **Decision-rule analyzer:** `scripts/apply_decision_rule.py SUMMARY.csv --control 00_baseline`
  — applies the Manifesto per cell, prints structured verdict.
- **Skill `research-phase-runner`** (auto-loads on phase-related tasks) —
  codifies the per-phase workflow (pre-check, dispatch, decision-rule,
  STATE.md update, commit verdict).
- **MCP tools** on `liqmig-research`:
  - `current_state` — read this file into structured form
  - `apply_decision_rule(summary_csv, control_cell)` — programmatic verdict
  - `data_roots`, `list_reports`, `parse_report`, `audit_run_artifacts`
- **Signal harness (Round 1 deliverable):** `python -m liquidity_migration signal-harness {build-panel, compute-ic, combined-portfolio}`
- **Sweep orchestrators (current — Round-1 phase scripts removed in the
  2026-05-28 cleanup; r1/r13 are the canonical patterns to adapt for R2/R3/R9):**
  - `scripts/r1_filter_audit_sweep.py` — R1 filter audit (the active sweep).
  - `scripts/r13_exit_rule_sweep.py` — R13 exit-rule re-opt (ready; dispatch after R1).
  - `scripts/_sweep_runtime.py` — shared parallel orchestrator (`Cell` + `run_sweep`), reused by every sweep.
  - `scripts/r1_robustness.py` — Tier-2 demo-candidate verdict + fragility diagnostics from per-cell ledgers.

## Non-negotiables (every session)

1. Pre-push gate (`ruff` + `pytest`) before every `git push`.
2. Never `REAL_MONEY=true`. Demo + paper only.
3. Never commit or push without operator confirmation.
4. Never modify `docs/backtesting_errors_we_never_repeat.md`,
   `docs/parameter_pre_registration.md`, or `configs/volume_alpha.default.yaml`
   without operator instruction.
5. Round 2's decision-rule structure (three-tier, demo-arbiter; AMENDED
   2026-05-28 by operator instruction, on principle) is pre-committed — no
   FURTHER loosening to rescue a specific cell. The amendment moved heavy
   stats to the demo→real-money gate and made the backtest→demo gate
   permissive; the real-money (Tier 3) gate is NOT loosened.
6. MAR-primary, Sharpe-secondary is pre-committed — no flipping back to
   Sharpe-primary mid-program.
7. Strategy stays at current frozen promoted profile until R11 passes
   AND ≥30 days forward demo evidence accumulates.

## How to update this file

Update STATE.md as part of every sub-phase verdict commit. The skill
`research-phase-runner` handles this. Keep under 200 lines; if it grows,
push detail into the sub-phase verdict docs.
