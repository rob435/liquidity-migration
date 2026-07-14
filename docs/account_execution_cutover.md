# Account execution cutover

Status: implemented locally but **not deployed and not deployment-ready**.
Demo and deterministic paper only. This runbook does not grant real-money
authority.

## Evidence status

The local cutover has one strict `execution_environment=demo|paper` route.
There is no `SUBMIT_ORDERS`/`PAPER_MODE` fallback. LONG and CONTINUOUS import a
public-only market-data plane; the retired direct executor, exit/report
companions, sleeve router, order executor, and Bybit trade-router/WebSocket
submission clients are deleted. Only the account owner retains venue-mutation
authority.

The account sequencer, atomic journal, aggregation/risk, raw L2 capture,
paper/demo owners, native protection, reconciliation, target-only routes, and
desired-target convergence have local automated coverage. Accepted targets
remain `target_pending` until fills reconstruct the desired position;
terminal rejects/cancels are retried with bounded, restart-derived command
identities. That is software evidence, not host or venue evidence.

The account journal builds transactions on an isolated prospective state and
publishes its in-process cache only after the durable transaction rename. The
private execution consumer, reconciliation loop, and Telegram reader therefore
cannot observe a partially-applied or failed transaction. Native-protection
state and venue mutations are separately serialized by one reentrant manager
lock. These are concurrency invariants under automated fault tests, not proof
of host or venue behavior.

The 2026-07-13 audit first found the old fleet active at clean commit
`5f6d9986d935`, with both account owners absent. The demo key permission check
passed and a read-only venue query was flat. Flat maintenance then began at
`2026-07-13T22:53:17Z`: every project unit was stopped, a second venue query
confirmed zero positions and zero regular/conditional orders, and the unit plus
flatness evidence was retained under the host's mode-restricted cutover-evidence
directory.

Staged topology installation of clean commit `4950b4cb0520` passed 142 Linux
smoke tests, installed both inactive owner units, and removed the retired risk
and combined-book reporter units. It did not start a process or create either
cutover marker. The guarded reset then re-proved venue flatness, archived 12
legacy roots/projections to the verified archive whose SHA-256 is
`07e76e35e688fb6f20e17c78ea9bc8489144c852f4c99fcb9964d887c06c6d6a`,
rebuilt compatibility projections from canonical journals, and created all six
fresh empty account/inbox/capture roots. No service was active before or after
the reset.

The first 20-USDT rule-probe ceiling failed before order submission because
current `BTCUSDT` structural minimum quantity exceeded it. The failed attempt
is retained. A second, flat-account 200-USDT feasibility probe passed and
produced the self-hashed three-symbol rule receipt. Its largest observed minimum
is 62.1029 USDT, so the original 30-USDT calibration plan is closed as
infeasible. Static inspection then closed the 80-USDT v2 before startup because
BTC quantity-step rounding erased its executable buffer. Prospective v3 fixes
160 USDT and enforces a quantization-safe 2.5-times-minimum bound before any
calibration target/fill outcome. Route/risk/symbol files now exist. The capture
marker remains absent, both owners remain inactive, and every captured-tape
gate remains open. This is a valid stopped/reset staging boundary, not
deployment readiness or final-flatness evidence.

The full acceptance gate remains open, and no deploy-authorization assessment
or receipt has been issued:

- the credentialed `api-demo` rule gate passed only for the three calibration
  symbols; candidate coverage for actual LONG/CONTINUOUS strategy tapes remains
  open;
- standard and bounded sniper-retrace LONG historical execution submit
  chronological same-timestamp
  targets to a persistent account kernel and consumes risk/execution feedback
  before the next daily decision. It evaluates capacity at the actual delayed
  or retrace entry boundary, after hourly exits already knowable by then; this
  intentionally fixes the legacy daily-scan and signal-time reservation ordering
  rather than claiming byte-equivalence. Provisional hourly triggers also feed
  the session chronologically, batch at equal timestamps, and consume account
  rejection before confirmation. Sniper waits longer than 24 hours remain
  post-run account replay until their trigger windows enter the same online
  decision path;
