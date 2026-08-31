#!/usr/bin/env python3
"""Run the offline engine-WAL versus Bybit accounting view."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from liquidity_migration.research.venue_wal_accounting import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
