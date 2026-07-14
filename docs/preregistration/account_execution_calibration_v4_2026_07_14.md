# Demo execution calibration v4

Status: prospective and execution-outcome unseen at registration. Registered
after rule/quantity feasibility and clock-transport diagnostics only. The demo
owner has run alone and stayed flat; no calibration target, market order, fill,
slippage, fee, realized P&L, or funding outcome has been observed. Forward
execution evidence only; no alpha, LONG/CONTINUOUS parity, deployment, HFT, or
real-money claim.

## Revision boundary

V1 failed rule feasibility before order submission. V2 failed static
quantity-step headroom. V3's first independent clock receipt failed its 50-ms
worst-case error ceiling:

- receipt file SHA-256:
  `da7f30efc9304ef28a954c1eff5a76c9be2e4d84800acbc518b41969f45baf21`;
- self-hash:
  `13bbcc099be2e1f760703fa03c16da9a5bdf4c5b4c9164222fe597198c2e4a6c`;
- NTP synchronized: true;
- five selected RTTs: 187.557--188.960 ms;
- estimated maximum error: 95.208695 ms;
- median local-minus-exchange correction: -8.954308 ms.

Every schema-v1 request created a new TLS session. A subsequent unauthenticated
persistent-session diagnostic reduced the stable RTT only to roughly 169 ms,
showing that network geography, not a retryable handshake outlier, dominates.
No blind retry and no reinterpretation of the failed receipt is allowed.

V4 prospectively changes the transport and claim before any target: establish
one TLS connection first, collect all 21 HTTP/1.1 keep-alive samples on that
single session, abort instead of reconnecting, select the five lowest RTTs, and
use a 100-ms worst-case midpoint-error ceiling. This ceiling is derived from
spent transport-feasibility data and is proportionate to hourly/sub-hourly
strategy scheduling. The receipt must disclose its error bound. It is too weak
for HFT, queue timing, matching-engine entry, or sub-100-ms causal claims.

## Bound venue/rule inputs

The successful flat-account rule receipt remains unchanged:

- verified timestamp: `1783986138270164217` ns;
- receipt file SHA-256:
  `a5053de858bceeafc8ca76c1a902719b7fad184cc26e3b0b42b3502b7babc756`;
- self-hash:
  `ae4f4916cfa7e0ec7200c832af0e1100ceda2d78b805f46e6eac3d1a92427c7a`;
- observed minimum notionals: `BTCUSDT=62.1029`, `ETHUSDT=17.6703`,
  `BUSDT=5.05579` USDT.

The 160-USDT request remains at least 2.5 times every observed minimum. For any
positive current step notional `x <= requested`, rounding toward zero produces
`floor(requested / x) * x >= requested / 2`; every nonzero rounded target thus
retains at least 80 USDT, above the 77.628625-USDT registered buffer. A larger
step rejects rather than silently becoming zero.

## Fixed sample plan

- Environment: Bybit `api-demo`, USDT linear, never mainnet.
- Symbols in order: `BTCUSDT`, `ETHUSDT`, `BUSDT`.
- Five round trips per symbol; direction alternates by
  `(round + symbol_index) % 2`.
- One position at a time; every open converges before its matching flat.
- Requested notional: 160 USDT; leverage: 2; post-fill hold: one second.
- Calibration-only risk envelope: 200-USDT component/symbol/account gross caps,
  100-USDT initial-margin cap, 2x leverage cap, explicit 2% native disaster
  stop. It intentionally blocks general strategy sizing.
- Target-only author `execution-calibration-v1` publishes through the HEDGE
  adapter and canonical inbox with no private credentials.
- Every transition is durably appended to the common hash-chained
  `StrategyEvent` tape before publication. Resume requires an exact prefix and
  immutable request identities.
- Thirty transitions produce 30 commands/fills and 15 reductions when every
  request succeeds. Zero-fill terminal orders do not count.
- An optional final 160-USDT `BTCUSDT` funding hold may be appended only with a
  venue-published close-not-before timestamp registered before its open, no
  more than 24 hours ahead and after settlement.

## Clock and calibration gates

The clock receipt contract is exact: schema v2, official
`https://api-demo.bybit.com/v5/market/time`, one preconnected TLS/HTTP1.1
session, 21 samples, five lowest RTTs, NTP synchronized, selected RTT no more
than 250 ms, estimated maximum midpoint error no more than 100 ms, and age no
more than 24 hours. Any reconnect or parameter drift aborts.

The execution-twin floors remain 5,000 adjusted feed observations, 30 targets,
30 commands, 30 request/ack samples, 30 filled orders, 10 P&L events, three
symbols, 95% command/book linkage, 99% nonnegative adjusted latency, and 99%
reference match within 0.01 bp. Runtime/receipt validation rejects weaker
floors. The latency point estimates must be read with the recorded clock-error
interval; passing does not erase that uncertainty.

Bybit depth is market-by-price. Passive queue position remains unidentifiable
and `passive_queue_calibrated=false`. Multifill/incomplete-fill rates are
reported even if zero; absence is not proof they cannot occur.

## Abort and non-conclusions

Abort on stale/blocked owner health, foreign exposure, simultaneous working
orders, rejected targets, convergence timeout, route/rule mismatch, capture
gap, non-flat round-trip boundary, clock reconnect, or clock quality failure.
Keep the sole owner running for protection and strictly reducing recovery if
exposure remains.

This sequence does not replace actual LONG/CONTINUOUS target-tape comparison,
historical/paper replay, venue accounting, final flatness, or owner-first start
evidence. Those gates remain open even if calibration passes.
