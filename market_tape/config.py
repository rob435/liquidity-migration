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
                               # written: day | hour

[budget]
monthly_gb = 1300              # inbound allowance for this recorder
shed = ["crowded:trades", "core:book:1"]   # what to give up first when over pace

[[tier]]
name = "core"
feeds = ["book:50", "book:1", "trades", "ticker", "liquidations"]
universe = { kind = "top_turnover", top = 30, leave_top = 45, quote = "USDT" }

[[tier]]
name = "crowded"
feeds = ["book:50", "trades"]
universe = { kind = "funding_below", threshold_bp = 8, sticky_hours = 48, quote = "USDT", exclude_tiers = ["core"] }

[[tier]]
name = "wide"
feeds = ["ticker", "liquidations"]
universe = { kind = "listed", quote = "USDT", exclude_tiers = ["core"] }
```

Feeds: `book:<levels>` (the venue says which level counts it offers; `book:1`
is the top of book), `trades`, `ticker` (last, mark, index, funding, open
interest, best bid and ask, 24h turnover and price change, as the venue pushes
them), `liquidations`, `kline:<interval>` (venue candles, e.g. `kline:1m`), and
`open_interest:<seconds>` (a REST poll, for venues that push no open interest).

Universes:

- `symbols` (an inline list) and `file` (one symbol per line, `#` comments)
  are fixed for the life of the process.
- `listed`: every perpetual the venue lists as trading, optionally filtered by
  `quote`; re-read with each table snapshot.
- `top_turnover`: the `top` names by 24h turnover. A member stays until it
  falls below rank `leave_top` (default one and a half times `top`), so a name
  on the boundary does not flap.
- `funding_below`: names whose funding rate is at or below `-threshold_bp`.
- `turnover_surge`: names whose 24h turnover is at least `ratio` times what
  the last table snapshot showed for them.
- `price_move`: names whose 24h price change is at least `pct` (a fraction:
  0.2 is twenty percent) in either direction.

The last four are live: the recorder reads them off the ticker stream it is
already recording and promotes a name within one maintenance tick of the
observation, not at the next daily snapshot. A name that qualified stays for
`sticky_hours` (default 48) after its last qualifying observation. The ticker
feed on a `listed` tier is the sensor; without it, a live universe sees only
the names some tier already records. `exclude_tiers` removes names already
covered by the named tiers.

Budget: `monthly_gb` is this recorder's inbound allowance. The recorder
projects a month from its last 24 hours of received bytes; when the projection
is over the allowance it gives up the first entry of `shed` (a `tier:feed`
pair), then the next an hour later, and restores them in reverse once the
projection is under `restore_below` (default 0.8) of the allowance. Without a
budget the recorder only measures. The full-universe configuration for a
machine with unbounded bandwidth and disk is one tier: `listed` with every feed
and no budget. See `examples/`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

CONFIG_SCHEMA = 1
VENUES = ("bybit", "binance")
FEED_NAMES = ("book", "trades", "ticker", "liquidations", "kline", "open_interest")
UNIVERSE_KINDS = ("symbols", "file", "listed", "top_turnover", "funding_below", "turnover_surge", "price_move")
LIVE_KINDS = ("top_turnover", "funding_below", "turnover_surge", "price_move")
SNAPSHOT_CADENCES = ("day", "hour")
DEFAULT_STICKY_HOURS = 48.0


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
    leave_top: int = 0
    threshold_bp: float = 0.0
    ratio: float = 0.0
    pct: float = 0.0
    sticky_hours: float = DEFAULT_STICKY_HOURS
    exclude_tiers: tuple[str, ...] = ()

    @property
    def dynamic(self) -> bool:
        """Changes while the recorder runs, so its topics are added and removed live."""

        return self.kind in LIVE_KINDS or self.kind == "listed"

    @property
    def live(self) -> bool:
        """Decided from the ticker stream, not from a table snapshot."""

        return self.kind in LIVE_KINDS


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
class BudgetSettings:
    monthly_gb: float | None = None
    shed: tuple[tuple[str, str], ...] = ()
    restore_below: float = 0.8
    act_every_minutes: float = 60.0

    @property
    def enforced(self) -> bool:
        return self.monthly_gb is not None


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    venue: VenueSettings
    storage: StorageSettings
    tiers: tuple[Tier, ...]
    topics_per_connection: int = 150
    snapshot_cadence: str = "day"
    budget: BudgetSettings = field(default_factory=BudgetSettings)
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


def _sticky_hours(raw: Mapping[str, Any], *, tier: str) -> float:
    if "sticky_hours" in raw and "sticky_days" in raw:
        raise ConfigError(f"tier {tier!r}: give sticky_hours or sticky_days, not both")
    if "sticky_days" in raw:
        days = float(raw["sticky_days"])
        if days <= 0:
            raise ConfigError(f"tier {tier!r}: sticky_days must be positive")
        return days * 24.0
    hours = float(raw.get("sticky_hours", DEFAULT_STICKY_HOURS))
    if hours <= 0:
        raise ConfigError(f"tier {tier!r}: sticky_hours must be positive")
    return hours


