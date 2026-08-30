# `liquidity_migration/`

Python is the research, market-data, strategy, and operations plane. It has no
authenticated order path. The Rust workspace under `engine/` is the sole
account and execution authority.

## Packages

| Package | Responsibility |
| --- | --- |
| `core/` | Business-neutral time, serialization, durable files, filesystem watches, logging, typed operational configuration, and venue-realm types |
| `marketdata/` | Credential-free public REST and WebSocket feeds and caches |
| `data/` | Point-in-time datasets, ingestion, manifests, histories, universes, and trade tapes |
| `rules/` | Registered decision rules, the pure typed LONG decision contract, and the strict absolute target-book contract |
| `research/` | Historical replay, panels, measurement, charts, and evidence reports |
| `strategy/` | LONG and CARRY producers, the independent Exodus event consumer, durable pre-settlement tapes, persistent sleeve state, scheduling, account-heartbeat projection, and target evidence |
| `venue/` | Read-only authenticated account observation used by operator diagnostics; never order mutation |
| `policy/` | Operational-profile rendering, execution environment, and real-money arming checks |
| `ops/` | Notifications and read-only operator reporting |
| `cli/` | `python -m liquidity_migration` research/data command surface |
| `runtime/` | Cross-package runtime health views, including strict engine heartbeat parsing |

`__init__.py` and `__main__.py` are the only modules at the package root.

## Dependency rule

Imports must follow the ranks enforced by
[`tests/repo/test_import_order.py`](../tests/repo/test_import_order.py). Lower
layers cannot import strategy or operations code, and registered rules cannot
depend on historical research engines. Absolute imports are mandatory.

The live seam is deliberately narrow:

```text
public data -> Python strategy -> durable absolute target book -> Rust engine
CARRY -> typed hash-chained pre-settlement tape -> Exodus producer
private venue state --------------------------------------------> Rust engine
Rust engine -> exact-identity heartbeat -> Python sizing and exit gates
```

A strategy daemon is a plug on `strategy/strategy_host.py`. The host owns
public caches, semantic account-change wakes, deadlines, price-touch wakes,
event tapes, cycle health, and activation evidence. A plug supplies decision
logic and publishes only its own target-book source.

## Entrypoints

`python -m liquidity_migration` is the research and data CLI. Runtime wrappers
live under `scripts/runtime/`; systemd units call those wrappers so module moves
do not leak into unit files. See [`scripts/README.md`](../scripts/README.md).
