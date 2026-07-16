# Autonomous improvement cycle 007: serialize staged VPS operations

## Finding

- Audit timestamp: `2026-07-16T02:02:08Z`.
- Audited commit: `cd2abdcbf87869af924d4ae931c15852e0d4b80d`,
  plus the named local changes.
- The workflow-level concurrency key included `github.ref`. Manual dispatches
  from different refs therefore occupied different groups even though every VPS
  job targeted the same host and `/opt/liquidity-migration` checkout.
- The remote entrypoint had no host mutex. A GitHub job from another ref, a
  local operator, or another caller could enter `install`, `activate`, or
  `verify` concurrently.
- These are not harmless duplicate reads. Install changes the checkout and
  virtual environment, installs systemd units, disables the fleet, writes
  resolved sleeves, and invalidates authority. Activation starts that same
  fleet. A harmful interleave could start units and then disable them or retire
  their authority; two installs could switch the checkout during validation.

GitHub documents job-level concurrency groups as the mechanism that limits a
shared group to one running job:
<https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency>.

## Prospective reproduction

The regression requires the VPS job itself to use a constant, ref-independent
group and requires the remote lock to be acquired before the common mode
dispatch. Before implementation it failed because no job-level VPS group was
present; source inspection independently confirmed no remote lock primitive or
call existed.

The test is structural because exercising the actual race would mutate the
deployed host. The failure path and shared mutation surfaces are explicit in the
same entrypoint; no VPS experiment was needed to establish that unsynchronized
processes can interleave them.

## Implementation

- The manual `vps` job now joins `liquidity-migration-vps`, independent of the
  selected ref, without cancelling an operation already in progress.
- The remote script creates a root-owned mode-`0700` runtime lock directory,
  refuses a symlink at that boundary, opens a dedicated lock descriptor, and
  takes a non-blocking `flock`.
- The descriptor remains open for the remote shell's complete lifetime. Lock
  acquisition occurs once before dispatch, so it covers `install`, `activate`,
  and `verify` uniformly.
- A collision fails closed before the selected mode reads or mutates deployed
  state. GitHub provides the first coordination layer; the host lock also covers
  local invocations and callers outside that workflow.

## Validation

- Runtime-script suite: 20 passed locally in 0.16 seconds.
- Prospective/focused checks: 3 passed locally.
- Full local pytest suite after this cycle: 1,611 passed in 20.69 seconds.
- Repository-wide Ruff: passed.
- Package-wide mypy: 85 modules passed.
- `bash -n scripts/deploy_vps_live.sh`: passed.
- Ruby/Psych YAML parse and expected nested concurrency values: passed.
- Locked Python 3.11.5 runtime-script suite: 20 passed in 0.16 seconds; all 26
  lockfile pins matched and `pip check` was clean.
- Locked Ruff 0.15.14, shell parse, and `git diff --check`: passed.

No workflow was dispatched and no VPS was contacted, so this is source-level
hardening rather than an installed-state claim.

## Scope and next candidates

- GitHub concurrency guarantees exclusion, not execution of every queued
  dispatch; pending-run replacement and ordering remain platform policy.
- The host mutex covers callers of `deploy_vps_live.sh`. Ledger reset execution
  uses its own lock, and operational-authority issuance does not share this
  lifecycle mutex. The documentation deliberately says “remote entrypoint,” not
  every possible VPS mutation.
- A later change could unify deploy, reset, and authority issuance under one
  canonical host-mutation lease, but that is broader than the demonstrated
  deploy race and requires its own lock ordering design.
