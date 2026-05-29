# Research-program state

**Last updated:** 2026-05-29 (R1–R6 COMPLETE; engine RE-BASELINED honest by `9f52819`+`b1a3368` — 100% taker / bar_extreme stops / calendar returns / permutation-null. **R9 / DAILY ARCHITECTURE A = DOCUMENTED NULL** under honest methodology: every pre-registered daily lever tested — best stack (drop_all_4 entries + risk_equal 2% sizing + ff6_4pct exit) = **bybit MAR 1.39 (real edge) / binance −1.3% (no edge)** → fails the cross-venue Tier-2 bar. The earlier +0.45 demo-eligibility was a pre-hardening optimistic-stop-fill artifact (#14). **DECISION: DO NOTHING** — frozen promoted profile unchanged, nothing promoted. R12 sniper + C0–C3 continuous (Architecture B) = **operator decision** (~week build, low prior given the daily null). [R9 verdict](docs/preregistration/round2/r9-integrated-strategy-verdict.md).)

> If you are a Claude session opening this repo for the first time, read this
> file FIRST. It tells you in 60 seconds what's been done, what's running,
> and what's next.

## TL;DR

- Strategy: Bybit (+Binance) liquidity-migration short, **research-stage**.
  Live demo + paper run a frozen "promoted" profile; not deployed for real money.
- **Cadence direction:** the deployed signal is **Architecture A** (daily-close
  features, +1h entry delay — what Round 1 and all R-phases tested). The active
  *direction* is a lowest-latency, **fully event-driven** system: the runtime is
  already event-driven (WS bar-close wakes, WS stop enforcement), and **Architecture
  B** (continuous rolling-window signal, C-phases C0–C3) is the pre-registered path
  off daily-frequency. See "Two signal architectures in scope" below. Moving the live
  signal off daily requires the C0 engine + OOS re-validation — it is not yet shipped.
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
- **LEAD R1 CANDIDATE `R1_drop_all_4` — FALSIFIED under the hardened engine
  (2026-05-29 re-baseline).** Its original demo-eligibility (pooled MAR Δ **+0.45**) was
  substantially a **pre-hardening optimistic-stop-fill artifact**. Under the honest
  engine (`bar_extreme` stops + 100% taker + calendar returns) it **FALSIFIES Tier-2**:
  pooled MAR Δ **+0.05** (< +0.1 bar), **binance return NEGATIVE** (−0.25× at 45bps,
  −0.12 sum-net at honest 15bps), bybit edge real-but-weak (MAR Δ +0.15, bootstrap
  P(Δ>0)=87%) vs binance no-edge (P=25%). DD blew out (bybit −10.6%→−30.5%, binance
  −13.9%→−47.3%). **Re-baseline cascade premise FALSIFIED** — R2/R13/R5 carry-forwards
  are not demo evidence on their own; R9 `R9_event_only` baseline = production, not
  drop_all_4. Program continues to its pre-registered R9 integrated-stack test (R4 factor
  caps target the DD blowout); **default do-nothing if R9 falsifies.** Verdicts:
  [original](docs/preregistration/round2/r1-per-filter-audit-verdict.md) ·
  [hardened re-baseline](docs/preregistration/round2/r1-rebaseline-hardened-verdict.md).
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
| R1 | Per-filter hypothesis audit (softer criterion) | **COMPLETE (full-PIT, 2026-05-29).** 14/14 cells `full_pit_universe`. `drop_all_4` DEMO-ELIGIBLE (pooled MAR Δ +0.45) → re-baseline cascade TRIGGERED. Tag `r1_filter_audit_max12_2026-05-28`. **HARDENED RE-BASELINE (2026-05-29): `drop_all_4` FALSIFIES Tier-2** — pooled MAR Δ +0.45→+0.05, binance ret +0.56×→−0.25× (bar_extreme stops tripled DD); demo-eligibility was a pre-hardening stop-fill artifact. [original verdict](docs/preregistration/round2/r1-per-filter-audit-verdict.md) · [re-baseline verdict](docs/preregistration/round2/r1-rebaseline-hardened-verdict.md) |
| R2 | Per-feature standalone decile-sort + correlation matrix | **COMPLETE (full-PIT, 2026-05-29).** The 5 Phase-5 IC features collapse to ONE dominant factor (PC1 = 87.8% bybit / 81.4% binance of decile-P&L variance; pairwise Spearman 0.72–0.92) — the plan's 2-orthogonal-factor premise fails. Tag `r2_per_feature_2026-05-29`. [verdict](docs/preregistration/round2/r2-per-feature-standalone-verdict.md) |
| R3 | Bearish stack honest test (H2 retried) | **COMPLETE (full-PIT, 2026-05-29).** H2 DECISIVELY CLOSED — the mirror-imaged bearish stack produces 0 trades on BOTH venues (load-bearing filters, not the quality gates). Tag `r3_bearish_stack_2026-05-29`. [verdict](docs/preregistration/round2/r3-bearish-stack-verdict.md) |
| R4 | Risk-factor model construction (JS-style, 8 factors) | **COMPLETE (full-PIT, 2026-05-29).** 6 validated factors (dropped XS-3d-momentum: sign-flip factor return; alt-season + CLI deferred, off critical path). All 3 criteria pass both venues; variance-capture via the HONEST within-day permutation null (p=0.0 both venues — not the in-sample tautology; audit2 `b1a3368` A1), residual mean ~0 → Tier-3 residual-Sharpe machinery confirmed (incl. B1 `decompose` entry-ts fix). Tag `r4_risk_model_2026-05-29`. [verdict](docs/preregistration/round2/r4-risk-model-verdict.md) |
| R5 | 1/realized-vol position sizing | **COMPLETE (full-PIT, 2026-05-29).** Every `risk_equal` cell REJECTED (Tier-1 falsifier, bybit MAR Δ ≤ −1.0) → dollar-equal sizing stays; R9 uses dollar-equal. Tag `r5_position_sizing_2026-05-29`. [verdict](docs/preregistration/round2/r5-position-sizing-verdict.md) |
| R6 | Per-name per-bar cost model | **CODE COMPLETE (2026-05-29).** `cost_model.py` — surface + OLS fit + per-trade predict + ledger recosting (model-vs-legacy) + summary; 12 tests. Default = honest 15bps taker (supersedes legacy ×3 = 45bps over-count). **β-calibration DATA-GATED** on ≥30d VPS demo/paper → queued (turnkey recipe: `reconcile_paper_demo` + `fit_cost_model`); per-cell delta folds into R9 run-up. [verdict](docs/preregistration/round2/r6-cost-model-verdict.md) |
| R7 | Stress test suite (named historical events) | not started (R4✓ + R6✓ deps met) |
| R8 | Capacity analysis (per-cell AUM ceiling) | not started (R6✓ dep met; needs the size/ADV term — present in cost_model) |
| R9 | Integrated strategy assembly | **DOCUMENTED NULL (full-PIT, honest engine, 2026-05-29).** All daily levers tested via hardened re-baselines + IC pre-check (no blind 7-cell build needed — diagnostics determine each cell). Best stack (drop_all_4 + risk_equal 2% + ff6_4pct) = bybit MAR 1.39 (real edge) / binance −1.3% (no edge) → fails cross-venue Tier-2. DO NOTHING. [verdict](docs/preregistration/round2/r9-integrated-strategy-verdict.md) |
| R10 | Promotion-bar validation sweep | **not run** — downstream of a Tier-2 demo-candidate, which does not exist (R9 null) |
| R11 | Pre-2023 OOS gate (mandatory final) | **not run** — no R10 finalist (R9 null) |
| R12 | **Sniper entry execution layer** — sub-1h fill optimization on top of daily signal: 1m kline ingestion (R12a), simulator (R12b), univariate test of 5 sniper flavors (R12c), R9 integration (R12d), entry-delay reduction sweep (R12e), sniper stress test (R12f). Missed fills counted as $0-P&L. | **OPERATOR DECISION** — not auto-pursued: entry-fill optimization cannot create the absent binance edge (R9 null); building it on a no-edge daily strategy is unjustified. |
| C0 | **Continuous-signal engine** — rolling-feature registry + K-minute step backtest engine + regression validation (continuous at 1d step + 24h window = numerically equivalent to the daily backtest, `np.allclose` — per the progressive standard, not bit-identical). The foundation for Architecture B. | **OPERATOR DECISION** — ~5-7 day build, low prior given the daily R9 null (same features, which anti-select within events; binance no edge). The only genuinely-untested track; not auto-pursued per the "default do-nothing" + don't-run-expensive-research-on-a-falsified-premise. |
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
- **NO research runs currently in-flight.** R1 + R13 + R5 + R2 + R3 + R4 COMPLETE
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
  - **R4 risk-factor model COMPLETE** (full-PIT, 2026-05-29; gates R7 / R9
    factor-caps / Tier-3 residual-Sharpe; map in memory `r4-risk-model-implementation-map`).
    `liquidity_migration/risk_model.py` — `build_factor_panel` + `fit_factor_returns`
    (per-day XS OLS → factor returns + residuals) + `decompose_strategy_pnl` (per-trade
    explained vs residual P&L → residual_sharpe = the Tier-3 gate input). 12 unit tests.
    **6 validated factors** (btc_beta, xs_rank_ret_30d, realized_vol_rank, funding_rate_z,
    liquidity_rank, premium_index_z); dropped xs_rank_ret_3d (sign-flip factor return,
    criterion-1 fail); alt-season + `risk-model` CLI deferred (off critical path). All 3
    pre-reg criteria pass both venues; variance-capture via honest permutation null
    (p=0.0 both venues, audit2 `b1a3368`; the prior "~47% explained" was an in-sample
    tautology — corrected), residual mean ~0. Tag `r4_risk_model_2026-05-29`;
    [verdict](docs/preregistration/round2/r4-risk-model-verdict.md).
  - **⚠️ ENGINE RE-BASELINED by `9f52819`** (methodology hardening, concurrent
    session; pre-reg `docs/preregistration/round2/r-audit-methodology-hardening.md`).
    New conservative defaults: 100% taker (`maker_fill_probability` → 0.0 in
    `configs/volume_alpha.default.yaml`, 15 bps RT), `stop_fill_mode=bar_extreme`, M4
    calendar-shift fwd-returns, M1 promotion gate enforces DD+Sharpe. Per its decision
    rule, prior cell-vs-control deltas are `exploratory` until re-run under these
    defaults. **R3** (0-trades, structural) ROBUST; **R4** (factor model) UNAFFECTED
    (re-validated, permutation null). **R1 RE-BASELINE DONE (2026-05-29):** `drop_all_4`
    **FALSIFIES Tier-2** under honest costs (pooled MAR Δ +0.45→+0.05, binance ret
    +0.56×→−0.25×; `bar_extreme` stops tripled DD) — demo-eligibility was a stop-fill
    artifact. [re-baseline verdict](docs/preregistration/round2/r1-rebaseline-hardened-verdict.md).
    R13/R5/R2 carry-forwards likewise are not demo evidence on their own; R9
    `event_only` baseline = production.
  - **R6 cost model CODE COMPLETE** (2026-05-29): `cost_model.py` surface + fit +
    predict + recost + summary, 12 tests; default 15 bps taker; β-calibration
    data-gated → queued; [verdict](docs/preregistration/round2/r6-cost-model-verdict.md).
  - **R9 IC-selectivity PRE-CHECK (2026-05-29) — FALSIFIED.** `scripts/r9_ic_selectivity_precheck.py`:
    composite IC (5 features, high=short) vs `gross_trade_return` within event trades is
    NEGATIVE and monotonic both venues (Spearman −0.16 bybit / −0.31 binance; top-IC
    quintile gross −0.036/−0.083, bottom +0.048/+0.060). The event trigger already selects
    the high-vol/extended basket (R2), so the marginal IC sorts the WRONG way → the
    pre-registered `event_AND_ic` / `ic_only_top_decile` cells would ANTI-select. The
    inverse (low-IC = better short) is positive in-sample but a post-hoc sign flip (not
    promotable; error #17). So IC selectivity cannot rescue the negative event return.
  - **DAILY ARCHITECTURE A = DOCUMENTED NULL (2026-05-29) → DO NOTHING.** All daily levers
    tested under the honest engine: R5 `risk_equal` 2% sizing (best; DD −47%→−22%) + R13
    `ff6_4pct` exit (best) on drop_all_4 = the strongest stack → **bybit MAR 1.39 (real
    edge) / binance −1.3% (no edge)** → fails cross-venue Tier-2. Tags
    `r9_event_sizing_hardened_2026-05-29`, `r9_exit_sizing_hardened_2026-05-29`.
    [R9 verdict](docs/preregistration/round2/r9-integrated-strategy-verdict.md). Frozen
    promoted profile UNCHANGED, nothing promoted. bybit-only edge is real but fails the
    pre-committed cross-venue bar (would need a NEW operator pre-reg).
  - **OPERATOR DECISION — Architecture B (C0–C3) / R12 sniper:** the only remaining
    pre-registered tracks; large builds, low prior given the daily null. NOT auto-pursued
    (default do-nothing + don't run expensive research on a falsified premise). **Loop
    PAUSED for operator steer.** limit-chase EXIT enable test-gated/post-validation.
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
