# `liquidity_migration/`

Python is the research, evidence, data, policy, notification, and deployment
support plane. It has no live directional decision, registered directional
config render, or authenticated order path. The Rust workspace under `engine/`
owns public-signal production, registered config rendering, live
LONG/CARRY/Exodus decisions, private account state, risk, and execution.

## Packages

| Package | Responsibility |
| --- | --- |
| `core/` | Business-neutral time, serialization, durable files, typed operational configuration, and venue-realm types |
| `marketdata/` | Historical/PIT ingestion, public download helpers, and research caches |
| `data/` | Point-in-time datasets, manifests, histories, universes, and trade tapes |
| `rules/` | Registered research metadata, takeover-source readers, and persistent clients for the Rust strategy replay contract |
| `research/` | Historical replay, panels, measurement, charts, accounting, and evidence reports |
| `policy/` | Real-money arming, funded-profile, and systemd-environment checks |
| `ops/` | Telegram controls and read-only operator reporting |
| `cli/` | `python -m liquidity_migration` research and data commands |

`__init__.py` and `__main__.py` are the only modules at the package root.

`research/lab/` is the study harness: the daily panel, the fast backtester,
the per-trade overlay against a matched placebo, the plateau checks, and the
evidence-note renderer. Every decision-influencing study runs through it so the
next negative result is reproducible and the next positive one is gradeable.

The market tape — recorder, Drive archives, loader, book rebuild, bars — is the
sibling package [`market_tape/`](../market_tape/README.md) at the repository
root. It imports nothing from `liquidity_migration`; research code may import
it (it sits below `core` in the order), and nothing on a live decision path
reads it.

## Dependency rule

Imports follow the ranks enforced by
[`tests/repo/test_import_order.py`](../tests/repo/test_import_order.py). Lower
layers cannot import research, policy, or operations code, and registered
rules cannot depend on historical research engines. Absolute imports are
mandatory.

The Python seams are read-only with respect to trading:

```text
historical or PIT data -> Python research -> Rust strategy_contract -> report
public forward capture -> immutable research archive
Rust heartbeat/trades  -> Python liveness and notifications
operator request       -> commit-bound helper -> Rust control spool
```

The live Rust flow is documented in
[`docs/architecture.md`](../docs/architecture.md).

## Entrypoints

`python -m liquidity_migration` exposes research and data work. Deployed Python
units are observers, notifications, public forward capture/upload, backup,
Telegram transport, and research-only jobs. Live signal acquisition,
directional config rendering, reduction, account control, and execution run in
the `signal-worker` and `engine` Rust binaries through the trusted launcher. See
[`scripts/README.md`](../scripts/README.md) and
[`deploy/systemd/README.md`](../deploy/systemd/README.md).
