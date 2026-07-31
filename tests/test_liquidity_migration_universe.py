from __future__ import annotations

import polars as pl
import pytest

from liquidity_migration.config import UniverseConfig
from liquidity_migration.universe import build_current_universe_table, format_universe_report
from liquidity_migration._common import MS_PER_DAY


def test_current_universe_table_filters_and_ranks() -> None:
    snapshot_ts_ms = 1_800_000_000_000
    instruments = pl.DataFrame(
        [
            _instrument("BTCUSDT", snapshot_ts_ms - 100 * MS_PER_DAY),
            _instrument("AAAUSDT", snapshot_ts_ms - 100 * MS_PER_DAY),
            _instrument("BBBUSDT", snapshot_ts_ms - 40 * MS_PER_DAY),
            _instrument("CCCUSDT", snapshot_ts_ms - 5 * MS_PER_DAY),
            _instrument("DDDUSDT", snapshot_ts_ms - 100 * MS_PER_DAY, status="Settled"),
        ]
    )
    tickers = pl.DataFrame(
        [
            _ticker("BTCUSDT", 100_000_000.0),
            _ticker("AAAUSDT", 50_000_000.0),
            _ticker("BBBUSDT", 10_000_000.0),
            _ticker("CCCUSDT", 20_000_000.0),
            _ticker("DDDUSDT", 30_000_000.0),
        ]
    )

    table = build_current_universe_table(
        instruments,
        tickers,
        universe_config=UniverseConfig(
            min_turnover_24h=5_000_000.0,
            min_age_days=30,
            rank_start=1,
            rank_end=10,
            max_symbols=10,
            exclude_symbols=("BTCUSDT",),
        ),
        snapshot_ts_ms=snapshot_ts_ms,
    )

    assert table["symbol"].to_list() == ["AAAUSDT", "BBBUSDT"]
    assert table["liquidity_rank"].to_list() == [1, 2]


def test_universe_report_contains_symbol_csv() -> None:
    report = format_universe_report(
        {
            "name": "mid",
            "snapshot": "2026-05-03T00:00:00+00:00",
            "rows": 1,
            "symbols": ["AAAUSDT"],
            "symbol_csv": "AAAUSDT",
            "config": {
                "min_turnover_24h": 1_000_000.0,
                "min_age_days": 30,
                "max_age_days": 0,
                "rank_start": 21,
                "rank_end": 80,
                "max_symbols": 60,
                "exclude_symbols": ["BTCUSDT"],
            },
            "survivorship_warning": "warning",
            "universe": [
                {
                    "liquidity_rank": 21,
                    "symbol": "AAAUSDT",
                    "turnover_24h": 1_000_000.0,
                    "listing_age_days": 99.0,
                    "open_interest_value": 500_000.0,
                    "funding_rate": 0.0001,
                }
            ],
        }
    )

    assert "AAAUSDT" in report
    assert "21-80" in report


def test_current_universe_table_excludes_non_perp_and_non_usdt_contracts() -> None:
    """The perpetuals-only, USDT-settled invariant must hold at the universe.py
    boundary: a dated-delivery future, an inverse/USDC contract, and a row with a
    missing contractType are injected alongside two clean perps, and only the clean
    perps may survive.
    """
    snapshot_ts_ms = 1_800_000_000_000
    old = snapshot_ts_ms - 100 * MS_PER_DAY
    instruments = pl.DataFrame(
        [
            _instrument("AAAUSDT", old),  # clean LinearPerpetual / USDT -> kept
            _instrument("BBBUSDT", old),  # clean LinearPerpetual / USDT -> kept
            # dated-delivery future: contract_type is the operative exclusion.
            _instrument("FUT0101USDT", old, contract_type="LinearFutures"),
            # inverse perp settled in the coin (not USDT) -> excluded by settle_coin AND contract_type.
            _instrument("INVUSD", old, contract_type="InversePerpetual", settle_coin="USD"),
            # USDC-margined contract -> excluded by the USDT settle filter.
            _instrument("USDCPERP", old, settle_coin="USDC"),
            # missing/None contractType -> the is_in filter drops a null, so it is excluded.
            _instrument("NOCTUSDT", old, contract_type=None),
        ]
    )
    tickers = pl.DataFrame(
        [
            _ticker("AAAUSDT", 50_000_000.0),
            _ticker("BBBUSDT", 40_000_000.0),
            _ticker("FUT0101USDT", 90_000_000.0),  # high turnover would rank it FIRST if not excluded
            _ticker("INVUSD", 80_000_000.0),
            _ticker("USDCPERP", 70_000_000.0),
            _ticker("NOCTUSDT", 60_000_000.0),
        ]
    )

    table = build_current_universe_table(
        instruments,
        tickers,
        universe_config=UniverseConfig(
            min_turnover_24h=5_000_000.0,
            min_age_days=30,
            rank_start=1,
            rank_end=10,
            max_symbols=10,
        ),
        snapshot_ts_ms=snapshot_ts_ms,
    )

    assert table["symbol"].to_list() == ["AAAUSDT", "BBBUSDT"]
    assert "LinearFutures" not in table["contract_type"].to_list()
    assert set(table["settle_coin"].to_list()) == {"USDT"}


