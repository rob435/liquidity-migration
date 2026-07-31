# scripts/

Grouped by who runs the thing and why.

**The four files at the top level are the ones something outside this directory
names directly** — two operator routers and the two entry points systemd
executes. Everything they dispatch to lives in a group below, so moving a
grouped script never edits a unit file.

| Path | Who runs it | Contents |
| --- | --- | --- |
| `dev.sh` | you, before a push | `doctor`, `check` (ruff, mypy, pytest) |
| `ops.sh` | you, against the host | operator router: status, equity, reset, deploy, real-money preflight |
| `run_authorized_runtime.sh` | systemd | the wrapper every unit's `ExecStart` names; dispatches into `runtime/` |
| `deploy_vps_live.sh` | systemd / GitHub Actions | install, activate, verify, rollout, mainnet arming |
| `runtime/` | systemd, via the wrapper | one script per sleeve or service: the event engines, the account owners, the hedge and rmom jobs, the fleet liveness check |
| `vps/` | you, when the host is broken | SSH recovery, rescue-boot restore, rollout readiness |
| `maintain/` | you, one-shot | ledger reset, universe and instrument-rule freezes, demo-rule probes, venue-accounting reconcile, hedge warm-start |
| `data/` | you or the refresh timer | point-in-time data-root and panel builders, residual-momentum precompute |
| `research/` | you, offline | screens, scorers, equity curves, cost and diagnostic reports, the research-refresh workflow |
| `devtools/` | `dev.sh` | `repo_doctor.py`, `run_with_stub.py` |

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
