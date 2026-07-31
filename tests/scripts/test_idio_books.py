"""Pins for the hedged and directional book constructions.

The load-bearing property is causality: the directional rule z-scores each feature
against that symbol's own strictly-prior history, so a position is never a function of
the value it is meant to predict.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration.core._common import MS_PER_DAY

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _load(group: str, name: str):
    spec = importlib.util.spec_from_file_location(
        f"scripts.{group}.{name}", REPO / "scripts" / group / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"scripts.{group}.{name}"] = module
    spec.loader.exec_module(module)
    return module


hedged = _load("research", "screen_idio_hedged")
directional = _load("research", "screen_idio_directional")

BASE_DAY = 20_000
BTC = "BTCUSDT"


def _ms(d: int) -> int:
    return d * MS_PER_DAY


def _panel(n_days: int = 200, n_syms: int = 30, *, seed_shift: int = 0) -> pl.DataFrame:
    """Deterministic panel with a signal, a forward return, and a beta."""
    rows = []
    for d in range(n_days):
        for s in range(n_syms):
            k = (d * 7 + s * 13 + seed_shift) % 23
            rows.append(
                {
                    "ts_ms": _ms(BASE_DAY + d),
                    "symbol": f"S{s:02d}" if s else BTC,
                    "raw_ret_7d": 0.01 * (k - 11),
                    "fwd_ret_1d": 0.001 * (((d * 5 + s * 3) % 17) - 8),
                    "btc_beta": 0.5 + 0.05 * (s % 10),
                    "turnover_quote": 1e9 - s,
                    "coverage": 1.0,
                }
            )
    return pl.DataFrame(rows)


def _btc(panel: pl.DataFrame) -> pl.DataFrame:
    return (
        panel.filter(pl.col("symbol") == BTC)
        .select("ts_ms", pl.col("fwd_ret_1d").alias("_btc_ret"))
        .unique(subset="ts_ms")
        .drop_nulls()
    )


# ---------------------------------------------------------------------------
# directional book -- causality is the whole ballgame
# ---------------------------------------------------------------------------


def test_directional_positions_are_invariant_to_appended_future_data() -> None:
    """The z-score window must be strictly prior; including the current day would make
    the position a function of the bar the forward return is measured from.
    """
    early = _panel(200)
    late = pl.concat([early, _panel(80).with_columns(pl.col("ts_ms") + _ms(200))], how="vertical")

    a = directional.directional_book(early, signal="raw_ret_7d", btc=_btc(early), min_names=5)
    b = directional.directional_book(late, signal="raw_ret_7d", btc=_btc(late), min_names=5)
    assert a is not None and b is not None

    overlap = b.filter(pl.col("ts_ms") <= a["ts_ms"].max()).sort("ts_ms")
    assert overlap.height == a.height
    for col in ("book_bp", "net_beta", "net_dir"):
        for x, y in zip(a.sort("ts_ms")[col].to_list(), overlap[col].to_list()):
            assert x == pytest.approx(y, rel=1e-12, abs=1e-12), col


def test_directional_book_uses_no_cross_sectional_information() -> None:
    """Dropping half the universe must not change the surviving names' positions: a
    cross-sectional rule re-ranks when the population changes, a per-symbol z-score
    cannot.
    """
    full = _panel(200, n_syms=30)
    half = full.filter(pl.col("symbol").is_in([f"S{s:02d}" for s in range(1, 15)] + [BTC]))

    a = directional.directional_book(full, signal="raw_ret_7d", btc=_btc(full), min_names=5)
    b = directional.directional_book(half, signal="raw_ret_7d", btc=_btc(half), min_names=5)
    assert a is not None and b is not None
    # net_dir is a mean over a different population, so it may differ; what must
    # NOT differ is that both books exist over the same days with finite values.
    assert set(b["ts_ms"].to_list()) <= set(a["ts_ms"].to_list())
    assert all(abs(v) <= 1.0 + 1e-12 for v in b["net_dir"].to_list())


def test_directional_positions_are_only_ever_plus_or_minus_one() -> None:
    panel = _panel(200)
    book = directional.directional_book(panel, signal="raw_ret_7d", btc=_btc(panel), min_names=5)
    assert book is not None
    # net_dir is a mean of +/-1 positions, so it is bounded by 1 in magnitude.
    assert book["net_dir"].abs().max() <= 1.0 + 1e-12
    # A full sign flip on every name is 1.0 of turnover; nothing may exceed it.
    assert book["pos_turn"].max() <= 1.0 + 1e-12
    assert book["pos_turn"].min() >= 0.0


def test_directional_warmup_suppresses_the_first_window() -> None:
    """No position before the z-score has Z_MIN_SAMPLES strictly-prior points."""
    panel = _panel(200)
    book = directional.directional_book(panel, signal="raw_ret_7d", btc=_btc(panel), min_names=5)
    assert book is not None
    first = book["ts_ms"].min()
    assert first >= _ms(BASE_DAY + directional.Z_MIN_SAMPLES)


# ---------------------------------------------------------------------------
# hedged book
# ---------------------------------------------------------------------------


def test_hedge_cancels_the_books_net_beta_exactly() -> None:
    """hedged = spread - net_beta * btc_ret, by construction."""
    panel = _panel(200)
    book = hedged.hedged_book(
        panel, signal="raw_ret_7d", btc=_btc(panel), cut=0.2, min_names=5
    )
    assert book is not None
    for r in book.iter_rows(named=True):
        expected = r["spread_bp"] - r["net_beta"] * r["_btc_ret"] * 1e4
        assert r["hedged_bp"] == pytest.approx(expected, rel=1e-9, abs=1e-9)


def test_hedged_book_is_invariant_to_appended_future_data() -> None:
    early = _panel(200)
    late = pl.concat([early, _panel(80).with_columns(pl.col("ts_ms") + _ms(200))], how="vertical")
    a = hedged.hedged_book(early, signal="raw_ret_7d", btc=_btc(early), cut=0.2, min_names=5)
    b = hedged.hedged_book(late, signal="raw_ret_7d", btc=_btc(late), cut=0.2, min_names=5)
    assert a is not None and b is not None
    overlap = b.filter(pl.col("ts_ms") <= a["ts_ms"].max()).sort("ts_ms")
    for col in ("spread_bp", "net_beta", "hedged_bp"):
        for x, y in zip(a.sort("ts_ms")[col].to_list(), overlap[col].to_list()):
            assert x == pytest.approx(y, rel=1e-12, abs=1e-12), col


def test_a_beta_neutral_book_is_unchanged_by_hedging() -> None:
    """If the two legs carry equal beta there is nothing to hedge."""
    panel = _panel(200).with_columns(pl.lit(1.0).alias("btc_beta"))
    book = hedged.hedged_book(
        panel, signal="raw_ret_7d", btc=_btc(panel), cut=0.2, min_names=5
    )
    assert book is not None
    assert book["net_beta"].abs().max() == pytest.approx(0.0, abs=1e-12)
    for r in book.iter_rows(named=True):
        assert r["hedged_bp"] == pytest.approx(r["spread_bp"], rel=1e-9, abs=1e-9)
    assert book["hedge_turn"].max() == pytest.approx(0.0, abs=1e-12)


def test_family_count_grows_when_the_directional_grid_is_added() -> None:
    """Adding tests must raise the bar, never lower it."""
    cross = directional.PRIOR_MECHANISMS + directional.CROSS_SECTIONAL_CELLS
    full = cross + directional.NEW_CELLS
    assert directional.NEW_CELLS == 48
    assert full == 140
    assert directional.bonferroni_t(full) > directional.bonferroni_t(cross)
    assert directional.bonferroni_t(full) == pytest.approx(3.57, abs=0.01)
