from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from liquidity_migration.account_candidate_universe import (
    build_candidate_universe_artifact,
    enforce_frozen_candidate_population,
    load_candidate_universe,
    write_candidate_universe,
)
from liquidity_migration.continuous_demo import ContinuousDemoCycleConfig
from liquidity_migration.deterministic_serialization import canonical_json
from liquidity_migration.long_native_event_demo import LongNativeDemoCycleConfig


SNAPSHOT_NS = 1_800_000_000_000_000_000


def _instrument(
    symbol: str,
    *,
    status: str = "Trading",
    launch_time: str = "1700000000000",
    prelisting: bool = False,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "contractType": "LinearPerpetual",
        "status": status,
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


def _payload() -> dict[str, object]:
    instruments = [
        _instrument("AAAUSDT"),
        _instrument("BBBUSDT"),
        _instrument("CCCUSDT", prelisting=True),
        _instrument("USDCUSDT"),
    ]
    tickers = [
        _ticker("AAAUSDT", "3000000"),
        _ticker("BBBUSDT", "1000000"),
        _ticker("CCCUSDT", "9000000"),
        _ticker("USDCUSDT", "9000000"),
        _ticker("TICKERONLYUSDT", "9000000"),
    ]
    return build_candidate_universe_artifact(
        instruments,
        tickers,
        snapshot_ts_ns=SNAPSHOT_NS,
        long_config=LongNativeDemoCycleConfig(),
        continuous_config=ContinuousDemoCycleConfig(),
    )


def test_build_records_union_and_exact_exclusion_reasons() -> None:
    payload = _payload()
    assert payload["symbols"] == ["AAAUSDT", "BBBUSDT"]
    assert payload["profile_eligible_symbols"] == {
        "long": ["AAAUSDT"],
        "continuous": ["AAAUSDT", "BBBUSDT"],
    }
    decisions = {row["symbol"]: row for row in payload["decisions"]}
    assert decisions["BBBUSDT"]["profiles"]["long"] == {
        "included": False,
        "reasons": ["turnover_below_floor"],
    }
    assert decisions["CCCUSDT"]["profiles"]["continuous"]["reasons"] == [
        "prelisting"
    ]
    assert decisions["USDCUSDT"]["profiles"]["continuous"]["reasons"] == [
        "excluded_by_config"
    ]
    assert decisions["TICKERONLYUSDT"]["profiles"]["continuous"]["reasons"] == [
        "missing_instrument"
    ]


def test_write_load_and_enforce_population(tmp_path: Path) -> None:
    path = write_candidate_universe(tmp_path / "candidate.json", _payload())
    assert os.stat(path).st_mode & 0o777 == 0o600
    frozen = load_candidate_universe(path)
    assert frozen.symbols == ("AAAUSDT", "BBBUSDT")
    # A post-freeze listing is ignored, not admitted.
    assert enforce_frozen_candidate_population(
        ["NEWUSDT", "BBBUSDT", "AAAUSDT"],
        frozen,
        context="test",
    ) == ("AAAUSDT", "BBBUSDT")
    with pytest.raises(RuntimeError, match="lost 1 symbol"):
        enforce_frozen_candidate_population(
            ["AAAUSDT", "NEWUSDT"],
            frozen,
            context="test",
        )
    with pytest.raises(FileExistsError):
        write_candidate_universe(path, _payload())


def test_loader_rejects_tamper_symlink_and_open_permissions(tmp_path: Path) -> None:
    path = write_candidate_universe(tmp_path / "candidate.json", _payload())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["symbols"].append("EVILUSDT")
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(ValueError, match="artifact_sha256"):
        load_candidate_universe(path)

    clean = write_candidate_universe(tmp_path / "clean.json", _payload())
    os.chmod(clean, 0o644)
    with pytest.raises(ValueError, match="group/world"):
        load_candidate_universe(clean)
    os.chmod(clean, 0o600)
    link = tmp_path / "link.json"
    link.symlink_to(clean)
    with pytest.raises(ValueError, match="non-symlink"):
        load_candidate_universe(link)


def test_builder_rejects_duplicate_raw_symbol() -> None:
    with pytest.raises(ValueError, match="duplicate symbol"):
        build_candidate_universe_artifact(
            [_instrument("AAAUSDT"), _instrument("AAAUSDT")],
            [_ticker("AAAUSDT", "3000000")],
            snapshot_ts_ns=SNAPSHOT_NS,
            long_config=LongNativeDemoCycleConfig(),
            continuous_config=ContinuousDemoCycleConfig(),
        )


def test_builder_records_noncanonical_ticker_only_source_rejection(
    tmp_path: Path,
) -> None:
    synthetic = _ticker("WC_ENG_ARG_USDT-15JUL26", "14932.6400")
    synthetic.update({
        "deliveryTime": "1784073600000",
        "predictedDeliveryPrice": "",
        "curPreListingPhase": "",
    })
    payload = build_candidate_universe_artifact(
        [_instrument("AAAUSDT")],
        [_ticker("AAAUSDT", "3000000"), synthetic],
        snapshot_ts_ns=SNAPSHOT_NS,
        long_config=LongNativeDemoCycleConfig(),
        continuous_config=ContinuousDemoCycleConfig(),
    )

    assert payload["schema_version"] == 2
    assert payload["symbols"] == ["AAAUSDT"]
    assert payload["raw_source"]["ticker_rows"] == 2
    assert payload["raw_snapshot"]["ticker_rows"][1] == synthetic
    assert payload["rejected_ticker_rows"] == [{
        "row_index": 1,
        "raw_symbol": "WC_ENG_ARG_USDT-15JUL26",
        "reason": "noncanonical_ticker_only_symbol",
    }]
    assert {row["symbol"] for row in payload["decisions"]} == {"AAAUSDT"}
    assert load_candidate_universe(
        write_candidate_universe(tmp_path / "candidate.json", payload)
    ).symbols == ("AAAUSDT",)

    with pytest.raises(ValueError, match="invalid candidate-universe symbol"):
        build_candidate_universe_artifact(
            [_instrument("WC_ENG_ARG_USDT-15JUL26")],
            [synthetic],
            snapshot_ts_ns=SNAPSHOT_NS,
            long_config=LongNativeDemoCycleConfig(),
            continuous_config=ContinuousDemoCycleConfig(),
        )

    with pytest.raises(ValueError, match="duplicate symbol"):
        build_candidate_universe_artifact(
            [_instrument("AAAUSDT")],
            [synthetic, dict(synthetic)],
            snapshot_ts_ns=SNAPSHOT_NS,
            long_config=LongNativeDemoCycleConfig(),
            continuous_config=ContinuousDemoCycleConfig(),
        )

    with pytest.raises(ValueError, match="invalid candidate-universe symbol"):
        build_candidate_universe_artifact(
            [_instrument("AAAUSDT")],
            [{"turnover24h": "3000000"}],
            snapshot_ts_ns=SNAPSHOT_NS,
            long_config=LongNativeDemoCycleConfig(),
            continuous_config=ContinuousDemoCycleConfig(),
        )


def test_loader_recomputes_noncanonical_ticker_source_rejections(
    tmp_path: Path,
) -> None:
    synthetic = _ticker("WC_ENG_ARG_USDT-15JUL26", "14932.6400")
    payload = build_candidate_universe_artifact(
        [_instrument("AAAUSDT")],
        [_ticker("AAAUSDT", "3000000"), synthetic],
        snapshot_ts_ns=SNAPSHOT_NS,
        long_config=LongNativeDemoCycleConfig(),
        continuous_config=ContinuousDemoCycleConfig(),
    )
    payload["rejected_ticker_rows"][0]["reason"] = "silently_ignored"
    payload["artifact_sha256"] = ""
    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    path = write_candidate_universe(tmp_path / "candidate.json", payload)

    with pytest.raises(ValueError, match="rejected ticker rows are inconsistent"):
        load_candidate_universe(path)
