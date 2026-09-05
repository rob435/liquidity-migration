"""The recorder's pure parts: universes, live promotion, topic planning, shards, bytes, budget, status."""

from __future__ import annotations

import json
import os
import logging
import shutil
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from market_tape.config import (
    BudgetSettings,
    CaptureConfig,
    ConfigError,
    Feed,
    StorageSettings,
    Tier,
    Universe,
    VenueSettings,
    parse_config,
)
from market_tape import record
from market_tape.record import BudgetController, ByteMeter, Recorder, Shard, shard_topics
from market_tape.schema import SCHEMA_VERSION
from market_tape.venues.bybit import BybitAdapter

needs_zstd = pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd is not installed")

HOUR_NS = 3_600 * 1_000_000_000
DAY_NS = 24 * HOUR_NS
BASE_NS = 1_800_000_000 * 1_000_000_000  # 2027-01-15T08:00:00Z

DEEP_FEEDS = (Feed("book", "50"), Feed("book", "1"), Feed("trades"), Feed("ticker"), Feed("liquidations"))
WIDE_FEEDS = (Feed("ticker"), Feed("liquidations"))


def instrument(symbol: str, quote: str = "USDT", symbol_type: str = "") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "status": "Trading",
        "quoteCoin": quote,
        "settleCoin": quote,
        "contractType": "LinearPerpetual",
        "symbolType": symbol_type,
    }


def build(
    tmp_path: Path,
    *tiers: Tier,
    cadence: str = "day",
    per_connection: int = 150,
    budget: BudgetSettings | None = None,
    reanchor: bool = True,
) -> Recorder:
    config = CaptureConfig(
        venue=VenueSettings("bybit", "linear"),
        storage=StorageSettings(root=tmp_path, queue_frames=16, status_interval_seconds=30.0),
        tiers=tiers,
        topics_per_connection=per_connection,
        reanchor_books_each_hour=reanchor,
        snapshot_cadence=cadence,
        budget=budget or BudgetSettings(),
        source_path=Path("deploy/capture/bybit-linear.toml"),
    )
    return Recorder(config, adapter=BybitAdapter(rest_url="http://unused"))


def ticker(symbol: str, **values: float) -> dict[str, Any]:
    return {"kind": "ticker", "symbol": symbol, "values": values}


# ---------------------------------------------------------------- building


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


# ---------------------------------------------------------------- universes


def test_static_universes_resolve_without_the_venue_tables(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("deep", DEEP_FEEDS, Universe("symbols", symbols=("BTCUSDT", "AGIUSDT"))))

    assert recorder.resolve_tiers(BASE_NS, None) == {"deep": ["AGIUSDT", "BTCUSDT"]}


def test_the_listed_universe_takes_the_quote_and_a_dynamic_tier_without_tables_is_empty(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("wide", WIDE_FEEDS, Universe("listed", quote="USDT")))
    tables = {"instruments": [instrument("BTCUSDT"), instrument("ETHPERP", "USDC")], "tickers": []}

    assert recorder.resolve_tiers(BASE_NS, tables) == {"wide": ["BTCUSDT"]}
    recorder.tables = None
    assert recorder.resolve_tiers(BASE_NS, None) == {"wide": []}