- CONTINUOUS market-order historical execution now advances positions one bar
  at a time and submits same-timestamp targets to a persistent account kernel;
  account rejection/acceptance feeds back before the next decision. The
  adverse-limit research mode remains a chronological strategy simulation with
  post-run account replay until a limit-order execution port exists;
- historical replay currently uses synthetic one-level bar books, while paper
  uses captured Bybit L2 and demo uses the venue. Actual market-tape parity is
  therefore false;
- historical, paper, and demo now dispatch scheduling inputs through the same
  ordered, hash-chained event clock. The recorded event time is injected into
  the strategy callback, so the callback no longer rereads wall time. Arrival
  adapters and some historical signal-selection loops remain distinct, so this
  is deterministic scheduling infrastructure, not full strategy parity;
- no captured actual LONG/CONT target tape has yet passed decision key,
  rejection key, target quantity, and order/position hash comparison across
  all three environments;
- reconstructed fill P&L has not yet passed the new immutable venue-accounting
  receipt. The demo owner now ingests strict `SETTLEMENT` rows as canonical
  funding P&L, and the final read-only capture can bind exact `TRADE`,
  closed-PnL, and funding rows to the stopped journal plus pre/post flatness.
  No actual receipt exists yet. Missing execution fees remain explicitly
  pending, and same-symbol venue fills are netted, so exact component P&L is
  also pending unless a prospective allocation policy can be shown to be
  identifiable from canonical orders and executions;
- no demo tape has met the preregistered execution-twin sample floors, no clock
  offset receipt has supported one-way latency estimates, and no calibration
  receipt has passed. Paper startup therefore remains intentionally blocked.

Do not issue the deploy-ready authorization by interpreting
`canonical_common_kernel_parity` as full strategy parity. Reports also carry
`cross_environment_strategy_parity=false`, `venue_rule_parity=false`, and more
specific evidence labels for this reason.

Once actual captured runs exist, write the structural journal receipt with:

```bash
scripts/ops.sh account-parity \
  --environment historical=/path/to/historical-account-root \
  --environment paper=/path/to/paper-account-root \
  --environment demo=/path/to/demo-account-root \
  --quantity-tolerance 1e-12 \
  --output /path/to/account-kernel-parity.json
```

The command refuses empty journals, exits nonzero on mismatch, binds the
receipt to a normalized SHA-256 for each source, and writes a mode-`0600`,
self-hashed receipt atomically. It intentionally records
`full_cross_environment_acceptance_passed=false`: journal structure alone cannot
prove actual market-tape provenance, full strategy parity, fresh demo rules,
credentialed demo execution, or P&L agreement.

## Staged topology installation

After entering the flat maintenance window and stopping the currently installed
fleet as described in the ownership switch below, install the current checkout:

```bash
INSTALL_PREFLIGHT_ONLY=1 \
  EXPECTED_COMMIT="$(git rev-parse HEAD)" \
  scripts/deploy_vps_live.sh
```

GitHub Actions exposes the same phase as the manual `install-preflight` mode.
For provider-console recovery, use:

```bash
INSTALL_PREFLIGHT_ONLY=1 \
  EXPECTED_COMMIT="$(git rev-parse HEAD)" \
  scripts/vps_console_recover_and_deploy.sh
```

This phase checks out the requested commit, installs dependencies when needed,
runs the checked smoke tests, installs the current systemd manifest, and stops
and removes unknown historical `liquidity-migration-*` units and their drop-ins.
That last action is an intentional host mutation: it prevents a retired order
mutator from surviving the cutover preparation.

