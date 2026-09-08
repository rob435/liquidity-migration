# One-sided execution study

## Purpose

Compare execution costs for actual directional order intentions using recorded books, finite public trades, observed account fees, and explicit queue and latency assumptions.

## Spec Tables

| Component | Contract |
| --- | --- |
| Command | `engine-tools execution-study --config PATH` |
| Implementation | `engine/engine-tools/src/execution_study/`; existing `engine-core::working::plan` supplies the current working-order planner |
| Configuration | `configs/execution_study_mainnet_v1.json`, schema 1; rolling 48 h; CARRY, LONG, EXODUS; Bybit USDT linear contracts |
| Order source | `/var/lib/liquidity-migration-engine-mainnet/engine.wal`; CRC-checked incremental projection, original client/execution IDs, frozen instrument rules and order terms |
| Market source | Closed hours under `/var/lib/liquidity-migration/forward-market/YYYY-MM-DD/HH/SYMBOL/`; Bybit L50 books and public trades |
| Optional archive backfill | Same relative paths under `output_dir/tape/`; primary recorder files take precedence; source hashes appear in the report |
| Account rates | Authenticated `GET /v5/account/fee-rate` per symbol, refreshed every 24 h after account identity verification; unavailable rates exclude that symbol |
| Offline rates | Optional `fee_snapshot_path`: JSON `account_id`, `realm`, `rates: {SYMBOL: {maker, taker, observed_ns}}`; rates are decimal fractions, not basis points |
| Service | `liquidity-migration-execution-study.service`; `liquidity-engine-mainnet:liquidity-migration`; mainnet downstream timer job |
| Schedule / resources | 120 s after boot; 900 s after completion; 30 s timer accuracy; 600 s timeout; 384 MiB memory; one CPU maximum; nice 15 / idle I/O |
| Credentials | Root-owned `bybit-mainnet.env` and `engine-mainnet.env`; `BYBIT_INVENTORY_CREDENTIAL_SET=execution`; the Rust inventory probe exposes GET-only account methods; `REAL_MONEY` is unset |
| Writes | `/var/lib/liquidity-migration/execution-study/` only; no WAL, engine config, spool, or account mutation |
| Reports | `latest.txt`, `latest.json`; per-order records under `orders/CONFIG_SHA256/CODE_COMMIT/UTC_DAY-ORDER_ID_SHA256.json` |
| Restart / retention | `observed.json` resumes at a complete frame; partial active tails retry; `replay-cache.json` preserves complete comparisons after recorder files expire; retained projection/cache cover the rolling window, per-order reports persist |
| Operator read | `scripts/ops.sh execution-study [--json]` |

| Policy | Hypothetical behavior |
| --- | --- |
| `cross` | Cross finite displayed L50 liquidity at simulated arrival |
| `current` | Original request kind/price and existing native working planner; GTC can take liquidity |
| `post_only5s`, `post_only30s`, `post_only120s` | Join the decision-time near touch; post-only rejection if marketable on arrival; retry rejected quotes at 5 s observations; deadline cancel then cross the remaining quantity |
| `adaptive120s` | Re-evaluate every 5 s; join, improve one tick, or retreat one tick from touch using current side lean and decayed aggressive flow; cancel/replace loses priority; cross remaining quantity at 120 s |
| `passive_skip120s` | Join touch; cancel at 120 s; value unfilled quantity at the common horizon |

