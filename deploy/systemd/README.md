# VPS systemd deployment

This directory defines one execution topology for demo and deterministic paper:
strategy sleeves publish absolute targets, while one owner per account is the
only process allowed to mutate or reconcile account state.

This is not real-money authorization. The current cutover evidence and open
acceptance conditions are recorded in `docs/account_execution_cutover.md`.

## Execution ownership

- `liquidity-migration-account-execution.service` owns Bybit demo credentials,
  orders, fills, positions, native protection, position and funding
  reconciliation, the canonical demo account journal, desired-vs-executed
  convergence, and operational Telegram messages.
- `liquidity-migration-account-paper-execution.service` owns the deterministic
  paper account journal and consumes its separate target inbox and L2 capture;
  its unit explicitly removes every demo/mainnet private credential,
  `REAL_MONEY`, and Telegram credential from the inherited environment.
- LONG and CONTINUOUS demo/paper services are target producers. Their unit
  environments remove Bybit private keys, `REAL_MONEY`, and Telegram
  credentials even though they retain the shared public configuration file.
- The target-only hedge and local RMOM refresh apply the same explicit removal;
  the demo account owner is the only fresh-epoch unit that receives demo
  private credentials.
- `liquidity-migration-continuous-hedge.service` names its `demo|paper` route
  explicitly; `HEDGE_ACTION=execute` publishes to that owner inbox, while the
  launcher defaults to dry-run and never places venue orders itself.
- `liquidity-migration-demo-liveness.service` reads canonical account journals
  and captures. It does not load the private credential environment.

The retired `liquidity-migration-bybit-risk.service`, combined-book report
service/timer, and `run_bybit_demo_ws_risk_engine.sh` must not be reintroduced.
Historical archives and incident receipts remain evidence; they are not live
runtime inputs.

Accepted targets are not reported as positions until canonical fills converge.
Terminal rejects/cancels are recovered by the owner with bounded deterministic
retries, and overdue/exhausted convergence blocks the owner health consumed by
producers and liveness. Telegram emits one hourly account summary plus confirmed
lifecycle/loss-threshold events; a venue/local mismatch is shown explicitly and
untrusted exposure/estimated-uPnL alerts are suppressed. Valuation is labelled
as captured L2 midpoint—not Bybit `markPrice` or venue uPnL—and missing fresh
midpoints render valuation unavailable rather than substituting entry price.
Demo exits retain their unrelated-risk exemption but still require fresh
same-symbol venue truth.

The inbox assigns a locked durable arrival sequence, coalesces collective later
component replacements, and carries a per-component creation revision so a
delayed pre-flat entry cannot reopen after a newer safety transition. Demo and
paper account, inbox and capture roots must be absolute and pairwise disjoint;
deploy, recovery and verification reject equal, nested or canonical aliases.

The liquidation collector remains always on. The depth collector is still an
explicit operator opt-in because a missed forward capture cannot be bought back.

## Safe staged installation

The account-owner cutover can install its current software and units before the
deploy gate exists:

```bash
CANDIDATE_BRANCH="$(git branch --show-current)"
test -n "$CANDIDATE_BRANCH"
INSTALL_PREFLIGHT_ONLY=1 \
  BRANCH="$CANDIDATE_BRANCH" \
  EXPECTED_COMMIT="$(git rev-parse HEAD)" \
  scripts/deploy_vps_live.sh
```

The provider-console recovery script accepts the same
`INSTALL_PREFLIGHT_ONLY=1` setting, and GitHub Actions exposes a manual
`install-preflight` mode. The phase checks out the exact commit, installs the
current scripts/config and systemd manifest, and disables/removes unknown
historical `liquidity-migration-*` units and drop-ins. It deliberately does not
read or modify the capture-enable marker, pre-cutover runtime marker, or
deploy-ready receipt, load the Bybit or owner route
environments, or enable/start/restart any current `liquidity-migration` unit.
Already-running current units are left running, so enter the maintenance window
and stop the installed fleet before invoking this phase; otherwise the checkout
would change scripts/config beneath active processes.

A successful `install-preflight-ok` is an installation receipt only. It does not
authorize startup and does not make the open evidence in
`docs/account_execution_cutover.md` pass.

## Capture authority versus deploy authorization

Owners and producers fail closed unless the bounded evidence-collection marker
exists; only the safe staged installation above is exempt:

```text
/etc/liquidity-migration/account-execution-capture-enabled
```

