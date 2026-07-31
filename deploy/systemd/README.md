# VPS systemd topology

The installed topology separates strategy target production from account
execution. Every guarded service enters through
`scripts/run_authorized_runtime.sh`, which replaces itself with the
commit-owned workload for that unit and entrypoint.

## Services

| Unit | Role |
| --- | --- |
| `liquidity-migration-account-execution.service` | Sole Bybit demo order, fill, position, funding, protection, journal, and health owner |
| `liquidity-migration-account-paper-execution.service` | Integration-only uncalibrated paper account owner |
| `liquidity-migration-bybit-long-{demo,paper}.service` | LONG target producers |
| `liquidity-migration-bybit-carry-{demo,paper}.service` | CARRY target producers |
| `liquidity-migration-bybit-continuous-{demo,paper}.service` | CONTINUOUS component-target producers |
| `liquidity-migration-paper-target-mirror.service` | Republishes the demo fleet's targets onto the paper route |
| `liquidity-migration-continuous-hedge.service` | Demo-only hedge target publisher |
| `liquidity-migration-continuous-rmom-refresh.service` | Residual-momentum refresh |
| `liquidity-migration-demo-liveness.service` | Account/strategy watchdog and notification surface |
| `liquidity-migration-account-execution-mainnet.service` | Bybit **mainnet** real-money order/fill/position/protection/journal owner |
| `liquidity-migration-bybit-{carry,long}-mainnet.service` | Real-money target producers, gated by `CARRY_MAINNET_SLEEVE` / `LONG_MAINNET_SLEEVE` |
| `liquidity-migration-mainnet-liveness.service` | Mainnet account/strategy watchdog and notification surface |

The mainnet units are installed by the manifest and started only when a mainnet
toggle is on: `activate` and `activate-mainnet` both route through
`start_mainnet_fleet`, which creates the mainnet state roots and requires the
arming preflight to pass before anything starts. With both toggles off — the
repository default, and a host override can only narrow — `verify` asserts each
one inactive and disabled; with either on it asserts the funded fleet up exactly
like the demo half. `stop-mainnet` takes it back down (publication only; exposure
is unchanged). See `docs/real_money.md`.

The hedge, RMOM, and liveness services are invoked by their matching timers.
Target producers and auxiliary services have private API, mainnet, `REAL_MONEY`,
and unnecessary Telegram variables explicitly removed.

Neither liveness unit has an ordering, requirement, binding, part-of, requisite,
uphold, or wants edge to the owner it watches — a stopped or failed owner is what
it alerts on. Their only lifecycle edges are `Wants`/`After` for network
readiness. The mainnet observer loads `bybit-mainnet.env` for the Telegram
credentials only and unsets both API-key pairs and `REAL_MONEY`.

Strategy cycle `ts_ms` is the causal scheduling input, not a completion
timestamp. Once cycle output, target capture, and the decision outcome are
durable, each producer atomically publishes a completion projection bound to
systemd's current `INVOCATION_ID`; the watchdog reads that for age and WS-store
size and binds it back to the causal cycle row, so a prior service generation
cannot mask a hung restart. Before the first projection, the current generation
gets the same bounded ten-minute grace as the cycle SLA. The observer suppresses
only the nonterminal queue-head L2 subscription transition (latched at 30s; the
terminal timeout still pages). Missing, stale, reconciliation, and capital
health failures are never suppressed.

The demo watchdog also reopens the bound demo-rule receipt and warns in its
final 24 hours before the strict 168-hour limit; invalid, future-dated, or
expired evidence is critical. That is visibility only — it starts no
maintenance. The mainnet scope skips it: that receipt is the demo realm's
empirical order probe, which has no mainnet counterpart.

## Deployment lifecycle

GitHub dispatches share one repository-wide VPS concurrency group, independent
of the selected Git ref. The workflow exposes guarded `rollout` alongside staged
`install`, `activate`, and read-only `verify`; a push alone remains CI-only.
`activate-mainnet` and `stop-mainnet` are deliberately absent from it — they run
from a shell only.
The remote deploy entrypoint holds a non-blocking host-local advisory lock
(the canonical maintenance inode plus the two legacy deploy/reset inodes) for
the whole operation, so a concurrent cooperating operation fails closed before
reading or changing deployed state.

Deploy Git commands do not inherit caller `GIT_*` variables, user/system Git
configuration, replacement objects, external index selection, or hooks. The
exact helper is extracted from the requested local commit with an explicit Git
directory/work tree and minimal environment; the remote checkout uses the same
isolation, a private temporary index for cleanliness, and `HEAD` checks before
and after comparison. This protects commit selection from ordinary
Git-environment drift, not from a compromised host.

