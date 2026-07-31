# liquidity_migration/

125 modules in eleven subpackages. The path tells you what a module is for; the
import order tells you what it is allowed to know.

## Where things are

| Package | What lives here | What does not |
| --- | --- | --- |
| `core/` | Substrate with no business meaning: time and format helpers, YAML config, env flags, systemd logging, deterministic clocks and finite JSON, immutable-file snapshots, canonical symbol identity, the demo/mainnet realm enum | Anything naming an account, dataset, sleeve, or venue endpoint |
| `marketdata/` | The credential-free public price plane: Bybit public REST/WS, the Binance public client, the WS kline pipeline, the ticker cache | Anything authenticated — that is `venue/` |
| `data/` | The data root on disk and what fills or validates it: atomic dataset read/write, ingestion, archives and manifests, history fetchers, universe construction, PIT membership and coverage, the trade tape | Live sockets |
| `account/` | The single owner that turns an accepted target into a command: contracts, the deterministic kernel, the filesystem route and target inbox, execution ports, leases, liveness, protection price math | Credentials, venue transport, strategy decisions |
| `venue/` | The credentialed Bybit edge — everything that can place, amend, or cancel a real order, and everything that reads venue truth back: private transport, the order adapter, execution-stream normalization, REST reconciliation, instrument rules, native stops, wedged-command resolution | Anything usable without an API key |
| `strategy/` | What the fleet decides to hold right now: the three sleeve producers and daemons, plus shared per-cycle machinery — candidate population, public data plane, planning, scheduling replay, cycle health | The historical engine behind a sleeve — that is `research/backtest/` |
| `research/panels/` | Causal point-in-time math: panel in, score out — feature panel, risk model, residual momentum, idio price paths, cross-sectional evaluator, cross-venue substrate | |
| `research/backtest/` | Every sleeve's registered rule and historical equity engine, plus chart writers | |
| `research/execution/` | Measurement of what actually happened: trade diagnostics, measured cost model, twin calibration, three-way reconciliation, venue accounting | |
| `policy/` | The dials: operational sizing profile, execution config, equity-anchored envelope, the account loss halt, real-money profile and arming, systemd environment reading | |
| `ops/` | The operator surface in both directions: Telegram and notifications, the destructive reset path and its archive, epoch reset, maintenance lock, demo/paper agreement | |
| `cli/` | `python -m liquidity_migration` — `commands.py` and its argparse builders in `parsers.py` | |
| `runtime/` | What a systemd unit actually executes: the demo and paper owner runners, readiness, and the paper twin (mirror, equity, funding accrual, passive execution) | |

`__init__.py` and `__main__.py` are the only modules at the package root.

## Import order

Measured from the AST, not asserted. Every import points down this list; there
are no cycles between packages.

```
core → marketdata → data → account → research → strategy → venue → policy → ops/cli → runtime
```

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
- `strategy/` is flat at 17 modules; filename prefixes (`carry_*`, `continuous_*`,
  `long_*`) already group them. The next sleeve pushes it past 20 — split
  per-sleeve then, not on a new axis.

## Entry points

`python -m liquidity_migration` is the research and data CLI. Ten modules are
run directly as `python -m liquidity_migration.<pkg>.<module>` from committed
shell under `scripts/` and `deploy/`; no systemd unit names a Python module, so
unit files never change when a module moves. See
[`scripts/README.md`](../scripts/README.md) for who runs what.
