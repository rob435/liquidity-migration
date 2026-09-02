"""The recorder's pure parts: tier universes, topic planning, shards, the status file."""

from __future__ import annotations

import json
import queue
import shutil
from pathlib import Path
from typing import Any

import pytest

from market_tape.config import (
    CaptureConfig,
    ConfigError,
    Feed,
    StorageSettings,
    Tier,
    Universe,
    VenueSettings,
    parse_config,
)
from market_tape.record import Recorder, Shard, shard_topics
from market_tape.schema import SCHEMA_VERSION
from market_tape.venues.bybit import BybitAdapter

needs_zstd = pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd is not installed")

DAY_NS = 86_400 * 1_000_000_000
BASE_NS = 1_800_000_000 * 1_000_000_000  # 2027-01-15T08:00:00Z

DEEP_FEEDS = (Feed("book", "50"), Feed("book", "1"), Feed("trades"), Feed("ticker"), Feed("liquidations"))
WIDE_FEEDS = (Feed("book", "1"), Feed("trades"), Feed("ticker"), Feed("liquidations"))


def instrument(symbol: str, quote: str = "USDT") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "status": "Trading",
        "quoteCoin": quote,
        "settleCoin": quote,
        "contractType": "LinearPerpetual",
    }


def build(tmp_path: Path, *tiers: Tier, cadence: str = "day", per_connection: int = 150) -> Recorder:
    config = CaptureConfig(
        venue=VenueSettings("bybit", "linear"),
        storage=StorageSettings(root=tmp_path, queue_frames=16, status_interval_seconds=30.0),
        tiers=tiers,
        topics_per_connection=per_connection,
        snapshot_cadence=cadence,
        source_path=Path("deploy/capture/bybit-linear.toml"),
    )
    return Recorder(config, adapter=BybitAdapter(rest_url="http://unused"))


def test_a_recorder_needs_a_root_and_a_feed_the_venue_publishes(tmp_path: Path) -> None:
    tier = Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",)))
    config = CaptureConfig(venue=VenueSettings("bybit", "linear"), storage=StorageSettings(), tiers=(tier,))

    with pytest.raises(ConfigError, match="storage root"):
        Recorder(config, adapter=BybitAdapter())

    open_interest = Tier("deep", (Feed("open_interest", "60s"),), Universe("symbols", symbols=("BTCUSDT",)))
    with pytest.raises(ConfigError, match="open interest"):
        build(tmp_path, open_interest)


def test_a_symbol_file_that_names_nothing_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "symbols.txt"
    path.write_text("# nothing yet\n", encoding="utf-8")
    tier = Tier("deep", (Feed("trades"),), Universe("file", path=path))

    with pytest.raises(ConfigError, match="names no symbols"):
        build(tmp_path, tier)

    path.write_text("btcusdt\nETHUSDT, btcusdt\n", encoding="utf-8")
    recorder = build(tmp_path, tier)
    assert recorder.static_symbols["deep"] == ("BTCUSDT", "ETHUSDT")


def test_static_universes_resolve_without_the_venue_tables(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("deep", DEEP_FEEDS, Universe("symbols", symbols=("BTCUSDT", "AGIUSDT"))))

    assert recorder.resolve_tiers(BASE_NS, None) == {"deep": ["AGIUSDT", "BTCUSDT"]}


def test_the_listed_universe_takes_the_quote_and_a_dynamic_tier_without_tables_is_empty(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("wide", WIDE_FEEDS, Universe("listed", quote="USDT")))
    tables = {"instruments": [instrument("BTCUSDT"), instrument("ETHPERP", "USDC")], "tickers": []}

    assert recorder.resolve_tiers(BASE_NS, tables) == {"wide": ["BTCUSDT"]}
    assert recorder.resolve_tiers(BASE_NS, None) == {"wide": []}


