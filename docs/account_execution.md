# Account Execution

This is the current demo/paper execution contract. Code, systemd units, strict
environment files, and generated receipts define implemented behavior if this
document drifts.

## Ownership

Strategy processes publish absolute component targets. They do not hold private
venue credentials and do not submit, adopt, repair, or close venue orders.

- `liquidity-migration-account-execution.service` is the sole Bybit demo
  mutator and the authority for orders, fills, positions, funding, native
  protection, reconciliation, and demo account health.
- `liquidity-migration-account-paper-execution.service` owns deterministic
  paper fills and accounting against its independent market capture.
- LONG and CONTINUOUS demo/paper services publish targets to the corresponding
  inbox.
- The continuous hedge publishes demo targets; it never calls the venue.
- RMOM refresh owns only its causal input file.
- Liveness reads canonical health and strategy inputs and may notify; it has no
  Bybit credential and no systemd activation/order dependency on the owner it
  observes.

The account journal is authoritative. Parquet, reports, notifications, and
strategy read models are projections or telemetry. See `docs/account_journal.md`.

The demo hourly account notification labels owner/reconciliation state as
`Account execution health`. Separately, it displays the latest completed
CONTINUOUS BTC gate and entry funnel from a small receipt-bound projection.
The owner never scans the growing strategy-cycle ledger, and unavailable or
stale strategy telemetry changes only the message—not account admission.

Each owner derives its requested route without filesystem mutation, acquires
its persistent account-owner lease, and only then ensures or creates the paired
account/inbox route manifests. A losing owner therefore cannot initialize route
manifests before discovering the active owner. The demo lease is the
authenticated Bybit user-wide capability under
`/run/lock/liquidity-migration`; the paper lease is local to its canonical
account root. The paper owner accepts the intentional private-parent mount
boundary created by systemd `ReadWritePaths`; this is a non-root-only opt-in,
and the exact parent and leaf mount identities are still pinned and revalidated
for the lease lifetime. Both owners retain the same validated single-link inode,
and route mismatch still fails closed after acquisition.

## Data flow

```text
market data -> strategy target -> durable inbox -> account kernel
             -> risk decision -> venue/paper command -> observations
             -> canonical journal -> projections, health, notifications
```

The inbox serializes arrival, coalesces later replacements, and carries
component revisions so an older entry cannot reopen after a newer zero target.
The account kernel aggregates cross-sleeve targets, applies verified symbol
rules and absolute account risk policy, journals rejection or command intent,
and reconciles observations idempotently.

Lifecycle clocks and strategy protection begin from attributable confirmed
fills, not accepted targets or decision prices. The demo owner separately
requires exchange-native disaster protection for reconstructed venue exposure.
That protection is an account safety control, not a strategy stop or alpha claim.

## Operational profiles

| Profile | Authorized surface | Additional conditions |
| --- | --- | --- |
| `demo-operational` | Demo owner, demo LONG/CONTINUOUS, demo hedge/RMOM, demo liveness | Paper disabled; `CONTINUOUS_PAPER_SLEEVE=off`; liveness scope `demo` |
| `operational` | Both owners, allowed demo/paper producers, hedge/RMOM, demo-paper liveness | Explicitly uncalibrated paper integration twin; liveness scope `demo-paper` |

Both profiles require `ACCOUNT_RAW_MARKET_PERSISTENCE=0`. Owners still maintain
live sequence-aware L2, bounded readiness, exact decision books, journals,
reconciliation, and protection; they simply do not append every public frame.
Deployment derives one authorization-bound scheduling-capture path per
environment: `<ACCOUNT_CAPTURE_ROOT>/strategy-targets.jsonl` for demo and
`<ACCOUNT_PAPER_CAPTURE_ROOT>/strategy-targets.jsonl` for paper. LONG and
CONTINUOUS share that tape within an environment through the existing locked,
hash-chained writer. Older per-producer fallback tapes remain preserved as
pre-boundary history; they are not silently merged into a prospective epoch.

Neither profile asserts alpha, historical replay agreement, promotion, or
mainnet readiness.

The paper model is commit-owned rather than runtime-calibrated: it walks the
visible depth-50 decision book, applies 5.5 bps taker fees and 2.0 bps residual
adverse slippage, allows partial fills by book level, models zero latency, and
rejects decisions older than 250 ms. Every modeled ACK, fill, status, runner
name, and owner-health record carries
`integration_only_uncalibrated`. This is an integration simulator, not
performance or executable-price evidence. Changing it requires a new commit
and operational authorization.

The continuous hedge route is demo-only. Paper CONTINUOUS can exercise the
component execution path, but it is not full hedged-portfolio parity.

