# Research Program State

Last updated: 2026-07-14.

This is a descriptive live operating page, not research policy. Durable
research decisions are in
`docs/research_summary.md`; dated experiment anchors are indexed in
`docs/preregistration/INDEX.md`.

## Operational headline

| Sleeve | Mode | Current state |
| --- | --- | --- |
| `continuous_ensemble_v2` | Bybit demo + paper | Maintenance-stopped; target-only unit installed but not started |
| `LongV11aDivWeekendVol` | Bybit demo + paper | Maintenance-stopped; target-only unit installed but not started |
| Shared account execution | demo + paper | Demo owner stopped after verified-flat V6 failure; V4/V5/V6 evidence retained; capture marker enabled; paper never started |

- Mainnet is not enabled. Changing that requires an explicit owner instruction
  and new evidence.
- Flat maintenance began at `2026-07-13T22:53:17Z`. Immediately before the
  stop, the VPS was clean at `5f6d9986d935`, the demo key was order-capable,
  and Bybit reported zero active positions, regular orders, and conditional
  orders. Every `liquidity-migration-*` unit was then stopped. The post-stop
  venue query was still flat, and the before/after unit inventories plus
  checksums are retained under
  `/var/lib/liquidity-migration/cutover-evidence/20260713T225317Z`.
- The last retained operator record says staged topology from branch
  `codex/account-execution-cutover` advanced through commit `b82a378cfcf0` and
  passed 142 Linux smoke tests. It installed the two owner units and removed the
  retired Bybit risk and combined-book reporter units without starting any unit
  or creating a capture/deploy marker. The host-only evidence is not stored in
  this worktree, so `b82a378cfcf0` is not independently proven as the VPS's
  current checkout; re-stage and verify the exact candidate before V7. This is
  maintenance staging, not an accepted full deployment.
- The guarded all-sleeve reset completed at `2026-07-13T23:43:14Z`. It re-proved
  venue flatness, archived 12 legacy projections/roots plus preserved risk
  state to a verified 335-MB archive with SHA-256
  `07e76e35e688fb6f20e17c78ea9bc8489144c852f4c99fcb9964d887c06c6d6a`,
  rebuilt compatibility projections from preserved canonical journals, and
  created six fresh empty demo/paper account, inbox, and capture roots. Every
  unit was inactive before reset and remained inactive afterward.
- A first 20-USDT rule-probe ceiling failed before order submission because
  current BTC minimum quantity exceeded it. The failure is retained rather
  than hidden. A second flat-account 200-USDT feasibility probe passed with
  observed minima `BTCUSDT=62.1029`, `ETHUSDT=17.6703`, and `BUSDT=5.05579`
  USDT and no residual order/position. That invalidated the original $30 plan.
  Static inspection then closed the $80 v2 before startup because BTC
  quantity-step rounding erased its executable buffer. V3 fixed $160 and a
  quantization-safe 2.5-times-minimum preflight, but its first clock receipt
  failed the fixed 50-ms error ceiling. Persistent diagnostics showed the
  official demo endpoint path itself is roughly 169 ms RTT, so no blind retry
  was attempted. V4 retained $160 and registered one preconnected session with
  a disclosed 100-ms worst-case error ceiling. Its fresh schema-v2 clock
  receipt passed with an 84.805-ms maximum midpoint-error estimate.
