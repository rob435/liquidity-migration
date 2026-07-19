# Prospective Runtime Parity Execution Epoch: Post-17 Amendments

This file extends the frozen base contract and Amendments 1--17 without
changing either earlier file.  A runner using an amendment here must bind this
file by exact SHA-256 in addition to the earlier contract identities.

## Amendment 18: staged sign changes and durable structural failure evidence

Registered 2026-07-19 after the clean-commit
`d54eb524c208b96025deaa9f3f62839e55bcad91` comparator processed all 29,449
registered hourly boundaries and then failed its terminal-flat requirement,
before a replacement run and without inspecting any monetary, return, alpha,
cost, or thesis outcome.  The invalid attempt is preserved at
`reports/prospective-runtime-parity-execution-epoch-2026-07-18/runtime-parity/.active-production-comparator.working-d54eb524c208-boundary-flat-xrp`.
Its create-only termination receipt SHA-256 is
`7f5959e06e973617f511e74849dda0187a305032856c92340a7dbf2cddbc437c`.
The 70 files preceding that receipt total 118,009,066 bytes and have logical
SHA-256 `3167c721c664f0515ba95effbd7a9794c05c5ab15c1fcbed174d0c997546fddc`.
All 21 registered prefix artifacts remained byte-identical.

The terminal state retained component
`continuous/continuous_fade_v2/continuous_fade_v2-XRPUSDT-1689260400000-p3/XRPUSDT`
and XRPUSDT position `-56018.094544556836`, with no working order.  Structural
request traces explain the origin rather than merely the terminal symptom.
The component entry was accepted once.  Its first ordinary max-hold zero
replacement was rejected at `1689354000000200000` ns, after an opposing LONG
XRPUSDT component had been accepted.  Every one of the 19,347 captured zero
replacement attempts for this component was rejected with
`sign_flip_requires_flat`; the captured span ends at request ordinal 19,966.
The comparator treated those pre-target-commit exit rejections as nonfatal and
continued.  At the final boundary, one-pass component ordering could remove
the opposing LONG target only after the CONTINUOUS close had already been
rejected, leaving the CONTINUOUS exposure for which no second pass existed.

The replacement implementation must preserve the account kernel's hard
no-cross-through-zero invariant.  When an otherwise admissible component
replacement changes the aggregate target to the opposite side of the
reconstructed venue position, it may commit the new desired component state
only by staging execution to flat: the first command targets exactly zero, is
reduce-only, and cannot include opposite-side quantity.  Opposite-side
convergence is eligible only after the journal proves the venue position flat,
using the production convergence path and its applicable fresh market,
position-truth, health, and risk admission.  A rejected risk, lifecycle,
revision, quantity, market, or execution check remains rejected; this
amendment does not turn a failed gate into acceptance.

The historical production-function port must apply the exact account-request
provenance transformation used by the demo/paper service.  It must also drive
the same staged convergence semantics at causal request boundaries; it may not
approximate the transition with a direct sign-flipping fill.  Tests must cover
opposing LONG and CONTINUOUS targets, one-component removal, flat-first command
identity, post-flat convergence, crash/retry idempotency, and a boundary where
multiple opposing components are all removed.

The comparator must fail immediately on any rejected ordinary exit,
protection, lifecycle close, or terminal-flat request, including a rejection
that occurs before a target commit.  Valid entry admission rejections remain
traceable outcomes and do not by themselves invalidate structural parity.  A
terminal flatten may make bounded deterministic passes, but it must stop on the
first explicit rejection or lack of structural progress and must end with zero
component targets, zero positions, and zero working orders.

Before terminal flatten, and on every exceptional exit, the runner must flush
all buffered structural traces, materialize and verify the account journal,
and create a failure receipt containing the exception, exact request feedback,
nonmonetary state identities, clocks, file identities, and prefix comparison.
This diagnostic persistence must not calculate or inspect P&L, returns, costs,
or economic aggregates.  A subsequent full comparator still starts from an
empty output root and replays all 29,449 hours; a checkpoint is evidence, not a
resume authority.

The replacement passes only if it preserves all 21 registered prefix files,
covers the complete registered clock and feature populations, has no rejected
risk-reducing request, reconciles BTC-risk and lifecycle identities, verifies
the materialized journal, and ends structurally flat.  Only then may its
already-generated monetary fields be opened under the original contract.
This amendment grants no alpha, cost, thesis, deployment, or real-money
conclusion.
