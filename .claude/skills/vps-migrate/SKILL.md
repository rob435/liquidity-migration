---
name: vps-migrate
description: "Migrate or rebuild the liquidity-migration live VPS and restore GitHub Actions deploy. Use when switching VPS IP, Hetzner rebuild, SSH/deploy workflow failures, host-key fingerprint errors, deploy-key mismatch, or 'expected commit X but VPS has Y'. Covers GitHub vars/secrets, pinned host/deploy fingerprints, authorized_keys recovery, local checked deploy, and workflow_dispatch modes. Canonical refs: .github/workflows/vps-deploy.yml, deploy/systemd/README.md, scripts/deploy_vps_live.sh."
---

# VPS migrate / rebuild runbook

Live demo runs on a single VPS (`/opt/liquidity-migration`). GitHub Actions
(`.github/workflows/vps-deploy.yml`) and local scripts share the same checked
deploy path. **Demo only — never set `REAL_MONEY=true`.**

## When to use

- New box, Hetzner rebuild, IP change, or provider console recovery
- CI step **Verify VPS host key** fails (wrong `VPS_ED25519_FINGERPRINT`)
- CI step **Verify deploy key fingerprint** fails (wrong `VPS_SSH_PRIVATE_KEY`)
- **Verification failed: expected commit … but VPS has …** (stale checkout; see §5)
- User asks to migrate VPS, fix deploy workflow, or sync VPS to `main`

## Migration checklist

Copy and tick through:

```
- [ ] New VPS has /opt/liquidity-migration (clone or console recovery)
- [ ] /etc/liquidity-migration/bybit-demo.env present (copy from old box or backup)
- [ ] GitHub variable VPS_HOST = new IP/DNS
- [ ] GitHub variable VPS_ED25519_FINGERPRINT = ssh-keyscan result (§2)
- [ ] GitHub secret VPS_SSH_PRIVATE_KEY = canonical deploy key (§3) — NOT a new random key unless rotating
- [ ] /root/.ssh/authorized_keys has GA + operator public keys (§4)
- [ ] Local or CI deploy with EXPECTED_COMMIT = target main SHA (§5)
- [ ] verify-ok / deploy-verify-ok on that SHA
- [ ] (Optional) Update workflow default + tests if IP/fingerprint changed permanently (§6)
```

---

## 1. Read canonical sources first

| What | Where |
|------|--------|
| Workflow + pinned defaults | `.github/workflows/vps-deploy.yml` |
| Systemd + SSH recovery keys | `deploy/systemd/README.md` |
| Checked deploy | `scripts/deploy_vps_live.sh` |
| Read-only verify | `scripts/verify_vps_live.sh` |
| Wait for SSH then deploy | `scripts/wait_for_vps_recovery_and_deploy.sh` |
| Pinned console paste commands | `scripts/print_vps_recovery_command.sh` |

Default `SSH_TARGET` in scripts: `root@116.202.15.128` (override with env).

---

## 2. Host key fingerprint (new box = new pin)

Every rebuild gets a **new SSH host key**. You must update the pin.

```bash
NEW_HOST=116.202.15.128   # or the new IP
ssh-keyscan -T 20 -t ed25519 "$NEW_HOST" | ssh-keygen -lf - -E sha256
```

Set GitHub **repository variable** (not secret):

