# Autonomous improvement cycle 005: independent liveness observer

## Finding

- Audit timestamp: `2026-07-16T01:41:11Z`.
- Audited commit: `cd2abdcbf87869af924d4ae931c15852e0d4b80d`,
  plus the named local changes.
- `liquidity-migration-demo-liveness.service` declared both
  `Requires=` and `After=` on the demo account owner.
- The three-minute timer could therefore start an intentionally stopped sole
  venue writer. If the required owner failed activation, ordering could prevent
  the oneshot observer from running, suppressing the Telegram alert and healthy
  heartbeat exactly when owner startup was broken.
- `check_demo_liveness.py` already treats every non-active owner state as an
  immediate critical condition. The unit dependency contradicted that observer
  contract. Paper-owner monitoring was already independent, making the topology
  asymmetric.

## Prospective reproduction and implementation

A static regression forbids the monitored owner in systemd activation/lifecycle
directives (`Requires`, `Wants`, `Requisite`, `BindsTo`, `PartOf`, `Upholds`,
`Before`, and `After`). It failed before the edit on `Requires=`.

The unit now retains only network ordering:

```ini
Wants=network-online.target
After=network-online.target
```

No replacement owner dependency was added: `Wants=` would still resurrect the
writer, while `Requisite=` would still suppress observation. Deployment already
starts and verifies owners explicitly before enabling the liveness timer.

## Validation

- Runtime-script and liveness-checker focus: 61 passed in 0.20 seconds.
- Locked Python 3.11.5 focus: 61 passed in 0.23 seconds; all 26 lockfile
  dependency pins matched.
- Locked focused Ruff: passed.
- Full local pytest suite after this cycle: 1,606 passed in 20.76 seconds.
- Repository-wide Ruff: passed.
- Package-wide mypy: 85 modules passed.
- Existing tests still prove inactive owners generate critical alerts.
- `git diff --check`: passed in the final combined worktree.

No unit was installed, started, stopped, or reloaded. No VPS was contacted, so
this is a source-level deployment change, not an installed-state claim.
A basic static unit parse found all required sections/directives, but no genuine
Linux systemd verifier was available on the macOS host. `systemd-analyze verify`
remains an install-time validation requirement.

Residual limitation: the observer still depends on its authorization receipt,
environment files, wrapper verification, local Python runtime, timer, host, and
notification transport. The optional external heartbeat can detect some of
those failures, but no heartbeat URL is provisioned by default. Removing the
owner activation dependency fixes the direct self-defeating coupling; it does
not make the monitor externally independent of the host it observes.
