"""audit2c: hedge runner robustness — warmstart staleness uses the MAX date."""

from __future__ import annotations

from datetime import date

import scripts.run_continuous_hedge as hedge_runner


def test_warmstart_last_date_returns_max_not_last_in_file(tmp_path) -> None:
    # Final row is OLDER than an earlier row (non-monotonic / out-of-order append).
    p = tmp_path / "ws.csv"
    p.write_text(
        "date,btc_ret,eth_ret\n2026-06-10,0.01,0.01\n2026-06-12,0.02,0.02\n2026-06-09,0.0,0.0\n",
        encoding="utf-8",
    )
    assert hedge_runner._warmstart_last_date(p) == date(2026, 6, 12)  # max, not the last row 06-09


def test_warmstart_last_date_ordered_file_unchanged(tmp_path) -> None:
    p = tmp_path / "ws.csv"
    p.write_text(
        "date,btc_ret,eth_ret\n2026-06-10,0.01,0.01\n2026-06-11,0.02,0.02\n2026-06-12,0.0,0.0\n",
        encoding="utf-8",
    )
    assert hedge_runner._warmstart_last_date(p) == date(2026, 6, 12)
