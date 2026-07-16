# Autonomous improvement cycle 001: fail-closed venue positions

## Scope and baseline

- Audit timestamp: `2026-07-16T00:56:36Z`.
- Audited commit: `2d42c7b78bd4945d65eb90f6a3e33d1b2e901cf2` on
  `codex/demo-operational-cutover`, plus the current uncommitted worktree.
- The worktree already contained 393 status entries. They were treated as
  user-owned; no cleanup, reset, staging, deployment, or VPS contact occurred.
- No prior `reports/autonomous-improvement/` log existed and `reports/` was
  wholly ignored. `.gitignore` now retains this subdirectory while continuing
  to ignore other generated reports.
- No research contract is active. This cycle changed account-runtime evidence
  validation, not strategy logic or research parameters.
- Pre-change repository gates in the intended environment were green: Ruff,
  package-wide mypy over 85 modules, and 1,573 pytest cases.

## Candidate ranking

| Rank | Finding | Consequence | Disposition |
| ---: | --- | --- | --- |
| 1 | `account_reconcile.py` converted missing, nonnumeric, NaN, and infinite authenticated position sizes to zero, then skipped the row. It also treated a `None` response as an empty list. | A malformed venue response could be journaled as healthy flat truth and permit risk-increasing account targets. | Fixed in this cycle. |
| 2 | `volume_events_pit.py` derives required symbol-date tails from the klines being checked, so missing active-symbol tail days can shrink the requirement and pass. | Invalid full-PIT/historical-population evidence. | Fix before any PIT-dependent research; no active experiment exists. |
| 3 | Venue-wide regular and conditional order ownership is checked at startup but not on every reconciliation pass. | A post-start unowned order can remain outside kernel working exposure until it fills. | Next account-runtime candidate. |
| 4 | `downloaders.py` advances coverage markers after an empty incremental tail response. | A transient empty provider response can permanently suppress later recovery. | Next data-reliability candidate. |
| 5 | The liveness unit `Requires` the account owner that it is intended to report dead. | Owner startup failure can suppress the watchdog itself. | Next observability candidate. |
| 6 | VPS workflow concurrency is ref-scoped and the remote deploy script has no cross-caller mutex; host overrides are not bound to the configured public key. | Two branches can race one host, and migrations cannot rotate the pin coherently. | Next infrastructure candidate. |
| 7 | The shared file-lock age threshold can evict a lock whose process is demonstrably alive. | Long critical sections can overlap and corrupt journal or dataset state. | Needs a fail-closed ownership fix and multiprocess regression. |
| 8 | Sparse missing archive dates are fetched as one continuous multi-year interval. | Reproduction made 53 calls where two contiguous runs require two calls (96.2% fewer). | Measured performance candidate. |

The position parser was selected over the research-PIT defect because it sits
on the currently intended demo account's risk-admission boundary. The PIT
defect remains research-critical and must be resolved before a claim that
depends on historical membership.

## Reproduction and root cause

`BybitAccountReconciler.reconcile_once()` previously used `_finite_or_zero()`
for `row["size"]` and then ignored any row whose normalized size was not
positive. This collapsed unknown evidence into proven zero exposure:

| Venue response | Before | After |
| --- | --- | --- |
| `None` | Healthy empty snapshot | `RuntimeError`: non-list payload |
| Positive row with `size="not-a-number"` | Row skipped; healthy flat possible | `RuntimeError`: size must be numeric |
| `size="NaN"` or `"Infinity"` | Row skipped | `RuntimeError`: size must be finite |
| Negative size | Row skipped | `RuntimeError`: size must be non-negative |
| Positive size with missing/unknown side | Row skipped | `RuntimeError`: invalid side |
| Empty list | Healthy flat | Healthy flat (unchanged) |
| Canonical `{symbol, side="", size="0"}` row | Healthy flat | Healthy flat (explicitly tested) |

The pre-fix regression run failed immediately: `None` did not raise and a
mapping payload reached an `AttributeError` rather than a bounded validation
error. A separate isolated probe confirmed that a nonnumeric positive-position
row produced `healthy=True`, `venue_positions={}`, and no mismatch.

## Implementation

- Added one strict response boundary, `_validated_venue_position_rows()`.
- Require a list-like response containing only mapping rows.
- Require every row to have a nonempty symbol, finite nonnegative numeric size,
  and a canonical side. A positive position requires `Buy` or `Sell`; a zero
  row may use the exchange's canonical empty side.
- Feed only validated rows into aggregation, native-protection reconciliation,
  and position-row telemetry.
- Preserve existing valid-row aggregation, quantity tolerances, dual-side
  rejection, journal checkpointing, and strategy decisions.
- Added parameterized regressions for malformed payloads/rows and an explicit
  valid-zero control.

## Validation

- Focused regression: 13 passed.
- Complete reconciliation module: 21 passed.
- Locked Python 3.11 reconciliation module: 21 passed in 1.90 seconds.
- Focused Ruff: passed.
- Focused mypy: passed.
- Package-wide locked check: Python 3.11 + `mypy==1.20.2`, 85 modules passed.
- Repository-wide Ruff: passed.
- Package-wide local mypy: 85 modules passed.
- Full pytest snapshot: 1,588 passed in 21.69 seconds.

This is a correctness and runtime-safety change. No speed claim is made, so a
before/after benchmark is not applicable. No historical strategy output,
decision, target, ledger key, or continuous numeric result was changed.

## Limitations and next cycle

- Validation uses deterministic fakes; no authenticated venue mutation or VPS
  operation was needed or performed.
- Concurrent workspace activity changed unrelated files during the audit.
  Results describe the exact worktree at each named command, not a clean-commit
  deployment claim.
- Next cycle should address continuous open-order ownership reconciliation or
  the PIT tail false-positive. The former protects current demo execution; the
  latter is mandatory before PIT-dependent research.
