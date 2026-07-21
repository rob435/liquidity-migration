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


def native_stop_from_reference(
    *,
    reference_price: float,
    stop_fraction: float,
    tick_size: float,
    long_position: bool,
) -> float:
    """Build an outward-rounded stop from a current decision reference."""

    if not math.isfinite(reference_price) or reference_price <= 0.0:
        raise ValueError("native stop reference_price must be finite and positive")
    if not math.isfinite(stop_fraction) or not 0.0 < stop_fraction < 1.0:
        raise ValueError("native stop fraction must be finite and in (0, 1)")
    raw = reference_price * (1.0 - stop_fraction if long_position else 1.0 + stop_fraction)
    stop = round_native_stop(raw, tick_size, long_position=long_position)
    if long_position and stop >= reference_price:
        raise ValueError("long native stop must be below its reference price")
    if not long_position and stop <= reference_price:
        raise ValueError("short native stop must be above its reference price")
    return stop
