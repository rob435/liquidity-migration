from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from liquidity_migration.strategy import strategy_planning as planning


# The venue-reading age is measured against the wall clock, not against the
# caller's cycle stamp, so these fixtures anchor to real now -- taken when the
# test runs, never at import. Anchoring at import made the whole file pass
# alone and fail inside the suite, because by then import was a minute ago and
# every fixture was older than the 30s bound.
BOUND_NS = 30_000_000_000


def _route(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(account_path=tmp_path, account_id="6039967")


def _heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **overrides: object,
) -> int:
    """Write one engine heartbeat and point the producers at it.

    The keys are the ones `engine-core/src/heartbeat.rs` renders; the Rust side
    has its own test that the file carries exactly this key set.
    """

    now_ns = time.time_ns()
    payload: dict[str, object] = {
        "account_available_usdt": 4_100.25,
        "account_equity_usdt": 9_876.5,
        "account_observed_wall_ts_ms": now_ns // 1_000_000 - 1_000,
        "account_user_id": "6039967",
        "realm": "demo",
        "mode": "live",
        "may_open": True,
    }
    payload.update(overrides)
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("ENGINE_ACCOUNT_HEARTBEAT_FILE", str(path))
    return now_ns


def test_equity_comes_from_the_engine_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _heartbeat(tmp_path, monkeypatch)

    equity, error = planning.account_owner_equity_or_error(
        _route(tmp_path),
        environment="demo",
    )

    assert (equity, error) == (9_876.5, "")


def test_an_engine_that_stopped_reading_the_venue_blocks_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heartbeat is current; the venue reading inside it is not.

    This is the case the whole check exists for. An engine whose loop keeps
    running while its venue reads fail rewrites this file on time forever, so
    ageing the file would say healthy. Ageing the reading stamp says what
    is true: nobody knows what this account holds.
    """

    _heartbeat(
        tmp_path,
        monkeypatch,
        account_observed_wall_ts_ms=(time.time_ns() - BOUND_NS) // 1_000_000 - 1,
    )

    equity, error = planning.account_owner_equity_or_error(
        _route(tmp_path),
        environment="demo",
    )

    assert equity == 0.0
    assert "not reading the venue" in error


def test_a_mainnet_heartbeat_can_never_size_a_demo_producer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Realm, not account id.

    The two halves name an account differently on the live host -- the route
    calls it `bybit-mainnet-unified`, the engine reports the venue's user
    number -- so comparing ids blocked every entry. Realm is the value they
    genuinely share, and it catches the failure that actually matters: one
    realm's equity sizing the other realm's producer.
    """

    _heartbeat(tmp_path, monkeypatch, realm="mainnet", account_user_id="552445993")

    equity, error = planning.account_owner_equity_or_error(
        _route(tmp_path),
        environment="demo",
    )

    assert equity == 0.0
    assert "'mainnet' realm, not 'demo'" in error
    assert "552445993" in error


def test_an_engine_with_no_account_reading_yet_blocks_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Booted, beating, has not read the venue yet: null, not zero."""

    _heartbeat(
        tmp_path,
        monkeypatch,
        account_equity_usdt=None,
        account_observed_wall_ts_ms=None,
    )

    equity, error = planning.account_owner_equity_or_error(
        _route(tmp_path),
        environment="demo",
    )

    assert equity == 0.0
    assert "no account reading yet" in error


def test_a_missing_heartbeat_blocks_entries_rather_than_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENGINE_ACCOUNT_HEARTBEAT_FILE", str(tmp_path / "absent.json"))

    equity, error = planning.account_owner_equity_or_error(
        _route(tmp_path),
        environment="demo",
    )

    assert equity == 0.0
    assert error


def test_a_stored_reading_cannot_outlive_the_venue_reading_behind_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serving a stored reading must not let ages stack.

    The reading is young, but the venue observation it carries is already at
    the bound, so serving it for another reading-lifetime would plan against
    equity twice as old as a live read allows.
    """

    now_ns = _heartbeat(tmp_path, monkeypatch)
    stored = planning.OwnerHealthReading(
        equity_usdt=9_876.5,
        error="",
        read_wall_ts_ns=now_ns,
        receipt_wall_ts_ns=now_ns - BOUND_NS - 1,
    )

    assert not stored.is_serveable(now_ns=now_ns, max_age_ns=BOUND_NS)


def test_a_reading_stamped_on_the_engines_own_clock_is_not_mistaken_for_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fault a live deploy found, from the reading side.

    The engine's clock is monotonic and counts from an instant near its own
    boot, so a healthy engine six seconds old would stamp `6_000`. Read as a
    unix stamp that is 1970, and it must block rather than look fresh -- and
    the engine now converts the age to a wall stamp so this cannot arrive.
    """

    _heartbeat(tmp_path, monkeypatch, account_observed_wall_ts_ms=6_000)

    equity, error = planning.account_owner_equity_or_error(
        _route(tmp_path),
        environment="demo",
    )

    assert equity == 0.0
    assert "not reading the venue" in error
