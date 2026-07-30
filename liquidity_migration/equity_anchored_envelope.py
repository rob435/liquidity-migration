"""An account envelope that is a fraction of the wallet, not a fixed number.

The owner's six pre-trade caps were calibrated against a constant
``capital_reference_usdt``, while producers sized off *live* venue equity. That
split is the defect B4 describes from both ends: fund below the reference and
every cap sits above anything reachable, so the envelope stops binding; grow
above it and the load-time envelope proof silently stops being true.

Pinning a number solves it in one direction and creates work in the other —
every deposit or withdrawal becomes a config change, and a stale one is a live
mis-sized book. Anchoring the reference to observed equity solves it in both,
because every producer envelope and every account cap in the profile is linear
in the reference: the profile is really a set of *ratios*, and the reference is
the scale.

Three properties make that safe rather than merely convenient.

**Authority still binds the rule.** The authority receipt hashes the profile,
which carries the ratios and the anchoring mode. The absolute caps are a pure
function of (bound profile, observed equity), so "limits cannot change without
invalidating authority" is unchanged — what is bound is the rule, and the input
is venue truth rather than an operator's memory.

**Contraction is immediate; expansion is not.** Equity down rescales the caps
on the next observation, because that is the safe direction. Equity up waits
for a move larger than a dead band, so ordinary equity wander cannot re-scale
the envelope every cycle — the same defect class as the 2026-07-30 resize churn
that moved ~9%/yr of the account through fees.

**Unknown equity moves nothing.** A missing, non-finite, non-positive, or stale
reading holds the current reference. Contracting on unknown data would be a
blind action taken on no evidence, and the loss guard already owns "too stale to
judge" by blocking new risk. Below the configured floor the reference clamps to
the floor rather than collapsing, so the envelope stays well-defined and the
kernel's venue-minimum checks are what refuse the trade.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .account_contracts import AccountRiskPolicy
from .operational_profile import OperationalProfile, profile_at_capital_reference

__all__ = ["EquityAnchoredEnvelope", "EnvelopeRebase"]

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EnvelopeRebase:
    """One observed transition of the capital reference."""

    previous_usdt: float
    current_usdt: float
    equity_usdt: float
    direction: str
    detail: str = ""


class EquityAnchoredEnvelope:
    """Resolve the capital reference, and the caps derived from it, over time."""

    def __init__(
        self,
        profile: OperationalProfile,
        *,
        initial_equity_usdt: float | None = None,
    ) -> None:
        self._declared = profile
        self._resolved = profile
        self.last_error = ""
        if initial_equity_usdt is not None:
            self.observe_equity(initial_equity_usdt)

    @property
    def tracks_equity(self) -> bool:
        return self._declared.capital_reference.tracks_equity

    @property
    def profile(self) -> OperationalProfile:
        return self._resolved

    @property
    def reference_usdt(self) -> float:
        return self._resolved.capital_reference_usdt

    def policy(self) -> AccountRiskPolicy:
        return self._resolved.account_risk.to_policy()

    def observe_equity(self, equity_usdt: float | None) -> EnvelopeRebase | None:
        """Fold one equity reading in. Returns the rebase, or None if held."""

        if not self.tracks_equity:
            return None
        settings = self._declared.capital_reference
        if equity_usdt is None:
            return None
        try:
            observed = float(equity_usdt)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(observed) or observed <= 0.0:
            # Unknown is not evidence of small. Hold, and let the loss guard and
            # the health chain own the "cannot judge the account" state.
            return None
        target = max(observed * settings.equity_fraction, settings.floor_usdt)
        current = self._resolved.capital_reference_usdt
        if math.isclose(target, current, rel_tol=1e-12, abs_tol=1e-9):
            return None
        contracting = target < current
        if not contracting and target <= current * (1.0 + settings.expand_dead_band_fraction):
            return None
        try:
            rebased = profile_at_capital_reference(self._declared, target)
        except ValueError as exc:
            # The proof is re-run at every reference precisely so a scale that
            # breaks it is refused rather than shipped. Keep the prior envelope.
            self.last_error = (
                f"refusing to rebase the capital reference to {target:g} USDT: {exc}"
            )[:1000]
            _logger.error("%s", self.last_error)
            return None
        self._resolved = rebased
        self.last_error = ""
        transition = EnvelopeRebase(
            previous_usdt=current,
            current_usdt=target,
            equity_usdt=observed,
            direction="contract" if contracting else "expand",
            detail=(
                f"capital reference {current:,.2f} -> {target:,.2f} USDT "
                f"on observed equity {observed:,.2f}"
            ),
        )
        _logger.warning("account envelope rebased: %s", transition.detail)
        return transition