def test_a_stock_perpetual_enters_no_tier_however_it_ranks_or_funds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The venue lists stocks, ETFs and commodities as perpetuals. The sleeves
    never trade them, so the capture subscribes nothing for them: not the
    ranked tiers, not the funding tiers, not the wide ticker."""

    recorder = build(
        tmp_path,
        Tier("core", DEEP_FEEDS, Universe("top_turnover", top=2, quote="USDT")),
        Tier(
            "crowded",
            (Feed("book", "50"),),
            Universe("funding_below", threshold_bp=8.0, quote="USDT", exclude_tiers=("core",)),
        ),
        Tier("wide", WIDE_FEEDS, Universe("listed", quote="USDT", exclude_tiers=("core", "crowded"))),
    )
    tables = {
        "instruments": [
            instrument("BTCUSDT"),
            instrument("MYXUSDT", symbol_type="innovation"),
            instrument("SKHYNIXUSDT", symbol_type="stock"),
            instrument("SOXLUSDT", symbol_type="ETF"),
            instrument("XAUUSDT", symbol_type="commodity"),
        ],
        "tickers": [
            {"symbol": "SKHYNIXUSDT", "turnover24h": "9000000", "fundingRate": "-0.0050"},
            {"symbol": "XAUUSDT", "turnover24h": "8000000", "fundingRate": "-0.0050"},
            {"symbol": "BTCUSDT", "turnover24h": "7000", "fundingRate": "0.0001"},
            {"symbol": "MYXUSDT", "turnover24h": "10", "fundingRate": "-0.0050"},
            {"symbol": "SOXLUSDT", "turnover24h": "5", "fundingRate": "0.0001"},
        ],
    }

    resolved = recorder.resolve_tiers(BASE_NS, tables)

    # The stock and the commodity outrank everything and fund at -50 bp; the
    # crypto names take their places.
    assert resolved == {"core": ["BTCUSDT", "MYXUSDT"], "crowded": [], "wide": []}
    topics, _ = recorder.plan_topics(resolved)
    assert not any("SKHYNIX" in t or "SOXL" in t or "XAU" in t for tier in topics.values() for t in tier)

    with caplog.at_level(logging.INFO):
        recorder._log_listed(tables)
    assert "2 USDT perpetuals in the domain; outside it ETF=1 commodity=1 stock=1" in caplog.text


def test_a_ranked_member_keeps_its_place_for_sticky_hours_after_it_last_ranked_inside_top(tmp_path: Path) -> None:
    """LONG holds a name that surged into the top ten for up to three days; by
    then it can sit at rank 300. The rank hysteresis alone drops its book
    mid-hold; the time floor keeps it until the hold and its exit are on tape."""

    recorder = build(
        tmp_path,
        Tier(
            "core", (Feed("book", "50"),), Universe("top_turnover", top=1, leave_top=2, sticky_hours=4.0, quote="USDT")
        ),
    )
    names = ("AUSDT", "BUSDT", "CUSDT", "PUMPUSDT")
    recorder.tables = {"instruments": [instrument(name) for name in names], "tickers": []}
    for name, turnover in zip(names, (300.0, 200.0, 100.0, 9000.0)):
        recorder.live.observe(name, {"turnover_24h": turnover}, BASE_NS)
    assert recorder.resolve_tiers(BASE_NS + 1)["core"] == ["PUMPUSDT"]

    # The pump fades to rank 4, past leave_top. Rank alone would drop it now.
    recorder.live.observe("PUMPUSDT", {"turnover_24h": 1.0}, BASE_NS + 2)
    assert recorder.resolve_tiers(BASE_NS + 3)["core"] == ["AUSDT", "PUMPUSDT"]
    assert recorder.resolve_tiers(BASE_NS + 4 * HOUR_NS)["core"] == ["AUSDT", "PUMPUSDT"]
    # Four hours after it last ranked inside the top, it goes.
    assert recorder.resolve_tiers(BASE_NS + 1 + 4 * HOUR_NS)["core"] == ["AUSDT"]

    # Without the floor the old hysteresis is unchanged: the default is off.
    bare = build(
        tmp_path / "bare",
        Tier("core", (Feed("book", "50"),), Universe("top_turnover", top=1, leave_top=2, quote="USDT")),
    )
    bare.tables = recorder.tables
    for name, turnover in zip(names, (300.0, 200.0, 100.0, 9000.0)):
        bare.live.observe(name, {"turnover_24h": turnover}, BASE_NS)
    bare.resolve_tiers(BASE_NS + 1)
    bare.live.observe("PUMPUSDT", {"turnover_24h": 1.0}, BASE_NS + 2)
    assert bare.resolve_tiers(BASE_NS + 3)["core"] == ["AUSDT"]


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


def test_top_turnover_follows_the_ticker_live_and_leaves_only_below_the_wider_rank(tmp_path: Path) -> None:
    recorder = build(
        tmp_path, Tier("core", (Feed("trades"),), Universe("top_turnover", top=2, leave_top=3, quote="USDT"))
    )
    names = ["AUSDT", "BUSDT", "CUSDT", "DUSDT"]
    tables = {
        "instruments": [instrument(name) for name in names],
        "tickers": [
            {"symbol": name, "turnover24h": str(turnover)} for name, turnover in zip(names, (400, 300, 200, 100))
        ],
    }
    assert recorder.resolve_tiers(BASE_NS, tables)["core"] == ["AUSDT", "BUSDT"]

    # D's turnover explodes on the ticker: it enters at once, B slips to rank 3 and stays.
    recorder.live.observe("DUSDT", {"turnover_24h": 350.0}, BASE_NS + 1)
    assert recorder.resolve_tiers(BASE_NS + 2)["core"] == ["AUSDT", "BUSDT", "DUSDT"]

    # C overtakes both: C enters at rank 2, D holds rank 3, B is rank 4, past leave_top, and goes.
    recorder.live.observe("CUSDT", {"turnover_24h": 360.0}, BASE_NS + 3)
    assert recorder.resolve_tiers(BASE_NS + 4)["core"] == ["AUSDT", "CUSDT", "DUSDT"]


def test_a_live_tier_without_an_instrument_table_still_honours_its_quote(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("busy", (Feed("trades"),), Universe("top_turnover", top=5, quote="USDT")))
    # Cold start: no instrument table has landed, so the ticker stream is the only
    # universe there is, and it carries every symbol the venue streams -- other
    # quotes and the venue's own non-perpetual naming included.
    for symbol, turnover in (
        ("BTCUSDT", 900.0),
        ("WLDUSDC", 800.0),
        ("ADAUSD_PERP", 700.0),
        ("ETHUSDT", 600.0),
    ):
        recorder.live.observe(symbol, {"turnover_24h": turnover}, BASE_NS)

    assert recorder.resolve_tiers(BASE_NS + 1)["busy"] == ["BTCUSDT", "ETHUSDT"]


def test_an_exclude_tiers_universe_drops_the_names_an_earlier_tier_holds(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("deep", DEEP_FEEDS, Universe("symbols", symbols=("BTCUSDT",))),
        Tier("wide", WIDE_FEEDS, Universe("listed", quote="USDT", exclude_tiers=("deep",))),
    )
    tables = {"instruments": [instrument("BTCUSDT"), instrument("AGIUSDT")], "tickers": []}

    assert recorder.resolve_tiers(BASE_NS, tables) == {"deep": ["BTCUSDT"], "wide": ["AGIUSDT"]}


def test_a_crowded_name_keeps_its_tier_for_its_sticky_hours_and_a_delisted_one_drops_at_once(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("deep", DEEP_FEEDS, Universe("symbols", symbols=("BTCUSDT",))),
        Tier(
            "crowded",
            (Feed("book", "50"),),
            Universe("funding_below", threshold_bp=10.0, sticky_hours=48.0, quote="USDT", exclude_tiers=("deep",)),
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

    # Day two: SOMI qualifies, AGI recovered but is inside its 48 hours.
    second = recorder.resolve_tiers(BASE_NS + DAY_NS, {"instruments": instruments, "tickers": days[1]})
    assert second["crowded"] == ["AGIUSDT", "SOMIUSDT"]

    # Day three: AGI's 48 hours are up; SOMI is inside its own, but delisted, so it drops.
    instruments.pop()
    third = recorder.resolve_tiers(BASE_NS + 2 * DAY_NS, {"instruments": instruments, "tickers": days[2]})
    assert third["crowded"] == []


def test_a_crowded_name_at_exactly_the_threshold_qualifies_and_a_shallower_one_does_not(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("crowded", (Feed("book", "50"),), Universe("funding_below", threshold_bp=10.0, quote="USDT")),
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


def test_a_funding_collapse_on_the_ticker_promotes_within_the_tick_and_expires_after_sticky_hours(
    tmp_path: Path,
) -> None:
    recorder = build(
        tmp_path,
        Tier(
            "crowded",
            (Feed("book", "50"),),
            Universe("funding_below", threshold_bp=8.0, sticky_hours=2.0, quote="USDT"),
        ),
    )
    tables = {
        "instruments": [instrument("AGIUSDT"), instrument("BTCUSDT")],
        "tickers": [{"symbol": "AGIUSDT", "fundingRate": "0.0001"}],
    }
    assert recorder.resolve_tiers(BASE_NS, tables)["crowded"] == []

    # 14:00: the ticker shows -12 bp; the next tick promotes.
    recorder.live.observe("AGIUSDT", {"funding_rate": -0.0012, "mark_price": 0.42}, BASE_NS + 6 * HOUR_NS)
    assert recorder.resolve_tiers(BASE_NS + 6 * HOUR_NS + 30 * 10**9)["crowded"] == ["AGIUSDT"]

    # The rate recovers; the name stays for its two sticky hours, then goes.
    recorder.live.observe("AGIUSDT", {"funding_rate": 0.0001}, BASE_NS + 7 * HOUR_NS)
    assert recorder.resolve_tiers(BASE_NS + 7 * HOUR_NS)["crowded"] == ["AGIUSDT"]
    assert recorder.resolve_tiers(BASE_NS + 8 * HOUR_NS + 30 * 10**9)["crowded"] == []

    # A ticker for a name the instrument table does not list never promotes it.
    recorder.live.observe("GHOSTUSDT", {"funding_rate": -0.0050}, BASE_NS + 9 * HOUR_NS)
    assert recorder.resolve_tiers(BASE_NS + 9 * HOUR_NS)["crowded"] == []


def test_a_turnover_surge_is_measured_against_the_last_snapshot(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("surging", (Feed("book", "50"),), Universe("turnover_surge", ratio=3.0, sticky_hours=1.0, quote="USDT")),
    )
    tables = {
        "instruments": [instrument("HNTUSDT"), instrument("BTCUSDT")],
        "tickers": [{"symbol": "HNTUSDT", "turnover24h": "1000000"}, {"symbol": "BTCUSDT", "turnover24h": "9e9"}],
    }
    assert recorder.resolve_tiers(BASE_NS, tables)["surging"] == []

    recorder.live.observe("HNTUSDT", {"turnover_24h": 2_999_999.0}, BASE_NS + 1)
    assert recorder.resolve_tiers(BASE_NS + 2)["surging"] == []
    recorder.live.observe("HNTUSDT", {"turnover_24h": 3_000_000.0}, BASE_NS + 3)
    assert recorder.resolve_tiers(BASE_NS + 4)["surging"] == ["HNTUSDT"]

    # The next snapshot raises the baseline to the new level; the surge is over, the name lingers an hour.
    recorder.resolve_tiers(
        BASE_NS + 5,
        {"instruments": tables["instruments"], "tickers": [{"symbol": "HNTUSDT", "turnover24h": "3000000"}]},
    )
    assert recorder.resolve_tiers(BASE_NS + 6)["surging"] == ["HNTUSDT"]
    assert recorder.resolve_tiers(BASE_NS + 4 + HOUR_NS)["surging"] == []


def test_a_price_move_promotes_either_direction(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("movers", (Feed("book", "50"),), Universe("price_move", pct=0.15, sticky_hours=1.0, quote="USDT")),
    )
    recorder.tables = {
        "instruments": [instrument("UPUSDT"), instrument("DOWNUSDT"), instrument("FLATUSDT")],
        "tickers": [],
    }
    recorder.live.observe("UPUSDT", {"price_change_24h_pct": 0.151}, BASE_NS)
    recorder.live.observe("DOWNUSDT", {"price_change_24h_pct": -0.20}, BASE_NS)
    recorder.live.observe("FLATUSDT", {"price_change_24h_pct": 0.149}, BASE_NS)

    assert recorder.resolve_tiers(BASE_NS + 1)["movers"] == ["DOWNUSDT", "UPUSDT"]


# ------------------------------------------------------------------- topics


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


def test_a_shed_pair_leaves_its_topics_out_of_the_plan(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("deep", (Feed("book", "50"), Feed("trades")), Universe("symbols", symbols=("BTCUSDT",))),
        Tier("wide", (Feed("book", "1"), Feed("ticker")), Universe("listed", quote="USDT", exclude_tiers=("deep",))),
    )
    tables = {"instruments": [instrument("BTCUSDT"), instrument("ETHUSDT")], "tickers": []}
    resolved = recorder.resolve_tiers(BASE_NS, tables)

    topics, feeds = recorder.plan_topics(resolved, shed=[("wide", "book:1")])

    assert topics == {"deep": ["orderbook.50.BTCUSDT", "publicTrade.BTCUSDT"], "wide": ["tickers.ETHUSDT"]}
    assert [feed.text for feed in feeds["ETHUSDT"]] == ["ticker"]


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


# ------------------------------------------------------------------- shards


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self) -> None:
        return None


def unstarted_shard(recorder: Recorder, tier: str, topics: list[str], index: int = 0) -> Shard:
    return Shard(
        index=index,
        tier=tier,
        topics=list(topics),
        adapter=recorder.adapter,
        frames=recorder.frames,
        on_frame=recorder._on_frame,
        on_overrun=recorder._on_overrun,
        emit=recorder.emit,
    )


def test_a_shard_reads_frames_without_pure_python_utf8_validation(tmp_path: Path, monkeypatch) -> None:
    # websocket-client validates every text frame byte by byte in Python; at
    # five thousand frames a second that was half a shard's CPU and starved the
    # pong reads, and json.loads rejects a malformed frame anyway.
    recorder = build(tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))))
    shard = unstarted_shard(recorder, "deep", ["publicTrade.BTCUSDT"])
    seen: dict[str, object] = {}

    class FakeApp:
        def __init__(self, url: str, **callbacks: object) -> None:
            seen["url"] = url
            seen["callbacks"] = set(callbacks)

        def run_forever(self, **kwargs: object) -> None:
            seen["run_forever"] = kwargs

    monkeypatch.setattr(record.websocket, "WebSocketApp", FakeApp)

    shard._connect_once()

    assert seen["callbacks"] == {"on_open", "on_message", "on_error"}
    assert seen["run_forever"] == {"ping_interval": 20, "ping_timeout": 10, "skip_utf8_validation": True}
    assert shard.socket is None, "the app is dropped when its run loop returns"


def test_a_live_shard_changes_its_subscription_in_place(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))))
    shard = unstarted_shard(recorder, "deep", ["publicTrade.BTCUSDT", "publicTrade.ETHUSDT"])
    socket = FakeSocket()
    shard.socket = socket  # type: ignore[assignment]
    shard.connected = True

    added, removed = shard.update(["publicTrade.BTCUSDT", "publicTrade.AGIUSDT"])

    assert (added, removed) == (["publicTrade.AGIUSDT"], ["publicTrade.ETHUSDT"])
    assert shard.topics == ["publicTrade.BTCUSDT", "publicTrade.AGIUSDT"]
    assert [json.loads(text) for text in socket.sent] == [
        {"op": "unsubscribe", "args": ["publicTrade.ETHUSDT"]},
        {"op": "subscribe", "args": ["publicTrade.AGIUSDT"]},
    ]
    # Nothing to change sends nothing.
    assert shard.update(["publicTrade.BTCUSDT", "publicTrade.AGIUSDT"]) == ([], [])
    assert len(socket.sent) == 2


def test_re_anchoring_drops_and_retakes_only_the_book_topics_in_chunks(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("deep", DEEP_FEEDS, Universe("symbols", symbols=("BTCUSDT",))))
    books = [f"orderbook.50.SYM{index}USDT" for index in range(record.REANCHOR_CHUNK + 3)]
    topics = [*books[:4], "publicTrade.BTCUSDT", "tickers.BTCUSDT", *books[4:]]
    shard = unstarted_shard(recorder, "deep", topics)
    socket = FakeSocket()
    shard.socket = socket  # type: ignore[assignment]
    shard.connected = True

    assert shard.reanchor_books("2027-01-15T08", limit=len(books)) == len(books)

    # Each chunk is unsubscribed and resubscribed together, so a symbol is
    # gone for one round trip rather than for the whole shard's pass.
    assert [json.loads(text) for text in socket.sent] == [
        {"op": "unsubscribe", "args": books[: record.REANCHOR_CHUNK]},
        {"op": "subscribe", "args": books[: record.REANCHOR_CHUNK]},
        {"op": "unsubscribe", "args": books[record.REANCHOR_CHUNK :]},
        {"op": "subscribe", "args": books[record.REANCHOR_CHUNK :]},
    ]
    # The subscription itself is unchanged: this is a re-anchor, not a replan.
    assert shard.topics == topics
    assert shard.reanchored("2027-01-15T08")
    assert not shard.reanchored("2027-01-15T09")


def test_a_shard_with_no_books_and_a_disconnected_one_re_anchor_nothing(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("wide", WIDE_FEEDS, Universe("symbols", symbols=("BTCUSDT",))))
    ticker_only = unstarted_shard(recorder, "wide", ["tickers.BTCUSDT"])
    ticker_only.socket = FakeSocket()  # type: ignore[assignment]
    ticker_only.connected = True
    assert ticker_only.reanchor_books("2027-01-15T08", limit=40) == 0
    assert ticker_only.reanchored("2027-01-15T08"), "nothing to anchor is anchored"

    offline = unstarted_shard(recorder, "wide", ["orderbook.50.BTCUSDT"])
    assert offline.reanchor_books("2027-01-15T08", limit=40) == 0
    assert not offline.reanchored("2027-01-15T08"), "it re-anchors by connecting"
    assert offline.reanchors == 0


def test_the_hourly_pass_is_bounded_per_tick_and_resumes_where_it_stopped(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("deep", DEEP_FEEDS, Universe("symbols", symbols=("BTCUSDT",))))
    per_shard = record.REANCHOR_TOPICS_PER_TICK
    shards = []
    for index in range(2):
        topics = [f"orderbook.50.S{index}N{n}USDT" for n in range(per_shard)]
        shard = unstarted_shard(recorder, "deep", topics, index=index)
        shard.socket = FakeSocket()  # type: ignore[assignment]
        shard.connected = True
        shards.append(shard)
    recorder.tier_shards["deep"] = shards

    hour = BASE_NS
    recorder._reanchor_books(hour)
    assert shards[0].reanchor_cursor == per_shard, "the tick's whole budget went to the first shard"
    assert shards[1].reanchor_cursor == 0, "bounded per tick, not all at once"

    recorder._reanchor_books(hour + 30 * 10**9)
    assert [s.reanchor_cursor for s in shards] == [per_shard, per_shard]
    before = [len(s.socket.sent) for s in shards]  # type: ignore[union-attr]
    recorder._reanchor_books(hour + 60 * 10**9)
    assert [len(s.socket.sent) for s in shards] == before, "once an hour, not once a tick"  # type: ignore[union-attr]

    # The next UTC hour makes every shard due again.
    recorder._reanchor_books(hour + HOUR_NS)
    assert shards[0].reanchor_cursor == per_shard and shards[0].reanchor_hour.endswith("T09")


def test_a_shard_that_just_connected_is_already_anchored_for_its_hour(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("deep", DEEP_FEEDS, Universe("symbols", symbols=("BTCUSDT",))))
    shard = unstarted_shard(recorder, "deep", ["orderbook.50.BTCUSDT"])
    day, hour = record.utc_day_hour(time.time_ns())
    shard.mark_anchored(f"{day}T{hour}")
    assert shard.reanchored(f"{day}T{hour}"), "connecting subscribed, and subscribing anchored"


def test_re_anchoring_is_off_when_the_config_says_so(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("deep", DEEP_FEEDS, Universe("symbols", symbols=("BTCUSDT",))), reanchor=False)
    shard = unstarted_shard(recorder, "deep", ["orderbook.50.BTCUSDT"])
    shard.socket = FakeSocket()  # type: ignore[assignment]
    shard.connected = True
    recorder.tier_shards["deep"] = [shard]

    recorder._reanchor_books(BASE_NS)

    assert shard.reanchors == 0


def test_a_shard_that_is_not_connected_only_records_the_new_list(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))))
    shard = unstarted_shard(recorder, "deep", ["publicTrade.BTCUSDT"])

    added, removed = shard.update(["publicTrade.ETHUSDT"])

    assert (added, removed) == (["publicTrade.ETHUSDT"], ["publicTrade.BTCUSDT"])
    assert shard.topics == ["publicTrade.ETHUSDT"]


def test_reconciling_a_tier_keeps_placement_fills_room_and_closes_empty_shards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = build(
        tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))), per_connection=2
    )
    created: list[Shard] = []

    def quiet_shard(tier: str, topics: list[str]) -> Shard:
        shard = unstarted_shard(recorder, tier, topics, index=len(created))
        created.append(shard)
        return shard

    monkeypatch.setattr(recorder, "_new_shard", quiet_shard)

    recorder._reconcile_tier("deep", ["t1", "t2", "t3"])
    assert [shard.topics for shard in recorder.tier_shards["deep"]] == [["t1", "t2"], ["t3"]]

    # t2 leaves, t4 arrives: t4 takes t2's room; nobody reconnects.
    recorder._reconcile_tier("deep", ["t1", "t3", "t4"])
    assert [shard.topics for shard in recorder.tier_shards["deep"]] == [["t1", "t4"], ["t3"]]
    assert len(created) == 2

    # Only t3 is left: the first shard empties and is closed; the second keeps t3 on its own connection.
    recorder._reconcile_tier("deep", ["t3"])
    assert [shard.topics for shard in recorder.tier_shards["deep"]] == [["t3"]]
    assert created[0].stop.is_set() and not created[1].stop.is_set()

    # More than the room left opens a new shard for the overflow only.
    recorder._reconcile_tier("deep", ["t3", "t5", "t6", "t7"])
    assert [shard.topics for shard in recorder.tier_shards["deep"]] == [["t3", "t5"], ["t6", "t7"]]
    assert len(created) == 3


def test_shard_topics_never_mixes_connection_groups() -> None:
    group = lambda topic: "market" if topic.startswith("m") else "public"  # noqa: E731
    assert shard_topics(["p1", "m1", "p2", "m2", "p3"], 2, group) == [["p1", "p2"], ["p3"], ["m1", "m2"]]
    assert shard_topics(["p1", "m1", "p2"], 2) == [["p1", "m1"], ["p2"]]


def test_binance_shards_carry_one_path_each_and_live_adds_stay_on_their_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = CaptureConfig(
        venue=VenueSettings("binance", "usdm"),
        storage=StorageSettings(root=tmp_path, queue_frames=16, status_interval_seconds=30.0),
        tiers=(
            Tier(
                "core",
                (Feed("book", "1"), Feed("trades"), Feed("ticker")),
                Universe("symbols", symbols=("BTCUSDT", "ETHUSDT")),
            ),
        ),
        topics_per_connection=3,
    )
    recorder = Recorder(config)
    created: list[Shard] = []

    def quiet_shard(tier: str, topics: list[str]) -> Shard:
        shard = unstarted_shard(recorder, tier, topics, index=len(created))
        created.append(shard)
        return shard

    monkeypatch.setattr(recorder, "_new_shard", quiet_shard)
    topics, _ = recorder.plan_topics({"core": ["BTCUSDT"]})
    recorder._reconcile_tier("core", topics["core"])
    plans = [shard.topics for shard in recorder.tier_shards["core"]]
    assert plans == [["btcusdt@bookTicker"], ["btcusdt@aggTrade", "btcusdt@markPrice@1s", "btcusdt@ticker"]]
    for shard in recorder.tier_shards["core"]:
        recorder.adapter.connection_url(shard.topics)  # one path each, or this raises

    # ETH arrives live: its top of book joins the public shard, its market streams open a new market shard.
    topics, _ = recorder.plan_topics({"core": ["BTCUSDT", "ETHUSDT"]})
    recorder._reconcile_tier("core", topics["core"])
    plans = [shard.topics for shard in recorder.tier_shards["core"]]
    assert plans == [
        ["btcusdt@bookTicker", "ethusdt@bookTicker"],
        ["btcusdt@aggTrade", "btcusdt@markPrice@1s", "btcusdt@ticker"],
        ["ethusdt@aggTrade", "ethusdt@markPrice@1s", "ethusdt@ticker"],
    ]
    for shard in recorder.tier_shards["core"]:
        recorder.adapter.connection_url(shard.topics)


# ---------------------------------------------------------------- refreshes


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


@needs_zstd
def test_a_replan_reconciles_only_the_tiers_whose_topics_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = build(
        tmp_path,
        Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))),
        Tier("crowded", (Feed("book", "50"),), Universe("funding_below", threshold_bp=8.0, quote="USDT")),
    )
    tables = {"instruments": [instrument("BTCUSDT"), instrument("AGIUSDT")], "tickers": []}
    recorder.adapter.fetch_tables = lambda: tables  # type: ignore[method-assign]
    reconciled: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(recorder, "_reconcile_tier", lambda name, topics: reconciled.append((name, list(topics))))

    recorder._refresh(BASE_NS, restart=True)
    assert reconciled == [("deep", ["publicTrade.BTCUSDT"])]

    recorder.live.observe("AGIUSDT", {"funding_rate": -0.0011}, BASE_NS + 1)
    changed = recorder._replan(BASE_NS + 2, apply=True)
    assert changed == ["crowded"]
    assert reconciled[-1] == ("crowded", ["orderbook.50.AGIUSDT"])


# ---------------------------------------------------------------- the bytes


def test_the_byte_meter_keeps_a_day_per_key_and_the_window_it_has_seen() -> None:
    meter = ByteMeter(BASE_NS)
    meter.add("all", 100, BASE_NS)
    meter.add("all", 50, BASE_NS + 30 * 10**9)
    meter.add("tier:wide", 50, BASE_NS + 30 * 10**9)
    assert meter.last_day("all", BASE_NS + 60 * 10**9) == 150
    assert meter.last_day("tier:wide", BASE_NS + 60 * 10**9) == 50

    # A day later the old minutes fall out of the window as new bytes arrive.
    meter.add("all", 7, BASE_NS + DAY_NS + 60 * 10**9)
    assert meter.totals["all"] == 157
    assert meter.last_day("all", BASE_NS + DAY_NS + 60 * 10**9) == 7
    assert meter.last_day("missing", BASE_NS) == 0
    assert meter.window_ns(BASE_NS + 60 * 10**9) == 60 * 10**9
    assert meter.window_ns(BASE_NS + 3 * DAY_NS) == DAY_NS
    assert meter.keys("tier:") == ["tier:wide"]


def test_frames_are_metered_by_tier_and_feed_class(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))))
    trade = {"kind": "public_trade", "symbol": "BTCUSDT"}
    book = {"kind": "orderbook_delta", "symbol": "BTCUSDT", "depth": 50}

    recorder._meter("deep", [trade], 120, BASE_NS)
    recorder._meter("deep", [book], 800, BASE_NS)
    recorder._meter("wide", [], 40, BASE_NS)

    assert Recorder.feed_class([book]) == "book:50"
    assert Recorder.feed_class([{"kind": "kline", "interval": "1m"}]) == "kline:1m"
    status = recorder.bytes_status(BASE_NS + 1)
    assert status["received_total"] == 960
    assert status["by_tier_24h"] == {"deep": 920, "wide": 40}
    assert status["by_feed_24h"] == {"deep:book:50": 800, "deep:trades": 120, "wide:control": 40}


def test_a_frame_the_disk_gate_drops_still_counts_against_the_inbound_allowance(tmp_path: Path) -> None:
    """`budget.monthly_gb` is an inbound allowance and `budget.shed` gives up
    subscriptions to stay under it, so the meter must count what the venue
    sent. Metering behind `disk_blocked` measures what the disk kept instead,
    which collapses the projection precisely while a recorder is discarding
    the most and can restore shed feeds — more inbound — during a storage
    incident."""

    recorder = build(tmp_path, Tier("wide", (Feed("ticker"),), Universe("listed", quote="USDT")))
    frame = json.dumps(
        {
            "topic": "tickers.AGIUSDT",
            "type": "delta",
            "ts": 1_800_000_000_000,
            "data": {"symbol": "AGIUSDT", "fundingRate": "-0.0015"},
        }
    )
    # What a crossing leaves behind: the gate is shut and the venue keeps sending.
    recorder.disk_blocked = True
    recorder.frames.put(("frame", frame, BASE_NS, "wide"))
    recorder.frames.put(None)

    recorder._write_loop()

    assert recorder.written_rows == 0
    assert recorder.disk_dropped_frames == 1
    assert recorder.meter.last_day("all", BASE_NS + 1) == len(frame)
    assert recorder.meter.last_day("tier:wide", BASE_NS + 1) == len(frame)
    # The per-feed split needs the normalized rows, which the gate never produced.
    assert recorder.meter.keys("feed:") == []


def test_a_blocked_recorder_projects_the_bytes_it_discards_not_the_bytes_it_keeps(tmp_path: Path) -> None:
    """The number the budget acts on, end to end: one hour of frames the disk
    refused must project the same allowance as one hour it wrote."""

    settings = BudgetSettings(monthly_gb=400.0)
    recorder = build(
        tmp_path, Tier("wide", (Feed("ticker"),), Universe("listed", quote="USDT")), budget=settings
    )
    frame = json.dumps(
        {
            "topic": "tickers.AGIUSDT",
            "type": "delta",
            "ts": 1_800_000_000_000,
            "data": {"symbol": "AGIUSDT", "fundingRate": "-0.0015"},
        }
    )
    for at_ns in (BASE_NS, BASE_NS + HOUR_NS):
        recorder.frames.put(("frame", frame, at_ns, "wide"))
    recorder.frames.put(None)
    recorder.disk_blocked = True

    recorder._write_loop()

    recorder.budget.step(BASE_NS + HOUR_NS)
    blocked = recorder.budget.projected_gb
    assert blocked is not None
    # The same two frames, metered by the unblocked path over the same window.
    reference = ByteMeter(recorder.meter.started_ns)
    reference.add("all", len(frame), BASE_NS)
    reference.add("all", len(frame), BASE_NS + HOUR_NS)
    assert blocked == pytest.approx(
        BudgetController(settings, reference).projection_gb(BASE_NS + HOUR_NS)
    )
    assert recorder.disk_dropped_frames == 2


def _metered_hour(meter: ByteMeter, at_ns: int, wide_book: int, movers_book: int, rest: int) -> None:
    meter.add("all", wide_book + movers_book + rest, at_ns)
    meter.add("feed:wide:book:1", wide_book, at_ns)
    meter.add("feed:movers:book:50", movers_book, at_ns)
    meter.add("feed:core:trades", rest, at_ns)


def test_the_budget_sheds_what_the_projection_needs_and_counts_shed_pairs_out_of_it() -> None:
    meter = ByteMeter(BASE_NS)
    settings = BudgetSettings(
        monthly_gb=400.0, shed=(("wide", "book:1"), ("movers", "book:50")), restore_below=0.8, act_every_minutes=60
    )
    budget = BudgetController(settings, meter)

    # Less than an hour of history: no projection, no action.
    _metered_hour(meter, BASE_NS + 60 * 10**9, 500_000_000, 300_000_000, 200_000_000)
    assert budget.step(BASE_NS + 60 * 10**9) is False
    assert budget.projected_gb is None

    # One hour in, 1 GB received: 720 GB a month against 400. The first pair
    # alone carries 360 of it, so the first pair is all that goes.
    assert budget.step(BASE_NS + HOUR_NS) is True
    assert budget.shed_active == [("wide", "book:1")]
    assert budget.shed_gb[("wide", "book:1")] == pytest.approx(360.0)
    assert budget.over is True

    # A second hour of traffic without the shed pair. Its first-hour bytes
    # still sit in the window; the projection leaves them out and reads the
    # 360 GB/month that is still subscribed — under the allowance, so nothing
    # more is shed, and the shed pair (360 on top of 360) does not fit under
    # the restore line.
    _metered_hour(meter, BASE_NS + HOUR_NS + 60 * 10**9, 0, 300_000_000, 200_000_000)
    assert budget.step(BASE_NS + 2 * HOUR_NS) is False
    assert budget.projected_gb == pytest.approx(360.0)
    assert budget.over is False
    assert budget.shed_active == [("wide", "book:1")]
    status = budget.status()
    assert status["shed"] == ["wide:book:1"]
    assert status["shed_gb_month"] == {"wide:book:1": 360.0}
    assert status["shed_order"] == ["wide:book:1", "movers:book:50"]


def test_the_budget_sheds_several_pairs_in_one_action_and_says_when_the_list_is_not_enough(
    caplog: pytest.LogCaptureFixture,
) -> None:
    meter = ByteMeter(BASE_NS)
    settings = BudgetSettings(
        monthly_gb=100.0, shed=(("wide", "book:1"), ("movers", "book:50")), restore_below=0.8, act_every_minutes=60
    )
    budget = BudgetController(settings, meter)
    _metered_hour(meter, BASE_NS + 60 * 10**9, 500_000_000, 300_000_000, 200_000_000)

    # 720 against 100: both pairs go at once (720 - 360 - 216 = 144), and
    # what is left is still over, which is said rather than left to status.json.
    with caplog.at_level(logging.WARNING):
        assert budget.step(BASE_NS + HOUR_NS) is True
    assert budget.shed_active == [("wide", "book:1"), ("movers", "book:50")]
    assert [record.getMessage() for record in caplog.records][-1].startswith(
        "over budget with every sheddable feed shed: 144 GB/month projected against 100 allowed"
    )

    # An hour on, the rest still runs at 144 GB/month with nothing left to
    # shed: no change, said again.
    _metered_hour(meter, BASE_NS + HOUR_NS + 60 * 10**9, 0, 0, 200_000_000)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert budget.step(BASE_NS + 2 * HOUR_NS) is False
    assert any("every sheddable feed shed" in record.getMessage() for record in caplog.records)
    # Between actions, quiet.
    caplog.clear()
    assert budget.step(BASE_NS + 2 * HOUR_NS + 10 * 60 * 10**9) is False
    assert caplog.records == []


def test_a_shed_pair_is_restored_only_when_its_own_month_fits_under_the_restore_line() -> None:
    meter = ByteMeter(BASE_NS)
    settings = BudgetSettings(
        monthly_gb=400.0, shed=(("wide", "book:1"), ("movers", "book:50")), restore_below=0.8, act_every_minutes=60
    )
    budget = BudgetController(settings, meter)
    _metered_hour(meter, BASE_NS + 60 * 10**9, 500_000_000, 300_000_000, 200_000_000)
    budget.settings = BudgetSettings(monthly_gb=100.0, shed=settings.shed, restore_below=0.8, act_every_minutes=60)
    assert budget.step(BASE_NS + HOUR_NS) is True
    assert budget.shed_active == [("wide", "book:1"), ("movers", "book:50")]
    budget.settings = settings

    # A day later the meter shows almost nothing. The last pair shed carried
    # 216 GB/month: under 320, restored. The first carried 360: over 320, it
    # stays shed however quiet the tape is, because restoring it would put
    # the recorder straight back over.
    late = BASE_NS + 2 * DAY_NS
    meter.add("all", 1, late)
    assert budget.step(late) is True
    assert budget.shed_active == [("wide", "book:1")]
    assert budget.step(late + HOUR_NS) is False
    assert budget.shed_active == [("wide", "book:1")]
    assert budget.over is False


def test_without_a_budget_the_controller_only_projects() -> None:
    meter = ByteMeter(BASE_NS)
    budget = BudgetController(BudgetSettings(), meter)
    meter.add("all", 5 * 10**8, BASE_NS + HOUR_NS)

    assert budget.step(BASE_NS + HOUR_NS) is False
    assert budget.projected_gb == pytest.approx(360.0)
    assert budget.over is False


# ------------------------------------------------------------------- status


def test_the_status_file_carries_what_the_host_watchdog_reads(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("deep", (Feed("book", "50"), Feed("trades")), Universe("symbols", symbols=("BTCUSDT",))),
        Tier("wide", (Feed("ticker"),), Universe("listed", quote="USDT", exclude_tiers=("deep",))),
        budget=BudgetSettings(monthly_gb=1300.0, shed=(("wide", "ticker"),)),
    )
    recorder.tier_symbols = {"deep": ["BTCUSDT"], "wide": ["AGIUSDT"]}
    recorder.tier_topics = {"deep": ["orderbook.50.BTCUSDT", "publicTrade.BTCUSDT"], "wide": ["tickers.AGIUSDT"]}
    recorder.tier_shards["deep"] = [unstarted_shard(recorder, "deep", recorder.tier_topics["deep"])]
    recorder._on_frame(BASE_NS)
    recorder._on_overrun()
    recorder.disk_dropped_frames = 3
    recorder.disk_blocked = True
    recorder.budget.shed_active = [("wide", "ticker")]

    recorder._write_status()
    payload = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))

    assert payload["kind"] == "forward_capture_status"
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["pid"] == os.getpid()
    assert payload["venue"] == "bybit" and payload["market"] == "linear"
    assert payload["config"] == "deploy/capture/bybit-linear.toml"
    assert payload["last_receive_ns"] == BASE_NS
    # The watchdog reads silence and socket loss against this, so a recorder
    # seconds old does not page as a dead venue.
    assert payload["started_at_ns"] == recorder.started_at_ns > 0
    assert payload["received_frames"] == 1
    assert payload["dropped_frames"] == 1
    assert payload["disk_dropped_frames"] == 3
    assert payload["disk_blocked"] is True
    assert payload["queue_capacity"] == 16
    assert payload["status_interval_seconds"] == 30.0
    assert payload["shards"] == [
        {
            "index": 0,
            "tier": "deep",
            "topics": 2,
            "connected": False,
            "reconnects": 0,
            "reanchors": 0,
            "last_message_ns": 0,
        }
    ]
    assert payload["tiers"] == [
        {
            "name": "deep",
            "universe": "symbols",
            "live": False,
            "feeds": ["book:50", "trades"],
            "shed": [],
            "symbols": 1,
            "topics": 2,
            "names": ["BTCUSDT"],
        },
        {
            "name": "wide",
            "universe": "listed",
            "live": False,
            "feeds": ["ticker"],
            "shed": ["ticker"],
            "symbols": 1,
            "topics": 1,
            "names": ["AGIUSDT"],
        },
    ]
    assert payload["budget"]["monthly_gb"] == 1300.0
    assert payload["budget"]["shed"] == ["wide:ticker"]
    assert payload["budget"]["over"] is False
    assert set(payload["bytes"]) == {"received_total", "received_24h", "window_seconds", "by_tier_24h", "by_feed_24h"}


def test_the_maintenance_tick_writes_the_heartbeat_without_walking_the_tape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`status.json` is this unit's heartbeat and the watchdog's silence
    reading both. Retention walks tens of thousands of files on the host, so
    the tick that writes the heartbeat may not wait on a pass; the pruner
    thread owns it."""

    recorder = build(tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))))

    def refuse() -> dict[str, list[dict[str, Any]]]:
        raise RuntimeError("venue refused")

    recorder.adapter.fetch_tables = refuse  # type: ignore[method-assign]
    monkeypatch.setattr(recorder, "_reconcile_tier", lambda name, topics: None)
    # The free floor is the developer box's, not the host's: pin it so the tick
    # reads `disk_blocked` from the code under test and not from this disk.
    recorder.retention.min_free_bytes = 1
    directory = tmp_path / "2027-01-15" / "10" / "BTCUSDT"
    directory.mkdir(parents=True)
    expired = directory / "segment-000000.jsonl.zst"
    expired.write_bytes(b"long past retention")
    os.utime(expired, (1_000_000, 1_000_000))

    recorder._maintenance()

    assert (tmp_path / "status.json").exists()
    assert recorder.disk_blocked is False
    assert expired.exists()

    recorder._retention_pass()

    assert not expired.exists()


