# Research-program state

**Last updated:** 2026-05-27 (post code changes 1-4 + Phase 0 pre-reg)

> If you are a Claude session opening this repo for the first time, read this
> file FIRST. It tells you in 60 seconds what's been done, what's running,
> and what's next. Then read the docs it points at.

## TL;DR

- Strategy: Bybit (+Binance) liquidity-migration short, **research-stage**.
  Live demo + paper run a frozen "promoted" profile; not deployed for real money.
- Phase 0 filter LOO audit (2026-05-28): **REJECTED**, 0 candidates.
  Falsifier hits confirm `crowding`, `event_rank_frac`, `turnover_ratio`
  earn their keep. `docs/preregistration/2026-05-27-phase0-verdict.md`.
- Phase 1 universe diagnostic (2026-05-28): **H1 falsified** — universe
  widening hurts Sharpe but doesn't explain the DD shift. Other candidates
  (bug-fix, regime, code drift) remain open. `docs/preregistration/2026-05-27-phase1-verdict.md`.
- Phase 5 IC test (2026-05-28): **5 features survive @ fwd_ret_3d** —
  vol_of_vol_30d (-0.087), realized_vol_7d (-0.081), dist_from_30d_low
  (-0.071), xs_rank_ret_7d (-0.043), xs_rank_ret_3d (-0.039). All negative
  IC → short-side signal. Phase 6 triggered.
  `docs/preregistration/2026-05-27-phase5-verdict.md`.
- Current research program: 7-phase rank-direction edge + universe isolation +
  filter audit + signal-research harness. Pre-reg at
  `docs/preregistration/2026-05-27-rank-direction-edge-and-universe-isolation-research-plan.md`.
- All 4 code changes merged. Phase 0 REJECTED (filter stack stays as-is;
  crowding/event_rank_frac/turnover_ratio confirmed load-bearing).
  Phase 1 H1-FALSIFIED (universe widening hurts Sharpe but doesn't drive
  the DD shift; Phase 2 in-sample numbers can be interpreted at face value).
- Phase 2 pre-registered + orchestrator ready. Phase 6 pre-registered +
  orchestrator written. Phase 3 / 4 still conditional on Phase 2 candidates.
- Next action: dispatch Phase 2 (the big ~3h rank-direction grid) and
  Phase 6 in parallel — different code paths (Phase 6 reads pre-built
  feature panels; Phase 2 reads klines/funding/OI/premium). Then Phase 7
  OOS gate for any candidates.

## What's done

| Date | Item | Receipt |
|---|---|---|
| 2026-05-26 | Demo↔backtest universe unified; u32 underflow in rank_improvement fixed | commit `78df65a` |
| 2026-05-27 | Sweep orchestrator + 10-cell EXPLORATORY sweep on Mac (REJECTED) | commit `8fb61b5` (pre-reg), `2f67746` (verdict) |
| 2026-05-27 | v5-listing supplement made always-on; "fallback" naming dropped | commit `17f55e5` |
| 2026-05-27 | 7-phase research plan pre-registered (rank-direction + signal harness) | commit `e7dd104` |
| 2026-05-27 | Code changes 1-3: rank-direction flag, sweep parallelism, legacy-archive manifest builder | commit `ef9d3aa` |
| 2026-05-27 | Code change 4: signal_harness module (20 features + IC + portfolio + CLI) | commit `ed7c5d8` |
| 2026-05-27 | Phase 0 pre-registered (filter LOO audit, 15 cells × 2 venues) | commit `54f7163` |
| 2026-05-28 | Phase 0 verdict: REJECTED (0 candidates, 3 falsifiers, 11 inconclusive) | commit `8d7e1de` |
| 2026-05-28 | Phase 1 verdict: H1 FALSIFIED on baseline (DD Δ +1.7pp < +5pp); avg sh_Δ=+1.09, avg dd_Δ=-5.78pp | commit `a5b7c05` |
| 2026-05-28 | Phase 5 verdict: 5 features survive @ fwd_ret_3d (vol_of_vol_30d, realized_vol_7d, dist_from_30d_low, xs_rank_ret_7d, xs_rank_ret_3d) → **Phase 6 triggered** | this commit |

## What's NOT done (the work)

The pre-registered plan at
`docs/preregistration/2026-05-27-rank-direction-edge-and-universe-isolation-research-plan.md`.
Open work:

### Code changes — all merged

| Change | Receipt |
|---|---|
| 1. `--liquidity-migration-rank-direction` flag | commit `ef9d3aa` |
| 2. `scripts/sweep_cells.py` parallelism (ThreadPoolExecutor + shared `_sweep_runtime.py`) | commit `ef9d3aa` (+ this commit factors out the runtime) |
| 3. `scripts/build_legacy_archive_manifest.py` | commit `ef9d3aa` |
| 4. `liquidity_migration/signal_harness.py` module + CLI | commit `ed7c5d8` |

