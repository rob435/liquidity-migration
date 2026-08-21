"""The live entry gate's money-touching logic, tested pure.

The gate turns a judged trigger event into a real demo entry target, so the
decisions here — when to enter, when to leave, what the book says — get the
same treatment as any other order-path code: every branch proven, no live
call anywhere.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
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
H = 3_600_000


def _component(age_h: float = 0.0, *, price: float = 100.0, atr: float = 0.05) -> dict:
    return {
        "entry_ts_ms": NOW_MS - int(age_h * H),
        "trigger_price": price,
        "atr_pct": atr,
        "notional_usdt": 50.0,
    }


class TestExitReason:
    def test_fresh_position_with_price_inside_bands_stays(self) -> None:
        assert ledger.gate_exit_reason(_component(1.0), 101.0, NOW_MS) is None

    def test_wide_stop_level_exits_at_any_age(self) -> None:
        # 3 x 5% ATR below the 100 reference = 85.
        assert ledger.gate_exit_reason(_component(1.0), 84.9, NOW_MS) == "wide_stop_assumed"

    def test_decayed_stop_needs_the_age_and_the_level(self) -> None:
        # 1.5 x 5% below reference = 92.5; young position ignores it.
        assert ledger.gate_exit_reason(_component(47.0), 92.0, NOW_MS) is None
        assert ledger.gate_exit_reason(_component(49.0), 92.0, NOW_MS) == "decayed_stop"

    def test_take_profit_at_four_atr(self) -> None:
        assert ledger.gate_exit_reason(_component(1.0), 120.1, NOW_MS) == "take_profit"

    def test_time_stop_at_max_hold_even_without_a_price(self) -> None:
        assert ledger.gate_exit_reason(_component(72.5), None, NOW_MS) == "time_stop"

    def test_wide_stop_outranks_take_profit_when_both_read_true(self) -> None:
        # A garbage price cannot satisfy both bands; ordering still puts the
        # stop first so a broken feed fails toward closing, not holding.
        component = _component(1.0, atr=0.0)
        component["atr_pct"] = 0.0
        assert ledger.gate_exit_reason(component, 99.0, NOW_MS) == "wide_stop_assumed"


class TestSizing:
    def test_low_vol_name_gets_the_full_slot(self) -> None:
        # 10% annualized vol -> weight capped at 1.0 -> 5% of equity.
        assert ledger.gate_entry_notional(1000.0, 0.10 / 19.1049) == pytest.approx(50.0, rel=1e-3)

    def test_high_vol_name_is_cut_to_the_floor(self) -> None:
        # 300% annualized vol -> weight 0.30/3.0 = 0.10 -> floored at 0.25.
        assert ledger.gate_entry_notional(1000.0, 3.0 / 19.1049) == pytest.approx(12.5, rel=1e-3)

    def test_missing_vol_takes_the_full_slot(self) -> None:
        assert ledger.gate_entry_notional(1000.0, None) == pytest.approx(50.0)


def _heartbeat(tmp: Path, *, equity: float = 1500.0, age_s: float = 5.0) -> str:
    p = tmp / "heartbeat.json"
    p.write_text(json.dumps({"account_equity_usdt": equity, "wall_ts_ms": NOW_MS - age_s * 1000}))
    return str(p)


def _sibling(tmp: Path, symbols: list[str]) -> tuple[str, ...]:
    p = tmp / "sibling.json"
    p.write_text(
        json.dumps({"targets": [{"symbol": s, "notional_usdt": 40.0} for s in symbols]})
    )
    return (str(p),)


def _event(symbol: str, *, score: int = 7, would: bool = True) -> dict:
    return {
        "would_enter": would,
        "facts": {
            "symbol": symbol,
            "trigger_price": 10.0,
            "atr_14d_pct": 0.05,
            "sigma_daily_30d": 0.03,
            "trigger_window_h": 4,
        },
        "judgment": {"pump_quality_score": score},
    }


def _run(tmp: Path, events, prices=None, *, live=True, drain=False, heartbeat=None, siblings=()):
    import os

    os.environ["LLM_GATE_LIVE"] = "1" if live else ""
    os.environ["LLM_GATE_DRAIN"] = "1" if drain else ""
    try:
        return ledger.gate_run(
            tmp,
            events,
            prices or {},
            NOW,
            book_path=str(tmp / "book.json"),
            heartbeat_path=heartbeat or str(tmp / "missing-heartbeat.json"),
            sibling_paths=siblings,
        )
    finally:
        os.environ.pop("LLM_GATE_LIVE", None)
        os.environ.pop("LLM_GATE_DRAIN", None)


class TestGateRun:
    def test_entry_publishes_a_positive_target_with_the_wide_stop(self, tmp_path: Path) -> None:
        actions = _run(tmp_path, [_event("AAAUSDT")], heartbeat=_heartbeat(tmp_path))
        assert [a["action"] for a in actions] == ["entry"]
        book = json.loads((tmp_path / "book.json").read_text())
        assert book["source"] == "long_llm_gate_v1"
        (row,) = book["targets"]
        assert row["symbol"] == "AAAUSDT"
        assert row["notional_usdt"] > 0
        assert row["stop_loss_fraction"] == pytest.approx(0.15)
        assert book["valid_until_ms"] > book["decision_ts_ms"]

    def test_no_heartbeat_means_no_entry_and_says_so(self, tmp_path: Path) -> None:
        actions = _run(tmp_path, [_event("AAAUSDT")])
        assert [a["action"] for a in actions] == ["skip:no_equity_read"]
        assert json.loads((tmp_path / "book.json").read_text())["targets"] == []

    def test_stale_heartbeat_fails_closed(self, tmp_path: Path) -> None:
        hb = _heartbeat(tmp_path, age_s=400.0)
        actions = _run(tmp_path, [_event("AAAUSDT")], heartbeat=hb)
        assert [a["action"] for a in actions] == ["skip:no_equity_read"]

    def test_a_sibling_holding_the_symbol_blocks_the_entry(self, tmp_path: Path) -> None:
        actions = _run(
            tmp_path,
            [_event("AAAUSDT")],
            heartbeat=_heartbeat(tmp_path),
            siblings=_sibling(tmp_path, ["AAAUSDT"]),
        )
        assert [a["action"] for a in actions] == ["skip:sibling_holds"]

    def test_gate_off_with_nothing_held_touches_nothing(self, tmp_path: Path) -> None:
        assert _run(tmp_path, [_event("AAAUSDT")], live=False) == []
        assert not (tmp_path / "book.json").exists()

    def test_gate_off_still_manages_exits_for_held_positions(self, tmp_path: Path) -> None:
        _run(tmp_path, [_event("AAAUSDT")], heartbeat=_heartbeat(tmp_path))
        actions = _run(tmp_path, [], prices={"AAAUSDT": 8.4}, live=False)
        assert [a["action"] for a in actions] == ["exit:wide_stop_assumed"]
        book = json.loads((tmp_path / "book.json").read_text())
        (row,) = book["targets"]
        assert row["notional_usdt"] == 0.0

    def test_drain_zeroes_everything_now(self, tmp_path: Path) -> None:
        _run(tmp_path, [_event("AAAUSDT")], heartbeat=_heartbeat(tmp_path))
        actions = _run(tmp_path, [], drain=True)
        assert [a["action"] for a in actions] == ["exit:drain"]
        book = json.loads((tmp_path / "book.json").read_text())
        assert all(row["notional_usdt"] == 0.0 for row in book["targets"])

    def test_an_exited_symbol_sits_in_cooldown(self, tmp_path: Path) -> None:
        hb = _heartbeat(tmp_path)
        _run(tmp_path, [_event("AAAUSDT")], heartbeat=hb)
        _run(tmp_path, [], prices={"AAAUSDT": 8.4}, heartbeat=hb)
        # Inside the closing grace the re-entry is blocked without a row.
        assert _run(tmp_path, [_event("AAAUSDT")], heartbeat=hb) == []
        # Past the grace the 7-day cooldown takes over, and says so.
        state_path = tmp_path / "gate_state.json"
        state = json.loads(state_path.read_text())
        state["closing"] = {}
        state_path.write_text(json.dumps(state))
        actions = _run(tmp_path, [_event("AAAUSDT")], heartbeat=hb)
        assert [a["action"] for a in actions] == ["skip:cooldown"]

    def test_capacity_is_hard(self, tmp_path: Path) -> None:
        hb = _heartbeat(tmp_path)
        events = [_event(f"SYM{i}USDT") for i in range(ledger.GATE_MAX_CONCURRENT + 1)]
        actions = _run(tmp_path, events, heartbeat=hb)
        assert [a["action"] for a in actions].count("entry") == ledger.GATE_MAX_CONCURRENT
        assert "skip:capacity" in [a["action"] for a in actions]

    def test_only_would_enter_events_enter(self, tmp_path: Path) -> None:
        actions = _run(
            tmp_path,
            [_event("AAAUSDT", would=False), _event("BBBUSDT")],
            heartbeat=_heartbeat(tmp_path),
        )
        assert [(a["action"], a["symbol"]) for a in actions] == [("entry", "BBBUSDT")]