def test_a_disk_under_the_free_floor_prunes_now_instead_of_waiting_out_the_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`min_free_disk_gb` is free space on the whole filesystem, so anything
    sharing it can put the recorder under the floor, and under the floor every
    frame is counted and thrown away. The tick that sees it must start the only
    thing that frees room rather than leave the pruner asleep for the rest of
    `RETENTION_INTERVAL_SECONDS`."""

    recorder = build(tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))))

    def refuse() -> dict[str, list[dict[str, Any]]]:
        raise RuntimeError("venue refused")

    recorder.adapter.fetch_tables = refuse  # type: ignore[method-assign]
    monkeypatch.setattr(recorder, "_reconcile_tier", lambda name, topics: None)
    # Long enough that a pass inside it can only come from the wake, never the clock.
    monkeypatch.setattr(record, "RETENTION_INTERVAL_SECONDS", 3600.0)

    started = threading.Event()
    woken = threading.Event()
    passes: list[int] = []

    def counted(now: float | None = None, *, free_credit: int = 0) -> list[Path]:
        passes.append(len(passes))
        (started if len(passes) == 1 else woken).set()
        return []

    monkeypatch.setattr(recorder.retention, "prune", counted)
    # A daemon thread, so a failed assertion below reports itself rather than
    # the teardown that a missing wake would trip over.
    pruner = threading.Thread(target=recorder._retention_loop, name="test-retention", daemon=True)
    pruner.start()
    assert started.wait(5.0), "the pruner never made its first pass"

    # A writable disk leaves the pruner on its routine interval.
    recorder.retention.min_free_bytes = 1
    recorder._maintenance()
    assert recorder.disk_blocked is False
    assert not woken.wait(0.5)

    # Now nothing on this filesystem is enough.
    recorder.retention.min_free_bytes = 1 << 62
    recorder._maintenance()
    assert recorder.disk_blocked is True
    assert woken.wait(5.0), "the pruner slept out its interval while the disk was blocked"

    # The level, not only the crossing: a burst ends on the pass that finds no
    # deficit left and that leaves the gate shut, so the still-blocked tick is
    # where the next pass has to come from.
    woken.clear()
    recorder._maintenance()
    assert recorder.disk_blocked is True
    assert woken.wait(5.0), "a blocked tick left the pruner asleep"

    # A stop wakes the pruner: shutdown never waits out a retention interval.
    recorder.stop.set()
    recorder.prune_now.set()
    pruner.join(5.0)
    assert not pruner.is_alive()


def test_a_pass_that_frees_room_opens_the_writer_gate_instead_of_the_next_status_tick(
    tmp_path: Path,
) -> None:
    """`disk_blocked` gates every frame in `_write_loop`, and only a retention
    pass frees room, so the pass that frees it is what must open the gate.
    Leaving that to `_maintenance` throws away a whole
    `status_interval_seconds` of tape onto a disk that already has space."""

    recorder = build(tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))))
    directory = tmp_path / "2027-01-15" / "10" / "BTCUSDT"

    def expired(name: str) -> Path:
        # A pass that empties an hour removes the directory too, so each file
        # remakes its own.
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(b"long past retention")
        os.utime(path, (1_000_000, 1_000_000))
        return path

    first = expired("segment-000000.jsonl.zst")
    # What a crossing leaves behind: the free-space tick, or the append that
    # failed first, has closed the gate.
    recorder.disk_blocked = True

    # A pass that deletes but leaves the disk under the floor changes nothing:
    # the floor is the developer box's, not the host's, so pin it.
    recorder.retention.min_free_bytes = 1 << 62
    recorder._retention_pass()

    assert not first.exists()
    assert recorder.disk_blocked is True

    # Room is back, so the frame after this pass is tape, not a dropped count.
    expired("segment-000001.jsonl.zst")
    recorder.retention.min_free_bytes = 1
    recorder._retention_pass()

    assert recorder.disk_blocked is False

    row = {"kind": "ticker", "symbol": "BTCUSDT", "values": {}, "local_receive_ts_ns": BASE_NS}
    recorder.frames.put(("rows", [row], BASE_NS, "deep"))
    recorder.frames.put(None)
    recorder._write_loop()

    assert recorder.written_rows == 1
    assert recorder.disk_dropped_frames == 0


def test_a_pass_that_deletes_and_leaves_the_gate_shut_passes_again_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`prune` stops on the free space it counts from the sizes it unlinked;
    `writable()` reads the kernel's. While the filesystem is still releasing
    blocks the two disagree, so a pass can delete, believe it reached the
    floor, and leave the gate shut. Nothing else wakes the pruner then —
    `_maintenance` arms `prune_now` on the crossing only, and `_write_loop`
    never reaches an append to fail on — so the pass that fell short must be
    what runs the next one. Sleeping out `RETENTION_INTERVAL_SECONDS` instead
    throws away the tape the pass already made room for."""

    recorder = build(tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))))
    # Long enough that a second pass inside it can only come from the first.
    monkeypatch.setattr(record, "RETENTION_INTERVAL_SECONDS", 3600.0)
    # What a crossing leaves behind, and the developer box's own free space
    # pinned under the floor so `writable()` reads the code under test.
    recorder.disk_blocked = True
    recorder.retention.min_free_bytes = 1 << 62

    passes: list[int] = []
    third = threading.Event()

    def short(now: float | None = None, *, free_credit: int = 0) -> list[Path]:
        passes.append(len(passes))
        # Two passes the kernel does not yet agree with, then one it does.
        if len(passes) == 3:
            recorder.retention.min_free_bytes = 1
            third.set()
        return [Path(f"2027-01-15/10/BTCUSDT/segment-{len(passes):06d}.jsonl.zst")]

    monkeypatch.setattr(recorder.retention, "prune", short)
    # A daemon thread, so a failed assertion below reports itself rather than
    # the teardown that a missing pass would trip over.
    pruner = threading.Thread(target=recorder._retention_loop, name="test-retention", daemon=True)
    pruner.start()

    assert third.wait(5.0), f"the pruner slept with the gate shut after {len(passes)} pass(es)"
    deadline = time.monotonic() + 5.0
    while recorder.disk_blocked and time.monotonic() < deadline:
        time.sleep(0.01)
    assert recorder.disk_blocked is False, "the pass that reached the floor did not open the gate"

    # The retry ends where it must: a pass that deletes nothing does not walk
    # the tape again, so a full disk holding no tape is not spun on.
    assert recorder._retention_pass() is False
    monkeypatch.setattr(recorder.retention, "prune", lambda now=None, *, free_credit=0: [])
    recorder.disk_blocked = True
    recorder.retention.min_free_bytes = 1 << 62
    assert recorder._retention_pass() is False

    recorder.stop.set()
    recorder.prune_now.set()
    pruner.join(5.0)
    assert not pruner.is_alive()


