from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from liquidity_migration.strategy.account_candidate_universe import (
    account_exposure_labels,
    build_candidate_universe_artifact,
    build_profile_universe_tables,
    carry_profile_universe_inputs,
    enforce_frozen_candidate_frames,
    load_candidate_universe,
    long_profile_universe_inputs,
    require_scheduled_retirements_flat,
    scheduled_retirement_exposure,
    strategy_instruments_universe_inputs,
    write_candidate_universe,
)
from liquidity_migration.account.account_intent_client import (
    AccountTargetPublisher,
    requested_target,
)
from liquidity_migration.account.account_route import ensure_account_route
from liquidity_migration.account.account_service import SleeveAdapterKind
from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.data.downloaders import _normalize_instruments, _normalize_tickers
from liquidity_migration.strategy.long_native_event_demo import LongNativeDemoCycleConfig


SNAPSHOT_NS = 1_800_000_000_000_000_000


def _instrument(
    symbol: str,
    *,
    status: str = "Trading",
    launch_time: str = "1700000000000",
    prelisting: bool = False,
    symbol_type: object = "",
    delivery_time: str = "0",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "contractType": "LinearPerpetual",
        "status": status,
        "symbolType": symbol_type,
        "baseCoin": symbol.removesuffix("USDT"),
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "launchTime": launch_time,
        "deliveryTime": delivery_time,
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
    )


def downgrade_payload_to_schema_four(payload: dict[str, object]) -> dict[str, object]:
    """Rewrite a schema-5 payload into the schema-4 shape it replaced.

    Schema 4 is no longer produced anywhere, so the converter and the loader
    refusal both need one built by hand. This reverses exactly the renames:
    the instrument set goes back under the retired sleeve's name, and the
    decision rows go back to ``profiles``/``included_in_union``.
    """

    old = json.loads(json.dumps(payload))
    instrument_inputs = old.pop("strategy_instruments_inputs")
    instrument_hash = old.pop("strategy_instruments_input_sha256")
    instruments = old.pop("strategy_instruments")
    old["schema_version"] = 4
    old["profile_inputs"]["continuous"] = instrument_inputs
    old["profile_input_sha256"]["continuous"] = instrument_hash
    old["profile_eligible_symbols"]["continuous"] = instruments
    for row in old["decisions"]:
        populations = row.pop("populations")
        populations["continuous"] = populations.pop("strategy_instruments")
        row["profiles"] = populations
        row["included_in_union"] = row.pop("included_in_strategy_instruments")
    old["artifact_sha256"] = ""
    old["artifact_sha256"] = hashlib.sha256(canonical_json(old)).hexdigest()
    return old


def test_schema_four_artifact_is_refused_and_names_the_converter(tmp_path: Path) -> None:
    """An operator meeting an old artifact must be told how to move it."""

    path = write_candidate_universe(
        tmp_path / "candidate.json",
        downgrade_payload_to_schema_four(_payload()),
    )
    with pytest.raises(ValueError, match="migrate_candidate_universe_schema.py"):
        load_candidate_universe(path)


def _payload_with_three_distinct_populations() -> dict[str, object]:
    """A snapshot where the three populations are genuinely different sizes.

    AAAUSDT is old and liquid, so every population holds it. BBBUSDT is under
    LONG's turnover floor. NEWUSDT is three days old, so it is under both
    sleeves' maturity floors but is still a listed perpetual. Anything
    checking that the populations relate correctly has to use a fixture like
    this: on one where they coincide, the check passes no matter what the
    code does.
    """

    snapshot_ms = SNAPSHOT_NS // 1_000_000
    return build_candidate_universe_artifact(
        [
            _instrument("AAAUSDT"),
            _instrument("BBBUSDT"),
            _instrument(
                "NEWUSDT",
                launch_time=str(snapshot_ms - 3 * 24 * 60 * 60 * 1_000),
            ),
        ],
        [
            _ticker("AAAUSDT", "3000000"),
            _ticker("BBBUSDT", "1000000"),
            _ticker("NEWUSDT", "9000000"),
        ],
        snapshot_ts_ns=SNAPSHOT_NS,
        long_config=LongNativeDemoCycleConfig(),
    )


