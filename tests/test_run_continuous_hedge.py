from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

import scripts.run_continuous_hedge as hedge_runner
from liquidity_migration.account_route import ensure_account_route
from liquidity_migration.continuous_hedge_manager import HedgeDecision2F, HedgeModelPrior
from liquidity_migration.continuous_rebalance import ContinuousRebalanceResizePlan


OPERATIONAL_PROFILE = Path(__file__).resolve().parents[1] / "configs" / "operational.demo.json"


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


def _two_factor_decision(
    *,
    plan_btc: ContinuousRebalanceResizePlan | None = None,
    plan_eth: ContinuousRebalanceResizePlan | None = None,
    target_btc_usdt: float = 300.0,
    target_eth_usdt: float = 200.0,
    fell_back_to_btc: bool = False,
) -> HedgeDecision2F:
    return HedgeDecision2F(
        beta_window_days=90,
        ratio_btc=0.03,
        ratio_eth=0.0 if fell_back_to_btc else 0.02,
        target_btc_usdt=target_btc_usdt,
        target_eth_usdt=target_eth_usdt,
        n_obs_joint=90,
        plan_btc=plan_btc,
        plan_eth=plan_eth,
        fell_back_to_btc=fell_back_to_btc,
    )


def _setup_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    argv: list[str] | None = None,
    equity_usdt: float = 10_000.0,
    warm_eth: list[float | None] | None = None,
    model_prior_through: date | None = None,
    account_root: bool = True,
    account_inbox: bool = True,
    hedge_qty: dict[str, float] | None = None,
    pending_hedge_symbols: set[str] | None = None,
    execution_environment: str = "demo",
) -> None:
    unit = [-0.002, 0.002] * 45
    btc = [0.01, -0.01] * 45
    eth = [None] * 90 if warm_eth is None else warm_eth
    through = model_prior_through or datetime.now(timezone.utc).date() - timedelta(days=1)
    dates = tuple(through - timedelta(days=len(unit) - index - 1) for index in range(len(unit)))
    model_prior = HedgeModelPrior(
        dates=dates,
        unit_returns=tuple(unit),
        btc_returns=tuple(btc),
        eth_returns=tuple(eth),
        data_through_date=through,
        source_summary_sha256="a" * 64,
        artifact_sha256="b" * 64,
    )
    monkeypatch.setattr(hedge_runner, "REPO", tmp_path)
    monkeypatch.setattr(hedge_runner, "load_hedge_model_prior", lambda path: model_prior)

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
    args = list(argv or [])
    if "--execution-environment" not in args:
        args[:0] = ["--execution-environment", execution_environment]
    if "--operational-profile-file" not in args:
        args.extend(("--operational-profile-file", str(OPERATIONAL_PROFILE)))
    if account_root:
        args.extend(("--account-root", str(tmp_path / "account")))
    if account_inbox and "--account-inbox-root" not in args:
        args.extend(("--account-inbox-root", str(tmp_path / "inbox")))
    if account_root and account_inbox:
        ensure_account_route(
            account_id=f"bybit-{execution_environment}-unified",
            environment=execution_environment,
            account_root=tmp_path / "account",
            inbox_root=tmp_path / "inbox",
        )
    monkeypatch.setattr(sys, "argv", ["run_continuous_hedge.py", *args])


def _execute_args(tmp_path) -> list[str]:
    return [
        "--execute",
        "--btc-price",
        "100000",
        "--eth-price",
        "3000",
        "--account-inbox-root",
        str(tmp_path / "inbox"),
    ]


def test_validate_model_prior_accepts_old_immutable_tape_without_account_route(
    monkeypatch, tmp_path, capsys
) -> None:
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=["--validate-model-prior-only"],
        model_prior_through=date(2026, 7, 9),
        account_root=False,
        account_inbox=False,
    )

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "model_prior_valid"
    assert out["model_prior_data_through_date"] == "2026-07-09"
    assert out["model_prior_live_extension"] is False


def test_validate_model_prior_rejects_malformed_tape(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=["--validate-model-prior-only"],
        account_root=False,
        account_inbox=False,
    )
    monkeypatch.setattr(
        hedge_runner,
        "load_hedge_model_prior",
        lambda path: (_ for _ in ()).throw(ValueError("malformed prior")),
    )

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "model_prior_invalid"
    assert out["error"] == "malformed prior"


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
        argv=_execute_args(tmp_path),
        pending_hedge_symbols={"BTCUSDT", "ETHUSDT"},
    )
    monkeypatch.setattr(
        hedge_runner,
        "compute_hedge_decision_2f",
        lambda cfg, **kwargs: _two_factor_decision(),
    )

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "execute_no_action"
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
        argv=["--execute", "--btc-price", "100000"],
        account_inbox=False,
    )

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "account_route_config_missing"


