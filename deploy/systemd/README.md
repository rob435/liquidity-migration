# VPS systemd topology

The installed topology separates strategy target production from account
execution. Every guarded service enters through
`scripts/run_authorized_runtime.sh`, which replaces itself with the
commit-owned workload for that unit and entrypoint.

Operator commands, deploy modes, profiles and failure handling are in
[`../../docs/operations.md`](../../docs/operations.md); this file is only the
unit shapes.

## Services

Fifteen unit files: eleven services and four timers (two liveness, the LLM ledger, the trade notifier).

| Unit | Role |
| --- | --- |
| `liquidity-migration-engine.service` | The Rust execution engine — sole Bybit **demo** mutator, on the fleet's demo account (555899665, `bybit-demo.env`); holds that account's single-writer lease — see below |
| `liquidity-migration-engine-mainnet.service` | The Rust engine on the **funded** account — runs in shadow and sends nothing until the owner turns it live |
| `liquidity-migration-bybit-long-demo.service` | LONG target producer |
| `liquidity-migration-bybit-carry-demo.service` | CARRY target producer |
| `liquidity-migration-bybit-{carry,long}-mainnet.service` | Real-money target producers; both start when `REAL_MONEY` is armed, sized by the installed risk profile |
| `liquidity-migration-demo-liveness.service` | Account/strategy watchdog and notification surface |
| `liquidity-migration-mainnet-liveness.service` | Mainnet account/strategy watchdog and notification surface |
| `liquidity-migration-telegram-controls.service` | Owner control buttons (pause/resume — there is no close button) — the sole `getUpdates` consumer |
| `liquidity-migration-llm-ledger.service` | LLM driver judgments on movers and trigger events, and the live gate's book (`long_llm_gate_v1`) — run by its hourly timer |
| `liquidity-migration-trade-notify.service` | Diffs the target books and sends every sleeve's entries and exits to the owner's DM — run by its 5-minute timer |

The liveness services are invoked by their matching timers, and the engines own
the accounts. Target producers and auxiliary services have private API, mainnet,
`REAL_MONEY`, and unnecessary Telegram variables explicitly removed.

## Dependency edges

No unit can take the fleet down with it. Every unit's only lifecycle edges
are `Wants=`/`After=` on `network-online.target` — nothing `Requires=`
anything else in the fleet.

- **Producers** (demo and mainnet alike) publish target books to disk; the
  engine reads the books and owns the account. No producer binds to the engine:
  a dead engine leaves the producers running and publishing.
- **Neither liveness unit** has an ordering, requirement, binding, part-of,
  requisite, uphold, or wants edge to the units it watches — a stopped or
  failed unit is what it alerts on. The mainnet observer loads
  `bybit-mainnet.env` for the Telegram credentials only and unsets both
  API-key pairs and `REAL_MONEY`.
- **The control panel** (`telegram-controls`) likewise has no edge to the
  fleet it controls: it must keep serving buttons while the units it pauses
  or resumes are stopped. It holds Telegram credentials only — the API-key
  pairs and `REAL_MONEY` are unset — and acts through `systemctl` and the
  sleeve override + resolve library.

## The engine units

What the engine does with an account is [`../../docs/engine.md`](../../docs/engine.md);
`liquidity-migration-engine.service` is the odd unit here, in three ways.

- **It owns the fleet's demo account.** It loads `bybit-demo.env` — demo
  account 555899665, the live demo book — and holds that account's
  single-writer kernel lease. Nothing else writes to the account: anything
  else taking the lease stops the engine from starting rather than letting
  two writers wedge each other.
- **The host opts in.** The manifest installs the unit file everywhere, but
  the deploy starts and verifies it only where `/etc/liquidity-migration/engine.env`
  and the built binary both exist (`engine_installed` in
  [`../../scripts/deploy_vps_live.sh`](../../scripts/deploy_vps_live.sh)).
  Everywhere else the unit sits installed and stopped and nothing asks about
  it. `deploy/engine.env.template` is the file to fill in.
- **Its build cannot fail the deploy.** `cargo` runs in a clone of its own at
  `/opt/engine-build` after the fleet is up and verified, and after a rollout
  has disarmed its rollback trap. A missing toolchain or a build that will not
  compile prints a line and leaves the previously installed binary running.

`liquidity-migration-engine-mainnet.service` has the same shape on the funded
account: gated by `/etc/liquidity-migration/engine-mainnet.env` plus the
binary, started through `start_mainnet_fleet` when `REAL_MONEY=true` in
`/etc/liquidity-migration/bybit-mainnet.env` — the single arming switch — and
in shadow until the owner's own config says otherwise. See the Real-money
section of [`../../docs/operations.md`](../../docs/operations.md).

Neither engine unit is in `LM_AUTHORIZED_UNITS`
([`../lib_sleeves.sh`](../lib_sleeves.sh)). Every unit on that list must be
installed byte-identical on every host and runs with the credential pairs
stripped; the engines are opt-in per host and need their account's key pair —
they are what trades.

## Watchdog timers

| Timer | First fire | Then |
| --- | --- | --- |
| `demo-liveness.timer` | `OnActiveSec=1min` | `OnUnitActiveSec=3min` |
| `mainnet-liveness.timer` | `OnActiveSec=10min` | `OnUnitActiveSec=3min` |
| `llm-ledger.timer` | `OnCalendar=*-*-* *:05:00` | `Persistent=true` |
| `trade-notify.timer` | `OnCalendar=*-*-* *:0/5:30` | `Persistent=true` |

The demo observer fires a minute after the timer arms; cold-start noise is
handled by the watchdog's own startup grace (`--max-cycle-age-min 10`). Both
run with `--cooldown-min 60`, so a repeated condition pages hourly rather than
every pass. Both alert on a service that is enabled but not active.

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
