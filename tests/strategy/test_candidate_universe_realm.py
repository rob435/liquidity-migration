from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.strategy.account_candidate_universe import (
    build_candidate_universe_artifact,
    load_candidate_universe,
    write_candidate_universe,
)
from liquidity_migration.strategy.carry_demo import _candidate_filtered_universe
from liquidity_migration.account.execution_environment import candidate_universe_realm
from liquidity_migration.strategy.long_native_event_demo import LongNativeDemoCycleConfig
from liquidity_migration.core.venue_realm import VenueRealm


REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_NS = 1_800_000_000_000_000_000
PRODUCER_MODULES = (
    "liquidity_migration/ops/candidate_rule_coverage.py",
    "liquidity_migration/strategy/carry_demo.py",
    "liquidity_migration/strategy/event_demo_data.py",
    "liquidity_migration/strategy/long_native_event_demo.py",
    "scripts/maintain/freeze_venue_instrument_rules.py",
    "scripts/maintain/probe_bybit_demo_rules.py",
)


def _instrument(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "contractType": "LinearPerpetual",
        "status": "Trading",
        "symbolType": "",
        "baseCoin": symbol.removesuffix("USDT"),
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "launchTime": "1700000000000",
        "deliveryTime": "0",
        "priceFilter": {"tickSize": "0.1"},
        "lotSizeFilter": {
            "qtyStep": "0.01",
            "minOrderQty": "0.01",
            "minNotionalValue": "5",
            "maxOrderQty": "1000",
            "maxMktOrderQty": "500",
        },
        "fundingInterval": "480",
        "isPreListing": False,
    }


def _ticker(symbol: str, turnover: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "lastPrice": "10",
        "markPrice": "10",
        "indexPrice": "10",
        "bid1Price": "9.9",
        "ask1Price": "10.1",
        "turnover24h": turnover,
        "volume24h": "100000",
        "openInterest": "100",
        "openInterestValue": "1000",
        "fundingRate": "0.0001",
    }


_INSTRUMENTS = [_instrument("AAAUSDT"), _instrument("BBBUSDT")]
_TICKERS = [_ticker("AAAUSDT", "3000000"), _ticker("BBBUSDT", "9000000")]


class _RecordingMarket:
    """Stand-in for BybitMarketData; records the realm-derived constructor flags."""

    instances: list[_RecordingMarket] = []

    def __init__(self, *, demo: bool = False, rate_limiter: Any = None) -> None:
        self.demo = demo
        self.rate_limiter = rate_limiter
        _RecordingMarket.instances.append(self)

    def get_instruments_info(self, *, require_complete: bool = False) -> list[dict[str, Any]]:
        assert require_complete
        return _INSTRUMENTS

    def get_tickers(self) -> list[dict[str, Any]]:
        return _TICKERS


def _freeze_module() -> Any:
    path = REPO_ROOT / "scripts" / "maintain" / "freeze_account_candidate_universe.py"
    spec = importlib.util.spec_from_file_location(
        "freeze_account_candidate_universe_test", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _artifact(realm: VenueRealm) -> dict[str, Any]:
    return build_candidate_universe_artifact(
        _INSTRUMENTS,
        _TICKERS,
        snapshot_ts_ns=SNAPSHOT_NS,
        long_config=LongNativeDemoCycleConfig(),
        realm=realm,
    )


def _frozen_file(tmp_path: Path, realm: VenueRealm) -> Path:
    return write_candidate_universe(tmp_path / f"candidate-{realm.value}.json", _artifact(realm))


def test_no_producer_loads_a_candidate_universe_at_the_default_realm() -> None:
    """The loader defaults to demo, so an unnamed realm only fails on mainnet."""

    unnamed: list[str] = []
    for relative in PRODUCER_MODULES:
        tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func.id if isinstance(node.func, ast.Name) else ""
            if called != "load_candidate_universe":
                continue
            if not any(keyword.arg == "realm" for keyword in node.keywords):
                unnamed.append(f"{relative}:{node.lineno}")
    assert unnamed == []


def test_candidate_universe_realm_maps_every_environment() -> None:
    assert candidate_universe_realm("demo") is VenueRealm.DEMO
    assert candidate_universe_realm("mainnet") is VenueRealm.MAINNET
    # The retired paper twin is no longer a parseable environment.
    with pytest.raises(ValueError, match="execution_environment"):
        candidate_universe_realm("paper")
    with pytest.raises(ValueError, match="execution_environment"):
        candidate_universe_realm("")


@pytest.mark.parametrize("argv", [[], ["--realm", "testnet"]])
def test_freeze_script_requires_an_explicit_realm(tmp_path: Path, argv: list[str]) -> None:
    module = _freeze_module()
    with pytest.raises(SystemExit) as excinfo:
        module.main([*argv, "--output", str(tmp_path / "candidate.json")])
    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    ("realm", "endpoint"),
    [(VenueRealm.DEMO, "api-demo.bybit.com"), (VenueRealm.MAINNET, "api.bybit.com")],
)
def test_freeze_script_selects_the_client_and_stamp_for_each_realm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    realm: VenueRealm,
    endpoint: str,
) -> None:
    module = _freeze_module()
    monkeypatch.setattr(module, "BybitMarketData", _RecordingMarket)
    _RecordingMarket.instances = []
    output = tmp_path / "candidate.json"

    assert module.main(["--realm", realm.value, "--output", str(output)]) == 0

    assert len(_RecordingMarket.instances) == 1
    assert _RecordingMarket.instances[0].demo is (realm is VenueRealm.DEMO)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["environment"] == realm.value
    assert payload["endpoint"] == endpoint
    summary = json.loads(capsys.readouterr().out)
    assert summary["realm"] == realm.value
    assert summary["endpoint"] == endpoint
    # The stamp is the loader's contract, not decoration.
    assert load_candidate_universe(output, realm=realm).symbols == ("AAAUSDT", "BBBUSDT")


def test_mainnet_artifact_loads_for_mainnet_and_is_refused_for_demo(tmp_path: Path) -> None:
    path = _frozen_file(tmp_path, VenueRealm.MAINNET)
    assert load_candidate_universe(path, realm=VenueRealm.MAINNET).symbols == (
        "AAAUSDT",
        "BBBUSDT",
    )
    with pytest.raises(ValueError, match="is for realm 'mainnet', not 'demo'"):
        load_candidate_universe(path, realm=VenueRealm.DEMO)
    # The default is demo, which is exactly why every producer must name its realm.
    with pytest.raises(ValueError, match="is for realm 'mainnet', not 'demo'"):
        load_candidate_universe(path)


def test_carry_producer_reads_the_artifact_of_its_own_environment(tmp_path: Path) -> None:
    mainnet = _frozen_file(tmp_path, VenueRealm.MAINNET)
    demo = _frozen_file(tmp_path, VenueRealm.DEMO)

    assert _candidate_filtered_universe(
        ["AAAUSDT", "ZZZUSDT"],
        candidate_universe_file=str(mainnet),
        realm=candidate_universe_realm("mainnet"),
        standing_symbols=set(),
    ) == (["AAAUSDT"], 1)
    assert _candidate_filtered_universe(
        ["AAAUSDT", "ZZZUSDT"],
        candidate_universe_file=str(demo),
        realm=candidate_universe_realm("demo"),
        standing_symbols=set(),
    ) == (["AAAUSDT"], 1)
    with pytest.raises(ValueError, match="is for realm 'demo', not 'mainnet'"):
        _candidate_filtered_universe(
            ["AAAUSDT"],
            candidate_universe_file=str(demo),
            realm=candidate_universe_realm("mainnet"),
            standing_symbols=set(),
        )
