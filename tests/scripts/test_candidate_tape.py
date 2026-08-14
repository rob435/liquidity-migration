from __future__ import annotations

import polars as pl

from scripts.data.build_candidate_tape import _valid_ohlc


def test_invalid_ohlc_is_explicitly_rejected() -> None:
    frame = pl.DataFrame(
        {
            "open": [1.0, None],
            "high": [1.1, 1.1],
            "low": [0.9, 0.9],
            "close": [1.0, 1.0],
        }
    )

    assert frame.filter(_valid_ohlc()).height == 1
