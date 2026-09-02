"""The capture config: what a TOML file may say, and what it may not."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import market_tape.config as config_module
from market_tape.config import (
    ConfigError,
    Feed,
    load_config,
    load_symbol_file,
    parse_config,
    parse_feed,
    validate_symbols,
)

ROOT = Path(config_module.__file__).resolve().parents[1]

MINIMAL = """
[venue]
name = "bybit"
market = "linear"

[[tier]]
name = "deep"
feeds = ["book:50"]
universe = { kind = "symbols", symbols = ["BTCUSDT"] }
"""


def parse(text: str, *, base_dir: Path | None = None):
    return parse_config(tomllib.loads(text), base_dir=base_dir or ROOT)


def docstring_example() -> str:
    doc = config_module.__doc__ or ""
    start = doc.index("```toml") + len("```toml")
    return doc[start : doc.index("```", start)]


def test_the_documented_example_parses() -> None:
    config = parse(docstring_example())

    assert config.venue.name == "bybit"
    assert config.venue.market == "linear"
    assert config.storage.root == Path("/var/lib/liquidity-migration/forward-market")
    assert config.storage.segment_max_mb == 64.0
    assert config.storage.retention_days == 30
    assert config.topics_per_connection == 150
    assert config.snapshot_cadence == "day"
    assert [tier.name for tier in config.tiers] == ["deep", "crowded", "wide"]
    assert [feed.text for feed in config.tier("crowded").feeds] == ["book:50"]
    crowded = config.tier("crowded").universe
    assert crowded.kind == "funding_below"
    assert (crowded.threshold_bp, crowded.sticky_days, crowded.exclude_tiers) == (10.0, 2, ("deep",))
    assert crowded.dynamic
    deep = config.tier("deep").universe
    assert deep.kind == "file"
    assert deep.path == ROOT / "deploy" / "forward-capture-symbols.txt"
    assert not deep.dynamic
    assert config.tier("wide").universe.quote == "USDT"


def test_a_config_file_is_read_and_relative_paths_take_the_base_directory(tmp_path: Path) -> None:
    path = tmp_path / "capture.toml"
    path.write_text(
        MINIMAL.replace(
            '{ kind = "symbols", symbols = ["BTCUSDT"] }',
            '{ kind = "file", path = "names/deep.txt" }',
        )
        + '\n[storage]\nroot = "tape"\n',
        encoding="utf-8",
    )

    config = load_config(path, base_dir=tmp_path)

    assert config.source_path == path.resolve()
    assert config.tier("deep").universe.path == tmp_path / "names" / "deep.txt"
    assert config.storage.root == tmp_path / "tape"

    absolute = parse(
        MINIMAL.replace(
            '{ kind = "symbols", symbols = ["BTCUSDT"] }',
            '{ kind = "file", path = "/etc/liquidity-migration/deep.txt" }',
        ),
        base_dir=tmp_path,
    )
    assert absolute.tier("deep").universe.path == Path("/etc/liquidity-migration/deep.txt")


def test_every_feed_spells_itself_out() -> None:
    assert parse_feed("book:50") == Feed("book", "50")
    assert parse_feed("book:50").levels == 50
    assert parse_feed("book:1").text == "book:1"
    assert parse_feed("trades") == Feed("trades", None)
    assert parse_feed("trades").text == "trades"
    assert parse_feed("ticker") == Feed("ticker", None)
    assert parse_feed("liquidations") == Feed("liquidations", None)
    assert parse_feed("kline:1m") == Feed("kline", "1m")
    assert parse_feed("open_interest") == Feed("open_interest", "60s")
    assert parse_feed("open_interest").seconds == 60.0
    assert parse_feed("open_interest:30").seconds == 30.0
    assert parse_feed(" trades ") == Feed("trades", None)

    for text in ("book", "book:0", "book:fifty", "kline", "trades:1", "ticker:x", "open_interest:0", "depth"):
        with pytest.raises(ConfigError):
            parse_feed(text)

    with pytest.raises(ConfigError):
        parse_feed("trades").levels
    with pytest.raises(ConfigError):
        parse_feed("book:50").seconds


def test_a_symbol_file_is_commentable_and_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "symbols.txt"
    path.write_text("# watched\nagiusdt, BTCUSDT\nAGIUSDT # duplicate\n\n", encoding="utf-8")

    assert load_symbol_file(path) == ("AGIUSDT", "BTCUSDT")
    assert validate_symbols(["ethusdt", " BTCUSDT ", "btcusdt"]) == ("BTCUSDT", "ETHUSDT")
    with pytest.raises(ConfigError):
        validate_symbols(["BTC-26SEP26"])


def test_an_unknown_venue_is_refused() -> None:
    with pytest.raises(ConfigError, match="unknown venue"):
        parse(MINIMAL.replace('name = "bybit"', 'name = "kraken"'))
    with pytest.raises(ConfigError, match="venue.market"):
        parse(MINIMAL.replace('market = "linear"', 'market = ""'))
    with pytest.raises(ConfigError, match=r"\[venue\]"):
        parse("[[tier]]\nname = 'deep'\nfeeds = ['trades']\nuniverse = { kind = 'listed' }\n")
    with pytest.raises(ConfigError, match="schema"):
        parse("schema = 2\n" + MINIMAL)


def test_a_config_without_a_tier_is_refused() -> None:
    with pytest.raises(ConfigError, match=r"\[\[tier\]\]"):
        parse('[venue]\nname = "bybit"\nmarket = "linear"\n')
    with pytest.raises(ConfigError, match="feeds list"):
        parse(MINIMAL.replace('feeds = ["book:50"]', "feeds = []"))
    with pytest.raises(ConfigError, match="unique"):
        parse(MINIMAL + MINIMAL.partition("[[tier]]")[2].join(("[[tier]]", "")))


def test_a_tier_may_not_repeat_a_feed() -> None:
    with pytest.raises(ConfigError, match="repeats a feed"):
        parse(MINIMAL.replace('feeds = ["book:50"]', 'feeds = ["book:50", "trades", "book:50"]'))
    assert [feed.text for feed in parse(MINIMAL.replace('feeds = ["book:50"]', 'feeds = ["book:50", "book:1"]')).tiers[0].feeds] == [
        "book:50",
        "book:1",
    ]


def test_a_book_feed_names_its_level_count() -> None:
    with pytest.raises(ConfigError, match="level count"):
        parse(MINIMAL.replace('feeds = ["book:50"]', 'feeds = ["book"]'))
    with pytest.raises(ConfigError, match="level count"):
        parse(MINIMAL.replace('feeds = ["book:50"]', 'feeds = ["book:deep"]'))


def test_a_tier_may_only_exclude_an_earlier_tier() -> None:
    later = """
