"""The trade-update differ: what pages the phone and what stays quiet."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "notify_book_changes",
    Path(__file__).resolve().parents[2] / "scripts" / "runtime" / "notify_book_changes.py",
)
notify = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(notify)


def _book(tmp: Path, rows: list[tuple[str, float]]) -> str:
    p = tmp / "book.json"
    p.write_text(json.dumps({"targets": [{"symbol": s, "notional_usdt": n} for s, n in rows]}))
    return str(p)


class TestReadPositiveTargets:
    def test_zero_rows_are_not_positions(self, tmp_path: Path) -> None:
        path = _book(tmp_path, [("AAAUSDT", 40.0), ("BBBUSDT", 0.0)])
        assert notify.read_positive_targets(path) == {"AAAUSDT": 40.0}

    def test_an_unreadable_book_is_none_not_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "torn.json"
        p.write_text("{ torn")
        assert notify.read_positive_targets(str(p)) is None

    def test_a_missing_book_is_none(self, tmp_path: Path) -> None:
        assert notify.read_positive_targets(str(tmp_path / "absent.json")) is None


class TestDiffMessages:
    def test_a_new_symbol_is_an_entry_with_its_size(self) -> None:
        msgs = notify.diff_messages("LONG", {}, {"ADAUSDT": 35.69})
        assert msgs == ["LONG entry: ADAUSDT $35.69"]

    def test_a_vanished_symbol_is_an_exit(self) -> None:
        msgs = notify.diff_messages("CARRY", {"EDENUSDT": 120.0}, {})
        assert msgs == ["CARRY exit: EDENUSDT"]

    def test_a_resize_stays_off_the_phone(self) -> None:
        assert notify.diff_messages("CARRY", {"EDENUSDT": 120.0}, {"EDENUSDT": 90.0}) == []

    def test_exodus_speaks_short_and_covered(self) -> None:
        assert notify.diff_messages("EXODUS", {}, {"ONGUSDT": 85.0}) == ["EXODUS short: ONGUSDT $85.00"]
        assert notify.diff_messages("EXODUS", {"ONGUSDT": 85.0}, {}) == ["EXODUS covered: ONGUSDT"]

    def test_messages_come_out_sorted_and_complete(self) -> None:
        msgs = notify.diff_messages(
            "LONG", {"OLDUSDT": 10.0}, {"AUSDT": 1.0, "BUSDT": 2.0}
        )
        assert msgs == ["LONG entry: AUSDT $1.00", "LONG entry: BUSDT $2.00", "LONG exit: OLDUSDT"]
