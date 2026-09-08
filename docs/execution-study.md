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
| Published-rate discrepancy | [English base schedule](https://www.bybit.com/en/help-center/article/Trading-Fee-Structure) lists 5.5/2 bp taker/maker and permits regional differences; the [Ukrainian VIP-0 table](https://www.bybit.com/uk-UA/help-center/article/Benefits-of-the-VIP-Program) lists 10/3.6 bp, matching the account sample. The owner confirms Ukrainian identity verification. Lower pricing and the exact account classification require Bybit confirmation |
| Offline rates | Optional `fee_snapshot_path`: JSON `account_id`, `realm`, `rates: {SYMBOL: {maker, taker, observed_ns}}`; rates are decimal fractions, not basis points |
| Service | `liquidity-migration-execution-study.service`; `liquidity-engine-mainnet:liquidity-migration`; mainnet downstream timer job |
| Schedule / resources | 120 s after boot; 900 s after completion; 30 s timer accuracy; 600 s timeout; 384 MiB memory; one CPU maximum; nice 15 / idle I/O |
| Credentials | Root-owned `bybit-mainnet.env` and `engine-mainnet.env`; `BYBIT_INVENTORY_CREDENTIAL_SET=execution`; the Rust inventory probe exposes GET-only account methods; `REAL_MONEY` is unset |
| Writes | `/var/lib/liquidity-migration/execution-study/` only; no WAL, engine config, spool, or account mutation |
| Reports | `latest.txt`, `latest.json`; per-order records under `orders/CONFIG_SHA256/CODE_COMMIT/UTC_DAY-ORDER_ID_SHA256.json` |
| Restart / retention | `observed.json` resumes at a complete frame; partial active tails retry; `replay-cache.json` preserves complete comparisons after recorder files expire; retained projection/cache cover the rolling window, per-order reports persist |
| Invalid symbol tape | File read, parse, symbol or time-order errors leave that symbol's uncached arms unscored; preserve the error and available source hashes, continue other symbols, and retry incomplete results |
| Off-host retention | Host `backup.env` includes the study output directory in the existing checked engine-state backup |
| Operator read | `scripts/ops.sh execution-study [--json]` |

| Policy | Hypothetical behavior |
| --- | --- |
| `cross` | Cross finite displayed L50 liquidity at simulated arrival |
| `current` | Original request kind/price and existing native working planner; GTC can take liquidity |
| `passive_entry_30s` | LONG openings join even a one-tick spread; native GTC working planner, 30 s window, 15 s reprice, at most one passive amend, no lean/urgency improvement, then bounded cross and 20 s cross grace. Other sleeves and reductions retain `current` behavior |
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
| Actual accounting | Deduplicated normal/recovered fills attached to observed orders; original paid fees; unknown fees remain unknown; legacy rows without execution ID/time are counted separately and exclude their order from crossing calibration; excludes unmatched native-stop/manual executions and is not a whole-account ledger |
| Actual all-in scope | Fills with both a finite positive recorded order-arrival midpoint and a stated fee; retain negative rebates; `costed.fill_notional_coverage` reports their share of all identified filled notional |
| Actual slippage / total, USDT | `sum(side_sign * filled_qty * (fill_price - order_arrival_mid))`; add the actual fees of those same fills for total cost; slippage includes spread crossing and subsequent price movement, not funding |
| Actual all-in basis, bp | Divide each cost component by `sum(filled_qty * order_arrival_mid)` over the same costed fills and multiply by `1e4`; never add means with different populations or denominators |
| Actual rollups | `metrics.actual` by sleeve/symbol/action; `metrics.actual_by_slice` for all, sleeve, action and UTC decision day; `metrics.actual_by_order` by original client order ID; report cost dollars, basis points and coverage |
| Missing actual costs | Missing fee/midpoint counts remain explicit; zero costed fills produce null cost dollars/basis points. Original known fees remain visible even when a midpoint is absent; unidentified legacy rows remain outside these numbers |
| Markouts | Signed midpoint move from fill price at +1/+15/+60/+300 s, each within 2 s; actual fills use exchange execution time against recorder wall time; this retains cross-clock uncertainty |
| Decision features | Book time, bid/ask prices and sizes, spread bp, side lean and signed aggressive-flow score; features use observations available before the decision |
| Adaptive parameters | Side lean `bid_share - 0.5` for buys, opposite for sells; thresholds ±0.15; signed trade flow decays over 3 s, scales by touch depth and clips to ±4; attacked-side score >0.5 retreats |

| Runtime execution | Contract |
| --- | --- |
| LONG openings, both realms | `WorkPolicy::passive_entry_30s()` selected by `render-native-config`; both generated templates carry the complete policy |
| Passive order | PostOnly at the near touch; a marketable arrival is rejected rather than charged taker |
| Cross remainder | Cancel, confirm terminal state through independent REST lookup, reconcile exact cumulative fills, then admit one IOC remainder against a fresh quote |
| Transport | Mainnet trade WS; demo REST because demo trade WS is unavailable. A demo observation does not measure mainnet transport latency |
| Reductions / other sleeves | Existing policy selection; protective exits and LONG reductions do not acquire the new entry patience |
| Restart | `OrderSent.dispatch.intent.work` is retained in `OpenOrderState.entry_work` across rotation. Boot schedules cancellation of the venue-confirmed worked opening remainder through the existing paced cancel path without waiting for a quote; the old monotonic deadline is not resumed |
| Older snapshots | Missing `entry_work` remains readable as unknown; boot does not invent a policy for an old snapshot that discarded it |
| Evidence boundary | The rest/cross policy requires measured fills in each realm. Recorded-book comparisons are seen data; annual bar returns do not establish maker fill probability or realized savings |

## Invariants

- Must report all configured policy/queue/latency cells, including missing observations, partial fills, rejected quotes and missed winning moves.
- Must keep actual fills and hypothetical fills separate; a better simulated cost does not establish a live fill probability.
- Must treat every order as an independent marginal counterfactual, with no joint inventory, market response, hidden liquidity, funding P&L or cross-order request quota simulation.
- Must account for the limitation that recorded tape includes the incumbent's actual orders; this replay cannot remove their market impact.
- Must keep observed book/flow features causal; a later fill or markout is a label, never a decision input.
- Must not interpret passive reductions as permission to delay protective exits or strategy deadlines.
- Must not interpret queue scenarios as guaranteed bounds; aggregate displayed data cannot reconstruct exact venue queue position.
- Must keep completed results attached to their code/config/input identities; partial or absent public tape stays unscored.
- Must identify which data shaped the rule and which data evaluates it; reused observations and correlated orders do not become independent evidence.
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
PATH="$(rustup which --toolchain 1.90.0 cargo | xargs dirname):$PATH" cargo test --manifest-path engine/Cargo.toml -p engine-tools execution_study
PATH="$(rustup which --toolchain 1.90.0 cargo | xargs dirname):$PATH" cargo test --manifest-path engine/Cargo.toml -p engine-venue fee_rate_tests

# Offline: use a config with copied WAL/tape paths, a separate output directory,
# and an explicit fee_snapshot_path in the schema above.
engine/target/release/engine-tools execution-study --config /tmp/execution-study-config.json
```
