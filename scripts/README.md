# scripts/

Grouped by who runs the thing and why.

**The five files at the top level are the ones something outside this directory
names directly** — the two routers, the owner's one-click redeploy, the deploy
engine, and the wrapper systemd executes. Everything they dispatch to lives in
a group below, so moving a grouped script never edits a unit file.

| Path | Who runs it | Contents |
| --- | --- | --- |
| `dev.sh` | you, before a push | `doctor`, `check` (ruff, mypy, pytest, the engine's Rust tests) |
| `ops.sh` | you, against the host | the operator router; `ops.sh help`, and the verb table in [`docs/operations.md`](../docs/operations.md) |
| `deploy_everything.command` | **the owner, by double-click** | the whole redeploy in one click: stop the fleet (funded units included), install GitHub main, activate — which starts the funded fleet when `REAL_MONEY` is armed — and verify. No prompts; clicking it is the decision. |
| `run_authorized_runtime.sh` | systemd | the wrapper every unit's `ExecStart` names; dispatches into `runtime/` |
| `deploy_vps_live.sh` | you (via `ops.sh deploy`) / GitHub Actions | the deploy engine; modes are tabulated in [`docs/operations.md`](../docs/operations.md) |
| `runtime/` | systemd, via the wrapper | the LONG and CARRY event engines; the fleet liveness check the two liveness timers run (watchdog alerts plus the daily engine digest); `notify_book_changes.py`, which the trade-notify timer runs to put entries, exits and their P&L on the phone; `upload_forward_capture.sh`, which verifies each new compressed market-data batch in Drive; `backup_state.sh`, the nightly off-box copy of the WALs and trade files; and `chaos_drill.sh`, the weekly demo crash-recovery rehearsal |
| `vps/` | you, when the host is broken | SSH recovery, rescue-boot restore, rollout readiness, flatten |
| `maintain/` | you, one-shot | candidate-universe freeze and schema migration |
| `data/` | you or the refresh timer | point-in-time data-root and panel builders, residual-momentum precompute, and the Binance positioning-metrics refresh (`refresh_binance_metrics.py`, feeds the panel's `--metrics-root` columns) |
| `research/` | you, offline | scorers, equity curves, deterministic quote-arm sweeps, the research-refresh workflow, and `daily_evidence_run.sh`; `llm_driver_ledger.py` is also run by the fleet timer |
| `devtools/` | `dev.sh` | `repo_doctor.py` |
| `git-hooks/` | git, on push | the tracked `pre-push` gate, which runs `dev.sh check` before anything leaves |

## Conventions

- A script computes the repository root as `Path(__file__).resolve().parents[2]`
  (shell: `"$(dirname …)/../.."`). Grouped scripts sit two levels down.
- `build_*` writes a data artifact, `screen_*` and `tune_*` explore a mechanism,
  `score_*` grades a registered config forward, `check_*` reports without
  mutating, `probe_*` asks the venue, `freeze_*` pins an artifact, `run_*` is a
  long-lived or scheduled job.
- Nothing in `research/` or `data/` may mutate a venue.

Operator commands are documented in [`docs/operations.md`](../docs/operations.md);
the research and data CLI is `python -m liquidity_migration --help`.
