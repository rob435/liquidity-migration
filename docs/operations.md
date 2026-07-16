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

Deploy, activation, verification, operational-authority issuance, and
destructive ledger reset share the host-wide
`/run/liquidity-migration/maintenance.lock`. The current scripts also nest the
retired deploy/reset lock leaves so the first rolling upgrade cannot overlap an
older installed maintenance process. A collision fails closed before the
selected operation reads or mutates deployed state. These are persistent,
root-owned mode-`0600` inodes: a descriptor-safe helper rejects symlink,
hardlink, mount-leaf, and replacement attacks around the inherited descriptors.
Deploy transmits that helper from the exact requested Git commit rather than
executing a helper from the not-yet-verified remote checkout.

The supported operational-authority route opens and non-blockingly flocks the
canonical lock and both legacy leaves before it changes into the checkout or
imports deployed Python. It then uses the installed helper to revalidate the
three inherited inode, owner, mode, parent, and Linux mount identities; the
issuer revalidates the same inherited descriptors again and retains them until
publication returns. The shell cannot add `O_NOFOLLOW` to its initial
redirection, so the root-controlled namespace and immediate post-open identity
checks remain part of the boundary. These cooperative locks do not defend
against a hostile privileged process that can replace entries inside that
namespace or ignore the locks entirely.
The raw Python module `issue` command fails unless this exact pre-import
descriptor handoff is present; acquiring a fallback lock after module import
would be too late. The module's read-only `verify` commands do not require that
handoff.

Local helper extraction and remote deploy Git reads run with a minimal
environment, explicit Git directory and work tree, system configuration and
replacement objects disabled, and no inherited index. Remote cleanliness checks
populate a private temporary index, disable hooks and fsmonitor, compare tracked
and non-ignored untracked state to the requested commit, and recheck `HEAD`.
This isolates the exact-commit decision from caller `GIT_*` variables and
ordinary user/system Git configuration. Git still reads repository-local config
and exclude rules, and ignored paths are deliberately outside the cleanliness
claim; the check is not an attestation of the host or remote repository.

The quiescent boundary also protects filesystem-lock protocol migrations.
Before installing a commit that changes locking, stop any manual or ad-hoc
process using the checkout or its data roots; systemd unit discovery cannot see
those clients. Never mix the retired create/unlink lock implementation with the
persistent-flock implementation against the same root.

While the fleet is stopped, install validates complete paper and shared-demo
runtime trees through directory descriptors before changing either batch. It
rejects symlinks, multiply linked files, special files, and root, nested, or
regular-file mount boundaries, including same-device Linux bind mounts. Missing
direct-child roots and cache/lock directories are then created relative to the
held data-root descriptor; owner and mode changes never use recursive pathname
`chown` or `find` traversal.

Do not issue authority until the installed environment files and runtime inputs
are final. Installing another commit invalidates the sequence.

### 2. Issue operational authority

The receipt path is
`/etc/liquidity-migration/account-execution-operational-ready`. Creation is
exclusive and never overwrites an existing receipt. A demo-only receipt is
root-owned mode `0600`; an operational demo+paper receipt is root-owned,
mode `0640`, and readable only by the non-login paper runtime group. Receipt
publication and loading are bounded to 1 MiB, the output parent must be
issuer-owned and not writable by group or other, and the final inode must have
exactly one link.

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

The production CLI is root-only. While the three maintenance locks remain held,
it invokes trusted `/usr/bin/systemctl` with a fixed minimal environment and
requires the exact managed manifest of nine services and three timers to
have `LoadState=loaded` and `ActiveState=inactive`. This check runs initially,
again after validating the private staging inode before the final name exists,
and a third time after the same inode is linked at the final name but before it
can be loaded as authority. Missing, failed, activating, deactivating, or active
units all fail closed.

Every source recheck uses trusted `/usr/bin/git`, an explicit Git directory and
work tree, disabled system/global configuration and replacement objects, a
private temporary index, and `HEAD` checks around the comparison. This detects
tracked changes hidden by the checkout's ordinary index flags. A separate
descriptor-rooted walk hashes every raw tracked file or symlink target against
the exact commit blob and compares its Git mode, so repository filters,
attributes, and line-ending normalization cannot make different bytes appear
clean; unsupported gitlinks fail closed. Non-ignored untracked files are also
rejected. The issuer reopens and exactly compares the machine, environment
files, inputs, roots, and Git state at both precommit phases; a stable first
capture alone is not enough.

Publication writes and fsyncs a randomized sibling at mode `0400`, validates it
through the held descriptor, links that exact inode create-only at the final
name, removes the staging link, and repeats validation while the final inode is
still mode `0400` and single-linked. Only descriptor `fchmod` to `0600` or
`0640` commits authority. No source or systemd truth callback runs after that
transition. A systemd `ConditionPathExists` may see the precommit final name,
but `verify-runtime` requires the committed mode and therefore refuses it.

