"""The exodus short's decision surface: config, cover clock, book shape.

The registered file is configs/lane2_exodus_short_v1.json; these tests pin
the contract the carry producer and the engine both rely on: notionals
render NEGATIVE with the fence stop, covers are decided by the clock alone,
and a torn state file reads as flat (covers, never strands).
"""

import json
from pathlib import Path

import pytest

from liquidity_migration.rules.exodus_short import (
    ExodusShortConfig,
    ExodusShortError,
    ExodusShortRecord,
    next_cover_deadline_ts_ms,
    records_from_payload,
    records_to_payload,
    render_exodus_book,
    split_due_covers,
)

CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "configs" / "lane2_exodus_short_v1.json"
)
MIN_MS = 60_000
S = 1_755_648_000_000  # an hour boundary, like every Bybit settlement


@pytest.fixture(scope="module")
def cfg() -> ExodusShortConfig:
    return ExodusShortConfig.from_json(CONFIG_PATH)


def _record(symbol: str = "DEEPUSDT", settlement_ts_ms: int = S) -> ExodusShortRecord:
    return ExodusShortRecord(
        symbol=symbol,
        notional_usdt=54.0,
        settlement_ts_ms=settlement_ts_ms,
        fired_ts_ms=settlement_ts_ms - 10 * MIN_MS,
    )


class TestRegisteredConfig:
    def test_the_registered_file_loads_with_the_measured_constants(
        self, cfg: ExodusShortConfig
    ) -> None:
        assert cfg.config_id == "lane2_exodus_short_v1"
        assert cfg.cover_minutes_after_settlement == 60
        assert cfg.entry_valid_minutes_after_settlement == 20
        assert cfg.stop_loss_fraction == 0.35
        assert cfg.entry_leverage == 2.0

    def test_an_unknown_trigger_basis_is_refused(self, tmp_path: Path) -> None:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        payload["rule"]["trigger"]["basis"] = "all_name_settlement_deaths"
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ExodusShortError, match="trigger basis"):
            ExodusShortConfig.from_json(bad)

    def test_an_unknown_sizing_basis_is_refused(self, tmp_path: Path) -> None:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        payload["rule"]["sizing"]["basis"] = "fixed_notional"
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ExodusShortError, match="sizing basis"):
            ExodusShortConfig.from_json(bad)


class TestCoverClock:
    def test_covers_exactly_at_settlement_plus_the_registered_minutes(
        self, cfg: ExodusShortConfig
    ) -> None:
        record = _record()
        cover = S + cfg.cover_minutes_after_settlement * MIN_MS
        kept, covered = split_due_covers([record], now_ms=cover - 1, cfg=cfg)
        assert kept == [record] and covered == []
        kept, covered = split_due_covers([record], now_ms=cover, cfg=cfg)
        assert kept == [] and covered == [record]

    def test_the_deadline_is_the_earliest_cover(self, cfg: ExodusShortConfig) -> None:
        a = _record("AUSDT", S)
        b = _record("BUSDT", S + 3_600_000)
        assert next_cover_deadline_ts_ms([b, a], cfg) == a.cover_ts_ms(cfg)
        assert next_cover_deadline_ts_ms([], cfg) is None


class TestBookShape:
    def test_records_render_negative_with_the_fence_stop(
        self, cfg: ExodusShortConfig
    ) -> None:
        text = render_exodus_book(
            [_record()], cfg=cfg, now_ms=S - 10 * MIN_MS, source="exodus_short"
        )
        book = json.loads(text)
        (target,) = book["targets"]
        assert target["notional_usdt"] == -54.0
        assert target["stop_loss_fraction"] == 0.35
        assert target["leverage"] == cfg.entry_leverage

    def test_validity_closes_entries_at_settlement_plus_the_window(
        self, cfg: ExodusShortConfig
    ) -> None:
        # The engine closes entries 15 minutes before a book expires, so
        # validity of S+20 means no fill later than S+5.
        text = render_exodus_book(
            [_record()], cfg=cfg, now_ms=S - 10 * MIN_MS, source="exodus_short"
        )
        assert json.loads(text)["valid_until_ms"] == (
            S + cfg.entry_valid_minutes_after_settlement * MIN_MS
        )

    def test_an_empty_book_is_cash_not_silence(self, cfg: ExodusShortConfig) -> None:
        text = render_exodus_book([], cfg=cfg, now_ms=S, source="exodus_short")
        book = json.loads(text)
        assert book["targets"] == []
        assert book["valid_until_ms"] > S


class TestStateFile:
    def test_records_round_trip(self, cfg: ExodusShortConfig) -> None:
        records = [_record("AUSDT"), _record("BUSDT", S + 3_600_000)]
        assert records_from_payload(records_to_payload(records)) == sorted(
            records, key=lambda r: r.symbol
        )

    def test_a_torn_state_file_reads_as_flat(self) -> None:
        # Losing state must cover every open short, never strand one: an
        # unreadable payload is an empty book, and absence is the exit.
        assert records_from_payload({"open": [{"symbol": "AUSDT"}]}) == []
        assert records_from_payload("not a mapping") == []
        assert records_from_payload({"open": "not a list"}) == []
