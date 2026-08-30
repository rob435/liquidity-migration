"""Independent Exodus input, planning, state, publication, and reporting.

The producer consumes CARRY's typed pre-settlement tape. Its planner is pure:
given the tape, prior Exodus state, engine view, clock, and one resolved config,
it returns the next state and absolute target book without reading files,
environment variables, venue data, or ambient time.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import polars as pl

from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.core.durable_file import durable_atomic_replace, durable_create
from liquidity_migration.data.storage import exclusive_file_lock, write_dataset
from liquidity_migration.policy.execution_environment import EXECUTION_ENVIRONMENT_VALUES
from liquidity_migration.rules.engine_targets import publish_target_book
from liquidity_migration.rules.exodus_short import (
    MIN_MS,
    ExodusShortConfig,
    ExodusShortRecord,
    next_cover_deadline_ts_ms,
    records_from_payload,
    records_to_payload,
    render_exodus_book,
    split_due_covers,
)
from liquidity_migration.runtime.engine_account_health import (
    TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
    require_recent_engine_account,
)
from liquidity_migration.core.env_flags import validate_systemd_invocation_id
from liquidity_migration.strategy.presettlement_events import (
    CarryPresettlementEvent,
    load_carry_presettlement_events,
)
from liquidity_migration.strategy.target_book_evidence import PublishedTargetCyclePayload


_logger = logging.getLogger(__name__)
_CONFIGS_DIR = Path(__file__).resolve().parents[2] / "configs"
_EXODUS_PROFILES: dict[str, Path] = {
    "v1": _CONFIGS_DIR / "lane2_exodus_short_v1.json",
}
EXODUS_PROFILE_CHOICES = tuple(sorted(_EXODUS_PROFILES))
DEFAULT_EXODUS_PROFILE = "v1"
EXODUS_ENGINE_SLEEVE = "exodus"
EXODUS_BOOK_SOURCE = "exodus_short"
EXODUS_CYCLES_DATASET = "exodus_cycles"
EXODUS_STATE_SCHEMA_VERSION = 4
_EXODUS_STATE_NAME = "exodus_state.json"
_LEGACY_EXODUS_STATE_NAME = "exodus_shorts.json"
_EXODUS_STATE_IDENTITY_NAME = "exodus_state_identity.json"
_EXODUS_STATE_IDENTITY_SCHEMA_VERSION = 2
_EXODUS_STATE_IDENTITY_V1_FIELDS = frozenset(
    {
        "schema_version",
        "state_path",
        "genesis_source",
        "legacy_path",
        "legacy_sha256",
    }
)
_EXODUS_STATE_IDENTITY_V2_FIELDS = _EXODUS_STATE_IDENTITY_V1_FIELDS | {"effective_config_sha256"}


@dataclasses.dataclass(frozen=True, slots=True)
class ConfigProvenance:
    """Where one effective Exodus field came from."""

    field: str
    source: str
    source_sha256: str


@dataclasses.dataclass(frozen=True, slots=True)
class ExodusEffectiveConfig:
    """The only config object accepted by the Exodus planner and runner."""

    profile_name: str
    rule: ExodusShortConfig
    environment: str
    event_path: Path
    target_book_path: Path
    engine_heartbeat_path: Path
    expected_account_user_id: str
    invocation_id: str
    entry_leverage: float
    provenance: tuple[ConfigProvenance, ...]

    def __post_init__(self) -> None:
        if self.profile_name not in EXODUS_PROFILE_CHOICES:
            raise ValueError(f"unknown Exodus profile {self.profile_name!r}")
        if self.environment not in EXECUTION_ENVIRONMENT_VALUES:
            raise ValueError("Exodus environment is not registered")
        if (
            not self.event_path.is_absolute()
            or not self.target_book_path.is_absolute()
            or not self.engine_heartbeat_path.is_absolute()
        ):
            raise ValueError("Exodus event, target-book, and heartbeat paths must be absolute")
        if not self.expected_account_user_id.strip():
            raise ValueError("Exodus expected engine account user id is required")
        if self.invocation_id:
            validate_systemd_invocation_id(
                self.invocation_id,
                label="Exodus producer INVOCATION_ID",
            )
        if not math.isfinite(self.entry_leverage) or self.entry_leverage <= 0.0:
            raise ValueError("Exodus entry leverage must be positive and finite")
        fields = [row.field for row in self.provenance]
        required = {
            "profile_name",
            "rule",
            "environment",
            "event_path",
            "target_book_path",
            "engine_heartbeat_path",
            "expected_account_user_id",
            "invocation_id",
            "entry_leverage",
        }
        if len(fields) != len(required) or set(fields) != required:
            raise ValueError("Exodus config provenance is incomplete or duplicated")

    def provenance_dict(self) -> dict[str, dict[str, str]]:
        return {
            row.field: {
                "source": row.source,
                "source_sha256": row.source_sha256,
            }
            for row in self.provenance
        }


def resolve_exodus_effective_config(
    *,
    profile_name: str,
    environment: str,
    event_path: str | Path,
    target_book_path: str | Path,
    engine_heartbeat_path: str | Path,
    expected_account_user_id: str,
    invocation_id: str = "",
    entry_leverage: float,
    operational_profile_path: str | Path,
    operational_profile_sha256: str,
) -> ExodusEffectiveConfig:
    """Resolve files and CLI inputs once, before the first cycle."""

    try:
        rule_path = _EXODUS_PROFILES[profile_name]
    except KeyError:
        raise ValueError(
            f"unknown Exodus profile {profile_name!r}; supported: {', '.join(EXODUS_PROFILE_CHOICES)}"
        ) from None
    rule_bytes = rule_path.read_bytes()
    rule_sha256 = hashlib.sha256(rule_bytes).hexdigest()
    rule = ExodusShortConfig.from_json(rule_path)
    operational_source = str(Path(operational_profile_path).expanduser().resolve())
    raw_event_path = Path(event_path).expanduser()
    raw_target_path = Path(target_book_path).expanduser()
    raw_heartbeat_path = Path(engine_heartbeat_path).expanduser()
    if not str(event_path).strip() or not raw_event_path.is_absolute():
        raise ValueError("Exodus event tape path must be absolute")
    if not str(target_book_path).strip() or not raw_target_path.is_absolute():
        raise ValueError("Exodus target book path must be absolute")
    if not str(engine_heartbeat_path).strip() or not raw_heartbeat_path.is_absolute():
        raise ValueError("Exodus engine heartbeat path must be absolute")
    expected_user_id = str(expected_account_user_id).strip()
    if not expected_user_id:
        raise ValueError("Exodus expected engine account user id is required")
    if len(operational_profile_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in operational_profile_sha256):
        raise ValueError("Exodus operational_profile_sha256 must be 64 lowercase hex")
    return ExodusEffectiveConfig(
        profile_name=profile_name,
        rule=rule,
        environment=environment,
        event_path=raw_event_path.resolve(),
        target_book_path=raw_target_path.resolve(),
        engine_heartbeat_path=raw_heartbeat_path.resolve(),
        expected_account_user_id=expected_user_id,
        invocation_id=str(invocation_id),
        entry_leverage=float(entry_leverage),
        provenance=(
            ConfigProvenance("profile_name", "command line", ""),
            ConfigProvenance("rule", str(rule_path.resolve()), rule_sha256),
            ConfigProvenance("environment", "command line", ""),
            ConfigProvenance("event_path", "command line", ""),
            ConfigProvenance("target_book_path", "command line", ""),
            ConfigProvenance("engine_heartbeat_path", "runtime environment", str(raw_heartbeat_path.resolve())),
            ConfigProvenance("expected_account_user_id", "runtime environment", expected_user_id),
            ConfigProvenance("invocation_id", "service manager", str(invocation_id)),
            ConfigProvenance("entry_leverage", operational_source, operational_profile_sha256),
        ),
    )


@dataclasses.dataclass(frozen=True, slots=True)
class ExodusState:
    """Durable state owned only by the Exodus producer."""

    open_records: tuple[ExodusShortRecord, ...] = ()
    consumed_event_ids: tuple[str, ...] = ()
    entry_closed_ts_ms_by_symbol: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        symbols = [record.symbol for record in self.open_records]
        if symbols != sorted(symbols) or len(symbols) != len(set(symbols)):
            raise ValueError("Exodus open state must be uniquely sorted by symbol")
        if tuple(sorted(self.consumed_event_ids)) != self.consumed_event_ids:
            raise ValueError("Exodus consumed event ids must be uniquely sorted")
        if len(set(self.consumed_event_ids)) != len(self.consumed_event_ids):
            raise ValueError("Exodus consumed event ids contain duplicates")
        if any(not value.startswith("carry-presettlement-") for value in self.consumed_event_ids):
            raise ValueError("Exodus state contains an invalid consumed event id")
        closed_symbols = [symbol for symbol, _ts_ms in self.entry_closed_ts_ms_by_symbol]
        if closed_symbols != sorted(closed_symbols) or len(closed_symbols) != len(set(closed_symbols)):
            raise ValueError("Exodus closed-entry state must be uniquely sorted")
        if any(
            symbol not in symbols or type(ts_ms) is not int or ts_ms <= 0
            for symbol, ts_ms in self.entry_closed_ts_ms_by_symbol
        ):
            raise ValueError("Exodus closed-entry state has an invalid row")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXODUS_STATE_SCHEMA_VERSION,
            "consumed_event_ids": list(self.consumed_event_ids),
            "entry_closed_ts_ms_by_symbol": {symbol: ts_ms for symbol, ts_ms in self.entry_closed_ts_ms_by_symbol},
            "open": records_to_payload(list(self.open_records))["open"],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExodusState":
        # The combined CARRY producer wrote rule-state schemas 1 and 2. A new
        # Exodus root can ingest one copied file without inventing consumption
        # history; the tape and open-symbol check make replay idempotent.
        if set(payload) in ({"open"}, {"schema_version", "open"}):
            records = records_from_payload(payload)
            return cls(open_records=tuple(records))
        if set(payload) == {"schema_version", "consumed_event_ids", "open"}:
            if payload["schema_version"] != 3:
                raise ValueError("unsupported Exodus producer state schema")
            closed_payload: Mapping[str, Any] = {}
        elif set(payload) == {
            "schema_version",
            "consumed_event_ids",
            "entry_closed_ts_ms_by_symbol",
            "open",
        }:
            if payload["schema_version"] != EXODUS_STATE_SCHEMA_VERSION:
                raise ValueError("unsupported Exodus producer state schema")
            raw_closed = payload["entry_closed_ts_ms_by_symbol"]
            if not isinstance(raw_closed, Mapping):
                raise ValueError("Exodus entry_closed_ts_ms_by_symbol must be an object")
            closed_payload = raw_closed
        else:
            raise ValueError("Exodus state has unexpected or missing fields")
        consumed = payload["consumed_event_ids"]
        if not isinstance(consumed, list) or any(not isinstance(row, str) for row in consumed):
            raise ValueError("Exodus consumed_event_ids must be an array of strings")
        records = records_from_payload({"schema_version": 2, "open": payload["open"]})
        return cls(
            open_records=tuple(records),
            consumed_event_ids=tuple(consumed),
            entry_closed_ts_ms_by_symbol=tuple(
                sorted(
                    ((str(symbol), ts_ms) for symbol, ts_ms in closed_payload.items()),
                    key=lambda row: row[0],
                )
            ),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class ExodusDecisionInput:
    now_ms: int
    events: tuple[CarryPresettlementEvent, ...]
    held_symbols: frozenset[str] | None
    working_entry_symbols: frozenset[str] | None
    held_positions: Mapping[str, tuple[str, float, float]] | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ExodusDecisionOutput:
    staged_state: ExodusState
    final_state: ExodusState
    active_records: tuple[ExodusShortRecord, ...]
    cover_records: tuple[ExodusShortRecord, ...]
    opened_event_ids: tuple[str, ...]
    opened_symbols: tuple[str, ...]
    covered_symbols: tuple[str, ...]
    entry_closed_symbols: tuple[str, ...]
    retired_symbols: tuple[str, ...]
    blocked_events: tuple[tuple[str, str], ...]
    target_book_bytes: bytes
    next_cover_ts_ms: int | None


def decide_exodus(
    decision_input: ExodusDecisionInput,
    prior_state: ExodusState,
    config: ExodusEffectiveConfig,
) -> ExodusDecisionOutput:
    """Pure Exodus state transition and exact target-book serialization."""

    if type(decision_input.now_ms) is not int or decision_input.now_ms <= 0:
        raise ValueError("Exodus decision clock must be a positive integer")
    active, covers = split_due_covers(
        list(prior_state.open_records),
        now_ms=decision_input.now_ms,
        cfg=config.rule,
    )
    closed_entries = dict(prior_state.entry_closed_ts_ms_by_symbol)
    retired_symbols: list[str] = []
    retired_records: list[ExodusShortRecord] = []
    newly_closed_entries: list[str] = []
    if (
        decision_input.held_symbols is not None
        and decision_input.working_entry_symbols is not None
        and decision_input.held_positions is not None
    ):
        retained: list[ExodusShortRecord] = []
        for record in active:
            symbol = record.symbol
            if (
                symbol in closed_entries
                and symbol not in decision_input.held_symbols
                and symbol not in decision_input.working_entry_symbols
            ):
                retired_records.append(record)
                retired_symbols.append(symbol)
                continue
            holding = decision_input.held_positions.get(symbol)
            if holding is not None and symbol not in decision_input.working_entry_symbols:
                side, qty, _entry_px = holding
                target_reached = side == "short" and (
                    record.target_qty is None or qty >= record.target_qty * (1.0 - 1e-9)
                )
                if target_reached and symbol not in closed_entries:
                    closed_entries[symbol] = decision_input.now_ms
                    newly_closed_entries.append(symbol)
            retained.append(record)
        active = retained
    active_symbols = {record.symbol for record in active}
    zero_records = sorted(covers + retired_records, key=lambda record: record.symbol)
    cover_symbols = {record.symbol for record in zero_records}
    consumed = set(prior_state.consumed_event_ids)
    opened_ids: list[str] = []
    opened_symbols: list[str] = []
    blocked: list[tuple[str, str]] = []

    for event in decision_input.events:
        if event.event_id in consumed:
            continue
        if event.environment != config.environment:
            blocked.append((event.event_id, "environment_mismatch"))
            continue
        if (
            event.source_profile != config.rule.accepted_source_profile
            or event.source_config_id != config.rule.accepted_source_config_id
        ):
            blocked.append((event.event_id, "incompatible_source"))
            continue
        if decision_input.held_symbols is None or decision_input.working_entry_symbols is None:
            blocked.append((event.event_id, "engine_account_health_unavailable"))
            continue
        if event.symbol in cover_symbols:
            blocked.append((event.event_id, "symbol_cover_pending"))
            continue
        if event.symbol in active_symbols:
            blocked.append((event.event_id, "symbol_already_open"))
            continue
        entry_deadline_ms = event.settlement_ts_ms + (config.rule.entry_valid_minutes_after_settlement - 15) * MIN_MS
        if decision_input.now_ms >= entry_deadline_ms:
            consumed.add(event.event_id)
            blocked.append((event.event_id, "entry_deadline_passed"))
            continue
        if event.carry_side != "long" or event.carry_qty is None or event.mark_px is None:
            consumed.add(event.event_id)
            blocked.append((event.event_id, "no_exact_carry_long"))
            continue
        record = ExodusShortRecord(
            symbol=event.symbol,
            notional_usdt=event.carry_qty * event.mark_px,
            settlement_ts_ms=event.settlement_ts_ms,
            fired_ts_ms=event.fired_ts_ms,
            target_qty=event.carry_qty,
        )
        active.append(record)
        active_symbols.add(event.symbol)
        consumed.add(event.event_id)
        opened_ids.append(event.event_id)
        opened_symbols.append(event.symbol)

    active = sorted(active, key=lambda record: record.symbol)
    covers = sorted(covers, key=lambda record: record.symbol)
    staged_records = sorted(active + zero_records, key=lambda record: record.symbol)
    consumed_ids = tuple(sorted(consumed))
    staged_symbols = {record.symbol for record in staged_records}
    staged_closed = tuple(
        sorted((symbol, ts_ms) for symbol, ts_ms in closed_entries.items() if symbol in staged_symbols)
    )
    staged_state = ExodusState(tuple(staged_records), consumed_ids, staged_closed)

    pending_covers = [
        record
        for record in zero_records
        if decision_input.held_symbols is None
        or decision_input.working_entry_symbols is None
        or record.symbol in decision_input.held_symbols
        or record.symbol in decision_input.working_entry_symbols
    ]
    final_records = tuple(sorted(active + pending_covers, key=lambda record: record.symbol))
    final_symbols = {record.symbol for record in final_records}
    final_state = ExodusState(
        final_records,
        consumed_ids,
        tuple((symbol, ts_ms) for symbol, ts_ms in staged_closed if symbol in final_symbols),
    )
    book = render_exodus_book(
        active,
        cfg=config.rule,
        now_ms=decision_input.now_ms,
        source=EXODUS_BOOK_SOURCE,
        entry_leverage=config.entry_leverage,
        cover_records=zero_records,
        entry_closed_ts_ms_by_symbol=dict(staged_closed),
    ).encode("utf-8")
    return ExodusDecisionOutput(
        staged_state=staged_state,
        final_state=final_state,
        active_records=tuple(active),
        cover_records=tuple(covers),
        opened_event_ids=tuple(opened_ids),
        opened_symbols=tuple(opened_symbols),
        covered_symbols=tuple(record.symbol for record in covers),
        entry_closed_symbols=tuple(sorted(newly_closed_entries)),
        retired_symbols=tuple(sorted(retired_symbols)),
        blocked_events=tuple(blocked),
        target_book_bytes=book,
        next_cover_ts_ms=next_cover_deadline_ts_ms(active, config.rule),
    )


def exodus_state_path(root: str | Path) -> Path:
    return Path(root).expanduser() / _EXODUS_STATE_NAME


def load_exodus_state(root: str | Path) -> ExodusState:
    path = exodus_state_path(root)
    try:
        path.lstat()
    except FileNotFoundError:
        return ExodusState()
    return _load_exodus_state_path(path)


def _load_exodus_state_path(path: Path) -> ExodusState:
    """Read one strict current or combined-producer Exodus state file."""

    snapshot = read_stable_file(
        path,
        label="Exodus producer state",
        reject_empty=True,
        require_single_link=True,
        max_bytes=4 * 1024 * 1024,
    )
    payload = json.loads(snapshot.data)
    if not isinstance(payload, Mapping):
        raise ValueError("Exodus state must contain an object")
    if canonical_json(payload) + b"\n" != snapshot.data:
        raise ValueError("Exodus state is not canonical JSON")
    return ExodusState.from_dict(payload)


def _state_identity_path(root: Path) -> Path:
    return root / _EXODUS_STATE_IDENTITY_NAME


def _effective_config_sha256(config: ExodusEffectiveConfig) -> str:
    """Hash state semantics; restart evidence never changes state ownership."""

    payload = {
        "profile_name": config.profile_name,
        "rule": dataclasses.asdict(config.rule),
        "environment": config.environment,
        "event_path": str(config.event_path),
        "target_book_path": str(config.target_book_path),
        "engine_heartbeat_path": str(config.engine_heartbeat_path),
        "expected_account_user_id": config.expected_account_user_id,
        "entry_leverage": config.entry_leverage,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _read_state_identity(root: Path) -> Mapping[str, Any] | None:
    path = _state_identity_path(root)
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    snapshot = read_stable_file(
        path,
        label="Exodus state identity",
        reject_empty=True,
        require_single_link=True,
        max_bytes=64 * 1024,
    )
    payload = json.loads(snapshot.data)
    if not isinstance(payload, Mapping):
        raise ValueError("Exodus state identity has unexpected or missing fields")
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int:
        raise ValueError("unsupported Exodus state identity schema")
    expected_fields = {
        1: _EXODUS_STATE_IDENTITY_V1_FIELDS,
        _EXODUS_STATE_IDENTITY_SCHEMA_VERSION: _EXODUS_STATE_IDENTITY_V2_FIELDS,
    }.get(schema_version)
    if expected_fields is None:
        raise ValueError("unsupported Exodus state identity schema")
    if set(payload) != expected_fields:
        raise ValueError("Exodus state identity has unexpected or missing fields")
    if payload["state_path"] != str(exodus_state_path(root).resolve()):
        raise ValueError("Exodus state identity names a different state path")
    if payload["genesis_source"] not in {
        "adopted_owned",
        "legacy_import",
        "initialized_empty",
    }:
        raise ValueError("Exodus state identity has an invalid genesis source")
    if not isinstance(payload["legacy_path"], str) or not isinstance(payload["legacy_sha256"], str):
        raise ValueError("Exodus state identity has invalid legacy provenance")
    if schema_version == _EXODUS_STATE_IDENTITY_SCHEMA_VERSION:
        config_sha256 = payload["effective_config_sha256"]
        if (
            not isinstance(config_sha256, str)
            or len(config_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in config_sha256)
        ):
            raise ValueError("Exodus state identity has an invalid config digest")
    if canonical_json(payload) + b"\n" != snapshot.data:
        raise ValueError("Exodus state identity is not canonical JSON")
    return payload


def _create_state_identity(
    root: Path,
    *,
    config: ExodusEffectiveConfig,
    genesis_source: str,
    legacy_path: Path | None = None,
) -> None:
    legacy_source = ""
    legacy_sha256 = ""
    if legacy_path is not None:
        snapshot = read_stable_file(
            legacy_path,
            label="legacy Exodus producer state",
            reject_empty=True,
            require_single_link=True,
            max_bytes=4 * 1024 * 1024,
        )
        legacy_source = str(legacy_path.resolve())
        legacy_sha256 = hashlib.sha256(snapshot.data).hexdigest()
    payload = {
        "schema_version": _EXODUS_STATE_IDENTITY_SCHEMA_VERSION,
        "state_path": str(exodus_state_path(root).resolve()),
        "genesis_source": genesis_source,
        "legacy_path": legacy_source,
        "legacy_sha256": legacy_sha256,
        "effective_config_sha256": _effective_config_sha256(config),
    }
    durable_create(
        _state_identity_path(root),
        canonical_json(payload) + b"\n",
        label="Exodus state identity",
    )


def _require_matching_state_identity(
    root: Path,
    *,
    identity: Mapping[str, Any],
    state: ExodusState,
    config: ExodusEffectiveConfig,
) -> None:
    expected_sha256 = _effective_config_sha256(config)
    if identity["schema_version"] == 1:
        if not _exodus_state_is_empty(state):
            raise RuntimeError(
                "Exodus state identity schema v1 cannot be attributed to the "
                "effective config because the persisted state is nonempty"
            )
        upgraded = dict(identity)
        upgraded["schema_version"] = _EXODUS_STATE_IDENTITY_SCHEMA_VERSION
        upgraded["effective_config_sha256"] = expected_sha256
        durable_atomic_replace(
            _state_identity_path(root),
            canonical_json(upgraded) + b"\n",
            label="Exodus state identity",
        )
        return
    if identity["effective_config_sha256"] != expected_sha256:
        raise RuntimeError("Exodus persisted state belongs to a different effective config")


def _exodus_state_is_empty(state: ExodusState) -> bool:
    return not (state.open_records or state.consumed_event_ids or state.entry_closed_ts_ms_by_symbol)


def _load_or_initialize_exodus_state(
    root: Path,
    *,
    config: ExodusEffectiveConfig,
    held_symbols: frozenset[str] | None,
    working_entry_symbols: frozenset[str] | None,
) -> tuple[ExodusState, str]:
    """Load owned state, import the old CARRY file once, or establish a new root.

    A missing owned file beside an existing engine-visible book is ambiguous.
    The producer leaves that book untouched unless the combined producer's
    strict legacy state is available for a one-time import.
    """

    owned_path = exodus_state_path(root)
    identity = _read_state_identity(root)
    legacy_path = config.event_path.parent / _LEGACY_EXODUS_STATE_NAME
    try:
        owned_path.lstat()
    except FileNotFoundError:
        pass
    else:
        state = _load_exodus_state_path(owned_path)
        if identity is None:
            if not _exodus_state_is_empty(state):
                raise RuntimeError(
                    "Exodus persisted state has no config identity and is nonempty, "
                    "so it cannot be attributed to the effective config"
                )
            try:
                legacy_path.lstat()
            except FileNotFoundError:
                identity_legacy_path = None
            else:
                identity_legacy_path = legacy_path
            _create_state_identity(
                root,
                config=config,
                genesis_source="adopted_owned",
                legacy_path=identity_legacy_path,
            )
        else:
            _require_matching_state_identity(
                root,
                identity=identity,
                state=state,
                config=config,
            )
        return state, "owned"

    if identity is not None:
        raise RuntimeError(
            "Exodus state is missing after this state root was initialized; leaving the engine-visible target untouched"
        )

    try:
        legacy_path.lstat()
    except FileNotFoundError:
        legacy_state = None
    else:
        legacy_state = _load_exodus_state_path(legacy_path)
    if legacy_state is not None:
        save_exodus_state(root, legacy_state)
        _create_state_identity(
            root,
            config=config,
            genesis_source="legacy_import",
            legacy_path=legacy_path,
        )
        return legacy_state, f"imported:{legacy_path}"

    try:
        config.target_book_path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError(
            "Exodus state is missing while its target book exists; leaving the engine-visible target untouched"
        )
    if held_symbols is None or working_entry_symbols is None:
        raise RuntimeError("Exodus state cannot initialize without a conclusive engine account view")
    if held_symbols or working_entry_symbols:
        raise RuntimeError("Exodus state cannot initialize while the engine reports Exodus exposure")
    initial = ExodusState()
    save_exodus_state(root, initial)
    _create_state_identity(
        root,
        config=config,
        genesis_source="initialized_empty",
    )
    return initial, "initialized_empty"


def save_exodus_state(root: str | Path, state: ExodusState) -> None:
    durable_atomic_replace(
        exodus_state_path(root),
        canonical_json(state.to_dict()) + b"\n",
        label="Exodus producer state",
    )


def run_exodus_cycle(
    data_root: str | Path,
    *,
    config: ExodusEffectiveConfig,
    now_ms: int | None = None,
) -> PublishedTargetCyclePayload:
    """Read inputs, call the pure planner, publish, transition, and report."""

    root = Path(data_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    cycle_now_ms = int(now_ms if now_ms is not None else time.time_ns() // 1_000_000)
    cycle_id = f"exodus-target-{config.rule.config_id}-{cycle_now_ms}"
    with exclusive_file_lock(root / ".locks" / "exodus_cycle.lock", stale_seconds=300):
        engine_error = ""
        held_symbols: frozenset[str] | None = None
        held_positions: Mapping[str, tuple[str, float, float]] | None = None
        working_symbols: frozenset[str] | None = None
        try:
            reading = require_recent_engine_account(
                config.environment,
                max_age_ns=TARGET_PRODUCER_HEALTH_MAX_AGE_NS,
                now_ns=cycle_now_ms * 1_000_000,
                path=config.engine_heartbeat_path,
                expected_account_user_id=config.expected_account_user_id,
            )
            if EXODUS_ENGINE_SLEEVE in reading.strategies:
                held_positions = reading.holdings_for_strategy(EXODUS_ENGINE_SLEEVE)
                held_symbols = frozenset(held_positions)
                working_symbols = reading.working_entries_for_strategy(EXODUS_ENGINE_SLEEVE)
            else:
                engine_error = "engine account heartbeat omits the exodus strategy"
        except (OSError, RuntimeError, ValueError) as exc:
            engine_error = str(exc)

        prior, state_source = _load_or_initialize_exodus_state(
            root,
            config=config,
            held_symbols=held_symbols,
            working_entry_symbols=working_symbols,
        )
        events = load_carry_presettlement_events(config.event_path)

        output = decide_exodus(
            ExodusDecisionInput(
                now_ms=cycle_now_ms,
                events=events,
                held_symbols=held_symbols,
                working_entry_symbols=working_symbols,
                held_positions=held_positions,
            ),
            prior,
            config,
        )
        # Exposure and tape consumption are durable before they become visible
        # to the engine. Covers reverse that order: publish explicit zeros,
        # then delete state only after a conclusive flat engine projection.
        if output.staged_state != prior:
            save_exodus_state(root, output.staged_state)
        publication = publish_target_book(
            config.target_book_path,
            output.target_book_bytes.decode("utf-8"),
        )
        if output.final_state != output.staged_state:
            save_exodus_state(root, output.final_state)

        payload: dict[str, Any] = {
            "cycle_id": cycle_id,
            "ts_ms": cycle_now_ms,
            "sleeve": EXODUS_ENGINE_SLEEVE,
            "mode": f"{config.environment}_rust_target_book",
            "environment": config.environment,
            "strategy_id": config.rule.config_id,
            "strategy_profile": config.profile_name,
            "config_provenance": json.dumps(config.provenance_dict(), sort_keys=True, separators=(",", ":")),
            "state_source": state_source,
            "event_tape_path": str(config.event_path),
            "events_seen": len(events),
            "events_consumed": len(output.final_state.consumed_event_ids),
            "opened": list(output.opened_symbols),
            "covered": list(output.covered_symbols),
            "entry_closed": list(output.entry_closed_symbols),
            "retired": list(output.retired_symbols),
            "blocked_events": [f"{event_id}:{reason}" for event_id, reason in output.blocked_events],
            "open_names": len(output.active_records),
            "engine_account_health_error": engine_error,
            "book_written": True,
            "target_book_path": str(config.target_book_path),
            "target_book_object_path": str(publication.object_path),
            "target_book_sha256": publication.sha256,
            "next_cover_ts_ms": output.next_cover_ts_ms,
        }
        write_dataset(
            pl.DataFrame([payload], infer_schema_length=None),
            root,
            EXODUS_CYCLES_DATASET,
            partition_by=("date",),
        )
    return PublishedTargetCyclePayload(
        payload,
        target_book_path=config.target_book_path,
        target_book_object_path=publication.object_path,
    )


def format_exodus_cycle_summary(payload: Mapping[str, Any]) -> str:
    error = str(payload.get("engine_account_health_error") or "none")
    return (
        "exodus target producer "
        f"events={payload.get('events_seen', 0)} "
        f"opened={len(payload.get('opened') or [])} "
        f"covered={len(payload.get('covered') or [])} "
        f"open={payload.get('open_names', 0)} "
        f"engine_health={error}"
    )