def _quote(raw: Mapping[str, Any]) -> str | None:
    quote = raw.get("quote")
    return str(quote).upper() if quote else None


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
        return Universe(kind, quote=_quote(raw), exclude_tiers=exclude)
    if kind == "top_turnover":
        top = int(raw.get("top") or 0)
        if top <= 0:
            raise ConfigError(f"tier {tier!r}: top_turnover needs top > 0")
        leave_top = int(raw.get("leave_top") or round(top * 1.5))
        if leave_top < top:
            raise ConfigError(f"tier {tier!r}: leave_top must be at least top")
        return Universe(kind, top=top, leave_top=leave_top, quote=_quote(raw), exclude_tiers=exclude)
    sticky = _sticky_hours(raw, tier=tier)
    if kind == "funding_below":
        threshold = float(raw.get("threshold_bp") or 0.0)
        if threshold <= 0:
            raise ConfigError(f"tier {tier!r}: funding_below needs threshold_bp > 0")
        return Universe(kind, threshold_bp=threshold, sticky_hours=sticky, quote=_quote(raw), exclude_tiers=exclude)
    if kind == "turnover_surge":
        ratio = float(raw.get("ratio") or 0.0)
        if ratio <= 1.0:
            raise ConfigError(f"tier {tier!r}: turnover_surge needs ratio > 1")
        return Universe(kind, ratio=ratio, sticky_hours=sticky, quote=_quote(raw), exclude_tiers=exclude)
    pct = float(raw.get("pct") or 0.0)
    if pct <= 0:
        raise ConfigError(f"tier {tier!r}: price_move needs pct > 0 (a fraction, 0.2 is twenty percent)")
    return Universe(kind, pct=pct, sticky_hours=sticky, quote=_quote(raw), exclude_tiers=exclude)


def _positive(table: Mapping[str, Any], name: str, default: float, *, section: str) -> float:
    value = table.get(name, default)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{section}.{name} must be a number") from exc
    if number <= 0:
        raise ConfigError(f"{section}.{name} must be positive")
    return number


def _budget(raw: Mapping[str, Any], tiers: Iterable[Tier]) -> BudgetSettings:
    if not isinstance(raw, Mapping):
        raise ConfigError("[budget] must be a table")
    monthly = raw.get("monthly_gb")
    monthly_gb = None if monthly is None else _positive(raw, "monthly_gb", 0.0, section="budget")
    restore_below = float(raw.get("restore_below", 0.8))
    if not 0.0 < restore_below < 1.0:
        raise ConfigError("budget.restore_below must be between 0 and 1")
    act_every = _positive(raw, "act_every_minutes", 60.0, section="budget")
    feeds_by_tier = {tier.name: {feed.text for feed in tier.feeds} for tier in tiers}
    shed: list[tuple[str, str]] = []
    for text in raw.get("shed", ()):
        tier_name, separator, feed_text = str(text).partition(":")
        if not separator or tier_name not in feeds_by_tier or feed_text not in feeds_by_tier[tier_name]:
            raise ConfigError(f"budget.shed entry {text!r} must be tier:feed for a tier and feed in this config")
        if (tier_name, feed_text) in shed:
            raise ConfigError(f"budget.shed repeats {text!r}")
        shed.append((tier_name, feed_text))
    if shed and monthly_gb is None:
        raise ConfigError("budget.shed needs budget.monthly_gb")
    return BudgetSettings(monthly_gb=monthly_gb, shed=tuple(shed), restore_below=restore_below, act_every_minutes=act_every)


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
        if not tier_name or tier_name in seen or ":" in tier_name:
            raise ConfigError(f"tier names must be unique, non-empty, and free of ':', got {tier_name!r}")
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

    budget = _budget(data.get("budget") or {}, tiers)
    known = {"schema", "venue", "storage", "connection", "snapshots", "budget", "tier"}
    extra = {key: value for key, value in data.items() if key not in known}
    return CaptureConfig(
        venue=venue,
        storage=storage,
        tiers=tuple(tiers),
        topics_per_connection=topics_per_connection,
        snapshot_cadence=cadence,
        budget=budget,
        source_path=source_path,
        extra=extra,
    )


def load_config(path: Path, *, base_dir: Path | None = None) -> CaptureConfig:
    """Read a TOML capture config. Relative paths resolve against `base_dir`
    (default: the current working directory, which on the host is the checkout)."""

    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return parse_config(data, base_dir=(base_dir or Path.cwd()).resolve(), source_path=path.resolve())
