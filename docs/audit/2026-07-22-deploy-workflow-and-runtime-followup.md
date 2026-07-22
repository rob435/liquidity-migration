# 2026-07-22 deploy workflow and runtime follow-up

## Scope and authority boundary

This follow-up profiles the stopped deployment path, adds a guarded one-command
rollout, and checks the deployed demo/paper runtime after the journal-publication
remediation. It does not change strategy signals, sizing, execution economics,
accounting, or native-protection levels. It does not enable `REAL_MONEY`, flatten
a position, or cancel a pre-existing venue order. Push and deployment state are
reported separately from this implementation record.

At the final live sample the account was deliberately left running because it
was non-flat. Authenticated Bybit demo truth and canonical reconstruction both
showed MIRAUSDT short `1896.2`; the venue held one verified reduce-only,
close-on-trigger Full MarkPrice stop at `0.05372`. The new rollout readiness
gate would reject that state before stopping any unit.

## Deployment bottleneck and measured change

The previous install path performed a descriptor-safe full paper-tree
inspection, then reopened, `fchmod`ed, and `fsync`ed every regular file and
directory even when its owner, group, and mode were already exact. The real
paper roots contain roughly 123,000 entries, so this no-change rewrite was the
dominant stopped-time cost.

A controlled Linux benchmark on the VPS used a temporary tree of 200
directories, 2,000 files, and an existing `.locks` directory, all already at
the required root ownership and `0700`/`0600` modes:

| Implementation | Elapsed | Result |
| --- | ---: | --- |
| Deployed pre-change normalizer | 5.35 s | Full inspection, redundant permission writes/syncs, final rescan |
| Local candidate transmitted without changing the checkout | 0.79 s | Full inspection, zero no-op writes/syncs, final rescan |

That is an 85% reduction on the controlled no-change workload. Linear scaling
would put the mutation command itself near 44 seconds on a similarly shaped
123,000-entry tree, but filesystem cache and shape make that an estimate, not a
deployment-time promise. Separate batch preflight, final verification,
dependency checks, and activation remain real work.

The candidate now selects only entries whose planned owner/group/mode differs,
and permission helpers avoid `fchmod` and `fsync` when no write is needed. It
removes one redundant post-mutation entry loop, but retains the independent
final descriptor-rooted rescan of the complete path set, inode, type, mount ID,
owner, group, and mode. Adversarial tests prove an already-correct tree performs
no permission writes and that a late file insertion is still rejected.

## Guarded rollout automation

`scripts/ops.sh deploy --execute rollout` now keeps the exact staged safety
physics inside one host-wide maintenance-lock transaction:

1. require a full target commit plus explicit profile, authorization reference,
   and exact demo/paper-only owner acknowledgement;
2. fetch and prove the target is on the selected remote branch before stopping
   anything;
3. verify the current receipt and topology;
4. require a verified canonical journal, fresh healthy owner/reconciliation,
   zero local/direct venue positions, zero aggregate targets, zero canonical
   working orders, and empty regular plus conditional Bybit order inventories;
5. stop producers, timer readers, and watchdogs before owners, bind health to
   the exact post-producer journal head, stop owners, and repeat the flat proof;
6. run the stopped exact-commit install, issue a new create-only authority under
   the inherited lock descriptors, activate owner-first, and verify topology.

A pre-install failure restores the previously verified topology. Once checkout
installation begins, the old receipt is no longer rollback authority, so a
failure forces the managed fleet stopped for explicit recovery. The script
prints elapsed time for material phases, keeps SSH alive during long checks,
and bounds residual-momentum bootstrap to 300 seconds with 10-second retries by
default instead of permitting a 30-minute partial activation.

## Runtime audit

The read-only sample covered the deployed exact executable commit
`6dad49ca4ab099c83cb5e954533f71d9cee6929a` from 16:00 through approximately
18:52 UTC:

- both owners and all four persistent producers were active with zero restarts;
  all three timers were waiting and no unit was failed;
- 319 canonical authenticated venue checkpoints were journaled, none unhealthy,
  none over 60 seconds apart, with a maximum interval of `42.842s`;
- the sampled latest snapshot was healthy and mismatch-free; owner health was
  healthy and about two seconds old;
- no traceback, critical page, reconciliation-stale page, or unhealthy
  reconciliation occurred after activation;
- 11 public-market WebSocket ping/pong timeouts occurred: seven continuous
  demo, two continuous paper, and two LONG paper. Each affected service stayed
  active with zero restarts and completed a later cycle. This is recoverable
  provider transport noise on the available evidence, not an execution-health
  defect;
