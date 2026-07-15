# Demo execution calibration v7

Status: **closed and spent after the final funding-hold close failed**. This
contract was prospective and execution-outcome unseen at registration. It
remains the historical statement of the V7 plan and abort rule; its prospective
corrected-defect successor is
`account_execution_calibration_v8_2026_07_15.md`. Forward execution evidence
only; no alpha, LONG/CONTINUOUS parity, deployment, HFT, or real-money claim.

## Observed outcome

V7 ran on exact host commit
`98b3916a4a135df3508f051f2354bc2346904690`. This was an operational
deviation from the later exact-candidate sequence: candidate
`c7d6509d3a21c75db77ed9486129a3cc4cfaa591` subsequently passed its local and
non-contacting Linux candidate gates but was never installed on the host. V7
therefore cannot support that candidate even absent the failure below.

The fixed sequence of 30 transitions (15 round trips) completed and the
preregistered BTC funding hold opened `+0.002 BTC`. Its close-not-before boundary was
`2026-07-14T08:02:00Z`; the exact zero replacement was durably published at
`2026-07-14T08:02:06Z`. The run then closed as failed under the unchanged owner
error rule because strict reduction admission repeatedly saw position truth
roughly 9--20 seconds old against the four-second freshness limit. The failed
receipt reports `status=failed`, `account_flat_after=false`, plan hash
`57aac4431f792c72ef0d406f86573729412ca63e912c179574d9b8c126be7af6`,
self-hash
`5b502b9194e6dc38b80560ac8a487e193eeb367f55d350b527effb281ee746da`,
and receipt-file SHA-256
`de28cb15729af45299a9975deb33cd33e00e653ca3e7d9d83bd165125216e948`.
Its retained host path is
`/var/lib/liquidity-migration/cutover-evidence/20260713T225317Z/demo-calibration-v7-run.json`.
No threshold was changed and the failed step was not resumed.

A separately labelled HEDGE-authored canonical recovery target
`target-982890e451c5713e4f7d770cc03339f38d94988fb480916308100df060059f52`
closed the position through reduce-only command
`71627988-4e7e-5ccb-9974-e25ffd1fcce2`. The recovery-only owner then failed
closed when concurrent REST redelivery proposed changed content for existing
Close event `1486ed03-0c9d-5e76-aaba-4ce80e6c9870`. Final source-reopened and
authenticated venue proof established journal integrity at 6,804 events, head
`dce7856a44e2c3e76c0caec045bf98453552fc355fccd5b3915d3175de98cad9`,
zero aggregate/component target, zero local/venue position, zero regular or
conditional order, and no active project unit.

V7 is valid negative evidence for operational reliability on its exact old
runtime and contradicts its pass rule. Its execution observations are spent,
are excluded from every successor floor, and cannot be merged into V8 or the
natural holdout. Paper and ordinary producers never started; no deployment or
activation marker was issued.

## Prospective pre-run amendment — 2026-07-14T10:51:30Z

This amendment was registered before any V7 target, order, fill, slippage, fee,
P&L, funding, or calibration outcome was observed. V7 has not started and no V7
sample exists. The `demo-calibration-20260714-v7` name is retained for the fresh
epoch; the original registration below is preserved rather than rewritten.

The candidate V7 commit now contains calibration-contract changes beyond the
original health-publication-only delta. Its exact clean identity is not frozen
yet and may also contain separately reviewed account-ownership, public/private
market-data-boundary, and deterministic-comparator hardening. Those changes
must pass their own tests and review. The fixed symbol/target sequence, $160
notional, risk envelope, operational abort rule, clock rule, and all original
latency/slippage sample floors remain unchanged.

The decision boundary changes prospectively as follows:

- The original execution-twin sample gates are now the
  `market_order_smoke_gate_passed` gate. They can support only the bounded V7
  market-order feed, request/ack, book-linkage, fee, and slippage smoke claim.
