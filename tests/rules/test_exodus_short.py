"""The exodus short's decision surface: config, cover clock, book shape.

The registered file is configs/lane2_exodus_short_v1.json; these tests pin
the contract the carry producer and the engine both rely on: quantities and
notionals render NEGATIVE with the fence stop, covers are decided by the clock
alone, and a torn state file is unknown rather than an invented flat decision.
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
        target_qty=3.2,
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
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        assert payload["rule"]["sizing"]["basis"] == "carry_position_at_fire"

    def test_metadata_names_the_runtime_without_claiming_venue_permission(self) -> None:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        assert payload["surface"] == "runtime strategy rule for demo and funded producers"
        assert payload["authorizes"] == (
            "rule parameters only; venue permission comes from the separately armed "
            "Rust engine and its host credential"
        )

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
        assert target["target_qty"] == -3.2
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

    def test_staggered_records_keep_independent_entry_deadlines(
        self, cfg: ExodusShortConfig
    ) -> None:
        text = render_exodus_book(
            [_record("EARLYUSDT", S), _record("LATEUSDT", S + 60 * MIN_MS)],
            cfg=cfg,
            now_ms=S - 10 * MIN_MS,
            source="exodus_short",
        )
        book = json.loads(text)
        targets = {row["symbol"]: row for row in book["targets"]}
        assert book["valid_until_ms"] == S + 80 * MIN_MS
        assert targets["EARLYUSDT"]["entry_valid_until_ms"] == S + 5 * MIN_MS
        assert targets["LATEUSDT"]["entry_valid_until_ms"] == S + 65 * MIN_MS

    def test_cover_records_render_as_named_zero_targets(
        self, cfg: ExodusShortConfig
    ) -> None:
        record = _record("DYNAMICUSDT")
        book = json.loads(
            render_exodus_book(
                [],
                cfg=cfg,
                now_ms=S + 60 * MIN_MS,
                source="exodus_short",
                cover_records=[record],
            )
        )
        assert book["targets"] == [
            {
                "leverage": 2.0,
                "notional_usdt": 0.0,
                "stop_loss_fraction": 0.35,
                "symbol": "DYNAMICUSDT",
            }
        ]

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

    def test_schema_v1_state_loads_without_an_exact_quantity(self) -> None:
        payload = {
            "schema_version": 1,
            "open": [
                {
                    "symbol": "AUSDT",
                    "notional_usdt": 54.0,
                    "settlement_ts_ms": S,
                    "fired_ts_ms": S - 10 * MIN_MS,
                }
            ],
        }
        assert records_from_payload(payload)[0].target_qty is None

    def test_original_unversioned_empty_state_remains_readable(self) -> None:
        assert records_from_payload({"open": []}) == []

    def test_original_unversioned_record_migrates_losslessly(
        self, cfg: ExodusShortConfig
    ) -> None:
        legacy = {
            "open": [
                {
                    "symbol": "AUSDT",
                    "notional_usdt": 54.0,
                    "settlement_ts_ms": S,
                    "fired_ts_ms": S - 10 * MIN_MS,
                }
            ]
        }
        records = records_from_payload(legacy)
        assert records[0].target_qty is None
        target = json.loads(
            render_exodus_book(
                records,
                cfg=cfg,
                now_ms=S - 10 * MIN_MS,
                source="exodus_short",
            )
        )["targets"][0]
        assert target["notional_usdt"] == -54.0
        assert target["target_qty"] is None

        migrated = records_to_payload(records)
        assert migrated["schema_version"] == 2
        assert migrated["open"][0]["target_qty"] is None
        assert records_from_payload(migrated) == records

    @pytest.mark.parametrize(
        "payload",
        [
            {"schema_version": 1, "open": [{"symbol": "AUSDT"}]},
            "not a mapping",
            {"schema_version": 1, "open": "not a list"},
            {},
            {"open": [], "unexpected": True},
        ],
    )
    def test_a_torn_state_file_fails_closed(self, payload) -> None:
        with pytest.raises(ValueError):
            records_from_payload(payload)
