# Demo execution calibration v1

Status: prospective and outcome-unseen at registration. Forward-execution
evidence only; no alpha, LONG/CONTINUOUS parity, deployment, or real-money
claim.

## Claim and decision

Estimate a Bybit demo market-order execution twin for the single account owner:
feed latency, decision-to-socket delay, request/ack RTT, clock-adjusted entry and
response latency, fill spacing/partial-fill frequency, visible-book walk,
residual slippage, fees, closed P&L, and funding. A passing receipt may unblock
the deterministic paper owner. It cannot establish strategy parity or authorize
the full deployment.

The runtime source must be one exact clean commit containing this contract. The
target-sequence receipt records that commit, event-tape hash, account-journal
head, and demo-rule receipt. Previously accumulated sleeve-local demo rows are
spent operational history and are excluded by the fresh account epoch.

## Fixed sample plan

- Environment: Bybit `api-demo`, USDT linear, never mainnet.
- Symbols, in order: `BTCUSDT`, `ETHUSDT`, `BUSDT`.
- Five round trips per symbol, iterating the symbol order within each round.
- Direction alternates deterministically by `(round + symbol_index) % 2`.
- One position at a time; every open must converge before its matching flat.
- Explicit notional: 30 USDT per open; leverage: 2; post-fill hold: one second.
- Target author: target-only `execution-calibration-v1` through the HEDGE
  adapter and canonical account inbox. It receives no Bybit credentials.
- Scheduling: every transition is durably appended to the shared hash-chained
  `StrategyEvent` tape before publication. Resume is allowed only from an exact
  verified plan prefix and immutable request identities.
- This produces 30 target transitions/order commands/fills and 15 reductions
  when every request succeeds. Zero-fill terminal orders do not count.
- A separate final 30-USDT `BTCUSDT` funding hold may be appended. Its close
  timestamp must be registered before the open from Bybit's published next
  funding time, no more than 24 hours ahead, and must be after the settlement.

The 30-USDT value is an experiment-specific exposure, not a runtime resize
floor. Each probed demo minimum must be no more than 80% of it; otherwise the
sequence refuses rather than changing size after inspecting fills.

## Clock and calibration gates

Before calibration, collect 21 unauthenticated `api-demo` server-time samples
on the VPS, retain the five lowest-RTT samples, and use their median midpoint
offset. `timedatectl` must report NTP synchronized. The selected RTT ceiling is
250 ms and the conservative offset-error ceiling is 50 ms. The receipt is
self-hashed and must be no older than 24 hours.

The execution-twin floors remain those registered in
`docs/account_execution_cutover.md`: 5,000 clock-adjusted feed observations, 30
targets, 30 commands, 30 request/ack samples, 30 filled orders, 10 P&L events,
three symbols, 95% command/book linkage, 99% nonnegative clock-adjusted latency,
and 99% reference match within 0.01 bp. Do not lower a floor after viewing the
epoch.

Bybit depth is market-by-price. Passive queue position is not identifiable, so
the result must keep passive queue calibration false. Observed multifill and
incomplete-fill rates are reported even if zero; absence in this bounded sample
is not proof that partial fills cannot occur.

## Abort and non-conclusions

Abort on stale/blocked owner health, foreign exposure, any simultaneous working
order, rejected target, convergence timeout, route/rule mismatch, capture gap,
or non-flat round-trip boundary. Keep the sole owner running for protection and
strictly reducing recovery if exposure remains. Resume cannot overwrite a
changed plan or tape.

This sequence does not replace actual LONG/CONTINUOUS target-tape comparison,
historical/paper replay, venue accounting, final flatness, or owner-first start
evidence. Those gates stay open even if calibration passes.
