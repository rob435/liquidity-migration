from __future__ import annotations

import logging
import zlib

from liquidity_migration.continuous_identity import (
    continuous_order_link_id,
    continuous_suborder_link_id,
    continuous_trade_id,
    recover_snipe_trade_id_from_link,
)
from liquidity_migration.order_link_id import _base36, decode_entry_order_link_id


def test_continuous_identity_round_trips_component_link_and_trade_id_seq() -> None:
    sig = 1_700_000_123_456

    link = continuous_order_link_id("en-cp3", symbol="WIFUSDT", signal_ts_ms=sig, reentry_seq=2)

    assert decode_entry_order_link_id(link) == ("continuous", 1_700_000_123_000, 2, "p3")
    assert continuous_trade_id("STRAT", "WIFUSDT", sig, 0) == "STRAT-WIFUSDT-1700000123456"
    assert continuous_trade_id("STRAT", "WIFUSDT", sig, 2) == "STRAT-WIFUSDT-1700000123456-2"


def test_recover_snipe_trade_id_from_link_uses_supplied_component_tags() -> None:
    sig = 1_700_000_123_000
    trade_id = f"{continuous_trade_id('STRAT', 'WIFUSDT', sig, 1)}-p4p5-snipe"
    link = continuous_suborder_link_id("en-cs", symbol="WIFUSDT", signal_ts_ms=sig, trade_id=trade_id)

    assert (
        recover_snipe_trade_id_from_link(
            link,
            strategy_id="STRAT",
            symbol="WIFUSDT",
            signal_ts_ms=sig,
            components=("p3", "p4p5"),
            max_seq=3,
        )
        == trade_id
    )


def test_historical_snipe_decoder_reads_four_and_three_character_hashes() -> None:
    """Keep archived order attribution readable after sniper runtime removal."""

    sig = 1_700_000_123_000
    components = ("p3", "p4p3", "p4p5")
    for component in (*components, ""):
        for seq in (0, 1):
            base = continuous_trade_id("STRAT", "WIFUSDT", sig, seq)
            trade_id = f"{base}-{component}-snipe" if component else f"{base}-snipe"
            current_link = continuous_suborder_link_id(
                "en-cs",
                symbol="WIFUSDT",
                signal_ts_ms=sig,
                trade_id=trade_id,
            )
            legacy_hash = _base36(zlib.crc32(trade_id.encode("utf-8")) % (36**3)).rjust(3, "0")
            legacy_link = f"{current_link.rsplit('-x', 1)[0]}-x{legacy_hash}"

            for link in (current_link, legacy_link):
                assert (
                    recover_snipe_trade_id_from_link(
                        link,
                        strategy_id="STRAT",
                        symbol="WIFUSDT",
                        signal_ts_ms=sig,
                        components=components,
                    )
                    == trade_id
                )

    assert (
        recover_snipe_trade_id_from_link(
            "lm-en-cs-WIFUSDT-abc123",
            strategy_id="STRAT",
            symbol="WIFUSDT",
            signal_ts_ms=sig,
            components=components,
        )
        is None
    )


def test_historical_snipe_decoder_warns_only_on_ambiguous_hash(caplog) -> None:
    components = ("p3", "p4p3", "p4p5")
    collision: tuple[int, str] | None = None
    for seconds in range(1, 6_000):
        signal_ts_ms = seconds * 1_000
        counts: dict[str, int] = {}
        for seq in range(4):
            base = continuous_trade_id("STRAT", "WIFUSDT", signal_ts_ms, seq)
            for component in (*components, ""):
                candidate = f"{base}-{component}-snipe" if component else f"{base}-snipe"
                suffix = _base36(zlib.crc32(candidate.encode("utf-8")) % (36**3)).rjust(3, "0")
                counts[suffix] = counts.get(suffix, 0) + 1
        duplicate = next((suffix for suffix, count in counts.items() if count > 1), None)
        if duplicate is not None:
            collision = signal_ts_ms, duplicate
            break

    assert collision is not None
    signal_ts_ms, suffix = collision
    logger_name = "historical-snipe-decoder"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        assert (
            recover_snipe_trade_id_from_link(
                f"lm-en-cs-WIF-zzz-x{suffix}",
                strategy_id="STRAT",
                symbol="WIFUSDT",
                signal_ts_ms=signal_ts_ms,
                components=components,
                logger=logging.getLogger(logger_name),
            )
            is None
        )
    assert any("AMBIGUOUS" in record.message for record in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=logger_name):
        assert (
            recover_snipe_trade_id_from_link(
                "lm-en-cs-WIF-zzz-x000",
                strategy_id="STRAT",
                symbol="WIFUSDT",
                signal_ts_ms=999_000_000_000,
                components=components,
                logger=logging.getLogger(logger_name),
            )
            is None
        )
    assert not caplog.records
