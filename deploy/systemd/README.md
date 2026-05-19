# VPS systemd deployment

The active VPS services are:

- `model050426-bybit-demo.service`: event entry/normal lifecycle runner.
- `model050426-bybit-risk.service`: fast exit-only risk runner.

Install or refresh it on the VPS from a trusted local checkout:

```bash
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/deploy_vps_live.sh
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/verify_vps_live.sh
```

The script refuses a dirty VPS checkout, forces the configured remote URL,
resets the deploy branch to `origin/main`, runs focused runtime tests, checks
the promoted TP26 and live TP21+FF6 strategy constants, backs up
`/etc/model050426/bybit-demo.env`, enforces the expected Telegram chat ID,
disables retired legacy units (`model050426.service` plus the old daily signal
timer/service), refreshes both active systemd units, restarts both services, and
prints the active systemd state plus non-secret entry-profile settings. The
verify script is read-only and checks the same commit, strategy
constants, Telegram chat ID, systemd unit settings, and active service state
without pulling or restarting; it also fails if retired legacy units are still
active or enabled.
Both scripts wait briefly before checking service activity so a process that
dies immediately after startup does not produce a false pass. Override with
`SYSTEMD_SETTLE_SECONDS=<seconds>` if needed.

GitHub Actions can also run the same checked path from
`.github/workflows/vps-deploy.yml`. Repository secret `VPS_SSH_PRIVATE_KEY`
holds the dedicated GitHub Actions deploy key; the console recovery script adds
the matching public key to `/root/.ssh/authorized_keys`. The workflow derives
the secret's public key and checks its fingerprint before SSH, so a rotated or
mis-pasted secret fails before deployment. Run the `VPS Deploy` workflow
manually in `verify` or `deploy` mode, or let guarded `main` pushes to
live-code/deploy paths trigger deployment. Optional repository variables:
`VPS_HOST`, `VPS_USER`, `VPS_ED25519_FINGERPRINT`, and
`EXPECTED_TELEGRAM_CHAT_ID`.

If the VPS was rebuilt and SSH rejects the local key, add this public key back
to the VPS through the provider console before running the deploy script. The
recovery script also installs the GitHub Actions public deploy key shown below.

```text
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp hetzner
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKykZKBc1KapzJXdFORWMhjaNFC4zPeEZkOAbu32aTXX model050426-github-actions-20260519
```

On the VPS, the target file is normally `/root/.ssh/authorized_keys` for the
default `SSH_TARGET=root@204.168.202.167`.

If SSH is unavailable but provider console root access works, run the recovery
deploy directly on the VPS:

```bash
scripts/print_vps_recovery_command.sh
scripts/print_vps_recovery_command.sh --recommended-only
scripts/print_vps_recovery_command.sh --rescue-only

apt-get update && apt-get install -y ca-certificates curl
curl -fsSL https://raw.githubusercontent.com/rob435/MODEL05042026/main/scripts/vps_restore_ssh_access.sh | bash

apt-get update && apt-get install -y ca-certificates curl
curl -fsSL https://raw.githubusercontent.com/rob435/MODEL05042026/main/scripts/vps_rescue_restore_ssh_access.sh | bash

EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/deploy_vps_live.sh
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/verify_vps_live.sh
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/wait_for_vps_recovery_and_deploy.sh

apt-get update && apt-get install -y ca-certificates curl
curl -fsSL https://raw.githubusercontent.com/rob435/MODEL05042026/main/scripts/vps_console_recover_and_deploy.sh | CLEAN_DIRTY_CHECKOUT=1 bash
```