Normal precommit failures remove only the descriptor-identified inode created
by that attempt and preserve a foreign collision. A hard kill can leave either
a hidden mode-`0400` staging sibling or a mode-`0400` final file; neither is a
valid authorization, though a final-name orphan blocks a later create-only
attempt until an operator preserves, diagnoses, and deliberately removes it. A
kill after the permission transition can leave a valid receipt before the
issuing command prints success. Reopen and verify the artifact rather than
assuming command output is the commit point.

These guarantees are local and point-in-time. The systemd manifest does not
discover an ad-hoc process, and the advisory maintenance locks constrain only
cooperating clients. Stop manual processes that use the checkout, account
roots, or venue credentials before issuance. The receipt is not a signature,
WORM storage, remote host attestation, ongoing service-state monitor, or
mainnet authority; ignored repository paths remain outside the
tracked-cleanliness claim.

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
fsyncs the old epoch before clearing account/inbox/capture payload in place and
rebuilding projections. After quiescence and before either account-owner lease,
descriptor-rooted preflight validates the complete account, paper, demo, and
selected reset trees; Linux mount identities additionally reject same-device
bind aliases. Lease inodes are prepared without truncation and are revalidated
before and after the inherited-descriptor flock and metadata write.

Archive publication uses a trusted archive directory, a fixed child environment
with `PATH=/usr/bin:/bin` and no inherited `TAR_OPTIONS`, an exclusively created
mode-`0600` archive inode, descriptor-bound tar output, and an exclusive digest
sidecar; the exact inode, size, and SHA-256 are reopened and checked immediately
before deletion.
`--archive-dir` must name a dedicated leaf disjoint from every reset target;
alternate Linux mount views of the reset data filesystem are rejected so bind
aliases cannot hide overlap. A missing leaf is created mode `0700`; an existing
safe leaf is left unchanged. Canonical lock inodes are validated and preserved
so a reset cannot split one mutex into old and new path identities.

The output archive and sidecar are descriptor-bound, but `tar` still walks the
preflighted input trees by pathname. Service quiescence, both owner leases, and
the absence of unmanaged writers are therefore part of the archive-consistency
boundary. The reset cannot discover an ad-hoc process merely because systemd is
quiet. After the first removal there is no cross-root rollback: an I/O failure
or non-cooperating mutation can leave a partial clear, and cleanup deliberately
keeps every managed unit stopped for inspection and recovery from the durable
archive.

Both account owner leases stay held through the destructive boundary; with
`--leave-stopped --receipt`, they remain held through the receipt's final root
reopen. Removal and ownership restoration are descriptor-relative and finish
with exact-tree/absence rescans. Any failure after clearing begins leaves
managed units stopped. The reset never closes positions, cancels orders, or
deletes an audit archive.
`--leave-stopped` is required before a new authorization.

A requested reset receipt is published only while every managed unit is meant
to remain stopped and both owner leases are still held. The receipt creator
independently observes the exact systemd unit set loaded and inactive, reopens
the clean candidate commit, archive, sidecar, embedded manifest, and six fresh
roots, then writes and fsyncs an exclusive hidden mode-`0400` staging inode.
After validating that private inode it repeats the systemd and candidate checks,
links the same inode create-only at the final name, and removes the staging name.
While the linked final name is still non-loadable mode `0400`, it reopens the
receipt sources and fresh roots and repeats the systemd, candidate, parent, and
inode checks. Only then does mode `0600` commit the exact inode as a loadable
success artifact; the creator fsyncs the file and parent and revalidates only
the committed parent/inode identity. A normal publication failure attempts
descriptor-bound removal of only the inode it created.

The receipt is durable point-in-time evidence, not WORM storage, a signature,
ongoing systemd monitoring, or execution/deployment authority. An interruption
before the mode-`0600` transition leaves no loadable success artifact. Once that
transition occurs, all source, candidate, and systemd truth checks have passed,
but a crash can leave a valid receipt before the creator returns or completes
its final identity/durability checks. The receipt proves those point-in-time
checks, not later service state. Consumers must reopen and verify it,
independently check current service state, and separately authorize any
subsequent activation.

Receipt verification hashes and reopens the exact archive and embedded manifest
and recomputes the recorded account-epoch member-presence map. It does not
semantically reconcile every manifest `archived_targets` or
`preserved_risk_state` entry against tar members, and it format-checks
`ledger_reset_utc` without bounding that stamp to the receipt's start/finish
times. Its Git checks also trust repository-local configuration/excludes and
deliberately omit ignored paths. Those are explicit review obligations under
the cooperative, root-controlled host boundary, not facts the receipt proves.

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
