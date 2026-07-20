#!/usr/bin/env python3
"""1m re-simulation harness for the tail-risk program (P0.1).

Lane-1 research tooling. Re-walks recorded CONTINUOUS trades on the local
Bybit 1m surface (``~/SHARED_DATA/bybit_render_1m/klines_1m``) and must
reproduce every recorded exit exactly (the T-F standard: 0 mismatches,
exit-price rel diff <= 1e-12) before any variant is expressible.

Two resolvers, both structurally causal (single forward pass over minutes,
pre-entry bars never read, early return at the exit):

- ``resolve_hourly_parity``: aggregates minutes into the engine's 1h decision
  bars and mirrors ``trade_lifecycle.on_bar`` ordering exactly — stop
  precedence, then TP touch (exit at TP price, ts = bar end), then the
  boundary close (``max_hold`` at the first present bar end >= planned exit,
  ``data_end`` when data stops earlier). This is the exact-reproduction mode.
- ``resolve_intrabar``: first-touch at 1m granularity (the variant surface;
  June-2026 ``intrabar_engine`` semantics). When stop AND take-profit both
  touch inside the same 1m bar the path is genuinely ambiguous (1m OHLC hides
  sub-minute order): policy is **adverse-first** (stop fills, taxonomy item
  14), and every ambiguous bar is counted and reported, never hidden.

Warm-state honesty (item 15): mae/mfe and any armed state initialize at the
recorded entry; minutes before ``entry_ts_ms`` are skipped unread, and tests
prove pre-entry and post-exit perturbations cannot change a resolution.

The canonical ledgers this harness validates against:
- the T-A paired render books (``v4_shared.load_render_book``), whose symbol
  universe the 1m root was fetched for; and
- the V2 barebones CONTINUOUS ledger (``common.load_ledger``) restricted to
  the 1m root's [2023-03-26, ...) coverage — the pre-window remainder is
  enumerated, not silently skipped.

Usage (research box):
  .venv\\Scripts\\python.exe scripts/research_v3/resim_1m.py --books render_gate_on,render_gate_off,barebones
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import polars as pl

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_HOUR, exact_duration_ms  # noqa: E402
from liquidity_migration.trade_lifecycle import (  # noqa: E402
    _bar_excursion,
    _bar_exit_hits,
)
from scripts.research_v3 import common  # noqa: E402
from scripts.research_v3 import v4_shared  # noqa: E402

MS_PER_MINUTE = 60_000
MINUTES_PER_HOUR = 60
RENDER_1M_ROOT = Path.home() / "SHARED_DATA" / "bybit_render_1m" / "klines_1m"
KLINES_1H_ROOT = Path.home() / "SHARED_DATA" / "bybit_full_pit" / "klines_1h"
EXIT_PRICE_REL_TOL = 1e-12
PLANNED_HOLD_MS = exact_duration_ms(hours=24)

Minute = tuple[int, float, float, float]  # (ts_ms bar-open, high, low, close)


@dataclass(frozen=True)
class TradeSpec:
    """One recorded trade to re-walk. ``entry_ts_ms`` is the entry bar END."""

    symbol: str
    entry_ts_ms: int
    entry_price: float
    take_profit_price: float
    planned_exit_ts_ms: int
    side: str = "short"
    stop_price: float | None = None


@dataclass(frozen=True)
class Resolution:
    exit_ts_ms: int
    exit_price: float
    exit_reason: str
    mae: float
    mfe: float
    minutes_walked: int
    hours_completed: int
    incomplete_hours: int  # completed decision hours with < 60 minutes present
    missing_hours: int  # whole hours absent from the stream before the exit
    ambiguous_bars: int  # bars where stop AND TP both touched (adverse-first applied)
    boundary_gap: bool  # max_hold resolved without the exact boundary bar present
    first_touch_ts_ms: int | None


def _hour_end(ts_ms: int) -> int:
    return (int(ts_ms) // MS_PER_HOUR + 1) * MS_PER_HOUR


def resolve_hourly_parity(spec: TradeSpec, minutes: Iterable[Minute]) -> Resolution:
    """Exact-reproduction mode: 1m minutes -> engine 1h bars -> engine exits.

    Mirrors ``tf_mfe_giveback.walk_exit`` / ``trade_lifecycle.on_bar`` bar
    ordering: per completed hour update mae/mfe, stop precedence, TP touch,
    then the >=-boundary close. An hour is only evaluated once a later minute
    (or stream end) proves it complete — decisions at bar t use only <=t data.
    """
    mae = 0.0
    mfe = 0.0
    minutes_walked = 0
    hours_completed = 0
    incomplete_hours = 0
    missing_hours = 0
    ambiguous = 0
    cur_end: int | None = None
    h_hi = float("-inf")
    h_lo = float("inf")
    h_cl = float("nan")
    h_n = 0
    last_completed_end: int | None = None
    last_completed_close: float | None = None

    def eval_hour(end: int, hi: float, lo: float, cl: float, n: int) -> Resolution | None:
        nonlocal mae, mfe, hours_completed, incomplete_hours, ambiguous
        nonlocal last_completed_end, last_completed_close
        hours_completed += 1
        if n < MINUTES_PER_HOUR:
            incomplete_hours += 1
        adverse, favorable = _bar_excursion(spec.entry_price, side=spec.side, high=hi, low=lo)
        mae = min(mae, adverse)
        mfe = max(mfe, favorable)
        stop_hit, tp_hit = _bar_exit_hits(
            side=spec.side, high=hi, low=lo,
            stop_price=spec.stop_price, take_profit_price=spec.take_profit_price,
        )
        if stop_hit and tp_hit:
            ambiguous += 1
        if stop_hit:  # engine order: stop precedence before TP
            return Resolution(
                end, float(spec.stop_price), "stop_loss", mae, mfe, minutes_walked,
                hours_completed, incomplete_hours, missing_hours, ambiguous, False, end,
            )
        if tp_hit:
            return Resolution(
                end, float(spec.take_profit_price), "take_profit", mae, mfe, minutes_walked,
                hours_completed, incomplete_hours, missing_hours, ambiguous, False, end,
            )
        if end >= spec.planned_exit_ts_ms:
            return Resolution(
                end, cl, "max_hold", mae, mfe, minutes_walked,
                hours_completed, incomplete_hours, missing_hours, ambiguous,
                end != spec.planned_exit_ts_ms, None,
            )
        last_completed_end = end
        last_completed_close = cl
        return None

    last_minute_end: int | None = None
    for ts, hi, lo, cl in minutes:
        ts = int(ts)
        if ts < spec.entry_ts_ms:  # warm-state honesty: pre-entry bars unread
            continue
        end = _hour_end(ts)
        if cur_end is None:
            cur_end = end
            missing_hours += max(0, (end - (spec.entry_ts_ms + MS_PER_HOUR)) // MS_PER_HOUR)
        elif end != cur_end:
            resolved = eval_hour(cur_end, h_hi, h_lo, h_cl, h_n)
            if resolved is not None:
                return resolved
            missing_hours += max(0, (end - cur_end) // MS_PER_HOUR - 1)
            cur_end = end
            h_hi = float("-inf")
            h_lo = float("inf")
            h_n = 0
        h_hi = max(h_hi, float(hi))
        h_lo = min(h_lo, float(lo))
        h_cl = float(cl)
        h_n += 1
        minutes_walked += 1
        last_minute_end = ts + MS_PER_MINUTE

    if h_n and last_minute_end == cur_end:
        # trailing hour is complete (its final minute closes exactly on the hour)
        resolved = eval_hour(int(cur_end), h_hi, h_lo, h_cl, h_n)
        if resolved is not None:
            return resolved
    if last_completed_end is None:
        return Resolution(
            spec.entry_ts_ms, spec.entry_price, "data_end", mae, mfe, minutes_walked,
            hours_completed, incomplete_hours, missing_hours, ambiguous, False, None,
        )
    return Resolution(
        int(last_completed_end), float(last_completed_close), "data_end", mae, mfe,
        minutes_walked, hours_completed, incomplete_hours, missing_hours, ambiguous, False, None,
    )


def resolve_intrabar(spec: TradeSpec, minutes: Iterable[Minute]) -> Resolution:
    """Variant surface: first-touch exits at 1m granularity.

    Ambiguity policy (taxonomy item 14): when stop AND TP touch inside one 1m
    bar the sub-minute order is unobservable -> adverse-first (stop fills),
    with the bar counted in ``ambiguous_bars``. Touches are honored through
    the boundary hour (minutes with open < planned exit), exactly like the
    engine's boundary bar; with full coverage ``max_hold`` lands on the minute
    closing at the boundary, else ``boundary_gap`` is flagged.
    """
    mae = 0.0
    mfe = 0.0
    minutes_walked = 0
    ambiguous = 0
    last_close: float | None = None
    last_end: int | None = None
    for ts, hi, lo, cl in minutes:
        ts = int(ts)
        if ts < spec.entry_ts_ms:  # warm-state honesty
            continue
        if ts >= spec.planned_exit_ts_ms:
            break
        minutes_walked += 1
        adverse, favorable = _bar_excursion(spec.entry_price, side=spec.side, high=float(hi), low=float(lo))
        mae = min(mae, adverse)
        mfe = max(mfe, favorable)
        stop_hit, tp_hit = _bar_exit_hits(
            side=spec.side, high=float(hi), low=float(lo),
            stop_price=spec.stop_price, take_profit_price=spec.take_profit_price,
        )
        if stop_hit and tp_hit:
            ambiguous += 1
        if stop_hit:  # adverse-first on the ambiguous bar
            return Resolution(
                ts + MS_PER_MINUTE, float(spec.stop_price), "stop_loss", mae, mfe,
                minutes_walked, 0, 0, 0, ambiguous, False, ts,
            )
        if tp_hit:
            return Resolution(
                ts + MS_PER_MINUTE, float(spec.take_profit_price), "take_profit", mae, mfe,
                minutes_walked, 0, 0, 0, ambiguous, False, ts,
            )
        last_close = float(cl)
        last_end = ts + MS_PER_MINUTE
    if last_end is None:
        return Resolution(
            spec.entry_ts_ms, spec.entry_price, "data_end", mae, mfe, 0, 0, 0, 0, ambiguous, False, None,
        )
    reason = "max_hold" if last_end >= spec.planned_exit_ts_ms else "data_end"
    return Resolution(
        last_end, last_close, reason, mae, mfe, minutes_walked, 0, 0, 0, ambiguous,
        reason == "max_hold" and last_end != spec.planned_exit_ts_ms, None,
    )


# ---------------------------------------------------------------------------
# Data loading and ledger validation (research-box paths)
# ---------------------------------------------------------------------------


def minute_dates(spec: TradeSpec) -> list[str]:
    """UTC dates whose partitions cover [entry bar open, planned exit]."""
    d0 = dt.datetime.fromtimestamp((spec.entry_ts_ms - MS_PER_HOUR) / 1000, tz=dt.timezone.utc).date()
    d1 = dt.datetime.fromtimestamp(spec.planned_exit_ts_ms / 1000, tz=dt.timezone.utc).date()
    out = []
    day = d0
    while day <= d1:
        out.append(day.isoformat())
        day += dt.timedelta(days=1)
    return out


def load_symbol_minutes(symbol: str, dates: Iterable[str], root: Path = RENDER_1M_ROOT) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for day in sorted(set(dates)):
        leaf = root / f"date={day}" / f"symbol={symbol}" / "bars.parquet"
        if leaf.exists():
            frames.append(pl.read_parquet(leaf, columns=["ts_ms", "high", "low", "close"]))
    if not frames:
        return pl.DataFrame(schema={"ts_ms": pl.Int64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64})
    return pl.concat(frames).unique("ts_ms", keep="first").sort("ts_ms")


def iter_minutes(frame: pl.DataFrame) -> Iterator[Minute]:
    yield from zip(
        frame["ts_ms"].to_list(), frame["high"].to_list(),
        frame["low"].to_list(), frame["close"].to_list(),
    )


def load_symbol_hours(symbol: str, dates: Iterable[str], root: Path = KLINES_1H_ROOT) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for day in sorted(set(dates)):
        leaf = root / f"date={day}" / f"symbol={symbol}" / "part.parquet"
        if leaf.exists():
            frames.append(pl.read_parquet(leaf, columns=["ts_ms", "high", "low", "close"]))
    if not frames:
        return pl.DataFrame(schema={"ts_ms": pl.Int64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64})
    return pl.concat(frames).unique("ts_ms", keep="first").sort("ts_ms")


def classify_mismatch(
    spec: TradeSpec,
    window: pl.DataFrame,
    recorded_exit_ts_ms: int,
    hours_1h: pl.DataFrame,
) -> str:
    """Attribute a parity mismatch to the surface or to the harness.

    - ``1m_ends_before_recorded_exit``: the 1m tape stops before the recorded
      exit bar (delisting-tail class) — the exit is unreachable on this surface.
    - ``feed_divergence``: the 1m and raw-1h venue surfaces disagree on some
      decision bar of the walked window (presence, high, low, or close) —
      listing/delisting-edge class; quarantined from variant work.
    - ``harness``: both surfaces agree bar-for-bar and the walk still missed —
      a real bug; hard failure.
    """
    post_entry = window.filter(pl.col("ts_ms") >= spec.entry_ts_ms)
    if post_entry.is_empty() or int(post_entry["ts_ms"].max()) + MS_PER_MINUTE < recorded_exit_ts_ms:
        return "1m_ends_before_recorded_exit"
    agg = (
        post_entry.with_columns(
            ((pl.col("ts_ms") // MS_PER_HOUR + 1) * MS_PER_HOUR).alias("hour_end")
        )
        .group_by("hour_end")
        .agg(
            pl.col("high").max().alias("high"),
            pl.col("low").min().alias("low"),
            pl.col("close").last().alias("close"),
        )
        .sort("hour_end")
        .filter(pl.col("hour_end") <= recorded_exit_ts_ms)
    )
    raw = (
        hours_1h.with_columns((pl.col("ts_ms") + MS_PER_HOUR).alias("hour_end"))
        .filter(
            (pl.col("hour_end") > spec.entry_ts_ms) & (pl.col("hour_end") <= recorded_exit_ts_ms)
        )
        .select("hour_end", "high", "low", "close")
        .sort("hour_end")
    )
    if agg.height != raw.height or agg["hour_end"].to_list() != raw["hour_end"].to_list():
        return "feed_divergence"
    joined = agg.join(raw, on="hour_end", suffix="_1h")
    for col in ("high", "low", "close"):
        rel = (
            (joined[col] - joined[f"{col}_1h"]).abs()
            / joined[f"{col}_1h"].abs().clip(lower_bound=1e-18)
        ).max()
        if rel is not None and float(rel) > EXIT_PRICE_REL_TOL:
            return "feed_divergence"
    return "harness"


def spec_from_row(row: dict[str, Any]) -> TradeSpec:
    planned = row.get("planned_exit_ts_ms")
    if planned is None or (isinstance(planned, float) and planned != planned):
        planned = int(row["entry_ts_ms"]) + PLANNED_HOLD_MS
    return TradeSpec(
        symbol=str(row["symbol"]),
        entry_ts_ms=int(row["entry_ts_ms"]),
        entry_price=float(row["entry_price"]),
        take_profit_price=float(row["take_profit_price"]),
        planned_exit_ts_ms=int(planned),
        side=str(row.get("side", "short")),
        stop_price=None,
    )


def validate_book(
    trades: pl.DataFrame,
    *,
    book_name: str,
    root: Path = RENDER_1M_ROOT,
    window_start_ts_ms: int | None = None,
) -> dict[str, Any]:
    """Re-walk every trade of one recorded book; return the parity report.

    Hard bar (T-F standard): every in-domain trade must land on its recorded
    (exit_ts_ms, exit_reason) with exit-price rel diff <= 1e-12. Trades whose
    1m path is incomplete are bucketed and enumerated, never silently passed.
    """
    buckets: dict[str, int] = {
        "reproduced_exact": 0,
        "harness_mismatch": 0,
        "feed_divergence": 0,
        "out_of_domain_1m_ends_before_recorded_exit": 0,
        "out_of_domain_pre_window": 0,
        "out_of_domain_no_1m_data": 0,
    }
    mismatches: list[dict[str, Any]] = []
    worst_price_rel = 0.0
    worst_entry_rel = 0.0
    mae_mfe_worst = 0.0
    incomplete_path_trades = 0

    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in trades.iter_rows(named=True):
        by_symbol.setdefault(str(row["symbol"]), []).append(row)

    for symbol, rows in sorted(by_symbol.items()):
        dates: set[str] = set()
        specs = []
        for row in rows:
            spec = spec_from_row(row)
            specs.append((row, spec))
            if window_start_ts_ms is not None and spec.entry_ts_ms < window_start_ts_ms:
                continue
            dates.update(minute_dates(spec))
        frame = load_symbol_minutes(symbol, dates, root) if dates else None
        for row, spec in specs:
            if window_start_ts_ms is not None and spec.entry_ts_ms < window_start_ts_ms:
                buckets["out_of_domain_pre_window"] += 1
                continue
            if frame is None or frame.is_empty():
                buckets["out_of_domain_no_1m_data"] += 1
                continue
            window = frame.filter(
                (pl.col("ts_ms") >= spec.entry_ts_ms - MS_PER_HOUR)
                & (pl.col("ts_ms") < spec.planned_exit_ts_ms + MS_PER_HOUR)
            )
            if window.is_empty():
                buckets["out_of_domain_no_1m_data"] += 1
                continue
            entry_bar = window.filter(pl.col("ts_ms") < spec.entry_ts_ms)
            if entry_bar.height:
                entry_rel = abs(float(entry_bar["close"][-1]) - spec.entry_price) / spec.entry_price
                worst_entry_rel = max(worst_entry_rel, entry_rel)
            res = resolve_hourly_parity(spec, iter_minutes(window))
            gapped = bool(res.incomplete_hours or res.missing_hours)
            incomplete_path_trades += int(gapped)
            price_rel = abs(res.exit_price - float(row["exit_price"])) / float(row["exit_price"])
            exact = (
                res.exit_ts_ms == int(row["exit_ts_ms"])
                and res.exit_reason == str(row["exit_reason"])
                and price_rel <= EXIT_PRICE_REL_TOL
            )
            if exact:
                buckets["reproduced_exact"] += 1
                worst_price_rel = max(worst_price_rel, price_rel)
                for field in ("mae", "mfe"):
                    rec = row.get(field)
                    if rec is not None:
                        mae_mfe_worst = max(mae_mfe_worst, abs(getattr(res, field) - float(rec)))
            else:
                hours_1h = load_symbol_hours(symbol, minute_dates(spec))
                cause = classify_mismatch(spec, window, int(row["exit_ts_ms"]), hours_1h)
                if cause == "1m_ends_before_recorded_exit":
                    buckets["out_of_domain_1m_ends_before_recorded_exit"] += 1
                elif cause == "feed_divergence":
                    buckets["feed_divergence"] += 1
                else:
                    buckets["harness_mismatch"] += 1
                if len(mismatches) < 50:
                    mismatches.append(
                        {
                            "trade_id": row.get("trade_id"),
                            "symbol": symbol,
                            "cause": cause,
                            "recorded": {
                                "exit_ts_ms": int(row["exit_ts_ms"]),
                                "exit_reason": str(row["exit_reason"]),
                                "exit_price": float(row["exit_price"]),
                            },
                            "walked": {
                                "exit_ts_ms": res.exit_ts_ms,
                                "exit_reason": res.exit_reason,
                                "exit_price": res.exit_price,
                            },
                            "incomplete_hours": res.incomplete_hours,
                            "missing_hours": res.missing_hours,
                            "gapped_path": gapped,
                        }
                    )
    return {
        "book": book_name,
        "trades": int(trades.height),
        "buckets": buckets,
        "incomplete_path_trades": incomplete_path_trades,
        "worst_exit_price_rel_diff": worst_price_rel,
        "worst_entry_price_rel_diff": worst_entry_rel,
        "worst_mae_mfe_abs_diff": mae_mfe_worst,
        "mismatches_sample": mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--books", default="render_gate_on,render_gate_off,barebones")
    parser.add_argument("--out-date", default=dt.date.today().isoformat())
    parser.add_argument("--max-trades", type=int, default=None, help="debug cap per book")
    args = parser.parse_args()

    out_dir = REPO / "reports" / "tail-risk-program" / f"p01-resim-1m-{args.out_date}"
    out_dir.mkdir(parents=True, exist_ok=True)

    first_1m_day = min(p.name.split("=", 1)[1] for p in RENDER_1M_ROOT.iterdir() if p.name.startswith("date="))
    window_start_ts = int(
        dt.datetime.fromisoformat(first_1m_day).replace(tzinfo=dt.timezone.utc).timestamp() * 1000
    ) + MS_PER_HOUR  # entry bar needs the prior hour of minutes

    inputs: dict[str, Any] = {"render_1m_root": str(RENDER_1M_ROOT), "first_1m_day": first_1m_day}
    reports = []
    hard_failures = 0
    for book_name in [b.strip() for b in args.books.split(",") if b.strip()]:
        if book_name == "barebones":
            inputs["v2"] = common.verify_v2_inputs()
            trades = common.load_ledger("continuous")
            window_start = window_start_ts
        elif book_name in ("render_gate_on", "render_gate_off"):
            arm = book_name.removeprefix("render_")
            trades = v4_shared.load_render_book(arm)
            window_start = window_start_ts
        else:
            raise SystemExit(f"unknown book: {book_name}")
        if args.max_trades:
            trades = trades.head(args.max_trades)
        report = validate_book(trades, book_name=book_name, window_start_ts_ms=window_start)
        reports.append(report)
        hard_failures += report["buckets"]["harness_mismatch"]
        print(json.dumps({k: v for k, v in report.items() if k != "mismatches_sample"}), flush=True)

    receipt_path = out_dir / "parity_report.json"
    receipt_path.write_text(json.dumps({"reports": reports}, indent=1), encoding="utf-8")
    common.write_manifest(
        out_dir,
        kind="tail_risk_p01_resim_1m_parity",
        inputs=inputs,
        params={
            "resolver": "resolve_hourly_parity (engine 1h decision bars from 1m aggregation)",
            "hard_bar": "exit_ts_ms + exit_reason equal, exit_price rel diff <= 1e-12",
            "ambiguity_policy": "adverse-first on both-touch bars, counted and reported (item 14)",
            "warm_state": "mae/mfe and armed state initialize at entry; pre-entry bars unread (item 15)",
            "books": args.books,
        },
        output_files={"parity_report.json": receipt_path},
        extra={"hard_failures": hard_failures},
    )
    print(f"hard_failures={hard_failures}", flush=True)
    return 1 if hard_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
