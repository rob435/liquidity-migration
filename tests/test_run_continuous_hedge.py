from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

import scripts.run_continuous_hedge as hedge_runner
from liquidity_migration.account_route import ensure_account_route
from liquidity_migration.continuous_hedge_manager import HedgeDecision, HedgeDecision2F
from liquidity_migration.continuous_rebalance import ContinuousRebalanceResizePlan


def _resize_plan(
    *,
    symbol: str = "BTCUSDT",
    side: str = "Buy",
    reduce_only: bool = False,
    current_notional: float = 0.0,
    target_notional: float = 500.0,
) -> ContinuousRebalanceResizePlan:
    delta = target_notional - current_notional
    return ContinuousRebalanceResizePlan(
        trade_id="hedge",
        symbol=symbol,
        side=side,
        reduce_only=reduce_only,
        qty=abs(delta) / 100_000.0,
        current_notional_usdt=current_notional,
        target_notional_usdt=target_notional,
        delta_notional_usdt=delta,
        reason="hedge_resize",
    )


def _single_decision(plan: ContinuousRebalanceResizePlan | None) -> HedgeDecision:
    return HedgeDecision(
        beta_window_days=90,
        hedge_ratio_equity_frac=0.05,
        target_notional_usdt=500.0,
        current_notional_usdt=0.0,
        n_obs=90,
        plan=plan,
    )


def _two_factor_decision() -> HedgeDecision2F:
    return HedgeDecision2F(
        beta_window_days=90,
        ratio_btc=0.03,
        ratio_eth=0.02,
        target_btc_usdt=300.0,
        target_eth_usdt=200.0,
        n_obs_joint=90,
        plan_btc=_resize_plan(target_notional=300.0),
        plan_eth=_resize_plan(symbol="ETHUSDT", target_notional=200.0),
        fell_back_to_btc=False,
    )


def _setup_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    argv: list[str] | None = None,
    equity_usdt: float = 10_000.0,
    warm_eth: list[float | None] | None = None,
    warmstart_last: date | None = None,
    account_root: bool = True,
    account_inbox: bool = True,
    hedge_qty: dict[str, float] | None = None,
    pending_hedge_symbols: set[str] | None = None,
) -> None:
    unit = [-0.002, 0.002] * 45
    btc = [0.01, -0.01] * 45
    eth = [None] * 90 if warm_eth is None else warm_eth
    monkeypatch.setattr(hedge_runner, "REPO", tmp_path)
    monkeypatch.setattr(hedge_runner, "load_warmstart_2f", lambda path: (unit, btc, eth))
    monkeypatch.setattr(
        hedge_runner,
        "_warmstart_last_date",
        lambda path: warmstart_last or date.today(),
    )

    def owner_health(*args, **kwargs):
        if equity_usdt <= 0.0:
            raise RuntimeError("account-owner health is unavailable")
        return SimpleNamespace(equity_usdt=equity_usdt)

    monkeypatch.setattr(hedge_runner, "require_recent_account_owner_health", owner_health)
    monkeypatch.setattr(
        hedge_runner,
        "_account_continuous_book_state",
        lambda root, *, equity_usdt: hedge_runner.LiveBookState(0.5, True, "test"),
    )
    quantities = hedge_qty or {}
    monkeypatch.setattr(
        hedge_runner,
        "_current_account_hedge_qty",
        lambda root, *, strategy_id, symbol="BTCUSDT": quantities.get(symbol, 0.0),
    )
    monkeypatch.setattr(
        hedge_runner,
        "_pending_account_hedge_symbols",
        lambda root, *, strategy_id: set(pending_hedge_symbols or ()),
    )
    monkeypatch.delenv("HEDGE_MODE", raising=False)
    args = list(argv or [])
    if account_root:
        args.extend(("--account-root", str(tmp_path / "account")))
    if account_inbox and "--account-inbox-root" not in args:
        args.extend(("--account-inbox-root", str(tmp_path / "inbox")))
    if account_root and account_inbox:
        ensure_account_route(
            account_id="bybit-demo-unified",
            environment="demo",
            account_root=tmp_path / "account",
            inbox_root=tmp_path / "inbox",
        )
    monkeypatch.setattr(sys, "argv", ["run_continuous_hedge.py", *args])


def _submit_args(tmp_path) -> list[str]:
    return [
        "--submit",
        "--btc-price",
        "100000",
        "--account-inbox-root",
        str(tmp_path / "inbox"),
    ]


def test_account_book_state_uses_canonical_short_targets(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        hedge_runner,
        "canonical_strategy_trade_rows",
        lambda *args, **kwargs: pl.DataFrame(
            [
                {"status": "open", "side": "short", "notional_usdt": 1_000.0},
                {"status": "open", "side": "short", "notional_usdt": 2_000.0},
                {"status": "target_pending", "side": "short", "notional_usdt": 500.0},
                {"status": "target_pending", "side": "short", "notional_usdt": 0.0},
                {"status": "open", "side": "long", "notional_usdt": 9_000.0},
                {"status": "closed", "side": "short", "notional_usdt": 4_000.0},
            ]
        ),
    )

    state = hedge_runner._account_continuous_book_state(tmp_path, equity_usdt=10_000.0)

    assert state.gross_short_frac == pytest.approx(0.35)
    assert state.gross_short_frac_known is True
    assert state.gross_short_frac_source == "account_kernel_desired_targets"


def test_account_book_state_refuses_unknown_equity(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        hedge_runner,
        "canonical_strategy_trade_rows",
        lambda *args, **kwargs: pl.DataFrame([{"status": "open", "side": "short", "notional_usdt": 1_000.0}]),
    )

    state = hedge_runner._account_continuous_book_state(tmp_path, equity_usdt=0.0)

    assert state.gross_short_frac == 0.0
    assert state.gross_short_frac_known is False
    assert state.gross_short_frac_source == "account_target_equity_unavailable"


