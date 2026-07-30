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
| `status` | Read-only verification of the installed checkout and active topology. |
| `equity` | Repository-standard descriptive equity curves. |
| `research-refresh` | Plan, run, resume, or reconcile the offline market-data/features/backtest workflow. |
| `reset` | Remote ledger/archive preview; mutation requires `--execute`. |
| `venue-accounting` | Read-only demo journal/venue accounting capture. |
| `kill-criteria` | Weekly read-only sleeve K1/K2/K3 trip report; exit 3 on any trip. |
| `test` | Run pytest with forwarded arguments. |
| `deploy --execute install` | Install an exact commit while every project unit is stopped. |
| `deploy --execute activate` | Start the fleet allowed by the installed profile and sleeve toggles. |
| `deploy --execute rollout ...` | Guarded flat-account preflight, stopped install, activation, and verification in one locked operation. |

Environment overrides are `SSH_TARGET`, `REPO_DIR`, `PYTHON`, `BRANCH`,
`EXPECTED_COMMIT`, and the deploy script's documented SSH/repository settings.
`EXPECTED_COMMIT` must always be a full lowercase 40-character commit.

## Staged deployment

Deployment is deliberately split: installation happens with the fleet stopped,
activation starts it. For routine maintenance, the guarded rollout mode runs
both in one locked operation:

```bash
COMMIT="$(git rev-parse HEAD)"
EXPECTED_COMMIT="$COMMIT" BRANCH="$(git branch --show-current)" \
  scripts/ops.sh deploy --execute rollout \
  --profile operational
```

Use `--profile demo-operational` when paper integration is not intended. The
profile is an explicit input and is recorded at
`/etc/liquidity-migration/profile`. Rollout never enables mainnet, flattens a position, cancels a pre-existing order, or
widens a risk boundary. If the exact bound demo-rule evidence has expired —
or has merely passed half of its 168-hour lifetime — rollout places and
cancels its own bounded PostOnly demo probes only after the account has
passed every stopped flatness gate, so freshness renewal is a side effect of
ordinary deployment rather than an operator deadline. Standalone `install`
never probes.

The manual GitHub Actions workflow exposes the same four modes; `rollout`
requires a profile. A push still runs CI only and never deploys.

Before stopping anything, rollout fetches and proves the exact target is on the
selected remote branch, verifies the currently authorized commit and topology,
and requires all of the following at once: a verified canonical journal, fresh
healthy owner and reconciliation evidence, zero reconstructed and
authenticated venue positions, zero aggregate target, zero canonical working
orders, and a directly authenticated empty regular/conditional Bybit order
inventory. A non-flat or uncertain account fails immediately with the current
fleet untouched.

On a ready account, rollout stops producers, timer-driven readers, and
watchdogs before owners; binds owner health to the exact post-producer journal
head; stops owners; then repeats the local and authenticated flat-account
proof. Only after that final stopped proof may checkout installation begin.
The stopped install records the resolved profile, and activation starts owners
before producers and verifies the resulting topology. If the demo rules are
stale, the stopped install first refreshes them. It uses the current structural
notional and the prior rule artifact only as search hints: it first
tests the current structural boundary's adjacent steps, then (when needed)
rescales the prior rejected/accepted notional bracket to the current probe
price, freshly tests both endpoints, and bisects any remaining quantity-step
gap. A changed boundary falls back to the complete search. The fresh probe
keeps a current per-symbol ticker rather than reusing a potentially old bulk
price snapshot, and reports completed symbols, elapsed time, and ETA. It still
requires exact order/link identity, terminal cancellation on official order
surfaces, zero fills, empty trade history, cleanup, and final direct-venue
flatness. Its shared request limiter is capped at the registered 10 requests
per second. The prior rule artifact is preserved, the new path is atomically
installed, and rollout repeats its direct local/venue flat proof.

A failure before checkout installation restores the previously verified
topology. Once installation begins, any failure forces every managed unit
stopped for explicit recovery.
Each material phase prints start, success/failure, and elapsed seconds. Paper
and demo tree preflights run concurrently; neither normalizer starts until both
read-only plans pass, then the disjoint paper/demo normalizers run concurrently
and retain their independent final full rescans. Activation reuses a preserved
residual-momentum artifact only when its complete gate passes and the prior
refresh unit is not failed. Missing, stale, malformed, small-cross-section, or
failed-unit state takes the existing refresh path, which retries every 10
seconds and has a bounded 300-second deadline; both durations remain
positive-integer environment overrides.

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
and writes the resolved profile marker. Success reports `units_started=0`.

