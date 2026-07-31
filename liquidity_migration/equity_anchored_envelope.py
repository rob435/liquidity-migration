"""An account envelope that is a fraction of the wallet, not a fixed number.

Every producer envelope and account cap in the profile is linear in
``capital_reference_usdt``, so the profile is a set of ratios and the reference
is the scale. Three rules govern how that scale moves:

- Contraction is immediate; expansion waits for a move larger than a dead band,
  so ordinary equity wander cannot re-scale (and re-trade) the book every cycle.
- A missing, non-finite, or non-positive reading holds the current reference.
  Unknown is not evidence of small; the loss guard owns "cannot judge".
- Below the configured floor the reference clamps to the floor, so the envelope
  stays well-defined and the kernel's venue-minimum checks refuse the trade.
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
            # A scale the profile cannot satisfy is refused; keep the prior envelope.
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