def test_btc_fallback_publishes_btc_target_and_explicit_eth_flatten(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=_execute_args(tmp_path),
        hedge_qty={"ETHUSDT": 2.0},
    )
    monkeypatch.setattr(
        hedge_runner,
        "compute_hedge_decision_2f",
        lambda cfg, **kwargs: _two_factor_decision(
            target_btc_usdt=500.0,
            target_eth_usdt=0.0,
            fell_back_to_btc=True,
        ),
    )

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "target_queued"
    assert out["mode"] == "target_publish"
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
        item["intent"]["leverage"] == 2.0
        for request in requests
        for item in request["intents"]
    )
    assert all(
        item["intent"]["metadata"]["model_prior_artifact_sha256"] == "b" * 64
        for request in requests
        for item in request["intents"]
    )
    assert len(out["queued"]["request_ids"]) == 2


def test_two_factor_targets_are_published_as_one_batch(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=[*_execute_args(tmp_path), "--eth-price", "3000"],
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


def test_paper_execution_publishes_only_to_bound_paper_route(
    monkeypatch, tmp_path, capsys
) -> None:
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=_execute_args(tmp_path),
        execution_environment="paper",
    )
    monkeypatch.setattr(
        hedge_runner,
        "compute_hedge_decision_2f",
        lambda cfg, **kwargs: _two_factor_decision(),
    )

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["execution_environment"] == "paper"
    assert out["status"] == "target_queued"
    route = json.loads((tmp_path / "account" / "account_route.json").read_text())
    assert route["account_id"] == "bybit-paper-unified"
    assert route["environment"] == "paper"


def test_execution_environment_is_required(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_continuous_hedge.py"])

    with pytest.raises(SystemExit) as excinfo:
        hedge_runner.main()

    assert excinfo.value.code == 2


def test_missing_canonical_equity_blocks_publish(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=_execute_args(tmp_path),
        equity_usdt=0.0,
    )

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "execute_blocked_account_owner_unhealthy"
    assert list((tmp_path / "inbox" / "pending").glob("*.json")) == []


def test_missing_btc_price_is_explicit_in_dry_run(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(monkeypatch, tmp_path)

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "dry_run_btc_price_unavailable"


def test_old_model_prior_does_not_block_protective_target(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=_execute_args(tmp_path),
        model_prior_through=datetime.now(timezone.utc).date() - timedelta(days=10),
    )
    monkeypatch.setattr(
        hedge_runner,
        "compute_hedge_decision_2f",
        lambda cfg, **kwargs: _two_factor_decision(
            plan_btc=_resize_plan(),
            target_btc_usdt=500.0,
            target_eth_usdt=0.0,
            fell_back_to_btc=True,
        ),
    )

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "target_queued"
    assert {row["symbol"] for row in out["queued"]["targets"]} == {"BTCUSDT", "ETHUSDT"}
    assert out["model_prior_age_days_informational"] == 10
    assert out["model_prior_live_extension"] is False


def test_old_model_prior_preserves_risk_reducing_target(monkeypatch, tmp_path, capsys) -> None:
    reduce = _resize_plan(
        side="Sell",
        reduce_only=True,
        current_notional=700.0,
        target_notional=500.0,
    )
    _setup_runner(
        monkeypatch,
        tmp_path,
        argv=_execute_args(tmp_path),
        model_prior_through=datetime.now(timezone.utc).date() - timedelta(days=10),
    )
    monkeypatch.setattr(
        hedge_runner,
        "compute_hedge_decision_2f",
        lambda cfg, **kwargs: _two_factor_decision(
            plan_btc=reduce,
            target_btc_usdt=500.0,
            target_eth_usdt=0.0,
            fell_back_to_btc=True,
        ),
    )

    assert hedge_runner.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "target_queued"
    assert out["queued"]["target_count"] == 2


def test_target_publish_failure_fails_armed_run(monkeypatch, tmp_path, capsys) -> None:
    _setup_runner(monkeypatch, tmp_path, argv=_execute_args(tmp_path))
    monkeypatch.setattr(
        hedge_runner,
        "_publish_hedge_target_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert hedge_runner.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "target_publish_failed"
    assert out["publish_error"] == "disk full"