- Receipt schema v3 retains the v2 `min_observed_multi_fill_orders=3` and
  `min_partial_fill_spacing_samples=3` rule and additionally requires a
  clock-adjusted socket-send-to-first-fill and exchange-fill-to-local-fill
  response sample for each filled order. An observed multifill order is a command
  with multiple positive fills. A spacing sample requires two positive fills for the same
  command with valid strictly increasing venue timestamps. Equal timestamps
  establish multifill behavior but leave spacing interval-censored at the venue
  timestamp resolution. Terminal incomplete single-fill orders remain reported,
  but do not satisfy the multifill floor. Both requirements must pass before
  partial-fill timing/behavior is called calibrated.
- `execution_twin_gate_passed` is the conjunction of the smoke gate and the
  separate partial-fill gate. Paper-owner startup and cutover acceptance remain
  blocked when only the smoke gate passes.
- A smoke-only receipt can be inspected offline only with the full-gate check
  explicitly disabled. Its config sets `allow_partial_fills=false`, uses
  `fill_spacing_ns=0`, and applies
  `single_level_full_fill_or_reject`: any order that would require multiple book
  levels or an incomplete fill is rejected. Zero is a marker that no split-fill
  timing is modeled, not a 0-ns latency estimate. The former 1-ns fallback is
  prohibited.
- The order command's immutable creation timestamp anchors decision-to-socket
  timing. It cannot precede the linked book's local receive timestamp, and age is
  measured at socket send. API create-response timing remains an API boundary;
  it is not substituted for exchange first-fill or local fill-response timing.
  Schema-v2 receipts are not accepted for paper configuration under this
  corrected model.

The fixed small-order V7 sequence does not guarantee a multifill. If it reaches
all original floors with no identifiable partial-fill spacing, retain the epoch
as a passing market-order smoke result and an inconclusive partial-fill result;
do not resize, extend, reset, or retry opportunistically. Paper remains blocked.
A later targeted partial-fill study would need a new prospective size/risk,
sample, and stopping rule without merging its observations into V7.

Even when the new minimum passes, three repeated observations are only a bounded
existence/timing basis. They do not resolve a multifill frequency distribution,
make `p75`/`p95`/`p99` empirical tail quantiles at N=3, identify partial-fill
probability outside the sample, future book mutation, market impact, or passive
queue position. Those higher labels remain explicit stress choices. The
market-by-price queue limitation in the original registration remains binding.
The paper twin's split quantities still follow immutable decision-book levels;
an observed multifill does not prove a one-to-one mapping between MBP levels and
venue execution partitions.

## Prospective runtime-safety amendment — 2026-07-14T23:16:37Z

This amendment was registered before any V7 target or execution outcome. It
does not change the fixed V7 sequence, risk envelope, clock rule, sample floors,
or abort rule. Non-finite rule-age/readiness values now fail rather than disable
their comparisons: the owner accepts no more than 168 hours of rule age and 30
seconds of queue-head market warmup, validated before credentials or startup.
The credential-free target producer requires `REAL_MONEY` to be unset or
explicitly false before marker or Git inspection. Private Bybit HTTP/WebSocket
clients accept only `api-demo` (`demo=true`, `testnet=false`), and mutations
revalidate that realm before the account-bound lease. Fresh-epoch root values
are reopened through the authority verifier and transferred to Bash only as
NUL-delimited data; generated systemd EnvironmentFiles are never
shell-evaluated. The older private Bybit and account-route EnvironmentFiles are
also descriptor-read as current-user-owned, single-link, exact-mode-`0600`
data, with only fixed allowlisted keys transferred; sleeve toggles use a strict
three-key data parser. This is machine enforcement of the already registered
demo-only and bounded-freshness contract, not a post-result revision.

## Original V7 registration (retained)

## Revision boundary

V1 failed rule feasibility before order submission. V2 failed static
quantity-step headroom. V3 failed its registered clock-error ceiling before a
target. V4 spent its first target on bounded reconciliation and competing-ACK
races. V5 proved those repairs but exposed a protection-state ordering defect.
V6 proved the retained-stop repair across four real closes, then stopped after
event 9 because owner health remained one journal sequence behind long enough
to fail the exact-head gate.

The V6 mismatch follows directly from two registered owner cadences:
reconciliation appends an immutable venue-snapshot every two seconds, while
unchanged health normally publishes every five seconds. A health file can be
fresh and semantically healthy yet deliberately bind the preceding journal
head. The consumer correctly rejects it; a fixed retry cadence is not a proof
that it will observe the shorter exact-match windows.

