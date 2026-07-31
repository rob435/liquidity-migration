# Operations

[`scripts/ops.sh`](../scripts/ops.sh) is the operator router and
[`scripts/deploy_vps_live.sh`](../scripts/deploy_vps_live.sh) the deploy engine behind it;
both act on one VPS over SSH. `scripts/ops.sh help` is the current surface. Mainnet arming
is out of scope here — see [`real_money.md`](real_money.md).

## Commands

| Command | Effect |
| --- | --- |
| `status` | Read-only topology verification (`deploy_vps_live.sh verify`). No arguments. |
| `equity [ARGS]` | Descriptive equity curves. `--sleeves long,continuous`, `--years N`, `--chart-leverage X`. |
| `research-refresh {plan,run,reconcile}` | Append-first data/features/backtest workflow. `plan` mutates nothing. |
| `reset [ARGS]` | Demo/paper ledger reset. Preview unless `--execute`. |
| `venue-accounting --account-root R --start-time-ms N --output PATH` | Reconcile the demo journal against Bybit executions, fees, closed P&L, funding, positions, open orders. Read-only. |
| `wedged-command {report,probe}`, `--execute resolve` | Read venue truth for an order command that can no longer progress; `resolve` writes one journal transition, never resends an order, and refuses while the venue still holds it. |
| `real-money {preflight,render-profile}` | Read-only arming report; profile render (`--execute --output PATH` writes one non-secret file). Starts nothing. |
| `test [PYTEST_ARGS]` | Local pytest. |
| `deploy --execute {install,activate,rollout}` | Staged deploy or guarded rollout. |

`SSH_TARGET` (`root@116.202.15.128`), `REPO_DIR` (`/opt/liquidity-migration`) and `PYTHON`
override the defaults; `LOCAL=1` runs `real-money` against this checkout. Every deploy mode
— `status` included — needs `EXPECTED_COMMIT` as a full lowercase 40-character commit, and
reads `BRANCH` (default `main`), `REPO_URL`, `REMOTE`, `SSH_OPTS`, `GITHUB_TOKEN` (falls
back to `gh auth token`) and the two `RMOM_BOOTSTRAP_*` durations. Deploy, activate, verify
and an executing reset share `/run/liquidity-migration/maintenance.lock`; a collision fails
before reading or mutating anything.

## Staged deployment

```bash
COMMIT="$(git rev-parse HEAD)"
export EXPECTED_COMMIT="$COMMIT" BRANCH="$(git branch --show-current)"
scripts/ops.sh deploy --execute install    # fleet stays stopped
scripts/ops.sh deploy --execute activate
scripts/ops.sh status
```

**install** needs a clean remote checkout and a quiescent `liquidity-migration-*` fleet. It
checks out the exact commit from `$REMOTE/$BRANCH`, installs `requirements.lock` with
`--no-deps`, runs Ruff, mypy and six focused runtime test files, installs the unit manifest,
disables every project unit, removes unknown `liquidity-migration-*` units, resolves the
hedge timer, writes `/etc/liquidity-migration/sleeves.resolved.env` and normalizes the paper
and demo runtime trees. Prints `install-ok commit=<sha> units_started=0`. It does **not**
write the profile marker; only `rollout` does.

**activate** reads `/etc/liquidity-migration/profile` (defaulting to `operational` when
absent), checks demo-key order permission, validates the hedge model prior when the hedge
timer is on, starts owners before producers, seeds residual momentum and enables the RMOM
timer when a CONTINUOUS sleeve is on, enables the liveness timer, then verifies. **verify**
asserts owners, producers and timers match the profile and resolved toggles; all three
mainnet units inactive and disabled; no failed oneshot; every installed unit file
byte-identical to the checkout's manifest with no drop-ins; demo order permission still good.
It compares units, not the installed HEAD, and prints `verify-ok` with commit and profile.

## Guarded rollout

```bash
EXPECTED_COMMIT="$COMMIT" BRANCH="$(git branch --show-current)" \
  scripts/ops.sh deploy --execute rollout --profile operational
```

`--profile demo-operational|operational` is required and lands in the profile marker just
before activation. Phases, each printing start/ok/failed with elapsed seconds: prefetch
the target and confirm it is on `$REMOTE/$BRANCH`; verify the current topology;
flat-account check (no venue position, no working order, zero aggregate target, read from
the journal and from Bybit directly); stop producers, timers and watchdogs, recheck
flatness against the exact post-producer journal head, stop owners, recheck stopped;
stopped install; record the profile; activate and verify. A non-flat or unreadable account
fails at the first flat check with the fleet untouched; failure before the install phase
restores the previous topology, and from the install phase on every managed unit is left
stopped.

Demo instrument rules carry a 7-day age limit and a rollout past half of it re-probes, so
freshness is a side effect of ordinary deployment rather than an operator deadline. The
probe places and cancels bounded PostOnly demo orders, only after the stopped flat checks
pass. [`demo_rule_probe.py`](../liquidity_migration/demo_rule_probe.py) exists because the
Bybit demo realm rejects orders its own `minNotionalValue` accepts — the real per-symbol
boundary is measured, not readable from the instrument spec.

