# What was ported, and what changed on the way

The Python controls stay the reference and are untouched:
`liquidity_migration/policy/account_loss_guard.py`,
`liquidity_migration/policy/equity_anchored_envelope.py`,
`liquidity_migration/venue/venue_protection.py`, and the per-sleeve capital
partition in `liquidity_migration/account/account_kernel.py`.

What moved here is the **decision**: given an account and an order, allow it,
allow less of it, or refuse it and say why. Everything the Python controls do
*around* that decision — journal receipts, protection revisions, flatten
publishing, operator state files — is not a kernel decision and did not come
across. Where a Python rule leaned on that machinery, the row below says what it
became and why the outcome is the same or stricter.

Two words used throughout:

- **entry**: an order that adds exposure. It must carry a stop, and it is
  judged by the envelope and the partition.
- **exit**: `reduce_only`, on the opposite side of a position the account view
  actually holds, clamped to that position. It skips the stop, envelope, and
  partition controls, exactly as a risk-reducing batch does in the Python
  kernel.

## Order of evaluation

Fixed, and the first refusal wins:

1. **account view freshness** — older than `max_account_view_age_ns` is
   `StaleAccountView`; a view stamped after the decision it is judging is
   `UnknownState`;
2. **readability** of the view and the intent — `UnknownState`;
3. **account loss guard** — `LossGuardTripped`;
4. **exit or entry** — an exit is clamped to the position and stops here;
5. **stop discipline** — `MissingStop`;
6. **equity-anchored envelope** — `EnvelopeBreached`;
7. **per-strategy partition** — clamps, then `PartitionExhausted`.

## The mapping