def test_top_turnover_takes_the_ranked_head_of_the_listed_names(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("busy", (Feed("trades"),), Universe("top_turnover", top=2, quote="USDT")))
    tables = {
        "instruments": [instrument("BTCUSDT"), instrument("ETHUSDT"), instrument("AGIUSDT")],
        "tickers": [
            {"symbol": "AGIUSDT", "turnover24h": "10"},
            {"symbol": "BTCUSDT", "turnover24h": "9000"},
            {"symbol": "ETHUSDT", "turnover24h": "500"},
            {"symbol": "SOLUSDT", "turnover24h": "1000000"},
        ],
    }

    # SOL outranks everything but is not in the instrument table, so it is not listed.
    assert recorder.resolve_tiers(BASE_NS, tables) == {"busy": ["BTCUSDT", "ETHUSDT"]}


def test_an_exclude_tiers_universe_drops_the_names_an_earlier_tier_holds(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("deep", DEEP_FEEDS, Universe("symbols", symbols=("BTCUSDT",))),
        Tier("wide", WIDE_FEEDS, Universe("listed", quote="USDT", exclude_tiers=("deep",))),
    )
    tables = {"instruments": [instrument("BTCUSDT"), instrument("AGIUSDT")], "tickers": []}

    assert recorder.resolve_tiers(BASE_NS, tables) == {"deep": ["BTCUSDT"], "wide": ["AGIUSDT"]}


def test_a_crowded_name_keeps_its_tier_for_two_days_and_a_delisted_one_drops_at_once(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("deep", DEEP_FEEDS, Universe("symbols", symbols=("BTCUSDT",))),
        Tier(
            "crowded",
            (Feed("book", "50"),),
            Universe("funding_below", threshold_bp=10.0, sticky_days=2, quote="USDT", exclude_tiers=("deep",)),
        ),
    )
    instruments = [instrument(symbol) for symbol in ("BTCUSDT", "AGIUSDT", "SOMIUSDT")]
    days = [
        [{"symbol": "AGIUSDT", "fundingRate": "-0.0020"}, {"symbol": "SOMIUSDT", "fundingRate": "0.0001"}],
        [{"symbol": "AGIUSDT", "fundingRate": "0.0001"}, {"symbol": "SOMIUSDT", "fundingRate": "-0.0030"}],
        [{"symbol": "AGIUSDT", "fundingRate": "0.0001"}, {"symbol": "SOMIUSDT", "fundingRate": "0.0001"}],
    ]

    first = recorder.resolve_tiers(BASE_NS, {"instruments": instruments, "tickers": days[0]})
    assert first["crowded"] == ["AGIUSDT"]

    # Day two: SOMI qualifies, AGI recovered but stays through its second day.
    second = recorder.resolve_tiers(BASE_NS + DAY_NS, {"instruments": instruments, "tickers": days[1]})
    assert second["crowded"] == ["AGIUSDT", "SOMIUSDT"]

    # Day three: AGI ages out, SOMI keeps its second day, and a delisted name drops.
    instruments.pop()
    third = recorder.resolve_tiers(BASE_NS + 2 * DAY_NS, {"instruments": instruments, "tickers": days[2]})
    assert third["crowded"] == []


def test_a_crowded_name_at_exactly_the_threshold_qualifies_and_a_shallower_one_does_not(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("crowded", (Feed("book", "50"),), Universe("funding_below", threshold_bp=10.0, sticky_days=1, quote="USDT")),
    )
    tables = {
        "instruments": [instrument(symbol) for symbol in ("AGIUSDT", "SOMIUSDT", "DOGEUSDT", "BADUSDT", "NEWUSDT")],
        "tickers": [
            {"symbol": "AGIUSDT", "fundingRate": "-0.0012"},
            {"symbol": "SOMIUSDT", "fundingRate": "-0.0010"},
            {"symbol": "DOGEUSDT", "fundingRate": "-0.0009"},
            {"symbol": "BADUSDT", "fundingRate": "n/a"},
            {"symbol": "NEWUSDT"},
        ],
    }

    assert recorder.resolve_tiers(BASE_NS, tables)["crowded"] == ["AGIUSDT", "SOMIUSDT"]


