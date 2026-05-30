---
name: research-phase-runner
description: "Execution workflow for running a research experiment from the current forward plan (docs/research_plan_intraday_kernel.md — the intraday-detection kernel, phases K0/K1/K2). Use any time you are about to run, conditionally-run, or write up an experiment — covers pre-checks, dispatch, the three-tier demo-arbiter decision rule (scripts/r1_robustness.py + scripts/apply_decision_rule.py), the verdict receipt, STATE.md update, and the commit. Keeps the three-tier thresholds intact; the Tier-3 real-money gate stays strict."
---

# Running a research experiment

Use this every time you run, conditionally-run, or write up an experiment from the
current forward plan at **`docs/research_plan_intraday_kernel.md`** — the intraday-detection
kernel (K0 upside-ceiling → K1 backtest → K2 build).

Always read **STATE.md** first — it tells you what's done, what's pending, and the
current binding decision rules.

## The thesis you are testing

The alpha is the **SELECTION** signal — the discrete liquidity-migration event picks the
candidate pool (a fade short on seasoned names, not catch-the-top). E1 settled the
EXECUTION question: entry *timing* is a non-lever (fade-confirmation ≈ immediate), so the
forward work is **faster detection of the same event** (the kernel — detect intraday off the
WS stream), NOT a new selector. Keep the proven event + age/residual-momentum gates; don't
swap in the rank-all continuous decile (regime-conditional, rejected).

## The decision framework — three-tier, demo-arbiter

Ordered by how expensive a false positive is. **The exact thresholds are owned by
STATE.md ("Decision rules currently binding") — read them there; do not copy the
numbers here (that is how they drift).**

1. **Investigation** — keep studying? Loose (MAR-Δ direction + trade minimums).
2. **Demo-candidate** (→ forward demo) — LOOSE: positive return both venues + a
   small positive pooled-MAR-Δ bar + a per-venue floor + trade minimums. Fragility
   diagnostics (bootstrap p5, leave-one-month-out, sub-period thirds, residual
   Sharpe) are **reported, NOT blocking** — they set demo order.
3. **Real-money** (demo → mainnet) — STRICT, NOT loosened: forward-demo OOS pass
   + bootstrap pooled MAR-Δ left-tail ≥ 0 + positive factor-residual Sharpe +
   stress + capacity. There is no internal pre-2023 OOS root — pristine OOS is the
   forward demo/paper ledgers (`docs/data_roots.md`).

Principle: permissive where being wrong is free (backtest→demo is paper), strict where
it costs real money. The forward demo is both the multiple-testing arbiter and the OOS
surface — uncapped. MAR-primary (pooled), Sharpe secondary.

## Workflow (apply per experiment)

1. **Pre-check.** Read STATE.md. Confirm the experiment's gate is met (each kernel phase
   gates the next — K1 needs K0's upside-ceiling pass; K2 needs K1). Confirm required code is
   merged. Confirm data roots present (`~/SHARED_DATA/{bybit,binance}_full_pit`).

2. **Plan the arms/cells.** Re-read the experiment's section in the plan. Do NOT add
   off-menu cells without a dated amendment (a new pre-registration receipt) first.

3. **Dispatch.** Single cells: `scripts/volume_events_cell.sh` (fills the
   production-baseline flags; you pass overrides). Multi-cell sweeps: write a
   `scripts/_sweep_runtime.py`-based dispatcher for the experiment (the old R-phase
   dispatchers were deleted — `_sweep_runtime.py` is the reusable primitive: a
   dispatcher declares `BASELINE_PARAMS` + a list of `Cell`s and imports it).
   Full-PIT sweeps are memory-bound — use the worker/thread settings in STATE.md
   ("full-PIT op note"); over-parallelizing OOMs the box. Always run **both venues**.

4. **Apply the decision rule.**
   ```bash
   python scripts/r1_robustness.py --sweep-tag <SWEEP_TAG>
   ```
   emits the pooled-MAR-Δ Tier-2 verdict (engine-DD MAR) + bootstrap p5,
   leave-one-month-out, and sub-period thirds from the per-cell ledgers.
   `scripts/apply_decision_rule.py` is the **legacy strict (Sharpe) bar** —
   reference only, not the promotion gate (run with `--help` for args).
   Do not move thresholds downward to rescue a cell (see non-negotiables).

5. **Write the verdict.** Dated receipt under `docs/preregistration/<YYYY-MM-DD>-<exp>-verdict.md`
   with: experiment, full per-arm/cell metrics, the Tier-2 verdict + fragility, the
   verdict paragraph (incl. the falsifier outcome — a negative result is first-class),
   the forward pointer, AND a one-paragraph roll-up into `docs/research_summary.md`.

6. **Update STATE.md.** Move the experiment to its terminal state; add new helpers /
   open questions. Keep STATE.md under ~120 lines.

7. **Commit + propose push to operator.** Pre-push gate
   (`.venv/bin/python -m ruff check liquidity_migration tests` +
   `.venv/bin/python -m pytest -q`) MUST pass. NEVER push without operator confirmation.

## Pre-committed behaviours (non-negotiable)

- **No FURTHER loosening.** The framework was loosened ONCE, on principle,
  pre-registered. Do not loosen again to rescue a near-miss; the Tier-3 real-money
  gate stays strict. A cell short of the Tier-2 bar is descriptive, not a candidate.
- **No off-menu cells.** New cells need a dated pre-registration amendment first.
- **Gates are real.** A failed gate (E1 says selection-only; E2 doesn't clear Tier-2;
  E3 doesn't beat the 1h squeeze) means file a one-paragraph negative-trigger note and
  stop — don't look for excuses to run the gated phase. **Failure is a first-class
  outcome**; the strategy stays frozen. There is no "ship something" obligation.
- **Both venues.** Cross-venue agreement is the robustness bar; a single-venue edge
  does not clear Tier-2.

## Useful MCP tools

The `liqmig-research` MCP server exposes report/state tooling (`current_state`,
`parse_report`, `audit_run_artifacts`, `data_roots`, …) — see STATE.md "Helpers"
for the current list. `apply_decision_rule` is the legacy-bar verdict (reference
only); the Tier-2 verdict is `scripts/r1_robustness.py`.

## Communication style during an experiment

Report after each experiment ends with: a 2-line headline (what ran, what verdict);
the Tier-2 verdict + fragility output (~10 lines); the verdict file path; the next
experiment to trigger (or "gate failed — stop"). Do NOT report mid-run progress unless
something fails — let sweeps run to completion.

Operator is learning quant fundamentals — explain in plain language, and surface
inconsistencies BEFORE running.
