from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from liquidity_migration.strategy import strategy_planning as planning
from liquidity_migration.account.account_owner_health import AccountOwnerHealthHeadPending


def _route(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(account_path=tmp_path, account_id="demo-account")


def test_owner_equity_retries_only_the_exact_head_publication_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def owner_health(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise AccountOwnerHealthHeadPending(
                "account-owner health journal sequence mismatch: health=10, journal=11"
            )
        return SimpleNamespace(equity_usdt=9_876.5)

    monkeypatch.setattr(planning, "require_recent_account_owner_health", owner_health)

    equity, error = planning.account_owner_equity_or_error(
        _route(tmp_path),
        environment="demo",
        head_retry_attempts=4,
        head_retry_seconds=0.25,
        sleep=sleeps.append,
    )

    assert (equity, error) == (9_876.5, "")
    assert calls == 3
    assert sleeps == [0.25, 0.25]


def test_owner_equity_does_not_retry_a_terminal_health_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def owner_health(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        raise RuntimeError("account owner is blocked: native protection missing")

    monkeypatch.setattr(planning, "require_recent_account_owner_health", owner_health)

    equity, error = planning.account_owner_equity_or_error(
        _route(tmp_path),
        environment="demo",
        sleep=sleeps.append,
    )

    assert equity == 0.0
    assert error == "RuntimeError: account owner is blocked: native protection missing"
    assert calls == 1
    assert sleeps == []


def test_owner_equity_reports_a_head_race_after_the_bounded_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def owner_health(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        raise AccountOwnerHealthHeadPending(
            "account-owner health journal sequence mismatch: health=20, journal=21"
        )

    monkeypatch.setattr(planning, "require_recent_account_owner_health", owner_health)

    equity, error = planning.account_owner_equity_or_error(
        _route(tmp_path),
        environment="demo",
        head_retry_attempts=3,
        head_retry_seconds=0.1,
        sleep=sleeps.append,
    )

    assert equity == 0.0
    assert error.startswith("AccountOwnerHealthHeadPending: account-owner health journal sequence mismatch")
    assert calls == 3
    assert sleeps == [0.1, 0.1]


@pytest.mark.parametrize(
    ("attempts", "seconds", "message"),
    ((0, 1.0, "attempts"), (1, -0.1, "delay")),
)
def test_owner_equity_rejects_invalid_retry_bounds(
    tmp_path: Path,
    attempts: int,
    seconds: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        planning.account_owner_equity_or_error(
            _route(tmp_path),
            environment="demo",
            head_retry_attempts=attempts,
            head_retry_seconds=seconds,
        )
