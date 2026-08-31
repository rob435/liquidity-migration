from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.core.venue_realm import VenueRealm
from liquidity_migration.data.candidate_universe import (
    build_candidate_universe_artifact,
    build_profile_universe_tables,
    carry_profile_universe_inputs,
    long_profile_universe_inputs,
    strategy_instruments_universe_inputs,
    write_candidate_universe,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_NS = 1_800_000_000_000_000_000


def _instrument(
    symbol: str,
    *,
    launch_time: str = "1700000000000",
    prelisting: bool = False,
    symbol_type: object = "",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "contractType": "LinearPerpetual",
        "status": "Trading",
        "symbolType": symbol_type,
        "baseCoin": symbol.removesuffix("USDT"),
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "launchTime": launch_time,
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
        "isPreListing": prelisting,
    }


def _ticker(symbol: str, turnover: str) -> dict[str, object]:
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


def _artifact(*, realm: VenueRealm = VenueRealm.DEMO) -> dict[str, Any]:
    return build_candidate_universe_artifact(
        [
            _instrument("AAAUSDT"),
            _instrument("BBBUSDT"),
            _instrument("CCCUSDT", prelisting=True),
            _instrument("USDCUSDT"),
        ],
        [
            _ticker("AAAUSDT", "3000000"),
            _ticker("BBBUSDT", "1000000"),
            _ticker("CCCUSDT", "9000000"),
            _ticker("USDCUSDT", "9000000"),
            _ticker("TICKERONLYUSDT", "9000000"),
        ],
        snapshot_ts_ns=SNAPSHOT_NS,
        long_universe_superset_size=120,
        realm=realm,
    )


def test_build_records_populations_and_exact_exclusion_reasons() -> None:
    payload = _artifact()

    assert payload["schema_version"] == 5
    assert payload["environment"] == "demo"
    assert payload["endpoint"] == "api-demo.bybit.com"
    assert payload["symbols"] == ["AAAUSDT", "BBBUSDT"]
    assert payload["strategy_instruments"] == ["AAAUSDT", "BBBUSDT"]
    assert payload["profile_eligible_symbols"] == {
        "long": ["AAAUSDT"],
        "carry": ["AAAUSDT", "BBBUSDT"],
    }
    decisions = {row["symbol"]: row for row in payload["decisions"]}
    assert decisions["BBBUSDT"]["populations"]["long"] == {
        "included": False,
        "reasons": ["turnover_below_floor"],
    }
    assert decisions["CCCUSDT"]["populations"]["strategy_instruments"][
        "reasons"
    ] == ["prelisting"]
    assert decisions["USDCUSDT"]["populations"]["strategy_instruments"][
        "reasons"
    ] == ["excluded_by_config"]
    assert decisions["TICKERONLYUSDT"]["populations"]["strategy_instruments"][
        "reasons"
    ] == ["missing_instrument"]


def test_profiles_are_strict_subsets_of_the_instrument_population() -> None:
    snapshot_ms = SNAPSHOT_NS // 1_000_000
    instruments = [
        _instrument("AAAUSDT"),
        _instrument("BBBUSDT"),
        _instrument(
            "NEWUSDT",
            launch_time=str(snapshot_ms - 3 * 86_400_000),
        ),
    ]
    tickers = [
        _ticker("AAAUSDT", "3000000"),
        _ticker("BBBUSDT", "1000000"),
        _ticker("NEWUSDT", "9000000"),
    ]
    tables = build_profile_universe_tables(
        instruments,
        tickers,
        population_inputs={
            "long": long_profile_universe_inputs(120),
            "carry": carry_profile_universe_inputs(),
            "strategy_instruments": strategy_instruments_universe_inputs(),
        },
        snapshot_ts_ms=snapshot_ms,
    )
    populations = {
        name: set(table["symbol"].to_list()) for name, table in tables.items()
    }

    assert populations["long"] < populations["carry"] < populations[
        "strategy_instruments"
    ]


def test_non_crypto_products_are_excluded_before_liquidity_ranking() -> None:
    instruments = [
        _instrument("AAAUSDT"),
        _instrument("INNOVUSDT", symbol_type="innovation"),
        _instrument("AAOIUSDT", symbol_type="stock"),
    ]
    payload = build_candidate_universe_artifact(
        instruments,
        [
            _ticker("AAAUSDT", "10000000"),
            _ticker("INNOVUSDT", "20000000"),
            _ticker("AAOIUSDT", "100000000"),
        ],
        snapshot_ts_ns=SNAPSHOT_NS,
        long_universe_superset_size=1,
    )

    assert payload["symbols"] == ["AAAUSDT", "INNOVUSDT"]
    assert payload["profile_eligible_symbols"]["long"] == ["INNOVUSDT"]
    assert payload["excluded_instrument_rows"] == [
        {
            "row_index": 2,
            "symbol": "AAOIUSDT",
            "symbol_type": "stock",
            "reason": "outside_crypto_perp_strategy_domain",
        }
    ]


def test_builder_rejects_invalid_or_duplicate_source_rows() -> None:
    with pytest.raises(ValueError, match="symbolType"):
        build_candidate_universe_artifact(
            [_instrument("AAAUSDT", symbol_type=7)],
            [_ticker("AAAUSDT", "3000000")],
            snapshot_ts_ns=SNAPSHOT_NS,
            long_universe_superset_size=120,
        )
    with pytest.raises(ValueError, match="duplicate symbol"):
        build_candidate_universe_artifact(
            [_instrument("AAAUSDT"), _instrument("AAAUSDT")],
            [_ticker("AAAUSDT", "3000000")],
            snapshot_ts_ns=SNAPSHOT_NS,
            long_universe_superset_size=120,
        )


def test_builder_records_noncanonical_ticker_source_rejection() -> None:
    synthetic = _ticker("WC_ENG_ARG_USDT-15JUL26", "14932.6400")
    payload = build_candidate_universe_artifact(
        [_instrument("AAAUSDT")],
        [_ticker("AAAUSDT", "3000000"), synthetic],
        snapshot_ts_ns=SNAPSHOT_NS,
        long_universe_superset_size=120,
    )

    assert payload["symbols"] == ["AAAUSDT"]
    assert payload["rejected_ticker_rows"] == [
        {
            "row_index": 1,
            "raw_symbol": "WC_ENG_ARG_USDT-15JUL26",
            "reason": "noncanonical_ticker_only_symbol",
        }
    ]


def test_write_is_owner_only_and_never_overwrites(tmp_path: Path) -> None:
    path = write_candidate_universe(tmp_path / "candidate.json", _artifact())

    assert os.stat(path).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_candidate_universe(path, _artifact())


class _RecordingMarket:
    instances: list[_RecordingMarket] = []

    def __init__(self, *, demo: bool = False, rate_limiter: Any = None) -> None:
        self.demo = demo
        self.rate_limiter = rate_limiter
        self.instances.append(self)

    def get_instruments_info(
        self, *, require_complete: bool = False
    ) -> list[dict[str, object]]:
        assert require_complete
        return [_instrument("AAAUSDT"), _instrument("BBBUSDT")]

    def get_tickers(self) -> list[dict[str, object]]:
        return [_ticker("AAAUSDT", "3000000"), _ticker("BBBUSDT", "9000000")]


def _freeze_module() -> Any:
    path = REPO_ROOT / "scripts" / "maintain" / "freeze_account_candidate_universe.py"
    spec = importlib.util.spec_from_file_location("freeze_candidate_universe_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_freeze_script_requires_an_explicit_realm(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _freeze_module().main(["--output", str(tmp_path / "candidate.json")])
    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    ("realm", "endpoint", "demo"),
    [
        ("demo", "api-demo.bybit.com", True),
        ("mainnet", "api.bybit.com", False),
    ],
)
def test_freeze_script_selects_and_stamps_the_realm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    realm: str,
    endpoint: str,
    demo: bool,
) -> None:
    module = _freeze_module()
    _RecordingMarket.instances = []
    monkeypatch.setattr(module, "BybitMarketData", _RecordingMarket)
    output = tmp_path / "candidate.json"

    assert module.main(["--realm", realm, "--output", str(output)]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len(_RecordingMarket.instances) == 1
    assert _RecordingMarket.instances[0].demo is demo
    assert payload["environment"] == realm
    assert payload["endpoint"] == endpoint