def _instrument(
    symbol: str,
    launch_time_ms: int,
    *,
    status: str = "Trading",
    contract_type: str | None = "LinearPerpetual",
    settle_coin: str = "USDT",
    symbol_type: str | None = None,
) -> dict:
    return {
        "ts_ms": 1,
        "symbol": symbol,
        "category": "linear",
        "contract_type": contract_type,
        "symbol_type": symbol_type,
        "status": status,
        "settle_coin": settle_coin,
        "launch_time_ms": launch_time_ms,
        "tick_size": 0.01,
        "qty_step": 0.001,
        "min_notional_value": 5.0,
        "is_prelisting": False,
    }


def _ticker(symbol: str, turnover_24h: float) -> dict:
    return {
        "ts_ms": 1,
        "symbol": symbol,
        "last_price": 1.0,
        "open_interest": 1_000.0,
        "open_interest_value": 1_000.0,
        "turnover_24h": turnover_24h,
        "volume_24h": turnover_24h,
        "funding_rate": 0.0,
    }


# --------------------------------------------------------------------------- #
# Non-perp / non-USDT / missing-contractType rows excluded                     #
# --------------------------------------------------------------------------- #
def test_universepit2_excludes_dated_inverse_usdc_and_missing_contract_type() -> None:
    snapshot = 1_800_000_000_000
    old = snapshot - 100 * MS_PER_DAY
    instruments = pl.DataFrame([
        _instrument("AAAUSDT", old),
        _instrument("BBBUSDT", old),
        _instrument("FUT0101USDT", old, contract_type="LinearFutures"),  # dated delivery
        _instrument("INVUSD", old, contract_type="InversePerpetual", settle_coin="USD"),
        _instrument("USDCPERP", old, settle_coin="USDC"),
        _instrument("NOCTUSDT", old, contract_type=None),  # missing contractType
    ])
    tickers = pl.DataFrame([
        _ticker("AAAUSDT", 50_000_000.0),
        _ticker("BBBUSDT", 40_000_000.0),
        _ticker("FUT0101USDT", 90_000_000.0),  # would rank first if not excluded
        _ticker("INVUSD", 80_000_000.0),
        _ticker("USDCPERP", 70_000_000.0),
        _ticker("NOCTUSDT", 60_000_000.0),
    ])
    table = build_current_universe_table(
        instruments, tickers,
        universe_config=UniverseConfig(
            min_turnover_24h=5_000_000.0, min_age_days=30, rank_start=1, rank_end=10, max_symbols=10,
        ),
        snapshot_ts_ms=snapshot,
    )
    assert table["symbol"].to_list() == ["AAAUSDT", "BBBUSDT"]
    assert "LinearFutures" not in table["contract_type"].to_list()
    assert set(table["settle_coin"].to_list()) == {"USDT"}


