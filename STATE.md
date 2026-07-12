# Research Program State

Last updated: 2026-07-12.

This is a descriptive live operating page, not research policy. Durable
research decisions are in
`docs/research_summary.md`; dated experiment anchors are indexed in
`docs/preregistration/INDEX.md`.

## Operational headline

| Sleeve | Mode | Current state |
| --- | --- | --- |
| `continuous_ensemble_v2` | Bybit demo + paper | On and flat after the canonical-journal migration boundary; the migrated TUSDT demo row is closed and the paper row is retained as non-open history |
| `LongV11aDivWeekendVol` | Bybit demo + paper | On and flat after the canonical-journal migration boundary |

- Mainnet is not enabled. Changing that requires an explicit owner instruction
  and new evidence.
- Direct Bybit demo snapshot at `2026-07-12T22:48Z`: account-wide equity
  `$10,027.35`, zero positions, zero open orders, and zero venue exposure. The
  hourly report preview independently agrees: no open Bybit positions, one
  TUSDT open/close change in the last hour, and `+$3.15` realized.
- Canonical execution-journal release `6f2bde773` is deployed and checked. The
  local and GitHub full gates each passed `2,768` tests with one skip; the
  exact-commit VPS verifier passed after the migration reset. Mainnet remains
  categorically disabled.
- Execution history is now the hash-chained, append-only
  `canonical_journal/events.jsonl`. Trade/order Parquet, TCA, Telegram, and
  reconciliation state are replayable projections. The lifecycle reducer is
  shared by historical, paper, and demo modes, and venue executions retain
  per-fill ID, quantity, price, fee, venue time, latency, and deferred
  1/5/30-minute markouts.
- The `2026-07-12T22:47:28Z` migration boundary archived the old generated views
  at
  `data/_archive/ledger-reset-20260712T224728Z-canonical-journal-migration.tar.gz`
  (SHA-256
  `2dfc39ef6262007cd42cb5f0bf401ba208dcf32472636a5368f706d1fc6cd748`).
  It retained the journals, appended verified-flat facts, rebuilt projections,
  and restored all nine previously active units.
- The demo-only 10x execution-stress release remains deployed and independently
  verified. This is execution-stress evidence, not permission or evidence for
  mainnet use.
- CONTINUOUS demo and paper now make scale explicit: the registered 2%-of-equity
  base is multiplied by `NOTIONAL_MULTIPLIER=10`, while
  `ENTRY_LEVERAGE=10` supplies exchange margin. The first changes order quantity;
  the second alone would not. This prospective epoch is for lifecycle, venue,
  fill, hedge, and reporting stress. Its P&L is not 1x alpha validation.
- A bounded XRPUSDT demo probe directly established the current small-order
  boundary: 4.65 XRP (`$5.114535`) was rejected as off-grid, 4.5 XRP
  (`$4.94955`) was rejected below the 5 USDT minimum, and 4.6 XRP filled and
  reduce-only closed at `$5.05954` with 10x leverage. Total fees were
  `$0.00556550`; the account and all services finished flat/healthy. Receipt:
  `docs/bybit_demo_min_order_probe_2026-07-12.md`.
- The final clean epoch is archived at
  `data/_archive/ledger-reset-20260712T204249Z-clean-slate-hedge-verified.tar.gz`
  (SHA-256 `e256e5b709ff6443b6b888c36bc53c873c731b63632593abb90de082ecd5cb01`).
  The archive contains the bounded live hedge smoke evidence; no smoke row was
  left in the forward ledgers.
- The hourly operator report now renders one compact human-readable portfolio
  snapshot. The post-reset print-only receipt says 0 positions, all four systems
  on, no recent position changes, and no action needed. Telegram transport still
  returned HTTP 429 during the two smoke adjustment notifications after the
  earlier flood. There was no retry loop; successful live delivery remains
  unverified until Telegram's server-side cooldown expires.
- The Bybit depth and liquidation collectors are active and fresh. They are
  forward context/shadow data, not historical alpha evidence.
- An external liveness dead-man URL is still not provisioned. The on-box timer
  works, but an off-box heartbeat should be added before any mainnet discussion.

## 1000TAGUSDT incident

Entry equity was `$10,039.6785`. Venue executions and account records reconcile
as follows:

