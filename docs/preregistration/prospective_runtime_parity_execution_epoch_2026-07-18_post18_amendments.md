# Prospective Runtime Parity Execution Epoch: Post-18 Amendments

This file extends the frozen base contract and Amendments 1--18 without
changing any earlier file.  A runner using an amendment here must bind this
file by exact SHA-256 in addition to the earlier contract identities.

## Amendment 19: exposure-clamped component removal and atomic terminal flat

Registered 2026-07-19 during code review of the uncommitted Amendment 18
implementation, before an affected test or replacement comparator run and
without inspecting any monetary, return, alpha, cost, or thesis outcome.

Amendment 18 covered the observed case in which removing one offsetting
component revealed an aggregate target on the opposite side of the
reconstructed venue position.  The same structural class also has a
same-side case: removing an offsetting component can reveal a retained target
whose absolute quantity is larger than the current venue position.  An
independently published request labelled as an exit must not turn that
intermediate desired-state transition into an exposure-increasing order.

For an explicit zero replacement of an existing nonzero component, immediate
execution is therefore evaluated separately from the eventual aggregate
desire.  If the retained aggregate is on the opposite side, or the venue is
flat while the retained aggregate is nonzero, the immediate execution target
is exactly zero.  If the retained aggregate is on the same side but farther
from zero than the reconstructed/projected venue position, the immediate
execution target is clamped to that current projected position and no
increase is ordered.  A retained aggregate already between the projected
position and zero may be executed normally.  Any command produced by this
component-removal class must be risk-reducing and may never cross zero.

This immediate, exposure-clamped transition is eligible for the narrow
risk-reduction admission used for exits, including when capital headroom is
unavailable.  That exemption applies only to the immediate command.  Any
remaining residual desire that would add exposure is eligible only through
the production convergence path and its ordinary fresh market, account-wide
health, position-truth, and capital-risk admission.  A rejected convergence
gate remains rejected and must leave the reconstructed venue exposure no
larger than before the component removal.

Terminal comparator flattening must publish all currently nonzero component
zeros as one atomic risk-owned request per deterministic pass.  Independent
one-component terminal requests could otherwise create order-dependent
intermediate exposure when same-symbol components offset, including when the
net venue position is already zero.  The terminal routine retains the bounded
pass and no-progress checks from Amendment 18, and it must still finish with
zero component targets, zero positions, and zero working orders.

Tests must cover the observed opposite-side flat-first transition, a
same-side farther-from-zero removal under breached entry limits, rejection of
the later exposure-increasing convergence, direct nonzero sign-flip
rejection, idempotent replay, and atomic terminal removal of multiple
same-symbol offsetting components including a zero-net case.  The full
comparator must still start from an empty root and preserve all registered
prefix identities.  This amendment grants no alpha, cost, thesis, deployment,
or real-money conclusion.