V7 changes the owner publication invariant rather than weakening the consumer.
The owner republishes health after every newly observed journal sequence/hash,
including reconciliation and asynchronous execution events. A journal-only
publication reuses the last verified wallet snapshot. Wallet REST is refreshed
only after a completed request, a status/detail/readiness change, or the
existing five-second interval, so head binding does not create an API burst.
The health artifact remains atomic, exact-head validation is unchanged, and a
concurrent append still invalidates the old health until the next owner loop.

This repair was selected only from V6 operational failure timing, not from its
latency, slippage, fee, P&L, or fill-distribution result. V6's nine target events,
their account events, captures, clock receipt, and prospective funding input are
excluded from every V7 floor. The exact registered plan id is
`demo-calibration-20260714-v7`; V6 is non-resumable.

## Fresh-epoch boundary

Before V7 emits a target:

1. validate journal-only health publication, exact-head rebound, unchanged
   wallet-refresh cadence, the prior protection transition, and all account
   gates; then run the complete local suite and remote Linux smoke suite on one
   exact clean commit;
2. stage that commit while every project unit is stopped;
3. re-prove zero demo positions plus zero regular and conditional orders;
4. archive/reset the V6 demo and untouched paper account, inbox, and capture
   roots into a new verified archive; do not reuse the V6 journal or capture
   sample;
5. start the demo account owner alone, require fresh exact-head health and all
   three growing L2 books, then capture a new independent schema-v2 clock
   receipt;
6. register the venue-published BTC funding timestamp before the optional hold
   opens. Paper and every ordinary strategy producer remain stopped.

The existing rule receipt may be reused only while its strict verifier still
accepts its age, environment, self-hash, and exact rows. Its registered identity
is:

- receipt file SHA-256:
  `a5053de858bceeafc8ca76c1a902719b7fad184cc26e3b0b42b3502b7babc756`;
- self-hash:
  `ae4f4916cfa7e0ec7200c832af0e1100ceda2d78b805f46e6eac3d1a92427c7a`;
- observed minimum notionals: `BTCUSDT=62.1029`, `ETHUSDT=17.6703`,
  `BUSDT=5.05579` USDT.

If the receipt expires or current rules disagree, probe again while flat and
bind the replacement before the V7 target. Do not silently retain these
numbers.

## Fixed sample plan

- Environment: Bybit `api-demo`, USDT linear, never mainnet.
- Symbols in order: `BTCUSDT`, `ETHUSDT`, `BUSDT`.
- Five round trips per symbol; direction alternates by
  `(round + symbol_index) % 2`.
- One position at a time; every open converges before its matching flat.
- Requested notional: 160 USDT; leverage: 2; post-fill hold: one second.
- The request remains at least 2.5 times every verified minimum. Venue-step
  rounding toward zero must preserve the registered 25% executable buffer; a
  larger step rejects rather than silently becoming zero.
- Calibration-only risk envelope: 200-USDT component/symbol/account gross caps,
  100-USDT initial-margin cap, 2x leverage cap, explicit 2% native disaster
  stop. It intentionally blocks general strategy sizing.
- Target-only author `execution-calibration-v1` publishes through the HEDGE
  adapter and canonical inbox with no private credentials.
- Every transition is appended to a fresh common hash-chained `StrategyEvent`
  tape before publication. No V6 prefix or account event is imported.
- Thirty transitions produce 30 commands/fills and 15 reductions when every
  request succeeds. Zero-fill terminal orders do not count.
- Append one final 160-USDT `BTCUSDT` funding hold only with a freshly observed
  venue-published close-not-before timestamp registered before its open, no
  more than 24 hours ahead and after settlement.

## Clock and calibration gates

The clock receipt contract remains unchanged: schema v2, official
`https://api-demo.bybit.com/v5/market/time`, one preconnected TLS/HTTP1.1
session, 21 samples, five lowest RTTs, NTP synchronized, selected RTT no more
than 250 ms, estimated maximum midpoint error no more than 100 ms, age no more
than 24 hours, and abort on reconnect. A fresh V7 receipt is required even
though V6 passed.

