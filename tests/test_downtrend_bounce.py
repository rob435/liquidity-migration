"""Tests for the standalone down-regime bounce paper product (downtrend_bounce_v1)."""

from __future__ import annotations

import datetime as dt
import json

import polars as pl

from liquidity_migration.downtrend_bounce import (
    SLEEVE_GROSS,
    bounce_targets,
    mark_book,
    run_paper_cycle,
)

D = dt.date(2026, 1, 31)


def _daily_frame(n_alts: int = 60, *, btc_down: bool = True, skip: dict | None = None,
                 turnover: dict | None = None) -> pl.DataFrame:
    """Synthetic daily rows for [D-32, D]; ALTi 7d return rises with i."""
    skip = skip or {}
    turnover = turnover or {}
    rows = []
    for k in range(33):
        day = D - dt.timedelta(days=32 - k)
        btc = 100.0 - (1.0 if btc_down else -1.0) * k  # monotone trend through D
        rows.append(("BTCUSDT", day, btc, 1e9))
        for i in range(n_alts):
            sym = f"ALT{i:02d}USDT"
            if day in skip.get(sym, set()):
                continue
            # close grows i*1% over the last 7 days, flat before
            base = 100.0
            days_from_end = (D - day).days
            close = base * (1 + i * 0.01 * max(0.0, (7 - days_from_end) / 7)) if days_from_end < 7 else base
            rows.append((sym, day, close, turnover.get(sym, 1_000_000.0)))
    return pl.DataFrame(
        {"symbol": [r[0] for r in rows], "d": [r[1] for r in rows],
         "close": [r[2] for r in rows], "turnover_quote": [r[3] for r in rows]}
    )


def test_up_regime_means_flat() -> None:
    tgt = bounce_targets(_daily_frame(btc_down=False), D)
    assert tgt["regime_down"] is False
    assert tgt["weights"] == {}


def test_down_regime_selects_bottom_decile_equal_weight() -> None:
    tgt = bounce_targets(_daily_frame(), D)
    assert tgt["regime_down"] is True
    assert tgt["btc30"] < 0
    assert tgt["n_universe"] == 60
    assert len(tgt["weights"]) == 6  # max(int(60*0.1), 5)
    assert set(tgt["weights"]) == {f"ALT{i:02d}USDT" for i in range(6)}
    assert abs(sum(tgt["weights"].values()) - SLEEVE_GROSS) < 1e-12
    assert all(abs(w - SLEEVE_GROSS / 6) < 1e-12 for w in tgt["weights"].values())


def test_liq_floor_and_coverage_gaps_exclude_names() -> None:
    frame = _daily_frame(
        turnover={"ALT00USDT": 100.0},               # below the 500k floor
        skip={"ALT01USDT": {D - dt.timedelta(days=3)}},  # missing day in the signal window
    )
    tgt = bounce_targets(frame, D)
    assert tgt["n_universe"] == 58
    assert "ALT00USDT" not in tgt["weights"]
    assert "ALT01USDT" not in tgt["weights"]


def test_small_universe_means_no_book() -> None:
    tgt = bounce_targets(_daily_frame(n_alts=20), D)
    assert tgt["regime_down"] is True
    assert tgt["weights"] == {}
    assert tgt["n_universe"] == 20


def test_mark_book_arithmetic() -> None:
    rows = []
    for sym, c0, c1 in [("AUSDT", 100.0, 110.0), ("BUSDT", 100.0, 95.0)]:
        rows.append((sym, D - dt.timedelta(days=1), c0))
        rows.append((sym, D, c1))
    daily = pl.DataFrame(
        {"symbol": [r[0] for r in rows], "d": [r[1] for r in rows],
         "close": [r[2] for r in rows], "turnover_quote": [1e6] * len(rows)}
    )
    weights = {"AUSDT": 0.25, "BUSDT": 0.25}
    mark = mark_book(daily, weights, D, turnover=0.5, funding={"AUSDT": 0.001})
    assert abs(mark["gross_return"] - (0.25 * 0.10 - 0.25 * 0.05)) < 1e-9
    assert abs(mark["cost_return"] - (-0.5 * 6.0 / 1e4)) < 1e-12
    assert abs(mark["funding_return"] - (-0.25 * 0.001)) < 1e-12
    assert mark["funding_missing"] is False
    assert mark["marked_names"] == 2

    mark_nf = mark_book(daily, weights, D, turnover=0.5, funding=None)
    assert mark_nf["funding_missing"] is True
    assert mark_nf["funding_return"] is None


def _write_klines(root, frame: pl.DataFrame) -> None:
    for day in frame["d"].unique().to_list():
        part = root / "klines_1h" / f"date={day.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        sub = frame.filter(pl.col("d") == day).with_columns(pl.lit(1, dtype=pl.Int64).alias("ts_ms"))
        sub.select(["ts_ms", "symbol", "close", "turnover_quote"]).write_parquet(part / "part-0.parquet")


def test_run_paper_cycle_emits_target_then_marks(tmp_path) -> None:
    frame = _daily_frame()
    _write_klines(tmp_path, frame)
    ledger = tmp_path / "ledger.jsonl"

    s1 = run_paper_cycle(tmp_path, "bybit", ledger, decision_date=D - dt.timedelta(days=1))
    assert s1["target"]["regime_down"] is True
    assert s1["target"]["n_names"] == 6

    s2 = run_paper_cycle(tmp_path, "bybit", ledger, decision_date=D)
    assert "mark" in s2
    assert s2["mark"]["funding_missing"] is True  # no funding dataset in the tmp root
    assert s2["mark"]["marked_names"] == 6

    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    kinds = [r["kind"] for r in rows]
    assert kinds == ["target", "mark", "target"]
    assert rows[1]["for_decision_date"] == (D - dt.timedelta(days=1)).isoformat()
    assert rows[1]["mark_date"] == D.isoformat()
    # books overlap day-to-day, so turnover is the one-way weight change only
    assert rows[2]["turnover"] < 2 * SLEEVE_GROSS

    s3 = run_paper_cycle(tmp_path, "bybit", ledger, decision_date=D)
    assert s3.get("skipped") == "target_exists"
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 3