- one LONG demo cycle failed entries closed because exact owner health named
  journal sequence `33272` while the journal had just advanced to `33273`.

The final item is actionable even though it was capital-safe: an ordinary
healthy projection publication race could discard an hourly entry opportunity.
The candidate gives that one typed “healthy but strictly behind” condition up
to four exact reads separated by one second. Stale, blocked, future-dated,
wrong-account, health-ahead, and equal-sequence hash contradictions still fail
immediately. Exhaustion remains fail-closed and visible.

## Release-attempt finding: expired demo-rule evidence

The first fresh pre-deploy status sample at approximately 19:33 UTC found a
new, distinct maintenance issue: the current operational receipt remained
byte-exact, all six persistent services were active with zero restarts, and the
VPS clock was coherent, but the bound empirical demo-rule receipt had crossed
its registered 168-hour age limit. Normal verification therefore failed with
`demo rules receipt is stale or future-dated`. The rollout had not stopped or
changed any unit. Direct authenticated truth still showed MIRAUSDT short
`1896.2`, the same canonical aggregate target, and one reduce-only,
close-on-trigger stop, so the account also remained correctly ineligible for
deployment.

The follow-up fixes the maintenance deadlock without weakening activation:

- a target-commit helper reruns the old authorization verification and may
  ignore freshness only when the bound rules are genuinely expired; a
  future-dated timestamp or any other verification error remains fatal;
- the hedge and liveness one-shots were independently confirmed to be failing
  in their strict authorization wrapper with status 2 before workload
  execution. Shutdown verification tolerates only that exact failed-unit shape
  while rules are proven expired; post-activation verification remains strict;
- expiry means the old topology cannot be restarted under its former receipt.
  Rollout now marks the stop boundary irreversible up front in this case, so a
  later failure forces all managed units stopped instead of attempting a false
  rollback;
- that narrow result is used only to prove the current topology and reach the
  existing flat-account shutdown gates; all new authority and runtime startup
  continue to use strict freshness;
- after a stopped flat proof, stale rules are re-probed automatically. The old
  516-symbol receipt required 7,383 order-threshold attempts (median 14 per
  symbol). An unchanged prior boundary now needs at most the adjacent rejected
  and accepted attempts, while a changed boundary falls back to the full
  search. This removes about 86% of threshold attempts in the unchanged case
  without copying old observations into fresh evidence;
- the probe keeps exact terminal cancellation/no-fill/trade-history evidence,
  preserves old and failed receipts, atomically updates the demo input only
  after success, and is followed by another direct local/venue flat proof.

The optimization materially reduces the exceptional weekly refresh path but
does not promise a fixed duration: provider rate limits and terminal-order
visibility remain external latency. Routine fresh-rule deployments skip the
probe entirely.

## Live rollout defect and containment

The 19:59 UTC recovery attempt exposed a shell status-propagation defect. The
readiness helper correctly printed a failure, but `rollout_flat_check` then ran
credential cleanup and implicitly returned the cleanup command's zero status.
`run_phase` therefore printed `phase-ok` and began the stop sequence. The SSH
session was interrupted immediately and its orphaned remote shell was killed
before either account owner or the continuous producers stopped. Three timers
had stopped and LONG demo had entered its configured stop path; canceling the
systemd job could not undo the already-delivered SIGTERM, and LONG demo later
ended failed by stop timeout. Both owners, LONG paper, and both continuous
producers remained active. Authenticated venue truth and the canonical journal
still agreed on MIRAUSDT `-1896.2`, with the original reduce-only,
close-on-trigger stop intact.

The fix captures the helper exit code before cleanup and returns it explicitly;
a regression assertion now binds that shell contract. The emitted health error
also revealed a second read-order race: readiness captured `now` before a full
journal verification that took long enough for the owner to publish a newer
health file. Runtime checks now resample the clock after the journal read and
let the health loader sample its own current time; deterministic tests retain
an explicit fixed clock. Neither change converts a failed or non-flat check
into a pass. The account's non-flat MIRA position remains a hard refusal before
any future stop.

Because the interrupted attempt left LONG demo failed and three enabled timers
inactive, recovery topology verification also permits an enabled-but-inactive
downstream unit only under the independently proven expired-rule condition.
Both owners remain mandatory and exact, and the next operation remains the
read-only local/direct-venue flatness proof. Strict post-activation topology
verification has no degraded-state exception.

