"""Standalone down-regime bounce product (``downtrend_bounce_v1``) — paper-first.

The one validated way to deploy capital during BTC-30d-down regimes, where the
continuous book is deliberately hedge+cash. The profile is FROZEN from the D3
receipt (``downtrend-bounce-long-2026-06-09``, standalone bar PASS on both venues:
Sharpe 2.03 bybit / 0.81 binance, funding-positive). DR1
(``downtrend-hedged-bounce-2026-06-10``) falsified the short-BTC hedge overlay —
the sleeve's drawdown is alt-idiosyncratic, not index beta — so the product is
UNHEDGED and carries its native −30..−44% in-regime drawdown class as an operator
risk-budget decision (the D3 "separate product" clause).

PAPER ONLY: this module submits no orders. Each daily cycle (run after the UTC
close) computes the day-D-close decision, appends the target book for day D+1 to an
append-only JSONL ledger, and marks the previous row's book with day-D realized
close-to-close returns, 12 bps round-trip costs on one-way turnover, and real
funding day-sums when the root's funding dataset covers the day. Demo-order wiring
is operator-gated behind the pre-registered forward bar
(``downtrend-bounce-forward-paper-2026-06-10``), which also requires an
account-level latched disaster stop before any order submission.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import polars as pl

STRATEGY_ID = "downtrend_bounce_v1"

# D3-frozen profile — do not tune without a fresh pre-registration.
LIQ_FLOOR_USDT = 500_000.0
SLEEVE_GROSS = 0.5
DECILE_FRAC = 0.10
MIN_DECILE_NAMES = 5
MIN_UNIVERSE_NAMES = 50
BTC_TREND_DAYS = 30
SIGNAL_DAYS = 7
COST_PER_SIDE_BPS = 6.0  # 12 bps round-trip on one-way turnover, research convention

BTC_SYMBOL = "BTCUSDT"
# Same exclusion set as the D1/D2/D3 research scripts.
STABLE_SYMBOLS = {
    "BUSDUSDT", "DAIUSDT", "FDUSDUSDT", "FRAXUSDT", "GUSDUSDT", "LUSDUSDT", "PYUSDUSDT",
    "SUSDUSDT", "TUSDUSDT", "USD1USDT", "USDCUSDT", "USDDUSDT", "USDEUSDT", "USDPUSDT",
    "USTCUSDT", "USDYUSDT",
}
FUNDING_DATASET = {"bybit": "funding", "binance": "binance_usdm_funding"}


def daily_rows_from_klines(root: Path, start: dt.date, end: dt.date) -> pl.DataFrame:
    """Daily (symbol, d, close, turnover_quote) from ``klines_1h`` date partitions.

    Same construction as the verified research panel tail: per symbol-day the last
    hourly close by ts_ms and the summed quote turnover.
    """
    frames = []
    day = start
    while day <= end:
        part = root / "klines_1h" / f"date={day.isoformat()}"
        if part.exists():
            raw = pl.read_parquet(part, columns=["ts_ms", "symbol", "close", "turnover_quote"])
            agg = (
                raw.sort("ts_ms")
                .group_by("symbol", maintain_order=True)
                .agg(pl.col("close").last(), pl.col("turnover_quote").sum())
                .with_columns(pl.lit(day).alias("d"))
            )
            frames.append(agg.select(["symbol", "d", "close", "turnover_quote"]))
        day += dt.timedelta(days=1)
    if not frames:
        return pl.DataFrame(
            {"symbol": pl.Series([], dtype=pl.Utf8), "d": pl.Series([], dtype=pl.Date),
             "close": pl.Series([], dtype=pl.Float64), "turnover_quote": pl.Series([], dtype=pl.Float64)}
        )
    return pl.concat(frames)


def _close_map(daily: pl.DataFrame, day: dt.date) -> dict[str, float]:
    sub = daily.filter(pl.col("d") == day)
    return {s: float(c) for s, c in sub.select(["symbol", "close"]).iter_rows() if c is not None and c > 0}


def bounce_targets(daily: pl.DataFrame, decision_date: dt.date) -> dict[str, Any]:
    """Day-D-close decision: the target book for day D+1 (causal through D).

    Mirrors the D3 ``run_sleeve`` selection with k=1: on a BTC-30d-down decision
    close, LONG the bottom decile of the 7d return among eligible names, equal
    weight, gross ``SLEEVE_GROSS``. Eligibility requires a row on every calendar day
    in [D-SIGNAL_DAYS, D] (a missing day means listing gap / delisting — skip), and
    turnover(D) >= the liq floor. Empty book when the universe is under
    ``MIN_UNIVERSE_NAMES`` or the regime is up.
    """
    close_now = _close_map(daily, decision_date)
    close_30 = _close_map(daily, decision_date - dt.timedelta(days=BTC_TREND_DAYS))
    btc_now, btc_then = close_now.get(BTC_SYMBOL), close_30.get(BTC_SYMBOL)
    if btc_now is None or btc_then is None:
        return {"regime_down": None, "btc30": None, "weights": {}, "n_universe": 0,
                "error": "btc_close_missing"}
    btc30 = btc_now / btc_then - 1.0
    if btc30 >= 0:
        return {"regime_down": False, "btc30": btc30, "weights": {}, "n_universe": 0}

    window = [decision_date - dt.timedelta(days=k) for k in range(SIGNAL_DAYS + 1)]
    maps = {day: _close_map(daily, day) for day in window}
    turn = {
        s: float(t)
        for s, t in daily.filter(pl.col("d") == decision_date).select(["symbol", "turnover_quote"]).iter_rows()
        if t is not None
    }
    close_sig = maps[decision_date - dt.timedelta(days=SIGNAL_DAYS)]
    signal: dict[str, float] = {}
    for sym, c_now in maps[decision_date].items():
        if sym == BTC_SYMBOL or sym in STABLE_SYMBOLS:
            continue
        if turn.get(sym, 0.0) < LIQ_FLOOR_USDT:
            continue
        if any(sym not in maps[day] for day in window):
            continue
        c_then = close_sig.get(sym)
        if c_then is None or c_then <= 0:
            continue
        signal[sym] = c_now / c_then - 1.0
    n = len(signal)
    if n < MIN_UNIVERSE_NAMES:
        return {"regime_down": True, "btc30": btc30, "weights": {}, "n_universe": n}
    dec = max(int(n * DECILE_FRAC), MIN_DECILE_NAMES)
    chosen = sorted(signal, key=lambda s: signal[s])[:dec]
    weight = SLEEVE_GROSS / dec
    return {
        "regime_down": True,
        "btc30": btc30,
        "weights": {s: weight for s in chosen},
        "n_universe": n,
        "signal": {s: round(signal[s], 6) for s in chosen},
    }


def load_funding_day(root: Path, venue: str, day: dt.date, symbols: list[str]) -> dict[str, float] | None:
    """Real funding-rate day-sums for ``day``; None when the dataset has no partition."""
    base = root / FUNDING_DATASET[venue] / f"date={day.isoformat()}"
    if not base.exists():
        return None
    out: dict[str, float] = {}
    for sym in symbols:
        part = base / f"symbol={sym}"
        if part.exists():
            out[sym] = float(pl.read_parquet(part, columns=["funding_rate"])["funding_rate"].sum())
    return out


def mark_book(
    daily: pl.DataFrame,
    weights: dict[str, float],
    mark_date: dt.date,
    turnover: float,
    funding: dict[str, float] | None,
) -> dict[str, Any]:
    """Realized day-``mark_date`` PnL for a book of ``weights`` (long, close-to-close)."""
    close_now = _close_map(daily, mark_date)
    close_prev = _close_map(daily, mark_date - dt.timedelta(days=1))
    gross = 0.0
    missing: list[str] = []
    for sym, w in weights.items():
        c1, c0 = close_now.get(sym), close_prev.get(sym)
        if c1 is None or c0 is None or c0 <= 0:
            missing.append(sym)
            continue
        gross += w * (c1 / c0 - 1.0)
    cost = -turnover * COST_PER_SIDE_BPS / 1e4
    fund_pnl = None
    if funding is not None:
        # Longs pay positive rates, receive negative — research-sim convention.
        fund_pnl = -sum(w * funding.get(sym, 0.0) for sym, w in weights.items())
    net = gross + cost + (fund_pnl or 0.0)
    return {
        "gross_return": round(gross, 8),
        "cost_return": round(cost, 8),
        "funding_return": None if fund_pnl is None else round(fund_pnl, 8),
        "funding_missing": funding is None,
        "net_return": round(net, 8),
        "marked_names": len(weights) - len(missing),
        "missing_names": missing,
    }


def _read_ledger(ledger_path: Path) -> list[dict[str, Any]]:
    if not ledger_path.exists():
        return []
    rows = []
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _append_ledger(ledger_path: Path, row: dict[str, Any]) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def run_paper_cycle(
    root: Path,
    venue: str,
    ledger_path: Path,
    decision_date: dt.date | None = None,
) -> dict[str, Any]:
    """One daily paper cycle: mark yesterday's book, emit today's target book.

    ``decision_date`` defaults to the latest date with a klines partition (which is
    the most recent completed UTC day on a fresh root). Idempotent per decision
    date: a second run for the same date is a no-op.
    """
    if venue not in FUNDING_DATASET:
        raise ValueError(f"unknown venue {venue!r}")
    if decision_date is None:
        kl = root / "klines_1h"
        dates = sorted(p.name.split("=", 1)[1] for p in kl.iterdir() if p.name.startswith("date="))
        if not dates:
            raise FileNotFoundError(f"no klines_1h partitions under {root}")
        decision_date = dt.date.fromisoformat(dates[-1])
    start = decision_date - dt.timedelta(days=BTC_TREND_DAYS + 2)
    daily = daily_rows_from_klines(root, start, decision_date)

    rows = _read_ledger(ledger_path)
    targets = [r for r in rows if r.get("kind") == "target"]
    marked = {r.get("for_decision_date") for r in rows if r.get("kind") == "mark"}
    summary: dict[str, Any] = {"venue": venue, "decision_date": decision_date.isoformat()}

    if any(r["decision_date"] == decision_date.isoformat() for r in targets):
        summary["skipped"] = "target_exists"
        return summary

    # 1) mark the most recent unmarked prior target whose book day == decision_date
    prev = targets[-1] if targets else None
    if prev and prev["decision_date"] not in marked:
        book_date = dt.date.fromisoformat(prev["book_date"])
        if book_date <= decision_date and prev["weights"]:
            funding = load_funding_day(root, venue, book_date, list(prev["weights"]))
            mark = mark_book(daily, prev["weights"], book_date, prev["turnover"], funding)
            _append_ledger(ledger_path, {
                "kind": "mark", "strategy_id": STRATEGY_ID, "venue": venue,
                "for_decision_date": prev["decision_date"], "mark_date": book_date.isoformat(),
                **mark,
            })
            summary["mark"] = mark
        elif book_date <= decision_date:
            _append_ledger(ledger_path, {
                "kind": "mark", "strategy_id": STRATEGY_ID, "venue": venue,
                "for_decision_date": prev["decision_date"], "mark_date": book_date.isoformat(),
                "net_return": 0.0, "flat": True,
            })

    # 2) emit the new target book
    tgt = bounce_targets(daily, decision_date)
    prev_w = prev["weights"] if prev else {}
    turnover = sum(abs(tgt["weights"].get(s, 0.0) - prev_w.get(s, 0.0)) for s in set(tgt["weights"]) | set(prev_w))
    _append_ledger(ledger_path, {
        "kind": "target", "strategy_id": STRATEGY_ID, "venue": venue,
        "decision_date": decision_date.isoformat(),
        "book_date": (decision_date + dt.timedelta(days=1)).isoformat(),
        "regime_down": tgt["regime_down"], "btc30": tgt["btc30"],
        "n_universe": tgt["n_universe"], "weights": tgt["weights"],
        "turnover": round(turnover, 8),
        "profile": {
            "liq_floor_usdt": LIQ_FLOOR_USDT, "sleeve_gross": SLEEVE_GROSS,
            "signal_days": SIGNAL_DAYS, "btc_trend_days": BTC_TREND_DAYS,
            "cost_per_side_bps": COST_PER_SIDE_BPS,
        },
    })
    summary["target"] = {
        "regime_down": tgt["regime_down"], "btc30": tgt["btc30"],
        "n_names": len(tgt["weights"]), "n_universe": tgt["n_universe"],
        "turnover": round(turnover, 6),
    }
    return summary
