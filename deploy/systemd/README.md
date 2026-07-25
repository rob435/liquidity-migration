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
The liveness unit is deliberately independent of account-owner activation: a
stopped or failed owner is an observation to alert on, never a dependency to
start or wait for. Its unit has no ordering, requirement, binding, part-of,
requisite, uphold, or wants edge to the demo owner. Its only lifecycle edges are
`Wants`/`After` for network readiness; the matching timer triggers it and the
receipt path condition gates execution.

Strategy cycle `ts_ms` remains the causal scheduling input and is not an
operational completion timestamp. After cycle output, target capture, and the
decision outcome are durable, each producer atomically publishes a private
completion projection bound to systemd's current `INVOCATION_ID`. The watchdog
uses that receipt for age and current WS-store size, and binds it back to the
exact causal cycle row. A prior service generation cannot mask a hung restart.
Before the first receipt, only the current generation receives the same bounded
ten-minute grace as the cycle SLA. The account owner remains fail-closed during
that window. The observer suppresses only the exact nonterminal queue-head L2
subscription transition: the owner latches it to a maximum of 30 seconds, and
the resulting terminal timeout still pages. Missing, stale, reconciliation, and
capital health failures are never suppressed.

The watchdog also reopens the bound empirical demo-rule receipt and warns in
its final 24 hours before the strict 168-hour limit. Invalid, future-dated, or
expired evidence is critical. This is advance maintenance visibility only: the
watchdog never submits refresh orders, changes authority, or starts a deploy.

## Deployment lifecycle

GitHub dispatches share one repository-wide VPS concurrency group, independent
of the selected Git ref. The workflow exposes guarded `rollout` and `recover`
alongside staged `install`, `activate`, and read-only `verify`; the two guarded
modes require explicit authorization inputs and a push alone remains CI-only.
The remote deploy entrypoint holds a non-blocking host-local advisory lock for
the complete operation. Operational-authority issuance joins that boundary: its supported
operator route flocks the canonical maintenance inode and the legacy deploy and
reset inodes before opening the checkout or importing deployed Python, then the
helper and issuer revalidate those inherited descriptors. A concurrent
cooperating operation therefore fails closed before reading or changing
deployed state.

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
project unit, removes unknown surfaces, writes resolved sleeve toggles, and
archives retired authority files. Paper/demo tree preflights run concurrently;
both must pass before the two disjoint normalizers run concurrently, and each
normalizer keeps its independent final complete rescan. Install starts nothing.

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

The root-only issuer requires the exact nine services and three timers in this
manifest to be loaded and inactive. It checks once before source capture, once
after validating a private staging receipt, and once after linking that same
inode at the final name but before committing it. Every later phase also
reopens the machine, bound environments, inputs, roots, and checkout. Git runs
with a fixed executable and minimal environment, explicit Git directory and
work tree, disabled replacement objects, a private temporary index, and
bracketing `HEAD` checks. A separate descriptor-rooted raw-byte and Git-mode
comparison to every commit blob prevents clean filters, attributes, or
line-ending normalization from hiding tracked changes; gitlinks fail closed.
The supported `scripts/ops.sh` issuer supplies locks before Python import. Raw
module issuance refuses to run without that inherited handoff.

Receipt publication is bounded to 1 MiB and uses a root-owned, descriptor-bound
mode-`0400` staging inode. It links the same inode create-only at the final path,
removes the staging link, and keeps the single-link final file at mode `0400`
through both final systemd/source checks. Only `fchmod` to demo mode `0600` or
demo+paper mode `0640` commits authority. `ConditionPathExists` alone is not a
grant: the runtime wrapper rejects a precommit mode-`0400` file before `exec`.

A hard kill may leave a hidden mode-`0400` staging sibling or an invalid
mode-`0400` final file; a kill after mode commit may leave a valid receipt before
the command reports success. Preserve and verify either state before deliberate
cleanup. This is cooperative, point-in-time host authorization. Advisory locks
and systemd inspection do not detect an unmanaged manual process or constrain a
hostile privileged actor, and the receipt is not signed or WORM evidence.

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

`configs/operational.demo.json` is installed at the demo
`ACCOUNT_RISK_POLICY_FILE` during the stopped install, then mirrored into the
paper boundary. LONG, CONTINUOUS, the demo hedge, and the account owners parse
those same bytes. Sizing/leverage is deliberately absent from the strategy
unit `Environment=` lines, and authorization refuses a producer/owner leverage
or registered exposure-envelope mismatch before a receipt can be issued.

Repository sleeve defaults live in `deploy/sleeves.env`. The optional host file
may narrow an enabled sleeve to off. The resolved file is generated during
install and later bound into authority. Off stops new targets but does not
flatten existing state.

## Failure handling

Do not add drop-ins, change `ExecStart`, edit an authorization, or hand-start a
subset after failure. Preserve the receipt, journal, unit state, and logs; stop
unsafe writers; then diagnose from the exact installed commit.

The guarded reset is dry-run by default and refuses mutation until Bybit demo is
flat with no orders. It publishes the old epoch through an exclusively created,
descriptor-bound archive and sidecar, fsyncs them, and rechecks the exact
archive identity and digest before clearing account/inbox/capture payload.
Account, paper, and shared-demo filesystem trees are preflighted and normalized
through held descriptors; mount boundaries and unsafe aliases fail closed, and
no root recursive pathname ownership traversal is used.
With `--leave-stopped --receipt`, the creator validates a hidden mode-`0400`
staging inode, independently rechecks every managed unit inactive, and links the
same inode create-only at the final name. It repeats the unit and source checks
while that final name is still non-loadable mode `0400`; only then does mode
`0600` commit success. Post-commit work fsyncs and revalidates the artifact
identity rather than making another systemd claim. The receipt is point-in-time
epoch evidence only; it does not authorize activation.
See `docs/operations.md`.
