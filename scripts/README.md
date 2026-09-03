# Scripts & Tooling Directory Map (`scripts/`)

Directory structure, invocation roles, naming conventions, and decision-parity tooling.

---

## 1. Top-Level Entry Points & Subdirectories

| Path | Primary Operator | Purpose & Mandate | Reference |
| :--- | :--- | :--- | :--- |
| **`dev.sh`** | Developer | Local development pre-flight: `doctor` and `check` (ruff, shellcheck, mypy, pytest, rustfmt, clippy). | CLI |
| **`ops.sh`** | Operator / VPS | Fleet management router: status, logs, deploy, rollback, flatten, attest-flat. | [`docs/operations.md`](../docs/operations.md) |
| **`deploy_vps_live.sh`** | CI / Ops | Deployment engine: decoupled handover, binary unpacking, state takeover, rollback. | [`docs/operations.md`](../docs/operations.md) |
| **`runtime/`** | Systemd daemons | Service wrappers: liveness checks, Telegram notifications, Google Drive backup (`backup_state.sh`). | Systemd units |
| **`data/`** | Refresh jobs | Data pipelines: PIT manifests, Bybit candidate-window mark tapes, Binance metrics refresh. | [`docs/data.md`](../docs/data.md) |
| **`research/`** | Quant / Offline | Strategy scorers, equity curves, research-refresh pipelines, replay adapters. | [`docs/research/governance.md`](../docs/research/governance.md) |
| **`vps/`** | Emergency ops | Disaster recovery scripts: SSH rescue, emergency flatten, manual state dump. | Runbook |
| **`git-hooks/`** | Git | Automated pre-push quality gate (`dev.sh check`). | Git hook |

---

## 2. Script Naming Conventions

* `build_*`: Generates a point-in-time data artifact or manifest.
* `screen_*`: Fast Lane-1 exploratory factor or universe screen.
* `tune_*`: Parameter sweep or sensitivity analysis.
* `score_*`: Evaluates a committed rule against forward Lane-2 data.
* `check_*`: Read-only health, integrity, or drift diagnostic.
* `probe_*`: Direct read-only venue REST query.
* **Safety Invariant**: Scripts in `research/` and `data/` **never mutate venue state or place live orders**.

---

## 3. Strategy Replay & Decision-Parity Tools

```bash
# Replay native Rust Exodus contract against recorded test fixtures
python scripts/research/replay_native_strategy_contract.py \
  --sleeve exodus --input tests/fixtures/exodus_live_contract_replay_v1.json

# Render and verify native engine configuration TOML from registered JSON
engine render-native-config --check ...
```
