---
name: research-phase-runner
description: "Execution workflow for running a pre-registered research experiment in this quant repo. Current open experiments are tracked in STATE.md ('Current Status' / 'Current Research Direction') + docs/research_summary.md; the per-arc forward plans were consolidated there. Use any time you are about to run, conditionally-run, or write up an experiment — covers pre-checks, dispatch, the three-tier demo-arbiter decision rule (scripts/r1_robustness.py + scripts/apply_decision_rule.py), the verdict receipt, STATE.md update, and the commit. Keeps the three-tier thresholds intact; the Tier-3 real-money gate stays strict."
---

> **ERASURE NOTE (2026-06-11, operator order):** the daily SHORT sleeve was
> ERASED from the system — `volume-events` backtest, `event-demo-cycle`,
> `event_demo_daemon`, `short_profile`, `volume_events_cell.sh`, short deploy
> units and short reconcile commands NO LONGER EXIST. Ignore any instruction
> below that references them; long + continuous guidance still applies.

# Running a research experiment

Use this every time you run, conditionally-run, or write up an experiment. **The current
open experiments live in STATE.md ("Current Status" / "Current Research Direction") and `docs/research_summary.md`** — the
per-arc forward plans (intraday kernel, continuous-fade) concluded and were consolidated into
the summary (git history has the originals). Always read **STATE.md** first — it tells you
what's done, what's pending, and the current binding decision rules.

## The program you are working (post-erasure, 2026-06-11)

Two research lines survive: the **CONTINUOUS fade book** (the live demo/paper book,
research-stage, promoted-in-code only by operator override) and the
**long-native v11a sleeve** (demo/paper enabled in current deploy state). The daily SHORT selection program was ERASED 2026-06-11 by operator
order — do not propose short work or re-mine its window. As of 2026-06-12 the
window is open again only for pre-registered, tightly scoped research; closed
families in `docs/research_summary.md` stay closed. The active queue and decision
rules live in STATE.md plus `docs/research_summary.md`.

## The decision framework — three-tier, demo-arbiter

Ordered by how expensive a false positive is. **The current binding decision surface is
`docs/research_summary.md` plus STATE.md ("Current Status" / "Current Research Direction");
the three-tier framing is also referenced in `docs/limitations.md` and the
`scripts/apply_decision_rule.py` docstring — read the thresholds there, do not copy the
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

1. **Pre-check.** Read STATE.md. Confirm the experiment's gate is met (where one phase gates
   the next, the earlier phase must have passed first). Confirm required code is merged.
   Confirm data roots present (`~/SHARED_DATA/{bybit,binance}_full_pit`).

2. **Plan the arms/cells.** Re-read the experiment's section in the plan. For
   any added arm, cell, metric, data source, or mechanism, write a dated
   amendment or a new pre-registration receipt first.

3. **Dispatch.** (`volume_events_cell.sh` was erased with the short engine —
   single-cell runs now go through a dispatcher too.) For a serious sweep, write
   a small dated dispatcher for the specific pre-registered cells and call the
   relevant package runner directly. Do not revive deleted generic sweep scripts.
   Preserve the old sweep discipline:
   predeclare worker/thread counts for memory-heavy cells because
   over-parallelizing OOMs the box. Always run **both venues**.

4. **Apply the decision rule.**
   ```bash
   python scripts/r1_robustness.py --sweep-tag <SWEEP_TAG>
   ```
   emits the pooled-MAR-Δ Tier-2 verdict (engine-DD MAR) + bootstrap p5,
   leave-one-month-out, and sub-period thirds from the per-cell ledgers.
   `scripts/apply_decision_rule.py` is the **legacy strict (Sharpe) bar** —
   reference only, not the promotion gate (run with `--help` for args).
   Do not move thresholds downward to rescue a cell (see non-negotiables).

5. **Write the verdict.** Dated receipt under `docs/preregistration/<exp>-<YYYY-MM-DD>.md`
   with: experiment, full per-arm/cell metrics, the Tier-2 verdict + fragility, the
   verdict paragraph (incl. the falsifier outcome — a negative result is first-class),
   the forward pointer, AND a one-paragraph roll-up into `docs/research_summary.md`.

6. **Update STATE.md.** Move the experiment to its terminal state; add new helpers /
   open questions. Keep STATE.md tight — one page of signal, not a changelog
   (~200 lines is the practical ceiling).

7. **Commit + propose push to operator.** Pre-push gate
   (`.venv/bin/python -m ruff check liquidity_migration tests scripts` +
   `.venv/bin/python -m pytest -q`) MUST pass. NEVER push without operator confirmation.

## Research discipline (non-negotiable)

- **No unregistered mining.** Important feature families may be studied in
  multiple stages; each stage needs a dated pre-registration or amendment before
  it touches the per-venue working roots. The amendment must explain why the
  prior stage was insufficient, what new information or mechanism is being
  tested, and which outcomes would change the decision.
- **No tiny-script verdicts for important features.** A serious feature decision
  must leave enough artifacts to audit: population, data-root identity, code
  version/config hash, per-venue ledgers or event rows, effect sizes in return
  units where applicable, fragility diagnostics, and a written falsifier. A
  small script can be a measurement tool, not the whole decision record.
- **Gates are real but not terminal for the whole research family.** A failed
  registered stage blocks that exact mechanism from being promoted or cited as
  evidence. It does not prohibit a later, better-designed stage with a fresh
  preregistration, new data, a corrected lifecycle, or a genuinely different
  mechanism.
- **No threshold lowering to rescue a near-miss.** Revisions can expand the
  program or improve measurement quality, but they cannot move the previously
  declared bar after seeing the result. The Tier-3 real-money gate stays strict.
- **Both venues.** Cross-venue agreement is the robustness bar; a single-venue
  edge does not clear Tier-2.

## Optional MCP tools

If available, the `liqmig-research` MCP server exposes report/state tooling
(`current_state`, `parse_report`, `audit_run_artifacts`, `data_roots`, ...) —
the MCP server itself is the source of truth for the current list. Treat those tools as accelerators,
not dependencies. Without MCP, read `STATE.md`, `docs/data_roots.md`, and report
files directly. `apply_decision_rule` is the legacy-bar verdict (reference only);
the Tier-2 verdict is `scripts/r1_robustness.py`.

## Communication style during an experiment

Report after each experiment ends with: a 2-line headline (what ran, what verdict);
the Tier-2 verdict + fragility output (~10 lines); the verdict file path; the next
experiment to trigger (or "gate failed — stop"). Do NOT report mid-run progress unless
something fails — let sweeps run to completion.

Operator is learning quant fundamentals — explain in plain language, and surface
inconsistencies BEFORE running.