[venue]
name = "bybit"
market = "linear"

[[tier]]
name = "first"
feeds = ["trades"]
universe = { kind = "listed", exclude_tiers = ["second"] }

[[tier]]
name = "second"
feeds = ["trades"]
universe = { kind = "symbols", symbols = ["BTCUSDT"] }
"""
    with pytest.raises(ConfigError, match="not an earlier tier"):
        parse(later)
    with pytest.raises(ConfigError, match="not an earlier tier"):
        parse(MINIMAL.replace('{ kind = "symbols", symbols = ["BTCUSDT"] }', '{ kind = "listed", exclude_tiers = ["deep"] }'))


def test_a_dynamic_universe_needs_its_own_dial() -> None:
    with pytest.raises(ConfigError, match="threshold_bp"):
        parse(MINIMAL.replace('{ kind = "symbols", symbols = ["BTCUSDT"] }', '{ kind = "funding_below" }'))
    with pytest.raises(ConfigError, match="threshold_bp"):
        parse(MINIMAL.replace('{ kind = "symbols", symbols = ["BTCUSDT"] }', '{ kind = "funding_below", threshold_bp = 0 }'))
    with pytest.raises(ConfigError, match="sticky_days"):
        parse(
            MINIMAL.replace(
                '{ kind = "symbols", symbols = ["BTCUSDT"] }',
                '{ kind = "funding_below", threshold_bp = 10, sticky_days = -1 }',
            )
        )
    with pytest.raises(ConfigError, match="sticky_days"):
        parse(
            MINIMAL.replace(
                '{ kind = "symbols", symbols = ["BTCUSDT"] }',
                '{ kind = "funding_below", threshold_bp = 10, sticky_days = 0 }',
            )
        )
    default_sticky = parse(
        MINIMAL.replace('{ kind = "symbols", symbols = ["BTCUSDT"] }', '{ kind = "funding_below", threshold_bp = 10 }')
    )
    assert default_sticky.tiers[0].universe.sticky_days == 1
    with pytest.raises(ConfigError, match="top"):
        parse(MINIMAL.replace('{ kind = "symbols", symbols = ["BTCUSDT"] }', '{ kind = "top_turnover" }'))
    with pytest.raises(ConfigError, match="top"):
        parse(MINIMAL.replace('{ kind = "symbols", symbols = ["BTCUSDT"] }', '{ kind = "top_turnover", top = 0 }'))
    with pytest.raises(ConfigError, match="unknown universe kind"):
        parse(MINIMAL.replace('{ kind = "symbols", symbols = ["BTCUSDT"] }', '{ kind = "open_interest" }'))
    with pytest.raises(ConfigError, match="at least one symbol"):
        parse(MINIMAL.replace('{ kind = "symbols", symbols = ["BTCUSDT"] }', '{ kind = "symbols", symbols = [] }'))
    top = parse(MINIMAL.replace('{ kind = "symbols", symbols = ["BTCUSDT"] }', '{ kind = "top_turnover", top = 25, quote = "usdt" }'))
    assert (top.tiers[0].universe.top, top.tiers[0].universe.quote) == (25, "USDT")


def test_the_storage_and_cadence_dials_must_make_sense() -> None:
    with pytest.raises(ConfigError, match="cadence"):
        parse(MINIMAL + '\n[snapshots]\ncadence = "minute"\n')
    with pytest.raises(ConfigError, match="segment_max_mb must be positive"):
        parse(MINIMAL + "\n[storage]\nsegment_max_mb = 0\n")
    with pytest.raises(ConfigError, match="topics_per_connection"):
        parse(MINIMAL + "\n[connection]\ntopics_per_connection = -1\n")
    hourly = parse(MINIMAL + '\n[snapshots]\ncadence = "hour"\n')
    assert hourly.snapshot_cadence == "hour"
    with pytest.raises(KeyError):
        hourly.tier("crowded")


def test_the_shipped_example_configs_parse() -> None:
    for path in sorted((ROOT / "market_tape" / "examples").glob("*.toml")):
        config = load_config(path, base_dir=ROOT)
        assert config.tiers