The demo hedge sizes current BTC/ETH targets from live canonical CONTINUOUS
gross exposure, current account equity, and current prices, but its beta and
BTC-vol regime inputs come from a commit-owned immutable historical model prior
through 2026-07-09. The runtime does not extend that prior: bounded demo bars
and current account projections do not reconstruct causal per-unit CONTINUOUS
daily returns including funding and costs. Every published hedge target carries
the prior artifact hash, source-summary hash, period, row count, and
`model_prior_live_extension=false`. Missing, malformed, future-dated, or
estimator-inadequate prior data fails closed. Prior age is informational, not a
freshness gate; coefficient drift remains a limitation, and the prior is not
current calibration, alpha evidence, or performance evidence.

## Authorization

No guarded unit may run without the create-only receipt at:

```text
/etc/liquidity-migration/account-execution-operational-ready
```

The supported issuer is root-only and holds the canonical host maintenance
lock plus the legacy deploy and reset leaves from before the deployed checkout
is opened or imported until receipt publication returns. After the shell opens
those inherited descriptors, both the installed helper and the issuer validate
their exact root-owned, single-link inode and Linux mount identities. This
excludes cooperating deploy and reset operations across a rolling lock-protocol
upgrade.
Raw `python -m liquidity_migration.operational_runtime_authority issue` is not a
supported substitute: it has already imported checkout code before it can lock,
so it refuses issuance without the exact pre-import descriptor handoff from
`scripts/ops.sh`. Direct `verify` and `verify-runtime` remain read-only paths.

The issuer and verifier bind:

- one exact clean Git commit and repository path;
- the machine identity and selected profile;
- strict environment-file identities (`0600` for demo/private files; root-owned
  `0640` for the paper route and sleeve file);
- absolute, real, owner-controlled, pairwise-disjoint account, inbox, and
  capture roots;
- the candidate universe used as the owner symbol file;
- complete candidate-to-demo-rule coverage;
- risk-policy and credential-file identities;
- resolved sleeve toggles and raw-persistence/liveness settings;
- the exact shared strategy-target tape derived from each authorized capture
  root;
- `paper_execution_model_scope=integration_only_uncalibrated` for
  `operational` (and `not_applicable_no_paper` for `demo-operational`).

Every byte and recorded identity field of a bound environment or runtime-input
file, every raw tracked regular-file or symlink byte and Git mode, every bound
root identity, the exact checkout commit, the normalized machine identity, and
the profile must still match. Runtime verification also rejects mainnet
variables, ambiguous `REAL_MONEY`, unauthorized units, alternate unit
fragments/drop-ins, and unregistered command lines. The exact workload argv is
owned by `scripts/run_authorized_runtime.sh` in the authorized commit.

Issuance requires the exact nine guarded services and three triggering timers
to be loaded and inactive. The trusted, fixed-environment systemd observation
runs before source capture and at both later precommit phases. Git verification
uses an isolated temporary index and explicit, minimally configured Git
directory/work-tree command, so ambient `GIT_*` variables, replacement refs,
and ordinary index flags cannot redirect or hide the tracked comparison. It
also walks the exact commit tree and hashes raw descriptor-read worktree bytes
and symlink targets against each blob, independently of clean filters,
line-ending normalization, or attributes; unsupported gitlinks fail closed.
Non-ignored untracked paths are rejected, while ignored paths remain outside
this claim. At both precommit phases the issuer also reopens the machine
identity, environment files, runtime inputs, and roots and compares them with
the receipt payload.

The 1 MiB-bounded receipt is first written and fsynced as a root-owned mode-`0400`
staging inode. That same inode is linked create-only at the final name and the
staging link is removed; it remains mode `0400`, single-linked, and invalid to
consumer receipt loaders through the final source and systemd checks. Descriptor
`fchmod` to profile mode `0600` or `0640` is the authority commit, and no
semantic check follows it. Thus the systemd path condition is only an existence
precheck: `verify-runtime` still rejects an interrupted mode-`0400` final file
before executing the registered command.

A hard interruption may leave a randomized hidden mode-`0400` staging file or
an invalid mode-`0400` final file. An interruption after the permission commit
may instead leave a valid receipt even if the issuer did not print success.
Preserve and inspect such artifacts before deliberate cleanup or reissue. The
protocol assumes a cooperative root-controlled host: systemd cannot identify
manual processes and advisory locks cannot constrain clients that ignore them.
It is local point-in-time authorization, not continuous monitoring, signed
attestation, WORM evidence, or mainnet authority.