| Layer | Base | Sniper | Total |
| --- | ---: | ---: | ---: |
| Price PnL | -$72.44380000 | -$15.10110000 | -$87.54490000 |
| Trading fees | -$0.29927414 | -$0.05532786 | -$0.35460200 |
| Before funding | -$72.74307414 | -$15.15642786 | -$87.89950200 |
| Six funding credits | — | — | +$0.20271274 |
| Bybit account Closed-PnL | — | — | **-$87.69678926** |

The final loss was 0.873502% of entry equity. The local sniper ledger is not
venue authority because the shared-symbol exit was historically attributed to
the wrong leg. Full reconstruction and decisions:
`docs/incidents/2026-07-10-1000tag.md`.

This event does not establish that a fixed stop helps. Full portfolio replays
of 20%, 40%, and 80% adverse stops reduced MAR on both venues. What it does
establish is that a demo-only add-on without paper/backtest parity, an explicit
loss budget, or reliable component attribution was unjustified.

## Continuous target

- Baseline clock: `2026-06-18T19:54:00Z`.
- Components: p3 `1/3`, p4p3 `2/9`, p4p5 `4/9`.
- Entry: stable causal rmom q25, inverse-vol sizing (`target=0.01`, clamp `2`),
  prior-day BTC uptrend gate, and `CTRL_BTC_RISK_70_90_35` sizing.
- Portfolio: max 25 active shorts, max 5 new per cycle, BTC+ETH hedge, BTC-vol
  regime, daily rebalance off.
- Exit: component TP12 and durable 24-hour max hold.
- Disabled: sniper, fixed/server stop, left-decile, stop-approach, failed-fade,
  breakeven, re-entry cooldown, portfolio heat overlay, account drawdown overlay.

Sniper is pinned off in demo, paper, deploy, verify, and recovery. Cleanup still
handles legacy or late sniper fills while new sniper entries remain disabled.

## Hedge availability and limits

- The tape is built from the exact live TP12 + BTC-risk-sizing object on the
  stable-only RMOM engine, with modeled funding, 200 observations, and a
  validated data boundary of `2026-07-09`. On `2026-07-12` it is three days old,
  inside the armed manager's maximum age. The official Bybit 1x receipt remains
  `exploratory`: +24.36% return, -1.20% max drawdown, MAR 6.22. It is an
  operational beta input, not new alpha or promotion evidence.
- The three historical BUSDT shorts were not left unhedged by an inactive timer.
  Their combined gross short fraction was only 1.02%; every five-minute manager
  pass computed a `$2.73` BTC target and `$0.00` ETH target. The then-active $25
  strategy floor suppressed it, but removing that floor would still not have
  produced a BTC order: the contemporaneous 0.001 BTC quantity step required
  roughly `$64.18` at the venue.
- The arbitrary $25 hedge floor is now removed. Every nonzero desired target is
  planned and the executor reports the desired/current delta plus the live
  per-leg `qtyStep`, `minOrderQty`, `minNotionalValue`, and effective executable
  minimum. The contemporaneous effective minimums were about `$64.18` BTC and
  `$18.21` ETH; suitable alt contracts can execute close to `$5`.
- A bounded deployed-code smoke at `2026-07-12T20:41Z` proved the actual venue
  lifecycle. The manager bought 0.001 BTC at Bybit's confirmed `64172.5` fill,
  persisted it as `continuous_addon`, produced no transient untracked-position
  alert, then sold the same 0.001 BTC reduce-only at `64169.9`. Bybit finished
  with zero positions and zero open orders. This is demo execution evidence, not
  evidence that the hedge improves returns.
- The manager now reconciles the idempotent BTC/ETH target every five minutes,
  not only at 00:35 UTC. A stale non-flat book fails even when the desired order
  is below the venue's executable filters, and liveness treats stale beta with
  open positions as critical. The CSV carries its validated data-through boundary and source
  summary SHA-256, so a quiet no-trade gap is not mistaken for stale data.
- Hedge intent is durable before the venue mutation. Immediate execution-history
  lag falls back to terminal Bybit order history; a genuinely unreadable fill is
  labelled provisional and later venue reconciliation replaces rather than
  double-adds it. The reset workflow writes a verified-flat boundary before the
  hedge timer restarts, so a controlled clean slate no longer fails as unknown
  ledger state.