### Phases

| Phase | Purpose | Pre-requisites | Status |
|---|---|---|---|
| 0 | Filter LOO audit | none | **COMPLETE — REJECTED** (0 candidates, 3 falsifiers, 11 inconclusive). See [verdict](docs/preregistration/2026-05-27-phase0-verdict.md). |
| 1 | Universe-isolation diagnostic | Change 3 + `build_legacy_archive_manifest.py` (run 2026-05-27) | **COMPLETE — H1 falsified** (universe widening ≠ DD-driver). See [verdict](docs/preregistration/2026-05-27-phase1-verdict.md). |
| 2 | Rank-direction full grid | Change 1 | **pre-registered, ready to dispatch** |
| 3 | Exit selection (conditional on P2 candidate) | P2 candidate | not started |
| 4 | Hybrid event types (conditional on P2+P3) | P2+P3 | not started |
| 5 | Signal-research harness + univariate IC | Change 4 | **COMPLETE — 5 survivors @ fwd_ret_3d** (Phase 6 triggered). See [verdict](docs/preregistration/2026-05-27-phase5-verdict.md). |
| 6 | Combined-signal portfolio (conditional on P5 ≥3 survivors) | P5 survivors | **pre-registered + orchestrator written, ready to dispatch** |
| 7 | Pre-2023 OOS gate (MANDATORY for every finalist) | any finalist | **pre-registered**, waiting on finalists |

Hard end-date on Phase 7: **2026-06-15**.

## What's running

- **Live demo** (Singapore VPS 5.223.42.109): event_demo_daemon + ws_risk_daemon
  + long_native_event_demo_daemon under systemd. Frozen promoted profile.
  Logs: VPS `journalctl -u bybit-demo-*`. Local ledgers under
  `data/bybit-demo-event/`.
- **Paper shadow** (same VPS, same profile, no order submission):
  `data/bybit-paper-event/`.
- **NO research runs currently in-flight.** (The Mac sweep finished on
  2026-05-27.)

## What's broken

Nothing known. Pre-push gate clean as of HEAD = `2f67746`:
- `.venv/bin/python -m ruff check liquidity_migration tests` → clean
- `.venv/bin/python -m pytest -q` → 849/849 pass

## Decision rules currently binding

The **Strictness Manifesto** in the research plan supersedes the older
2026-05-28 sweep's looser rule for any cell run under the new plan:

- Per-cell candidate: Sharpe Δ ≥ **+0.5** on **both** venues, DD Δ ≤
  **-5pp** on **both**, sign-consistent across **3** sub-periods,
  ≥**50** trades/sub-period on Bybit.
- FDR ceiling: **max 3 candidates** per phase-group forward to Phase 7.
  Excess cells satisfying criteria → top-N by combined-venue Sharpe,
  rest **closed-rejected**.
- Phase 7 OOS gate: all 3 sub-periods × both venues × sign-consistent ×
  DD < 50% required.

Apply via `python scripts/apply_decision_rule.py --help`.

## Helpers (when you need them)

- **CLI baseline wrapper:** `scripts/volume_events_cell.sh --cell-id X --overrides 'KEY=VALUE,…'`
  — fills in the 30+ baseline flags so a cell only specifies what differs.
- **Decision-rule analyzer:** `scripts/apply_decision_rule.py SUMMARY.csv --control 00_baseline`
  — applies the Strictness Manifesto per cell, prints structured verdict.
- **Skill `research-phase-runner`** (auto-loads on phase-related tasks) —
  codifies the per-phase workflow (pre-check, dispatch, decision-rule,
  STATE.md update, commit verdict).
- **MCP tools** on `liqmig-research`:
  - `current_state` — read this file into structured form
  - `apply_decision_rule(summary_csv, control_cell)` — programmatic verdict
  - `data_roots`, `list_reports`, `parse_report`, `audit_run_artifacts`
    — pre-existing.

## Non-negotiables (every session)

1. Pre-push gate (`ruff` + `pytest`) before every `git push`.
2. Never `REAL_MONEY=true`. Demo + paper only.
3. Never commit or push without operator confirmation.
4. Never modify `docs/backtesting_errors_we_never_repeat.md`,
   `docs/parameter_pre_registration.md`, or `configs/volume_alpha.default.yaml`
   without operator instruction.
5. Strictness Manifesto thresholds are pre-committed — no post-hoc loosening.
6. Phase 1 results are `biased_benchmark` only — never trade a 474-restricted
   config regardless of how good its backtest looks.

## How to update this file

Update STATE.md as part of every phase verdict commit. The skill
`research-phase-runner` handles this automatically; if doing manually, edit
the "What's done", "What's NOT done" → "Phases" status, and any other
section that materially changed. Keep it under 200 lines. If it grows,
push detail into the phase-specific verdict docs.
