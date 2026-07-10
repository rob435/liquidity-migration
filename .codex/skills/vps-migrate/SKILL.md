---
name: vps-migrate
description: Migrate or rebuild the demo and paper VPS and restore checked GitHub Actions deployment. Use for VPS replacement, IP or host-key changes, SSH recovery, deploy-key mismatch, workflow failures, or expected-commit drift. Derive all hosts, fingerprints, keys, workflow modes, and service state from current canonical files and provider or GitHub state; never rely on values embedded in a skill, enable real money, or destroy a dirty checkout without explicit approval.
---

# Migrate or recover the VPS

Keep this workflow low-freedom because it crosses SSH, credentials, deploy, and
running demo/paper services. Derive current values from:

- `.github/workflows/vps-deploy.yml`;
- `scripts/deploy_vps_live.sh` and `scripts/verify_vps_live.sh`;
- `scripts/wait_for_vps_recovery_and_deploy.sh`;
- `scripts/print_vps_recovery_command.sh` and local recovery scripts;
- `deploy/systemd/README.md`, units, and `deploy/sleeves.env`;
- GitHub variables/secrets and the provider console.

Do not copy IPs, host fingerprints, public keys, chat IDs, or fallback branches
from old docs or this skill.

## Preflight

1. Confirm the target host, provider state, repository, branch, and exact commit.
2. Inspect local and remote worktree status without cleaning anything.
3. Confirm the task authorizes deploy/recovery, not merely diagnosis.
4. Verify that all credential paths remain demo/paper and `REAL_MONEY=false`.
5. Read current script/workflow help and refusal conditions.

If the remote checkout is dirty, preserve and inspect its diff first. Do not use
`CLEAN_DIRTY_CHECKOUT`, reset, overwrite, or delete files without explicit owner
approval for that cleanup and a verified archive/patch.

## Establish SSH identity

- Obtain the new Ed25519 host fingerprint directly from the target and verify it
  through the provider console or another trusted channel before updating pins.
- Read the expected deploy-key fingerprint from the current workflow/scripts;
  derive the supplied private key's public fingerprint locally without printing
  the private key.
- Distinguish host key, deploy key, and operator key. Rotate only the identity the
  task calls for.
- Update GitHub variables/secrets through the authorized interface. Never commit
  private keys or environment secrets.

## Recover from a trusted source

Prefer commands generated from a trusted local checkout at the exact target
commit. Avoid unpinned branch-tip `curl | bash` recovery. If the provider console
is required, use the repository's generator, inspect its output, and have the
operator paste only the scoped recovery command.

Confirm on the host:

- the repository and intended commit exist;
- demo environment and sleeve files are present with correct ownership/mode;
- authorized keys match the verified identities;
- no unexpected mainnet credential or `REAL_MONEY` setting is active.

## Deploy and verify

Use the checked deploy with an exact `EXPECTED_COMMIT`, then the read-only
verifier. A verify-only workflow never repairs a stale checkout.

```bash
EXPECTED_COMMIT=COMMIT SSH_TARGET=USER_AT_HOST scripts/deploy_vps_live.sh
EXPECTED_COMMIT=COMMIT SSH_TARGET=USER_AT_HOST scripts/verify_vps_live.sh
```

Use the current script syntax and environment names; the placeholders above are
not literal values. Confirm the success marker, checked-out commit, resolved
sleeves, credential mode, service/timer state, liveness, and reconciliation.

If the IP, host pin, or deploy identity changed permanently, update workflow,
tests, script defaults, recovery material, and operator docs together. Run the
focused runtime/deploy tests plus relevant lint before proposing a push.

## Diagnose by symptom

- Host-key failure: verify the new host independently, then update the host pin.
- Deploy-key fingerprint failure: correct the secret or perform a complete,
  intentional rotation across workflow, authorized keys, scripts, and tests.
- Permission denied: verify user, authorized keys, file modes, and provider
  console state.
- Expected-commit mismatch: run checked deploy; do not pretend verify is deploy.
- Dirty-checkout refusal: inspect, archive, and request cleanup authority.
- CI-only failure: compare repository variables/secrets and workflow environment
  with the successful local command without exposing secrets.

Never enable real-money trading as part of VPS recovery. Mainnet requires a
separate control plane and exact owner authorization under `docs/governance.md`.
