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

## Verification and current status

- rollout readiness unit/adversarial tests: passed;
- stale-authority shutdown and prior-bracket reprobe tests: passed;
- paper normalizer safety and no-op fast-path tests: passed;
- owner-health, LONG, and CONTINUOUS focused tests: passed;
- Linux before/after timing benchmark: passed;
- repository doctor, Ruff, and mypy: passed;
- full local gate: `2249 passed / 1 skipped`.

This report records implementation and local validation, not operational
activation. Deployment status must be established independently from the exact
pushed commit, authenticated rollout receipt, and fresh read-only status output.
