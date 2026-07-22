# 2026-07-22 demo journal-publication race

## Scope and safety boundary

This audit reconstructs the 12:11 UTC demo reconciliation-health page from the
canonical account journal, immutable transaction mtimes, systemd journals,
owner-health artifacts, and authenticated venue snapshots. It changes journal
cache coordination only. It does not change strategy decisions, sizing, order
economics, stop levels, account accounting, or paper execution behavior.

The deployed environment remained demo/paper only with `REAL_MONEY=false`.
No position was flattened, no journal was reset or edited, and the local fix
was not deployed during this audit. At the final read-only observation the demo
account still held MIRAUSDT, so a stopped install was not treated as an
incidental implementation step.

## What happened

The notification sequence was truthful but exposed a delayed authority path:

1. DEXEUSDT opened short `7.5 @ 4.423` from command
   `35a28249-4af0-5058-9c7a-4bd9732c07a4`.
2. Its entry-attached Bybit Full MarkPrice stop triggered about three seconds
   later and filled `+7.5 @ 4.442`.
3. Notifications initially labelled both lifecycle updates as local journal
   facts awaiting venue reconciliation, then emitted the confirmation pages
   after authenticated agreement returned.
4. The independent liveness service observed the prior checkpoint and paged:
   `canonical account reconciliation health is 2.8 min old`.
5. The next watchdog run emitted `resolved — account_health_stale`.

The relevant durable timestamps are:

| Evidence | UTC timestamp | Consequence |
| --- | --- | --- |
| Prior healthy venue snapshot, sequence 31987 | 12:08:26.768780 | Last published reconciliation before the stall |
| DEXE entry fill received | 12:08:58.941213 | Reconstructed short becomes attributable |
| Native-stop execution received | 12:09:02.146699 | Venue had already flattened DEXE |
| Delayed protection refresh rejected | 12:09:47 | Bybit error 10001: zero position |
| Native-stop transaction, sequences 31999--32006, committed | 12:11:08.025833 | Stop execution finally enters canonical state |
| Close/P&L transaction, sequences 32007--32008, committed | 12:11:08.065833 | Account-net close facts become durable |
| Next healthy venue snapshot, sequence 32010 | 12:11:09.987654 | Venue/local agreement is restored |
| Watchdog page printed | 12:11:18.554841 | It had observed the old checkpoint during the stall |
| Watchdog resolution printed | 12:14:18.280840 | Condition no longer active |

The venue-snapshot interval was exactly `163.219s`; normal surrounding
intervals were about 30--50 seconds. Across 413 post-activation checkpoints in
the captured journal, this was the only interval over 60 seconds.

## Root cause

`AccountJournal.transact` correctly makes a complete transaction authoritative
by fsync and atomic replacement before updating its rebuildable JSONL
projection. It also maintains an in-process event/state cache so ordinary owner
transactions do not replay immutable history.

There was one uncovered interleaving:

1. Writer A atomically replaced a new transaction segment.
2. Before Writer A acquired `_cache_lock` to publish the matching cache, hot
   Reader B observed the transaction-directory mtime change.
3. Reader B interpreted the local segment as an external commit and replayed
   every transaction while holding `_cache_lock`.
4. Writer A waited for `_cache_lock` while still owning the cross-process
   journal writer lock.
5. Other execution and reconciliation transactions queued behind Writer A.

The DEXE tape carries the resulting cascade. Its fill transaction was about
47 seconds behind the fill receive timestamp. The native-stop adoption and
close/P&L transactions were about 126 seconds behind their receive timestamp.
That delay made the entry-fill handler attempt a redundant protection refresh
after the venue-native stop had already flattened the position; the definite
zero-position rejection was correct and fail-closed, but it was downstream of
the journal stall.

An isolated authoritative journal copy contained 32,681 events across 22,550
transactions. Full verification took 2.77 seconds on the audit host, while the
exact pre-stop DEXE state replayed the ten-event adoption/finalization in 9ms
when its cache and projection were coherent. The business transition,
component attribution, and durable writes were therefore not the minute-scale
path; the unintended cache miss was.

## Remediation and invariants

The journal now marks only the local segment-commit/cache-publication window.
While that marker is active, cooperative in-process readers return the prior
coherent cache signature instead of scanning the now-changed transaction
directory. The writer then publishes events, event-ID index, storage signature,
and reduced state together and clears the marker.

The change preserves these boundaries:

- the transaction segment remains the durable commit point;
- cross-process writers remain serialized by the existing journal file lock;
- concurrent readers may linearize on the prior state while the writing call is
  still in progress, but never observe prospective or partially published
  state;
- a write/publication exception clears the marker, so the next reader discovers
  and reconstructs any already-durable segment;
- projection failure still cannot roll back an authoritative transaction;
- no health threshold is widened and no venue error is relabelled as success.

The regression test commits a real segment, pauses before cache publication,
starts a hot reader, and makes any immutable-history replay fail the test. The
reader must finish from the prior cache while the writer is paused; after
release, the new state must be visible and intact.

## Other live findings

- Exact deployment verification passed for
  `cf0b582ac13f4e87caabec59af2a40987fcf6a4c`, profile `operational`.
- Demo and paper owners were active with zero restarts; all three timers were
  active and no systemd unit was failed.
- Demo and paper owner-health artifacts were fresh and healthy. Paper retained
  only LAUSDT venue-minimum dust (`0.1`), classified by the deployed policy as
  `converged_within_venue_minimum`.
- The one paper startup traceback was a pre-book recovery attempt for TREEUSDT;
  the owner became ready seconds later and did not repeat it.
- The CONTINUOUS public kline client logged reconnectable ping/pong timeouts,
  but the service had no restart or traceback, continued completing cycles,
  and did not create another liveness alert. This audit does not promote that
  provider transport noise into a separate defect without failed cycle or
  readiness evidence.
- At 15:25 UTC, the latest authenticated demo snapshots showed MIRAUSDT
  `-2079.5` both venue and reconstructed, no mismatches, and no DEXE exposure.
  This is a point-in-time observation, not current authority to stop, deploy,
  or flatten the account.

## Verification

Local checks completed:

- exact publication-race and adjacent cache/failure tests: passed;
- complete `tests/test_account_kernel.py`: passed;
- focused account execution, reconciliation, service, owner-health, native
  protection, and liveness set: 255 passed;
- Ruff: passed;
- mypy: passed for 114 source files;
- `git diff --check`: passed;
- full `scripts/dev.sh check`: 2,225 passed / 1 skipped.

Green local checks do not claim deployment or current venue truth.