def test_current_hedge_qty_reads_only_canonical_strategy_rows(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        hedge_runner,
        "canonical_strategy_trade_rows",
        lambda *args, **kwargs: pl.DataFrame(
            [
                {"status": "open", "symbol": "BTCUSDT", "signed_qty": 0.02},
                {"status": "open", "symbol": "BTCUSDT", "signed_qty": 0.03},
                {"status": "target_pending", "symbol": "BTCUSDT", "signed_qty": 0.04},
                {"status": "target_pending", "symbol": "BTCUSDT", "signed_qty": 0.0},
                {"status": "closed", "symbol": "BTCUSDT", "signed_qty": 1.0},
                {"status": "open", "symbol": "ETHUSDT", "signed_qty": 5.0},
            ]
        ),
    )

    qty = hedge_runner._current_account_hedge_qty(
        tmp_path,
        strategy_id="continuous_btc_hedge_v2",
    )

    assert qty == pytest.approx(0.09)


def test_pending_hedge_target_without_resize_is_not_republished(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=_submit_args(tmp_path),
        pending_hedge_symbols={"BTCUSDT", "ETHUSDT"},
    )
    monkeypatch.setattr(
        hedge_runner,
        "compute_hedge_decision",
        lambda cfg, **kwargs: _single_decision(None),
    )

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "submit_no_action"
    assert out["pending_target_refresh_skips"] == ["BTCUSDT", "ETHUSDT"]
    assert list((tmp_path / "inbox" / "pending").glob("*.json")) == []


def test_account_root_is_required_even_for_dry_run(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(monkeypatch, tmp_path, account_root=False)

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "account_route_config_missing"


def test_publish_requires_account_inbox(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=["--submit", "--btc-price", "100000"],
        account_inbox=False,
    )

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "account_route_config_missing"


def test_btc_mode_publishes_btc_target_and_explicit_eth_flatten(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=_submit_args(tmp_path),
        hedge_qty={"ETHUSDT": 2.0},
    )
    monkeypatch.setattr(
        hedge_runner,
        "compute_hedge_decision",
        lambda cfg, **kwargs: _single_decision(None),
    )

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "target_queued"
    assert out["mode"] == "publish"
    assert out["queued"]["target_count"] == 2
    pending = list((tmp_path / "inbox" / "pending").glob("*.json"))
    assert len(pending) == 2
    requests = [json.loads(path.read_text()) for path in pending]
    notionals = {
        item["intent"]["symbol"]: item["intent"]["signed_notional_usdt"]
        for request in requests
        for item in request["intents"]
    }
    assert notionals == {"BTCUSDT": 500.0, "ETHUSDT": 0.0}
    assert all(
        item["intent"]["leverage"] == 10.0
        for request in requests
        for item in request["intents"]
    )
    assert len(out["queued"]["request_ids"]) == 2
    assert not hasattr(hedge_runner, "_submit_plan")


def test_two_factor_targets_are_published_as_one_batch(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=[*_submit_args(tmp_path), "--eth-price", "3000"],
        warm_eth=[0.001] * 90,
    )
    monkeypatch.setattr(
        hedge_runner,
        "compute_hedge_decision_2f",
        lambda cfg, **kwargs: _two_factor_decision(),
    )

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "target_queued"
    assert out["queued"]["target_count"] == 2
    assert {row["symbol"] for row in out["queued"]["targets"]} == {
        "BTCUSDT",
        "ETHUSDT",
    }


def test_missing_canonical_equity_blocks_publish(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=_submit_args(tmp_path),
        equity_usdt=0.0,
    )

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "submit_blocked_account_owner_unhealthy"
    assert list((tmp_path / "inbox" / "pending").glob("*.json")) == []


def test_missing_btc_price_is_explicit_in_dry_run(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(monkeypatch, tmp_path)

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "dry_run_btc_price_unavailable"


def test_stale_warmstart_blocks_only_risk_increase(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=_submit_args(tmp_path),
        warmstart_last=date.today() - timedelta(days=10),
    )
    monkeypatch.setattr(
        hedge_runner,
        "compute_hedge_decision",
        lambda cfg, **kwargs: _single_decision(_resize_plan()),
    )

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "target_queued_partial_blocked_stale_warmstart"
    assert [row["symbol"] for row in out["blocked_legs"]] == ["BTCUSDT"]
    assert out["queued"]["targets"] == [
        {
            "symbol": "ETHUSDT",
            "target_notional_usdt": 0.0,
            "target_key": "hedge/continuous_btc_hedge_v2/eth/ETHUSDT",
        }
    ]


def test_stale_warmstart_allows_risk_reducing_target(monkeypatch, tmp_path, capsys) -> None:
    reduce = _resize_plan(
        side="Sell",
        reduce_only=True,
        current_notional=700.0,
        target_notional=500.0,
    )
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=_submit_args(tmp_path),
        warmstart_last=date.today() - timedelta(days=10),
    )
    monkeypatch.setattr(
        hedge_runner,
        "compute_hedge_decision",
        lambda cfg, **kwargs: _single_decision(reduce),
    )

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "target_queued"
    assert out["queued"]["target_count"] == 2


def test_target_publish_failure_fails_armed_run(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(monkeypatch, tmp_path, argv=_submit_args(tmp_path))
    monkeypatch.setattr(
        hedge_runner,
        "_publish_hedge_target_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "target_publish_failed"
    assert out["publish_error"] == "disk full"
