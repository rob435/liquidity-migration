"""The research→engine target book renders exact, parseable, atomic JSON."""

import json

import pytest

from liquidity_migration.rules.engine_targets import (
    TARGET_BOOK_VERSION,
    TARGET_BOOK_EXTENDED_VERSION,
    EngineTarget,
    parse_target_book_bytes,
    publish_target_book,
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


def test_a_target_can_carry_its_own_entry_deadline() -> None:
    deadline = 1786665900000
    text = _book(targets=[EngineTarget("KAITOUSDT", -54.0, 0.35, 2.0, deadline)])
    parsed = json.loads(text)
    assert parsed["version"] == TARGET_BOOK_EXTENDED_VERSION
    assert parsed["targets"][0]["entry_valid_until_ms"] == deadline
    assert parsed["targets"][0]["target_qty"] is None
    assert parse_target_book_bytes(text.encode()).targets[0].entry_valid_until_ms == deadline


def test_an_exact_quantity_promotes_v2_and_round_trips() -> None:
    text = _book(
        targets=[EngineTarget("KAITOUSDT", -54.0, 0.35, 2.0, target_qty=-3.2)]
    )
    payload = json.loads(text)
    assert payload["version"] == TARGET_BOOK_EXTENDED_VERSION
    assert payload["targets"][0]["entry_valid_until_ms"] is None
    assert payload["targets"][0]["target_qty"] == -3.2
    assert parse_target_book_bytes(text.encode()).targets[0].target_qty == -3.2


def test_deadline_fields_and_versions_cannot_be_mixed() -> None:
    legacy = json.loads(_book())
    legacy["targets"][0]["entry_valid_until_ms"] = legacy["valid_until_ms"]
    with pytest.raises(ValueError, match="invalid fields"):
        parse_target_book_bytes(json.dumps(legacy).encode())

    current = json.loads(
        _book(
            targets=[
                EngineTarget("KAITOUSDT", -54.0, 0.35, 2.0, 1786665900000)
            ]
        )
    )
    del current["targets"][0]["entry_valid_until_ms"]
    with pytest.raises(ValueError, match="version 2"):
        parse_target_book_bytes(json.dumps(current).encode())


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
            {"targets": [EngineTarget("BTCUSDT", 1.0, 0.35, 2.0, 0)]},
            "entry_valid_until_ms",
        ),
        (
            {"targets": [EngineTarget("BTCUSDT", 1.0, 0.35, target_qty=0.0)]},
            "target_qty",
        ),
        (
            {"targets": [EngineTarget("BTCUSDT", 1.0, 0.35, target_qty=-1.0)]},
            "same sign",
        ),
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


def test_publication_archives_exact_content_before_activation(tmp_path) -> None:
    path = tmp_path / "carry.json"
    first = publish_target_book(path, _book())
    first_bytes = first.object_path.read_bytes()

    second = publish_target_book(
        path,
        _book(targets=[EngineTarget("KAITOUSDT", 12.0, 0.35)]),
    )

    assert first.object_path != second.object_path
    assert first.object_path.read_bytes() == first_bytes
    assert second.object_path.read_bytes() == path.read_bytes()
