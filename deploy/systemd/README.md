# Fleet systemd units

## Purpose

Describe the fleet's service identities, activation, and lifecycle from [the manifest](../fleet_manifest.tsv) and the unit files in this directory.

Every realm's four units — engine, signal worker, liveness service, liveness timer — and its manifest rows are generated from [`../realms.tsv`](../realms.tsv) by `python -m liquidity_migration.policy.realms render`. The manifest's generated rows sit between `# BEGIN GENERATED …` and `# END GENERATED …` markers; everything else in it is hand-written. Never edit a generated file by hand.

## Spec Tables

All family names below have the `liquidity-migration-` prefix; the group is `liquidity-migration` unless shown otherwise.

| Family | Realm | Lifecycle | Activation / cadence | User | Role |
| --- | --- | --- | --- | --- | --- |
| `signal-worker-demo` | Demo | Downstream | Boot | `liquidity-signal-worker` | Public observations and signal spool |
| `signal-worker-mainnet` | Mainnet | Downstream | Funded activation | `liquidity-signal-worker` | Public observations and signal spool |
| `signal-worker-mexc` | MEXC | Downstream | Funded activation | `liquidity-signal-worker` | Public observations and signal spool from MEXC's own public API |
| `signal-worker-hyperliquid` | Hyperliquid | Downstream | Funded activation | `liquidity-signal-worker` | Public observations and signal spool from Hyperliquid's own `/info` API and socket |
| `engine` | Demo | Owner | Boot, after worker | `liquidity-engine-demo` | Account execution and WAL |
| `engine-mainnet` | Mainnet | Owner | Funded activation, after worker | `liquidity-engine-mainnet` | Account execution and WAL |
| `engine-mexc` | MEXC | Owner | Funded activation, after worker | `liquidity-engine-mexc` | MEXC USDT-perp execution and WAL |
| `engine-hyperliquid` | Hyperliquid | Owner | Funded activation, after worker | `liquidity-engine-hyperliquid` | Hyperliquid perpetual execution and WAL |
| `forward-capture` | Shared | Independent | Boot | `liquidity-capture` | Bybit market tape |
| `forward-capture-binance` | Shared | Independent | Boot | `liquidity-capture` | Binance market tape |
| `market-tape-upload` | Shared | Independent | Hourly, minute 10 UTC | `root:root` | Pack and upload market tape |
| `backup` | Shared | Independent | Every 15 min at :02, :17, :32, :47 UTC | `root:root` | State and WAL backup |
| `storage-reclaim` | Shared | Independent | Hourly, minute 41 UTC | `root:root` | Verified-history reclamation: releases, apt cache, quarantine archive, sealed WAL below the engine's floor |
| `equity-recorder` | Shared | Independent | Every minute, second 20 | `liquidity-observer` | Append fleet metrics and push configured remote metrics |
| `host-liveness` | Shared | Independent | Every 180 s | `liquidity-observer` | Host, independent units, watchdog plane, external dead-man |
| `demo-liveness` | Demo | Downstream | Every 30 s | `liquidity-observer` | Demo and shared downstream health |
| `mainnet-liveness` | Mainnet | Downstream | Every 30 s while funded activation is enabled | `liquidity-observer` | Mainnet health |
| `mexc-liveness` | MEXC | Downstream | Every 30 s while mexc activation is enabled | `liquidity-observer` | MEXC health |
| `hyperliquid-liveness` | Hyperliquid | Downstream | Every 30 s while hyperliquid activation is enabled | `liquidity-observer` | Hyperliquid health |
| `execution-study` | Mainnet | Downstream | Every 900 s after completion while funded activation is enabled | `liquidity-engine-mainnet` | Read account fees, compare one-sided execution on recorded orders/tape; [contract](../../docs/execution-study.md) |
| `trade-notify` | Shared | Downstream | Every 5 minutes, second 30 | `liquidity-observer` | Attributed entries and realized exits to Telegram |
| `telegram-controls` | Shared | Downstream | Boot | `liquidity-controls:liquidity-controls` | Control requests through the account owner |
| `llm-ledger` | Shared | Downstream | Hourly, minute 05 | `liquidity-llm` | Public research nominations and judgments |
| `chaos-drill` | Demo | Downstream | Sunday 09:13 UTC | `root:root` | Demo restart and recovery drill |

