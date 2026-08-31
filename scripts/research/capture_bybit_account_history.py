#!/usr/bin/env python3
"""Capture Bybit executions, closed PnL, and account transactions with GET requests only.

Credentials are read only from the repository's realm-specific environment
variables. This command has no POST path and does not consult ``REAL_MONEY``;
it grants no trading authority.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import certifi

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from liquidity_migration.core.venue_realm import (  # noqa: E402
    REALM_REST_ENDPOINTS,
    VenueRealm,
    venue_realm,
)

RECV_WINDOW_MS = 5_000
MAX_WINDOW_MS = 7 * 86_400_000
MAX_PAGES = 10_000


class CaptureError(RuntimeError):
    """The signed read did not produce complete, bounded evidence."""


@dataclass(frozen=True)
class Source:
    name: str
    path: str
    params: Mapping[str, str]
    time_field: str
    row_kind: str


SOURCES = (
    Source(
        name="execution",
        path="/v5/execution/list",
        params={"category": "linear", "settleCoin": "USDT", "limit": "100"},
        time_field="execTime",
        row_kind="execution",
    ),
    Source(
        name="closed_pnl",
        path="/v5/position/closed-pnl",
        params={"category": "linear", "limit": "100"},
        time_field="updatedTime",
        row_kind="closed_pnl",
    ),
    Source(
        name="transaction",
        path="/v5/account/transaction-log",
        params={"accountType": "UNIFIED", "category": "linear", "currency": "USDT", "limit": "50"},
        time_field="transactionTime",
        row_kind="transaction",
    ),
)


def parse_time(value: str) -> int:
    text = value.strip()
    if text.isdigit():
        return int(text)
    if len(text) == 10:
        parsed = dt.datetime.combine(dt.date.fromisoformat(text), dt.time(), tzinfo=dt.timezone.utc)
    else:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamps with a time must include a UTC offset")
        parsed = parsed.astimezone(dt.timezone.utc)
    return int(parsed.timestamp() * 1000)


def credential_variables(realm: VenueRealm, credential_set: str) -> tuple[str, str]:
    if realm == VenueRealm.DEMO:
        return "BYBIT_DEMO_API_KEY", "BYBIT_DEMO_API_SECRET"
    if credential_set == "read-only":
        return "BYBIT_ATTEST_API_KEY", "BYBIT_ATTEST_API_SECRET"
    return "BYBIT_REAL_API_KEY", "BYBIT_REAL_API_SECRET"


def read_credentials(realm: VenueRealm, credential_set: str) -> tuple[str, str, tuple[str, str]]:
    variables = credential_variables(realm, credential_set)
    values = tuple(os.environ.get(name, "").strip() for name in variables)
    missing = [name for name, value in zip(variables, values, strict=True) if not value]
    if missing:
        raise CaptureError(f"missing credential environment variable(s): {', '.join(missing)}")
    return values[0], values[1], variables


class BybitReadClient:
    """The narrow signed capability: one authenticated GET method."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        *,
        timeout: float = 30.0,
        clock_ms: Callable[[], int] | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._secret = api_secret
        self.timeout = timeout
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._opener = opener or urllib.request.urlopen
        self._ssl_context = ssl.create_default_context(cafile=certifi.where())

    def get(self, path: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        query = urllib.parse.urlencode(list(params.items()))
        timestamp = str(self._clock_ms())
        signed = f"{timestamp}{self.api_key}{RECV_WINDOW_MS}{query}".encode()
        signature = hmac.new(self._secret.encode(), signed, hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            f"{self.base_url}{path}?{query}",
            headers={
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": str(RECV_WINDOW_MS),
                "X-BAPI-SIGN": signature,
                "User-Agent": "liquidity-migration-read-only-accounting/1",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.timeout, context=self._ssl_context) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise CaptureError(f"{path}: venue HTTP status {exc.code}") from None
        except urllib.error.URLError:
            raise CaptureError(f"{path}: venue transport failed") from None
        try:
            envelope = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CaptureError(f"{path}: venue returned malformed JSON ({exc})") from None
        if not isinstance(envelope, Mapping):
            raise CaptureError(f"{path}: venue returned a non-object response")
        if envelope.get("retCode") != 0:
            raise CaptureError(
                f"{path}: venue rejected the read with code {envelope.get('retCode')!r}: "
                f"{str(envelope.get('retMsg') or '')[:200]}"
            )
        return envelope


def _window_slices(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    slices: list[tuple[int, int]] = []
    cursor = start_ms
    while cursor < end_ms:
        next_cursor = min(cursor + MAX_WINDOW_MS, end_ms)
        slices.append((cursor, next_cursor))
        cursor = next_cursor
    return slices


def fetch_source(
    client: BybitReadClient, source: Source, start_ms: int, end_ms: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pages = 0
    slices = _window_slices(start_ms, end_ms)
    for slice_start, slice_end in slices:
        cursor = ""
        while True:
            if pages >= MAX_PAGES:
                raise CaptureError(f"{source.name}: pagination still continued after {MAX_PAGES} pages")
            params = dict(source.params)
            params["startTime"] = str(slice_start)
            params["endTime"] = str(slice_end - 1)
            if cursor:
                params["cursor"] = cursor
            envelope = client.get(source.path, params)
            result = envelope.get("result")
            if not isinstance(result, Mapping) or not isinstance(result.get("list"), list):
                raise CaptureError(f"{source.name}: venue response has no result list")
            server_time = envelope.get("time")
            for raw_row in result["list"]:
                if not isinstance(raw_row, Mapping):
                    raise CaptureError(f"{source.name}: venue result contains a non-object row")
                try:
                    row_time = int(str(raw_row.get(source.time_field) or ""))
                except ValueError:
                    raise CaptureError(
                        f"{source.name}: row has no integer {source.time_field}"
                    ) from None
                if not start_ms <= row_time < end_ms:
                    continue
                row = dict(raw_row)
                row["_kind"] = source.row_kind
                row["_server_time_ms"] = server_time
                rows.append(row)
            next_cursor = result.get("nextPageCursor")
            if not isinstance(next_cursor, str):
                raise CaptureError(f"{source.name}: nextPageCursor is not a string")
            pages += 1
            if not next_cursor:
                break
            if next_cursor == cursor:
                raise CaptureError(f"{source.name}: pagination cursor did not advance")
            cursor = next_cursor
    rows.sort(
        key=lambda row: (
            int(str(row.get(source.time_field) or "0")),
            str(row.get("execId") or row.get("tradeId") or row.get("orderId") or row.get("id") or ""),
        )
    )
    receipt = {
        "complete": True,
        "endpoint": source.path,
        "slices": len(slices),
        "pages": pages,
        "rows": len(rows),
    }
    return rows, receipt


def capture(
    *,
    realm: VenueRealm,
    credential_set: str,
    start_ms: int,
    end_ms: int,
    client: BybitReadClient | None = None,
) -> list[dict[str, Any]]:
    if end_ms <= start_ms:
        raise CaptureError("--end must be after --start")
    api_key, secret, variables = read_credentials(realm, credential_set)
    active_client = client or BybitReadClient(REALM_REST_ENDPOINTS[realm], api_key, secret)
    identity_envelope = active_client.get("/v5/user/query-api", {})
    identity = identity_envelope.get("result")
    if not isinstance(identity, Mapping) or not str(identity.get("userID") or ""):
        raise CaptureError("the venue did not identify the authenticated user")

    all_rows: list[dict[str, Any]] = []
    receipts: dict[str, dict[str, Any]] = {}
    for source in SOURCES:
        rows, receipt = fetch_source(active_client, source, start_ms, end_ms)
        all_rows.extend(rows)
        receipts[source.name] = receipt
    manifest = {
        "_kind": "capture",
        "schema_version": 1,
        "complete": True,
        "captured_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "realm": realm.value,
        "user_id": str(identity["userID"]),
        "api_key_sha256": hashlib.sha256(api_key.encode()).hexdigest(),
        "credential_variables": list(variables),
        "start_ms": start_ms,
        "end_ms_exclusive": end_ms,
        "sources": receipts,
        "boundary": "[start_ms, end_ms_exclusive)",
    }
    return [manifest, *all_rows]


def write_capture(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    resolved = path.expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(resolved, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        resolved.unlink(missing_ok=True)
        raise


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--realm", required=True, choices=("demo", "mainnet"))
    parser.add_argument(
        "--credential-set",
        choices=("read-only", "execution"),
        default="read-only",
        help="mainnet defaults to BYBIT_ATTEST_*; execution selects BYBIT_REAL_* after key rotation",
    )
    parser.add_argument("--start", required=True, help="UTC ISO time/date or epoch milliseconds, inclusive")
    parser.add_argument("--end", required=True, help="UTC ISO time/date or epoch milliseconds, exclusive")
    parser.add_argument("--out", required=True, type=Path, help="new mode-0600 JSONL capture path")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        realm = venue_realm(args.realm)
        start_ms = parse_time(args.start)
        end_ms = parse_time(args.end)
        rows = capture(
            realm=realm,
            credential_set=args.credential_set,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        write_capture(args.out, rows)
    except (CaptureError, OSError, ValueError) as exc:
        raise SystemExit(f"account-history capture failed: {exc}") from None
    manifest = rows[0]
    sources = manifest["sources"]
    print(
        f"captured {sources['execution']['rows']} executions, {sources['closed_pnl']['rows']} closed-PnL rows, "
        f"and {sources['transaction']['rows']} transactions to {args.out.expanduser().resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