This marker enables the guarded demo/paper capture topology. It is first issued
for tape collection and, if the cutover is later authorized, persists unchanged
for the deployed runtime. It is not evidence of parity, calibration, P&L
agreement, or deployment readiness. Initial full deployment additionally
requires a mode-`0600` JSON authorization receipt at:

```text
/etc/liquidity-migration/account-execution-deploy-ready
```

The one-time deployment verifies the capture marker and deploy receipt before
checkout and refuses the retired ambiguous `account-execution-ready` filename.
Routine same-commit verification/recovery instead reopens the persistent
activation latch and exact bound artifacts; expiry cannot grant a new deploy or
invalidate an already activated filesystem epoch. The deploy receipt is
self-hashed, expires within 24 hours, and binds the reviewed evidence to this
host and exact clean staged commit. Do not `touch` this path. Issue it through
`scripts/ops.sh cutover-authority` only after the gates in
`docs/account_execution_cutover.md` actually pass. This integrity check does
not turn operator judgment into a signature or an automatic evidence verdict.

The owner-approved demo/paper operational path is intentionally separate from
that research-promotion deploy receipt. It uses the create-only mode-`0600`
receipt:

```text
/etc/liquidity-migration/account-execution-operational-ready
```

`scripts/ops.sh operational-authority` verifies it remotely;
`operational-authority --execute issue` is the required mutation handshake.
The temporary `calibration` profile authorizes only the demo account owner and
requires raw persistence `1`. The permanent `operational` profile requires a
passing paper twin receipt, raw persistence `0` for both owners, and binds all
nine guarded units. Both profiles bind the exact clean commit, machine,
environment files, immutable config inputs, and runtime-root identities.
Natural/fresh overrides, simultaneous operational and research-deploy
receipts, mainnet variables, or changed inputs fail closed.

Bulk raw retention and execution safety are distinct. With persistence `0`,
the owners still subscribe to and reconstruct live L2, publish a bounded atomic
readiness sidecar, and durably capture each exact decision book. They simply do
not subscribe to public trades or append every raw L2 frame. With persistence
`1`, raw L2/public-trade segments are also written for V8/natural evidence.
Existing capture bytes are not deleted by either mode.

The demo owner route is configured in
`/etc/liquidity-migration/account-execution.env`:

```bash
ACCOUNT_EXECUTION_KERNEL_REQUIRED=1
ACCOUNT_EXECUTION_ROOT=/opt/liquidity-migration/data/bybit-account-execution
ACCOUNT_INTENT_INBOX_ROOT=/opt/liquidity-migration/data/bybit-account-intents
ACCOUNT_CAPTURE_ROOT=/opt/liquidity-migration/data/bybit-account-market-capture
ACCOUNT_RAW_MARKET_PERSISTENCE=0
ACCOUNT_SYMBOLS_FILE=/etc/liquidity-migration/account-execution/symbols.txt
ACCOUNT_DEMO_RULES_FILE=/etc/liquidity-migration/account-execution/demo-rules.json
ACCOUNT_RISK_POLICY_FILE=/etc/liquidity-migration/account-execution/risk-policy.json
DISASTER_STOP_FRACTION=<explicit-owner-choice>
```

The paper owner has a distinct route in
`/etc/liquidity-migration/account-paper-execution.env`:

```bash
ACCOUNT_PAPER_KERNEL_REQUIRED=1
ACCOUNT_EXECUTION_ROOT=/opt/liquidity-migration/data/bybit-account-paper
ACCOUNT_INTENT_INBOX_ROOT=/opt/liquidity-migration/data/bybit-account-paper-intents
ACCOUNT_PAPER_CAPTURE_ROOT=/opt/liquidity-migration/data/bybit-account-paper-market-capture
ACCOUNT_RAW_MARKET_PERSISTENCE=0
ACCOUNT_SYMBOLS_FILE=/etc/liquidity-migration/account-paper-execution/symbols.txt
ACCOUNT_DEMO_RULES_FILE=/etc/liquidity-migration/account-execution/demo-rules.json
ACCOUNT_RISK_POLICY_FILE=/etc/liquidity-migration/account-paper-execution/risk-policy.json
ACCOUNT_TWIN_CALIBRATION_FILE=/etc/liquidity-migration/account-paper-execution/execution-twin-calibration.json
ACCOUNT_TWIN_LATENCY_QUANTILE=p50
ACCOUNT_TWIN_SLIPPAGE_QUANTILE=p50
PAPER_EQUITY_USDT=10000
```