def test_a_topic_an_earlier_tier_claimed_is_not_subscribed_twice(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("deep", (Feed("book", "50"), Feed("trades")), Universe("symbols", symbols=("BTCUSDT",))),
        Tier("wide", (Feed("book", "50"),), Universe("listed", quote="USDT")),
    )
    tables = {"instruments": [instrument("BTCUSDT"), instrument("ETHUSDT")], "tickers": []}

    resolved = recorder.resolve_tiers(BASE_NS, tables)
    topics, feeds_by_symbol = recorder.plan_topics(resolved)

    assert resolved == {"deep": ["BTCUSDT"], "wide": ["BTCUSDT", "ETHUSDT"]}
    assert topics == {
        "deep": ["orderbook.50.BTCUSDT", "publicTrade.BTCUSDT"],
        "wide": ["orderbook.50.ETHUSDT"],
    }
    assert [feed.text for feed in feeds_by_symbol["BTCUSDT"]] == ["book:50", "trades"]
    assert [feed.text for feed in feeds_by_symbol["ETHUSDT"]] == ["book:50"]


def test_topics_are_sharded_in_order(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("deep", DEEP_FEEDS, Universe("symbols", symbols=("A", "B", "C"))),
        per_connection=7,
    )
    topics = recorder.plan_topics(recorder.resolve_tiers(BASE_NS, None))[0]["deep"]

    assert len(topics) == 15
    shards = shard_topics(topics, 7)
    assert [len(shard) for shard in shards] == [7, 7, 1]
    assert [topic for shard in shards for topic in shard] == topics
    assert shard_topics([], 7) == []
    with pytest.raises(ValueError):
        shard_topics(topics, 0)


@needs_zstd
def test_the_first_refresh_fills_the_tiers_and_starts_no_shard(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))),
        Tier("wide", (Feed("ticker"),), Universe("listed", quote="USDT", exclude_tiers=("deep",))),
    )
    tables = {"instruments": [instrument("BTCUSDT"), instrument("AGIUSDT")], "tickers": [{"symbol": "AGIUSDT"}]}
    recorder.adapter.fetch_tables = lambda: tables  # type: ignore[method-assign]

    recorder._refresh(BASE_NS, restart=False)

    assert recorder.tier_symbols == {"deep": ["BTCUSDT"], "wide": ["AGIUSDT"]}
    assert recorder.tier_topics == {"deep": ["publicTrade.BTCUSDT"], "wide": ["tickers.AGIUSDT"]}
    assert recorder.tier_shards == {"deep": [], "wide": []}
    assert recorder.lanes == []
    assert sorted(recorder.feeds_by_symbol) == ["AGIUSDT", "BTCUSDT"]
    assert recorder.snapshot_failures == 0
    assert recorder.snapshots.last_ns == BASE_NS
    assert sorted(path.name.split("-")[0] for path in (tmp_path / "2027-01-15" / "08" / "_meta").iterdir()) == [
        "instruments",
        "tickers",
    ]


def test_a_venue_that_will_not_answer_leaves_the_snapshot_clock_open(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))),
        Tier("wide", (Feed("ticker"),), Universe("listed", quote="USDT")),
    )

    def refuse() -> dict[str, list[dict[str, Any]]]:
        raise RuntimeError("venue refused")

    recorder.adapter.fetch_tables = refuse  # type: ignore[method-assign]
    recorder._refresh(BASE_NS, restart=False)

    assert recorder.snapshot_failures == 1
    assert recorder.snapshots.last_key is None
    assert recorder.snapshots.due(BASE_NS)
    assert recorder.tables is None
    assert recorder.tier_symbols == {"deep": ["BTCUSDT"], "wide": []}