The phase does **not** inspect, require, create, or remove either
`account-execution-capture-enabled` or `account-execution-deploy-ready`; it does
not read the Bybit or account-owner route environments; and it does not enable,
start, or restart any current owner,
producer, collector, or other `liquidity-migration` service/timer. Current units
that were already running remain in their prior runtime state. Do not run it over
an active fleet: the checkout itself changes scripts/config under those
processes. The maintenance stop and flatness checks below are therefore
prerequisites, not cleanup left for after installation. The scripts enforce the
first boundary before checkout: any active, activating, deactivating, or
reloading `liquidity-migration-*` unit makes install preflight refuse.
`install-preflight-ok` means only that the software and unit topology are
installed. It is neither acceptance evidence nor authority to trade.

## Preconditions

1. Bybit demo is flat and has no open or conditional orders. The demo owner
   independently enforces this for an empty/new account journal before it can
   claim an intent: startup reads Bybit's all-kinds open-order view and an
   explicit `StopOrder` view, deduplicates by `orderId`/`orderLinkId`, and
   aborts on any row or either query failure. It does not cancel or adopt the
   order. On a non-empty restart, an open order is accepted only when it matches
   a still-working kernel command by client/venue id or the journal-backed
   native-protection verifier identifies the exchange-generated stop. REST
   position reconciliation must also be healthy before startup continues.
2. Archive the legacy LONG, CONTINUOUS, hedge, and risk ledgers. Do not delete
   the archive; those rows remain historical receipts, not operational truth.
3. Generate fresh empty demo and paper account roots, inboxes and raw-capture
   roots. Never seed the account kernel from stale `open` rows. The account
   journal schema is versioned and there is deliberately no automatic import of
   legacy sleeve ledgers. All six demo/paper account, inbox and capture paths
   must be absolute and pairwise disjoint; equality, nesting and canonical
   `..` aliases fail deploy, recovery and verification.
4. Supply a demo-verified rule row for every candidate/held symbol, including
   `tick_size`, `qty_step`, `min_qty`, `min_notional`, `max_order_qty`, and
   `max_leverage`. Public/mainnet instrument metadata is not accepted as demo
   verification. The owner also rejects receipts older than seven days by
   default (`MAX_DEMO_RULE_AGE_HOURS`).
5. Verify the configured demo API key is not read-only and has order-submit
   permission. The account owner checks this at startup; sleeves never receive
   the key.
6. Supply explicit absolute account risk limits and an explicit
   `DISASTER_STOP_FRACTION`. There is deliberately no hidden default.
7. The canonical strategy-state projection is implemented for LONG,
   CONTINUOUS, and hedge target routes. Complete captured-tape historical,
   paper, and demo comparison before issuing deploy authorization.

## Demo rule probe

Run this only during the flat maintenance window, with the account owner and
every pre-cutover order mutator stopped. It acquires the same owner lease,
refuses a non-flat account, reads structural metadata through `api-demo`,
binary-searches the smallest accepted PostOnly quantity-step notional, cancels every accepted
probe, and performs a final flatness audit:

```bash
python3 scripts/probe_bybit_demo_rules.py \
  --symbols BUSDT,BTCUSDT,ETHUSDT \
  --account-root data/bybit-account-execution \
  --max-probe-notional-usdt 200 \
  --leverage 10 \
  --output /etc/liquidity-migration/account-execution/demo-rules.json \
  --confirm-demo-probe
```

The schema-v2 receipt is self-hashed, written mode `0600`, atomically replaced,
and fsynced before it can gate owner startup; the owner rejects unsigned or
altered receipts. The resulting `min_notional` is venue-derived, not the retired local `$25`
resize floor. It is the smallest accepted quantity step at the recorded probe
price, so it conservatively upper-bounds the hidden threshold by at most one
quantity step. Unknown rejects, transport failures, uncancelled orders, stale
receipts, or any residual position fail the gate rather than being interpreted
as a minimum. Bybit documents that demo uses `api-demo.bybit.com`, supports
order create/cancel, and has an incomplete API surface; the probe therefore
uses actual order create/cancel rather than assuming the optional pre-check
endpoint is available: <https://bybit-exchange.github.io/docs/v5/demo>.

## Ownership switch

The switch must be one maintenance transaction, not a rolling overlap:

