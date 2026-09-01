from __future__ import annotations

import hashlib
import hmac
import json
import stat
from pathlib import Path

import pytest

from liquidity_migration.core.venue_realm import VenueRealm
from scripts.research.capture_bybit_account_history import (
    RECV_WINDOW_MS,
    BybitReadClient,
    CaptureError,
    SOURCES,
    capture,
    credential_variables,
    fetch_source,
    parse_time,
    write_capture,
)


class Reply:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self) -> Reply:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_signed_client_exposes_only_a_get_surface_and_signs_the_exact_query() -> None:
    requests = []

    def opener(request, **_):
        requests.append(request)
        return Reply({"retCode": 0, "retMsg": "OK", "result": {"list": []}, "time": 123})

    client = BybitReadClient(
        "https://unit.invalid",
        "public-key",
        "private-secret",
        clock_ms=lambda: 1_700_000_000_000,
        opener=opener,
    )
    client.get("/v5/execution/list", {"category": "linear", "limit": "100"})

    request = requests[0]
    query = "category=linear&limit=100"
    expected = hmac.new(
        b"private-secret",
        f"1700000000000public-key{RECV_WINDOW_MS}{query}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert request.get_method() == "GET"
    assert request.full_url == f"https://unit.invalid/v5/execution/list?{query}"
    assert request.headers["X-bapi-sign"] == expected
    assert "private-secret" not in request.full_url
    assert all("private-secret" not in value for value in request.headers.values())


def test_mainnet_defaults_to_the_separate_read_only_key() -> None:
    assert credential_variables(VenueRealm.MAINNET, "read-only") == (
        "BYBIT_ATTEST_API_KEY",
        "BYBIT_ATTEST_API_SECRET",
    )
    assert credential_variables(VenueRealm.MAINNET, "execution") == (
        "BYBIT_REAL_API_KEY",
        "BYBIT_REAL_API_SECRET",
    )
    assert credential_variables(VenueRealm.DEMO, "read-only") == (
        "BYBIT_DEMO_API_KEY",
        "BYBIT_DEMO_API_SECRET",
    )


class EmptyHistoryClient:
    def get(self, path: str, params: dict[str, str]) -> dict:
        if path == "/v5/user/query-api":
            return {"retCode": 0, "result": {"userID": "12345"}, "time": 3_000}
        return {
            "retCode": 0,
            "result": {"list": [], "nextPageCursor": ""},
            "time": 2,
        }


def test_terminal_null_cursor_ends_pagination() -> None:
    class NullCursorClient:
        def get(self, _path: str, _params: dict[str, str]) -> dict:
            return {
                "retCode": 0,
                "result": {"list": [], "nextPageCursor": None},
                "time": 2_000,
            }

    rows, receipt = fetch_source(
        NullCursorClient(),  # type: ignore[arg-type]
        SOURCES[0],
        1_000,
        2_000,
    )

    assert rows == []
    assert receipt["pages"] == 1
    assert receipt["complete"] is True


def test_non_string_non_null_cursor_is_rejected() -> None:
    class InvalidCursorClient:
        def get(self, _path: str, _params: dict[str, str]) -> dict:
            return {
                "retCode": 0,
                "result": {"list": [], "nextPageCursor": 7},
                "time": 2_000,
            }

    with pytest.raises(CaptureError, match="nextPageCursor"):
        fetch_source(
            InvalidCursorClient(),  # type: ignore[arg-type]
            SOURCES[0],
            1_000,
            2_000,
        )


def test_missing_cursor_is_rejected() -> None:
    class MissingCursorClient:
        def get(self, _path: str, _params: dict[str, str]) -> dict:
            return {
                "retCode": 0,
                "result": {"list": []},
                "time": 2_000,
            }

    with pytest.raises(CaptureError, match="nextPageCursor is missing"):
        fetch_source(
            MissingCursorClient(),  # type: ignore[arg-type]
            SOURCES[0],
            1_000,
            2_000,
        )


def test_capture_receipts_bind_all_three_complete_sources_and_the_account(monkeypatch) -> None:
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-secret")

    rows = capture(
        realm=VenueRealm.DEMO,
        credential_set="read-only",
        start_ms=1_000,
        end_ms=2_000,
        client=EmptyHistoryClient(),  # type: ignore[arg-type]
    )

    assert len(rows) == 1
    manifest = rows[0]
    assert manifest["complete"] is True
    assert manifest["realm"] == "demo"
    assert manifest["user_id"] == "12345"
    assert manifest["api_key_sha256"] == hashlib.sha256(b"demo-key").hexdigest()
    assert set(manifest["sources"]) == {"execution", "closed_pnl", "transaction"}
    assert all(receipt["complete"] for receipt in manifest["sources"].values())
    assert manifest["sources"]["execution"]["params"] == {
        "category": "linear",
        "settleCoin": "USDT",
        "limit": "100",
    }
    assert manifest["venue_query_start_time_ms"] == 3_000
    assert manifest["venue_query_end_time_ms"] == 3_000
    assert manifest["retention_start_ms"] < manifest["start_ms"]
    serialized = json.dumps(manifest)
    assert "demo-secret" not in serialized
    assert "demo-key\"" not in serialized


def test_capture_file_is_new_and_owner_readable_only(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    rows = [{"_kind": "capture", "complete": True}]

    write_capture(path, rows)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == rows[0]
    with pytest.raises(FileExistsError):
        write_capture(path, rows)


def test_time_parser_has_an_explicit_utc_boundary() -> None:
    assert parse_time("1970-01-02") == 86_400_000
    assert parse_time("1970-01-01T00:00:01Z") == 1_000
    assert parse_time("1001") == 1_001
    with pytest.raises(ValueError, match="UTC offset"):
        parse_time("2026-08-30T21:30:00")


def test_capture_rejects_an_empty_window_before_any_history_read(monkeypatch) -> None:
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-secret")
    with pytest.raises(CaptureError, match="after"):
        capture(
            realm=VenueRealm.DEMO,
            credential_set="read-only",
            start_ms=2_000,
            end_ms=2_000,
            client=EmptyHistoryClient(),  # type: ignore[arg-type]
        )


def test_capture_rejects_a_window_that_has_not_closed_at_the_venue(monkeypatch) -> None:
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-secret")

    with pytest.raises(CaptureError, match="venue server time"):
        capture(
            realm=VenueRealm.DEMO,
            credential_set="read-only",
            start_ms=1_000,
            end_ms=3_001,
            client=EmptyHistoryClient(),  # type: ignore[arg-type]
        )


class RecentVenueClient(EmptyHistoryClient):
    def get(self, path: str, params: dict[str, str]) -> dict:
        if path == "/v5/user/query-api":
            return {
                "retCode": 0,
                "result": {"userID": "12345"},
                "time": 1_800_000_000_000,
            }
        return super().get(path, params)


def test_capture_rejects_a_window_older_than_the_documented_retention(monkeypatch) -> None:
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-secret")

    with pytest.raises(CaptureError, match="two-year"):
        capture(
            realm=VenueRealm.DEMO,
            credential_set="read-only",
            start_ms=1_000,
            end_ms=2_000,
            client=RecentVenueClient(),  # type: ignore[arg-type]
        )


class ChangingIdentityClient(EmptyHistoryClient):
    def __init__(self, *, final_user: str, initial_time: int, final_time: int) -> None:
        self.final_user = final_user
        self.times = iter((initial_time, final_time))
        self.identity_calls = 0

    def get(self, path: str, params: dict[str, str]) -> dict:
        if path == "/v5/user/query-api":
            self.identity_calls += 1
            return {
                "retCode": 0,
                "result": {
                    "userID": "12345" if self.identity_calls == 1 else self.final_user
                },
                "time": next(self.times),
            }
        return super().get(path, params)


def test_capture_rechecks_identity_after_all_pagination(monkeypatch) -> None:
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-secret")
    client = ChangingIdentityClient(final_user="other-user", initial_time=3_000, final_time=4_000)

    with pytest.raises(CaptureError, match="user changed"):
        capture(
            realm=VenueRealm.DEMO,
            credential_set="read-only",
            start_ms=1_000,
            end_ms=2_000,
            client=client,  # type: ignore[arg-type]
        )

    assert client.identity_calls == 2


def test_capture_rechecks_retention_against_final_server_time(monkeypatch) -> None:
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-secret")
    client = ChangingIdentityClient(
        final_user="12345",
        initial_time=parse_time("2026-08-30T00:00:00Z"),
        final_time=parse_time("2026-08-31T00:00:00Z"),
    )

    with pytest.raises(CaptureError, match="before pagination completed"):
        capture(
            realm=VenueRealm.DEMO,
            credential_set="read-only",
            start_ms=parse_time("2024-08-30T12:00:00Z"),
            end_ms=parse_time("2024-08-30T12:00:01Z"),
            client=client,  # type: ignore[arg-type]
        )

    assert client.identity_calls == 2