def test_a_young_listing_is_tradable_but_outside_every_sleeve_profile() -> None:
    """The instrument set is strictly wider than the sleeve profiles."""

    payload = _payload_with_three_distinct_populations()

    assert payload["strategy_instruments"] == ["AAAUSDT", "BBBUSDT", "NEWUSDT"]
    assert payload["symbols"] == ["AAAUSDT", "BBBUSDT", "NEWUSDT"]
    assert payload["profile_eligible_symbols"] == {
        "long": ["AAAUSDT"],
        "carry": ["AAAUSDT", "BBBUSDT"],
    }
    decisions = {row["symbol"]: row for row in payload["decisions"]}
    assert decisions["NEWUSDT"]["populations"]["carry"]["reasons"] == [
        "listing_age_below_floor"
    ]
    assert decisions["NEWUSDT"]["included_in_strategy_instruments"] is True


def test_build_records_instrument_set_and_exact_exclusion_reasons() -> None:
    payload = _payload()
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
    assert decisions["CCCUSDT"]["populations"]["strategy_instruments"]["reasons"] == [
        "prelisting"
    ]
    assert decisions["USDCUSDT"]["populations"]["strategy_instruments"]["reasons"] == [
        "excluded_by_config"
    ]
    assert decisions["TICKERONLYUSDT"]["populations"]["strategy_instruments"][
        "reasons"
    ] == ["missing_instrument"]


def test_symbols_equal_what_the_old_three_way_union_produced() -> None:
    """The rename must not move one symbol.

    Schema 4 built ``symbols`` as ``long | continuous | carry``. Schema 5
    takes the instrument set directly. Those agree only because every sleeve
    profile is the instrument set with extra gates switched on, so each one
    sits inside it — this is the check that the claim still holds.
    """

    payload = _payload_with_three_distinct_populations()
    long_config = LongNativeDemoCycleConfig()
    # Recompute each population straight from the raw snapshot, so the union
    # this compares against does not come from the same field of the artifact
    # it is checking.
    tables = build_profile_universe_tables(
        payload["raw_snapshot"]["instrument_rows"],
        payload["raw_snapshot"]["ticker_rows"],
        population_inputs={
            "long": long_profile_universe_inputs(long_config),
            "carry": carry_profile_universe_inputs(),
            "strategy_instruments": strategy_instruments_universe_inputs(),
        },
        snapshot_ts_ms=SNAPSHOT_NS // 1_000_000,
    )
    populations = {
        name: set(table["symbol"].to_list()) for name, table in tables.items()
    }
    # A fixture where the populations coincide would pass this whatever the
    # code did, so refuse to run on one.
    assert populations["long"] < populations["carry"] < populations["strategy_instruments"]

    schema_four_union = (
        populations["long"]
        | populations["carry"]
        | populations["strategy_instruments"]
    )
    assert payload["symbols"] == sorted(schema_four_union)
    assert payload["strategy_instruments"] == sorted(schema_four_union)