Bybit demo and Telegram secrets remain in
`/etc/liquidity-migration/bybit-demo.env`. The demo account owner loads both.
The liveness watchdog loads Telegram only and explicitly removes every Bybit
credential plus `REAL_MONEY` before execution. Target producers explicitly
unset private API and Telegram variables, and the demo owner explicitly removes
the unused mainnet credential names. All three private EnvironmentFiles above
must be current-user-owned, single-link regular files with exact mode `0600`
and strict `KEY=VALUE` syntax. Checked deploy, verification, and recovery read
only an allowlist through the stable data parser; they never source these files
as shell programs. Shell expansion, escape syntax, duplicate keys, ambiguous
whitespace, and unknown `REAL_MONEY` spellings are refused. `REAL_MONEY` is
refused throughout this workflow.

## Checked deploy and verification

From a trusted checkout:

```bash
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/deploy_vps_live.sh
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/verify_vps_live.sh
```

The deploy refuses missing owner routes/capture authority, a missing, stale,
altered, cross-host, or wrong-commit deploy authorization, a dirty remote checkout,
unexpected commit, real-money configuration, failed demo-key permission probe,
invalid strategy constants, stale required rmom gates, unknown legacy units, or
an owner/producer that does not become active. It starts both account owners
before enabled target producers. Verification is read-only and checks the same
topology and unit environment latches.

That full deploy route remains the natural/research-promotion path. For the
owner-approved operational path, first use `INSTALL_PREFLIGHT_ONLY=1` to install
and verify the exact guarded unit surface without starting anything. Then use
the operational authorization profiles above and start owners before any
producer. Do not invent an `account-execution-deploy-ready` receipt merely to
enter operational mode.

Before the evidence window, explicitly bind the staged clean commit and machine
with `authorized-deploy-epoch prepare-evidence-runtime`. That command creates
the owner-only mode-`0600`
`/etc/liquidity-migration/account-execution-pre-cutover-ready` marker; absence
is a hard failure, not an implicit legacy fallback. The authorized cutover later
consumes that marker and writes the persistent fresh-epoch latch next to
`/etc/liquidity-migration/fresh-deploy`. Before it materializes any environment
file, it also publishes the irreversible mode-`0600`
`account-execution-fresh-epoch-activation-started.json` history marker. A
failed or partial activation retains that marker; deleting the environment,
latch, or authorization cannot make `prepare-evidence-runtime` legal again.

Every one of the nine account-owner, producer, hedge, refresh, and liveness
units enters through `run_authorized_fresh_runtime.sh`. Systemd may select only
the checked `main` or owner `readiness` entrypoint; the wrapper owns the exact
command and argv and rejects caller-supplied commands. Installation and verify
also reject any guarded-unit drop-in, alternate fragment, or effective
`ExecStart`/`ExecStartPost` override. The wrapper checks the
exact environment inherited by that process and immediately `exec`s the
workload, so there is no second systemd environment load between verification
and execution. Before activation it requires the commit/machine-bound evidence
marker. After activation it performs bounded checks of the clean checkout,
machine, authorization, fresh manifest, materialization receipt, every fragment,
and the unit's required values. Any partial or deleted activation state, a
lingering pre-cutover marker, or a changed dependency blocks startup instead of
falling back to a legacy root. The latch preserves the original authorization
identity after its bounded issuance window; it does not grant new deployment
authority.

Sleeve enablement comes from `deploy/sleeves.env`, narrowed only by
`/etc/liquidity-migration/sleeves.env`. The resolved values are written to
`/etc/liquidity-migration/sleeves.resolved.env`. Turning a sleeve off stops new
targets; it does not disable either account owner.

GitHub Actions uses `.github/workflows/vps-deploy.yml`. Console and SSH recovery
must use the checked scripts rather than hand-starting a partial fleet:

```bash
scripts/print_vps_recovery_command.sh --recommended-only
EXPECTED_COMMIT="$(git rev-parse HEAD)" scripts/vps_console_recover_and_deploy.sh
```

Full recovery is activated-latch, same-commit only: it does not fetch or upgrade.
If explicitly asked to clean a dirty checkout, it archives the dirty bytes and
resets to the already-proved full commit before sourcing any checkout helper or
Python verifier. The console printer embeds scripts from that exact trusted
local Git object; it does not bootstrap this private repository through an
anonymous content URL. Initial deploy and recovery install only exact
`requirements.lock` versions with dependency resolution disabled; neither runs
an editable install nor upgrades pip.
Preactivation must use the checked initial deploy, and any partial activation
state is preserved as an incident and fails closed.

