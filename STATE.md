# Research-program state

**Last updated:** 2026-06-02. This file = **live/operational state + binding decision rules**.
**All research findings, results, verdicts, and open methodology debts live in ONE file:
[docs/research_summary.md](docs/research_summary.md)** (per-arc write-ups + one-off research
scripts were consolidated there and deleted; git history is the backstop).

> First session here? Read this file, then `docs/research_summary.md`. That's the whole map.

## Current status (one paragraph)

Bybit (+Binance) liquidity-migration **short**, research-stage — the live demo + paper run a
frozen `promoted` profile; **NOT real money**. The alpha is the **SELECTION** signal (the
liquidity-migration event = candidate pool; a fade short on seasoned names, not catch-the-top)
+ a plain +1h short. E1 falsified the EXECUTION half (fade-confirmation entry ≈ immediate), so
the open lead is **SELECTION refinement** — the **age gate + residual-momentum gate** (both
Tier-2 demo-candidates, in-sample). The intraday-kernel and continuous-fade arcs **concluded**
(marginal/redundant — see summary). Nothing is promoted; forward demo is the arbiter. Numbers +
full record: **`docs/research_summary.md`**.

## What's running (live deployed state)

- **SHORT `promoted` profile = `drop_all_4 + age300 + ff6`** (`event_demo._demo_event_config`):
  - `age300` SELECTION gate (`pit_age_days_min` 90→**300**, E2) + `ff6_4pct` failed-fade EXIT
    (6h/4%/1%mfe/cloc0) stacked on `drop_all_4` (drops the 4 non-earning vetoes/bounds +
    `max_active=12`, systemd `MAX_ACTIVE_SYMBOLS=12`). ⚠️ `drop_all_4` **fails the Tier-2 cross-venue
    guard** (binance net-negative under the corrected engine) — kept by explicit operator override
    for forward-demo observation; revert if binance stays negative. `strategy_id` kept ⇒ deploy date
    = clean pre/post split. Receipts: `docs/preregistration/{promote-age-ff6-demo-2026-05-31,drop-all-4-promotion}.md`.
- **LONG `div` profile** (`_v11a_long_native_config`, **code-complete, NOT yet deployed**): universe
  10→50, max_concurrent 5→10, de-risk-only vol-target (0.60 annual). Portfolio construction, not a
  new signal (FC is the alpha ceiling). Receipt: `docs/preregistration/div-promotion.md`.
- **CONTINUOUS-fade sleeve — LIVE demo (`SUBMIT_ORDERS=1`, go-live 2026-06-01) + NEW paper shadow.**
  4th forward sleeve, separate everything (root `data/bybit-continuous-{demo,paper}-event`, datasets
  `continuous_fade_{demo,paper}_*`, orderLinkId `lm-en-c-`). Live config: short top decile (D9),
  `rmom_quantile=0.33` (APPLIED), liq≥$500k/h, `max_active=25`, `max_hold=48h`, **wide server-side
  disaster stop `0.25`** (the "leave-decile" state-exit is a PROFIT exit, never a risk control), the
  **circuit breaker ENABLED at w24/n8** (`entry_pause_after_adverse_exits=8`, window 1440min — protective
  tail insurance, not a validated MAR win; disable via `=0`), and 4 sub-cycle reactivity tiers
  (tick-driven protective exits + hysteresis + cooldown; a per-bar `LivePanelCache`). **EXPLORATORY** —
  the demo is the only OOS arbiter. Pending operator deploys (NOT pushed): entry-timing `+1h`
  (`entry_confirm_delay_hours=1`, the ~2× lever) and the optional de-gross to `~0.3`.
  - Signal: the live decile is recomputed off the live ticker every 60s, **bit-identical to the
    backtest** (shared `compute_continuous_decile_panel`); gated on a daily-refreshed
    `residual_momentum.parquet` (no rmom ⇒ no entries, fail-safe).
  - **Shared-account safety:** the single `ws_risk` service reads all THREE demo roots
    (`DATA_ROOT`+`LONG_DATA_ROOT`+`CONTINUOUS_DATA_ROOT`), tags rows by `sleeve`, routes writes
    per-sleeve, and closes continuous orphans into the continuous ledger; account-wide same-symbol
    exclusion keeps sleeves disjoint. Deploy restarts risk BEFORE the continuous daemon. The paper
    shadow submits no orders on its own root, so ws_risk ignores it.