def test_builder_excludes_non_crypto_products_before_liquidity_ranking(
    tmp_path: Path,
) -> None:
    instruments = [
        _instrument("AAAUSDT"),
        _instrument("INNOVUSDT", symbol_type="innovation"),
        _instrument("AAOIUSDT", symbol_type="stock"),
        _instrument("XAUUSDT", symbol_type="commodity"),
        _instrument("EURUSDT", symbol_type="forex"),
        _instrument("FUTUREUSDT", symbol_type="future_tradfi"),
    ]
    tickers = [
        _ticker("AAAUSDT", "10000000"),
        _ticker("INNOVUSDT", "20000000"),
        _ticker("AAOIUSDT", "100000000"),
        _ticker("XAUUSDT", "90000000"),
        _ticker("EURUSDT", "80000000"),
        _ticker("FUTUREUSDT", "70000000"),
    ]

    payload = build_candidate_universe_artifact(
        instruments,
        tickers,
        snapshot_ts_ns=SNAPSHOT_NS,
        long_config=replace(
            LongNativeDemoCycleConfig(),
            universe_superset_size=1,
        ),
    )

    assert payload["schema_version"] == 5
    assert payload["strategy_domain"] == "crypto_perpetuals"
    assert payload["strategy_symbol_types"] == ["", "innovation"]
    assert payload["symbols"] == ["AAAUSDT", "INNOVUSDT"]
    assert payload["strategy_instruments"] == ["AAAUSDT", "INNOVUSDT"]
    assert payload["profile_eligible_symbols"] == {
        "long": ["INNOVUSDT"],
        "carry": ["AAAUSDT", "INNOVUSDT"],
    }
    assert payload["raw_source"]["instrument_rows"] == 6
    assert payload["raw_source"]["strategy_instrument_rows"] == 2
    assert payload["raw_source"]["excluded_instrument_rows"] == 4
    assert payload["raw_snapshot"]["instrument_rows"] == instruments
    assert payload["excluded_instrument_rows"] == [
        {
            "row_index": 2,
            "symbol": "AAOIUSDT",
            "symbol_type": "stock",
            "reason": "outside_crypto_perp_strategy_domain",
        },
        {
            "row_index": 3,
            "symbol": "XAUUSDT",
            "symbol_type": "commodity",
            "reason": "outside_crypto_perp_strategy_domain",
        },
        {
            "row_index": 4,
            "symbol": "EURUSDT",
            "symbol_type": "forex",
            "reason": "outside_crypto_perp_strategy_domain",
        },
        {
            "row_index": 5,
            "symbol": "FUTUREUSDT",
            "symbol_type": "future_tradfi",
            "reason": "outside_crypto_perp_strategy_domain",
        },
    ]
    decisions = {row["symbol"]: row for row in payload["decisions"]}
    assert decisions["AAOIUSDT"]["populations"]["strategy_instruments"] == {
        "included": False,
        "reasons": ["outside_crypto_perp_strategy_domain"],
    }
    assert load_candidate_universe(
        write_candidate_universe(tmp_path / "candidate-v3.json", payload)
    ).symbols == ("AAAUSDT", "INNOVUSDT")


def test_builder_rejects_non_string_symbol_type() -> None:
    with pytest.raises(ValueError, match="symbolType"):
        build_candidate_universe_artifact(
            [_instrument("AAAUSDT", symbol_type=7)],
            [_ticker("AAAUSDT", "3000000")],
            snapshot_ts_ns=SNAPSHOT_NS,
            long_config=LongNativeDemoCycleConfig(),
        )


def test_write_then_load_a_candidate_universe_once(tmp_path: Path) -> None:
    path = write_candidate_universe(tmp_path / "candidate.json", _payload())
    assert os.stat(path).st_mode & 0o777 == 0o600
    frozen = load_candidate_universe(path)
    assert frozen.symbols == ("AAAUSDT", "BBBUSDT")
    with pytest.raises(FileExistsError):
        write_candidate_universe(path, _payload())


def test_profile_specific_population_records_and_reuses_scheduled_retirement(
    tmp_path: Path,
) -> None:
    frozen = load_candidate_universe(
        write_candidate_universe(tmp_path / "candidate.json", _payload())
    )
    assert frozen.profile_symbols == {
        "long": ("AAAUSDT",),
        "carry": ("AAAUSDT", "BBBUSDT"),
    }
    assert frozen.strategy_instruments == ("AAAUSDT", "BBBUSDT")
    now_ms = SNAPSHOT_NS // 1_000_000 + 1_000
    delivery_ms = now_ms + 3 * 24 * 60 * 60 * 1_000
    instruments = _normalize_instruments(
        [
            _instrument("AAAUSDT"),
            _instrument("BBBUSDT", delivery_time=str(delivery_ms)),
        ]
    )
    tickers = _normalize_tickers(
        [_ticker("AAAUSDT", "3000000"), _ticker("BBBUSDT", "1000000")]
    )
    registry = tmp_path / "retirements.json"

    long = enforce_frozen_candidate_frames(
        instruments,
        tickers,
        frozen,
        profile="long",
        snapshot_ts_ms=now_ms,
        context="test LONG",
        retirement_registry_path=registry,
    )
    assert long.active_symbols == ("AAAUSDT",)
    assert long.temporarily_ineligible == ()
    assert long.scheduled_retirements == ()
    assert not registry.exists()

    carry = enforce_frozen_candidate_frames(
        instruments,
        tickers,
        frozen,
        profile="carry",
        snapshot_ts_ms=now_ms,
        context="test CARRY",
        retirement_registry_path=registry,
    )
    assert carry.active_symbols == ("AAAUSDT",)
    assert [row.symbol for row in carry.scheduled_retirements] == ["BBBUSDT"]
    assert registry.stat().st_mode & 0o777 == 0o600

    # After venue removal, the prospectively captured observation continues to
    # explain the disappearance instead of turning every cycle into an outage.
    after_removal = enforce_frozen_candidate_frames(
        _normalize_instruments([_instrument("AAAUSDT")]),
        _normalize_tickers([_ticker("AAAUSDT", "3000000")]),
        frozen,
        profile="carry",
        snapshot_ts_ms=delivery_ms + 1,
        context="test CARRY",
        retirement_registry_path=registry,
    )
    assert [row.symbol for row in after_removal.scheduled_retirements] == ["BBBUSDT"]