The GitHub Actions workflow dispatches the same four modes — `rollout`, `install`,
`activate`, `verify` — and passes `--profile` on rollout. A push runs CI only.

## Profiles and sleeves

| Profile | Runs |
| --- | --- |
| `demo-operational` | Demo owner, demo producers, hedge/RMOM timers, liveness. Paper owner and paper sleeves must be off. |
| `operational` | Both owners, plus the paper producers and target mirror the toggles allow. |

[`deploy/sleeves.env`](../deploy/sleeves.env) is the repository ceiling; the host file
`/etc/liquidity-migration/sleeves.env` may only narrow `on` to `off`.

| Toggle | Now | Units it gates |
| --- | --- | --- |
| `LONG_SLEEVE` | on | `bybit-long-demo`, `bybit-long-paper` |
| `CARRY_SLEEVE` | on | `bybit-carry-demo` |
| `PAPER_TARGET_MIRROR` | on | `paper-target-mirror` |
| `CONTINUOUS_SLEEVE` | off | `bybit-continuous-demo`; forces the hedge timer on |
| `CONTINUOUS_PAPER_SLEEVE` | off | `bybit-continuous-paper` |
| `CARRY_PAPER_SLEEVE` | off | `bybit-carry-paper`, retired in favour of the mirror |
| `CARRY_MAINNET_SLEEVE`, `LONG_MAINNET_SLEEVE` | off | Never started here; verify asserts all three mainnet units off |

Turning a sleeve off stops new targets; it does not flatten an existing target or venue
position, so flatten through the account owner and confirm journal/venue agreement before
retiring one. Each unit names only `UNIT:ENTRYPOINT`, and
[`run_authorized_runtime.sh`](../scripts/run_authorized_runtime.sh) maps that pair to one
complete command line and execs it; callers cannot append argv.

Sizing lives in [`configs/operational.demo.json`](../configs/operational.demo.json): edit the
repository copy, never the installed `/etc` copy, then reinstall. `pytest -q
tests/test_operational_profile.py` runs the loader, which rejects unknown keys, non-finite
values, producer leverage above the account maximum and envelopes that cannot fit the owner
caps at `capital_reference_usdt`.

## Ledger reset

```bash
scripts/ops.sh reset --sleeves all                       # preview (default)
scripts/ops.sh reset --execute --leave-stopped --sleeves all --label planned-reset
```

Also `--sleeves long|continuous|carry|all`, `--archive-dir DIR` (default `data/_archive`),
`--include-reports`, `--include-caches`, `--settle-seconds N`, `--env-file`,
`--account-env-file`, `--paper-account-env-file`.

`--execute` refuses unless the demo account is already flat with no open orders, the
maintenance lock is free, mainnet configuration is absent, every managed unit reports
`inactive`, and every submit-armed unit loads the same credential file. It never cancels an
order or closes a position.
[`reset_path_safety.py`](../liquidity_migration/reset_path_safety.py) validates every target
path before anything is deleted, and both account-owner leases are held across the
destructive boundary. Journals, inboxes and captures are archived with a SHA-256 sidecar
re-checked immediately before deletion; configs, lock inodes, `residual_momentum.parquet`
and root-level market data survive. After the first removal there is no rollback — a failure
leaves the units stopped and the archive as the only recovery source.

## When activation or verification fails

- Preserve the journal, unit state and logs; diagnose from the exact installed commit. Do
  not hand-start a partial fleet, edit an installed `ExecStart`, or mutate state to get a
  green result. `phase-failed name=<phase> ... status=N` names the failing step.
- Known break: `verify failed: resolved PAPER_TARGET_MIRROR does not match loaded toggle`.
  `activate` and `verify` read six sleeve variables out of the resolved env and then
  compare nine, so `PAPER_TARGET_MIRROR` is treated as `off` and every host with the
  mirror on fails. Fix the variable list in
  [`deploy_vps_live.sh`](../scripts/deploy_vps_live.sh); do not turn the mirror off.
- A failed rollout leaves everything stopped. Finish it with
  `ROLLOUT_REFRESH_STALE_DEMO_RULES=1 ... deploy --execute install` then `activate`.
- Read logs on the host with `journalctl -u liquidity-migration-<unit> -n 200 --no-pager`;
  `systemctl list-units 'liquidity-migration-*'` shows the fleet.
- To flatten, publish through the account owner — reductions bypass every cap by design.
  Do not cancel or close by hand on the venue while an owner is running: the journal
  becomes the thing that disagrees with the account.
- Host replacement, SSH or deploy-key recovery, and expected-commit drift: the
  `vps-migrate` skill.

## Rules

- Local gates, none of which touch the VPS: `scripts/dev.sh doctor`, `scripts/dev.sh check`,
  `.venv/bin/python -m pytest -q` (2728 pass).
- Mainnet arming: [`real_money.md`](real_money.md). Agent working rules:
  [`AGENTS.md`](../AGENTS.md).