### Install

```bash
COMMIT="$(git rev-parse HEAD)"
EXPECTED_COMMIT="$COMMIT" BRANCH="$(git branch --show-current)" \
  scripts/deploy_vps_live.sh install
```

Install requires a clean target checkout and a fully quiescent project fleet.
It checks out the exact remote commit, installs locked dependencies, runs the
focused validation, installs only the current unit manifest, disables every
project unit, removes unknown surfaces, and writes resolved sleeve toggles.
Paper/demo tree preflights run concurrently; both must pass before the two
disjoint normalizers run concurrently, and each normalizer keeps its independent
final rescan. Install starts nothing.

Configure the environment, roots, candidate universe, rules, risk policy, and
sleeve toggles on the stopped host before activating. Choose the `operational`
profile only for an intended demo+paper fleet; the paper execution model is
commit-owned and explicitly `integration_only_uncalibrated`
(`docs/architecture.md`, Realms and credentials).

### Activate and verify

```bash
EXPECTED_COMMIT="$COMMIT" scripts/deploy_vps_live.sh activate
EXPECTED_COMMIT="$COMMIT" scripts/deploy_vps_live.sh verify
```

Activation reopens all bound identities and requires the fleet still quiescent.
When the hedge timer is enabled, activation validates the commit-owned hedge
model prior before starting anything and verification rechecks it. This is an
integrity/sufficiency check, not a wall-clock freshness gate. It then starts owners
before enabled producers. A preserved RMOM table is reused only when the full
current-day gate passes and the prior refresh unit is not failed; otherwise the
bounded refresh path repairs it. Activation enables only allowed timers and
verifies the topology. `verify` is read-only. Both refuse a dirty/wrong
checkout, changed inputs, unknown unit surfaces, unit overrides, an exchange
credential or an enabled `REAL_MONEY` in the paper environment, or
profile/topology disagreement.

## Profiles

- `demo-operational`: demo owner and allowed demo producers; demo hedge/RMOM;
  demo-scoped liveness. Every paper unit is stopped/disabled and
  `CONTINUOUS_PAPER_SLEEVE=off`.
- `operational`: demo and paper owners; allowed demo/paper producers; hedge/RMOM;
  demo-paper liveness. Paper fills are integration-only and uncalibrated.

There is no paper hedge unit. Paper CONTINUOUS therefore shadows component
decisions and sizing, not the complete hedged portfolio.

Both require `ACCOUNT_RAW_MARKET_PERSISTENCE=0`. Live L2 readiness, exact
decision books, canonical journals, and account protection remain active.

## Environment files

- `/etc/liquidity-migration/bybit-demo.env`
- `/etc/liquidity-migration/account-execution.env`
- `/etc/liquidity-migration/account-paper-execution.env`
- `/etc/liquidity-migration/sleeves.resolved.env`
- `/etc/liquidity-migration/bybit-mainnet.env` and
  `account-execution-mainnet.env` — absent unless the owner installs them; no
  deploy mode writes either.

Deploy requires root-owned mode-`0600` demo/private files, root-owned
mode-`0640` paper-route/sleeve files bound to the dedicated paper runtime group,
and paper-owned mode-`0600` input mirrors. Demo and paper account/inbox/capture
roots must be absolute, real, pairwise disjoint, and non-nested. Candidate/rule
coverage is proved against the demo sources, and the paper candidate, rule, and
risk mirrors must be byte-exact copies.

`configs/operational.demo.json` is installed at the demo
`ACCOUNT_RISK_POLICY_FILE` during the stopped install, then mirrored into the
paper boundary. LONG, CONTINUOUS, the demo hedge, and the account owners parse
those same bytes. Sizing/leverage is deliberately absent from the strategy unit
`Environment=` lines.

Repository sleeve defaults live in `deploy/sleeves.env`. The optional host file
may narrow an enabled sleeve to off. The resolved file is generated during
install. Off stops new targets but does not flatten existing state.

## Failure handling

Do not add drop-ins, change `ExecStart`, or hand-start a subset after failure.
Preserve the journal, unit state, and logs; stop unsafe writers; then diagnose
from the exact installed commit.

The guarded reset is dry-run by default and refuses mutation until Bybit demo is
flat with no orders. It publishes the old epoch through an exclusively created,
descriptor-bound archive and sidecar, fsyncs them, and rechecks the exact
archive identity and digest before clearing account/inbox/capture payload.
Account, paper, and shared-demo filesystem trees are preflighted and normalized
through held descriptors; mount boundaries and unsafe aliases fail closed, and
no root recursive pathname ownership traversal is used. `--leave-stopped`
executes the reset and leaves every managed unit stopped.

See `docs/operations.md`.
