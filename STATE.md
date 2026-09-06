# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-06 19:10:11 UTC; authenticated host/native observation after the completed Round-2 deployment |
| Evidence | [Deploy run `34049060363`](https://github.com/rob435/liquidity-migration/actions/runs/34049060363); [qualification and private archive index](docs/tier1-round2-evidence.json) |
| Host | `208.84.103.4`; 4 vCPU, 8 GiB RAM, 118 GB disk |
| Deployed commit | `af09aab53fc13cf53f66c393931c0ecccd765598`; all three installed binary hashes and loaded engine/worker/child images match the release artifact |
| Funded permission | `REAL_MONEY=true`; verified by `scripts/ops.sh status` at 17:48 UTC |
| Runtime state | Both engines and workers active; workers ready, repair gaps closed, 165/165 tickers, zero stream faults; five native positions and five exact full-size protective stops per realm. LONG callbacks abort after the TAO opening acknowledgement; a missing sleeve stop in the WAL latches both engines at `may_open=false`. Exact physical quantities agree with authenticated native exposure |
| Worker lifetime | LONG child PIDs disappear after `LONG filled state is invalid` at 18:05:23 demo and 18:05:44 mainnet. Other children continue; no service restart or OOM is captured. Executed-inventory correction is under local verification. |
| Stored previous commit | `93404ff666dd3fc13957c7918dbcbe7e195f5caf`; rollback remains subject to runtime-input and WAL compatibility, with forward repair across incompatible state |
| Disk | 42.32 GB free; watchdog minimum 25 GiB on `/var/lib`. Measured WAL growth is workload-dependent and is not a steady-state capacity guarantee. |
| Timer observation | Ordinary-input and control-spool cancellation corrections are deployed, but the 18:00 demo probe still queues PULL 5.912s late. Order-source and timer deferral repairs remain open. [Qualification](docs/tier1-round2-evidence.json) |
| Entry permissions | CARRY/LONG/EXODUS enabled in both realms, demo PROBE enabled; original permissions survive handover |
| Current implementation | [Implementation checkpoint](docs/tier1-round-handoff.md); [remaining qualification scope](docs/tier1-audit-round-2.md) |

| Sleeve ID | Mainnet | Demo | Repository authority |
| --- | --- | --- | --- |
| 0 | CARRY | CARRY | `configs/lane2_carry_hold_v7.json` |
| 1 | LONG | LONG | `configs/long_native_v12.json` |
| 2 | EXODUS | EXODUS | `configs/lane2_exodus_short_v1.json` |
| 3 | MAKER, quoting disabled | PROBE, enabled | `deploy/engine.mainnet.toml.template`, `deploy/engine.demo.toml.template` |

| Setting | Repository authority |
| --- | --- |
| Capital, leverage, exposure and rolling loss | `configs/operational.json` |
| Native configuration | Rust `render-native-config`; generated template regions |
| Account identity and permission | Root-owned realm env/config on the host; authenticated account reader |
| Signal delivery | Durable spool plus `stream.sock` notification; worker owns unpublished/retained prefixes |
| Units and activation | `deploy/fleet_manifest.tsv`; [systemd inventory](deploy/systemd/README.md) |
| Equity and telemetry | Equity-recorder timer, one-minute sampling; [observability](docs/observability.md) |
| History | [CHANGELOG.md](CHANGELOG.md); dated archived entries retain prior incidents and deployments |

## Invariants

- Must distinguish a dated host observation from local source and test results.
- Must replace this snapshot after verifying an actual deployment; commit success alone does not update it.
- Must preserve append-only sleeve IDs, exact ownership and native protection.
- Must verify WAL/worker compatibility before selecting a previous binary.

## Operational Recipes

```sh
scripts/ops.sh status
scripts/ops.sh --help
```

[Operations](docs/operations.md) · [Engine](docs/engine.md) · [Data](docs/data.md) · [Trading rules](docs/trading_logic.md)