def test_the_status_file_carries_what_the_host_watchdog_reads(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("deep", (Feed("book", "50"), Feed("trades")), Universe("symbols", symbols=("BTCUSDT",))),
        Tier("wide", (Feed("ticker"),), Universe("listed", quote="USDT", exclude_tiers=("deep",))),
    )
    recorder.tier_symbols = {"deep": ["BTCUSDT"], "wide": ["AGIUSDT"]}
    recorder.tier_topics = {"deep": ["orderbook.50.BTCUSDT", "publicTrade.BTCUSDT"], "wide": ["tickers.AGIUSDT"]}
    recorder.tier_shards["deep"] = [
        Shard(
            index=0,
            tier="deep",
            topics=recorder.tier_topics["deep"],
            adapter=recorder.adapter,
            frames=queue.Queue(),
            on_frame=recorder._on_frame,
            on_overrun=recorder._on_overrun,
            emit=recorder.emit,
        )
    ]
    recorder._on_frame(BASE_NS)
    recorder._on_overrun()
    recorder.disk_dropped_frames = 3
    recorder.disk_blocked = True

    recorder._write_status()
    payload = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))

    assert payload["kind"] == "forward_capture_status"
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["venue"] == "bybit" and payload["market"] == "linear"
    assert payload["config"] == "deploy/capture/bybit-linear.toml"
    assert payload["last_receive_ns"] == BASE_NS
    assert payload["received_frames"] == 1
    assert payload["dropped_frames"] == 1
    assert payload["disk_dropped_frames"] == 3
    assert payload["disk_blocked"] is True
    assert payload["queue_capacity"] == 16
    assert payload["status_interval_seconds"] == 30.0
    assert payload["shards"] == [
        {"index": 0, "tier": "deep", "topics": 2, "connected": False, "reconnects": 0, "last_message_ns": 0}
    ]
    assert payload["tiers"] == [
        {
            "name": "deep",
            "universe": "symbols",
            "feeds": ["book:50", "trades"],
            "symbols": 1,
            "topics": 2,
            "names": ["BTCUSDT"],
        },
        {
            "name": "wide",
            "universe": "listed",
            "feeds": ["ticker"],
            "symbols": 1,
            "topics": 1,
            "names": ["AGIUSDT"],
        },
    ]


def test_a_wide_tier_lists_its_count_and_not_every_name(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("wide", (Feed("ticker"),), Universe("listed", quote="USDT")))
    recorder.tier_symbols["wide"] = [f"SYM{index:03d}USDT" for index in range(65)]

    status = recorder.tier_status(recorder.config.tiers[0])

    assert status["symbols"] == 65
    assert "names" not in status


def test_a_row_handed_in_by_a_side_lane_reaches_the_writer_queue(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))))

    recorder.emit({"kind": "ticker", "symbol": "BTCUSDT", "local_receive_ts_ns": BASE_NS})

    kind, payload, received_ns = recorder.frames.get_nowait()
    assert kind == "rows"
    assert payload == [{"kind": "ticker", "symbol": "BTCUSDT", "local_receive_ts_ns": BASE_NS}]
    assert received_ns == BASE_NS
    assert recorder.received_frames == 1
    assert recorder.last_receive_ns == BASE_NS


def test_the_host_config_drives_the_same_recorder(tmp_path: Path) -> None:
    text = """
[venue]
name = "bybit"
market = "linear"

[snapshots]
cadence = "hour"

[[tier]]
name = "deep"
feeds = ["book:50", "trades"]
universe = { kind = "symbols", symbols = ["BTCUSDT"] }

[[tier]]
name = "wide"
feeds = ["book:1"]
universe = { kind = "listed", quote = "USDT", exclude_tiers = ["deep"] }
"""
    import tomllib

    config = parse_config(tomllib.loads(text), base_dir=tmp_path)
    recorder = Recorder(config, root=tmp_path, adapter=BybitAdapter(rest_url="http://unused"))
    tables = {"instruments": [instrument("BTCUSDT"), instrument("AGIUSDT")], "tickers": []}

    topics = recorder.plan_topics(recorder.resolve_tiers(BASE_NS, tables))[0]

    assert recorder.snapshots.cadence == "hour"
    assert topics == {"deep": ["orderbook.50.BTCUSDT", "publicTrade.BTCUSDT"], "wide": ["orderbook.1.AGIUSDT"]}