- **Live VPS** (Hetzner 116.202.15.128, 4 GB — migrated 2026-06-04 from the decommissioned
  Singapore 5.223.42.109, retired for cost): short `event_demo_daemon` + `ws_risk_daemon` under
  systemd. Long + continuous + the short paper shadow are retired via the `deploy/sleeves.env`
  kill-switch to fit the smaller box (`SHORT_PAPER_SLEEVE`/`LONG_SLEEVE`/`CONTINUOUS_SLEEVE`=off);
  ws_risk still protects any open positions of the retired sleeves until they exit (no flatten).
- **No research runs in-flight.**

**Research state (all arcs CONCLUDED — full record + open debts in `docs/research_summary.md`):**
- **Daily age+rmom (the validated lead):** `age300` ~doubles cross-venue MAR (robust to
  threshold/regime/cost/stop-fill); the `rmom` gate is a squeeze-filter that fixes the recent decay.
  Both Tier-2 demo-candidates, in-sample. The rmom gate must be live-wired for a faithful forward demo.
- **Open pre-registered cell (run-pending):** the `age+rmom+ff6` combined stack (do the three
  separately-validated refinements add or overlap?). Receipt `docs/preregistration/age-rmom-ff6-combined-2026-05-31.md`.
- **Intraday kernel + burst-short:** CONCLUDED — fill-timing dead (E1), detection-timing dead (K1a),
  standalone intraday burst-short marginal + unvalidated (funding eats ~85%); the daily late entry
  sidesteps the intraday squeeze. I3 engine-grade test is an operator-gated coin-flip (deprioritised).
- **Continuous-fade:** a real, all-weather, cross-venue signal tradeable on liquid names, **but it
  does NOT beat the daily on MAR** and the continuous short is 0.65–0.72 correlated with the live
  daily short (redundant capacity). Its market-neutral L/S overlay is a *candidate* diversifier — but
  the existing long sleeve already diversifies better; the gating test is **G1 (3-way redundancy
  backtest)** in the summary. Alpha-program wins (entry-timing +1h, rmom33, de-gross 0.3) are in the
  continuous live-config above.
- **Open methodology debts (2026-06-02 audit, operator-gated):** binance funding-interval undercount
  (~50% on 4h alts — inflates the binance promotion-gate MAR), age300 live≠backtest definition, rmom
  look-ahead unconfirmed, decompose day-grid off-by-one. Detail in the summary.

- **Open actions (operator's call — profile change is a hard line):** (a) forward-demo the age gate
  (deploy-ready) and/or the residual-momentum gate (live-wire the signal first); (b) the deployed
  short demo runs `max_active=3` vs the validated `max_active=12` — consider moving it + `risk_equal`
  sizing; (c) deploy the continuous entry-timing `+1h` ± de-gross; (d) run G1 (continuous redundancy).

## Engine defaults (current)

- **Stop fill: `bar_extreme_capped` (10% cap)** — realistic bad-case. `stop` (optimistic) /
  `bar_extreme` (worst-case) selectable via `--stop-fill-mode`.
- **Cost:** 100% taker; 15 bps base round-trip; sweeps default to ×3 = 45 bps.
- **Full-PIT universe required** (engine aborts on coverage gaps); the PIT gate is scoped to each
  symbol's traded span `[first_kline, last_kline]`.
- **Universe sourcing:** the `rank_end: 120` in `configs/volume_alpha.default.yaml` is NOT the trading
  universe — it's a current-turnover snapshot read ONLY by `discover-universe` (survivorship-biased,
  scouting only). The `volume-events` backtest + the live demo/paper trade the full-PIT root on PIT
  daily liquidity ranks (`UNIVERSE_RANK_END=0` ⇒ full ~750-perp universe). The pre-2026-05-24 narrow
  current-universe demo was a real bias (caused the DRIFTUSDT divergence); match-the-backtest fixed it.

## Decision rules currently binding — three-tier, demo-arbiter

Principle: permissive where being wrong is free (backtest→demo is paper), strict where it costs real
money. Forward demo/paper is the arbiter. MAR-primary (Return/Drawdown), Sharpe secondary.

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
- Forward-demo OOS pass (no internal pre-2023 OOS root — pristine OOS = the forward demo/paper
  ledgers, per `docs/data_roots.md`): MAR > 0 both venues over the forward window; DD < 50%; sign-consistent
- ≥30 days forward demo + daily paper-shadow reconciliation
- Block-bootstrap pooled MAR-Δ p5 ≥ 0 (seed=0, block=3mo, n=5000)
- Residual Sharpe ≥ +0.3 (factor-model residual; `liquidity_migration/risk_model.py`
  `decompose_strategy_pnl`, see `docs/preregistration/r4-risk-model-verdict.md`)
