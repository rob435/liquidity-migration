# Research-program state

**Last updated:** 2026-05-30. **All research findings, results, and verdicts now live in
one place: [docs/research_summary.md](docs/research_summary.md).** Round 1 + Round 2
per-phase plans and verdicts were consolidated there and removed (git history has the
originals). This file = live/operational state + the binding decision rules.

> First session in this repo? Read this file, then `docs/research_summary.md`.

## Current status (one paragraph)

Bybit (+Binance) liquidity-migration **short**, research-stage — the live demo + paper run
a frozen "promoted" profile; NOT real money. Round 2 initially concluded a documented null,
but on **2026-05-29 that was shown to be substantially a methodology artifact** — worst-case
`bar_extreme` stop fills + `max_active=3` over-concentration + a ×3 (45 bps) cost stacked
together. Under the realistic capped stop fill at `max_active=12`, the daily strategy is
**positive on both venues in-sample** (bybit +37.8% / −27.5% DD / Sharpe 0.70; binance
−4.7% net but gross +16.1%, ~breakeven at honest 15 bps). **Framing (E1-corrected 2026-05-30):**
the alpha is the **SELECTION** signal (the liquidity-migration event = candidate pool) +
a plain +1h short. **E1 + E1b falsified the EXECUTION half**: `fixed_delay` (immediate)
vs `promoted_quality_squeeze` (fade-confirmation) on the same pool gives no robust
cross-venue premium (pooled MAR Δ +0.01, Tier-2 `descriptive`; paired test noise; robust
to 6× engagement). Immediate-entry shorting alone is bybit **+67.3% / MAR 2.76** at honest
15 bps (binance +8.0%, funding-missing). At 1h granularity the fade-confirmation is a
near-no-op, so **E3 (sniper) is dropped** and **E2 pivots to SELECTION refinement**
(exhaustion-quality gate — older/liquid names, exhausting OI & taker-flow). Plan:
**`docs/research_plan_selection_execution.md`**; full detail + E1 verdict:
**`docs/research_summary.md`**. Nothing is promoted; forward demo is the arbiter.

## What's running

- **LONG sleeve `div` promotion (2026-05-30, code-complete, not yet deployed):** the
  `MultiStratV1` long-FC profile (`_v11a_long_native_config`) gained the `div`
  risk-engineering — universe 10→50, max_concurrent 5→10, de-risk-only vol-target
  (0.60 annual, max_scale 1.0). Cross-venue confirmed (both venues MAR up, DD lower,
  trades ~2×; figures in `docs/preregistration/div-promotion.md`). Portfolio construction, NOT a new signal
  (FC remains the alpha ceiling). Receipt: `docs/preregistration/div-promotion.md`.
  On deploy, the deploy date is the clean pre/post split for the `MultiStratV1` long
  ledger (the strategy_id was kept; a 12h sleeve was tested and **rejected** — additive
  on Binance but a drag on Bybit, fails the cross-venue bar).
- **Live demo** (Singapore VPS 5.223.42.109): `event_demo_daemon` + `ws_risk_daemon` +
  `long_native_event_demo_daemon` under systemd. Frozen promoted profile. Ledgers in
  `data/bybit-demo-event/`.
- **Paper shadow** (same VPS, same profile, no order submission): `data/bybit-paper-event/`.
- **No research runs in-flight.** (E1+E1b complete = SELECTION-dominant; E2 complete 2026-05-30.)
- **E2 RESULT (strong, the headline):** an **exhaustion-quality SELECTION refinement** —
  `--liquidity-migration-pit-age-days-min=300` (drop symbols younger than 300 days) —
  **~doubles daily-DD MAR cross-venue** (bybit +2.93→+5.96, binance +0.25→+2.81), is
  all-thirds-positive both venues, and **fixes the recent weak third on both** (bybit −2%→+25%,
  binance −26%→+4%). Mechanism verified: young-name shorts are systematic net losers (fresh
  listings squeeze); the signal works on seasoned names. This **explains the recent edge-decay**.
  `prior30-max-return-max=0.14` is a secondary cross-venue risk-reducer (halves DD).
  `universe-rank-max=110` (liq-tighten) REJECTED. Tier-2 demo-candidate, **in-sample — forward
  demo is the arbiter**. See `docs/preregistration/e2-exhaustion-selection-2026-05-30.md`.
