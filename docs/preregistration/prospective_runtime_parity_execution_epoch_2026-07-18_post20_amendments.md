# Prospective Runtime Parity Execution Epoch: Post-20 Amendments

This file extends the frozen base contract and Amendments 1--20 without
changing any earlier file.  A runner using an amendment here must bind this
file by exact SHA-256 in addition to the earlier contract identities.

## Amendment 21: flat-venue delisting target invalidation

Registered 2026-07-19 during code review, before implementation, an affected
test, or a replacement comparator run, and without inspecting any monetary,
return, alpha, cost, or thesis outcome.

A delisting boundary can find the reconstructed venue position already flat
while same-symbol component targets remain nonzero and offset one another.
The existing lifecycle branch treats any such latent desired state as an
unrecoverable comparator error.  That is unnecessarily order-dependent and
does not model the fact that a delisted instrument is no longer an eligible
execution target.

When the venue position is flat and there are nonzero component targets for a
symbol at its registered causal delisting dispatch, the comparator must
publish one atomic risk-owned request replacing every such component with
zero.  The request uses the registered settlement proxy as the final market
reference, passes through the exact production account owner, and is required
to be accepted.  It may not create a nonzero order from the flat venue state.
The lifecycle event then blocks all future entries as already registered.

This allowance applies only to target invalidation while the venue is flat.
A nonflat venue position still uses the registered external delisting
settlement fill.  Any remaining component target, nonzero position, or working
order after either branch is a structural failure.  Tests must cover multiple
same-symbol offsetting targets at zero net, atomic cancellation without a
venue order, exact request traceability, and continued blocking of later
entries.  This amendment grants no alpha, cost, thesis, deployment, or
real-money conclusion.