def test_a_burst_of_owed_passes_deletes_the_deficit_once_not_the_whole_tape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successor is owed because the kernel's free space disagreed with what
    the pass unlinked, and the retries run back to back with nothing between
    them. So a successor that re-reads that same number derives the whole
    deficit again and deletes it again, pass after pass, until the tape has no
    file left: a floor held by something other than tape costs every hour of
    history the recorder holds. Each pass must credit what the burst already
    unlinked and stop where the first one did."""

    recorder = build(tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))))
    # Long enough that every pass inside the burst comes from the burst itself.
    monkeypatch.setattr(record, "RETENTION_INTERVAL_SECONDS", 3600.0)
    recorder.retention.retention_days = 36_500
    recorder.retention.max_bytes = 10**12
    recorder.retention.min_free_bytes = 400
    directory = tmp_path / "2027-01-15" / "10" / "BTCUSDT"
    directory.mkdir(parents=True)
    for index in range(20):
        (directory / f"segment-{index:06d}.jsonl.zst").write_bytes(b"x" * 100)

    # A filesystem that has not released a single unlinked block: free space
    # reads the same under the floor however much the burst deletes, so every
    # pass is owed a successor and `writable()` never opens the gate.
    monkeypatch.setattr(
        "market_tape.storage.shutil.disk_usage",
        lambda path: SimpleNamespace(total=10_000, used=9_800, free=200),
    )
    # What a crossing leaves behind.
    recorder.disk_blocked = True

    real_prune = recorder.retention.prune
    passes: list[int] = []
    settled = threading.Event()

    def counted(now: float | None = None, *, free_credit: int = 0) -> list[Path]:
        passes.append(len(passes))
        deleted = real_prune(now, free_credit=free_credit)
        if not deleted:
            settled.set()
        return deleted

    monkeypatch.setattr(recorder.retention, "prune", counted)
    # A daemon thread, so a failed assertion below reports itself rather than
    # the teardown that a runaway burst would wait on.
    pruner = threading.Thread(target=recorder._retention_loop, name="test-retention", daemon=True)
    pruner.start()

    assert settled.wait(5.0), f"the burst never ended after {len(passes)} pass(es)"
    assert len(passes) == 2, "the successor deleted a second deficit instead of crediting the first"
    assert len(list(directory.glob("*.zst"))) == 17
    assert recorder.disk_blocked is True

    recorder.stop.set()
    recorder.prune_now.set()
    pruner.join(5.0)
    assert not pruner.is_alive()


def test_a_blocked_tick_runs_the_pass_the_credited_burst_stopped_short_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credited burst ends on the pass that finds no deficit left, and it
    ends with the gate still shut whenever the kernel released the unlinked
    blocks and another writer on the filesystem took them — which is what two
    recorders sharing one disk do to each other. The tape still holds hours
    nobody needs and every frame is being counted and dropped, so the next
    blocked status tick is what must run the next pass. Arming `prune_now` on
    the crossing alone leaves the pruner asleep for a whole
    `RETENTION_INTERVAL_SECONDS` there."""

    recorder = build(tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))))

    def refuse() -> dict[str, list[dict[str, Any]]]:
        raise RuntimeError("venue refused")

    recorder.adapter.fetch_tables = refuse  # type: ignore[method-assign]
    monkeypatch.setattr(recorder, "_reconcile_tier", lambda name, topics: None)
    # Long enough that a pass inside it can only come from a wake, never the clock.
    monkeypatch.setattr(record, "RETENTION_INTERVAL_SECONDS", 3600.0)
    recorder.retention.retention_days = 36_500
    recorder.retention.max_bytes = 10**12
    recorder.retention.min_free_bytes = 400
    directory = tmp_path / "2027-01-15" / "10" / "BTCUSDT"
    directory.mkdir(parents=True)
    for index in range(20):
        (directory / f"segment-{index:06d}.jsonl.zst").write_bytes(b"x" * 100)

    # A filesystem whose free space never moves however much the burst
    # unlinks: the neighbour recorder takes each freed block as it is
    # released. The credit is then an overstatement, so the successor finds no
    # deficit and the burst stops with the disk still under the floor.
    monkeypatch.setattr(
        "market_tape.storage.shutil.disk_usage",
        lambda path: SimpleNamespace(total=10_000, used=9_800, free=200),
    )
    # What a crossing leaves behind.
    recorder.disk_blocked = True

    real_prune = recorder.retention.prune
    passes: list[int] = []
    settled = threading.Event()

    def counted(now: float | None = None, *, free_credit: int = 0) -> list[Path]:
        passes.append(len(passes))
        deleted = real_prune(now, free_credit=free_credit)
        if not deleted:
            settled.set()
        return deleted

    monkeypatch.setattr(recorder.retention, "prune", counted)
    # A daemon thread, so a failed assertion below reports itself rather than
    # the teardown that a missing pass would wait on.
    pruner = threading.Thread(target=recorder._retention_loop, name="test-retention", daemon=True)
    pruner.start()

    assert settled.wait(5.0), f"the burst never ended after {len(passes)} pass(es)"
    assert len(passes) == 2
    assert len(list(directory.glob("*.zst"))) == 17
    assert recorder.disk_blocked is True

    settled.clear()
    recorder._maintenance()

    assert settled.wait(5.0), "a blocked tick left the pruner asleep on a tape it could still trim"
    assert len(passes) == 4
    assert len(list(directory.glob("*.zst"))) == 14

    recorder.stop.set()
    recorder.prune_now.set()
    pruner.join(5.0)
    assert not pruner.is_alive()


