from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MS_PER_DAY = 86_400_000


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "continuous_forward_report", REPO / "scripts" / "continuous_forward_report.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["continuous_forward_report"] = module
    spec.loader.exec_module(module)
    return module


def _today_ms() -> int:
    return (int(datetime.now(timezone.utc).timestamp() * 1000) // MS_PER_DAY) * MS_PER_DAY


def _payload(*, daily_days: int, daily_latest: int, cont_days: int, cont_latest: int,
             common_days: int, ok: bool) -> dict:
    return {
        "ok": ok,
        "summary": {
            "common_days": common_days,
            "min_common_days": 30,
            "maturity_day_ts": 1_702_505_600_000,
            "daily_observed_days": daily_days,
            "latest_daily_day_ts": daily_latest,
            "continuous_observed_days": cont_days,
            "latest_continuous_day_ts": cont_latest,
            "continuous_full_days": cont_days,
            "continuous_full_total_return": 0.02,
            "continuous_full_mar": 0.8,
            "mode": "continuous_only" if common_days < 30 else "comparison",
        },
        "daily": {"total_return": 0.01, "mar": 0.5},
        "continuous": {"total_return": 0.02, "mar": 0.8},
        "issues": [],
        "notes": [],
        "report_path": "report.md",
        "json_path": "report.json",
    }


def test_collecting_when_both_legs_fresh_and_window_immature() -> None:
    mod = _load_module()
    today = _today_ms()
    payload = _payload(daily_days=4, daily_latest=today - MS_PER_DAY, cont_days=5,
                       cont_latest=today, common_days=4, ok=True)
    message = mod.format_message(payload)
    assert "continuous-vs-daily forward: COLLECTING" in message
    assert "common_days=4/30" in message
    assert "coverage_gap_days=1 lagging=daily" in message
    assert "continuous OWN-WINDOW: 5d return=2.00% MAR=0.80" in message


def test_continuous_only_when_daily_leg_dead() -> None:
    # 2026-06-11 operator decision: a lagging/absent DAILY leg is never an alarm —
    # the continuous report stands alone.
    mod = _load_module()
    today = _today_ms()
    lagging = _payload(daily_days=1, daily_latest=today - 5 * MS_PER_DAY, cont_days=10,
                       cont_latest=today, common_days=1, ok=True)
    assert "continuous-vs-daily forward: CONTINUOUS-ONLY" in mod.format_message(lagging)
    empty = _payload(daily_days=0, daily_latest=0, cont_days=10,
                     cont_latest=today, common_days=0, ok=True)
    assert "continuous-vs-daily forward: CONTINUOUS-ONLY" in mod.format_message(empty)


def test_stale_only_when_continuous_itself_lags_today() -> None:
    mod = _load_module()
    today = _today_ms()
    payload = _payload(daily_days=4, daily_latest=today - 4 * MS_PER_DAY, cont_days=6,
                       cont_latest=today - 4 * MS_PER_DAY, common_days=4, ok=True)
    message = mod.format_message(payload, stale_coverage_gap_days=2)
    assert "continuous-vs-daily forward: STALE" in message


def test_pass_after_common_window_matures() -> None:
    mod = _load_module()
    today = _today_ms()
    payload = _payload(daily_days=31, daily_latest=today, cont_days=31,
                       cont_latest=today, common_days=31, ok=True)
    assert "continuous-vs-daily forward: PASS" in mod.format_message(payload)


def test_main_exits_nonzero_when_attempted_send_fails(monkeypatch, tmp_path) -> None:
    """ROUND 4 (pins the round-3 fix): the watchdog monitors this oneshot on
    the premise that a failed ATTEMPTED telegram send exits nonzero. Exiting 0
    meant a STALE/FAIL day with Telegram down vanished silently."""
    module = _load_module()
    today = _today_ms()
    stale = _payload(
        daily_days=40, daily_latest=today, cont_days=40, cont_latest=today - 5 * MS_PER_DAY,
        common_days=40, ok=True,
    )
    monkeypatch.setattr(module, "run_continuous_vs_daily_forward_comparison", lambda *a, **k: stale)
    monkeypatch.setattr(module, "send_telegram_message", lambda *a, **k: False)
    monkeypatch.setattr(
        sys, "argv",
        ["continuous_forward_report.py", "--daily-data-root", str(tmp_path),
         "--continuous-data-root", str(tmp_path), "--telegram"],
    )
    assert module.main() == 1

    # And exit 0 when the send goes through.
    monkeypatch.setattr(module, "send_telegram_message", lambda *a, **k: True)
    assert module.main() == 0