Deploy, activation, verification, and destructive ledger reset share the
host-wide
`/run/liquidity-migration/maintenance.lock`. The current scripts also nest the
retired deploy/reset lock leaves so the first rolling upgrade cannot overlap an
older installed maintenance process. A collision fails closed before the
selected operation reads or mutates deployed state. These are persistent,
root-owned mode-`0600` inodes: a descriptor-safe helper rejects symlink,
hardlink, mount-leaf, and replacement attacks around the inherited descriptors.
Deploy transmits that helper from the exact requested Git commit rather than
executing a helper from the not-yet-verified remote checkout.

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
runtime trees through directory descriptors before changing either batch. The
two disjoint read-only plans run concurrently, but both must pass before either
mutation batch begins; the disjoint normalizers then run concurrently. It
rejects symlinks, multiply linked files, special files, and root, nested, or
regular-file mount boundaries, including same-device Linux bind mounts. Missing
direct-child roots and cache/lock directories are then created relative to the
held data-root descriptor; owner and mode changes never use recursive pathname
`chown` or `find` traversal.

An already-normalized paper tree is a read-only fast path: the initial complete
inspection identifies only entries whose exact owner/group/mode needs repair,
so compliant entries are not reopened for `fchmod` or `fsync`. A separate final
complete descriptor-rooted rescan still compares the entire path set, inode,
type, mount identity, owner, group, and mode, including entries skipped by the
mutation pass. The optimization removes redundant writes, not validation.

Install also validates `configs/operational.demo.json`, installs its exact bytes
at the demo `ACCOUNT_RISK_POLICY_FILE`, and creates the isolated byte-exact
paper mirror. All strategy producers, the demo hedge, and both account owners
consume that one schema.

### Operational sizing profile

Edit `configs/operational.demo.json` to change operational leverage, notional
scale, entry capacity, the BTC trend gate, or account caps. Edit the repository copy and reinstall rather than editing
the installed `/etc` copies in place.

Validate a proposed edit before committing it:

```bash
.venv/bin/python -c \
  'from liquidity_migration.operational_profile import load_operational_profile; print(load_operational_profile("configs/operational.demo.json"))'
.venv/bin/python -m pytest -q tests/test_operational_profile.py
```

The validator rejects unknown keys, non-finite values, producer leverage above
the account maximum, and active LONG/CONTINUOUS sizing envelopes that cannot fit
the owner caps at `capital_reference_usdt`. Absolute account limits still
govern actual state, including hedge and simultaneous sleeve exposure, so a
coherent profile does not promise that every future signal will pass risk.
Apply changes through the normal stopped install and activation sequence;
changing this operational profile is not research promotion.

### 2. Activate and verify

```bash
EXPECTED_COMMIT="$COMMIT" scripts/ops.sh deploy --execute activate
EXPECTED_COMMIT="$COMMIT" scripts/ops.sh status
```

Activation requires the fleet to be quiescent, probes demo-key order permission, and validates the commit-owned
hedge model prior before starting anything when the hedge timer is enabled. The
check covers schema, provenance, causal boundary, and estimator sufficiency;
the prior is intentionally not subject to a wall-clock freshness limit. It then
starts account owners before enabled producers, reuses an already-valid current
residual-momentum gate or rebuilds it when required, enables the allowed timers,
and verifies the resulting topology. The liveness watchdog warns during
the final 24 hours of the bound 168-hour demo-rule lifetime so exceptional
maintenance is visible before expiry; it does not refresh rules or mutate the
venue by itself.

`status` is read-only but still fails if the checkout, roots, effective unit
surface, profile, or enabled topology differs. A failed
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
  --label planned-reset
```

The reset refuses live positions/orders, real-money configuration, mismatched
credential ownership, concurrent mutation, or unsafe writers. It archives and
fsyncs the old epoch before clearing account/inbox/capture payload in place and
rebuilding projections. After quiescence and before either account-owner lease,
it clears only stopped systemd failure metadata and requires every managed unit
to report literal `inactive`. Then
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

Both account owner leases stay held through the destructive boundary. Removal
and ownership restoration are descriptor-relative and finish
with exact-tree/absence rescans. Any failure after clearing begins leaves
managed units stopped. The reset never closes positions, cancels orders, or
deletes an audit archive.

## Evidence utilities

```bash
# Read-only stopped demo accounting capture.
scripts/ops.sh venue-accounting --help

# Descriptive curves, not promotion evidence.
scripts/ops.sh equity --help

# Offline append-first data/features/backtest plan; no mutation in plan mode.
scripts/ops.sh research-refresh plan --end YYYY-MM-DD

# Tests.
scripts/ops.sh test -q
```

Venue accounting reconciles the canonical demo journal with Bybit executions,
fees, closed P&L, funding, positions, and open orders over the named interval.
It is accounting evidence for that interval, not strategy parity, alpha, or
deployment authority.

## Recovery rule

When activation or verification fails, preserve the journal, unit state, and
logs; stop unsafe writers; and diagnose from the exact installed
commit. Do not weaken gates, mutate bound files, or replace systemd `ExecStart`
with an ad hoc command.
