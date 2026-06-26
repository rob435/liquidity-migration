# VPS systemd deployment

The deployable VPS units are:

- (The daily-short sleeve's units — `bybit-demo` / `bybit-paper` — and the
  `demo-health` entry watchdog were ERASED with the sleeve, operator order
  2026-06-11; the deploy removes them from a live host.)
- `liquidity-migration-bybit-long-demo.service` and
  `liquidity-migration-bybit-long-paper.service`: long-native v11a demo/paper pair.
- `liquidity-migration-bybit-risk.service`: shared fast exit-only risk runner for every
  configured ledger root; it has no sleeve toggle.
- `liquidity-migration-bybit-continuous-demo.service`: continuous-fade demo runner.
- `liquidity-migration-bybit-continuous-paper.service`: no-order continuous evidence
  collector.
- `liquidity-migration-liquidation-collector.service`: always-on live liquidation
  collectors (Bybit `allLiquidation` + Binance `forceOrder`; append-only JSONL, no
  order path). Enabled by the deploy.
- `liquidity-migration-depth-collector.service`: Bybit forward order-book depth
  collector (hourly band snapshots; public REST, append-only JSONL, no order path).
  Built but **NOT auto-enabled** — operator-gated (`systemctl enable --now
  liquidity-migration-depth-collector.service`); the deploy installs the unit and
  restarts it only if already enabled. Once enabled, deploy/verify/recovery fail
  loud unless the unit is both enabled and active, because Bybit depth history is
  unbuyable after a capture gap.
- Timers include the demo-liveness watchdog, combined-book report, continuous rmom
  refresh, and the daily continuous BTC+ETH hedge (submit-armed; see below).
  (The demo-health timer was erased with the short sleeve.)

Which sleeve units actually run is governed by `deploy/sleeves.env` plus the
optional host override `/etc/liquidity-migration/sleeves.env`. The host override
can only narrow a repo-on sleeve to off; repo-side off is a hard ceiling. Deploy
writes the final values to `/etc/liquidity-migration/sleeves.resolved.env`, which
systemd units consume.
As of 2026-06-18 the live set is long demo/paper, continuous demo, and
continuous paper (`LONG_SLEEVE=on`, `CONTINUOUS_SLEEVE=on`,
`CONTINUOUS_PAPER_SLEEVE=on`). All are demo/paper only.

Install or refresh it on the VPS from a trusted local checkout:

```bash
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/deploy_vps_live.sh
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/verify_vps_live.sh
```

The script refuses a dirty VPS checkout, forces the configured remote URL,
resets the deploy branch to `origin/main`, runs focused runtime tests, checks
the promoted strategy constants, backs up `/etc/liquidity-migration/bybit-demo.env`,
enforces the expected Telegram chat ID, syncs all
`deploy/systemd/liquidity-migration-*` service/timer files, applies the sleeve
kill-switches, restarts the shared risk service and only the enabled sleeve units,
and prints active systemd state plus non-secret entry-profile settings. The verify
script is read-only and checks the same commit, strategy constants, Telegram chat ID,
systemd unit settings, and sleeve active/enabled state without
pulling or restarting.
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
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICWcgpE3GLy65yWFuh5RAH5CEgyLqRPAGvROXGwAxmVv liquidity-migration-github-actions-20260609
```

On the VPS, the target file is normally `/root/.ssh/authorized_keys` for the
default `SSH_TARGET=root@116.202.15.128`.

The current VPS address is `116.202.15.128`. If SSH is unavailable but the
Hetzner Cloud web console opens the installed OS as root, run the recovery deploy
directly on the VPS:

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
sshd root-login settings, validates the promoted strategy constants, refreshes
systemd, applies the same sleeve kill-switches as the normal deploy, restarts the
shared risk service and enabled sleeves, and prints non-secret service state. It
prints `deploy-verify-ok` only after it has also verified enabled sleeves are
active/enabled, disabled sleeves are down, the demo service has the expected
one-minute `promoted` settings, and the risk service uses
`ORDER_SUBMIT_MODE=ws_then_rest`. Set
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

Manual install or refresh on the VPS, if you are deliberately bypassing the checked
deploy scripts:

```bash
cp deploy/systemd/liquidity-migration-*.service /etc/systemd/system/
cp deploy/systemd/liquidity-migration-*.timer /etc/systemd/system/
systemctl daemon-reload
# Enable only the units whose toggle is on in deploy/sleeves.env, plus the always-on
# risk service and support timers. As of 2026-06-18:
systemctl enable --now liquidity-migration-bybit-risk.service
systemctl enable --now liquidity-migration-bybit-long-demo.service
systemctl enable --now liquidity-migration-bybit-long-paper.service
systemctl enable --now liquidity-migration-bybit-continuous-demo.service
systemctl enable --now liquidity-migration-bybit-continuous-paper.service
systemctl enable --now liquidity-migration-liquidation-collector.service
systemctl enable --now liquidity-migration-demo-liveness.timer
systemctl enable --now liquidity-migration-combined-book-report.timer
systemctl enable --now liquidity-migration-continuous-rmom-refresh.timer
systemctl enable --now liquidity-migration-continuous-hedge.timer
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
The risk
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

Single-submitter safety (post the 2026-06-11 short-sleeve erasure): the
order-submitting units are the long demo sleeve
(`liquidity-migration-bybit-long-demo.service`, `SUBMIT_ORDERS=1`), the
continuous demo sleeve
(`liquidity-migration-bybit-continuous-demo.service`, `SUBMIT_ORDERS=1`,
`continuous_ensemble_v2`, inverse-vol component sizing with
`TARGET_VOL_PER_NAME=0.01`/`VOL_WEIGHT_CLAMP=2`, daily vol-target rebalance
disabled, `CTRL_BTC_RISK_70_90_35` BTC-risk entry sizing enabled, no
venue-side stop; demo/paper surface) and the daily BTC+ETH hedge timer
(`liquidity-migration-continuous-hedge.timer`) — the hedge unit ships
**`SUBMIT_HEDGE=1` + `CONFIRM_DEMO_ORDERS=1` (operator-armed 2026-06-10)**, so
it SUBMITS demo orders; runtime guards + staleness gates still apply. The
continuous paper shadow is unconditionally `SUBMIT_ORDERS=0`/`PAPER_MODE=1`
(verified fail-loud on every deploy). Everything is demo-account-only.