def test_a_retention_pass_that_cannot_delete_leaves_the_pruner_thread_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = build(tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))))

    def refuse(now: float | None = None, *, free_credit: int = 0) -> list[Path]:
        raise OSError("read-only file system")

    monkeypatch.setattr(recorder.retention, "prune", refuse)
    with caplog.at_level(logging.ERROR):
        recorder._retention_pass()

    assert "tape retention pass failed" in caplog.text


def test_a_wide_tier_lists_its_count_and_not_every_name(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("wide", (Feed("ticker"),), Universe("listed", quote="USDT")))
    recorder.tier_symbols["wide"] = [f"SYM{index:03d}USDT" for index in range(65)]

    status = recorder.tier_status(recorder.config.tiers[0])

    assert status["symbols"] == 65
    assert "names" not in status


def test_a_row_handed_in_by_a_side_lane_reaches_the_writer_queue(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))))

    recorder.emit({"kind": "ticker", "symbol": "BTCUSDT", "local_receive_ts_ns": BASE_NS})

    kind, payload, received_ns, tier = recorder.frames.get_nowait()
    assert kind == "rows"
    assert payload == [{"kind": "ticker", "symbol": "BTCUSDT", "local_receive_ts_ns": BASE_NS}]
    assert received_ns == BASE_NS
    assert tier == "lanes"
    assert recorder.received_frames == 1
    assert recorder.last_receive_ns == BASE_NS


