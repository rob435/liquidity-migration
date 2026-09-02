#!/usr/bin/env python3
"""The hourly market-tape packer lives in `market_tape.pack`; this path runs it unchanged.

`python -m market_tape pack --help` shows the arguments.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from market_tape.pack import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
