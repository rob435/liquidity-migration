"""Republish the demo fleet's account targets onto the paper route.

One fleet decides, both execute, so every remaining difference between the two
books is attributable to execution. Deciding independently cannot give that:
paper's producers poll the demo data stores on their own grid, offset ~13s, and
so read different copies of the same inputs.

**Scale.** ``scale`` multiplies every mirrored notional and defaults to 1.0 —
verbatim. Sizing paper by ``paper_equity / demo_equity`` is supported and is
right when the goal is a proportionally identical book at a different account
size, but it makes fills incomparable: quantities then differ for a reason that
is not execution.

**Provenance.** Every mirrored intent carries ``mirror_source_request_id``,
``mirror_source_environment`` and ``mirror_scale`` in its metadata.

**Ownership.** The demo capture tape is ``0600 root:root`` and the paper owner
runs unprivileged, so the mirror runs privileged, reads the tape in place, and
hands the queued files to the inbox's owner. The tape's mode does not change.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from liquidity_migration.account.account_route import AccountRoute
from liquidity_migration.account.account_service import AccountIntentInbox, AccountTargetRequest, RequestedIntent
from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.account.strategy_runtime import SleeveTargetIntent
from liquidity_migration.strategy.strategy_target_replay import (
    CapturedTargetRequest,
    TargetSchedulingCaptureEvent,
    load_target_scheduling_capture_bytes,
)

_logger = logging.getLogger(__name__)

MIRROR_CURSOR_SCHEMA_VERSION = 1

#: Metadata keys whose value is a USDT notional and therefore scales with the
#: request. Anything not listed here (weights, prices, timestamps, fractions)
#: is scale-invariant and passes through untouched.
_SCALED_METADATA_KEYS = frozenset(
    {
        "raw_target_notional_usdt",
        "signed_notional_usdt",
        "prior_standing_notional_usdt",
    }
)


class PaperTargetMirrorError(RuntimeError):
    """The mirror cannot establish what demo published, so it publishes nothing."""


@dataclass(frozen=True, slots=True)
class PaperTargetMirrorReport:
    healthy: bool
    capture_events_read: int
    requests_mirrored: int
    requests_skipped: int
    scale: float
    cursor_offset: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _MirrorCursor:
    offset: int
    capture_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MIRROR_CURSOR_SCHEMA_VERSION,
            "offset": self.offset,
            "capture_hash": self.capture_hash,
        }


def mirrored_request_id(source_request_id: str) -> str:
    """One paper request per demo request, stable across restarts.

    Independent of ``scale``, so a restart mid-tape is idempotent rather than
    re-publishing every target at whatever scale is current.
    """

    digest = hashlib.sha256(f"paper-mirror:{source_request_id}".encode("utf-8")).hexdigest()
    return f"target-{digest}"


def _scaled_metadata(metadata: Mapping[str, Any], *, scale: float) -> dict[str, Any]:
    scaled = dict(metadata)
    if scale != 1.0:
        for key in _SCALED_METADATA_KEYS & set(scaled):
            value = scaled[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            scaled[key] = float(value) * scale
    return scaled


def mirror_request(
    request: AccountTargetRequest,
    *,
    route: AccountRoute,
    scale: float,
) -> AccountTargetRequest:
    """Rebind one demo request onto the paper route, optionally rescaled.

    ``batch_id``, ``target_key``, ``decision_key``, ``component_id`` and
    ``created_ts_ns`` carry across unchanged, so the agreement check can compare
    the two fleets bar by bar without a join table.
    """

    if not (scale > 0.0):
        raise PaperTargetMirrorError("mirror scale must be positive")
    intents: list[RequestedIntent] = []
    for item in request.intents:
        source = item.intent
        metadata = _scaled_metadata(source.metadata or {}, scale=scale)
        metadata["mirror_source_request_id"] = request.request_id
        metadata["mirror_source_environment"] = request.environment
        metadata["mirror_scale"] = scale
        intents.append(
            RequestedIntent(
                adapter_kind=item.adapter_kind,
                intent=SleeveTargetIntent(
                    decision_key=source.decision_key,
                    target_key=source.target_key,
                    strategy_id=source.strategy_id,
                    component_id=source.component_id,
                    symbol=source.symbol,
                    # A flat target stays exactly flat: the capture contract
                    # keys the exit/entry stage on every notional being zero.
                    signed_notional_usdt=(
                        0.0
                        if source.signed_notional_usdt == 0.0
                        else source.signed_notional_usdt * scale
                    ),
                    leverage=source.leverage,
                    reason=source.reason,
                    metadata=metadata,
                ),
            )
        )
    return AccountTargetRequest(
        request_id=mirrored_request_id(request.request_id),
        batch_id=request.batch_id,
        created_ts_ns=request.created_ts_ns,
        route_id=route.route_id,
        account_id=route.account_id,
        environment=route.environment,
        intents=tuple(intents),
    )


def _published_requests(
    events: Iterable[TargetSchedulingCaptureEvent],
    *,
    sleeves: frozenset[str],
) -> list[CapturedTargetRequest]:
    """Captured requests in publication order, exits before entries.

    The capture records the leader's own ordering, and reordering it on the
    follower would let a mirrored entry precede the exit that frees its room.
    """

    rows: list[CapturedTargetRequest] = []
    for event in events:
        if event.sleeve not in sleeves:
            continue
        rows.extend(sorted(event.requests, key=lambda item: item.publication_order))
    return rows


class PaperTargetMirror:
    """Copy the demo fleet's published targets onto the paper inbox."""

    __slots__ = ("tape_path", "route", "inbox", "sleeves", "cursor_path", "_cursor")

    def __init__(
        self,
        *,
        tape_path: str | Path,
        route: AccountRoute,
        inbox: AccountIntentInbox,
        sleeves: Iterable[str],
        cursor_path: str | Path,
    ) -> None:
        self.tape_path = Path(tape_path).expanduser()
        self.route = route
        self.inbox = inbox
        self.sleeves = frozenset(str(name).strip().lower() for name in sleeves)
        if not self.sleeves:
            raise ValueError("paper target mirror requires at least one sleeve")
        self.cursor_path = Path(cursor_path).expanduser()
        self._cursor = self._load_cursor()

    def _load_cursor(self) -> _MirrorCursor:
        try:
            raw = self.cursor_path.read_bytes()
        except FileNotFoundError:
            return _MirrorCursor(offset=0, capture_hash="")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PaperTargetMirrorError("paper mirror cursor is unreadable") from exc
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != MIRROR_CURSOR_SCHEMA_VERSION
            or not isinstance(payload.get("offset"), int)
            or not isinstance(payload.get("capture_hash"), str)
        ):
            raise PaperTargetMirrorError("paper mirror cursor has invalid fields")
        return _MirrorCursor(offset=int(payload["offset"]), capture_hash=str(payload["capture_hash"]))

    def _store_cursor(self, cursor: _MirrorCursor) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cursor_path.with_suffix(self.cursor_path.suffix + ".tmp")
        data = json.dumps(cursor.to_dict(), sort_keys=True).encode("utf-8") + b"\n"
        descriptor = os.open(str(tmp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(tmp, self.cursor_path)
        self._cursor = cursor

    def _hand_to_inbox_owner(self, path: Path) -> None:
        """Give a queued file the inbox's own ownership.

        The mirror is privileged and the owner is not, so a root-written 0600
        file would be invisible to the process that has to claim it. Ownership
        comes from the destination directory rather than a configured uid.
        """

        try:
            target = os.stat(self.inbox.root)
        except OSError as exc:  # pragma: no cover - inbox is created at startup
            raise PaperTargetMirrorError("paper inbox root is unreadable") from exc
        try:
            current = os.stat(path)
        except FileNotFoundError:
            return
        if (current.st_uid, current.st_gid) == (target.st_uid, target.st_gid):
            return
        try:
            os.chown(path, target.st_uid, target.st_gid)
        except PermissionError as exc:
            raise PaperTargetMirrorError(
                "paper mirror cannot hand queued intents to the inbox owner; "
                "it must run privileged or as the inbox user"
            ) from exc

    def poll(self, *, scale: float = 1.0) -> PaperTargetMirrorReport:
        try:
            # lstat first: read_stable_file turns a missing path into a generic
            # ValueError, hiding "the leader has not published yet".
            self.tape_path.lstat()
        except OSError:
            return PaperTargetMirrorReport(
                healthy=False,
                capture_events_read=0,
                requests_mirrored=0,
                requests_skipped=0,
                scale=scale,
                cursor_offset=self._cursor.offset,
                detail="demo target capture tape is missing",
            )
        snapshot = read_stable_file(
            self.tape_path,
            label="demo target scheduling capture",
            require_single_link=True,
        )
        data = snapshot.data
        cursor = self._cursor
        if len(data) < cursor.offset:
            # The tape was reset or rotated; re-reading from the start would
            # replay the whole history onto a live book.
            raise PaperTargetMirrorError(
                "demo target capture tape shrank below the mirror cursor; "
                "a tape reset needs an explicit mirror cursor reset"
            )
        tail = data[cursor.offset :]
        if not tail:
            return PaperTargetMirrorReport(
                healthy=True,
                capture_events_read=0,
                requests_mirrored=0,
                requests_skipped=0,
                scale=scale,
                cursor_offset=cursor.offset,
                detail="",
            )
        events, chain_hash = load_target_scheduling_capture_bytes(
            tail,
            prior_capture_hash=cursor.capture_hash or None,
        )
        mirrored = 0
        skipped = 0
        for captured in _published_requests(events, sleeves=self.sleeves):
            if captured.durable_queue_state == "failed":
                # Demo never executed it, so paper must not either.
                skipped += 1
                continue
            request = mirror_request(captured.request, route=self.route, scale=scale)
            path = self.inbox.submit(request)
            self._hand_to_inbox_owner(path)
            mirrored += 1
        self._store_cursor(_MirrorCursor(offset=len(data), capture_hash=chain_hash))
        if mirrored:
            _logger.info(
                "paper mirror published %d demo target request(s) at scale %.6f",
                mirrored,
                scale,
            )
        return PaperTargetMirrorReport(
            healthy=True,
            capture_events_read=len(events),
            requests_mirrored=mirrored,
            requests_skipped=skipped,
            scale=scale,
            cursor_offset=len(data),
            detail="",
        )
