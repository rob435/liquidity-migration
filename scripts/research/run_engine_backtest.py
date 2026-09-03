#!/usr/bin/env python3
"""Run `engine backtest` and read its outputs back as a research report.

Purpose: one command from a recorded tape to citable numbers, with every
figure traceable to a file the engine wrote.

| Input / output | Written by | Read here as |
| --- | --- | --- |
| `--report` JSON | the engine (`BacktestReport`) | venue books, engine ledger, reconciliation — the authority |
| `--trades` JSONL | the engine (`ClosedTrade` rows) | per-trip gross, fees, net, holding time, maker share, shortfall |
| `--equity` JSONL | the simulated venue (`EquityPoint` rows) | the equity path for drawdown |

Invariants:
- Money is summed as the engine wrote it. `fees_usdt: null` is an unknown
  fee and stays unknown; it is counted, never coerced to zero.
- Returns are arithmetic on the initial capital. Annualisation is by the
  tape's calendar span, never by trade count, and only for spans of a day
  or more.
- A Sharpe ratio is computed only from daily equity changes and only with
  at least seven such days; otherwise it is `null` with the reason.
- Drawdown is peak-to-trough on the equity series, which includes open
  positions at every fill and settlement, not on closed trips alone.

Recipe:
    python -m market_tape rows ARCHIVE --hours 2026-09-02T00..2026-09-03T00 > tape.jsonl
    python scripts/research/run_engine_backtest.py \
        --config engine/engine.demo.toml --tape tape.jsonl \
        --instruments ARCHIVE/2026-09-02/00/_meta/instruments-*.json.zst \
        --out-dir var/backtests/2026-09-02
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENGINE_BIN = REPO_ROOT / "engine" / "target" / "release" / "engine"
MS_PER_DAY = 86_400_000
MIN_SHARPE_DAYS = 7


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engine-bin", type=Path, default=DEFAULT_ENGINE_BIN)
    parser.add_argument("--config", type=Path, required=True, help="engine TOML with [[strategy]] blocks")
    parser.add_argument("--tape", type=Path, required=True, help="market_tape rows, .jsonl or .jsonl.zst")
    parser.add_argument("--instruments", type=Path, required=True, help="_meta/instruments-*.json[.zst]")
    parser.add_argument("--signals", type=Path, default=None, help="signal spool directory to replay")
    parser.add_argument("--out-dir", type=Path, required=True, help="where the run's files go; must not hold a log already")
    parser.add_argument("--capital", type=float, default=10_000.0)
    parser.add_argument("--taker-fee", type=float, default=0.00055)
    parser.add_argument("--maker-fee", type=float, default=0.0002)
    parser.add_argument("--rtt-ms", type=int, default=175)
    parser.add_argument("--private-latency-ms", type=int, default=60)
    parser.add_argument("--mmr", type=float, default=0.005)
    parser.add_argument(
        "--durable-log",
        action="store_true",
        help="fsync the run's log at every barrier as the live engine does; off, a rerun rewrites the same bytes",
    )
    return parser.parse_args(argv)


def run_engine(args: argparse.Namespace) -> Path:
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    report = out / "report.json"
    cmd = [
        str(args.engine_bin),
        "backtest",
        "--config",
        str(args.config),
        "--tape",
        str(args.tape),
        "--instruments",
        str(args.instruments),
        "--wal",
        str(out / "backtest.wal"),
        "--trades",
        str(out / "trades.jsonl"),
        "--equity",
        str(out / "equity.jsonl"),
        "--report",
        str(report),
        "--capital",
        str(args.capital),
        "--taker-fee",
        str(args.taker_fee),
        "--maker-fee",
        str(args.maker_fee),
        "--rtt-ms",
        str(args.rtt_ms),
        "--private-latency-ms",
        str(args.private_latency_ms),
        "--mmr",
        str(args.mmr),
    ]
    if args.signals is not None:
        cmd.extend(["--signals", str(args.signals)])
    if args.durable_log:
        cmd.append("--durable-log")
    print("$ " + " ".join(cmd), file=sys.stderr)
    # The engine's tracing goes to stdout beside its report table; keep the
    # console to warnings unless the caller asked for more.
    env = dict(os.environ)
    env.setdefault("RUST_LOG", "warn")
    completed = subprocess.run(cmd, check=False, env=env)
    if completed.returncode != 0:
        raise SystemExit(f"engine backtest exited with {completed.returncode}")
    return report


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open() as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise SystemExit(f"{path}:{number}: not JSON: {error}") from error
    return rows


def trip_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    priced = [t for t in trades if t.get("round_trip")]
    nets = [float(t["round_trip"]["net_usdt"]) for t in priced]
    gross = sum(float(t["round_trip"]["gross_usdt"]) for t in priced)
    fees_known = [t["fees_usdt"] for t in trades if t.get("fees_usdt") is not None]
    held_ms = [int(t["round_trip"]["held_ms"]) for t in priced]
    maker = [(float(t["maker_share"]), float(t["round_trip"]["entry_notional_usdt"])) for t in priced if t.get("maker_share") is not None]
    maker_notional = sum(n for _, n in maker)
    shortfall = [float(t["arrival_shortfall_bps"]) for t in trades if t.get("arrival_shortfall_bps") is not None]
    return {
        "closed_trips": len(trades),
        "priced_trips": len(priced),
        "unpriced_trips": len(trades) - len(priced),
        "wins": sum(1 for n in nets if n > 0),
        "win_rate": (sum(1 for n in nets if n > 0) / len(nets)) if nets else None,
        "gross_usdt": gross,
        "fees_usdt": sum(float(f) for f in fees_known),
        "trips_with_unknown_fee": len(trades) - len(fees_known),
        "net_usdt": sum(nets),
        "mean_net_usdt": statistics.fmean(nets) if nets else None,
        "median_held_s": (statistics.median(held_ms) / 1000.0) if held_ms else None,
        "maker_share_notional_weighted": (sum(s * n for s, n in maker) / maker_notional) if maker_notional > 0 else None,
        "mean_arrival_shortfall_bps": statistics.fmean(shortfall) if shortfall else None,
    }


def equity_metrics(points: list[dict[str, Any]], initial: float, final_equity: float) -> dict[str, Any]:
    series = [(int(p["wall_ms"]), float(p["equity_usdt"])) for p in points]
    series.sort()
    peak = initial
    max_dd = 0.0
    for _, equity in series + [(0, final_equity)]:
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    daily: dict[int, float] = {}
    for wall_ms, equity in series:
        daily[wall_ms // MS_PER_DAY] = equity
    closes = [daily[d] for d in sorted(daily)]
    sharpe: float | None = None
    sharpe_note = None
    if len(closes) >= MIN_SHARPE_DAYS + 1:
        returns = [(b - a) / a for a, b in zip(closes, closes[1:]) if a > 0]
        sd = statistics.pstdev(returns)
        sharpe = (statistics.fmean(returns) / sd) * math.sqrt(365.0) if sd > 0 else None
        if sharpe is None:
            sharpe_note = "zero variance of daily returns"
    else:
        sharpe_note = f"needs {MIN_SHARPE_DAYS} daily equity closes, have {max(len(closes) - 1, 0)}"
    return {
        "equity_points": len(series),
        "max_drawdown_frac": max_dd,
        "daily_closes": len(closes),
        "sharpe_daily_annualised": sharpe,
        "sharpe_note": sharpe_note,
    }


def build_metrics(report: dict[str, Any], trades: list[dict[str, Any]], equity: list[dict[str, Any]], capital: float) -> dict[str, Any]:
    venue = report["venue"]
    span_ms = int(report["end_wall_ms"]) - int(report["start_wall_ms"])
    span_days = span_ms / MS_PER_DAY
    net_change = float(venue["equity_usdt"]) - float(venue["initial_cash_usdt"])
    return_frac = net_change / capital if capital > 0 else None
    return {
        "span_days": span_days,
        "market_events": report["market_events"],
        "orders_sent": report["orders_sent"],
        "reconciliation": report["reconciliation"],
        "venue": venue,
        "engine_ledger": report["engine"],
        "trips": trip_metrics(trades),
        "equity": equity_metrics(equity, float(venue["initial_cash_usdt"]), float(venue["equity_usdt"])),
        "return_on_capital_frac": return_frac,
        # Annualising a span shorter than a day multiplies noise by hundreds;
        # the number is withheld rather than printed.
        "annualised_return_frac": (return_frac * 365.0 / span_days) if return_frac is not None and span_days >= 1.0 else None,
        "tape": report["tape"],
    }


def fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def print_summary(m: dict[str, Any]) -> None:
    t, e, v, r = m["trips"], m["equity"], m["venue"], m["reconciliation"]
    lines = [
        f"span {fmt(m['span_days'], 3)} d; events {m['market_events']}; orders {m['orders_sent']}",
        f"equity {fmt(v['initial_cash_usdt'], 2)} -> {fmt(v['equity_usdt'], 2)} USDT; return {fmt(m['return_on_capital_frac'])}; annualised {fmt(m['annualised_return_frac'])}",
        f"fills {v['fills']} (maker {v['maker_fills']}, stop {v['stop_fills']}, liquidation {v['liquidation_fills']}); rejected {v['rejected_orders']}; funding {fmt(v['funding_paid_usdt'], 4)} over {v['funding_settlements']}",
        f"trips {t['closed_trips']} ({t['priced_trips']} priced, {t['wins']} won, win rate {fmt(t['win_rate'])}); gross {fmt(t['gross_usdt'], 2)}; fees {fmt(t['fees_usdt'], 2)} ({t['trips_with_unknown_fee']} unknown); net {fmt(t['net_usdt'], 2)}",
        f"maker share {fmt(t['maker_share_notional_weighted'])}; arrival shortfall {fmt(t['mean_arrival_shortfall_bps'], 2)} bp; median held {fmt(t['median_held_s'], 1)} s",
        f"max drawdown {fmt(e['max_drawdown_frac'])}; sharpe {fmt(e['sharpe_daily_annualised'], 2)}" + (f" ({e['sharpe_note']})" if e["sharpe_note"] else ""),
        f"reconciliation: engine {fmt(r['engine_closed_net_usdt'], 6)} vs venue {fmt(r['venue_closed_net_usdt'], 6)}; agrees {fmt(r['agrees'])}",
    ]
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report_path = run_engine(args)
    report = json.loads(report_path.read_text())
    trades = read_jsonl(args.out_dir / "trades.jsonl")
    equity = read_jsonl(args.out_dir / "equity.jsonl")
    metrics = build_metrics(report, trades, equity, args.capital)
    print_summary(metrics)
    (args.out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    print(f"metrics written to {args.out_dir / 'metrics.json'}", file=sys.stderr)
    if report["reconciliation"].get("agrees") is False:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