- The committed account-execution overhaul has one append-only account
  kernel, atomic cross-sleeve target aggregation/risk, deterministic scheduling
  and fault injection, sequence-aware L2 capture, a market-order execution twin,
  target-only LONG/CONTINUOUS/hedge/risk adapters, and fail-closed paper/demo
  owner launchers. Component lifecycle clocks and protection now start from
  attributable confirmed fills, not accepted targets or decision prices;
  account/symbol reduction P&L is counted once per canonical batch. Telegram
  separates venue position truth from local reconstruction and labels
  L2-midpoint P&L as an estimate. The runtime now requires one explicit
  `demo|paper` environment, and the unreachable sleeve-direct execution modules
  are deleted. The demo owner also recovers strict Bybit funding-settlement rows,
  and a new owner-serialized read-only receipt can reconcile stopped-journal
  target/order/fill lineage, fees, closed P&L, funding, and pre/post flatness.
  Commit `b82a378cfcf0` is published and was recorded as staged on the VPS. Its
  bounded calibration driver, retained-stop transition, and independent public
  clock-offset receipt passed 2,428 local tests and 142 remote Linux smoke
  tests. The demo owner started alone, created the route, stayed healthy/flat,
  and captured all three L2 books.
  V4 then emitted one canonical BTC target and received a real `0.002 BTC`
  fill. It failed immediately rather than accepting a result: REST position
  truth temporarily preceded private-fill journal propagation, and the REST
  ACK/private-fill race exposed a second-observer immutable-ACK collision. A
  separate canonical recovery-zero target closed the position; final evidence
  proved local and venue flatness plus no open orders, and the owner was
  stopped. V4 is spent and cannot be resumed or counted. Commit `c113d78014e0`
  then passed 2,424 local tests, repository-wide Ruff, scoped mypy, and 142
  remote Linux smoke tests. A second guarded reset archived the failed V4 epoch
  to a 6.0-MB archive with SHA-256
  `56cb3787d12b9c6e72bb684e59b37e3c6fbdc62fded8db32612da293bf629f7c`
  and created another six fresh roots. V5's fresh clock receipt passed with an
  84.668-ms maximum midpoint-error estimate. Its first BTC open and close each
  filled `0.002 BTC`; the journal recorded both fees and a provisional
  `-0.13755984 USDT` reduction P&L. V5 still aborted: after the zero target
  removed the component owner but before its reduce-only fill updated the
  position, native-protection sync misclassified the canonical in-flight close
  as ownerless. Final self-hashed evidence proved local/venue flatness and no
  open orders, and the owner was stopped. V5 is spent. Commit `b82a378cfcf0`
  then passed 2,428 local tests, repository-wide Ruff, scoped mypy, and 142
  remote Linux smoke tests. A third guarded reset archived V5 under SHA-256
  `bdcb6399c255863eef648b7424ca9121ef46c49726a1b98dff026d3d74969c0f`.
  V6's fresh clock gate passed at 84.664-ms maximum error. Its retained-stop
  repair worked across four real closes, but event 9 opened `+0.08 ETH` and the
  next between-step gate failed because health stayed one journal sequence
  behind reconciliation's newer snapshot. The exact V6 error was
  `health=201, journal=202`; the producer stopped without lowering the gate.
  One separately labelled canonical recovery command closed ETH. Self-hashed
  evidence proved local/venue flatness, zero orders, and a genuine stopped
  health/journal match at sequence 367. V6 is spent. Paper and ordinary
  producers were never started. Prospective V7 republishes owner health after
  every journal-head change while reusing the last wallet snapshot for
  journal-only refreshes; exact-head validation and every numerical gate remain
  unchanged. V7 still requires full validation, an exact staged commit, and a
  new archived/reset epoch before any V7 target.
- Historical CONTINUOUS market orders and LONG standard, bounded sniper, and
  provisional triggers now consume risk/execution feedback through a persistent
  common-kernel session before later decisions. Historical, paper, and demo now
  share an ordered hash-chained event-clock boundary and callback time for the
  registered active LONG/CONT natural market-order paths. This is not literal
  parity for every timer or mode: arrival/selection adapters are not yet full
  strategy parity, and hedge/RMOM/liveness, CONTINUOUS adverse-limit mode, and
  LONG waits beyond 24 hours remain outside that runtime claim or in post-run
  replay.
- The cutover acceptance gate is open: fresh rules and failed V4/V5/V6 evidence
  exist, but the current local follow-on has not been frozen into a clean exact
  candidate or passed the non-contacting `candidate-ci` gate. V7 and its
  partial-fill gate have not run; there is no passing calibration
  target/order/fill/P&L tape, second full registered-output natural-holdout reset, owner-first
  readiness pair, 120-hour natural LONG/CONT tape, periodic clock series,
  venue-accounting/final-flatness receipt, stopped-source seal, offline
  replay/parity/sufficiency/drift result, fresh-deploy epoch, or full
  historical/paper/demo comparison. The stopped and fresh epoch constructors
  are integrity mechanisms in source, not evidence that either epoch exists.
  The earlier generic stopped-tree provenance implementation blocker is closed
  in source: target-replay manifest v2, event parity v3, captured-account replay
  v3, comparison scope v3, kernel receipt v4, natural sufficiency v3, and the
  authority aggregate v4 now form a source-reopening path/hash dependency chain
  with derived-output disjointness checks. Their local timestamps enforce
  declared internal chronology; they are not authenticated wall-clock proof.
  The target manifest assigns its completion time after replay construction,
  while dependency hashes and source reopening carry the causal provenance.
  None of these validators is a run artifact. The paper owner refuses startup
  without a passing calibration. Full deploy requires a short-lived,
  mode-`0600` `account-execution-deploy-ready` authorization receipt that binds
  the exact clean commit and complete source-reopened gate set. No such receipt
  has been issued.