- **Open action (operator's call — NOT done autonomously):** the live demo runs
  `pit-age-days-min≈90`; `age_min=300` is a strong candidate to test forward (profile change
  needs operator OK; hard-line).
- **E2b RESULT (confirms E2):** age sensitivity is **not a knife-edge** — dropping young
  names roughly doubles MAR across age 200/300/400 on **both** venues, all-thirds-positive
  both, recent-third improved both (bybit −2%→+17–24%), LOO-stable, bootstrap P(Δ>0) 86–96%
  (binance age400 p5>0). bybit saturates ~age200 (≈MAR 6); binance monotone to 400.
  `age400` joint-best (bybit 6.91 / binance 5.64, ample trades); `age300` conservative;
  `prior30+age` an optional DD-reducer (mild bybit fragility). `age-alone` is the primary
  robust refinement. **E2c: cost-robust** — at 3× cost (45 bps) the baseline degrades (binance
  baseline goes negative) but the age-gated book stays strongly positive both venues. **E2d:
  fill-robust too** — under worst-case `bar_extreme` fills the baseline goes negative on binance
  but the age-gated book stays strongly positive (bybit age300 +67%/MAR3.85; binance age300
  +27%/MAR2.00) — which **closes the loop on the original Round-2 null** (caused by worst-case
  fills on a universe incl. wild-wick young names; the age gate is the structural remedy). So the
  age gate is robust to **threshold + regime + cost + stop-fill** — in-sample validation is
  exhaustive; the only remaining gate is forward demo. See `docs/preregistration/e2b-age-combo-2026-05-30.md`,
  `e2c-age-cost-robust-2026-05-30.md`, `e2d-age-stopfill-2026-05-30.md`.
- **Continuous architecture (c2b, EXPLORATORY) — C0 NOT justified:** the age-gated continuous
  decile short looked cost-positive cross-venue @168h on the **full window**, but the recent/early
  split shows the edge is **entirely recent-regime (2025–26 alt-bear, substantially short-beta)** —
  even the beta-neutral L/S is **negative in the early 26 months** (−14/−12 bps) and only positive
  recent. So it's regime-conditional, not all-weather → **C0 build is NOT recommended on current
  evidence.** (Corrects an earlier over-claim.) The robust, all-weather result is the **discrete
  age gate**. Detail: `docs/preregistration/exploratory/c0-continuous-engine-scope-2026-05-30.md`.
- **P3b (BUILT + VALIDATED, operator-greenlit): residual-momentum SELECTION gate = robust Tier-2
  DEMO-CANDIDATE.** Integrated into the engine (commit 17df8ba; config
  `liquidity_migration_residual_momentum_max`, `<root>/residual_momentum.parquet` signal,
  scripts/precompute_residual_momentum.py; default-inactive, unit-tested, 1054 tests pass). Gated
  backtest (sweep tag `p3b_rmom_gate_2026-05-30`, gate=per-venue median rmom +0.1377/+0.1148):
  return 2–3×, Sharpe doubled, DD halved both venues; all-thirds-positive, LOO-stable, bootstrap
  p5≫0 → r1_robustness **DEMO-ELIGIBLE**. Tier-3 residual (overlap-aware): factor-neutralizes both;
  **binance certified (+1.10), bybit residual-neutral full-window (+0.00, +2.18 recent)** → NOT a
  clean cross-venue alpha cert. Value = risk-reduction + factor-neutralization + venue-asymmetric/
  recent residual alpha. See `docs/preregistration/p3b-rmom-gate-backtest-2026-05-30.md`.
- **Highest-value next step (operator's call):** forward-demo the **residual-momentum gate** (the
  strongest validated demo-candidate) and/or the discrete age gate (pit-age 300). The gate is a config
  change on the engine (`--liquidity-migration-residual-momentum-max <median>` + precomputed signal);
  the promoted profile is unchanged until you move it. Profile change needs operator OK (hard line);
  forward demo is the real Tier-3 arbiter for whether the residual alpha persists OOS.
  **Deploy paths + the rmom-gate live-pipeline prerequisite (same-code #16 gap): `docs/forward_demo_readiness.md`.**
  TL;DR: the **age gate** is deploy-ready (simple PIT feature, lowest friction — start here); the
  **residual-momentum gate** is the stronger result but its signal must be live-wired first (scheduled
  daily precompute extending `residual_momentum.parquet`, PIT-safe) before a faithful forward demo.
- **Open action (from the 2026-05-29 re-baseline):** the deployed demo runs `max_active=3`
  (worst day −36%, DD −87% under honest fills); the research-validated value is
  `max_active=12` (worst day −4.8%, DD −27.5%). Consider moving the demo to 12 and/or
  `risk_equal` sizing. See `docs/research_summary.md`.

## Engine defaults (current)

- **Stop fill: `bar_extreme_capped` (10% cap)** — realistic bad-case (caps adverse slippage
  at 10% beyond the trigger). `stop` (optimistic) and `bar_extreme` (worst-case) remain
  selectable via `--stop-fill-mode`.
- **Cost:** 100% taker; 15 bps base round-trip; sweeps default to ×3 = 45 bps (conservative).
- **Full-PIT universe required** (engine aborts on coverage gaps); the PIT gate is scoped to
  each symbol's traded span `[first_kline, last_kline]` (pre-listing/post-delisting empty
  phantoms excluded; mid-history gaps still caught).

## Decision rules currently binding — three-tier, demo-arbiter

Principle: permissive where being wrong is free (backtest→demo is paper), strict where it
costs real money. Forward demo/paper is the arbiter. MAR-primary (Return/Drawdown), Sharpe
secondary.

### Tier 1 — Investigation — unchanged
- MAR Δ > 0 on majority venues (2/2 OR 1/2 with other ≥ −0.5 MAR)
- No return sign-flip vs control; ≥30 Bybit / ≥20 Binance trades
- Falsifier: MAR Δ ≤ −1.0 either venue OR return negative OR DD > 70% OR <10 trades/sub-period

### Tier 2 — Demo-candidate (→ forward demo) — LOOSENED
- Return positive on **both** venues (direction guard)
- **Pooled** MAR Δ > +0.1 (mean of the two venue MAR deltas)
- Neither venue worse than MAR Δ ≥ −0.5
- ≥30 Bybit / ≥20 Binance trades total
- Fragility diagnostics (bootstrap p5, LOO, sign-consistency, residual Sharpe) REPORTED,
  non-blocking — set demo order, not eligibility

### Tier 3 — Real-money (demo → mainnet) — STRICT, not loosened
- Forward-demo OOS pass (no internal pre-2023 OOS root exists — pristine OOS = the forward
  demo/paper ledgers, per `docs/data_roots.md`): MAR > 0 both venues over the forward window;
  DD < 50%; sign-consistent
- ≥30 days forward demo + daily paper-shadow reconciliation
- Block-bootstrap pooled MAR-Δ p5 ≥ 0 (seed=0, block=3mo, n=5000)
- Residual Sharpe ≥ +0.3 (factor-model residual; foundation built + validated —
  `liquidity_migration/risk_model.py` `decompose_strategy_pnl`, see
  `docs/preregistration/r4-risk-model-verdict.md`)
- Stress pass + capacity ≥ 10× deployment size

The forward demo (fresh data can't be overfit) is both the multiple-testing arbiter and the
OOS surface — uncapped. `scripts/r1_robustness.py` emits the Tier-2 verdict + fragility from
per-cell ledgers; `scripts/apply_decision_rule.py` is the legacy strict (Sharpe) bar only.

## What's broken

Nothing known. Pre-push gate clean: `.venv/bin/python -m ruff check liquidity_migration tests`
+ `.venv/bin/python -m pytest -q` both pass.

**Fixed 2026-05-30 — PIT gate / reconcile plumbing** (was: backtest↔paper showed
spurious `pit_membership_fail`/`paper-only`). Root cause: PIT membership was keyed
on the signal *stamp* date (D+1, daily-close signals fire at 00:00 of the next day)
instead of the *trading* day; the archive only publishes the trading day, so fresh
signals never validated. Fix keys membership on `date(ts_ms-1ms)`
(`volume_events_features._attach_event_archive_membership`), proven on the real
Bybit manifest (HEMIUSDT et al. now pass). Plus: `pit_coverage.py` staleness check,
`download-data` coverage warning + `--refresh-manifest`, `volume-events
--pit-membership strict|current-universe`, richer reject diagnostics, and a
bash-3.2-safe `volume_events_cell.sh`. **One-command reconcile:
`bash scripts/reconcile.sh`** (skill `pit-reconcile`, design `docs/pit_gate.md`).
Op note: the 16 GB research box can't run a full `bybit_full_pit` cell (~23 GB).

## Helpers (when you need them)

- **Demo-forward reconcile (one command):** `bash scripts/reconcile.sh` — pull VPS
  ledgers → refresh manifest → coverage check → backtest → `reconcile-all` →
  summary. `--dry-run` to preview. Skill: `pit-reconcile`; design: `docs/pit_gate.md`.
- **CLI baseline wrapper:** `scripts/volume_events_cell.sh --cell-id X --overrides 'KEY=VAL,…'`
  fills the 30+ baseline flags (now bash-3.2-safe on macOS; `DRY_RUN=1` to preview).
- **Decision-rule analyzer:** `scripts/apply_decision_rule.py SUMMARY.csv --control 00_baseline`.
- **Tier-2 verdict + fragility:** `scripts/r1_robustness.py --sweep-tag <TAG>`.
- **Continuous-signal prechecks:** `scripts/c1_continuous_ic_precheck.py` (IC),
  `scripts/c2_continuous_tradeability_precheck.py` (decile L/S tradeability).
- **Skill `research-phase-runner`** (auto-loads) — per-phase run/verdict workflow.
- **MCP tools** on `liqmig-research`: `current_state`, `data_roots`, `list_reports`,
  `parse_report`, `audit_run_artifacts`, `apply_decision_rule`.
- **Full-PIT op note:** one `volume-events` cell peaks ~23 GB → run full-PIT sweeps at
  `SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8` (over-parallelizing OOMs the box); clear
  `<root>/.locks/*.lock` after any OOM/kill.

## Non-negotiables (every session)

1. Pre-push gate (`ruff` + `pytest`) before every `git push`.
2. Never `REAL_MONEY=true`. Demo + paper only.
3. Never commit or push without operator confirmation.
4. Never modify `docs/backtesting_errors_we_never_repeat.md`,
   `docs/parameter_pre_registration.md`, or `configs/volume_alpha.default.yaml` without
   operator instruction.
5. The three-tier decision structure is pre-committed — no further loosening to rescue a
   specific cell; the Tier-3 real-money gate is NOT loosened.
6. MAR-primary, Sharpe-secondary is pre-committed.
7. Strategy stays at the frozen promoted profile until the Tier-3 gate passes AND ≥30 days
   forward demo evidence accumulates.

## How to update this file

Keep it short (live/operational state + decision rules). Research results go in
`docs/research_summary.md`, not here. Keep under ~120 lines.
