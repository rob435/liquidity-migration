# VPS systemd deployment

The deployable VPS units are:

- `liquidity-migration-bybit-long-demo.service` and
  `liquidity-migration-bybit-long-paper.service`: long-native v11a demo/paper pair.
- `liquidity-migration-bybit-risk.service`: shared fast exit-only risk runner for every
  configured ledger root; it has no sleeve toggle.
- `liquidity-migration-bybit-continuous-demo.service`: continuous-fade demo runner.
- `liquidity-migration-bybit-continuous-paper.service`: no-order continuous evidence
  collector.
- `liquidity-migration-liquidation-collector.service`: always-on live Bybit
  liquidation collector (`allLiquidation`; append-only JSONL, no order path).
  Enabled by the deploy.
- `liquidity-migration-depth-collector.service`: Bybit forward order-book depth
  collector (hourly band snapshots; public REST, append-only JSONL, no order path).
  Built but **NOT auto-enabled** — operator-gated (`systemctl enable --now
  liquidity-migration-depth-collector.service`); the deploy installs the unit and
  restarts it only if already enabled. Once enabled, deploy/verify/recovery fail
  loud unless the unit is both enabled and active, because Bybit depth history is
  unbuyable after a capture gap.
- Timers include the demo-liveness watchdog, combined-book report, continuous rmom
  refresh, and the five-minute continuous BTC+ETH target hedge (submit-armed; see below).

Which sleeve units actually run is governed by `deploy/sleeves.env` plus the
optional host override `/etc/liquidity-migration/sleeves.env`. The host override
can only narrow a repo-on sleeve to off; repo-side off is a hard ceiling. Deploy
writes the final values to `/etc/liquidity-migration/sleeves.resolved.env`, which
systemd units consume.
As of 2026-06-30 the live set is long demo/paper, continuous demo, and
continuous paper (`LONG_SLEEVE=on`, `CONTINUOUS_SLEEVE=on`,
`CONTINUOUS_PAPER_SLEEVE=on`). All are demo/paper only.

Install or refresh it on the VPS from a trusted local checkout:

```bash
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/deploy_vps_live.sh
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/verify_vps_live.sh
```

`EXPECTED_COMMIT` accepts a unique 7-40 character hexadecimal prefix or a full
commit ID; deploy and verify resolve it to the same full object before checking
the checkout. For private GitHub HTTPS remotes, a local deploy automatically
uses the authenticated `gh` credential when `GITHUB_TOKEN` is unset. Explicit
`GITHUB_TOKEN` remains supported and takes precedence. The credential travels
over SSH stdin for the fetch only and is not printed or persisted on the VPS.

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
When a continuous sleeve is enabled, deploy validates its residual-momentum
gate before restart. It runs the refresh oneshot only when a gate is
missing/stale or the gate build/validation code changed; healthy current gates
are retained. Validation still fails closed unless the explicit
`ALLOW_EMPTY_RMOM_GATE=1` first-boot override is set.
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
`git clean -fd`. Ignored live data is not removed by that clean command. The
generated strict command omits
`CLEAN_DIRTY_CHECKOUT=1` and refuses a dirty checkout.

Manual install or refresh on the VPS, if you are deliberately bypassing the checked
deploy scripts:

