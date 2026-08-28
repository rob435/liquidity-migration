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


def test_default_gate_path_is_inside_the_llm_owned_state_root() -> None:
    assert ledger.GATE_CANDIDATES_PATH == (
        "/var/lib/liquidity-migration/llm-driver-ledger/llm-gate-candidates.json"
    )


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
        assert row["valid_until_ms"] == row["decision_ts_ms"] + 60 * 60_000
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


class TestTakerRatioDayMean:
    """The one order-flow fact that graded era-stable. It is a MEAN of the
    five-minute ratios, and the rubric's threshold is only meaningful against
    that -- the day's aggregate ratio is a different, lower number."""

    MIDNIGHT = 1_700_006_400_000  # some UTC midnight

    def _rows(self, n: int, value: float = 1.0, *, start_offset_ms: int = 0):
        base = self.MIDNIGHT - 86_400_000 + start_offset_ms
        return [
            {"timestamp": str(base + i * 300_000), "buySellRatio": str(value)}
            for i in range(n)
        ]

    def test_a_whole_day_averages(self) -> None:
        rows = self._rows(288, 1.5)
        assert ledger.taker_ratio_day_mean(rows, self.MIDNIGHT) == 1.5

    def test_it_is_the_mean_of_ratios_not_the_ratio_of_sums(self) -> None:
        rows = self._rows(144, 2.0) + self._rows(144, 0.5, start_offset_ms=144 * 300_000)
        # mean of ratios = 1.25; a ratio of equal-weight sums would be 1.0
        assert ledger.taker_ratio_day_mean(rows, self.MIDNIGHT) == 1.25

    def test_a_part_day_is_refused_rather_than_averaged(self) -> None:
        assert ledger.taker_ratio_day_mean(self._rows(60, 3.0), self.MIDNIGHT) is None

    def test_todays_own_buckets_are_not_counted(self) -> None:
        # Rows stamped at or after midnight belong to the running day.
        today = [
            {"timestamp": str(self.MIDNIGHT + i * 300_000), "buySellRatio": "9.0"}
            for i in range(288)
        ]
        assert ledger.taker_ratio_day_mean(today, self.MIDNIGHT) is None
        mixed = self._rows(288, 1.2) + today
        assert ledger.taker_ratio_day_mean(mixed, self.MIDNIGHT) == 1.2

    def test_no_rows_is_a_null_not_an_error(self) -> None:
        assert ledger.taker_ratio_day_mean([], self.MIDNIGHT) is None


class TestEnrichLeverageFlowFacts:
    """v6 fact set: the leverage-flow paths (OI 24h/48h, premium path) the
    rubric's flow classification consumes. Every read stays optional."""

    HOUR_MS = 3_600_000

    @staticmethod
    def _mock_http(monkeypatch: pytest.MonkeyPatch, routes: dict[str, object]) -> None:
        def fake(url: str, **kwargs: object) -> object:
            for fragment, value in routes.items():
                if fragment in url:
                    return value
            return {"result": {"list": []}}

        monkeypatch.setattr(ledger, "_http_json", fake)

    def test_oi_paths_and_premium_path_are_attached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        oi_rows = [
            {"timestamp": str(NOW_MS - (48 - j) * self.HOUR_MS), "openInterest": str(100 + j)}
            for j in range(49)
        ]
        prem_rows = [[str(NOW_MS - (24 - j) * self.HOUR_MS), "0", "0", "0", "0.0005", "0"] for j in range(25)]
        self._mock_http(
            monkeypatch,
            {
                "open-interest": {"result": {"list": oi_rows}},
                "premium-index-price-kline": {"result": {"list": prem_rows}},
            },
        )

        facts = ledger.enrich("AAAUSDT", {"perp_premium_bp": 12.0})

        assert facts["oi_change_24h_pct"] == pytest.approx(19.35, abs=0.01)
        assert facts["oi_change_48h_pct"] == pytest.approx(48.0, abs=0.01)
        assert facts["premium_bp_24h_ago"] == pytest.approx(5.0)
        assert facts["premium_change_24h_bp"] == pytest.approx(7.0)

    def test_a_short_oi_history_gives_24h_only_and_no_premium_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 30 hourly points: enough for the 24h change (25 needed), not for 48h.
        oi_rows = [
            {"timestamp": str(NOW_MS - (29 - j) * self.HOUR_MS), "openInterest": str(200 - j)}
            for j in range(30)
        ]
        self._mock_http(
            monkeypatch,
            {
                "open-interest": {"result": {"list": oi_rows}},
                "premium-index-price-kline": {"result": {"list": []}},
            },
        )

        facts = ledger.enrich("BBBUSDT", {"perp_premium_bp": -3.0})

        assert "oi_change_48h_pct" not in facts
        assert facts["oi_change_24h_pct"] == pytest.approx((171.0 / 195.0 - 1.0) * 100, abs=0.01)
        assert "premium_bp_24h_ago" not in facts
        assert "premium_change_24h_bp" not in facts


class TestEnrichTurnoverToOiChurn:
    """v7 fact set: the day's traded volume against the standing open interest,
    the churn read the manufactured-pump step consumes. The venue reports OI in
    contracts, so notional derives as contracts x price."""

    OI_ROWS = [{"timestamp": str(NOW_MS), "openInterest": "1000000"}]

    def test_the_churn_ratio_is_attached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        TestEnrichLeverageFlowFacts._mock_http(
            monkeypatch, {"open-interest": {"result": {"list": self.OI_ROWS}}}
        )
        facts = ledger.enrich(
            "AAAUSDT", {"turnover_24h_usdt": 24_000_000.0, "last_price": 2.0}
        )
        assert facts["turnover_to_oi_24h"] == 12.0

    def test_missing_turnover_price_or_oi_leaves_the_fact_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        TestEnrichLeverageFlowFacts._mock_http(
            monkeypatch, {"open-interest": {"result": {"list": self.OI_ROWS}}}
        )
        assert "turnover_to_oi_24h" not in ledger.enrich("AAAUSDT", {"last_price": 2.0})
        assert "turnover_to_oi_24h" not in ledger.enrich(
            "AAAUSDT", {"turnover_24h_usdt": 24_000_000.0}
        )
        TestEnrichLeverageFlowFacts._mock_http(monkeypatch, {})
        assert "turnover_to_oi_24h" not in ledger.enrich(
            "AAAUSDT", {"turnover_24h_usdt": 24_000_000.0, "last_price": 2.0}
        )


def test_prompt_version_buckets_the_v7_fact_set() -> None:
    """--grade buckets by PROMPT_VERSION: a fact-set or rubric change must land
    in a new bucket, never rewrite v6's forward record."""
    assert ledger.PROMPT_VERSION == "driver-judgment-v7-crime-pump"