Prefer the generated pinned command from `scripts/print_vps_recovery_command.sh`
when possible; use `scripts/print_vps_recovery_command.sh --recommended-only`
when you want only the full installed-OS command to paste into the provider
console, or `scripts/print_vps_recovery_command.sh --rescue-only` when you want
only the Hetzner Rescue SSH-key restore command.
`scripts/vps_restore_ssh_access.sh` only restores root public-key SSH access,
prints the restored authorized-key fingerprints, and exits, which is useful
when you want this local checkout or GitHub Actions to run the checked deploy
after access is fixed. `scripts/vps_rescue_restore_ssh_access.sh` is the
Hetzner Rescue fallback: run it as rescue root when the installed OS console is
unavailable, then reboot back to local disk and run the checked deploy from this
checkout. `scripts/wait_for_vps_recovery_and_deploy.sh` can be left running
locally while you perform the console or Rescue step; it waits until public-key
SSH works, then calls the checked deploy and read-only verifier with the pinned
commit. The full console recovery restores the same SSH
access, prints the same fingerprints, clones or repairs `/opt/MODEL050426`,
forces the configured remote URL, resets the deploy branch to `origin/main`,
builds the local venv if needed, installs missing Ubuntu deploy prerequisites,
writes an sshd recovery override for root public-key login, prints the effective
sshd root-login settings, validates the promoted TP26 and live TP21+FF6
constants, refreshes systemd, restarts both live services, and prints non-secret
service state. Set `EXPECTED_COMMIT=<full sha>` before `bash` if you want the
console deploy to refuse anything except one pinned commit.
The console script also waits before checking active service state; override
with `SYSTEMD_SETTLE_SECONDS=<seconds>` if needed.

The generated full recovery command sets `CLEAN_DIRTY_CHECKOUT=1` by default.
If the existing `/opt/MODEL050426` checkout is dirty, the script saves tracked
diffs, status, and a tarball/list of untracked non-ignored files under
`/root/model050426-deploy-backups` before running `git reset --hard` and
`git clean -fd`. Ignored live data such as `data/bybit-demo-event` is not
removed by that clean command. The generated strict command omits
`CLEAN_DIRTY_CHECKOUT=1` and refuses a dirty checkout.

Manual install or refresh on the VPS:

```bash
cp deploy/systemd/model050426-bybit-demo.service /etc/systemd/system/model050426-bybit-demo.service
cp deploy/systemd/model050426-bybit-risk.service /etc/systemd/system/model050426-bybit-risk.service
systemctl daemon-reload
systemctl enable --now model050426-bybit-demo.service
systemctl enable --now model050426-bybit-risk.service
systemctl restart model050426-bybit-demo.service
systemctl restart model050426-bybit-risk.service
```

Required secrets live outside git in:

```text
/etc/model050426/bybit-demo.env
```

That environment file must define the Bybit demo API credentials and Telegram
credentials. Deploy/recovery backs it up and sets only `TELEGRAM_CHAT_ID` to the
expected target, preserving the API secrets and bot token. Telegram is enabled
for material alerts only: entries, exits, position reconciliation, or
position-report errors. Quiet no-trade cycles still write local reports but must
not notify. The services submit demo orders only.
The entry service currently uses `STRATEGY_PROFILE=demo_relaxed`, a higher-frequency
test-only profile with separate full-PIT evidence in `docs/system_status.md`.
It shares the promoted strategy's conservative `promoted_quality_squeeze` entry
router for promoted-grade events but keeps relaxed `demo_relaxed` gates for
forward plumbing visibility. It is not the promoted research default. The risk
service does not open entries; it repairs exchange-native stop/TP state, listens to
demo private WebSocket position/order/execution streams plus the mainnet public
ticker stream, and submits reduce-only exits. On the demo account, WebSocket
decides exits while REST remains the order-submit fallback because Bybit
WebSocket Trade does not currently support demo trading. The demo socket uses
the normal private execution stream; `execution.fast` is disabled because the
demo private socket rejects that topic.
`STREAM_START_TIMEOUT_SECONDS` bounds private/public WebSocket startup so a
blocked subscription is reported while REST reconciliation and exchange-native
stops keep covering open risk.

Champion/challenger safety: `scripts/run_bybit_demo_event_engine.sh` refuses
`SUBMIT_ORDERS=1` unless `STRATEGY_PROFILE=demo_relaxed` or the deprecated
`observe` alias is used. Promoted, no-crowding, sniper, execution-only, and
hedge candidates are shadow-only until the manifest in
`champion-challenger` is intentionally updated and re-audited.

The retired `model050426-bybit-demo-signal.timer` / `.service` daily signal scan
must stay disabled; the active runner is the event-driven loop above.
