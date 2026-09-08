# Changelog

The dated operational log: deploys, incidents, repairs, and change points,
newest first. One entry per change or per first report of a fault, updated in
place when the same matter moves on; a refused deploy, a re-fire of a known
incident, or a check that changed nothing gets no entry. Current truth lives
in [STATE.md](STATE.md); when something happens, add the dated entry here and
edit STATE.md to match.

Older history: [September 1-5](docs/history/CHANGELOG-2026-09-01-through-05.md),
[August 2026](docs/history/CHANGELOG-2026-08.md).

- **2026-09-08 — Restore CARRY daily holding at owner direction.**
  - Render both realms with intraday funding and pre-settlement exits disabled.
    Hold durable quantity targets until the next daily decision; upcoming-book
    omissions no longer close positions early. Keep native v7 selection, sizing,
    entry execution, stops, and explicit reductions. No new CARRY exit fires feed
    EXODUS; existing event consumption and timed covers remain intact.
  - Read old checkpoints without quantity anchors and adopt existing holdings at
    boot. Already-fired exits remain suppressed for their original decision day.
    Research native replay reads the same policy and quantity targets; retained
    legacy lifecycle fixtures still exercise the intraday path.
  - Two behavioral regressions fail before the change; five daily-hold tests
    cover early drops, funding observations, partial fills, price moves, restart,
    delayed prices, working-order continuity and next-day rebalancing.
    This changes the holding policy; it does not reproduce the old daily
    diagnostic curve's future-return-filtered population or establish its profit.
  - Deployment retains each realm's installed configuration before rendering its
    replacement. A stopped-state conversion preserves checkpoint payloads across
    unchanged CARRY rules with edited source metadata and EXODUS stop tightening.
    Probe offset changes retain counters, timers, subscriptions, callback IDs and
    pending work in a durable runtime record. Boot recovery checks the result;
    retries resume after partial appends without clearing strategy state.
  - At 14:34:16 UTC, demo starts with preserved quantities but the deployment
    readiness check rejects `rolling-loss trip is on`: 164.54 USDT rolling
    loss against a 162.70 USDT limit. Startup and soak checks now distinguish that
    enforced entry restriction from process failure. The risk state and limit
    remain unchanged, ordinary liveness alerts remain critical, and all other
    heartbeat/resource failures still block deployment. Both failing readiness
    regressions pass after the correction.

- **2026-09-08 — Repair execution recovery, account limits and research costs.**
  - Research defaults read `configs/bybit_fee_rates.json` or
    `LIQUIDITY_MIGRATION_FEE_SNAPSHOT`. The authenticated sample contains
    10/3.6 bp taker/maker for most observed symbols and 11/4 for CAP/HEMI;
    generic defaults use the maximum observed rates, explicit scenarios remain
    available, and symbol-specific missing rates raise. No published-rate
    fallback or claim of coverage for unobserved symbols. The owner confirms
    Ukrainian identity verification; Bybit's Ukrainian VIP-0 table matches
    10/3.6 bp. Lower pricing and institutional DCP eligibility remain pending
    authenticated Bybit support access;
    the authenticated DCP query succeeds with an empty `dcpInfos` list.
  - Remove the daily forward-grading runner, financed-LONG forward scorer,
    ledger eligibility metadata and governance requirement. Preserve market
    capture, historical reports, stable rule IDs and honest data-reuse limits.
  - Run daily-weight and hourly native CARRY scoring over matched historical
    days from 2021-11-20 through 2026-09-06 at 7.78 and 14.36 bp per side.
    The 1,752-day [comparison](docs/research/carry-scorer-comparison.md) retains
    all eras, gross/net, raw series, frozen pre-change sources and missing-data
    limits. At 14.36 bp, matched 2026 daily/hourly results are +172.85%/−19.06%
    on notional base. Neither is a funded-account result or a grade of the new
    stop policy.
  - LONG uses 30-second PostOnly rest in both realms. Crossing cancels the
    passive order, obtains an independent terminal lookup and recovers every
    exact fill before admitting one IOC remainder. Cancel acknowledgement
    alone cannot cross. Restart cancels recovered worked sleeve openings even
    when they reduce an opposing physical position. Mainnet remains WS and
    demo REST because Bybit does not offer demo trade WS. The observed sample
    has six LONG opening orders/16 fills, all taker; 14 measured order RTTs
    have a 9.77 ms median. That sample does not establish future maker share.
  - Account reads retain exact liquidation/mark prices and IM/MM ratios.
    Initial-margin cap is 70% of reference; per-symbol gross cap is 50%.
    Opening and held stops use at most half inverse leverage (10% at 5×),
    including higher observed venue leverage on a shared symbol,
    tighten further for known liquidation proximity, and never loosen existing
    protection. EXODUS/CARRY defaults move from 35% to 10%; LONG ATR requests
    are capped at admission. Closed 24-hour loss includes current account open
    losses. These change execution economics and require separate research.
  - The owner confirms no hand trading; mainnet keeps sole leverage authority.
    A proven own-lot reduction may
    pass the opening latch when it grows the physical net behind a hand trade;
    exact owned quantity still bounds it. Preserve existing hand-side venue
    stops; existing virtual stops protect opposing owned lots.
  - Apply a 100 bp mark collar to entries, exits and amendments; market intents
    become bounded IOC limits, including exact full-position dust closes.
    Five distinct rejected engine IDs in 10 seconds durably latch openings
    and cancel remaining entries. Routine account reads compare venue exposure
    and recover history before latching unexplained drift. Bybit Full-position
    native stops still execute as venue market orders without this custom collar.
  - A Bybit book sequence gap invalidates and refreshes only its L1/L50 topic.
    Trade WS pings every 20 seconds, reconnects after a missing 10-second pong
    deadline and routes concurrent requests by ID. Up to ten adjacent distinct
    reprices reach Bybit before replies; intervening cancel/order commands retain
    ordering. Uncertain sent requests are never blindly resent.
  - CARRY decision eligibility uses receipt wall time rather than an hour-floored
    watermark: complete midnight data is eligible at 00:20, subject to the
    existing minute cadence and source readiness. Historical hourly replay does
    not establish 00:20 fills. Correct recorder ownership and serial WAL-barrier
    comments; record RTT only with its measurement scope.
  - Backup starts every 15 minutes with a 10-minute run budget and 30-minute
    completed-copy age alert. Host liveness measures venue clock offset with RTT
    uncertainty. The [storage/standby cutover](docs/infrastructure-layout.md)
    remains unexecuted: the host has one writable 119 GiB disk and no standby.
    The owner defers buying storage and a second host; these risks remain open.
  - Regressions reproduce missing pong redial, whole-socket book resets,
    00:20 clock delay, hardcoded research fees, exact late-fill crossing,
    wide stops, open-loss and margin admission, shared exits, symbol limits,
    routine drift, price collars, reject storms and serial reprices. The
    production venue wrapper also reproduces the serial reprice fallback before
    its forwarding fix. EXODUS checkpoint bytes and quantities remain identical;
    its tighter stop adds a restart restop effect. Local checks pass: 1,723 Python tests (one skip), 2,049 Rust
    tests (eight existing ignores), repository doctor, Ruff, ShellCheck, mypy,
    Rust 1.90 rustfmt and strict Clippy. Deployment remains pending.

