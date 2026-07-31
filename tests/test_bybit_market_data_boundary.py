from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
PUBLIC_MODULE = REPO / "liquidity_migration" / "marketdata" / "bybit_market_data.py"
PRIVATE_MODULE = REPO / "liquidity_migration" / "venue" / "bybit.py"
OWNER_ADAPTER_MODULE = REPO / "liquidity_migration" / "venue" / "bybit_execution_adapter.py"
OWNER_RUNNER_MODULE = REPO / "liquidity_migration" / "runtime" / "account_service_runner.py"
ACTIVE_MARKET_DATA_PRODUCERS = (
    "strategy/continuous_demo.py",
    "strategy/continuous_demo_daemon.py",
    "data/downloaders.py",
    "strategy/event_demo_data.py",
    "marketdata/kline_stream_manager.py",
    "strategy/long_native_event_demo.py",
    "strategy/long_native_event_demo_daemon.py",
    "data/universe.py",
)
PUBLIC_PROCESS_MODULES = (
    "liquidity_migration.marketdata.bybit_market_data",
    "liquidity_migration.account.execution_adapters",
    "liquidity_migration.research.backtest.continuous_events",
    "liquidity_migration.research.backtest.long_native",
    "liquidity_migration.strategy.continuous_demo",
    "liquidity_migration.strategy.continuous_demo_daemon",
    "liquidity_migration.strategy.long_native_event_demo",
    "liquidity_migration.strategy.long_native_event_demo_daemon",
)
NEUTRAL_ACCOUNT_MODULES = (
    "liquidity_migration.policy.account_execution_config",
    "liquidity_migration.runtime.account_paper_runner",
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_public_market_data_module_has_no_private_account_surface() -> None:
    tree = _tree(PUBLIC_MODULE)
    imported_modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    called_attributes = {
        node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    referenced_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

    assert "liquidity_migration.account.account_owner_lease" not in imported_modules
    assert "liquidity_migration.venue.bybit" not in imported_modules
    assert "BybitPrivateClient" not in class_names
    assert "BybitPrivateWebSocketStream" not in class_names
    assert not {
        "place_order",
        "amend_order",
        "cancel_order",
        "set_leverage",
        "set_trading_stop",
    }.intersection(called_attributes)
    assert not {
        "api_key",
        "api_secret",
        "BYBIT_DEMO_API_KEY",
        "BYBIT_DEMO_API_SECRET",
        "BYBIT_REAL_API_KEY",
        "BYBIT_REAL_API_SECRET",
        "REAL_MONEY",
    }.intersection(referenced_names)


def test_active_market_data_producers_import_the_public_plane_directly() -> None:
    for filename in ACTIVE_MARKET_DATA_PRODUCERS:
        path = REPO / "liquidity_migration" / filename
        imports = {
            node.module
            for node in ast.walk(_tree(path))
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert "liquidity_migration.marketdata.bybit_market_data" in imports, filename
        assert "liquidity_migration.venue.bybit" not in imports, filename


@pytest.mark.parametrize("module", PUBLIC_PROCESS_MODULES)
def test_public_and_target_modules_do_not_transitively_load_private_execution(module: str) -> None:
    forbidden = (
        "liquidity_migration.venue.bybit",
        "liquidity_migration.account.account_owner_lease",
        "liquidity_migration.venue.bybit_execution_adapter",
    )
    code = (
        "import importlib, sys; "
        f"importlib.import_module({module!r}); "
        f"forbidden={forbidden!r}; "
        "present=[name for name in forbidden if name in sys.modules]; "
        "sys.exit('private modules loaded: ' + ','.join(present)) if present else None"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, f"{module}: {proc.stderr or proc.stdout}"


@pytest.mark.parametrize("module", NEUTRAL_ACCOUNT_MODULES)
def test_paper_and_shared_config_do_not_load_demo_owner(module: str) -> None:
    forbidden = (
        "liquidity_migration.venue.bybit",
        "liquidity_migration.venue.bybit_execution_adapter",
        "liquidity_migration.runtime.account_service_runner",
    )
    code = (
        "import importlib, sys; "
        f"importlib.import_module({module!r}); "
        f"forbidden={forbidden!r}; "
        "present=[name for name in forbidden if name in sys.modules]; "
        "sys.exit('demo owner loaded: ' + ','.join(present)) if present else None"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, f"{module}: {proc.stderr or proc.stdout}"


def test_private_bybit_module_does_not_export_public_market_data() -> None:
    private = importlib.import_module("liquidity_migration.venue.bybit")
    public_names = {
        "INTERVAL_MS",
        "BybitKlineStreamPool",
        "BybitMarketData",
        "BybitPublicTickerStream",
        "BybitRestRateLimiter",
    }
    assert public_names.isdisjoint(private.__all__)
    assert not {name for name in public_names if hasattr(private, name)}


def test_demo_owner_reaches_only_the_owner_adapter_with_a_leased_private_client() -> None:
    runner_tree = _tree(OWNER_RUNNER_MODULE)
    runner_imports = {
        node.module for node in ast.walk(runner_tree) if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "liquidity_migration.venue.bybit_execution_adapter" in runner_imports
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "liquidity_migration.account.execution_adapters"
        and any(alias.name == "BybitDemoExecutionAdapter" for alias in node.names)
        for node in ast.walk(runner_tree)
    )

    leased_clients = [
        node
        for node in ast.walk(runner_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "BybitPrivateClient"
        and any(
            keyword.arg == "mutation_lease"
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == "owner_lease"
            for keyword in node.keywords
        )
    ]
    assert len(leased_clients) == 1
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "BybitDemoExecutionAdapter"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "private_client"
        for node in ast.walk(runner_tree)
    )

    adapter_tree = _tree(OWNER_ADAPTER_MODULE)
    adapter_imports = {
        node.module for node in ast.walk(adapter_tree) if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    adapter_calls = {
        node.func.attr
        for node in ast.walk(adapter_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "liquidity_migration.marketdata.bybit_errors" in adapter_imports
    assert "liquidity_migration.venue.bybit" not in adapter_imports
    assert {"place_order", "set_leverage"}.issubset(adapter_calls)

    private_tree = _tree(PRIVATE_MODULE)
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "liquidity_migration.account.account_owner_lease"
        and any(alias.name == "DemoAccountMutationLease" for alias in node.names)
        for node in ast.walk(private_tree)
    )
