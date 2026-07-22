# 2026-07-22 paper reduction-convergence incident audit

Scope: the supplied Telegram log ending at 08:15 UTC, the canonical paper
account journal and health projection, current source/tests, and read-only
deployment verification at commit `694540cc94767a2a6cbb636d68fac5f380f8bf9e`.
The Bybit demo and paper accounts are separate; no mainnet authority or effect
is involved.

## Outcome

The page was real but paper-only. TREEUSDT's desired paper position had fallen
from `-3001.9` to `-1334.2`, yet the paper position remained `-3001.9`. The
owner built the correct reduce-only buy of `1667.7` three times. Every modeled
order was definitively rejected as `stale_decision`, after which the generic
three-retry convergence budget permanently labelled the reduction
`retry_exhausted`. LAUSDT's `0.1` residual was separately correct
venue-minimum dust and did not cause the unhealthy state.

The repair keeps the 250 ms paper entry rule unchanged, gives only strict
reduce-only commands the same five-second freshness boundary already enforced
by the account owner, and makes strict reductions persist with capped
exponential backoff. Exposure-increasing and sign-flipping retries remain
finite. The existing journal is preserved: after activation, its next
deterministic TREEUSDT recovery ordinal is `0004`; no reset, synthetic target,
or manual position edit is required.

## Reconstruction

1. TREEUSDT correctly accumulated three independent components. The third
   convergence fill was not a duplicate; it completed a target that arrived
   while an earlier component order was still working.
2. At journal sequences 755 and 761, two component targets moved to zero. Their
   immediate reductions were rejected as `stale_decision`, leaving desired
   aggregate `-1334.2` against paper position `-3001.9`.
3. Convergence sequences 767--782 produced three distinct, correct,
   reduce-only commands of `+1667.7`. Each had zero fill and a definitive
   `stale_decision` ACK rejection.
4. The account owner accepts market input up to five seconds old. The paper
   market twin independently applied its 250 ms decision-age rule to every
   command, including exits. A quiet symbol can have a valid, sequence-complete
   book older than 250 ms without being older than the owner's freshness SLA.
5. Once the third convergence batch rejected, the service applied the entry
   retry ceiling to the reduction and stopped creating recovery work. Health
   correctly failed closed, but recovery had become inert.

The transient demo L2-staleness alert in the same Telegram excerpt resolved
three minutes later and is not on this causal path. The critical 08:15 page was
generated from the independent paper account health projection.

## Repair invariants

- Entry admission and the registered passive A/B experiment retain the 250 ms
  decision-age limit.
- Only commands already proven `reduce_only` may request the wider decision-age
  bound; the generic twin rejects attempts to apply it to an entry.
- The wider bound is exactly the account owner's shared five-second market SLA,
  not a second unrelated literal. Books beyond it and future books still fail.
- ACK, fill, and terminal model observations record book age, effective limit,
  limit source, and whether the reduction allowance was used.
- Strict reduction convergence has no attempt-count terminal state. Its retry
  delay remains bounded: 1, 2, 4, 8, 16, then 30 seconds by default.
- Exposure-increasing or sign-flipping convergence retains the configured
  finite attempt ceiling.
- Overdue reductions remain health-blocking while retrying. Persistence is not
  reported as convergence or success.
- Venue-minimum dust remains non-retryable and healthy.

## Evidence

Focused regression coverage proves the incident boundary directly:

- an over-250 ms exposure command is still rejected;
- a reduction using the same book fills when the age is within five seconds;
- a reduction older than five seconds still rejects;
- reduction retries pass the exposure retry ceiling, use a unique next ordinal,
  honor the backoff cap, and eventually converge;
- exposure retry exhaustion and venue-minimum dust behavior remain unchanged.

Focused account/execution validation completed with 219 passing tests. The
full repository gate completed with repository doctor, Ruff, and mypy green,
plus 2,224 passing tests and one intentional skip.

Deployment and post-activation account evidence must be appended only after the
exact committed implementation is installed and observed; local tests alone do
not prove runtime recovery.