1. Stop every strategy publisher, the hedge timer, the account owner, and any
   pre-cutover mutator or reporter still installed on the host. Repository
   manifests no longer install the old risk/order-repair or combined-book
   reporter units; a host upgraded from an older checkout must prove those
   processes absent rather than merely disabled for one boot.
2. Run the staged topology installation. It removes unknown historical units
   and installs the owner units needed by the reset and later checked deploy,
   without starting anything. Recheck that every current owner/producer and
   every pre-cutover authority remains inactive.
3. Confirm Bybit demo is still flat and manually cancel any remaining regular
   or conditional orders. The owner is a fail-closed verifier, not a cleanup
   mechanism: it aborts instead of cancelling an unowned row.
4. Write `/etc/liquidity-migration/account-execution.env` with one durable demo
   account root, target inbox and raw-capture root shared by LONG, CONTINUOUS,
   hedge, the liveness checker and the owner:

   ```bash
   ACCOUNT_EXECUTION_KERNEL_REQUIRED=1
   ACCOUNT_EXECUTION_ROOT=/opt/liquidity-migration/data/bybit-account-execution
   ACCOUNT_INTENT_INBOX_ROOT=/opt/liquidity-migration/data/bybit-account-intents
   ACCOUNT_CAPTURE_ROOT=/opt/liquidity-migration/data/bybit-account-market-capture
   ACCOUNT_SYMBOLS_FILE=/etc/liquidity-migration/account-execution/symbols.txt
   ACCOUNT_DEMO_RULES_FILE=/etc/liquidity-migration/account-execution/demo-rules.json
   ACCOUNT_RISK_POLICY_FILE=/etc/liquidity-migration/account-execution/risk-policy.json
   DISASTER_STOP_FRACTION=REPLACE_WITH_EXPLICIT_FRACTION
   ```

   The placeholder is intentionally invalid; replace it with an owner-chosen
   fraction strictly between zero and one before evidence collection.

   The target launchers require the account root, inbox, and capture-enabled
   marker when the kernel latch is set. The owner additionally requires the
   capture root; neither surface falls back to direct venue mutation.
5. Write `/etc/liquidity-migration/account-paper-execution.env` with the distinct
   paper account, inbox, and capture roots shown in the deterministic paper
   section below. Do not alias or nest any demo and paper route.
6. Complete the credentialed demo-rule probe, then review and execute the
   flat-account archive/reset while the fleet remains stopped:

   ```bash
   scripts/reset_demo_paper_ledgers.sh --sleeves all --label account-cutover
   scripts/reset_demo_paper_ledgers.sh \
     --execute --sleeves all --label account-cutover
   ```

   The staged installation is what makes the new owner units available for the
   reset's route verification; the reset must not create either cutover marker
   or start units that were inactive on entry. Retain the archive and SHA-256
   sidecar, and prove all six new account/inbox/capture roots are empty.
7. Create
   `/etc/liquidity-migration/account-execution-capture-enabled` only for the
   bounded demo/paper evidence window. This marker authorizes data collection;
   it makes no parity, calibration, P&L, or deployment claim. The retired
   ambiguous `account-execution-ready` filename has no enabling effect and must
   not exist.
8. Start **only** `liquidity-migration-account-execution.service`. Verify its
   owner lease, API-key permission check, raw L2 capture, private execution
   stream, REST position reconciliation, strict transaction-log funding
   reconciliation, verified rules, native protection, owner-health artifact,
   convergence health, and canonical summary before starting a demo producer.
9. Start the target-only demo sleeves needed for the registered tape. They must
   report queued targets, never self-authored fills/P&L or venue mutation. Keep
   paper owner/producers stopped until demo calibration passes.
