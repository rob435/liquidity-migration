"""The frozen row contract of the market tape.

Every row on the tape is one JSON object on one line. The writer side
(`venues/*.py` normalizers) builds rows only through the constructors here; the
reader side (`load.py`) turns them back into the typed rows here. Changing a
field name or meaning is a schema change: bump SCHEMA_VERSION, keep reading
the old shape, and say so in README.md.

Schema history:

- 1: rows carry no `venue`; the venue is the archive folder (`bybit-linear`).
     `previous_update_id` is the recorder's own view of the row before.
- 2: every row carries `venue`. Book rows gain `first_update_id` (Binance `U`;
     0 elsewhere) and `previous_update_id` is the venue's own previous id when
     the venue publishes one (Binance `pu`), else the recorder's view.

Timestamps are integer nanoseconds. `local_receive_ts_ns` is the recorder
host's wall clock at receipt and is the sort key of the tape. Venue
timestamps keep the venue's names in spirit: `exchange_system_ts_ns` is when
the venue's gateway sent the message, `exchange_engine_ts_ns` when its
matching engine produced it, `exchange_ts_ns` the event's own time (a trade,
a liquidation).

Prices and sizes are stored as the venue's decimal strings inside book
levels, so no precision is lost; the typed rows convert to float.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Union

SCHEMA_VERSION = 2

KIND_BOOK_SNAPSHOT = "orderbook_snapshot"
KIND_BOOK_DELTA = "orderbook_delta"
KIND_TRADE = "public_trade"
KIND_TICKER = "ticker"
KIND_LIQUIDATION = "liquidation"
KIND_KLINE = "kline"
ROW_KINDS = (KIND_BOOK_SNAPSHOT, KIND_BOOK_DELTA, KIND_TRADE, KIND_TICKER, KIND_LIQUIDATION, KIND_KLINE)

SNAPSHOT_INSTRUMENTS = "instruments_snapshot"
SNAPSHOT_TICKERS = "tickers_snapshot"
SNAPSHOT_KINDS = (SNAPSHOT_INSTRUMENTS, SNAPSHOT_TICKERS)

SIDES = ("Buy", "Sell")

#: The venue-neutral names a ticker row may carry under `values`. A row carries
#: only the fields the venue pushed in that message; a missing field means
#: "unchanged", never zero.
TICKER_VALUE_FIELDS = (
    "last_price",
    "mark_price",
    "index_price",
    "open_interest",
    "open_interest_value",
    "funding_rate",
    "next_funding_time_ms",
    "bid_price",
    "bid_size",
    "ask_price",
    "ask_size",
    "turnover_24h",
    "volume_24h",
)
TICKER_INT_FIELDS = frozenset({"next_funding_time_ms"})

Level = tuple[float, float]
RawLevel = list[str] | tuple[str, str]


class SchemaError(ValueError):
    """A row that does not follow the contract."""


# ------------------------------------------------------------- constructors


def book_row(
    *,
    venue: str,
    symbol: str,
    snapshot: bool,
    depth: int,
    local_receive_ts_ns: int,
    exchange_system_ts_ns: int,
    exchange_engine_ts_ns: int,
    bids: Iterable[RawLevel],
    asks: Iterable[RawLevel],
    update_id: int,
    previous_update_id: int,
    cross_sequence: int = 0,
    previous_cross_sequence: int = 0,
    first_update_id: int = 0,
    restart_snapshot: bool = False,
    sequence_gap: bool = False,
) -> dict[str, Any]:
    return {
        "kind": KIND_BOOK_SNAPSHOT if snapshot else KIND_BOOK_DELTA,
        "venue": venue,
        "symbol": symbol,
        "depth": int(depth),
        "local_receive_ts_ns": int(local_receive_ts_ns),
        "exchange_system_ts_ns": int(exchange_system_ts_ns),
        "exchange_engine_ts_ns": int(exchange_engine_ts_ns),
        "bids": [list(level) for level in bids],
        "asks": [list(level) for level in asks],
        "update_id": int(update_id),
        "previous_update_id": int(previous_update_id),
        "first_update_id": int(first_update_id),
        "cross_sequence": int(cross_sequence),
        "previous_cross_sequence": int(previous_cross_sequence),
        "restart_snapshot": bool(restart_snapshot),
        "sequence_gap": bool(sequence_gap),
    }


def trade_row(
    *,
    venue: str,
    symbol: str,
    local_receive_ts_ns: int,
    exchange_ts_ns: int,
    trade_id: str,
    price: float,
    qty: float,
    side: str,
) -> dict[str, Any]:
    if side not in SIDES:
        raise SchemaError(f"trade side must be Buy or Sell, got {side!r}")
    return {
        "kind": KIND_TRADE,
        "venue": venue,
        "symbol": symbol,
        "local_receive_ts_ns": int(local_receive_ts_ns),
        "exchange_ts_ns": int(exchange_ts_ns),
        "trade_id": str(trade_id),
        "price": float(price),
        "qty": float(qty),
        "side": side,
    }


def ticker_row(
    *,
    venue: str,
    symbol: str,
    local_receive_ts_ns: int,
    exchange_system_ts_ns: int,
    message_type: str,
    values: Mapping[str, float | int],
    cross_sequence: int = 0,
) -> dict[str, Any]:
    unknown = sorted(set(values) - set(TICKER_VALUE_FIELDS))
    if unknown:
        raise SchemaError(f"ticker values outside the contract: {unknown}")
    return {
        "kind": KIND_TICKER,
        "venue": venue,
        "symbol": symbol,
        "local_receive_ts_ns": int(local_receive_ts_ns),
        "exchange_system_ts_ns": int(exchange_system_ts_ns),
        "message_type": message_type,
        "cross_sequence": int(cross_sequence),
        "values": dict(values),
    }


def liquidation_row(
    *,
    venue: str,
    symbol: str,
    local_receive_ts_ns: int,
    exchange_system_ts_ns: int,
    exchange_ts_ns: int,
    position_side: str,
    qty: float,
    bankruptcy_price: float,
) -> dict[str, Any]:
    if position_side not in SIDES:
        raise SchemaError(f"liquidation side must be Buy or Sell, got {position_side!r}")
    return {
        "kind": KIND_LIQUIDATION,
        "venue": venue,
        "symbol": symbol,
        "local_receive_ts_ns": int(local_receive_ts_ns),
        "exchange_system_ts_ns": int(exchange_system_ts_ns),
        "exchange_ts_ns": int(exchange_ts_ns),
        "position_side": position_side,
        "qty": float(qty),
        "bankruptcy_price": float(bankruptcy_price),
    }


def kline_row(
    *,
    venue: str,
    symbol: str,
    interval: str,
    local_receive_ts_ns: int,
    exchange_system_ts_ns: int,
    start_ms: int,
    end_ms: int,
    open: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    turnover: float,
    confirmed: bool,
) -> dict[str, Any]:
    """One venue candle as pushed; `confirmed` says the interval has closed.
    The venue pushes the open candle repeatedly as it changes; the last row with
    `confirmed` true is the candle."""

    return {
        "kind": KIND_KLINE,
        "venue": venue,
        "symbol": symbol,
        "interval": interval,
        "local_receive_ts_ns": int(local_receive_ts_ns),
        "exchange_system_ts_ns": int(exchange_system_ts_ns),
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "open": float(open),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume),
        "turnover": float(turnover),
        "confirmed": bool(confirmed),
    }


def snapshot_payload(
    *,
    kind: str,
    venue: str,
    market: str,
    recorded_at_ns: int,
    source: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if kind not in SNAPSHOT_KINDS:
        raise SchemaError(f"unknown snapshot kind {kind!r}")
    return {
        "kind": kind,
        "venue": venue,
        "market": market,
        # Bybit's word for its market; kept so schema-1 readers find it.
        "category": market,
        "schema": SCHEMA_VERSION,
        "recorded_at_ns": int(recorded_at_ns),
        "source": source,
        "rows": rows,
    }


# --------------------------------------------------------------- typed rows


@dataclass(frozen=True, slots=True)
class BookRow:
    venue: str
    symbol: str
    snapshot: bool
    depth: int
    local_receive_ts_ns: int
    exchange_system_ts_ns: int
    exchange_engine_ts_ns: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    update_id: int
    previous_update_id: int
    first_update_id: int
    cross_sequence: int
    previous_cross_sequence: int
    restart_snapshot: bool
    sequence_gap: bool

    @property
    def kind(self) -> str:
        return KIND_BOOK_SNAPSHOT if self.snapshot else KIND_BOOK_DELTA


@dataclass(frozen=True, slots=True)
class TradeRow:
    venue: str
    symbol: str
    local_receive_ts_ns: int
    exchange_ts_ns: int
    trade_id: str
    price: float
    qty: float
    side: str

    kind = KIND_TRADE


@dataclass(frozen=True, slots=True)
class TickerRow:
    venue: str
    symbol: str
    local_receive_ts_ns: int
    exchange_system_ts_ns: int
    message_type: str
    cross_sequence: int
    values: Mapping[str, float | int]

    kind = KIND_TICKER


@dataclass(frozen=True, slots=True)
class LiquidationRow:
    venue: str
    symbol: str
    local_receive_ts_ns: int
    exchange_system_ts_ns: int
    exchange_ts_ns: int
    position_side: str
    qty: float
    bankruptcy_price: float

    kind = KIND_LIQUIDATION


@dataclass(frozen=True, slots=True)
class KlineRow:
    venue: str
    symbol: str
    interval: str
    local_receive_ts_ns: int
    exchange_system_ts_ns: int
    start_ms: int
    end_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float
    confirmed: bool

    kind = KIND_KLINE


Row = Union[BookRow, TradeRow, TickerRow, LiquidationRow, KlineRow]


def _int(obj: Mapping[str, Any], name: str, default: int | None = 0) -> int:
    value = obj.get(name)
    if value is None:
        if default is None:
            raise SchemaError(f"row lacks {name}")
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"{name} is not an integer: {value!r}") from exc


def _levels(raw: Any, name: str) -> tuple[Level, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SchemaError(f"{name} is not a list")
    levels = []
    for level in raw:
        try:
            price, size = level[0], level[1]
            levels.append((float(price), float(size)))
        except (TypeError, ValueError, IndexError) as exc:
            raise SchemaError(f"{name} has a malformed level {level!r}") from exc
    return tuple(levels)


def parse_row(obj: Mapping[str, Any], *, default_venue: str) -> Row:
    """One JSON object from the tape as a typed row.

    `default_venue` fills schema-1 rows, which carry no venue of their own.
    Rows with an unknown kind raise; callers decide whether to skip them.
    """

    kind = obj.get("kind")
    venue = str(obj.get("venue") or default_venue)
    symbol = str(obj.get("symbol") or "")
    if not symbol:
        raise SchemaError("row lacks a symbol")
    received = _int(obj, "local_receive_ts_ns", None)
    if kind in (KIND_BOOK_SNAPSHOT, KIND_BOOK_DELTA):
        return BookRow(
            venue=venue,
            symbol=symbol,
            snapshot=kind == KIND_BOOK_SNAPSHOT,
            depth=_int(obj, "depth"),
            local_receive_ts_ns=received,
            exchange_system_ts_ns=_int(obj, "exchange_system_ts_ns"),
            exchange_engine_ts_ns=_int(obj, "exchange_engine_ts_ns"),
            bids=_levels(obj.get("bids"), "bids"),
            asks=_levels(obj.get("asks"), "asks"),
            update_id=_int(obj, "update_id"),
            previous_update_id=_int(obj, "previous_update_id"),
            first_update_id=_int(obj, "first_update_id"),
            cross_sequence=_int(obj, "cross_sequence"),
            previous_cross_sequence=_int(obj, "previous_cross_sequence"),
            restart_snapshot=bool(obj.get("restart_snapshot", False)),
            sequence_gap=bool(obj.get("sequence_gap", False)),
        )
    if kind == KIND_TRADE:
        side = str(obj.get("side") or "")
        if side not in SIDES:
            raise SchemaError(f"trade side must be Buy or Sell, got {side!r}")
        return TradeRow(
            venue=venue,
            symbol=symbol,
            local_receive_ts_ns=received,
            exchange_ts_ns=_int(obj, "exchange_ts_ns"),
            trade_id=str(obj.get("trade_id") or ""),
            price=float(obj.get("price") or 0.0),
            qty=float(obj.get("qty") or 0.0),
            side=side,
        )
    if kind == KIND_TICKER:
        raw_values = obj.get("values")
        if not isinstance(raw_values, Mapping):
            raise SchemaError("ticker row lacks values")
        values: dict[str, float | int] = {}
        for name, value in raw_values.items():
            if name not in TICKER_VALUE_FIELDS:
                raise SchemaError(f"ticker value outside the contract: {name}")
            values[name] = int(value) if name in TICKER_INT_FIELDS else float(value)
        return TickerRow(
            venue=venue,
            symbol=symbol,
            local_receive_ts_ns=received,
            exchange_system_ts_ns=_int(obj, "exchange_system_ts_ns"),
            message_type=str(obj.get("message_type") or ""),
            cross_sequence=_int(obj, "cross_sequence"),
            values=values,
        )
    if kind == KIND_LIQUIDATION:
        side = str(obj.get("position_side") or "")
        if side not in SIDES:
            raise SchemaError(f"liquidation side must be Buy or Sell, got {side!r}")
        return LiquidationRow(
            venue=venue,
            symbol=symbol,
            local_receive_ts_ns=received,
            exchange_system_ts_ns=_int(obj, "exchange_system_ts_ns"),
            exchange_ts_ns=_int(obj, "exchange_ts_ns"),
            position_side=side,
            qty=float(obj.get("qty") or 0.0),
            bankruptcy_price=float(obj.get("bankruptcy_price") or 0.0),
        )
    if kind == KIND_KLINE:
        return KlineRow(
            venue=venue,
            symbol=symbol,
            interval=str(obj.get("interval") or ""),
            local_receive_ts_ns=received,
            exchange_system_ts_ns=_int(obj, "exchange_system_ts_ns"),
            start_ms=_int(obj, "start_ms"),
            end_ms=_int(obj, "end_ms"),
            open=float(obj.get("open") or 0.0),
            high=float(obj.get("high") or 0.0),
            low=float(obj.get("low") or 0.0),
            close=float(obj.get("close") or 0.0),
            volume=float(obj.get("volume") or 0.0),
            turnover=float(obj.get("turnover") or 0.0),
            confirmed=bool(obj.get("confirmed", False)),
        )
    raise SchemaError(f"unknown row kind {kind!r}")
