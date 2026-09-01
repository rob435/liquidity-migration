---
name: vps-migrate
description: Migrate or recover the demo VPS and restore checked GitHub Actions operation. Use for VPS replacement, IP or host-key changes, SSH recovery, deploy-key mismatch, deploy workflow failures, or expected-commit drift. Derive hosts, fingerprints, keys, workflow modes, and service state from current canonical files and provider/GitHub state; never rely on values embedded in a skill, enable real money, or destroy a dirty checkout without explicit approval.
---

# Migrate or recover the VPS

This workflow crosses SSH, credentials, deployment, and running demo
services. Derive current values from:

- `.github/workflows/vps-deploy.yml`;
- `scripts/deploy_vps_live.sh` with
  `deploy|verify|stop-mainnet|disarm-mainnet`;
- `scripts/vps/print_vps_recovery_command.sh` and the current SSH restore scripts;
- `deploy/systemd/README.md`, unit files, and `deploy/sleeves.env`;
- GitHub variables/secrets and the provider console.

Do not copy hosts, fingerprints, public keys, chat IDs, or branches from old
receipts or this skill.

## Preflight

1. Confirm the host, provider state, repository, branch, exact commit, and
   intended mode.
2. Inspect local and remote worktree state without cleaning it.
3. Confirm the task authorizes recovery/deployment, not only diagnosis.
4. Verify all credential paths remain demo and `REAL_MONEY=false`.
5. Read current workflow/script refusal conditions.
6. Record whether the fleet is quiescent and which commit is installed.

If the checkout is dirty, preserve and inspect its diff first. Do not reset,
overwrite, or delete it without explicit cleanup authority and a verified
archive/patch.

## Establish SSH identity

- Obtain the target's Ed25519 host fingerprint directly and verify it through the
  provider console or another trusted channel before changing pins.
- Derive a supplied private key's public fingerprint locally without printing
  the private key.
- Distinguish host key, deploy key, and operator key. Rotate only the identity in
  scope.
- Update GitHub variables/secrets through the authorized interface. Never commit
  private keys or private environment files.

## Restore access

Generate recovery material from a trusted checkout at the exact intended commit:

```bash
scripts/vps/print_vps_recovery_command.sh COMMIT
scripts/vps/print_vps_recovery_command.sh --rescue-only COMMIT
```

Inspect the generated command before using the provider console. It embeds the
restore script from the named Git object rather than fetching an unpinned branch
tip.

After SSH returns, confirm the repository/commit, strict environment-file
ownership and modes, authorized keys, demo-only credential set, and absence of
unexpected `REAL_MONEY` or mainnet variables.

## Deploy operation

```bash
# Fetch the exact commit, build, install, restart the fleet.
EXPECTED_COMMIT=COMMIT SSH_TARGET=USER_AT_HOST \
  scripts/deploy_vps_live.sh deploy

# Read-only fleet summary.
SSH_TARGET=USER_AT_HOST scripts/deploy_vps_live.sh verify
```

Use current script help and environment names; placeholders are not literal values.

Deploy requires the target commit to be on the selected remote branch. It
installs locked dependencies and the current unit manifest, renders the native
configs, runs state takeover while the owners are stopped, and starts the
signal worker before the engine in each realm. The funded realm starts only
while `REAL_MONEY` is armed. Verify never repairs drift.

Confirm the exact commit, resolved sleeves, credential mode, service/timer
state, start order, liveness, and journal/venue agreement appropriate to the
task.

## GitHub Actions

The manual workflow exposes `deploy`, `verify`, and `disarm-mainnet`. It runs
CI first, configures the pinned SSH identity, and passes the workflow commit to
the selected mode. A verify workflow cannot update a stale checkout; run
`deploy`.

If host/IP/deploy identity changes permanently, update workflow variables or
pins, scripts, tests, recovery material, and operator docs together. Run the
focused runtime/deploy tests and lint before proposing a push.

## Diagnose by symptom

- Host-key failure: independently verify the target, then update the pin.
- Deploy-key mismatch: correct the secret or perform a complete intentional
  rotation across workflow, authorized keys, scripts, and tests.
- Permission denied: verify user, authorized keys, modes, and provider state.
- Expected-commit mismatch: run `deploy`; verify is not deploy.
- Dirty checkout: inspect and archive; request cleanup authority.
- CI-only failure: compare workflow variables/secrets and environment with the
  successful local command without exposing secrets.

Never enable real-money trading as part of VPS recovery. Mainnet requires a
separate control plane and exact owner authorization under `AGENTS.md`.