10. Capture the minimum registered demo target/order/ack/fill/P&L and raw-book
    sample, calibrate the twin, and install the self-hashed passing calibration
    receipt in the paper route. Stop on a failed sample gate; do not lower a
    threshold after inspecting the result.

    Natural LONG/CONTINUOUS events remain required for their strategy-parity
    comparison, but they need not be abused to manufacture the execution-twin
    sample count. The prospective bounded driver in
    `docs/preregistration/account_execution_calibration_v3_2026_07_13.md` publishes
    one tiny target at a time through the same owner and event clock, holds no
    credentials, and explicitly cannot satisfy LONG/CONTINUOUS parity:

    ```bash
    scripts/ops.sh demo-calibration --execute \
      --account-root /opt/liquidity-migration/data/bybit-account-execution \
      --inbox-root /opt/liquidity-migration/data/bybit-account-intents \
      --demo-rules-file /etc/liquidity-migration/account-execution/demo-rules.json \
      --event-tape /var/lib/liquidity-migration/cutover-evidence/demo-calibration-events.jsonl \
      --output /var/lib/liquidity-migration/cutover-evidence/demo-calibration-run.json \
      --expected-commit "$(git rev-parse HEAD)" \
      --plan-id demo-calibration-20260713-v3
    ```

    Add the preregistered `--funding-symbol BTCUSDT` and an explicit future
    `--funding-close-not-before-ms` only when that timestamp was recorded from
    the venue before the hold opened. Never improvise it after inspecting the
    funding result.
11. Start the paper owner first, verify fresh bound owner health, then start the
    corresponding paper producers. Collect paper tapes and construct the
    historical replay from the same recorded event clock and declared market
    inputs.
12. Bring both target sets to canonical zero, verify venue/demo flatness, stop
    all producers, and retain owner-first systemd/journal evidence. Let the demo
    owner complete one final position/funding reconciliation and write fresh
    health, then stop both owners. Within five minutes, capture the exact demo
    accounting epoch while the shared owner lease proves no journal writer is
    running:

    ```bash
    EPOCH_START_MS="$(python3 -c 'from liquidity_migration.account_kernel import read_account_journal; import sys; e=read_account_journal(sys.argv[1], verify=True); print(e[0].wall_ts_ns // 1000000)' /opt/liquidity-migration/data/bybit-account-execution)"
    scripts/ops.sh venue-accounting \
      --account-root /opt/liquidity-migration/data/bybit-account-execution \
      --account-id bybit-demo-unified \
      --start-time-ms "$EPOCH_START_MS" \
      --output /etc/liquidity-migration/account-execution/venue-accounting.json
    ```

    The prospectively registered defaults require at least two `TRADE` rows,
    one closed-PnL row, and one `SETTLEMENT` row. This deliberately requires the
    bounded demo sample to span an actual funding settlement; do not lower the
    floor after observing a zero-funding window. The fresh epoch must remain at
    most seven days because that is the venue API's explicit query boundary.
    The command is read-only at the
    venue, refuses mainnet credentials, replays the stopped canonical journal,
    and exits nonzero unless exact target/order/fill lineage, observed fees,
    fill P&L, funding, and local/venue pre/post flatness all agree. Reconcile the
    event-tape hashes, historical/paper/demo journal structure, and documented
    scheduler limitations separately. Remove
    `account-execution-capture-enabled` and stop producers immediately on any
    abort condition.
