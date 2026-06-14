from __future__ import annotations

import polars as pl

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
    """universe-pit-2: the 'perpetuals-only, USDT-settled by construction' invariant
    (docs/data_roots.md; backtesting_errors_we_never_repeat.md rule 12, instrument
    lifecycle) must hold at the universe.py boundary. The positive test only ever fed
    LinearPerpetual/USDT rows, so a refactor could silently let a dated-delivery future,
    an inverse/USDC contract, or a row with a missing contractType through. Inject one of
    each alongside two clean perps and assert ONLY the clean perps survive."""
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
) -> dict:
    return {
        "ts_ms": 1,
        "symbol": symbol,
        "category": "linear",
        "contract_type": contract_type,
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
