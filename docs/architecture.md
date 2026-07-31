# Architecture

How the runtime is split, where account truth lives, what the kernel refuses, which module
owns what. Code, tests, units, and generated artifacts define behavior when this file
drifts; deployed state is [`STATE.md`](../STATE.md). Replaces `account_execution.md`,
`account_journal.md`, `repository_map.md`, and `trade_diagnostics.md`.

## Producer / owner split

Strategy processes publish absolute component targets. They hold no venue credentials and
never submit, adopt, repair, or close an order.

| Role | Units | Mutates a venue |
| --- | --- | --- |
| Account owner, demo | `account-execution` | Yes — sole Bybit demo mutator |
| Account owner, mainnet | `account-execution-mainnet` | Yes, once the owner arms it |
| Account owner, paper | `account-paper-execution` | No venue exists; modeled fills only |
| Target producers | `bybit-{long,continuous,carry}-{demo,paper,mainnet}` | No |
| Target mirror | `paper-target-mirror` | No — republishes demo targets verbatim |
| Hedge / RMOM | `continuous-hedge`, `continuous-rmom-refresh` (+ timers) | No |
| Liveness | `demo-liveness` (+ timer) | No credential, no ordering dependency on the owner it watches |

```text
market data -> strategy target -> durable inbox -> account kernel
            -> risk decision -> OrderCommand -> submission attempt
            -> venue or modeled observations -> account journal -> projections
```

One owner per account, held by a persistent lease
([`account_owner_lease.py`](../liquidity_migration/account_owner_lease.py)): demo's is the
authenticated Bybit user-wide capability under `/run/lock/liquidity-migration`, paper's is
local to its account root. An owner derives its route without touching the filesystem,
takes the lease, then creates the paired account/inbox manifests
([`account_route.py`](../liquidity_migration/account_route.py)), so a losing owner cannot
initialize routes before discovering the active one. The inbox (`AccountIntentInbox` in
[`account_service.py`](../liquidity_migration/account_service.py)) is a filesystem queue —
`pending/processing/completed/failed/arrival`, atomic claim, durable arrival sequence — that
coalesces later replacements and carries component revisions, so an older entry cannot
reopen a component after a newer zero target.

## The account journal

Event-sourced, and the accounting authority for demo, paper, mainnet, and historical
replay. Implementation: [`account_kernel.py`](../liquidity_migration/account_kernel.py).

```text
<account_root>/account_journal/
  transactions/*.json   authoritative atomic transaction segments
  events.jsonl          rebuildable projection for humans and tooling
  journal.lock          cross-process writer lock
```

A write commits one complete segment by atomic replacement and fsync before the JSONL
projection is touched, so a crash exposes the prior segment set or the whole new one —
never half a target batch. Where segments exist, readers ignore the projection as an
authority source; a non-empty projection with no segments is rejected, never migrated.

Each event carries schema, stable UUID, global sequence, type, correlation and causation
IDs, account/sleeve/symbol, wall and monotonic time, payload, `prev_event_hash`, reducer
`state_hash`, and `event_hash`. The event ID is deterministic from account ID plus the
caller's idempotency key: redelivery with identical content is a no-op, reuse with changed
content is an integrity error. Verification rejects sequence gaps, duplicate IDs, mixed
account IDs, hash-chain breaks, illegal transitions, and state-hash disagreement.

```text
market_input_ref -> decision -> target -> risk_decision -> order_command
                 -> submission_attempt -> ack -> fill -> protection -> close -> pnl
```

`ack_observation`, `order_status`, and `venue_snapshot` are supplemental facts — transport
timing, terminal partial-fill/cancel state, authenticated venue truth — added without
rewriting earlier events. Positions, fees, funding, P&L, and cross-sleeve aggregates are
reconstructed by replay, never read from a mutable projection; read models, health,
notifications, and reports are consumers.

Cold and stateful readers use `read_account_journal(..., verify=True)` or
`verify_account_journal(...)`. Per-cycle producers use `AccountJournalCursor`, which
re-reads only segments added since the last call and cold-reads on any prefix mismatch.
Never put a bare `read_account_journal` in a per-cycle path: at 28.5k segments a full read
cost ~20 s CPU and ~250 MB peak, per call.

## Pre-trade gate

`AccountExecutionKernel._evaluate_batch` is the single admission point. It scores a whole
batch and journals a `risk_decision` carrying every rejection key. Strictly risk-reducing
batches bypass the growth caps — nothing here may block an exit.