def test_universe_excludes_non_crypto_symbol_types_before_ranking() -> None:
    snapshot = 1_800_000_000_000
    old = snapshot - 100 * MS_PER_DAY
    instruments = pl.DataFrame([
        _instrument("AAAUSDT", old),
        _instrument("INNOVUSDT", old, symbol_type="innovation"),
        _instrument("AAOIUSDT", old, symbol_type="stock"),
        _instrument("XAUUSDT", old, symbol_type="commodity"),
        _instrument("EURUSDT", old, symbol_type="forex"),
        _instrument("FUTUREUSDT", old, symbol_type="future_tradfi"),
    ])
    tickers = pl.DataFrame([
        _ticker("AAAUSDT", 10_000_000.0),
        _ticker("INNOVUSDT", 20_000_000.0),
        _ticker("AAOIUSDT", 100_000_000.0),
        _ticker("XAUUSDT", 90_000_000.0),
        _ticker("EURUSDT", 80_000_000.0),
        _ticker("FUTUREUSDT", 70_000_000.0),
    ])

    table = build_current_universe_table(
        instruments,
        tickers,
        universe_config=UniverseConfig(
            min_turnover_24h=0.0,
            min_age_days=30,
            rank_start=1,
            rank_end=2,
            max_symbols=2,
        ),
        snapshot_ts_ms=snapshot,
    )

    assert table["symbol"].to_list() == ["INNOVUSDT", "AAAUSDT"]
    assert table["liquidity_rank"].to_list() == [1, 2]
    assert table["symbol_type"].to_list() == ["innovation", None]


# --------------------------------------------------------------------------- #
# These carry their own `_univ_*` helpers: they need extra columns             #
# (delivery_time_ms, null launch) the `_instrument`/`_ticker` fixtures above   #
# do not take.                                                                 #
# --------------------------------------------------------------------------- #
def _univ_instrument(symbol, launch_ms, *, contract_type="LinearPerpetual", settle_coin="USDT", **extra):
    row = {
        "ts_ms": 1, "symbol": symbol, "category": "linear", "contract_type": contract_type,
        "symbol_type": None,
        "status": "Trading", "settle_coin": settle_coin, "launch_time_ms": launch_ms,
        "tick_size": 0.01, "qty_step": 0.001, "min_notional_value": 5.0, "is_prelisting": False,
    }
    row.update(extra)
    return row


def _univ_ticker(symbol, turnover):
    return {
        "ts_ms": 1, "symbol": symbol, "last_price": 1.0, "open_interest": 1_000.0,
        "open_interest_value": 1_000.0, "turnover_24h": turnover, "volume_24h": turnover,
        "funding_rate": 0.0,
    }


def test_universepit1_missing_contract_type_column_raises() -> None:
    """``contract_type`` is a hard required column: a frame missing it must raise rather
    than no-op the only perpetuals-only barrier and pass dated-delivery futures
    straight through.
    """
    snapshot = 1_800_000_000_000
    old = snapshot - 100 * MS_PER_DAY
    instruments = pl.DataFrame([_univ_instrument("AAAUSDT", old)]).drop("contract_type")
    tickers = pl.DataFrame([_univ_ticker("AAAUSDT", 50_000_000.0)])
    with pytest.raises(RuntimeError, match="contract_type"):
        build_current_universe_table(
            instruments, tickers,
            universe_config=UniverseConfig(min_turnover_24h=5_000_000.0, min_age_days=30),
            snapshot_ts_ms=snapshot,
        )


def test_universe_missing_symbol_type_column_raises() -> None:
    snapshot = 1_800_000_000_000
    old = snapshot - 100 * MS_PER_DAY
    instruments = pl.DataFrame([_univ_instrument("AAAUSDT", old)]).drop("symbol_type")
    tickers = pl.DataFrame([_univ_ticker("AAAUSDT", 50_000_000.0)])

    with pytest.raises(RuntimeError, match="symbol_type"):
        build_current_universe_table(
            instruments,
            tickers,
            universe_config=UniverseConfig(
                min_turnover_24h=5_000_000.0,
                min_age_days=30,
            ),
            snapshot_ts_ms=snapshot,
        )


def test_universepit1_dated_delivery_excluded_by_delivery_time() -> None:
    """Defense-in-depth: a row that slips the contract_type allow-list but carries a
    non-null/non-zero delivery_time_ms (a dated delivery contract) is excluded."""
    snapshot = 1_800_000_000_000
    old = snapshot - 100 * MS_PER_DAY
    instruments = pl.DataFrame([
        _univ_instrument("AAAUSDT", old, delivery_time_ms=0),  # perp -> kept
        # contract_type passes the allow-list (label says perpetual) but it has a
        # real delivery time -> dated contract, must be excluded by the belt check.
        _univ_instrument("FUT1231USDT", old, contract_type="LinearPerpetual",
                         delivery_time_ms=snapshot + 30 * MS_PER_DAY),
    ])
    tickers = pl.DataFrame([
        _univ_ticker("AAAUSDT", 50_000_000.0),
        _univ_ticker("FUT1231USDT", 90_000_000.0),  # would rank first if not excluded
    ])
    table = build_current_universe_table(
        instruments, tickers,
        universe_config=UniverseConfig(
            min_turnover_24h=5_000_000.0, min_age_days=30, rank_start=1, rank_end=10, max_symbols=10,
        ),
        snapshot_ts_ms=snapshot,
    )
    assert table["symbol"].to_list() == ["AAAUSDT"]