- This hedge covers portfolio beta only. It would not have protected the
  idiosyncratic 1000TAGUSDT squeeze and is not a substitute for the registered
  ex-ante loss-budget or granular adverse-state work.

## Deployed safety release

- Side- and component-aware WS risk reconciliation, orphan adoption, side-flip
  handling, false-empty protection, quantity-conserving Closed-PnL allocation,
  and cost-source provenance.
- Durable planned exit deadlines and restart recovery for CONTINUOUS and LONG.
- Stable-only residual momentum with exact schema, duplicate/non-finite guards,
  and no provisional rows entering signals.
- Wallet-only equity high-water persistence separated from entry-health
  snapshots, so a non-wallet snapshot defect cannot erase risk memory.
- Guarded ledger reset: dry-run default, explicit execute, flat/no-orders check,
  REAL_MONEY refusal, writer quiescence, credential binding, lock, archive hash,
  fsync, allowlist deletion, retained high-water state, and a post-delete
  verified-flat cycle boundary before hedge restart.
- Hedge submissions persist intent before venue mutation, recover delayed fills
  from order history, label unresolved fills provisionally, and reconcile them
  without quantity double-counting. Untracked-position alerts honor the same
  90-second grace already used by adoption/exit actions.
- Reconciliation fails on stale remote market planes, separates open exposure
  from historical notional, and labels local price PnL/fees/venue allocations
  without pretending funding is present.
- LONG selected-entry rejections are durable; deterministic alerts are
  restart-safe and rate-limited by stable rejection class.
- `scripts/ops.sh` is the one operator surface for status, reconcile, equity,
  reset, research plans, tests, and checked deploy.
- Continuous hedge target reconciliation is five-minute and fail-loud on stale
  non-flat state; the source tape is self-describing and hash-bound to its
  official current-object summary.

## Clean ledger boundary

- The `2026-07-12T22:47:28Z` all-sleeve reset is the canonical migration
  boundary. It first refused while a real TUSDT demo short and its TP order were
  still open. The position was then closed through an idempotent reduce-only
  demo order, Bybit was re-proven flat, and the guarded reset completed.
- Reset no longer deletes lifecycle authority. It bootstraps legacy rows into
  the journal, records the verified-flat venue boundary, archives generated
  views, removes only allowlisted projections/epoch telemetry, then rebuilds
  trade/order/TCA views by replay.
- Continuous demo replay contains one closed TUSDT row, 16 verified journal
  events, and two TCA rows (entry and close). Continuous paper retains its
  pre-boundary simulated row as `awaiting_pnl`, which is excluded from exposure
  and new close attempts. LONG, hedge, and shared compatibility roots are flat.
- The earlier execution/reconciliation evidence remains recoverable from both
  the journal and dated archives. A clean forward boundary does not erase or
  upgrade that historical evidence.

## Long v11a research read

Latest internal refresh through the 2026-06-23 signal day:

| Venue | Trades | Return | Max DD | Sharpe-like |
| --- | ---: | ---: | ---: | ---: |
| Bybit | 188 | +32.87% | -3.46% | 1.98 |
| Binance | 190 | +27.59% | -4.00% | 1.46 |

The object survives best-month removal, 2x/3x cost stress, worst-12-month
windows, and the matched symbol null on both venues. The material dependency is
unchanged: removing take-profit exits flips both venues negative. Treat the
small forward sample as execution evidence, not validation.

## Research and data readiness

- `continuous-tail-survival-2026-07-10.md` registers only control plus
  0.10%/0.15%/0.25% ex-ante +100%-loss budgets. No heavy run has executed.
  Signals end 2026-07-10 exclusive; exit-path kline/funding data ends 2026-07-12
  exclusive. Both venues, full stable-rmom history, exact funding cadence, and
  byte-bound full-PIT receipts are required for a positive verdict.
- `continuous-granular-adverse-risk-2026-07-10.md` registers the separate
  executable adverse-state mechanism study. No treatment run has executed.
- Current canonical roots are not granular-ready. Bybit has no canonical
  `klines_5m` dataset in the current root; Binance’s legacy granular files are
  stale before the current PIT tail. The old 2026-06-27 5m validation artifact
  is not current-root readiness.
