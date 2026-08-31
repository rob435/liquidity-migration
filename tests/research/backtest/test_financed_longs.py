"""Contracts for the committed financed-longs configs.

A lookahead in the hysteresis state, a gross cap that levers instead of diluting, or a
funding accrual with the wrong sign each make the number better rather than raise, so
each is pinned on deterministic synthetic frames.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from liquidity_migration.research.backtest.financed_longs import (
    CarryReplaySettings,
    daily_scores,
    live_contract_scores,
    prepare,
    score_carry_hold,
    venue_view,
    volatility_scale,
)
from liquidity_migration.rules.carry_hold import (
    HOUR_MS,
    CarryHoldConfig,
    FinancedLongsError,
    daily_grid,
    top_n_universe,
)
from liquidity_migration.rules.rust_strategy_contract import (
    rust_carry_research_weights as carry_hold_weights,
)
from liquidity_migration.rules.carry_event_tape import CarryPresettlementEvent

CONFIG_DIR = Path(__file__).resolve().parents[3] / "configs"
POSITIVE_TS_OFFSET = 20_000 * 24 * HOUR_MS


@pytest.fixture
def carry_cfg() -> CarryHoldConfig:
    return CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v1.json")


def _panel(
    symbols: int = 14,
    hours: int = 480,
    funding_bp: dict[str, list[float]] | None = None,
    drift: dict[str, float] | None = None,
) -> pl.DataFrame:
    """Hourly panel; funding settles every 8h (bar_ts divisible by 8h)."""
    rows: list[dict[str, object]] = []
    funding_bp = funding_bp or {}
    drift = drift or {}
    for s in range(symbols):
        sym = f"S{s:02d}USDT" if s else "BTCUSDT"
        price = 100.0
        f_series = funding_bp.get(sym)
        for h in range(hours):
            ts = h * HOUR_MS
            price *= 1.0 + drift.get(sym, 0.0)
            settle = (h % 8) == 0
            idx = h // 8
            rate = (f_series[idx % len(f_series)] if f_series else 1.0) / 1e4
            rows.append(
                {
                    "symbol": sym,
                    "bar_ts_ms": ts,
                    "by_close": price,
                    "by_turnover_quote": 1_000_000.0 * (symbols - s),
                    "by_funding": rate,
                    "by_funding_age_h": 0.0 if settle else float((h % 8)),
                    "bn_close": price * 1.001,
                    "bn_turnover_quote": 900_000.0 * (symbols - s),
                    "bn_funding": rate / 2.0,
                    "bn_funding_age_h": 0.0 if settle else float((h % 8)),
                }
            )
    return pl.DataFrame(rows)


def _universe(panel: pl.DataFrame, top_n: int = 100) -> pl.DataFrame:
    return top_n_universe(daily_grid(prepare(panel)), top_n)


def _positive_panel(**kwargs: object) -> pl.DataFrame:
    return _panel(**kwargs).with_columns(
        (pl.col("bar_ts_ms") + POSITIVE_TS_OFFSET).alias("bar_ts_ms")
    )


def _replay_settings(*, max_entries: int = 10) -> CarryReplaySettings:
    return CarryReplaySettings(
        environment="mainnet",
        source_profile="carry_hold_v7_live_v1",
        notional_multiplier=3.0,
        entry_leverage=5.0,
        stop_loss_fraction=0.35,
        max_new_entries_per_cycle=max_entries,
    )


class TestResearchEquityChart:
    """The wrapper-supported standard-format render for registered configs, so a Lane-2
    config never needs a hand-built lookalike chart.
    """

    def test_renders_standard_chart_with_research_label(self, tmp_path: Path) -> None:
        from liquidity_migration.research.backtest.financed_longs import research_equity_chart

        panel = _panel(funding_bp={"S01USDT": [-15.0]})
        payload = research_equity_chart(
            panel,
            CONFIG_DIR / "lane2_carry_hold_v1.json",
            tmp_path,
            start="1970-01-01",
            end="1970-02-01",
        )
        assert payload["config_id"] == "lane2_carry_hold_v1"
        assert payload["run_label"] == "lane2_carry_hold_v1_research_seen_data_corrected_scorer"
        assert payload["decision_authority"] == "rust_registered_rule"
        png = tmp_path / "lane2_carry_hold_v1_equity_btc.png"
        assert png.exists() and png.stat().st_size > 0
        assert payload["png"] == str(png)
        assert (tmp_path / "lane2_carry_hold_v1_daily_equity.csv").exists()
        assert (tmp_path / "lane2_carry_hold_v1_summary.json").exists()
        # The chart must carry the full standard metric tile set.
        assert {"total_return_pct", "max_drawdown_pct", "sharpe_daily_ann", "mar", "years"} <= set(
            payload["metrics"]
        )

    def test_config_scores_dispatches_carry_and_rejects_dead_shapes(self) -> None:
        import json as _json

        from liquidity_migration.research.backtest.financed_longs import config_scores

        panel = _panel(funding_bp={"S01USDT": [-15.0]})
        _, _, carry_id, _ = config_scores(panel, CONFIG_DIR / "lane2_carry_hold_v1.json")
        _, _, carry2_id, _ = config_scores(panel, CONFIG_DIR / "lane2_carry_hold_v2.json")
        assert carry_id == "lane2_carry_hold_v1"
        assert carry2_id == "lane2_carry_hold_v2"
        # The leaders ("signal") and spread ("spread") shapes were deleted
        # 2026-08-19 by operator override; a leftover config must fail loudly.
        import tempfile

        for shape in ("signal", "spread"):
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                _json.dump({"config_id": "ghost", "rule": {shape: {}}}, f)
            with pytest.raises(FinancedLongsError, match="unrecognized"):
                config_scores(panel, f.name)


class TestLiveContractReplay:
    def test_standard_replay_uses_one_persistent_rust_process(
        self,
        carry_cfg: CarryHoldConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import liquidity_migration.research.backtest.financed_longs as financed_longs

        real_contract = financed_longs.RustStrategyContract
        instances: list[object] = []

        class RecordingContract:
            def __init__(self) -> None:
                self.inner = real_contract()
                self.requests = 0
                self.entered = 0
                self.exited = 0
                instances.append(self)

            def __enter__(self) -> RecordingContract:
                self.inner.__enter__()
                self.entered += 1
                return self

            def request(self, payload: dict[str, object]) -> dict[str, object]:
                self.requests += 1
                assert payload["operation"] == "carry_reduce"
                return self.inner.request(payload)

            def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
                self.exited += 1
                self.inner.__exit__(exc_type, exc, traceback)

        monkeypatch.setattr(financed_longs, "RustStrategyContract", RecordingContract)
        panel = _positive_panel(symbols=1, funding_bp={"BTCUSDT": [-15.0]})
        universe = _universe(panel)
        scores, diagnostics = financed_longs.live_contract_scores(
            carry_hold_weights(universe, carry_cfg),
            universe,
            panel,
            carry_cfg,
            replay_settings=_replay_settings(),
        )

        assert scores.height > 1
        assert len(instances) == 1
        instance = instances[0]
        assert instance.entered == 1
        assert instance.exited == 1
        assert instance.requests > scores.height
        assert diagnostics["decision_authority"] == "supplied_reference_fixture"
        assert diagnostics["daily_scorer_authority"] == "supplied_reference_fixture"
        assert diagnostics["contract_transport"] == "one_persistent_jsonl_process"

    def test_standard_v7_route_refuses_python_daily_weights(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import liquidity_migration.research.backtest.financed_longs as financed_longs

        def forbidden_python_scorer(*args: object, **kwargs: object) -> pl.DataFrame:
            raise AssertionError("the standard v7 route called the Python CARRY scorer")

        def native_contract_stub(
            weights: pl.DataFrame | None,
            signal_grid: pl.DataFrame,
            hourly_view: pl.DataFrame,
            cfg: CarryHoldConfig,
            **kwargs: object,
        ) -> tuple[pl.DataFrame, dict[str, object]]:
            del hourly_view, cfg
            assert weights is None
            assert kwargs["decision_source"] == "rust_signal_batch"
            times = sorted(int(value) for value in signal_grid["bar_ts_ms"].unique().to_list())
            return (
                pl.DataFrame(
                    {
                        "bar_ts_ms": times[:2],
                        "gross_bp": [0.0, 0.0],
                        "oneway": [0.0, 0.0],
                        "cost_bp": [0.0, 0.0],
                        "net_bp": [0.0, 0.0],
                    }
                ),
                {"daily_scorer_authority": "rust_carry_native"},
            )

        monkeypatch.setattr(financed_longs, "carry_hold_weights", forbidden_python_scorer)
        monkeypatch.setattr(financed_longs, "top_n_universe", forbidden_python_scorer)
        monkeypatch.setattr(financed_longs, "live_contract_scores", native_contract_stub)
        payload = financed_longs.research_equity_chart(
            _panel(symbols=50),
            CONFIG_DIR / "lane2_carry_hold_v7.json",
            tmp_path,
            start="1970-01-01",
            end="1970-02-01",
            replay_mode="live_contract",
            replay_settings=_replay_settings(max_entries=100),
        )

        assert payload["contract_replay"] == {
            "daily_scorer_authority": "rust_carry_native"
        }

    def test_native_v7_scores_the_daily_book_inside_rust(self) -> None:
        import liquidity_migration.research.backtest.financed_longs as financed_longs

        cfg = CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v7.json")
        symbols = [f"S{index:03d}USDT" for index in range(110)]
        signal_rows: list[dict[str, object]] = []
        for day in range(92):
            ts = POSITIVE_TS_OFFSET + day * 24 * HOUR_MS
            for index, symbol in enumerate(symbols):
                signal_rows.append(
                    {
                        "symbol": symbol,
                        "bar_ts_ms": ts,
                        "by_close": 100.0,
                        "by_turnover_quote": float(1_000_000 - index),
                        "by_funding": -0.002,
                        "by_funding_age_h": 0.0,
                        "adv24": float(1_000_000 - index),
                        "trail_fund_24h": -0.012,
                        "momentum": 0.1,
                        "ret_3d": 0.1,
                        "vol_30d_daily": 0.1,
                        "dtrail_2d": 0.0,
                        "crowd_persistence": 0.5,
                        "turn_growth_3d": 1.0,
                        "d_tt_ls_3d": 1.0,
                    }
                )
        first_replay_ts = POSITIVE_TS_OFFSET + 90 * 24 * HOUR_MS
        hourly_rows: list[dict[str, object]] = []
        for hour in range(49):
            ts = first_replay_ts + hour * HOUR_MS
            for symbol in symbols:
                hourly_rows.append(
                    {
                        "symbol": symbol,
                        "bar_ts_ms": ts,
                        "by_close": 100.0,
                        "by_funding": -0.002,
                        "by_funding_age_h": float(hour % 8),
                    }
                )

        signal_batches: list[dict[str, object]] = []

        class RecordingContract:
            def __init__(self, inner: object) -> None:
                self.inner = inner

            def request(self, payload: dict[str, object]) -> dict[str, object]:
                batch = payload["signal_batch"]
                if isinstance(batch, dict):
                    signal_batches.append(batch)
                return self.inner.request(payload)  # type: ignore[attr-defined,no-any-return]

        with financed_longs.RustStrategyContract() as inner:
            scores, diagnostics = live_contract_scores(
                None,
                pl.DataFrame(signal_rows),
                pl.DataFrame(hourly_rows),
                cfg,
                replay_settings=_replay_settings(max_entries=200),
                strategy_contract=RecordingContract(inner),
                decision_source="rust_signal_batch",
            )

        assert scores.height == 2
        assert diagnostics["decision_authority"] == "rust_carry_native"
        assert diagnostics["daily_scorer_authority"] == "rust_carry_native"
        assert diagnostics["signal_replay_days"] == 90
        assert diagnostics["last_effective_universe_size"] == 100
        assert int(diagnostics["max_active_names"]) == 100
        assert len(signal_batches) == 3
        assert signal_batches[0]["decision_ts_ms"] == first_replay_ts
        assert len(signal_batches[0]["rows"]) == 91 * len(symbols)  # type: ignore[arg-type]
        assert signal_batches[0]["upcoming_rows"] == []
        assert signal_batches[1]["decision_ts_ms"] == first_replay_ts
        assert len(signal_batches[1]["upcoming_rows"]) == len(symbols)  # type: ignore[arg-type]
        assert signal_batches[2]["decision_ts_ms"] == first_replay_ts + 24 * HOUR_MS
        assert signal_batches[2]["upcoming_rows"] == []
        first_batch_symbols = {
            row["symbol"]
            for row in signal_batches[0]["rows"]  # type: ignore[union-attr]
            if row["bar_ts_ms"] == POSITIVE_TS_OFFSET
        }
        assert first_batch_symbols == set(symbols)

    def test_replays_cap_trail_drop_anchor_and_typed_presettlement_identity(
        self,
        carry_cfg: CarryHoldConfig,
    ) -> None:
        deep = {symbol: [-15.0] for symbol in ["BTCUSDT", *[f"S{i:02d}USDT" for i in range(1, 14)]]}
        panel = _positive_panel(funding_bp=deep)
        universe = _universe(panel)
        decision_times = sorted(int(value) for value in universe["bar_ts_ms"].unique().to_list())
        first, second = decision_times[:2]
        weights = pl.DataFrame(
            {
                "bar_ts_ms": [first, first, first, second, second],
                "symbol": ["BTCUSDT", "S01USDT", "S02USDT", "BTCUSDT", "S02USDT"],
                "w": [0.1, 0.1, 0.1, 0.1, 0.1],
            }
        )
        event = CarryPresettlementEvent(
            environment="mainnet",
            source_profile="carry_hold_v7_live_v1",
            source_config_id=carry_cfg.config_id,
            decision_ts_ms=first,
            fired_ts_ms=first + 7 * HOUR_MS + 50 * 60_000,
            settlement_ts_ms=first + 8 * HOUR_MS,
            symbol="BTCUSDT",
            running_rate=0.0,
            mark_px=100.0,
            carry_side=None,
            carry_qty=None,
            carry_avg_entry_px=None,
        )

        scores, diagnostics = live_contract_scores(
            weights,
            universe,
            panel,
            carry_cfg,
            replay_settings=_replay_settings(max_entries=2),
            presettlement_events=(event,),
        )

        assert scores.height == len(decision_times)
        assert diagnostics["pre_settlement_clock"] == "typed_event_replay"
        assert diagnostics["presettlement_exit_fires"] == 1
        assert int(diagnostics["drop_exit_fires"]) >= 1
        assert int(diagnostics["sizing_anchor_requests"]) >= 1
        assert int(diagnostics["entry_cap_deferrals"]) >= 1
        assert diagnostics["max_active_names"] == 3
        assert diagnostics["admission_trail"] == "trail_fund_24h"
        assert diagnostics["resize_mark_missing_skips"] == 0
        assert int(diagnostics["idle_cadence_wakes"]) >= 1
        assert diagnostics["execution_model"] == (
            "modeled_immediate_target_fill_at_observed_hourly_mark"
        )

    def test_carries_quantity_and_reapplies_resize_deadband_at_hourly_marks(
        self,
        carry_cfg: CarryHoldConfig,
    ) -> None:
        panel = _positive_panel(
            symbols=1,
            funding_bp={"BTCUSDT": [-15.0]},
            drift={"BTCUSDT": 0.10},
        )
        universe = _universe(panel)
        weights = carry_hold_weights(universe, carry_cfg)

        scores, diagnostics = live_contract_scores(
            weights,
            universe,
            panel,
            carry_cfg,
            replay_settings=_replay_settings(),
        )

        assert scores.height > 1
        assert int(diagnostics["hourly_mark_wakes"]) > 0
        assert int(diagnostics["planned_resizes"]) > 0
        assert diagnostics["holding_state"] == "carried_quantity_entry_and_current_mark"

    def test_rejects_a_presettlement_tape_from_another_decision_source(
        self,
        carry_cfg: CarryHoldConfig,
    ) -> None:
        panel = _positive_panel(funding_bp={"BTCUSDT": [-15.0]})
        universe = _universe(panel)
        weights = carry_hold_weights(universe, carry_cfg)
        first = int(universe["bar_ts_ms"].min())
        event = CarryPresettlementEvent(
            environment="demo",
            source_profile="carry_hold_v7_live_v1",
            source_config_id=carry_cfg.config_id,
            decision_ts_ms=first,
            fired_ts_ms=first + 60_000,
            settlement_ts_ms=first + 8 * HOUR_MS,
            symbol="BTCUSDT",
            running_rate=0.0,
            mark_px=100.0,
            carry_side=None,
            carry_qty=None,
            carry_avg_entry_px=None,
        )

        with pytest.raises(FinancedLongsError, match="environment"):
            live_contract_scores(
                weights,
                universe,
                panel,
                carry_cfg,
                replay_settings=_replay_settings(),
                presettlement_events=(event,),
            )

        wrong_decision = dataclasses.replace(
            event,
            environment="mainnet",
            decision_ts_ms=first - 24 * HOUR_MS,
        )
        with pytest.raises(FinancedLongsError, match="outside the replay"):
            live_contract_scores(
                weights,
                universe,
                panel,
                carry_cfg,
                replay_settings=_replay_settings(),
                presettlement_events=(wrong_decision,),
            )


class TestAccounting:
    def test_long_receives_negative_funding(self) -> None:
        # price flat, funding -10 bp/8h: net_return over 24h = +30 bp of funding
        panel = _panel(funding_bp={"S01USDT": [-10.0]})
        u = _universe(panel)
        row = u.filter(pl.col("symbol") == "S01USDT").head(1)
        assert row["net_return"][0] == pytest.approx(3 * 10.0 / 1e4, rel=1e-6)

    def test_turnover_charged_on_entry_and_exit(self, carry_cfg: CarryHoldConfig) -> None:
        panel = _panel(funding_bp={"S01USDT": [1.0, -15.0, -15.0, 1.0, 1.0]})
        u = _universe(panel)
        w = carry_hold_weights(u, carry_cfg)
        scores = daily_scores(w, u, carry_cfg.fee_side_bp).sort("bar_ts_ms")
        # first held day pays entry on 0.10 notional
        assert scores["cost_bp"][0] == pytest.approx(0.10 * carry_cfg.fee_side_bp, rel=1e-6)
        # Total one-way turnover must be even: every entry has a matching exit.
        # Iterating only bars PRESENT in `weights` leaves a flat decision day
        # charging neither the exit into it nor the re-entry out of it, while
        # gross treats the book as liquidated.
        held = {int(value) for value in w["bar_ts_ms"].unique().to_list()}
        decision_bars = sorted({int(item) for item in u["bar_ts_ms"].unique().to_list()})
        assert scores["bar_ts_ms"].to_list() == decision_bars
        flat_days = [ts for ts in decision_bars if ts not in held]
        assert flat_days, "fixture must contain at least one interior flat day"
        by_ts = dict(zip(scores["bar_ts_ms"].to_list(), scores["oneway"].to_list()))
        # Every held bar charges an entry; the FIRST flat bar after each held
        # block charges the matching exit; a second consecutive flat bar charges
        # nothing. The old behaviour charged only the 5 entries (0.50).
        entries = [ts for ts in decision_bars if ts in held]
        exits = [
            ts
            for index, ts in enumerate(decision_bars)
            if ts not in held and index > 0 and decision_bars[index - 1] in held
        ]
        assert len(entries) == 5
        assert len(exits) == 5
        for ts in entries + exits:
            assert by_ts[ts] == pytest.approx(0.10, rel=1e-6)
        for ts in decision_bars:
            if ts not in entries and ts not in exits:
                assert by_ts[ts] == pytest.approx(0.0, abs=1e-12)
        assert scores["oneway"].sum() == pytest.approx(0.10 * 10, rel=1e-9)

    def test_terminal_hold_is_liquidated_on_a_final_cash_row(self) -> None:
        universe = pl.DataFrame(
            {
                "bar_ts_ms": [0, 86_400_000],
                "symbol": ["AAAUSDT", "AAAUSDT"],
                "net_return": [0.01, 0.02],
            }
        )
        weights = pl.DataFrame(
            {
                "bar_ts_ms": [0, 86_400_000],
                "symbol": ["AAAUSDT", "AAAUSDT"],
                "w": [0.25, 0.25],
            }
        )

        scores = daily_scores(weights, universe, fee_side_bp=5.0)

        assert scores["bar_ts_ms"].to_list() == [0, 86_400_000]
        assert scores["gross_bp"].to_list() == pytest.approx([25.0, 50.0])
        assert scores["oneway"].to_list() == pytest.approx([0.25, 0.25])
        assert scores["cost_bp"].to_list() == pytest.approx([1.25, 1.25])

    def test_vol_scale_uses_strict_prior_window(self) -> None:
        rng = np.random.default_rng(7)
        net = rng.normal(0.0, 50.0, 120)
        scale = volatility_scale(net, target_annual=0.15, lookback_days=30, max_leverage=3.0)
        assert (scale[:30] == 0).all()
        assert scale.max() <= 3.0

    def test_venue_view_rejects_unknown(self) -> None:
        with pytest.raises(FinancedLongsError):
            venue_view(_panel(), "okx")

    def test_binance_view_scores(self) -> None:
        view = venue_view(_panel(), "binance")
        assert "by_close" in view.columns and "bn_close" not in view.columns

    def test_score_carry_hold_end_to_end(self, carry_cfg: CarryHoldConfig) -> None:
        panel = _panel(funding_bp={"S01USDT": [-15.0]})
        out = score_carry_hold(panel, carry_cfg)
        assert out["config_id"] == "lane2_carry_hold_v1"
        assert out["days"] > 0