- Stress pass + capacity ≥ 10× deployment size

`scripts/r1_robustness.py` emits the Tier-2 verdict + fragility from per-cell ledgers;
`scripts/apply_decision_rule.py` is the legacy strict (Sharpe) bar only.

## What's broken

Nothing known. Pre-push gate clean: `.venv/bin/python -m ruff check liquidity_migration tests`
+ `.venv/bin/python -m pytest -q` both pass (1129).

Recent landed work (full detail in git history / `docs/research_summary.md`):
- **2026-06-02 — zero-friction all-sleeve reconcile + continuous PAPER sleeve (NOT pushed).**
  `scripts/reconcile.sh` is self-provisioning + all-sleeve: pull all sleeves → refresh manifest →
  **auto-download recent klines** → **auto-recompute rmom** → coverage check → **minimal-window**
  backtest (~45d warm-up vs 150d, validated to reproduce the identical forward trade set; the 300d
  age gate is manifest-derived so a short kline window is exact) → reconcile SHORT/LONG/CONTINUOUS →
  one headline. New: `reconcile-continuous-paper-demo` + the continuous signal-consistency replay +
  the operationalized continuous paper sleeve (systemd + `--paper-mode` + deploy verify asserts it
  never submits). Skill `pit-reconcile`; design `docs/pit_gate.md`.
- **2026-06-02 — continuous rmom blackout fixed + monitored** (the gate built zero rows since go-live;
  klines-dataset sniff + trailing null-residual rows through `end` + a staleness watchdog).
- **2026-05-30 — PIT gate / reconcile plumbing** (membership keyed on `date(ts_ms-1ms)` = trading day;
  one-command reconcile). Op note: the 16 GB VPS can't run a full `bybit_full_pit` cell (~23 GB).

## Helpers (when you need them)

- **Demo-forward reconcile (one command, ALL sleeves):** `bash scripts/reconcile.sh` —
  self-provisioning (pull → manifest → auto kline-fill → auto-rmom → minimal-window backtest →
  reconcile short/long/continuous → headline). `--sleeves` to subset, `--dry-run` to preview,
  `--full-window` for the old 150d. Skill `pit-reconcile`; design `docs/pit_gate.md`.
- **CLI baseline wrapper:** `scripts/volume_events_cell.sh --cell-id X --overrides 'KEY=VAL,…'`.
- **Decision-rule analyzer:** `scripts/apply_decision_rule.py SUMMARY.csv --control 00_baseline`.
- **Tier-2 verdict + fragility:** `scripts/r1_robustness.py --sweep-tag <TAG>`.
- **Equity curves (one command, ALL sleeves):** `bash scripts/equity_curves.sh [--sleeves …]
  [--years 3]` — runs each sleeve's EXACT deployed profile over the window and emits the
  equity-vs-BTC PNG + run_label. The promoted profiles live in ONE place:
  **`liquidity_migration/promoted.py`** (`short_profile`/`long_profile`/`continuous_profile`,
  pinned by `tests/test_promoted_profiles.py`) — never reverse-engineer flags again.
- **Continuous signal-consistency:** `scripts/continuous_demo_signal_check.py --root <demo-root>`.
- **Skill `research-phase-runner`** (auto-loads) — per-experiment run/verdict workflow.
- **MCP tools** on `liqmig-research`: `current_state`, `data_roots`, `list_reports`, `parse_report`,
  `audit_run_artifacts`, `apply_decision_rule`.
- **Full-PIT op note:** one `volume-events` cell peaks ~23 GB → run full-PIT sweeps at
  `SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8`; clear `<root>/.locks/*.lock` after any OOM/kill.

## Non-negotiables (every session)

1. Pre-push gate (`ruff` + `pytest`) before every `git push`.
2. Never `REAL_MONEY=true`. Demo + paper only.
3. Never commit or push without operator confirmation.
4. Never modify `docs/backtesting_errors_we_never_repeat.md`, `docs/parameter_pre_registration.md`,
   or `configs/volume_alpha.default.yaml` without operator instruction.
5. The three-tier decision structure is pre-committed — no further loosening to rescue a specific
   cell; the Tier-3 real-money gate is NOT loosened.
6. MAR-primary, Sharpe-secondary is pre-committed.
7. Strategy stays at the frozen promoted profile until the Tier-3 gate passes AND ≥30 days forward
   demo evidence accumulates.

## How to update this file

Keep it short (live/operational state + decision rules). Research results go in
`docs/research_summary.md`, not here. Keep under ~120 lines.