def test_profile_reconciliation_treats_live_liquidity_drift_as_temporary(
    tmp_path: Path,
) -> None:
    frozen = load_candidate_universe(
        write_candidate_universe(tmp_path / "candidate.json", _payload())
    )
    now_ms = SNAPSHOT_NS // 1_000_000 + 1_000
    reconciliation = enforce_frozen_candidate_frames(
        _normalize_instruments(
            [_instrument("AAAUSDT"), _instrument("BBBUSDT")]
        ),
        _normalize_tickers(
            [
                _ticker("AAAUSDT", "1500000"),
                _ticker("BBBUSDT", "1000000"),
                _ticker("WC_ESP_ARG_USDT-19JUL26", "9000000"),
            ]
        ),
        frozen,
        profile="long",
        snapshot_ts_ms=now_ms,
        context="test LONG",
        retirement_registry_path=tmp_path / "retirements.json",
    )

    assert reconciliation.active_symbols == ()
    assert reconciliation.temporarily_ineligible_rows() == [
        {"symbol": "AAAUSDT", "reasons": ["turnover_below_floor"]}
    ]
    assert reconciliation.scheduled_retirements == ()
    assert not (tmp_path / "retirements.json").exists()


def test_profile_reconciliation_journals_unexplained_absence_and_continues(
    tmp_path: Path,
) -> None:
    # A structural change without delivery evidence drops the symbol to
    # journaled temporary ineligibility instead of turning every cycle into
    # an outage. It re-enters automatically if the venue restores it; nothing
    # post-freeze can enter regardless.
    frozen = load_candidate_universe(
        write_candidate_universe(tmp_path / "candidate.json", _payload())
    )
    now_ms = SNAPSHOT_NS // 1_000_000 + 1_000
    reconciliation = enforce_frozen_candidate_frames(
        _normalize_instruments(
            [
                _instrument("AAAUSDT", status="Settling"),
                _instrument("BBBUSDT"),
            ]
        ),
        _normalize_tickers(
            [_ticker("AAAUSDT", "3000000"), _ticker("BBBUSDT", "1000000")]
        ),
        frozen,
        profile="long",
        snapshot_ts_ms=now_ms,
        context="test LONG",
        retirement_registry_path=tmp_path / "retirements.json",
    )

    assert reconciliation.active_symbols == ()
    assert reconciliation.temporarily_ineligible_rows() == [
        {"symbol": "AAAUSDT", "reasons": ["unexplained_absence_from_venue"]}
    ]
    assert reconciliation.scheduled_retirements == ()
    assert not (tmp_path / "retirements.json").exists()


