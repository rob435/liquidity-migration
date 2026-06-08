from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "continuous_forward_report", REPO / "scripts" / "continuous_forward_report.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["continuous_forward_report"] = module
    spec.loader.exec_module(module)
    return module


def test_format_message_collecting_until_common_window_matures() -> None:
    mod = _load_module()
    payload = {
        "ok": False,
        "summary": {
            "common_days": 4,
            "min_common_days": 30,
            "maturity_day_ts": 1_702_505_600_000,
            "daily_observed_days": 4,
            "latest_daily_day_ts": 1_700_000_000_000,
            "continuous_observed_days": 5,
            "latest_continuous_day_ts": 1_700_086_400_000,
        },
        "daily": {"total_return": 0.01, "mar": 0.5},
        "continuous": {"total_return": 0.02, "mar": 0.8},
        "issues": ["common forward window 4d below min_common_days 30"],
        "report_path": "report.md",
        "json_path": "report.json",
    }

    message = mod.format_message(payload)

    assert "continuous-vs-daily forward: COLLECTING" in message
    assert "common_days=4/30" in message
    assert "maturity_day_ts=1702505600000" in message
    assert "coverage daily=4d latest=1700000000000; continuous=5d latest=1700086400000" in message
    assert "coverage_gap_days=1 lagging=daily" in message
    assert "continuous return=2.00% MAR=0.80" in message
    assert "json=report.json" in message


def test_format_message_stale_when_one_ledger_lags() -> None:
    mod = _load_module()
    payload = {
        "ok": False,
        "summary": {
            "common_days": 4,
            "min_common_days": 30,
            "daily_observed_days": 4,
            "latest_daily_day_ts": 1_700_000_000_000,
            "continuous_observed_days": 6,
            "latest_continuous_day_ts": 1_700_172_800_000,
        },
        "daily": {"total_return": 0.01, "mar": 0.5},
        "continuous": {"total_return": 0.02, "mar": 0.8},
        "issues": ["common forward window 4d below min_common_days 30"],
        "report_path": "report.md",
    }

    message = mod.format_message(payload, stale_coverage_gap_days=2)

    assert "continuous-vs-daily forward: STALE" in message
    assert "coverage_gap_days=2 lagging=daily" in message


def test_format_message_pass_after_common_window_matures() -> None:
    mod = _load_module()
    payload = {
        "ok": True,
        "summary": {"common_days": 31, "min_common_days": 30},
        "daily": {"total_return": 0.01, "mar": 0.5},
        "continuous": {"total_return": 0.02, "mar": 0.8},
        "issues": [],
        "report_path": "report.md",
    }

    assert "continuous-vs-daily forward: PASS" in mod.format_message(payload)
