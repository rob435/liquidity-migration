# Operations

`scripts/ops.sh` is the supported operator router. Inspect its current help
before acting:

```bash
scripts/ops.sh help
```

No command enables mainnet. Demo actions still mutate an external demo account
and require explicit task scope. Unknown safety-critical state fails closed.

## Commands

| Command | Effect |
| --- | --- |
| `status` | Read-only verification of the exact authorized checkout and active topology. |
| `equity` | Repository-standard descriptive equity curves. |
| `reset` | Remote ledger/archive preview; mutation requires `--execute`. |
| `clock-offset --execute` | Capture a VPS/Bybit public-clock receipt. |
| `operational-authority` | Verify the current operational receipt. |
| `operational-authority --execute issue` | Create one new operational receipt. |
| `venue-accounting` | Read-only demo journal/venue accounting capture. |
| `test` | Run pytest with forwarded arguments. |
| `deploy --execute install` | Install an exact commit while every project unit is stopped. |
| `deploy --execute activate` | Start the fleet allowed by the current receipt and sleeve toggles. |

Environment overrides are `SSH_TARGET`, `REPO_DIR`, `PYTHON`, `BRANCH`,
`EXPECTED_COMMIT`, and the deploy script's documented SSH/repository settings.
`EXPECTED_COMMIT` must always be a full lowercase 40-character commit.

## Staged deployment

Deployment is deliberately split. Installation cannot authorize startup, and
authorization cannot install different code.

### 1. Install stopped

From a trusted checkout whose target commit exists on the selected remote branch:

```bash
COMMIT="$(git rev-parse HEAD)"
EXPECTED_COMMIT="$COMMIT" BRANCH="$(git branch --show-current)" \
  scripts/ops.sh deploy --execute install
```

The remote install requires a clean checkout and a quiescent
`liquidity-migration-*` fleet. It fetches and checks out the exact commit,
installs `requirements.lock` without dependency resolution, runs the focused
runtime tests and Ruff, installs the current unit manifest, disables every
project unit, removes unknown unit surfaces, writes resolved sleeve toggles,
and archives retired authority files. Success reports `units_started=0`.

Do not issue authority until the installed environment files and runtime inputs
are final. Installing another commit invalidates the sequence.

### 2. Issue operational authority

The receipt path is
`/etc/liquidity-migration/account-execution-operational-ready`. Creation is
exclusive and never overwrites an existing receipt. A demo-only receipt is
root-owned mode `0600`; an operational demo+paper receipt is root-owned,
mode `0640`, and readable only by the non-login paper runtime group.

```bash
EXPECTED_COMMIT="$COMMIT" scripts/ops.sh operational-authority --execute issue \
  --profile demo-operational \
  --expected-commit "$COMMIT" \
  --repo-root /opt/liquidity-migration \
  --authorization-reference "owner task: bounded demo operation" \
  --owner-acknowledgement AUTHORIZE_DEMO_PAPER_OPERATION_WITHOUT_RESEARCH_PROMOTION
```

Use `--profile operational` only when the explicitly uncalibrated paper
integration fleet is intended. The issuer binds the clean commit, machine,
profile, strict environment-file bytes, runtime-root identities, candidate
universe, verified rule coverage, risk inputs, sleeve toggles, and the explicit
paper execution-model scope. It refuses mainnet credentials, ambiguous
`REAL_MONEY`, bulk raw persistence, root aliases, or inconsistent inputs.

To replace a receipt, stop the fleet, preserve the old receipt with its attempt
artifacts, remove it deliberately, then issue a new one. Never edit or `touch`
the JSON.

### 3. Activate and verify

```bash
EXPECTED_COMMIT="$COMMIT" scripts/ops.sh deploy --execute activate
EXPECTED_COMMIT="$COMMIT" scripts/ops.sh status
```

Activation reopens the receipt and every bound input, requires the fleet to be
quiescent, probes demo-key order permission, and validates the commit-owned
hedge model prior before starting anything when the hedge timer is enabled. The
check covers schema, provenance, causal boundary, and estimator sufficiency;
the prior is intentionally not subject to a wall-clock freshness limit. It then
starts account owners before enabled producers, seeds residual momentum when
required, enables the allowed timers, and verifies the resulting topology.
Every guarded workload also runs `verify-runtime` immediately before `exec`.

`status` is read-only but still fails if the checkout, machine, receipt, inputs,
roots, effective unit surface, profile, or enabled topology differs. A failed
verification is not permission to hand-start a partial fleet.

## Profiles and sleeve toggles

- `demo-operational` permits the demo owner, demo LONG/CONTINUOUS producers,
  demo hedge/RMOM services, and demo-scoped liveness. Paper owner and producers
  must be disabled; `CONTINUOUS_PAPER_SLEEVE=off`.
- `operational` permits both owners, demo/paper producers allowed by sleeve
  toggles, hedge/RMOM services, and demo-paper liveness. Paper fills are marked
  `integration_only_uncalibrated` and support no performance claim.

The hedge service publishes only to demo. Paper CONTINUOUS provides
component-path integration coverage, not execution evidence or full
hedged-portfolio parity.

Bulk depth/liquidation collectors are not part of the surviving unit manifest,
and account raw-market persistence is authorization-locked off. Persistent demo
and paper workers have individual memory, soft-pressure, and swap ceilings sized
for the 4 GiB host. After first `operational` activation, observe at least several
complete strategy cycles with `systemctl show ... -p MemoryCurrent -p MemoryPeak`
and `free -h`. A clean start is not a capacity result. If paper hits a limit or
causes sustained pressure, stop its producers and owner, preserve their roots,
and issue a new demo-only authorization rather than weakening a limit live.

`deploy/sleeves.env` is the repository ceiling. The host override at
`/etc/liquidity-migration/sleeves.env` may only narrow `on` to `off`. Turning a
sleeve off stops new targets; it does not flatten its existing target or venue
position. Flatten through the account route and verify journal/venue agreement
before retirement.

## Reset

Preview is the default:

```bash
scripts/ops.sh reset --sleeves all
```

Mutation requires explicit execution and an already-flat demo account:

```bash
scripts/ops.sh reset --execute --leave-stopped --sleeves all \
  --label planned-reset --receipt /absolute/new/reset-receipt.json
```

The reset refuses live positions/orders, real-money configuration, mismatched
credential ownership, concurrent mutation, or unsafe writers. It archives and
fsyncs the old epoch before creating empty account/inbox/capture roots and
rebuildable projections. It never closes positions, cancels orders, or deletes
an audit archive. `--leave-stopped` is required before a new authorization.

## Evidence utilities

```bash
# Public clock evidence; writes on the VPS.
scripts/ops.sh clock-offset --execute --help

# Read-only stopped demo accounting capture.
scripts/ops.sh venue-accounting --help

# Descriptive curves, not promotion evidence.
scripts/ops.sh equity --help

# Tests.
scripts/ops.sh test -q
```

Venue accounting reconciles the canonical demo journal with Bybit executions,
fees, closed P&L, funding, positions, and open orders over the named interval.
It is accounting evidence for that interval, not strategy parity, alpha, or
deployment authority.

## Recovery rule

When activation or verification fails, preserve the receipt, journal, unit
state, and logs; stop unsafe writers; and diagnose from the exact installed
commit. Do not weaken gates, mutate bound files, or replace systemd `ExecStart`
with an ad hoc command.