```bash
cp deploy/systemd/liquidity-migration-*.service /etc/systemd/system/
cp deploy/systemd/liquidity-migration-*.timer /etc/systemd/system/
systemctl daemon-reload
# Enable only the units whose toggle is on in deploy/sleeves.env, plus the always-on
# risk service and support timers. As of 2026-06-30:
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

## Safe demo/paper ledger archive and reset

Run the ledger reset command from `/opt/liquidity-migration`. Its default is a
read-only plan; `--execute` is mandatory for any service or file change.

```bash
scripts/reset_demo_paper_ledgers.sh
scripts/reset_demo_paper_ledgers.sh --sleeves continuous
scripts/reset_demo_paper_ledgers.sh --execute --sleeves all --label exit-overhaul
```

Execution stops every shared-account writer (including the risk service and
submit-armed hedge) plus maintenance timers, refuses `REAL_MONEY` or ambiguous
account flags, and takes a nonblocking process lock at
`/run/lock/liquidity-migration-ledger-reset.lock` so two execute runs cannot
overlap. Before stopping anything, it verifies that systemd's resolved
`EnvironmentFiles` for the risk, LONG demo, CONTINUOUS demo, and hedge units all
include the same resolved file selected by `--env-file`; this prevents proving one
account flat while quiescing writers for another. It then queries Bybit demo to
prove there are no positions or open orders. The lock remains held through normal
restart verification and failure-recovery restarts.

After the flat proof, the command writes and verifies a timestamped archive with
an audit manifest, persists an fsynced `.sha256` sidecar, and fsyncs the archive
directory before removing only allowlisted trade/order/cycle datasets and
the continuous risk, lifecycle, and `continuous_dynexit_shadow.jsonl` operational
ledgers in both demo and paper roots. Clearing them prevents pre-reset risk-health,
lifecycle, or shadow-exit evidence from contaminating the new forward window. The
continuous selection includes the hedge ledger; `all` also includes the shared
compatibility ledger. Initially inactive sleeve units remain inactive; initially
active daemons and timers are restarted and verified.

Configs, lock directories, residual-momentum signals, root-level market data,
reports, and `.cache` directories are preserved by default. `--include-reports`
and `--include-caches` are explicit opt-ins; the latter may require a slow market-
data bootstrap. The continuous `continuous_account_equity_state.json` high-water
state is snapshotted into the archive and retained live: wiping it would erase
account-level drawdown memory and make the first post-reset cycle report a false
zero drawdown. The command never flattens positions or cancels orders itself—do
that through the normal demo workflow, then rerun the reset. Archives default to
`data/_archive/ledger-reset-<UTC timestamp>[-label].tar.gz`.

Required secrets live outside git in:

```text
/etc/liquidity-migration/bybit-demo.env
```

That environment file must define the Bybit demo API credentials and Telegram
credentials. Deploy/recovery backs it up and sets only `TELEGRAM_CHAT_ID` to the
expected target, preserving the API secrets and bot token. The Bybit key must be
non-read-only and include `ContractTrade` `Order` and `Position` permissions.
Bybit can still list those granular permissions while reporting `readOnly=1`;
that key can read wallet/position state but fails later at
`set_leverage`/`place_order` with `ErrCode: 10005`. Checked deploy, verify,
console recovery, submit-armed wrappers, and the demo-liveness watchdog all
probe `get_api_key_information()` and fail/page on read-only or missing mutation
permissions. Recovery is to replace `/etc/liquidity-migration/bybit-demo.env`
with a non-read-only demo key, never by setting `REAL_MONEY`.
Telegram separates an hourly account digest from material position events.
The digest runs at `HH:05`, reads the Bybit position endpoint as the authority
for current exposure, and labels disagreeing local rows as stale bookkeeping.
Position alerts cover entries, exits (including take-profit reason and realised
P&L), reconciliation/safety faults, and first crossings of the configured 5%,
10%, 20%, and 40% loss bands. The shared `ws_risk` process is the sole producer
for successful opens/closes after venue confirmation; sleeve daemons retain
rate-limited order failures, so one fill cannot generate two success messages.
Server-side close reasons are taken from Bybit order history (`stopOrderType` /
`createType`); when that metadata is unavailable, a crossed ledger TP/SL is
labelled explicitly as a price inference rather than a confirmed order type.
Loss bands are restart-safe and deduplicated; an
unchanged band can remind at most once per 24 hours. The hourly hedge-manager
status comes from resolved `CONTINUOUS_HEDGE_TIMER` state, never from the
continuous-sleeve toggle. The liveness watchdog
reminds on an unchanged operational fault at most every six hours and sends a
resolution message when it clears. Quiet no-trade cycles still write local
reports but must not notify. The services submit demo orders only.
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

Single-submitter safety: the order-submitting units are the long demo sleeve
(`liquidity-migration-bybit-long-demo.service`, `SUBMIT_ORDERS=1`), the
continuous demo sleeve
(`liquidity-migration-bybit-continuous-demo.service`, `SUBMIT_ORDERS=1`,
`continuous_ensemble_v2`, inverse-vol component sizing with
`TARGET_VOL_PER_NAME=0.01`/`VOL_WEIGHT_CLAMP=2`, daily vol-target rebalance
disabled, `CTRL_BTC_RISK_70_90_35` BTC-risk entry sizing enabled,
`CONTINUOUS_SNIPER=0`, no
venue-side stop; demo/paper surface) and the five-minute BTC+ETH hedge timer
(`liquidity-migration-continuous-hedge.timer`) — the hedge unit ships
**`SUBMIT_HEDGE=1` + `CONFIRM_DEMO_ORDERS=1` (operator-armed 2026-06-10)**, so
it SUBMITS demo orders; runtime guards + staleness gates still apply. The
continuous paper shadow is unconditionally `SUBMIT_ORDERS=0`/`PAPER_MODE=1`
(verified fail-loud on every deploy). Everything is demo-account-only.