| Check | Bound |
| --- | --- |
| Gross notional | `max_component_gross_notional_usdt`, `max_account_gross_notional_usdt` |
| Initial margin | `max_initial_margin_usdt`, and observed `available_margin_usdt` |
| Per-symbol notional | `max_symbol_notional_usdt` |
| Leverage | `max_leverage`, plus the venue's per-symbol ceiling |
| Sleeve partition (B3) | `sleeve_limits[sleeve]` gross and initial-margin shares |
| Instrument rules | quantity step, min quantity, min notional, tick |
| Inputs | missing market input, stale component revision, non-positive equity, unavailable capital snapshot |

**B3 sleeve partition.** Account-wide caps let one sleeve consume the whole envelope. When
the profile declares `sleeve_limits`, each sleeve is also held to its own share, priced at
the same reference prices as the prior book so the comparison isolates this batch's
quantity change. A sleeve the partition does not name gets nothing
(`unpartitioned_sleeve`); an untouched, non-growing sleeve is skipped, so a sleeve over its
share cannot veto another's de-risking.

**Equity-anchored envelope**
([`equity_anchored_envelope.py`](../liquidity_migration/equity_anchored_envelope.py)). The
profile is a set of ratios; the capital reference is the scale. Caps are a fraction of
observed wallet equity, not a pinned number. Equity down rescales immediately; equity up
waits for a move past a dead band. A missing, non-finite, non-positive, or stale reading
holds the current reference — unknown is not evidence of small.

**Daily loss halt**
([`account_loss_guard.py`](../liquidity_migration/account_loss_guard.py)). Account-level,
because the failure that matters is the whole book moving together. `OK`; `BLOCKED` when
equity is too stale to judge (no new risk, positions stay under their venue stops);
`TRIPPED` when the daily ceiling breaks against a fresh reading (flatten and stop, never
self-clears). The anchor is the day's opening equity, not a high-water mark, and it is
snapshotted so a restart cannot refresh the budget.

**Venue-native protection**
([`venue_protection.py`](../liquidity_migration/venue_protection.py),
[`protection_engine.py`](../liquidity_migration/protection_engine.py)). Every
exposure-increasing demo command durably carries a Full-position MarkPrice stop before any
provider call — `stopLoss`, `slTriggerBy=MarkPrice`, `tpslMode=Full`, `slOrderType=Market`
in the create request. A position without a durable active stop cannot be scaled up and an
existing stop never moves inward. Order creation is asynchronous, so the private stream and
REST position truth still confirm it; a missing or crossed stop latches
`breached_unprotected` and flattens.

## Realms and credentials

`ExecutionEnvironment` answers *which owner a producer publishes to*; `VenueRealm` answers
*which venue a private credential authenticates against*. Separate types on purpose:
`paper` is an owner with no venue at all.

| | demo | paper | mainnet |
| --- | --- | --- | --- |
| Realm | `demo` | none | `mainnet` |
| REST host | `api-demo.bybit.com` | — | `api.bybit.com` |
| Credentials | `BYBIT_DEMO_API_KEY/_SECRET` | none | `BYBIT_REAL_API_KEY/_SECRET` |
| Account ID | `bybit-demo-unified` | `bybit-paper-unified` | `bybit-mainnet-unified` |
| Env files | `bybit-demo.env`, `account-execution.env`, `sleeves.resolved.env` | `account-paper-execution.env`, `sleeves.resolved.env` | `bybit-mainnet.env`, `account-execution-mainnet.env` |
| Unit user | root | `liquidity-migration-paper` | root |
| Instrument rules | `demo_rule_probe`, empirical | mirrored demo rules | `get_instruments_info`, read-only |

The realm is a required argument with no default and every fallback lands on `demo`;
mainnet requires someone to type it *and* `REAL_MONEY` armed on top. Producers inherit only
the public/route values they need and explicitly unset private API, mainnet, `REAL_MONEY`,
and Telegram variables. Paper units pin `REAL_MONEY=false` and reject inherited exchange
credentials. Env files are strict `KEY=VALUE` data, parsed and never sourced as shell.

[`demo_rule_probe.py`](../liquidity_migration/demo_rule_probe.py) submits and cancels real
PostOnly orders on demo to find the empirically accepted minimum notional: the demo realm
rejects some orders its own `minNotionalValue` says it should accept. It refuses any realm
but demo; mainnet takes the declared `minNotionalValue` at face value and labels the source
`venue_declared`.

