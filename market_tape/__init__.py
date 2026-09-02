"""Record, ship, and read the public market tape of crypto perpetual venues.

This package stands alone: it imports nothing from the trading repository it
lives in, so it can move to its own repository unchanged. Read `README.md`
beside this file first.
"""

from market_tape.schema import SCHEMA_VERSION

__all__ = ["SCHEMA_VERSION"]