- `VPS_ED25519_FINGERPRINT` → e.g. `SHA256:TJRbvgB8nfhwmNDv4hM3jDkPXnRv6BGLQ3cPst2PfE4`
  (the 2026-06-09 rebuild's pin — ALWAYS re-derive from the box; never copy an old doc)

Optional: `VPS_HOST`, `VPS_USER` (default `root`).

Workflow fallback default lives in `vps-deploy.yml` env block; vars override it.

**Do not skip this on a new VPS** — leaving the old fingerprint makes
**Verify VPS host key** fail closed (by design).

---

## 3. GitHub Actions deploy key (do not confuse with host key)

The workflow **rejects** arbitrary private keys. Secret `VPS_SSH_PRIVATE_KEY`
must derive to this fingerprint (checked in workflow + `tests/test_runtime_scripts.py`
— `.github/workflows/vps-deploy.yml` `GITHUB_ACTIONS_DEPLOY_KEY_FINGERPRINT` is the
canonical value; if this doc and the workflow disagree, the workflow wins):

```text
SHA256:Gki6YjdsUksh/TozZ/55sxSwimK7T9MOf2pgWSbqFNU
```

Matching **public** key (must be in VPS `authorized_keys`; also the
`GITHUB_ACTIONS_SSH_PUBLIC_KEY` default in `scripts/vps_restore_ssh_access.sh`):

```text
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICWcgpE3GLy65yWFuh5RAH5CEgyLqRPAGvROXGwAxmVv liquidity-migration-github-actions-20260609
```

Operator console key (also installed by recovery scripts):

```text
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp hetzner
```

If the user pasted a **new** key into `VPS_SSH_PRIVATE_KEY` during migration,
either restore the canonical private key in the secret **or** do a full rotation
(§6).

---

## 4. Restore SSH on a fresh / rebuilt VPS

From provider console as **root** (no SSH yet):

```bash
# From a trusted local checkout at the commit you will deploy:
scripts/print_vps_recovery_command.sh --recommended-only
# Paste the printed curl | bash into the Hetzner/install OS console
```

Minimal SSH-only restore:

```bash
scripts/print_vps_recovery_command.sh
# Use the "Minimal SSH-only recovery" block, or:
curl -fsSL https://raw.githubusercontent.com/rob435/liquidity-migration/main/scripts/vps_restore_ssh_access.sh | bash
```

Rescue mode (Hetzner Rescue boot):

```bash
scripts/print_vps_recovery_command.sh --rescue-only
```

Then either local deploy (§5) or GitHub **wait-deploy** (workflow_dispatch).

---

## 5. Sync VPS to `main` (deploy vs verify)

| Mode | Pulls? | When |
|------|--------|------|
| `scripts/deploy_vps_live.sh` | Yes — `fetch` + checkout `origin/main`, then pin check | Always to fix drift |
| `scripts/verify_vps_live.sh` | **No** — only checks current `HEAD` | After deploy, or CI `verify` |
| GH `workflow_dispatch` → **verify** | No | Read-only; fails if behind |
| GH `workflow_dispatch` → **deploy** / **wait-deploy** | Yes (via deploy script) | Use to ship |
| GH `push` to `main` (guarded paths) | Yes | Auto-deploy |

**Commit mismatch** (`expected commit X but VPS has Y`):

1. VPS is behind — `origin/main` may already be correct; working tree was not updated.
2. **Fix:** run deploy with the SHA GitHub expects (usually `main` tip):

```bash
cd /path/to/liquidity-migration
TARGET="$(git rev-parse origin/main)"   # or the failing GITHUB_SHA
EXPECTED_COMMIT="$TARGET" \
EXPECTED_TELEGRAM_CHAT_ID=8388367561 \
SSH_TARGET=root@NEW_HOST \
scripts/deploy_vps_live.sh

EXPECTED_COMMIT="$TARGET" \
SSH_TARGET=root@NEW_HOST \
scripts/verify_vps_live.sh
```

Success markers: `deploy-verify-ok commit=…` and `verify-ok commit=…`.

If deploy refuses **dirty checkout**, use console recovery with
`CLEAN_DIRTY_CHECKOUT=1` (`print_vps_recovery_command.sh` recommended block).

Probe without deploying:

```bash
ssh root@NEW_HOST 'cd /opt/liquidity-migration && git rev-parse HEAD && git fetch origin main && git rev-parse origin/main'
```

---

## 6. Permanent IP / fingerprint / key rotation (code change)

When the new IP or host fingerprint is stable, update **in lockstep**:

1. `.github/workflows/vps-deploy.yml` — `VPS_HOST` / `VPS_ED25519_FINGERPRINT` defaults
2. `tests/test_runtime_scripts.py` — pinned fingerprint assertions
3. Script defaults: `SSH_TARGET` in `deploy_vps_live.sh`, `verify_vps_live.sh`,
   `wait_for_vps_recovery_and_deploy.sh`, `print_vps_recovery_command.sh` comments
4. `deploy/systemd/README.md` — documented IP and recovery examples

**Deploy key rotation only** (rare): also update
`GITHUB_ACTIONS_DEPLOY_KEY_FINGERPRINT` in the workflow, the public key in
`vps_restore_ssh_access.sh` / `vps_console_recover_and_deploy.sh`, GitHub secret
`VPS_SSH_PRIVATE_KEY`, and VPS `authorized_keys`.

Run before push:

```bash
.venv/bin/python -m ruff check liquidity_migration tests scripts
.venv/bin/python -m pytest -q tests/test_runtime_scripts.py -k vps_deploy
```

---

## 7. GitHub Actions workflow modes

| `workflow_dispatch` mode | Effect |
|--------------------------|--------|
| **deploy** | Checked deploy to `GITHUB_SHA` |
| **wait-deploy** | Poll SSH until up, then deploy + verify |
| **verify** | Read-only; **fails if VPS behind** — not a deploy |

Push to `main` on guarded paths runs **Checked deploy** only (not verify).

Pre-push gate still applies if you commit workflow changes.

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|----------------|-----|
| Host key verification failed | Old `VPS_ED25519_FINGERPRINT` | §2 |
| Deploy key fingerprint grep failed | Wrong secret key | §3 — restore canonical or rotate §6 |
| Permission denied (publickey) | Missing GA key on box | §4 |
| expected commit X but VPS has Y | Verify-only or never deployed | §5 deploy |
| Refusing deploy: dirty checkout | Local changes on VPS | Console recovery `CLEAN_DIRTY_CHECKOUT=1` |
| ssh-keyscan timeouts in CI | Brute-force load on 22 | Workflow retries; check firewall allows GitHub |
| Deploy OK locally, CI fails | Vars/secrets not set in GitHub | Set `VPS_HOST`, fingerprint, secret |

---

## 9. Agent execution order

1. Confirm target commit: `git rev-parse origin/main` (or user-supplied `GITHUB_SHA`).
2. Get host fingerprint if IP changed (§2).
3. Confirm user updated GitHub vars/secrets (or offer exact values from keyscan).
4. If SSH works from environment: run §5 deploy then verify with `EXPECTED_COMMIT`.
5. If SSH down: print `scripts/print_vps_recovery_command.sh` output for operator paste; suggest **wait-deploy** after keys restored.
6. If IP/fingerprint defaults in repo are stale vs production, propose §6 PR after deploy is green.

Never enable real-money trading. Never commit `.env` or private keys.