## Evidence-window startup and inspection

After staged installation, the flat reset, fresh demo rules, and explicit
capture authorization, set the demo owner environment to
`ACCOUNT_RAW_MARKET_PERSISTENCE=1` and issue the calibration-only operational
authorization. Start the demo account owner alone. Verify fresh bound owner
health before running V8; ordinary producers stay stopped. Calibrate from the
resulting demo tape before starting the paper owner. This sequence is an
evidence window, not a full deploy. Useful checks are:

```bash
install -m 0600 /dev/null /etc/liquidity-migration/account-execution-capture-enabled
scripts/ops.sh operational-authority --execute issue \
  --profile calibration \
  --expected-commit "$(git -C /opt/liquidity-migration rev-parse HEAD)" \
  --repo-root /opt/liquidity-migration \
  --authorization-reference "owner task: bounded V8 bootstrap" \
  --owner-acknowledgement AUTHORIZE_DEMO_PAPER_OPERATION_WITHOUT_RESEARCH_PROMOTION
systemctl start liquidity-migration-account-execution.service
systemctl status liquidity-migration-account-execution.service
python3 -c 'from liquidity_migration.account_owner_health import require_recent_account_owner_health; require_recent_account_owner_health("/opt/liquidity-migration/data/bybit-account-execution", environment="demo", expected_account_id="bybit-demo-unified", max_age_ns=30_000_000_000)'
# Only the registered V8 driver may publish during calibration.
systemctl status liquidity-migration-account-paper-execution.service
systemctl status liquidity-migration-demo-liveness.timer
systemctl list-units 'liquidity-migration-*'
journalctl -u liquidity-migration-account-execution.service -n 200 --no-pager
```

Do not start the paper owner until its passing calibration receipt is installed.
After V8, stop the demo owner, preserve the calibration authorization, install
the verified twin receipt, set raw persistence `0` in both owner environments,
and issue a new `operational` receipt at the well-known path before starting
either owner. The create-only writer will not overwrite the calibration
receipt; archive it into the private attempt evidence directory first.
Do not issue `account-execution-deploy-ready` during live evidence collection.
After targets and the venue are flat, stop producers, let both owners write
final fresh health, and stop the owners. The owner-serialized read-only final
capture is:

```bash
scripts/ops.sh venue-accounting \
  --account-root /opt/liquidity-migration/data/bybit-account-execution \
  --account-id bybit-demo-unified \
  --start-time-ms FRESH_ACCOUNT_EPOCH_START_MS \
  --output /etc/liquidity-migration/account-execution/venue-accounting.json
```

It machine-checks exact target/order/fill lineage, TRADE and closed-PnL totals,
funding settlements, and pre/post local/venue flatness. The default sample
requires two trades, one closed-PnL row, and one funding row over an epoch no
longer than seven days. Prepare the open assessment beforehand and issue the
authorization within five minutes of the stopped owners' final health files;
the venue-accounting and flatness roles point directly to this receipt.

## Flat-account archive and reset

`scripts/reset_demo_paper_ledgers.sh` is dry-run by default. Execute only after
reviewing the plan. On a pre-cutover host, stop the installed fleet and complete
the safe staged installation first; the reset verifies the new owner units and
their route environments before mutating any ledger:

```bash
scripts/reset_demo_paper_ledgers.sh --sleeves all
scripts/reset_demo_paper_ledgers.sh --execute --sleeves all --label account-cutover
```

Execution acquires a nonblocking process lock, verifies the selected credential
and route environments belong to the expected owners without later overrides,
stops all target producers and both account owners, and then proves Bybit demo
has zero positions and open orders. It
then writes and fsyncs a timestamped archive plus SHA-256 sidecar before any
removal.

The reset archives and recreates the configured demo/paper account roots,
intent inboxes, and capture roots as empty directories. Strategy journals stay
live while their compatibility trade/order projections are rebuilt. Demo rows
receive the proven venue-flat boundary; prior active paper rows are explicitly
marked as belonging to an archived deterministic epoch, never as venue-verified
flat. The retired
hedge-ledger and shared-risk compatibility roots are archived and removed, not
recreated. Previously active owners restart before previously active target
producers and all restarted units are verified.

The reset never closes positions, cancels orders, enables real money, or deletes
an audit archive. Reports and strategy market-data caches remain opt-in reset
targets through `--include-reports` and `--include-caches`.