- Strict bounded audit: Bybit 2026-07-03..09 has 4,288 PIT symbol-days; 5m is
  missing, funding is complete on 14.16%, OI has 607 partial days and no
  complete hourly days, and premium content is invalid under the new contract.
  Binance 2026-06-26..07-02 has 5,292 PIT symbol-days; legacy 5m/metrics have no
  complete current-window days, bookDepth is missing, and funding/OI/premium/
  taker-flow completeness is 83.79%/79.48%/83.79%/83.31%.
- Forward Bybit depth/liquidation capture may inform later shadow diagnostics;
  it cannot fill historical treatment features.

Strategy-overhaul status is still synthetic and outcome blind:

- The CONTINUOUS raw population builder now validates a narrow OHLCV source
  projection and restarts rolling history after any interior hourly gap. Its
  diagnostic S02 wrapper emits the exact registry-typed 196-field projection,
  requires separate exact warmup/source and retained signal-window key
  inventories, and derives stable RMOM[D]'s causal-computability time as
  `D - 1 day + 1 hour` from the frozen shift-3 target construction.
  Provisional RMOM rows remain unavailable. This is an offline causality bound,
  not a claim about historical publication, ingestion, or operational latency.
  CONTINUOUS S03 and S04 are separate exact typed projections with anchor/parity
  and path-completeness checks.
- LONG now requires the exact runtime v11a config at every stage, canonicalizes
  the signal key, validates global hourly keys and consumed OHLC geometry,
  checks the daily close against the exact signal-hour close, reconstructs
  stage-owned values before accepting downstream input, and refuses non-frozen
  horizons on the registered S04 path. Its S02 wrapper validates supplied
  population/age, recomputes rank metadata, and emits exactly 138 fields. A
  separate outcome-blind builder now reconstructs availability, BTC/ETH regime,
  and configured BTC-month sidecars from raw hourly OHLC, preserves unavailable
  context as null, and parity-checks the production fallbacks. S03 emits exactly
  30 fields; S04 consumes exact S02+S03 and emits exactly 71 fields after geometry
  reconstruction.
- A central schema projector enforces exact order, dtypes, non-null fields, and
  unique registered keys. Registry v4 distinguishes
  `builder`/`passthrough`/`adapter`/`projection`/`missing`/
  `semantic_mismatch`; no current field remains marked missing or semantically
  mismatched, while six receipt/provenance blockers remain explicit. This is
  structural software evidence, not a canonical child freeze.
- Phase 0 now binds an internally replayable dirty source snapshot, a selected
  observed-environment identity, mechanically derived config/scope/component
  artifacts, normalized venue-local map rows, and actual file-byte hashes. Git
  objects, import hooks/`sys.path`, unsigned provenance labels/receipts, and
  external map review strings are not authenticated. Required-dataset source
  labels now reject obvious venue/root swaps, identical or overlapping physical
  venue roots are refused, external maps remain diagnostic/untrusted, and
  `root_lineage.json` preserves the missing canonical-lineage blocker. The root snapshot is
  explicitly `BYTE_SNAPSHOT_ONLY`: it does not prove registered scope, earliest
  history, Phase-0 semantics, or S01 readiness. The diagnostic stage byte-binding
  utility lets S00 bind only config/source/environment before S01 exists; S01
  then adds root/PIT/map/population identities and starts the downstream run
  identity. Artifact schema, row count, key hash, and outcome blindness remain
  explicitly unverified caller declarations. Construction requires the config
  identity to equal the exact repository-derived canonical object; later
  archival byte verification intentionally does not reinterpret old declarations
  through the mutable current schema registry or config factories. That generic
  byte utility does not validate stage semantics or transitive provenance. It
  remains a diagnostic primitive, not a canonical S00-S04 semantic chain; no
  real root has entered either the byte or semantic stage-receipt path.
