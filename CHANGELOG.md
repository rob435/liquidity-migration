# Changelog

The dated operational log: deploys, incidents, repairs, and change points,
newest first. One entry per change or per first report of a fault, updated in
place when the same matter moves on; a refused deploy, a re-fire of a known
incident, or a check that changed nothing gets no entry. Current truth lives
in [STATE.md](STATE.md); when something happens, add the dated entry here and
edit STATE.md to match.

Older history: [September 1-5](docs/history/CHANGELOG-2026-09-01-through-05.md),
[August 2026](docs/history/CHANGELOG-2026-08.md).

- **2026-09-06 — Round-2 runtime cleanup and integration.**
  - Separate the funded runtime from simulation, benchmark, backtest and operator
    tools; release and install `engine`, `engine-tools` and `signal-worker` together.
    Remove the unused Rust recorder, redundant current-universe builder and
    orphan pack wrapper; retain venues, research, Grafana, demo probe and all
    persistent sleeve identities. Archive dated changelog sections and retain
    prior audit/evidence revisions in Git.
  - Retain one exact in-flight quantity frontier, decode changed callback state
    once, preserve callback timing through durable effects and use current
    quote age at admission. Borrowed WAL encoding refuses lossy nonfinite fees
    and preserves rotation ordering; shared public HTTP scheduling keeps its
    bounded response and publication contracts.
  - Integrate the current archive, terminal-fill, callback-contention, continuous
    shutdown, process-CPU and unknown-cost risk repairs. Move their CLI/process
    regressions with the tools crate. Recovery instructions use supported verbs;
    malformed Bybit account envelopes force recovery instead of silent omission.
  - The sustained benchmark exposes `account view is newer than the decision it
    judges` after a valid account refresh. Risk now receives current admission
    time separately from the durable decision stamp; old comparisons also allow
    an account that becomes stale while waiting. Replay timing uses optional
    process-local measurements instead of mixing retained monotonic clocks.
  - Local qualification passes 2,395 release tests, strict Clippy, both doctest
    profiles, six repeated heavy fault seeds and copied-WAL boot/rotation/reboot.
    Three 60-second isolated workloads complete 299/580/576 submits with zero
    risk refusals or barrier failures. The mandatory pre-push gate passes
    2,394 regular debug tests and 1,608 Python tests; the separate debug example
    adds one pass. Full-day replay and broader resource measurements retain
    explicit scope limits.
  - The first combined workflow stops before VPS installation: a real-socket
    test races its 25 ms in-memory-test cancellation deadline on Linux. A
    controlled 100 ms delivery delay reproduces the failure. Only that test
    freezes its engine clock; HTTP timeouts and fill/cancel/replay assertions
    remain, with six venue tests passing in both profiles and four existing
    cancellation-deadline controls passing. Production code is unchanged.
  - Cleanup lands in `8f96e603`; the test-only follow-up `93404ff6` passes the
    mandatory developer gate and both Linux runs (2,399 passed, zero failed,
    six ignored). Workflow `34043450919` deploys the exact three-binary artifact
    successfully at 16:02:06 UTC. Demo/funded engines restart at 16:01:07/16:01:37;
    native positions and protective stops survive the handover.
  - At 16:05:32/16:05:56 UTC, LONG closes NEAR and ZEC in demo/funded accounts.
    Exact owned quantities match the fills, including two distinct 0.01 funded
    ZEC executions. The remaining four positions per realm have exact full-size
    native stops. Persistent CARRY/LONG children exceed the former CPU limit
    without replacement; workers finish repair with no stream faults.
  - The natural 16:15 demo probe is admitted and cancelled without a fill,
    removing the old unknown-cost refusal. Its PULL timer fires 5.323 seconds
    late; cancellation itself takes 9.049 ms. The biased event loop can starve
    ordinary timers, maintenance, signals and controls behind continuously
    ready market input. Five ordinary lanes now rotate, with one handler per
    outer private/recovery priority check. The control spool retains one IO
    operation and its poll deadline across cancelled reads, including already
    elapsed deadlines. Eight actual old-code assertion failures pass after the
    corrections; rejection, retirement errors and immutable restart bytes are
    covered. The heartbeat fixture waits for its observed market count instead
    of a fixed virtual stop; the first integrated failure remains in evidence.
    The copied-WAL stop rehearsal also pins its clock to the authenticated
    capture time: current wall time correctly triggers the captured LONG
    holding expiries and violates that fixture's stop-repair-only scope. All
    seven exact repairs, zero-order and accounting/replay assertions remain.
  - Correction `af09aab5` passes 2,404 local release tests, strict Clippy, both
    doctest profiles, six repeated heavy fault seeds and the copied-WAL
    rehearsal. Three measured workloads complete 299/581/581 submits with no
    risk refusal or barrier failure. The mandatory push gate passes 2,403
    regular debug tests and 1,608 Python tests. Both Linux runs pass 2,408 tests;
    workflow `34049060363` deploys the exact release artifact at 17:46:07 UTC.
    Both workers are ready by 17:50:35 after startup coverage repair; all eight
    positions retain their exact native stops and original entry permissions.
  - The 18:00 demo probe still queues PULL 5.912 seconds late. A deferred
    second ACK rereads unrelated WAL frames for 6.851 seconds, while busy
    per-sleeve timers shift their deadline by one second and race market
    callbacks. New callback sources retain their exact append offset; unread
    owners retain their first source. Busy timers keep their deadline and
    alternate with market callbacks through the existing owners.
  - At 18:05:23/18:05:44 UTC, both LONG children abort with `LONG filled state
    is invalid` after the TAO opening acknowledgement. Planning exposure
    includes the unfilled reservation and incorrectly becomes a filled state
    with no entry basis. LONG now reconciles fills and retirement against
    executed sleeve inventory; reservations still constrain order sizing.
    The existing heartbeat error field also includes callback-process faults.
    All ten native positions retain exact full-size stops during repair.
    Allocated fills also skip recording the opening order's logical stop,
    leaving the canonical TAO sleeve stop empty while its native stop remains
    attached. Repeated missing-stop reconciliation records latch both accounts
    and add WAL traffic. The correction restores the owned stop before
    callbacks and recovers already rotated sole-owner state from its explicit
    same-side durable stop witness; reconciliation remains an explicit step.
    The integrated residual fixture now explicitly omits its helper stop,
    matching its unprotected starting state without changing quantity or
    accounting assertions.
  - Combined correction passes 2,426 debug and 2,426 release tests, strict
    Clippy, both doctest profiles, six repeated heavy fault seeds and the
    historical copied-WAL boot/rotation/reboot rehearsal. The source manifest
    contains 662 unchanged implementation and check files.

