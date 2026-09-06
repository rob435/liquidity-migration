# Operational state

## Purpose

Record the latest verified host snapshot and the source of each operational setting.

## Spec Tables

| Observation | Value |
| --- | --- |
| Verified at | 2026-09-06 13:33:47 UTC; combined Round-2 deployment remains pending |
| Evidence | [Deploy run `34035526455`](https://github.com/rob435/liquidity-migration/actions/runs/34035526455); private host/native captures indexed at `/tmp/tier1-deploy-observation/post420-observation-summary.json` |
| Host | `208.84.103.4`; 4 vCPU, 8 GiB RAM, 118 GB disk |
| Deployed commit | `420c73477fbc85c4d23981ebea3042b310362e7f` |
| Funded permission | `REAL_MONEY=true` at the observation above |
| Runtime state | Both engines and workers active; workers ready with repair gaps closed; six native positions and six matching protective stops per realm |
| Pending runtime repair | Reused CARRY/LONG children exhaust a cumulative CPU limit; probe admission refuses unknown legacy entry cost. Fixes through `16689a98` are integrated into the Round-2 candidate, awaiting deployment |
| Stored previous commit | A predecessor without segment-v7 support cannot read the current WAL; use forward repair across incompatible formats |
| Disk | 49.73 GB free at the observation; watchdog minimum 25 GiB on `/var/lib` |
| Entry permissions | CARRY/LONG/EXODUS enabled in both realms, demo PROBE enabled; durable restoration verified at 13:30:37 UTC |
| Candidate | [Implementation checkpoint](docs/tier1-round-handoff.md); [round-2 open work](docs/tier1-audit-round-2.md) |

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
