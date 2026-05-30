# Research-program state

**Last updated:** 2026-05-30. **All research findings, results, and verdicts now live in
one place: [docs/research_summary.md](docs/research_summary.md).** Round 1 + Round 2
per-phase plans and verdicts were consolidated there and removed (git history has the
originals). This file = live/operational state + the binding decision rules.

> First session in this repo? Read this file, then `docs/research_summary.md`.

## Current status (one paragraph)

Bybit (+Binance) liquidity-migration **short**, research-stage — the live demo + paper run
a frozen "promoted" profile; NOT real money. **Framing (E1-corrected 2026-05-30):** the alpha
is the **SELECTION** signal (the liquidity-migration event = candidate pool) + a plain +1h
short. E1+E1b **falsified the EXECUTION half** — fade-confirmation entry adds no robust
cross-venue premium over immediate entry, so **E3 (sniper) is dropped** and the open lead is
**SELECTION refinement** (the age gate + residual-momentum gate, under "What's running"). (The
earlier "Round 2 = null" was a methodology artifact — worst-case fills + over-concentration.)
Plan: **`docs/research_plan_intraday_kernel.md`** (the forward direction — faster intraday
detection of the same event); numbers + full record (the dated source of truth):
**`docs/research_summary.md`**. Nothing is promoted; forward demo is the arbiter.

## What's running

- **SHORT sleeve `drop_all_4` promotion (2026-05-30, OPERATOR OVERRIDE — code-complete,
  deploy pending owner push):** the `promoted` profile drops the 4 non-earning vetoes/bounds
  (`day_return` floor, `stop_pressure`, `realized_loss`, `universe_rank_max`) and runs
  `max_active=12` (systemd `MAX_ACTIVE_SYMBOLS` 3→12 on demo+paper). ⚠️ **FAILS the Tier-2
  cross-venue guard under the corrected engine** (binance net-negative; the deleted research's
  "winner" was an optimistic `stop_fill='stop'` artifact). Deployed by explicit operator
  override for forward-demo observation only — revert if binance stays negative. `strategy_id`
  unchanged → deploy date = clean pre/post split. Receipt + numbers:
  `docs/preregistration/drop-all-4-promotion.md`.
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
- **No research runs in-flight.** Research state — full detail + numbers in
  `docs/research_summary.md` (the dated record; the per-phase E1/E2/P3/c2b receipts were
  consolidated there and removed 2026-05-30 — git history has the originals):
  - **E1+E1b — execution is a non-lever:** fade-confirmation adds no robust cross-venue premium
    over immediate entry → selection-dominant; E3 (sniper) dropped.
  - **E2/E2b/c/d — the age gate (lead):** `--liquidity-migration-pit-age-days-min≈300` (drop
    names younger than ~300d) roughly doubles cross-venue MAR and fixes the recent weak third;
    robust to threshold/regime/cost/stop-fill — **Tier-2 demo-candidate, in-sample.** Deploy-ready
    (simple PIT feature). Mechanism: young-name shorts are systematic losers (fresh listings squeeze).
  - **P3b — residual-momentum gate:** built + integrated (engine config
    `liquidity_migration_residual_momentum_max`, default-inactive), r1_robustness **DEMO-ELIGIBLE**;
    Tier-3 residual binance-certified, bybit recent-only (not a clean cross-venue cert). Stronger
    than the age gate but its signal must be live-wired before a faithful forward demo.
  - **Continuous architecture (c2b) — C0 NOT justified:** the edge is regime-conditional (recent
    alt-bear only), not all-weather; the robust result is the discrete age gate.
- **Open actions (operator's call — NOT autonomous; profile change is a hard line):** (a)
  forward-demo the **age gate** (deploy-ready) and/or the **residual-momentum gate** (live-wire the
  signal first — `docs/forward_demo_readiness.md`); (b) the deployed demo runs `max_active=3` vs the
  research-validated `max_active=12` (materially lower worst-day + DD) — consider moving it +
  `risk_equal` sizing. Numbers: `docs/research_summary.md`.

## Engine defaults (current)

- **Stop fill: `bar_extreme_capped` (10% cap)** — realistic bad-case (caps adverse slippage
  at 10% beyond the trigger). `stop` (optimistic) and `bar_extreme` (worst-case) remain
  selectable via `--stop-fill-mode`.
- **Cost:** 100% taker; 15 bps base round-trip; sweeps default to ×3 = 45 bps (conservative).
- **Full-PIT universe required** (engine aborts on coverage gaps); the PIT gate is scoped to
  each symbol's traded span `[first_kline, last_kline]` (pre-listing/post-delisting empty
  phantoms excluded; mid-history gaps still caught).
- **Universe sourcing (clarification — the `rank_end: 120` in `configs/volume_alpha.default.yaml`
  is NOT the trading universe):** that 120-rank `universe:` block is a *current-turnover snapshot*
  setting read ONLY by `discover-universe` (a live `get_tickers()` snapshot — survivorship-biased
  by construction, benchmark/scouting only). The actual paths bypass it: the `volume-events`
  backtest reads zero of it — it ranks within the full-PIT root on PIT daily liquidity ranks and
  trades the strategy's `rank_min..rank_max` band; the live demo/paper run match-the-backtest mode
  (`UNIVERSE_RANK_END=0 / UNIVERSE_MAX_SYMBOLS=0` → the full ~750-perp universe). The pre-2026-05-24
  demo did run a narrow current-universe (~220–400 by ticker turnover) — that was a real
  current-universe bias and caused the DRIFTUSDT demo≠backtest divergence; the match-the-backtest
  switch fixed it. So "the old narrow-universe demo was biased" is correct *for that legacy path*;
  the current backtest + live demo are not on the 120.

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
