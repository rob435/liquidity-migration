"""Join engine WAL fills to Bybit account receipts without touching the venue."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import struct
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from liquidity_migration.core.venue_realm import REALM_REST_ENDPOINTS, VenueRealm

WAL_MAGIC = b"EWAL0001"
FLAT_QTY = Decimal("1e-9")
MONEY_TOLERANCE = Decimal("1e-8")
CAPTURE_MAX_WINDOW_MS = 7 * 86_400_000
CAPTURE_SOURCE_CONTRACT = {
    "execution": (
        "/v5/execution/list",
        {"category": "linear", "settleCoin": "USDT", "limit": "100"},
    ),
    "closed_pnl": (
        "/v5/position/closed-pnl",
        {"category": "linear", "limit": "100"},
    ),
    "transaction": (
        "/v5/account/transaction-log",
        {"accountType": "UNIFIED", "category": "linear", "currency": "USDT", "limit": "50"},
    ),
}
ACTIVATION_RECEIPT_FIELDS = (
    "commit",
    "sha256",
    "launcher_sha256",
    "control_helper_sha256",
    "controls_sudoers_sha256",
    "telegram_bot_sha256",
)
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EvidenceError(ValueError):
    """The supplied evidence cannot be read without guessing."""


@dataclass(frozen=True)
class WalRecordRow:
    sequence: int
    segment: int
    record: dict[str, Any]


@dataclass(frozen=True)
class WalSegmentIdentity:
    index: int
    path: str
    size: int
    sha256: str
    records: int
    torn_tail: bool


@dataclass(frozen=True)
class WalRead:
    records: tuple[WalRecordRow, ...]
    segments: tuple[WalSegmentIdentity, ...]
    complete_family: bool
    damaged: bool
    issues: tuple[str, ...]


@dataclass(frozen=True)
class EngineBoot:
    sequence: int
    version: str
    config_sha256: str
    wall_ts_ms: int | None


@dataclass(frozen=True)
class EngineOrder:
    client_order_id: str
    strategy: str
    symbol: str
    side: str
    qty: Decimal
    reduce_only: bool
    boot: EngineBoot | None


@dataclass(frozen=True)
class EngineFill:
    sequence: int
    exec_id: str
    client_order_id: str
    venue_order_id: str | None
    strategy: str | None
    symbol: str | None
    side: str
    qty: Decimal | None
    px: Decimal | None
    fee: Decimal | None
    is_maker: bool | None
    venue_ts_ms: int | None
    source: str
    boot: EngineBoot | None
    order_boot: EngineBoot | None


@dataclass
class EngineTrade:
    sleeve: str
    symbol: str
    fills: list[EngineFill] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def opened_ms(self) -> int | None:
        return self.fills[0].venue_ts_ms if self.fills else None

    @property
    def closed_ms(self) -> int | None:
        return self.fills[-1].venue_ts_ms if self.fills else None


@dataclass(frozen=True)
class WalAccounting:
    orders: Mapping[str, EngineOrder]
    boots: tuple[EngineBoot, ...]
    fills: tuple[EngineFill, ...]
    closed_trades: tuple[EngineTrade, ...]
    open_trades: tuple[EngineTrade, ...]
    issues: tuple[str, ...]


@dataclass(frozen=True)
class VenueCapture:
    manifest: Mapping[str, Any] | None
    executions: tuple[Mapping[str, Any], ...]
    closed_pnl: tuple[Mapping[str, Any], ...]
    transactions: tuple[Mapping[str, Any], ...]
    sha256: str
    path: str
    issues: tuple[str, ...]


@dataclass(frozen=True)
class DeploymentEvidence:
    receipt_path: str
    receipt_sha256: str
    receipt: Mapping[str, str]
    engine_binary_path: str
    engine_binary_sha256: str
    engine_config_path: str
    engine_config_sha256: str
    expected_commit: str
    expected_binary_sha256: str
    expected_config_sha256: str
    issues: tuple[str, ...]


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        out = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return out if out.is_finite() else None


def _integer(value: Any) -> int | None:
    number = _decimal(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def _retention_start_ms(server_time_ms: int) -> int:
    server_time = dt.datetime.fromtimestamp(server_time_ms / 1000, tz=dt.timezone.utc)
    if server_time.year <= 2:
        return 0
    try:
        boundary = server_time.replace(year=server_time.year - 2)
    except ValueError:
        boundary = server_time.replace(year=server_time.year - 2, day=28)
    return int(boundary.timestamp() * 1000)


def _close(left: Decimal | None, right: Decimal | None, *, money: bool = False) -> bool:
    if left is None or right is None:
        return False
    tolerance = MONEY_TOLERANCE if money else Decimal("1e-12")
    return abs(left - right) <= tolerance


def crc32c(payload: bytes) -> int:
    """CRC-32C in the same reflected form used by the Rust WAL crate."""

    crc = 0xFFFFFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def _load_json(payload: bytes | str, source: str) -> dict[str, Any]:
    try:
        value = json.loads(payload, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{source}: malformed JSON ({exc})") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{source}: expected a JSON object")
    return value


def _require_identity(label: str, value: str, pattern: re.Pattern[str]) -> str:
    if not pattern.fullmatch(value):
        raise EvidenceError(f"{label} must be exact lowercase hexadecimal")
    return value


def read_deployment_evidence(
    receipt_path: Path,
    engine_binary_path: Path,
    engine_config_path: Path,
    *,
    expected_commit: str,
    expected_binary_sha256: str,
    expected_config_sha256: str,
) -> DeploymentEvidence:
    expected_commit = _require_identity("expected commit", expected_commit, COMMIT_RE)
    expected_binary_sha256 = _require_identity(
        "expected engine binary SHA-256", expected_binary_sha256, SHA256_RE
    )
    expected_config_sha256 = _require_identity(
        "expected engine config SHA-256", expected_config_sha256, SHA256_RE
    )

    resolved_receipt = receipt_path.expanduser().resolve()
    receipt_raw = resolved_receipt.read_bytes()
    if not receipt_raw.endswith(b"\n") or b"\r" in receipt_raw:
        raise EvidenceError(f"{resolved_receipt}: activation receipt is not exact newline text")
    try:
        lines = receipt_raw.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise EvidenceError(f"{resolved_receipt}: activation receipt is not ASCII") from exc
    if len(lines) != len(ACTIVATION_RECEIPT_FIELDS):
        raise EvidenceError(
            f"{resolved_receipt}: activation receipt must have exactly "
            f"{len(ACTIVATION_RECEIPT_FIELDS)} lines"
        )
    receipt: dict[str, str] = {}
    for line, field_name in zip(lines, ACTIVATION_RECEIPT_FIELDS, strict=True):
        prefix = f"{field_name}="
        if not line.startswith(prefix):
            raise EvidenceError(
                f"{resolved_receipt}: activation receipt field {field_name!r} is missing or out of order"
            )
        value = line.removeprefix(prefix)
        pattern = COMMIT_RE if field_name == "commit" else SHA256_RE
        receipt[field_name] = _require_identity(
            f"activation receipt {field_name}", value, pattern
        )

    resolved_binary = engine_binary_path.expanduser().resolve()
    resolved_config = engine_config_path.expanduser().resolve()
    binary_sha256 = hashlib.sha256(resolved_binary.read_bytes()).hexdigest()
    config_sha256 = hashlib.sha256(resolved_config.read_bytes()).hexdigest()
    issues: list[str] = []
    if receipt["commit"] != expected_commit:
        issues.append("activation receipt commit differs from the expected exact commit")
    if receipt["sha256"] != expected_binary_sha256:
        issues.append("activation receipt engine digest differs from the expected binary SHA-256")
    if binary_sha256 != receipt["sha256"]:
        issues.append("engine binary SHA-256 differs from the activation receipt")
    if binary_sha256 != expected_binary_sha256:
        issues.append("engine binary SHA-256 differs from the expected binary SHA-256")
    if config_sha256 != expected_config_sha256:
        issues.append("engine config SHA-256 differs from the expected config SHA-256")
    return DeploymentEvidence(
        receipt_path=str(resolved_receipt),
        receipt_sha256=hashlib.sha256(receipt_raw).hexdigest(),
        receipt=receipt,
        engine_binary_path=str(resolved_binary),
        engine_binary_sha256=binary_sha256,
        engine_config_path=str(resolved_config),
        engine_config_sha256=config_sha256,
        expected_commit=expected_commit,
        expected_binary_sha256=expected_binary_sha256,
        expected_config_sha256=expected_config_sha256,
        issues=tuple(issues),
    )


def _segment_candidates(family: Path) -> tuple[list[tuple[int, Path]], bool, list[str]]:
    found: list[tuple[int, Path]] = []
    issues: list[str] = []
    if family.exists():
        found.append((1, family))
    prefix = f"{family.name}."
    if family.parent.exists():
        for child in family.parent.iterdir():
            suffix = child.name.removeprefix(prefix) if child.name.startswith(prefix) else ""
            if len(suffix) == 6 and suffix.isdigit() and int(suffix) >= 2:
                found.append((int(suffix), child))
    found.sort(key=lambda item: item[0])
    indices = [index for index, _ in found]
    complete = bool(indices) and indices == list(range(1, indices[-1] + 1))
    if not found:
        issues.append(f"WAL family {family} has no segments")
    elif not complete:
        issues.append(f"WAL family has missing segments: found {indices}")
    return found, complete, issues


def _read_segment(path: Path, index: int) -> tuple[list[dict[str, Any]], bool, bytes]:
    raw = path.read_bytes()
    if len(raw) < len(WAL_MAGIC) or raw[: len(WAL_MAGIC)] != WAL_MAGIC:
        raise EvidenceError(f"{path}: not an engine WAL (magic does not match)")
    records: list[dict[str, Any]] = []
    offset = len(WAL_MAGIC)
    torn = False
    while offset < len(raw):
        if len(raw) - offset < 8:
            torn = True
            break
        payload_len, expected_crc = struct.unpack_from("<II", raw, offset)
        frame_offset = offset
        offset += 8
        if payload_len == 0 or payload_len > len(raw) - offset:
            torn = True
            break
        payload = raw[offset : offset + payload_len]
        actual_crc = crc32c(payload)
        if actual_crc != expected_crc:
            raise EvidenceError(
                f"{path}: WAL frame checksum differs at byte {frame_offset} "
                f"(wanted {expected_crc:#010x}, got {actual_crc:#010x})"
            )
        records.append(_load_json(payload, f"{path}:frame@{frame_offset}"))
        offset += payload_len
    if index >= 2 and records and records[0].get("kind") != "segment_base":
        return [], torn, raw
    return records, torn, raw


def read_wal_family(path: Path) -> WalRead:
    family = path.expanduser().resolve()
    candidates, complete, issues = _segment_candidates(family)
    rows: list[WalRecordRow] = []
    identities: list[WalSegmentIdentity] = []
    damaged = False
    for index, segment_path in candidates:
        records, torn, raw = _read_segment(segment_path, index)
        trusted = index == 1 or bool(records)
        if not trusted:
            issues.append(f"ignored untrusted rotation segment {segment_path}")
            continue
        damaged |= torn
        identities.append(
            WalSegmentIdentity(
                index=index,
                path=str(segment_path),
                size=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                records=len(records),
                torn_tail=torn,
            )
        )
        for record in records:
            rows.append(WalRecordRow(len(rows) + 1, index, record))
    if damaged:
        issues.append("a WAL segment has bytes after its last complete CRC-checked frame")
    return WalRead(tuple(rows), tuple(identities), complete, damaged, tuple(issues))


def _table_name(table: Sequence[Any], raw_id: Any) -> str | None:
    index = _integer(raw_id)
    if index is None or index < 0 or index >= len(table):
        return None
    value = str(table[index]).strip()
    return value or None


def _enum_payload(value: Any, variant: str) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    payload = value.get(variant)
    if payload is None:
        payload = value.get(variant.lower())
    return payload if isinstance(payload, Mapping) else None


def _put_unique(
    destination: dict[str, Any], key: str, value: Any, label: str, issues: list[str]
) -> None:
    if not key:
        issues.append(f"{label} has an empty identity")
        return
    known = destination.get(key)
    if known is None:
        destination[key] = value
    elif known != value:
        issues.append(f"{label} {key!r} has conflicting WAL rows")


def parse_wal_accounting(wal: WalRead, sleeve: str = "long") -> WalAccounting:
    strategies: list[Any] = []
    symbols: list[Any] = []
    orders: dict[str, EngineOrder] = {}
    acks: dict[str, str] = {}
    boots: list[EngineBoot] = []
    active_boot: EngineBoot | None = None
    raw_fills: list[
        tuple[int, Mapping[str, Any], str, list[Any], EngineBoot | None]
    ] = []
    issues = list(wal.issues)

    def learn_order(request: Mapping[str, Any], *, restatement: bool = False) -> None:
        client_id = str(request.get("client_order_id") or "")
        strategy = _table_name(strategies, request.get("strategy"))
        symbol = _table_name(symbols, request.get("symbol"))
        qty = _decimal(request.get("qty"))
        side = str(request.get("side") or "")
        if strategy is None or symbol is None or qty is None or qty <= 0 or side not in {"Buy", "Sell"}:
            issues.append(f"order {client_id or '<blank>'!r} has unresolved or invalid WAL fields")
            return
        known = orders.get(client_id)
        order_boot = known.boot if restatement and known is not None else active_boot
        _put_unique(
            orders,
            client_id,
            EngineOrder(
                client_order_id=client_id,
                strategy=strategy,
                symbol=symbol,
                side=side,
                qty=qty,
                reduce_only=bool(request.get("reduce_only", False)),
                boot=order_boot,
            ),
            "client order id",
            issues,
        )

    for row in wal.records:
        record = row.record
        kind = record.get("kind")
        if kind == "boot":
            active_boot = EngineBoot(
                sequence=row.sequence,
                version=str(record.get("version") or ""),
                config_sha256=str(record.get("config_sha256") or ""),
                wall_ts_ms=_integer(record.get("wall_ts_ms")),
            )
            boots.append(active_boot)
            continue
        if kind in {"names", "segment_base"}:
            next_strategies = record.get("strategies")
            next_symbols = record.get("symbols")
            if isinstance(next_strategies, list) and isinstance(next_symbols, list):
                strategies = list(next_strategies)
                symbols = list(next_symbols)
            else:
                issues.append(f"WAL sequence {row.sequence} has malformed name tables")
            if kind == "segment_base":
                open_orders = record.get("open_orders")
                if isinstance(open_orders, list):
                    for open_order in open_orders:
                        if isinstance(open_order, Mapping) and isinstance(open_order.get("request"), Mapping):
                            learn_order(open_order["request"], restatement=True)
            continue
        if kind == "order_sent":
            request = record.get("request")
            if isinstance(request, Mapping):
                learn_order(request)
            else:
                issues.append(f"WAL sequence {row.sequence} has no readable order request")
            continue
        if kind == "order_update":
            update = record.get("update")
            ack = _enum_payload(update, "Ack")
            if ack is not None:
                client_id = str(ack.get("client_order_id") or "")
                venue_id = str(ack.get("venue_order_id") or "")
                _put_unique(acks, client_id, venue_id, "order acknowledgement", issues)
                continue
            fill_payload = _enum_payload(update, "Fill")
            if fill_payload is not None:
                raw_fills.append(
                    (row.sequence, fill_payload, "order_update", list(symbols), active_boot)
                )
            continue
        if kind == "recovered_fill":
            raw_fills.append(
                (row.sequence, record, "recovered_fill", list(symbols), active_boot)
            )

    fills: list[EngineFill] = []
    seen_exec_ids: dict[str, EngineFill] = {}
    for sequence, payload, source, fill_symbols, boot in raw_fills:
        client_id = str(payload.get("client_order_id") or "")
        order = orders.get(client_id)
        symbol = _table_name(fill_symbols, payload.get("symbol"))
        if symbol is None and order is not None and order.strategy == sleeve:
            issues.append(
                f"{sleeve} fill {str(payload.get('exec_id') or '<blank>')} at WAL sequence "
                f"{sequence} has an unresolved symbol"
            )
        if symbol is not None and order is not None and symbol != order.symbol:
            issues.append(
                f"fill {str(payload.get('exec_id') or '<blank>')} symbol differs from its WAL order"
            )
        if symbol is None and order is not None:
            symbol = order.symbol
        side = str(payload.get("side") or "")
        if order is not None and side != order.side:
            issues.append(
                f"fill {str(payload.get('exec_id') or '<blank>')} side differs from its WAL order"
            )
        fee = _decimal(payload.get("fee"))
        if payload.get("fee_known") is False:
            fee = None
        engine_fill = EngineFill(
            sequence=sequence,
            exec_id=str(payload.get("exec_id") or ""),
            client_order_id=client_id,
            venue_order_id=acks.get(client_id),
            strategy=order.strategy if order is not None else None,
            symbol=symbol,
            side=side,
            qty=_decimal(payload.get("qty")),
            px=_decimal(payload.get("px")),
            fee=fee,
            is_maker=payload.get("is_maker") if isinstance(payload.get("is_maker"), bool) else None,
            venue_ts_ms=_integer(payload.get("venue_ts_ms")),
            source=source,
            boot=boot,
            order_boot=order.boot if order is not None else None,
        )
        if engine_fill.exec_id:
            if engine_fill.exec_id in seen_exec_ids:
                issues.append(f"execution id {engine_fill.exec_id!r} is repeated in the WAL")
            else:
                seen_exec_ids[engine_fill.exec_id] = engine_fill
        fills.append(engine_fill)

    closed, opened = _group_closed_trades(fills, sleeve, issues)
    return WalAccounting(
        orders=orders,
        boots=tuple(boots),
        fills=tuple(fills),
        closed_trades=tuple(closed),
        open_trades=tuple(opened),
        issues=tuple(issues),
    )


def _valid_fill(fill: EngineFill) -> bool:
    return (
        bool(fill.exec_id)
        and bool(fill.client_order_id)
        and bool(fill.venue_order_id)
        and fill.symbol is not None
        and fill.side in {"Buy", "Sell"}
        and fill.qty is not None
        and fill.qty > 0
        and fill.px is not None
        and fill.px > 0
        and fill.fee is not None
        and fill.is_maker is not None
        and fill.venue_ts_ms is not None
        and fill.venue_ts_ms > 0
        and fill.boot is not None
    )


def _group_closed_trades(
    fills: Sequence[EngineFill], sleeve: str, parent_issues: list[str]
) -> tuple[list[EngineTrade], list[EngineTrade]]:
    current: dict[str, tuple[Decimal, EngineTrade]] = {}
    closed: list[EngineTrade] = []
    for fill in sorted(fills, key=lambda item: item.sequence):
        if fill.strategy != sleeve:
            continue
        if fill.symbol is None or not fill.symbol.strip():
            parent_issues.append(
                f"{sleeve} fill {fill.exec_id or '<blank>'} at WAL sequence "
                f"{fill.sequence} has no resolved symbol"
            )
            continue
        if fill.qty is None or fill.qty <= 0:
            parent_issues.append(
                f"{sleeve} fill {fill.exec_id or '<blank>'} at WAL sequence "
                f"{fill.sequence} has no positive quantity"
            )
            continue
        if fill.side not in {"Buy", "Sell"}:
            parent_issues.append(
                f"{sleeve} fill {fill.exec_id or '<blank>'} at WAL sequence "
                f"{fill.sequence} has invalid side {fill.side!r}"
            )
            continue
        signed = fill.qty if fill.side == "Buy" else -fill.qty
        held, trade = current.get(fill.symbol, (Decimal(0), EngineTrade(sleeve, fill.symbol)))
        if held == 0 and signed < 0:
            parent_issues.append(f"{sleeve} starts a short {fill.symbol} position at WAL sequence {fill.sequence}")
            continue
        if held > 0 and held + signed < -FLAT_QTY:
            trade.issues.append(f"fill {fill.exec_id or '<blank>'} crosses through flat")
        trade.fills.append(fill)
        held += signed
        if abs(held) < FLAT_QTY:
            closed.append(trade)
            current.pop(fill.symbol, None)
        else:
            current[fill.symbol] = (held, trade)
    return closed, [trade for _, trade in current.values()]


def read_venue_capture(path: Path) -> VenueCapture:
    resolved = path.expanduser().resolve()
    raw = resolved.read_bytes()
    manifest: Mapping[str, Any] | None = None
    manifest_count = 0
    executions: list[Mapping[str, Any]] = []
    closed_pnl: list[Mapping[str, Any]] = []
    transactions: list[Mapping[str, Any]] = []
    issues: list[str] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        row = _load_json(line, f"{resolved}:{line_number}")
        kind = row.get("_kind")
        if kind == "capture":
            manifest_count += 1
            manifest = row
        elif kind == "execution":
            executions.append(row)
        elif kind == "closed_pnl":
            closed_pnl.append(row)
        elif kind in {"transaction", "txn"}:
            transactions.append(row)
        else:
            issues.append(f"venue file line {line_number} has unknown _kind {kind!r}")
    if manifest is None:
        issues.append("venue file has no capture-completeness manifest")
    elif manifest_count != 1:
        issues.append(f"venue file has {manifest_count} capture manifests, not exactly one")
    return VenueCapture(
        manifest=manifest,
        executions=tuple(executions),
        closed_pnl=tuple(closed_pnl),
        transactions=tuple(transactions),
        sha256=hashlib.sha256(raw).hexdigest(),
        path=str(resolved),
        issues=tuple(issues),
    )


def _dedupe(
    rows: Iterable[Mapping[str, Any]], key_name: str, label: str, issues: list[str]
) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        key = str(row.get(key_name) or "")
        if not key:
            issues.append(f"{label} row has no {key_name}")
            continue
        known = out.get(key)
        if known is None:
            out[key] = row
        else:
            detail = "conflicting" if known != row else "duplicate"
            issues.append(f"{label} {key!r} has {detail} rows")
    return out


def _manifest_covers(capture: VenueCapture, opened_ms: int | None, closed_ms: int | None) -> list[str]:
    manifest = capture.manifest
    if manifest is None:
        return ["venue capture has no completeness manifest"]
    issues: list[str] = []
    if manifest.get("schema_version") != 1 or manifest.get("complete") is not True:
        issues.append("venue capture is not marked complete under schema 1")
    start = _integer(manifest.get("start_ms"))
    end = _integer(manifest.get("end_ms_exclusive"))
    expected_slices = (
        (end - start + CAPTURE_MAX_WINDOW_MS - 1) // CAPTURE_MAX_WINDOW_MS
        if start is not None and end is not None and end > start
        else None
    )
    if opened_ms is None or closed_ms is None or start is None or end is None:
        issues.append("venue capture or WAL trade has no exact time boundary")
    elif not (start <= opened_ms <= closed_ms < end):
        issues.append("venue capture does not cover the complete WAL trade")
    sources = manifest.get("sources")
    if not isinstance(sources, Mapping):
        issues.append("venue capture has no per-source completion receipts")
    else:
        actual_rows = {
            "execution": len(capture.executions),
            "closed_pnl": len(capture.closed_pnl),
            "transaction": len(capture.transactions),
        }
        if set(sources) != set(CAPTURE_SOURCE_CONTRACT):
            issues.append("venue capture source receipts do not name exactly the required sources")
        for source, (endpoint, params) in CAPTURE_SOURCE_CONTRACT.items():
            receipt = sources.get(source)
            if not isinstance(receipt, Mapping) or receipt.get("complete") is not True:
                issues.append(f"venue capture does not prove complete {source} pagination")
                continue
            if receipt.get("endpoint") != endpoint:
                issues.append(f"venue capture {source} receipt names the wrong endpoint")
            if receipt.get("params") != params:
                issues.append(f"venue capture {source} receipt names the wrong fixed filters")
            expected_rows = _integer(receipt.get("rows"))
            if expected_rows is None or expected_rows != actual_rows[source]:
                issues.append(
                    f"venue capture {source} row count differs: "
                    f"manifest={receipt.get('rows')!s}, file={actual_rows[source]}"
                )
            slices = _integer(receipt.get("slices"))
            pages = _integer(receipt.get("pages"))
            if slices is None or expected_slices is None or slices != expected_slices:
                issues.append(
                    f"venue capture {source} slice count differs: "
                    f"manifest={receipt.get('slices')!s}, expected={expected_slices!s}"
                )
            if pages is None or slices is None or pages < slices:
                issues.append(f"venue capture {source} has an invalid page receipt")
    retention_start = _integer(manifest.get("retention_start_ms"))
    venue_query_start_time = _integer(manifest.get("venue_query_start_time_ms"))
    venue_query_end_time = _integer(manifest.get("venue_query_end_time_ms"))
    expected_retention_start = (
        _retention_start_ms(venue_query_end_time) if venue_query_end_time is not None else None
    )
    if (
        start is None
        or end is None
        or retention_start is None
        or venue_query_start_time is None
        or venue_query_end_time is None
        or venue_query_end_time < venue_query_start_time
        or retention_start != expected_retention_start
        or start < retention_start
        or end > venue_query_start_time
        or end > venue_query_end_time
    ):
        issues.append(
            "venue capture window is not proven inside the finally retained, already-closed interval"
        )
    realm_text = str(manifest.get("realm") or "")
    try:
        realm = VenueRealm(realm_text)
    except ValueError:
        issues.append("venue capture does not name a supported realm")
    else:
        if manifest.get("api_base_url") != REALM_REST_ENDPOINTS[realm]:
            issues.append("venue capture realm and API endpoint differ")
    if manifest.get("credential_set") not in {"read-only", "execution"}:
        issues.append("venue capture does not identify its credential set")
    if not realm_text or not str(manifest.get("user_id") or ""):
        issues.append("venue capture does not identify its realm and venue user")
    return issues


def _field_match(
    issues: list[str], label: str, actual: Any, expected: Any, *, numeric: bool = False, money: bool = False
) -> None:
    if numeric:
        actual_number = _decimal(actual)
        expected_number = _decimal(expected)
        if actual_number is None or expected_number is None or actual_number != expected_number:
            issues.append(f"{label} differs: venue={actual!s}, WAL={expected!s}")
    elif actual != expected:
        issues.append(f"{label} differs: venue={actual!r}, WAL={expected!r}")


def _trade_values(fills: Sequence[EngineFill]) -> tuple[Decimal, Decimal, Decimal] | None:
    if any(fill.qty is None or fill.px is None or fill.fee is None for fill in fills):
        return None
    entry = sum((fill.qty * fill.px for fill in fills if fill.side == "Buy"), Decimal(0))  # type: ignore[operator]
    exit_value = sum((fill.qty * fill.px for fill in fills if fill.side == "Sell"), Decimal(0))  # type: ignore[operator]
    fees = sum((fill.fee for fill in fills if fill.fee is not None), Decimal(0))
    return entry, exit_value, fees


def _transaction_change(row: Mapping[str, Any], issues: list[str], identity: str) -> Decimal | None:
    cash_flow = _decimal(row.get("cashFlow"))
    funding = _decimal(row.get("funding")) or Decimal(0)
    fee = _decimal(row.get("fee")) or Decimal(0)
    change = _decimal(row.get("change"))
    if cash_flow is None or change is None:
        issues.append(f"transaction {identity} has incomplete cash arithmetic")
        return None
    expected = cash_flow + funding - fee
    if not _close(change, expected, money=True):
        issues.append(f"transaction {identity} breaks change = cashFlow + funding - fee")
    return change


def _boot_receipt(boot: EngineBoot | None) -> dict[str, Any] | None:
    if boot is None:
        return None
    return {
        "wal_sequence": boot.sequence,
        "version": boot.version,
        "config_sha256": boot.config_sha256,
        "wall_ts_ms": boot.wall_ts_ms,
    }


def _fill_boot_issues(
    fills: Sequence[EngineFill], sleeve: str, expected_config_sha256: str
) -> list[str]:
    issues: list[str] = []
    for fill in fills:
        if fill.strategy != sleeve:
            continue
        identity = fill.exec_id or "<blank>"
        for role, boot in (
            ("fill-recording", fill.boot),
            ("order-lineage", fill.order_boot),
        ):
            if boot is None:
                if role == "fill-recording":
                    issues.append(
                        f"{sleeve} fill {identity} at WAL sequence {fill.sequence} "
                        "precedes every Boot"
                    )
                else:
                    issues.append(
                        f"WAL order for {sleeve} fill {identity} has no preceding Boot"
                    )
                continue
            if not boot.version:
                issues.append(f"Boot at WAL sequence {boot.sequence} has no engine version")
            if not SHA256_RE.fullmatch(boot.config_sha256):
                issues.append(
                    f"Boot at WAL sequence {boot.sequence} has an invalid config SHA-256"
                )
            if boot.config_sha256 != expected_config_sha256:
                issues.append(
                    f"{sleeve} fill {identity} uses Boot config "
                    f"{boot.config_sha256 or '<blank>'}, not the expected engine config "
                    f"SHA-256, for its {role}"
                )
            if boot.wall_ts_ms is None or boot.wall_ts_ms <= 0:
                issues.append(
                    f"Boot at WAL sequence {boot.sequence} has no positive wall timestamp"
                )
    return issues


def _fill_receipt(fill: EngineFill) -> dict[str, Any]:
    return {
        "wal_sequence": fill.sequence,
        "wal_source": fill.source,
        "exec_id": fill.exec_id,
        "client_order_id": fill.client_order_id,
        "venue_order_id": fill.venue_order_id,
        "symbol": fill.symbol,
        "side": fill.side,
        "qty": fill.qty,
        "price": fill.px,
        "fee": fill.fee,
        "is_maker": fill.is_maker,
        "venue_ts_ms": fill.venue_ts_ms,
        "active_boot": _boot_receipt(fill.boot),
        "order_boot": _boot_receipt(fill.order_boot),
    }


def _execution_receipt(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "exec_id": row.get("execId"),
        "client_order_id": row.get("orderLinkId"),
        "venue_order_id": row.get("orderId"),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "qty": row.get("execQty"),
        "price": row.get("execPrice"),
        "fee": row.get("execFee"),
        "is_maker": row.get("isMaker"),
        "exec_type": row.get("execType"),
        "venue_ts_ms": row.get("execTime"),
        "capture_server_ts_ms": row.get("_server_time_ms"),
    }


def _transaction_receipt(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "transaction_id": row.get("id"),
        "trade_id": row.get("tradeId"),
        "client_order_id": row.get("orderLinkId"),
        "venue_order_id": row.get("orderId"),
        "symbol": row.get("symbol"),
        "category": row.get("category"),
        "currency": row.get("currency"),
        "type": row.get("type"),
        "side": row.get("side"),
        "qty": row.get("qty"),
        "post_size": row.get("size"),
        "price": row.get("tradePrice"),
        "fee": row.get("fee"),
        "funding": row.get("funding"),
        "cash_flow": row.get("cashFlow"),
        "change": row.get("change"),
        "venue_ts_ms": row.get("transactionTime"),
        "capture_server_ts_ms": row.get("_server_time_ms"),
    }


def _closed_pnl_receipt(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "venue_order_id": row.get("orderId"),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "exec_type": row.get("execType"),
        "closed_size": row.get("closedSize"),
        "entry_value": row.get("cumEntryValue"),
        "exit_value": row.get("cumExitValue"),
        "open_fee": row.get("openFee"),
        "close_fee": row.get("closeFee"),
        "closed_pnl": row.get("closedPnl"),
        "fill_count": row.get("fillCount"),
        "updated_ms": row.get("updatedTime"),
        "capture_server_ts_ms": row.get("_server_time_ms"),
    }


def reconcile_trade(
    trade: EngineTrade,
    capture: VenueCapture,
    wal: WalRead,
    wal_accounting_issues: Sequence[str] = (),
) -> dict[str, Any]:
    issues = list(trade.issues)
    issues.extend(wal_accounting_issues)
    issues.extend(capture.issues)
    if not wal.complete_family:
        issues.append("WAL segment family is incomplete")
    if wal.damaged:
        issues.append("WAL family has a torn or damaged tail")
    issues.extend(_manifest_covers(capture, trade.opened_ms, trade.closed_ms))
    if any(not _valid_fill(fill) for fill in trade.fills):
        issues.append("one or more WAL fills lacks an immutable id, order acknowledgement, fee, or fill fact")

    execution_by_id = _dedupe(capture.executions, "execId", "venue execution", issues)
    transaction_by_id = _dedupe(capture.transactions, "id", "venue transaction id", issues)
    unique_transactions = tuple(transaction_by_id.values())
    transaction_by_trade = _dedupe(
        (row for row in unique_transactions if row.get("type") == "TRADE"),
        "tradeId",
        "venue transaction trade id",
        issues,
    )
    matched_executions: list[Mapping[str, Any]] = []
    matched_transactions: list[Mapping[str, Any]] = []
    fill_receipts: list[dict[str, Any]] = []
    for fill in trade.fills:
        execution = execution_by_id.get(fill.exec_id)
        if execution is None:
            issues.append(f"WAL execution {fill.exec_id or '<blank>'} is absent from venue history")
        else:
            matched_executions.append(execution)
            _field_match(
                issues,
                f"execution {fill.exec_id} client order",
                execution.get("orderLinkId"),
                fill.client_order_id,
            )
            _field_match(
                issues,
                f"execution {fill.exec_id} venue order",
                execution.get("orderId"),
                fill.venue_order_id,
            )
            _field_match(issues, f"execution {fill.exec_id} symbol", execution.get("symbol"), fill.symbol)
            _field_match(issues, f"execution {fill.exec_id} side", execution.get("side"), fill.side)
            _field_match(
                issues,
                f"execution {fill.exec_id} quantity",
                execution.get("execQty"),
                fill.qty,
                numeric=True,
            )
            _field_match(
                issues,
                f"execution {fill.exec_id} price",
                execution.get("execPrice"),
                fill.px,
                numeric=True,
            )
            _field_match(
                issues,
                f"execution {fill.exec_id} fee",
                execution.get("execFee"),
                fill.fee,
                numeric=True,
                money=True,
            )
            _field_match(
                issues,
                f"execution {fill.exec_id} time",
                _integer(execution.get("execTime")),
                fill.venue_ts_ms,
            )
            _field_match(
                issues,
                f"execution {fill.exec_id} maker flag",
                execution.get("isMaker"),
                fill.is_maker,
            )
            if execution.get("execType") != "Trade":
                issues.append(
                    f"execution {fill.exec_id} is {execution.get('execType')!r}, not an ordinary trade"
                )

        transaction = transaction_by_trade.get(fill.exec_id)
        if transaction is None:
            issues.append(f"execution {fill.exec_id} has no matching transaction-log TRADE")
        else:
            matched_transactions.append(transaction)
            _field_match(
                issues,
                f"transaction {fill.exec_id} client order",
                transaction.get("orderLinkId"),
                fill.client_order_id,
            )
            _field_match(
                issues,
                f"transaction {fill.exec_id} venue order",
                transaction.get("orderId"),
                fill.venue_order_id,
            )
            _field_match(issues, f"transaction {fill.exec_id} symbol", transaction.get("symbol"), fill.symbol)
            _field_match(issues, f"transaction {fill.exec_id} side", transaction.get("side"), fill.side)
            _field_match(
                issues,
                f"transaction {fill.exec_id} quantity",
                transaction.get("qty"),
                fill.qty,
                numeric=True,
            )
            _field_match(
                issues,
                f"transaction {fill.exec_id} price",
                transaction.get("tradePrice"),
                fill.px,
                numeric=True,
            )
            _field_match(
                issues,
                f"transaction {fill.exec_id} fee",
                transaction.get("fee"),
                fill.fee,
                numeric=True,
                money=True,
            )
            _field_match(
                issues,
                f"transaction {fill.exec_id} time",
                _integer(transaction.get("transactionTime")),
                fill.venue_ts_ms,
            )
            if transaction.get("currency") != "USDT" or transaction.get("category") != "linear":
                issues.append(f"transaction {fill.exec_id} is not a linear USDT receipt")
            _transaction_change(transaction, issues, fill.exec_id)
        fill_receipts.append(
            {
                "wal": _fill_receipt(fill),
                "venue_execution": _execution_receipt(execution),
                "venue_transaction": _transaction_receipt(transaction),
            }
        )

    opened = trade.opened_ms
    closed = trade.closed_ms
    interval_rows = [
        row
        for row in unique_transactions
        if row.get("symbol") == trade.symbol
        and opened is not None
        and closed is not None
        and opened <= (_integer(row.get("transactionTime")) or -1) <= closed
    ]
    interval_trade_ids = {
        str(row.get("tradeId") or "") for row in interval_rows if row.get("type") == "TRADE"
    }
    wal_exec_ids = {fill.exec_id for fill in trade.fills if fill.exec_id}
    if interval_trade_ids != wal_exec_ids:
        issues.append("venue transaction path contains missing or foreign executions")

    interval_execution_ids = {
        str(row.get("execId") or "")
        for row in execution_by_id.values()
        if row.get("symbol") == trade.symbol
        and row.get("execType") == "Trade"
        and opened is not None
        and closed is not None
        and opened <= (_integer(row.get("execTime")) or -1) <= closed
    }
    if interval_execution_ids != wal_exec_ids:
        issues.append("venue execution path contains missing or foreign trade executions")

    ordered_path = sorted(interval_rows, key=lambda row: (_integer(row.get("transactionTime")) or -1, str(row.get("id") or "")))
    trade_times = [
        _integer(row.get("transactionTime"))
        for row in ordered_path
        if row.get("type") == "TRADE"
    ]
    if len(trade_times) != len(set(trade_times)):
        issues.append("same-millisecond trade ordering is ambiguous")
    trade_time_set = set(trade_times)
    current_size = Decimal(0)
    settlement_rows: list[Mapping[str, Any]] = []
    settlement_total = Decimal(0)
    change_total = Decimal(0)
    for row in ordered_path:
        row_type = str(row.get("type") or "")
        identity = str(row.get("tradeId") or row.get("id") or "<blank>")
        change = _transaction_change(row, issues, identity)
        if change is not None:
            change_total += change
        if row_type == "TRADE":
            qty = _decimal(row.get("qty"))
            post_size = _decimal(row.get("size"))
            side = row.get("side")
            if qty is None or qty <= 0 or post_size is None or side not in {"Buy", "Sell"}:
                issues.append(f"transaction trade {identity} has invalid position fields")
                continue
            expected_size = current_size + (qty if side == "Buy" else -qty)
            if not _close(post_size, expected_size):
                issues.append(f"transaction trade {identity} breaks the one-way position path")
            current_size = post_size
        elif row_type == "SETTLEMENT":
            settlement_rows.append(row)
            if _integer(row.get("transactionTime")) in trade_time_set:
                issues.append(f"settlement {identity} shares a timestamp with a trade")
            size = _decimal(row.get("size"))
            funding = _decimal(row.get("funding"))
            if size is None or funding is None or not _close(size, current_size):
                issues.append(f"settlement {identity} does not match the held position")
            elif (
                row.get("side") != "Buy"
                or row.get("currency") != "USDT"
                or row.get("category") != "linear"
            ):
                issues.append(f"settlement {identity} is not for the long USDT position")
            else:
                settlement_total += funding
        else:
            issues.append(f"unexpected transaction type {row_type!r} inside the position interval")
    if not _close(current_size, Decimal(0)):
        issues.append("venue transaction path does not finish flat")

    close_order_ids = {
        fill.venue_order_id for fill in trade.fills if fill.side == "Sell" and fill.venue_order_id
    }
    close_rows: list[Mapping[str, Any]] = []
    for order_id in sorted(close_order_ids):
        candidates = [
            row
            for row in capture.closed_pnl
            if row.get("orderId") == order_id and row.get("symbol") == trade.symbol
        ]
        if len(candidates) != 1:
            issues.append(f"closing venue order {order_id} has {len(candidates)} closed-PnL rows")
            continue
        row = candidates[0]
        close_rows.append(row)
        for field_name in (
            "closedSize",
            "cumEntryValue",
            "cumExitValue",
            "openFee",
            "closeFee",
            "closedPnl",
            "fillCount",
            "updatedTime",
        ):
            if _decimal(row.get(field_name)) is None:
                issues.append(f"closed-PnL {order_id} has no numeric {field_name}")
        order_fills = [fill for fill in trade.fills if fill.venue_order_id == order_id and fill.side == "Sell"]
        _field_match(
            issues,
            f"closed-PnL {order_id} quantity",
            row.get("closedSize") or row.get("qty"),
            sum((fill.qty for fill in order_fills if fill.qty is not None), Decimal(0)),
            numeric=True,
        )
        fill_count = _integer(row.get("fillCount"))
        if fill_count is None or fill_count != len(order_fills):
            issues.append(f"closed-PnL {order_id} fill count differs from the WAL")
        order_exit_value = sum(
            (
                fill.qty * fill.px
                for fill in order_fills
                if fill.qty is not None and fill.px is not None
            ),
            Decimal(0),
        )
        order_close_fee = sum(
            (fill.fee for fill in order_fills if fill.fee is not None),
            Decimal(0),
        )
        _field_match(
            issues,
            f"closed-PnL {order_id} exit value",
            row.get("cumExitValue"),
            order_exit_value,
            numeric=True,
            money=True,
        )
        _field_match(
            issues,
            f"closed-PnL {order_id} close fee",
            row.get("closeFee"),
            order_close_fee,
            numeric=True,
            money=True,
        )
        close_times = [fill.venue_ts_ms for fill in order_fills if fill.venue_ts_ms is not None]
        updated_ms = _integer(row.get("updatedTime"))
        if not close_times or updated_ms is None or updated_ms < max(close_times):
            issues.append(f"closed-PnL {order_id} update time precedes the WAL close")
        if row.get("side") != "Sell" or row.get("execType") != "Trade":
            issues.append(f"closed-PnL {order_id} is not an ordinary long close")

    values = _trade_values(trade.fills)
    accounting: dict[str, Any] = {}
    if values is None:
        issues.append("WAL price-and-fee accounting is incomplete")
    else:
        entry_value, exit_value, wal_fees = values
        gross = exit_value - entry_value
        wal_price_fee_net = gross - wal_fees
        venue_execution_fees = sum(
            (_decimal(row.get("execFee")) or Decimal(0) for row in matched_executions), Decimal(0)
        )
        transaction_fees = sum(
            (_decimal(row.get("fee")) or Decimal(0) for row in matched_transactions), Decimal(0)
        )
        closed_entry_value = sum(
            (_decimal(row.get("cumEntryValue")) or Decimal(0) for row in close_rows), Decimal(0)
        )
        closed_exit_value = sum(
            (_decimal(row.get("cumExitValue")) or Decimal(0) for row in close_rows), Decimal(0)
        )
        closed_open_fees = sum(
            (_decimal(row.get("openFee")) or Decimal(0) for row in close_rows), Decimal(0)
        )
        closed_close_fees = sum(
            (_decimal(row.get("closeFee")) or Decimal(0) for row in close_rows), Decimal(0)
        )
        closed_pnl = sum(
            (_decimal(row.get("closedPnl")) or Decimal(0) for row in close_rows), Decimal(0)
        )
        for label, actual, expected in (
            ("venue execution fee sum", venue_execution_fees, wal_fees),
            ("transaction fee sum", transaction_fees, wal_fees),
            ("closed-PnL entry value", closed_entry_value, entry_value),
            ("closed-PnL exit value", closed_exit_value, exit_value),
            ("closed-PnL fee sum", closed_open_fees + closed_close_fees, wal_fees),
            ("closed-PnL all-in result", closed_pnl, wal_price_fee_net + settlement_total),
        ):
            if not _close(actual, expected, money=True):
                issues.append(f"{label} differs: venue={actual}, WAL-derived={expected}")
        trade_cash_flow = sum(
            (_decimal(row.get("cashFlow")) or Decimal(0) for row in matched_transactions), Decimal(0)
        )
        if not _close(trade_cash_flow, gross, money=True):
            issues.append(f"transaction cash-flow P&L differs: venue={trade_cash_flow}, WAL gross={gross}")
        if not _close(change_total, wal_price_fee_net + settlement_total, money=True):
            issues.append("transaction-log account change does not equal WAL net plus funding")
        accounting = {
            "entry_value_usdt": entry_value,
            "exit_value_usdt": exit_value,
            "gross_usdt": gross,
            "fees_usdt": wal_fees,
            "price_fee_net_usdt": wal_price_fee_net,
            "funding_usdt": settlement_total,
            "venue_confirmed_net_usdt": closed_pnl if not issues else None,
            "transaction_change_usdt": change_total,
            "closed_pnl_usdt": closed_pnl,
        }

    status = "venue_confirmed" if not issues else "not_venue_confirmed"
    return {
        "status": status,
        "sleeve": trade.sleeve,
        "symbol": trade.symbol,
        "opened_ms": trade.opened_ms,
        "closed_ms": trade.closed_ms,
        "wal_execution_ids": [fill.exec_id for fill in trade.fills],
        "venue_execution_ids": sorted(interval_execution_ids),
        "transaction_trade_ids": sorted(interval_trade_ids),
        "client_order_ids": sorted({fill.client_order_id for fill in trade.fills if fill.client_order_id}),
        "venue_order_ids": sorted({fill.venue_order_id for fill in trade.fills if fill.venue_order_id}),
        "fill_count": len(trade.fills),
        "settlement_ids": [str(row.get("id") or "") for row in settlement_rows],
        "fill_receipts": fill_receipts,
        "settlement_receipts": [_transaction_receipt(row) for row in settlement_rows],
        "closed_pnl_receipts": [_closed_pnl_receipt(row) for row in close_rows],
        "accounting": accounting,
        "issues": sorted(set(issues)),
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def reconcile(
    wal_path: Path,
    venue_path: Path,
    sleeve: str = "long",
    *,
    expected_realm: str,
    expected_user_id: str,
    deployment_receipt_path: Path,
    engine_binary_path: Path,
    engine_config_path: Path,
    expected_commit: str,
    expected_binary_sha256: str,
    expected_config_sha256: str,
    trade_execution_id: str | None = None,
) -> dict[str, Any]:
    wal = read_wal_family(wal_path)
    accounting = parse_wal_accounting(wal, sleeve)
    venue = read_venue_capture(venue_path)
    deployment = read_deployment_evidence(
        deployment_receipt_path,
        engine_binary_path,
        engine_config_path,
        expected_commit=expected_commit,
        expected_binary_sha256=expected_binary_sha256,
        expected_config_sha256=expected_config_sha256,
    )
    scope_issues: list[str] = []
    selected_trades = list(accounting.closed_trades)
    selection_issues: list[str] = []
    if trade_execution_id is not None:
        if not trade_execution_id or trade_execution_id.strip() != trade_execution_id:
            selection_issues.append("the requested trade execution id is empty or padded")
            selected_trades = []
        else:
            selected_trades = [
                trade
                for trade in accounting.closed_trades
                if any(fill.exec_id == trade_execution_id for fill in trade.fills)
            ]
            if len(selected_trades) != 1:
                selection_issues.append(
                    f"trade execution id {trade_execution_id!r} matches "
                    f"{len(selected_trades)} closed {sleeve} trades, not exactly one"
                )
                selected_trades = []
    if expected_realm not in {realm.value for realm in VenueRealm}:
        scope_issues.append(f"expected realm {expected_realm!r} is unsupported")
    if not expected_user_id:
        scope_issues.append("expected venue user id is empty")
    if venue.manifest is None:
        scope_issues.append("venue capture cannot be bound to the expected realm and user")
    else:
        if venue.manifest.get("realm") != expected_realm:
            scope_issues.append("venue capture realm differs from the expected realm")
        if str(venue.manifest.get("user_id") or "") != expected_user_id:
            scope_issues.append("venue capture user differs from the expected venue user")
    scope_issues.extend(deployment.issues)
    scope_issues.extend(
        _fill_boot_issues(
            [fill for trade in selected_trades for fill in trade.fills],
            sleeve,
            expected_config_sha256,
        )
    )
    trades = [
        reconcile_trade(trade, venue, wal, (*accounting.issues, *scope_issues))
        for trade in selected_trades
    ]
    confirmed = sum(row["status"] == "venue_confirmed" for row in trades)
    report = {
        "schema_version": 2,
        "generated_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "claim": "closed sleeve trades are venue-confirmed only when immutable fills and all account cash legs reconcile",
        "validity": "valid" if trades and confirmed == len(trades) else "limited",
        "shaped_vs_graded": "This accounting diagnostic was built after seeing earlier venue history; it is not alpha evidence.",
        "scope": {
            "sleeve": sleeve,
            "realm": venue.manifest.get("realm") if venue.manifest else None,
            "venue_user_id": venue.manifest.get("user_id") if venue.manifest else None,
            "expected_realm": expected_realm,
            "expected_venue_user_id": expected_user_id,
            "trade_execution_id": trade_execution_id,
        },
        "deployment_and_authorization": "read-only evidence; no trading or real-money authority",
        "deployment": {
            "activation_receipt_path": deployment.receipt_path,
            "activation_receipt_sha256": deployment.receipt_sha256,
            "activation_receipt": deployment.receipt,
            "engine_binary_path": deployment.engine_binary_path,
            "engine_binary_sha256": deployment.engine_binary_sha256,
            "engine_config_path": deployment.engine_config_path,
            "engine_config_sha256": deployment.engine_config_sha256,
            "expected_commit": deployment.expected_commit,
            "expected_binary_sha256": deployment.expected_binary_sha256,
            "expected_config_sha256": deployment.expected_config_sha256,
            "issues": list(deployment.issues),
        },
        "wal": {
            "family": str(wal_path.expanduser().resolve()),
            "complete_family": wal.complete_family,
            "damaged": wal.damaged,
            "segments": [segment.__dict__ for segment in wal.segments],
            "boots": [_boot_receipt(boot) for boot in accounting.boots],
            "issues": list(accounting.issues),
        },
        "venue_capture": {
            "path": venue.path,
            "sha256": venue.sha256,
            "manifest": venue.manifest,
            "execution_rows": len(venue.executions),
            "closed_pnl_rows": len(venue.closed_pnl),
            "transaction_rows": len(venue.transactions),
            "issues": list(venue.issues),
        },
        "summary": {
            "closed_trades": len(trades),
            "wal_closed_trades": len(accounting.closed_trades),
            "venue_confirmed": confirmed,
            "not_venue_confirmed": len(trades) - confirmed,
            "open_wal_positions": len(accounting.open_trades),
        },
        "trades": trades,
        "issues": selection_issues,
        "non_conclusions": [
            "This does not prove producer-to-target parity or point-in-time model validity.",
            "This does not authorize deployment, mainnet trading, or account ownership.",
            "A missing row is missing evidence, not a zero fee or zero funding payment.",
            "A WAL Boot records config and package version, not commit or binary digest; "
            "the deployment receipt proves the captured installed generation but cannot "
            "cryptographically prove that an older historical Boot ran that binary.",
            "For a recovered fill, order_boot identifies the generation that wrote OrderSent "
            "and active_boot identifies the generation that recorded recovery; both configs "
            "are checked, without treating venue time as WAL sequence.",
        ],
    }
    return _json_value(report)


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    resolved = path.expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        resolved.unlink(missing_ok=True)
        raise


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wal", required=True, type=Path, help="engine WAL family path")
    parser.add_argument("--venue-history", required=True, type=Path, help="captured Bybit account-history JSONL")
    parser.add_argument("--sleeve", default="long", help="exact engine strategy name (default: long)")
    parser.add_argument(
        "--trade-execution-id",
        help="grade only the unique closed trade containing this exact WAL execution id",
    )
    parser.add_argument("--expected-realm", required=True, choices=("demo", "mainnet"))
    parser.add_argument("--expected-user-id", required=True, help="independently expected Bybit account user id")
    parser.add_argument(
        "--deployment-receipt",
        required=True,
        type=Path,
        help="exact six-line activation.complete receipt from the deployed host",
    )
    parser.add_argument("--engine-binary", required=True, type=Path, help="deployed engine binary")
    parser.add_argument("--engine-config", required=True, type=Path, help="exact engine config bytes")
    parser.add_argument("--expected-commit", required=True, help="independently retained full rollout commit")
    parser.add_argument(
        "--expected-binary-sha256",
        required=True,
        help="independently retained rollout engine binary SHA-256",
    )
    parser.add_argument(
        "--expected-config-sha256",
        required=True,
        help="independently retained SHA-256 of the engine config bytes",
    )
    parser.add_argument("--out", required=True, type=Path, help="new mode-0600 JSON evidence report")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = reconcile(
            args.wal,
            args.venue_history,
            args.sleeve,
            expected_realm=args.expected_realm,
            expected_user_id=args.expected_user_id,
            deployment_receipt_path=args.deployment_receipt,
            engine_binary_path=args.engine_binary,
            engine_config_path=args.engine_config,
            expected_commit=args.expected_commit,
            expected_binary_sha256=args.expected_binary_sha256,
            expected_config_sha256=args.expected_config_sha256,
            trade_execution_id=args.trade_execution_id,
        )
        write_report(args.out, report)
    except (EvidenceError, OSError) as exc:
        raise SystemExit(f"accounting evidence is unreadable: {exc}") from None
    summary = report["summary"]
    print(
        f"closed {summary['closed_trades']}; venue-confirmed {summary['venue_confirmed']}; "
        f"not confirmed {summary['not_venue_confirmed']}; report {args.out.expanduser().resolve()}"
    )
    return 0 if summary["closed_trades"] and not summary["not_venue_confirmed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
