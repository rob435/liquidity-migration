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
| `runtime/` | systemd, via the wrapper | fleet liveness checks, engine position/trade notifications, verified forward uploads, nightly state backups, and the weekly demo recovery drill |
| `vps/` | you, when the host is broken | SSH recovery, rescue-boot restore, rollout readiness, flatten |
| `maintain/` | you, one-shot | candidate-universe freeze |
| `data/` | you or the refresh timer | point-in-time data-root and panel builders, candidate-window Bybit one-minute trade/mark tapes, residual-momentum precompute, and the Binance positioning-metrics refresh (`refresh_binance_metrics.py`, feeds the panel's `--metrics-root` columns) |
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

## Decision-contract checks

These commands exercise the strategy seam without venue authority:

- `python scripts/research/replay_native_strategy_contract.py --sleeve exodus
  --input tests/fixtures/exodus_live_contract_replay_v1.json` calls the native
  Rust Exodus reducer and prints its canonical decision-contract report.
- `python scripts/research/compare_toxic_quoter.py --help` describes the
  recorded-market replay. Python streams normalized events to the Rust quoter
  reducer; it does not carry a second copy of the quote decision.
- `engine render-native-config` is the sole registered-rule renderer for the
  native directional blocks and the maker canary. Deployment runs it once to
  write the installed config and again in check mode before activation.

These are code-parity checks. They do not prove fills, profit, or permission to
trade.

Operator commands are documented in [`docs/operations.md`](../docs/operations.md);
the research and data CLI is `python -m liquidity_migration --help`.

[`deploy/fleet_manifest.tsv`](../deploy/fleet_manifest.tsv) is the one inventory
for unit identity, lifecycle and stop order, timers, operator policy, health
checks, and runtime input/output artifacts. Rollout, Rust worker/engine
activation, and fleet tests derive their unit sets from that manifest.
`run_authorized_runtime.sh` keeps an explicit current-unit entrypoint table
because the manifest does not encode commands; tests require exact service
coverage between them.
