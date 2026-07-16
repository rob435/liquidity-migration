# VPS systemd topology

The installed topology separates strategy target production from account
execution. Every guarded service enters through
`scripts/run_authorized_runtime.sh`, which verifies the exact operational
receipt before replacing itself with the commit-owned workload.

This topology is demo/paper only. It never authorizes mainnet.

## Services

| Unit | Role |
| --- | --- |
| `liquidity-migration-account-execution.service` | Sole Bybit demo order, fill, position, funding, protection, journal, and health owner |
| `liquidity-migration-account-paper-execution.service` | Integration-only uncalibrated paper account owner |
| `liquidity-migration-bybit-long-{demo,paper}.service` | LONG target producers |
| `liquidity-migration-bybit-continuous-{demo,paper}.service` | CONTINUOUS component-target producers |
| `liquidity-migration-continuous-hedge.service` | Demo-only hedge target publisher |
| `liquidity-migration-continuous-rmom-refresh.service` | Residual-momentum refresh |
| `liquidity-migration-demo-liveness.service` | Account/strategy watchdog and notification surface |

The hedge, RMOM, and liveness services are invoked by their matching timers.
Target producers and auxiliary services have private API, mainnet,
`REAL_MONEY`, and unnecessary Telegram variables explicitly removed.

## Deployment lifecycle

### Install

```bash
COMMIT="$(git rev-parse HEAD)"
EXPECTED_COMMIT="$COMMIT" BRANCH="$(git branch --show-current)" \
  scripts/deploy_vps_live.sh install
```

Install requires a clean target checkout and a fully quiescent project fleet.
It checks out the exact remote commit, installs locked dependencies, runs the
focused validation, installs only the current unit manifest, disables every
project unit, removes unknown surfaces, writes resolved sleeve toggles, and
archives retired authority files. It starts nothing.

### Authorize

After the environment, roots, candidate universe, rules, risk policy, and sleeve
toggles are final, issue a create-only receipt:

```bash
scripts/ops.sh operational-authority --execute issue \
  --profile demo-operational \
  --expected-commit "$COMMIT" \
  --repo-root /opt/liquidity-migration \
  --authorization-reference "owner task: bounded demo operation" \
  --owner-acknowledgement AUTHORIZE_DEMO_PAPER_OPERATION_WITHOUT_RESEARCH_PROMOTION
```

Use `operational` only for an intended demo+paper fleet. The paper execution
model is commit-owned and explicitly `integration_only_uncalibrated`; receipt
details are in `docs/account_execution.md`.

### Activate and verify

```bash
EXPECTED_COMMIT="$COMMIT" scripts/deploy_vps_live.sh activate
EXPECTED_COMMIT="$COMMIT" scripts/deploy_vps_live.sh verify
```

Activation reopens all bound identities and requires the fleet still quiescent.
When the hedge timer is enabled, activation validates the commit-owned hedge
model prior before starting anything and verification rechecks it. This is an
integrity/sufficiency check, not a wall-clock freshness gate. It then starts owners
before enabled producers, seeds RMOM when needed, enables only allowed timers,
and verifies the topology. `verify` is read-only. Both refuse a dirty/wrong
checkout, changed inputs, unknown unit surfaces, unit overrides, mainnet
variables, or profile/topology disagreement.

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

The authorization parser requires root-owned mode-`0600` demo/private files,
root-owned mode-`0640` paper-route/sleeve files bound to the dedicated paper
runtime group, and paper-owned mode-`0600` input mirrors. It binds every byte.
Demo and paper account/inbox/capture roots must be absolute, real, pairwise
disjoint, and non-nested. The issuer proves candidate/rule coverage against the
demo sources once, then requires isolated paper candidate, rule, and risk
mirrors to be byte-exact copies.

Repository sleeve defaults live in `deploy/sleeves.env`. The optional host file
may narrow an enabled sleeve to off. The resolved file is generated during
install and later bound into authority. Off stops new targets but does not
flatten existing state.

## Failure handling

Do not add drop-ins, change `ExecStart`, edit an authorization, or hand-start a
subset after failure. Preserve the receipt, journal, unit state, and logs; stop
unsafe writers; then diagnose from the exact installed commit.

The guarded reset is dry-run by default and refuses mutation until Bybit demo is
flat with no orders. It archives and fsyncs the old epoch before creating new
account/inbox/capture roots. See `docs/operations.md`.
