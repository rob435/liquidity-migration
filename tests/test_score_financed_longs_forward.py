"""Contracts for the financed-longs forward ledger appender.

The ledger is receipts: append-first, never rewriting an existing
(date, config_id) row, every scored day present with an explicit
``forward_eligible`` flag, and the carry-hold v2-vs-v1 paired differential
derived inside the same file. Idempotency and append-only behavior are the
load-bearing properties — a scorer that silently rewrites history can
flatter a forward record.
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl
import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.score_financed_longs_forward import DIFF_ID, main  # noqa: E402

HOUR_MS = 3_600_000


def _panel_parquet(tmp_path: Path) -> Path:
    """Year-sharded panel: deep-funding S01 (carry) and a financed leader S02."""
    rows: list[dict[str, object]] = []
    symbols = 24
    hours = 1100
    for s in range(symbols):
        sym = f"S{s:02d}USDT" if s else "BTCUSDT"
        price = 100.0
        for h in range(hours):
            drift = 0.004 if sym == "S02USDT" else (0.0005 if sym == "BTCUSDT" else 0.0)
            price *= 1.0 + drift
            settle = (h % 8) == 0
            if sym == "S01USDT":
                rate = -15.0 / 1e4
            elif sym == "S02USDT":
                rate = -1.0 / 1e4
            else:
                rate = 1.0 / 1e4
            rows.append(
                {
                    "symbol": sym,
                    "bar_ts_ms": h * HOUR_MS,
                    "by_close": price,
                    "by_turnover_quote": 1_000_000.0 * (symbols - s),
                    "by_funding": rate,
                    "by_funding_age_h": 0.0 if settle else float(h % 8),
                    "bn_close": price * 1.001,
                    "bn_turnover_quote": 900_000.0 * (symbols - s),
                    "bn_funding": rate / 2.0,
                    "bn_funding_age_h": 0.0 if settle else float(h % 8),
                }
            )
    root = tmp_path / "panel"
    (root / "1970").mkdir(parents=True)
    pl.DataFrame(rows).write_parquet(root / "1970" / "panel.parquet")
    return root


@pytest.fixture
def panel_root(tmp_path: Path) -> Path:
    return _panel_parquet(tmp_path)


def _run(panel_root: Path, ledger: Path, *extra: str) -> None:
    assert main(["--panel-root", str(panel_root), "--ledger", str(ledger), *extra]) == 0


class TestForwardLedger:
    def test_creates_all_config_rows_and_differential(self, panel_root: Path, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.csv"
        _run(panel_root, ledger)
        out = pl.read_csv(ledger)
        ids = set(out["config_id"].to_list())
        assert {"lane2_carry_hold_v1", "lane2_carry_hold_v2", DIFF_ID} <= ids
        # the differential is exactly v2 - v1 per shared date
        v1 = out.filter(pl.col("config_id") == "lane2_carry_hold_v1")
        v2 = out.filter(pl.col("config_id") == "lane2_carry_hold_v2")
        d = out.filter(pl.col("config_id") == DIFF_ID)
        assert d.height == min(v1.height, v2.height) > 0
        joined = (
            d.select("date", pl.col("net_bp").alias("diff"))
            .join(v2.select("date", pl.col("net_bp").alias("a")), on="date")
            .join(v1.select("date", pl.col("net_bp").alias("b")), on="date")
        )
        assert (
            (joined["diff"] - (joined["a"] - joined["b"])).abs().max()
        ) == pytest.approx(0.0, abs=1e-6)

    def test_seen_data_is_not_forward_eligible(self, panel_root: Path, tmp_path: Path) -> None:
        # synthetic dates are 1970 — decades before any registration commit
        ledger = tmp_path / "ledger.csv"
        _run(panel_root, ledger)
        out = pl.read_csv(ledger)
        assert not out["forward_eligible"].any()

    def test_idempotent_rerun_appends_nothing(self, panel_root: Path, tmp_path: Path) -> None:
        ledger = tmp_path / "ledger.csv"
        _run(panel_root, ledger)
        first = ledger.read_bytes()
        _run(panel_root, ledger)
        assert ledger.read_bytes() == first

    def test_append_only_extension_preserves_existing_rows(
        self, panel_root: Path, tmp_path: Path
    ) -> None:
        ledger = tmp_path / "ledger.csv"
        _run(panel_root, ledger, "--end-date", "1970-02-01")
        early = pl.read_csv(ledger)
        assert early.height > 0
        assert (early["date"] < "1970-02-01").all()
        _run(panel_root, ledger)
        full = pl.read_csv(ledger)
        assert full.height > early.height
        # every early row survives byte-identically (receipts, not state)
        merged = early.join(
            full, on=["date", "config_id"], how="left", suffix="_new"
        )
        assert (merged["net_bp"] - merged["net_bp_new"]).abs().max() == pytest.approx(0.0, abs=1e-12)
        assert (merged["scored_at"] == merged["scored_at_new"]).all()
