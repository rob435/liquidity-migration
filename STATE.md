# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-06 16:49:06 UTC; authenticated host/native observation after the completed Round-2 deployment |
| Evidence | [Deploy run `34043450919`](https://github.com/rob435/liquidity-migration/actions/runs/34043450919); [qualification and private archive index](docs/tier1-round2-evidence.json) |
| Host | `208.84.103.4`; 4 vCPU, 8 GiB RAM, 118 GB disk |
| Deployed commit | `93404ff666dd3fc13957c7918dbcbe7e195f5caf`; all three installed binary hashes and loaded engine/worker/child images match the release artifact |
| Funded permission | `REAL_MONEY=true`; verified by `scripts/ops.sh status` at 16:03:30 UTC |
| Runtime state | Both engines and workers active; workers ready, repair gaps closed, 165/165 tickers, zero stream faults; four native positions and four exact full-size protective stops per realm |
| Worker lifetime | All eight child PIDs/start ticks persist across the 2,018-second capture window; the four CARRY/LONG children exceed 150 cumulative CPU seconds without replacement. No captured OOM, restart or callback fault. |
| Stored previous commit | `420c73477fbc85c4d23981ebea3042b310362e7f`; rollback remains subject to runtime-input and WAL compatibility, with forward repair across incompatible state |
| Disk | 41.71 GB free; watchdog minimum 25 GiB on `/var/lib`. Measured WAL growth is workload-dependent and is not a steady-state capacity guarantee. |
| Pending correction | The naturally scheduled demo probe exposes timer starvation on the deployed code; ordinary-input and control-spool cancellation corrections await deployment. [Qualification](docs/tier1-round2-evidence.json) |
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
