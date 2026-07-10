"""Regression checks for the canonical Binance full-PIT refresh surface."""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "build_full_pit_binance.sh"


def test_script_is_valid_bash_and_daily_tail_follows_clean_monthly_rebuild() -> None:
    subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    text = SCRIPT.read_text(encoding="utf-8")
    monthly = text.index("build-binance-oos --data-root")
    daily = text.index("topup-daily-klines --data-root")
    manifest_read = text.index("pl.read_parquet")

    assert monthly < daily < manifest_read
    assert '--start "$DAILY_START" --end "$END"' in text
    assert "BINANCE_DAILY_START" in text


def test_daily_tail_default_is_month_start_containing_end_minus_one_day() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "end - dt.timedelta(days=1)" in text
    assert ".replace(day=1).isoformat()" in text
    assert 'if [ -z "$SYMBOLS" ]' in text