13. Only after every registered gate passes, create an open assessment template
    bound to the already-staged full commit. Machine validation covers the
    schema-v2 demo-rule receipt, fresh demo/paper owner-health artifacts bound
    to their journal heads, passing execution-twin calibration receipt, and
    self-hashed structural kernel-parity and venue-accounting receipts. The same
    venue receipt machine-gates actual demo target/order/fill/P&L evidence,
    closed-PnL/funding agreement, and final flatness. For topology/reset,
    demo/paper owner-first start ordering, and event-clock comparison, create
    reviewed-evidence wrappers over the exact immutable source files. Those
    wrappers bind bytes and an operator claim; they do not pretend the
    underlying judgment was automated.

    ```bash
    COMMIT="$(git rev-parse HEAD)"
    scripts/ops.sh cutover-authority template \
      --authorized-commit "$COMMIT" \
      --authorized-by OWNER_REVIEW_ID \
      --output /etc/liquidity-migration/account-execution-cutover-assessment.json

    # Repeat review-evidence for each non-machine-validated role in the template.
    scripts/ops.sh cutover-authority review-evidence \
      --role demo_owner_start_sequence \
      --claim 'OWNER_ACTIVE_AND_HEALTHY_BEFORE_ANY_DEMO_PRODUCER_START' \
      --reviewed-by OWNER_REVIEW_ID \
      --source /absolute/path/to/demo-systemd-start-order.log \
      --output /absolute/path/to/demo-owner-start-evidence.json
    ```

    Populate every evidence path and decision. Change a gate from `open` to
    `passed` only after its registered rule passes; do not revise the rule after
    viewing the result. Then issue and verify the short-lived receipt:

    ```bash
    scripts/ops.sh cutover-authority issue \
      --assessment /etc/liquidity-migration/account-execution-cutover-assessment.json \
      --repo-root /opt/liquidity-migration \
      --output /etc/liquidity-migration/account-execution-deploy-ready

    scripts/ops.sh cutover-authority verify \
      --receipt /etc/liquidity-migration/account-execution-deploy-ready \
      --expected-commit "$COMMIT" \
      --repo-root /opt/liquidity-migration
    ```

    Point both `venue_accounting_reconciliation` and
    `venue_flatness_snapshot` evidence entries in the assessment at the
    machine-validated accounting receipt; do not wrap it in an operator
    attestation. Stop both owners only after their final healthy artifacts are
    written, and issue within the registered five-minute owner-health age.
    Prepare the still-open assessment before that final stop so this remains
    operationally achievable without relaxing the gate.

    The mode-`0600` receipt expires within 24 hours and binds the assessment and
    evidence hashes to the host machine-id fingerprint and exact clean commit.
    Its self-hash is not a signature, and the operator remains responsible for
    the non-machine-verifiable decisions. Full deploy, verification, and
    recovery require the capture marker plus this receipt and refuse the
    retired ambiguous marker. They validate authority before checkout, so a
    failed or stale gate cannot silently update the host. With the current
    evidence, leave the deploy-ready receipt absent and do not deploy.

## Deterministic event tape

Historical, paper, and demo scheduling inputs use `StrategyEvent` and
`DeterministicEventClock`. The total order is event time, event phase, source,
source sequence, and canonical event id. Inputs are fsynced to a hash-chained
JSONL tape before the callback runs; duplicates, chain corruption, and backward
time fail closed. A `VirtualClock` advances to the same recorded event time in
replay, and live callbacks receive that time as `now_ms` instead of rereading
ambient wall time.

This establishes one scheduling boundary and replay mechanism. It does not make
different market tapes equal, turn synthetic bar books into L2, or prove that
all historical signal-selection code is the live adapter. Compare event-tape
hashes only when the declared input tape and strategy configuration are also
identical.

## Demo execution-twin calibration

After the demo owner has captured actual data, create the receipt with a fresh,
independently sourced clock-offset receipt:

```bash
scripts/ops.sh clock-offset --execute \
  --output /var/lib/liquidity-migration/cutover-evidence/clock-offset.json

scripts/ops.sh twin-calibrate \
  --account-root /opt/liquidity-migration/data/bybit-account-execution \
  --market-capture-root /opt/liquidity-migration/data/bybit-account-market-capture \
  --account-id bybit-demo-unified \
  --clock-offset-receipt /path/to/clock-offset.json \
  --output /etc/liquidity-migration/account-paper-execution/execution-twin-calibration.json
```

The default preregistered floors are 5,000 clock-adjusted feed observations, 30
targets, 30 commands, 30 request/ack samples, 30 actually filled orders, 10 P&L
events, three symbols, and 95% command-to-captured-book linkage. Entry/response
latency and feed latency must be at least 99% nonnegative after clock correction.
At least 99% of command references must also match the linked, ungapped captured
decision-book midpoint within 0.01 basis point. Rejected or cancelled zero-fill
commands cannot satisfy the filled-order floor.