def test_universepit3_join_drop_is_logged(caplog) -> None:
    """A symbol present in instruments but missing from the tickers snapshot is silently
    dropped by the inner join, so the drop count must be surfaced (info log) to make a
    partial or throttled ``get_tickers`` fetch observable per cycle.
    """
    snapshot = 1_800_000_000_000
    old = snapshot - 100 * MS_PER_DAY
    instruments = pl.DataFrame([
        _univ_instrument("AAAUSDT", old),
        _univ_instrument("BBBUSDT", old),  # no ticker row -> dropped by the join
    ])
    tickers = pl.DataFrame([_univ_ticker("AAAUSDT", 50_000_000.0)])
    with caplog.at_level("INFO", logger="liquidity_migration.universe"):
        table = build_current_universe_table(
            instruments, tickers,
            universe_config=UniverseConfig(
                min_turnover_24h=5_000_000.0, min_age_days=30, rank_start=1, rank_end=10, max_symbols=10,
            ),
            snapshot_ts_ms=snapshot,
        )
    assert table["symbol"].to_list() == ["AAAUSDT"]
    assert any("dropped" in rec.getMessage() and "ticker" in rec.getMessage() for rec in caplog.records)


def test_universepit5_null_launch_time_surfaced_in_unlimited_mode(caplog) -> None:
    """In unlimited-universe mode a symbol with null ``launch_time_ms`` has a null
    listing age and passes through silently, so the count of unknown-age symbols must
    be surfaced.
    """
    snapshot = 1_800_000_000_000
    old = snapshot - 100 * MS_PER_DAY
    instruments = pl.DataFrame([
        _univ_instrument("AAAUSDT", old),
        _univ_instrument("NULLAGEUSDT", None),  # null launch_time_ms -> unknown age
    ])
    tickers = pl.DataFrame([
        _univ_ticker("AAAUSDT", 50_000_000.0),
        _univ_ticker("NULLAGEUSDT", 40_000_000.0),
    ])
    with caplog.at_level("INFO", logger="liquidity_migration.universe"):
        table = build_current_universe_table(
            instruments, tickers,
            # unlimited mode: no age gate active.
            universe_config=UniverseConfig(
                min_turnover_24h=5_000_000.0, min_age_days=0, max_age_days=0,
                rank_start=1, rank_end=0, max_symbols=0,
            ),
            snapshot_ts_ms=snapshot,
        )
    # Null-age symbol passes through in unlimited mode (the residual exposure).
    assert "NULLAGEUSDT" in table["symbol"].to_list()
    assert any("null launch_time_ms" in rec.getMessage() for rec in caplog.records)


def test_universepit5_null_launch_time_dropped_when_age_gated() -> None:
    """With an active age floor, a null-launch (unknown-age) symbol is conservatively
    dropped (the is_not_null guard), as before — the fix only adds observability."""
    snapshot = 1_800_000_000_000
    old = snapshot - 100 * MS_PER_DAY
    instruments = pl.DataFrame([
        _univ_instrument("AAAUSDT", old),
        _univ_instrument("NULLAGEUSDT", None),
    ])
    tickers = pl.DataFrame([
        _univ_ticker("AAAUSDT", 50_000_000.0),
        _univ_ticker("NULLAGEUSDT", 40_000_000.0),
    ])
    table = build_current_universe_table(
        instruments, tickers,
        universe_config=UniverseConfig(
            min_turnover_24h=5_000_000.0, min_age_days=30, rank_start=1, rank_end=10, max_symbols=10,
        ),
        snapshot_ts_ms=snapshot,
    )
    assert table["symbol"].to_list() == ["AAAUSDT"]