The paper execution twin is commit-owned, not calibrated: 5.5 bps taker fee, 2.0 bps
residual adverse slippage, a walk of the visible depth-50 decision book, partial fills by
book level, zero modeled latency, and rejection of exposure-increasing decisions older than
250 ms. Every modeled ACK, fill, status, and health record is tagged
`integration_only_uncalibrated`.

## Units, profile, sleeves

A unit names only `UNIT:ENTRYPOINT`;
[`scripts/run_authorized_runtime.sh`](../scripts/run_authorized_runtime.sh) maps that pair
to one complete command line and `exec`s it. Callers cannot append argv. The installed
profile — `demo-operational` or `operational` — is a plain marker at
`/etc/liquidity-migration/profile`, written at install and read back by verify. Deploy
modes: `install | activate | verify | rollout`. Activation starts owners before producers;
shutdown stops producers before owners.

[`deploy/sleeves.env`](../deploy/sleeves.env) is the repository ceiling — a host override
may turn a repo-enabled sleeve off but cannot resurrect a disabled one. Today `LONG_SLEEVE`,
`CARRY_SLEEVE`, and `PAPER_TARGET_MIRROR` are on; `CONTINUOUS_SLEEVE`,
`CONTINUOUS_PAPER_SLEEVE`, `CARRY_PAPER_SLEEVE`, `CARRY_MAINNET_SLEEVE`, and
`LONG_MAINNET_SLEEVE` are off. Turning a sleeve off stops target publication; it does not
cancel, close, or zero prior state. Flattening means publishing zero targets through the
owner and waiting for fills.

Operator routes ([`scripts/ops.sh`](../scripts/ops.sh)): `status`, `equity`,
`research-refresh`, `reset`, `venue-accounting`, `wedged-command`,
`real-money {preflight|render-profile}`, `test`,
`deploy --execute {install|activate|rollout}`.

## Subsystem map

| Module | Owns | Tests |
| --- | --- | --- |
| [`account_kernel.py`](../liquidity_migration/account_kernel.py), [`account_contracts.py`](../liquidity_migration/account_contracts.py) | Journal storage, reducer, event/risk contracts, pre-trade gate, command emission | `test_account_kernel.py`, `test_account_journal_cursor.py`, `test_account_risk_reduction.py`, `test_sleeve_capital_partition.py` |
| [`account_service.py`](../liquidity_migration/account_service.py) | Inbox queue, owner loop, crash-safe request replay | `test_account_service.py` |
| [`account_service_runner.py`](../liquidity_migration/account_service_runner.py), [`account_paper_runner.py`](../liquidity_migration/account_paper_runner.py) | Owner processes, realm wiring, paper twin config | `test_account_service_runner_readiness.py`, `test_account_owner_health.py`, `test_paper_account_equity.py` |
| [`account_service_bybit.py`](../liquidity_migration/account_service_bybit.py), [`bybit_execution_adapter.py`](../liquidity_migration/bybit_execution_adapter.py), [`account_execution_stream.py`](../liquidity_migration/account_execution_stream.py) | Bybit providers, order submission, private WS | `test_account_service_bybit.py`, `test_account_execution_stream.py` |
| [`execution_adapters.py`](../liquidity_migration/execution_adapters.py) | Deterministic modeled execution twin | `test_passive_execution.py` |
| [`account_intent_client.py`](../liquidity_migration/account_intent_client.py), [`strategy_targets.py`](../liquidity_migration/strategy_targets.py), [`paper_target_mirror.py`](../liquidity_migration/paper_target_mirror.py) | Write-only producer boundary; verbatim demo-to-paper republication | `test_account_intent_client.py`, `test_strategy_targets.py`, `test_paper_target_mirror.py` |
| [`account_route.py`](../liquidity_migration/account_route.py), [`account_owner_lease.py`](../liquidity_migration/account_owner_lease.py) | Root identity; one owner per account | `test_account_route.py`, `test_account_owner_lease.py` |
| [`account_reconcile.py`](../liquidity_migration/account_reconcile.py), [`account_venue_accounting.py`](../liquidity_migration/account_venue_accounting.py), [`account_strategy_state.py`](../liquidity_migration/account_strategy_state.py) | REST recovery, venue truth, accounting rows, sleeve planning read model | `test_account_reconcile.py`, `test_account_funding_reconcile.py`, `test_account_venue_accounting.py`, `test_account_strategy_state.py` |
| [`equity_anchored_envelope.py`](../liquidity_migration/equity_anchored_envelope.py), [`operational_profile.py`](../liquidity_migration/operational_profile.py) | Capital reference and derived caps | `test_equity_anchored_envelope.py`, `test_operational_profile.py` |
| [`account_loss_guard.py`](../liquidity_migration/account_loss_guard.py) | Daily loss halt | `test_account_loss_guard.py` |
| [`venue_protection.py`](../liquidity_migration/venue_protection.py), [`protection_engine.py`](../liquidity_migration/protection_engine.py) | Stop placement, repair, external-fill adoption | `test_venue_protection.py`, `test_protection_engine.py` |
| [`venue_realm.py`](../liquidity_migration/venue_realm.py), [`execution_environment.py`](../liquidity_migration/execution_environment.py), [`venue_instrument_rules.py`](../liquidity_migration/venue_instrument_rules.py), [`demo_rule_probe.py`](../liquidity_migration/demo_rule_probe.py) | Realm/owner separation, credential selection, rules per realm | `test_venue_realm.py`, `test_venue_instrument_rules.py`, `test_demo_rule_probe.py` |
| [`reset_path_safety.py`](../liquidity_migration/reset_path_safety.py), [`account_epoch_reset.py`](../liquidity_migration/account_epoch_reset.py), [`account_reset_archive.py`](../liquidity_migration/account_reset_archive.py) | Descriptor-rooted checks before an `rm -rf`, epoch transition, archive | `test_reset_path_safety.py`, `test_account_epoch_reset.py`, `test_account_reset_archive.py` |
| [`trade_diagnostics.py`](../liquidity_migration/trade_diagnostics.py), [`post_fill_markouts.py`](../liquidity_migration/post_fill_markouts.py) | Command-grain TCA projection, markout scheduling | `test_trade_diagnostics.py`, `test_post_fill_markouts.py` |
| [`market_capture.py`](../liquidity_migration/market_capture.py), [`ws_state_cache.py`](../liquidity_migration/ws_state_cache.py), [`account_owner_health.py`](../liquidity_migration/account_owner_health.py) | Book capture, stream state, health projection | `test_market_capture.py`, `test_account_owner_health.py` |

