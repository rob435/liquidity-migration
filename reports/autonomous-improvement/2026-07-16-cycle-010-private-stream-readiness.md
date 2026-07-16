# Autonomous improvement cycle 010: fail-closed private execution-stream readiness

## Finding

- Audit timestamp: `2026-07-16T02:51:35Z`.
- Audited commit: `cd2abdcbf87869af924d4ae931c15852e0d4b80d`,
  plus the named local changes from earlier cycles.
- The centralized demo account owner constructed and subscribed its private
  execution/order WebSocket once. It never called the existing
  `BybitPrivateWebSocketStream.is_connected()` probe, did not include stream
  state in `AccountHealthChain`, and published durable owner health from market
  readiness and REST position reconciliation alone.
- A permanently dead private socket could therefore leave owner health green
  and admit exposure-increasing requests. Two-second REST reconciliation
  reduced the duration of missing fill/accounting evidence but did not restore
  execution latency or the mutation callbacks.
- Git history confirmed this was a centralization regression. Commit `3abf674`
  supervised socket liveness; commit `27428d4` introduced the sole account
  owner without restoring that supervision.
- The first socket-level repair was still insufficient. Pinned pybit 5.16.0
  sends subscription requests and returns before the asynchronous venue ACK.
  On a negative subscription ACK, pybit can keep the socket connected while
  removing the callback. TCP liveness alone would still have reported a false
  healthy state.

Prospective inspection before implementation found zero source/test calls to
`.is_connected()` and 124 focused account/transport tests passing despite the
missing supervision.

## Implementation

- `BybitPrivateWebSocketStream` now tracks pybit's asynchronous control plane:
  socket state, positive authentication, and positive ACKs for both `execution`
  and `order` are all required for readiness.
- Every new authentication generation clears prior subscription ACKs. A pybit
  internal reconnect therefore cannot reuse evidence from the old socket.
- The wrapper fails startup if the exact pinned pybit control hooks are absent;
  a dependency upgrade cannot silently degrade this safety condition.
- `PrivateExecutionStreamSupervisor` probes readiness on every owner iteration
  and again at exposure-increasing admission. `False` and unknown readiness
  block owner health and new exposure immediately.
- A continuously not-ready stream starts one background rebuild after the
  configured 180-second default; the same bound is the retry cooldown, avoiding
  authentication storms. Provider construction and subscription never block
  the owner loop, REST reconciliation, or strict risk-reducing requests.
- A candidate gets a bounded ten-second handshake window and is not published
  until it proves full readiness. The exact incumbent is retained during that
  window. If it recovers, the candidate is discarded; a timed-out or rejected
  candidate is closed without replacing the incumbent. Publication uses an
  identity compare-and-swap before retiring the old stream.
- Durable owner health includes the precise authentication/subscription/socket
  reason. `ACCOUNT_PRIVATE_WS_RECONNECT_SECONDS` is passed by the authorized
  service wrapper, and the operational contract documents the behavior.

## Prospective regressions

The new tests prove that:

- a dead stream blocks immediately, then rebuilds and resubscribes only after
  the bound;
- a quiet connected and fully acknowledged stream never rebuilds;
- failed subscription setup retries after cooldown rather than latching;
- connected-but-unacknowledged readiness overrides raw socket liveness;
- a negative asynchronous order-subscription ACK remains unhealthy while the
  socket itself is connected;
- re-authentication invalidates old topic ACKs;
- admission actively reprobes rather than trusting cached loop health;
- a deliberately stalled subscription does not block the owner thread;
- an unready candidate times out without replacing or closing the incumbent;
- a recovered incumbent wins over an in-flight candidate; and
- unknown liveness blocks health without destroying a possibly live socket.

## Validation

- Final focused transport/supervisor suite: 104 passed in 2.94 seconds.
- Broader account owner, service, Bybit, and readiness focus under the exact
  locked Python 3.11.5 environment: 244 passed in 5.34 seconds.
- Full local Python 3.13.5 suite: 1,624 passed in 23.75 seconds.
- Repository-wide Ruff: passed.
- Package-wide mypy: 85 modules passed.
- Service wrapper `bash -n`: passed.
- `git diff --check`: passed.
- All 26 `requirements.lock` pins matched the isolated Python 3.11 environment;
  `pip check` reported no broken requirements.
- Two independent read-only reviews found the asynchronous ACK false-green and
  the premature candidate swap. Both were fixed; the final review reported no
  blockers.

No Bybit API, demo account, VPS, workflow, or deployed service was contacted.
PyPI was contacted only to construct the isolated exact-pin Python 3.11
validation environment. No strategy research or backtest was run, and strategy
decisions/numerics were not changed.

## Residual scope and next candidates

- Readiness proves the local socket, authentication, and subscription control
  ACKs; it cannot prove that every future venue message will be delivered.
  Authenticated REST reconciliation remains the independent position/order
  backstop.
- The ACK tracker intentionally depends on private hooks in exact-pinned pybit
  5.16.0 and fails startup if they disappear. Any dependency upgrade requires a
  targeted compatibility review rather than a permissive fallback.
- The 180-second reconnect/cooldown and ten-second candidate handshake are
  operational defaults, not research thresholds. They should be tuned from
  observed operational evidence if the deployed venue path shows a different
  recovery distribution.
- A provider constructor that exceeds its own transport bounds can leave the
  daemon rebuild thread in flight, but owner health remains blocked and the
  account/REST loop remains responsive.
- No live demo handshake or recovery was exercised in this offline cycle.
- The highest remaining cross-cutting correctness candidate is the storage
  mutex protocol: persistent kernel `flock` can remove conditional-unlink
  TOCTOU, but requires a quiescent old/new migration and fork/path tests.
- A separate owner-health audit should also verify that all slower accounting
  providers, especially funding reconciliation, are represented in durable
  health rather than only exposure admission.