def test_delivery_time_change_updates_registry_and_continues(tmp_path: Path) -> None:
    frozen = load_candidate_universe(
        write_candidate_universe(tmp_path / "candidate.json", _payload())
    )
    now_ms = SNAPSHOT_NS // 1_000_000 + 1_000
    first_delivery_ms = now_ms + 3 * 24 * 60 * 60 * 1_000
    moved_delivery_ms = first_delivery_ms + 24 * 60 * 60 * 1_000
    registry = tmp_path / "retirements.json"
    tickers = _normalize_tickers(
        [_ticker("AAAUSDT", "3000000"), _ticker("BBBUSDT", "1000000")]
    )

    first = enforce_frozen_candidate_frames(
        _normalize_instruments(
            [
                _instrument("AAAUSDT"),
                _instrument("BBBUSDT", delivery_time=str(first_delivery_ms)),
            ]
        ),
        tickers,
        frozen,
        profile="carry",
        snapshot_ts_ms=now_ms,
        context="test CARRY",
        retirement_registry_path=registry,
    )
    assert [row.delivery_time_ms for row in first.scheduled_retirements] == [
        first_delivery_ms
    ]

    moved = enforce_frozen_candidate_frames(
        _normalize_instruments(
            [
                _instrument("AAAUSDT"),
                _instrument("BBBUSDT", delivery_time=str(moved_delivery_ms)),
            ]
        ),
        tickers,
        frozen,
        profile="carry",
        snapshot_ts_ms=now_ms + 1_000,
        context="test CARRY",
        retirement_registry_path=registry,
    )

    row = moved.scheduled_retirements[0]
    assert row.symbol == "BBBUSDT"
    assert row.delivery_time_ms == moved_delivery_ms
    assert row.first_observed_ts_ms == first.scheduled_retirements[0].first_observed_ts_ms
    assert row.evidence_source == "live_instrument_delivery_time_updated"
    assert moved.active_symbols == ("AAAUSDT",)

    # The cycle after the move has to be able to read what the move wrote: the
    # writer records the moved date under its own evidence source, so a reader
    # that accepted only the original one would raise "candidate-retirement
    # registry record is invalid" on every pass — on a path LONG runs every
    # cycle, with real retirements standing on the funded account.
    again = enforce_frozen_candidate_frames(
        _normalize_instruments(
            [
                _instrument("AAAUSDT"),
                _instrument("BBBUSDT", delivery_time=str(moved_delivery_ms)),
            ]
        ),
        tickers,
        frozen,
        profile="carry",
        snapshot_ts_ms=now_ms + 2_000,
        context="test CARRY",
        retirement_registry_path=registry,
    )
    kept = again.scheduled_retirements[0]
    assert kept.delivery_time_ms == moved_delivery_ms
    assert kept.first_observed_ts_ms == first.scheduled_retirements[0].first_observed_ts_ms


def test_scheduled_retirement_reentry_stays_nontradable_and_continues(
    tmp_path: Path,
) -> None:
    frozen = load_candidate_universe(
        write_candidate_universe(tmp_path / "candidate.json", _payload())
    )
    now_ms = SNAPSHOT_NS // 1_000_000 + 1_000
    delivery_ms = now_ms + 3 * 24 * 60 * 60 * 1_000
    registry = tmp_path / "retirements.json"
    tickers = _normalize_tickers(
        [_ticker("AAAUSDT", "3000000"), _ticker("BBBUSDT", "1000000")]
    )

    enforce_frozen_candidate_frames(
        _normalize_instruments(
            [
                _instrument("AAAUSDT"),
                _instrument("BBBUSDT", delivery_time=str(delivery_ms)),
            ]
        ),
        tickers,
        frozen,
        profile="carry",
        snapshot_ts_ms=now_ms,
        context="test CARRY",
        retirement_registry_path=registry,
    )

    # The venue cancels the delisting: BBBUSDT is fully eligible again. The
    # cycle continues; the symbol stays non-tradable while its prospective
    # delivery evidence stands.
    reentered = enforce_frozen_candidate_frames(
        _normalize_instruments([_instrument("AAAUSDT"), _instrument("BBBUSDT")]),
        tickers,
        frozen,
        profile="carry",
        snapshot_ts_ms=now_ms + 1_000,
        context="test CARRY",
        retirement_registry_path=registry,
    )

    assert reentered.active_symbols == ("AAAUSDT",)
    assert reentered.temporarily_ineligible_rows() == [
        {
            "symbol": "BBBUSDT",
            "reasons": ["scheduled_retirement_reentered_eligibility"],
        }
    ]
    assert [row.symbol for row in reentered.scheduled_retirements] == ["BBBUSDT"]