- **2026-09-06 08:07 UTC — Worker recovery, recorder finalization and rollback repair.**
  - Both workers on `cece1d9f` remain alive but degraded: hourly source pruning
    discards the beginning of the unchanged daily CARRY repair range. Repair
    then fetches that same prefix again. Pruning now retains the daily scorer
    cursor's full kline/funding/whale windows until the next decision advances.
    Ingest, hot-update and serialized-restart regression fails with all three
    coverage windows missing before the fix and preserves them afterward.
  - Quiet recorder symbols retain prior-hour `.partial` files while continuous
    traffic prevents the writer queue's one-second idle timeout. The writer
    now checks hourly finalization on a monotonic cadence even under load.
    The regression leaves the quiet symbol open before the fix and publishes
    its complete file afterward, without waiting for a new frame on it.
  - Automatic/manual rollback and an explicit older deploy can replace the
    candidate with a reader that refuses its WAL or worker checkpoint. They
    now preserve the candidate for forward repair unless runtime inputs are
    identical; twelve failing-before cases exercise the destructive paths and
    equivalent-runtime rollback controls remain available. The imported
    heartbeat regression fixture now models Linux stat on macOS.
  - Backup at 03:17 UTC and tape upload at 08:10 UTC fail with Google OAuth
    `invalid_grant: Token has been expired or revoked.` The three configured
    copies contain the same rejected credential. At 08:18:52 UTC the existing
    client reconnects with unchanged `drive.file` scope; authenticated access
    to the backup and tape folders succeeds. Canonical/runtime copies use the
    refreshed credential and retain protected pre-change backups.
    Backup completes at 08:24:45 UTC (83 files, 15,034,191,872 bytes), and tape
    upload at 08:27:06 UTC (13 archives, 5,636,556,800 bytes); remote sizes,
    archive hashes and the backup check match. The existing app is now In
    production: static homepage/privacy pages ship from `6b5ed1d5` through
    Pages run `34023744649`, without adding OAuth clients or scopes. A fresh
    Production authorization passes forced token refresh; canonical/runtime
    configs are installed atomically at 09:20:57 UTC. This removes the fixed
    seven-day Testing expiry; brand verification is not claimed.
  - Three stopped legacy sources in each realm have no accepted pending
    observations but cannot report their terminal publication frontiers.
    Source retirement now records that terminal outcome without advancing
    accepted cursors or fabricating consumption. Demo sequence 11613 and
    mainnet 11226 remain irrecoverable; demo 11614 and mainnet 11227/49416 are
    recovered expired snapshots retained without application. Exact checkpoint
    evidence and recovered bytes remain under
    `/var/lib/liquidity-migration-wal-quarantine/legacy-source-retirement-20260906`.
    The deployment applies an explicit realm plan under the WAL lock after
    stopping it. Required `LegacySignalSourceRetired` and `segment_base_v7`
    retain this outcome through restart and reject incompatible old readers.
    Copied live WAL rehearsals preserve every original byte and accepted
    cursor, pass native verification and append nothing on identical retry.
  - Live-WAL rehearsal exposes legacy quantity drift: a `0.2899999999999999`
    holding sends `0.28` and retains uncloseable dust. `LegacyQuantityGridAdopted`
    durably resolves each eligible legacy contribution to its unique native
    grid point within 64 binary64 ULPs per input before history allocation.
    Canonical suffixes remain exact; cash, fees, unknown basis and sleeve
    stops remain intact. Durable exact allocation slices also retain residuals
    below `1e-9` when the raw execution has no native amount fields.
    An integration regression then exposes a priced legacy `0.1 + 0.2` lot
    whose exact `0.3` native close leaves dust and loses its closed trade.
    Adoption context now normalizes legacy units before canonical reductions,
    so normal accounting reconstructs closes and reopenings at their actual
    timestamps. Original automatic FIFO and internal full-close allocations
    are validated before reconstructing their legacy-dependent slices;
    explicit native quantities and fees remain unchanged.
  - Demo ENA's missing StopLoss execution at 2026-08-25 21:08:19.719 UTC sells
    1,564 units with a 0.12135702 USDT fee. All fifteen recorded executions
    match native receipts; the missing identity appears nowhere in the
    retained 29-file WAL family. `ClaimsDropped` on August 27 removes only
    ownership and leaves phantom physical exposure. New boots retain missing
    claims and report physical/native disagreement. Historical WAL remains
    unchanged; the recovered venue receipt and chronological accounting stay
    in the private operational evidence archive. `reconcile-clear` preserves
    exact native quantities and refuses a still-owned missing fill.
  - Frozen migration source passes 2,333 release tests (six ignored), release
    doctests and six heavy-fault 300-second simulations with two crashes and
    identical repeated replay. Captured native fixtures preserve all fourteen
    positions through boot and rotation; seven removed mock stops are repaired
    without orders. Current qualification and actual fail-before controls are
    indexed in the [retained deployment evidence](https://github.com/rob435/liquidity-migration/blob/16689a981c100632a1277567fb312e89a49c5309/docs/tier1-deployment-evidence.json).
  - Deployment of `2422be0d` reaches demo startup at 11:36:57 UTC, then
    exits at 11:37:01 with `wal io: invalid type: map, expected a string at
    line 1 column 129`. Retained archive sequence 313 reproduces the same
    failure: the boot epoch reader interprets a limit request's structured
    `kind` as a WAL record tag. Demo is held stopped with seven native stops;
    mainnet retains its incumbent process. The explicit demo reconciliation
    completes before this independent archive failure.
  - Epoch and lineage readers now distinguish record tags, nested order
    kinds and nullable verdict IDs. Callback readers decode event payloads
    only for callback record kinds. The actual demo failure, a real nullable
    mainnet verdict and a real-WAL boot/restart regression fail before these
    changes and pass afterward.
  - The disabled, flat mainnet quoter updates microstate on every subscribed
    quote and forces callback WAL writes. Its normal quote path now requires
    quoting to be enabled; inventory drains and cancellation remain active.
    Two real-child regressions fail before this condition change and pass
    afterward on the integrated source.
  - Terminal lookups now compare exact cumulative fills before retiring an
    order, including halt recovery; missing executions request history.
    Durable callbacks distinguish temporary contention from failure, retaining
    acknowledgements, timers and controls until the current invocation settles.
    Independent before/after controls exercise both faults on the integrated tree.
  - At 11:45 UTC the incumbent mainnet process opens eight 930-unit CAP shorts
    before reducing 6510 units. All 29 venue fills match WAL records; the final
    protected EXODUS short is 930. The candidate uses owned sleeve allocations
    across stale venue-net readings, and the refreshed full current-segment
    migration rehearsal passes with the new ownership.
  - At 12:42:45 UTC the installed runtime-control CLI exhausts host memory
    while loading the retained WAL family solely to resolve a sleeve identity;
    the kernel kills it at 7,181,820 KiB anonymous RSS before submission.
    Reading the newest trusted segment lowers the actual archived-history CLI
    regression from 150 MB to 14 MB while preserving durable identity and
    torn-tail refusal. The incumbent engine remains healthy. Its existing CLI durably pauses all
    three directional sleeves while the forward repair is qualified. The
    EXODUS pause callback closes the remaining CAP short at 12:46:19.109;
    its incumbent checkpoint has already removed that target prematurely.
    The exact 930-unit reduction and fee match native execution history.
  - Host liveness repeatedly receives HTTP 400 from the existing on-call
    routine, but discards its error body. Bounded, credential-redacted API
    rejection diagnostics now retain the reason; eight mocked cases fail
    before the fix and pass afterward. The remote rejection cause remains
    unconfirmed until the existing timer reports it.
  - Forward repair `420c7347` passes 2,352 local release tests, the full developer
    gate and six repeated heavy-fault simulations. Run `34035526455` deploys
    successfully at 13:28:02 UTC. Both engines replay retained archives and
    adopt exact quantities; all six legacy retirements are durable. Native
    reads show six positions and six matching stops per realm; mainnet ZEC
    and LIT stops tighten to 792.43 and 3.697, demo LIT to 3.694. Three funded
    entry permissions resume at 13:29:52–13:29:58 with verified durable controls.
    Both workers close their repair gaps and report ready by 13:32 UTC;
    recorder drops and old-hour partial counts remain zero.
  - Post-deploy observation finds repeated CARRY/LONG subprocess failures:
    `failed to fill whole buffer` begins at 13:30:01 UTC. Linux applies a
    20-second cumulative CPU limit to children reused for many callbacks.
    The lifetime limit is removed; the supervisor retains its per-callback
    deadline and process-group termination. The deployment also exposes a
    signal worker ignoring SIGTERM while awaiting engine readiness; one
    registered listener now spans recovery, readiness and live operation.
    Actual CLI tests cover shutdown, restart and the listener handoff.
  - Demo probe is refused at 13:30:00 UTC with `portfolio position has unknown
    entry value`. Legacy inventory correctly retains unknown accounting
    cost, but prospective risk unnecessarily requires that cost. Unknown-cost
    sleeves now use the latest accepted market price for exposure and stop
    distance; known costs retain their conservative valuation. Missing prices,
    missing/crossed stops and gross caps still refuse new risk. Two actual
    failing-before regressions cover shared/opposing ownership and restart
    without changing accounting bytes; all 144 risk tests pass afterward.
    The integrated block passes 2,358 release tests (six ignored), strict
    Clippy, release doctests and six repeated heavy-fault simulations; all 591
    source files remain unchanged through qualification. Its deployment is pending.

- **2026-09-06 — Tier-1 exact ownership and recovery qualification.**
  - All 56 accepted audit IDs have current dispositions: 42 implemented,
    13 retained decisions and one corrected finding. The accepted audit is
    not revalidated; implementation and regression evidence are verified.
  - Ordinary and native sleeve exits, account/risk quantities, known prices,
    margin and loss calculations keep canonical values. Exact lot cash and
    fees survive partial fills and rotation; v6 requires linked cost basis,
    and the precision marker makes incompatible predecessor readers refuse.
  - Terminal order ownership uses a bounded cache and one cancellable WAL
    lookup. Durable epochs prevent restart/counter ID reuse; rejected exits,
    late fills, fees and shared/opposing sleeve obligations replay once.
  - Execution history sorts on disk and folds into books without retaining
    the complete response. Empty or untrusted pages and pending durable
    dispatches cannot advance history. Abandoned rotation prefixes no longer
    strand archive lookup; corruption after a committed restatement errors.
  - Qualification: 2,300 debug and 2,300 release tests pass,
    five expected ignores per profile; strict Clippy, formatting, doctests,
    1,514 Python tests, Linux process limits and the 270-symbol worker envelope
    pass. All 48 simulator seed runs, each repeated, pass evaluated checks
    and produce identical WAL replay; flat-only accounting checks run on
    36 flat endings. 108 isolated fault/pass cases cover 106 distinct controls.
  - [Current audit](docs/tier1-audit-round-2.md),
    [source-bound evidence](https://github.com/rob435/liquidity-migration/blob/2422be0d9ca5a40e0ad954c6499d9f5a35e77d5c/docs/tier1-round-evidence.json) and
    [implementation contract](docs/tier1-round-handoff.md) contain the details.
    The [archived callback incident](docs/history/CHANGELOG-2026-09-01-through-05.md) records its local repair. No deployment or
    live account qualification is performed in this round.
