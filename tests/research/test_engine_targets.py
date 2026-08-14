"""The research→engine target book renders exact, parseable, atomic JSON."""

import json

import pytest

from liquidity_migration.research.engine_targets import (
    TARGET_BOOK_VERSION,
    EngineTarget,
    render_target_book,
    write_target_book,
)


def _book(**overrides) -> str:
    kwargs = {
        "source": "carry_hold_v4_live_v1",
        "decision_ts_ms": 1786665600000,
        "valid_until_ms": 1786687200000,
        "targets": [
            EngineTarget("KAITOUSDT", 54.0, 0.35, 2.0),
            EngineTarget("COTIUSDT", 41.5, 0.35, 2.0),
        ],
    }
    kwargs.update(overrides)
    return render_target_book(**kwargs)


def test_book_round_trips_with_targets_sorted_by_symbol() -> None:
    parsed = json.loads(_book())
    assert parsed["version"] == TARGET_BOOK_VERSION
    assert parsed["source"] == "carry_hold_v4_live_v1"
    assert parsed["decision_ts_ms"] == 1786665600000
    assert [row["symbol"] for row in parsed["targets"]] == ["COTIUSDT", "KAITOUSDT"]
    assert parsed["targets"][1] == {
        "symbol": "KAITOUSDT",
        "notional_usdt": 54.0,
        "stop_loss_fraction": 0.35,
        "leverage": 2.0,
    }


def test_rendering_is_deterministic() -> None:
    assert _book() == _book()


def test_a_zero_notional_is_kept_as_an_explicit_exit() -> None:
    # Zero is an instruction to hold none, which is how a book says "close
    # this" without needing a separate verb.
    parsed = json.loads(_book(targets=[EngineTarget("KAITOUSDT", 0.0, 0.35)]))
    assert parsed["targets"][0]["notional_usdt"] == 0.0


def test_an_empty_book_is_legal_and_means_hold_nothing() -> None:
    # Deciding cash is a decision. Writing no book at all is not, and the
    # engine reads that differently: it holds steady.
    parsed = json.loads(_book(targets=[]))
    assert parsed["targets"] == []


def test_a_short_target_keeps_its_sign() -> None:
    parsed = json.loads(_book(targets=[EngineTarget("KAITOUSDT", -54.0, 0.35)]))
    assert parsed["targets"][0]["notional_usdt"] == -54.0


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"source": "bad source"}, "not a plain identifier"),
        ({"decision_ts_ms": 0}, "must be positive"),
        ({"valid_until_ms": 1786665600000}, "must be after"),
        ({"targets": [EngineTarget("btcusdt", 1.0, 0.35)]}, "upper-case"),
        ({"targets": [EngineTarget("BTC-USDT", 1.0, 0.35)]}, "upper-case"),
        ({"targets": [EngineTarget("BTCUSDT", float("nan"), 0.35)]}, "finite"),
        ({"targets": [EngineTarget("BTCUSDT", float("inf"), 0.35)]}, "finite"),
        ({"targets": [EngineTarget("BTCUSDT", 1.0, 0.0)]}, "between 0 and 1"),
        ({"targets": [EngineTarget("BTCUSDT", 1.0, 1.0)]}, "between 0 and 1"),
        ({"targets": [EngineTarget("BTCUSDT", 1.0, 0.35, 0.0)]}, "positive finite"),
        (
            {"targets": [EngineTarget("BTCUSDT", 1.0, 0.35), EngineTarget("BTCUSDT", 2.0, 0.35)]},
            "twice",
        ),
    ],
)
def test_bad_books_are_refused_with_a_named_reason(kwargs: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _book(**kwargs)


def test_writing_is_atomic_and_leaves_no_temp_file(tmp_path) -> None:
    path = tmp_path / "book" / "carry.json"
    write_target_book(path, _book())
    assert json.loads(path.read_text(encoding="utf-8"))["source"] == "carry_hold_v4_live_v1"
    assert list(path.parent.iterdir()) == [path], "the temp file must not survive"


def test_a_rewrite_replaces_the_previous_book_whole(tmp_path) -> None:
    path = tmp_path / "carry.json"
    write_target_book(path, _book())
    write_target_book(path, _book(targets=[EngineTarget("KAITOUSDT", 12.0, 0.35)]))
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert [row["symbol"] for row in parsed["targets"]] == ["KAITOUSDT"]
