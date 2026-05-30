# VPS systemd deployment

The active VPS services are:

- `liquidity-migration-bybit-demo.service`: event entry/normal lifecycle runner.
- `liquidity-migration-bybit-risk.service`: fast exit-only risk runner.
- `liquidity-migration-bybit-paper.service`: dry-run paper shadow of the demo
  runner on a separate data root (`data/bybit-paper-event`) — submits no orders.

Install or refresh it on the VPS from a trusted local checkout:

```bash
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/deploy_vps_live.sh
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/verify_vps_live.sh
```

The script refuses a dirty VPS checkout, forces the configured remote URL,
resets the deploy branch to `origin/main`, runs focused runtime tests, checks
the promoted TP26 and live TP21+FF6 strategy constants, backs up
`/etc/liquidity-migration/bybit-demo.env`, enforces the expected Telegram chat ID,
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
manually in `verify`, `deploy`, or `wait-deploy` mode, or let guarded `main`
pushes to live-code/deploy paths trigger deployment. `wait-deploy` is the mode
to start before or during provider-console recovery: it verifies the deploy key
and host key, waits until public-key SSH starts working, then runs the same
checked deploy plus read-only verifier against the pinned GitHub SHA. Optional
repository variables: `VPS_HOST`, `VPS_USER`, `VPS_ED25519_FINGERPRINT`, and
`EXPECTED_TELEGRAM_CHAT_ID`.

If the VPS was rebuilt and SSH rejects the local key, add this public key back
to the VPS through the provider console before running the deploy script. The
recovery script also installs the GitHub Actions public deploy key shown below.

```text
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp hetzner
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKykZKBc1KapzJXdFORWMhjaNFC4zPeEZkOAbu32aTXX liquidity-migration-github-actions-20260519
```

On the VPS, the target file is normally `/root/.ssh/authorized_keys` for the
default `SSH_TARGET=root@204.168.202.167`.

The current VPS address `204.168.202.167` resolves to Hetzner Online's
Helsinki cloud network. If SSH is unavailable but the Hetzner Cloud web console
opens the installed OS as root, run the recovery deploy directly on the VPS:

```bash
scripts/print_vps_recovery_command.sh
scripts/print_vps_recovery_command.sh --recommended-only
scripts/print_vps_recovery_command.sh --rescue-only

EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/deploy_vps_live.sh
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/verify_vps_live.sh
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/wait_for_vps_recovery_and_deploy.sh
```

Prefer the generated pinned command from `scripts/print_vps_recovery_command.sh`
when possible; use `scripts/print_vps_recovery_command.sh --recommended-only`
when you want only the full installed-OS command to paste into the Hetzner Cloud
console, or `scripts/print_vps_recovery_command.sh --rescue-only` when you want
only the Hetzner Rescue SSH-key restore command. If the installed OS console is
unavailable, enable Hetzner Rescue for the server, boot into rescue root, run
the rescue command, reboot back to the installed OS, and let the existing
`wait-deploy` job or `scripts/wait_for_vps_recovery_and_deploy.sh` finish the
checked deploy. Do not paste a raw `main` branch `raw.githubusercontent.com`
recovery URL unless you intentionally want a moving-target deploy; the generated
command pins the exact commit and passes `EXPECTED_COMMIT` so the VPS refuses
stale or unexpected code.
`scripts/vps_restore_ssh_access.sh` only restores root public-key SSH access,
prints the restored authorized-key fingerprints, and exits, which is useful
when you want this local checkout or GitHub Actions to run the checked deploy
after access is fixed. `scripts/vps_rescue_restore_ssh_access.sh` is the
Hetzner Rescue fallback: run it as rescue root when the installed OS console is
unavailable, then reboot back to local disk and run the checked deploy from this
checkout. `scripts/wait_for_vps_recovery_and_deploy.sh` can be left running
locally while you perform the console or Rescue step; it waits until public-key
SSH works, then calls the checked deploy and read-only verifier with the pinned
commit. The GitHub `VPS Deploy` workflow's `wait-deploy` mode wraps the same
helper for cases where you want Actions to keep waiting instead of a local
terminal. The full console recovery restores the same SSH access, prints the
same fingerprints, clones or repairs `/opt/liquidity-migration`,
forces the configured remote URL, resets the deploy branch to `origin/main`,
builds the local venv if needed, installs missing Ubuntu deploy prerequisites,
writes an sshd recovery override for root public-key login, prints the effective
sshd root-login settings, validates the promoted TP26 and live TP21+FF6
constants, refreshes systemd, restarts both live services, and prints non-secret
service state. It prints `deploy-verify-ok` only after it has also verified the
active units are enabled, retired legacy units are inactive and disabled, the
demo service has the expected one-minute `promoted` settings, and the risk
service uses `ORDER_SUBMIT_MODE=ws_then_rest`. Set
`EXPECTED_COMMIT=<full sha>` before `bash` if you want the console deploy to
refuse anything except one pinned commit.
The console script also waits before checking active service state; override
with `SYSTEMD_SETTLE_SECONDS=<seconds>` if needed.

