# Scripts & Tooling Directory Map (`scripts/`)

Directory structure, invocation roles, naming conventions, and decision-parity tooling.

---

## 1. Top-Level Entry Points & Subdirectories

| Path | Primary Operator | Purpose & Mandate | Reference |
| :--- | :--- | :--- | :--- |
| **`dev.sh`** | Developer | Local development pre-flight: `doctor` and `check` (ruff, shellcheck, mypy, pytest, rustfmt, clippy). | CLI |
| **`ops.sh`** | Operator / VPS | Fleet management router: status, logs, why, alert-drill, deploy, rollback, flatten, attest-flat and verify-account-identity per realm, canary-order on any realm the fleet does not yet trade, and stop/disarm per funded realm. Every realm list comes from [`deploy/realm_fields.tsv`](../deploy/realm_fields.tsv), rendered from [`deploy/realms.tsv`](../deploy/realms.tsv). | [`docs/operations.md`](../docs/operations.md) |
| **`deploy_vps_live.sh`** | CI / Ops | Deployment engine: decoupled handover, binary unpacking, state takeover, rollback. Stages only bytes that pass `release_artifact.py verify --require-candidate`. | [`docs/operations.md`](../docs/operations.md) |
| **`release_artifact.py`** | CI | Release archives: `smoke` qualifies the exact candidate with a release-profile recovery/functional run and packs the receipts, `qualify` adds the on-demand paired latency study, `verify`/`unpack --require-candidate` refuse an archive with no candidate qualification or a different compilation contract. | [`docs/operations.md`](../docs/operations.md) |
| **`runtime/`** | Systemd daemons | Service wrappers: liveness checks, Telegram notifications, Google Drive backup (`backup_state.sh`), host storage reclamation (`reclaim_host_storage.py`). | Systemd units |
| **`runtime/engine_status.py`** | Operator | `engine_status.py HEARTBEAT_PATH [--now-ms N]`: one engine's heartbeat as HEALTH, EXPOSURE and BLOCKERS. Read-only stdlib; no venue access and no new state. Reached by `ops.sh why [REALM]` and by each `deploy verify`. | [`docs/operations.md`](../docs/operations.md) |
| **`data/`** | Refresh jobs | Data pipelines: PIT manifests, Bybit candidate-window mark tapes, Binance metrics refresh. | [`docs/data.md`](../docs/data.md) |
| **`research/`** | Quant / Offline | Strategy scorers, equity curves, research-refresh pipelines, replay adapters. | [`docs/research/governance.md`](../docs/research/governance.md) |
| **`vps/`** | Emergency ops | Disaster recovery scripts: SSH rescue, emergency flatten, manual state dump. | Runbook |
| **`git-hooks/`** | Git | Automated pre-push quality gate (`dev.sh check`). | Git hook |

---

## 2. Script Naming Conventions

* `build_*`: Generates a point-in-time data artifact or manifest.
* `screen_*`: Fast Lane-1 exploratory factor or universe screen.
* `tune_*`: Parameter sweep or sensitivity analysis.
* Research scorers compare explicitly selected historical windows and recorded live outcomes.
* `check_*`: Read-only health, integrity, or drift diagnostic.
* `probe_*`: Direct read-only venue REST query.
* **Safety Invariant**: Scripts in `research/` and `data/` **never mutate venue state or place live orders**.

---

## 3. Strategy Replay & Decision-Parity Tools

```bash
# Render every realm's units, env templates and fleet-manifest rows from
# deploy/realms.tsv, then prove the checked-in bytes match
python -m liquidity_migration.policy.realms render
python -m liquidity_migration.policy.realms check

# Replay native Rust Exodus contract against recorded test fixtures
python scripts/research/replay_native_strategy_contract.py \
  --sleeve exodus --input tests/fixtures/exodus_live_contract_replay_v1.json

# Render and verify native engine configuration TOML from registered JSON
engine render-native-config --check ...

# Replay a recorded market_tape through the live loop on a simulated venue
# and read the engine's own trades/equity/report back as research metrics
python scripts/research/run_engine_backtest.py --config engine/engine.demo.toml \
  --tape tape.jsonl --instruments instruments.json.zst --out-dir var/backtests/run-1
```