| Python rule | Rust rule | Simplified | Why the outcome holds |
| --- | --- | --- | --- |
| Loss guard `BLOCKED` when equity is missing, non-positive, or older than `max_equity_staleness_ns` | Step 1/2: `StaleAccountView` on age, `UnknownState` on an unreadable equity | One age bound (`max_account_view_age_ns`) covers what Python splits between the loss guard's equity staleness and the protection health chain | Both refuse new risk. Rust also refuses exits while blind, which Python does not — stricter, see below |
| Loss guard `now - equity_ts_ns` | `intent.decided_ns - account.observed_ns` | The engine has no wall clock on the hot path; both stamps are engine monotonic nanoseconds | Same quantity. An intent that sat around makes its own view look older, which only refuses more |
| Loss guard "timestamp is in the future" `BLOCKED` | `UnknownState` | — | Same refusal; the Rust reason carries the detail string instead of a `BLOCKED` label |
| Loss guard `TRIPPED` when `opening - equity >= max_daily_loss_usdt` | `LossGuardTripped { equity_usdt, floor_usdt }`, `floor = opening - ceiling`, latched | Reported as a floor rather than a loss | `loss >= ceiling` and `equity <= opening - ceiling` are the same test |
| Anchor is the UTC day's opening equity, from `_utc_day(equity_ts_ns)` | `Kernel::observe_wall_clock_ns` supplies venue wall time; the day is `wall_ns / 86_400e9` | The monotonic clock in `AccountView` cannot name a UTC day, so the engine must hand one in | Same day boundary (Unix time has no leap seconds). With no wall clock ever supplied the anchor is set once and never re-anchors — the budget is spent, never refreshed |
| `snapshot()` / `restore()` to `account_loss_guard.json`, so a restart cannot refresh the budget | `loss_guard_anchor()` / `restore_loss_guard()`, for the engine to put in its log | No file handling in the kernel | Same anchor, same trip, same restart behaviour; the caller owns durability |
| `reset()` — operator only | `reset_loss_guard()` | — | Same |
| On `TRIPPED` the owner loop publishes a flatten (`publish_loss_ceiling_flatten`); on `BLOCKED` risk-reducing orders still flow | A genuine reduce-only exit passes the staleness and trip refusals (classified before them); entries are refused | The kernel gates orders; it does not plan flattens | Faithful: a trip's remedy IS exits, and blocking the flatten would be strictness in the harmful direction. The exit is clamped to the last known position and the venue's reduce-only enforcement bounds a mis-size. A flagged exit that reduces nothing stays refused as unknown state. (Ruled at integration, 2026-08-14, replacing the build agent's stricter refuse-everything choice.) |
| Envelope: `target = max(equity * equity_fraction, floor_usdt)`; contract immediately, expand only past the dead band, hold on a missing/non-finite/non-positive reading | Same, in `envelope.rs`, including Python's `math.isclose(rel_tol=1e-12, abs_tol=1e-9)` no-op band | — | Line-for-line |
| Envelope caps: `max_account_gross_notional_usdt = reference * multiple` | `allowance_usdt = reference * gross_notional_multiple * disaster_stop_fraction`, against a worst-case loss of `Σ notional * max(disaster_stop_fraction, that order's own stop distance)` | The Rust deny reason speaks in worst-case loss, not notional | With every position at the disaster stop the two are the same inequality scaled by `disaster_stop_fraction`. An order whose own stop is *wider* than the disaster stop is charged more — stricter |
| The book judged is position + working orders, at one set of prices both sides | Account view positions + registered orders not yet filled + this order, all at `max(last traded/observed price, position entry price)` | — | Same projected book. Taking the higher price never under-values it |
| `profile_at_capital_reference` rescales every cap when the reference moves | Partition shares are multiplied by `reference_now / reference_configured` | Only the caps this kernel owns are scaled | Same ratios follow the wallet |
| `max_symbol_notional_usdt`, the component/account gross split, `max_initial_margin_usdt` at account level, and the `available_margin` increase test | **Not ported** | The task named four controls; these are the other account-kernel caps | Named here so nobody reads their absence as a decision. The engine is looser than the Python fleet by exactly these four caps |
| Adapter refuses an exposure-increasing command with no durable entry-attached protection, before any venue call | `MissingStop` | The engine attaches the stop with the entry the same way; the kernel only decides | Same refusal, and equally before any venue call |
| Long stop must be below the durable decision reference price, short above, both comparisons inclusive | Same, against **every** price the order could fill at: its own limit and the last price the kernel was given | Python has one durable reference price; the engine may hold two disagreeing numbers | Same inclusive geometry; requiring the stop to clear both is stricter than picking either |
| `_optional_float`: a stop of `0`, negative, or unreadable is *absent* | A `StopSpec` whose `trigger_px` is not a positive finite number is `MissingStop` | — | Same reading |
| Reduce-only commands must not carry entry protection | Exits skip the stop check | Rust does not refuse an exit that carries a stop, it ignores it | The venue never sees it: the engine strips the stop at request build AND the gateway refuses to render stop fields on a reduce-only payload (both proven by tests since the money review found this claim was, at the time, false and its covering test vacuous) |
| Reduce-only is *derived*: the kernel compares the target against the projected position | The `reduce_only` flag on the intent must be set, and the order must genuinely reduce | The engine has no target book to derive it from | An order that reduces without the flag is judged as an entry, so it needs a stop and pays partition — stricter. An order with the flag that would not reduce is `UnknownState` |
| A position seen without its stop latches `breached_unprotected` / `_unarmed_entries`, blocks health, and publishes a flatten | Any position in the account view with `stop_attached == false` refuses every entry, on any symbol | No latch and no flatten: the kernel re-reads the view each cycle, and flattening is not its decision | Refusing new risk is the half of the response the kernel owns. Account-wide rather than per-symbol, matching the Python health chain — stricter than the per-symbol `missing_existing_native_protection` key. **Weaker in one respect:** Python's crossed-stop latch survives the stop coming back, this block clears as soon as the venue reports the stop attached. The engine has no journal to latch in |
| Stop price equality tolerance of half a tick | None | The kernel never compares a stop to a venue-rounded one; quantization happens at the venue boundary, as `engine-types` documents | Nothing to tolerate |
| `sleeve_limits`: a gross share and a margin share per sleeve | `StrategyAllocation { max_gross_notional_usdt, max_initial_margin_usdt }`, margin taken as `gross / leverage` with the account leverage from config | The engine's `Intent` carries no leverage, so one account-wide leverage stands in | Both shares still bind, and the tighter one wins, as in Python |
| A sleeve the partition does not name gets nothing | `PartitionExhausted` with `remaining_usdt: 0.0` | — | Same |
| Empty `sleeve_limits` means unpartitioned | Empty `allocations` means the partition step is a no-op | — | Same; this is the shipped demo profile |
| A batch that puts a sleeve over its share is **refused whole** | The order is **clamped** to what the share still funds; refused only when the remainder is below `min_order_notional_usdt` or not positive | — | **Deliberate difference.** Neither path lets the book exceed the share; Python sends nothing, this sends the part that fits. Chosen because `RiskVerdict::Allow { qty }` exists to clamp |
| An over-share sleeve must not veto another sleeve's de-risking | Exits skip the partition entirely | — | Same property, reached more directly |
| Sleeve attribution comes from the target payload | `register_order(client_order_id, intent, approved_qty)` binds an approved order to its strategy; `on_update` then follows fills and cancels | `OrderUpdate` carries no strategy, so the engine must bind the id it mints. This is an `engine-risk` method, not a change to `engine-types` | A fill for an order the kernel never approved is charged against **every** strategy's share until it nets out — stricter, and it is the signature of a second writer on the account |
| Load-time proof that shares sum inside the account caps | `KernelConfig::validate`, called by `Kernel::new` | — | Same refusal, at construction |
| `below_min_qty` / `below_min_notional` from instrument rules, reduce-only exempt | `min_order_notional_usdt`, entries only | The kernel holds no instrument rules; `min_qty` and step rounding stay at the venue boundary | The notional floor is the one the clamp needs |
| Reduction sized against the reconstructed position alone | An exit is clamped to the net position in the view; a `reduce_only` order that would not reduce is `UnknownState` | — | Same size, and a contradiction refuses instead of reaching the venue |
| Journal receipts, protection revisions, epochs, operator state files | None | The engine's memory is its own append-only log | No decision depended on them. Two pieces of state outlive the process, and both now do so through the log: the loss-guard anchor rides a control-anchor record (durable on change, restored at boot), and recovered in-flight orders are re-registered with the kernel so the partition keeps charging them |

Negative available margin is *not* a fault: the kernel checks that
`available_usdt` is a number and no more. Hand-trading a funded account makes it
negative in ordinary operation.

## Python defaults, for the record

Nothing below is compiled in. `KernelConfig` has no `Default`; the caller
supplies every number.

| Config field | Python source | Value |
| --- | --- | --- |
| `max_account_view_age_ns` | `account_loss_guard.DEFAULT_MAX_EQUITY_STALENESS_NS` | 120 s. Related: protection health age, 10 s default (`account_service.py`), floored at 4 s by the runtime |
| `loss_guard.max_daily_loss_usdt` | `configs/operational.mainnet.json` | 10.0, which is 0.1 × the reference. Demo profile: unset |
| `envelope.reference_usdt` | `capital_reference_usdt` | demo 250_000.0, mainnet 100.0 |
| `envelope.tracks_equity` | `capital_reference.mode` | mainnet `account_equity`; the demo profile has no block, so its reference is fixed |
| `envelope.equity_fraction` | `capital_reference.equity_fraction` | 1.0 |
| `envelope.floor_usdt` | `capital_reference.floor_usdt` | 100.0 |
| `envelope.expand_dead_band_fraction` | `capital_reference.expand_dead_band_fraction` | 0.05 |
| `envelope.gross_notional_multiple` | `max_account_gross_notional_usdt / capital_reference_usdt` | demo 2.0, mainnet 1.7676767676767676 — which is (1.0 carry + 0.75 long) / 0.99 |
| `envelope.disaster_stop_fraction` | `DISASTER_STOP_FRACTION` in `deploy/account-execution-mainnet.env.template` → `--disaster-stop-fraction` → `fallback_stop_fraction`, required in (0, 1) with no default | 0.35, the same number as carry's `declared_stop_loss_fraction` |
| `partition.leverage` | `account_risk.max_leverage` | 2.0 |
| `partition.allocations` (mainnet) | `account_risk.sleeve_limits` | carry 100.0 gross / 56.57142857142857 margin; long 75.0 / 42.42857142857142; continuous 1.7676767676767677 / 1.0 |
| `partition.allocations` (demo) | none declared | empty — unpartitioned |
| `partition.min_order_notional_usdt` | `InstrumentRules.min_notional`, per symbol from the venue | 1.0 in the partition tests |
| `qty_tolerance` | `AccountRiskPolicy.quantity_tolerance` | 1e-12 |

## The tests

`cargo test -p engine-risk`. Each case names the Python test it mirrors in a
comment above it, so the two implementations are checked against one table:

- `tests/loss_guard.rs` — `tests/policy/test_account_loss_guard.py`
- `tests/envelope.rs` — `tests/policy/test_equity_anchored_envelope.py`
- `tests/partition.rs` — `tests/account/test_sleeve_capital_partition.py`
- `tests/stops.rs` — `tests/venue/test_venue_protection.py` and the entry
  protection cases in `tests/account/test_account_kernel.py`
- `tests/order_and_fail_closed.rs` — evaluation order and unknown state
