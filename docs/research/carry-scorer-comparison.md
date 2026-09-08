# CARRY scorer comparison

## Purpose

Compare the daily-weight and hourly native-reducer CARRY scorers on matched historical days at 7.78 and 14.36 bp per side.

## Spec Tables

| Scope | Value |
|---|---|
| Config | `lane2_carry_hold_v7`; frozen sources at `46d9334549c80c423ab73e322027719f3299f273` |
| Native sizing | Multiplier 3; entry leverage 5; stop fraction 0.35; normalized million-dollar sizing equity |
| Return denominator | One unit of notional base; these are not account-equity or funded-account returns |
| Matched window | 2021-11-20 through 2026-09-06 inclusive; 1,752 days |
| Full daily coverage | 2021-01-08 through 2026-09-06; 2,068 days |
| Full hourly coverage | 2021-11-20 through 2026-09-07; 1,753 days |
| Inputs | `cross_venue_panel_v1` before 2025-06-01; refreshed `annual_20260908/panel-extended` thereafter; 12,348,264 rows |
| Artifact root | `~/SHARED_DATA/bybit_full_pit/reports/infra_20260908` |
| Identity | `comparison_identity.json`: source commit, source-file and input-shard SHA-256; `source-configs/` retains the exact rule and operational profile |
| Raw series | `daily_scores.csv`, `hourly_scores.csv`; `carry_2021_2026_cost_grid.csv` includes both complete, unmatched windows |
| Matched results | `matched_comparison.csv`; Decimal product independently agrees with binary64 recomputation within 1e-10 equity units |

All returns below are compounded percentages. Gross includes modeled funding and excludes execution cost; each net column subtracts one-way turnover times its stated per-side cost.

| Year | Days | Daily gross | Daily 7.78 | Daily 14.36 | Hourly gross | Hourly 7.78 | Hourly 14.36 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2021 | 42 | -0.28% | -0.29% | -0.30% | -0.10% | -0.12% | -0.13% |
| 2022 | 365 | +15.75% | +14.76% | +13.93% | -20.43% | -21.29% | -22.00% |
| 2023 | 365 | +35.58% | +33.88% | +32.45% | +44.76% | +42.57% | +40.75% |
| 2024 | 366 | +16.35% | +14.98% | +13.83% | +19.87% | +18.24% | +16.88% |
| 2025 | 365 | +358.85% | +335.24% | +316.22% | +190.75% | +172.38% | +157.74% |
| 2026 | 249 | +195.77% | +183.13% | +172.85% | -10.33% | -15.17% | -19.06% |
| all | 1752 | +2371.14% | +2070.56% | +1845.03% | +259.59% | +206.24% | +167.33% |

| Evidence boundary | Consequence |
|---|---|
| Early 2021 | Native scoring refuses fewer than 50 mature symbols; its first supported continuous window begins 2021-11-20. Earlier native returns are unavailable, not zero |
| Daily universe | `prepare()` requires an available future 24-hour return before ranking. The daily path is a diagnostic comparator with terminal-selection bias |
| Native execution | Hourly observed marks and immediate modeled target fills; 164,875 unpriced execution skips; no queue, intrahour path, market impact or venue outage reconstruction |
| Native exit observations | 1,023 settled-exit fires, 395 drop-exit fires, 2,686 resizes; zero historical pre-settlement running-rate observations |
| Decision clock | Daily grid and hourly-close replay do not establish a causal fill at 00:20 UTC. The corrected live lag requires receipt-time evidence |
| Cost sensitivity | Costs reprice identical reconstructed quantities; fee changes do not alter this scorer’s decisions. Margin, stop and execution policy changes require separate replay |
| Selection | Both windows contain data used to shape the rule and inspect its performance; neither is an independent performance grade |

## Invariants

- Must compare identical dates and the same return denominator.
- Must preserve missing observations and scorer refusals as coverage limits.
- Must report the hourly path’s negative 2022 and 2026 results alongside the daily path’s positive results.
- Must never interpret these normalized returns as the funded account’s ledger or proof that the current 3× allocation is profitable.
- Must keep the frozen 0.35-stop experiment separate from the current 0.10-stop risk policy.

## Operational Recipes

```sh
study="$HOME/SHARED_DATA/bybit_full_pit/reports/infra_20260908"
cat "$study/settings.json"
cat "$study/hourly_diagnostics.json"
cat "$study/matched_comparison.csv"
cat "$study/comparison_identity.json"
# The new checkout and result directory must not exist.
git worktree add --detach /tmp/carry-frozen-46d93345 46d9334549c80c423ab73e322027719f3299f273
PATH="$(rustup which --toolchain 1.90.0 cargo | xargs dirname):$PATH" CARGO_INCREMENTAL=0 cargo build --manifest-path /tmp/carry-frozen-46d93345/engine/Cargo.toml -p engine-strategies --bin strategy_contract
LIQUIDITY_MIGRATION_STRATEGY_CONTRACT_BIN=/tmp/carry-frozen-46d93345/engine/target/debug/strategy_contract .venv/bin/python "$study/compare_carry.py" /tmp/carry-frozen-46d93345 "$study/recomputed"
.venv/bin/python "$study/summarize_comparison.py" "$study/recomputed"
```

The scorer invocation is retained in `compare_carry.py` under the artifact root. Its source commit, frozen configuration files and input hashes define this run; changing any of them creates a new experiment.
