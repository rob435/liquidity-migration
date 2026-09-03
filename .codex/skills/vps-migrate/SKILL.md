---
name: vps-migrate
description: Migrate or recover the demo VPS and restore checked GitHub Actions operation. Use for VPS replacement, IP or host-key changes, SSH recovery, deploy-key mismatch, deploy workflow failures, or expected-commit drift. Derive hosts, fingerprints, keys, workflow modes, and service state from current canonical files and provider/GitHub state; never rely on values embedded in a skill, enable real money, or destroy a dirty checkout without explicit approval.
---

# VPS Migration, Deployment & Host Recovery

## 1. Purpose
Define technical specifications, SSH authentication mechanisms, deployment procedures, and recovery runbooks for maintaining and restoring the live production VPS fleet.

---

## 2. Spec Tables

### Host Specification & Network Identity

| Attribute | Canonical Value | Configuration Authority | Invariant |
| :--- | :--- | :--- | :--- |
| **Static IPv4** | `208.84.103.4` | DNS & GitHub Variable `VPS_HOST` | Bound in Bybit API key IP whitelist. |
| **SSH Port** | `22` | `/etc/ssh/sshd_config` on host | Standard OpenSSH port. |
| **SSH User** | `root` | `deploy_vps_live.sh` / GitHub Secrets | Root authentication via Ed25519 only. |
| **Host Key Type** | `Ed25519` | Known hosts pin in GitHub Actions | Pinned fingerprint verified against host console. |
| **Hardware** | 4 vCPU, 8 GB RAM, 127 GB SSD | Provider instance spec | Minimum requirements for 2 engines + 2 workers. |
| **State Directory** | `/var/lib/liquidity-migration/` | Systemd unit specifications | Shared signals, controls, WALs, logs. |

### Deployment & Recovery Tooling Reference

| Tool / Script | Syntax | Execution Target | Role |
| :--- | :--- | :--- | :--- |
| **Live Deploy** | `scripts/deploy_vps_live.sh deploy` | Local or CI $\rightarrow$ VPS | Atomic sync of pre-built release binaries and configs. |
| **Live Verify** | `scripts/deploy_vps_live.sh verify` | VPS (Read-only) | Reads live commits, unit heartbeats, and armed status. |
| **Rescue Command** | `scripts/vps/print_vps_recovery_command.sh <commit>` | Provider Console | Outputs pasteable rescue command restoring locked commit and keys. |
| **Ops Wrapper** | `scripts/ops.sh deploy [mode]` | Operator Workstation | Convenience router for deployment, rollback, and verify. |

### Fleet Startup Sequencing & Dependencies

| Startup Order | Systemd Unit | Dependencies | Success Criteria |
| :---: | :--- | :--- | :--- |
| **1** | `liquidity-migration-signal-worker-demo.service` | `network-online.target` | Publishes heartbeat to `/var/lib/liquidity-migration-signal-worker-demo/`. |
| **2** | `liquidity-migration-signal-worker-mainnet.service`| `network-online.target` | Publishes heartbeat to `/var/lib/liquidity-migration-signal-worker-mainnet/`. |
| **3** | `liquidity-migration-engine.service` | `signal-worker-demo.service` | Obtains demo lease lock; connects WebSocket; starts WAL. |
| **4** | `liquidity-migration-engine-mainnet.service` | `signal-worker-mainnet.service` | Validates `REAL_MONEY=true`; takes funded lease; starts WAL. |

### Incident Triage & Remediation Matrix

| Failure Symptom | Probable Cause | Remediation Recipe |
| :--- | :--- | :--- |
| **SSH Connection Refused** | VPS rebooted with altered host key or firewall rule. | Generate recovery script via `print_vps_recovery_command.sh` and paste into provider console. |
| **Deploy Rejected in CI** | Git tree dirty on VPS or uncommitted change point. | Inspect remote diff via SSH; never run `git reset --hard` without verifying patch. |
| **Rollback Triggered (180s)** | New binary failed heartbeat verification or crashed on boot. | Inspect unit journal: `journalctl -u <unit> -e`; fix root cause locally and re-deploy. |
| **Sequence Gap Crash Loop** | Signal worker crashed mid-batch; engine desynced from spool. | Blank `source_generation` in worker checkpoint, prune orphan files, restart worker then engine. |

---

## 3. Invariants

- **Must Never Enable `REAL_MONEY` During Recovery**: VPS migration and recovery *must never* enable funded trading on its own initiative; keep `REAL_MONEY=false` unless explicitly authorized.
- **Must Never Overwrite Dirty Checkout Blindly**: If the remote VPS checkout contains untracked or modified files, preserve the diff before deploying or resetting.
- **Must Verify Host Fingerprint Out-of-Band**: When host keys change, verify the new fingerprint through the provider console before updating CI secrets.
- **Worker Must Start Before Engine**: The signal worker *must* be online and writing its signal spool before the execution engine boots.

---

## 4. Operational Recipes

### Verify VPS Fleet Status
```bash
# Verify live deployment commit and unit health
SSH_TARGET=root@208.84.103.4 scripts/deploy_vps_live.sh verify

# Or via operator script
scripts/ops.sh status
```

### Deploy Exact Commit to Production VPS
```bash
# Push directly to main, build binaries, and execute atomic deploy
EXPECTED_COMMIT=$(git rev-parse HEAD) SSH_TARGET=root@208.84.103.4 \
  scripts/deploy_vps_live.sh deploy
```

### Emergency SSH Access Restoration via Console
```bash
# Generate single pasteable command to restore access from provider rescue console
scripts/vps/print_vps_recovery_command.sh $(git rev-parse HEAD)
```

### Roll Back to Previous Deployed Commit
```bash
# Execute immediate rollback to known good commit recorded in STATE.md
EXPECTED_COMMIT=23eff2fe SSH_TARGET=root@208.84.103.4 \
  scripts/deploy_vps_live.sh rollback
```
