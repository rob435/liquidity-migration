# liquidity_migration/

106 modules in twelve subpackages, not counting the seventeen `__init__.py`.
The path tells you what a module is for; the import order tells you what it is
allowed to know.

## Where things are

| Package | What lives here | What does not |
| --- | --- | --- |
| `core/` | Substrate with no business meaning: time and format helpers, YAML config, env flags, systemd logging, deterministic clocks and finite JSON, immutable-file snapshots, canonical symbol identity, the demo/mainnet realm enum | Anything naming an account, dataset, sleeve, or venue endpoint |
| `marketdata/` | The credential-free public price plane: Bybit public REST/WS, the Binance public client, the WS kline pipeline, the ticker cache | Anything authenticated — that is `venue/` |
| `data/` | The data root on disk and what fills or validates it: atomic dataset read/write, ingestion, archives and manifests, history fetchers, universe construction, PIT membership and coverage, the trade tape | Live sockets |
| `account/` | The producers' library, left by the deleted Python order path and proven load-bearing 2026-08-19: contracts, the deterministic kernel, the filesystem route, leases, liveness, protection price math. Nothing here turns a target into a command any more — the Rust engine does | Credentials, venue transport, strategy decisions, placing orders |
| `rules/` | The registered decision rules production replays, and the target-book contract: the carry-hold rule and its live decision frame, the exodus short's sleeve surface, the LONG FC-v11a/v12 profiles with their features and signal, the persisted LONG identities, daily-bar feature math, the engine target-book writer | The historical engines and scorers that grade these rules — that is `research/backtest/` |
| `venue/` | The credentialed Bybit edge the surviving Python tools use: private transport, instrument rules, wedged-command resolution, market data with a key. The Rust engine is the only order path; the Python order adapter and quote manager stay only as fixtures for the kernel's tests, and the REST reconciler and execution stream were deleted 2026-08-19 | Anything usable without an API key; placing real orders — that is the engine's |
| `strategy/` | What the fleet decides to hold right now: the two sleeve producers and daemons, plus shared per-cycle machinery — candidate population, public data plane, planning, scheduling replay, cycle health | The historical engine behind a sleeve — that is `research/backtest/` |
| `research/panels/` | Causal point-in-time math: panel in, score out — feature panel, risk model, residual momentum, cross-venue substrate | |
| `research/backtest/` | Every sleeve's historical equity engine and chart writers, replaying the registered rules from `rules/` | The rules themselves — that is `rules/` |
| `research/execution/` | Measurement of what actually happened: trade diagnostics, the measured cost model, the passive-fill probe, venue accounting, and the quote lab | |
| `policy/` | The dials: operational sizing profile, execution config, real-money profile and arming, systemd environment reading. The equity-anchored envelope and the account loss halt moved to the engine (`engine/engine-risk/`) with the 2026-08-14 order-path deletion | |
| `ops/` | The operator surface in both directions: Telegram and notifications, the destructive reset path and its archive, epoch reset, maintenance lock | |
| `cli/` | `python -m liquidity_migration` — `commands.py` and its argparse builders in `parsers.py` | |
| `runtime/` | Empty since the account owner runners went with the Python order path (2026-08-14); kept as the import order's named sink | |

`__init__.py` and `__main__.py` are the only modules at the package root.

## Import order

Measured from the AST, not asserted. Every import points down this list; there
are no cycles between packages.

```
core → marketdata → data → account → rules → research → strategy → venue → policy → ops/cli → runtime
```

`rules/` may import only `core/`, `marketdata/`, `data/`, and `account/`: a
registered decision rule that live sleeves replay must never pull research
machinery. `tests/repo/test_import_order.py` holds the whole order.

The two ends are the useful ones. **`core/` knows nothing** — change it and you
can affect anything. **`runtime/` is a sink** — nothing imports it, so a change
there cannot reach the rest of the package.

`account/` importing only `core/` and `data/` is load-bearing: the kernel stays
independent of venue transport and of every strategy. If you find yourself
adding a `venue/` or `strategy/` import to `account/`, the design is telling you
the code belongs somewhere else.

## Conventions

- **Absolute imports only.** `from liquidity_migration.venue.bybit import ...`,
  never `from ..venue.bybit import ...`. The group is visible at the top of every
  file, and grep finds every caller.
- A module's package is chosen by what it *is*, not by who calls it. `market_capture`
  is in `account/` because only account owners use it, not in `marketdata/`.
- `strategy/` is flat at 13 modules; filename prefixes (`carry_*`, `long_*`)
  already group them. The next sleeve pushes it toward 20 — split
  per-sleeve then, not on a new axis.
- A sleeve daemon is a plug on `strategy/strategy_host.py`: the host owns the
  market planes, wake machinery (bar, account-journal commit, time deadline,
  idle floor), evidence tapes, and health receipts; a new strategy supplies the
  plug surface documented in the host's module docstring plus one CLI wiring.

## Entry points

`python -m liquidity_migration` is the research and data CLI. Seven modules are
run directly as `python -m liquidity_migration.<pkg>.<module>` from committed
shell under `scripts/` and `deploy/`; no systemd unit names a Python module, so
unit files never change when a module moves. See
[`scripts/README.md`](../scripts/README.md) for who runs what.
