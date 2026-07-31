"""Regression checks for the canonical Binance full-PIT refresh surface."""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "data" / "build_full_pit_binance.sh"


def test_script_is_valid_bash_and_stages_daily_tail_with_monthly_rebuild() -> None:
    subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    text = SCRIPT.read_text(encoding="utf-8")
    monthly = text.index("build-binance-oos --data-root")
    manifest_read = text.index("pl.read_parquet")

    assert monthly < manifest_read
    assert '--daily-start "$DAILY_START"' in text
    assert "topup-daily-klines --data-root" not in text
    assert "BINANCE_DAILY_START" in text


def test_daily_tail_default_is_month_start_containing_end_minus_one_day() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "end - dt.timedelta(days=1)" in text
    assert ".replace(day=1).isoformat()" in text
    assert 'if [ -z "$SYMBOLS" ]' in text


def test_builder_exposes_bounded_batches_strict_completeness_and_symbol_validation() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "BINANCE_JOB_BATCH_SIZE" in text
    assert "BINANCE_MAX_FAILURE_RATIO:-0" in text
    assert '--job-batch-size "$JOB_BATCH_SIZE"' in text
    assert '--max-failure-ratio "$MAX_FAILURE_RATIO"' in text
    assert "validate_usdm_usdt_symbols" in text
    assert "PYTHONUTF8=1" in text


def test_help_and_unknown_arguments_cannot_start_a_build() -> None:
    help_result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    refused = subprocess.run(
        ["bash", str(SCRIPT), "unexpected"],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )

    assert help_result.returncode == 0
    assert "Configuration is environment-only" in help_result.stdout
    assert refused.returncode == 2
    assert "accepts no positional arguments" in refused.stderr