The receipt binds the verified account journal and every raw capture segment by
SHA-256. It reports feed latency, decision-to-socket delay, request/ack RTT,
clock-adjusted order entry/response, fill response/spacing, multifill and
incomplete-filled-order rates, zero-fill terminal orders, fees, and adverse
slippage. Rejects are not mislabeled as partial fills. Slippage is separated into
the visible decision-book walk and the residual observed after that walk; the
paper twin applies a selected residual quantile after walking captured depth.
The baseline uses `p50`; `p75`/`p95`/`p99` are explicit stress choices.

The demo client preserves [Bybit V5's top-level response `time`](https://bybit-exchange.github.io/docs/v5/order/create-order#response-example)
in the canonical ack metadata path; without that server timestamp the request/ack RTT remains
observable, but one-way order-entry and response estimates are unavailable and
the calibration gate cannot pass. Duplicate-link recovery lookups are excluded
from latency samples: they prove idempotent venue ownership, not create-request
timing. The response-envelope timestamp is an API-server boundary, not proof of
matching-engine entry time.

The clock receipt samples Bybit's unauthenticated `GET /v5/market/time` endpoint
from the VPS and combines it with a required NTP-synchronized local clock. It
selects the five lowest-RTT observations from 21 and refuses an estimated error
above 50 ms or selected RTT above 250 ms. This bounds, but does not eliminate,
the symmetric-path assumption in one-way latency estimates.

Bybit depth is market-by-price, not market-by-order. It cannot identify passive
queue position. The receipt therefore leaves passive queue calibration false
and scopes the twin to market orders with an immutable replay-book assumption.
That limitation is not repairable by inventing a queue parameter.

## Target lifecycle and operator messages

- An accepted target is desired state, not a fill. Strategy projections label
  it `target_pending`; it reserves admission/capacity but is excluded from
  exits, P&L, protection, and open-position reporting until reconstructed fills
  converge.
- A component lifecycle starts only from an attributable first fill. Max hold is
  first-fill time plus the target's duration, and component stop/take-profit
  prices are derived from confirmed fill VWAP. A pre-fill target, partial entry,
  or ambiguous aggregate-only same-symbol fill cannot trigger component
  protection.
- A rejected, cancelled, or partially-filled-cancelled order leaves the latest
  accepted desire durable. The owner retries only the uncovered residual with
  deterministic command ids and bounded exponential backoff. It never sends an
  offsetting order while an older order for the symbol is still working.
- Before executing a restart backlog, an older non-flat request may be archived
  as `superseded` when the final intents from one or more later requests replace
  all of its component keys. This matters when one atomic multi-component entry
  is followed by separate per-component safety flats. A target-flat safety
  transition is never skipped in favor of a later re-entry. Inbox order is a
  locked, durable local arrival sequence, not a producer filename, exchange
  timestamp, or wall-clock sort.
- Each target also carries its local creation revision. The kernel rejects a
  delayed request older than the latest accepted revision for that component,
  so an entry generated before a protection flat cannot arrive late and reopen
  it. Equal-time ambiguity is fail-closed for flat-to-nonzero transitions.
- Strictly proven reducing targets may proceed when entry-only margin/cap
  checks or unrelated books are unhealthy. Entries and increases still require
  fresh account-wide market, capital, rule, reconciliation, and protection
  health. Demo reductions still require a fresh direct venue-vs-kernel match
  for every requested symbol; venue-flat/local-open truth blocks the order
  instead of retrying Bybit error 110017. The kernel repeats the reduction
  proof inside the serialized transaction; the service preview cannot grant an
  exemption.
- Telegram sends a compact hourly account summary plus confirmed open, close,
  protection, and deduplicated loss-threshold events. Rejections and
  convergence polls do not emit per-cycle messages. During a venue/local
  mismatch it reports venue quantities separately from local reconstruction
  and suppresses untrusted exposure/estimated-uPnL and loss alerts. Local
  fill/P&L events are labelled as awaiting venue reconciliation rather than
  green closes. Valuation is explicitly the fresh captured Bybit L2 midpoint,
  not Bybit `markPrice` or venue-supplied uPnL. A missing, stale or gapped
  midpoint renders midpoint/notional/estimated-uPnL unavailable and degrades
  health; entry price is never substituted as a zero-P&L valuation.
- Each terminal reduce-only batch checkpoints incremental account/symbol P&L,
  including when another component remains open on the same symbol. The close
  message says `Reduced` rather than `Closed` in that case and retains the
  causal component ids and reasons. It does not allocate the net venue result
  to a component: component attribution stays `pending_account_netting`.
  The demo owner records strict venue funding settlements as separate immutable
  P&L events. Fill P&L, venue closed-PnL, funding completeness, and absent
  execution fees remain visibly provisional until the final machine-checked
  venue-accounting receipt reconciles the stopped epoch.
- Entry circuit-breaker losses are counted from those canonical account/symbol
  reduction events once per P&L key. Component ownership rows cannot multiply a
  single venue reduction into several losses.
- Bybit native TP/SL is one `Full`-position process-death seatbelt. When
  components supply different stops, it uses the outermost stop (long: lowest;
  short: highest), never the tightest component stop. Component stop/TP exits
  remain software target replacements, so one component trigger cannot flatten
  all same-symbol owners at the venue.

## Deterministic paper owner

Paper sleeves publish to a separate shared inbox and one paper account owner;
they do not independently pencil fills into sleeve Parquet ledgers. Configure
`/etc/liquidity-migration/account-paper-execution.env` with distinct roots:

```bash
ACCOUNT_PAPER_KERNEL_REQUIRED=1
ACCOUNT_EXECUTION_ROOT=/opt/liquidity-migration/data/bybit-account-paper
ACCOUNT_INTENT_INBOX_ROOT=/opt/liquidity-migration/data/bybit-account-paper-intents
ACCOUNT_PAPER_CAPTURE_ROOT=/opt/liquidity-migration/data/bybit-account-paper-market-capture
ACCOUNT_SYMBOLS_FILE=/etc/liquidity-migration/account-paper-execution/symbols.txt
ACCOUNT_DEMO_RULES_FILE=/etc/liquidity-migration/account-execution/demo-rules.json
ACCOUNT_RISK_POLICY_FILE=/etc/liquidity-migration/account-paper-execution/risk-policy.json
ACCOUNT_TWIN_CALIBRATION_FILE=/etc/liquidity-migration/account-paper-execution/execution-twin-calibration.json
ACCOUNT_TWIN_LATENCY_QUANTILE=p50
ACCOUNT_TWIN_SLIPPAGE_QUANTILE=p50
PAPER_EQUITY_USDT=10000
```

The paper owner uses the same account kernel and captured-book market-order
twin as historical replay. It refuses to start unless the self-hashed demo
calibration receipt passes its registered sample gate. Its L2 capture has an
independent liveness check; a healthy demo capture cannot mask a dead paper
feed.

## Abort conditions

Remove `account-execution-capture-enabled` and the
`account-execution-deploy-ready` authorization receipt, then stop target producers immediately if any
of these occur. If reconstructed or venue exposure is non-flat, keep the sole account
owner running only for reconciliation, native protection, and strictly reducing
recovery; stopping the only exit authority while exposure remains is not a
fail-closed action. Stop the owner after flatness is verified, or immediately if
the owner lease/journal itself is unsafe.

- venue/reconstructed position mismatch;
- desired target/working order/position convergence exceeds its health SLA or
  exhausts the bounded retry policy;
- missing or stale raw book outside an explicitly journaled, strictly reducing
  exit-only path;
- missing demo-verified rule row;
- open reconstructed position without active native disaster protection;
- a native stop is cancelled with residual reconstructed exposure;
- unknown external execution that is not position-reducing under an active
  native protection;
- duplicate account-owner lease;
- target/order/position parity hash mismatch.

Do not fall back to legacy direct order submission. Flattening demo exposure is
an explicit operator decision; real-money action remains prohibited.
