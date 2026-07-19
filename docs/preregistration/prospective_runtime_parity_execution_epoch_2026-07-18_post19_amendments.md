# Prospective Runtime Parity Execution Epoch: Post-19 Amendments

This file extends the frozen base contract and Amendments 1--19 without
changing any earlier file.  A runner using an amendment here must bind this
file by exact SHA-256 in addition to the earlier contract identities.

## Amendment 20: dimensionally valid opposite-side detection

Registered 2026-07-19 during code review, before implementation, an affected
test, or a replacement comparator run, and without inspecting any monetary,
return, alpha, cost, or thesis outcome.

The kernel's existing no-cross-through-zero branch detected an opposite-side
target with `projected_qty * target_qty < -quantity_tolerance`.  The left side
has squared-quantity units while the tolerance has quantity units.  It can
therefore fail to identify two opposite nonzero quantities when their product
is numerically smaller than the tolerance.

Opposite-side detection must instead require each quantity independently to
exceed the declared absolute quantity tolerance and then compare their signs.
The same predicate must govern direct sign-flip rejection and the
exposure-clamped component-removal path.  Tests must include opposite targets
whose product magnitude is below the tolerance while each quantity remains
above it.  This correction changes no registered tolerance, admission rule,
economic model, thesis, deployment boundary, or real-money authority.