- A full-window outcome-blind Phase-0 inventory ran on the local workstation
  roots and wrote diagnostic bundle
  `strategy-overhaul-phase0-bccefdfc38ae9fda3c17` (`receipt.json` SHA-256
  `ed5fb3687280db691dcda5e32e00005a8dd48dd2fb403c2f48fe6cb69a81bb03`).
  It exited 2 with `NOT_READY`; an immediate strict re-execution returned
  successfully before the reporting wrapper failed to serialize immutable
  mappings. The run read no OHLCV/RMOM numeric values or outcomes and authorized
  no S01, outcome, deployment, or real-money action. Binance lacks the seven
  daily manifest/kline partitions 2026-07-03 through 2026-07-09, all 471,321
  Binance manifest pairs lack persisted observation provenance, and Binance RMOM
  has no `is_provisional`. Bybit covers the registered window but 360 kline rows
  have no source label. The run also exposed an overly narrow Bybit source-label
  sanity registry: `bybit_public_trades` and `bybit_rest` are venue-compatible
  production labels but were classified incompatible. That software defect is
  prospective; it does not rewrite this receipt. Both roots lack canonical
  authenticated root-lineage receipts, the auto map was therefore bundled but
  not consumed, and S02 config parity was `UNWIRED` in this exact run. No real
  S02 feature tape, S03 entry artifact, S04 path-label artifact, or outcome
  analysis has run.
- After that receipt was verified, the narrow canonical Binance daily-tail
  builder repaired 2026-07-03..09 on the local root: 5,628 archive jobs,
  129,088 appended hourly rows, 245 recorded 404s, zero hard failures, and a
  coverage-derived 593,757-row manifest with non-null
  `binance_vision_archive` source/membership provenance. A post-build key/schema
  audit found all seven required date directories, zero duplicate
  `(symbol,date,url)` keys, and zero null source labels. This intentionally
  changed the root after the `NOT_READY` receipt; that receipt is historical and
  a new Phase-0 identity is required for current-root evidence.
- Prospectively, current source now derives S02 config parity as `WIRED` for all
  11 targets from consumer-owned validators, rather than a handwritten checked
  set. Canonical sorted source/expected-population JSONL and a strict receipt
  bind config/root/PIT/full manifest-pair/map identities; only the full
  reconstructing verifier can construct the object accepted by either S02
  builder. A separate Parquet/Arrow semantic verifier checks the current
  registry, registered scope, config exclusions, exact S02 population and LONG
  ages, selected S02-S04 invariants, and transitive parent identities. These are
  synthetic implementation results only. They do not rewrite the historical
  `UNWIRED` receipt, prove supplied root/PIT completeness or authenticity,
  authenticate RMOM/LONG sidecar provenance, instantiate a real stage chain, or
  authorize outcomes, deployment, or real money. The population and semantic
  paths remain all-in-memory and the three population files are installed
  atomically per file rather than transactionally as one directory bundle; the
  big-PC run path therefore still needs memory/atomicity validation.

## Next actions

1. Let both sleeves accrue a post-fix forward sample; reconcile after meaningful
   fills or any VPS/data change.
2. On the larger machine after 2026-07-12, refresh/verify both full-PIT roots and
   run the frozen tail-survival matrix. A pass only authorizes a new shadow
   review; it does not change the live profile.
3. Build and audit granular datasets before running the adverse-state study.
   Do not infer missing sub-hour data from 1h bars.
4. Preserve the verified local `NOT_READY` Phase-0 bundle above as diagnostic
   evidence; do not overwrite or promote it. Capture the prospective source-label
   and now-`WIRED` consumer implementation in a new identity, then use
   `scripts/ops.sh overhaul-plan`
   for the shallow preflight and `scripts/ops.sh overhaul-phase0` for a new
   content-addressed inventory after the roots are refreshed. The remaining
   seven-day Binance gap is 2026-07-03..09. No big-PC Phase-0 bundle has run.
   Big-PC location does not make a receipt authoritative; its artifacts must
   pass the same internal re-execution checks, while upstream authenticity and
   canonical root lineage remain separate evidence requirements.
   Non-executable finite child templates and the proposed v4 six-artifact schema
   registry exist, but the mismatch ledger above remains blocking, so canonical
   child contracts/manifests remain absent. Refresh the roots and produce a
   semantically verified S00 evidence bundle before binding the
   population/config/RMOM-source
   identities or running any real tape or label stage.

## Canonical references

- `docs/operations.md` — operator commands.
- `docs/promoted_trading_logic.md` — active profile/runtime contract.
- `docs/research_summary.md` — durable evidence and decisions.
- `docs/data_roots.md` and `docs/pit_gate.md` — data/PIT contracts.
- `docs/preregistration/INDEX.md` — active experiments and closed arcs.