The execution-twin floors remain unchanged: 5,000 adjusted feed observations,
30 targets, 30 commands, 30 request/ack samples, 30 filled orders, 10 P&L
events, three symbols, 95% command/book linkage, 99% nonnegative adjusted
latency, and 99% reference match within 0.01 bp. Runtime and receipt validation
must reject weaker floors. Point estimates remain bounded by the disclosed
clock-error interval.

Bybit depth is market-by-price. Passive queue position remains unidentifiable,
so `passive_queue_calibrated=false` regardless of whether V7 passes.
Multifill/incomplete-fill rates are reported even if zero; absence is not proof
they cannot occur.

## Decision and abort rule

Any target rejection, owner error, unexpected blocked-health reason,
reconciliation mismatch lasting more than ten seconds, exact-head health that
does not rebound, journal conflict, foreign exposure, simultaneous working
order, convergence timeout, route/rule drift, capture gap, clock failure,
missing or invalid native stop, or non-flat round-trip boundary closes V7 as
failed. Do not resume after a venue-mutating failure; flatten through a separate
canonical recovery target, preserve the failure, and register a new clean epoch
before another sample.

Even a passing V7 permits only construction of the execution-twin receipt and
paper-owner startup. Actual LONG/CONTINUOUS target tapes, common-clock replay,
venue accounting, funding, final flatness, owner-first evidence, and the
deployment authorization assessment remain independent open gates.

## Independent common-clock comparison contract

This clarification does not change V7's sample, abort rule, or decision rule
and cannot make a V7 outcome satisfy strategy parity. The independent
LONG/CONTINUOUS common-clock gate requires fresh non-empty historical, paper,
and demo `StrategyEvent` tapes produced from byte-identical immutable replay
input artifacts. For this cutover, the input is explicitly a captured
target/scheduling artifact consumed by a dedicated offline replay adapter—not a
claimed raw-market snapshot and not an unrelated file merely hashed by a live
daemon. Every event must bind that artifact with
`replay_input_sha256`. Each environment must also provide a separate
post-callback hash-chained decision tape with exactly one raw-event-id-keyed,
sorted `decision_keys` outcome per event (including an explicit empty list for
a no-decision cycle). Raw sources must be mapped explicitly per environment;
normalization may replace only those exact source labels and
`execution_environment`. Event time, phase/kind, source sequence, canonical
normalized event identity, remaining payload, and decision keys are exact
comparisons with no numeric tolerance. `ingest_ts_ns` remains bound in the raw
tape but is excluded from normalized parity because it is arrival telemetry,
not an order-key/input field.

The natural and parity artifacts are causally separate. LONG and CONT may share
one interprocess-locked capture path. After each production callback returns,
the producer re-reads the returned `PublishedTargetRequest` files under the
route-bound inbox lock; only an error-free, verifiable publication appends the
natural capture and companion outcome. Successful no-target callbacks are
explicit empty rows. Freeze that provenance artifact before the offline replay
creates historical, paper, and demo-labelled scheduling tapes from the same
bytes. Those labels do not represent account-owner or venue execution.

The machine receipt must reproduce from all nine bound replay files, pass each native
event/decision hash chain plus duplicate/backward/alignment checks, and pass
both normalized chain comparisons. A missing identity, changed input byte,
mismatch, corrupt tape, or ordinary live producer without a companion outcome
tape fails closed. The
receipt proves only normalized scheduling/input-declaration/decision-tape
equality. It does not authenticate market-data provenance, prove strategy or
configuration identity omitted from payloads, replace account-kernel parity,
or establish signal-selection parity, raw-market-tape parity, venue fills,
fees, P&L, funding, alpha, deployment readiness, or authorization. The replay
adapter and post-callback producer outcome code cannot satisfy the gate without
a fresh natural capture and successful offline receipt. Decision keys come from
durable published `AccountTargetRequest` intents; failed or unverifiable
callbacks leave an outcome missing rather than record an empty success. Actual
V7 demo orders/fills/accounting remain separate evidence and cannot be replaced
by the offline `demo` scheduling directory.