Authorization follows stopped installation and precedes activation. It cannot
be inferred from test success, a research result, a notification, or an old
receipt. See `docs/operations.md` for the exact sequence.

## Environment boundary

The demo owner reads:

- `/etc/liquidity-migration/bybit-demo.env`
- `/etc/liquidity-migration/account-execution.env`
- `/etc/liquidity-migration/sleeves.resolved.env`

The paper owner reads
`/etc/liquidity-migration/account-paper-execution.env` and the resolved sleeve
file. Target producers inherit only the public/route values they need and
explicitly unset private API, mainnet, `REAL_MONEY`, and Telegram variables.
Paper runtime verification reopens only those paper/non-secret files; the
full-profile issuer binds the demo credential file once while the fleet is
stopped. Paper units explicitly pin `REAL_MONEY=false`, reject inherited
exchange credentials, and mount repository code read-only.

On the small operational host, each demo target producer owns one bounded
public kline store. Its paper counterpart keeps a distinct strategy root and
can read only the leader's group-bound kline snapshot path; CONTINUOUS also
reads the leader's single group-bound RMOM file. Other demo strategy files are
not recursively exposed. Missing bars retain the public REST fallback without
creating another bulk collector or WS bootstrap.

Demo and credential environment files remain root-owned mode `0600`. The paper
route and resolved sleeve files are root-owned mode `0640` for the dedicated
non-login `liquidity-migration-paper` group. Paper candidate, rule, and risk
inputs are isolated byte-exact mirrors owned by that runtime user at mode
`0600`; candidate/rule coverage is proved once against the original demo-bound
source during issuance. All environment files are strict `KEY=VALUE` data and
are parsed, never sourced as shell programs. Duplicate keys, shell syntax,
aliases, nested roots, and unknown real-money spellings are refused.

`deploy/sleeves.env` sets the repository ceiling. A host override may turn a
repo-enabled sleeve off but cannot resurrect a repo-disabled sleeve. An off
sleeve stops target publication; it does not cancel, close, or zero prior state.

## Startup and shutdown

Activation requires a quiescent fleet and starts owners before producers:

1. verify authority and demo-key permission;
2. start demo owner and wait for same-invocation health/readiness;
3. for `operational`, start paper owner and wait for readiness;
4. start enabled LONG and CONTINUOUS producers;
5. seed/enable RMOM, then enable hedge and liveness timers;
6. verify every required unit active/enabled and every forbidden unit off.

On shutdown or retirement, stop producers before owners. Turning a sleeve off
does not flatten it: publish zero targets through the owner, wait for canonical
fills and reconciliation, and prove venue/journal flatness first.

## Failure and recovery

The runtime fails health closed on stale market data, missing rules, rejected or
unresolved commands, journal corruption, position/order mismatch, missing
native protection, or a private execution/order WebSocket that lacks any of:
socket liveness, positive authentication, or positive acknowledgements for both
execution and order subscriptions. Private-stream readiness is probed on every
owner-loop iteration and again at exposure-increasing admission. Any failed or
unconfirmed condition blocks owner health and new exposure immediately; after
`ACCOUNT_PRIVATE_WS_RECONNECT_SECONDS` continuously not ready, the owner builds
and subscribes one replacement in a background thread. Every authentication
generation must obtain fresh subscription acknowledgements; old ACKs do not
survive an internal reconnect. Attempts use the same configured value as a
cooldown to avoid authentication storms. A candidate gets ten seconds to prove
readiness in the background; the prior stream remains published until that
proof succeeds, and a recovered prior stream wins over the candidate. REST
reconciliation and strict risk-reducing requests remain available while the
handshake runs. An unavailable/ambiguous socket probe fails health closed but is
not enough evidence to destroy and recreate a possibly live authenticated
connection.
Unsafe root/config changes and authorization drift also fail health closed.

Do not repair a failed activation by hand-starting units, editing the receipt,
adding a systemd override, or deleting journal evidence. Preserve the exact
failure state, stop unsafe writers, and diagnose against the installed commit.
Ledger reset is permitted only after authenticated demo flatness and produces a
verified archive before a new epoch.

## Evidence boundary

Demo operation can observe venue order lifecycle, fills, latency, fees, funding,
and operational reliability for its exact epoch. Paper operation validates only
the software path against its declared model; its modeled prices, fills, fees,
and timing support no execution-quality or performance claim. A
venue-accounting receipt proves only its named journal/venue interval.

Mainnet requires a separate decision-grade evidence pack, explicit capital and
risk limits, monitoring/recovery, expiry, and narrow owner authorization. None
of the operational receipts in this repository satisfies that boundary.
