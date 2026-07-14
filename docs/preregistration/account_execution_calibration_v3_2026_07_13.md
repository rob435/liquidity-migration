# Demo execution calibration v3

Status: prospective and execution-outcome unseen at registration. Registered
after rule feasibility and a static quantity-step audit only. No account owner,
calibration target, market order, fill, latency, slippage, fee, P&L, or funding
outcome has been observed. Forward execution evidence only; no alpha,
LONG/CONTINUOUS parity, deployment, or real-money claim.

## Revision boundary

V1's 20-USDT probe ceiling failed before order submission. V2 then used the
verified 62.1029-USDT BTC minimum to request 80 USDT, but static inspection
found that rounding toward the 0.001 BTC quantity step would return the target
to one approximately 62-USDT step. V2 is closed before owner startup rather
than relabeling requested headroom as executable headroom.

The same successful flat-account rule receipt remains the only viewed venue
input:

- verified timestamp: `1783986138270164217` ns;
- receipt file SHA-256:
  `a5053de858bceeafc8ca76c1a902719b7fad184cc26e3b0b42b3502b7babc756`;
- self-hash:
  `ae4f4916cfa7e0ec7200c832af0e1100ceda2d78b805f46e6eac3d1a92427c7a`;
- observed minimum notionals: `BTCUSDT=62.1029`, `ETHUSDT=17.6703`,
  `BUSDT=5.05579` USDT;
- probe quantity steps/prices: BTC `0.001 @ 62102.9`, ETH `0.01 @ 1767.03`,
  B `1 @ 0.10757`.

For any positive current step notional `x <= requested`, venue-step rounding
toward zero produces `floor(requested / x) * x >= requested / 2`. V3 therefore
requires requested notional to be at least `2 * 1.25 * observed_minimum` for
every symbol. The largest bound is `155.25725` USDT for BTC, fixed prospectively
at a round 160 USDT. Thus every nonzero rounded target retains at least 80 USDT
of executable notional, exceeding the registered 77.628625-USDT buffer. A
current step larger than the request still rejects; it never becomes a silent
zero.

## Claim and decision

Estimate a Bybit demo market-order execution twin for the single account owner:
feed latency, decision-to-socket delay, request/ack RTT, clock-adjusted entry and
response latency, fill spacing/partial-fill frequency, visible-book walk,
residual slippage, fees, closed P&L, and funding. A passing receipt may unblock
the deterministic paper owner. It cannot establish strategy parity or authorize
the full deployment.

The runtime source must be one exact clean commit containing this contract. The
target-sequence receipt records that commit, route-manifest hash, demo-rule file
and artifact hashes, event-tape hash, and account-journal head. Previously
accumulated sleeve-local demo rows are excluded by the fresh archived/reset
account epoch.

## Fixed sample plan

- Environment: Bybit `api-demo`, USDT linear, never mainnet.
- Symbols, in order: `BTCUSDT`, `ETHUSDT`, `BUSDT`.
- Five round trips per symbol, iterating the symbol order within each round.
- Direction alternates deterministically by `(round + symbol_index) % 2`.
- One position at a time; every open must converge before its matching flat.
- Explicit requested notional: 160 USDT per open; leverage: 2; post-fill hold:
  one second.
- Calibration-only risk envelope: 200-USDT component/symbol/account gross caps,
  100-USDT initial-margin cap, 2x leverage cap, and an explicit 2% native
  disaster stop. This envelope intentionally blocks general strategy sizing.
- Target author: target-only `execution-calibration-v1` through the HEDGE
  adapter and canonical account inbox. It receives no Bybit credentials.
- Scheduling: every transition is durably appended to the shared hash-chained
  `StrategyEvent` tape before publication. Resume is allowed only from an exact
  verified plan prefix and immutable request identities.
- This produces 30 target transitions/order commands/fills and 15 reductions
  when every request succeeds. Zero-fill terminal orders do not count.
- A separate final 160-USDT `BTCUSDT` funding hold may be appended. Its close
  timestamp must be registered before the open from Bybit's published next
  funding time, no more than 24 hours ahead, and must be after settlement.

The 160-USDT value is experiment-specific exposure, not a runtime resize floor.
The runner rejects a rule set unless requested notional is at least 2.5 times
every observed minimum, preserving the 25% buffer after worst-case nonzero
step rounding rather than only before it.

## Clock and calibration gates

Before calibration, collect 21 unauthenticated `api-demo` server-time samples
on the VPS, retain the five lowest-RTT samples, and use their median midpoint
offset. `timedatectl` must report NTP synchronized. The selected RTT ceiling is
250 ms and the conservative offset-error ceiling is 50 ms. The receipt is
self-hashed and must be no older than 24 hours. Parameters that differ from
this exact contract are rejected.

The execution-twin floors remain those registered in
`docs/account_execution_cutover.md`: 5,000 clock-adjusted feed observations, 30
targets, 30 commands, 30 request/ack samples, 30 filled orders, 10 P&L events,
three symbols, 95% command/book linkage, 99% nonnegative clock-adjusted latency,
and 99% reference match within 0.01 bp. Runtime and receipt validation reject a
weaker floor; do not lower one after viewing the epoch.

Bybit depth is market-by-price. Passive queue position is not identifiable, so
the result must keep passive queue calibration false. Observed multifill and
incomplete-fill rates are reported even if zero; absence in this bounded sample
is not proof that partial fills cannot occur.

## Abort and non-conclusions

Abort on stale/blocked owner health, foreign exposure, any simultaneous working
order, rejected target, convergence timeout, route/rule mismatch, capture gap,
or non-flat round-trip boundary. Keep the sole owner running for protection and
strictly reducing recovery if exposure remains. Resume cannot overwrite a
changed plan, tape, run receipt, or rule/route identity.

This sequence does not replace actual LONG/CONTINUOUS target-tape comparison,
historical/paper replay, venue accounting, final flatness, or owner-first start
evidence. Those gates stay open even if calibration passes.
