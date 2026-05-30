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
−4.7% net but gross +16.1%, ~breakeven at honest 15 bps). **Framing (corrected):** the
strategy is a SELECTION signal (the liquidity-migration event = candidate pool) + an
EXECUTION signal (enter on the fade-confirmation — pop then giveback, "fade the fade", NOT
short-at-the-top; this is `promoted_quality_squeeze`). The continuous variant carries real
cross-venue selection IC but was only ever tested with *immediate* entry — its "null" is
about timing the top, not the signal; applying the execution layer (+ sniper) to it is the
open lead. **Forward plan (narrow, for the 5950X): `docs/research_plan_selection_execution.md`**
— E1 execution-premium → E2 continuous+execution → E3 sniper. Full detail:
**`docs/research_summary.md`**. Nothing is promoted; forward demo is the arbiter.

## What's running

- **LONG sleeve `div` promotion (2026-05-30, code-complete, not yet deployed):** the
  `MultiStratV1` long-FC profile (`_v11a_long_native_config`) gained the `div`
  risk-engineering — universe 10→50, max_concurrent 5→10, de-risk-only vol-target
  (0.60 annual, max_scale 1.0). Cross-venue confirmed (Bybit MAR 1.46→1.58, Binance
  0.91→1.30, both DD lower, trades ~2×). It's portfolio construction, NOT a new signal
  (FC remains the alpha ceiling). Receipt: `docs/preregistration/div-promotion.md`.
  On deploy, the deploy date is the clean pre/post split for the `MultiStratV1` long
  ledger (the strategy_id was kept; a 12h sleeve was tested and **rejected** — additive
  on Binance but a drag on Bybit, fails the cross-venue bar).
- **Live demo** (Singapore VPS 5.223.42.109): `event_demo_daemon` + `ws_risk_daemon` +
  `long_native_event_demo_daemon` under systemd. Frozen promoted profile. Ledgers in
  `data/bybit-demo-event/`.
- **Paper shadow** (same VPS, same profile, no order submission): `data/bybit-paper-event/`.
- **No research runs in-flight.**
- **Next research run (the lead):** E1 from `docs/research_plan_selection_execution.md`
  — `--entry-policy fixed_delay` vs `promoted_quality_squeeze` on the daily strategy
  (realistic baseline, both venues) to quantify the execution signal's contribution.
  Cheap, decisive, no new code; pre-register before running.
- **Open action (from the 2026-05-29 re-baseline):** the deployed demo runs `max_active=3`
  (worst day −36%, DD −87% under honest fills); the research-validated value is
  `max_active=12` (worst day −4.8%, DD −27.5%). Consider moving the demo to 12 and/or
  `risk_equal` sizing. See `docs/research_summary.md`.

## Engine defaults (current)

- **Stop fill: `bar_extreme_capped` (10% cap)** — realistic bad-case (caps adverse slippage
  at 10% beyond the trigger). Was `bar_extreme` (worst-case wick); that change corrected the
  Round-2 daily null. `stop` (optimistic) and `bar_extreme` (worst-case) remain selectable
  via `--stop-fill-mode`.
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
- **5950X full-PIT op note:** one `volume-events` cell peaks ~23 GB → run sweeps at
  `SWEEP_MAX_WORKERS=1` (8 OOMs the box); clear `<root>/.locks/*.lock` after any OOM/kill.

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
