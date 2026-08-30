from __future__ import annotations

import pytest

from liquidity_migration.marketdata.kline_store import _parse_ws_kline_event


def _bar(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "start": 1_700_000_000_000,
        "open": "100",
        "high": "110",
        "low": "90",
        "close": "105",
        "volume": "10",
        "turnover": "1000",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "bar",
    [
        _bar(open="nan"),
        _bar(high="inf"),
        _bar(low="-inf"),
        _bar(close="0"),
        _bar(volume="-1"),
        _bar(turnover="nan"),
        _bar(high="104"),
        _bar(low="106"),
        _bar(start=-1),
    ],
)
def test_ws_kline_parser_rejects_nonfinite_or_impossible_bars(
    bar: dict[str, object],
) -> None:
    assert _parse_ws_kline_event(bar) is None
