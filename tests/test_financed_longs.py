"""Contracts for the committed financed-longs configs.

The failure mode of a funding-conditioned long book is silent flattery: a
lookahead in the hysteresis state, a gross cap that levers instead of
diluting, or a funding accrual with the wrong sign all make the number
*better*, not raise. Those contracts are pinned here on deterministic
synthetic frames.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from liquidity_migration.financed_longs import (
    HOUR_MS,
    CarryHoldConfig,
    FinancedLeadersConfig,
    FinancedLongsError,
    btc_gate,
    carry_hold_weights,
    daily_grid,
    daily_scores,
    financed_leaders_weights,
    prepare,
    prepare_decision,
    score_carry_hold,
    settlement_exact_funding,
    top_n_universe,
    venue_view,
    volatility_scale,
)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"


@pytest.fixture
def carry_cfg() -> CarryHoldConfig:
    return CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v1.json")


@pytest.fixture
def leaders_cfg() -> FinancedLeadersConfig:
    return FinancedLeadersConfig.from_json(CONFIG_DIR / "lane2_financed_leaders_v1.json")


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


class TestCarryHoldState:
    def test_enters_below_enter_and_holds_until_exit(self, carry_cfg: CarryHoldConfig) -> None:
        # S01 funding path (bp/8h): benign, deep, recovering-but-below-exit, recovered
        panel = _panel(funding_bp={"S01USDT": [1.0, -15.0, -5.0, 1.0]})
        u = _universe(panel)
        w = carry_hold_weights(u, carry_cfg)
        held = w.filter(pl.col("symbol") == "S01USDT")
        assert held.height > 0
        # never held while funding is benign before the first deep print
        first_deep_ts = 8 * HOUR_MS  # second settlement
        assert held["bar_ts_ms"].min() >= first_deep_ts
        # exit-by-hysteresis: -5 bp is below the -3 bp exit bound, so the state
        # survives the recovery print and drops only at the +1 bp settlement
        ts_held = set(held["bar_ts_ms"].to_list())
        assert any(t >= 16 * HOUR_MS for t in ts_held)

    def test_no_lookahead_in_state(self, carry_cfg: CarryHoldConfig) -> None:
        # Deep funding arrives only at the LAST settlement covered by the grid:
        # no decision bar strictly before that settlement may hold the name.
        hours = 480
        n_settlements = hours // 8
        pattern = [1.0] * (n_settlements - 4) + [-15.0] * 4
        panel = _panel(hours=hours, funding_bp={"S01USDT": pattern})
        u = _universe(panel)
        held = carry_hold_weights(u, carry_cfg).filter(pl.col("symbol") == "S01USDT")
        deep_ts = (n_settlements - 4) * 8 * HOUR_MS
        assert held.filter(pl.col("bar_ts_ms") < deep_ts).height == 0

    def test_gross_cap_dilutes_instead_of_levering(self, carry_cfg: CarryHoldConfig) -> None:
        deep = {f"S{s:02d}USDT": [-20.0] for s in range(1, 14)}
        panel = _panel(funding_bp=deep)
        u = _universe(panel)
        w = carry_hold_weights(u, carry_cfg)
        gross = w.group_by("bar_ts_ms").agg(pl.col("w").abs().sum().alias("g"))
        assert float(gross["g"].max()) <= carry_cfg.gross_cap + 1e-9
        # 13 names at cap 0.10 exceeds 1.0, so per-name weight must be scaled down
        per_name = w.filter(pl.col("bar_ts_ms") == w["bar_ts_ms"].max())["w"]
        assert float(per_name.max()) < carry_cfg.per_name_cap


class TestDepthScaling:
    """v2 sizing: w = cap * clip(|trail_fund_24h| / ref, floor, 1.0).

    The state machine is v1's; only the size of a held name changes. v1
    configs carry no depth block and must keep the flat cap bit-for-bit.
    """

    def test_v1_config_has_no_depth_scaling_and_keeps_flat_cap(
        self, carry_cfg: CarryHoldConfig
    ) -> None:
        assert carry_cfg.depth_ref_bp_per_day is None
        panel = _panel(funding_bp={"S01USDT": [-15.0]})
        w = carry_hold_weights(_universe(panel), carry_cfg)
        assert set(w["w"].to_list()) == {carry_cfg.per_name_cap}

    def test_v2_config_round_trip(self) -> None:
        cfg = CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v2.json")
        assert cfg.config_id == "lane2_carry_hold_v2"
        assert cfg.venue == "bybit"
        # state machine identical to v1 by construction
        assert (cfg.enter_bp, cfg.exit_bp) == (10.0, 3.0)
        assert (cfg.per_name_cap, cfg.gross_cap) == (0.1, 1.0)
        assert cfg.depth_ref_bp_per_day == 120.0
        assert cfg.depth_floor == 0.25

    def test_weight_scales_with_trailing_daily_rate(self, carry_cfg: CarryHoldConfig) -> None:
        # Constant per-print rates on 8h settlements give trailing daily rates
        # of exactly 3x the print: -150, -60, -36 bp/day -> scales 1.0, 0.5, 0.3.
        cfg = dataclasses.replace(carry_cfg, depth_ref_bp_per_day=120.0, depth_floor=0.25)
        panel = _panel(
            funding_bp={"S01USDT": [-50.0], "S02USDT": [-20.0], "S03USDT": [-12.0]}
        )
        w = carry_hold_weights(_universe(panel), cfg)
        last = w.filter(pl.col("bar_ts_ms") == w["bar_ts_ms"].max())
        by_sym = dict(zip(last["symbol"].to_list(), last["w"].to_list()))
        assert by_sym["S01USDT"] == pytest.approx(0.10, rel=1e-9)
        assert by_sym["S02USDT"] == pytest.approx(0.05, rel=1e-9)
        assert by_sym["S03USDT"] == pytest.approx(0.03, rel=1e-9)

    def test_floor_binds_on_shallow_trailing_rate(self, carry_cfg: CarryHoldConfig) -> None:
        # One acute print (-15 bp) on the first decision bar, then -4 bp prints:
        # the hysteresis keeps the state (-4 < -3 exit bound) while the trailing
        # rate sits at -12..-23 bp/day, far under ref -> every held bar sizes at
        # the floor, INCLUDING the entry bar itself (shallow history at entry).
        cfg = dataclasses.replace(carry_cfg, depth_ref_bp_per_day=120.0, depth_floor=0.25)
        pattern = [-4.0] * 21 + [-15.0] + [-4.0] * 38
        panel = _panel(funding_bp={"S01USDT": pattern})
        w = carry_hold_weights(_universe(panel), cfg).filter(pl.col("symbol") == "S01USDT")
        assert w.height > 1
        assert set(round(v, 12) for v in w["w"].to_list()) == {
            round(cfg.per_name_cap * cfg.depth_floor, 12)
        }

    def test_missing_trail_column_fails_closed(self, carry_cfg: CarryHoldConfig) -> None:
        cfg = dataclasses.replace(carry_cfg, depth_ref_bp_per_day=120.0)
        panel = _panel(funding_bp={"S01USDT": [-15.0]})
        u = _universe(panel).drop("trail_fund_24h")
        with pytest.raises(FinancedLongsError, match="trail_fund_24h"):
            carry_hold_weights(u, cfg)

    def test_unsupported_basis_rejected(self, tmp_path: Path) -> None:
        import json

        payload = json.loads((CONFIG_DIR / "lane2_carry_hold_v2.json").read_text())
        payload["rule"]["sizing"]["depth_scaling"]["basis"] = "last_print"
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(payload))
        with pytest.raises(FinancedLongsError, match="basis"):
            CarryHoldConfig.from_json(bad)

    def test_score_carry_hold_v2_end_to_end(self) -> None:
        cfg = CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v2.json")
        panel = _panel(funding_bp={"S01USDT": [-15.0]})
        out = score_carry_hold(panel, cfg)
        assert out["config_id"] == "lane2_carry_hold_v2"
        assert out["days"] > 0


class TestV3Filters:
    """v3: toxic-band block/suspend, dead-name vol floor, recovery-velocity exit.

    Each feature is exercised in isolation via dataclasses.replace so a failure
    names its filter; the committed JSON is round-tripped and run end-to-end.
    v1/v2 behaviour is pinned unchanged by the existing classes (all v3 fields
    default off).
    """

    def test_v3_config_round_trip(self) -> None:
        cfg = CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v3.json")
        assert cfg.config_id == "lane2_carry_hold_v3"
        assert cfg.toxic_band_ret3d == (-0.30, -0.05)
        assert cfg.min_vol30_daily == 0.05
        assert cfg.trail_recovery_exit_bp_2d == 30.0
        assert cfg.depth_ref_bp_per_day == 120.0  # v2 sizing carried forward

    def test_toxic_band_blocks_entry(self, carry_cfg: CarryHoldConfig) -> None:
        import dataclasses

        cfg = dataclasses.replace(carry_cfg, toxic_band_ret3d=(-0.30, -0.05))
        # S01 grinds down ~-10% per 3d (in band); S02 flat (out of band).
        panel = _panel(
            hours=1100,
            funding_bp={"S01USDT": [-15.0], "S02USDT": [-15.0]},
            drift={"S01USDT": -0.0015},
        )
        w = carry_hold_weights(_universe(panel), cfg)
        assert w.filter(pl.col("symbol") == "S01USDT").height == 0
        assert w.filter(pl.col("symbol") == "S02USDT").height > 0

    def test_capitulation_below_band_still_enters(self, carry_cfg: CarryHoldConfig) -> None:
        import dataclasses

        cfg = dataclasses.replace(carry_cfg, toxic_band_ret3d=(-0.30, -0.05))
        # -0.006/h -> 3d return ~-35%, BELOW the band: the U-shape keeps it.
        panel = _panel(hours=1100, funding_bp={"S01USDT": [-15.0]}, drift={"S01USDT": -0.006})
        w = carry_hold_weights(_universe(panel), cfg)
        assert w.filter(pl.col("symbol") == "S01USDT").height > 0

    def test_vol_floor_blocks_dead_names(self, carry_cfg: CarryHoldConfig) -> None:
        import dataclasses

        cfg = dataclasses.replace(carry_cfg, min_vol30_daily=0.05)
        # Funding turns deep only AFTER the ~744h vol warm-up, so the entry
        # attempt happens with a KNOWN zero vol (flat price): blocked under the
        # v3 floor (entry-only; a null vol fails open by design), held by v1.
        pattern = [1.0] * 95 + [-15.0] * 43
        panel = _panel(hours=1100, funding_bp={"S01USDT": pattern})
        u = _universe(panel)
        deep = u.filter((pl.col("symbol") == "S01USDT") & (pl.col("by_funding") < -10.0 / 1e4))
        assert deep.height > 0 and deep["vol_30d_daily"].is_not_null().all(), (
            "fixture must attempt entry only after the vol warm-up"
        )
        assert carry_hold_weights(u, cfg).filter(pl.col("symbol") == "S01USDT").height == 0
        assert carry_hold_weights(u, carry_cfg).filter(pl.col("symbol") == "S01USDT").height > 0

    def test_recovery_velocity_exits_the_state(self, carry_cfg: CarryHoldConfig) -> None:
        import dataclasses

        cfg = dataclasses.replace(carry_cfg, trail_recovery_exit_bp_2d=30.0)
        # Deep -15 bp prints, then -4 bp prints: -4 < -3 keeps the v1 state
        # forever, but the trailing daily rate recovers -45 -> -12 bp/day
        # (+33 bp over 2d) and the velocity exit ends it; -4 bp never
        # re-qualifies (-4 > -10).
        n = 1100 // 8
        pattern = [-15.0] * (n // 2) + [-4.0] * (n - n // 2)
        panel = _panel(hours=1100, funding_bp={"S01USDT": pattern})
        u = _universe(panel)
        with_vel = carry_hold_weights(u, cfg).filter(pl.col("symbol") == "S01USDT")
        without = carry_hold_weights(u, carry_cfg).filter(pl.col("symbol") == "S01USDT")
        assert with_vel.height > 0
        assert with_vel["bar_ts_ms"].max() < without["bar_ts_ms"].max()

    def test_enabled_features_fail_closed_without_columns(self, carry_cfg: CarryHoldConfig) -> None:
        import dataclasses

        cfg = dataclasses.replace(carry_cfg, toxic_band_ret3d=(-0.30, -0.05))
        panel = _panel(funding_bp={"S01USDT": [-15.0]})
        u = _universe(panel).drop("ret_3d")
        with pytest.raises(FinancedLongsError, match="ret_3d"):
            carry_hold_weights(u, cfg)

    def test_score_carry_hold_v3_end_to_end(self) -> None:
        cfg = CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v3.json")
        panel = _panel(hours=1100, funding_bp={"S01USDT": [-15.0]})
        out = score_carry_hold(panel, cfg)
        assert out["config_id"] == "lane2_carry_hold_v3"
        assert "days" in out


class TestPrepareDecision:
    """The live frame: same signals as ``prepare``, no forward-return gate."""

    SIGNALS = [
        "by_funding",
        "adv24",
        "trail_fund_24h",
        "momentum",
        "ret_3d",
        "vol_30d_daily",
        "dtrail_2d",
    ]

    def test_shared_rows_are_bit_identical_to_research_frame(self) -> None:
        panel = _panel(funding_bp={"S01USDT": [1.0, -15.0, -5.0, 1.0]})
        view = venue_view(panel, "bybit")
        research = prepare(view)
        live = prepare_decision(view)
        assert live.height > research.height
        joined = research.select(["symbol", "bar_ts_ms", *self.SIGNALS]).join(
            live.select(["symbol", "bar_ts_ms", *self.SIGNALS]),
            on=["symbol", "bar_ts_ms"],
            how="inner",
            suffix="_live",
        )
        assert joined.height == research.height
        for col in self.SIGNALS:
            a, b = joined[col], joined[f"{col}_live"]
            assert (a.is_null() == b.is_null()).all(), col
            fa, fb = a.drop_nulls(), b.drop_nulls()
            assert (fa == fb).all(), col

    def test_live_frame_keeps_the_terminal_bars(self) -> None:
        panel = _panel(hours=480)
        view = venue_view(panel, "bybit")
        last_panel_bar = int(view["bar_ts_ms"].max())  # type: ignore[arg-type]
        research = prepare(view)
        live = prepare_decision(view)
        # prepare()'s forward-return gate drops each symbol's final 24 bars
        # (the registered terminal-day frame caveat); a live decision cannot
        # know the next 24h exists, so the decision frame keeps them.
        assert int(live["bar_ts_ms"].max()) == last_panel_bar  # type: ignore[arg-type]
        assert int(research["bar_ts_ms"].max()) < last_panel_bar  # type: ignore[arg-type]

    def test_live_frame_still_requires_maturity(self) -> None:
        panel = _panel(hours=480)
        view = venue_view(panel, "bybit")
        live = prepare_decision(view)
        # momentum needs 168h of history: bar 167 is the first with a full
        # lookback, so nothing younger may appear in the live frame either.
        assert int(live["bar_ts_ms"].min()) >= 168 * HOUR_MS  # type: ignore[arg-type]


class TestFundingSpread:
    """Cross-venue neutral spread book: sign convention, hysteresis, fees.

    The fixture ties bn_funding = by_funding / 2, so a deep bybit print of
    -60 bp gives an 8h-settled spread of 3 x (-60 + 30) = -90 bp/day: long
    bybit / short binance, receiving the differential.
    """

    def test_config_round_trip_and_dispatch(self) -> None:
        from liquidity_migration.financed_longs import FundingSpreadConfig, config_scores

        cfg = FundingSpreadConfig.from_json(CONFIG_DIR / "lane2_funding_spread_v1.json")
        assert (cfg.enter_bp_per_day, cfg.exit_bp_per_day) == (80.0, 20.0)
        panel = _panel(hours=1100, funding_bp={"S01USDT": [-60.0]})
        _, _, cid, venue = config_scores(panel, CONFIG_DIR / "lane2_funding_spread_v1.json")
        assert cid == "lane2_funding_spread_v1"
        assert venue == "bybit+binance"

    def test_neutral_return_is_the_funding_differential_when_prices_track(self) -> None:
        from liquidity_migration.financed_longs import prepare_spread, daily_grid

        # Flat prices on both venues: the neutral fwd-24h return must equal
        # -(paid_by) + paid_bn = 3 x (60 - 30) bp = +90 bp for the deep name.
        panel = _panel(hours=1100, funding_bp={"S01USDT": [-60.0]})
        u = daily_grid(prepare_spread(panel))
        row = u.filter(pl.col("symbol") == "S01USDT").head(1)
        assert row["spread_bpd"][0] == pytest.approx(-90.0, rel=1e-9)
        assert row["net_return"][0] == pytest.approx(90.0 / 1e4, rel=1e-6)

    def test_hysteresis_enters_deep_and_exits_converged(self) -> None:
        from liquidity_migration.financed_longs import (
            FundingSpreadConfig,
            funding_spread_weights,
            prepare_spread,
            daily_grid,
        )

        cfg = FundingSpreadConfig.from_json(CONFIG_DIR / "lane2_funding_spread_v1.json")
        n = 1100 // 8
        # deep spread (-90 bp/day) for the first half, converged (-22.5) after:
        # exit fires when |spread| < 20?  -22.5 stays above 20 -> pick -6 bp
        # prints -> spread -9 bp/day < 20 -> exit.
        pattern = [-60.0] * (n // 2) + [-6.0] * (n - n // 2)
        panel = _panel(hours=1100, funding_bp={"S01USDT": pattern})
        u = daily_grid(prepare_spread(panel))
        w = funding_spread_weights(u, cfg).filter(pl.col("symbol") == "S01USDT")
        assert w.height > 0
        spreads = dict(zip(u.filter(pl.col("symbol") == "S01USDT")["bar_ts_ms"].to_list(),
                           u.filter(pl.col("symbol") == "S01USDT")["spread_bpd"].to_list()))
        held_spreads = [spreads[t] for t in w["bar_ts_ms"].to_list()]
        assert all(abs(s) >= cfg.exit_bp_per_day for s in held_spreads)

    def test_fees_charged_on_both_legs(self) -> None:
        from liquidity_migration.financed_longs import FundingSpreadConfig, score_funding_spread

        cfg = FundingSpreadConfig.from_json(CONFIG_DIR / "lane2_funding_spread_v1.json")
        panel = _panel(hours=1100, funding_bp={"S01USDT": [-60.0]})
        out = score_funding_spread(panel, cfg)
        assert out["config_id"] == "lane2_funding_spread_v1"
        assert out["days"] > 0
        # entry day cost = 0.10 notional x BOTH legs x per-side fee
        from liquidity_migration.financed_longs import (
            daily_scores, funding_spread_weights, prepare_spread, daily_grid, top_n_universe,
        )
        u = top_n_universe(daily_grid(prepare_spread(panel)), cfg.universe_top_n)
        w = funding_spread_weights(u, cfg)
        s = daily_scores(w, u, 2.0 * cfg.fee_side_bp).sort("bar_ts_ms")
        assert s["cost_bp"][0] == pytest.approx(0.10 * 2.0 * cfg.fee_side_bp, rel=1e-6)


class TestFinancedLeaders:
    def test_funding_cap_excludes_paying_longs(self, leaders_cfg: FinancedLeadersConfig) -> None:
        # S01 rallies with negative funding (financed); S02 rallies with positive.
        # 24 symbols so the momentum decile holds both leaders; 1,100 hours so
        # the 30-day BTC gate window exists on the daily grid.
        panel = _panel(
            symbols=24,
            hours=1100,
            funding_bp={"S01USDT": [-2.0], "S02USDT": [5.0]},
            drift={"S01USDT": 0.0041, "S02USDT": 0.004},
        )
        grid = daily_grid(prepare(panel))
        u = top_n_universe(grid, 100)
        gate = btc_gate(grid, leaders_cfg.btc_gate_lookback_days)
        w = financed_leaders_weights(u, gate, leaders_cfg)
        names = set(w["symbol"].to_list())
        assert "S01USDT" in names
        assert "S02USDT" not in names

    def test_gate_uses_prior_days_only(self, leaders_cfg: FinancedLeadersConfig) -> None:
        grid = daily_grid(prepare(_panel()))
        gate = btc_gate(grid, leaders_cfg.btc_gate_lookback_days)
        # flat BTC: trend defined only once a full prior window exists, and nulls before
        head = gate.head(leaders_cfg.btc_gate_lookback_days)
        assert head["btc_trend"].null_count() == head.height

    def test_config_round_trip(self, leaders_cfg: FinancedLeadersConfig) -> None:
        assert leaders_cfg.funding_cap_bp == 0.0
        assert leaders_cfg.btc_gate_threshold == -0.05
        assert leaders_cfg.venue == "bybit"


#: The exact age sequence the production panel carries across one 8h funding
#: cycle (captured from cross_venue_panel_v1, BTCUSDT): float-ms arithmetic
#: leaves epsilon on several steps, and the bar after a settlement reads
#: 0.9999999999999999 — which `age < 1.0` wrongly counted as a second
#: settlement, charging every 8h/4h/2h print twice (2026-07-28 correction).
PRODUCTION_AGE_CYCLE = [
    0.0,
    0.9999999999999999,
    1.9999999999999998,
    3.0,
    3.9999999999999996,
    5.0,
    6.0,
    6.999999999999999,
]


def _funding_frame(ages: list[float], rate: float = -10.0 / 1e4) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["XUSDT"] * len(ages),
            "bar_ts_ms": [h * HOUR_MS for h in range(len(ages))],
            "by_funding": [rate] * len(ages),
            "by_funding_age_h": ages,
        }
    ).sort(["symbol", "bar_ts_ms"])


class TestSettlementDetector:
    """Regression contracts on REAL panel age shapes, not idealized ones."""

    def test_epsilon_age_bar_is_not_a_second_settlement(self) -> None:
        # 33 hourly bars = 4 settlements (h=0,8,16,24,32) with the production
        # epsilon pattern in between. The 24h window after bar 0 contains the
        # settlements at h=8,16,24 and nothing else: exactly 3 prints.
        ages = (PRODUCTION_AGE_CYCLE * 5)[:33]
        frame = _funding_frame(ages)
        out = frame.with_columns(settlement_exact_funding(24).alias("paid"))
        assert out["paid"][0] == pytest.approx(3 * (-10.0 / 1e4), abs=1e-15)

    def test_one_hour_interval_charges_every_print_once(self) -> None:
        # A 1h-interval symbol settles at every bar close: age is 0.0 on every
        # row and the 24h window charges 24 prints, each exactly once.
        frame = _funding_frame([0.0] * 25)
        out = frame.with_columns(settlement_exact_funding(24).alias("paid"))
        assert out["paid"][0] == pytest.approx(24 * (-10.0 / 1e4), abs=1e-15)

    def test_age_drop_after_gap_counts_the_missed_settlement_once(self) -> None:
        # The settlement bar itself is missing from the panel (data gap): the
        # next visible bar shows the new print aged 1.5h. The age DROP marks
        # one settlement; the stale continuation rows after it mark none.
        ages = [0.0, 0.9999999999999999, 1.9999999999999998, 1.5, 2.5, 3.5]
        frame = _funding_frame(ages)
        out = frame.with_columns(settlement_exact_funding(5).alias("paid"))
        # Window after bar 0 covers bars 1..5: only the age-drop bar (1.5).
        assert out["paid"][0] == pytest.approx(1 * (-10.0 / 1e4), abs=1e-15)


class TestResearchEquityChart:
    """The wrapper-supported standard-format render for registered configs.

    Exists so a Lane-2 config never needs a hand-built lookalike chart (the
    equity-curve skill forbids second formats; 2026-07-28).
    """

    def test_renders_standard_chart_with_research_label(self, tmp_path: Path) -> None:
        from liquidity_migration.financed_longs import research_equity_chart

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
        png = tmp_path / "lane2_carry_hold_v1_equity_btc.png"
        assert png.exists() and png.stat().st_size > 0
        assert payload["png"] == str(png)
        assert (tmp_path / "lane2_carry_hold_v1_daily_equity.csv").exists()
        assert (tmp_path / "lane2_carry_hold_v1_summary.json").exists()
        # The chart must carry the full standard metric tile set.
        assert {"total_return_pct", "max_drawdown_pct", "sharpe_daily_ann", "mar", "years"} <= set(
            payload["metrics"]
        )

    def test_config_scores_dispatches_both_rule_shapes(self) -> None:
        from liquidity_migration.financed_longs import config_scores

        panel = _panel(funding_bp={"S01USDT": [-15.0]})
        _, _, carry_id, _ = config_scores(panel, CONFIG_DIR / "lane2_carry_hold_v1.json")
        _, _, carry2_id, _ = config_scores(panel, CONFIG_DIR / "lane2_carry_hold_v2.json")
        _, _, leaders_id, _ = config_scores(panel, CONFIG_DIR / "lane2_financed_leaders_v1.json")
        assert carry_id == "lane2_carry_hold_v1"
        assert carry2_id == "lane2_carry_hold_v2"
        assert leaders_id == "lane2_financed_leaders_v1"


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
        # Turnover used to iterate only bars PRESENT in `weights`, so a flat
        # decision day charged neither the exit into it nor the re-entry out of
        # it while gross treated the book as liquidated (2026-07-27 audit M19).
        held = {int(value) for value in w["bar_ts_ms"].unique().to_list()}
        decision_bars = sorted(
            value
            for value in {int(item) for item in u["bar_ts_ms"].unique().to_list()}
            if min(held) <= value <= max(held)
        )
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
        assert len(exits) == 4
        for ts in entries + exits:
            assert by_ts[ts] == pytest.approx(0.10, rel=1e-6)
        for ts in decision_bars:
            if ts not in entries and ts not in exits:
                assert by_ts[ts] == pytest.approx(0.0, abs=1e-12)
        assert scores["oneway"].sum() == pytest.approx(0.10 * 9, rel=1e-9)

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
        cfg = FinancedLeadersConfig.from_json(CONFIG_DIR / "lane2_financed_leaders_binance_v1.json")
        assert cfg.venue == "binance"
        view = venue_view(_panel(), "binance")
        assert "by_close" in view.columns and "bn_close" not in view.columns

    def test_score_carry_hold_end_to_end(self, carry_cfg: CarryHoldConfig) -> None:
        panel = _panel(funding_bp={"S01USDT": [-15.0]})
        out = score_carry_hold(panel, carry_cfg)
        assert out["config_id"] == "lane2_carry_hold_v1"
        assert out["days"] > 0
