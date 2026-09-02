"""The capture configuration: one TOML file says what a recorder records.

A recorder records one venue. It records a list of tiers; each tier names a
universe of symbols and the feeds to take for them. A symbol in several tiers
gets the union of their feeds, and each venue topic is subscribed once.

```toml
schema = 1

[venue]
name = "bybit"                 # bybit | binance
market = "linear"              # bybit: linear ; binance: usdm

[storage]
root = "/var/lib/liquidity-migration/forward-market"   # `record --root` overrides
segment_max_mb = 64
retention_days = 30
max_disk_gb = 60
min_free_disk_gb = 25

[connection]
topics_per_connection = 150

[snapshots]
cadence = "day"                # how often the instrument and ticker tables are
                               # written and the universes re-read: day | hour

[[tier]]
name = "deep"
feeds = ["book:50", "book:1", "trades", "ticker", "liquidations"]
universe = { kind = "file", path = "deploy/forward-capture-symbols.txt" }

[[tier]]
name = "crowded"
feeds = ["book:50"]
universe = { kind = "funding_below", threshold_bp = 10, sticky_days = 2, exclude_tiers = ["deep"] }

[[tier]]
name = "wide"
feeds = ["book:1", "trades", "ticker", "liquidations"]
universe = { kind = "listed", quote = "USDT", exclude_tiers = ["deep"] }
```

Feeds: `book:<levels>` (the venue says which level counts it offers; `book:1`
is the top of book), `trades`, `ticker` (last, mark, index, funding, open
interest, best bid and ask, 24h turnover, as the venue pushes them),
`liquidations`, `kline:<interval>` (venue candles, e.g. `kline:1m`), and
`open_interest:<seconds>` (a REST poll, for venues that push no open interest).

Universes: `symbols` (an inline list), `file` (one symbol per line, `#`
comments), `listed` (every perpetual the venue lists as trading, optionally
filtered by `quote`), `top_turnover` (the `top` names by 24h turnover from the
ticker table), and `funding_below` (names whose funding rate is at or below
`-threshold_bp`, kept for `sticky_days` days). `exclude_tiers` removes names
already covered by the named tiers. The `listed`, `top_turnover`, and
`funding_below` kinds are re-read at the snapshot cadence.

The full-universe configuration for a machine with unbounded bandwidth and
disk is one tier: `listed` with every feed. See `examples/`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

CONFIG_SCHEMA = 1
VENUES = ("bybit", "binance")
FEED_NAMES = ("book", "trades", "ticker", "liquidations", "kline", "open_interest")
UNIVERSE_KINDS = ("symbols", "file", "listed", "top_turnover", "funding_below")
SNAPSHOT_CADENCES = ("day", "hour")


class ConfigError(ValueError):
    """The configuration cannot be recorded from."""


@dataclass(frozen=True, slots=True)
class Feed:
    name: str
    arg: str | None = None

    @property
    def text(self) -> str:
        return self.name if self.arg is None else f"{self.name}:{self.arg}"

    @property
    def levels(self) -> int:
        if self.name != "book" or self.arg is None:
            raise ConfigError(f"{self.text} has no level count")
        return int(self.arg)

    @property
    def seconds(self) -> float:
        if self.name != "open_interest" or self.arg is None:
            raise ConfigError(f"{self.text} has no poll interval")
        return float(self.arg.rstrip("s"))


@dataclass(frozen=True, slots=True)
class Universe:
    kind: str
    symbols: tuple[str, ...] = ()
    path: Path | None = None
    quote: str | None = None
    top: int = 0
    threshold_bp: float = 0.0
    sticky_days: int = 1
    exclude_tiers: tuple[str, ...] = ()

    @property
    def dynamic(self) -> bool:
        """Re-read at every snapshot, so its shards may restart."""

        return self.kind in ("listed", "top_turnover", "funding_below")


@dataclass(frozen=True, slots=True)
class Tier:
    name: str
    feeds: tuple[Feed, ...]
    universe: Universe


@dataclass(frozen=True, slots=True)
class VenueSettings:
    name: str
    market: str
    ws_url: str | None = None
    rest_url: str | None = None


@dataclass(frozen=True, slots=True)
class StorageSettings:
    root: Path | None = None
    segment_max_mb: float = 64.0
    fsync_every_records: int = 1_000
    retention_days: int = 30
    max_disk_gb: float = 60.0
    min_free_disk_gb: float = 25.0
    queue_frames: int = 32_768
    status_interval_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    venue: VenueSettings
    storage: StorageSettings
    tiers: tuple[Tier, ...]
    topics_per_connection: int = 150
    snapshot_cadence: str = "day"
    source_path: Path | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def tier(self, name: str) -> Tier:
        for tier in self.tiers:
            if tier.name == name:
                return tier
        raise KeyError(name)


# ------------------------------------------------------------------ parsing


def parse_feed(text: str) -> Feed:
    name, _, raw_arg = str(text).strip().partition(":")
    if name not in FEED_NAMES:
        raise ConfigError(f"unknown feed {text!r}; feeds are {', '.join(FEED_NAMES)}")
    arg: str | None = raw_arg.strip() or None
    if name == "book":
        if arg is None or not arg.isdigit() or int(arg) <= 0:
            raise ConfigError(f"a book feed names its level count, like book:50, got {text!r}")
    elif name == "kline":
        if not arg:
            raise ConfigError(f"a kline feed names its interval, like kline:1m, got {text!r}")
    elif name == "open_interest":
        if arg is None:
            arg = "60s"
        try:
            if float(arg.rstrip("s")) <= 0:
                raise ValueError
        except ValueError as exc:
            raise ConfigError(f"an open_interest feed names a poll interval in seconds, got {text!r}") from exc
    elif arg is not None:
        raise ConfigError(f"{name} takes no argument, got {text!r}")
    return Feed(name, arg)


def load_symbol_file(path: Path) -> tuple[str, ...]:
    symbols: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.partition("#")[0].strip()
        symbols.update(token.upper() for token in text.replace(",", " ").split() if token)
    return tuple(sorted(symbols))


def validate_symbols(symbols: Iterable[str]) -> tuple[str, ...]:
    result = tuple(sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}))
    invalid = [symbol for symbol in result if not symbol.isalnum()]
    if invalid:
        raise ConfigError(f"invalid symbols: {invalid}")
    return result


def _universe(raw: Mapping[str, Any], *, tier: str, base_dir: Path) -> Universe:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"tier {tier!r}: universe must be a table")
    kind = str(raw.get("kind") or "")
    if kind not in UNIVERSE_KINDS:
        raise ConfigError(f"tier {tier!r}: unknown universe kind {kind!r}; kinds are {', '.join(UNIVERSE_KINDS)}")
    exclude = tuple(str(name) for name in raw.get("exclude_tiers", ()))
    if kind == "symbols":
        symbols = validate_symbols(raw.get("symbols", ()))
        if not symbols:
            raise ConfigError(f"tier {tier!r}: a symbols universe needs at least one symbol")
        return Universe(kind, symbols=symbols, exclude_tiers=exclude)
    if kind == "file":
        text = raw.get("path")
        if not text:
            raise ConfigError(f"tier {tier!r}: a file universe needs a path")
        path = Path(str(text))
        if not path.is_absolute():
            path = base_dir / path
        return Universe(kind, path=path, exclude_tiers=exclude)
    if kind == "listed":
        quote = raw.get("quote")
        return Universe(kind, quote=str(quote).upper() if quote else None, exclude_tiers=exclude)
    if kind == "top_turnover":
        top = int(raw.get("top") or 0)
        if top <= 0:
            raise ConfigError(f"tier {tier!r}: top_turnover needs top > 0")
        quote = raw.get("quote")
        return Universe(kind, top=top, quote=str(quote).upper() if quote else None, exclude_tiers=exclude)
    threshold = float(raw.get("threshold_bp") or 0.0)
    if threshold <= 0:
        raise ConfigError(f"tier {tier!r}: funding_below needs threshold_bp > 0")
    sticky = int(raw.get("sticky_days", 1))
    if sticky <= 0:
        raise ConfigError(f"tier {tier!r}: sticky_days must be positive")
    quote = raw.get("quote")
    return Universe(
        kind,
        threshold_bp=threshold,
        sticky_days=sticky,
        quote=str(quote).upper() if quote else None,
        exclude_tiers=exclude,
    )


def _positive(table: Mapping[str, Any], name: str, default: float, *, section: str) -> float:
    value = table.get(name, default)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{section}.{name} must be a number") from exc
    if number <= 0:
        raise ConfigError(f"{section}.{name} must be positive")
    return number


def parse_config(data: Mapping[str, Any], *, base_dir: Path, source_path: Path | None = None) -> CaptureConfig:
    if int(data.get("schema", CONFIG_SCHEMA)) != CONFIG_SCHEMA:
        raise ConfigError(f"capture config schema {data.get('schema')} is not {CONFIG_SCHEMA}")
    venue_table = data.get("venue")
    if not isinstance(venue_table, Mapping):
        raise ConfigError("config needs a [venue] table")
    name = str(venue_table.get("name") or "")
    if name not in VENUES:
        raise ConfigError(f"unknown venue {name!r}; venues are {', '.join(VENUES)}")
    market = str(venue_table.get("market") or "")
    if not market:
        raise ConfigError("venue.market is required")
    venue = VenueSettings(
        name=name,
        market=market,
        ws_url=str(venue_table["ws_url"]) if venue_table.get("ws_url") else None,
        rest_url=str(venue_table["rest_url"]) if venue_table.get("rest_url") else None,
    )

    storage_table = data.get("storage") or {}
    if not isinstance(storage_table, Mapping):
        raise ConfigError("[storage] must be a table")
    root = storage_table.get("root")
    root_path = Path(str(root)) if root else None
    if root_path is not None and not root_path.is_absolute():
        root_path = base_dir / root_path
    storage = StorageSettings(
        root=root_path,
        segment_max_mb=_positive(storage_table, "segment_max_mb", 64.0, section="storage"),
        fsync_every_records=int(_positive(storage_table, "fsync_every_records", 1_000, section="storage")),
        retention_days=int(_positive(storage_table, "retention_days", 30, section="storage")),
        max_disk_gb=_positive(storage_table, "max_disk_gb", 60.0, section="storage"),
        min_free_disk_gb=_positive(storage_table, "min_free_disk_gb", 25.0, section="storage"),
        queue_frames=int(_positive(storage_table, "queue_frames", 32_768, section="storage")),
        status_interval_seconds=_positive(storage_table, "status_interval_seconds", 30.0, section="storage"),
    )

    connection = data.get("connection") or {}
    topics_per_connection = int(_positive(connection, "topics_per_connection", 150, section="connection"))

    snapshots = data.get("snapshots") or {}
    cadence = str(snapshots.get("cadence") or "day")
    if cadence not in SNAPSHOT_CADENCES:
        raise ConfigError(f"snapshots.cadence must be one of {SNAPSHOT_CADENCES}, got {cadence!r}")

    raw_tiers = data.get("tier")
    if not isinstance(raw_tiers, list) or not raw_tiers:
        raise ConfigError("config needs at least one [[tier]]")
    tiers: list[Tier] = []
    seen: set[str] = set()
    for raw in raw_tiers:
        if not isinstance(raw, Mapping):
            raise ConfigError("each [[tier]] must be a table")
        tier_name = str(raw.get("name") or "")
        if not tier_name or tier_name in seen:
            raise ConfigError(f"tier names must be unique and non-empty, got {tier_name!r}")
        seen.add(tier_name)
        feeds_raw = raw.get("feeds")
        if not isinstance(feeds_raw, list) or not feeds_raw:
            raise ConfigError(f"tier {tier_name!r} needs a feeds list")
        feeds = tuple(parse_feed(text) for text in feeds_raw)
        if len({feed.text for feed in feeds}) != len(feeds):
            raise ConfigError(f"tier {tier_name!r} repeats a feed")
        universe = _universe(raw.get("universe") or {}, tier=tier_name, base_dir=base_dir)
        for excluded in universe.exclude_tiers:
            if excluded not in seen or excluded == tier_name:
                raise ConfigError(f"tier {tier_name!r} excludes {excluded!r}, which is not an earlier tier")
        tiers.append(Tier(tier_name, feeds, universe))

    extra = {key: value for key, value in data.items() if key not in {"schema", "venue", "storage", "connection", "snapshots", "tier"}}
    return CaptureConfig(
        venue=venue,
        storage=storage,
        tiers=tuple(tiers),
        topics_per_connection=topics_per_connection,
        snapshot_cadence=cadence,
        source_path=source_path,
        extra=extra,
    )


def load_config(path: Path, *, base_dir: Path | None = None) -> CaptureConfig:
    """Read a TOML capture config. Relative paths resolve against `base_dir`
    (default: the current working directory, which on the host is the checkout)."""

    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return parse_config(data, base_dir=(base_dir or Path.cwd()).resolve(), source_path=path.resolve())
