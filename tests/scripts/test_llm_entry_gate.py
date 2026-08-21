"""The candidates publisher's contract, tested pure.

The ledger's whole output to the trading path is the LONG sleeve's candidates
file: which judged pump events may be entered. Everything after that read
belongs to the LONG producer and is tested there.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import time
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "llm_driver_ledger",
    Path(__file__).resolve().parents[2] / "scripts" / "research" / "llm_driver_ledger.py",
)
ledger = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(ledger)

NOW = dt.datetime(2026, 8, 21, 12, 0, tzinfo=dt.timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
TRIGGER_BAR_END = "2026-08-21T11:00:00+00:00"
TRIGGER_TS_MS = int(dt.datetime.fromisoformat(TRIGGER_BAR_END).timestamp() * 1000)


def _event(symbol: str, *, score: int = 7, would: bool = True) -> dict:
    return {
        "would_enter": would,
        "facts": {
            "symbol": symbol,
            "trigger_price": 10.0,
            "trigger_bar_end_utc": TRIGGER_BAR_END,
            "atr_14d_pct": 0.05,
            "sigma_daily_30d": 0.03,
            "turnover_rank": 4,
            "trigger_window_h": 4,
        },
        "judgment": {"pump_quality_score": score},
    }


def _publish(tmp: Path, events) -> list[dict]:
    return ledger.publish_gate_candidates(events, path=str(tmp / "candidates.json"))


class TestPublishGateCandidates:
    def test_a_would_enter_event_is_published_whole(self, tmp_path: Path) -> None:
        published = _publish(tmp_path, [_event("AAAUSDT")])
        assert [e["symbol"] for e in published] == ["AAAUSDT"]
        row = json.loads((tmp_path / "candidates.json").read_text())
        assert row["decision_ts_ms"] == pytest.approx(int(time.time() * 1000), abs=5000)
        assert row["valid_until_ms"] == row["decision_ts_ms"] + 90 * 60_000
        (event,) = row["events"]
        assert event["symbol"] == "AAAUSDT"
        assert event["score"] == 7
        assert event["trigger_ts_ms"] == TRIGGER_TS_MS
        assert event["trigger_price"] == 10.0
        assert event["atr_pct"] == 0.05
        assert event["sigma_daily_30d"] == 0.03
        assert event["turnover_rank"] == 4
        assert event["trigger_window_h"] == 4

    def test_only_would_enter_events_are_published(self, tmp_path: Path) -> None:
        published = _publish(tmp_path, [_event("AAAUSDT", would=False), _event("BBBUSDT")])
        assert [e["symbol"] for e in published] == ["BBBUSDT"]

    def test_an_event_without_a_usable_atr_or_price_publishes_nothing(self, tmp_path: Path) -> None:
        bad_atr = _event("AAAUSDT")
        bad_atr["facts"]["atr_14d_pct"] = None
        bad_price = _event("BBBUSDT")
        bad_price["facts"]["trigger_price"] = 0.0
        assert _publish(tmp_path, [bad_atr, bad_price]) == []
        assert json.loads((tmp_path / "candidates.json").read_text())["events"] == []

    def test_a_missing_trigger_bar_stamp_falls_back_to_now(self, tmp_path: Path) -> None:
        event = _event("AAAUSDT")
        del event["facts"]["trigger_bar_end_utc"]
        (published,) = _publish(tmp_path, [event])
        assert published["trigger_ts_ms"] == pytest.approx(int(time.time() * 1000), abs=5000)

    def test_the_write_replaces_the_previous_file_whole(self, tmp_path: Path) -> None:
        _publish(tmp_path, [_event("AAAUSDT")])
        _publish(tmp_path, [_event("BBBUSDT")])
        row = json.loads((tmp_path / "candidates.json").read_text())
        assert [e["symbol"] for e in row["events"]] == ["BBBUSDT"]

    def test_no_events_still_writes_an_empty_fresh_file(self, tmp_path: Path) -> None:
        _publish(tmp_path, [])
        row = json.loads((tmp_path / "candidates.json").read_text())
        assert row["events"] == []