The generated full recovery command sets `CLEAN_DIRTY_CHECKOUT=1` by default.
If the existing `/opt/liquidity-migration` checkout is dirty, the script saves tracked
diffs, status, and a tarball/list of untracked non-ignored files under
`/root/liquidity-migration-deploy-backups` before running `git reset --hard` and
`git clean -fd`. Ignored live data such as `data/bybit-demo-event` is not
removed by that clean command. The generated strict command omits
`CLEAN_DIRTY_CHECKOUT=1` and refuses a dirty checkout.

Manual install or refresh on the VPS:

```bash
cp deploy/systemd/liquidity-migration-bybit-demo.service /etc/systemd/system/liquidity-migration-bybit-demo.service
cp deploy/systemd/liquidity-migration-bybit-risk.service /etc/systemd/system/liquidity-migration-bybit-risk.service
cp deploy/systemd/liquidity-migration-bybit-paper.service /etc/systemd/system/liquidity-migration-bybit-paper.service
systemctl daemon-reload
systemctl enable --now liquidity-migration-bybit-demo.service
systemctl enable --now liquidity-migration-bybit-risk.service
systemctl enable --now liquidity-migration-bybit-paper.service
systemctl restart liquidity-migration-bybit-demo.service
systemctl restart liquidity-migration-bybit-risk.service
systemctl restart liquidity-migration-bybit-paper.service
```

Required secrets live outside git in:

```text
/etc/liquidity-migration/bybit-demo.env
```

That environment file must define the Bybit demo API credentials and Telegram
credentials. Deploy/recovery backs it up and sets only `TELEGRAM_CHAT_ID` to the
expected target, preserving the API secrets and bot token. Telegram is enabled
for material alerts only: entries, exits, position reconciliation, or
position-report errors. Quiet no-trade cycles still write local reports but must
not notify. The services submit demo orders only.
The entry service uses `STRATEGY_PROFILE=promoted` at `close_location_min = 0.30`
with `MAX_ACTIVE_SYMBOLS=12` — the de-concentrated (12 concurrent positions)
`drop_all_4` package promoted 2026-05-30 (see
`docs/preregistration/drop-all-4-promotion.md`), on the conservative
`promoted_quality_squeeze` entry router. It runs match-the-backtest universe mode
(`UNIVERSE_RANK_END=0 / UNIVERSE_MAX_SYMBOLS=0`, the full perp universe) — the
`drop_all_4` package drops the rank-max band so the strategy trades from rank 31
upward, and submits demo (paper) orders only. This is a
demo-only paper forward test — not real-money validated. See `STATE.md` for
live status and `docs/event_demo_daemon.md` for the daemon runbook. The risk
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

Single-submitter safety: the active demo systemd unit pins
`Environment=STRATEGY_PROFILE=promoted`, and
`scripts/run_bybit_demo_event_engine.sh` refuses `SUBMIT_ORDERS=1` unless
`STRATEGY_PROFILE=promoted`. The `demo_relaxed`, no-crowding, sniper,
execution-only, and hedge candidates are shadow-only.

The retired `model050426-bybit-demo-signal.timer` / `.service` daily signal scan
must stay disabled; the active runner is the event-driven loop above.
