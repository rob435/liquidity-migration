#!/usr/bin/env python3
"""Operator entry point for the evidence-bound account cutover authorization."""

from __future__ import annotations

import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.account_cutover_authority import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