| Contract | Source / value |
| --- | --- |
| Realms, their venues, postures and derived names | [realms.tsv](../realms.tsv), rendered by [liquidity_migration/policy/realms.py](../../liquidity_migration/policy/realms.py) |
| Fleet membership, lifecycle, realm, operator policy | [fleet_manifest.tsv](../fleet_manifest.tsv); the per-realm rows are generated |
| Unit installation and manifest helpers | [lib_sleeves.sh](../lib_sleeves.sh); realm facts in [lib_realms.sh](../lib_realms.sh) |
| Deploy launcher / remote implementation | [deploy_vps_live.sh](../../scripts/deploy_vps_live.sh) / [deploy_remote.sh](../../scripts/vps/deploy_remote.sh) |
| Independent families | Seven: both captures, upload, backup, storage-reclaim, equity-recorder, host-liveness |
| Capture restart | Only when its unit, capture configuration, symbol file, Python package, or runtime dependency input changes |
| Engine / worker restart | `Restart=always`, `RestartSec=5`, at most five starts per 300 seconds; exhaustion leaves the service failed until an explicit restart/reset, with no automatic flatten |
| Engine liveness | The engine writes its heartbeat from the event loop every five seconds; realm liveness detects age over 60 seconds on its 30-second timer. No systemd watchdog notification protocol is implemented |
| Engine state | Separate `StateDirectory` per realm; no two realms share a WAL, a spool, a control spool, or a heartbeat |
| Funded switch | `REAL_MONEY=true` in that realm's own credential file, named by the realm table (`<venue>-mainnet.env`). Explicit disarm rewrites the named file to false |
| Whether a deploy starts a realm | its `posture` in [realms.tsv](../realms.tsv): `stopped` stops and disables the realm's units, `running` hands over. Posture is never authorization; `REAL_MONEY` still is |
| Venue credential isolation | Each engine loads one venue's credential file and unsets every other venue's keys; every worker unsets all of them |
| Observer credentials | Notification units use `notifications.env`; liveness also uses `oncall.env`; equity uses optional `observability.env` |
| Research credentials | LLM ledger uses optional `llm-ledger.env`; venue credentials are unset |
| Host Python dependencies | [requirements-runtime.lock](../../requirements-runtime.lock): `websocket-client` for live capture; development and CI use [requirements.lock](../../requirements.lock) |

## Invariants

- Must preserve independent units through realm handover, stop, and disarm; changed capture inputs restart only the affected recorder.
- Must start each realm's signal worker before its engine and verify the new processes' heartbeats during handover.
- Must keep writes inside each unit's declared writable paths and state directories.
- Must keep venue credentials out of public workers, recorders, observers and controls; execution-study uses the account user's GET-only probe with `REAL_MONEY` unset.
- Must never infer funded authorization from a unit being installed or enabled.
- Must never hand-edit a generated unit file; `python -m liquidity_migration.policy.realms check` fails the gate on drift.

## Operational Recipes

```sh
# Re-render every realm's units, env templates and manifest rows, then verify.
python -m liquidity_migration.policy.realms render
python -m liquidity_migration.policy.realms check

# Read the canonical lifecycle inventory from the repository root.
awk -F '|' '!/^#/ {print $1, $3, $4, $6}' deploy/fleet_manifest.tsv

# On the host: inspect active units and timer schedules without changing them.
systemctl list-units --all 'liquidity-migration-*' --no-pager
systemctl list-timers --all 'liquidity-migration-*' --no-pager
systemctl show liquidity-migration-engine-mainnet.service \
  --property=ActiveState,MainPID,NRestarts,Restart,RestartUSec,StartLimitIntervalUSec,StartLimitBurst,WatchdogUSec

# After fixing a crash-loop cause, on the host, for the affected unit.
systemctl reset-failed liquidity-migration-engine.service
systemctl start liquidity-migration-engine.service
```
