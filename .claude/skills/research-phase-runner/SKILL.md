---
name: research-phase-runner
description: "Execution workflow for the multi-phase research program pre-registered at docs/preregistration/2026-05-27-rank-direction-edge-and-universe-isolation-research-plan.md. Use any time you are about to run, conditionally-run, or write up a phase from that plan — covers pre-checks, dispatch, decision-rule application via scripts/apply_decision_rule.py, STATE.md update, and per-phase verdict commit. Keeps the Strictness Manifesto thresholds intact and the FDR ceiling honoured."
---

# Running a research phase

Use this every time you are about to run, conditionally-run, or write up a
phase from the pre-registered research program. The plan lives at
`docs/preregistration/2026-05-27-rank-direction-edge-and-universe-isolation-research-plan.md`.

Always read STATE.md before starting — it tells you what's done, what's
pending, and what the current decision rules are.

## Phase-runner workflow (apply per phase)

1. **Pre-check.** Read STATE.md. Confirm prior phases that this phase depends
   on have completed and produced the input it needs. Confirm the code
   changes the phase requires are merged. Confirm data roots present.

2. **Plan the cells.** Re-read the phase's "Cells" table in the plan doc.
   Do NOT add cells not in the table without writing a new dated pre-reg
   first.

3. **Dispatch.** Use `scripts/volume_events_cell.sh` per cell —
   it fills in the production-baseline flags so you only pass overrides.
   For multi-cell phases, use `scripts/sweep_cells.py` (after Change 2's
   ThreadPoolExecutor parallelism lands) with `SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4`.

4. **Apply the decision rule.** Once the per-cell summary CSV exists, run:
   ```bash
   python scripts/apply_decision_rule.py <SUMMARY_CSV> --control 00_baseline
   ```
   The output table gives you per-cell verdicts (candidate / reject /
   inconclusive). The Strictness Manifesto thresholds are the defaults;
   do not override them downward.

5. **Enforce the FDR ceiling.** Max 3 candidates from Phases 2-4 group AND
   max 3 from Phase 6 group may forward to Phase 7. If more cells qualify,
   sort by combined-venue Sharpe (the script reports this) and close-reject
   the rest in writing. **Closed-rejected cells are not a "menu for later."**

6. **Write the verdict.** Update or create the dated pre-reg file under
   `docs/preregistration/<YYYY-MM-DD>-phaseN-<short-name>.md` with:
   - Stage updated to "run-complete, <ACCEPTED|REJECTED|INCONCLUSIVE>"
   - Full per-cell metrics table
   - Decision-rule application output
   - Verdict paragraph including any falsification of the phase's hypothesis
   - Forward pointer to whichever phase fires next, or "program complete"
     if nothing forwards

7. **Update STATE.md.** Move the phase from "not started" / "in progress"
   to its terminal state in the Phases table. Add any new helpers,
   reorganised state, or open questions.

8. **Commit + propose push to operator.** Two-file commit (verdict pre-reg
   + STATE.md). Pre-push gate (`ruff` + `pytest`) MUST pass. NEVER push
   without operator confirmation.

## Conditional-phase triggers (from the plan)

| Phase | Trigger |
|---|---|
| 3 (exit selection) | Phase 2 produced ≥1 candidate (`P2_det_*` or `P2_both_*` ideally) |
| 4 (hybrid event types) | Phase 2 + Phase 3 both produced viable inputs |
| 6 (combined-signal portfolio) | Phase 5b reported ≥3 surviving features |
| 7 (pre-2023 OOS) | Any phase produced any finalist; mandatory before promotion |

If a trigger is not met, the phase does NOT run. File a 1-paragraph note
in the program ledger explaining the negative trigger; do not look for
excuses to run it anyway.

## Pre-committed behaviours (non-negotiable)

- **No threshold loosening.** If a cell falls 0.01 short of the Manifesto's
  +0.5 Sharpe-Δ on one venue, it is INCONCLUSIVE, not a candidate.
  Filing as inconclusive is the discipline; arguing for "well it's very
  close" is the failure mode.
- **No off-menu cells.** If a phase's results suggest a new cell would
  resolve an ambiguity, file the suggestion as an amendment to the plan
  with a dated pre-reg entry, AND only run after operator review. Never
  silently expand the menu mid-phase.
- **No Phase 8.** Phase 7 is the final gate. A Phase 7 failure means
  closed. There is no escape hatch.
- **Hard end-date 2026-06-15.** After this date the inverse-direction
  edge hypothesis is rejected by fiat and the program closes.
- **biased_benchmark stays biased.** Phase 1 cells are NEVER traded in
  production, regardless of their numbers.

## Phase-specific gotchas

- **Phase 0 (filter LOO):** disabling a filter means setting it to its
  permissive value (e.g. `turnover-ratio-min 0`, `pit-age-days-min 0`).
  Some flags do not have a "disable" value; use the wrapper's
  `--extra '--flag-name value'` pattern if a non-baseline flag is needed.
- **Phase 1 (universe diagnostic):** requires the
  `scripts/build_legacy_archive_manifest.py` side-copy of the manifest
  to exist. Verify before running. If it doesn't, build it first; the
  side-copy is symlinked, so re-building is cheap.
- **Phase 2 (direction grid):** the `--liquidity-migration-rank-direction`
  flag must be in the codebase (Change 1). Run a one-cell smoke
  invocation first to verify the flag is accepted before dispatching
  66 cells.
- **Phase 3a (excursion measurement):** new code in the signal-harness
  module is required; do not hand-roll this in a notebook. The harness
  computes adverse/favourable excursion distributions cleanly with
  PIT-honouring +1h fill alignment.
- **Phase 3c (sensitivity grid):** ~13 hours wall on the 5950X. Kick
  off as an overnight job. Do not interrupt; the orchestrator flushes
  partial results after every cell.
- **Phase 5 (signal harness):** the panel-build step (5a) is a one-off
  per venue and should be cached. Re-running IC computation is cheap
  once the panel exists. Do NOT rebuild the panel for each IC run.
- **Phase 7 (OOS):** assess pre-2023 root state on first contact. If
  rebuild needed, flag to operator before kicking off (~6h data
  download).

## Useful MCP tools

- `current_state()` — returns STATE.md as structured data.
- `apply_decision_rule(summary_csv, control_cell)` — programmatic
  verdict per cell (matches the script).
- `parse_report(path)` — reads a `volume_event_research_report.md` into
  headline metrics.
- `audit_run_artifacts(path)` — checks the integrity-standard artifact
  completeness for a run.
- `data_roots()` — current data-root index.

## Communication style during a phase

Report after each phase ends with:
- 2-line headline: what ran, what verdict
- The decision-rule analyzer output verbatim (~10 lines)
- The verdict pre-reg file path
- The next phase to trigger (or "program complete")

Do NOT report mid-run progress unless something fails. The operator's
attention is not the bottleneck; let phases run to completion before
surfacing.
