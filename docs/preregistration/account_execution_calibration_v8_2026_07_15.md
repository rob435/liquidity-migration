# Demo execution calibration v8

Status: prospective and execution-outcome unseen. Registered at
`2026-07-15T02:18:26Z`, after the V7 operational failure and verified-flat
recovery, but before any V8 target, order, fill, slippage, fee, realized P&L,
funding, or calibration result. Study mode is `forward_execution`; deployment
is bounded Bybit demo only and authorization remains `unauthorized`. This is no
alpha, LONG/CONTINUOUS parity, HFT, deployment, or real-money claim.

Prospective runtime-binding clarification on 2026-07-15, before any V8 target
or outcome: the demo route must explicitly set
`ACCOUNT_RAW_MARKET_PERSISTENCE=1`. A machine/clean-commit/input-bound
`calibration` authorization may start only the demo account owner; paper,
ordinary producers, hedge, refresh, and liveness remain unauthorized for this
epoch. This receipt supplies startup authority only and cannot satisfy any V8
sample, clock, smoke, partial-fill, accounting, or result gate. After V8 closes,
the owner must stop and preserve it before any permanent raw-disabled
operational authorization is issued.

## Claim, action, and exposure boundary

V8 asks whether one exact replacement candidate fixes the two V7 owner defects
and can complete the already frozen bounded calibration lifecycle under the
unchanged safety, sample, clock, smoke, and partial-fill rules. A full pass may
only construct the execution-twin receipt, fill the compatibility
`v7_training` role in downstream schemas, and permit an owner-only paper
startup check. It cannot start paper producers, the natural holdout, a deploy,
or mainnet by itself.

V7 exposed the complete sequence of 30 transitions (15 round trips), its
execution observations, and one funding hold. It then failed on the funding close. All V7
facts and the recovery fill are spent and excluded from every V8 count,
distribution, threshold, and artifact. V8 uses a new forward-time surface and a
new six-root account/inbox/capture epoch. Reopening is justified only by the
corrected operational defects; no V7 latency, slippage, fee, P&L, funding, or
fill-distribution result selected a parameter or rule.

There is one allowed V8 candidate and one fixed epoch. The effective sample
units are the preregistered orders/fills and within-order fill spacings below;
three symbols are descriptive coverage, not independent trials. There is no
variant search or multiplicity selection. Any abort is the V8 result. Do not
resume, resize, extend, reset, relabel, or combine it with V7.

The exact registered plan id is `demo-calibration-20260715-v8`.

## Prospective corrected-defect boundary

The replacement candidate changes only the owner paths exposed by V7 plus
their focused evidence:

1. Funding/journal recovery runs before direct account reconciliation. Position
   truth is timestamped only after the REST position response, and the owner
   uses the journal's verified immutable cache/read-only state snapshots instead
   of repeatedly rebuilding or deep-copying the growing journal. Hot journal
   appends advance the already-verified transaction-store signature without
   reparsing or re-globbing every immutable segment. The strict
   reduction truth age remains `2 * reconcile_seconds`; the ordinary
   two-second cadence and four-second limit are unchanged.
2. Terminal reduce-batch Close and provisional P&L are built from state re-read
   inside one serialized journal transaction. Concurrent private-stream and
   REST redelivery can append that pair once or observe it as complete; it
   cannot propose changed immutable content from a stale pre-transaction
   snapshot.

Focused tests must reproduce the old stale timestamp, funding-before-position
ordering, uncached funding scan, append-time transaction-history rescan, and
concurrent Close collision before proving the repair. The complete local
candidate gate and non-contacting exact-head
Linux candidate gate remain mandatory. No evidence threshold or retry bound is
weakened.

## Fresh-epoch boundary

Before V8 emits a target:

1. freeze one exact clean replacement commit after focused tests; then, without
   editing that commit, pass repository-wide Ruff/full pytest, scoped mypy,
   import/package integrity, all current cutover gates, and the exact
   non-contacting `candidate-ci` run;
2. stage only that commit while every project unit is stopped and verify its
   host identity and effective demo-only configuration;
3. re-prove zero Bybit demo positions, zero regular orders, and zero conditional
   orders;
4. preserve the failed V7 receipt and external ledger, then archive/reset all
   six demo/paper account, inbox, and capture roots into a new verified V8
   epoch; no V7 journal, capture, request, or event prefix may enter it;
5. start the demo account owner alone, require fresh exact-head health and all
   three growing L2 books, then capture a new independent schema-v2 clock
   receipt;
6. verify a current strict demo-rule receipt for every registered symbol and,
   before an optional funding hold opens, record its venue-published BTC
   close-not-before timestamp. Paper and every ordinary producer remain
   stopped.

The existing rule receipt may be reused only if its current verifier accepts
the exact environment, rows, age (no more than 168 hours), and hashes. Otherwise
probe again while flat using the already registered 100-bp PostOnly distance,
no more than 200 USDT notional, no more than five private requests per second,
and no more than 10x tested leverage. The probe does not count toward V8.

## Fixed sample and safety plan

- Environment: Bybit `api-demo`, USDT linear; `demo=true`, `testnet=false`, and
  `REAL_MONEY` unset or explicitly false. Mainnet credentials are forbidden.
- Symbols in exact order: `BTCUSDT`, `ETHUSDT`, `BUSDT`.
- Five round trips per symbol; direction alternates by
  `(round + symbol_index) % 2`.