def test_a_failed_append_blocks_the_disk_and_asks_for_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The append is the first detector of a full disk — it fails milliseconds
    in, where the free-space tick is a status interval behind — so it is also
    what starts the pass that frees room."""

    recorder = build(tmp_path, Tier("wide", (Feed("ticker"),), Universe("listed", quote="USDT")))

    def no_space(row: Any) -> list[Any]:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(recorder.writer, "append", no_space)
    frame = json.dumps(
        {
            "topic": "tickers.AGIUSDT",
            "type": "delta",
            "ts": 1_800_000_000_000,
            "data": {"symbol": "AGIUSDT", "fundingRate": "-0.0015"},
        }
    )
    recorder.frames.put(("frame", frame, BASE_NS, "wide"))
    recorder.frames.put(None)

    with caplog.at_level(logging.ERROR):
        recorder._write_loop()

    assert recorder.disk_blocked is True
    assert recorder.written_rows == 0
    assert recorder.disk_dropped_frames == 1
    assert "No space left on device" in caplog.text
    assert recorder.prune_now.is_set(), "a full disk left the pruner asleep"


@needs_zstd
def test_the_writer_feeds_the_live_state_from_ticker_rows(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("wide", (Feed("ticker"),), Universe("listed", quote="USDT")))
    frame = json.dumps(
        {
            "topic": "tickers.AGIUSDT",
            "type": "delta",
            "ts": 1_800_000_000_000,
            "data": {"symbol": "AGIUSDT", "fundingRate": "-0.0015", "turnover24h": "123456", "price24hPcnt": "0.21"},
        }
    )
    recorder.frames.put(("frame", frame, BASE_NS, "wide"))
    recorder.frames.put(None)

    recorder._write_loop()

    live, _ = recorder.live.view()
    assert live["AGIUSDT"].funding_rate == pytest.approx(-0.0015)
    assert live["AGIUSDT"].turnover_24h == pytest.approx(123456.0)
    assert live["AGIUSDT"].price_change_24h == pytest.approx(0.21)
    assert live["AGIUSDT"].updated_ns == BASE_NS
    assert recorder.written_rows == 1
    assert recorder.meter.totals["feed:wide:ticker"] == len(frame)
    for segment in recorder.writer.close():
        recorder.compressor.submit(segment)


def test_the_host_config_drives_the_same_recorder(tmp_path: Path) -> None:
    text = """