Producer-side strategy modules (`long_native*`, `continuous_*`, `carry_demo*`,
`financed_longs.py`, `lane2_blend.py`) are documented with the research they implement:
[`trading_logic.md`](trading_logic.md),
[`strategy_program.md`](strategy_program.md). Data roots, PIT rules, and clock domains:
[`data.md`](data.md).

## Trade diagnostics

[`scripts/build_trade_diagnostics.py`](../scripts/build_trade_diagnostics.py)
`--account-root R --capture-root C --out DIR` builds one deterministic row per canonical
order command from a verified journal plus the exact decision books. It refuses a dirty
tree unless `--allow-dirty`, refuses an existing output, and writes exactly
`execution_tca.parquet` and `manifest.json`. `s=+1` buy, `s=-1` sell; costs positive when
adverse. `M0` is the decision-book midpoint, `P` the quantity-weighted fill price, `Mh` the
first healthy midpoint at or after `h`.

```text
arrival_shortfall_bps = 10_000 * s * (P - M0) / M0
effective_spread_bps  = 20_000 * s * (P - M0) / M0
signed_markout_bps(h) = 10_000 * s * (Mh - P) / P
fee_bps               = 10_000 * observed_fee / abs(filled_notional)
```

Horizons are 1s, 15s, 1m, 5m. The private-execution and REST consumers enqueue only an
execution ID; the owner loop resolves it later and registers horizon tasks, so diagnostics
never delay fill accounting. A horizon whose five-second lateness bound expires is
terminally missing with a reason and a null midpoint — never zero. Coverage is reported per
horizon; late, gapped, restarted, or capacity-rejected fills cannot be silently dropped.

Grains do not interchange: `command_id` answers "was this order executed well",
`(sleeve, symbol, signal_ts)` answers "did this idea work", a target batch answers "did
concurrent ideas share one shock". Component rows, order updates, partial fills, and
overlapping horizons are not independent observations. Never subtract timestamps from
different clock domains and call the answer latency.

## Evidence boundary and working rules

Demo observes real venue order lifecycle, latency, fees, and funding for its exact epoch.
Paper validates the software path against its declared model and supports no
execution-quality or performance claim. LONG's forward record is demo-only. `carry_hold`'s
corrected benchmark Sharpe is 1.21 (t 2.31) — it does not beat the CONTINUOUS benchmark.
Grading: [`../AGENTS.md`](../AGENTS.md).

- Validate in proportion: `scripts/dev.sh test tests/test_x.py`, then `dev.sh doctor`, then
  `dev.sh check` (ruff, mypy, `.venv/bin/python -m pytest -q`, 2728 passing).