- **2026-09-08 — Work LONG demo entries for 30 seconds and recover resting entries after restart.**
  - LONG demo joins the near touch, including one-tick spreads, for 30 s with
    at most one passive amend at the existing 15 s cadence. The remaining
    quantity crosses through the existing bounded GTC planner and 20 s grace.
    GTC can take on arrival. Mainnet LONG entry selection, reductions, capital
    and other sleeves' policies are unchanged. The exact native policy joins
    all eight latency/queue cells in the mainnet execution study.
  - Boot discards the working-order supervisor while the venue may still hold
    its entries. At 09:46 UTC the new boot regression reproduces the missing
    cancellation: `left: []`, `right: [(SymbolId(0), "eng-1700000000000-6")]`.
    Preserve the originating work policy in order snapshots, and cancel the
    confirmed worked remainder after boot through the existing paced path.
    Regression coverage includes rotation, a dark book, refused-cancel retries,
    accepted-cancel latching, partial passive fills and the native cross deadline.
  - Annual research accepts an explicit execution cutoff and unions adjacent
    funding download intervals. Missing historical prices still fail PIT
    coverage; trading delisted names cannot override that failure. The curve
    CLI accepts a stated all-in cost per side without applying its legacy
    multiplier again. The September 8 annual artifacts live at
    `~/SHARED_DATA/bybit_full_pit/reports/annual_20260908/`; these are seen-data
    reconstructions, not evidence that the new execution policy earns the return.
  - The completed 365-day minute LONG rebuild returns +83.8648% after fees,
    slippage and funding at the declared $100 / 6× scale; August contributes
    most of the gain. CARRY's notional-normalized hourly reconstruction loses
    9.1114% at 14.357519 bp per side and breaks even at 5.320282 bp. Missing
    pre-settlement observations and unpriced target attempts remain explicit.
    Daily CARRY score ledgers now retain gross, turnover and cost beside equity.
    Full results and era/cost cells: [annual review](docs/research/annual-execution-2026-09-08.md).
  - Deploy `441811eb` through [run `34213632476`](https://github.com/rob435/liquidity-migration/actions/runs/34213632476):
    31 healthy demo observations over 300 s; `deploy-ok` at 10:23:36 UTC.
    All loaded images match the verified release; nine positions per realm
    have eighteen exact full-size native stops. Local/hosted Rust totals are
    2,042/2,044; Python 1,719, with eight Rust ignores and one Python skip.
  - Reconcile the owner's earlier profitable CARRY results: the July 28
    double-funding correction predates the later positive runs. On refreshed
    inputs the daily v7 comparator still returns +275.9041% at the same all-in
    cost over 364 days. The hourly −9.1114% result changes the position path
    and cannot establish that fees erased CARRY's edge. Disabling only the
    hourly harness's intraday funding exits yields +148.6933% on all 365 days
    at unchanged costs; missing live pre-settlement observations prevent a
    live-exit recommendation. Retained EXODUS proxy
    validation has seven false fires out of 49; no annual live-trigger return
    is reconstructed from settled funding alone.
  - Repeated Binance metrics downloads fail for five unicode symbols:
    `UnicodeEncodeError: 'ascii' codec can't encode characters in position 35-37`.
    Encode URL path components while preserving native symbols on disk. The
    regression fails before the fix and passes afterward; retry retrieves 62
    symbol-days, with 48 HTTP 404 absences and no remaining request failures.

- **2026-09-08 — Check clock units without assuming sub-millisecond scheduling.**
  - The local push gate fails `wall_stamps_share_the_unix_epoch` at
    `ns / 1_000_000 <= ms.saturating_add(1)`. A forced 3 ms scheduling gap
    reproduces the failure. Bracket both readings with independent system
    timestamps; the same gap passes while checking both epoch and units.
    Runtime clock functions are unchanged.

- **2026-09-08 — Measure one-sided execution against actual order intentions.**
  - Add the Rust `execution-study` command and a fifteen-minute mainnet timer:
    incremental CRC-checked WAL reads, account-specific fee observations,
    paired closed-hour tape replay, persistent per-order reports and replay cache.
    Compare crossing, the existing working planner, 5/30/120 s post-only
    patience, an exploratory book/flow quote, and passive expiry. Keep both
    queue scenarios and all 5/25/100/250 ms latency cells; show price, fee,
    missed-opportunity costs, actual/hypothetical markouts and crossing error.
  - The authenticated fee read confirms 10/3.6 bp taker/maker on the five
    sampled directional symbols and BTC; CAP is 11/4 bp. Five Sep 7 orders
    match 14 venue/WAL fills. Four crossing prices match closely at 5 ms;
    ARB is 21.62 bp cheaper than the observed fill at that latency, so this
    model cannot justify a live execution change. In the two early opening
    orders, waiting 120 s loses 35.28 bp versus crossing at 100 ms; missing
    the trade loses 10.35 bp. These are seen-data diagnostics, not alpha.
  - Recovered tape members verify against the Drive archive manifests.
    The wider copied family exposes a legacy fill without an execution ID
    at `engine.wal:149292769`; the initial reader refuses it. Count such
    rows explicitly without claiming deduplicated fills or calibration;
    the reproducer fails before this compatibility fix and passes afterward.
    Tests cover finite queue/partial fills, post-only rejection, cancellation
    races, replacement priority, native deadlines, missing observations,
    same-process clock alignment, recovered-fill deduplication, fee absence,
    actual-fill calibration and cache reuse after tape expiry. Source and
    deployment status are recorded in [the study contract](docs/execution-study.md)
    and [STATE](STATE.md); no directional policy or arming setting is changed.
  - The first host run at 01:08:39 UTC finds an archived receive-time
    regression: `tape line 8263: local_receive_ts_ns 1788698566254491806
    is before the previous row's 1788698566261663423`. Keep that symbol's
    comparisons unscored, record its file/error, and continue the report for
    other symbols. The complete-command regression fails before this fix;
    it also verifies healthy cached results survive and repaired input retries.
  - Deploy `06220e56` through run
    [`34177470521`](https://github.com/rob435/liquidity-migration/actions/runs/34177470521),
    with 31 healthy demo observations over 300 s before mainnet handover.
    Final code succeeds at 01:58:13 and 02:02:15 UTC using 8.676 / 1.056 CPU
    seconds. The second report has 54 aligned orders, 91 identified fills and
    eleven complete comparisons; all nine earlier complete results are reused
    unchanged. Thirty-two orders lack frozen instrument rules and eleven CAP
    orders retain the tape error. The authenticated rates cover thirteen
    symbols: eleven at 10/3.6 bp, CAP and HEMI at 11/4 bp taker/maker.
    The 02:05:53 UTC backup checks 257 remote files; live, staged and downloaded
    report bytes match, with 54 per-order records in the stage. Final host
    reads verify all four loaded images against the release, healthy workers
    and nineteen exact full-size native stops. Local / Linux Rust totals are
    2,035 / 2,037; Python is 1,715 with one root/systemd skip; zero failures.
  - The 07:47:54 UTC report contains 23 orders / 54 fills and twelve complete
    counterfactuals; older XCN orders leave the rolling window. All 54 trades
    match authenticated Bybit execution IDs, order IDs, prices, quantities,
    fees, maker flags and timestamps. Their measured arrival cost is
    `1.14046812` USDT fees plus `0.442607` USDT slippage: `14.357519` bp over
    `1102.610518` USDT reference notional. The same account window has 55
    funding settlements and `0.34536423` USDT net funding credit, kept separate
    from execution cost. Artifacts: `/tmp/execution-study-review-20260908/`.
  - Add actual all-in cost dollars, basis points, coverage and missing-input
    counts to the text report; expose all/sleeve/action/day and per-order
    breakdowns in JSON. Compute fees and price cost over the same fills and
    arrival notional, retaining rebates. Regressions for mixed missing inputs,
    buy/sell signs, empty data and partial coverage fail before the change and
    pass afterward. Hypothetical policies and funded trading are unchanged.
    Deploy `329ba5dc` in run
    [`34203614326`](https://github.com/rob435/liquidity-migration/actions/runs/34203614326)
    at 08:33:55 UTC after 31 healthy demo observations over 300 s. All 23
    order records and policy outcomes remain identical; installed actual costs
    match the independent venue/dollar calculation. The new report succeeds
    at 08:35:08 and 08:38:02, with twelve cache hits on the repeat run. Loaded
    images match the release; both workers are ready and all twenty-one native
    stops match. Local / Linux Rust totals are 2,037 / 2,039, Python 1,715,
    zero failures; eight Rust ignores and one root/systemd Python skip remain.

- **2026-09-07 22:56 UTC — The `capture-disk` page returns, and the read-only
  diagnostic still cannot name the writer holding the disk. The recorders are
  correct; the filesystem is genuinely at their reserved floor. `mode=diagnose`
  now reports directory totals, so the next page is decidable without SSH.**
  - Incident `host-681737fd16e1f806`, scope `host`, host `ip-208-84-103-4`,
    `new_critical_refs=capture-disk`. Exact alert text, 22:56:24 UTC, raised
    once per recorder: `CRITICAL recorder storage is blocked; frames are
    counted but not written` and `CRITICAL recorder forward-market-binance
    storage is blocked; frames are counted but not written`, with
    `WARNING ... dropped 369082 frames` (Bybit) and `139267 frames` (Binance)
    since the previous check. Level-triggered on `disk_blocked is True`
    (`scripts/runtime/check_fleet_liveness.py:458`, raised at `:460`).
  - **The funded engine is not implicated.** Diagnose run `34168433363` at
    22:58:10 UTC: `liquidity-migration-engine-mainnet` active, heartbeat 3 s;
    both signal workers `status=ready`, `spool_backpressured=false`,
    `bybit_ws_gap_open=false`; `systemctl --failed` lists 0 units. Only
    research tape is lost.
  - **Not a regression.** Deployed commit is `f1fbe34`, current `main`,
    deployed 22:34 UTC the same day; all six earlier recorder fixes and the
    sliding window are live. The blocks are bounded to one
    `status_interval_seconds` cycle — 22:53:12 → 22:55:42 on Binance — which
    is `1702d14d` working, not the 300 s block it replaced.
  - **The disk is at the floor, which is the reserve doing its job.**
    `/dev/sda2 118G 88G 25G 78% /` against `min_free_disk_gb = 25`
    (`deploy/capture/bybit-linear.toml:28`,
    `deploy/capture/binance-usdm.toml:31`). `writable()` blocks the recorder
    above that floor (`market_tape/storage.py:440`), which is the reservation
    held for the mainnet WAL. Free space has fallen 27G → 25G since the
    2026-09-05 08:36 deploy, so the two tape caps (60 + 18 GB) plus the rest
    of the host no longer fit under 118G with a 26.8 GB reserve.
  - **What the evidence could not settle, and why.** Whether the 88G was tape
    at its `max_disk_gb` caps (a repository config change) or a foreign writer
    leaking (an owner action) turns on per-directory usage. `verify_mode`
    printed one `df -h /var/lib` line and nothing else, and the routine has no
    SSH key, so every page from this floor has had to hand the owner a `du`
    recipe to run by hand. That gap is the repository defect fixed here.
  - **The reading, and the answer: it is not the tape.** Diagnose run
    `34171910942` at 00:01:03 UTC, the first to carry `report_disk_usage`:

    | Directory | Bytes | Note |
    | :--- | ---: | :--- |
    | `/var/lib/liquidity-migration/backup` | 34 115 317 760 | 31.8 GiB, 97% of the state root |
    | `/opt` | 5 949 329 408 | toolchain, release, build target |
    | `/var/lib/liquidity-migration/forward-market` | 604 639 232 | Bybit tape, cap 60 GB |
    | `/var/log/journal` | 296 947 712 | |
    | `/var/lib/liquidity-migration/forward-market-binance` | 114 565 120 | Binance tape, cap 18 GB |

    Both tape roots together hold 691 MiB against 78 GB of allowed cap, so
    `max_disk_gb` is not what binds and the sliding window is working; the
    consumer is `backup`, taken up by *Root cause and repair* below. The same
    reading explains the silence the payload shows on the Binance pruner: with
    109 MiB of tape and nothing expired, `prune` has nothing to delete, so no
    `retention removed` line can appear however often a blocked tick arms it.
  - **Not accounted for.** `/` read 84G used at 00:01:01 while the three roots
    above total ~41 GiB, so ~43 GiB sat outside them. `DISK_REPORT_ROOTS` now
    also carries `/` and `/var/lib` at depth 1 so the next reading closes.
    The diagnostic is piped to the host from the runner's checkout, so this
    takes effect on the next `mode=diagnose` with no deploy.
  - **Fix.** `report_disk_usage` (`scripts/vps/deploy_remote.sh`), called from
    `verify_mode`, prints `disk <bytes> <path>` for one level under
    `/var/lib/liquidity-migration`, `/var/log/journal` and `/opt`, largest
    first, capped at 20 lines. `du -kx -d 1` reports allocated bytes rounded to KiB, stays on one
    filesystem and prints only directory totals. A missing root is skipped.
    The GNU-only form silently returned no totals on macOS; four existing
    regressions fail there before the portable command and pass afterward.
  - **Tests.** `tests/scripts/test_diagnose_disk_report.py`, five cases
    driving the extracted function over a built tree: directories named with
    their bytes largest first, one level deep with no file name, a missing
    root skipped, the line cap honoured, and `verify_mode` taking the reading.
    All five fail on the previous `deploy_remote.sh` and pass on this one.
  - **Root cause and repair.** The execution-study host read identifies
    `34,115,301,376` bytes in `backup/stage`, versus `4,305,788,928` in Bybit
    tape. The local backup duplicates sealed WALs indefinitely. After the
    existing remote checksum check, replace byte-identical sealed segment
    copies with hard links; exclude the highest numbered, growing segment.
    Keep other state independent and preserve rsync replacement semantics.
    The complete backup regression fails on the original script and passes
    with the fix; active appends, unequal snapshots and repeat runs are tested.
    At 01:04:04 UTC the first host run verifies all 198 remote files, then
    fails with `OSError: [Errno 18] Invalid cross-device link`. systemd's
    backup `StateDirectory` creates a distinct mount despite matching device
    IDs. Let the script create `backup/` and retain systemd's `receipts/`
    directory. An isolated root/systemd fixture reproduces the failure with
    the original unit and passes with the corrected unit.
    No live WAL or remote backup is pruned. The initial helper deploy at
    01:07:47 UTC in run
    [`34174591340`](https://github.com/rob435/liquidity-migration/actions/runs/34174591340)
    on `a7e96e8` cannot release blocks through that mount boundary; the final
    unit correction and measured recovery are recorded below.
  - **The page re-fires at 00:44:49 UTC because the repair was committed, not
    shipped.** Incident `host-ecbac293ecc90d5e`, scope `host`,
    `new_critical_refs=capture-disk,capture-disk:forward-market-binance`. Both
    recorders block — Bybit 00:43:25, Binance 00:43:49 — and drop 200,061 /
    28,170 frames in one watchdog interval. The host runs `0ebfc45`, deployed
    00:00:28 → 00:16:43, which predates `51beedc` by twelve minutes. Diagnose
    [`34174373585`](https://github.com/rob435/liquidity-migration/actions/runs/34174373585)
    at 00:45:54 UTC clears the funded engine: `engine-mainnet` active on a 3 s
    heartbeat, both workers `ready` with `spool_backpressured=false`, zero
    failed units. Only research tape is lost.
  - **The whole filesystem is named, and the ~43 GiB the 00:01 reading left
    unattributed is engine state.** Same run, 00:46:00 UTC:

    | Directory | Bytes |
    | :--- | ---: |
    | `/` | 93 636 464 640 |
    | `/var/lib` | 83 094 859 776 |
    | `/var/lib/liquidity-migration/backup` | 34 115 317 760 |
    | `/var/lib/liquidity-migration-engine` | 17 312 976 896 |
    | `/var/lib/liquidity-migration-engine-mainnet` | 17 243 029 504 |
    | `/var/lib/liquidity-migration-wal-quarantine` | 7 851 503 616 |
    | `/opt` | 6 031 826 944 |
    | `/var/lib/liquidity-migration/forward-market` | 4 218 232 832 |
    | `/var/lib/liquidity-migration/forward-market-binance` | 731 377 664 |

    Both tape roots hold 4.61 GiB against 78 GB of `max_disk_gb`. No current
    source names `wal-quarantine`.
  - **What ends the block is tape, not the repair.** Retention deletes until
    free passes the floor: `forward-market` 3.93 → 1.18 GiB and
    `forward-market-binance` 0.68 → 0.20 GiB by 01:07:40 UTC, `/` free 25G →
    28G. Diagnose
    [`34175732869`](https://github.com/rob435/liquidity-migration/actions/runs/34175732869)
    at 01:08:41 UTC reads `ok scope=host warnings-present-no-critical`: both
    `capture-disk` refs are gone and only the cumulative dropped-frame
    warnings remain. So ~3.3 GiB of research tape paid for a floor that
    31.77 GiB of duplicated backup holds.
  - **Two units are failed at 01:10:14 UTC and the read-only page cannot say
    why.** `systemctl --failed` lists `liquidity-migration-backup.service` —
    the job that owns the reclaim and the WAL's only off-box copy — and
    `liquidity-migration-execution-study.service`, on its first run after this
    deploy. `verify_mode` reads journals for the units it expects to be
    running, so a failed unit outside that list arrives as a name and nothing
    else. `report_failed_units` (`scripts/vps/deploy_remote.sh`) now prints
    `failed-unit <id>`, its result properties and 20 journal lines for each
    failed `liquidity-migration-*` unit, capped at five units. The diagnostic
    is piped from the runner's checkout, so this takes effect on the next
    `mode=diagnose` with no deploy.
  - **Tests.** `tests/scripts/test_diagnose_failed_units.py`, six cases over
    stubbed `systemctl`/`journalctl`: the unit named with its result and
    journal, every failed unit reported, a healthy host silent, the journal
    tail bounded per unit, the unit count truncated with a line saying so, and
    `verify_mode` taking the reading. All six fail on the previous
    `deploy_remote.sh` and pass on this one.
  - **The reading, first run: the reclaim itself is what fails the backup.**
    Diagnose
    [`34176153613`](https://github.com/rob435/liquidity-migration/actions/runs/34176153613)
    at 01:17:51 UTC prints both journals. `liquidity-migration-backup.service`
    exited `1/FAILURE` at 01:04:05 UTC in
    `link_sealed_backup_wals.py:39`: `OSError: [Errno 18] Invalid
    cross-device link: '/var/lib/liquidity-migration-engine/engine.wal.000002'
    -> '/var/lib/liquidity-migration/backup/stage/var/lib/liquidity-migration-engine/.engine.wal.000002.link-45af26e7'`.
    The run reached it with the off-box copy already landed and checked — `0
    differences found`, `198 matching files` at 01:04:04 UTC — so what the
    failure costs is the receipt (`backup.last-success`, written after the
    link pass, last stamped 21:20:13 UTC) and the `history` retention delete,
    not the copy.
  - **Why `st_dev` did not predict it.** The pass skips a segment whose
    `st_dev` differs from its staged copy, so the link was attempted on two
    paths the kernel reports as one filesystem. `link` requires one *mount*:
    it returns `EXDEV` across a mount boundary on a single superblock, and the
    unit's `StateDirectory=liquidity-migration/backup` makes the stage its own
    mount. A `findmnt` of both paths on the host would confirm the boundary;
    the routine has no SSH key and the diagnostic does not read mounts.
  - **Fix.** `EXDEV` from `os.link` now ends that source's pass and counts one
    `unlinkable_roots` in the printed receipt; every other `OSError` still
    fails the run. The off-box copy, the receipt and history retention no
    longer depend on a stage the kernel will not link into.
    `tests/scripts/test_backup_sealed_wals.py` adds the host's exact error —
    `EXDEV` leaves `(0, 0, 1)`, all three snapshots independent, no `.link-`
    temporary behind — and `EACCES` still raising. The first fails on the
    previous script with `[Errno 18]` and passes on this one. This fallback
    keeps backup completion independent of linking; the unit correction below
    removes the actual mount boundary and recovers the duplicate blocks.
  - **Deployed at 01:42:31 UTC in run
    [`34176429460`](https://github.com/rob435/liquidity-migration/actions/runs/34176429460),
    and the block returns while the duplicate blocks stay.** No backup run has
    started since, so the unit still carries the 01:04:05 failure and the fix
    is unproven until the 03:17 UTC slot. Free space fell back under the floor
    as the deploy staged its release and the tape regrew: both recorders are
    `CRITICAL` again at 01:43:24 and 01:46:25 UTC, dropping 1,522,375 then
    528,243 frames (Bybit) and 442,267 then 155,989 (Binance) per interval.
    Retention frees tape, tape regrows, the floor is reached again — with
    31.77 GiB of duplicated stage, 32.2 GiB of live WAL and 7.3 GiB of
    `wal-quarantine` on 118G there is no headroom left to hold. The funded
    engine is unaffected throughout: `engine-mainnet` active, both workers
    `ready`, `spool_backpressured=false`, no failed engine unit.
  - **Final repair.** Remove only the backup path from the unit's
    `StateDirectory`; retain the receipts directory. The script creates its
    own stage. The isolated root/systemd fixture proves identical sealed files
    share their inode with this unit and remain independent with the original
    unit, including when the EXDEV fallback is present. The study's separate
    tape failure is resolved in the execution-study entry above.
  - **Resolved 02:03 UTC: the reclaim ran and the floor is gone.** The unit
    fix ships in `06220e5`, with `deploy-ok` at 01:56:16 UTC in run
    [`34177470521`](https://github.com/rob435/liquidity-migration/actions/runs/34177470521).
    Diagnose
    [`34178684761`](https://github.com/rob435/liquidity-migration/actions/runs/34178684761)
    at 02:02:55 UTC reads `/dev/sda2 118G 54G 59G 48% /` — 25G free to 59G —
    and `/` falls from 93 674 799 104 to 57 639 882 752 bytes. The stage still
    reads 35 217 141 760 while the two engine state directories read
    532 520 960 and 462 704 640: `du` charges a hard-linked inode to whichever
    path it walks first, so the sealed segments now hold one copy of their
    blocks, counted under the stage. `systemctl --failed` lists no units;
    `backup.service` is `activating` mid-run and the execution study is clear.
    The host watchdog reads `ok scope=host units-and-heartbeats-healthy` at
    02:00:19 UTC with no `capture-disk` ref and no dropped-frame warning; both
    recorders are active on 15 s and 29 s heartbeats and the tape is regrowing
    at 765 MB Bybit and 128 MB Binance. `engine-mainnet` is active on a 3 s
    heartbeat and `engine` on 0 s, both workers `ready` with
    `spool_backpressured=false`, real money armed throughout.
    `/var/lib/liquidity-migration-wal-quarantine` still holds 7 851 503 616
    bytes no current source names; with 59G free that is the owner's call and
    not this incident's.
    The first corrected backup checks 200 remote files at 01:51:42 UTC,
    then reports `sealed WAL links=126 released_stage_bytes=33847439360
    unlinkable_roots=0` at 01:52:56 and succeeds at 01:54:47. The repeat run
    succeeds at 02:05:53 with 257 matching files, zero differences and zero
    additional links or unlinkable roots. At 02:09:44, all 126 links remain,
    the growing WALs are independent, and 57.779 GiB is available. Both disk
    drop counters remain unchanged since 01:54:47 while receipt times advance.
    All 131 predeploy WAL paths retain their inode and show no shrinkage.

- **2026-09-07 — Historical source adapters and explicit sparse-data execution.**
  - Separate recorder decoding/Bybit book reconstruction, normalized events,
    exact instrument catalogs, virtual delivery and execution assumptions.
    Reuse the archive importer and existing Rust strategy/risk/accounting core.
    Add mapped CSV/Parquet trades, tickers, reconstructed books and completed
    bars, explicit delivery/membership metadata and runnable actual Bybit samples.
  - Preserve the book simulator's binary64 contract. Add trade/bar models with
    declared spread, slippage, shared participation and conservative candle
    ordering. New modes emit derived exact grid quantities and require declared
    USDT settlement metadata; the sample's partial-reduction residual disappears.
    Passive limits use the declared spread for admission and print/close reach.
  - Reproduce stale book execution after a chain gap, missing receipt time
    becoming epoch zero, truncated zstd input becoming successful EOF, missing
    strategy features becoming a quiet run, absent execution observations,
    source-dependent mode validation and sparse-mode pending orders surviving
    liquidation. Liquidation cancels those orders before later observations.
    Regressions fail without each fix and pass with it. Preserve the failed
    diagnostics under `/tmp/connected-work-20260907`.
  - Actual Bybit CSV/mapped CSV/Parquet produce byte-identical normalized rows,
    WALs and trade files: 89 orders, 23 fills, 11 closed trips, net
    `-0.3531254284500014` USDT. Twelve hourly bars through CSV/Parquet produce
    identical WALs: three orders, two fills, one closed trip, net
    `-0.14267467319999985` USDT. These samples shape adapter diagnostics;
    they establish no alpha or independent execution-model validation.
  - Recorder/normalized-book equivalence compares decisions, fills, accounting,
    trade output and WAL bytes exactly. The supported LONG feature-generation
    path stays in the existing PIT/native research code; managed full-engine
    lifecycle replay remains an explicit unsupported requirement.
    The 1,500-row real recorder excerpt matches the frozen original binary's
    two orders, zero fills and every WAL record except the expected build commit
    label; continuous accounting matches exactly and neither zero-trade run
    emits a trade file.
  - Final Rust 1.90 developer gate passes 2,018 Rust and 1,696 Python tests,
    strict Clippy, formatting, Ruff, mypy and ShellCheck; eight fixture-dependent
    Rust tests are ignored by the broad gate. Separate copied-family and
    native/legacy rehearsals pass. Final external adapter runs reproduce the
    same WAL/trade bytes after the liquidation fix.

- **2026-09-07 — Current-WAL accounting and sustained resource qualification.**
  - The Python accounting reader ignores rotated v3–v7 bases and current
    identity/order/fill tags in the before regressions. Teach that research
    reader the retained versions, stream all CRC-checked frames, retain original
    sequence/hash identities, and use native CRC32C instead of a Python bit loop.
    Also refuse a file that shrinks during its length-bounded read.
  - Reconstruct the complete Sep 6 captured USDT linear cash/execution window:
    38 demo / 77 mainnet trades and 28/29 funding executions match exactly;
    transaction cash changes are `159.55178845` / `10.56366464` USDT. Keep
    inferred cash boundaries distinct from independent account observations.
    Full-day strategy/order/ownership reproduction remains incomplete; exact
    missing boundaries, public payload qualification and lifecycle/version
    replay requirements live in operations/STATE.
    Private order-history snapshots also match cumulative fills/fees for all
    109/49 engine requests. Preserve one historical demo XCN request/terminal
    difference: requested 61,960, venue-adjusted/filled 30,980, original remainder
    cancelled. Seven deactivated native stops have no engine client IDs.
  - Preserve the frozen `80db33df` sustained cells, including the 10.26 s burst
    submit maximum and 301 filled-history refusals. The offline 2M-operation
    soak retains exactly 65,536 IDs and recovers every row at all history tiers.
    On the same 1.12 MB WAL, traced reader time is 5.656 → 0.103 s and peak
    Python allocation 9.41 → 4.70 MB; this is not a funded-runtime speedup.
  - Recheck original/copied conversion hashes and all 27 base transformations;
    non-base frames remain byte-identical. All 27 converted boots, nine queued/
    nine prepared callback reads, both archived fill queries and native/legacy
    restart-rotation rehearsals pass. Retain the interrupted debug boot scan
    and optimized qualification separately. Keep required archive lineage,
    grid adoption, callback recovery, native protective repair and the compatible
    release; the 21:04:59 UTC read still contains all three scalar requests,
    and neither wall time nor history passes the Sep 13 strict boundary.
    No live WAL conversion/pruning, capital or arming change is made.

- **2026-09-07 — Remove obsolete audits and Python runtime code.**
  - Delete the four Tier-1 audit/handoff/evidence files; retain their immutable
    Git snapshots and all benchmark cells. Consolidate live recovery requirements
    into engine/operations docs and keep the terminal-expiry and unqualified
    production-day replay boundaries in STATE.md.
  - Remove the unused Bybit REST limiter/statistics, Telegram sleeve-file rewrite
    API, CARRY takeover model, invocation-ID validator and two orphan subprocess
    fixtures. Retained REST requests, results, errors and retry delays match the
    prior implementation across twelve scripted cases; active research/data
    Python and the LONG research state remain supported.
  - Move minute equity/health sampling and curve display to `engine-tools
    record-equity`; the systemd observer and operator route use that companion.
    Preserve append-first monthly files, finite/null sample values, metric names,
    configured zero series and gaps. Standard JSON replaces Python's nonfinite
    token extension; the metrics client uses a direct endpoint without proxy or
    redirect handling. Local records survive remote push failure.
  - Pin the recorder's verified candidate command before checkout removes its
    Python entrypoint. The prior deployment order fails the executable
    transition test with `can't open file .../record_equity.py: [Errno 2] No
    such file or directory`; the fix retains sampling through checkout and
    failed soak without changing the funded runtime before its demo gate.
  - Rust 1.90 developer gate passes 2,003 Rust tests and 1,675 Python tests,
    zero failures and eight Rust ignores. The 29 saved Python cases match
    JSON bytes, metric lines and filenames exactly. A compiled-command check
    also matches all six fresh host artifacts, Influx bytes and both curves;
    the local HTTP fixture uses no production push credentials.
  - Workflow `34145183215` stops at CI with `fatal: invalid object name
    '29366d3a'`: the handover fixture reads history absent from a shallow
    checkout. Define the old unit's execution contract inside the fixture.
    A real shallow clone reproduces five setup errors before the change and
    passes all five cases afterward; all 99 focused deployment tests pass.
    The unchanged deployment implementation still fails both deleted-script
    regressions. No host deployment starts in this refused run.
  - Retry [workflow `34146148488`](https://github.com/rob435/liquidity-migration/actions/runs/34146148488)
    passes 2,005 hosted Rust / 1,675 Python tests and deploys `76ad13ae`
    after 31 healthy demo observations over 300 seconds. At 17:24:28 UTC,
    both realms load the verified archive, all four services are healthy and
    all fourteen native stops match their full position quantities; quantities
    and stop levels match the 16:40:27 UTC predeploy read. Rust sampling pushes
    all six records during the soak and after global installation; both curve
    commands read the existing history. The temporary recorder override clears,
    all retained reader hashes match and every predeploy WAL path remains
    without shrinking. The prior latency qualification remains bound to `70f4c557`.

- **2026-09-06 — Round-3 embedded execution.**
  - Replace child strategy execution with one embedded path for production,
    bench, simulation and backtest. Catch reducer panics, fault only their
    sleeve and cancel its orders; unchanged state writes no callback WAL.
    Retain historical process-callback readers and restart ownership.
  - Combine checkpoint, intent, verdict, order and attempted-send durability
    before dispatch. Preserve a checkpoint barrier before an uncached leverage
    mutation. Default venue features compile Bybit; five other venues retain
    separate feature conformance builds. Exact constructors and the single
    main select loop follow the accepted Round-3 plan.
  - Separate fifty writable WAL kinds from eight retained kinds; preserve
    their original read tags, write current fee/checkpoint tags directly and
    keep the readback assertion in debug builds. A real scan-count regression
    observes `(6, 8)` decodes for `(3, 4)` records before the scan reuse and
    `(3, 4)` afterward. Replay and live state project a same-direction sleeve
    stop into native repair intent. A crash after the sleeve record now keeps
    95.1 instead of the stale 90.0 in the regression; native repair omits a
    covered StopSet. Uncovered and legacy repairs retain their write. The
    failing-barrier fixture targets SleeveStopSet and still prevents the
    native call. No retained host reader is deleted.
    Add `wal-convert-v5` for an offline complete family copied into a new
    directory. Reuse the checksum/record scanner, preserve frame sequence
    identity and retained source payloads, materialize existing v5 priced or
    unpriced lot semantics, and relocate callback offsets by segment/sequence.
    Eight focused fixtures cover exact state, retirement outcomes, callbacks,
    archived orders, repeat conversion and damaged/incomplete inputs; 19 WAL
    unit tests, both CLI entry points and scoped strict Clippy pass. The
    numbered-suffix fixture fails against the initial converter and passes
    after refusing to relabel an archive segment as a whole family. Returned
    errors remove the new directory; interruption can leave partial output.
    Assemble copied quarantine families at retained append boundaries: demo
    has 39 segments / 2,253,351 records and mainnet 38 / 851,734. Conversion
    upgrades 14 / 13 bases. Independent full CRC/source-hash checks preserve
    every other payload byte and exact owned quantities with unpriced lots;
    all 27 affected segment fills reports match before/after. The two spliced
    prefix files are reconstructed from retained frames and boot ordering,
    not an unavailable original whole-family checksum. Originals and readers
    remain intact; no host family is converted. Same-Mac before/after narrow
    decision p50 is 17.007 / 17.263 µs, both missing 10 µs, while narrow
    submit is 4.886527 / 4.968447 ms. Wide decision p99 is 35.551 / 23.711 µs.
    All 700 opportunities per pair complete with one barrier and zero failures.
    Reopen R3-13 before a fixed A B B A control on the same frozen images.
    Both B cells pass narrow decision 7.751 / 8.631 µs and submit
    4.718591 / 4.968447 ms; all 400 orders complete with one barrier.
    Both A decisions also return below 10 µs, while the final A submit
    misses at 5.324799 ms. Current point targets pass with the preceding
    wide cell; no converter regression or scheduling cause is established.
    Retain all misses and the separate stored CI budget. Commit `46bbb346`
    passes the required developer gate with 1,971 Rust tests, seven ignored,
    and 1,693 Python tests, zero failures.
  - Qualification `34104340078` fails before benchmarks on `46bbb346`:
    after compiling reference A, candidate B cannot find `engine_wal::conversion`
    because Cargo reuses the baseline dependency in their shared target directory.
    Normal Linux CI passes 1,973 Rust / 1,693 Python tests. Add R3-17 before
    separating the build directories. The real Cargo regression builds both sources
    successfully, but B prints A before the fix; after isolation B prints B and
    all three packed executables contain B. All 82 qualifier tests pass. Earlier
    paired archive hashes/logs still verify; their candidate dependency source
    attribution remains uncertain for those shared-target bytes; the later isolated
    build does not retroactively establish their contents.
  - Add R3-18 before repairing strategy assembly over retained paged callbacks.
    The real v7 fixture fails with `callback cursor restatement requires paged
    replay`; assembly now takes committed runtime from the existing paged replay.
    Its duplicate-owner assertion also fails before preserving the prior rejection.
    Committed runtime, timers, subscriptions and pending queue authority pass the
    focused checks. The first repaired image passes all 27 paired copied boots,
    nine queued and nine prepared callback retrievals; source-frontier coverage
    is zero. Demo archive lookup matches three rows through Filled 2220. Mainnet's
    first query returns only its request, so the strengthened final-source fixture
    requires observed terminal/fill state before selecting that query. Frozen reader
    image `1e757dbf` passes all 27 final paired bases / 54 boots with complete state,
    ordered venue effects and tape equality, and exactly one derived account response
    per boot. Nine queued/nine prepared retrievals pass; real source-frontier coverage
    remains zero. Demo's filled archive has three matching rows / quantity 110;
    mainnet has 13 rows / quantity 1.2. Original and converted input sizes/mtimes stay
    unchanged. Commit `8c92c964` passes the mandatory gate with 1,980 Rust tests,
    eight ignored, and 1,694 Python tests; its earlier push stops on rustfmt module
    ordering after Python passes. Fresh isolated qualification `34111799713` passes
    1,980 release tests, eight ignored, and the account workloads. All eight fixed
    cells complete 800 orders with one barrier each and zero failures. Candidate
    decision p99 median 15.05 µs fails the 13.95 µs absolute and 10.8 µs paired
    limits; submit p50 1.12 ms passes both. No qualified archive is published.
    The separate sanctioned deployment `34114063829` consumes the normal release
    artifact after functional checks; latency acceptance remains open under R3-13.
    Delete R3-06 and R3-17 after verifying absolute enforcement and both independent
    builds; the failed latency verdict establishes neither a qualified archive nor
    completion of the point targets.
    Deployment `34114063829` completes at 11:14:17 UTC after the 300-second demo
    soak. The 11:14:38 authenticated host read verifies both loaded images,
    fourteen exact full-size native stops, may_open=true and zero restarts/OOMs;
    mainnet worker recovery remains in progress at that observation, then reports
    ready at 11:18:06 on the same PID with complete coverage and zero stream faults. Both native
    state checks report already-complete. All three compatible binaries and the
    staged archive remain on the host with verified hashes; original WAL files
    remain without shrinking. Reader reduction proceeds locally only after this
    compatible release is retained and deployed. Remove the completed R3-18
    paged-assembly and R3-20 engine-clock rows after their failing regressions,
    copied/seeded replay checks and this deployment.
  - Add R3-16 before preserving source decimal instrument constraints and requiring
    exact metadata in simulation/backtest. The old backtest emits `exact_terms=None`;
    the repaired fixture retains step `0.0100000000000000000000000001` and submits
    `0.0900000000000000000000000009`, where the binary64 grid permits `0.1`.
    All 35 backtest tests pass, including deterministic 1,800-second replay; simulated
    fills and cash remain binary64. Light seed 1 and heavy seed 7 expose a separate
    legacy FIFO full-close fault across canonical grid adoption, recorded as R3-19
    before its repair. Two prior-code regressions fail on a `0.1` close. The repair
    keeps native quantities exact and raw legacy economics distinct from normalized
    inventory. Its existing allocation carries the actual grid; older readers cannot
    consume those normalized receipts. Deploy reader support before enabling the
    runtime writer and exact simulation metadata; no new feature flag is introduced.
    Stage A's release reader accepts the generated two-record canonical-base/new-
    receipt fixture rejected by the prior binary, independently of historical adoption.
    Its full frozen-writer WAL fills report matches exactly. Final focused scopes pass
    24 legacy, 77 execution, four allocation and 13 forced-close tests.
  - Remove the Python state-import writers/codecs under R3-08 after both realms
    verify native checkpoints. Delete three codecs, four import-only types,
    the translation trait method, CLI and deploy staging/import calls. Preserve
    all ten native initialization/verification bodies, canonical codecs, provenance,
    account/WAL locks and source retirement. Deployment retains empty-WAL plus
    no-legacy-source-files initialization; retained snapshots require the compatible
    release. Focused checks pass 128 Rust and 84 Python tests plus strict Clippy;
    the initial unused test-helper Clippy failure is retained separately.
    The combined metadata/import-removal image measures narrow decision 20.127 µs
    (miss), narrow submit 4.878335 ms (pass), and wide decision p99 38.303 µs (pass),
    against reader-stage 15.671 µs / 4.968447 ms / 25.919 µs. All 700 orders retain
    one barrier, zero failures and valid readbacks. No scheduling change is made.
    Extend R3-13 before simplifying the profiled stop path. Its diagnostic assigns
    49.6% of mean pre-decision time to virtual/native stop handling; the quarter-run
    medians drift, so this is not acceptance or a scheduling diagnosis. Read only
    per-symbol position/stop rows, separate synchronous repair discovery, and box
    the nested emergency/exit future only when that work exists. Preserve native,
    dirty-barrier and cursor-ordered control precedence. Original-function
    comparisons pass 34 populated/empty/error-order cases across three tests;
    34 additional stop, portfolio and shared-sleeve tests and strict core Clippy
    pass. Fixture setup errors and the first test-only Clippy failure remain
    separate diagnostics, not claimed product faults. Ordinary release measurements
    at 11:28:04–11:29:24 UTC pass narrow decision 6.083 µs and wide decision p99
    21.167 µs, but narrow submit 5.406719 ms misses 5 ms. Before values are
    20.127 µs / 38.303 µs / 4.878335 ms. All 700 orders complete with one barrier,
    zero failures and valid readbacks. Retain the miss and source-attribution limit. Reduced readers pass 91 WAL tests, including duplicate version tags
    in both orders with unchanged input bytes, plus 29 converter/CLI/paged-registry/
    quantity/metadata checks. All 27 converted-only candidate boots pass: demo
    14 and mainnet 13, nine queued/nine prepared retrievals, both filled archive
    IDs and all 154 source-file length/mtime checks; scratch directories are
    removed. Prior paired-image evidence remains distinct from these candidate-only
    state checks; no real source-frontier events or future fills are supplied. Remove the
    completed R3-07 row after compatible-release retention and candidate checks.
    The fixed A B B A control at 11:45:26–11:46:47 keeps all four submit misses:
    5.353471 / 5.267455 / 5.361663 / 5.230591 ms. Both new-image decision
    medians pass at 6.459 / 5.795 µs, while their p99s exceed both old-image
    p99s. All 400 orders complete with one barrier and valid unchanged readbacks.
    The control does not attribute the earlier submit increase to this change;
    R3-13 remains open.
    Commit `937ba60d` passes the mandatory pinned developer gate with 1,974 Rust
    tests, zero failed, eight ignored, and 1,718 Python tests, then pushes directly
    to main. Qualification `34119432164` and deployment `34119441979` target that SHA.
    The 11:54:39 authenticated predeploy read preserves all fourteen exact full-size
    stops; both engines and workers remain ready without restarts or OOMs. Deployment
    completes at 12:16:29 after all 31 demo observations pass through 12:15:56.
    Both native-state checks report already-complete. The 12:18:33 host read
    verifies Stage B images in both realms, ready workers, fourteen unchanged
    full-size exact stops and zero restarts/OOMs. All earlier WAL paths remain
    without shrinking; compatible Stage A images and archive remain unchanged.
    Isolated qualification `34119432164` passes 1,974 release tests, zero failed,
    eight ignored, account/history workloads and all eight fixed cells. Candidate
    medians 5,950 ns decision p99 / 1,115,000 ns submit p50 pass both absolute
    and relative budgets. The separate qualified Linux archive and embedded log
    verify; its bytes differ from the deployed ordinary archive. Remove completed
    R3-16 and R3-19 rows; the Mac submit target remains open.
    Add R3-21 before streaming portfolio route inputs: native sampling finds
    repeated vector growth in route maintenance. The sampled narrow run remains
    a diagnostic (5.919/588.287 µs decision p50/p99, 5.324799 ms submit p50),
    with all 100 orders and one barrier each; no timing-boundary change is made.
    Stream live-order references and insert route symbols directly into the ordered
    set. Preserve fresh walks and output/refusal/feed-effect order without a cache.
    The same thirteen focused checks pass before/after; strict core Clippy passes.
    The 12:13:00–12:14:20 ordinary pair measures narrow decision 6.211 µs,
    wide decision p99 18.591 µs and narrow submit 5.386239 ms; submit remains
    above 5 ms. All 700 orders keep one barrier and valid readbacks. The fixed
    after-sample removes the identified vector allocation branch; its 5.091327 ms
    submit median remains diagnostic and is not used for acceptance.
    One buffered critical-path diagnostic records every unchanged order boundary,
    with all 100 timelines and WAL clocks agreeing. It measures 4.780031 ms
    submit p50 and does not reproduce the ordinary miss. Fresh physical protection
    grows from 37.708 to 66.791 µs across order-count quarters. Restore all five
    temporary source files exactly; the ordinary rebuild reproduces `ae8c86fb`.
    Add R3-22 before folding repeated owned physical-stop candidate buffers.
    Replace repeated owned candidate buffers with borrowed per-side extrema and
    a running planner extremum. The same 846 cases match actual prior code; twelve
    distinct focused tests and strict core Clippy pass. Ordinary narrow/wide cells
    pass decision targets at 5.711 / 10.919 µs but narrow submit misses at
    6.094847 ms; its observed barrier median is 5.267455 ms. All 700 orders
    complete with one barrier and unchanged valid readbacks. Retain the miss and
    unproven storage attribution. Add R3-23 before restricting optional admission
    constructors to deliberate test fixtures and migrating the account-state soak.
    Ordinary builds now expose exact boot only; optional boot and scalar order,
    amendment and explicit-stop construction stay test-only. Preserve the original
    early quantity-conversion error order and missing-metadata refusals. Historical
    fixtures and protective/archive/grid recovery remain unchanged. Thirty-five
    focused checks pass before/after, plus one exact metadata test; ten matched
    soak boots keep holdings, covered stops and may_open. Native binding adds one
    identity record per boot; full-boot timing includes the exact metadata path.
    Strict core Clippy passes after marking a remaining scalar helper test-only.
    Freeze the final ordinary image, then clear 5.3 GiB of rebuildable release
    cache while retaining all captured data and binaries. One fixed old/new
    narrow/wide comparison keeps the old submit miss at 5.283839 ms. Final
    source meets the point targets: decision p50 7.959 µs, wide decision p99
    26.431 µs and narrow submit p50 4.882431 ms. All 1,400 comparison orders
    complete with one barrier and valid unchanged readbacks. The comparison
    does not establish a stable bound or attribute the submit difference solely
    to code. Remove completed R3-21, R3-22 and R3-23 rows. Commit `70f4c557`
    passes the pinned developer gate with 1,981 Rust tests, zero failed, eight
    ignored, and 1,718 Python tests, then pushes directly to main. All seven
    venue-feature jobs pass on that SHA. Qualification `34128439094` and
    deployment `34128449431` target the same source; the normal release archive
    verifies. The 13:36:56 predeploy read verifies fourteen full-size exact native
    stops, ready workers, may_open=true and zero restarts/OOMs; all four compatible
    Stage A files remain unchanged. Deployment completes at 13:55:11 after all
    31 demo observations pass through 13:54:35; mainnet handover follows at
    13:54:36. Both native-state checks report already-complete. The 13:56:11
    host read verifies loaded archive hashes, ready workers, may_open=true,
    fourteen unchanged exact full-size stops and zero restarts/OOMs. All earlier
    WAL paths remain without shrinking; compatible Stage A files remain unchanged.
    A journal read through 13:57:23 finds zero errors across all four services.
    Optimized qualification `34128439094` passes 1,981 release tests, zero failed,
    eight ignored, account/history workloads and all eight fixed cells. Candidate
    median decision p99 / submit p50 is 2,000 / 721,150 ns, within absolute and
    same-worker relative limits. The qualified archive and embedded log verify;
    its bytes differ from the ordinary deployed release. Retain the candidate's
    23.22 ms submit p99 / 86.05 ms maximum. Remove the completed R3-13 row.
    Stop the remaining R3-08 deletion under the owner's funded-account exception:
    archived scalar frontiers, legacy inventory, simulated fills and
    protective repair still need it. Current full stops and cache expiry do not
    retire those contracts; keep the original acceptance unmet.
  - Add R3-20 before fixing nondeterministic portfolio retry timing. Heavy seed 7
    reconciles twice but first differs at WAL index 3816: emergency 46 and a deferred
    quote change order after identical cancel completion. Retry deadlines use real
    `Instant` while simulation uses virtual time. The actual prior implementation
    fails the 250 ms engine-clock deadline assertion; engine-clock nanoseconds pass
    all four focused portfolio-control tests with the same exponential delay and
    30 s cap. No pump, event priority or fault rate changes. The first fixed seed 7 pair
    repeats exactly (`6c79023c8cae...`), with 209 orders, 119 fills, two injected deaths,
    zero restarts and 189 faults; clean/light/heavy tests pass. A test-only API spelling
    compile error is retained separately
    and is not counted as the failing regression.
    Reader-stage Mac cells run 10:11:31–10:12:51 UTC without builds/tests/scans:
    narrow decision 15.671 µs misses 10 µs, submit 4.968447 ms passes 5 ms; wide
    decision p99 25.919 µs passes 50 µs. All 700 orders have one barrier and zero
    failures; both Rust readbacks pass. The shared before cell has narrow
    decision 16.591 µs / submit 4.927487 ms and wide decision p99 40.735 µs.
  - Add registered-plug conformance, Exact/WAL properties and authenticated
    demo private frames. The corpus includes LINK Buy 53.9 at 13.329 with
    0.39513821 USDT fee; its replay uses the real WebSocket parser. Convert
    async checks to paused clocks and explicit I/O or durability progress.
    Preserve the live public-stream probe as a separate process test; it
    passes against Bybit. Share one I/O-progress helper in each test binary.
    Private-gap conformance now restores an omitted execution through the
    real Bybit/Hyperliquid REST clients and reconciles overlap by execution
    ID; Binance's unavailable account-wide recovery remains an explicit
    refusal. All fourteen conformance cases pass with every feature enabled.
  - Reuse callback action storage and remove immutable risk-policy parsing
    from the pending-order loop. Normalize binary64 powers of two directly,
    avoid a repeated product reduction and divide aggregate exact margin
    once. Old-algorithm comparisons preserve canonical bytes and margin
    results/refusals across changing prices and reservation lifecycle steps.
    Group quantities by effective price and stop fraction within each fresh
    assessment, preserving validation and price-read order. Batch rational
    normalization across equal-denominator runs; finish produces canonical
    Exact values. An original-algorithm oracle matches 4,096 sum prefixes,
    including cancellation and extreme scales. Index pending signed quantities
    per symbol; the original
    rowwise interval matches exact values, errors and canonical bytes across
    4,096 lifecycle steps and reconstructed books. Both decision targets pass;
    the quantity-index cells record 5.40 ms narrow submit and 7.40 ms wide.
    After the stop change, the cells are 5.33 ms narrow / 7.13 ms wide;
    narrow submit still misses 5 ms, while the unchanged Mac budget passes.
    Subsequent priced-quantity grouping and batched sums reduce the measured
    medians to 5.03 ms narrow / 5.25 ms wide; borrowing stop-fraction keys
    measures 5.05 / 5.41 ms. Borrowed price keys then measure 5.05 / 5.35 ms,
    a 20 µs improvement in each fresh paired cell. Reuse validated order-term
    projections within each operation; original/current comparisons preserve
    canonical bytes, errors and partial request updates across 512 cases and
    six order-kind/sleeve-effect combinations. All 227 type/risk tests pass.
    Projection cells measure 5.07 / 5.35 ms and establish no end-to-end gain.
    Fresh subscription hash sets do not improve the wide path; replace them
    with temporary per-symbol feed bitsets, preserving admission attempts,
    retries, retirement order and partial updates against the original loop.
    The bitsets measure 5.04 / 5.24 ms and wide dispatch queue 107.5 µs.
    Accept ordinary exact storage values by the unchanged digit bound implied
    by their bit lengths; retain decimal checks at the boundary. Original
    signed numerator/denominator comparisons and canonical roundtrips pass.
    Those cells measure 5.02 / 5.18 ms. Yield after durable authorization and
    venue-command registration so its actor can start I/O before route work.
    The full core suite exposes a fixture that expected a pending rejection
    from an immediate mock reply; give that fixture a 1 ms virtual delay.
    The final core suite passes 810 tests, zero failed, two ignored. The yield
    measures 5.03 / 5.14 ms; wide queue falls from 106.8 to 4.4 µs. Three
    unchanged narrow repeats measure 4.981 / 5.083 / 4.989 ms. Every final
    cell passes one barrier plus 1 ms, but narrow 5 ms is not consistent.
    All cells, including the failed hosted calibration, stay recorded.
    The follow-up developer gate passes 1,962 Rust tests and 1,646 Python
    tests; release all-target qualification passes 1,961 tests. Both profiles
    have zero failures and seven ignored tests. Both copied-WAL fixtures pass
    separately in release, repairing all twelve deliberately removed stops.
    Six heavy seeds with two crashes each produce byte-identical repeat WALs.
    Workflow `34081612658` deploys `fc2ad99c` after 300 demo seconds through
    04:18:23 UTC, then hands over mainnet at 04:18:24 and completes at
    04:18:56. Hosted debug passes 1,964 tests. The 04:19:36 read verifies
    both loaded image pairs and all twelve full-size native stops, with zero
    restarts/OOMs. Every retained WAL filename survives without shrinking.
    The existing drill requires identical runtime source, so its earlier
    `a4189a48`/`32858587` success does not qualify the changed `fc2ad99c`/
    `32858587` pair. Reopen changed-runtime rollback acceptance without
    attempting the known-refused drill or altering mainnet.
    Add an explicit full-SHA pair to the existing demo helper. It selects a
    reviewed retained predecessor independently of `previous-commit`, checks
    the current deployment under the existing lock and restores that current
    release after predecessor failure. Default weekly equality checks remain.
    The selected-pair regression fails at the original runtime-equality check
    before the change; all 29 helper tests pass afterward. The old reader also
    parses all 4,466 records of a captured current demo segment without a torn
    or corrupt tail. Workflow `34085705580` deploys `905c10d3` after 300
    healthy demo seconds through 05:26:22 UTC and leaves mainnet running.
    The explicit `32858587`/`905c10d3` demo return drill passes from 05:29:01
    to 05:31:12 UTC, verifying both loaded images and fresh account readiness.
    The 05:31:53 read confirms original mainnet PIDs, unchanged generation
    markers, all twelve full-size native stops and no missing/shrunk WAL files.
    Delete R3-09 after this selected-pair acceptance. The required push gate
    passes 1,962 Rust tests and 1,661 Python tests; hosted debug passes 1,964.
    Hosted qualification `34081614240` passes 1,962 release tests and
    account-state workloads, then fails decision p99 at 9.3 µs against
    9.0 µs. Submit p50 1.16 ms passes; all 100 opportunities complete with
    one barrier each and zero failures. No qualified archive is uploaded.
    Retain the failed sample and unchanged limits.
    Add a temporary manual eight-cell same-worker comparison of the qualified
    baseline archive and a fresh candidate build to investigate calibration;
    retain raw measurements and remove the workflow after the experiment.
    Run `34084393881` completes all eight fixed cells and 800 orders with
    one barrier each and zero failures. Identical baseline bytes fail the
    original 9 µs decision limit twice, as does the fresh candidate. Replace
    only the 6 µs decision reference with the A-only median run-level p99 of
    9.3 µs; keep the 1.09 ms submit reference and 1.5× rule. The decision
    limit becomes 13.95 µs; B at 18.9 µs remains a failure. Keep all original
    verdicts and remove the temporary workflow. Fresh qualification
    `34085706786` passes all 1,962 release tests and account workloads, then
    fails decision p99 at 14.3 µs versus 13.95 µs; submit p50 is 1.16 ms,
    with 100 orders, one barrier each and zero failures. No qualified archive
    is uploaded. Match the four-run decision reference with exactly four
    fresh candidate cells and compare median run-level metrics. Keep both
    references and limits fixed, retain all individual verdicts and raw logs,
    and fail on process errors or invalid selected histograms. The existing
    qualification-entry regression fails on the first 14.3 µs cell before
    the fix and passes afterward at median 9.0 µs, retaining that failed cell.
    The strict archive layout stays unchanged; benchmark WALs remain temporary.
    A malformed duplicate histogram exposes a parser acceptance bug; its
    regression fails before and passes after counting every selected row.
    All 61 qualifier tests pass. The fixed Mac after pair uses unchanged
    executable bytes and completes all 700 orders with one barrier and zero
    failures. Narrow submit measures 5.079039 ms and misses 5 ms; decision
    targets pass. Retain this miss beside the earlier accepted point cell.
    Commit `ecc3ea12` passes the developer gate with 1,962 Rust and 1,673
    Python tests; hosted debug passes 1,964 Rust tests. Fresh qualification
    `34088883848` passes 1,962 release tests and account workloads but fails
    all four decision cells: 32.0 / 28.9 / 15.5 / 15.7 µs, median 22.3 µs
    against 13.95 µs. Submit medians are 1.09 / 1.04 / 1.03 / 1.07 ms;
    their median 1.055 ms passes. All 400 orders have one barrier and zero
    failures. No qualified archive is uploaded; the fixed four-cell
    estimator does not resolve the hosted failure.
    Replace Linux absolute acceptance with an explicit same-worker relative
    comparison against freshly built baseline source `a4189a48`: fixed
    A B B A B A A B cells, 1.5× baseline medians, all build/check work first.
    Keep absolute verdicts and the candidate-only archive layout. Darwin
    retains absolute acceptance. A noisy baseline can hide a change; a
    relative pass is not an absolute latency pass. All 79 qualifier tests
    and three doc-link tests pass, including twice-baseline rejection and
    actual final-cell binary mutation refusal. The old API reproduces the
    22.3 µs absolute failure; the new contract preserves it as a diagnostic.
    The fixed Mac after pair uses unchanged bytes: narrow submit 4.939775 ms,
    wide decision p99 10.255 µs, 700 orders, one barrier each, zero failures.
    Prior misses remain recorded; no runtime speedup or stable 5 ms bound
    follows from this helper change. Commit `6de33fa3` passes the developer
    gate with 1,962 Rust and 1,691 Python tests; normal hosted checks pass
    1,964 Rust and 1,691 Python tests. Fresh hosted qualification
    `34093133061` passes 1,962 release tests and account workloads. All eight
    fixed cells complete 800 orders with one barrier each and zero failures.
    Baseline medians are 13.65 µs decision p99 / 1.24 ms submit p50;
    candidate medians 10.5 µs / 1.25 ms pass the relative gate and absolute
    median limits. Three individual decision cells still fail the absolute
    limit; one baseline barrier maximum is 137.93 ms. Retain all cells and
    prior failures. Downloaded candidate binaries and the embedded log verify.
    The completion audit reopens R3-06: relative-only acceptance does not meet
    the original stored-budget requirement. Restore absolute failure as a
    publication error alongside the paired comparison, keeping both verdicts,
    all eight cells and unchanged limits. Both absolute-fail/relative-pass
    regressions fail before with `DID NOT RAISE` and pass afterward; 81 qualifier
    tests and three doc-link tests pass. The verified eight-cell recorded log
    passes the corrected checker; doubling either metric for B or for both
    images fails. This replay is not fresh hosted qualification. The unchanged
    Mac executable measures narrow submit 5.136383 ms before and 5.058559 ms
    after; both miss 5 ms. Each pair completes all 700 orders with one barrier
    each and zero failures. The CI helper change has no engine runtime change
    or VPS redeploy; retained-WAL conversion and removals remain open.
    The final qualified-source Mac remeasurement records narrow decision
    p50 4.751 µs, submit p50 4.997119 ms and wide decision p99 15.047 µs.
    All 700 opportunities complete with one barrier each and zero failures.
    Delete R3-13 after its parity and point targets pass; record the 2.881 µs
    narrow headroom, earlier misses and different measurement-build hashes.
  - Add the accepted demo soak, watchdog, rollback and runtime-only Python
    deployment changes. Workflow `34074541111` deploys `a4189a48` on
    2026-09-07: demo passes all 300 seconds from 02:06:41 to 02:11:41 UTC
    before mainnet handover at 02:11:42. Hosted debug passes 1,961 tests,
    zero failed, seven ignored. Both loaded runtime hashes match the default
    Bybit artifact; no strategy children remain. Both workers are ready at
    02:13 UTC. Native reads verify six fully protected positions per realm;
    all four services have zero restarts/OOMs. Engine watchdogs are active
    and host Python contains only pip and websocket-client. A 30-second
    demo sample measures 28,537 WAL bytes/s with zero engine errors.
    Hosted qualification `34074530152` passes 1,959 release tests and
    account-state workloads. Its separately built Linux artifact measures
    decision p99 6.0 µs and submit p50 1.09 ms, replacing the provisional
    Linux budget with measured references and 1.5× limits. Doubling either
    measured segment fails the validator. The calibrated repeat `34076340582` fails decision p99 at 16.6 µs
    against 9.0 µs, despite unchanged runtime source; submit p50 1.51 ms passes.
    Retain the failure and unchanged budget; calibration acceptance reopens.
    Workflow `34076341887` completes generation `32858587` after a second
    300-second demo soak through 02:43:42 UTC, leaving identical-runtime
    mainnet on its prior PID and image. The sanctioned demo drill activates
    predecessor `a4189a48` at 02:47:01 and restores `32858587` at 02:48:03,
    verifying each loaded image and readiness without rewinding durable state.
    Mainnet PIDs remain unchanged. Native reads at 02:49:28 verify all twelve stops.
    The final developer gate passes 1,959 Rust tests (zero failed, seven ignored) and
    1,646 Python tests. All six venue feature builds and their tests pass;
    combined venue/public/market-data qualification passes 810 tests, with
    strict all-feature workspace Clippy. The prior captured-WAL boot, replay,
    rotation and stop-repair regression passes on this candidate. The release suite passes 1,957 tests, zero failed, seven ignored; all six
    repeated heavy fault seeds pass. Both current retained families pass real-WAL boot, replay and rotation;
    all twelve removed stops are repaired without placing orders. The fixture
    uses Bybit catalog decoding and continuing captured quotes with mocked
    transport, risk and collateral. Submit latency remains open. Current
    host reads show six fully protected positions per realm and healthy
    deployed engines and workers.
    [Execution measurements](docs/execution-performance.md) retain every
    measured cell and its interference/source limits.

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
  - Workflow `34054719567` stops before handover on two Linux timer fixtures:
    their unreserved prepared callback starts during `on_timers`, then the
    mocked completion double-registers the owner. The fixtures reserve that
    completion before the first drain; deadline, private-source priority,
    timer replacement and replay assertions remain unchanged. Runtime source
    stays at `f6c71460`; its full local debug/release suites pass 2,426 tests
    each and the required push gate passes 2,425 regular debug and 1,608 Python
    tests. Fresh measured workloads complete 299/587/584 submits with no risk
    refusal or barrier failure.
  - Workflow `34055716541` deploys `bb4bc3d3` at 19:53 UTC after 2,430
    Linux tests pass. The sanctioned handover clears both historical latches
    after authenticated exposure agreement. Fresh 20:01 UTC reads show both
    engines opening-enabled, no strategy errors or restarts, continuing LONG
    checkpoints and five exact full-size native stops per realm. The 20:00
    demo probe records PULL 1.041 ms after its deadline, down from 5.912 s;
    dispatch follows 9.557 ms later. The focused capture omits the FIRE
    preparation clock, so it does not establish total resting time.

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
  - [Audit snapshot](https://github.com/rob435/liquidity-migration/blob/29366d3a2013701a0956a2a471a7c916bf6980e2/docs/tier1-audit-round-2.md),
    [source-bound evidence](https://github.com/rob435/liquidity-migration/blob/2422be0d9ca5a40e0ad954c6499d9f5a35e77d5c/docs/tier1-round-evidence.json) and
    [implementation checkpoint](https://github.com/rob435/liquidity-migration/blob/29366d3a2013701a0956a2a471a7c916bf6980e2/docs/tier1-round-handoff.md) contain the details.
    The [archived callback incident](docs/history/CHANGELOG-2026-09-01-through-05.md) records its local repair. No deployment or
    live account qualification is performed in this round.