[venue]
name = "bybit"
market = "linear"

[snapshots]
cadence = "hour"

[budget]
monthly_gb = 100
shed = ["wide:book:1"]

[[tier]]
name = "deep"
feeds = ["book:50", "trades"]
universe = { kind = "symbols", symbols = ["BTCUSDT"] }

[[tier]]
name = "wide"
feeds = ["book:1"]
universe = { kind = "listed", quote = "USDT", exclude_tiers = ["deep"] }
"""
    config = parse_config(tomllib.loads(text), base_dir=tmp_path)
    recorder = Recorder(config, root=tmp_path, adapter=BybitAdapter(rest_url="http://unused"))
    tables = {"instruments": [instrument("BTCUSDT"), instrument("AGIUSDT")], "tickers": []}

    topics = recorder.plan_topics(recorder.resolve_tiers(BASE_NS, tables))[0]

    assert recorder.snapshots.cadence == "hour"
    assert recorder.budget.settings.monthly_gb == 100.0
    assert topics == {"deep": ["orderbook.50.BTCUSDT", "publicTrade.BTCUSDT"], "wide": ["orderbook.1.AGIUSDT"]}


def test_the_shipped_host_configs_plan_every_tier(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("bybit-linear", "binance-usdm"):
        with (root / "deploy" / "capture" / f"{name}.toml").open("rb") as handle:
            config = parse_config(tomllib.load(handle), base_dir=root)
        assert config.budget.enforced
        assert [tier.name for tier in config.tiers][-1] == "wide"
        # The whole universe has a trade tape on the venue we replay; the book is tiered.
        wide = set(config.tier("wide").feeds)
        assert {Feed("ticker"), Feed("liquidations")} <= wide and not any(f.name == "book" for f in wide)
        if name == "bybit-linear":
            assert Feed("trades") in wide
            assert config.storage.queue_frames == 262_144
        else:
            assert config.storage.queue_frames == 131_072
        assert config.tier("crowded").universe.kind == "funding_below"
        # CARRY holds from a -10 bp settled print until settled funding rises above
        # -3 bp; both venues observe the whole zone from -3 bp predicted.
        assert config.tier("crowded").universe.threshold_bp == 3.0
        assert config.tier("crowded").universe.sticky_hours == 72.0
        for tier_name, feed in config.budget.shed:
            assert feed in {f.text for f in config.tier(tier_name).feeds}
        assert not any(name == "wide" and feed == "ticker" for name, feed in config.budget.shed[:-1])

    # Bybit is the venue that gets replayed: its shed list may never reach the
    # book or prints of a sleeve's own universe, nor any sensor.
    with (root / "deploy" / "capture" / "bybit-linear.toml").open("rb") as handle:
        bybit = parse_config(tomllib.load(handle), base_dir=root)
    core = bybit.tier("core").universe
    assert (core.kind, core.top, core.leave_top) == ("top_turnover", 120, 160), "core is LONG's live enter/leave rank"
    assert core.sticky_hours == 96.0, "LONG holds up to 72 h; the book stays through the hold and a day of tail"
    shed = set(bybit.budget.shed)
    assert not shed & {("core", "book:50"), ("core", "trades"), ("crowded", "book:50"), ("crowded", "trades")}
    assert not any(feed == "ticker" for _, feed in shed)
    assert bybit.budget.shed[0] == ("overheated", "book:50"), "the one deep tier no sleeve trades goes first"


def test_stop_events_are_independent_per_shard(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("deep", (Feed("trades"),), Universe("symbols", symbols=("BTCUSDT",))))
    first = unstarted_shard(recorder, "deep", ["a"], index=0)
    second = unstarted_shard(recorder, "deep", ["b"], index=1)
    first.close()
    assert first.stop.is_set() and not second.stop.is_set()
    assert isinstance(second.stop, threading.Event)


# ---------------------------------------------------- the other live universes


def test_positive_funding_promotes_into_an_overheated_tier(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier(
            "overheated",
            (Feed("book", "50"),),
            Universe("funding_above", threshold_bp=8.0, sticky_hours=1.0, quote="USDT"),
        ),
    )
    recorder.tables = {
        "instruments": [instrument("HOTUSDT"), instrument("COLDUSDT"), instrument("WARMUSDT")],
        "tickers": [],
    }
    recorder.live.observe("HOTUSDT", {"funding_rate": 0.0008}, BASE_NS)
    recorder.live.observe("COLDUSDT", {"funding_rate": -0.0020}, BASE_NS)
    recorder.live.observe("WARMUSDT", {"funding_rate": 0.00079}, BASE_NS)

    assert recorder.resolve_tiers(BASE_NS + 1)["overheated"] == ["HOTUSDT"]

    # The rate cools; the name keeps its tier for the sticky hour, then goes.
    recorder.live.observe("HOTUSDT", {"funding_rate": 0.0001}, BASE_NS + 2)
    assert recorder.resolve_tiers(BASE_NS + HOUR_NS)["overheated"] == ["HOTUSDT"]
    assert recorder.resolve_tiers(BASE_NS + 1 + HOUR_NS)["overheated"] == []


def test_top_movers_ranks_by_the_size_of_the_move_and_keeps_a_member_until_it_falls_below_leave_top(
    tmp_path: Path,
) -> None:
    recorder = build(
        tmp_path, Tier("movers", (Feed("book", "50"),), Universe("top_movers", top=2, leave_top=3, quote="USDT"))
    )
    names = ("AUSDT", "BUSDT", "CUSDT", "DUSDT")
    recorder.tables = {"instruments": [instrument(name) for name in names], "tickers": []}
    for name, change in zip(names, (0.20, -0.30, 0.05, 0.01)):
        recorder.live.observe(name, {"price_change_24h_pct": change}, BASE_NS)
    assert recorder.resolve_tiers(BASE_NS + 1)["movers"] == ["AUSDT", "BUSDT"]

    # C overtakes A; A is now rank 3, inside leave_top, so it stays.
    recorder.live.observe("CUSDT", {"price_change_24h_pct": -0.25}, BASE_NS + 2)
    assert recorder.resolve_tiers(BASE_NS + 3)["movers"] == ["AUSDT", "BUSDT", "CUSDT"]
    # D moves to rank 3: not a member and not in the top two, so it does not enter; A drops to rank 4 and leaves.
    recorder.live.observe("DUSDT", {"price_change_24h_pct": 0.22}, BASE_NS + 4)
    assert recorder.resolve_tiers(BASE_NS + 5)["movers"] == ["BUSDT", "CUSDT"]


def test_a_price_burst_is_measured_against_the_sample_one_window_back(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier(
            "bursting",
            (Feed("book", "50"),),
            Universe("price_burst", pct=0.05, window_hours=1.0, sticky_hours=1.0, quote="USDT"),
        ),
    )
    recorder.tables = {"instruments": [instrument("POPUSDT")], "tickers": []}
    recorder.live.observe("POPUSDT", {"mark_price": 1.00}, BASE_NS)

    # Half an hour in, up six percent: the history does not reach an hour back yet, so nothing.
    recorder.live.observe("POPUSDT", {"mark_price": 1.06}, BASE_NS + HOUR_NS // 2)
    assert recorder.resolve_tiers(BASE_NS + HOUR_NS // 2 + 1)["bursting"] == []

    # An hour and a minute in, still up six percent on the hour: promoted.
    recorder.live.observe("POPUSDT", {"mark_price": 1.06}, BASE_NS + HOUR_NS + 60 * 10**9)
    assert recorder.resolve_tiers(BASE_NS + HOUR_NS + 61 * 10**9)["bursting"] == ["POPUSDT"]

    # Later the price is where it was an hour before, so it no longer qualifies; the name lingers its sticky hour, then goes.
    recorder.live.observe("POPUSDT", {"mark_price": 1.06}, BASE_NS + HOUR_NS + 50 * 60 * 10**9)
    assert recorder.resolve_tiers(BASE_NS + HOUR_NS + 50 * 60 * 10**9 + 1)["bursting"] == ["POPUSDT"]
    assert recorder.resolve_tiers(BASE_NS + 2 * HOUR_NS + 2 * 60 * 10**9)["bursting"] == []


def test_a_volume_burst_is_the_window_trading_beyond_the_same_window_a_day_earlier(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier(
            "flooding",
            (Feed("book", "50"),),
            Universe("volume_burst", ratio=3.0, window_hours=1.0, sticky_hours=1.0, quote="USDT"),
        ),
    )
    recorder.tables = {"instruments": [instrument("HNTUSDT"), instrument("DULLUSDT")], "tickers": []}
    recorder.live.observe("HNTUSDT", {"turnover_24h": 24_000.0}, BASE_NS)
    recorder.live.observe("DULLUSDT", {"turnover_24h": 24_000.0}, BASE_NS)

    # An average hour of 28,000 is 1,167; three of them are 3,500. HNT grew 4,000, DULL 3,000.
    recorder.live.observe("HNTUSDT", {"turnover_24h": 28_000.0}, BASE_NS + HOUR_NS + 60 * 10**9)
    recorder.live.observe("DULLUSDT", {"turnover_24h": 27_000.0}, BASE_NS + HOUR_NS + 60 * 10**9)
    assert recorder.resolve_tiers(BASE_NS + HOUR_NS + 61 * 10**9)["flooding"] == ["HNTUSDT"]


def test_an_open_interest_jump_either_way_promotes(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier(
            "levering",
            (Feed("book", "50"),),
            Universe("oi_change", pct=0.10, window_hours=1.0, sticky_hours=1.0, quote="USDT"),
        ),
    )
    recorder.tables = {
        "instruments": [instrument("UPUSDT"), instrument("DOWNUSDT"), instrument("FLATUSDT")],
        "tickers": [],
    }
    for name in ("UPUSDT", "DOWNUSDT", "FLATUSDT"):
        recorder.live.observe(name, {"open_interest": 100.0}, BASE_NS)
    later = BASE_NS + HOUR_NS + 60 * 10**9
    recorder.live.observe("UPUSDT", {"open_interest": 110.0}, later)
    recorder.live.observe("DOWNUSDT", {"open_interest": 89.0}, later)
    recorder.live.observe("FLATUSDT", {"open_interest": 109.0}, later)

    assert recorder.resolve_tiers(later + 1)["levering"] == ["DOWNUSDT", "UPUSDT"]


def test_the_live_history_keeps_one_sample_a_minute_and_only_as_far_back_as_the_longest_window(tmp_path: Path) -> None:
    recorder = build(
        tmp_path,
        Tier("bursting", (Feed("book", "50"),), Universe("price_burst", pct=0.05, window_hours=1.0, quote="USDT")),
        Tier("levering", (Feed("book", "50"),), Universe("oi_change", pct=0.10, window_hours=2.0, quote="USDT")),
    )
    live = recorder.live
    assert live.history_ns == int(2.0 * 3600 * 1e9 * 1.25)
    for second in range(0, 4 * 3600, 10):
        live.observe("BTCUSDT", {"mark_price": 1.0 + second / 1e6}, BASE_NS + second * 10**9)
    samples = live.history["BTCUSDT"]
    spacing = {(b.ns - a.ns) // 10**9 for a, b in zip(samples, list(samples)[1:])}
    assert spacing == {60}
    assert len(samples) <= 2.5 * 60 + 2
    # The oldest kept sample is the one just past the window, so a lookback at the window's edge resolves.
    edge = samples[-1].ns - live.history_ns
    assert samples[0].ns <= edge < samples[1].ns
    assert live.earlier("BTCUSDT", edge) is samples[0]
    assert live.earlier("BTCUSDT", samples[0].ns - 1) is None
    assert live.earlier("ETHUSDT", edge) is None


def test_without_a_windowed_universe_no_history_is_kept(tmp_path: Path) -> None:
    recorder = build(tmp_path, Tier("deep", DEEP_FEEDS, Universe("symbols", symbols=("BTCUSDT",))))
    recorder.live.observe("BTCUSDT", {"mark_price": 1.0}, BASE_NS)
    assert recorder.live.history == {} and recorder.live.history_ns == 0