- One position at a time; every open converges before its matching flat.
- Requested notional: 160 USDT; leverage: 2; post-fill hold: one second.
- The request must remain at least 2.5 times every verified minimum. Rounding
  toward zero must preserve the registered 25% executable buffer; otherwise
  reject before publication.
- Calibration-only caps: 200 USDT component/symbol/account gross, 100 USDT
  initial margin, 2x leverage, and an explicit 2% native disaster stop.
- Credential-free author `execution-calibration-v1` publishes through the HEDGE
  adapter and canonical inbox. Every transition enters a fresh hash-chained
  `StrategyEvent` tape before publication.
- Thirty transitions must produce 30 commands/fills and 15 reductions when all
  requests succeed. Zero-fill terminal orders do not count.
- One final 160-USDT `BTCUSDT` funding hold may be appended only with a fresh
  venue-published close-not-before timestamp fixed before its open, no more than
  24 hours ahead and after settlement.

Queue-head market warmup remains at most 30 seconds. Position truth and health
freshness, native protection, exact-head binding, convergence, route/rule
identity, and account ownership remain fail-closed.

## Clock, sample, and decision rules

The independent clock receipt remains schema v2 from official
`https://api-demo.bybit.com/v5/market/time`: one preconnected TLS/HTTP1.1
session, 21 samples, select the five lowest RTTs, NTP synchronized, selected RTT
no more than 250 ms, estimated maximum midpoint error no more than 100 ms, age
no more than 24 hours, and abort on reconnect.

The market-order smoke floors remain: 5,000 adjusted feed observations, 30
targets, 30 commands, 30 request/ack samples, 30 filled orders, 10 P&L events,
three symbols, at least 95% command/book linkage, at least 99% nonnegative
adjusted latency, and at least 99% reference match within 0.01 bp. Point
estimates retain the clock-error interval.

The full schema-v3 execution-twin gate additionally requires at least three
observed multifill orders, at least three positive within-order spacing samples,
and clock-adjusted socket-send-to-first-fill plus exchange-fill-to-local-fill
response samples for every filled order. Equal venue timestamps prove multifill
but not positive spacing. If every smoke floor passes without enough spacing,
the result is `supports` only for bounded market-order smoke and `inconclusive`
for partial-fill calibration; paper remains blocked. Do not alter the sample to
manufacture multifills. Passive queue remains unidentifiable and
`passive_queue_calibrated=false`.

Any target rejection, owner error, unexpected blocked-health reason,
reconciliation mismatch lasting more than ten seconds, exact-head health that
does not rebound, journal conflict, foreign exposure, simultaneous working
order, convergence timeout, route/rule drift, capture gap, clock failure,
missing/invalid native stop, or non-flat round-trip boundary closes V8 as
failed. After a venue-mutating failure, use only a separately labelled canonical
strictly reducing recovery target, retain every failure artifact, and stop
flat. Recovery never counts toward V8.

## Required evidence and explicit non-conclusions

The external create-only evidence ledger must bind the exact commit/tree,
candidate local/CI receipts, host/machine/config identity, reset archive and
fresh roots, rule and clock receipts, event tape, calibration run receipt,
journal/capture identities, full twin receipt, every failure/deviation, and
final authenticated venue/local flatness. Expected run artifacts are stored
outside the repository in a new private V8 evidence directory and may not
overwrite V7.

A full pass is valid only for bounded demo market-order execution calibration
on the exact candidate, symbols, size, and observed epoch. It does not identify
partial-fill probability or tails, future book mutation, market impact, passive
queue, alpha, strategy parity, live capacity, deployment readiness, or
authorization. Downstream tools and schemas may retain lexical `V7`,
`v7_training`, and `v7-archive` names as compatibility labels for the single
passing preregistered training epoch. For this cutover those fields must bind
V8; the failed V7 receipt is forbidden from satisfying them.

## Pre-V8 rule-probe observability amendment

Registered prospectively at `2026-07-15T11:49:51Z`, after the rule-probe
failure below but before any V8 owner startup, target, order, fill, clock
receipt, or calibration outcome. Candidate
`181027b0853db9e543e30504211d701c7c95fc86` passed its complete local,
canonical pre-push, and single exact-head noncontacting Linux gates, installed
cleanly on the stopped VPS, and produced a passing six-root reset receipt. Its
first credentialed schema-v3 rule probe then failed before V8: the accepted
BUSDT PostOnly order had exact create/cancel identity, no execution rows, and a
terminal `Cancelled` row with zero cumulative quantity/value, but that row
first appeared on poll 11 at the five-second deadline. The required second
terminal confirmation therefore could not be collected. Cleanup and final
authenticated flatness both passed with zero positions and zero orders. The
failed probe receipt self-hash is
`a4928f48df13011e8fe84aad93eff6c46deb570fbed40dd76a7c2ca7c4e2d4dd`.

That result spends candidate `181027b` for operational evidence. It does not
spend or reveal the V8 sample because the owner and calibration driver never
started and the fresh account/inbox/capture roots remained empty. The next
candidate may change only the rule probe's bounded, read-only terminal-history
observation defaults from five seconds/50 polls to 30 seconds/100 polls. It
must still require at least two exact `Cancelled` observations, zero cumulative
fill quantity/value, empty exact-identity trade history, the unchanged five
private requests/second ceiling, cleanup, and final flatness. The new candidate
must repeat every candidate gate and use a new create-only probe path; the
failed receipt remains negative evidence and cannot be relabelled or combined
with a later pass.
