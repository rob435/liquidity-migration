# Changelog

The dated operational log: deploys, incidents, repairs, and change points,
newest first. Each entry is kept as it was written on its day, so a later
entry supersedes an earlier one — read from the top down. Current truth lives
in [STATE.md](STATE.md); when something happens, add the dated entry here and
edit STATE.md to match.

- **2026-09-05 03:21 UTC — The tenth page from the same free-space floor. No
  sixth defect: every mechanism in this payload is the deployed behaviour of
  the five merged, undeployed recorder fixes. Two things are new. The three
  "incidents" nine entries have been tracking are one incident under three
  names — `incident_id` is `sha256(scope + the newly-due CRITICAL refs)`, so
  it names which recorder's alert cleared its cooldown first and nothing else;
  all three hashes are re-derived below. And the payload prices the margin for
  the first time: an 8-file pass opened both recorders' gates, both wrote for
  exactly one 30-second status interval, and both re-crossed. The room a pass
  leaves is about 30 seconds of the pair's own writing, which is what sets the
  period of the oscillation — and it makes `d275885a` worth ~67× fewer
  crossings, not a nicety.**
  - Incident `host-681737fd16e1f806`, scope `host`, host `ip-208-84-103-4`,
    `new_critical_refs=capture-disk` — the unsuffixed ref, which is the Bybit
    recorder (`key()` appends `:{label}` only for a labelled recorder,
    `scripts/runtime/check_fleet_liveness.py:392-393`). Exact alert text:
    `CRITICAL recorder storage is blocked; frames are counted but not
    written`, level-triggered on `disk_blocked is True`
    (`:431`, raised at `:433`). Both WARNINGs are the drop counters:
    `recorder dropped 329814 frames since the last check (storage was
    blocked)` and `recorder forward-market-binance dropped 175822 frames since
    the last check (storage was blocked)` (`:436-451`).
  - **One incident, three ids.** `incident_text` builds
    `incident_key = "\n".join([scope, *newly-due CRITICAL keys])` and takes
    `sha256(...).hexdigest()[:16]`
    (`scripts/runtime/check_fleet_liveness.py:806-807`). Every id in this
    incident re-derives exactly:

    | `incident_key` | id | Means |
    | :--- | :--- | :--- |
    | `host\ncapture-disk` | `host-681737fd16e1f806` | Bybit's alert came due this run |
    | `host\ncapture-disk:forward-market-binance` | `host-16171e3c5e186136` | Binance's did |
    | `host\ncapture-disk\ncapture-disk:forward-market-binance` | `host-ecbac293ecc90d5e` | both did, in one run |

    `new_critical_refs` is the set that *cleared its cooldown*, not the set
    that is failing, so the id turns over as the two recorders' cooldowns
    drift against each other. Read the ten pages since 22:54 as one incident.
  - **The funded engine is not implicated and the host has not moved.** No
    engine, worker or timer is named; both units are `market_tape` recorders,
    research tape outside the order path. Pids are unchanged across all ten
    pages — 2259813 (Bybit), 2263691 (Binance) — so neither recorder has
    restarted and the host still runs `65ee75a7`. The 25 GiB floor is the
    reservation held for mainnet's WAL: `writable()` blocks the recorder
    *above* it (`market_tape/storage.py:426-433`), so it is intact by
    construction and stayed intact.
  - **Both recorders cross as one filesystem event; the apparent lag is tick
    phase.** Bybit's status ticks land on :14/:44, Binance's on :05/:35 —
    9.9 s apart through the whole excerpt. The first blocked tick on each unit
    carries almost no drops (Bybit +37 at 03:18:44.913, Binance +20 at
    03:18:35.057), which at their blocked rates is 14 ms and 18 ms of
    dropping, so each crossed within ~20 ms of its own tick. The crossings are
    **9.86 s apart** and the resumptions **9.88 s apart** — the same offset,
    which is `status_interval_seconds = 30` phase and nothing about the disk.
    The 00:36 entry's "0.75 s apart" and this page's "9.9 s" are the same
    event seen at different tick phases; neither is a property of the floor.
  - **What a retention pass buys, priced.** One pass ran in the whole blocked
    phase — Binance's, 03:19:46.769, **8 files** — and it ended the block on
    both units:

    | | Blocked | Gate opens | After the pass | Wrote | Re-blocked |
    | :--- | :--- | :--- | ---: | ---: | :--- |
    | Binance | 03:18:35.057 | 03:20:05.119 | 18.35 s | 35 005 rows in 30.01 s | 03:20:35.130 |
    | Bybit | 03:18:44.913 | 03:20:14.999 | 28.23 s | 87 124 rows in 30.02 s | 03:20:45.019 |

    Both blocks were 90.1 s. Both gates opened on a **status tick**, not on
    the pass — `fd604613`, which lets the pass that frees room open the
    writer's gate, is undeployed, and 18.35 s and 28.23 s of the 90 s were
    spent waiting for a tick on a disk that already had room. Neither pruner
    walked again in the 109 s to the last line, which is the deployed bare
    `stop.wait(RETENTION_INTERVAL_SECONDS)` (`market_tape/record.py:1121` at
    `65ee75a7`) with nothing woken by a crossing: `1d8fad9a` is undeployed
    too, so the next pass was not due until ~03:24:46.
  - **The margin is ~30 seconds of the pair's own output, and that is the
    whole oscillation.** The 8 files freed exactly what the pair then wrote
    before re-crossing: 122 129 rows in ~30 s. `projected_gb` 1295.2 + 419.5
    is 661.5 KB/s of inbound wire bytes, so ≤19.8 MB in that interval and
    ≤2.5 MB a file — compression only makes the true figure smaller. That is
    what the deployed `pressured = total > self.max_bytes or free <
    self.min_free_bytes` leaves (`market_tape/storage.py:362` at `65ee75a7`):
    `prune` stops on the number `writable()` unblocks on, so the margin is
    zero by construction and the next 30 seconds of tape re-crosses it.
    `d275885a`'s `free_target = min_free_bytes + 5%` is **1.25 GiB**, which at
    ≤19.8 MB per 30 s is **≥2029 s ≈ 34 minutes** between crossings instead of
    30 seconds. The 03:00 entry showed a 410-file pass buying one tick and a
    106-file pass buying nothing; this is the same fact measured from the
    other end, and it says the fix is a ≥67× cut in crossing frequency rather
    than a refinement. Pass size never measured room freed anyway — `prune`
    deletes for age and for `max_bytes` in the same walk — and the manifest's
    per-unlink `compressed_bytes` and `reason` is where the three separate
    (`market_tape/storage.py:413-421`).
  - **What the journal settles without SSH: `max_disk_gb` is not what binds.**
    A pass logs whenever it deletes anything at all
    (`market_tape/record.py:1166`, `:1133` at `65ee75a7`), and Bybit logged
    **no pass in 990.8 s**,
    of which 810.6 s were unblocked. That is at least two full 300-second
    passes finding `pressured` false, which needs `total <= max_bytes` and no
    file past `retention_days = 30`. Binance deleted only while blocked, and
    only 8 files. So neither recorder's tape is at its cap: 60 + 18 GB is not
    holding `/var/lib` at the floor, `min_free_disk_gb` is, exactly as
    STATE.md has said. What still needs the host is the other half — whether
    tape or non-tape growth ate the room — and the recipe for it is unchanged
    from the 03:00 entry above.
  - **The incident is episodic, not continuous.** Before the crossing Bybit
    ran **810.6 s with zero disk drops** at 3 156 rows/s and Binance 660.4 s
    at 1 135 rows/s. Then, across the 3 minutes of the blocked phase, the pair
    kept 122 129 rows and discarded 505 598 frames — **4.14 discarded for
    every one kept**. That is why the average since the 03:00 entry (937
    frames/s over ~20.7 min) is well under that entry's 3 268/s: the floor was
    not crossed for thirteen minutes, and then it was.
  - Loss, cumulative and never reset. Both windows are cut by the 40-line
    payload, so every figure is a lower bound; both recorders are inside an
    open block at the last line (Binance's second block is already ≥60.0 s).

    | Unit | First line | Last line | Added since the 03:00 entry | Rows kept |
    | :--- | ---: | ---: | ---: | ---: |
    | Bybit `forward-capture` | 15 736 002 (03:04:44) | 16 065 875 (03:21:15) | 807 906 | 2 737 502 |
    | Binance `forward-capture-binance` | 5 651 210 (03:07:04) | 5 827 039 (03:21:35) | 353 799 | 822 325 |
    | **Pair** | | **21 892 914** | **1 161 705** | **3 559 827** |

  - **Deploy refused a seventeenth time, same signature.** Run
    `33941811398`, `deploy main@d2e21d83`, dispatched 03:25:29 UTC and failed
    03:25:33 — 4 s. `ci`, `Deploy artifact` and `rust` all created and dead at
    03:25:30 → 03:25:33, each of their log downloads returning `failed to
    download logs: HTTP 404`; `disarm`, the release-test job, `vps` and
    `diagnose` skipped. No job ever started, so nothing reached the host:
    deployed commit stays `65ee75a7` and all five recorder fixes stay merged
    and undeployed. The cause is outside the repository — the account's
    payments failed, so GitHub assigns no runner. Dispatched on `d2e21d83`
    because it already carries all five; every commit after it is
    `CHANGELOG.md` and `STATE.md` only and installs the identical tree.
  - **The one action that ends this needs no runner**, from a workstation
    holding the SSH key:

    ```sh
    EXPECTED_COMMIT=3c1ebd22bb78fac6fabfcf3370836bbec32e9527 scripts/ops.sh deploy
    scripts/ops.sh status
    scripts/ops.sh curve mainnet 240
    ```

    It restarts the funded engine — `3c1ebd22` carries `697341e4` and
    `10ed1bd2`, so its `engine` tree differs from the deployed `65ee75a7` and
    the fingerprint hands over both realms. Do **not** install `06e17d4a` on
    its own; `3c1ebd22` is the commit that carries all five recorder fixes.
  - No code changed. Suite on this unmodified tree: **1439 passed, 9 skipped,
    16 failed**, all sixteen for missing container tooling — fourteen in
    `tests/market_tape/test_load.py`, `test_fixture_hour.py` and
    `tests/research/lab/test_lab_tape.py` raise `FileNotFoundError: 'zstd'`,
    two in `tests/scripts/test_observability_hygiene.py` report `backup: rsync
    is not installed`. Neither binary is present in this routine's container
    and neither can be installed from it. The pruner's own tests are green
    here: `tests/market_tape/test_record.py` and
    `tests/market_tape/test_tape_storage.py`, 69 passed, 9 skipped, including
    `test_a_successor_pass_credits_what_the_burst_already_unlinked` and
    `test_a_burst_of_owed_passes_deletes_the_deficit_once_not_the_whole_tape`.

- **2026-09-05 03:18 UTC — The same crossing the 03:21 entry above measures,
  paged three minutes earlier off the Binance unit alone, and read against the
  undeployed fixes rather than the deployed code. It finds a **sixth defect,
  in `3c1ebd22`**: a credited burst ends on its second pass with the gate
  still shut whenever the kernel released the unlinked blocks and the
  neighbouring recorder took them, and `_maintenance` armed the pruner on the
  crossing only, so the writer then waited out the whole 300-second interval.
  That is the 330.3 s, 360.2 s and 390.3 s blocks the 02:48 and 03:00 entries
  measured, and the five merged fixes would have left them in place. Fixed in
  `1702d14d` by arming on the level. This entry is deliberately thin on the
  crossing itself: the 03:21 entry above has it from both units, prices the
  margin, and shows the three incident ids are one incident.**
  - Incident `host-16171e3c5e186136`, scope `host`, host `ip-208-84-103-4`,
    `new_critical_refs=capture-disk:forward-market-binance`. Exact alert text:
    `CRITICAL recorder forward-market-binance storage is blocked; frames are
    counted but not written`. Level-triggered on `disk_blocked is True`
    (`scripts/runtime/check_fleet_liveness.py:431`, raised at `:433`). One
    unit named, a `market_tape` recorder, research tape outside the order
    path; pid 2263691 unchanged, host still `65ee75a7`. **The funded engine is
    not implicated**, and the 25 GiB floor is the reservation held for
    mainnet's WAL, which `writable()` blocks the recorder *above*
    (`market_tape/storage.py:426-433`).
  - **The payload: 840.5 s clean, then the crossing at 03:18:35.057.** Every
    line from 03:04:04.563 reads `disk_blocked=False` with `disk_dropped`
    frozen at 5 651 210 — 1 010 501 frames taken and 1 010 470 rows written,
    1 161/s each — and the block is 20 frames old at the tick that opens it.
    The 03:21 entry follows the same block through to its 90.1 s end.

    | Reading | 03:04:04.563 | 03:18:35.057 |
    | :--- | ---: | ---: |
    | `frames` | 25 548 129 | 26 558 630 |
    | `rows` | 19 896 830 | 20 907 300 |
    | `disk_dropped` | 5 651 210 | 5 651 230 |
    | `disk_blocked` | `False` | `True` |
  - **No Binance pass deleted a file in 871 s.** `_retention_pass` logs
    `retention removed` only when `prune` returned paths
    (`market_tape/record.py:1166`) and the excerpt carries no such line, while
    Binance's last three logged passes — 02:49:33, 02:54:36, 02:59:39, spaced
    302.6 s and 302.5 s — put the next three at ≈03:04:42, ≈03:09:44 and
    ≈03:14:47, all inside it. On the deployed `prune` a pass deletes nothing
    only when no file is past `retention_days`, the root is under `max_bytes`,
    and free space is at or above the floor
    (`65ee75a7:market_tape/storage.py:362`). This is the same reading the
    03:21 entry takes independently on the Bybit unit over 990.8 s: **neither
    tape is at its `max_disk_gb` cap, and `min_free_disk_gb` is what binds.**
  - **The sixth defect, in `3c1ebd22` and not on the host.** A pass frees to
    `free_target = min_free_bytes + 5%` (`market_tape/storage.py:383`,
    `FREE_HEADROOM_FRACTION` at `:47`) — 25 GiB + 1.25 GiB — so the first pass
    of a crossing unlinks `F1 ≈ free_target − S1`, where `S1` is the statvfs
    reading it opened with. It is owed a successor when the next `writable()`
    still reads under the floor (`market_tape/record.py:1178`). The credited
    successor opens with `free = S2 + F1` (`market_tape/storage.py:396`) and
    so deletes nothing as soon as **`S2 ≥ S1`** — which holds the moment the
    kernel has shown any part of the release and the neighbour has not taken
    more than the pass freed. That is the ordinary case here: the 02:33, 02:48
    and 03:00 entries each show a crossing decided by the other recorder's
    pass, and the 03:21 entry shows one 8-file Binance pass opening both
    units' gates. The burst then ends with `disk_blocked` still `True`,
    `_maintenance` armed `prune_now` on the crossing only, and `_write_loop`
    never reaches an append to fail on — so the pruner waited out
    `RETENTION_INTERVAL_SECONDS` (`market_tape/record.py:99`, waited at
    `:1148`) with a tape it could still trim. `06e17d4a` closed that hole by
    spinning until a fresh statvfs agreed, which is what cost the whole tape;
    `3c1ebd22` stopped the spin and reopened the hole.
  - **The fix (`1702d14d`).** `_maintenance` arms `prune_now` on every blocked
    tick rather than on the crossing (`market_tape/record.py:1201`). A block
    is then bounded by one `status_interval_seconds` — 30 s on both recorders
    — plus a walk, instead of 300 s. It reverses the rationale `1d8fad9a`
    wrote in ("while blocked nothing is written, so a repeated pass has
    nothing new to delete"), which is false on a shared filesystem: the tape
    is not growing, but free space moves under it, and that is the whole
    incident.
  - **What it does not cost.** Not tape: a pass deletes down to `free_target`
    and no further, so against a foreign writer consuming at rate `R` the tape
    gives up about `R` per unit time at either cadence — 30-second passes
    unlink ~`30R` each where 300-second passes unlink ~`300R`. What changes is
    only how long the writer is gated. The added cost is one `rglob` walk per
    status tick while blocked, on the pruner thread that exists so a walk
    never touches the heartbeat; the largest pass of this incident, 410
    unlinks, took 0.17 s.
  - **Tests.**
    `tests/market_tape/test_record.py::test_a_blocked_tick_runs_the_pass_the_credited_burst_stopped_short_of`
    drives the real pruner thread over 20 files with a statvfs that never
    moves: the credited burst ends after 2 passes with 17 files left and the
    gate shut, then one `_maintenance()` tick produces the next burst — 4
    passes, 14 files. `test_a_disk_under_the_free_floor_prunes_now_instead_of_waiting_out_the_interval`
    now asserts a still-blocked tick wakes the pruner, replacing the assertion
    that it must not. Reverting the two-line arming change fails both. Full
    `scripts/dev.sh check` with `zstd` and `rsync` installed in the container:
    1465 passed, ruff and mypy clean, `cargo clippy` and every engine suite
    green; ShellCheck is not installed here and CI runs it.
  - **The one action that ends this needs no runner**, from a workstation
    holding the SSH key:

    ```sh
    EXPECTED_COMMIT=1702d14d1380d7bbe26eb0425b7811a3eeeeb2b8 scripts/ops.sh deploy
    ```

    `1702d14d` carries all six recorder fixes. **Do not deploy `06e17d4a`**
    (uncredited retry, deletes the tape roots in this host's state) and do not
    deploy `3c1ebd22` on its own (the burst ends with the gate shut and the
    block still runs 300 s). Either way the deploy **hands over both realms
    and restarts the funded engine**, because the fingerprint hashes the whole
    `engine` tree and the chain already carries `697341e4` and `10ed1bd2`.
    That is the owner's call, which is why the recipe is written for a human
    and not dispatched from here.

- **2026-09-05 03:00 UTC — The ninth page from the same free-space floor, and
  the first one that finds a fifth defect. It is not on the host: it is in
  `06e17d4a`, the fix eight entries have been telling the owner to deploy. The
  owed-successor retry re-derives its deficit from the same statvfs that made
  the successor owed, so it deletes the deficit again on every retry, back to
  back, until the tape has no file left. This payload shows the exact
  condition that fires it — 120.1 s in which neither recorder wrote a row, a
  7-file pass already done, and free space still under the floor. Fixed in
  `3c1ebd22`; the recipe below now points there, not at `06e17d4a`. Also the
  longest block of the incident: 390.3 s and 1 100 454 frames.**
  - Incident `host-16171e3c5e186136`, scope `host`, host `ip-208-84-103-4`,
    `new_critical_refs=capture-disk:forward-market-binance` — the Binance-ref
    id, the same one the 02:33 entry carried. Exact alert text: `CRITICAL
    recorder forward-market-binance storage is blocked; frames are counted but
    not written`. Level-triggered on `disk_blocked is True`
    (`scripts/runtime/check_fleet_liveness.py:431`, raised at `:433`).
  - **The funded engine is not implicated and the host has not moved.** No
    engine, worker or timer is named; both units are `market_tape` recorders,
    research tape outside the order path. Pids are unchanged across all nine
    pages — 2259813 (Bybit), 2263691 (Binance) — so neither recorder has
    restarted and the host still runs `65ee75a7`. The 25 GiB floor is the
    reservation held for mainnet's WAL: `writable()` blocks the recorder
    *above* it (`market_tape/storage.py:426-433`), so the reservation is
    intact by construction and stayed intact.
  - **The host is still on the un-fixed code.** Binance's passes are 02:49:33,
    02:54:36 and 02:59:39 — 302.6 s and 302.5 s apart. Bybit's are 02:47:04,
    02:52:09 and 02:57:14 — 304.9 s and 305.1 s. That is the bare
    `RETENTION_INTERVAL_SECONDS` clock (`market_tape/record.py:99`, waited out
    at `:1148`) with nothing woken by a crossing.
  - **The fifth defect, in `06e17d4a` and not on the host.** `_retention_loop`
    keeps passing while a pass is owed a successor, and a successor is owed
    when the pass deleted and `writable()` still reads under the floor
    (`market_tape/record.py:1151-1181`). That disagreement is the whole
    premise: `prune` carries free space forward from the sizes it unlinked
    because "a filesystem need not release a deleted file's blocks by the time
    the next statvfs returns" (`market_tape/storage.py:362-368`). The successor
    then calls `prune` again, which opens with `free =
    shutil.disk_usage(self.root).free` — the number that was wrong — derives
    the same deficit from it, and deletes that much tape a second time. There
    is no delay between retries and the only exit is a pass that deletes
    nothing, so on a floor held by something other than tape the loop unlinks
    every non-snapshot file the recorder holds in the time it takes to walk
    the tree a few times. The `06e17d4a` commit message asserts the opposite
    ("a disk filled by something other than tape is walked once rather than
    spun on"); that holds only for a tape that is already empty. The existing
    test stubbed `prune` with a fixed one-path return, so no test ever ran a
    real pass twice in one burst (`tests/market_tape/test_record.py:1166`).
  - **The payload evidence that the trigger is live on this host.** Two
    windows in which the disk stayed under the floor while the tape was not
    the thing consuming it:

    | Window | Bybit rows | Binance rows | Tape passes inside it |
    | :--- | ---: | ---: | :--- |
    | 02:47:34 → 02:49:34 (120.1 s) | 0 | 0 | Bybit 7 files at 02:47:04 |
    | 02:58:11 → 03:00:34 (143.1 s, still open) | 0 | 0 | Binance 5 files at 02:59:39 |

    In the first, four consecutive status ticks read `disk_blocked=True` on
    both units with neither writing a byte of tape, and it took a 410-file
    Binance pass at 02:49:33 to clear it. In the second the payload ends with
    both recorders blocked and Binance's own pass 55.4 s behind it having
    changed nothing. Deploying `06e17d4a` into that state would put the
    pruner into an uncredited retry burst against a floor the tape does not
    hold, and it would delete the tape roots instead of resolving the
    crossing.
  - **The fix (`3c1ebd22`).** `prune` takes `free_credit`, added to its
    statvfs reading, and records what it unlinked in `last_freed_bytes`;
    `_retention_loop` accumulates the burst's total and credits each
    successor. The first pass of a burst is unchanged, so nothing about a
    normal crossing moves. A burst now deletes its deficit once and stops; the
    next scheduled pass, or the next crossing, re-derives against a fresh
    reading. Tests:
    `tests/market_tape/test_tape_storage.py::test_a_successor_pass_credits_what_the_burst_already_unlinked`
    pins a filesystem that releases no unlinked block and asserts the credited
    successor deletes nothing, and
    `tests/market_tape/test_record.py::test_a_burst_of_owed_passes_deletes_the_deficit_once_not_the_whole_tape`
    drives the real pruner thread through the same filesystem and asserts the
    burst is 2 passes leaving 17 of 20 files. Without the credit — reverting
    either half alone — the burst is 8 passes and the tape is empty. Full run:
    1462 passed, plus the 2 `backup_state.sh` tests this container fails for
    want of `rsync`, which fail identically on a clean tree.
  - **The longest block of the incident, and a recorder's own pass still does
    not bound it.** Bybit's rows froze at 58 081 690 from 02:45:41 to
    02:52:11 — **390.3 s, 1 100 454 frames discarded at 2 820/s** — straight
    through its own 02:47:04 pass of 7 files, which the 02:47:11 tick 6.8 s
    later still read as blocked. The 02:48 entry's 330.3 s maximum is beaten
    by 60 s. Binance's own worst was 02:51:34 → 02:57:34, 360.2 s and 385 645
    frames, straight through its own 106-file pass at 02:54:36, and it ended
    20.2 s after **Bybit's** 4-file pass at 02:57:14. Each pruner is still the
    other's only source of room.
  - **Pass size and recovery, a third independent measurement.**

    | Pass (Binance) | Files | Gate opens | Delay |
    | :--- | ---: | :--- | ---: |
    | 02:49:33.944 | 410 | 02:49:34.115 | **0.17 s**, then re-blocked at the next tick |
    | 02:54:36.563 | 106 | 02:57:34.362 | **177.8 s**, and by Bybit's pass, not this one |
    | 02:59:39.045 | 5 | — | still shut 55.4 s later at the last line |

    The largest pass of the whole incident bought one 30-second tick, in which
    the unit wrote 28 275 rows and crossed the floor again. Bybit's 5-file
    pass at 02:52:09 opened its own gate in 2.18 s and its 7-file pass at
    02:47:04 opened nothing. Pass size is not the dial, and it never measured
    room freed for the floor in the first place: `prune` deletes for age and
    for `max_bytes` in the same walk (`market_tape/storage.py:404`), so a file
    count conflates all three. The manifest is where that separates — every
    unlink appends `compressed_bytes` and a `reason` of `age` or `disk_limit`
    (`market_tape/storage.py:412-421`).
  - **The host readings that would settle it, which the routine cannot take.**
    Whether tape or non-tape growth holds `/var/lib` under 25 GiB is still the
    open question, and the two windows above are where to look. From a
    workstation with the SSH key:

    ```sh
    scripts/ops.sh status                 # deployed commit, unit heartbeats, disk
    scripts/ops.sh curve mainnet 240      # the minute samples through the incident
    ```

    and on the host, free space against the two tape roots' own footprints,
    plus the bytes each pass actually freed:

    ```sh
    df -h /var/lib
    du -sh /var/lib/liquidity-migration/forward-market \
           /var/lib/liquidity-migration/forward-market-binance \
           /var/lib/liquidity-migration-engine-mainnet \
           /var/log/journal
    tail -n 20000 /var/lib/liquidity-migration/forward-market-binance/manifest.jsonl \
      | python3 -c 'import json,sys,collections
    b=collections.Counter()
    for line in sys.stdin:
        row=json.loads(line)
        if row.get("kind","").endswith("_deleted"):
            b[row["reason"]]+=row["compressed_bytes"]
    print({k: round(v/2**30, 3) for k, v in b.items()}, "GiB")'
    ```

    If the two tape roots sum well under their 60 + 18 GB caps while the disk
    is at the floor, the room went somewhere else and the caps are not the
    dial to turn. The manifest sum says the same thing from the other side: a
    pass that freed hundreds of megabytes and bought one status tick is a
    floor being re-crossed by a writer that is not the tape.
  - Loss, cumulative and never reset. Both windows are cut by the 40-line
    payload, so every figure is a lower bound.

    | Unit | First line | Last line | Added since the 02:48 entry | Rows kept |
    | :--- | ---: | ---: | ---: | ---: |
    | Bybit `forward-capture` | 12 894 777 (02:43:40) | 15 257 969 (03:00:11) | 1 700 123 | 248 522 |
    | Binance `forward-capture-binance` | 4 562 699 (02:44:33) | 5 473 240 (03:00:34) | 752 691 | 100 498 |
    | **Pair** | | **20 731 209** | **2 452 814** | **349 020** |

    Over the 750.4 s since the 02:48 entry's last lines that is **3 268 frames
    a second discarded**, a shade worse than that entry's 3 224/s and still
    the worst rate of the incident. Across this payload's own window the pair
    threw away **9.38 frames for every row it kept** (Bybit 9.51, Binance
    9.06). At the last line both recorders are inside an open block.
  - **Deploy refused a sixteenth time, same signature.** Run `33941368676`,
    `deploy main@02062266`, dispatched 03:15:54 UTC and failed 03:16:00 — 6 s.
    `Deploy artifact` dead 2 s in, `ci` and `rust` 3 s in, each of their log
    downloads returning `failed to download logs: HTTP 404`; `diagnose`,
    `disarm`, `vps` and the release-test job skipped. No job ever started, so
    nothing reached the host: deployed commit stays `65ee75a7` and all five
    recorder fixes stay merged and undeployed. The cause is outside the
    repository — the account's payments failed, so GitHub assigns no runner.
  - **The one action that ends this tonight needs no runner**, from a
    workstation holding the SSH key:

    ```sh
    EXPECTED_COMMIT=3c1ebd22bb78fac6fabfcf3370836bbec32e9527 scripts/ops.sh deploy
    ```

    `3c1ebd22` carries all five recorder fixes. **Do not deploy `06e17d4a`**:
    it carries the uncredited retry, and this payload shows the host in the
    state that turns it into a tape-deleting loop. `3c1ebd22` still **hands
    over both realms and restarts the funded engine**, because the fingerprint
    hashes the whole `engine` tree and `06e17d4a` already carried `697341e4`
    and `10ed1bd2`; that handover is the known cost, the same one STATE.md
    records against `10ed1bd2`. It is the owner's call, which is why the
    recipe is written for a human and not dispatched from here.

- **2026-09-05 02:48 UTC — The eighth page from the same free-space floor, and
  it falsifies the 02:33 entry's ceiling. Still no fifth defect and no code
  change: every mechanism in this payload is the deployed behaviour of the
  four merged, undeployed fixes. What is new is that a block is *not* bounded
  by `RETENTION_INTERVAL_SECONDS` — the Bybit unit was blocked 330.3 s
  straight through its own retention pass — and that pass size buys nothing:
  a 290-file pass left the writer gated for 95.1 s while neither recorder was
  writing, where a 230-file pass five minutes earlier opened the gate in
  7.3 s. Deploy refused a fifteenth time.**
  - Incident `host-ecbac293ecc90d5e`, scope `host`, host `ip-208-84-103-4`,
    `new_critical_refs=capture-disk,capture-disk:forward-market-binance` —
    both refs new in the same page, which is why the id is the pair-hash seen
    at 00:01, 00:36, 01:32 and 01:53 rather than either single-ref id. Exact
    alert text, both lines: `CRITICAL recorder storage is blocked; frames are
    counted but not written` and `CRITICAL recorder forward-market-binance
    storage is blocked; frames are counted but not written`. Level-triggered
    on `disk_blocked is True`
    (`scripts/runtime/check_fleet_liveness.py:431`, raised at `:433`).
  - **The funded engine is not implicated and the host has not moved.** No
    engine, worker or timer is named; both units are `market_tape` recorders,
    research tape outside the order path. Pids are unchanged across all eight
    pages — 2259813 (Bybit), 2263691 (Binance) — so neither recorder has
    restarted and the host still runs `65ee75a7`. The 25 GiB floor is the
    reservation held for mainnet's WAL: `writable()` blocks the recorder
    *above* it (`market_tape/storage.py:415-422`), so the reservation is
    intact by construction and stayed intact.
  - **The host is still on the un-fixed code.** Binance's passes are 02:34:26,
    02:39:28 and 02:44:31 — 302.6 s and 302.5 s apart. Bybit's are 02:36:53,
    02:41:58 and 02:47:04 — 304.9 s and 305.6 s. That is the bare
    `RETENTION_INTERVAL_SECONDS` clock (`market_tape/record.py:99`, waited out
    at `:1140`) with nothing woken by a crossing. `1d8fad9a`, `d275885a`,
    `fd604613` and `06e17d4a` are all still merged and undeployed.
  - **The correction: `RETENTION_INTERVAL_SECONDS` is not the ceiling.** The
    02:30 entry bounded an unrescued block at "300 s, about 863 000 frames on
    this unit"; the 02:33 entry measured one at 240.2 s / 691 512 and
    generalised from that single sample to "`RETENTION_INTERVAL_SECONDS` is
    the ceiling; the expected cost is about half of it, since an unrescued
    crossing ends on the crossing recorder's own next scheduled pass." This
    payload holds a block that beats both. **Bybit was blocked 02:37:40 →
    02:43:10 — 330.3 s — and discarded 903 098 frames at 2 734/s.** Its own
    next scheduled pass fell *inside* that block, at 02:41:58, deleted 4 files,
    and did not end it: the ticks at 02:42:10 and 02:42:40 both still read
    `disk_blocked=True`, and the gate opened only at 02:43:10, **72.3 s and
    two status ticks after its own pass**. A recorder's own pass is therefore
    not a bound on its block, and neither is the pruner's clock. The 02:33
    entry's generalisation is withdrawn; its measurement of that one block
    stands.
  - **The mechanism, and why it is still `d275885a` and not a fifth defect.**
    `prune` stops deleting the instant its counted free reaches the floor
    (`market_tape/storage.py:394`, `pressured = total > self.max_bytes or free
    < free_target`, with `free_target == min_free_bytes` on the deployed tip),
    and `writable()` unblocks on that same number
    (`market_tape/storage.py:422`). So a pass ends with a margin equal to the
    overshoot of whichever file happened to be unlinked last — a few
    megabytes, and randomly so. Bybit's 4-file pass at 02:41:58 freed that
    margin, Binance's gate opened 4.8 s later at 02:42:03, and Binance wrote
    35 546 rows in the next 30 s and took all of it. That is the 02:33
    entry's "each pruner is the other's only source of room", now costing the
    other recorder 72 s instead of one tick.
  - **The measurement that pass size does not buy recovery, and where it runs
    out of evidence.**

    | Pass (Binance) | Files | Gate opens | Delay |
    | :--- | ---: | :--- | ---: |
    | 02:34:26.182 | 230 | 02:34:33.490 | **7.3 s** |
    | 02:39:28.740 | 290 | 02:41:03.844 | **95.1 s** |
    | 02:44:31.241 | 27 | 02:45:03.966 | **32.7 s** |

    The largest pass of the whole incident bought the worst recovery. It is
    the margin at the stopping point that sets recovery, not the size of the
    pass, and that margin is zero by construction. **But the 02:39:28 case is
    where the payload stops.** Across 02:39:28 → 02:41:03 the Bybit unit was
    blocked too (`rows` frozen at 57 895 402 from 02:37:40 to 02:43:10), so
    *neither recorder wrote tape*, and free space on `/var/lib` still sat
    under the floor for three consecutive status ticks after 290 files were
    unlinked. Binance discarded 87 307 frames in that window for nothing. The
    tape did not eat that room. Either the filesystem had not released the
    unlinked blocks, or a non-tape writer on `/var/lib` did — and at zero
    margin a non-tape writer of order 10^5 B/s is enough to re-cross the floor
    within one tick of any pass, however large.
  - **The host reading that would settle it, which the routine cannot take.**
    Whether tape or non-tape growth holds `/var/lib` under 25 GiB is still the
    open question, now with a specific window to look at. The owner can run,
    from a workstation with the SSH key:

    ```sh
    scripts/ops.sh status                 # deployed commit, unit heartbeats, disk
    scripts/ops.sh curve mainnet 240      # the minute samples through the incident
    ```

    and, on the host, the two numbers the payload cannot supply — total free
    space against the two tape roots' own footprints:

    ```sh
    df -h /var/lib
    du -sh /var/lib/liquidity-migration/forward-market \
           /var/lib/liquidity-migration/forward-market-binance \
           /var/lib/liquidity-migration-engine-mainnet \
           /var/log/journal
    ```

    If the two tape roots sum well under their 60 + 18 GB caps while the disk
    is at the floor, the room went somewhere else and the caps are not the
    dial to turn.
  - **No code changed, and what would change that.** Everything above is
    reproduced by the four fixes already on `main` and already covered by
    tests: the 300-second sleep (`1d8fad9a`), the pass that stops on the floor
    (`d275885a`, `FREE_HEADROOM_FRACTION` at `market_tape/storage.py:47`), the
    gate only `_maintenance` opened (`fd604613`), and the pass that falls
    short with no successor (`06e17d4a`) — see
    `tests/market_tape/test_record.py:1013-1190`. A fifth defect would be a
    crossing that survives all four *on the deployed tip*, and none of the
    four is on the host, so this payload cannot contain one. Adding a guard
    here would be machinery around a symptom whose fix is already merged and
    waiting on a runner.
  - Loss, cumulative and never reset. Both windows are cut by the 40-line
    payload, so every figure is a lower bound.

    | Unit | First line | Last line | Added since the 02:33 entry | Rows kept |
    | :--- | ---: | ---: | ---: | ---: |
    | Bybit `forward-capture` | 11 189 066 (02:31:40) | 13 557 846 (02:48:11) | 2 208 860 | 377 421 |
    | Binance `forward-capture-binance` | 3 995 011 (02:32:33) | 4 720 549 (02:48:04) | 694 709 | 226 654 |
    | **Pair** | | **18 278 395** | **2 903 569** | **604 075** |

    Over the 900.6 s since the 02:33 entry's last line that is **3 224 frames
    a second discarded**, the worst sustained rate of the incident and three
    times its 1 048/s average. The tape now throws away **4.81 frames for
    every row it keeps** (Bybit 5.85, Binance 3.07). At the last line Bybit is
    150.1 s into an open block with 422 826 frames already gone.
  - **Deploy refused a fifteenth time, same signature.** Run `33940468461`,
    `deploy main@28dc27a6`, dispatched 02:56:17 UTC and failed 02:56:23 — 6 s.
    `ci`, `rust` and `Deploy artifact` all dead 3 s in (02:56:19 → 02:56:22),
    every one of their log downloads returning `failed to download logs: HTTP
    404`; `diagnose`, `disarm`, the release-test job and `vps` skipped. No job
    ever started, so nothing reached the host: deployed commit stays
    `65ee75a7` and all four recorder fixes stay merged and undeployed. This is
    the fifteenth consecutive refusal since 19:17 UTC on 2026-09-04 and the
    cause is outside the repository — the account's payments failed, so GitHub
    assigns no runner. **The one action that ends this incident tonight needs
    no runner and only the owner can take it**, from a workstation holding the
    SSH key:

    ```sh
    EXPECTED_COMMIT=06e17d4a82f9a5a19e00f1cd0928b4a0da96e315 scripts/ops.sh deploy
    ```

    `06e17d4a` is the commit carrying all four recorder fixes. Everything
    between it and the current tip `28dc27a6` is `CHANGELOG.md` and `STATE.md`
    only — the two trees' `engine` directories are identical — so either
    commit installs the same thing, and either one **hands over both realms
    and restarts the funded engine**, because the fingerprint hashes the whole
    `engine` tree and `06e17d4a` already carries `697341e4` and `10ed1bd2`.
    That handover is the known cost of this deploy, not a surprise: it is the
    same one STATE.md records against `10ed1bd2`. It is the owner's call to
    make, and it is why the recipe is written for a human and not dispatched
    from here.

- **2026-09-05 02:33 UTC — The seventh page from the same free-space floor.
  No new defect: the host still runs the un-fixed code and all four merged
  fixes are still undeployed. What this payload adds is the measurement that
  settles `d275885a` on its own terms — a 187-file pass and a 4-file pass
  bought exactly the same thing, one status tick, because both stop on the
  number `writable()` unblocks on, and the neighbouring recorder on the same
  filesystem never saw the disk unblock at all. No code changed. Deploy
  refused a thirteenth time.**
  - Incident `host-16171e3c5e186136`, scope `host`, host `ip-208-84-103-4`,
    `new_critical_refs=capture-disk:forward-market-binance` — one new ref, and
    a *different incident id* from the 02:30 entry above, which carried
    `host-681737fd16e1f806` with `new_critical_refs=capture-disk`. The two
    pages are the same 02:28 crossing split across the two recorders' refs and
    fired three minutes apart; neither is a new event. Exact alert text:
    `CRITICAL recorder
    forward-market-binance storage is blocked; frames are counted but not
    written`. Level-triggered on `disk_blocked is True`
    (`scripts/runtime/check_fleet_liveness.py:431`, raised at `:433`).
  - **The funded engine is not implicated and the host has not moved.** No
    engine, worker or timer is named; both units are `market_tape` recorders,
    research tape outside the order path. Pids are unchanged across all seven
    pages — 2259813 (Bybit), 2263691 (Binance) — so neither recorder has
    restarted and the host still runs `65ee75a7`. The 25 GiB floor is the
    reservation held for mainnet's WAL: `writable()` blocks the recorder
    *above* it (`storage.py:415-422`), so the reservation is intact by
    construction and stayed intact.
  - **The host is still on the un-fixed code, and the pass cadence proves
    it.** Bybit's last recorded pass was 01:51:11.497 and its next is
    02:31:48.655 — 2437.158 s, eight intervals of 304.6 s. Binance's were
    01:49:07.376 and 02:29:23.499 — 2416.123 s, eight of 302.0 s. That is the
    bare `RETENTION_INTERVAL_SECONDS` clock plus each pass's walk, with
    nothing woken by a crossing: Bybit crossed at ~02:28:08 and waited 220 s
    for a pass, Binance crossed at ~02:28:00 and waited 83 s. `1d8fad9a`,
    `d275885a`, `fd604613` and `06e17d4a` are all still merged and
    undeployed.
  - **The new measurement: pass size does not matter when the pass stops on
    the floor.** Both recorders write to `/var/lib` (`--root
    /var/lib/liquidity-migration/forward-market` and `…-binance`), one
    filesystem, and both carry `min_free_disk_gb = 25`
    (`deploy/capture/bybit-linear.toml:28`,
    `deploy/capture/binance-usdm.toml:32`).

    | Time (UTC) | Event | Bybit `disk_blocked` | Binance `disk_blocked` |
    | :--- | :--- | :--- | :--- |
    | 02:28:03.286 | | | `True` (first) |
    | 02:28:10.252 | | `True` (first) | |
    | 02:29:23.499 | *Binance: `retention removed 187 tape files`* | | |
    | 02:30:03.351 | Binance's gate opens, `rows` +5 | | `False` |
    | 02:30:10.359 | **still blocked, 46.9 s after that pass** | `True` | |
    | 02:30:33.364 | Binance re-blocked, `rows` +29 393 | | `True` |
    | 02:31:48.655 | *Bybit: `retention removed 4 tape files`* | | |
    | 02:32:03.411 | Binance's gate opens, `rows` +24 | | `False` |
    | 02:32:10.440 | Bybit's gate opens, `rows` +59 | `False` | |
    | 02:32:33.427 | Binance re-blocked, `rows` +31 158 | | `True` |
    | 02:32:40.463 | Bybit re-blocked, `rows` +92 478 | `True` | |

    Binance unlinked 187 files and Bybit unlinked 4, and the two passes bought
    the identical thing: one 30-second status tick for the recorder that ran
    the pass. Binance's 187-file pass did not open Bybit's gate at all — 46.9 s
    later Bybit still read the disk as full, because Binance had resumed
    writing 7 s earlier and `prune` had left it exactly zero margin. That is
    `prune`'s `pressured` test and `writable()` sharing one threshold
    (`storage.py:394` and `storage.py:422`), which is what `d275885a` fixes,
    and it is now measured on a pass 47× larger than the one that reproduced
    it at 00:55.
  - **The other half is the shared filesystem, and it cuts both ways.**
    Binance's 02:32:03 unblock followed no pass of its own — its last was
    02:29:23. It came 14.8 s after *Bybit's* 02:31:48 pass. Each recorder's
    pruner is the other's only source of room between its own 300-second
    walks, and with no headroom in either pass, a recorder that resumes first
    takes the whole of what the other freed.
  - **What that block cost, on the Bybit unit, and it closes the 02:30
    entry's projection.** The 02:30 page's payload was cut at 02:30:10 with
    the Bybit block still open and no retention line anywhere in it, so that
    entry could only bound the cost by the pruner's clock: "300 s, about
    863 000 frames on this unit". This payload runs three minutes further and
    holds the end of that same block. Bybit was blocked 02:28:10 → 02:32:10 —
    **240.2 s, not 300**, ended by its own 02:31:48 pass plus the 22 s to the
    next status tick — and discarded **691 512 frames**, not 863 000, at
    2 559/s. `rows` were frozen at 57 611 732 for the whole of it and then
    moved 59. The interval that followed, with the gate open for essentially
    all of it, carried 92 478 rows (3 082/s) and dropped 43 frames; at that
    rate the 240.2 s would have carried about 740 000 rows. The 02:30 entry's
    reading of the shape is right and its arithmetic was an upper bound: an
    unrescued crossing ends on the crossing recorder's *own* next scheduled
    pass, so `RETENTION_INTERVAL_SECONDS` is the ceiling and the expected cost
    is half of it. Its two `retention removed` lines are also the correction
    to that entry's "at least two scheduled passes fell inside it and deleted
    nothing" — the passes were not inside its window, they were 3 min 13 s and
    5 min 38 s past its last line, and both deleted.
  - **No fifth defect, and what would change that.** Everything in this
    payload is the deployed behaviour of `65ee75a7`: the 300-second clock
    (`1d8fad9a`), the pass that stops on the floor (`d275885a`), the gate that
    only `_maintenance` opens (`fd604613`, visible as the 40.1 s between
    Binance's pass and its own unblock), and the pass that falls short with no
    successor (`06e17d4a`, visible as Bybit's 220 s wait). A crossing that
    survives all four on the deployed tip — a retention pass that deletes and
    the disk still full within one interval — would be a fifth, and this
    payload contains no such thing because none of the four is on the host.
  - Loss, cumulative and never reset. Both windows are cut by the 40-line
    payload, so every figure is a lower bound.

    | Unit | First line in payload | Last line | Added since the 01:53 entry |
    | :--- | ---: | ---: | ---: |
    | Bybit `forward-capture` | 10 580 038 (02:18:39) | 11 348 986 (02:33:10) | 1 835 784 |
    | Binance `forward-capture-binance` | 3 783 086 (02:16:32) | 4 025 840 (02:33:03) | 684 783 |
    | **Pair** | | **15 374 826** | **2 520 567** |

    Over the 2 404 s since the 01:53 entry's last line that averages 1 048
    frames a second, a third of the 3 127/s that entry recorded — but the
    average hides the shape. Binance dropped nothing from 02:16:32 to
    02:27:33 and Bybit nothing from 02:18:39 to 02:27:40, at least nine clean
    minutes, and then both crossed within 7.0 s of each other. Inside the
    oscillation that followed the pair ran at **3 372 frames a second**
    (Bybit 2 563/s over 02:28:10–02:33:10, Binance 809/s over
    02:28:03–02:33:03) — the worst rate of any window in this incident.
  - Checks run: none needed. This entry changes `CHANGELOG.md` and `STATE.md`
    only; no Python, Rust, config or unit file is touched.
  - **Deploy receipt: refused a fourteenth time.** This entry's own push was
    dispatched on `cdefebcd` at 02:41:10 UTC as run `33939774257` and failed
    at 02:41:17: `rust` and `Deploy artifact` dead 3 s in at 02:41:15, `ci`
    4 s in at 02:41:16; `diagnose` and `disarm` skipped at 02:41:12, the
    release-test job and `vps` at 02:41:16-17. Nothing reached the host.
  - **The thirteenth, dispatched for the 02:30 page.** Run `33939474310`,
    `deploy` on `main@0af3fc29`, dispatched 02:34:52 UTC and failed at
    02:34:59. `ci` dead 3 s in at 02:34:56, `Deploy artifact` 4 s in at
    02:34:57, `rust` 5 s in at 02:34:58; `diagnose` and `disarm` skipped at
    02:34:53, `vps` and the release-test job skipped at 02:34:58. Nothing
    reached the host. Same no-job-ever-started signature as `33938359607`,
    `33937280978`, `33934970737`, `33934851698`, `33933927629`,
    `33933636343`, `33932188757`, `33931474693`, `33928248402`,
    `33922197522`, `33921858031` and `33911912004`. It is the account's
    failed payments, not any commit. Deployed commit stays `65ee75a7`; all
    four recorder fixes are merged and undeployed, and the recorders keep
    crossing the floor until the owner runs the SSH path below.
  - Host action, and only the owner can run it. `06e17d4a` carries all four
    recorder fixes and this entry's tip carries no code, so either installs
    them; `capture_fingerprint` (`scripts/deploy_vps_live.sh:524-534`) hashes
    every `market_tape/*.py`, so `start_independent_units` restarts both
    recorders on the new code and no hand restart is needed. It also hands
    over both realms — the engine fingerprint hashes the whole `engine` tree
    — so the funded engine restarts:
    ```bash
    EXPECTED_COMMIT=06e17d4a82f9a5a19e00f1cd0928b4a0da96e315 scripts/ops.sh deploy
    scripts/ops.sh status
    ```
    Then the reading still open since 22:54 — whether tape or non-tape files
    hold the room, which decides whether the caps in `deploy/capture/*.toml`
    also want revisiting once the recorders stop blocking — and the equity and
    heartbeat record through the incident:
    ```bash
    df -h /var/lib
    du -sh /var/lib/liquidity-migration/forward-market \
           /var/lib/liquidity-migration/forward-market-binance
    du -sh --exclude=forward-market --exclude=forward-market-binance \
           /var/lib/liquidity-migration
    scripts/ops.sh curve mainnet 60
    ```
    The billing block is the root cause of the deploy half of this and is not
    fixable from here: GitHub has refused every Actions run since 18:03 UTC on
    2026-09-04.
- **2026-09-05 02:30 UTC — The seventh page from the same free-space floor.
  No new defect, and no code changed: this one measures the *first* defect
  alone, with nothing to rescue it. The 02:28:10 crossing woke no retention
  pass, and 120.1 s later the Bybit writer was still gated with `rows`
  frozen at the value it held on the crossing — where the six pages before it
  measured a 30-second oscillation, because a neighbouring pass happened to
  free room. A blocked writer's cost is bounded by the pruner's 300-second
  clock, not the 30-second status tick. All four recorder fixes stay merged
  and undeployed: the thirteenth dispatched deploy was refused 7 s in.**
  - Incident `host-681737fd16e1f806`, scope `host`, host `ip-208-84-103-4`,
    `new_critical_refs=capture-disk` — the same incident id and alert text as
    the 22:54, 23:50, 00:01, 00:36, 00:57, 01:32 and 01:53 pages, and for the
    first time in the run only the Bybit ref is new. Exact alert text:
    `CRITICAL recorder storage is blocked; frames are counted but not
    written`, with `WARNING recorder dropped 345496 frames since the last
    check (storage was blocked)` and the same warning for
    `forward-market-binance` at 119 723. Level-triggered on `disk_blocked is
    True` (`scripts/runtime/check_fleet_liveness.py:431`).
  - **The funded engine is not implicated and the host has not moved.** No
    engine, worker or timer is named; the unit is a `market_tape` recorder,
    research tape outside the order path. Pid 2259813 is unchanged across all
    seven pages, so the recorder has not restarted and the host still runs
    `65ee75a7`. The 25 GiB floor is the reservation held for mainnet's WAL
    and it held.
  - **Measured on this payload, on the Bybit unit.**

    | Line (UTC) | `disk_blocked` | `disk_dropped` | `rows` |
    | :--- | :--- | ---: | ---: |
    | 02:27:10.207 | `False` | 10 580 038 | 57 377 372 |
    | 02:27:40.231 | `False` | 10 580 038 | 57 471 818 |
    | 02:28:10.252 | `True` | 10 580 094 | 57 611 732 |
    | 02:28:40.278 | `True` | 10 664 393 | 57 611 732 |
    | 02:29:10.298 | `True` | 10 749 725 | 57 611 732 |
    | 02:29:40.324 | `True` | 10 838 702 | 57 611 732 |
    | 02:30:10.359 | `True` | 10 925 563 | 57 611 732 |

    The 691 s before the crossing carried no disk drop at all — `disk_dropped`
    is flat at 10 580 038 from the payload's first line, 02:16:09.645. The
    interval that ended at 02:27:40.231 wrote 94 446 rows in 30.024 s, 3 146
    rows/s. The 120.107 s from 02:28:10.252 to the last line wrote **0** rows
    and discarded 345 469 frames, 2 876 frames/s; at the rate of the interval
    before it that window carried about 378 000 rows.
  - **Which thread saw it, to the millisecond.** No `capture storage blocked;
    frames will be counted but not written` line appears anywhere in the
    excerpt, so `_write_loop` never reached an append that failed
    (`record.py:1106-1115` on `main`). `_maintenance` is what saw it, on its
    02:28:10 tick: the 56 frames between 10 580 038 and 10 580 094 are what
    the writer gated between the assignment and `_write_status` — 19 ms at
    that line's own drop rate.
  - **The diagnosis, on the code the host runs (`65ee75a7`).**

    | Deployed line | What it does | Fixed on `main` by |
    | :--- | :--- | :--- |
    | `market_tape/record.py:1141` | `_maintenance` is `self.disk_blocked = not self.retention.writable()`. Nothing arms a pass on the crossing. | `1d8fad9a` |
    | `market_tape/record.py:1118-1121` | `_retention_loop` makes one pass, then `self.stop.wait(RETENTION_INTERVAL_SECONDS)`. Only a shutdown shortens that wait. | `1d8fad9a`, `fd604613`, `06e17d4a` |
    | `market_tape/storage.py:362`, `:390` | `prune`'s `pressured` test and `writable()` share `min_free_bytes`, so a pass driven by free space stops on the exact threshold the writer unblocks on. | `d275885a` |

    Together those two `record.py` lines set how long a crossing lasts: up to
    a full `RETENTION_INTERVAL_SECONDS`. On this unit that is 300 s × 2 876
    frames/s ≈ 863 000 frames per unrescued crossing, an order of magnitude
    above the ~90 000 a 30-second rescued one costs.
  - **What is new, and it is a correction of scale, not of cause.** The
    earlier pages read the 30-second period as the defect's cost, and that
    period is the status tick — it only appears when the *other* recorder's
    scheduled pass frees room on the shared filesystem and the next
    `_maintenance` tick notices. This window holds no `retention removed`
    line from either unit across all 840.7 s of it, against a 300-second
    clock, so at least two scheduled passes fell inside it and deleted
    nothing: before the crossing the tape was under `max_disk_gb` and free
    space was over the floor, which is `pressured` returning `False`
    correctly. With no pass to rescue it the block was still open at the last
    journal line. The six earlier pages understated the per-crossing loss.
  - Loss, cumulative since each process started and never reset. Both windows
    are cut by the 40-line payload, so every figure is a lower bound.

    | Unit | First line in payload | Last line | Added since the 01:53 entry |
    | :--- | ---: | ---: | ---: |
    | Bybit `forward-capture` | 10 580 038 (02:16:09) | 10 925 563 (02:30:10) | 1 412 361 |
    | Binance `forward-capture-binance` | journal not in this payload | — | 119 723 since the watchdog's previous check |

    Bybit's 1 412 361 over the 2 224 s since the 01:53 entry's last line
    averages 635/s, but the average hides the shape: 691 s of the excerpt
    dropped nothing and the last 150.1 s dropped 345 525.
  - **The deploy receipt: refused, the thirteenth in a row.** Run
    `33939474310`, `deploy` on `main@0af3fc29`, dispatched 02:34:52 UTC and
    failed at 02:34:59. `ci` dead in 3 s, `Deploy artifact` in 4 s, `rust` in
    5 s; `diagnose`, `disarm`, `vps` and the release-test job all skipped.
    Every failed job's log download returns HTTP 404, so no runner ever
    started — the same failed-account-payment signature as the twelve before
    it, unbroken since 18:03 UTC on 2026-09-04. Nothing reached the host;
    deployed commit stays `65ee75a7`.
  - **What the owner has to run.** The SSH path needs no hosted runner and
    installs all four recorder fixes ([docs/operations.md](docs/operations.md)
    §4):

    ```bash
    EXPECTED_COMMIT=06e17d4a82f9a5a19e00f1cd0928b4a0da96e315 scripts/ops.sh deploy
    ```

    It hands over both realms — the fingerprint hashes the whole `engine`
    tree — so the funded engine restarts. Nothing else ends these crossings.
  - **The open host reading, unchanged from the 00:36 page.** Whether the room
    is going to tape or to something else on `/var/lib` is not decidable from
    a recorder journal. On the host:

    ```bash
    scripts/ops.sh curve mainnet
    df -h /var/lib && du -sh /var/lib/liquidity-migration/*
    ```

    `curve` also shows what the account was worth through the incident and
    which minutes had no heartbeat ([docs/observability.md](docs/observability.md)).
  - **One property of `06e17d4a` the owner should know about before it
    deploys, offered and not built.** The new retry loop ends on the pass that
    deletes nothing (`record.py:1138-1139`). If the disk is being filled
    faster than a pass frees it by something that is *not* tape, every
    successive pass still deletes, so the loop keeps walking and can delete
    far more tape than one crossing needs. Nothing observed tonight does that
    — the crossings are the tape reaching its own operating point — and a
    bound on it would be new machinery, so this is a note for the owner, not a
    change.
  - No code changed: `git diff --stat` is `CHANGELOG.md` and `STATE.md` only.
    Checks run: `pytest tests/repo/test_docs_links.py` (3 passed) on a
    throwaway venv, which is the gate this change can fail. The rest of
    `scripts/dev.sh check` is not runnable in this container — no `.venv`, no
    `ruff`, `mypy`, `shellcheck` or `cargo` — and no code path is touched.

- **2026-09-05 01:53 UTC — The sixth page from the same free-space floor, and
  it measures a fourth defect the three merged fixes do not reach: when a
  retention pass deletes and the gate is still shut, nothing runs another
  pass for a full `RETENTION_INTERVAL_SECONDS`. Bybit's 01:40:59 pass removed
  16 tape files, the gate stayed shut, and its own pruner did not walk again
  for 306.2 s — the block ended 180 s later on the *other* recorder's pass.
  Fixed in `market_tape/record.py:1128-1174`, tested, pushed to `main`.**
  - Incident `host-ecbac293ecc90d5e`, scope `host`, host `ip-208-84-103-4`,
    new critical refs `capture-disk` and `capture-disk:forward-market-binance`
    — the same incident id, refs and alert text as the 22:54, 23:50, 00:01,
    00:36, 00:57 and 01:32 pages. Exact alert text: `CRITICAL recorder storage
    is blocked; frames are counted but not written` and `CRITICAL recorder
    forward-market-binance storage is blocked; frames are counted but not
    written`. Level-triggered on `disk_blocked is True`
    (`scripts/runtime/check_fleet_liveness.py:431`).
  - **The funded engine is not implicated and the host has not moved.** No
    engine, worker or timer is named; both units are `market_tape` recorders,
    research tape outside the order path. Pids are unchanged across all six
    pages — 2259813 (Bybit), 2263691 (Binance) — so neither recorder has
    restarted and the host still runs `65ee75a7`. The 25 GiB floor is the
    reservation held for mainnet's WAL and it held.
  - **The host is still on the un-fixed code, and the payload proves it.**
    Retention passes in the window are on the bare 300-second clock, nothing
    woken by a crossing: Bybit at 01:40:59.718, 01:46:05.918 and 01:51:11.497
    (306.200 s and 305.579 s apart), Binance at 01:39:02.053, 01:44:04.750 and
    01:49:07.376 (302.697 s and 302.626 s). `1d8fad9a`, `d275885a` and
    `fd604613` are all still merged and undeployed.
  - **The new defect, and where it is.** `_retention_loop` made one pass and
    then waited on `prune_now` for `RETENTION_INTERVAL_SECONDS`
    (`record.py:1128-1132` before this change). While the writer is blocked
    nothing sets that event again: `_maintenance` arms it on the crossing
    only — `if blocked and not self.disk_blocked` (`record.py:1189`) — and
    `_write_loop` never reaches an append to fail on, because it returns at
    the `if self.disk_blocked` gate before trying (`record.py:1088-1090`). So
    a pass that deletes and leaves the gate shut is the end of the matter for
    five minutes, and the one thread that can free room is asleep for all of
    it.
  - **Measured on this payload, on the Bybit unit, to the millisecond.**

    | Line (UTC) | `disk_blocked` | `disk_dropped` | `rows` |
    | :--- | :--- | ---: | ---: |
    | 01:40:35.664 | `True` | 7 826 376 | 51 600 612 |
    | 01:40:59.718 | *`retention removed 16 tape files`* | | |
    | 01:41:05.685 | `True` | 7 904 389 | 51 600 612 |
    | 01:41:35.706 | `True` | 7 986 818 | 51 600 612 |
    | 01:42:05.722 | `True` | 8 067 039 | 51 600 612 |
    | 01:42:35.739 | `True` | 8 151 042 | 51 600 612 |
    | 01:43:05.771 | `True` | 8 237 030 | 51 600 612 |
    | 01:43:35.791 | `True` | 8 320 865 | 51 600 612 |
    | 01:44:04.750 | *Binance's pass: `retention removed 10 tape files`* | | |
    | 01:44:05.809 | `False` | 8 404 264 | 51 600 642 |
    | 01:44:35.826 | `True` | 8 404 297 | 51 676 000 |

    Its own pass deleted 16 files at 01:40:59.718 and the gate was still shut
    5.97 s later. Over the next 180.1 s the unit discarded 499 875 frames
    (2 776/s) and wrote 30 rows. The gate then opened 1.06 s after *Binance's*
    01:44:04.750 pass — the shared filesystem — and in the very next interval
    the unit wrote 75 358 rows (2 512/s). At that rate the 180.1 s carried
    about 452 000 rows and instead carried 30. Bybit's own pruner did not walk
    again until 01:46:05.918, 306.2 s after the pass that fell short.
  - **What the payload cannot separate, and why the fix does not need it to.**
    Two things can leave a pass short of the gate. The deployed pruner stops
    deleting on the exact floor `writable()` unblocks on, so the neighbouring
    recorder can re-cross it in seconds — that is `d275885a`. And `prune`
    decides by free space counted from the sizes it unlinked while
    `writable()` reads the kernel's, so the two disagree while the filesystem
    is still releasing blocks (`storage.py:362-365` says as much). At 6-second
    resolution this payload cannot say which one ended the 01:40:59 pass
    short. It does not have to: either way the pass fell short, and the defect
    is that nothing then ran a second one. The fix removes the five-minute
    sleep, so the cause of a short pass costs a walk instead of a window.
  - **The fix.** `_retention_pass` now returns whether it is owed a successor
    — it deleted, and the writer is still blocked — and `_retention_loop`
    keeps passing while it is (`record.py:1128-1174`). The pruner owns the
    walk, so the pass that fell short is what runs the next one. The retry
    ends on the pass that deletes nothing, so a disk filled by something other
    than tape is walked once and not spun on, and the file set is finite, so
    the loop terminates. A pass that cannot delete (`OSError`) and a pass on
    an unblocked disk both return `False` and change nothing.
  - **The test.**
    `tests/market_tape/test_record.py::test_a_pass_that_deletes_and_leaves_the_gate_shut_passes_again_at_once`
    pins `RETENTION_INTERVAL_SECONDS` to 3600 s so a second pass can only come
    from the first, blocks the gate, and lets two passes delete while the
    kernel still refuses before the third reaches the floor. It asserts the
    gate opens, then asserts a pass that deletes nothing returns `False` so
    the retry is bounded. Without the fix it fails on the first assertion:
    `AssertionError: the pruner slept with the gate shut after 1 pass(es)`.
  - Loss, cumulative and never reset. Both windows are cut by the 40-line
    payload, so every figure is a lower bound.

    | Unit | First line in payload | Last line | Added since the 01:32 entry |
    | :--- | ---: | ---: | ---: |
    | Bybit `forward-capture` | 7 658 360 (01:39:05) | 9 513 202 (01:53:06) | 2 941 861 |
    | Binance `forward-capture-binance` | 2 503 562 (01:36:31) | 3 341 057 (01:53:02) | 1 094 418 |
    | **Pair** | | **12 854 259** | **4 036 279** |

    Over the 1 291 s since the 01:32 entry's last line that is 3 127 frames a
    second — back to the 00:36–00:57 window's 3 260/s. The 01:32 entry
    recorded the interval between crossings lengthening; it has closed again,
    and the pair has now discarded more tape in the 21 minutes since that
    entry than in the 34 minutes before it.
  - Checks run: `pytest tests/market_tape tests/scripts` (504 passed),
    `scripts/dev.sh check` (1459 passed), `ruff`, `mypy`, and `cargo test`
    (all green). Three failures are this container, not this change, which
    touches only `market_tape/record.py`: two in
    `tests/scripts/test_observability_hygiene.py` are the missing `rsync`
    binary — `backup_state.sh` exits 2 with `backup: rsync is not installed`
    before reaching either assertion — and
    `tests/repo/test_dev_tooling.py::test_repository_doctor_emits_machine_readable_state`
    reads `drift` where it wants `matched`, because this container had no
    `.venv` and the one built for these checks resolved off the lock.
    ShellCheck is not installed here; CI runs it.
  - **Deploy receipt: refused a twelfth time.** Run `33938359607`, `deploy` on
    `main@06e17d4a`, dispatched 02:11:19 UTC and failed at 02:11:25. `ci`,
    `rust` and `Deploy artifact` were all dead 3 s in at 02:11:24; `diagnose`
    and `disarm` skipped at 02:11:21, and the release-test job and `vps`
    skipped at 02:11:24-25 — so nothing reached the host. All three failed
    jobs' log downloads return HTTP 404 — `failed to download logs: HTTP 404`
    — the same no-job-ever-started signature as `33937280978`, `33934970737`,
    `33934851698`, `33933927629`, `33933636343`, `33932188757`, `33931474693`,
    `33928248402`, `33922197522`, `33921858031` and `33911912004`. It is the
    account's failed payments, not any commit: this run's `rust` did not even
    reach the 39 s in `queued` that `33937280978` managed. Deployed commit
    stays `65ee75a7`; all four recorder fixes are merged and undeployed, and
    the recorders keep crossing the floor until the owner runs the SSH path
    below.
  - Host action, and only the owner can run it. `06e17d4a` is the tip and
    carries all four recorder fixes; `capture_fingerprint`
    (`scripts/deploy_vps_live.sh:524-534`) hashes every `market_tape/*.py`, so
    `start_independent_units` restarts both recorders on the new code and no
    hand restart is needed. It also hands over both realms — the engine
    fingerprint hashes the whole `engine` tree — so the funded engine
    restarts:
    ```bash
    EXPECTED_COMMIT=06e17d4a82f9a5a19e00f1cd0928b4a0da96e315 scripts/ops.sh deploy
    scripts/ops.sh status
    ```
    Then the reading still open since 22:54 — whether tape or non-tape files
    hold the room, which decides whether the caps in `deploy/capture/*.toml`
    also want revisiting once the recorders stop blocking — and the equity and
    heartbeat record through the incident:
    ```bash
    df -h /var/lib
    du -sh /var/lib/liquidity-migration/forward-market \
           /var/lib/liquidity-migration/forward-market-binance
    scripts/ops.sh curve mainnet 120
    ```

- **2026-09-05 01:32 UTC — The fifth page from the same free-space floor, and
  the first one that measures a third defect the two merged fixes do not
  reach: the pruner frees room but cannot open the writer's gate, so the
  recorder keeps discarding frames onto a disk that already has space until
  the next status tick. Bybit's 01:30:50 pass freed room; the writer stayed
  shut for 15.06 s and wrote 54 rows where it should have written ~48 000.
  Fixed in `market_tape/record.py:1146-1152`, tested, pushed to `main`.**
  - Incident `host-ecbac293ecc90d5e`, scope `host`, host `ip-208-84-103-4`,
    new critical refs `capture-disk` and `capture-disk:forward-market-binance`.
    Exact alert text: `CRITICAL recorder storage is blocked; frames are
    counted but not written` and `CRITICAL recorder forward-market-binance
    storage is blocked; frames are counted but not written`, with
    `WARNING recorder dropped 183163 frames since the last check (storage was
    blocked)` and `WARNING recorder forward-market-binance dropped 65227
    frames since the last check (storage was blocked)`. Level-triggered on
    `disk_blocked is True` (`scripts/runtime/check_fleet_liveness.py:431`).
  - **The funded engine is not implicated and the host has not moved.** No
    engine, worker or timer is named; both units are `market_tape` recorders,
    research tape outside the order path. Pids are unchanged from the 22:54,
    23:50, 00:01, 00:36 and 00:57 pages — 2259813 (Bybit), 2263691 (Binance) —
    so neither recorder has restarted and the host still runs `65ee75a7`. The
    25 GiB floor is the reservation held for mainnet's WAL and it held.
  - **The new defect, and where it is.** `disk_blocked` is the gate every
    frame passes: `_write_loop` counts and discards while it is `True`
    (`market_tape/record.py:1088-1090`). Only `_maintenance` ever cleared it
    (`record.py:1169`), and `_maintenance` runs on
    `status_interval_seconds` — 30 s on both recorders
    (`deploy/capture/bybit-linear.toml:29`,
    `deploy/capture/binance-usdm.toml:33`). The pruner is the only thing that
    frees room, and it could not say so. Every crossing therefore cost a full
    status interval of tape after the room was already back, and the 30-second
    period of the oscillation recorded since 00:01 is that interval, not the
    disk.
  - **Measured on this payload, on the Bybit unit, to the millisecond.**

    | Line (UTC) | `disk_blocked` | `disk_dropped` | `rows` |
    | :--- | :--- | ---: | ---: |
    | 01:30:05.178 | `True` | 6 388 188 | 51 236 846 |
    | 01:30:35.198 | `True` | 6 484 881 | 51 236 846 |
    | 01:30:50.164 | *`retention removed 2 tape files`* | | |
    | 01:31:05.221 | `False` | 6 571 285 | 51 236 900 |
    | 01:31:35.248 | `True` | 6 571 341 | 51 332 557 |

    The pass ended at 01:30:50.164 and the gate opened at the 01:31:05.221
    tick, 15.057 s later. Over that 30 s interval the unit discarded 86 404
    frames (2 878/s) and wrote 54 rows; the interval after it, unblocked, it
    wrote 95 657 rows (3 186/s). So the writer was shut, not starved: at the
    rate it managed once the gate opened, the 15.057 s carried about 48 000
    rows and instead carried 54, and about 43 300 frames were discarded onto a
    disk that had room. Binance shows the same shape one tick later — blocked
    01:30:01 and 01:30:31, `False` at 01:31:01 with `rows` up by 2, blocked
    again at 01:31:31 — which is the shared filesystem: Bybit's pass freed the
    space both units then waited a tick to use.
  - **This is not what `1d8fad9a` and `d275885a` fix, and it survives them.**
    `1d8fad9a` wakes the pruner on the crossing instead of the 300-second
    clock, so the room comes back in milliseconds rather than up to five
    minutes; `d275885a` frees past the floor by `FREE_HEADROOM_FRACTION` so a
    pass hands the writer 1.25 GiB instead of nothing. Neither touches the
    gate. On `main` before this entry a crossing would free room at once and
    then still discard every frame for up to `status_interval_seconds`. The
    stale gate is also what `status.json` publishes (`record.py:1258`), so it
    held the CRITICAL up for the extra tick as well.
  - **The fix.** `_retention_pass` clears `disk_blocked` when a pass that
    deleted something leaves `writable()` true (`record.py:1146-1152`). The
    pruner is what frees the room, so it is what says the room is back;
    recovery is now the pruner's walk, not the status interval. A pass that
    deletes nothing, a pass that cannot delete (`OSError`, already returning
    early), and a pass that deletes but stays under the floor all leave the
    gate shut, so a genuinely full or read-only filesystem still blocks.
  - **The test.**
    `tests/market_tape/test_record.py::test_a_pass_that_frees_room_opens_the_writer_gate_instead_of_the_next_status_tick`
    blocks the gate, runs a pass that deletes while still under the floor and
    asserts the gate stays shut, then runs a pass that deletes with room back
    and asserts the gate opens and the next frame is written rather than
    counted. Without the fix it fails on `assert recorder.disk_blocked is
    False` → `assert True is False`; with it, it passes.
  - Loss, cumulative and never reset. Both windows are cut by the 40-line
    payload, so every figure is a lower bound.

    | Unit | First line in payload | Last line | Added since the 00:57 entry |
    | :--- | ---: | ---: | ---: |
    | Bybit `forward-capture` | 6 388 138 (01:17:34) | 6 571 341 (01:31:35) | 511 898 |
    | Binance `forward-capture-binance` | 2 181 387 (01:15:30) | 2 246 639 (01:31:31) | 206 649 |
    | **Pair** | | **8 817 980** | **718 547** |

    The rate is down an order of magnitude from the 00:36–00:57 window's
    3 260 frames a second, because this window holds one crossing rather than
    a continuous oscillation: Binance was clean for 14 minutes before 01:30:01
    and Bybit for 12.5 minutes before 01:30:05. The floor is still crossed;
    the interval between crossings has lengthened.
  - Checks run: `pytest tests/market_tape tests/scripts` (503 passed),
    `scripts/dev.sh check` (1459 passed), `ruff`, `mypy market_tape`, and
    `cargo test` (all green). Two failures in
    `tests/scripts/test_observability_hygiene.py` are this container's missing
    `rsync` binary — `backup_state.sh` exits 2 with `backup: rsync is not
    installed` before reaching either assertion — not this change, which
    touches no shell script. ShellCheck is not installed here; CI runs it.
  - **Deploy receipt: refused an eleventh time.** Run `33937280978`, `deploy`
    on `main@1f627520`, dispatched 01:49:01 UTC and failed at 01:49:43.
    `ci` and `Deploy artifact` were dead 2 s in at 01:49:05, `diagnose` and
    `disarm` skipped at 01:49:03, `rust` sat in `queued` for 39 s with no
    runner assigned before failing at 01:49:42, and `vps` and the
    release-test job were skipped the same second — so nothing reached the
    host. Both failed jobs' log downloads return HTTP 404 — `failed to
    download logs: HTTP 404` — the same no-job-ever-started signature as
    `33934970737`,
    `33934851698`, `33933927629`, `33933636343`, `33932188757`, `33931474693`,
    `33928248402`, `33922197522`, `33921858031` and `33911912004`. It is the
    account's failed payments, not any commit. Deployed commit stays
    `65ee75a7`; all three recorder fixes are merged and undeployed, and the
    recorders keep crossing the floor until the owner runs the SSH path below.
  - Host action, and only the owner can run it. The tip carries all three
    recorder fixes; `capture_fingerprint` (`scripts/deploy_vps_live.sh:524-534`)
    hashes every `market_tape/*.py`, so `start_independent_units` restarts both
    recorders on the new code and no hand restart is needed:
    ```bash
    EXPECTED_COMMIT=fd604613cc222472670b65b74dc9abf5664e4be6 scripts/ops.sh deploy
    scripts/ops.sh status
    ```
    Then the reading still open since 22:54 — whether tape or non-tape files
    hold the room, which decides whether the caps in `deploy/capture/*.toml`
    also want revisiting once the recorders stop blocking:
    ```bash
    df -h /var/lib
    du -sh /var/lib/liquidity-migration/forward-market \
           /var/lib/liquidity-migration/forward-market-binance
    scripts/ops.sh curve mainnet 120
    ```

- **2026-09-05 00:57 UTC — The fourth page from the same free-space floor, and
  the first payload that catches the pruner in the act: six retention passes
  inside the window, all on the 300-second clock, and every one of them buying
  the writer one 30-second status tick or nothing at all. That is the deployed
  defect doing exactly what `1d8fad9a` and `d275885a` were written to stop.
  No new defect, no code changed, and the deploy was refused a ninth time.
  Tape discarded since 22:54 is now 8 099 433 frames, more than double the
  00:36 figure.**
  - Incident `host-681737fd16e1f806`, scope `host`, host `ip-208-84-103-4`,
    new critical ref `capture-disk`. Exact alert text: `CRITICAL recorder
    storage is blocked; frames are counted but not written`. The alert is
    level-triggered on `disk_blocked is True`
    (`scripts/runtime/check_fleet_liveness.py:431-434`), so the same id
    repeats on every crossing that clears the cooldown.
  - **The funded engine is not implicated and the host has not moved.** No
    engine, worker or timer is named; the two units in the payload are
    `market_tape` recorders, research tape outside the order path. Both pids
    are unchanged from the 22:54, 23:50, 00:01 and 00:36 pages — 2259813
    (Bybit), 2263691 (Binance) — so neither recorder has restarted and the
    host still runs `65ee75a7`. The 25 GiB floor is the reservation held for
    mainnet's WAL and it held. What is lost is tape.
  - **What this payload adds: the retention passes are visible, and they are
    on the clock.** The 00:36 excerpt contained no `retention removed N tape
    files` line at all, which is what left that entry's clean window
    unexplained. This one contains six, and their spacing is the deployed
    `self.stop.wait(RETENTION_INTERVAL_SECONDS)` (`market_tape/record.py:1121`
    at `65ee75a7`) to the tenth of a second — 302.4 s and 302.5 s apart on
    Binance, 304.8 s and 304.7 s on Bybit, the interval plus each pass's own
    walk. Nothing woke a pass on a crossing, because at `65ee75a7`
    `_maintenance` only assigns the flag (`record.py:1141`).

    | Pass (UTC) | Unit | Files | Next status tick | Room it bought |
    | :--- | :--- | ---: | :--- | :--- |
    | 00:43:39.104 | Binance | 8 | 00:43:59 `False` | one tick: 28 165 rows, then blocked at 00:44:29 |
    | 00:45:08.509 | Bybit | 133 | 00:45:30 `False` | one tick: 61 rows, then blocked at 00:46:00 |
    | 00:48:41.520 | Binance | 12 | 00:48:59 `True` | none |
    | 00:50:13.339 | Bybit | 19 | 00:50:30 `True` | none at the next tick; one tick at 00:51:00 |
    | 00:53:44.020 | Binance | 91 | 00:54:00 `True` | none |
    | 00:55:18.035 | Bybit | 4 | 00:55:31 `True` | none |

  - **The second half of the diagnosis, now measured rather than inferred.**
    At `65ee75a7`, `Retention.prune` re-evaluates `pressured = total >
    self.max_bytes or free < self.min_free_bytes`
    (`market_tape/storage.py:362`) and `Retention.writable()` returns `free >=
    self.min_free_bytes` (`storage.py:390`) — the same number — so a pass
    driven by free space stops on precisely the point the writer unblocks on.
    The 00:53:44 pass unlinked 91 files and the disk was blocked again 16 s
    later at the next tick; the 00:55:18 pass unlinked 4 and never unblocked
    at all. The room a pass returns is now smaller than one status interval
    of writing, so five of six passes bought nothing. Two recorders share the
    filesystem and `prune` tracks free space by the sizes it unlinks rather
    than re-reading `statvfs` (`storage.py:367` here, `storage.py:400` on
    `main`), so a pass's own accounting
    never sees the other recorder writing into the room it just freed. At a
    zero-headroom target that is fatal; against `main`'s target it is bounded
    by two orders of magnitude and needs no separate change.
  - Loss, cumulative and never reset. Both windows are cut by the 40-line
    payload, so every figure is a lower bound.

    | Unit | `disk_dropped` first line | last line (00:57:3x) | Added since the 00:36 entry's cut |
    | :--- | ---: | ---: | ---: |
    | Bybit `forward-capture` | 4 030 591 (00:43:30) | 6 059 443 | 3 164 972 |
    | Binance `forward-capture-binance` | 1 198 002 (00:39:59) | 2 039 990 | 1 035 413 |
    | **Pair** | | **8 099 433** | **4 200 385** |

    That is 4 200 385 frames discarded in the 21.5 minutes from 00:36:00, about
    3 260 frames a second across the pair. Binance wrote nothing at all from
    00:51:29 to 00:57:31 — `rows` pinned at 15 435 742 for six minutes, the
    longest stall of the night — and Bybit nothing from 00:55:01, `rows`
    pinned at 45 616 288.
  - **The two merged fixes remain together sufficient, and this payload
    tightens the margin rather than loosening it.** `1d8fad9a` wakes the
    pruner on the crossing (`record.py:1131`, `record.py:1159-1160`,
    `record.py:1114`) instead of the clock, so the 300 s of tape each crossing
    costs above goes away. `d275885a` frees to `min_free_bytes +
    FREE_HEADROOM_FRACTION * min_free_bytes` (`storage.py:47`, `374`) while
    `writable()` still unblocks on the floor (`storage.py:422`), which on this
    host is 1.25 GiB of headroom. The pair's inbound wire rate at the
    payload's last line is `projected_gb` 1370.9 + 443.0 = 1813.9 GB/month,
    about 700 KB/s, so that headroom is roughly 30 minutes — six retention
    intervals — of writing before the floor is reachable again, and inbound
    wire bytes are what compression reduces. Nothing in this payload is a
    defect the repository does not already fix.
  - No code changed in this entry, so no test changed and nothing needed
    running.
  - **Deploy receipt: refused a ninth time, same signature.** Run
    `33934851698`, `deploy` on `main@bb5d5ec4`, dispatched 01:01:12 UTC and
    failed at 01:01:18: `ci`, `rust` and `Deploy artifact` each dead in 3 s,
    and `disarm`, `diagnose`, `vps` and the release-test job all skipped;
    job `101220659870`'s log download returns HTTP 404, so no job started.
    Identical to `33933927629`, `33933636343`, `33932188757`, `33931474693`,
    `33928248402`, `33922197522`, `33921858031` and `33911912004`; it is the
    account's failed payments, not any commit. This entry's own commit
    `b7bdbe17` was then dispatched at 01:03:22 UTC as run `33934970737` and
    refused identically 6 s later — the tenth in a row since 19:35 UTC on
    2026-09-04. Deployed commit stays `65ee75a7` and the recorders keep
    crossing the floor until the owner runs the SSH path.
  - Host actions, in order, and only the owner can run them. `bb5d5ec4` and
    every commit after it carry both recorder fixes; `capture_fingerprint`
    (`scripts/deploy_vps_live.sh:524-534`) hashes every `market_tape/*.py`, so
    `start_independent_units` restarts both recorders on the new code and no
    hand restart is needed:
    ```bash
    EXPECTED_COMMIT=bb5d5ec4845a3bfe1536a8c8713d75a6f0a0e08b scripts/ops.sh deploy
    scripts/ops.sh status
    ```
    Then the reading still open since 22:54 — whether tape or non-tape files
    hold the room, which decides whether the caps in `deploy/capture/*.toml`
    also want revisiting once the recorders stop blocking:
    ```bash
    df -h /var/lib
    du -sh /var/lib/liquidity-migration/forward-market \
           /var/lib/liquidity-migration/forward-market-binance
    scripts/ops.sh curve mainnet
    ```
    `curve mainnet` is what shows the funded account through the incident and
    which minutes had no heartbeat at all.

- **2026-09-05 00:36 UTC — The third page of the night from the same free-space
  floor, and the first that contains a long clean stretch: both recorders wrote
  for 12 and 16 minutes with no disk drops at all, then crossed together at
  00:35:59 and 00:36:00. No code changed and nothing new is broken — what ends
  this is `1d8fad9a` and `d275885a` on `main`, and the deploy was refused a
  seventh and an eighth time. Tape discarded since 22:54 is now 3 899 048
  frames.**
  - Incident `host-ecbac293ecc90d5e`, scope `host`, host `ip-208-84-103-4`,
    new critical refs `capture-disk` and `capture-disk:forward-market-binance`.
    Exact alert text: `CRITICAL recorder storage is blocked; frames are counted
    but not written`, `CRITICAL recorder forward-market-binance storage is
    blocked; frames are counted but not written`, `WARNING recorder dropped 24
    frames since the last check (storage was blocked)`. The `capture-disk`
    CRITICAL is level-triggered on `disk_blocked is True`
    (`scripts/runtime/check_fleet_liveness.py:431-434`), so the id repeats
    every crossing that clears the cooldown.
  - **The funded engine is not implicated and the host has not moved.** No
    engine, worker or timer is named; both refs are `market_tape` recorders,
    research tape outside the order path. Both pids are unchanged from the
    23:50 and 00:01 pages — 2259813 (Bybit), 2263691 (Binance) — so neither
    recorder has restarted and the host still runs `65ee75a7`. The 25 GiB
    floor is the reservation held for mainnet's WAL
    (`deploy/engine.mainnet.toml.template:18`) and it held. What is lost is
    tape.
  - Timeline. Binance (`liquidity-migration-forward-capture-binance.service`)
    holds `disk_dropped=1004572 disk_blocked=False` from 00:19:28.880 to
    00:35:29.435 — 16 minutes, `rows` 14 374 239 → 15 270 938, not one frame
    discarded — then reads `disk_dropped=1004577 disk_blocked=True` at
    00:35:59.457. Bybit (`liquidity-migration-forward-capture.service`) does
    the same: `disk_dropped=2894410` pinned 00:23:29.578 → 00:35:30.172,
    `rows` 42 841 171 → 44 968 456, then `disk_dropped=2894471
    disk_blocked=True` at 00:36:00.204. Both windows are cut by the 40-line
    payload, so they are lower bounds. The two crossings are 0.75 s apart:
    one shared filesystem, as at 22:54 and 23:50. Five and 61 frames lost at
    the crossing tick, and the watchdog's 24 — the page fired at the start of
    the block, not inside it, so what this payload shows is the beginning of
    the loss, not its size.
  - Cumulative, and the counters never reset: 2 894 471 (Bybit) + 1 004 577
    (Binance) = 3 899 048 frames since 22:54. That is 480 931 more than the
    00:01 entry's cut, of which every frame fell in crossings that went
    unpaged or inside the cooldown.
  - **What is new is the clean window, and the deployed pruner cannot explain
    it on its own.** Neither journal carries a `retention removed N tape
    files` line anywhere in the payload, so no pass deleted anything — for
    room or for age — in 12 to 16 minutes, which is two to three passes each
    at `RETENTION_INTERVAL_SECONDS` = 300 s. Free space was therefore at or
    above 25 GiB and both totals under their caps that whole time, and the
    room came from before the excerpts. Two readings fit and the payload
    cannot separate them: the pass that unblocked the recorders before
    00:19 overshot the floor by the size of its last unlinked segment, or
    something else on `/var/lib` released and then reclaimed the room.
    Bounding it: `projected_gb` 1444.6 + 468.5 = 1913.1 GB/month is *inbound
    wire* bytes (`market_tape/record.py:508-515`), 738 KB/s for the pair, so
    16 minutes consumed at most 708 MB of disk and, at any real zstd ratio,
    nearer 100 MB — a single large hourly segment is in range. The host
    settles it; this run cannot.
  - Diagnosis, unchanged and still the *deployed* commit rather than a new
    defect. At `65ee75a7`, `Retention.prune` re-evaluates `pressured = total >
    self.max_bytes or free < self.min_free_bytes`
    (`market_tape/storage.py:362`) and `Retention.writable()` returns `free >=
    self.min_free_bytes` (`storage.py:390`), the same number, so a pass driven
    by free space hands the writer no headroom; `_retention_loop` waits
    `RETENTION_INTERVAL_SECONDS` on `stop` (`market_tape/record.py:1121`),
    which nothing can wake, and `_maintenance` only reads the flag
    (`record.py:1141`). A crossing therefore costs up to 300 s of tape and the
    pass that ends it buys one status tick.
  - **The two merged fixes are together sufficient, by arithmetic.**
    `1d8fad9a` wakes the pruner on the crossing instead of the clock, and
    `d275885a` frees to `min_free_bytes + FREE_HEADROOM_FRACTION *
    min_free_bytes` (`storage.py:47`, `374`, `394`) while `writable()` still
    unblocks on the floor (`storage.py:422`). On this host that headroom is
    1.25 GiB against a worst case of 221 MB written per 300 s interval —
    inbound wire bytes, which compression can only reduce — so the periodic
    pass alone trims ahead of the floor with about 6× margin and the writer
    stops reaching it; `prune_now` covers a burst that outruns the interval.
    This also corrects what the 00:01 entry left standing: after these deploy,
    the tape self-trims to the disk and `max_disk_gb` stops being the number
    that decides whether this recurs. The 60 + 18 GB caps remain the owner's
    call, but they now govern only how much history is *guaranteed*, not
    whether the recorders block.
  - No code changed in this entry. Nothing in the payload is a defect the
    repository does not already fix.
  - **Deploy receipt: refused twice more, seventh and eighth in a row, same
    signature both times.** Run `33933636343`, `deploy` on `main@c821585d`,
    dispatched 00:38:23 UTC and failed at 00:38:28: `ci` and `Deploy artifact`
    dead in 3 s, `rust` in 4 s, and `disarm`, the release-test job, `vps` and
    `diagnose` all skipped; job `101217183835`'s log download returns HTTP 404,
    so no job started. Run `33933927629` on `main@fdc6bf7c` — this entry's own
    commit — dispatched 00:43:47 and failed at 00:43:53 with `ci`, `rust` and
    `Deploy artifact` each dead in 3 s and the same four jobs skipped.
    Identical to `33932188757`, `33931474693`, `33928248402`, `33922197522`,
    `33921858031` and `33911912004`; it is the account's failed payments, not
    any commit. Deployed commit stays `65ee75a7` and the recorders keep
    crossing the floor until the owner runs the SSH path.
  - Host actions, in order, and only the owner can run them. Installing
    `fdc6bf7c` carries both recorder fixes; `capture_fingerprint`
    (`scripts/deploy_vps_live.sh:524-534`) hashes every `market_tape/*.py`, so
    `start_independent_units` restarts both recorders on the new code and no
    hand restart is needed:
    ```bash
    EXPECTED_COMMIT=fdc6bf7c1f1855a91fdb99aa8f6607755f127fe0 scripts/ops.sh deploy
    scripts/ops.sh status
    ```
    Then the reading that separates the two explanations above, and the one
    still open from 22:54 — whether the tape or non-tape files hold the room:
    ```bash
    df -h /var/lib
    du -sh /var/lib/liquidity-migration/forward-market \
           /var/lib/liquidity-migration/forward-market-binance
    scripts/ops.sh curve mainnet
    ```
    `curve mainnet` is what shows the funded account through the incident and
    which minutes had no heartbeat at all
    ([docs/observability.md](docs/observability.md)).

- **2026-09-05 00:01 UTC — The crossing stopped being an episode and became a
  30-second oscillation, and this time there is a second defect under it: the
  pruner stops deleting at exactly the free-space floor the writer unblocks
  on, so a pass hands the recorder no room and it re-blocks within one status
  tick. Fixed in `market_tape/storage.py`; 1 956 903 more frames of tape were
  discarded in the eleven minutes the payload covers.**
  - Incident `host-681737fd16e1f806`, scope `host`, host `ip-208-84-103-4`,
    new critical ref `capture-disk`. Exact alert text: `CRITICAL recorder
    storage is blocked; frames are counted but not written`. No engine,
    worker or timer is named. Both refs are `market_tape` recorders, which
    are research tape outside the order path; the 25 GiB floor is the
    reservation held for mainnet's WAL and it held. What is lost is tape.
  - **What is new, and it is not the retention interval.** Earlier crossings
    were episodes: blocked for minutes, then 14 minutes of clean ticks. In
    this payload each recorder recovers for exactly one 30 s status interval
    and blocks again. Bybit
    (`liquidity-migration-forward-capture.service`, pid 2259813) reads
    `disk_blocked=False` at 23:58:25 with `rows=38989246`, writes 73 904 rows,
    and is blocked again at the 23:58:55 tick. Binance (`…-binance.service`,
    pid 2263691) does the same one tick later: `disk_blocked=False` 23:59:27,
    23 154 rows, blocked at 23:59:57. Every other tick in the window is
    blocked. Counters over 23:50:55 → 00:01:28: Bybit `disk_dropped`
    1 087 045 → 2 534 841 (1 447 796 frames), Binance 374 169 → 883 276
    (509 107) — 1 956 903 frames for two 30 s windows of writing.
  - Diagnosis, and it is a defect in this repository. `Retention.prune`
    re-evaluated `pressured = total > self.max_bytes or free <
    self.min_free_bytes` per file (`market_tape/storage.py:380` at
    `65ee75a7`), and `Retention.writable()` — the O(1) check `_maintenance`
    reads every tick to set `disk_blocked` (`market_tape/record.py:1152`,
    `1161`) — returns `free >= self.min_free_bytes` (`storage.py:408`). The
    stop condition and the unblock condition are the same number, so a pass
    driven by free space returns the filesystem to the floor and not one byte
    further. The writer is then unblocked onto zero headroom: the segments it
    rolls in the next interval cross the floor again, and everything after
    that is counted and dropped until the next pass. That is the oscillation
    above, and it is why prune passes that are plainly working — `retention
    removed 3 tape files` 23:53:19, `16` 23:54:25, `9` 23:58:22, `41` 23:59:29
    — buy 30 seconds each.
  - The pruner is still on the 300 s loop, so the host still runs `65ee75a7`:
    those pass timestamps are 303 s and 304 s apart. `1d8fad9a` (prune on the
    crossing rather than at the next interval) remains merged and undeployed.
    On its own it would have made the chatter faster, not shorter — a prune
    that frees to the floor is a prune the next interval undoes whenever it
    runs. The two fixes are complementary and both are needed.
  - Changed: `market_tape/storage.py`. A pass that deletes for room now frees
    to `min_free_bytes + FREE_HEADROOM_FRACTION * min_free_bytes`
    (`storage.py:47`, `374`, `394`); `writable()` still blocks and unblocks on
    the floor itself (`storage.py:422`). The gap between the two thresholds is
    what makes a crossing resolve instead of repeat. On this host that is
    1.28 GiB of runway above a 25 GiB floor. Deleting for `max_bytes` or for
    age is untouched, and the pass holds *less* tape than before, never more:
    no cap moves, no disk is claimed, so this is not the size decision the
    2026-09-04 23:50 entry left with the owner. That one still stands —
    `max_disk_gb` 60 + 18 plus the floor is 105 GB of a 118 GB disk, and the
    tape will keep growing back into the floor until the caps change.
  - Test: `tests/market_tape/test_tape_storage.py::test_disk_pressure_leaves_
    the_writer_room_above_the_floor` models free space as what the tape does
    not hold, prunes from under the floor, then rolls one more segment and
    asserts the recorder is still writable. Without the fix it fails on
    exactly the incident's assertion — `assert retention.writable() is True`
    → `assert False is True` — because the pass stopped on the floor. With
    it, 195 `tests/market_tape` tests pass.
  - Local gate: `ruff check market_tape scripts liquidity_migration tests`
    clean, `mypy` clean over 92 files, `pytest -q` 1452 passed. Seven failures
    are this sandbox, identical on a stashed tree: `rsync` and the two
    `backup_state.sh` tests, the `doctor` tooling test, two `marketdata`
    paging tests, two research-chart tests. `scripts/dev.sh check` reaches
    the same point and stops at those; the `ruff format --check` diffs are
    pre-existing lines under this box's ruff 0.16.6 against the pinned build,
    none of them lines this change adds. The engine is Rust and untouched.
  - **Deploy receipt: refused again, same signature, sixth in a row.** Run
    `33932188757`, dispatched `deploy` on `main@d275885a` at 00:12:15 UTC,
    failed 5 s later at 00:12:20: `ci`, `rust` and `Deploy artifact` each died
    in 3 s with their log downloads returning HTTP 404, and `vps`, `diagnose`,
    `disarm` and the release-test job were all skipped. No job ever started.
    Identical to `33931474693`, `33928248402`, `33922197522`, `33921858031`
    and `33911912004`; it is the account's failed payments, not this commit.
    Deployed commit stays `65ee75a7`, so the recorders keep oscillating across
    the floor until the owner runs the SSH path below.
  - Host actions, in order, and only the owner can run them. Installing this
    commit carries `1d8fad9a` with it; `capture_fingerprint`
    (`scripts/deploy_vps_live.sh:523-535`) hashes every `market_tape/*.py`, so
    `start_independent_units` restarts both recorders on the new code and no
    hand restart is needed:
    ```bash
    EXPECTED_COMMIT=d275885a638e702ebea75bb14f19f1fee5810f89 scripts/ops.sh deploy
    scripts/ops.sh status
    ```
    Then the reading that is still open from 22:54 — whether the tape or
    non-tape files hold the room:
    ```bash
    df -h /var/lib
    du -sh /var/lib/liquidity-migration/forward-market \
           /var/lib/liquidity-migration/forward-market-binance
    scripts/ops.sh curve mainnet
    ```
    `curve mainnet` is what shows the funded account through the incident and
    which minutes had no heartbeat at all
    ([docs/observability.md](docs/observability.md)).

- **2026-09-04 23:50 UTC — The same floor crossing, at least the third in an
  hour. `1d8fad9a` fixes it; the host does not run `1d8fad9a`; the two
  recorders have now discarded 1 752 989 frames of tape since 22:54. Nothing
  new is broken and no code changed — the fix is sitting behind a GitHub
  account that will not start jobs.**
  - Incident `host-ecbac293ecc90d5e` again, scope `host`, host
    `ip-208-84-103-4`, new critical refs `capture-disk` and
    `capture-disk:forward-market-binance`. The id repeats because it is the
    scope and its refs, and the `capture-disk` CRITICAL is level-triggered on
    `disk_blocked is True`
    (`scripts/runtime/check_fleet_liveness.py:431-434`), so every crossing
    that clears the cooldown pages under it. Exact alert text: `CRITICAL
    recorder storage is blocked; frames are counted but not written`,
    `CRITICAL recorder forward-market-binance storage is blocked; frames are
    counted but not written`, `WARNING recorder dropped 215524 frames since
    the last check (storage was blocked)`, `WARNING recorder
    forward-market-binance dropped 76195 frames since the last check (storage
    was blocked)`.
  - **The funded engine is not implicated, and the floor is doing the job it
    exists for.** No engine, worker or timer is named in the page; both refs
    are `market_tape` recorders, which are research tape outside the order
    path. Free space on `/var/lib` is being held at 25 GiB and mainnet's WAL
    (`/var/lib/liquidity-migration-engine-mainnet/engine.wal`,
    `deploy/engine.mainnet.toml.template:18`) lives on that filesystem: the
    recorders stop so the engine does not. What is lost is tape.
  - Timeline, from the two journals. Bybit
    (`liquidity-migration-forward-capture.service`, pid 2259813) ticks clean
    from 23:36:24 to 23:50:25 with `disk_dropped` pinned at 1 087 017 and
    `disk_blocked=False`; at 23:50:55 it reads `disk_dropped=1087045
    disk_blocked=True` and `rows` freezes at 38 989 216 for the rest of the
    payload, reaching `disk_dropped=1302622` at 23:52:25 — 215 605 frames in
    90 s, and still blocked when the payload was cut. Binance
    (`…-binance.service`, pid 2263691) crosses 2 s later: last clean tick
    23:50:27 at `disk_dropped=374168`, first blocked 23:50:57, `rows` frozen
    at 13 454 846, `disk_dropped=450367` by 23:52:27 — 76 199 frames in 90 s.
    Two roots, one filesystem, 2 s apart: one shared crossing, as at 22:54.
  - At least one crossing between the two pages went unpaged in the payloads
    on hand. The counters are cumulative and never reset, so the arithmetic is
    direct: Bybit ran 313 938 → 1 087 017 between 22:57:21 and 23:36:24
    (773 079 frames), Binance 95 873 → 374 168 between 22:56:55 and 23:33:56
    (278 295 frames). Cumulative since 22:54, and still climbing at the cut:
    1 302 622 Bybit + 450 367 Binance = 1 752 989 frames of tape.
  - Diagnosis: unchanged from 22:54, and it is the *deployed* commit, not a
    new defect. The host runs `65ee75a7`, where `_retention_loop`
    (`market_tape/record.py:1128`) waits on `stop` for
    `RETENTION_INTERVAL_SECONDS` = 300 s and `prune` is the only thing that
    frees room, so each crossing costs up to five minutes of tape. The
    observed blocked rate — 71 900 (Bybit) and 25 400 (Binance) frames per
    30 s — puts a full 300 s window at about 719 000 and 254 000 frames,
    which is the size of the unpaged episode above. `1d8fad9a` on `main`
    replaces that wait with the `prune_now` event and cuts a crossing to one
    prune pass. It is not on the host.
  - Deploying it does reach the recorders, despite
    [docs/operations.md](docs/operations.md) §3 listing them as never stopped
    by a fleet deploy. `capture_fingerprint`
    (`scripts/deploy_vps_live.sh:523-535`) hashes every `market_tape/*.py`
    and `1d8fad9a` edits `market_tape/record.py`, so `start_independent_units`
    (`scripts/deploy_vps_live.sh:607`) restarts both units on the new code. No
    separate hand restart is needed.
  - New, and it is a size decision rather than a bug: `min_free_disk_gb`, not
    `max_disk_gb`, is what governs tape size on this host. `Retention.prune`
    (`market_tape/storage.py:380`) re-evaluates `pressured = total >
    self.max_bytes or free < self.min_free_bytes` per file and stops deleting
    the moment `free >= min_free_bytes`, so a pressure pass returns free space
    to the floor and no further; with `segment_max_mb = 64`
    (`deploy/capture/bybit-linear.toml:18`,
    `deploy/capture/binance-usdm.toml:22`) the margin a pass buys is a handful
    of segments, and the observed writable window between crossings is 14
    minutes of clean ticks. That is why the crossing repeats inside the hour
    instead of once. Deleting more than the floor demands would be the fix,
    and it is a size decision: `max_disk_gb` 60 + 18 = 78 GB of tape plus the
    25 GiB floor is 105 GB of a 118 GB disk, which is the race the config
    comments already warn about at `deploy/capture/bybit-linear.toml:22-23`
    and `deploy/capture/binance-usdm.toml:26-27`. The owner's call, not this
    routine's.
  - Nothing changed in code. The cause is in the repository, it is already
    fixed, and writing a second fix for it would be noise. Local gate on the
    docs-only change: `ruff check market_tape scripts liquidity_migration
    tests` clean, `tests/scripts/test_scripts_check_fleet_liveness.py` 39
    passed. `scripts/dev.sh check` cannot complete in this sandbox — there is
    no `.venv` and `websocket-client`, `pytest`, `mypy`, `polars` and `numpy`
    are absent, so `tests/market_tape/test_record.py` does not import; the
    `ruff format --check` diff is this box carrying ruff 0.15.8 against the
    pinned 0.16.5.
  - **Deploy receipt: refused again, same signature.** Run `33931474693`,
    dispatched `deploy` on `main@9c53b12e` at 23:59:48 UTC, failed 7 s later:
    `ci`, `rust` and `Deploy artifact` each died in 3–4 s with log downloads
    returning HTTP 404, and `vps`, `diagnose`, `disarm` and the release-test
    job were all skipped. No job ever started. That is the fifth consecutive
    refusal since 19:17 UTC and it is account-wide, not this commit's:
    `33928248402`, `33922197522`, `33921858031`, `33911912004` are identical.
    Deployed commit stays `65ee75a7`, so the recorders keep dropping tape on
    every crossing until the owner runs the SSH path or GitHub restores
    hosted capacity.
  - Host actions, in order, and only the owner can run them. The deploy needs
    no Actions runner, and installing `9c53b12e` carries `1d8fad9a` with it:
    ```bash
    EXPECTED_COMMIT=9c53b12e763d9c8aafef6420dcb37e7c3fd2d0e7 scripts/ops.sh deploy
    scripts/ops.sh status
    ```
    Then the reading that settles whether the tape or non-tape files hold the
    room — still open from 22:54:
    ```bash
    df -h /var/lib
    du -sh /var/lib/liquidity-migration/forward-market \
           /var/lib/liquidity-migration/forward-market-binance
    scripts/ops.sh curve mainnet
    ```
    `curve mainnet` is what shows the funded account through the incident and
    which minutes had no heartbeat at all
    ([docs/observability.md](docs/observability.md)).

- **2026-09-04 22:54 UTC — Both tape recorders crossed the 25 GiB free-space
  floor on `/var/lib` and threw away every frame for the rest of the pruner's
  300-second sleep: 313 938 Bybit frames and 95 873 Binance frames of tape,
  gone because detection was instant and the only remedy was on an unwakeable
  timer.**
  - Incident `host-ecbac293ecc90d5e`, scope `host`, host `ip-208-84-103-4`,
    new critical refs `capture-disk` and `capture-disk:forward-market-binance`.
    Exact alert text: `CRITICAL recorder storage is blocked; frames are counted
    but not written`, `CRITICAL recorder forward-market-binance storage is
    blocked; frames are counted but not written`, `WARNING recorder dropped
    313907 frames since the last check (storage was blocked)`, `WARNING
    recorder forward-market-binance dropped 95869 frames since the last check
    (storage was blocked)`.
  - **The funded engine is not implicated.** No engine, worker or timer is
    named in the page, no engine unit appears in the payload, and mainnet
    neither paged nor restarted. Both refs are `market_tape` recorders, which
    are research tape and sit outside the order path.
  - Timeline, from the two journals. Bybit's last clean tick is 22:54:21
    (`disk_blocked=False`, `disk_dropped=0`); its first blocked tick is
    22:54:51 with `disk_dropped=54`, and by the payload's last line at 22:57:21
    it reads `disk_dropped=313938`. Binance's last clean tick is 22:54:25 and
    its first blocked tick 22:54:55 with `disk_dropped=2`, reaching
    `disk_dropped=95873` at 22:56:55. In both, `rows` freezes at the crossing
    and never moves again — Bybit at 31 927 482, Binance at 10 939 653 — while
    `frames` keeps climbing. The Bybit journal's four shard disconnects
    (22:54:26, 22:55:17–18) are the venue's own and are unrelated: three
    reconnected inside 4 s and one inside 2 s, and the block spans them.
  - Diagnosis. Neither journal carries `capture storage blocked; frames will be
    counted but not written` (`market_tape/record.py:1115`), so the writer's
    `OSError` path never fired. The flag was set by the maintenance tick at
    `market_tape/record.py:1157`, `not self.retention.writable()`, and
    `Retention.writable`
    (`market_tape/storage.py:408`) is `shutil.disk_usage(self.root).free >=
    self.min_free_bytes` against `min_free_disk_gb = 25` in both
    `deploy/capture/bybit-linear.toml:28` and
    `deploy/capture/binance-usdm.toml:32`. Two processes with separate roots
    (`/var/lib/liquidity-migration/forward-market` and
    `…/forward-market-binance`) flipping within 34 s of each other is one
    shared filesystem crossing that floor, not two write errors. Every frame
    from there on is dropped at `market_tape/record.py:1088-1090`, before it is
    ever normalised or written.
  - The recorders stopping is the reservation working, and that is the point of
    the floor: mainnet's WAL is
    `/var/lib/liquidity-migration-engine-mainnet/engine.wal`
    (`deploy/engine.mainnet.toml.template:18`), on the same filesystem, so the
    25 GiB is headroom held for the funded engine against the tape. The fault
    is what came next.
  - Root cause. `_retention_loop` (`market_tape/record.py:1128`) ran
    `self.stop.wait(RETENTION_INTERVAL_SECONDS)` — 300 s, wakeable only by a
    shutdown. `prune` is the only thing in the process that frees room, so the
    recorder detected "no room" in milliseconds and then did nothing about it
    for up to five minutes, discarding every frame that arrived meanwhile. The
    comment at `record.py:93-99` justified the 300 s on the tape's own growth
    rate ("the disk cannot run out inside one interval"), which is true of the
    tape and false of the floor: `min_free_disk_gb` is free space on the whole
    filesystem, which anything sharing it can cross.
  - Changed. `Recorder.prune_now` (`market_tape/record.py:638`) is a
    `threading.Event` the pruner waits on instead of `stop`, so a pass can be
    started before the routine interval. The maintenance tick sets it on the
    crossing into blocked (`record.py:1159`) and the writer's first failed
    append sets it too (`record.py:1114`), that being the earliest detector of
    a full disk — it fails on the next append, where the free-space tick is a
    whole `status_interval_seconds` behind. It is set on the crossing and not
    on every blocked tick: while blocked nothing is written, so a repeat pass
    has nothing new to delete and level-triggering would walk tens of thousands
    of files every 30 s during exactly the incident that can least afford it.
    `run`'s shutdown sets it alongside `stop` so a stop still does not wait out
    an interval. The blocked window is now one prune pass plus one status tick,
    not up to 300 s. No floor, cap, cadence, budget or shed order changed.
  - Tests. `test_a_disk_under_the_free_floor_prunes_now_instead_of_waiting_out_the_interval`
    runs the real pruner thread with `RETENTION_INTERVAL_SECONDS` pinned to
    3600 s, so a second pass can only come from the wake; without the fix it
    fails on `the pruner slept out its interval while the disk was blocked`.
    It also asserts a writable disk does not wake it and that a block a pass
    cannot clear does not re-arm.
    `test_a_failed_append_blocks_the_disk_and_asks_for_a_pass` drives
    `_write_loop` with an append raising `OSError(28, "No space left on
    device")` and fails without the fix on `a full disk left the pruner
    asleep`.
    Local gate: `tests/market_tape/test_record.py` 53 passed, 3 skipped (`zstd`
    absent); `ruff check` and `ruff format --check` clean; `mypy market_tape`
    clean. `scripts/dev.sh check` reports 6 pre-existing `unused-ignore` mypy
    errors in `liquidity_migration/` and 12 pre-existing failures in
    `tests/market_tape/test_load.py` and
    `tests/scripts/test_observability_hygiene.py`; all are identical on a clean
    tree and are this sandbox missing `zstd`, `rclone`, `shellcheck`, `polars`
    and `numpy`. The change touches no Rust and no `liquidity_migration/`.
  - Diagnosed, not fixed, because it did not contribute. `projected_gb` *falls*
    while blocked — Bybit 1511.6 → 1491.1, Binance 497.0 → 489.9 — because
    `_meter` is only reached on the write path
    (`market_tape/record.py:1094`, `:1097`), which the drop at `:1088-1090`
    skips. The budget measures inbound bytes against the month's line and those
    bytes arrived over the wire whether or not they were written, so the
    projection under-reads exactly when the recorder is losing the most. Here
    it changed nothing: both projections sat far under their caps (2400 and
    700 GB) with no feed shed, so no tier was un-shed. It is a
    budget-accounting and reporting bug, and it is the owner's call whether to
    meter the dropped frames.
  - Open, and only the host can settle it: whether the floor was crossed by the
    tape exceeding its own caps or by non-tape files taking the room.
    `max_disk_gb` is 60 (Bybit) plus 18 (Binance) = 78 GB of tape, which with
    the 25 GiB floor leaves about 15 GB of a 118 GB disk for the OS, the venv,
    engine artifacts, WALs and archives; STATE.md's 18:13 UTC reading was 32 GB
    free. If non-tape growth is what crossed it, this fix makes the recorders
    delete tape to buy the engine headroom, which is correct but is not a size
    decision — that is `deploy/capture/*.toml`, and the owner's.
  - **Merged and undeployed.** The fix is `1d8fad9a` on `main`. The push
    started no cloud job (by design since `a487af06`), and the dispatched
    deploy, run `33928248402`, failed 5 s after it was created at 23:06:57 UTC:
    `ci`, `rust` and `Deploy artifact` each died in 3–4 s and their log
    downloads return HTTP 404, so the jobs never started and `vps` was skipped
    along with every other job. That is the same account-payment refusal
    STATE.md already carries, not a test failure — nothing in this change was
    ever run by a runner. Deployed commit stays `65ee75a7`, so **the recorders
    on the host still have the 300-second sleep and will lose tape on the next
    crossing.**
  - The SSH path needs no runner:

    ```bash
    EXPECTED_COMMIT=1d8fad9a26dbabf6bf9865d805c3c20a1fc78d3c scripts/ops.sh deploy
    ```

    This change is Python under `market_tape/`, so it restarts the two
    recorders and nothing else. The same deploy also carries `697341e4` and
    `10ed1bd2`, which do change the `engine` tree and so hand over both realms
    and restart the funded engine — that cost belongs to those commits, not
    this one.
  - Host-side, by hand:

    ```bash
    # What the floor is actually reading, and who holds the space
    scripts/ops.sh status
    du -sh /var/lib/liquidity-migration/forward-market \
           /var/lib/liquidity-migration/forward-market-binance
    df -h /var/lib

    # Did the funded account keep its heartbeat through the incident
    scripts/ops.sh curve mainnet

    # After the deploy, both recorders should read disk_blocked=False
    scripts/ops.sh logs forward-capture.service 50
    scripts/ops.sh logs forward-capture-binance.service 50
    ```

- **2026-09-04 ~21:24 UTC — Mainnet signal worker paged `degraded` when its
  120-minute grace expired, and the transport clauses in the page were the
  hourly universe refresh rebuilding the stream, not an outage.**
  - Incident `mainnet-014ec4a90a2fde5f`, scope `mainnet`, host
    `ip-208-84-103-4`, ref
    `worker-status:liquidity-migration-signal-worker-mainnet.service`. Exact
    alert text: `CRITICAL liquidity-migration-signal-worker-mainnet.service
    reports 'degraded': Bybit WebSocket repair gap open for 75s; ticker
    coverage incomplete (169/169 rows, 169/169 topics accepted); carry cycle
    has not completed`. The funded engine was not named, did not page, and is
    not implicated. No unit was down and no heartbeat was stale.
  - Timeline. The payload carries no page timestamp; every time below is
    derived from its journal, which runs unbroken to 21:23:19. The unit was
    stopped 19:22:53 and started 19:23:13 (pid 2264838). `STARTUP_MAX_MS` is
    120 min (`engine/signal-worker/src/live.rs:34`), so the grace ended
    21:23:13; with `last_carry_cycle_completed_wall_ts_ms` still `None`,
    `startup_runtime_status` (`live.rs:2859`) stops returning `starting` at
    that instant and the 3-minute watchdog paged at its first run after it.
    That is the whole verdict. The transport clauses are not why it paged.
  - Diagnosis. 75 s before the page puts the gap's open stamp within seconds
    of 21:23:19, the two instrument-lane rejection lines
    (`live.rs:1775`, `live.rs:1783`, inside `commit_universe_inputs`), which
    the Instruments arm calls at `live.rs:957` immediately before
    `reconfigure_stream` at `live.rs:966`. Nothing else can stamp a
    75-second-old gap: `open_gap` (`bybit_ws.rs:415`), `mark_source_fault`
    (`bybit_ws.rs:261`) and `prepare_epoch` (`bybit_ws.rs:366`) all use
    `gap_open_since_ms.get_or_insert`, so an already-open gap keeps its
    original stamp, and the journal carries no `gap opened in epoch` line and
    no lane failure between the 19:23:13 start and 21:23:19. What did happen
    is `reconfigure_stream` replacing the whole `BybitPublicStream` because
    the refreshed universe moved the symbol set. The health record lives in
    that object, so `gap_open_since_ms`, `reconnect_count`, `fault_count` and
    `epoch` all reset, and the successor's first epoch is 1, whose
    `reconnected: self.epoch > 1` (`bybit_ws.rs:768`) is false — no journal
    line marks the rebuild either.
  - Two consequences, the second worse than the page. The gap age the on-call
    page reads is the age of the last universe refresh, so a two-hour outage
    can read as seconds old and this incident's two pages (3651 s at 17:58,
    75 s here) are not comparable. And epoch numbering restarting at 1 defeats
    the token `mark_gap_repaired` matches on (`bybit_ws.rs:247`): `repair_epoch`
    still holds the outgoing stream's epoch, a stream that never disconnected
    sits at epoch 1, so a repair lane in flight across a rebuild can close the
    successor's boot gap on a token minted for a different subscription —
    coverage declared complete on one that was never verified.
  - Changed. `StreamContinuity` (`bybit_ws.rs:75`) is the transport history a
    replacement stream carries: epoch, gap flag and stamp, reconnect and fault
    counts. `BybitPublicStream::spawn_continuing` (`bybit_ws.rs:128`) seeds
    both the shared state (`SharedState::continuing`, `bybit_ws.rs:350`) and
    the worker's epoch counter from it, so the successor's first epoch is above
    every epoch an in-flight repair still holds and its boot gap keeps the
    older stamp. `LiveRunner::stream_reconfiguration` (`live.rs:2296`) returns
    the moved symbol set with the outgoing stream's history and
    `reconfigure_stream` (`live.rs:2310`) hands it over. No cadence, threshold,
    grace window or health definition changed.
  - Tests.
    `bybit_ws::tests::a_replacement_stream_continues_the_epoch_and_the_gap_clock`
    fails without the fix at `left: 0, right: 2` on the carried reconnect count,
    and asserts the successor's first epoch is 4 above an outgoing 3 and its
    gap stamp is the outgoing one.
    `live::tests::a_universe_refresh_hands_the_replacement_stream_the_old_transport_history`
    fails with `StreamContinuity { epoch: 0, gap_open: false, gap_open_since_ms:
    None, reconnect_count: 0, fault_count: 0 }` against the outgoing stream's
    `gap_open: true, gap_open_since_ms: Some(8640000000), fault_count: 1`.
    Local gate: `cargo test -p signal-worker` 120 passed (118 before these
    two), `cargo test --workspace` all green, `cargo fmt --check` and
    `cargo clippy --workspace --all-targets -D warnings` clean, Ruff clean, and
    `tests/scripts/test_scripts_check_fleet_liveness.py` 39 passed. The rest of
    pytest cannot collect in this sandbox — `certifi`, `numpy`, `polars` and 22
    other pinned packages are absent — which is a sandbox limit, not this
    change: it touches no Python.
  - Not fixed here, and it is the larger half. Why a mainnet cold fill has no
    completed carry cycle after 120 min is the same open question the demo page
    left at 19:16, and the payload does not reach it. Also unresolved by design:
    `ticker coverage incomplete (169/169 rows, 169/169 topics accepted)` is not
    a contradiction — `ticker_coverage_complete` turns on two inputs those
    counts do not measure, every sampled row carrying a mark price fresher than
    `mark_max_age_ms` (`sample_tickers`, `bybit_ws.rs:224`) and every cached row
    having been seen in a WebSocket snapshot (`TickerCache::ws_coverage_complete`,
    `bybit_ws.rs:634`). A cache refilled by the REST fallback after a rebuild
    reads 169/169 with coverage false. Publishing those two numerators would
    make the next page readable; it is instrumentation the owner has not asked
    for, so it is proposed here, not added.
  - Not the cause, for the next reader: the missing ~20:23 instrument-lane
    summary between 19:23:19 and 21:23:19 is the dropped hourly tick already
    fixed on `main` as `b29fd37` and undeployed. It is why the 21:23:19 refresh
    carried two hours of membership drift and moved the symbol set. The hourly
    `691 instrument row(s) left out of the table` and `40 ticker row(s)` lines
    are Bybit's dated futures kept out of a perpetuals table by design
    (`engine/signal-worker/src/normalize.rs:136`).
  - Deploy: **blocked, not done.** `vps-deploy.yml --ref main -f mode=deploy`
    dispatched run `33922197522` on `aaea42da` at 21:40:49 UTC. `ci`, `rust`
    and `Deploy artifact` each failed 3–4 s later with no log content at all
    (log download returns HTTP 404 on every one), and `vps`, `diagnose`,
    `disarm` and the qualification job were all skipped behind them. Same
    external block STATE.md already records — GitHub will not start hosted work
    while the account's payments are failing — and the same shape as
    `33921858031` at 21:36 and `33911912004` at 19:35. Deployed commit stays
    `65ee75a7`; mainnet stays on uninterrupted process commit `218905d4`. This
    fix is on `main` and unshipped.
  - Owner action, to deploy without a runner. Note what it costs: the realm
    fingerprint hashes the whole `engine` tree, so both realms take a real
    handover and the funded engine restarts.

    ```bash
    EXPECTED_COMMIT=10ed1bd2488570055a37b53b7b92dd959e863850 scripts/ops.sh deploy
    ```

  - Owner action, on the host. The minute samples hold what the page cannot:

    ```bash
    # The transport and the cold fill through the two hours before the page.
    grep '"kind": *"worker"' \
      /var/lib/liquidity-migration/equity/worker-mainnet-$(date -u +%Y-%m).jsonl \
      | jq -c 'select(.ts_ms >= 1788549600000)
               | {t: (.ts_ms/1000 | strftime("%H:%M")), status, ws_connected,
                  ws_gap_age_ms, ws_last_frame_age_ms, kline_topics_accepted,
                  ticker_capacity, carry_cycle_age_ms, long_cycle_age_ms}'

    # And the account through the same window.
    scripts/ops.sh curve mainnet
    ```

    A `ws_gap_age_ms` that drops to near zero at 21:23 without the carry cycle
    ever leaving `None` confirms the rebuild reset the clock rather than the
    transport recovering.

- **2026-09-04 ~20:25 UTC — Demo signal worker paged `degraded` 62 minutes
  into a fresh process, which proves a transport blip the page still cannot
  name: one Bybit frame drought longer than 30 s and shorter than the socket's
  own 45 s allowance.**
  - Incident `demo-0922e9f30da3bf98`, scope `demo`, host `ip-208-84-103-4`,
    ref `worker-status:liquidity-migration-signal-worker-demo.service`. Exact
    alert text: `CRITICAL liquidity-migration-signal-worker-demo.service
    reports 'degraded': Bybit WebSocket repair gap open for 3700s; carry cycle
    has not completed`. Demo only: the funded engine and the mainnet worker
    were not named, no unit was down, and no heartbeat was stale.
  - Same incident id as the 19:16 page because the id hashes scope plus refs
    (`check_fleet_liveness.py:807`), not the occurrence. This is a second
    firing, not a repeat of the first: an incident fires only for a CRITICAL
    key absent from the state file (`select_incidents_to_fire`,
    `check_fleet_liveness.py:719`), and a key is dropped when its condition
    clears (`select_alerts_to_send`, `check_fleet_liveness.py:694`). The
    19:22:23 stop cleared it, so this page is the **first** 3-minute check
    after the 19:22:45 start that saw a status outside
    `starting`/`recovering`/`ready`.
  - Timeline. pid 2264247 started 19:22:45. Gap age 3700 s puts the page at
    20:24:27–20:25:25 and the gap stamp at 19:22:47–19:23:45 — this process's
    boot gap (`SharedState::prepare_epoch`,
    `engine/signal-worker/src/bybit_ws.rs:305`, held unchanged by
    `gap_open_since_ms.get_or_insert`, `bybit_ws.rs:310`). Its journal carries
    exactly two lines, the instrument lane's rejection summary at 19:22:53 and
    again at 20:22:53 — so the hourly refresh that the 19:16 entry found
    dropped did run this hour, one hour apart, as `b29fd373` intends.
  - What the verdict proves. `STARTUP_MAX_MS` is 120 min
    (`engine/signal-worker/src/live.rs:34`) and the page is at 62 min, so the
    grace had **not** expired. With `last_carry_cycle_completed_wall_ts_ms`
    still `None`, `startup_runtime_status` (`live.rs:2842`) returns `starting`
    exactly while `stream_transport_healthy` (`live.rs:2777`) is true and
    `degraded` the moment it is not. The verdict was `degraded` and the status
    was acceptable at every earlier check, so transport was healthy for the
    first hour and flipped false once, at that heartbeat.
  - Which clause flipped, by elimination. The deployed watchdog (`65ee75a7`)
    prints a clause per failing input, and the page carries none of them
    except the gap and the carry cycle: `bybit_ws_connected` is true,
    `bybit_ws_ticker_coverage_complete` is true, both quarantine counts are 0,
    and the LONG cycle is inside 3 × 60 s. Coverage is recomputed every
    `ticker_cadence_ms` = 5000 in `sample_tickers` (`bybit_ws.rs:168`) and
    requires `ticker_topics_accepted == ticker_capacity` plus a mark no older
    than `mark_max_age_ms` = 30 000 ms for every symbol; with both quarantine
    counts 0 the kline count cannot be short either, because `topics()`
    (`bybit_ws.rs:1409`) subscribes one ticker and one kline topic per symbol
    and `subscribe` accumulates into the live set (`bybit_ws.rs:785`). The only
    input left in `stream_transport_healthy` is frame freshness:
    `bybit_ws_last_frame_ts_ms` more than 30 000 ms behind the heartbeat clock,
    or ahead of it.
  - And the socket never noticed. No fault, no `gap opened in epoch`, no
    `entered epoch`, no quarantine line for pid 2264247, in a tail unbroken
    from 16:20 and taken after the alert clock. So no reconnect happened and
    the drought stayed inside `data_idle_timeout` = 45 s
    (`bybit_ws.rs:605`): between 30 s and 45 s of silence is simultaneously a
    `degraded` producer verdict and a healthy socket, and it leaves no log
    line. `reconfigure_stream` (`live.rs:2293`, called at `live.rs:965`) was a
    no-op at 20:22:53 — a respawn would have reset the gap stamp and shown
    `connected` false, and the page shows neither.
  - The shape it shares with `mainnet-014ec4a90a2fde5f`. That page came ~60 s
    after its hourly instrument lane finished at 17:56:59; this one ~90 s after
    20:22:53. The completion arm runs `commit_universe_inputs`,
    `validate_candidate_instruments`, `reconfigure_stream`,
    `start_kline_repair` and a LONG watermark inline (`live.rs:952`-`977`) on a
    4-vCPU box that also carries two engines, two workers and two recorders.
    Whether that burst starves the stream task or the venue feed itself paused
    is **not** decidable from the payload; the coincidence is twice now.
  - Missing, and the host reading that settles it. The frame age at
    20:24–20:25, and whether the process or the feed stalled. Both are already
    on disk: `worker_sample` (`scripts/runtime/record_equity.py:219`) records
    `ws_last_frame_age_ms`, `heartbeat_age_ms`, `long_cycle_age_ms`,
    `kline_topics_accepted`, `ticker_capacity`, `rest_ticker_success_count` and
    `status` every minute. `_age_ms` clamps at 0
    (`record_equity.py:280`), so a frame stamp ahead of the clock reads as
    age 0 rather than negative.

    ```bash
    # The minute the verdict flipped, and what the frame age did around it.
    jq -c 'select(.kind == "worker" and .ts_ms >= 1788552000000)
           | {t: (.ts_ms/1000 | strftime("%H:%M")), status, ws_connected,
              ws_last_frame_age_ms, ws_gap_age_ms, heartbeat_age_ms,
              long_cycle_age_ms, carry_cycle_age_ms, kline_topics_accepted,
              ticker_capacity, rest_ticker_success_count}' \
      /var/lib/liquidity-migration/equity/worker-demo-$(date -u +%Y-%m).jsonl

    # The account and the heartbeat coverage through the same window.
    scripts/ops.sh curve demo
    scripts/ops.sh logs signal-worker-demo 200
    ```

    A `ws_last_frame_age_ms` above 30 000 at ~20:24 with `heartbeat_age_ms`
    flat says the feed paused; the same spike with `heartbeat_age_ms` and
    `long_cycle_age_ms` rising together says the process stalled and the
    frame age is a symptom.
  - Why the page still could not say it. `_transport_reasons`
    (`check_fleet_liveness.py:221`) prints both of those clauses. It is on
    `main` at `697341e4` and the host runs `65ee75a7`, so the incident lane
    stays blind until that deploy lands. Re-verified now: every
    `vps-deploy.yml` run since 19:15 fails in under 10 s with no job logs —
    `33910262256`, `33910383652`, `33910443631`, `33910515990`, `33911407276`,
    `33911912004`. This session added one build-free receipt of its own:
    `verify` on `e2345ca4`, run `33921858031`, dispatched 21:36:16 UTC. Its
    sole scheduled job `vps` failed at 21:36:22 after 4 s with every other job
    skipped, and its log download returns HTTP 404 — no logs were ever
    produced, so the job never started. The block is unchanged and external.
    No `deploy` was dispatched: this entry ships no code, and a deploy hands
    over both realms and restarts the funded engine.
  - No code change. Nothing in this repository is proven broken by this
    payload: the two candidates are dials and a guard, and both are the
    owner's call under `AGENTS.md`. **Proposal**, for a yes or no: (1) let
    `startup_runtime_status` keep the `TRANSIENT_RECOVERY_MAX_MS` = 2 min
    allowance (`live.rs:35`) that the post-startup path already gets, so a
    single sub-idle-timeout drought during a cold fill reads `recovering`
    instead of paging `CRITICAL`; or (2) raise `mark_max_age_ms` (30 s,
    `configs/signal-worker.demo.json`) to the socket's own 45 s tolerance so
    the two agree. (1) is the recommendation: it changes when the fleet pages,
    not what the worker considers fresh data.
  - Owner action. Nothing to restart — the worker recovered on its own and
    reported `starting` again; this entry is a diagnosis plus one reading. To
    ship the page fix without a runner:
    `EXPECTED_COMMIT=<main HEAD> scripts/ops.sh deploy` (it hands over both
    realms, so the funded engine restarts).

- **2026-09-04 19:16 UTC — Demo signal worker paged `degraded` the minute its
  120-minute cold-fill grace expired; the boot gap and the carry cycle were
  both still where they started.**
  - Incident `demo-0922e9f30da3bf98`, scope `demo`, host `ip-208-84-103-4`,
    ref `worker-status:liquidity-migration-signal-worker-demo.service`. Exact
    alert text: `CRITICAL liquidity-migration-signal-worker-demo.service
    reports 'degraded': Bybit WebSocket repair gap open for 7235s; carry cycle
    has not completed`. The funded engine was not named, did not page, and is
    not implicated. No unit was down and no heartbeat was stale.
  - Timeline. The unit was stopped and started five times between 15:59:58 and
    17:15:08; the paging process (pid 2223359) started 17:15:34. 7235 s before
    the page puts the gap's open stamp at 17:15:35 — the boot gap
    `SharedState::prepare_epoch` opens
    (`engine/signal-worker/src/bybit_ws.rs:305`), which
    `gap_open_since_ms.get_or_insert` then holds unchanged. So the boot gap was
    never closed in this process: `mark_gap_repaired` never ran with complete
    coverage, and the cold fill never finished. `STARTUP_MAX_MS` is 120 min
    (`engine/signal-worker/src/live.rs:34`), which expired at 19:15:34, and the
    3-minute watchdog paged at the first run after it, 19:16:10. The 19:13:25
    `Connection reset by peer` and the epoch-2 reconnect one second later are
    two minutes before the page and did **not** open this gap.
  - Diagnosis. The verdict comes from `startup_runtime_status`
    (`engine/signal-worker/src/live.rs:2842`): with
    `last_carry_cycle_completed_wall_ts_ms` still `None`, the process is
    `starting` only while it is inside the 120-minute window, and `degraded` the
    moment it is not — whatever the transport is doing. The transport was in
    fact sound, and the page's *absent* clauses prove it: read against
    `_signal_worker_detail` (`scripts/runtime/check_fleet_liveness.py:248`),
    `bybit_ws_connected` is true, `bybit_ws_ticker_coverage_complete` is true,
    both quarantine counts are zero, and the LONG cycle is inside its 3× cadence
    window. This is the same producer verdict as incident
    `mainnet-014ec4a90a2fde5f` at 17:58, reached from the other side of the same
    window: that one paged at 61 min on a transport clause, this one at 120 min
    on the clock.
  - The defect fixed here, which the payload does prove. Every lane-local
    source failure is an `eprintln!` (`lane_source_failure`,
    `engine/signal-worker/src/live.rs:270`) and a completed instrument lane
    prints its rejection summary unconditionally
    (`engine/signal-worker/src/live.rs:1773`); `instrument_cadence_ms` is
    `3600000` (`configs/signal-worker.demo.json`) and the demo venue's list
    yields `691 instrument row(s)` + `40 ticker row(s)` rejected every pass. The
    payload's journal covers 15:58:15 to 19:13:26 unbroken. pid 2223359 printed
    that summary at 17:15:36 and nothing at its one due tick, ~18:15:34 — no
    completion line and no failure line, so the lane was never spawned. Its only
    guard was `if !lanes.instruments && !lanes.funding`, and `lanes.instruments`
    cannot stick because it is cleared first thing in its own completion arm
    (`live.rs:953`). That leaves `lanes.funding`, set every
    `funding_cadence_ms` = `60000` (`live.rs:772`). A tick that lost that race
    was dropped outright — there was no retry — so the instrument table and the
    traded universe stood still for a full hour, silently, until the next tick
    into the same race.
  - Changed. `LaneState` gains `instruments_due`, and the hourly tick records
    the request instead of discarding it
    (`engine/signal-worker/src/live.rs:762`).
    `LiveRunner::start_instrument_lane_if_due` (`live.rs:1549`) starts the owed
    refresh as soon as the venue is free, and `LaneCompletion::FundingFinished`
    (`live.rs:1091`) calls it before the carry attempt that may spawn the next
    funding pass, so the refresh outranks it. An in-flight instrument lane
    satisfies the request rather than queueing a second one. The
    funding/instrument mutual exclusion is unchanged, as are every cadence,
    threshold and health definition.
  - Tests. `live::tests::an_instrument_refresh_held_off_by_funding_starts_when_that_pass_ends`
    holds the funding lane, fires the tick, and asserts the refresh survives and
    then starts at `FundingFinished`. Without the call it fails at `the owed
    instrument refresh starts as the funding pass ends`. Local gate:
    `cargo test -p signal-worker` 118 passed, `cargo test --workspace` all
    green, `cargo fmt --check` and `cargo clippy --workspace --all-targets -D
    warnings` clean, Ruff and mypy clean.
  - What this does **not** settle, and it is the larger half. This fix explains
    one missed hourly refresh; it does not explain why a 120-minute cold fill
    did not finish. Two candidates, neither reachable from the payload: the
    fill is genuinely slower than its grace on demo — five restarts in 76 min
    each reset the carry cycle to `None` and reopen the boot gap, and the one
    process that did get a clean two hours still did not finish — or one
    unfillable kline range holds `kline_repair_jobs(current_end)` non-empty
    forever, so the finished-repair check that calls `mark_gap_repaired`
    (`live.rs:1196`) never passes. Nothing logs or publishes that job count, so
    a gap held open by one bad range is unattributable from the page. Both are
    host readings. No guard, gate or extra instrumentation was added for them:
    that is the owner's call.
  - Deploy: **blocked, not done.** `gh workflow run vps-deploy.yml --ref main
    -f mode=deploy` dispatched run `33911912004` on `b29fd373` at 19:35:09 UTC.
    Every hosted job — `ci`, `rust`, `Deploy artifact` — failed 3 s later with
    no log content at all, and `vps` was skipped. This is the same external
    block STATE.md already records: GitHub will not start hosted work for this
    account while its payments are failing. Deployed commit stays `65ee75a7`;
    mainnet stays on uninterrupted process commit `218905d4`. The fix is on
    `main` and unshipped.
  - Owner action, on the host. Nothing to restart for the fix — it lands with
    the deploy, and the deploy needs billing fixed first. To settle the open
    half, read the minute samples the fleet already records (`worker_sample`,
    `scripts/runtime/record_equity.py:219`):

    ```bash
    # The cold fill, minute by minute, across the five restarts and this one.
    grep '"kind": *"worker"' \
      /var/lib/liquidity-migration/equity/worker-demo-$(date -u +%Y-%m).jsonl \
      | jq -c 'select(.ts_ms >= 1788534000000)
               | {t: (.ts_ms/1000 | strftime("%H:%M")), status, ws_connected,
                  ws_gap_age_ms, ws_last_frame_age_ms, carry_cycle_age_ms,
                  long_cycle_age_ms}'

    # Did any lane log at all after 17:15:36? A wedged lane logs nothing.
    scripts/ops.sh logs signal-worker-demo 400

    # And the account through the same window.
    scripts/ops.sh curve demo
    ```

    A `ws_gap_age_ms` that climbs from every restart without ever resetting to
    zero says no cold fill has ever completed on demo, which is a config
    question (the 120-minute grace, or the required history), not a restart.
    Five deliberate stops in 76 min is its own question: each one throws away
    the fill in progress.

- **2026-09-04 19:20 UTC — Per-commit Actions work is removed from the funded
  release path.**
  - The prior 24 hours contained 67 commits and 90 workflow runs. Their jobs
    consumed about 2,023 rounded runner-minutes: 937 in release soak and
    benchmark work, 489 in Rust gates, 357 building deploy artifacts, and 151
    in Python CI. The latest 100 artifacts alone occupied 1,228 MB against the
    private Free account's 500 MB included storage.
  - Ninety-eight obsolete, reproducible archives (1,203.7 MiB) were deleted.
    The latest exact archives for deployed commit `65ee75a7`, rollback target
    `16d52f88`, and the uninterrupted funded process `218905d4` remain: three
    artifacts totaling 37.1 MiB. Deleted archives are not recoverable from
    Actions, but their commits can reproduce them.
  - A push to `main` no longer starts GitHub-hosted work. Code pull requests
    retain Python and Rust gates and supersede older checks for the same pull
    request; docs-only pull requests are ignored. The local pre-push gate still
    checks Python and Rust before a direct push, and a funded deploy reruns the
    gates against the exact dispatched SHA before touching the host.
  - Release tests, the two-million-operation account-state soak, and the
    order-path benchmark move to explicit `mode=qualify`. Only `mode=deploy`
    creates and uploads the release archive, with two-day retention. Verify,
    rollback, diagnose, and disarm skip every build and test job.
  - The full local gate exposed a deterministic fresh-process failure in
    `the_override_is_confined_to_its_own_thread`: it assumed a newly initialized
    monotonic clock must already exceed 7 ns. The test now uses `u64::MAX` as
    the virtual sentinel, so it checks thread confinement without scheduling
    time as an input and no longer causes paid reruns.
  - This stops new automatic burn but does not restore already exhausted
    hosted capacity. The repository remains private. The funded VPS is not a
    runner; reliable no-minute operation requires a separate private Linux
    build host, while the ordinary workstation remains a poor always-on
    production dependency.
  - Run `33911407276` exercised the new build-free `verify` path: CI, Rust,
    artifact, and qualification jobs all skipped, and `vps` became the only
    scheduled job. GitHub refused it before runner assignment with the same
    failed-payment or spending-limit annotation. Storage cleanup therefore did
    not restore hosted compute; the external capacity block remains.

- **2026-09-04 ~17:58 UTC — Mainnet signal worker paged `degraded`; the page
  could not name its own cause, and now does.**
  - Incident `mainnet-014ec4a90a2fde5f`, scope `mainnet`, host
    `ip-208-84-103-4`, ref
    `worker-status:liquidity-migration-signal-worker-mainnet.service`. Exact
    alert text: `CRITICAL liquidity-migration-signal-worker-mainnet.service
    reports 'degraded': Bybit WebSocket repair gap open for 3651s; carry cycle
    has not completed`. The funded engine was not named and did not page; no
    unit was down, no heartbeat was stale, and the worker's own heartbeat was
    fresh.
  - Timeline from the payload's journal: the unit was restarted at 14:42:22,
    16:00:56, 16:43:14 and 16:56:53 UTC. The last process (pid 2212679) logged
    the hourly instrument-lane rejection summary at 16:57:00 and 17:56:59 and
    nothing else. `bybit_ws_gap_open_since_wall_ts_ms` + 3651 s puts the page
    at ~17:58 UTC, so the gap had been open since the 16:56:53 start — the boot
    gap `SharedState::prepare_epoch` opens
    (`engine/signal-worker/src/bybit_ws.rs:309`), which only
    `mark_gap_repaired` closes, and that runs only once a repair finishes with
    complete coverage (`engine/signal-worker/src/live.rs:1190`).
  - Diagnosis. The verdict comes from `heartbeat_status`
    (`engine/signal-worker/src/live.rs:2778`). With the carry cycle still
    `None`, `startup_runtime_status`
    (`engine/signal-worker/src/live.rs:2809`) returns `starting` — not
    `degraded` — while transport is healthy and the process is inside
    `STARTUP_MAX_MS` (120 min). The page came 61 min after start, so the
    verdict proves `stream_transport_healthy`
    (`engine/signal-worker/src/live.rs:2744`) was false at that heartbeat. Its
    clauses split in two: the ones the page reports (connected, ticker
    coverage, quarantine counts — all sound here) and two it does not — kline
    topics accepted against the symbol count, and frame age against
    `mark_max_age_ms` (30 000 ms, `configs/signal-worker.mainnet.json`). One of
    those two flipped the worker, and the page named neither, so the on-call
    routine could not reach the host-side fact. That gap in the incident lane
    is the fault fixed here; the wobble itself is a host reading the routine
    has no transport for.
  - Changed. `WorkerHeartbeat` now publishes `bybit_ws_max_frame_age_ms`, the
    limit the worker itself judges `bybit_ws_last_frame_ts_ms` by
    (`engine/signal-worker/src/live.rs:105`, set in both heartbeat writers).
    `_signal_worker_detail` in `scripts/runtime/check_fleet_liveness.py` now
    reports, for a connected worker, `N/M kline topics accepted` when the
    subscription is short, and `no Bybit WebSocket frame for Ns (limit Ns)`, a
    missing frame stamp, or a future one. A disconnected worker keeps its
    single line. No threshold, grace window, or health definition changed.
  - Tests. `tests/scripts/test_scripts_check_fleet_liveness.py::test_degraded_worker_page_names_the_transport_input_that_decided_it`
    rebuilds this incident's heartbeat; without the fix it fails with the
    incident's exact message, `worker reports 'degraded': Bybit WebSocket
    repair gap open for 300s; carry cycle has not completed`.
    `live::tests::the_heartbeat_publishes_the_frame_age_limit_it_judges_itself_by`
    fails when the limit is published as anything but `mark_max_age_ms`.
  - Not the cause, for the next reader: the hourly `691 instrument row(s) left
    out of the table (BTC-01DEC23: input: invalid symbol …)` and `40 ticker
    row(s) …` lines are Bybit's dated futures being kept out of a perpetuals
    table by design (`engine/signal-worker/src/normalize.rs:136`). They are
    two-thirds of the payload's 40 journal lines and carry no fault.
  - Owner action, on the host. The minute samples already hold the reading the
    page lacked — `worker_sample` records `ws_last_frame_age_ms` and
    `kline_topics_accepted` (`scripts/runtime/record_equity.py:251`), and
    `scripts/ops.sh curve mainnet` reads only the engine file, so read the
    worker file directly:

    ```bash
    # What the transport did through the incident hour, minute by minute.
    grep '"kind": *"worker"' \
      /var/lib/liquidity-migration/equity/worker-mainnet-$(date -u +%Y-%m).jsonl \
      | jq -c 'select(.ts_ms >= 1788537600000 and .ts_ms <= 1788544800000)
               | {t: (.ts_ms/1000 | strftime("%H:%M")), status, ws_connected,
                  ws_last_frame_age_ms, kline_topics_accepted, ticker_capacity,
                  ws_gap_age_ms, carry_cycle_age_ms}'

    # And the account through the same window.
    scripts/ops.sh curve mainnet
    ```

  - Deploy receipt: none. Commit `697341e4` is on `main`; its push checks (run
    `33910211410`) and the dispatched `vps-deploy.yml mode=deploy` (run
    `33910262256`) both failed in seconds with `ci`, `rust`, and
    `Deploy artifact` producing no logs at all (HTTP 404 on every job log) and
    `vps` skipped behind them. Every run since `8352e564` at 18:03 UTC ends
    the same way; the last run to execute anything was `33900447763` at
    17:24 UTC. Not specific to this work: the owner's own `93ab5cd` failed the
    same way in 6 s at 19:17 UTC, and run `33910443631` in 7 s at 19:18 UTC.
    This is GitHub refusing to start jobs for the account, not a
    test failure — the same billing refusal recorded at 18:17 UTC below. The
    fix is therefore merged and undeployed, and the host still runs
    `65ee75a7`. Local gate on this commit: Ruff, mypy (99 files), 1 429
    pytest, `cargo fmt`, `cargo clippy -D warnings`, and every Rust workspace
    test pass; the 17 pytest and 1 `market-tape` failures in this sandbox are
    missing `zstd`, `rsync`, `rclone`, and `shellcheck` and fail identically
    on the parent commit.
  - Owner action, to deploy once billing is fixed — or now, over SSH, which
    needs no GitHub runner:

    ```bash
    EXPECTED_COMMIT=697341e48fed6f23137860a58be4c5c13e7ae02e scripts/ops.sh deploy
    ```

    Note what that costs: the realm fingerprint hashes the whole `engine`
    tree, and this commit edits `engine/signal-worker/src/live.rs`, so
    `realm_unchanged` fails for both realms and each takes a real handover —
    stop, state import, start, fresh heartbeat inside 180 s. The funded
    engine's own crates are untouched, but it does get restarted. Deploy when
    the owner is willing to spend that, not because a watchdog message is
    waiting.
  - A `ws_last_frame_age_ms` above 30 000 names a frame drought;
    `kline_topics_accepted` below `ticker_capacity` names a short
    subscription. Nothing needs restarting for this fix: it changes only what
    the next page says. Four restarts in two hours on a two-hour cold-fill
    window is its own question — each restart resets the carry cycle to `None`
    and reopens the boot gap.

- **2026-09-04 18:17 UTC — Fleet observability is live; on-call delivery works,
  but GitHub billing blocks autonomous host action.**
  - The host writes six local JSONL samples before each remote push. Three
    consecutive verification rows say `recorded and pushed 6 samples` to the
    existing Grafana Cloud GB South zone 55 stack. The Johor host stays on that
    stack: this once-per-minute payload has no latency-sensitive control role,
    while changing regions would replace credentials, history, and stack
    identity for no operational gain.
  - Dashboard UID `liqmig-fleet` is reduced from 27 panels to 15: four current
    status tiles; four independently scaled equity and open-exposure cards;
    execution activity; p99 order-path latency; and compact freshness, capacity,
    and fault state. It is saved against
    `grafanacloud-proudtortoise1017-prom`; live engine, worker, recorder,
    account, and latency data populate. Explore resolves the `lm_` family and
    returns both realms for `lm_engine_account_age_ms`.
  - Commit `9a5abcf7` carries final dashboard version 10 after the operator,
    funded-scale, legend, single-purpose-panel, and locally padded sparkline
    passes. The renderer matches the generated JSON, all 27 dashboard tests
    pass, and the push gate passes Ruff, ShellCheck, mypy, 1,454 Python tests,
    Rust formatting and Clippy, and every Rust workspace test.
  - Run `33900447763` passed CI, Rust, and the verified artifact for
    `65ee75a7`, then GitHub refused to start the VPS job because recent account
    payments failed. The VPS recovery recipe installed the same exact artifact
    and commit at 17:34:41 UTC; rollback target is `16d52f88`. The funded engine
    was not restarted and remains on process commit `218905d4` with five
    positions, `may_open=true`, no loss trip, no strategy errors, and no pending
    flatten.
  - Fresh demo, mainnet, and host checks each return
    `ok scope=<scope> units-and-heartbeats-healthy`; all four one-shot results
    are `success` with exit 0, every always-on unit is active, and no systemd
    unit is failed. Both workers are transport-healthy and fully covered during
    bounded CARRY cold fill. Bybit is 16/16 connected and Binance 10/10, with
    zero reconnects, frame drops, disk drops, or blocked disk.
  - The live delivery drill already proved all three routes: Telegram accepted,
    the no-change Claude Code routine accepted, and the independent dead-man
    accepted in 2.9 s. Telegram remains the human pager, trade feed, and
    constrained control surface. The routine intentionally has no host SSH or
    venue credentials; its only diagnostic/deploy transport is
    `vps-deploy.yml`. GitHub's billing refusal therefore prevents it from
    completing autonomous host diagnosis or repair until the account is fixed,
    even though detection and all three delivery routes work.

- **2026-09-04 16:50 UTC — A realm handover is transactional through state
  takeover, and a deploy never disables its watchdogs.**
  - The 16:21 incident below exposed two separate faults in
    `scripts/deploy_vps_live.sh`: `stop_realm_units` used `disable --now`, and
    only a failed `start_realm` entered `rollback_after_failure`. A state
    import refusal exited earlier, leaving the realm, its liveness timer, and
    its boot enablement off.
  - A changed realm now runs stop, native-state import, and verified start as
    one handover. Any failure enters the existing exact-generation rollback;
    its fingerprint is recorded only after the whole handover succeeds.
    Transient handover uses `systemctl stop`, preserving enablement. The
    explicit funded stop/disarm path still uses `disable --now`.
  - `test_a_deploy_handover_stops_units_without_disabling_the_watchdogs`
    traces every manifest unit in both realms. The handover trace covers
    import failure, start failure, and success, including rollback and the
    fingerprint boundary. Both tests fail on `ba68e719`: the old trace contains
    `disable --now`, and no `handover_realm` exists. They pass with the repair.
    Full local gate: repository doctor ready, Ruff, ShellCheck and mypy clean,
    1,451 Python tests pass, Rust format and Clippy clean, and every Rust
    workspace test passes.
  - Manual deploy, rollback, and verify runs no longer repeat the release-only
    tests, soak, and benchmark. The exact SHA's push run still performs them;
    the manual VPS job retains its CI, Rust, release-artifact, and smoke gates.
    This keeps an off-path job from holding the serialized production queue
    after the host operation has finished.
  - Deploy receipt: run `33898806448` installed `16d52f88` at 17:15:42 UTC.
    CI passed in 1m51s, Rust in 4m05s, the verified release artifact in 5m31s,
    and the release-soak job was skipped in 0s. The VPS job finished in 1m04s:
    demo imported state and published fresh worker and engine heartbeats;
    mainnet and both recorders were `unchanged-left-running`; rollback target
    `218905d4`; `real-money armed`; every timer active.
  - Host verification: the installed handover contains `systemctl stop` and
    no `disable --now`; both realm workers and engines, both recorders,
    Telegram controls, trade notifications, and all three watchdog timers are
    active; every watchdog timer is enabled; `systemctl --failed` is empty.
    Demo and mainnet liveness plus the independent host scope each return
    `ok scope=<scope> units-and-heartbeats-healthy`. Mainnet kept its five
    positions, `may_open=true`, no rolling-loss trip, no strategy errors, and
    no pending flatten.
  - Live delivery drill: invocation `232a3630119b4907a9c8145b6512b904`
    returned `telegram accepted`, created no-change Claude Code session
    `session_01Jf15281KD9pJvLF32sFJGw`, and returned `dead-man accepted` in
    2.9 s. A post-release sampler run returned `recorded and pushed 6 samples`;
    both worker rows are durable locally and report connected, full ticker
    coverage, and no spool backpressure.
  - The 17:19:44 scheduled host tick exposed one remaining noisy edge:
    `WARNING capture-shards:forward-market-binance` reported 2 of 12 sockets
    down. At 17:19:34.625 the recorder published the newly expanded 12-shard
    topology; both sockets connected at 17:19:34.857/.863, and the next status
    was 12/12 with zero queue or disk drops. Partial shard loss now must appear
    on two consecutive host ticks before warning. Complete connection loss,
    stale frames, blocked storage, and drops remain immediate.

- **2026-09-04 16:45 UTC — The order path has a measurement again, and the
  recorder stopped dropping frames. Both verified on the host.**
  - `2594e6b6` deployed at 16:41 UTC in 317 s with an atomic mainnet
    handover. All six units active, both engines reporting the commit,
    `may_open` true, 5 positions each, zero strategy errors, every watchdog
    timer active and enabled. Demo runs four sleeves: the appended `probe` id
    3 is the first sleeve added to a realm with a non-empty WAL, which is what
    the append-only name check exists for.
  - The probe fired at 16:45:00 and again at 17:00:00.000780 UTC, on the
    wall-clock boundary, logging `probe rested symbol=BTCUSDT px=77314.0
    qty=0.001`, and the sampler at :21 read each measurement out of the
    engine's 60-second ledger: `end_to_end` p50 11.84 then 11.29 ms, `wire`
    p99 11.74 then 11.20 ms, WAL barrier p99 0.36 then 0.23 ms, `decide` under
    a microsecond. By the following minute the ledger was null again, which is
    the window doing its job: the probe at :00 and the sampler at :20 catch
    each other exactly once, and that is why the probe fires on the wall clock
    rather than on an interval since boot.
  - **Correction to this entry as first written.** It said `wire` is the socket
    write and the venue's round trip is the separate `ack` step, so this box's
    order path spends its time on the socket write. Both halves are wrong.
    `Segment::Wire` is recorded as `completed_ns - decided_ns`
    (`engine/engine-core/src/engine/venue_completion.inc.rs:56`) — the whole
    venue task, round trip included — and `Segment::Ack` records only when the
    adapter stamped `sent_ns`. `engine latency --wal` states it plainly: on the
    demo WAL all 107 `place` commands "carry no transport stamps, so their
    venue round trip is inside `all of it`", where it is p50 9.67 ms over those
    107. So an 11 ms demo reading is decision-to-completion including the round
    trip, not a socket write.
  - What the same tool says about the funded realm, which the probe does not
    touch: 317 of 320 mainnet places do carry the stamp, and their venue round
    trip is p50 3.74 ms, p99 59.48 ms, worst 429.76 ms, inside an `all of it`
    of p50 3.95 ms, p99 429.92 ms. The tail is the venue's, not the engine's.
    Named, not acted on: whether demo's places should carry the same stamp as
    mainnet's is a venue-adapter question and the owner's call.
  - On BTCUSDT the venue floor is `minOrderQty` 0.001 BTC, about 77 USDT at
    today's price, which dominates the 5 USDT `minNotionalValue`. The probe's
    `notional_usdt = 5.5` is therefore a floor the venue overrides, and each
    demo probe rests about 77 USDT of notional 3% under the bid for two
    seconds. Immaterial against 1,629 USDT of demo equity, and it never
    reaches the funded account, which has no probe.
  - Recorder, since the 16:19 restart carrying `skip_utf8_validation` and
    `queue_frames = 131072`: **0 queue overruns, 0 ping/pong timeouts, 0 shard
    reconnects, 0 dropped frames**, 16 of 16 shards connected, queue fill
    0.00. The 24 hours before the fix had 348 overruns, 160 timeouts, 501
    reconnects and 368 dropped frames.
  - The sampler emits every configured sleeve, so `sleeve_positions` now reads
    `{carry: 2, exodus: 0, long: 3, probe: 0}` on demo: a flat sleeve is a
    line at zero instead of a missing series, which is what the dashboard's
    Exodus gap was.

- **2026-09-04 16:21 UTC — Incident `host-bf5dcb6544d0dfdc`: the demo realm's
  own watchdog has been off since 16:17:16, and a hand restart cannot re-arm
  what the deploy disabled.**
  - The page, `scope=host` on `ip-208-84-103-4`, one new `CRITICAL` ref
    `watchdog:demo`: "`demo watchdog timer is inactive (disabled)`"
    (`scripts/runtime/check_fleet_liveness.py:566`). The routine session was
    created 16:21:10; the same line repeats in the host journal at 16:22:41,
    16:25:51 and 16:28:53.
  - The cause is the takeover fault in the entry below, repaired by `6027e7c`:
    `import_native_strategy_state` (`scripts/deploy_vps_live.sh:1231`) runs
    after `stop_realm_units demo` (`:1230`), `fail` is `exit 1`, and
    `rollback_after_failure` (`:338`) covers only `start_realm`, so a refused
    takeover exits with the realm's units stopped **and** disabled.
  - What this page adds. It fired before run `33894054427` reached the host.
    That job's ssh opened at 16:21:31 and its `stop_realm_units demo` ran at
    ~16:21:39; the incident POST landed at 16:21:09. The host-liveness ticks
    either side are 16:22:41, 16:25:50 and 16:28:53 on `OnUnitActiveSec=3min`,
    so the firing tick ran at ~16:19:41, and the demo-liveness journal in its
    payload ends at 16:17:16 with no 16:20:16 run. The demo timers were
    therefore already `disable --now`-ed before that deploy. `scripts/ops.sh
    deploy` (`scripts/ops.sh:299`) execs `scripts/deploy_vps_live.sh` straight
    over SSH and leaves no Actions record, and `stop_realm_units` (`:449`) is
    the only path in the tree that leaves a demo unit inactive and disabled:
    the same takeover refusal happened once out of band at ~16:18-16:19, and
    run `33894054427` then repeated it into a log at 16:22:06. Out-of-band
    deploys are demonstrably in use: run `33895768916`'s `vps` job exited
    `deploy failed: another deploy is already running` at 16:40:20, so
    something outside Actions held `/run/liquidity-migration/deploy.lock` at
    that moment.
  - The 16:24 hand restore started `liquidity-migration-engine.service` and
    `liquidity-migration-signal-worker-demo.service`. It did not re-enable the
    two demo timers. Diagnose run `33895444319`, 16:29:54:
    `liquidity-migration-demo-liveness.timer inactive`,
    `liquidity-migration-chaos-drill.timer inactive`, demo engine active
    heartbeat 1 s, demo worker active heartbeat 4 s, mainnet timer and units
    active, `real-money armed`, `deployed 1193043`. So the demo realm traded
    from 16:17:16 with nothing reading its heartbeat ages, `may_open`, the
    rolling-loss trip or the worker verdict; the host scope saw only that the
    timer was down. `ops.sh restart|start` runs bare `systemctl`
    (`scripts/ops.sh:216`), never `enable`, so a hand restart cannot restore
    what `disable --now` stripped, and until something enables them the timers
    do not return at boot either.
  - No code change here, and `6027e7c` is the right repair, re-derived from
    source: `Engine::boot`
    (`engine/engine-core/src/engine/boot_recovery.inc.rs:90`) already required
    only `configured.starts_with(prior)`, and `Engine::rotation_base`
    (`engine/engine-core/src/engine.rs:1149`) rewrites the Names table from
    the running config, so the WAL's 3-name table becomes 4 at the next
    rotation with no migration. `start_realm` reaches both demo timers through
    `start_unit` (`scripts/deploy_vps_live.sh:298`), which is
    `systemctl enable --now`: a landed deploy is what re-arms them.
  - Deploy receipt, and why this session dispatched none of its own: run
    `33895290275` (`6027e7c`) was cancelled by the next dispatch, and run
    `33895768916` (`2594e6b`, which contains `6027e7c`) reached the host and
    refused the lock at 16:40:20. A third dispatch would only collide with
    whatever holds it. The demo timers are re-armed by the first deploy that
    completes `start_realm demo`; until one does, they are down.
  - Host-side, by hand, only if no deploy lands: re-arm the two demo timers
    directly, since no `ops.sh` verb enables a unit.
    ```
    ssh root@208.84.103.4 'systemctl enable --now \
      liquidity-migration-demo-liveness.timer \
      liquidity-migration-chaos-drill.timer'
    scripts/ops.sh status
    ```
  - Two exposures for the owner, not built here. First,
    `stop_realm_units` disables for a handover that is about to re-enable, and
    the identical two lines run for the funded realm (`:1249`, `:1250`): a
    mainnet takeover refusal would leave the funded
    `liquidity-migration-engine-mainnet.service` stopped and disabled with
    positions open, and no reboot would bring it back. `systemctl stop` would be enough there; `stop_mainnet_units`
    (`:1140`) keeps `disable` for the disarm, where it is load-bearing. Second,
    `rollback_after_failure` could cover the takeover step as it covers the
    start. Both are risk trades on the funded path, so they are the owner's
    call.
  - Host-side readings that would date the out-of-band failure exactly, which
    this session cannot take: `systemctl show
    liquidity-migration-demo-liveness.timer -p InactiveEnterTimestamp`,
    `journalctl -u liquidity-migration-engine.service --since 16:15`, and
    `scripts/ops.sh curve demo 60`, whose minute samples write `state=absent`
    for a realm with no readable heartbeat.

- **2026-09-04 16:11 UTC — Incident `mainnet-014ec4a90a2fde5f`, third fire:
  nothing new in the worker, and the deploy-handoff hold turns out to be inert
  for every key it names.**
  - The page, mainnet scope on `ip-208-84-103-4`, one `CRITICAL` ref
    `worker-status:liquidity-migration-signal-worker-mainnet.service`:
    "`liquidity-migration-signal-worker-mainnet.service reports 'degraded':
    Bybit WebSocket repair gap open for 618s; ticker coverage incomplete; carry
    cycle has not completed`". Same id, same 16:00:56 process as the 16:02
    entry below, 618 s of gap instead of 73 s: the third fire of one flapping
    fault, and `fc22e3b` already carries the repair. The startup gate is
    `stream_startup_inputs_healthy`, which excludes ticker coverage, so a
    booting worker with a dipped coverage count and neither cycle run now
    publishes `starting`. Nothing in the worker is changed here.
  - What the flap adds to the diagnosis, from the payload alone. The mainnet
    timer is `OnUnitActiveSec=3min` and `select_incidents_to_fire` fires once
    per fault lifetime, rearming on resolution, so the runs at 16:05 and 16:08
    read `ok` — which means `stream_inputs_healthy`, ticker coverage included,
    was **true** at those instants. The socket was up and fully subscribed
    throughout; only the mark-freshness clause of coverage moved.
    `tickers.<symbol>` pushes a field only when it changes, so one quiet symbol
    aging past `mark_max_age_ms = 30000` between REST reconciles is enough
    (`engine/signal-worker/src/bybit_ws.rs:179`). The old gate turned that
    30-second dip into a page; the new one cannot see it.
  - Not the cause of this page, fixed because it is wrong as written. The
    deploy-handoff hold added in `1193043` computed `deploy_maintenance` only
    under `scope == "host"`, but every key in
    `_DEPLOY_TRANSITIONAL_ALERT_PREFIXES` except `capture-`, `watchdog:` and
    `manifest` is produced by the realm scopes alone — host watches only the
    units the manifest marks `independent`, and `deploy/fleet_manifest.tsv`
    marks no engine or signal worker so. The hold was therefore inert for
    `worker-status:`, `worker-spool:`, `may-open:`, `rolling-loss:` and the
    fleet's own `unit:`/`heartbeat:` keys, and the mainnet watchdog paged for
    units the host's own deploy was restarting. Every scope now consults the
    lock (`scripts/runtime/check_fleet_liveness.py:988`); a lock held past
    `_MAX_DEPLOY_AGE_SEC` pages through the host scope's `deploy-lock` check
    and the hold lifts at the same bound, so the worst case is 30 minutes of
    realm-scope silence, not indefinite. This page arrived ten minutes after
    the 16:01:05 release, so the hold would not have suppressed it.
  - Correcting this entry as first written: it said the hold is bounded by the
    deploy script's own lifetime because `flock` releases on exit. That is
    true of the script but not of the lock, and the evidence arrived minutes
    later — run `33895768916`'s `vps` job exited `deploy failed: another deploy
    is already running` at 16:40:20 against a lock no GitHub run held, from the
    out-of-band deploy `1861e808` identifies. `_MAX_DEPLOY_AGE_SEC` is the
    bound that actually holds, which is why it is stated that way above.
  - Deployed and verified. The out-of-band deploy landed `2594e6b`, which
    carries `92058e7`; `diagnose` run `33896781856` read the host at 16:44:21
    UTC: `deployed 2594e6b`, `real-money armed`, mainnet engine heartbeat 5 s
    and worker 1 s, demo engine 0 s and worker 2 s, every timer `active`,
    `systemctl --failed` empty, 32 GB free. The fix is visible in its own
    receipt: `mainnet-liveness` printed
    `ok scope=mainnet sanctioned-deploy-in-progress` at 16:41:03 and 16:43:21
    and demo the same at 16:42:51 — a line the realm scopes could not print
    before this change — then `ok scope=mainnet units-and-heartbeats-healthy`
    at 16:44:21. `worker-status:` is resolved and the host scope's
    `CRITICAL watchdog:demo` of 16:38:03 has cleared with the demo timers
    `active` again.
  - `test_a_realm_scope_holds_the_fleet_through_a_deploy_handoff` fails on the
    parent commit — the mainnet scope reads the manifest mid-handoff and prints
    a `CRITICAL manifest` — and passes here. Local: Ruff, mypy and 1422 Python
    tests green; the 16 failures in `tests/market_tape` and
    `tests/scripts/test_observability_hygiene.py` need `zstd`, `rclone` or
    `rsync`, which this container has not got, and fail identically on the
    parent commit.
  - Host-side, by hand: nothing. The funded engine and worker ran on `1193043`
    throughout, untouched. `scripts/ops.sh curve mainnet 40` and
    `scripts/ops.sh status` confirm the account and the heartbeats across the
    window.

- **2026-09-04 16:22 UTC — The demo realm was left stopped by a deploy: the
  state takeover refused the appended `probe` id the engine itself accepts.**
  - Run `33894054427`'s `vps` job, deploying `fc22e3b`, printed
    `engine: config strategy order ["carry", "long", "exodus", "probe"] does
    not match WAL Names ["carry", "long", "exodus"]` twice, then
    `deploy failed: cannot import exact LONG state for demo` and
    `deploy failed: demo strategy-state takeover failed`, and exited 1 at
    16:22:06. `import_native_strategy_state` runs after `stop_realm_units
    demo` and `fail` is `exit 1`, so `liquidity-migration-engine.service` and
    `liquidity-migration-signal-worker-demo.service` stayed down from
    16:21:40. Mainnet was never reached: the funded engine and worker kept
    running on `1193043` throughout, and both recorders read
    `unchanged-left-running`.
  - `039b781` appended `probe` as id 3 of the demo config, which is the
    engine's own rule: `Engine::boot`
    (`engine/engine-core/src/engine/boot_recovery.inc.rs:90`) requires only
    that the configured names start with the WAL's prefix. `verify_names`
    (`engine/engine-core/src/takeover.rs:388`) demanded exact equality, so the
    `import-strategy-state` and `verify-native-strategy-state` commands the
    deploy runs while the realm is stopped refused a config the engine would
    have booted.
  - `verify_names` now takes the same append-only rule: a config that extends
    the WAL's name list keeps its takeover; dropping a logged id, reordering,
    or inserting before one still fails, now saying `does not preserve the WAL
    Names prefix`. Existing ids cannot be renumbered, which is what the import
    depends on.
  - `an_appended_strategy_keeps_the_takeover_and_a_dropped_one_does_not` fails
    on the parent commit with the host's exact message and passes here. Local:
    `cargo fmt --check`, `cargo clippy -p engine-core --all-targets` and all
    510 `engine-core` library tests green.
  - Correction to this entry as first written: it said no config was reverted
    and no state was edited by hand, and that the realm would come back with
    the next deploy. That was already untrue when it was written. The demo
    realm had been restored by hand at 16:24 UTC, three minutes earlier: the
    appended `probe` block was stripped from
    `/etc/liquidity-migration/engine.toml` (the 4-sleeve file kept at
    `engine.toml.probe-4sleeve.bak`) and both demo units started. The template
    change was a pure append, so removing the block restores the previously
    rendered config exactly. Demo came back with 3 sleeves, `may_open` true,
    5 positions and a 2.5 s heartbeat, on the `fc22e3be` binary installed
    three minutes before that. Demo downtime was about six minutes, not until
    the next deploy. Stopping was the holding action, not the fix.
  - The next deploy re-renders the 4-sleeve demo config from the template, so
    the hand edit is transient and the probe arrives with it.

- **2026-09-04 16:02 UTC — Incident `mainnet-014ec4a90a2fde5f`: the mainnet
  half of the same cold-start page. Fixed by `fc22e3b`; the alert now says how
  short the fill is.**
  - The page, mainnet scope on `ip-208-84-103-4`, one `CRITICAL` ref
    `worker-status:liquidity-migration-signal-worker-mainnet.service`:
    "`liquidity-migration-signal-worker-mainnet.service reports 'degraded':
    Bybit WebSocket repair gap open for 73s; ticker coverage incomplete; carry
    cycle has not completed`". Same fault, same 16:00:56 restart, same
    diagnosis as the `demo-0922e9f30da3bf98` entry below, reached
    independently: `startup_runtime_status` could not grant `starting` while
    the stream's ticker coverage was still filling, so every boot published
    `degraded`. `fc22e3b` was already on `main` with the repair when this
    session went to push; nothing in that fix is duplicated here.
  - Same incident id, second fire. The ref id is per fault, and it rearms on
    resolution: the 15:57:34 fire on the 14:42:22 process (4512 s gap) is the
    entry below, repaired by `862a452`; this is the 16:02:13 fire on the
    16:00:56 process that replaced it. Two distinct faults, one id.
  - No trading fault. Only the watchdog reads this heartbeat — `grep -rl
    liquidity_migration_signal_worker_heartbeat` finds `live.rs`,
    `check_fleet_liveness.py` and its test — so a `degraded` verdict pages and
    changes no order. The payload's own detail shows the worker working: no
    disconnect, no quarantined topics, and only the carry cycle named, so the
    LONG cycle had completed inside those 73 seconds.
  - What changed here. `_signal_worker_detail`
    (`scripts/runtime/check_fleet_liveness.py:232`) printed a bare "ticker
    coverage incomplete", which could not tell a fill short by six symbols from
    an empty one — the reason this session could not close the diagnosis from
    the payload alone. It now prints the counts the heartbeat already carries:
    "ticker coverage incomplete (511/517 rows, 517/517 topics accepted)".
    `test_incomplete_ticker_coverage_says_how_short_the_fill_is` pins that and
    the absent-field fallback; it fails on the old bare string.
  - Two bounds the owner may want tighter, both `fc22e3b`'s deliberate choices,
    left alone rather than re-cut by a second writer minutes later:
    `stream_startup_inputs_healthy` requires every topic accepted and the
    stream connected, so the dial-and-subscribe window — ~11 chunks of 100
    topics, each with a 10 s ack timeout — still reads `degraded` from the
    first heartbeat, 5 s after boot; and an unfinished fill now pages at the
    120-minute `STARTUP_MAX_MS` bound rather than the ~2-3 minutes the fill
    itself takes. A bound of the fill's own size (dial, acks, one 30 s
    `mark_max_age_ms` window) would close both.
  - Also unchanged and pre-existing: `reconfigure_stream` (`live.rs`) respawns
    the stream when the hourly instrument refresh changes the symbol set, and
    that mid-life fill reads `degraded` under both the old rule and the new,
    because the startup budget runs from process start. Not seen in this
    incident's journal.
  - Host-side actions: none required. Verified with `scripts/dev.sh check`
    green on this commit — doctor `overall: ready`, Ruff and mypy clean, 1447
    pytest passed (`zstd` and `rsync` installed locally so the 16 tests
    `fc22e3b` had to skip ran), rustfmt and clippy clean, every cargo suite
    passed.
  - Receipt. `b9fbcd7` reached the host inside `2594e6b`, read as
    `deployed 2594e6b` at 16:44:21 UTC by diagnose run `33896781856`, and the
    mainnet realm printed `ok scope=mainnet units-and-heartbeats-healthy` in
    the same read: incident `mainnet-014ec4a90a2fde5f` is resolved on the host.
    The sanctioned deploy this session dispatched, run `33896925320`, then held
    the host 16:51:52-16:52:05 UTC and printed
    `deploy-ok commit=1861e808`, `rollback-target 2594e6b`, `real-money armed`,
    `mainnet-ok result=unchanged-left-running` — the funded engine's
    fingerprints did not move, so it was left running — with both signal
    workers at 4 s and 0 s heartbeats, the demo engine at 1 s and the funded
    engine at `-1s` — the file's mtime a second ahead of the reader's
    clock, which is a fresh heartbeat, not a stale one — the Bybit
    recorder 11 s, the Binance recorder 1 s, every timer active including the
    two demo timers the out-of-band handover had left disabled, and 31 GB free.

- **2026-09-04 15:57 UTC — Incident `mainnet-014ec4a90a2fde5f`: the funded
  worker's repair gap could not close after the first pass, because an
  epoch-less restart threw the live epoch away.**
  - The page, mainnet scope on `ip-208-84-103-4`, one `CRITICAL` ref
    `worker-status:liquidity-migration-signal-worker-mainnet.service`: "reports
    'degraded': Bybit WebSocket repair gap open for 4512s; carry cycle has not
    completed", sampled 15:57:34 UTC. The journal's only stream lines are
    `gap opened in epoch 1` with `Bybit public keep-alive was unanswered` at
    15:57:33 and `entered epoch 2` at 15:57:34, and 4512 s is exactly the age
    of the 14:42:22 process, so the gap dated from process start, not from the
    reconnect. The funded engine kept its heartbeat throughout and `real-money`
    stayed armed. This is the mainnet twin of `demo-0922e9f30da3bf98` below:
    same pre-fix binary, same tick, same `1193043` handover at 16:00:56 that
    replaced it, and the cold-start half of the fix is that entry's.
  - What that entry leaves open is the fix in `66088da` itself, which closes
    the gap only from an epoch some caller supplied.
    `start_kline_repair` overwrote `lanes.repair_epoch` with the caller's
    `None` (`live.rs:1490` on the parent commit) and `RepairFinished` took it
    (`live.rs:1176`), so the two callers that restart the lane without an
    epoch — the carry catch-up (`live.rs:2102`) and the instrument lane
    (`live.rs:961`) — discarded the epoch the stream had reported.
    `advance_kline_watermark` returns early while a repair runs, so with the
    carry scorer catching up, every restart follows the previous finish and
    nothing re-supplies it: `stream.mark_gap_repaired` is never reached and the
    gap stays open for the life of the process however complete the coverage
    becomes. That is the same permanently-degraded verdict this incident paged
    on, reached a second way.
  - The epoch is now stream state: adopted whenever the stream reports one,
    retained across an epoch-less restart, and read rather than taken at the
    finish, with the lane spawned from the retained value.
    `mark_gap_repaired` already refuses an epoch that is not the live connected
    one, so a retained epoch cannot close a gap belonging to a newer epoch.
  - `a_repair_restarted_without_an_epoch_keeps_the_live_one` fails on the
    parent commit twice over: `None` where the finished repair should have left
    `Some(4)`, and `None` again after the epoch-less restart. Local: `cargo fmt
    --check`, `cargo clippy --workspace --all-targets --locked -D warnings`,
    and every engine test green except `market-tape`'s
    `test_segment_writer_writes_and_compresses`, which fails identically on the
    parent commit because this container has no `zstd`; Ruff and
    `tests/scripts/test_scripts_check_fleet_liveness.py` green; ShellCheck and
    mypy are not installable here.
  - Owner action: none by hand. Read the result with `scripts/ops.sh status`,
    the worker with `scripts/ops.sh logs signal-worker-mainnet 200`, and the
    account through the incident with `scripts/ops.sh curve mainnet`.
  - Receipt: `862a452` reached the host inside `2594e6b`, read as `deployed
    2594e6b` at 16:44:21 UTC by `diagnose` run `33896781856` with both workers
    restarted (mainnet heartbeat 1 s, demo 2 s) and `ok scope=mainnet
    units-and-heartbeats-healthy`. That handover was an out-of-band run of the
    deploy script, not a workflow run: the sanctioned deploy of this commit,
    run `33895768916`, exited "deploy failed: another deploy is already
    running" at 16:40:20 against its lock. The 16:44 entry above holds the
    fleet reading.
  - Not fixed, proposed: a warm worker reports `degraded` for as long as any gap
    is open, so an ordinary venue reconnect — Bybit reset this process at 01:08
    and again at 15:57 — pages `CRITICAL` whenever a 3-minute watchdog tick
    lands before the repair closes it. Debouncing the verdict, or reading a gap
    younger than one repair pass as healthy, is a threshold for the owner.

- **2026-09-04 16:05 UTC — Incident `demo-0922e9f30da3bf98`: a cold start is
  not a degraded worker. The startup grace no longer waits on ticker coverage
  the stream has not delivered yet.**
  - The page, `scope=demo` on `ip-208-84-103-4`, one `CRITICAL` ref
    `worker-status:liquidity-migration-signal-worker-demo.service`:
    "`liquidity-migration-signal-worker-demo.service reports 'degraded': Bybit
    WebSocket repair gap open for 4530s; carry cycle has not completed`". It
    was true. The demo worker (PID 2139543) had run since 14:41:52 on the
    binary the 2f4af5e handover built, its repair gap open since 14:42:05 —
    process start — because that binary predates the epoch adoption in
    `66088da`. The `dc69448` and `bf30fd6` deploys both left the realm
    `unchanged-left-running`, so nothing replaced it. Run `33891965516` did:
    `deploy-ok commit=1193043` at 16:01:04, demo worker restarted 16:00:26,
    mainnet 16:00:56, `real-money armed`. That closes the pre-fix process.
  - The same deploy carried the new semantic worker check to the host at
    ~15:57:2x, before the realm handover, and the demo liveness timer's
    15:57:34 tick graded the old process with it. The page is that tick.
  - The restarted workers then paged on their own boot state:
    `CRITICAL worker-status:…-mainnet.service … 'degraded': Bybit WebSocket
    repair gap open for 73s; ticker coverage incomplete; carry cycle has not
    completed` at 16:02:13, the demo equivalent at 274 s at 16:05:13, each
    firing another on-call session, with `ok scope=demo` at 16:02:11 and `ok
    scope=mainnet` at 16:05:12 between them. Not a deploy artefact: the deploy
    lock was released at 16:01:05, and the realm scopes never consult it.
  - `startup_runtime_status` (`engine/signal-worker/src/live.rs`) held a worker
    at `starting` for the 120-minute cold-start budget only while
    `stream_inputs_healthy` was true, and that predicate requires
    `ticker_coverage_complete`. Coverage fills symbol by symbol from the
    stream and needs a fresh mark for all ~517 symbols, so a booting worker
    never has it: the grace could not apply at the one moment it exists for,
    and `runtime_status`'s live `degraded` reached the watchdog inside one
    3-minute interval of every restart.
  - The startup gate is now `stream_startup_inputs_healthy`: connected, every
    ticker and kline topic accepted, none quarantined, frames arriving.
    `stream_inputs_healthy` is that plus complete coverage and still decides
    `ready`. `heartbeat_status` composes the two so the heartbeat's status has
    one definition. A disconnected stream or a refused topic is `degraded`
    from the first heartbeat, past the 120-minute bound an unfinished backfill
    is a fault, and once both cycles have run incomplete coverage is the live
    verdict again.
  - Cost: a worker whose ticker coverage never completes pages at the
    120-minute bound instead of within 3 minutes of boot. The open repair gap
    and the incomplete coverage stay in the heartbeat throughout.
  - `a_cold_start_still_filling_ticker_coverage_is_starting_not_degraded`
    fails on the parent commit — `degraded` where `starting` is required — and
    passes here. Local: `cargo fmt --check`, workspace
    `cargo clippy --all-targets` and the 114 `signal-worker` library tests
    green; Ruff, mypy and 1421 Python tests green. 16 Python tests cannot run
    in this container — the `market_tape` load and fixture-hour tests need the
    `zstd` binary and the backup tests need `rclone`/`rsync`; none touch the
    signal worker or the liveness check, and the deploy gate runs them.
  - Not changed, and the owner's call: the `demo` and `mainnet` liveness
    scopes still evaluate through a sanctioned deploy, so a handover window
    can page on a process the deploy is about to replace. Suppressing a funded
    realm's worker verdict during a deploy is a risk trade, not a repair.
  - Also in the payload, not a fault: `instrument lane: 691 instrument row(s)
    left out of the table (BTC-01DEC23: input: invalid symbol …)` hourly.
    Bybit's linear list carries dated futures beside the perpetuals and
    `normalize_instruments_reporting` leaves them out by design rather than
    refusing the snapshot.
  - Host-side readings the owner can take: `scripts/ops.sh status` for the
    deployed commit and heartbeat ages, `scripts/ops.sh curve mainnet` for the
    account's equity through the window, and
    `jq '{status, bybit_ws_gap_open, bybit_ws_ticker_coverage_complete,
    last_carry_cycle_completed_wall_ts_ms}'
    /var/lib/liquidity-migration-signal-worker-mainnet/heartbeat.json` for the
    verdict this entry is about. No hand action is required.

- **2026-09-04 — The order path is measured every quarter hour, every sleeve
  is a series, and the Bybit recorder stops starving itself.**
  - What the first dashboard showed against what was true. "Open positions by
    sleeve" had no Exodus line because the sampler only emitted sleeves that
    held something; "dropped frames" read as a flat 326 because it charted a
    since-boot counter; the order-path panels were empty because the engine's
    latency ledger is a 60-second window and the funded engine sent two orders
    in fourteen hours. Both engines' `end_to_end_*` were null in every sample
    since the 00:08 deploy.
  - The recorder was the real fault. In the 24 h to 14:45 UTC the Bybit
    recorder logged 348 `overran the capture queue`, 160 `ping/pong timed out`
    and 501 shard reconnects across 21 shards, all since the 22:11 restart that
    raised `monthly_gb` from 1300 to 2400 and stopped shedding; Binance logged
    one. The queue hit 31,606 of 32,768 frames at 13:35 and again at 14:00,
    and every ping timeout came in a burst across every shard at once, which
    is the interpreter, not the network. `py-spy --gil` on the live process
    (12 s, 436 samples) put ~73% of interpreter time in websocket-client's
    receive path and ~10% in normalize and write. A local benchmark against
    Bybit's public stream (40 names, book/trades/ticker, 25 s each) measured
    0.22 ms CPU per frame with the library's defaults, 0.10 ms with
    `skip_utf8_validation=True`, and 0.13 ms on the `websockets` sync client
    with its C speedups; the flag wins and adds no dependency. Fix: the flag,
    and `queue_frames = 131072` in `deploy/capture/bybit-linear.toml` so a
    US-session burst buffers instead of overrunning, reconnecting and
    re-snapshotting every book on the shard. `json.loads` rejects a malformed
    frame regardless.
  - The order path is now measured on a clock. New `probe` plug in
    `engine-strategies`: on the **demo** engine only, every 15 minutes on the
    wall clock it rests one venue-minimum post-only `BTCUSDT` buy 3% under the
    bid with the stop the risk kernel requires, and pulls it two seconds
    later. It is `[[strategy]] probe`, id 3 in the demo config, appended after
    Exodus; mainnet's config is untouched. A fill is closed at market at once
    and shows only as an entry blocker, never a strategy error, and
    `notify_book_changes.py` hides the sleeve: the probe cannot page or
    message. Twelve plug tests pin the schedule, the price, the size, the pull,
    the refusal path, the drain and its retry, and the strict parameter table.
  - The probe stands down for the sleeves. A Bybit entry carries `stopLoss`
    with `tpslMode: Full`, so the stop it names belongs to the whole position
    on that symbol (`engine-venue/src/venues/bybit/gateway.rs`, `native_position_stop`).
    BTCUSDT is inside LONG's top-10-volume universe, so a probe entry there
    while LONG held the name could put LONG's position behind the probe's
    deliberately far stop instead of its own ATR stop. The probe now skips a
    symbol with a foreign position and reports it as a blocker, the same rule
    the quoter follows; a paused measurement is worth more than a moved stop.
    The test fails without the guard.
  - The sampler now emits every sleeve the heartbeat lists, zero included, and
    per sleeve its entry gate and blocker count; the whole latency ledger
    (`decide`, `durable`, `wire`, `ack`, `dispatch_queue`, `venue_task`,
    `core_resume`, `end_to_end` at p50/p99, `barrier_wait` and `quota_hold`
    p99), working orders, pending flattens, amend outcomes and fill costs; and
    for each signal worker its bounded verdict, raw transport and topic facts,
    reducer-cycle ages, WebSocket queue, and durable spool; and for each
    recorder the queue capacity and fill, shards configured and connected,
    reconnects since boot and bytes in 24 h. A null field is absent from the
    push, never zero. About 220 series, 10 MB a day on the host.
  - The dashboard is rendered by `deploy/grafana/render_dashboard.py` and the
    committed JSON must match it. Health is a state timeline with one lane per
    fact; sleeves are three panels through `label_replace`; the order path has
    a "last measured" stat (`time() - timestamp(last_over_time(...[30d]))`),
    end-to-end and per-step p99 plotted as points; signal-worker panels put the
    verdict beside transport, coverage, repair, cycle, queue, and spool facts;
    every since-boot counter is charted as `increase(...[5m])`; recorder panels
    add reconnects, queue fill and shards connected. A test refuses any
    expression naming a field the sampler does not push.
- **2026-09-04 — Observability follows producer verdicts, and watchdog
  maintenance follows the deploy lock.**
  - The first `2f4af5e5` rollout returned `ok` in all three liveness scopes,
    while both fresh signal-worker heartbeats said `status=degraded`, their
    Bybit repair gaps had remained open since process start, and no CARRY
    cycle had completed. A fresh file proved only that the process could write;
    liveness now requires the signal worker's semantic verdict and fails closed
    when a known worker or engine omits its producer-specific health fields.
  - The startup state had two separate defects. `LiveRunner::run` started the
    REST repair before the WebSocket established its epoch. `EpochStarted`
    found the lane busy and discarded the epoch, so the first successful repair
    could not close the WebSocket gap and the worker fetched the whole overlap a
    second time. An in-flight repair now adopts the newest live epoch. Healthy,
    transport remains `starting` for the existing 120-minute cold-backfill
    budget even while ticker coverage fills. After cold fill, a gap, repair, or
    incomplete ticker snapshot reports `recovering` for at most 120 seconds,
    only while the socket is connected and fresh, every configured topic is
    accepted, and none is quarantined. Full recovery resets that clock.
    Disconnected, stale, mismatched, or quarantined input degrades immediately;
    a persistent recovery or a backfill beyond its longer bound is a fault.
  - Live acceptance found the missing transition edge at 16:02, 16:05, and
    16:11 UTC. Watchdog samples caught transient incomplete-coverage heartbeats,
    read `status=degraded`, and fired incidents although the next heartbeat had
    full coverage and no transport or quarantine fault. Those samples do not
    distinguish first fill from expiry followed by REST replacement. Producer
    health now gives the cold fill its long bound and a later transport-healthy
    repair its short bound; the raw gap, repair, and incomplete-coverage facts
    remain visible throughout.
  - Incident `host-bf5dcb6544d0dfdc` proved the independent host watchdog can
    sample a realm while a sanctioned deploy has disabled its timer. Systemd
    enablement alone removed that false page but also hid a timer disabled by
    mistake while the funded engine kept running. The host scope now uses the
    deploy's existing exclusive lock as the maintenance fact. While it is held,
    the host scope suppresses transition-prone unit, heartbeat, recorder, and
    realm-watchdog checks without resolving their delivery state; disk, clock,
    upload, backup, and external dead-man checks continue. A lock beyond 30
    minutes pages; the bound covers the measured 12–19 minute host-build
    fallback. Outside it, demo is mandatory and mainnet is mandatory whenever
    enabled or trading.
  - Incident `host-84246120f8ea8c9f` exposed a separate lifecycle fault after
    the real recorder-stall repair: the Binance replacement published a
    zero-frame status during normal WebSocket warm-up. Recorder readiness no
    longer accepts file
    freshness alone: `status.json` names its process, and a deploy remains in
    maintenance until that PID is the unit's current `MainPID`, a shard is
    connected, and the replacement has received a market frame.
  - The read-only `diagnose` workflow has its own concurrency group, so an
    incident read no longer waits behind a completed host handover's release
    soak. Focused Python and signal-worker tests pass; the full repository gate,
    push, rollout, and live receipts follow below before this entry is closed.

- **2026-09-04 15:32 UTC — Incident `host-84246120f8ea8c9f`: the host watchdog
  read a two-second-old recorder as a dead venue. Silence and socket loss are
  now measured from the recorder's own start.**
  - The alert, on `ip-208-84-103-4`, host scope, two `CRITICAL` refs on the
    Binance recorder: `capture-shards:forward-market-binance` — "recorder
    forward-market-binance has no live venue connection" — and
    `capture-silent:forward-market-binance` — "recorder forward-market-binance
    has received no market frame yet" — beside a `WARNING capture-shards`
    "recorder has 1 of 16 venue connections down" on the Bybit recorder. No
    trading fault: neither engine, signal worker, nor
    `liquidity-migration-engine-mainnet.service` appears in the payload.
  - No fault existed. Run `33889491439`'s `vps` job held the host 15:31:36 to
    15:32:34 UTC and finished `success`; it carried `bf30fd6`, which changes
    `market_tape/`, so the deploy restarted the recorders. The journal is the
    whole incident: `Stopped` at 15:32:31, `Started` at 15:32:31, the first
    `capture status frames=0 rows=0` at 15:32:35,374, then nine `Websocket
    connected` lines from 15:32:35,648 to 15:32:35,709 — every shard live
    inside 400 ms. The watchdog's 3-minute run landed in that 274 ms window
    and read the newborn status file.
  - Diagnosis. `Recorder.run` (`market_tape/record.py:649` on the parent
    commit) starts the maintenance thread right after `_reconcile_shards()`,
    so `_maintenance` → `_write_status` (`record.py:1159`, parent) publishes
    `last_receive_ns = 0` and `shards[].connected = False` for every shard
    before the sockets have finished their handshake. That file is accurate.
    What was wrong is how it was read: in the watchdog running on the host at
    the time (`bf30fd6`), `evaluate_capture_status`
    (`scripts/runtime/check_fleet_liveness.py:260`) paged `CRITICAL` on
    `last_receive_ns <= 0` with no reference to how long the recorder had been
    up, and `check_fleet_liveness.py:297` paged `CRITICAL` when every shard was
    `connected: False`. Both read "has not started yet" as "has stopped" — the
    same false page as `host-bf5dcb6544d0dfdc`, in a different check. The
    deploy lock that `66088da` made the maintenance boundary does not cover
    this: it short-circuits `evaluate_watchdog_chain` alone, and the recorder
    checks run whether or not a deploy holds the lock.
  - What changed. `status.json` gains `started_at_ns`, the moment the process
    began recording (`record.py:627`, in the payload at `record.py:1172`).
    `evaluate_capture_status` computes the recorder's uptime from it
    (`check_fleet_liveness.py:361`) and holds both readings — silence and
    shard connectivity, the `WARNING` half included — until the recorder has been up longer than
    `--max-capture-silence-sec` (120 s). Past that, a recorder with no frame
    pages with the time it has been up ("no market frame in the 300s since it
    started"), and a recorder with every socket down still pages `CRITICAL`.
    Nothing else is graced: blocked storage, new drops, and the byte budget
    page as soon as the file says so. A status file without `started_at_ns`
    predates the field and keeps the old reading, so the check cannot be
    quieted by a missing key.
  - Proof. `test_a_recorder_seconds_old_is_not_a_dead_venue`
    (`tests/scripts/test_scripts_check_fleet_liveness.py`) replays this
    payload — 0.02 s of uptime, no frames, both shards down — and asserts no
    alert, then asserts the same file at 300 s of uptime pages both refs, that
    `disk_blocked` still pages inside the window, and that a payload with the
    key deleted keeps the old message. `started_at_ns` is asserted in
    `test_the_status_file_carries_what_the_host_watchdog_reads`
    (`tests/market_tape/test_record.py`). Both fail on the parent commit
    (`KeyError: 'started_at_ns'`, and the newborn payload raising two
    `CRITICAL`s) and pass here. Locally: Ruff, mypy, and the whole
    `tests/scripts` and `tests/market_tape` suites green, 1,436 of 1,438
    Python tests overall. The two that did not run are
    `test_backup_snapshots_locally_then_mirrors_to_the_drive_with_history` and
    `test_backup_refuses_a_credential_file_and_a_non_rclone_destination`:
    `rsync` is not installable in this on-call container
    (`rsync_3.2.7-1ubuntu1.2_amd64.deb` 404s on the mirror), so
    `backup_state.sh` exits 2 before either assertion. Neither touches the
    recorder or the watchdog; the repository gate on the push covers them.
  - Detection cost, and the owner's call. A venue that is dead when the
    recorder starts now pages 120 s later instead of at once. That is the same
    latency the check already accepted for a venue that dies while the
    recorder runs, and 120 s of tape is what a restart costs anyway.
  - Not established from the payload: whether the Bybit recorder's 1-of-16
    `WARNING` was the same startup artifact or a real reconnect. Its journal
    was not in the payload — `_incident_units`
    (`check_fleet_liveness.py:715`) attaches a unit's journal only for
    `CRITICAL` refs. The host reading that settles it:
    `sudo journalctl -u liquidity-migration-forward-capture.service --since
    '2026-09-04 15:30' | grep -E 'Websocket|shard|Started'`. Either way it is
    a warning, it did not fire this routine, and the next run's `RESOLVED
    capture-shards` line will say it cleared.
  - No host action is required beyond the deploy. Both recorders were up and
    connected within four seconds of the restart, the alerts self-resolved on
    the next 3-minute run, and no positions were touched.
  - `scripts/ops.sh curve mainnet` is the owner's reading for the funded
    account through 15:26-15:36 UTC: the equity sampler is `independent` and
    ran through the deploy, so a minute written `state=absent` there would say
    an engine actually lost its heartbeat during the restart window, which no
    alert claimed.

- **2026-09-04 14:49 UTC — Incident `host-08ad9d5834fa6d2f`: the recorder's
  heartbeat sat behind a full walk of the tape. Retention now has its own
  thread and its own cadence.**
  - The alert, on `ip-208-84-103-4`, host scope, two `CRITICAL` refs at once:
    `capture-silent` — "recorder has received no market frame for 126s (limit
    120s)" — and
    `heartbeat:liquidity-migration-forward-capture.service` — "heartbeat is
    126s old (limit 120s)". No funded engine unit alerted;
    `liquidity-migration-engine-mainnet.service` was not involved.
  - Both readings come from one file. `evaluate_heartbeats`
    (`scripts/runtime/check_fleet_liveness.py:154`) stats the unit's
    `output_artifact`, which for this unit is
    `/var/lib/liquidity-migration/forward-market/status.json`
    (`deploy/fleet_manifest.tsv:11`), and `evaluate_capture_status`
    (`check_fleet_liveness.py:229`) reads `last_receive_ns` out of the same
    file. Identical ages of 126 s mean the file was 126 s old, not that the
    venue went quiet — and the journal agrees: shards reconnect and subscribe
    through 14:49:07 ("shard 1 connected with 150 topics"), while not one
    `capture status frames=…` line appears in the 95 s the excerpt covers,
    where `status_interval_seconds = 30` should have produced three.
  - Diagnosis: `Recorder._maintenance` (`market_tape/record.py:1065` on the
    parent commit) opened every tick with
    `self.disk_blocked = not self.retention.writable()` and closed it with
    `self._write_status()`. `Retention.writable()`
    (`market_tape/storage.py:363`, parent commit) ran a full
    `prune()` first. `prune()` walked the whole tape with `rglob("*.zst")`
    and spent three `stat()` calls per file — the sort key, the size sum, and
    the `expired` test — plus a `shutil.disk_usage` **per file** at
    `storage.py:343` while the tape was under `max_disk_gb`, and then a
    second full walk of the tree — `os.walk` with an `rmdir` attempt on every
    directory — in `remove_empty_directories`, unconditionally, deletions or
    not. On this host that covers 517 USDT perpetuals ×
    24 hourly directories × the ~3 days that `max_disk_gb = 60` holds: tens of
    thousands of compressed segments, three to four syscalls each, every 30
    seconds, growing with the tape. When one pass ran past 120 s the heartbeat
    aged past the watchdog's limit and both refs fired together.
  - What changed. `Retention.writable()` is now the question its name asks:
    one `statvfs`, no walk, no deletions. `Retention.prune()` stats each file
    once, reads free space once, and carries free space forward by the bytes
    it unlinks — which is also truer than re-reading `statvfs`, since a
    filesystem need not release a deleted file's blocks at once — and only
    walks for empty directories when it deleted something. `Recorder` runs
    retention on a new `tape-retention` thread every
    `RETENTION_INTERVAL_SECONDS = 300`, catching and logging a failed pass the
    way the writer and compressor threads already do, so neither the cost nor
    the failure of housekeeping can hold or kill the thread that writes the
    heartbeat. The tape gains a few hundred MB in 300 s against a 25 GB
    `min_free_disk_gb` floor, so the longer cadence cannot run the disk out.
  - Proof. Five tests, each failing on the parent commit and passing here:
    `test_writable_asks_the_free_space_question_and_walks_nothing`,
    `test_a_prune_stats_each_file_once_and_reads_free_space_once` (1
    `disk_usage` call per pass, not one per file),
    `test_disk_pressure_stops_once_the_unlinked_bytes_clear_the_free_floor`
    (the carried-forward free space stops the pass instead of emptying the
    tape), `test_the_maintenance_tick_writes_the_heartbeat_without_walking_the_tape`,
    and `test_a_retention_pass_that_cannot_delete_leaves_the_pruner_thread_running`.
    Then the full `scripts/dev.sh check`: Ruff, ShellCheck, mypy over 98
    files, 1,428 Python tests, rustfmt, Clippy, and the whole Rust workspace.
  - Not established from the payload, and worth the owner's eye: whether the
    maintenance thread was merely slow or had already died on an exception
    out of `prune()` — the loop had no error containment, so an unlinkable
    file would have killed it silently. Both readings are on the host:
    `sudo journalctl -u liquidity-migration-forward-capture.service --since
    '2026-09-04 14:00' | grep -E 'tape-maintenance|Traceback|capture status'`
    tells them apart, and `scripts/ops.sh curve mainnet` shows which minutes
    of the incident recorded no recorder sample at all. Either way this change
    fixes it: the pass is cheap, off the heartbeat's thread, and cannot raise
    out of its loop.
  - The tape did lose frames. Eight shards logged "overran the capture queue;
    reconnecting for fresh snapshots" at 14:47:43, so the writer thread fell
    the full `queue_frames = 32768` behind and those frames are gone from the
    14:00 hour. The writer is a different thread from the maintainer, so this
    is not the same code path; it is consistent with the same pass — a
    syscall storm over tens of thousands of files on the filesystem the writer
    is appending to — and a mass reconnect re-subscribes 150 topics a shard,
    whose snapshot burst can overrun the queue again on its own. Which of the
    two drove it is not decidable from the payload. `dropped_frames` in
    `status.json` is the counter to watch after the deploy: it should stop
    climbing.
  - No host action is required beyond the deploy. The recorder was not
    restarted by hand, and the shards were connected throughout.
  - Why it fired here, from the entry below and `dd25715`. The `2f4af5e`
    deploy held the host 14:41:13-14:42:29 UTC and restarted both recorders —
    the same restart the entry below records as `RESOLVED capture-silent`. So
    this recorder was minutes old, walking a cold page cache. A 126 s age read
    at about 14:49 puts the last status write near 14:46:5x, which makes the
    first pass after restart roughly four minutes long; the 14:47:43 overrun
    falls inside the second. The disk trend `dd25715` recorded — 64 GB free at
    00:10 UTC, 36 GB at 15:07 — puts the tape at or near `max_disk_gb = 60`,
    which is the pass's most expensive mode: it deletes on every tick, and a
    deleting pass leaves the most directories for the second walk to try to
    `rmdir`. That second walk now runs only when a pass deleted something,
    once per 300 s rather than per 30 s.
  - Deploy receipt. `bf30fd6` deployed by run `33889491439`, `Run VPS mode`
    15:31:36-15:32:34 UTC: `deploy-ok commit=bf30fd67…`, rollback target
    `dc69448`, `real-money armed`. Neither realm's fingerprint moved, so
    `demo-ok` and `mainnet-ok result=unchanged-left-running`; the recorders
    are what this change touches and both took it —
    `capture-ok unit=liquidity-migration-forward-capture.service
    result=restarted`, same for the Binance unit. In the same receipt every
    unit is `active`: engines 2 s and 3 s, signal workers 1 s and 4 s, the
    Bybit recorder 12 s, the Binance recorder 3 s. The Bybit recorder's
    12 s reading is the fix's first evidence — it wrote `status.json` within
    seconds of a restart, where before this change the first maintenance tick
    carried a whole cold-cache prune. Disk `118G 78G 35G 70% /`, 35 GB free
    against the 25 GB floor and 36 GB at 15:07.
  - Still open, not this fix's to close: the disk is falling about 2 GB an
    hour and the watchdog's floor is 25 GB. Retention holds the tape under
    `max_disk_gb`; nothing here holds the *host* under its floor, and a
    `capture-disk` CRITICAL is what the fleet gets if it crosses.

- **2026-09-04 — Incident `host-bf5dcb6544d0dfdc`: the new watchdog-chain check
  paged CRITICAL on its own deploy. Requirement now reads systemd enablement,
  not a realm's runtime state.**
  - Alert, host scope, `ip-208-84-103-4`, inside 14:41:13-14:42:29 UTC:
    `CRITICAL demo watchdog timer is inactive (disabled)`, ref `watchdog:demo`,
    alongside `RESOLVED capture-silent` and
    `RESOLVED heartbeat:liquidity-migration-forward-capture.service`. The fire
    launched an on-call session. No trading fault: the funded engine was never
    named, `liquidity-migration-demo-liveness.service` logged
    `ok scope=demo units-and-heartbeats-healthy` on every three-minute run
    through 14:40:22 UTC, and the two `RESOLVED` lines are the recorders coming
    back after the same deploy restarted them.
  - Diagnosis. `2f4af5e` was committed 14:25:35 UTC; run `33884568238`'s `vps`
    job held the host from 14:41:13 to 14:42:29 UTC and finished `success`.
    It changed `deploy/systemd` and the fleet manifest, so both realm
    fingerprints changed and the deploy ran `stop_realm_units` →
    `start_realm` on each realm ([docs/operations.md](docs/operations.md)
    §Deployment Flow, step 4). `stop_realm_units`
    (`scripts/deploy_vps_live.sh:409`) runs `systemctl disable --now` on every
    realm unit, so mid-deploy the demo liveness timer reads
    `inactive` + `disabled`. The independent host watchdog samples every three
    minutes and landed inside that window. In
    `scripts/runtime/check_fleet_liveness.py:374`,
    `expected = realm == "demo" or enabled.startswith("enabled")` made the demo
    watchdog timer unconditionally required, so the deploy's own teardown read
    as a fault. Mainnet stayed silent only because its branch was gated on
    enablement and on `liquidity-migration-engine-mainnet.service` being
    active. That gate was not correct either: `start_realm` starts the owner
    before it enables the realm's timers, so the same false page was waiting
    for mainnet in the start half of every deploy.
  - Root cause: the check read a realm's transient runtime state as its
    requirement. The manifest's `always` activation scopes a unit to its
    realm's activation set, not to every minute of the host's life.
    `evaluate_watchdog_chain` now requires a realm watchdog timer exactly while
    systemd is enabled to run it, symmetrically for both realms, and no longer
    queries the mainnet engine. `systemctl enable --now` and `disable --now`
    move enablement and activation in one step, so neither half of a deploy
    leaves a window where the check demands a timer the deploy has legitimately
    torn down; an enabled timer that is not running, or whose last run did not
    exit `success`, still pages.
  - Trade-off, stated rather than hidden: a watchdog timer disabled by hand
    while its realm keeps trading is no longer caught by the host scope. The
    previous mainnet clause covered that case at the cost of a false page on
    every deploy. Restoring it race-free needs `start_realm` to enable a
    realm's liveness timer before its owner unit; that is a deploy-ordering
    change and the owner's call, not something this fix assumes.
  - Proof: `tests/scripts/test_scripts_check_fleet_liveness.py` gains
    `test_host_watchdog_chain_ignores_a_realm_a_deploy_has_torn_down` and
    `test_host_watchdog_chain_reads_enablement_not_the_engine`; both fail on
    the previous code (`watchdog:demo` fires on a torn-down realm;
    `watchdog:mainnet` fires off the engine's state) and pass with the fix.
    Focused file: 26 passed. Full `scripts/dev.sh check` gate run before push.
  - Deploy receipt. `dc69448` deployed by run `33887114107` at 15:07:45 UTC.
    Neither realm's fingerprint moved — the fix is a watchdog script, not
    engine input — so both engines kept running:
    `demo-ok result=unchanged-left-running`,
    `mainnet-ok result=unchanged-left-running`, `deploy-ok commit=dc69448…`,
    `real-money armed`, rollback target `2f4af5e`. Both liveness timers
    `active`, both engines `active` with 0 s and 2 s heartbeats. This deploy
    therefore never entered the teardown window that produced the page.
  - Watch the disk. The same receipt reads `/dev/sda2 118G 77G 36G 69% /`,
    against 64 GB free at 00:10 UTC the same day — roughly 28 GB in 15 h. The
    host watchdog's floor is 25 GB free on `/var/lib`; at that rate it is
    hours away. Not diagnosed here, and not this incident's cause.
  - Host-side, by hand, read-only — confirm what the account was worth through
    the incident and which minutes had no heartbeat, and check the disk trend:

    ```sh
    scripts/ops.sh curve mainnet 60
    scripts/ops.sh status
    scripts/ops.sh units
    ```

- **2026-09-04 — On-call is one supervised delivery plane, not three optional
  watchdog side effects.**
  - Live diagnosis found the host scope only loaded the absent
    `/etc/liquidity-migration/host-liveness.env`, while demo and mainnet both
    loaded one `/etc/liquidity-migration/liveness.env`. The shared
    `hc-ping.com` URL let either realm mask the other, and a host disk,
    recorder, upload, backup, or clock `CRITICAL` could reach Telegram but
    could not fire the incident routine. All three timers still reported
    `active`, so unit state alone falsely looked complete.
  - Delivery also consumed Telegram cooldown state before Telegram accepted
    the message, swallowed every dead-man transport error, and launched a new
    Claude Code session for the same unresolved fault every cooldown hour.
    A broken route therefore looked successful, suppressed its own retry, or
    created duplicate engineers.
  - `/etc/liquidity-migration/notifications.env` now owns Telegram transport;
    `/etc/liquidity-migration/oncall.env` owns the routine URL/token and the
    one watchdog-plane dead-man. Deploy atomically projects both from the
    existing private files on first use, validates exact keys, file mode, HTTPS
    endpoints, and the Anthropic routine host/path, then every observer loads
    only the dedicated files. No watchdog, trade notifier, or control bot
    receives a venue key or `REAL_MONEY`.
  - The independent host scope alone pings the external check and now
    supervises demo/mainnet watchdog timer state and last result. Telegram and
    incident-routine delivery have separate state: a failed sink retries on
    the next three-minute run; Telegram repeats after 60 minutes; an accepted
    agent fire rearms only after that critical reference resolves. Transport
    errors log only an exception class or HTTP status, never a URL carrying a
    Telegram bot token.
  - Incident payload schema 2 carries a stable incident id, the newly critical
    references, and relevant recorder/watchdog journals. `vps-deploy.yml`
    adds a fast read-only `diagnose` mode that skips builds, uses the pinned
    production SSH identity, and returns unit/watchdog evidence. The routine
    prompt requires that receipt before diagnosis and after deploy; delivery
    drills are explicit no-op events.
  - The first live rollout completed at 14:43 UTC on commit `2f4af5e5`: all
    three watchdog scopes returned `ok`, the new private route files were
    `root:root 0600`, both engines retained five positions with `may_open=true`,
    and Grafana accepted four fresh samples. Independent inspection then found
    both fresh signal-worker heartbeats self-reporting `degraded`, with their
    Bybit repair gaps still open. The old watchdog parsed engine admission but
    ignored the worker's own verdict, so it printed a false `ok`.
  - Realm liveness now accepts only the worker's bounded `starting` state or
    `ready`; `degraded`, `stopped`, an unknown verdict, malformed heartbeat
    shape, or spool backpressure is `CRITICAL`. The incident includes the
    worker journal and names source, cycle, coverage, quarantine, and gap-age
    evidence. The read-only `diagnose` dispatch now has a per-run concurrency
    group: the first live attempt exposed that the supposedly off-path release
    soak still held the mutating-workflow queue after host handover.
  - Proof before rollout: focused on-call/deploy tests pass, followed by the
    full `scripts/dev.sh check` gate: Ruff, ShellCheck, mypy, 1,427 Python
    tests, Rust formatting, Clippy, and the complete Rust workspace tests.

- **2026-09-04 — The fleet keeps an equity history: one sample a minute to
  disk, a curve readable on the host, and an optional push to Grafana Cloud.
  Every venue adapter that is not traded is declared dormant and pinned by a
  test.**
  - The gap: `heartbeat.json` is rewritten every five seconds and nothing kept
    the old one, so the fleet had no history of its own equity. `trades.jsonl`
    records realized round trips and says nothing between them — a drawdown
    that never closed a trade left no trace at all, and neither did the
    minutes an engine was down.
  - `scripts/runtime/record_equity.py`, run by
    `liquidity-migration-equity-recorder.timer` every minute at :20, reads
    every artifact the fleet manifest declares — both engine heartbeats and
    both tape-recorder status files — and appends one JSON line each to
    `/var/lib/liquidity-migration/equity/<kind>-<realm>-<YYYY-MM>.jsonl`. A
    realm with no readable heartbeat is recorded as `state=absent` rather than
    skipped: the gap is the fact worth keeping. `Persistent=false`, so a
    missed minute stays missed.
  - The unit is `independent` in the manifest: it keeps running through fleet
    restarts and funded stops, which is what lets it record them. It is also
    the only unit in the fleet that loads no venue environment file at all —
    its credential surface is empty by construction rather than by unsetting
    keys it was handed.
  - `scripts/ops.sh curve [REALM] [SAMPLES]` prints the recorded curve on the
    host: range, net change, a sparkline with holes where the heartbeat was
    missing, and the last twenty rows. No remote, no library.
  - With `METRICS_PUSH_URL`/`_USER`/`_TOKEN` set in
    `/etc/liquidity-migration/observability.env`, the same samples are pushed
    as InfluxDB line protocol in one POST — Grafana Cloud's free tier holds
    10k series and this fleet pushes about 70. Every sample carries `up`, so a
    dead engine pushes `up=0` rather than nothing. The push is best-effort:
    the local append happens first and a failed push exits 0 with a `WARNING`.
    `realm` is the only label: a label that changes value starts a new series,
    so labelling `state`, or a `venue` known only while the engine is up,
    would split a realm's history in two at the moment it went down.
    Dashboard: `deploy/grafana/liquidity-migration-fleet.json`. Setup:
    `docs/observability.md`.
  - Grafana Cloud is live on stack `proudtortoise1017`: instance `3560818`,
    Prometheus zone `prod-55-prod-gb-south-1`, and Influx write endpoint
    `https://influx-prod-55-prod-gb-south-1.grafana.net/api/v1/push/influx/write`.
    The first generated credential was read-only; at 12:16:06 UTC the service
    reported exactly `WARNING: metrics push failed: HTTPError: HTTP Error 401: Unauthorized`.
    It was removed from the host. Access policy
    `liquidity-migration-metrics-write` now grants only `metrics:write`; its
    `johor-equity-recorder` token is stored only in the root-owned host env.
    At 12:20:44 UTC the service reported `recorded and pushed 4 samples`, and
    every scheduled run through the 12:32 UTC verification did the same.
    Dashboard `liqmig-fleet` is imported and bound to
    `grafanacloud-proudtortoise1017-prom`. Its five current-value stat panels
    use instant Prometheus queries; range queries made Grafana return empty
    frames for the boolean cards even while the same metrics were present in
    Explore.
  - Dormant venues: six venues are compiled, one is traded. `docs/engine.md`
    §2 now names all ten selectable realms with their readiness and what each
    is, and `engine/engine-venue/tests/dormant_venues.rs` pins which realms are
    dormant, what dormancy means at boot per readiness class, and that every
    dormant gateway, private stream, and realm table is still linked — so
    deleting an adapter fails to compile in that test rather than at an order.
    ~19,600 lines kept deliberately; the price is CI time, the value is that a
    venue decision is a config change.
  - Doc repairs found on the way: CLAUDE.md pointed `engine bench` at a
    latency table `docs/engine.md` does not contain; the crate table omitted
    `engine-marketdata` and called `engine-venue` a two-venue crate;
    `MEXC_MAINNET` was the one venue constant the crate did not re-export. A
    new check in `tests/repo/test_docs_links.py` fails the gate when any doc
    names a repo path that does not exist (CHANGELOG.md exempt: history names
    what a change replaced).
  - Also fixed: `render_curve` crashed formatting a sample with no equity
    number, which is every `absent` row. Caught by its own test before deploy.

- **2026-09-03 — A sleeve sizes against its own fills, never the account's
  whole position: the 2026-08-22 1000PEPE hand-position sell-down, root-caused
  and fixed.**
  - What happened, from the funded WAL (segment 1 holds the imported
    records): on 2026-08-22 between 08:37:12 and 08:40:43 UTC the venue held
    33,180,700 `1000PEPEUSDT` long, of which LONG's own fills were 222,000
    (5 fills, $895.33). The owner had opened the rest by hand. On its next
    pass the LONG sleeve sent two reduce-only market sells tagged
    `book-resize`, `eng-1787357335566-12` for 17,113,700 and `-13` for
    16,067,000 — the venue's entire position down to LONG's own target — and
    they filled in 331 prints over 26 seconds: $134,459.79 traded, $134.46
    fee, about 9 bp arrival shortfall, roughly $257 all-in. No round trip was
    recorded, no `ERROR` line, no CHANGELOG entry until this one. `engine
    fills` charged all 336 prints to `long`.
  - Root cause: `native_common::planner_facts` built each sleeve's `Held`
    from `ctx.position()` — the account's whole holding — plus the sleeve's
    in-flight quantity, and skipped a symbol only when *another sleeve* held
    it (`foreign_position`). Exposure no engine order opened reads as
    nobody's there, so a hand position passed straight into the planner as
    the sleeve's own and was resized to the sleeve's target. The trait
    contract already said `position()` is "the wrong number for a strategy to
    hold inventory against"; the directional sleeves used it anyway. CARRY
    and EXODUS share the same builder and had the same exposure.
  - Fix: `Held` is now the sleeve's own signed fills (`my_position`) plus
    its in-flight quantity, capped by the account reading. A symbol whose
    venue position is entirely somebody else's, or sits on the other side of
    the sleeve's own fills, yields no holding — the planner neither exits nor
    resizes it. The fill sum is shaved of float dust at the `qty_step`'s
    decimal precision, and where it covers the venue's figure the venue's
    exact quantity is used, so partial-fill top-ups are unchanged.
    Five tests in `native_common`; the first reproduces the 08-22 shape
    (venue 33,180,700, own 222,000) and fails on the old code with
    `left: 33180700.0, right: 222000.0`. `docs/trading_logic.md` §7 carries
    the rule as item 4.
  - Bundled, because any edit under `engine/` restarts the funded engine:
    the unused `[profile.ci-test]` is gone from `engine/Cargo.toml` (the gate
    tests debug; release tests run off the deploy path).

- **2026-09-03 — Push runs stop queueing behind each other.**
  - Measured gate on `1e745078`: `rust` 3:57 (tests 2:57), artifact 5:46, ci
    1:50 — the deploy gate is the artifact, 5:46 against 20:50 this morning;
    release tests ran 12:57 off the path. But the next push sat `pending`
    behind that run, because the push concurrency group serialised runs and a
    run now lasts as long as its off-path release-test job. Push and PR runs
    take a group per `run_id`; dispatched VPS operations keep their one queue
    per ref, which is the only thing the group ever protected.

- **2026-09-03 — Every name has a trade tape: `wide` records prints.**
  - The exit-shaped tiers cover ~150 names with book and prints; the other
    ~350 had ticker and liquidations only, so a trade-level backtest of
    anything else ran on a third of the venue. Prints on a thin name measure
    about a tenth of a GB a month (`crowded:trades` 0.11 GB/name), so `wide`
    on Bybit now carries `trades` too — roughly 80–120 GB/month for a complete
    trade tape on all 517 names. Only the book is tiered. `wide:trades` sheds
    after `overheated` and before the discovery books: price and volume
    survive on the ticker, book depth does not survive anywhere.

- **2026-09-03 — The deep tiers hold a name as long as its sleeve would: the
  tape is shaped for exit studies first.**
  - `1e745078` deployed 21:46 UTC in **26 s**: `mainnet-fingerprint seeded from
    cc942816`, then `mainnet-ok result=unchanged-left-running` and the same for
    demo and both recorders. The funded engine was not restarted; the first
    gated deploy did what it was built for.
  - LONG holds a name that surged into turnover rank ≤ 10 for up to 72 h, and
    by day three a pumped name can sit at rank 300; `core` dropped it below
    rank 160, mid-hold, with the exit decision live. Ranked tiers now take a
    time floor: `sticky_hours` keeps a name for that long after it last ranked
    inside `top`, whatever its rank does. `core` is 96 h — the hold plus a day
    of tail. Off by default, so no other config changes meaning.
  - CARRY holds from a −10 bp settled print until settled funding rises above
    −3 bp, so a name at −4 bp for a week is a hold; `crowded` at −5 bp with
    48 h sticky dropped it. `crowded` now observes at the sleeve's **exit**
    line, −3 bp predicted, and holds 72 h past the last such reading: the whole
    hold zone by definition, plus EXODUS's settlement window. Both venues.
  - EXODUS needed nothing: its name is a CARRY hold seconds earlier, so it is
    in `crowded` or `core` with book and prints. The hourly re-anchor coincides
    with settlement and costs one round trip of deltas per name, on a fresh
    snapshot; written into the data spec so no study reads the seam as a venue
    event.
  - `docs/data.md` now carries the table of what the tape gives each sleeve's
    exit study — hold, deep-coverage guarantee, and the exit questions it can
    answer — ahead of the discovery tiers, which take whatever bandwidth is left.

- **2026-09-03 — Ceremony cut: a six-minute gate, and a deploy that restarts
  the funded engine only when the engine changed.**
  - `7625123f` deployed 21:3x UTC from the CI artifact; both recorders
    restarted on the crypto-only domain and the sleeve-shaped tiers.
  - **The gate tests the debug build.** The second run on the LTO-free profile
    took 9:52 warm against 10:06 cold, so the cache was never the cost: it is
    opt-level-3 codegen of the workspace into 34 test binaries on four cores,
    which `rust-cache` never caches. Clippy already builds the workspace in
    debug in 40 s; `cargo test` on top of it is a link and an 8-second run —
    the profile `scripts/dev.sh check` has always tested locally. `cargo test
    --release` moves to the release/soak job, `needs: [rust]` and off the
    `vps` path. Gate ≈ max(ci 2:00, rust ~3:00, artifact 5:45) against 20:50
    this morning.
  - **The realm handover is gated on what the realm runs from.** Every armed
    deploy ran `stop_realm_units mainnet → start_realm mainnet`, so a recorder
    config change restarted the funded engine. `realm_fingerprint` hashes the
    engine source *tree* (`git rev-parse <commit>:engine` — the binary embeds
    the commit and differs every time), `deploy/systemd`, the fleet manifest,
    the realm's worker config, and the rendered config and env files; a realm
    whose fingerprint matches and whose two long-running units are active is
    left trading (`mainnet-ok result=unchanged-left-running`), and picks the
    new binary up at its own next restart. The demo stop moves behind the same
    gate, after `install_release`, so nothing stops before the release is on
    disk. Tests pin the fingerprint's inputs, the gate on both realms, and the
    ordering.
  - `[profile.ci-test]` stays in `engine/Cargo.toml`, unused, until the next
    real engine change: any edit under `engine/` moves the tree hash and
    restarts the funded engine, and this commit is the first proof that a
    non-engine deploy does not.
  - The CI dispatch deploy carries the run's `GITHUB_TOKEN` to the host for its
    private fetch (`cc942816`, PR #18, merged just ahead of this). PRs are not
    the workflow from here: solo work pushes to `main`.

- **2026-09-03 — The deep tiers are the sleeves' own universes: `core` is
  LONG's rank band, `crowded` is CARRY's signal loosened.**
  - Sized from the live rules, not a guess. LONG enters at turnover rank 120
    and leaves at 160 with a $2M/24h floor and 30 days listed
    (`configs/signal-worker.mainnet.json`); 141 crypto names qualify today and
    the capture deep-recorded 30, leaving 93 LONG-eligible names ($30M down to
    $2.8M a day) on ticker alone. `core` is now `top = 120, leave_top = 160`.
  - CARRY enters when the last *settled* funding is ≤ −10 bp and exits above
    −3 bp (`docs/trading_logic.md` §4). `crowded` keyed on the *predicted* rate
    at −8 bp — barely loosened, and predicted leads settled by up to a funding
    interval. It is now −5 bp, so the book is recording as the crowd forms:
    16 names qualify today against 14, 10 of them at the sleeve's own −10.
    `overheated` mirrors at +5 (18 names against 9). Binance's two funding
    tiers move to 5 bp as well, so the cross-venue trades line up on the same
    trigger.
  - Bybit's allowance is 2,400 GB/month, from measured per-name rates (top-30
    book 17.8 GB, mid-rank 7.3, thin 2.4): ~2,300 projected at full sticky
    width. Binance measures 482 under its 700 and is untouched. The shed order
    now gives up `overheated` first — the one deep tier no sleeve trades — then
    the pump books, then their prints, and stops there: `crowded:*` joins
    `core:*` and `*:ticker` in the never-shed set, and the shipped-config test
    pins it.
  - One deploy for all of it. Each armed deploy runs the mainnet handover
    unconditionally (`stop_realm_units mainnet` → `start_realm mainnet`), so
    the recorder changes above and the crypto-only domain ship together rather
    than restarting the funded engine twice.

- **2026-09-03 — The recorder draws the same crypto line the sleeves do: stocks,
  ETFs and commodities leave every tier.**
  - Bybit files 230 of its 747 USDT `LinearPerpetual`s as `symbolType` `stock`
    (177), `ETF` (49) or `commodity` (4). The signal worker's live universe
    (`CRYPTO_SYMBOL_TYPES`) and the research universe table
    (`CRYPTO_LINEAR_SYMBOL_TYPES`) both keep only `""` and `"innovation"`; the
    recorder's `listed_symbols` kept everything. So the capture spent bytes on
    names no sleeve can hold and no study consumes, and the burst sensors read
    the US open as a pump: before the change `levering` resolved to seven names
    and all seven were equities (`APPSTOCKUSDT FLEXUSDT INTUUSDT NVDAUSDT
    TEAMUSDT TSLLUSDT WENSTOCKUSDT`), `flooding` was over half equities, `core`
    carried six (`CLUSDT KORUUSDT SNDKUSDT SOXLUSDT SPCXUSDT XAUUSDT`, ~18
    GB/month of 50-level book each), and one of the three "pump" books rebuilt
    as proof earlier today, `POETUSDT`, is Poet Technologies.
  - `BybitAdapter.listed_symbols` now keeps only `CRYPTO_SYMBOL_TYPES`, and since
    `listed()` is what every tier's `allowed()` resolves from, a stock enters no
    tier at all — not the ranked ones, not the funding ones, not the `wide`
    ticker. Nothing is subscribed for it. `XAUTUSDT` (Tether Gold) stays: the
    venue types it as an ordinary crypto token, and the rule follows the venue's
    field rather than a hand-picked list.
  - `excluded_listed` on both adapters counts what the filter left out, by the
    venue's own label, and the recorder logs it each time it takes the tables:
    `venue tables: 517 USDT perpetuals in the domain; outside it ETF=49
    commodity=4 stock=177`. A label the venue has not used yet lands in that
    line rather than in a silent gap.
  - Binance needed no change: it files the same products as
    `contractType: TRADIFI_PERPETUAL` (189 rows), which the adapter already
    refuses; the only non-`COIN` names it admits are the crypto indices
    `BTCDOMUSDT` and `ALLUSDT`. Its test now pins the refusal.
  - `tests/repo/test_crypto_domain_is_one_line.py` asserts the three constants
    agree, reading the worker's from `universe.rs` so a drift in any language
    fails one test. It lives in `tests/repo` because `market_tape` is isolated:
    its own tests may not name the trading package.
  - Expected on the host: `wide` falls from 716 to ~487 names (about 84 GB/month
    of ticker), `core` swaps six stocks and gold for the six crypto names ranked
    31–36, and the discovery tiers stop filling on the opening bell.

- **2026-09-03 — `f06a89f4` deployed; the deploy gate stops paying thin LTO on
  34 test binaries it never ships.**
  - Deployed 20:28 UTC from the CI artifact. `deploy-ok
    commit=f06a89f48980ea9a40a52fb77abaa900baeb4810`, rollback target
    `ce252af8`, `real-money armed`, every unit on a fresh heartbeat.
  - Verified on the host, splitting each hour-20 segment at the restart
    timestamp: Binance writes `ticker`, `public_trade` and `liquidation` and no
    book row of any kind; Bybit writes `orderbook_snapshot` + deltas + prints +
    ticker for `BTCUSDT` and `TUTUSDT`, so `core:trades` is recording again
    after being permanently shed under the old budget. `budget.shed` is empty
    against the 1800 GB allowance, `dropped=0 disk_dropped=0`, and the log
    carries `re-anchored 1 book topics for 2026-09-03T20`.
  - Coverage is total by construction, not by sampling: the venue lists 855
    instruments, of which 747 are USDT `LinearPerpetual` — and the tiers hold
    716 (`wide`) + 30 (`core`) + 1 (`pinned`) = 747, with an open segment on
    disk for each. 448 of those segments read 0 bytes because `SegmentWriter`
    opens with `buffering=65536`; a thin name shows nothing until 64 KB
    accumulate.
  - **The gate was 20:50, and 16 of those minutes were one link-time pass.**
    The CI Rust cache hits (13 `Compiling` lines, all of them workspace
    crates), the last crate starts at 20:05:02, and `Finished release profile`
    lands at 20:21:02. The whole suite *runs* in 7.8 s across 34 binaries. The
    silence is `lto = "thin"` being applied to every one of those binaries.
  - Fix: `[profile.ci-test]` inherits `release` and sets `lto = false`, and CI
    tests with it. Same opt-level, same `debug_assertions`, same overflow
    checks — LTO only changes cross-crate inlining. Measured cold on the same
    machine: 1146 s CPU with thin LTO, 686 s without, a 40% cut. Test selection
    is byte-identical, 1,687 tests either way.
  - The deployed binary is unchanged: `[profile.release]` keeps thin LTO, and
    the artifact build now runs as its own `rust-artifact` job *beside* the
    tests instead of after them. `vps` gates on `[ci, rust, rust-artifact]`, so
    a red test still blocks the deploy, and the artifact resolves by name so
    nothing downstream moved.

- **2026-09-03 — The capture earns its bandwidth: Binance stops recording books,
  Bybit gets the room, and every hour of tape anchors its own books.**
  - Verified first, on the running host: every name the funded engine holds is
    captured with book, prints and ticker. `NEARUSDT` 16,720 fifty-level deltas
    and 1,557 prints, `ZECUSDT` 38,857 and 32,441, `AGIUSDT` 8,018 plus 382
    top-of-book rows. The tiers are keyed on the same signals the sleeves
    trade, so LONG's names sit in `core` and CARRY's in `crowded` by
    construction.
  - **Binance records no order book.** Its 1000-level diff stream cost 792
    GB/month and nothing reads it: the only study that opens that tape is
    `research/lab/tape.py`, which asks for `mid`, and the cross-venue panel
    reads the REST hourly datasets, not the tape. `bookTicker` is not the
    cheap substitute it looks like — the config's own measurement is 434 KB/s
    for twenty names, 1.1 TB/month, more than the book it would replace. What
    stays is the ticker (`markPrice@1s`: the funding rate as it moves, mark and
    index) on every listed name and the trades where flow matters, which is
    every bars column the cross-venue work reads. `monthly_gb` 1300 → 700,
    `max_disk_gb` 30 → 18.
  - **Bybit takes the freed line**: `monthly_gb` 1300 → 1800, `max_disk_gb`
    40 → 60 (about three days on disk). At its 48-hour tier width the recorder
    projects 1,710 GB/month, so it now fits and sheds nothing. `core:trades`
    is out of the shed order entirely: it went last, so it was the first thing
    permanently sacrificed, and a maker replay fills resting orders against
    exactly those prints. On the host they had been zero since 13:27 UTC while
    the books kept flowing at 141k deltas an hour. The order now gives up the
    discovery tiers, then their prints, then the crowd books, and never a
    ticker or `core:book:50`.
  - **Hourly book anchoring** (`connection.reanchor_books_each_hour`, default
    on): every book topic is re-subscribed once per UTC hour, 40 topics per
    maintenance tick in chunks of 10 dropped and re-taken together, so a
    symbol's book is gone for one round trip rather than for its whole shard's
    pass. The venue answers a subscribe with a snapshot. The
    hour is the archive's unit, so each uploaded tar now replays on its own.
    Before this, only the recorder's start anchored a book: four recorded hours
    of `AGIUSDT` held 78,895 fifty-level deltas and no fifty-level snapshot, and
    a range starting at hour 02 produced 173,011 events, all trades, no book and
    no orders. Cost is one 2.5 KB snapshot per symbol per hour, under 1 GB/month.
  - `engine backtest` **refuses a book row from another venue**
    (`TapeError::UnsupportedVenue`). This reader chains by Bybit's monotone
    `update_id`; Binance brackets each diff with `first_update_id`/`pu`, so its
    rows read here would build a plausible book that is not the venue's. Trades
    and tickers carry no chaining and are still read from any venue.
  - Separation verified: distinct roots, distinct systemd `StateDirectory`,
    distinct Drive prefixes, and every row names its own `venue` (the Binance
    tape reads back `{'binance'}` and nothing else).

- **2026-09-03 — Incident: the CI deploy could not reach the funded host,
  because it sends the host no credential for the private fetch.**
  - `vps-deploy.yml` run 33802727037, `deploy main@42c1529`, dispatched
    20:31:10 UTC to carry the liveness fix. The `vps` job failed at 20:52:49
    UTC in `Run VPS mode`, on the host, at `fetch_exact_commit`:
    `fatal: could not read Username for 'https://github.com': terminal prompts
    disabled`, then `deploy failed: cannot fetch origin/main`. The deploy stops
    before it stops anything, so no unit moved and the funded engine kept
    trading `f06a89f4`, the commit the 20:28 UTC local deploy left on the host.
  - Cause: the workflow's `Run VPS mode` step passed `EXPECTED_COMMIT`,
    `BRANCH`, `SSH_TARGET` and `SSH_OPTS` and no `GITHUB_TOKEN`, and
    `actions/checkout` runs with `persist-credentials: false`. On the runner the
    script's `gh auth token` fallback (`scripts/deploy_vps_live.sh:58-60`) has
    no authenticated `gh`, so `GITHUB_TOKEN` was empty, so `git_authorized`
    (`:186`) skipped its authenticated path and the host fetched a private
    repository with whatever credential it had of its own. That worked at
    12:52 UTC (run 33756354829) and not at 20:52; the workflow has never
    supplied a token, so CI deploys have always rested on an undeclared host
    credential.
  - Fix: the step now passes `GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}`. The
    run's own `contents: read` token travels to the host inside the piped
    remote script, and `git_authorized` spends it on one fetch through a 0600
    `GIT_CONFIG_GLOBAL` it deletes afterwards — the mechanism the script
    already implements for an operator's own token. No credential is stored on
    the host and none is added to the repository.
  - `tests/scripts/test_runtime_scripts.py::test_the_ci_deploy_hands_the_host_a_token_for_the_private_fetch`
    reads the workflow and asserts the deploy step carries the token, that the
    permission it needs is `contents: read`, and that the remote body still
    spends it on the fetch. Without the fix it fails `KeyError: 'GITHUB_TOKEN'`.
  - Still open for the owner: the host's own https credential for
    `/opt/liquidity-migration` stopped working between 12:52 and 20:52 UTC.
    Nothing here touches it, and a local `scripts/ops.sh deploy` keeps working
    because `gh auth token` fills the same variable from the operator's shell.
  - The page that put an agent on this: `fleet liveness (mainnet)` raised
    `CRITICAL liquidity-migration-mainnet-liveness.timer is inactive` inside the
    20:28 UTC local deploy of `f06a89f4`, which predates the fix below by two
    commits — the funded twin the entry below predicts, one line and no other
    unit, resolved by `start_unit` a moment later. Watching that fix's deploy is
    how the failure above was found: the run had nobody on it.

- **2026-09-03 — Incident: a deploy pages its own liveness watchdog.**
  - `fleet liveness (demo)` raised two CRITICALs on `ip-208-84-103-4` inside the
    `ce252af8` deploy (19:09 UTC): `liquidity-migration-chaos-drill.timer is inactive` and
    `liquidity-migration-demo-liveness.timer is inactive`
    (`check_fleet_liveness.py::evaluate_units`). Both are real states, held for
    seconds, inside the deploy that caused them. No unit was down, no position
    was unprotected, and the funded engine was untouched.
  - `start_realm` walked one list in stop order, so the realm's `job-now`
    watchdog (`liquidity-migration-demo-liveness.service`, stop order 200) ran
    to completion before the same loop enabled the realm's timers
    (`demo-liveness.timer` 80, `chaos-drill.timer` 50) that
    `stop_realm_units demo` had just disabled. The watchdog checks every
    manifest unit for `active` with no grace, so it alerted on the two timers
    it was four lines early for, and its CRITICAL fired the on-call routine.
    Both realms carry the fault: `mainnet-liveness.service` (210) likewise
    precedes `mainnet-liveness.timer` (90).
  - The `job-now` units now run in a second pass, after every other activation
    unit in the realm is up. `lm_immediate_timer_jobs` drives that pass, and the
    first loop skips its members.

- **2026-09-03 — Replay throughput and mid-recording ranges, the first real-tape
  run, and the recorder's budget controller.**
  - `engine backtest` writes its log unsynced by default
    (`WalWriter::open_unsynced`: same frames, same sequences, no wait for the
    disk at a barrier); `--durable-log` keeps the live path's fsync per order.
    The 2 h, 8,335-order fixture: 35 s → 2.2 s, and the two logs are
    byte-identical. The live engine's `WalWriter::open` and `open_current`
    are durable as before.
  - The venue matches against the deepest book whose chain is intact
    (`Cursor::deepest_valid_depth`). Found on the first real tape: four hours
    of `AGIUSDT` cut from the middle of the recording carry 78,895
    `orderbook.50` deltas and no 50-level snapshot, so the deep book never
    chains; the `orderbook.1` stream is a snapshot every row and now stands in
    until a deep snapshot lands. Before, every order in such a range was
    refused for want of a book.
  - First real-tape run: `AGIUSDT` 2026-09-03T14..18Z from the host's recorder
    (109,983 rows), the maker canary's registered rule
    (`lane2_toxic_flow_quoter_v1`, `quote_enabled = true`), 1,000 USDT: 20,091
    market events, 6 orders, 3 maker fills, 1 closed trip, 0 rejections, 0
    fills priced at mark, 1.9 s. A rerun is byte-identical (log, trades,
    equity). Reconciliation not checkable: a position was open at tape end.
  - `market_tape` budget controller (`record.py::BudgetController`). The
    projection counted a shed pair's bytes for the rest of the trailing day,
    so one shed per hour drained the whole `shed` list in 12 h whatever the
    first shed had achieved; restore compared that same projection to
    `restore_below`, so nothing came back; running out of pairs was silent. On
    the host at 18:20 UTC all 12 pairs were shed, `core:trades` last at 13:27
    UTC, `projected_month_gb` 1710 against 1300. Now the projection leaves
    shed pairs' bytes out; one action sheds as many pairs as the projection
    needs; a pair returns only when its GB/month as measured at its shed fits
    under the restore line; over budget with the list exhausted is a `WARNING`
    per action. `status.json` gains `budget.shed_gb_month`.
  - The Bybit recorder's arithmetic, from 17 h of metering: the feeds the
    `shed` list cannot reach project ~1,500 GB/month on their own
    (`core:book:50` 697, `wide:ticker` 355, `crowded:book:50` 300,
    `core:ticker` 89, `crowded:trades` 34, `overheated:trades` 25) against
    `monthly_gb = 1300`. The list cannot meet the allowance; the recorder now
    sheds everything listed at once and says so every hour. What else goes —
    the crowd tiers' 50-level books (`crowded` 116 + `overheated` 115 names:
    |funding| ≥ 8 bp once in 48 h keeps a name, and 30 s re-resolution
    restarts the 48 h), the core's size, or the Binance share of the host's
    4 TB line — is the owner's decision; the shed order stands as written.
  - `storage.py::Retention.prune` deleted `_meta` table snapshots for disk
    room, oldest first by mtime, receipted as `segment_deleted`. They go with
    age only now, as `snapshot_deleted`.

- **2026-09-03 — `engine backtest`: the live loop on a recorded tape, in the
  tape's own time.**
  - Deleted the earlier replay driver (`engine-core/src/backtest/`,
    `scripts/research/run_engine_backtest.py`) and its process-global virtual
    clock. Audited before deletion: with a `biased` `select!` over an
    always-ready feed the loop never ticked, never ran a strategy timer, never
    polled the signal feed, and filled orders against the book at the end of
    the tape (20,000 events → 2 orders; `dispatch queue 60.03s`); it charged
    funding on every ticker frame (500 USDT where one settlement is 5), filled
    an unquoted symbol at an invented 100.00, ignored `reduce_only`, posted no
    margin, could not read `market_tape`'s row contract (a real-schema tape gave
    `0 orders, +0.00%` and exit 0), and its clock override broke 28 of 484
    `engine-core` tests when held for 300 ms. `scripts/dev.sh check` refused it.
  - Rebuilt as `engine backtest --config --tape --instruments --wal [--signals
    --trades --equity --report --capital --taker-fee --maker-fee --rtt-ms
    --private-latency-ms --mmr]`. The tape is `market_tape`'s frozen schema
    (`python -m market_tape rows`, `.jsonl` or `.jsonl.zst`); books are
    rebuilt with the recorder's chaining rule and the live feed's level
    merge; instruments come from the recorder's `instruments_snapshot`. A row
    that breaks the contract stops the run with its line number.
  - Time: `engine_types::clock` gains a thread-local virtual clock behind an
    RAII guard (no other thread can see it; a failed run cannot leave it
    installed). The loop's two timers come through a `LoopTimer` seam
    (`Engine::run_with_inputs_on`); `run_with_inputs` passes `SystemTimer`, so
    the live loop is unchanged and monomorphised. The tape feed is the only
    clock pump: nothing later is released while an earlier wait is due, and a
    lowest-priority pump task moves the clock only when the loop is blocked on
    a venue reply outside its `select!`.
  - Venue: fills walk the book level by level (partials), resting orders sit
    behind the displayed queue and fill from prints that reach them, stops
    trigger on the mark and fill through the gap, funding settles once per
    published boundary at the rate quoted before it, margin is posted and
    checked, `reduce_only`/tick/step/minimum refusals carry Bybit's codes,
    liquidation closes at Σ maintenance. Orders fly half a round trip each way
    (default 175 ms) and match against the book at arrival; private updates
    hop 60 ms. Not modelled: our impact on the tape's liquidity, reactions to
    us, liquidation fees, rate limits.
  - Report: venue books and the engine's `ClosedTrade` ledger side by side; a
    flat account whose two sides disagree fails the run. Two runs of one tape
    write byte-identical logs (tested; also plain vs `.zst` on a 2 h fixture:
    21,602 events, 8,335 orders, 6,667 fills, 1,635 trips, identical WAL,
    trades, equity).
  - `Engine::finish` now writes closed trades before the final ledger record:
    a trip closed after the last group-flush tick was missing from
    `trades.jsonl` on any graceful stop, live included.
  - `engine-wal`: a log named without a directory (`engine.wal`) could not be
    created — `Path::parent` of a bare name is `""`, and `File::open("")` is
    ENOENT. `engine bench --wal rel.wal` and a fresh host with the shipped
    `wal_path = "engine.wal"` hit it. Fixed with a test.
  - `scripts/research/run_engine_backtest.py` reads the engine's own report,
    trades, and equity files: arithmetic return on capital, calendar-span
    annualisation, equity-series drawdown, Sharpe only from ≥ 7 daily closes,
    unknown fees kept unknown.
  - Gates: `engine-core` 507 tests, `cargo clippy -D warnings`, `cargo fmt
    --check`, ruff, mypy green.

- **2026-09-03 — Payload encoding, step two: the writer emits the payload as
  a JSON string.**
  - `payload_wire::serialize` writes UTF-8 payloads as a string and anything
    else as the byte array. The carry row that was 20.8 MB on disk is about
    7 MB and rides the socket doorbell again. The worker's input-journal
    replay now compares observations as values, not bytes, so entries written
    under the old encoding still replay. Deployed only after step one was a
    finished deploy on both realms, so an auto-rollback lands on a reader
    that takes both shapes. Test extended:
    `a_payload_reads_as_a_string_or_as_an_array_of_bytes` (binary payloads
    still round-trip as the array).

- **2026-09-03 — A sequence gap is an `ERROR` line, not a crash loop; the
  payload reader takes a string as well as a byte array.**
  - *Gap.* `queue_signal_observation` (`engine/engine-core/src/engine/scheduling.inc.rs`)
    exited on `sequence != expected`. Every gap loop this repository has
    recorded was the engine's own doing (a lost frame boundary this morning, a
    dropped spool read this afternoon), and exiting never fetched the missing
    row: the cursor is durable and the restart met the same gap, with the
    funded book unattended. Now a row above `expected` is delivered with an
    `ERROR` line naming the source, the expected and received sequence, and
    the count skipped; the cursor records the jump. A row below `expected` is
    dropped with a `WARN`. `rewrote durable sequence` (same sequence, different
    bytes) stays fatal. Test:
    `a_gap_the_spool_cannot_fill_is_logged_and_the_engine_goes_on`. Runbook
    §8 rows updated; the generation recipe now serves the rewrite case.
  - *Payload encoding, step one.* `SignalObservation.payload: Vec<u8>` is
    written as a JSON array of integers, 3.3× the bytes (the 6.27 MB carry row
    is 20.8 MB on disk and takes ~0.5 s to parse). The type now reads the
    payload as a JSON string or as the array (`payload_wire`, in
    `engine/engine-types/src/strategy.rs`); the writer still emits the array.
    The writer flips only after this reader runs on both realms: the engine
    WAL and the worker's input journal both hold observations in the old
    shape, and a binary that could not read the new one would fail replay.
    Test: `a_payload_reads_as_a_string_or_as_an_array_of_bytes`.

- **2026-09-03 — Both signal workers crash-looped on a preflight miss, then
  a 20 MB carry row put both engines back into the sequence-gap loop. Three
  faults fixed at the root; no manual generation bump was needed.**
  - *What happened.* 12:53:13 UTC demo worker, 12:53:30 mainnet worker:
    `signal-worker: state: spool class preflight underestimated an emitted
    observation batch`, exit `status=2/INVALIDARGUMENT`, every ~75 s under
    `Restart=always` (46 exits each by 13:40). 13:01:26 demo engine, 13:02:44
    mainnet engine: `invalid signal frame size: 20824977 bytes (max: 16777216)`
    (demo: 20293767), exit. Every restart then failed within 4 s with
    `signal source directional_public_v1.g….carry has sequence gap: expected
    946, got 947` (demo: `expected 931, got 932`), 320 mainnet and 353 demo
    exits by 13:40. Row 946 was on disk the whole time. The funded account
    held SKR short (exodus), FLOCK and BICO long (carry) with no engine
    to exit them.
  - *Fault 1, worker preflight.* `projected_spool_files`
    (`engine/signal-worker/src/worker.rs`) had no arm for
    `WireEvent::LlmGateCandidates`, so a gate publication projected zero
    `current` rows and emitted one; the post-apply check refused the batch
    as underestimated and the process exited. The preflight shipped in
    `af40545e` this morning; the gate event has existed since `1c3cf4c3`.
    The first gate publication after the deploy (12:53) crashed both
    workers, and every restart replayed the same file. Now the arm projects
    one `current` row. Test:
    `a_gate_publication_passes_the_spool_preflight` (fails on the old code
    with the production error text).
  - *Fault 2, engine spool reader.* `SpoolSignalFeed` popped a row out of
    `known_paths` and then awaited its blocking read. The core drops the
    feed future whenever another `select!` branch wins, so a read that took
    longer than one poll (the 20 MB row: ~0.5 s to parse) was abandoned with
    its row already forgotten; the next row was read and the core saw a gap.
    The same class as this morning's frame fix, on the other half of the
    feed; this is also why every earlier gap loop followed a big row. Now
    the row stays in `known_paths` until its read completes and the
    in-flight `JoinHandle` is kept on the feed and joined on the next call.
    Test: `a_row_whose_read_the_core_dropped_is_still_delivered_first`.
  - *Fault 3, the frame cap.* `payload: Vec<u8>` serializes as a JSON array
    of integers, so a 6.27 MB `carry_feature_batch` payload (310 symbols) is
    a 20.8 MB envelope. The worker caps the payload at 16 MiB, the engine
    caps the *frame* at 16 MiB, and the frame carries the envelope. The
    worker now sends no frame for a row wider than the cap (the row is the
    delivery; the spool poll reads it), and an oversize frame length is a
    `WARN` and a dropped stream in the engine, not an exit. Tests:
    `a_row_wider_than_one_frame_rings_no_doorbell`,
    `an_oversize_frame_length_costs_its_stream_and_nothing_else`. The 3×
    encoding itself is the next change.
  - *Recovery.* Deploy only. With the reader fixed the engines read 946 and
    931 from the spool and the cursors advance; the worker stops exiting on
    the next gate file. Runbook §8 rows updated.

- **2026-09-03 — Refactored all project skills, MCP configuration, and Claude project memory into the Spec-First standard with tables.**
  - *Skills refactor.* Converted all 8 skills under `.codex/skills/` (`backtest-integrity`, `equity-curve`, `pit-reconcile`, `repo-map`, `research-phase-runner`, `research-report`, `run-strategy`, `vps-migrate`) into the 4-part Spec-First skeleton (Purpose, Spec Tables, Invariants, Operational Recipes). Replaced loose narrative paragraphs with structured markdown tables for parameter routing, artifact schemas, and failure triage matrices.
  - *MCP specification & config.* Created `docs/mcp.md` defining server registries, tool schemas, transport contracts, and permissions. Added clean `.mcp.json` at repository root with stdio transport.
  - *Claude project memory index.* Restructured `/Users/jhbvdnsbkvnsd/.claude/projects/-Users-jhbvdnsbkvnsd-Desktop-liquidity-migration/memory/MEMORY.md` into high-density reference tables covering standing conduct, tooling traps, engine runtime, lease locking, deployment procedures, and research findings. Refactored sub-indices `latency-and-order-path-index.md` and `historical-2026-06-07-index.md` to match.

- **2026-09-03 — Streamlined delivery pipeline: eliminated self-PR ceremony on `main`, added CI Rust caching, and decoupled heavy soak/benchmarks from the deploy path.**
  - *Ruleset change.* Removed mandatory `pull_request` and status-check gates from GitHub Ruleset `22048243`. Fast-forward linear direct pushes to `main` are enabled for hotfixes and operational changes, eliminating 15–20 minutes of dead queue time per agent iteration. Protections against deletions and force-pushes remain active.
  - *CI caching & job decoupling.* Added `Swatinem/rust-cache` to `.github/workflows/vps-deploy.yml` across `engine/`. Moved the 2,000,000-op account soak test and 20,000-event benchmark into a non-blocking parallel job (`rust-soak-bench`). The release compilation and smoke-test gate runs directly after unit tests (`cargo test --workspace --all-targets --release`), unblocking VPS deployments in 1–2 minutes rather than 12–19 minutes.
  - *Local testing mandate.* Codified in `AGENTS.md` that agents must run fast local unit tests (`cargo test -p <crate>`, ~3s) before pushing, forbidding the anti-pattern of using GitHub Actions or VPS deploys as parsing diagnostics.

- **2026-09-03 — The signal stream lost frame boundaries, both engines
  crash-looped, and the funded engine was down nine hours. Fixed at the root,
  with the instrument lane that had been dead since 09-01.**
  - *What happened.* Demo 01:01:28 UTC, mainnet 01:45:03 and 01:55:02, demo
    again 02:36:18: `invalid signal frame size: 1668489851 bytes`. That number
    is `0x6373227B`, little-endian ASCII `{"sc` — the opening of the JSON body
    read where a length prefix belonged. Each time the engine exited, and every
    restart then failed with `signal source directional_public_v1.g….carry has
    sequence gap: expected N, got N+1` under `Restart=always`: demo 3,153
    restarts by 10:46, mainnet 61 before it was stopped by hand at 01:56 with
    three carry positions open (SKR, FLOCK, BICO, about 74 USDT on 130 equity;
    venue stops resting). The funded engine stayed stopped until this fix
    deployed.
  - *Root cause, reader.* `UnixSignalFeed::next_observation`
    (`engine/engine-core/src/signals.rs`) is one branch of the core's
    `select!`, which drops the future whenever a market event wins. It read
    the frame with two `read_exact` calls, which are not cancel-safe: a poll
    that had taken the four length bytes and was waiting for the body lost
    them when dropped, and the next poll read the body's first four bytes as
    the next length. The worker made the window easy to hit by sending the
    length and the body in two separate `write` calls. Now the frame in
    progress lives on the feed (`Frame`), every read is a cancel-safe `read`
    that resumes where it stopped, and the worker sends one buffer in one
    `write`. Test: `a_frame_split_by_a_dropped_future_is_still_one_frame`,
    which fails on the old reader with the production error text.
  - *Root cause, permanent gap.* The observation the crashed engine had
    consumed off the socket existed nowhere else: the worker wrote a spool row
    only when the socket send failed. Now the worker writes the row first,
    always (`SpoolWriter::write_encoded_observation`), and the frame is a
    doorbell carrying the same bytes. The engine retires the row after the
    barrier whichever way it arrived, and on a frame it first delivers any
    rows with a lower sequence, which the worker wrote before that frame
    (`HybridSignalFeed`). Nothing an engine loses is lost. Cost: one fsync'd
    35 KB write per observation in the worker, at about nine a minute.
  - *Recovery of the two live gaps.* The rows for mainnet 11226 and demo 11613
    were consumed by crashed engines and are gone. Each worker was given a new
    generation (`source_generation` blanked in `checkpoint.json`), so its
    source id changed and the engine met it as a new source at sequence 1,
    keeping the old cursor. Recipe in docs/operations.md §8.
  - *The instrument lane.* Both workers logged `instrument lane: input:
    Trading instrument has already passed its delivery time` every hour since
    the 09-01 deploy of 07407a58, and from 09-03 01:03 `maxMktOrderQty is not
    positive`. Both are checks that fail on the venue's real shape: Bybit
    publishes `deliveryTime: "0"` on every perpetual (813 of 855 linear
    contracts today), and zero order-size maximums on Closed and Delivering
    contracts. One row refused the whole snapshot, so **both workers ran with
    an empty instrument table (`instruments: {}` in both checkpoints) for two
    days**. The fixture rows in the tests used `delivery_time_ms: None`, which
    the venue never sends. Now a zero maximum is no maximum
    (`published_maximum`), the snapshot-level delivery check is gone, and
    `instrument_is_trading` treats a contract at or past a real delivery clock
    as not trading, zero meaning no clock. Test:
    `a_snapshot_of_perpetuals_with_zero_delivery_clocks_passes_source_validation`,
    which fails on the old check.
    After that deploy the lane failed a third way, `invalid symbol
    "BTC-01DEC23"`: the Closed list carries 643 dated futures and the Trading
    list 40 (`BTCUSDT-04SEP26`), names the worker never trades. The lane now
    keeps every row it can and names what it left out
    (`normalize_instruments_reporting`), one line per snapshot: against the
    venue's lists of the day, 1,138 rows kept, 683 dated names left out, no
    other reason. One row cannot cost the table again.
    The ticker page the same lane fetches carries the same 40 dated names;
    it is now tolerant the same way (819 rows kept, 40 left out, no other
    reason). The single WebSocket ticker row stays strict, because a
    malformed frame there is a stream gap to repair, not a list to trim.
  - *On-call agent.* `check_fleet_liveness.py` now fires a Claude Code
    routine on any `CRITICAL` that clears its cooldown, when
    `INCIDENT_ROUTINE_FIRE_URL` and `INCIDENT_ROUTINE_FIRE_TOKEN` are set in
    `liveness.env`; payload is the alert lines plus each failing unit's last
    40 journal lines. The routine prompt is `deploy/incident-routine-prompt.md`;
    the owner creates the routine and its token at claude.ai/code/routines.
  - *Standing rule.* AGENTS.md §The Funded Engine Is Production: a fault in
    the funded engine is fixed, tested, deployed, and verified in the same
    session; stopping it is a holding action.
  - `Restart=always` stays. A start limit would strand the funded engine after
    a venue outage that it would otherwise recover from on its own; the fix
    above removes the fault that made the loop endless, and the on-call agent
    is what now answers a loop.
  - *Deploy and recovery receipt.* Merged as `a2dc5a45` (PR #14), deployed by
    `vps-deploy.yml` run 33750120171 (`mode=deploy`, all jobs success), on the
    host at 11:42 UTC. New generations at 11:43: mainnet
    `805c44f0…` → `b01e9e6f…`, demo `c4d0071f…` → `c3ed639a…`. The first engine
    start after that still died on the old gap: the dead generation's orphan
    rows (`…11227-…json`, `…11614-…json`) were still in the spool and sit
    above the cursor, so they read as the gap. Removed by hand; both engines
    active from 11:43:53 UTC. The recipe in docs/operations.md §8 now carries
    that step. Funded engine downtime: 01:56:04 to 11:43:53, 9 h 48 min.

- **2026-09-03 — An audit of the live fleet, and the eight things it found.**
  Read off the running host rather than the docs: both engines and both
  recorders healthy, the signal IPC connected in both realms over the sockets
  with no spool files, the funded lease held by one writer, and the funded WAL
  replaying clean (599,911 records over two segments, no CRC or torn frame, no
  order left in flight, reconcile finding nothing). What was wrong:
  - The funded account's 24h loss window was **tripped** — one close at
    −16.14 USDT against a 12.98 limit — and the risk kernel was correctly
    refusing every entry while letting exits flow. The heartbeat did not say
    so: `rolling_loss_tripped` read true while `strategy_entries_enabled`
    still reported every sleeve as entering, so the file an operator reads
    said "trading normally" about an account that was opening nothing. The
    beat now gates those switches on the window.
  - The off-box backup had not completed since 2026-09-01 03:17 UTC. It runs
    every six hours, and each run was killed at the 15-minute
    `TimeoutStartSec` before it could land a first full copy, so no run ever
    established a baseline and every later run repeated the whole transfer.
    Every run that did real work also peaked at exactly its 512 MB
    `MemoryMax`: four transfers at a 32 MB Drive chunk. The budget is now an
    hour (flock, not the timeout, is what stops two runs overlapping) and the
    chunk is 8 MB.
  - `LONG_NOTIONAL_MULTIPLIER=3.0` in the funded credential file is inert and
    always was: `notional_multiplier` is written in
    `liquidity_migration/policy/real_money_profile.py`, 6.0 for LONG and 3.0
    for carry, and no code anywhere reads that variable. Both realms render
    6.0. The funded file understated funded LONG size by half to anyone who
    read it; the dead lines are removed from the host.
  - The two recorders' `max_disk_gb` summed to 120 GB on a 118 GB filesystem,
    so neither ever pruned on its own cap and both raced the shared
    `min_free_disk_gb` guard instead. Bybit is now 40 GB and Binance 30 GB,
    sized on measured ingest (8.0 and 5.8 GB/day) and summing under the disk
    with room for the engines' WALs. Local tape is about five days either
    way; the hourly Drive archive is the history.
  - A tier with no instrument table fell back to the raw ticker stream and
    dropped its own quote filter with it, so a cold start could record
    `WLDUSDC` and `ADAUSD_PERP` off a USDT universe. The fallback now applies
    the same shape rules `listed_symbols` does. Twelve zero-byte Binance
    segments and thirty-two Bybit ones were the residue.
  - The staged-binary path verified `binaries.sha256` only `if [ -f ]` it, so
    an artifact that simply omitted the manifest installed unverified. The
    manifest is now required and `tar` is checked. CI has always produced one.
  - `engine fills` reports a log's whole history, and the funded log opens in
    shadow — orders worked out and never sent. The command now says how many
    shadow records it read rather than presenting the two eras as one.
  - Not changed, and why: `provision_mainnet` renders the funded config with
    the binary `install_release` just installed, so it cannot move above it.
    The window where the funded engine runs the old binary and old config
    while both new ones sit on disk stays, and is now documented where
    somebody would otherwise reorder it.
  Two things the audit got wrong and then disproved: the recorders' 1,300 GB
  allowances are per venue against the host's 4 TB line and are not
  over-subscribed, and the capture services' 1 GB memory ceiling is page cache
  from their own writes (anon 127 and 101 MB), not a leak.

- **2026-09-02 — Deployed `76a8fc59` at 22:45 UTC: decoupled Mainnet deployment, Unix socket IPC, and the Rust market-tape crate.**
  Mainnet deployment was decoupled from Demo verification: Demo was deployed,
  restarted, and checked for fresh heartbeats while Mainnet continued actively
  trading and quoting. Mainnet pre-flight and configuration validation ran in the
  background; the funded engine swap took 9 seconds. Signal delivery switched from
  filesystem spool polling to direct Unix domain socket streaming (`stream.sock`),
  cutting signal delivery latency to microseconds and eliminating SSD inode churn,
  with automatic disk spool fallback during restarts. The native `market-tape`
  Rust crate was added to the workspace and installed into `/opt/liquidity-migration-engine/bin/market-tape`.
  Both engines and signal workers heartbeated within 2 seconds of startup, and both
  market recorders are active with zero dropped frames.
- **2026-09-02 — The recorders are cut to fit their byte budgets.** Four
  minutes after the Binance fix, the meters read 0.64 MB/s inbound on Bybit
  (1.7 TB a month against 1.3) and 1.18 MB/s on Binance (3.0 TB against 1.0).
  The single largest feed on the host was Binance's top-of-book stream for
  twenty core names, 434 KB/s, more than that recorder's whole allowance, and
  redundant: the 1000-level diff book carries the top of book every 100 ms, as
  Bybit's 50-level book does every 20 ms. The top-of-book feed is dropped from
  every tier but the pinned canary on both venues, Binance's core is fifteen
  names (leaving below rank 22), Binance's allowance rises to 1,300 GB to match
  Bybit's (2.6 TB inbound plus about a tenth of that in uploads, inside the
  4 TB line), and the shed order becomes: the short-lived tiers' deep books,
  then their trades, then the core's trades, then (Binance only) the wide
  ticker. Expected after the change, from the same meters: Bybit about
  1.5 TB, Binance about 1.3 TB before the budget acts; the controller sheds
  the rest.
- **2026-09-02 — The Binance recorder was hearing only its book streams.**
  Verifying the deploy by the bytes each feed received showed the Binance
  recorder taking depth and top-of-book frames and nothing else: no trades, no
  mark price or funding, no 24h ticker, no liquidations, on any tier, so the
  wide tier wrote no rows and the live universes there saw only what the REST
  tables seeded. Probed from the host with the recorder's own URL, the venue
  confirmed it: Binance now routes its market streams by URL path, `/public`
  for the high-frequency streams (depth, `bookTicker`, `trade`) and `/market`
  for the rest (`aggTrade`, `markPrice`, `ticker`, `kline`, `!forceOrder@arr`),
  a connection receives only its own path's streams and silently drops the
  others, and a path-less URL is `/public`; the legacy path was retired on
  2026-04-23. The adapter now names each stream's path and the recorder gives
  every shard one path, filling live additions only into a shard of the same
  path; the tests fail without the change. Bybit is untouched. Deployed as
  `811e7335` at 21:38 UTC, one deploy, no rollback, both engines heartbeating
  on the commit within seconds. Ninety seconds in, Binance had 14 of 14
  shards connected and bytes on every feed class — trades, ticker,
  liquidations included — and 527 symbol directories in the hour where the
  earlier process had 30; Bybit 15 of 15 with 745. The host watchdog reports
  only the missing backup receipt.
- **2026-09-02 — Deployed `f17719d1` at 21:04 UTC: the tiered recorders on
  both venues, the live universe, the LLM gate on both realms, one profile.**
  The owner asked for the merge and the deploy in one go, and the freeze ended
  with it. One `scripts/ops.sh deploy`, no rollback: both signal workers and
  both engines heartbeated within seconds of their restart and report the
  commit; the funded engine came back with its rolling-loss trip still latched
  (until 2026-09-03 09:54 UTC), as expected. The Bybit recorder restarted on
  its fingerprint and the Binance recorder started for the first time. Sixty
  seconds in: Bybit 15 shards connected, 30 core names, 11 crowded (funding at
  or below -8 bp), 11 overheated (at or above +8 bp), 5 movers beyond the
  names other tiers already hold, 713 on the wide ticker; Binance 9 shards,
  20 core, 7 crowded, 2 overheated, 506 wide; no dropped frames on either. The
  windowed tiers (bursting, flooding, levering) show nothing until an hour of
  ticker history exists, by design. The host watchdog paged once during the
  rollout, while the Binance unit was still stopped, and the state backup's
  receipt is still missing: `liquidity-migration-backup.service` has been
  killed by its 15-minute start timeout on all three runs since the Drive
  backup shipped (the sources are about 1 GB, Drive already holds 920 MiB of
  them), so no engine-state backup has completed yet. Not fixed here.
- **2026-09-02 — Both realms run one thing: a live universe, the LLM entry
  gate on the native LONG sleeve, and one equity-following profile.** The
  owner's directive was that demo and the funded account run exactly the same
  strategies, that nothing is frozen or pinned, and that the LLM entry gate
  with its 4/12/24-hour triggers comes back. Three changes, one deploy.
  First, the frozen candidate-universe artifact is gone. The signal worker now
  derives the tradable universe itself on its hourly instrument cadence, from
  the realm venue's whole instrument list and the public ticker page: every
  trading USDT crypto perpetual is tradable; LONG's eligible set is the top 120
  by 24-hour turnover with a $2M turnover floor and a 30-day listing age,
  CARRY's the top 150 with a 7-day age; a member stays until it falls past rank
  160 or 200, so a name at the edge does not flap. Those dials live in
  `configs/signal-worker.<realm>.json`. A changed membership is one universe
  snapshot in the worker's input journal; the worker prunes what left, fetches
  history for what entered, and the engine keeps every held name's market
  subscription. The two hosts' frozen files had drifted nine days apart (demo
  frozen 2026-08-18, funded 2026-08-27; the LONG-eligible lists differed by 32
  names each way), which is exactly the divergence this removes. The freeze
  script, `liquidity_migration/data/candidate_universe.py`, the
  `--universe` argument, and `CANDIDATE_UNIVERSE_FILE` are deleted; a worker
  with no derived universe yet refuses every other input and resolves it before
  its lanes start. Second, the LLM entry gate is a live LONG trigger on both
  realms. The ledger's hourly publication (score at least 6 on the 4/12/24-hour
  windows, core ranks 1-10, wide 11-30, freshness veto, empty on regime off) is
  read by each worker every minute and handed to `long_native` as one
  `llm_gate_candidates` observation on the LONG source; the reducer enters a
  judged name at market as soon as it has a price, through the native sizing
  (BTC vol targeting from the worker's own daily bars, vol parity from the
  event's 30-day sigma), the 3-times-ATR stop and its decay, the three-day time
  exit, the cooldown, the capacity, and the one-minute admission budget. A name
  without measured volatility is refused; a trigger older than an hour or past
  the publication's validity is refused; a new publication replaces every gate
  candidate still waiting for a price or a slot. Entries carry the order-log
  tags `long-native-llm-gate` and `long-native-llm-gate-wide`, so the bands
  grade apart from native entries in the WAL's intent records. Gate settings
  sit outside the LONG decision fingerprint: the running checkpoints are kept.
  Third, `configs/operational.json` is the one profile for both realms
  (`operational.demo.json` and `operational.mainnet.json` are gone). Deploy
  renders it once from the dials in the funded credential file and installs the
  same bytes for each engine and worker; both engine templates now point at
  the rendered file. Demo's capital reference therefore follows its own equity
  (about $1,620 today) instead of a pinned $250,000: its gross cap becomes 5
  times equity, its margin cap equity itself, and its rolling-loss limit a
  tenth of equity, about $162, where it was $25,000 — one LONG stop-out can now
  trip demo for a day, exactly as it does the funded account. LONG and CARRY
  order sizes do not change on either realm; only the caps and the trip do.
  Nineteen new Rust tests cover the derivation, the hysteresis, the unresolved
  worker, the gate lane, the gate reducer path, and the single profile; the
  demo template's carry block now carries a zero capital reference like the
  funded one. Not a host change until the next deploy. That deploy is not
  reversible by `rollback` alone: the old worker binary refuses a checkpoint
  whose universe is not its frozen artifact, so a rollback of this generation
  must first move both signal-worker state roots aside
  (`/var/lib/liquidity-migration-signal-worker-{demo,mainnet}`) and let the
  old worker cold-start.
- **2026-09-02 — The fleet is back on the exact commit, after the deploy
  machinery refused it three times.** The fleet had been down 21h 40m, from
  2026-09-01 12:24 UTC to 2026-09-02 10:05 UTC. Deploying `5fc9d9e2` took
  three attempts, because cutting the deploy to the operations it performs had
  taken three things with it. The remote body runs over `bash -s`, whose
  working directory is the ssh login directory, and the environment installs
  `requirements.lock` without the project and sets no `PYTHONPATH`, so every
  `python -m liquidity_migration.*` in the deploy failed to resolve the
  package; the deploy now enters `REPO_DIR` once the checkout is at the exact
  commit. The funded takeover unsets `REAL_MONEY` and its reload allowlist no
  longer named it back, so the engine refused every funded state import, and
  the same allowlist had lost `BYBIT_INVENTORY_CREDENTIAL_SET`, which the
  Bybit gateway reads to choose its credential; both are named again, and a
  key absent from the credential file stays unset, so an unarmed account still
  refuses. Two tests in `tests/scripts/test_runtime_scripts.py` hold the
  working directory, the call order, and the allowlist, and both fail without
  the change. The engines now cap at 2 GB and report 217 MB and 303 MB in use,
  with no kernel kill and no restart; the funded engine's heartbeat names the
  installed commit. The account cost of the outage was one venue stop:
  HNTUSDT closed itself at 09:53 UTC, eleven minutes before the engine came
  up, for -14.97 USDT, which is -16.14 net of fees against a 12.72 limit and
  latched the rolling-loss trip until 2026-09-03 09:54 UTC — entries and
  growth refused, exits and cancels unaffected. Equity 127.18 USDT. The host
  gave back 115.9 MB of journals, about 30 MB of rotated logs, and 75 stale
  pre-activation heartbeats. The market tape's four recorded days moved into
  the hourly layout as `<day>.legacy.tar` under
  `LiquidityMigration/market-tape/bybit-linear/`, each archive verified
  against its source day, and the retired `forward-market` folder was emptied;
  `rclone purge` cannot remove the folder itself, because the remote is
  authorized with the `drive.file` scope and a folder delete needs write
  access to every child.
- **2026-09-02 — The recorders watch every side of the action.** The owner
  asked for capture wherever there might be an edge, not only where a sleeve
  acts today: positive funding, the day's movers, volume and volatility. Five
  live universe kinds join the recorder, all read off the ticker the wide tier
  already records: `funding_above` (the crowd fee at or above a line, longs
  paying up), `top_movers` (the biggest 24h moves either way, ranked with the
  same hysteresis as `top_turnover`), `price_burst` (a move of `pct` inside a
  window), `volume_burst` (the 24h turnover growing, inside a window, by a
  multiple of an average window's share — the hour trading far beyond the same
  hour a day ago), and `oi_change` (open interest up or down by `pct` inside a
  window). The windowed kinds compare against the recorder's own ticker
  history, one sample a minute kept as far back as the longest window. On the
  host, Bybit gains the `overheated` (+8 bp, 48 h), `bursting` (5% in an hour,
  6 h), `flooding` (three average hours of extra turnover in an hour, 6 h),
  and `levering` (10% open interest in an hour, 6 h) tiers, and `movers`
  becomes the day's ten biggest moves (leaving below rank 15) so its cost is
  bounded; Binance gains the same except `levering`, since it pushes no open
  interest. The budget sheds the short-lived tiers' deep books first, then
  their trades, then the core and crowded top of book. Not a host change until
  the next deploy.
- **2026-09-02 — The recorders follow the action live and keep to a byte
  budget.** The owner asked why deep capture waited for a daily snapshot to
  notice a crowded name, and pointed at the host's 4 TB a month line. Read on
  the host: inbound had been running at 74 GB a day (2.2 TB a month) with the
  81-name deep tier alone drawing 40 to 80 GB a day, and the wide tier's top
  of book and trades for 660 names, live for nine hours, had already written
  3.6 GB compressed — about the deep tier's whole day. Both recorders are now
  shaped around the ticker as the sensor: every listed name's funding, open
  interest, price, 24h turnover and change, and best bid and ask, pushed as
  they change, and cheap; the deep feeds go only where a sleeve acts. Four
  live universe kinds read that stream as it is written and promote within
  one maintenance tick, not at midnight: `top_turnover` (LONG's universe, the
  30 busiest names on Bybit and 20 on Binance, leaving only below rank 45 or
  30 so the boundary does not flap), `funding_below` (the crowd fee at or
  below -8 bp, kept 48 hours after it last was, so capture starts as the crowd
  forms before CARRY's -10 bp settled entry), `turnover_surge` (three times
  the day's snapshot, the HNT case, kept 24 hours), and `price_move` (fifteen
  percent either way, 24 hours). Promotion adds and removes topics on the open
  connections; a connection reconnects only when the venue drops it, and a
  REST book snapshot follows each live add on Binance in its own thread rather
  than inside the socket callback. The wide tier keeps the ticker and the
  liquidations and nothing heavier; the old symbol file becomes the pinned tier
  and names only the maker canary. Every received byte is metered per tier and
  per feed, and each recorder carries an inbound allowance for the month
  (1,300 GB Bybit, 1,000 GB Binance): when the projection from its last day of
  bytes runs over, it gives up the configured `tier:feed` pairs in order, one
  an hour — the movers' and surging names' deep books first, the wide ticker
  last — and restores them in reverse once under pace; the status file shows
  the bytes, the projection, and what is shed, the packer's receipt shows the
  month's upload bytes, and the host watchdog warns while a recorder is over.
  The ticker contract gains the 24h price change as a fraction. Not a host
  change until the next deploy.
- **2026-09-02 — The market tape becomes its own package, records Binance
  too, reads back as typed rows, and the host is frozen.** The owner's
  direction: stop mining the exhausted candle panel and build forward data
  capture we can make a strategy from. The recorder, the hourly Drive packer,
  and a new reader are now one standalone package, `market_tape/`, which
  imports nothing from the rest of the repository (a test enforces it) and can
  move to its own repository unchanged. A recorder runs from one TOML config
  (`deploy/capture/<venue>.toml`): a list of tiers, each a universe of symbols
  (`symbols`, `file`, `listed`, `top_turnover`, `funding_below`) and the feeds
  to take for them (`book:<levels>`, `trades`, `ticker`, `liquidations`,
  `kline:<interval>`, `open_interest:<seconds>`); a symbol in several tiers
  gets the union, each venue topic is subscribed once, and only the connections
  of a tier whose topic list changed reconnect. The Bybit host config
  reproduces the running recorder exactly — the symbol-file deep tier with
  50-level books, the crowded tier for names at or below -10 bp of funding, the
  wide tier of every other USDT perpetual — and
  `market_tape/examples/bybit-full-universe.toml` is the configuration for a
  machine with unbounded bandwidth and disk: one tier, every perpetual, every
  feed. The row contract is frozen in `market_tape/schema.py` (schema 2: every
  row carries `venue`; book rows carry the venue's own first and previous
  update ids); rows recorded before that read back with the venue of their
  archive. A Binance USD-M recorder joins as
  `liquidity-migration-forward-capture-binance.service`: the 60 busiest USDT
  perpetuals get the 1000-level diff book anchored by a paced REST snapshot on
  every connect, plus top of book, aggregate trades, mark and index with
  funding, the 24h ticker, and the all-market liquidation stream; the crowded
  and wide tiers mirror Bybit's. Binance publishes the last settled funding
  rate where Bybit publishes the upcoming one, so its crowded tier reacts one
  settlement later. The packer ships every tape in one run
  (`--tape NAME=ROOT`, landing under `LiquidityMigration/market-tape/<tape>/`)
  and skips a tape whose recorder has not started; the host watchdog reads
  both recorders' status files, the second one's alerts suffixed with its state
  directory; deploy fingerprints each recorder separately and restarts only the
  one whose inputs changed. Reading is the same package: `market_tape hours |
  rows | bars | book` over a host root, a directory laid out like the Drive
  folder, or `rclone:<remote:path>` through a cache; `market_tape.load`
  streams typed rows across symbols in receive order, `market_tape.book`
  rebuilds a book with each venue's own chaining rule (Binance's buffered
  snapshot recipe included), and `market_tape.bars` turns any row stream into
  fixed-interval bars. One small real hour of Bybit tape sits in
  `tests/market_tape/fixtures/` in both layouts with its expected numbers; that
  test is the frozen-schema regression. The study harness the closed programs
  used comes into the repository as `liquidity_migration/research/lab/`: the
  one-time input dumps, the daily panel, the fast numpy backtester, the
  per-trade overlay against a matched random-exit placebo, the five plateau
  checks, and the evidence-note renderer, plus `lab/tape.py`, which builds
  bars from either venue's tape and measures cross-venue lead-lag at any
  bucket size. The port was checked against the real artifacts: the
  backtester is bit-identical to the original on the 2,067 × 1,041 panel,
  the panel rebuild matches the original column for column, and the overlay
  reproduces every published exit-study cell (ETH-regime-off 20 trades
  +0.0183 t 1.95; funding ≥ 10 bp 13 trades +0.0186 t 1.53). Old script paths
  (`scripts/research/capture_bybit_forward.py`,
  `scripts/runtime/pack_market_tape.py`) still run the new code. And the host is
  frozen except for emergencies (`docs/operations.md` §Host freeze): every
  forward day of tape and of Lane-2 evidence is the scarce resource, and both
  fleet-down incidents of the previous two days came from deploy changes. Not a
  host change until the next deploy, which the owner runs; that deploy starts
  the Binance recorder.
- **2026-09-02 — The outside model hunt: fifty sources, thirty
  specifications on the Bybit panel, nothing new clears the bar.** The owner
  asked for the next step from outside the repository. Scouts read 22
  practitioner posts, 32 papers and 11 X threads with a stated rule and a
  number, and every
  replicable model was run on one point-in-time panel of Bybit USDT perpetuals,
  2021-01-01 to 2026-08-30, 1,041 names including delisted ones, funding
  settlement-exact, 7.78 bp per side: nine-lookback breakout ensembles with
  volatility targeting, time-series trend on the most liquid names at six
  lookbacks, EMA and Donchian rules, cross-sectional momentum at six lookbacks,
  8–10 week reversal, one-day reversal, funding factors both ways, a
  crowded-long short book, low-volatility, attention, open-interest growth, a
  market-state gate, and a BTC hedge on LONG. Best cells: 14-day trend Sharpe
  0.68 (t 1.6) and 14-day cross-sectional momentum 0.59 (t 1.4); the rest are
  dead or negative, and the published headline results (Sharpe above 1.5 on
  spot majors) do not transfer. Volatility targeting, the literature's
  drawdown tool, hurts both registered sleeves on their replications — CARRY's
  worst dip goes from −17% to −30% and its worst day from −7.7% to −23%
  because the scaler levers up in the quiet before each crowded-short event;
  the fixed multipliers are the lever, and their trade-off is recorded (live
  6.0 × 3.0: Sharpe 2.00, worst dip −46%, worst day −23%; half that: −25% and
  −12%). One internal lead: all of LONG's return sits in weeks when Bitcoin
  was up 4% or more (208 of 307 trades, +0.523 of +0.528; the 32 trades
  entered with Bitcoin down on the week lost −0.047, a result 0% of random
  subsets reproduce), yet the book-level gain from skipping or halving those
  entries is +0.02 to +0.05 units at paired t 0.7–1.8 — recorded as a Lane-2
  proposal for the owner, not adopted. Base rates recorded: funding half-life
  1.2 days; the most negative funding decile's price fall equals its funding
  received; CARRY's own cell nets +25 bp a day before costs; hour-of-day and
  weekday effects are not tradeable at the desk's costs; the K33
  negative-funding regime on Bitcoin replicates in direction over seven
  episodes and stays a base rate. Findings row in
  `docs/research/research_findings.md`; scripts, logs and the panel under
  `~/SHARED_DATA/bybit_full_pit/reports/external_model_hunt_2026-09-02/`. No
  dial, config or deploy changed.
- **2026-09-02 — Eight exit ideas tested, none survives its control; the
  recorder promotes crowded names into the deep tier.** An outside review
  proposed exits framed around continuation value: replace a held position
  when a blocked candidate is worth more, LONG horizons by entry thesis,
  renewal on a fresh signal, expiry on the signal clock, a CARRY continuation
  band, an Exodus microstructure cover, a pre-entry veto of premature Exodus
  fires, and maker-first scheduled exits. The registered v12 ledger was rebuilt
  with trigger legs and entry routes (307 trades, +0.528 book units), and every
  LONG clock variant loses both per trade and at book level with slots and
  cooldowns in place: signal clock +0.484, renewal +0.460, thesis +0.444,
  unconditional 96h +0.393 against v12's +0.528, the thesis rule indistinguishable
  from the same horizons dealt at random, renewal worse than random extension.
  The ten LONG slots refused one candidate in 5.7 years, so there is nothing
  to replace into. A walk-forward model of CARRY's remaining-day return on
  23,523 hourly states has out-of-sample correlation 0.04 and its policy never
  fires at one sigma. The Exodus fire population cannot be rebuilt faithfully
  from hourly data: against the venue's displayed rate on the tardis free days,
  the hourly proxy calls 7 of 49 fires falsely and inflates the premature share
  from 2% to 16%, so the veto question grades forward from the live WAL and the
  tape, not from history. Execution of scheduled exits was tried on the one
  hour of local book tape we hold (88 attempts) and grades nothing. Sixteen
  further LONG exits driven by market state rather than the trade's own P&L
  (BTC or ETH regime off, attention rank faded, name out of universe, funding
  crowded long, a reverse shock, a weak close) were graded the same way against
  a matched random-exit placebo: two cells, ETH regime off (20 trades) and
  funding at or above +10 bp (13 trades), beat the placebo but rest on one to
  three trades each, lose at the neighbouring threshold or with a one-day lag,
  and sit below the t 2.5 bar; nothing is promoted. Findings
  row in `docs/research/research_findings.md`; scripts, ledgers, and results
  under `~/SHARED_DATA/bybit_full_pit/reports/exit_program_2026-09-02/`. The
  market recorder now promotes any listed USDT perpetual whose funding rate is
  at or below -10 bp (the CARRY entry depth) into the deep tier for that day
  and the next (`--deep-funding-bp 10` on the unit), so the crowded names the
  CARRY and Exodus sleeves actually hold carry a 50-level book around their
  settlements; the promoted set is re-read with the daily instrument and ticker
  snapshot and listed in the recorder's `status.json`. Not deployed by this
  change.
- **2026-09-01 — The market recorder, its upload, and the backup stand apart
  from the trading fleet, and the fleet can roll itself back.** The fleet had
  been down since 13:32 UTC: the 13:30 deploy's demo engine was killed by the
  kernel nineteen times in a row at boot, and the old rollout then forced
  every unit stopped — including the recorder and the watchdogs, which had
  nothing to do with it. Measured on the host, a full replay of the demo log
  peaks at 1.57 GB of memory (322 MB for its newest 53 MB segment alone) and
  the funded log at 522 MB, against unit caps of 256 MB and 512 MB; neither
  engine could have booted. Both engine units now cap at 2 GB, sized to about
  six times the 256 MB rotation size. The fleet manifest gains a third
  lifecycle, `independent`: the recorder, the hourly market-tape upload, the
  six-hourly state backup, and a new host watchdog are never stopped by a
  deploy, a funded stop, or a disarm, and start at boot; deploy restarts the
  recorder only when its own inputs changed. Deploy records the commit whose
  deploy finished and the one before it; a realm that publishes no fresh
  heartbeat on a new commit is rolled back to the last finished one and the
  run fails visibly, and `rollback` is an operator mode (`ops.sh deploy
  rollback`, the CI dispatch choice). The backup, which had never run because
  its destination was unset, now snapshots the engines' logs, closed trades,
  heartbeats, worker checkpoints, target books, spools, takeover sources, and
  the two rendered engine configs locally and mirrors them to Google Drive
  (`LiquidityMigration/engine-state/latest`), moving changed or vanished files
  into a dated `history/` kept 60 days; it refuses any `*.env` source by name.
  The recorder rolls its files on the hour under `<day>/<HH>/<symbol>/`,
  spreads its subscriptions over several venue connections with backoff, adds
  a wide tier — top of book, trades, ticker, and liquidations for every other
  listed USDT perpetual, re-read daily — and writes a daily instrument and
  ticker snapshot; its memory cap rises from 512 MB, where it sat at peak, to
  1 GB. The Drive stops receiving hundreds of files an hour: each finished
  hour ships as one tar with a `MANIFEST.json` under
  `market-tape/bybit-linear/YYYY/MM/DD/`, checked against the Drive's hash
  before the hour is marked shipped; the four days recorded in the daily
  layout ship once as `<day>.legacy.tar`, and the old `forward-market` folder
  on the Drive is left for the owner to delete once they are there. The new
  host liveness scope pages on the recorder's own status (no frames, blocked
  storage, new drops, connections down), stale upload or backup receipts, a
  Drive short of space, disk, and the host clock; the realm scopes no longer
  watch shared units, disk, or the clock, so one cause pages once. Every
  engine build is stamped with its git commit: the log's Boot record and the
  heartbeat (`engine_commit`) name it, and the venue-confirmed accounting tool
  binds each graded fill's Boot to the expected commit and config hash in
  place of the retired seven-field activation receipt and the binary digests;
  logs from builds before the stamp cannot reach the label. On GitHub, `main`
  now requires a pull request with green `ci` and `rust` checks, linear
  history, and no force pushes or deletion; secret scanning, push protection,
  and vulnerability alerts are on. Not a host change until the next deploy,
  which the owner runs. That deploy starts the funded engine, because
  `REAL_MONEY=true` is present in the funded credential file.

- **2026-09-01 — The engine refuses new entries after a losing day of its own
  trades.** On the owner's instruction, an emergency last resort replaces the
  daily-loss halt retired on 2026-08-20, built without that halt's two faults.
  It reads only this engine's own closed round trips, valued as exit against
  entry minus venue fees, so the owner's hand trades on the same account
  cannot trip it; and its limit is a share of the capital reference
  (`account_risk.max_rolling_loss_fraction`, 0.1 in both profiles), so on the
  funded account it follows equity instead of sitting at a flat dollar figure.
  Once the trades closed inside any rolling 24 hours sum to that loss or
  worse, every entry and growing resize is refused with `RollingLossTripped`;
  exits and reductions pass, nothing needs resetting, and the trip clears on
  its own as the losing trades pass 24 hours of age. A restart rebuilds the
  window from the log's fills and a log rotation restates the in-window
  trades in the new segment's base, so a restart never clears it. At today's
  dials the limit is $10 on the funded account (reference $100) and $25,000
  on demo (pinned reference $250,000, far above anything the demo book loses
  in a day); the worst funded day in the log so far, 2026-08-28, lost $6.84.
  Funding and open positions are not in the sum; a trade whose opening fills
  are in a rotated-away segment cannot be priced and is not counted. Building
  it exposed a second fault: a venue stop firing arrives as a fill with no
  order id of ours, and the engine charged it to nobody, latched itself out
  of opening, and never recorded the loss — the one loss a loss limit most
  needs to see. Bybit rows now carry the venue's own reason (`createType`,
  `stopOrderType`, `execType`: stop, take-profit, liquidation, auto-deleverage)
  as `forced_close` on the fill, and such a fill is charged to the one sleeve
  whose claim on the symbol it reduces, priced as that sleeve's exit, and does
  not latch the engine; every other unowned fill stays a stranger's and
  latches as before. The same rule runs on replay, in boot reconciliation, and
  in gap recovery, so a restart after a stop-out reads it the same way. The
  funded log holds no live unowned fill to date (its 377 blank-id rows are all
  recovered hand trades), so the new path is exercised by fixtures built from
  Bybit's documented rows, not yet by a real stop. The operational profile is
  schema 3 with the new key, both templates are re-rendered to the new profile
  hashes, the funded renderer gains the dial `RM_ROLLING_LOSS_FRACTION`
  (default 0.10), the heartbeat reports the window (24-hour net, limit, trade
  count, tripped), and fleet liveness pages when the trip is on. CI and
  `dev.sh check` run rustfmt, clippy, and ShellCheck; both engine and Python
  suites pass. Not a host change; the next deploy carries it.

- **2026-09-01 — CI runs the Rust format and lint gates it documented.**
  `docs/engine.md` had told developers to run rustfmt, clippy with warnings
  denied, and the tests; the workflow and `scripts/dev.sh check` ran only the
  tests. Measured on the pinned 1.90.0 toolchain, rustfmt failed on three
  hunks in the runtime-control spool and clippy failed on two boolean
  expressions (`nonminimal_bool`) that the newer Homebrew clippy on the
  development machine accepts — the local cargo has no rustup and ignores
  `rust-toolchain.toml`. Both are fixed; the rewrites are semantic no-ops. The
  `rust` CI job and `dev.sh check` now run rustfmt and clippy before the tests,
  and the `ci` job and `dev.sh check` run ShellCheck at warning level over
  every tracked shell file (new `dev.sh shellcheck`). ShellCheck found six
  items: two sourced libraries without a shell directive, a mis-spelled
  directive in the backup script that disabled nothing, two unused locals in
  the Telegram helper, and a false-positive export warning; all are fixed
  with no behaviour change. `cargo audit` over the lockfile reports no
  advisories today; it is not a CI gate, because a new advisory in a
  transitive crate would block an urgent deploy the same way the retired
  activation machinery did. An outside review that prompted this pass also
  asked for a bot-attributed multi-level loss circuit breaker, a signed
  build-once artifact pipeline, Prometheus-style observability, a continuous
  double-entry ledger service, explicit strategy UUIDs, infrastructure-as-code
  for the host, and a pre-activation shadow comparison. None of those is
  built: the loss halt was removed on the owner's instruction on 2026-08-20
  and stays a proposal; the artifact pipeline re-creates the receipts and
  digests cut this morning; the rest is operating surface out of proportion to
  a one-host, two-account fleet. The same review noted that the venue
  confirmed accounting tool still needs an activation receipt no deploy
  writes; that remains the open owner decision recorded in `STATE.md`. Not a
  host change; the next deploy carries the two Rust rewrites.

- **2026-09-01 — The deploy machinery is cut to the operations it performs.**
  The audit found roughly twenty thousand lines of guards, gates, receipts,
  and proofs around a deploy whose real work is: fetch a commit, build, copy
  files, restart units. That machinery had kept the armed fleet down for days
  — dozens of failed activation attempts since 2026-08-28, ending in a staged
  install refused outright because the host holds funded configuration.
  Removed: the trusted runtime launcher and its permits, watchdog leases, and
  activation receipts (every unit now ExecStarts its real committed command);
  release markers and digest re-verification in the deploy, the operator
  router, and the Telegram helper; the install/activate/staged/rollout mode
  split and the funded-host refusal; topology snapshots, boot fences,
  quiescence proofs, quarantine inventories, and the sandboxed builder; and
  the liveness checker's identity re-proving. The deploy script is now one
  `deploy` mode plus read-only `verify` and the funded `stop-mainnet` /
  `disarm-mainnet` safety stops, at about a tenth of its size. Kept: the
  `REAL_MONEY` arming switch and funded preflight, exact-commit binding with
  the on-main ancestry check, state takeover, the engine's WAL and lease
  contracts, pinned CI SSH identities, the sudoers boundary, and the
  always-available disarm. Root SSH access and the pushed `main` branch are
  now the stated security boundary. Liveness pages on inactive units, stale
  heartbeats, a cannot-open engine, disk, backups, and host clock — not on
  hash identity. The venue-confirmed accounting tool still consumes a
  deployment-time activation receipt; future generations do not produce one,
  and that contract is an open owner decision.

- **2026-09-01 — A refused runtime control retires instead of wedging the
  engine.** The final control audit found that a durable control request the
  engine would never accept — unreadable bytes, an envelope from another
  schema generation surviving an upgrade, or a semantically stale command
  such as one naming an unconfigured sleeve — stayed in the spool while the
  refusal killed the process, so supervised restart re-read the same file and
  the engine restarted forever. The spool now quarantines any unreadable file
  as `<name>.rejected` and keeps polling, and the core refuses a semantically
  stale request by retiring it through the feed's reject path and continuing
  to run; the refused bytes stay on disk beside the spool for inspection.
  Accepted requests keep the exact WAL-barrier-before-retire contract. The
  operator CLI now reports a rejected request as an error naming the
  quarantined file instead of printing "durable and applied", and
  resubmitting the exact refused bytes clears the stale marker so the fresh
  verdict is the one reported. WAL replay of already-accepted requests is
  unchanged and strict.

- **2026-09-01 — Signal-worker environment projections stay root-only.** The
  deploy writer installs each generated worker environment as `root:root`
  mode `0600`, matching the strict loader used during activation. Systemd
  reads the file before dropping to the credential-free worker identity; the
  separate universe and operational-profile inputs remain group-readable.

- **2026-09-01 — Exodus takeover preserves the retired Python tape bytes.**
  The stopped-state codec keeps Python's exact finite-number spelling while it
  checks the CARRY event ID and tape hash. The compatibility parser is confined
  to this legacy source; ordinary engine and WAL JSON retain their existing
  number representation. Compact layout, sorted keys, exact schemas, semantic
  identities, and the full hash chain remain required.

- **2026-09-01 — Funded native takeover can use the installed execution
  credential without copying secrets.** The account probe remains a read-only
  Rust type with no order or account-mutation method. It prefers the optional
  globally read-only attestor when present and otherwise selects the existing
  funded environment explicitly. The armed rollout validates that selected
  file before stopping the incumbent, uses the host Python for that early
  private-environment read, sends the exact candidate environment loader with
  the remote rollout controller, and passes the exclusive account ID into
  every takeover command. Linux runtime-supervisor fixtures now substitute the
  current ownership comparison syntax, and the frozen-topic WebSocket test no
  longer assumes ordering between independently handed-off initial quotes.

- **2026-09-01 — Directional sleeves become perpetual across source and restart
  boundaries.** The credential-free worker replaces cycle-owned Bybit clients
  with one persistent public WebSocket actor plus independent bounded
  instrument, funding, candle-repair, and whale lanes. Subscription epochs,
  fresh ticker coverage, checked-through candle frontiers, market-only retry
  clocks, timed same-socket topic re-probes, endless capped reconnects, and REST
  repair keep accepted topics live and account for every eligible symbol as a
  feature row or explicit rejection. The engine also re-subscribes an
  individually silent top-of-book topic without disrupting healthy symbols. Cold
  acquisition is profile-scoped and chunked. Accepted lookbacks and every
  fetch page have hard row ceilings; each lane waits for the prior durable
  commit before retaining another result. Malformed, off-grid, revised, or
  out-of-range venue rows fail only their source lane before mutation, while
  sequence, state, spool, serialization, and disk failures remain process-fatal.
  Frequent source events use a
  bounded append journal between streamed checkpoint compactions instead of
  cloning and rewriting the whole history every five seconds. LONG and CARRY
  persist the registered one-minute admission budget across boot, market, and
  retry wakes; CARRY also preserves cross-sectional entry ranking and spends a
  slot only when the shared order planner can emit an opening order. Missing
  prices, instrument rules, and venue-minimum failures remain retryable without
  starving a lower-ranked viable entry. A monotone availability clock bounds
  every source prune, so an older parallel response cannot delete newer candle,
  funding, instrument, or whale state. Current
  outputs coalesce and republish after a stalled consumer drains; lifecycle and
  scorer catch-up records keep separate quotas, and class-specific pressure is
  a critical liveness fault even below the total spool cap. Launch and delivery
  clocks bound historical acquisition. An invalidated private account view and
  a durable opening timestamp ahead of a rolled-back wall clock block growth in
  every directional sleeve while exits and reductions continue. Exodus keeps
  transiently blocked handoffs pending and schedules their retry and deadline.
  The maker recovers its orders on boot and drains attributed inventory only
  when quoting is globally disabled or that symbol is retired; a refused drain
  retries on a bounded timer instead of immediately looping.
  Rollout stops the validated installed-plus-candidate fleet union, migrates
  reviewed universe bytes atomically, imports the exact retired CARRY and
  Exodus state formats, and binds root-owned takeover files to their checked
  inode before import. An armed rollout validates the separate mainnet attestor
  file before it snapshots or stops the incumbent. Signed venue accounting
  binds every fill to its engine
  boot and order boot, applies durable dropped claims, requires the exact
  seven-field activation receipt for the engine and signal-worker generation,
  rehashes both deployed binaries and the engine config against independent
  rollout digests, and rejects account-history captures whose endpoint,
  parameters, user,
  server-time window, or retention boundary is incomplete. Worker liveness
  pages producer, LONG, CARRY, spool,
  transport, and memory faults independently, validates the exact heartbeat
  schema and feature hashes, and pages at spool refusal boundaries. This
  repository change does not deploy or arm either account.

- **2026-08-31 — The active fleet has one native directional path.** Python
  producer daemons, target-book diagnostics, one-way schema migration tools,
  dedicated Python decision-contract launchers, retired unit tombstones, and
  their tests are removed. The fleet manifest lists active units only. Signal
  worker identities and environment files use their runtime names throughout
  deployment and funded-arming checks. The stopped-state importer remains the
  one takeover path until the Rust WAL contains complete native checkpoints.
  The registered Exodus rule now names the native CARRY-event trigger, native
  cover reducer, and shared Rust replay; these wording changes alter its
  registered byte hash and the renderer/fixture identities derived from it.
  Native reducer and input-contract faults now have their own typed engine
  heartbeat rows and page through fleet liveness; ordinary per-symbol entry
  blockers remain trading state and do not page.

- **2026-08-31 — Directional live decisions move into the Rust account owner.**
  One credential-free Rust signal worker per realm now acquires and persists
  the public LONG/CARRY inputs, publishes crash-atomic immutable observations,
  and reports exact source, feature, universe, and engine-config identities.
  The engine makes each observation durable before waking typed native LONG,
  CARRY, and Exodus reducers. CARRY's pre-settlement handoff is an internal WAL
  event, and every reducer owns a strict whole-sleeve checkpoint, restart-safe
  entry permission, and durable flatten path. A shared persistent Rust replay
  adapter is the research decision authority for all three sleeves. The
  standard CARRY v7 curve sends its backward-only feature frame through the
  native signal batch, including Rust top-N selection and daily weights; the
  Python daily scorer remains only for labelled v1-v5 reference comparisons.
  The mainnet maker rule is rendered from registered JSON by the Rust config
  renderer; its quote reducer remains disabled. `touch_sniper` keeps its
  restart-safe reducer but remains outside deployed templates. Six Python
  directional services and their runtime wrappers are retired from the current
  fleet. Rollout renders exact native configs only after installing the trusted
  Rust release, imports a complete account-bound legacy state bundle while the
  WAL and account are locked, and refuses partial, conflicting, corrupt, or
  wrong-account state. Telegram pause/resume and account flatten use durable
  engine controls while the signal workers keep running. Notifications read
  actual attributed positions and closed trades rather than target files. This
  repository change does not itself deploy or arm either account.

- **2026-08-30 — Strategy decisions and fleet identity gain one contract each.**
  Native LONG live and research planning now call the same pure typed reducer
  for signal, sizing, entry, stop and time exit; the contract has no
  take-profit. One typed effective config records field-level provenance, and
  operational profiles are the sole live sizing source. The hourly runner is
  explicitly diagnostic, while the new one-minute live-physics runner adds
  causal wakes, fill-anchored clocks, current target and capital-reference
  deadbands, fees and funding and labels its result a minute execution bound.
  Separate candidate-window mark-price and traded-price tapes preserve the
  live trigger/fill split; funding value uses the settlement mark. Each minute
  report freezes the exact local research/live source closure and
  runtime versions behind a recorded SHA-256, so a dirty exploratory tree is
  still identifiable. The LLM ledger is research-only and no longer feeds any
  Native LONG target path. CARRY now resolves its data root and private
  state/tape paths in the typed effective config. One pure lifecycle reducer
  owns sizing anchors, settled and pre-settlement exits, next-day drops,
  admission, entry caps and exact target bytes; live and historical replay
  call it. Historical replay carries modeled holdings, wakes deferred
  admission on the configured cadence, and applies standing targets at hourly
  marks while naming the assumed-fill boundary. It durably appends each
  hash-chained Exodus handoff, persists the
  reduced state, then publishes the exact book. A shared Python/Rust fixture
  fences those bytes and the $6 entry plus $1/5% resize boundaries, including
  current-mark valuation. Independent Exodus producers consume the handoff
  tapes, call their own typed reducer, own their state and books, and replay
  checked-in entry, restart and cover cycles with exact staged/final state and
  target bytes. The registered Exodus evidence now names that replay and says
  plainly that its discarded scratch economics cannot be reconstructed.
  The Rust quoter now puts signal decay, fair value, inventory, directional
  protection, venue minimums and quote effects behind one pure reducer used by
  both the live plug and Python-driven replay. Its mainnet economic block is a
  generated region of the registered JSON rule, and funded config installation
  refuses drift before copying it. `touch_sniper` now has a typed reducer and a
  fingerprinted WAL checkpoint restored with attributed position and owned
  orders; its consumed arm is durable before entry and survives WAL rotation.
  A second durable latch records an exit request before cancellation or close,
  resumes partial-entry cleanup after restart, and reconciles uncertain saved
  state with surviving attributed risk toward flat.
  A durable per-sleeve target
  latch now prevents a stale nonzero book from reopening a position that a
  venue-native stop just flattened, across live callbacks, restart, and WAL
  rotation; an explicit zero clears it. `deploy/fleet_manifest.tsv`
  is the canonical inventory for lifecycle order, activation, timers, operator
  policy, dependencies, health and runtime artifacts, including both engines,
  backup and chaos drill.
  LONG PIT taint now follows the exact causal input window: the signal start
  minus the 90-day maximum feature lookback through the last daily source bar
  admitted by the end-exclusive signal clock. Whole-root coverage remains a
  separate receipt, so unrelated stored dates neither bless nor taint a scoped
  replay.

- **2026-08-30 — Binance gains a fenced adapter, and the LONG evidence program
  stops overstating what it saw.** The Rust engine now compiles six venue
  families and ten exact realms. Binance USD-M has
  account-alias and one-way-mode checks, current routed public/private sockets,
  Algo stops, an explicit refusal of incomplete account-wide execution
  recovery, symbol-scoped execution IDs, and complete top-20 snapshots; both
  its testnet and mainnet remain production-blocked because no signed
  protective-stop lifecycle ran, ambiguous entry and stop HTTP 503 outcomes
  are not reconciled, and partial fills can fall below the market-exit minimum.
  The drawdown-week
  checker now reconciles explicit venue settlements, keeps a prior live
  PUMPFUN position apart from a missed execution, and reports ENA as
  ungradeable while AAVE is -922.79 bp all-in. The tape grader declares each
  input as a registered trade, tape proxy, or artificial exercise; this sample
  contains zero registered model rows. The durable findings restore the
  Binance carry replication at +10.1 bp/day over 1,756 seen days. Forward
  capture starts with 81 symbols: the existing maker/saved-L50 set plus LONG's
  top 50 by 90-day median daily turnover and a ten-rank buffer. Private
  research inputs stay outside Git in a mode-0600 local evidence archive.
  The pre-push gate clears checkout-local Git environment before tests create
  throwaway repositories, so a linked-worktree run cannot write fixture
  identity, refs, or index state into the caller's repository. The parity and
  tape checkers clear the same bindings before reading commit identity, so an
  explicit foreign checkout cannot resolve against the caller instead. The
  operational rollout installs the commit carrying this entry and restarts the
  widened recorder with the rest of the managed fleet.

- **2026-08-30 — The gate grows a wide band, labeled apart (owner
  directive: "wire it in for demo and add its own label").** The trigger
  scan now reads turnover ranks 1–30: ranks 11–30 are judged and published
  under the same score ≥ 6 bar and freshness veto but carry `band: "wide"`,
  and the LONG producer labels those entries `llm_gate_wide` in its state
  and transitions log, so the cohort's fills grade apart from the core
  rank ≤ 10 band. The measured motivation is the HNTUSDT case and the rank
  barrier's price (research_findings §the rank barrier: the 11–30 pool is
  9% graduating monsters, 91% junk averaging −126 bp/trade; entering HNT
  waited 8 hours and +47% for rank 9). The wide band is the judged attempt
  at that separation; its labeled forward record is the only evidence that
  can move the core cut. `TRIGGER_ROWS_MAX` 10 → 20 so a hot hour cannot
  starve the wide ranks out of the journal. Demo only — the mainnet unit
  still carries no gate configuration. Six new tests; the four that pin new
  behavior proved to fail without the change. This commit is the wide
  band's change point.

- **2026-08-30 — The whole-repository audit removes silent ambiguity at the
  inputs, order path, account edge, and deploy edge.** Market numbers now
  reject NaN, infinity, non-positive prices and invalid sizes at one shared
  boundary; funding, rolling clocks, WebSocket liveness, LONG admission,
  historical entry prices, financed cash, carry re-entry, and terminal
  liquidation all have direct regression cases. The Rust adapters validate
  fill identity and quantity before changing state, start scans at the real
  subscription boundary, parse venue numbers and cursors strictly, keep empty
  execution checkpoints durable, and preserve unknown fee truth. WAL formats
  remain backward-readable. Deploys bind one fetched commit and one SSH host,
  stop children with a measured grace, join timer triggers to their service
  invocations, keep notification retries durable, and make flattening refuse
  stale or partial account evidence. A new demo-only `engine canary-order`
  takes the account lease, verifies the authenticated UID, sends one protected
  minimum PostOnly order, requires exact terminal status and venue-clock
  execution history, and reconciles any ambiguous fill through one full close.
  The Bybit archive repair recovered 1,180 of 1,454 thin or missing symbol-days;
  strict full-PIT coverage still fails on 265 listing-inferred empty days and
  nine official gzip objects that decompress to zero bytes. Those 274 rows stay
  visible and required. Dependency locks were rebuilt and matched, both Python
  and Rust advisory scans found no known vulnerability, and the complete check
  finished with Ruff, mypy, 1,792 Python tests and the full Rust workspace
  green.

- **2026-08-30 — The gate stops chasing old moves, and every LONG entry
  says where it came from.** Two changes from the give-back program's live
  receipts (research_findings §LONG give-back). First, the LLM gate's
  freshness veto: a trigger whose name the ledger already flagged on two or
  more distinct earlier UTC days within the last four is journaled in full
  (`freshness_veto`, `prior_flag_days`) and never published — the AAVE
  2026-08-24 loss was exactly this chase, scored 7 on its third consecutive
  mover-day after a +45% three-day run and bought within 2.5% of the top.
  `--grade` buckets vetoed rows separately, so the ledger carries the veto's
  own forward A/B. Second, both LONG producers append an enter/leave
  attribution line per book transition (`LONG_ENGINE_BOOK_TRANSITIONS_PATH`,
  `targets/long-{demo,mainnet}-transitions.jsonl`) carrying the entry's
  pattern (`llm_gate` vs native `fomo_chase`); since the 2026-08-24 merge no
  close record could say which entries were the gate's, and this log is the
  durable split. A failed append warns and never stops the cycle. Six new
  tests, each proved to fail without its change. This commit is the veto's
  change point. Deployed the same night via `ops.sh deploy rollout` at
  `d673b578` (verify-ok, whole topology active): the demo engine's
  `SuccessExitStatus=143` is loaded and a live restart logged `Deactivated
  successfully`; forward-capture restarted onto the near CloudFront edge at
  00:57 UTC; both heartbeats healthy with `may_open: true`. Three agents
  were operating this repo and host concurrently that night — a competing
  rollout was detected by its held maintenance lock and waited out, never
  broken.

- **2026-08-30 — The forward tape has an off-box home.** An hourly uploader
  sends only completed `.zst` segments to Google Drive, checks each new batch
  before advancing its local ledger, and leaves a SHA-256 batch list beside
  the remote files. Open `.partial` segments, account WALs, credentials and
  environment files never enter this path. The first live object matched the
  VPS copy in both size and MD5; the Drive account reported roughly 5 TB free.

- **2026-08-30 — A commanded stop is now filed as one.** Teaching the engine
  to answer SIGTERM was only half of it, and the deployed binary proved it:
  every unit runs under the trusted supervisor, which forks its workload
  rather than replacing itself with it, so bash is systemd's main process. It
  answers a stop correctly — SIGTERM to the child, wait, escalate — and then
  exits 143. systemd's default success set is `{0}`, so every `systemctl stop`
  was filed as `Failed with result 'exit-code'` and paged the alerts line on
  every deploy. The eight long-running units now carry
  `SuccessExitStatus=143`, and a posture test holds every `Type=simple` unit
  to it and to the supervisor still exiting 143.

- **2026-08-30 — Every Telegram message is one monospace block, and the
  canary is off the phone.** Builders write plain text; `as_block` escapes it
  and wraps it once at the send, so trade updates, the daily summary, watchdog
  alerts and the engine digest all arrive as a block that copies in a tap and
  keeps its columns. The prose that explained itself is gone — the funding
  caveat, the reason a close could not be priced — and lives in
  [docs/notifications.md](docs/notifications.md) where it is read once instead
  of every day. `maker_canary` exercises the order path rather than earning,
  so its closed trades now reach stdout and journald only: no message, no row,
  and no part of the day's trip count or total. A day that was reported as
  "14 trips · none won · -$10.92" reads as "2 trips · none won · -$10.74".

- **2026-08-30 — The fleet was reaching Singapore by way of another
  continent.** Bybit is served through CloudFront, which picks its edge from
  the resolver the query arrives on. The box resolved over IPv6, has no
  working IPv6 egress, and was handed an edge 206 ms away; the same resolver
  asked over IPv4 named the Singapore edge 2 ms away, minutes from the box.
  Every REST call, every market message and every order had been paying that
  detour. The box now resolves over IPv4 only
  (`/etc/netplan/99-dns-ipv4-only.yaml`) and prefers IPv4 addresses
  (`/etc/gai.conf`), and the whole fleet was restarted onto the near edge.
  Measured on the box, before against after: edge ping 206.4 ms against
  2.0 ms; a full API call including the TLS handshake 858 ms against 26 ms;
  the trade socket's connect-to-authenticated round trip 429 ms against
  3.2 ms; a producer's restart-to-first-completed-cycle 30-100 minutes
  against 11 seconds. Both engines ran on this box at the same moment on
  either side of the change, which reads the difference directly:
  `venue_clock_offset_ms` -227 on the far edge against -21 on the near one.
  `forward-capture` was left running and still holds a far-edge socket.

- **2026-08-30 — The LLM driver ledger can import its own package.** The
  wrapper runs each script by path, so Python puts the script's directory on
  the import path rather than the repo root, and the package is not installed
  into the venv. `llm_driver_ledger.py` imported `liquidity_migration` without
  first putting the root on the path, so the unit had been failing at import
  and collecting nothing. It now bootstraps the path the way the other
  dispatched scripts do, and a test holds every script the wrapper can
  dispatch to that rule.

- **2026-08-30 — A systemd stop is a stop, and a stuck strategy writes one
  note.** The engine waited on SIGINT for its shutdown, and systemd stops
  services with SIGTERM, so every deploy killed it: the log's buffered tail
  never reached the OS, the account lease was dropped by process death rather
  than by hand, and systemd recorded the clean stop as `Failed with result
  'exit-code'` on status 143, which paged the alerts line. It now waits on
  both signals and exits zero. Separately, a refusal wrote a WARN and a WAL
  note every time, so a position whose protection refused each new entry wrote
  one per quote — 3110 of them in a single episode on 2026-08-29, into the log
  the fill and latency reports read. An unchanged refusal is now recorded
  once a minute with a count of what it stood for; what the engine refuses is
  unchanged.

- **2026-08-30 — The forward tape records the fast touch.** The recorder now
  keeps Bybit's 10 ms L1 snapshots beside its L50 book, trades, ticker and
  liquidation feeds. Every book row names its depth; L1 and L50 keep separate
  update histories and retain the venue cross-sequence that orders the feeds.
  A 15-second, 34-symbol live probe measured 14.5 KB/s of added raw WebSocket
  payload against 96.3 KB/s for the existing feeds.

- **2026-08-30 — The first toxic-flow canary stopped early, and whole-position
  dust can now close.** The registered run produced 10 new attributed fills,
  all maker: 8.81 bp all-in arrival cost and -14.52 bp signed one-minute
  markout. That is adverse but far too small to grade the rule. The run was
  stopped before its 30-fill-or-60-minute boundary when it left 10 AGI that
  the normal quantity/value checks would not submit. The quoter is disabled.
  For a venue that states this capability, the engine now recognizes only an
  exact, reduce-only, market exit for the whole fresh position as a below-minimum
  close. Bybit renders that request as `qty=0`, `reduceOnly=true`, and
  `closeOnTrigger=true`; the durable request keeps the actual quantity for
  accounting. Partial dust exits and malformed full-close requests remain
  refused.

- **2026-08-30 — Execution-health telemetry reaches the live heartbeat.** It
  now states p99 disk-wait residue, p99 request-quota hold, accepted amends
  confirmed versus pulled after the venue stayed silent, private-stream resets
  including the initial subscription, and venue clock minus host clock. The
  clock sign is pinned by a direct test so a positive number means the venue
  is ahead, matching the field's words. Each Telegram-enabled scope sends one
  plain digest per UTC day from these fields and retries until delivery; its
  day marker is reserved watchdog state, not an alert cooldown. Optional host
  clock and off-box-backup-stamp checks remain off until configured.

- **2026-08-30 — Deploys stop paging the alerts line.** The night's traffic —
  ten of twelve messages — was one alert churning: "producer restarted but has
  not completed a checkable cycle", CRITICAL, on every producer after every
  deploy, clearing itself 30–100 minutes later. The diagnosis: a producer's
  first completed cycle after restart pays the boot kline backfill
  (`bootstrap_timeout_seconds` budgets 20 minutes, and four producers on one
  box contend for the same REST budget — near 100 minutes observed after a
  full-fleet deploy), while the watchdog's startup grace reused the 10-minute
  steady-state freshness dial. Warming up read as hung. The fix gives startup
  its own physics: `--max-startup-min` (default 120) covers a producer that is
  verifiably active in its current systemd generation, silently; past it the
  page now says "up N min without completing a cycle — past the startup
  budget, so this is a hang, not a warmup". A dead or failed unit never gets
  the grace — unit-state checks page those within minutes, which is what makes
  the long budget safe. The regression test encodes last night's exact spam
  shape (45 and 100 minutes into boot, current generation, no receipt → no
  alert) and fails under the old grace.

  The daily digest also gains the engine's uptime, because every counter in it
  is since-boot and "fills 0" two minutes after a deploy was reading like a
  dead day.

- **2026-08-30 — The debugging channel gets one engine-health line a day, and
  the small hygiene lands.** Built and tested; the next deploy enables it.

  The heartbeat now carries the numbers this week's execution work created:
  how long the order path actually waited for the disk (`barrier_wait_p99_ns`)
  and for the request quota (`quota_hold_p99_ns`, a new ledger segment recorded
  at every order/cancel/amend completion), amends priced by the venue against
  amends pulled unanswered, private-stream resets, and the venue clock offset
  measured off the freshest quote with both clocks sampled together. The
  heartbeat's exact-keys test pins all six.

  Each liveness unit posts one plain-text digest per UTC day on the alerts
  line, built from that heartbeat: standing, equity, fills with maker share
  and slip and markout, submit and round-trip times, the two pacing numbers,
  the amend outcomes, and the clock offset. Absent fields print as dashes,
  never as confident zeros. The day advances only on a delivered message, so
  a failed send retries next run; a broken gate in either direction is pinned
  by a main-loop test proved to fail both ways. The hourly digest stays dead —
  this is daily, and `--no-daily-digest` turns it off per scope.

  The hygiene: the demo watchdog passes `--host-clock-check` and pages when
  `timedatectl` says the clock is undisciplined (one scope per box, so one
  cause pages once); `backup_state.sh` plus a nightly timer rsync the WALs and
  trade files off-box and touch a stamp whose age the watchdog alarms on —
  armed only once `backup.env` and `LIVENESS_BACKUP_STAMP_FILE` are configured,
  so nobody is paged about a backup they never set up; and `chaos_drill.sh`
  plus a Sunday timer kill the demo engine weekly and report clean/latched/
  did-not-return on the alerts line. The drill is hardwired to the demo unit,
  a test forbids the funded unit's name from appearing in it, and its timer is
  deliberately not Persistent — a box booting after a real outage has just had
  its recovery exercised and does not need a rehearsal on top.

- **2026-08-29 — The maker protects only the side aggressive flow is attacking.**
  Public trade notional is divided by displayed same-side dollars within a
  volatility-expanded near-touch band, then carried in 250 ms and 3 s decays.
  Buying widens or pulls only the ask; selling does the same only to the bid.
  Every attributed fill records both flow states, the combined score, nearby
  depth, spread, movement, and estimated queue beside its execution id. The
  34-name, two-day paired queue replay chose four basis points of widening per
  score over the fee-corrected control: +0.076 bp per markable quote, paired t
  11.75, with the improvement present on both dates. The selected arm still
  loses -0.171 bp per quote after the full fee assumption. It is registered as
  `lane2_toxic_flow_quoter_v1` for a minimum-size 30-fill-or-60-minute funded
  trial, not promoted as profitable.

- **2026-08-29 — Forward public market capture is an owned service.** A
  no-credential unit records Bybit L50 snapshots/deltas, public trades,
  mark/index price, the crowd fee (funding), open interest and liquidations
  with both venue and local receive times. It rotates per-symbol raw segments,
  atomically installs a `zstd` copy only after decompression verification,
  writes its SHA-256 receipt, and only then removes the raw bytes. Recovery
  keeps complete JSON lines after interruption. Retention removes completed
  compressed segments after 30 days, above 60 GB, or to preserve 25 GB free;
  disk pressure counts dropped frames without traceback spam. The live smoke
  captured book, trade and ticker rows with no writer-queue drops.

- **2026-08-29 — The disk barrier runs beside the send instead of in front of
  it.** The order path waited out a full `fdatasync` — ~2.2 ms on the VPS,
  3.95 ms measured here — before a single byte left, and the fsync was
  comparable in size to the venue round trip it was blocking. It now starts at
  the same moment the order is dispatched: the bytes are with the operating
  system before the send, and the disk's confirmation is awaited by the first
  news that the order traded, never by the send. On a venue milliseconds away
  the barrier finishes during the flight, so that wait is nothing; `still
  waiting on the disk` is the new ledger segment that measures the residue.
  Measured with `engine bench --venue-delay-ms 4` — a new flag that holds the
  pretend venue at a real venue's distance, which a localhost socket cannot
  model — the same binary with one line changed goes from 9.59 ms to 6.01 ms
  p50 message-to-submit-result, and 13.69 ms to 6.31 ms p99. The tail moves
  further than the median because a slow barrier used to stack on top of the
  round trip and now hides inside it.

  What it gives up, stated rather than buried: a machine that dies inside the
  barrier can leave an order at the venue the log does not name, which
  reconciliation already reads as an order it cannot account for and answers by
  latching opening off. Process death is unaffected — those bytes are with the
  operating system either way. Nothing is acted on before its order is durable;
  what moved is when the path stops waiting, not what it waits for. The
  durability thread holds its own descriptor for the log and is replaced on
  rotation, since a barrier syncs the file rather than the path and a stale one
  would pass while proving nothing.

- **2026-08-29 — An accepted amend now keeps its order instead of cancelling
  it.** Bybit answers `order.amend` by saying it took the request and never by
  saying what price it left the order at, so every accepted reprice was
  cancelled rather than resolved to a price the engine could not name. The
  venue does state that price — it republishes the order on the private stream
  when it changes without trading — and the decoder was dropping the message as
  a repeat acknowledgement. It now becomes `OrderUpdate::Amended`, carrying the
  price and what is still working, and that is what narrows the conservative
  old/new reservation an amend opens. Hyperliquid's repeated `open` carries
  `limitPx` and does the same. An amend whose price is not stated within two
  seconds is cancelled, which is the behaviour every amend used to get. Three
  engine tests pin the three endings, each proved to fail with only its own
  mechanism removed.

- **2026-08-29 — The Bybit gateway paces to this account's real quota, and a
  declined order no longer costs the next one a reconnect.** Every trade-socket
  acknowledgement carries a `header` block stating the account's own per-second
  limit for the endpoint that was called; the adapter was dropping it and
  pacing forever to the documented default of ten. It now reads that figure and
  uses it when it is the larger, so a market-maker tier stops being invisible.
  A smaller figure is logged and not adopted: every batch is already capped at
  the documented default, so pacing below it would leave an admitted batch
  unable to reserve at all. Separately, the socket worker treated a business
  rejection like a broken pipe and tore the connection down, making the next
  order pay a reconnect and a re-authentication for a declined one. Only
  transport and decode failures drop it now.

- **2026-08-29 — The quoter takes its price from the top-of-book topic.**
  Bybit publishes depth-1 about twice as often as depth-50. The quoter
  subscribed only to the deep book, so the price it quoted around was up to one
  publication interval old. It now subscribes to both: the touch topic sets the
  microprice, and the book pressure, queue and variance terms stay on the deep
  book, which is the only thing that carries them. Subscribing to both exposed
  a latent fault in `MarketState::apply` — a depth event overwrote the quote
  slot unconditionally, so the deeper book's older copy of the touch replaced a
  fresher one. The touch is now arbitrated by socket read stamp, the only field
  comparable across two topics that each sequence themselves. With one stream
  the behaviour is unchanged, which is what the whole strategy suite passing
  untouched shows.

- **2026-08-29 — Cancel and amend timing marks reach the log.** The Bybit
  adapter captured exact socket-write and acknowledgement stamps for both, and
  the venue enum that the engine actually holds did not forward
  `take_mutation_timing`. It inherited the trait's `None`, so every cancel and
  amend wrote `null` and read back as "unknown" while placements were complete.
  Fixed, and the class closed: a source-reading test now requires the enum to
  write an arm for every method of `VenueGateway`, defaulted or not, with a
  negative control proving the scan is not blind. A method with a default body
  needs no arm to compile, which is what made this silent.

- **2026-08-29 — The order path separates the quota hold from the venue's own
  leg, and `engine latency` reads it back.** The 249.74 ms p99 venue task in
  the funded canary above was mostly the client's own rate pacing, which had to
  be inferred rather than read. Every place, cancel and amend now records how
  long the adapter held it back to stay inside the request quota, as its own
  mark in `VenueTiming`. The two ask for opposite fixes — a slow round trip is
  the network or the matching engine, a long hold is a quota to raise — and one
  span could not tell them apart. `engine latency --wal PATH` reports every
  step at p50, p90, p99 and p99.9 per operation from those exact stamps, rather
  than the live ledger's 60-second p50/p99 rollup. Checked against a real bench
  log: its per-step medians reproduce the bench's own ledger table, and the
  signing leg it splits out of the venue task measured 53.7 us.

- **2026-08-29 — The funded trade WebSocket completed a minimum-size forward
  trial.** The AGI canary's quoting run sent 256 placements, 237 amendments and
  258 cancels through the authenticated socket. Disabling it cancelled the one
  remaining quote and sent one market close through the same socket, leaving
  the account flat with no open order. Across the 256 quote placements,
  socket-write-to-ack measured 3.60 ms median, 20.41 ms p90 and 54.90 ms p99.
  The whole venue task measured 3.73 ms median; its 249.74 ms p99 includes the
  client's deliberate rate pacing before the socket write and is not network
  latency. The earlier signed-REST sample on the same host had only three
  placements, with a 45.62 ms median whole-task time and no socket-write mark,
  so the measured median task improvement is 12.2x while its tail is too small
  to compare honestly. Seventeen maker fills and the taker close completed
  eight round trips for -0.0779 USDT net after fees.

- **2026-08-29 — Funded Bybit order entry stays on the allowlisted IPv4.**
  The dual-stack resolver chose the VPS's Malaysian IPv6 address for
  `wss://stream.bybit.com/v5/trade`, whose CloudFront distribution rejected
  that country before authentication. The same official hostname reached a
  `101 Switching Protocols` response over `208.84.103.4` and authenticated the
  funded key with `retCode 0`, without sending an order. The persistent trade
  socket now resolves the official hostname but dials only IPv4, retaining TLS
  hostname verification, TCP no-delay and the signed REST fallback if a real
  WebSocket warm-up fails.

- **2026-08-29 — The minimum-size funded maker trial found and closed an
  inventory-ordering fault.** The AGI canary sent two orders and its first
  venue fill was a 750-unit maker buy at 0.006919, about 5.19 USDT. The next
  planned ask was larger than the position and still marked as an opening
  order, so the risk kernel correctly refused to let it cross through flat.
  A quote on the inventory-reducing side is now reduce-only, capped at the
  quantity held, and carries no replacement stop. An old opening quote on
  that side is cancelled to a terminal venue update before its replacement is
  sent. The registered mainnet canary stays in the append-only strategy table
  with `quote_enabled = false`, which pulls its orders and drains only its own
  inventory.

- **2026-08-29 — Bybit trade-WebSocket refusal no longer prevents account
  recovery.** The official `wss://stream.bybit.com/v5/trade` edge accepts the
  same handshake from the operator laptop but returns HTTP 403 before
  authentication to `208.84.103.4`; public REST and public/private WebSockets
  remain reachable from the host. The gateway still warms and authenticates
  the trade socket at every boot, but a failed warm now records the exact
  error and uses the already-warmed signed REST mutation path for that run.
  Private `execution.fast` remains independent.

- **2026-08-29 — Fast execution subscriptions are realm-specific.** Bybit
  demo refuses `execution.fast`, while mainnet exposes it. The first maker-path
  rollout therefore stopped at demo activation and its rollout transaction
  left every managed unit stopped; the funded engine never started and no
  order was sent. Demo now subscribes to `order` and fee-bearing `execution`;
  mainnet adds `execution.fast` for early strategy reaction.

- **2026-08-29 — The funded fleet moved to `208.84.103.4`.** The host passed
  strict SSH identity, exact two-IP key identity, signed account, public and
  private stream, target-book, commit, unit, and activation checks. The overdue
  ONT carry exit sold 790 at 0.05743 in four fills and left the account flat
  with no open order. Thirty warm signed position reads measured 12.71 ms
  median / 23.80 ms p95 on the fleet host and 172.14 ms / 486.59 ms on the
  declared `116.202.15.128` backup. The complete funded environment is staged
  on the fleet host; both addresses remain deliberately allowlisted.

- **2026-08-29 — An empty first book closes every position the log assigns to
  its sleeve.** A follower now seeds its candidate names from durable fill
  attribution as well as its config, current book, and in-process memory. An
  empty book therefore closes an owned non-seed position immediately after an
  engine restart, while positions attributed to another sleeve or no engine
  order remain untouched.

- **2026-08-29 — Bybit prices received before a subscription acknowledgement
  are preserved.** The public stream can send valid price frames before its
  acknowledgement. The feed now buffers those frames through the subscription
  phase and applies them in arrival order, after the reconnect boundary when
  there is one. Active strategy target books are also published group-readable
  (`0640`), so the isolated engine users can read decisions written by the
  producer; an unchanged book with an old private mode is republished.

- **2026-08-29 — A failed first Bybit market-data dial now waits before it
  retries.** The feed increased its backoff counter but slept only after a
  socket had connected once. An unavailable first socket therefore redialled
  in a tight loop, hit Bybit's WebSocket connection limit, and kept both
  engines blind. The first attempt remains immediate; every failure after it
  waits on the increasing capped backoff.

- **2026-08-29 — The funded key may declare one deliberate backup host.**
  `BYBIT_REAL_API_KEY_IP` remains the required primary address and the optional
  `BYBIT_REAL_API_KEY_BACKUP_IP` names one distinct backup. Startup compares
  the whole declared set with Bybit's signed key-identity reply, so an
  undeclared third address, a missing declared address, a duplicate, wildcard,
  or non-host network still refuses funded execution. Demo, producer,
  notification, and read-only attestation processes remove the backup setting
  from their environments.

- **2026-08-29 — The funded fleet can be resumed from the phone that paused
  it.** `resume-mainnet` joins the control helper's fixed action list and the
  sudo policy, which is now an exact five-command boundary. The funded resume
  proves this generation's completed activation receipt and that the funded
  account owner is running before it starts either producer, verifies both came
  up, and re-quarantines the pair if either did not. It never opens the
  credential file, so it cannot arm a disarmed account. Pausing real-money
  trading from a phone was previously a one-way door that needed a full rollout
  to undo.

- **2026-08-29 — Both liveness units can carry the dead-man's switch.** The
  watchdog already pinged `LIVENESS_HEARTBEAT_URL` on a healthy run, but no
  unit loaded a file that could carry it, so the switch could not be
  provisioned without editing a unit. Both units now read the optional
  root-owned `/etc/liquidity-migration/liveness.env`. Until that file names a
  URL the switch stays unprovisioned and a total host loss is still silent —
  which is what a rollout produces, because stopping the fleet stops the
  watchdog too.

- **2026-08-29 — `engine wal-cost` measures the storage's share of the order
  path.** The WAL crate already timed one buffered append against one
  durability barrier — the fsync a send waits for — but only a test could reach
  it. It is now a subcommand, so the cost can be read on the host that runs the
  fleet and again against a memory-backed path, which bounds what
  power-loss-protected storage would buy before any durability redesign is
  argued from guesswork.

- **2026-08-29 — The funded engine takes sole leverage authority.**
  The owner has stopped hand-trading the funded account, and the funded UID
  contract already forbids venue bots, copy trading, and other trading API
  keys. The funded engine therefore arms leverage when a target book arrives
  rather than inline before an order, and an entry from flat no longer pays a
  `set_leverage` round trip — measured live at ~172 ms, 844 ms worst, which
  was most of the order path's p99. Every held position's leverage is checked
  against the venue's own position row on each account reading; a contradiction
  alarms, is written to the log, and turns inline confirmation back on for that
  symbol, and a failed pre-arm is a warning rather than a refusal. A unit test
  requires both realms to state the value, because an absent key means
  `shared`.

- **2026-08-29 — A healthy funded watchdog no longer fails the rollout.**
  `start_mainnet_fleet` ended with `systemctl is-failed --quiet ... && fail`.
  A well funded liveness pass makes `is-failed` return non-zero, so that
  and-list — the function's last statement — returned 1, and activation aborted
  with no message at all. The guard now uses `if ... then fail`, as does the
  demo check that was correct only by its position in the caller. A unit test
  rejects any `&& fail` that ends a deploy function.

- **2026-08-28 — Both liveness observers get the same cgroup memory
  visibility as the producers.** `scripts/runtime/check_fleet_liveness.py`
  imports Polars and runs as the demo and funded liveness units, which still
  set `ProcSubset=pid`. Hiding the non-process `/proc` files kills that pass,
  and activation gates a rollout on the immediate demo pass succeeding, so the
  producer repair alone left the next rollout failing one phase later. The unit
  test now derives the Polars-reaching set from the committed dispatcher and
  the wrappers it names, rather than listing four producer unit names.

- **2026-08-28 — Preserved strategy-event tapes survive the engine wake
  cutover.** The deterministic tape reader retains the former
  `journal_change` spelling at the same data-arrival phase as `engine_change`.
  It verifies the original event IDs and rolling hashes without rewriting
  history, then permits current engine-wake records to append to that chain;
  unrelated event kinds remain rejected.

- **2026-08-28 — Rollout installs a runtime-usable Python generation and
  preserves producer state across the identity boundary.** Fresh virtual
  environments are root-owned mode `0755`, are import-smoked as every
  unprivileged Python runtime identity, and producer launchers no longer fall
  back to the host interpreter. Stopped installation migrates the demo and
  funded LONG/CARRY/Exodus state trees descriptor-relative, rehomes the two
  external LONG state files to their producer, and upgrades only the exact
  empty v1 LONG shape to v2 while preserving cooldowns. The LLM candidates
  handoff now lives inside the LLM service's own state directory; LONG receives
  group-read access without granting that service write access to engine target
  books.

- **2026-08-28 — The daily-loss circuit breaker is retired.** Operational
  profiles are schema v2 and no longer expose a daily-loss setting. The Rust
  engine neither restores nor writes its former control anchors, so legacy
  anchor state cannot block startup; historical anchor and verdict records
  remain readable, and WAL rotation drops them. Stopped installation also
  reassigns existing demo and funded engine-state trees in place to their
  isolated service identities, rejecting links, hard-linked files, and
  unsupported nodes instead of replacing durable state.

- **2026-08-28 — Isolated engines retain the existing account lease inode.**
  Stopped installation now gives persistent account-lease files root ownership
  and group write access for the isolated engine identities. The deployment
  preserves each file instead of replacing its flock inode, rejects links and
  non-regular paths, and lets both demo and funded services reopen leases made
  before the engines stopped running as root.

- **2026-08-28 — Bybit position-mode startup follows the row, not its cursor.**
  Explicit-symbol checks now request the venue's 200-row maximum and prove
  one-way mode from exactly one matching `linear` row with `positionIdx 0`.
  Demo and mainnet attach an opaque cursor to that complete response; following
  the observed demo cursor repeats the same row. Cursor presence no longer
  rejects a valid startup. Missing, duplicate, wrong-symbol, malformed, and
  hedge-mode rows still abort before a heartbeat or order.

- **2026-08-28 — Rollout recovery repairs producer inputs and lock cleanup.**
  Candidate-universe loading now accepts the deployment's exact immutable
  projection: root-owned mode `0640`, readable by the runtime group but not
  writable by producers. Private verifier-owned artifacts remain mode `0600`.
  This reconciles the producer loader with the installed demo and mainnet
  files without handing either producer authority to rewrite the reviewed
  universe. Lock-file orphan sweeping also invalidates its cache after a known
  staging mutation and bounds every clean cache entry, so equal or coarse
  directory mtimes cannot hide an abandoned alias indefinitely.

- **2026-08-28 — Rollout compilation leaves the incumbent fleet live.**
  The exact target commit now compiles during rollout prefetch. Stopped
  installation rechecks the immutable build source plus the candidate's path,
  owner, hard-link count, and prefetched SHA-256 before copying it, and performs
  no Cargo fetch or compilation. Prefetch fills a clean locked Cargo cache,
  then runs proc macros and build scripts offline in a private network. This
  phase also fetches and binds the target branch and downloads the exact-version
  Python wheels into a byte-digested cache; stopped install builds a fresh
  environment only from that cache with `--no-index`, proves its distribution
  set exactly matches the lock, and atomically exchanges it with the prior
  environment. Transient builders have a runtime bound and are stopped on exit
  or signal. A cancellation before the stop boundary leaves the incumbent
  topology untouched. This removes dependency downloads and the release build
  from the service outage without changing the installed artifact bindings.
  Each prefetch also scrubs its disposable compiler checkout before verifying
  the exact commit, so stale benchmark output and cross-platform metadata cannot
  block or contaminate a later rollout. Cargo's ordinary hard-linked promoted
  binary is confined to the disposable target, byte-verified into an atomic
  single-link handoff, and only that handoff can reach stopped installation.
  Fresh Python dependency verification now enumerates only that generation's
  own site-packages, so stale source-tree metadata cannot enter or reject the
  exact installed-distribution comparison. Telegram control-policy comparison
  also canonicalizes each command before sorting, making its exact four-command
  proof independent of sudo's presentation order. Deployment now also
  reconciles legacy demo-engine environments with the committed exact account,
  venue, and realm binding before the build. Missing bindings are appended
  atomically while host-only dials are preserved; empty or conflicting bindings
  abort without modifying the file. The installed release directory is now
  root:root mode `0755`, matching the activation watchdog's trust boundary, and
  release verification checks that parent before permit creation.

- **2026-08-28 — Exodus handoff uses the position actually abandoned.**
  A v7 pre-settlement fire now snapshots the fresh carry-attributed venue
  quantity and the same ticker's mark price. Target-book v2 carries that exact
  signed quantity alongside its frozen audit notional and direct entry
  deadline; the Rust follower converges entry and partial fills by quantity,
  so later price movement cannot resize the handoff. Legacy v1 target books
  and Exodus state remain readable, while new state is schema v2. The obsolete
  `EXODUS_NOTIONAL_MULTIPLIER` dial is removed. Heartbeat working-entry rows now
  come from a counted live-order index rather than scanning all orders retained
  in the current WAL segment, keeping account-state publication cost bounded by
  live work as history grows.

- **2026-08-28 — Funded risk configuration has one runtime source.**
  The engine now reads the same preflight-validated operational-profile artifact
  as the funded producers. Carry's rendered stop declaration can widen the
  engine baseline but cannot narrow the ceiling used by LONG and Exodus. This
  removes the case where a valid operator dial passed producer preflight and
  was then refused by an engine still holding the committed default.

- **2026-08-28 — Exodus short joins the funded engine as sleeve three.**
  The funded carry producer selects `lane2_exodus_short_v1` and writes
  `exodus-mainnet.json`; the funded engine consumes it as the appended
  `exodus` strategy, crosses entries and covers, and reports its book and fills
  separately. Carry and long keep WAL ids zero and one; boot accepts this
  suffix addition but still refuses any reorder. Once that longer Names record
  reaches the WAL, recovery requires a three-sleeve-compatible binary and
  config. Deployment installs the committed funded engine config atomically,
  validates, quarantines, waits for, flattens, and notifies the funded Exodus
  book. No
  synthetic venue order is used: the first live order waits for a real v7
  pre-settlement exit fire.

- **2026-08-28 — Rollout no longer depends on account-flatness attestation.**
  The deployment path no longer snapshots an outgoing attestor, runs account
  inventory at three rollout phases, accepts `--require-flat`, or requires a
  mainnet attestor credential during activation. It also stops asking the
  outgoing generation for a release marker and activation receipt before the
  fleet is stopped, so the markerless `e4e6750` production generation can cross
  the upgrade boundary. The target build, release binding, ordered fleet stop,
  persistent boot fence, quiescence check, activation lease, target-topology
  verification, and rollback/quarantine handling remain. `attest-flat` stays
  available as an explicit read-only operator command and for loss reset. The
  arbitrary 2026-08-27 key-creation cutoff is also gone; funded identity still
  requires UTA, write access, exact single-host IP, ContractTrade Order and
  Position permissions, no withdrawal permission, and the dedicated account ID.
  A pre-install failure now restores the exact active and persistent/runtime
  enablement topology it observed. Markerless incumbents restart directly;
  marked releases receive a temporary binding to the unchanged artifacts while
  only observed units restart, followed by a replacement completion receipt. A
  failure after checkout mutation leaves the fleet stopped.

- **2026-08-28 — Opening-stop lookup stays flat as order history grows.**
  The live-order ledger maintains a per-symbol, per-side multiset of opening
  stop prices and exposes only each side's tightest level to placement. This
  replaces a full allocation and scan of every outstanding order before every
  batch without moving the durability boundary or weakening whole-position
  stop protection. On the production host's memory-backed filesystem, the
  10,000-order durability median fell from 265 µs to 14.6 µs; the real-WAL
  5,000-order run fell from about 29 s to 7.83 s. Three standard 1,000-order
  native runs put the local submit-result median at 1.26 ms and the
  median-of-runs p99 at 3.16 ms. The Bybit aggregate-inventory tests also state
  their fixtures' actual row counts, restoring the Ubuntu release gate without
  changing production parsing. Private-stream integration tests consume the
  first successful subscription's readiness reset and prove the same reset
  precedes updates after reconnect, matching the runtime contract.

- **2026-08-28 — Venue-mutation bursts yield at bounded safety boundaries.**
  One strategy wake retains FIFO order, its original latency clock, and its
  flood limits across cooperative turns. After each completed placement,
  cancel, amend, or stop mutation, the engine gives ready private lifecycle
  updates and a due account refresh priority before sending the next group;
  an already-selected trailing exit still completes when shutdown becomes
  ready. The strategy-host heartbeat watcher now completes an installation
  handshake and compares the decision projection across both inotify and
  polling handoffs, closing the immediate-start rename gap. Release CI runs
  the optimized engine suite, bounded account-history soak, order-path
  benchmark, and artifact smoke test. Funded disarm remains available when CI
  is red, preempts a running rollout, shares one bounded lock deadline, and a
  canceled rollout leaves the fleet stopped. Rollout builds require the pinned
  Rust toolchain during prefetch as well as compilation. Latency output and
  standing docs call the measured local boundary a parsed submit result; the
  available records do not establish a socket-write timestamp.

- **2026-08-28 — Audit series pushed; Ubuntu qualification is billing-blocked.**
  The 42-commit Rust-only migration series, ending in audit commit `206e40c21`,
  was fast-forwarded to `main`. Push workflow run `33130163698` created both
  Ubuntu jobs, but GitHub rejected each before assigning a runner or executing
  a step because recent account payments failed or the Actions spending limit
  must be increased. This is not a passing or failing test result: release
  qualification remains pending until the account owner fixes billing and the
  exact pushed commit's Python, Rust, bounded soak, build, and smoke steps run
  green. No VPS deploy or live venue order was performed.

- **2026-08-27 — The seven execution-audit gaps become explicit Rust and
  rollout contracts.** Sibling placements now validate and reserve in request
  order, append together, cross one WAL barrier, and reach Bybit as overlapping
  distinct-symbol HTTP chains over a ten-socket warm pool; same-symbol and
  nonce-sensitive chains retain serial wire order. Each mutation endpoint has
  a completion-anchored rolling quota, and native batch cancellation pulls a
  halted book in bounded ten-order groups while private terminal updates stay
  ahead of confirmation deadlines. Risk reservations include cumulative
  opposite-side pending quantity and restart charges only each order's
  unfilled remainder. Opening reprices require finite risk approval and retain
  their full old/requested price range through ambiguity, rotation, and
  restart, so high-price notional and low-price short-stop loss are both
  charged until a definitive answer or cancel. Whole-position stop intent now belongs to the fill that
  actually grows or crosses the position, never an unfilled sibling;
  same-side growth cannot loosen the tighter existing level, pre-wire checks
  include prior-wake live orders, and fresh account views actively repair any
  venue regression or latch opening off. Malformed daily-loss anchors abort
  startup instead of silently resetting the circuit breaker. Before fetch,
  rollout digest-verifies and freezes the outgoing installed engine. That
  immutable binary performs the pre-stop and owners-stopped flatness checks;
  the final boundary requires both it and the digest-bound installed target,
  while the incoming checkout and build candidate never attest. An outgoing
  release without `attest-flat` requires a signed, reviewed out-of-band
  bootstrap rather than falling back to incoming code. Mainnet checks
  receive only a separate globally read-only query key from an exact-schema,
  operator-owned attestor file, never the execution key. Direct install,
  activate, staged, and funded unit start/restart paths no longer bypass
  rollout on a funded-configured host. Mainnet inventory covers ordinary,
  spread, RFQ, active venue-native strategy, and reported cross-account
  asset/bot state. Nonadditive venue aggregates are not treated as an API
  guarantee, while aggregate-only values cannot masquerade as cash unless
  coin detail explicitly identifies positive USDT/USDC. Because Bybit cannot enumerate every bot instance,
  funded identity also requires an account-bound acknowledgement that its UID
  is dedicated to the engine with no hand trading, venue bots, copy trading, or
  other trading API keys. Rollout activation now uses a root watchdog to renew
  a boot- and process-bound ten-field six-second permit while trusted launchers
  supervise the candidate topology; only a synced, verified six-field release
  receipt survives reboot, so process death or power loss cannot preserve a
  partial activation. Permit renewal now records the pre-validation inode,
  takes a non-creating pin, and revalidates it under lock, so direct deletion or
  a valid-looking replacement cannot race recreation or adoption. Remote
  funded stop/disarm execute no checkout code; stop never reads credentials,
  and disarm uses an isolated root-owned interpreter with an embedded strict
  atomic rewrite after persistent quarantine. Deploy preflight and launchers reject
  writable critical checkout ancestry or Git metadata. Bybit startup verifies one-way mode
  for every configured or newly admitted symbol. Execution recovery aborts
  instead of clipping intervals older than venue history. Ubuntu CI runs the
  bounded-ID release soak that separates within-run ID cost from synthetic
  recovery-history cost. UTC loss rollover now carries bounded durable pre-midnight equity
  evidence (periodically and immediately on rises), preventing a crash or the
  first post-midnight order from erasing a boundary loss without making every
  account poll an unconditional fsync. Hyperliquid and Lighter testnets remain canary paths, while their
  mainnets and MEXC mainnet are source-gated from `engine run` until exact-realm
  live lifecycle evidence exists; public-feed continuity checks now match each
  protocol's evidence. Funded risk gains a durable 10 USDT UTC-day account-loss
  halt plus a stopped-engine, flat-account `loss-reset`; demo leaves it
  disabled. Standing docs now match fail-closed foreign-activity handling,
  direct adapter rule reads, and the remaining live-validation boundaries.
  Funded Bybit identity now rejects the exposed key generation and unsafe key
  shapes: keys must be created on or after 2026-08-27 22:30 UTC, UTA,
  write-capable, allowlisted only to the declared production host IP,
  ContractTrade Order+Position capable, and unable to withdraw. Creating the
  replacement and revoking the old key remain external owner actions.

- **2026-08-26 — The demo rule-receipt freshness alert is removed (owner
  directed).** The demo receipt no longer pages `demo_rules_age`; nothing in
  the demo runtime path reads the receipt, and a demo receipt in the back half
  of its life renews itself on the next rollout, so the weekly WARNING only
  taught operators to ignore a WARNING. The funded receipt still gates the
  owner, still renews on any deploy, and still pages WARNING/CRITICAL under
  `venue_rules_age` — that gate is untouched. `check_fleet_liveness.py` now
  scopes the rules-receipt gather to mainnet only.

- **2026-08-26 — The carry rule rename: registered rule goes to `lane2_carry_hold_v7`
  (name only).** The registration that was `lane2_carry_hold_v6` becomes
  `lane2_carry_hold_v7`, so the live name and the config filename both read
  v7. Nothing about the rule, the config, the parameter values, or the
  forward grading changed — the file `configs/lane2_carry_hold_v6.json` was
  renamed to `lane2_carry_hold_v7.json` and its `config_id` updated to
  `lane2_carry_hold_v7`; `CARRY_CONFIG_PATH`, the v7 profile, and both clock
  profiles now read `lane2_carry_hold_v7.json`. The v6↔v7 id is a DATING/NAME
  change point, not an evidence one: rows graded under `lane2_carry_hold_v6`
  (through 2026-08-21) are the same rule under the old id, and the forward
  experiment differential is now `carry_hold_v7_minus_v5`. The journal keys
  (`carry_hold_v6_live_v1`, `carry_hold_v7_live_v1`) and the settled-print
  rollback dial `CARRY_STRATEGY_PROFILE=v6` are unchanged.


  🔴 lost it, only where there is a verdict — and the verdict leads: an
  exit's first line is the dot, the account, the sleeve, and the net in
  bold, because the phone's notification preview shows one line. Every
  message names its account (RM = real money, DEMO = demo), sleeves act in
  verbs (enters, shorts, exits, covers, closed), prices carry four
  significant figures, every return reads as percent of the position (never
  basis points — those stay in the engine's reports), slip reads "paid" or
  "saved" because its adverse-positive convention runs against the net
  beside it, and the daily summary
  opens with the day's own colour
  over a monospace win–loss table whose rows are per account and sleeve — so
  real money never melts into a demo figure. Messages are Telegram HTML now:
  `send_telegram_message` grows an opt-in `parse_mode` argument, opt-in
  because HTML rejects a stray `<` — the notifier escapes its text and asks
  for it, the watchdog stays plain. The notifier's state schema is unchanged,
  so the changeover run sends nothing spurious. `scripts/runtime/
  notify_book_changes.py`, `liquidity_migration/ops/telegram.py`,
  `docs/notifications.md`.

- **2026-08-24 — LLM gate prompt v7: the crime-pump playbook joins the
  rubric (owner approved).** The driver-judgment prompt
  (`scripts/research/llm_driver_ledger.py`) moves to
  `driver-judgment-v7-crime-pump`. Two changes, both judgment food, no new
  mechanical rule: (1) a new enrichment fact `turnover_to_oi_24h` — the
  day's traded volume against the standing open interest (the venue reports
  OI in contracts, so notional derives as contracts × price) — the churn
  read that public research on manufactured pumps calls "brushed" volume;
  (2) the manufactured-pump step now names the two documented crime-pump
  shapes — the low-float walk-up and the short-squeeze bait — and each
  judgment reports a `manipulation_shape` verdict. The one outside number
  (volume-to-OI low single digits typical, 20+ suspect) is labeled
  unmeasured on this desk inside the prompt itself; every measured prior in
  the rubric is unchanged. `--grade` buckets by prompt version, so v7
  accrues its own forward record and v6's rows are untouched. The entry
  gate is unchanged: score ≥ 6, same candidates file, same LONG-sleeve
  sizing, exits, and stops. Motivation: a public post-mortem of seven
  manipulated tokens (MYX, COAI et al.); its mechanical signals are already
  measured dead on this book (OI exits, funding-flip exits, pool-level
  taker reads — receipts in `docs/research/research_findings.md` §2), so
  the judged rubric is the one seam that takes it. This commit is the
  change point. Deployed `b51aa3a8` via `staged --stop-first` the same day:
  verify-ok on the commit, both engines rebuilt on it, the funded engine's
  boot reconciliation stayed clean (`may_open: true` in the mainnet
  heartbeat — false is the latch, `engine/engine-types/src/wal.rs`), and
  the ledger service's first run under v7 completed green on a quiet hour
  (0 movers, 0 triggers, so 0 rows — the first journaled
  `driver-judgment-v7-crime-pump` row is the runtime receipt to watch).