def test_scheduled_retirement_requires_account_and_inbox_flatness(tmp_path: Path) -> None:
    frozen = load_candidate_universe(
        write_candidate_universe(tmp_path / "candidate.json", _payload())
    )
    now_ms = SNAPSHOT_NS // 1_000_000 + 1_000
    delivery_ms = now_ms + 1_000
    reconciliation = enforce_frozen_candidate_frames(
        _normalize_instruments(
            [
                _instrument("AAAUSDT"),
                _instrument("BBBUSDT", delivery_time=str(delivery_ms)),
            ]
        ),
        _normalize_tickers(
            [_ticker("AAAUSDT", "3000000"), _ticker("BBBUSDT", "1000000")]
        ),
        frozen,
        profile="carry",
        snapshot_ts_ms=now_ms,
        context="test CARRY",
        retirement_registry_path=tmp_path / "retirements.json",
    )
    route = ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )
    require_scheduled_retirements_flat(
        reconciliation,
        route=route,
        context="test CARRY",
    )

    publisher = AccountTargetPublisher(route)
    publisher.publish(
        batch_id="retired-symbol-risk",
        intents=(
            requested_target(
                adapter_kind=SleeveAdapterKind.HEDGE,
                decision_key="retired-symbol-risk/BBBUSDT",
                target_key="hedge/test/bbb/BBBUSDT",
                strategy_id="test",
                component_id="bbb",
                symbol="BBBUSDT",
                signed_notional_usdt=100.0,
                leverage=2.0,
                reason="test",
            ),
        ),
        created_ts_ns=now_ms * 1_000_000,
    )
    with pytest.raises(RuntimeError, match="unresolved_nonzero_request"):
        require_scheduled_retirements_flat(
            reconciliation,
            route=route,
            context="test CARRY",
        )


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
    )

    assert payload["schema_version"] == 5
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
        )

    with pytest.raises(ValueError, match="duplicate symbol"):
        build_candidate_universe_artifact(
            [_instrument("AAAUSDT")],
            [synthetic, dict(synthetic)],
            snapshot_ts_ns=SNAPSHOT_NS,
            long_config=LongNativeDemoCycleConfig(),
        )

    with pytest.raises(ValueError, match="invalid candidate-universe symbol"):
        build_candidate_universe_artifact(
            [_instrument("AAAUSDT")],
            [{"turnover24h": "3000000"}],
            snapshot_ts_ns=SNAPSHOT_NS,
            long_config=LongNativeDemoCycleConfig(),
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
    )
    payload["rejected_ticker_rows"][0]["reason"] = "silently_ignored"
    payload["artifact_sha256"] = ""
    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    path = write_candidate_universe(tmp_path / "candidate.json", payload)

    with pytest.raises(ValueError, match="rejected ticker rows are inconsistent"):
        load_candidate_universe(path)


def test_loader_recomputes_non_crypto_instrument_exclusions(tmp_path: Path) -> None:
    payload = build_candidate_universe_artifact(
        [_instrument("AAAUSDT"), _instrument("AAOIUSDT", symbol_type="stock")],
        [_ticker("AAAUSDT", "3000000"), _ticker("AAOIUSDT", "9000000")],
        snapshot_ts_ns=SNAPSHOT_NS,
        long_config=LongNativeDemoCycleConfig(),
    )
    payload["excluded_instrument_rows"][0]["reason"] = "silently_admitted"
    payload["artifact_sha256"] = ""
    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    path = write_candidate_universe(tmp_path / "candidate.json", payload)

    with pytest.raises(ValueError, match="excluded instrument rows are inconsistent"):
        load_candidate_universe(path)