- The owner reports that the Strategy Overhaul master plan is currently running
  on the big PC for alpha research. That workload is separate from this
  execution cutover; no big-PC result or artifact has been ingested or judged in
  this workspace, and it grants no deployment authority.
- The 2026-07-12 flatness snapshots, reset archives, XRP minimum-order probe,
  hedge smoke, and commit `6f2bde773` statements below are historical receipts.
  They do not describe the currently audited VPS checkout or satisfy this new
  account-owner cutover.
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
- Removed from the future runtime: the demo-only adverse-limit add-on. Disabled:
  fixed/server stop, left-decile, stop-approach, failed-fade, breakeven,
  re-entry cooldown, portfolio heat overlay, account drawdown overlay.

The adverse-limit config, placement, cleanup, notification, CLI, launcher and
unit wiring are absent from the future target-only runtime. Historical links and
ledger rows remain readable for attribution. This cleanup deletion is safe only
behind the account-owner startup gates: a new journal requires venue-flat
positions and zero regular or conditional orders; restarts accept only exact
journal-owned working orders or verified journal-backed native protection.
The TP12 and max-hold runtime now anchor to confirmed fill VWAP/time. This is an
intentional forward-runtime semantic correction, not a byte-parity refactor;
deployment requires a new archived/reset demo-paper epoch and fresh acceptance
evidence.

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

## Historical deployed safety release

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
- The current local `scripts/ops.sh` surface covers status, structural
  account-journal parity, equity, reset, research plans, tests, and checked
  deploy. Its obsolete sleeve-projection reconcile routes are removed; the
  deployed release remains unchanged until an authorized cutover.
- Continuous hedge target reconciliation is five-minute and fail-loud on stale
  non-flat state; the source tape is self-describing and hash-bound to its
  official current-object summary.

## Historical clean ledger boundary

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

1. Finish the account-owner candidate and clean exact-candidate Linux validation
   first. Then, still without deploying, run the registered flat maintenance
   sequence: archive/reset the six roots for V7, run the fresh V7 training
   epoch, materialize its immutable archive **before** the second full
   registered-output natural-holdout reset, start the paper owner alone and stop it cleanly, then
   start the demo owner alone before any producer and collect the fixed 120-hour
   natural LONG/CONT demo tapes. After T1, converge flat, stop the fleet,
   capture venue accounting/funding and final flatness, and seal the stopped
   source namespace before any offline historical/paper replay, structural
   parity, sufficiency, or execution-twin drift analysis.
   Write every derived analysis artifact to a separate root outside all 11
   sealed paths; create the ten fresh deployment roots only after every analysis
   gate passes, and keep those roots outside both namespaces. Do not substitute
   the retired sleeve-projection reconciler or a V7 training sample for the
   natural tape.
2. Let the owner-run Strategy Overhaul master plan continue on the big PC without
   coupling it to this VPS cutover. Ingest its immutable receipts only after the
   run finishes; a positive research result still does not change the live
   profile or authorize deployment.
3. Keep `codex/account-execution-cutover` off `main` until the registered outcome
   supports the cutover and the exact short-lived authorization exists. A
   fast-forward of `main` is the checked deployment boundary, not cleanup.
   Delete the cutover branch only after that deployment verifies; delete any
   other branch only after proving it has no unique commit or dirty worktree.
   Use `docs/account_execution_completion_handoff.md` for a bounded continuation
   across agent sessions. Defer the aggressive cleanup described in
   `docs/repository_cleanup_handoff.md` until the cutover is verified deployed
   or explicitly closed; run it from a separate clean branch/worktree rather
   than changing the frozen evidence candidate.
4. Build and audit granular datasets before running the adverse-state study.
   Do not infer missing sub-hour data from 1h bars.
5. Preserve the verified local `NOT_READY` Phase-0 bundle above as diagnostic
   evidence; do not overwrite or promote it. Capture the prospective source-label
   and now-`WIRED` consumer implementation in a new identity, then use
   `scripts/ops.sh overhaul-plan`
   for the shallow preflight and `scripts/ops.sh overhaul-phase0` for a new
   content-addressed inventory after the roots are refreshed. The remaining
   seven-day Binance gap is 2026-07-03..09. No new big-PC result has been
   ingested here. Big-PC location does not make a receipt authoritative; its artifacts must
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
- `docs/account_execution_completion_handoff.md` — bounded continuation prompt
  and efficient candidate-validation cadence.
- `docs/repository_cleanup_handoff.md` — deferred evidence-driven deletion
  campaign.