| Model term | Definition |
| --- | --- |
| Decision clock | Bridge intent monotonic decision time to the WAL's core-handled wall time within one process epoch; legacy orders use wire time explicitly; unresolved clocks remain unscored |
| Arrival delay | Observed decision-to-socket delay plus 5, 25, 100 or 250 ms hypothetical one-way hop; these are scenarios, not measured matching-engine latency |
| Queue scenarios | `trades_only`: displayed queue ahead, no cancellation credit; `cancellations_ahead`: infer displayed decreases after subtracting observed trades and credit them ahead |
| Passive fills | Correct-side aggressive trades consume queue and our remaining size; strictly worse print prices clear queue ahead but still supply finite volume; a book touch never fills |
| Cancel race | Original quote can fill until cancel arrival; replacement crosses only remaining quantity after cancellation; no simultaneous old/new quote |
| Freshness | Decision/request book must be valid, no more than 2 s old, and known at that time; an observed sequence gap during execution invalidates the arm |
| Common horizon | Decision +180 s; first valid book within 2 s supplies the midpoint; unfinished execution at this horizon is unscored |
| Price cost, bp | `1e4 * sum(side_sign * filled_qty * (fill_price - arrival_mid)) / (requested_qty * arrival_mid)` |
| Fee cost, bp | `1e4 * sum(fill_fee) / (requested_qty * arrival_mid)` |
| Missed cost, bp | `1e4 * side_sign * unfilled_qty * (horizon_mid - arrival_mid) / (requested_qty * arrival_mid)` |
| Total / saving | Price + fee + missed cost; `saving_vs_cross_bp = cross_cost - candidate_cost`; positive saving favours the candidate |
| Pairing | Same original order, queue assumption, latency, fee scenario and common mark; requested-notional weighted; openings/reductions, sleeves and UTC days remain separate |
| Crossing calibration | Fully observed market orders versus `cross` at each latency; mean signed, mean absolute and maximum absolute fill-price error per symbol; compare these errors with any claimed saving |
| Actual accounting | Deduplicated normal/recovered fills attached to observed orders; original paid fees; unknown fees remain unknown; excludes unmatched native-stop/manual executions and is not a whole-account ledger |
| Markouts | Signed midpoint move from fill price at +1/+15/+60/+300 s, each within 2 s; actual fills use exchange execution time against recorder wall time; this retains cross-clock uncertainty |
| Decision features | Book time, bid/ask prices and sizes, spread bp, side lean and signed aggressive-flow score; features use observations available before the decision |
| Adaptive parameters | Side lean `bid_share - 0.5` for buys, opposite for sells; thresholds ±0.15; signed trade flow decays over 3 s, scales by touch depth and clips to ±4; attacked-side score >0.5 retreats |

## Invariants

- Must report all configured policy/queue/latency cells, including missing observations, partial fills, rejected quotes and missed winning moves.
- Must keep actual fills and hypothetical fills separate; a better simulated cost does not establish a live fill probability.
- Must treat every order as an independent marginal counterfactual, with no joint inventory, market response, hidden liquidity, funding P&L or cross-order request quota simulation.
- Must keep observed book/flow features causal; a later fill or markout is a label, never a decision input.
- Must not interpret passive reductions as permission to delay protective exits or strategy deadlines.
- Must not interpret queue scenarios as guaranteed bounds; aggregate displayed data cannot reconstruct exact venue queue position.
- Must keep completed results attached to their code/config/input identities; partial or absent public tape stays unscored.
- Must label development data as seen; only subsequent days after a committed rule can grade that rule, and correlated orders are not independent samples.
- Must not claim a selected execution policy or a strategy return from this diagnostic alone.

## Operational Recipes

```bash
# Read the latest deployed result.
scripts/ops.sh execution-study
scripts/ops.sh execution-study --json > /tmp/execution-study-latest.json

# Run the configured read-only account/tape study now on the host.
scripts/ops.sh start execution-study.service
scripts/ops.sh logs execution-study.service 40

# Local tests on the pinned toolchain.
rustup run 1.90.0 cargo test --manifest-path engine/Cargo.toml -p engine-tools execution_study
rustup run 1.90.0 cargo test --manifest-path engine/Cargo.toml -p engine-venue fee_rate_tests

# Offline: use a config with copied WAL/tape paths, a separate output directory,
# and an explicit fee_snapshot_path in the schema above.
engine/target/release/engine-tools execution-study --config /tmp/execution-study-config.json
```