def _retiring_reconciliation(tmp_path: Path, *, now_ms: int):
    frozen = load_candidate_universe(
        write_candidate_universe(tmp_path / "candidate.json", _payload())
    )
    delivery_ms = now_ms + 1_000
    return enforce_frozen_candidate_frames(
        _normalize_instruments(
            [
                _instrument("AAAUSDT"),
                _instrument("BBBUSDT", delivery_time=str(delivery_ms)),
            ]
        ),
        _normalize_tickers(
            [_ticker("AAAUSDT", "3000000"), _ticker("BBBUSDT", "1000000")]
        ),
        frozen,
        profile="carry",
        snapshot_ts_ms=now_ms,
        context="test CARRY",
        retirement_registry_path=tmp_path / "retirements.json",
    )


def test_retirement_exposure_reports_what_the_raising_form_raises_on(
    tmp_path: Path,
) -> None:
    """The per-cycle report form must see exactly the raising form's exposure.

    A producer cycle consumes the report and keeps running (its own exit
    publication is what clears the exposure); the raising form stays for
    callers proving a precondition. Both must read the same account truth.
    """

    now_ms = SNAPSHOT_NS // 1_000_000 + 1_000
    reconciliation = _retiring_reconciliation(tmp_path, now_ms=now_ms)
    route = ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )

    assert scheduled_retirement_exposure(reconciliation, route=route) == {}

    publisher = AccountTargetPublisher(route)
    publisher.publish(
        batch_id="retired-symbol-risk",
        intents=(
            requested_target(
                adapter_kind=SleeveAdapterKind.HEDGE,
                decision_key="retired-symbol-risk/BBBUSDT",
                target_key="hedge/test/bbb/BBBUSDT",
                strategy_id="test",
                component_id="bbb",
                symbol="BBBUSDT",
                signed_notional_usdt=100.0,
                leverage=2.0,
                reason="test",
            ),
        ),
        created_ts_ns=now_ms * 1_000_000,
    )

    exposure = scheduled_retirement_exposure(reconciliation, route=route)
    assert exposure == {"BBBUSDT": ("unresolved_nonzero_request",)}
    with pytest.raises(RuntimeError, match="unresolved_nonzero_request"):
        require_scheduled_retirements_flat(
            reconciliation,
            route=route,
            context="test CARRY",
        )


def test_account_exposure_labels_enumerates_every_exposed_symbol(
    tmp_path: Path,
) -> None:
    """With no symbol filter the scan finds exposure it was not pointed at."""

    route = ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )
    assert account_exposure_labels(route=route) == {}

    publisher = AccountTargetPublisher(route)
    publisher.publish(
        batch_id="exposed-symbol",
        intents=(
            requested_target(
                adapter_kind=SleeveAdapterKind.HEDGE,
                decision_key="exposed-symbol/BBBUSDT",
                target_key="hedge/test/bbb/BBBUSDT",
                strategy_id="test",
                component_id="bbb",
                symbol="BBBUSDT",
                signed_notional_usdt=100.0,
                leverage=2.0,
                reason="test",
            ),
        ),
        created_ts_ns=(SNAPSHOT_NS // 1_000_000 + 1_000) * 1_000_000,
    )

    assert account_exposure_labels(route=route) == {
        "BBBUSDT": ("unresolved_nonzero_request",)
    }


def test_a_zero_notional_request_is_exposure_too(tmp_path: Path) -> None:
    """Every exit and flatten is a queued ZERO target, and the owner must
    read that symbol's rules to process it — so a receipt frozen without it
    wedges the request on restart (review finding, 2026-08-13)."""

    route = ensure_account_route(
        account_id="bybit-demo-unified",
        environment="demo",
        account_root=tmp_path / "account",
        inbox_root=tmp_path / "inbox",
    )
    AccountTargetPublisher(route).publish(
        batch_id="flat-symbol-exit",
        intents=(
            requested_target(
                adapter_kind=SleeveAdapterKind.HEDGE,
                decision_key="flat-symbol-exit/BBBUSDT",
                target_key="hedge/test/bbb/BBBUSDT",
                strategy_id="test",
                component_id="bbb",
                symbol="BBBUSDT",
                signed_notional_usdt=0.0,
                leverage=2.0,
                reason="exit",
            ),
        ),
        created_ts_ns=(SNAPSHOT_NS // 1_000_000 + 1_000) * 1_000_000,
    )

    assert account_exposure_labels(route=route) == {
        "BBBUSDT": ("unresolved_request",)
    }