The corrected path was then exercised at 20:16 UTC against pushed and
CI-green commit `dd860073c2f09024a8e124696c4f8a151a0c849e`. Old-topology
verification reported only the expected degraded-unit warnings, and the next
flatness phase returned status 1 with mutually consistent canonical position,
aggregate target, journal venue/reconstruction, direct authenticated Bybit
position, and protected-order evidence for MIRAUSDT `-1896.2`. The phase was
reported as failed, the stop boundary was never entered, no remote deploy shell
remained, and the installed checkout stayed at `6dad49c`. This confirms the
exit-status fix on the live refusal path; it does not claim deployment.

## Verification and current status

- rollout readiness unit/adversarial tests: passed;
- stale-authority shutdown and prior-bracket reprobe tests: passed;
- paper normalizer safety and no-op fast-path tests: passed;
- owner-health, LONG, and CONTINUOUS focused tests: passed;
- Linux before/after timing benchmark: passed;
- repository doctor, Ruff, and mypy: passed;
- full local gate: `2250 passed / 1 skipped`.

The preceding sections recorded implementation and local validation before
operational activation. The subsequent terminal-recovery section establishes
deployment separately from an exact pushed commit, authenticated receipts, and
fresh read-only status output.

## Owner-authorized terminal recovery and measured deployment

The owner subsequently authorized demo flatten/cancel, a full managed
demo/paper ledger reset, push, and deployment. The canonical owner accepted one
atomic zero-target batch and flattened MIRAUSDT; its command ID was
`7a9d9882-e2ae-5bf5-99ff-39ad0ace3697`. Canonical reconstruction and a separate
authenticated Bybit snapshot then agreed on zero positions and zero regular or
conditional orders. No mainnet credential or `REAL_MONEY` path was enabled.

The destructive reset did not begin until a second authenticated flatness check
passed and a durable archive had been written, hashed, and reopened. It archived
all 22 selected ledger/epoch targets to
`/opt/liquidity-migration/data/_archive/ledger-reset-20260722T213413Z-owner-authorized-full-reset-20260722.tar.gz`
(31,490,855 bytes; SHA-256
`e629df3efb8c0a3e5101479298589e23d65b7b95c9daa9859531a6da3f91c6d2`).
Persistent lock inodes, config, reports, caches, residual momentum, and root-level
market data were retained. The reset created fresh demo and paper boundaries and
left every managed unit loaded and inactive before receipt publication.

Three additional workflow defects were found by the real stopped recovery and
fixed without weakening ordinary rollout:

1. `reset-failed` is now called only for units actually in `ActiveState=failed`;
   healthy inactive units are still independently required to resolve as
   `LoadState=loaded` and `ActiveState=inactive`.
2. Recovery validates the root-owned `0600` demo route with the private loader
   and the intentional root-owned, paper-group `0640` route with the group-aware
   loader.
3. A fresh reset epoch may lack a journaled venue snapshot. Only during
   stopped-maintenance recovery, an exact-commit, full-scope, leave-stopped,
   fresh-root reset receipt whose archive and self-hash reopen successfully may
   stand in for that historical snapshot. The helper still reduces the fresh
   journal and directly queries authenticated Bybit positions plus regular and
   conditional orders. Running/ordinary rollout cannot use the exception.

Final recovery used receipt
`/opt/liquidity-migration/data/_archive/20260722-full-reset-receipt-230e9d1.json`
(artifact SHA-256
`db12d38848a4edd5230aaf8ff2d9a8c6b159c68c80ab95770b8c79ee4d3f0bcc`)
for runtime implementation commit
`230e9d1f51afe36fdfb8595e1c3ba7a41a26259a`. It froze 513 current symbols,
down from the stale receipt's 516, and freshly validated every symbol in one or
two threshold attempts. The rule phase took 1,385 seconds (`23m05s`) and the
complete stopped install took 1,531 seconds (`25m31s`); routine fresh-rule
deployments skip this exceptional probe. Activation took 81 seconds including a
54-second residual-momentum seed.

Post-activation evidence was independently re-read rather than inferred from
the automation result: both owners and all four producers were active/running,
all three timers were active/waiting, exact-head status returned `verify-ok`,
and rollout readiness at journal sequence 12 reported zero local positions,
aggregate targets, working orders, authenticated venue positions, and venue
orders. Local pre-push validation passed 2,257 tests with one skipped. Manual
GitHub Actions run
`https://github.com/rob435/liquidity-migration/actions/runs/29962789028`
then passed full CI plus the pinned-SSH VPS verify. These are point-in-time
demo/paper deployment facts; they grant no real-money authority and do not
promise that activated strategies remain flat.
