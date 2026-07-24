"""Synthetic timing, mapping, and coverage tests for the P0 cross-venue panel.

These fixtures are deliberately tiny and hand-computed. The properties under
test are the ones that decide whether a downstream anomaly result means
anything: no future leakage, no backward fill, honest coverage flags, and
identity failures that raise instead of silently merging.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.cross_venue_panel import (
    HOUR_MS,
    PanelBuildError,
    PanelSpec,
    build_panel,
    resolve_universe,
    write_panel,
)

DAY = dt.date(2026, 3, 2)
# 2026-03-02T00:00:00Z
BASE_MS = int(dt.datetime(2026, 3, 2, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _write(root: Path, dataset: str, day: dt.date, symbol: str, rows: list[dict[str, object]]) -> None:
    out = root / dataset / f"date={day.isoformat()}" / f"symbol={symbol}"
    out.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(out / "part.parquet")


def _hours(symbol: str, n: int, *, close: float, turnover: float = 1000.0) -> list[dict[str, object]]:
    return [
        {
            "ts_ms": BASE_MS + i * HOUR_MS,
            "symbol": symbol,
            "close": close + i,
            "turnover_quote": turnover,
        }
        for i in range(n)
    ]


def _ohlc(symbol: str, n: int, *, close: float) -> list[dict[str, object]]:
    return [{"ts_ms": BASE_MS + i * HOUR_MS, "symbol": symbol, "close": close + i * 0.001} for i in range(n)]


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """Two venue roots with one fully covered symbol over four hours."""

    bybit, binance = tmp_path / "bybit", tmp_path / "binance"
    sym = "AAAUSDT"
    _write(bybit, "klines_1h", DAY, sym, _hours(sym, 4, close=100.0))
    _write(bybit, "mark_price_1h", DAY, sym, _ohlc(sym, 4, close=100.0))
    _write(bybit, "index_price_1h", DAY, sym, _ohlc(sym, 4, close=99.5))
    _write(bybit, "premium_index_1h", DAY, sym, _ohlc(sym, 4, close=0.001))
    _write(
        bybit,
        "open_interest",
        DAY,
        sym,
        [
            {
                "ts_ms": BASE_MS + i * HOUR_MS,
                "symbol": sym,
                "open_interest": 500.0 + i,
                "open_interest_value": 50_000.0 + i,
            }
            for i in range(4)
        ],
    )
    _write(binance, "klines_1h", DAY, sym, _hours(sym, 4, close=101.0))
    _write(binance, "binance_usdm_premium_index_1h", DAY, sym, _ohlc(sym, 4, close=0.002))
    return bybit, binance


def _spec(roots: tuple[Path, Path], **kwargs: object) -> PanelSpec:
    bybit, binance = roots
    params: dict[str, object] = {
        "bybit_root": bybit,
        "binance_root": binance,
        "start": DAY,
        "end": DAY + dt.timedelta(days=1),
    }
    params.update(kwargs)
    return PanelSpec(**params)  # type: ignore[arg-type]


class TestTiming:
    def test_decision_time_is_one_hour_after_the_bar_opens(self, roots: tuple[Path, Path]) -> None:
        panel, _ = build_panel(_spec(roots))
        assert (panel["decision_ts_ms"] - panel["bar_ts_ms"] == HOUR_MS).all()

    def test_execution_delay_is_additive_to_the_completion_lag(self, roots: tuple[Path, Path]) -> None:
        panel, manifest = build_panel(_spec(roots, execution_delay_ms=5 * 60_000))
        assert (panel["decision_ts_ms"] - panel["bar_ts_ms"] == HOUR_MS + 5 * 60_000).all()
        assert manifest["timing"]["execution_delay_ms"] == 5 * 60_000

    def test_funding_settled_after_the_decision_is_never_visible(self, roots: tuple[Path, Path]) -> None:
        """A settlement at hour 2 must not appear on rows deciding before it."""

        bybit, _ = roots
        _write(
            bybit,
            "funding",
            DAY,
            "AAAUSDT",
            [
                {
                    "ts_ms": BASE_MS + 2 * HOUR_MS,
                    "symbol": "AAAUSDT",
                    "funding_rate": 0.0007,
                    "funding_event_kind": "settlement",
                }
            ],
        )
        panel, _ = build_panel(_spec(roots))
        rows = panel.sort("bar_ts_ms")
        # bar 0 decides at BASE+1h, bar 1 at BASE+2h -> settlement is visible
        # from bar 1 onward, never on bar 0.
        assert rows["by_funding"][0] is None
        assert rows["by_funding"][1] == pytest.approx(0.0007)
        assert rows["by_funding_age_h"][1] == pytest.approx(0.0)
        assert rows["by_funding_age_h"][3] == pytest.approx(2.0)

    def test_funding_from_before_the_window_is_carried_with_its_age(
        self, roots: tuple[Path, Path]
    ) -> None:
        bybit, _ = roots
        prior = DAY - dt.timedelta(days=1)
        _write(
            bybit,
            "funding",
            prior,
            "AAAUSDT",
            [
                {
                    "ts_ms": BASE_MS - 8 * HOUR_MS,
                    "symbol": "AAAUSDT",
                    "funding_rate": -0.0003,
                    "funding_event_kind": "settlement",
                }
            ],
        )
        panel, _ = build_panel(_spec(roots))
        rows = panel.sort("bar_ts_ms")
        assert rows["by_funding"][0] == pytest.approx(-0.0003)
        assert rows["by_funding_age_h"][0] == pytest.approx(9.0)

    def test_legacy_funding_partitions_without_event_kind_are_read_as_settled(
        self, roots: tuple[Path, Path]
    ) -> None:
        """Almost all real history predates the ``funding_event_kind`` column.

        A partition lacking it must still contribute its settlements, not be
        skipped for failing a filter on a column that does not exist.
        """

        bybit, _ = roots
        _write(
            bybit,
            "funding",
            DAY,
            "AAAUSDT",
            [{"ts_ms": BASE_MS, "symbol": "AAAUSDT", "funding_rate": 0.0005}],
        )
        panel, _ = build_panel(_spec(roots))
        assert panel.sort("bar_ts_ms")["by_funding"].to_list() == pytest.approx([0.0005] * 4)

    def test_mixed_schema_generations_are_combined(self, roots: tuple[Path, Path]) -> None:
        """Legacy and typed partitions coexist in one window without loss."""

        bybit, _ = roots
        prior = DAY - dt.timedelta(days=1)
        _write(
            bybit,
            "funding",
            prior,
            "AAAUSDT",
            [{"ts_ms": BASE_MS - 8 * HOUR_MS, "symbol": "AAAUSDT", "funding_rate": -0.0001}],
        )
        _write(
            bybit,
            "funding",
            DAY,
            "AAAUSDT",
            [
                {
                    "ts_ms": BASE_MS + 2 * HOUR_MS,
                    "symbol": "AAAUSDT",
                    "funding_rate": 0.0009,
                    "funding_event_kind": "settlement",
                },
                {
                    "ts_ms": BASE_MS + 3 * HOUR_MS,
                    "symbol": "AAAUSDT",
                    "funding_rate": 0.5,
                    "funding_event_kind": "predicted",
                },
            ],
        )
        rows = build_panel(_spec(roots))[0].sort("bar_ts_ms")
        # Bars 0..3 decide at BASE+1h..+4h. The legacy settlement covers the
        # first decision; the typed settlement at BASE+2h covers the rest. The
        # predicted rate at BASE+3h is never admitted at any horizon.
        assert rows["by_funding"].to_list() == pytest.approx([-0.0001, 0.0009, 0.0009, 0.0009])
        assert 0.5 not in rows["by_funding"].to_list()

    def test_non_settlement_funding_events_are_ignored(self, roots: tuple[Path, Path]) -> None:
        bybit, _ = roots
        _write(
            bybit,
            "funding",
            DAY,
            "AAAUSDT",
            [
                {
                    "ts_ms": BASE_MS,
                    "symbol": "AAAUSDT",
                    "funding_rate": 0.09,
                    "funding_event_kind": "predicted",
                }
            ],
        )
        panel, _ = build_panel(_spec(roots))
        assert panel["by_funding"].is_null().all()


class TestGapPolicy:
    def test_missing_hours_are_null_not_forward_filled(self, roots: tuple[Path, Path]) -> None:
        """Bybit has 4 hours of OI; drop hour 2 and it must stay null."""

        bybit, _ = roots
        _write(
            bybit,
            "open_interest",
            DAY,
            "AAAUSDT",
            [
                {
                    "ts_ms": BASE_MS + i * HOUR_MS,
                    "symbol": "AAAUSDT",
                    "open_interest": 500.0 + i,
                    "open_interest_value": 50_000.0 + i,
                }
                for i in (0, 1, 3)
            ],
        )
        panel, _ = build_panel(_spec(roots))
        rows = panel.sort("bar_ts_ms")
        assert rows["by_open_interest"][2] is None
        assert rows["cov_bybit_oi"].to_list() == [True, True, False, True]

    def test_a_symbol_absent_from_binance_prices_is_excluded_entirely(
        self, roots: tuple[Path, Path], tmp_path: Path
    ) -> None:
        bybit, _binance = roots
        _write(bybit, "klines_1h", DAY, "ZZZUSDT", _hours("ZZZUSDT", 4, close=7.0))
        _write(bybit, "premium_index_1h", DAY, "ZZZUSDT", _ohlc("ZZZUSDT", 4, close=0.0))
        universe, exclusions = resolve_universe(_spec(roots))
        assert universe == ("AAAUSDT",)
        assert "ZZZUSDT" in exclusions
        panel, manifest = build_panel(_spec(roots))
        assert panel["symbol"].unique().to_list() == ["AAAUSDT"]
        assert manifest["population"]["excluded"] >= 1


class TestCoverageAndDerived:
    def test_coverage_flags_are_true_when_every_field_is_present(
        self, roots: tuple[Path, Path]
    ) -> None:
        panel, _ = build_panel(_spec(roots))
        for flag in (
            "cov_bybit_price",
            "cov_bybit_mark",
            "cov_bybit_index",
            "cov_bybit_premium",
            "cov_bybit_oi",
            "cov_binance_price",
            "cov_binance_premium",
        ):
            assert panel[flag].all(), flag
        assert not panel["cov_bybit_funding"].any()

    def test_cross_venue_differences_are_computed_from_aligned_rows(
        self, roots: tuple[Path, Path]
    ) -> None:
        panel, _ = build_panel(_spec(roots))
        first = panel.sort("bar_ts_ms").row(0, named=True)
        assert first["basis_bp"] == pytest.approx((100.0 / 101.0 - 1.0) * 10_000.0)
        assert first["premium_diff_bp"] == pytest.approx((0.001 - 0.002) * 10_000.0)

    def test_manifest_records_bounds_population_and_yearly_coverage(
        self, roots: tuple[Path, Path]
    ) -> None:
        panel, manifest = build_panel(_spec(roots))
        assert manifest["artifact"] == "cross_venue_panel"
        assert manifest["date_bounds"] == {"start": "2026-03-02", "end_exclusive": "2026-03-03"}
        assert manifest["population"]["symbols"] == 1
        assert manifest["rows"] == panel.height
        assert manifest["coverage_by_year"]["2026"]["cov_bybit_price"] == 1.0
        assert manifest["timing"]["bar_completion_lag_ms"] == HOUR_MS
        assert len(manifest["config_hash"]) == 64


class TestIdentity:
    def test_non_canonical_partition_is_dropped_but_reported(
        self, roots: tuple[Path, Path]
    ) -> None:
        """``AAA%55SDT`` over-encodes an ASCII 'U'. It must not merge into
        AAAUSDT, and it must not vanish without a recorded reason."""

        bybit, _ = roots
        out = bybit / "klines_1h" / f"date={DAY.isoformat()}" / "symbol=AAA%55SDT"
        out.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(_hours("AAAUSDT", 1, close=1.0)).write_parquet(out / "part.parquet")

        universe, exclusions = resolve_universe(_spec(roots))
        assert universe == ("AAAUSDT",)
        assert "canonical" in exclusions["AAA%55SDT"]

        _panel, manifest = build_panel(_spec(roots))
        assert "AAA%55SDT" in manifest["population"]["exclusion_reasons"]

    def test_unicode_partition_round_trips_to_its_canonical_symbol(
        self, roots: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """A legitimately percent-encoded venue symbol must survive."""

        bybit, binance = roots
        encoded, canonical = "%E5%B8%81USDT", "币USDT"
        for root, dataset, close in (
            (bybit, "klines_1h", 5.0),
            (binance, "klines_1h", 5.5),
        ):
            _write(root, dataset, DAY, encoded, _hours(canonical, 2, close=close))
        _write(bybit, "premium_index_1h", DAY, encoded, _ohlc(canonical, 2, close=0.0))
        _write(binance, "binance_usdm_premium_index_1h", DAY, encoded, _ohlc(canonical, 2, close=0.0))

        universe, exclusions = resolve_universe(_spec(roots))
        assert canonical in universe
        assert encoded not in exclusions

    def test_requested_symbol_outside_the_population_is_recorded(
        self, roots: tuple[Path, Path]
    ) -> None:
        _universe, exclusions = resolve_universe(_spec(roots, symbols=("AAAUSDT", "NOPEUSDT")))
        assert exclusions["NOPEUSDT"] == "requested but not in both-venue population"


class TestSpecAndOutput:
    def test_end_must_be_after_start(self, roots: tuple[Path, Path]) -> None:
        with pytest.raises(PanelBuildError, match="end is exclusive"):
            _spec(roots, end=DAY)

    def test_negative_execution_delay_is_rejected(self, roots: tuple[Path, Path]) -> None:
        with pytest.raises(PanelBuildError, match="must not be negative"):
            _spec(roots, execution_delay_ms=-1)

    def test_config_hash_tracks_the_declared_configuration(self, roots: tuple[Path, Path]) -> None:
        assert _spec(roots).config_hash() == _spec(roots).config_hash()
        assert _spec(roots).config_hash() != _spec(roots, execution_delay_ms=1).config_hash()

    def test_empty_window_raises_rather_than_returning_a_silent_empty_panel(
        self, roots: tuple[Path, Path]
    ) -> None:
        far = DAY + dt.timedelta(days=400)
        with pytest.raises(PanelBuildError):
            build_panel(_spec(roots, start=far, end=far + dt.timedelta(days=1)))

    def test_write_panel_emits_parquet_and_a_self_hashed_manifest(
        self, roots: tuple[Path, Path], tmp_path: Path
    ) -> None:
        panel, manifest = build_panel(_spec(roots))
        out = tmp_path / "out"
        panel_path, manifest_path = write_panel(panel, manifest, out)
        payload = json.loads(manifest_path.read_text())
        assert pl.read_parquet(panel_path).height == panel.height
        assert len(payload["panel_sha256"]) == 64
        assert payload["config_hash"] == manifest["config_hash"]
