# VPS systemd topology

The installed topology separates strategy target production from account
execution. Every guarded service enters through
`scripts/run_authorized_runtime.sh`, which replaces itself with the
commit-owned workload for that unit and entrypoint.

Operator commands, deploy modes, profiles and failure handling are in
[`../../docs/operations.md`](../../docs/operations.md); this file is only the
unit shapes.

## Services

| Unit | Role |
| --- | --- |
| `liquidity-migration-account-execution.service` | Sole Bybit demo order, fill, position, funding, protection, journal, and health owner |
| `liquidity-migration-bybit-long-demo.service` | LONG target producer |
| `liquidity-migration-bybit-carry-demo.service` | CARRY target producer |
| `liquidity-migration-bybit-continuous-demo.service` | CONTINUOUS component-target producer |
| `liquidity-migration-continuous-hedge.service` | Demo-only hedge target publisher |
| `liquidity-migration-continuous-rmom-refresh.service` | Residual-momentum refresh |
| `liquidity-migration-demo-liveness.service` | Account/strategy watchdog and notification surface |
| `liquidity-migration-account-execution-mainnet.service` | Bybit **mainnet** real-money order/fill/position/protection/journal owner |
| `liquidity-migration-bybit-{carry,long}-mainnet.service` | Real-money target producers; both start when `REAL_MONEY` is armed, sized by the installed risk profile |
| `liquidity-migration-mainnet-liveness.service` | Mainnet account/strategy watchdog and notification surface |

The hedge, RMOM, and liveness services are invoked by their matching timers.
Target producers and auxiliary services have private API, mainnet, `REAL_MONEY`,
and unnecessary Telegram variables explicitly removed.

## Dependency edges

No unit can take the fleet down with it.

- **Demo producers** carry `Wants=` (not `Requires=`) on
  `liquidity-migration-account-execution.service`, plus `After=` for ordering.
  A dead owner leaves them running: they re-check owner health each cycle and
  plan entries as blocked while still publishing exits, so a degraded fleet
  keeps draining risk.
- **Mainnet producers** keep `Requires=` on the mainnet owner.
- **The hedge** has `After=` ordering only and no owner requirement: it
  publishes into a durable inbox and degrades in-process when owner health is
  dead, so a restarting owner must not stop a hedge pass from being scheduled.
- **Neither liveness unit** has an ordering, requirement, binding, part-of,
  requisite, uphold, or wants edge to the owner it watches — a stopped or
  failed owner is what it alerts on. Their only lifecycle edges are
  `Wants`/`After` for network readiness. The mainnet observer loads
  `bybit-mainnet.env` for the Telegram credentials only and unsets both
  API-key pairs and `REAL_MONEY`.

## Owner unit shapes

The two owners are deliberately different.

| | demo (`account-execution`) | mainnet (`account-execution-mainnet`) |
| --- | --- | --- |
| `ExecStartPost` readiness gate | none | `run_authorized_runtime.sh ... readiness` |
| Memory | `MemoryMax=1024M`, no `MemoryHigh` | `MemoryHigh=384M`, `MemoryMax=512M` |
| `RestartSec` | 5 | 2 |

The demo owner has **no** `ExecStartPost` readiness gate: a failing gate killed
a live owner that was still draining exits. Every invariant it asserted is
enforced at the point of use — producers re-check owner health per cycle,
queued entries self-expire, exits never expire, submission is gated inside the
owner, and the watchdog re-reads the same artifacts every 3 minutes. The
readiness module stays for manual and `verify` use. `MemoryHigh` is likewise
absent on demo: reclaim throttling on the latency-critical owner stretched
venue round-trips into the stale-exposure wedge, and `MemoryMax` already bounds
it.

Mainnet units are installed by the manifest and started only when the single
arming switch — `REAL_MONEY=true` in `/etc/liquidity-migration/bybit-mainnet.env`
— is set; a plain `activate` or `rollout` then routes through
`start_mainnet_fleet`. See the Real-money section of
[`../../docs/operations.md`](../../docs/operations.md).

## Watchdog timers

| Timer | First fire | Then |
| --- | --- | --- |
| `demo-liveness.timer` | `OnActiveSec=1min` | `OnUnitActiveSec=3min` |
| `mainnet-liveness.timer` | `OnActiveSec=10min` | `OnUnitActiveSec=3min` |

The demo observer fires a minute after the timer arms — cold-start noise is
handled by the watchdog's own startup grace (`--max-cycle-age-min 10`), not by
staying blind for ten minutes. Both run with `--cooldown-min 60`, so a repeated
condition pages hourly rather than every pass. It alerts on a service that is
enabled but not active.

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

The demo watchdog also reopens the bound demo-rule receipt and warns in the
final 24 hours before the age bound the owner was started with (168 hours by
default). That is visibility only — it starts no maintenance, and an ordinary
rollout re-probes the rules well before it matters. The mainnet scope skips it:
that receipt is the demo realm's empirical order probe, which has no mainnet
counterpart.
