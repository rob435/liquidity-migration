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
| `liquidity-migration-engine-mainnet.service` | The Rust engine on the **funded** account — runs only while `REAL_MONEY` is armed |
| `liquidity-migration-bybit-long-demo.service` | LONG target producer |
| `liquidity-migration-bybit-carry-demo.service` | CARRY target producer |
| `liquidity-migration-bybit-{carry,long}-mainnet.service` | Real-money target producers; both start when `REAL_MONEY` is armed, sized by the installed risk profile |
| `liquidity-migration-demo-liveness.service` | Account/strategy watchdog and notification surface |
| `liquidity-migration-mainnet-liveness.service` | Mainnet account/strategy watchdog and notification surface |
| `liquidity-migration-telegram-controls.service` | Owner control buttons (pause/resume — there is no close button) — the sole `getUpdates` consumer |
| `liquidity-migration-llm-ledger.service` | LLM driver judgments on movers and trigger events, and the judged candidates file the demo LONG sleeve enters through — run by its hourly timer |
| `liquidity-migration-trade-notify.service` | Sends every sleeve's entries and its exits with what they made to the owner's DM — run by its 5-minute timer |

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
  failed unit is what it alerts on. The mainnet observer loads the root-only
  `telegram-mainnet.env` projection; funded API keys and `REAL_MONEY` never
  enter the observer process.
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
- **The engine is mandatory.** Activation requires the exact locked release,
  `/etc/liquidity-migration/engine.env`, its config, a fresh heartbeat, and the
  expected account/venue/realm binding. Missing or mismatched inputs fail closed.
- **Its build is part of the deploy gate.** `cargo build --release --locked`
  runs while the fleet is quiescent. Any toolchain, fetch, compile, install,
  restart, digest, or commit-marker failure aborts activation.

`liquidity-migration-engine-mainnet.service` has the same shape on the funded
account: gated by `/etc/liquidity-migration/engine-mainnet.env` plus the
binary, started through `start_mainnet_fleet` when `REAL_MONEY=true` in
`/etc/liquidity-migration/bybit-mainnet.env` — the single arming switch, and
the whole of what decides whether the funded engine trades. See the Real-money
section of [`../../docs/operations.md`](../../docs/operations.md).

The engines are installed by the exact systemd manifest and run under distinct
unprivileged identities. Their root-only credential files are loaded by PID 1;
producer and observer processes receive only non-secret projections.

## Watchdog timers

| Timer | First fire | Then |
| --- | --- | --- |
| `demo-liveness.timer` | `OnActiveSec=1min` | `OnUnitActiveSec=3min` |
| `mainnet-liveness.timer` | `OnActiveSec=1min` | `OnUnitActiveSec=3min` |
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

Only the mainnet watchdog pages on rule-receipt age because mainnet enforces the
168-hour ceiling as a hard start refusal. Deployment validates the declared
receipt but never renews it or mutates venue state; an operator must install a
fresh reviewed read-only receipt before expiry.
