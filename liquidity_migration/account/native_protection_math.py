"""Pure price math shared by pre-entry and post-fill native protection."""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN


def round_native_stop(
    value: float,
    tick_size: float,
    *,
    long_position: bool,
) -> float:
    """Round a stop outward so tick normalization never tightens it."""

    if not math.isfinite(value) or value <= 0.0:
        raise ValueError("native stop value must be finite and positive")
    if not math.isfinite(tick_size) or tick_size <= 0.0:
        raise ValueError("native stop tick_size must be finite and positive")
    tick = Decimal(str(tick_size))
    units = Decimal(str(value)) / tick
    nearest = units.to_integral_value(rounding=ROUND_HALF_EVEN)
    if abs(units - nearest) <= Decimal("1e-12"):
        units = nearest
    rounding = ROUND_FLOOR if long_position else ROUND_CEILING
    output = float(units.to_integral_value(rounding=rounding) * tick)
    if not math.isfinite(output) or output <= 0.0:
        raise ValueError("rounded native stop must be finite and positive")
    return output
