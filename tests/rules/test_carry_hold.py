"""Contracts for the registered carry-hold decision rule.

A lookahead in the hysteresis state, a settlement charged twice, or a live
frame that drifts from the research frame each make the number better rather
than raise, so each is pinned on deterministic synthetic frames. The research
scorers that grade this rule keep their own tests in
``tests/research/backtest/test_financed_longs.py``, which shares the synthetic
panel fixture.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.research.backtest.financed_longs import prepare, score_carry_hold, venue_view
from liquidity_migration.rules.carry_hold import (
    HOUR_MS,
    CarryHoldConfig,
    FinancedLongsError,
    carry_hold_weights,
    daily_grid,
    prepare_decision,
    settlement_exact_funding,
    top_n_universe,
)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs"


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

    The state machine is v1's; only the size of a held name changes. v1 configs carry
    no depth block and must keep the flat cap bit-for-bit.
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

    Each feature is exercised in isolation via ``dataclasses.replace`` so a failure
    names its filter; the committed JSON is round-tripped and run end-to-end. All v3
    fields default off, so v1/v2 behaviour is pinned unchanged by the other classes.
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


#: The exact age sequence the production panel carries across one 8h funding
#: cycle (captured from cross_venue_panel_v1, BTCUSDT): float-ms arithmetic
#: leaves epsilon on several steps, and the bar after a settlement reads
#: 0.9999999999999999, which an `age < 1.0` predicate counts as a second
#: settlement, charging every 8h/4h/2h print twice.
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
    """Settlement-detector contracts on real panel age shapes, not idealized ones."""

    def test_epsilon_age_bar_is_not_a_second_settlement(self) -> None:
        # 33 hourly bars = 4 settlements (h=0,8,16,24,32) with the production
        # epsilon pattern in between. The 24h window after bar 0 contains the
        # settlements at h=8,16,24 and nothing else: exactly 3 prints.
        ages = (PRODUCTION_AGE_CYCLE * 5)[:33]
        frame = _funding_frame(ages)
        out = frame.with_columns(settlement_exact_funding(24).alias("paid"))
        assert out["paid"][0] == pytest.approx(3 * (-10.0 / 1e4), abs=1e-15)

    def test_forward_window_stays_inside_its_symbol(self) -> None:
        # Two symbols, symbol-major rows, every bar a settlement. The forward
        # shift must happen inside the per-symbol window: a global shift hands
        # the first symbol's tail rows the next symbol's sums (100x larger
        # here) instead of null.
        rows, hold = 6, 2
        frame = pl.DataFrame(
            {
                "symbol": ["AUSDT"] * rows + ["BUSDT"] * rows,
                "bar_ts_ms": [h * 3_600_000 for h in range(rows)] * 2,
                "by_funding": [1.0] * rows + [100.0] * rows,
                "by_funding_age_h": [0.0] * (2 * rows),
            }
        ).sort(["symbol", "bar_ts_ms"])
        out = frame.with_columns(settlement_exact_funding(hold).alias("paid"))
        first = out.filter(pl.col("symbol") == "AUSDT")["paid"]
        assert first[rows - hold :].null_count() == hold
        assert set(first[: rows - hold].to_list()) == {2.0}

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


class TestCrowdPersistence:
    """v4's sizing feature: how habitual a name's crowding is.

    The feature is counted in the symbol's own settlement sequence rather than on
    a clock, so the tests pin that too — a clock version silently reports the
    symbol's funding interval, and Bybit's interval mix moved hard in 2025.
    """

    def test_column_is_the_share_of_the_prior_twenty_settlements(self) -> None:
        # Settlements 0-29 deep, 30-59 benign. At settlement k the value covers
        # k-20 .. k-1, so k=35 spans 15 deep and 5 benign.
        panel = _panel(hours=480, funding_bp={"S01USDT": [-15.0] * 30 + [1.0] * 30})
        prepared = prepare(panel).filter(pl.col("symbol") == "S01USDT").sort("bar_ts_ms")

        def at(settlement: int) -> float:
            row = prepared.filter(pl.col("bar_ts_ms") == settlement * 8 * HOUR_MS)
            assert row.height == 1, f"settlement {settlement} not in the prepared frame"
            return float(row["crowd_persistence"].item())

        assert at(25) == pytest.approx(1.0)
        assert at(35) == pytest.approx(0.75)
        assert at(50) == pytest.approx(0.0)

    def test_value_is_carried_forward_between_settlements(self) -> None:
        # Bars between settlements must read the last completed settlement's
        # value, not null and not the next one's.
        panel = _panel(hours=480, funding_bp={"S01USDT": [-15.0] * 30 + [1.0] * 30})
        prepared = prepare(panel).filter(pl.col("symbol") == "S01USDT").sort("bar_ts_ms")
        base = prepared.filter(pl.col("bar_ts_ms") == 35 * 8 * HOUR_MS)["crowd_persistence"].item()
        for offset in (1, 4, 7):
            ts = 35 * 8 * HOUR_MS + offset * HOUR_MS
            row = prepared.filter(pl.col("bar_ts_ms") == ts)
            assert row["crowd_persistence"].item() == pytest.approx(base)

    def test_first_deep_print_never_counts_itself(self) -> None:
        # A name whose ONLY deep print is its most recent settlement must read 0.0
        # persistence at that bar: the feature describes history before the print,
        # so a single liquidation can never look habitual.
        deep_at = 25
        rates = [1.0] * deep_at + [-15.0] * 5
        panel = _panel(hours=480, funding_bp={"S01USDT": rates})
        prepared = prepare(panel).filter(pl.col("symbol") == "S01USDT").sort("bar_ts_ms")
        at_first_deep = prepared.filter(pl.col("bar_ts_ms") == deep_at * 8 * HOUR_MS)
        assert at_first_deep.height == 1
        assert at_first_deep["crowd_persistence"].item() == pytest.approx(0.0)

    def test_research_and_live_frames_agree(self) -> None:
        panel = _panel(hours=480, funding_bp={"S01USDT": [-15.0, 1.0]})
        view = venue_view(panel, "bybit")
        res = prepare(view).select("symbol", "bar_ts_ms", "crowd_persistence")
        live = prepare_decision(view).select("symbol", "bar_ts_ms", "crowd_persistence")
        joined = res.join(live, on=["symbol", "bar_ts_ms"], how="inner", suffix="_live")
        assert joined.height == res.height
        same = joined.select(
            (pl.col("crowd_persistence").eq_missing(pl.col("crowd_persistence_live"))).all()
        ).item()
        assert same, "the live frame must carry the identical feature"

    def test_low_persistence_drops_the_name(self) -> None:
        # One isolated deep print: persistence 0.0 <= the 0.10 cut, so v4 holds
        # nothing while v3 holds the name.
        panel = _panel(hours=480, funding_bp={"S01USDT": [1.0] * 25 + [-15.0] * 10})
        u = _universe(panel)
        v3 = CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v3.json")
        v4 = CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v4.json")
        held3 = carry_hold_weights(u, v3).filter(pl.col("symbol") == "S01USDT")
        held4 = carry_hold_weights(u, v4).filter(pl.col("symbol") == "S01USDT")
        assert held3.height > 0
        assert held4.height < held3.height

    def test_null_persistence_fails_open_at_full_size(self) -> None:
        # Fewer than PERSISTENCE_WINDOW settlements of history must NOT downsize:
        # that would make v4 a covert listing-age screen, and listing age has
        # produced two false positives in this program.
        #
        # An 8h-cadence symbol reaches 21 settlements inside prepare()'s own
        # 168h warmup, so the null branch is unreachable there. A 24h-cadence
        # symbol is the case that exercises it — which is also why the branch
        # never fires on the registered book.
        panel = _panel(hours=400, funding_bp={"S01USDT": [-15.0]}).with_columns(
            pl.when(pl.col("symbol") == "S01USDT")
            .then((pl.col("bar_ts_ms") // HOUR_MS % 24).cast(pl.Float64))
            .otherwise(pl.col("by_funding_age_h"))
            .alias("by_funding_age_h")
        )
        v4 = CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v4.json")
        prepared = prepare(panel).filter(pl.col("symbol") == "S01USDT")
        assert prepared["crowd_persistence"].null_count() == prepared.height, (
            "the 24h-cadence symbol must never reach a full 20-settlement window here"
        )
        u = _universe(panel)
        held = carry_hold_weights(u, v4).filter(pl.col("symbol") == "S01USDT")
        assert held.height > 0, "a null persistence must not block the name"
        # "Fails open" means the weight is whatever the rest of the rule says —
        # here the depth ladder's floor — not that persistence forces full size.
        off = carry_hold_weights(u, dataclasses.replace(v4, persistence_window=None)).filter(
            pl.col("symbol") == "S01USDT"
        )
        assert held.equals(off)

    def test_window_mismatch_is_rejected_not_silently_rescored(self) -> None:
        v4 = CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v4.json")
        bad = dataclasses.replace(v4, persistence_window=5)
        with pytest.raises(FinancedLongsError, match="persistence window"):
            carry_hold_weights(_universe(_panel()), bad)

    def test_entry_threshold_mismatch_is_rejected(self) -> None:
        v4 = CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v4.json")
        bad = dataclasses.replace(v4, enter_bp=25.0)
        with pytest.raises(FinancedLongsError, match="deeper than"):
            carry_hold_weights(_universe(_panel()), bad)


class TestRegisteredConfigsAreImmutable:
    """v4 must not move v1/v2/v3. The forward records depend on it."""

    @pytest.mark.parametrize("name", ["v1", "v2", "v3"])
    def test_persistence_defaults_off(self, name: str) -> None:
        cfg = CarryHoldConfig.from_json(CONFIG_DIR / f"lane2_carry_hold_{name}.json")
        assert cfg.persistence_window is None

    @pytest.mark.parametrize("name", ["v1", "v2", "v3"])
    def test_weights_ignore_the_new_column(self, name: str) -> None:
        cfg = CarryHoldConfig.from_json(CONFIG_DIR / f"lane2_carry_hold_{name}.json")
        u = _universe(_panel(funding_bp={"S01USDT": [-15.0, 1.0], "S02USDT": [-12.0]}))
        with_col = carry_hold_weights(u, cfg)
        without = carry_hold_weights(u.drop("crowd_persistence"), cfg)
        assert with_col.equals(without)

    def test_v4_declares_the_two_changes_and_nothing_else(self) -> None:
        v3 = CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v3.json")
        v4 = CarryHoldConfig.from_json(CONFIG_DIR / "lane2_carry_hold_v4.json")
        changed = {
            f.name
            for f in dataclasses.fields(CarryHoldConfig)
            if getattr(v3, f.name) != getattr(v4, f.name)
        }
        assert changed == {
            "config_id",
            "toxic_band_ret3d",
            "persistence_window",
        }, f"v4 changed more than the two declared levers: {changed}"
        assert v3.toxic_band_ret3d == (-0.30, -0.05)
        assert v4.toxic_band_ret3d == (-0.30, 0.0)
