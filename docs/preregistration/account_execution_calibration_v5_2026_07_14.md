# Demo execution calibration v5

Status: prospective and execution-outcome unseen for the new epoch at
registration. Registered after the V4 first-target failure and verified-flat
recovery, but before any V5 target, order, fill, slippage, fee, realized P&L, or
funding outcome. Forward execution evidence only; no alpha, LONG/CONTINUOUS
parity, deployment, HFT, or real-money claim.

## Revision boundary

V1 failed rule feasibility before order submission. V2 failed static
quantity-step headroom. V3 failed its registered clock-error ceiling before a
target. V4 passed its clock gate, then spent its first target when two expected
asynchronous transitions exposed runtime races:

- venue REST briefly saw `BTCUSDT=0.002` while the journal still reconstructed
  zero, so generic blocked-health handling aborted without allowing the private
  execution stream its bounded propagation interval;
- the REST create response and private execution callback both reported the
  same accepted venue order, but different local observation timestamps and
  provenance caused the second report to look like an immutable ACK rewrite.

V5 changes only the concurrency protocol needed to collect the already-fixed
sample. After target publication, and only while waiting for that request or
its convergence, the exact `account reconciliation mismatch` health state may
recover for at most ten seconds. Preflight, between-step flat boundaries,
funding holds, and every other blocked-health reason still fail immediately.
Exceeding ten seconds still aborts.

ACK acceptance and venue identity are now decided against the fresh state
inside the journal transaction lock. The first semantically valid ACK owns the
single order-state transition. A later observer with the same acceptance and
compatible venue identity is a semantic no-op; if it is the HTTP create
response, its request/response timestamps are retained as a separate immutable
`ack_observation` fact so calibration does not discard valid timing when a fill
arrives first. Changed acceptance or two different nonempty venue order IDs
fail closed. Execution-ID redelivery follows the same semantic rule for local
receive time/provenance while requiring command, quantity, price, fee, and
exchange timestamp to agree within the declared `1e-12` numeric tolerance
(timestamps and identities remain exact).

These repairs were selected from V4 operational failure evidence, not from its
slippage, latency, fill-distribution, fee, P&L, or funding result. V4 is excluded
from all V5 floors. The exact registered plan id is
`demo-calibration-20260714-v5`; the V4 tape is non-resumable.

## Fresh-epoch boundary

Before V5 emits a target:

1. validate the fix with focused race tests, the complete local suite, and the
   remote Linux smoke suite on one exact clean commit;
2. stage that commit while every project unit is stopped;
3. re-prove zero demo positions plus zero regular and conditional orders;
4. archive/reset the V4 demo and untouched paper account, inbox, and capture
   roots into a new verified archive; do not reuse the V4 journal or capture
   sample;
5. start the demo account owner alone, require fresh healthy owner state and all
   three L2 books, then capture a new independent schema-v2 clock receipt;
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
bind the replacement before the V5 target. Do not silently retain these
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
  tape before publication. No V4 prefix or account event is imported.
- Thirty transitions produce 30 commands/fills and 15 reductions when every
  request succeeds. Zero-fill terminal orders do not count.
- Append one final 160-USDT `BTCUSDT` funding hold only with a freshly observed
  venue-published close-not-before timestamp registered before its open, no
  more than 24 hours ahead and after settlement.

## Clock and calibration gates

The clock receipt contract remains unchanged from outcome-unseen V4: schema v2,
official `https://api-demo.bybit.com/v5/market/time`, one preconnected
TLS/HTTP1.1 session, 21 samples, five lowest RTTs, NTP synchronized, selected
RTT no more than 250 ms, estimated maximum midpoint error no more than 100 ms,
age no more than 24 hours, and abort on reconnect. A fresh V5 receipt is
required even though V4 passed.

The execution-twin floors remain unchanged: 5,000 adjusted feed observations,
30 targets, 30 commands, 30 request/ack samples, 30 filled orders, 10 P&L
events, three symbols, 95% command/book linkage, 99% nonnegative adjusted
latency, and 99% reference match within 0.01 bp. Runtime and receipt validation
must reject weaker floors. The point estimates remain bounded by the disclosed
clock-error interval.

Bybit depth is market-by-price. Passive queue position remains unidentifiable,
so `passive_queue_calibrated=false` regardless of whether V5 passes.
Multifill/incomplete-fill rates are reported even if zero; absence is not proof
they cannot occur.

## Decision and abort rule

Any target rejection, owner error, unexpected blocked-health reason,
reconciliation mismatch lasting more than ten seconds, journal conflict,
foreign exposure, simultaneous working order, convergence timeout, route/rule
drift, capture gap, clock failure, or non-flat round-trip boundary closes V5 as
failed. Do not resume after a venue-mutating failure; flatten through a separate
canonical recovery target, preserve the failure, and register a new clean
epoch before another sample.

Even a passing V5 permits only construction of the execution-twin receipt and
paper-owner startup. Actual LONG/CONTINUOUS target tapes, common-clock replay,
venue accounting, funding, final flatness, owner-first evidence, and the
deployment authorization assessment remain independent open gates.
