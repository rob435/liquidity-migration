#!/usr/bin/env python3
"""Causal continuous-rmom grid vs the deployed daily short baseline.

This is the execution harness for:
docs/preregistration/2026-06-05-continuous-causal-rmom-vs-daily.md

It deliberately reuses the production research engines and writes auditable
artifacts:
  - daily baseline report per venue
  - continuous per-cell short-only / D9 leg / D0 leg / L-S ledgers and equities
  - per-venue summary.csv
  - cross-venue pooled_summary.csv
  - verdict.json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration import promoted  # noqa: E402
from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR  # noqa: E402
from liquidity_migration.config import load_config  # noqa: E402
from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    _additive_equity,
    _btc_trend_returns,
    _daily_pnl_metrics,
    _fresh_entries,
    _market_daily_returns,
    _portfolio_mtm_equity,
    _run_trades,
    _split_metrics,
    build_continuous_panel,
)
from liquidity_migration.signal_harness import (  # noqa: E402
    _autodetect_dataset_names,
    _date_str_to_ms,
    _read_window,
)
from liquidity_migration.trade_lifecycle import _empty_trades, _funding_lookup  # noqa: E402
from liquidity_migration.volume_events import (  # noqa: E402
    _indexed_price_bars_by_symbol,
    run_volume_event_research,
)


ROOTS = {
    "bybit": Path.home() / "SHARED_DATA" / "bybit_full_pit",
    "binance": Path.home() / "SHARED_DATA" / "binance_full_pit",
}


def _finite(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _metric(metrics: dict[str, Any], *path: str) -> Any:
    cur: Any = metrics
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _day_return_series(equity: pl.DataFrame) -> dict[int, float]:
    if equity.is_empty() or "basket_return" not in equity.columns:
        return {}
    return {
        (int(ts) // MS_PER_DAY) * MS_PER_DAY: float(ret)
        for ts, ret in equity.select("ts_ms", "basket_return").iter_rows()
    }


def _corr(a: dict[int, float], b: dict[int, float]) -> float | None:
    days = sorted(set(a) | set(b))
    if len(days) < 3:
        return None
    xs = [a.get(d, 0.0) for d in days]
    ys = [b.get(d, 0.0) for d in days]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0.0 or vy <= 0.0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / math.sqrt(vx * vy)


def _parse_failed_fade_setups(raw: str) -> list[tuple[int, float, float]]:
    setups: list[tuple[int, float, float]] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if item.lower() in {"off", "none", "0"}:
            setups.append((0, 0.0, 0.0))
            continue
        pieces = item.split(":")
        if len(pieces) != 3:
            raise ValueError(f"failed-fade setup must be off or hours:loss_pct:min_mfe_pct; got {item!r}")
        setups.append((int(pieces[0]), float(pieces[1]), float(pieces[2])))
    return setups or [(0, 0.0, 0.0)]


def _payload_from_trades(
    *,
    config: ContinuousEventConfig,
    trades: pl.DataFrame,
    klines: pl.DataFrame,
    n_fresh_entries: int,
    skips: dict[str, int],
    construction: str,
) -> tuple[dict[str, Any], pl.DataFrame, pl.DataFrame]:
    equity = _additive_equity(trades)
    mtm_equity = _portfolio_mtm_equity(trades, klines)
    metrics = _split_metrics(trades, config)
    payload = {
        "construction": construction,
        "config": asdict(config),
        "config_hash": config.config_hash(),
        "run_label": "exploratory",
        "n_fresh_entries": int(n_fresh_entries),
        "n_trades": int(trades.height),
        "skips": skips,
        "metrics": metrics,
        "metrics_mtm": _daily_pnl_metrics(mtm_equity),
    }
    return payload, equity, mtm_equity


def _write_payload(
    out_dir: Path,
    *,
    payload: dict[str, Any],
    trades: pl.DataFrame,
    equity: pl.DataFrame,
    mtm_equity: pl.DataFrame,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if not trades.is_empty():
        trades.write_csv(out_dir / "trades.csv")
    if not equity.is_empty():
        equity.write_csv(out_dir / "equity.csv")
    if not mtm_equity.is_empty():
        mtm_equity.write_csv(out_dir / "mtm_equity.csv")
    (out_dir / "report.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


class VenueContext:
    def __init__(self, root: Path, *, start: str, end: str, max_pad_hours: int) -> None:
        self.root = root
        self.start = start
        self.end = end
        self.start_ms = _date_str_to_ms(start)
        self.end_ms = _date_str_to_ms(end)
        names = _autodetect_dataset_names(root)
        kname = names["klines_dataset"]
        self.klines = _read_window(
            root,
            kname,
            start_ms=self.start_ms,
            end_ms=self.end_ms + max_pad_hours * MS_PER_HOUR,
            columns=["ts_ms", "symbol", "open", "high", "low", "close"],
        )
        self.symbol_bars = _indexed_price_bars_by_symbol(self.klines) if not self.klines.is_empty() else {}
        funding = _read_window(
            root,
            names["funding_dataset"],
            start_ms=self.start_ms - 10 * MS_PER_DAY,
            end_ms=self.end_ms + max_pad_hours * MS_PER_HOUR,
        )
        self.funding_lookup = _funding_lookup(funding)
        self.market_daily = _market_daily_returns(self.klines) if not self.klines.is_empty() else None
        self.btc_trend_daily = _btc_trend_returns(self.klines) if not self.klines.is_empty() else None
        self.panels: dict[tuple[float, tuple[str, ...]], pl.DataFrame] = {}

    def panel(self, cfg: ContinuousEventConfig) -> pl.DataFrame:
        key = (round(float(cfg.rmom_quantile), 6), tuple(cfg.feature_set))
        if key not in self.panels:
            self.panels[key] = build_continuous_panel(self.root, cfg)
        return self.panels[key]


def _run_continuous(
    ctx: VenueContext,
    cfg: ContinuousEventConfig,
    *,
    construction: str,
) -> tuple[dict[str, Any], pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    panel = ctx.panel(cfg)
    entries = _fresh_entries(panel, cfg) if not panel.is_empty() else panel
    if not entries.is_empty() and ctx.symbol_bars:
        trades, skips = _run_trades(
            entries,
            ctx.symbol_bars,
            ctx.funding_lookup,
            cfg,
            ctx.market_daily,
            ctx.btc_trend_daily,
        )
    else:
        trades, skips = _empty_trades(), {}
    payload, equity, mtm_equity = _payload_from_trades(
        config=cfg,
        trades=trades,
        klines=ctx.klines,
        n_fresh_entries=int(entries.height) if not entries.is_empty() else 0,
        skips=skips,
        construction=construction,
    )
    return payload, trades, equity, mtm_equity


def _run_daily_baseline(root: Path, venue_out: Path, *, start: str, end: str) -> tuple[dict[str, Any], dict[int, float]]:
    costs = load_config("configs/volume_alpha.default.yaml").costs
    cfg = promoted.short_profile(start=start, end=end)
    out = venue_out / "daily_short_baseline"
    payload = run_volume_event_research(root, event_config=cfg, cost_config=costs, report_dir=out)
    eq_path = out / "volume_event_best_equity.csv"
    if eq_path.exists():
        equity = pl.read_csv(eq_path)
        daily_returns = _day_return_series(equity)
        equity_metrics = _daily_pnl_metrics(equity)
        best = payload.setdefault("best_scenario", {})
        for key in ("annualized_return", "mar", "sharpe_like", "worst_day_return"):
            if best.get(key) is None:
                best[key] = equity_metrics.get(key)
    else:
        daily_returns = {}
    return payload, daily_returns


def _cell_id(
    q: float,
    liq: float,
    hold: int,
    exit_mode: str,
    impact: float,
    capital: float,
    feature_set: tuple[str, ...],
    btc_trend_gate: str,
    entry_event_trigger: str,
    entry_pause_after_adverse_exits: int = 0,
    entry_pause_window_hours: int = 24,
    round_trip_cost_multiplier: float = 1.0,
    gross_exposure: float = 0.5,
    max_active: int = 25,
    sizing_mode: str = "flat",
    age_days_min: int = 0,
    failed_fade_hours: int = 0,
    failed_fade_loss_pct: float = 0.0,
    failed_fade_min_mfe_pct: float = 0.0,
) -> str:
    feat = "-".join(feature_set) or "none"
    cell = (
        f"q{int(round(q * 100)):02d}_liq{int(liq / 1000)}k_feat{feat}_btc{btc_trend_gate}_h{hold}_"
        f"{exit_mode}_evt{entry_event_trigger}_imp{int(impact)}_cap{int(capital / 1_000_000)}m"
    )
    if abs(round_trip_cost_multiplier - 1.0) > 1e-12:
        cell += f"_cost{int(round(round_trip_cost_multiplier * 100))}"
    if abs(gross_exposure - 0.5) > 1e-12:
        cell += f"_g{int(round(gross_exposure * 100))}"
    if max_active != 25:
        cell += f"_ma{max_active}"
    if sizing_mode != "flat":
        cell += f"_sz{sizing_mode}"
    if age_days_min > 0:
        cell += f"_age{age_days_min}"
    if failed_fade_hours > 0:
        cell += (
            f"_ff{failed_fade_hours}"
            f"l{int(round(failed_fade_loss_pct * 1000))}"
            f"m{int(round(failed_fade_min_mfe_pct * 1000))}"
        )
    if entry_pause_after_adverse_exits > 0:
        cell += f"_cb{entry_pause_after_adverse_exits}w{entry_pause_window_hours}"
    return cell


def _row(
    *,
    venue: str,
    cell_id: str,
    params: dict[str, Any],
    daily: dict[str, Any],
    daily_returns: dict[int, float],
    short_full: dict[str, Any],
    short_mtm_equity: pl.DataFrame,
    ls: dict[str, Any],
    ls_mtm_equity: pl.DataFrame,
    short_leg: dict[str, Any],
    long_leg: dict[str, Any],
) -> dict[str, Any]:
    daily_best = daily.get("best_scenario", {})
    daily_return = _finite(daily_best.get("total_return"))
    daily_mar = _finite(daily_best.get("mar"))
    short_return = _finite(_metric(short_full, "metrics_mtm", "total_return"))
    short_mar = _finite(_metric(short_full, "metrics_mtm", "mar"))
    ls_mar = _finite(ls.get("metrics_mtm", {}).get("mar"))
    short_returns = _day_return_series(short_mtm_equity)
    ls_returns = _day_return_series(ls_mtm_equity)
    return {
        "venue": venue,
        "cell_id": cell_id,
        **params,
        "daily_run_label": daily.get("run_label"),
        "daily_trades": daily_best.get("trades"),
        "daily_total_return": daily_return,
        "daily_mar": daily_mar,
        "daily_max_drawdown": daily_best.get("max_drawdown"),
        "daily_sharpe": daily_best.get("sharpe_like"),
        "short_only_trades": short_full.get("n_trades"),
        "short_only_mtm_total_return": short_return,
        "short_only_mtm_mar": short_mar,
        "short_only_mtm_max_drawdown": _metric(short_full, "metrics_mtm", "max_drawdown"),
        "short_only_delta_return_vs_daily": (
            short_return - daily_return if short_return is not None and daily_return is not None else None
        ),
        "short_only_delta_mar_vs_daily": (
            short_mar - daily_mar if short_mar is not None and daily_mar is not None else None
        ),
        "short_only_corr_daily": _corr(daily_returns, short_returns),
        "short_leg_trades": short_leg.get("n_trades"),
        "long_leg_trades": long_leg.get("n_trades"),
        "ls_trades": ls.get("n_trades"),
        "ls_mtm_total_return": _metric(ls, "metrics_mtm", "total_return"),
        "ls_mtm_mar": ls_mar,
        "ls_mtm_max_drawdown": _metric(ls, "metrics_mtm", "max_drawdown"),
        "ls_mtm_sharpe": _metric(ls, "metrics_mtm", "sharpe_like"),
        "ls_worst_day": _metric(ls, "metrics_mtm", "worst_day_return"),
        "ls_realized_early_return": _metric(ls, "metrics", "early", "total_return"),
        "ls_realized_recent_return": _metric(ls, "metrics", "recent", "total_return"),
        "ls_delta_mar_vs_daily": (ls_mar - daily_mar) if ls_mar is not None and daily_mar is not None else None,
        "ls_corr_daily": _corr(daily_returns, ls_returns),
    }


def _pooled(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_cell: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_cell.setdefault(str(row["cell_id"]), []).append(row)
    out: list[dict[str, Any]] = []
    for cell_id, parts in by_cell.items():
        if len(parts) < 2:
            continue
        by_venue = {str(p["venue"]): p for p in parts}
        vals = [p.get("ls_delta_mar_vs_daily") for p in parts if p.get("ls_delta_mar_vs_daily") is not None]
        pooled_delta = sum(float(v) for v in vals) / len(vals) if vals else None
        short_return_deltas = [
            p.get("short_only_delta_return_vs_daily")
            for p in parts
            if p.get("short_only_delta_return_vs_daily") is not None
        ]
        short_mar_deltas = [
            p.get("short_only_delta_mar_vs_daily") for p in parts if p.get("short_only_delta_mar_vs_daily") is not None
        ]
        pooled_short_return_delta = (
            sum(float(v) for v in short_return_deltas) / len(short_return_deltas) if short_return_deltas else None
        )
        pooled_short_mar_delta = sum(float(v) for v in short_mar_deltas) / len(short_mar_deltas) if short_mar_deltas else None
        ls_returns = [p.get("ls_mtm_total_return") for p in parts]
        positive_both = all(_finite(v) is not None and float(v) > 0.0 for v in ls_returns)
        short_beats_daily_both = (
            len(short_return_deltas) == len(parts)
            and len(short_mar_deltas) == len(parts)
            and all(float(v) > 0.0 for v in short_return_deltas)
            and all(float(v) > 0.0 for v in short_mar_deltas)
        )
        early_recent = all(
            _finite(p.get(k)) is not None and float(p[k]) > 0.0
            for p in parts
            for k in ("ls_realized_early_return", "ls_realized_recent_return")
        )
        corr_ok = all(_finite(p.get("ls_corr_daily")) is not None and abs(float(p["ls_corr_daily"])) < 0.45 for p in parts)
        deltas = [float(p["ls_delta_mar_vs_daily"]) for p in parts if p.get("ls_delta_mar_vs_daily") is not None]
        beats_rule = (
            positive_both
            and early_recent
            and corr_ok
            and len(deltas) == len(parts)
            and (all(d > 0.0 for d in deltas) or (pooled_delta is not None and pooled_delta > 0.0 and min(deltas) >= -0.5))
        )
        base = dict(parts[0])
        for drop in list(base):
            if drop.startswith("daily_") or drop.startswith("short_") or drop.startswith("long_") or drop.startswith("ls_"):
                base.pop(drop, None)
        out.append(
            {
                **base,
                "short_only_beats_daily_return_and_mar": short_beats_daily_both,
                "pooled_short_only_delta_return_vs_daily": pooled_short_return_delta,
                "pooled_short_only_delta_mar_vs_daily": pooled_short_mar_delta,
                "pooled_delta_mar_vs_daily": pooled_delta,
                "beats_rule_pre_stress": beats_rule,
                "bybit_short_only_return": by_venue.get("bybit", {}).get("short_only_mtm_total_return"),
                "binance_short_only_return": by_venue.get("binance", {}).get("short_only_mtm_total_return"),
                "bybit_short_only_mar": by_venue.get("bybit", {}).get("short_only_mtm_mar"),
                "binance_short_only_mar": by_venue.get("binance", {}).get("short_only_mtm_mar"),
                "bybit_short_only_delta_return": by_venue.get("bybit", {}).get("short_only_delta_return_vs_daily"),
                "binance_short_only_delta_return": by_venue.get("binance", {}).get(
                    "short_only_delta_return_vs_daily"
                ),
                "bybit_short_only_delta_mar": by_venue.get("bybit", {}).get("short_only_delta_mar_vs_daily"),
                "binance_short_only_delta_mar": by_venue.get("binance", {}).get("short_only_delta_mar_vs_daily"),
                "bybit_ls_mar": by_venue.get("bybit", {}).get("ls_mtm_mar"),
                "binance_ls_mar": by_venue.get("binance", {}).get("ls_mtm_mar"),
                "bybit_daily_mar": by_venue.get("bybit", {}).get("daily_mar"),
                "binance_daily_mar": by_venue.get("binance", {}).get("daily_mar"),
                "bybit_delta_mar": by_venue.get("bybit", {}).get("ls_delta_mar_vs_daily"),
                "binance_delta_mar": by_venue.get("binance", {}).get("ls_delta_mar_vs_daily"),
                "bybit_corr_daily": by_venue.get("bybit", {}).get("ls_corr_daily"),
                "binance_corr_daily": by_venue.get("binance", {}).get("ls_corr_daily"),
                "bybit_ls_return": by_venue.get("bybit", {}).get("ls_mtm_total_return"),
                "binance_ls_return": by_venue.get("binance", {}).get("ls_mtm_total_return"),
            }
        )
    return sorted(
        out,
        key=lambda r: (
            bool(r.get("short_only_beats_daily_return_and_mar")),
            bool(r.get("beats_rule_pre_stress")),
            _finite(r.get("pooled_short_only_delta_mar_vs_daily")) or -9999.0,
            _finite(r.get("pooled_short_only_delta_return_vs_daily")) or -9999.0,
            _finite(r.get("pooled_delta_mar_vs_daily")) or -9999.0,
        ),
        reverse=True,
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2023-04-01")
    p.add_argument("--end", default="2026-05-28")
    p.add_argument("--out", default=str(Path.home() / "SHARED_DATA" / "cont_causal_rmom_2026-06-05"))
    p.add_argument("--rmom-quantiles", default="0.25,0.33,0.50")
    p.add_argument("--feature-set", default=",".join(ContinuousEventConfig().feature_set))
    p.add_argument(
        "--feature-sets",
        default=None,
        help=(
            "Optional semicolon-separated feature-set grid. Each set is comma-separated, "
            "for example 'max_ret168;rv_168h,max_ret168'. Overrides --feature-set."
        ),
    )
    p.add_argument("--btc-trend-gates", default="off")
    p.add_argument("--entry-event-triggers", default="none")
    p.add_argument("--entry-pause-after-adverse-exits", default="0",
                   help="Comma-separated adverse-exit pause thresholds; 0 disables.")
    p.add_argument("--entry-pause-window-hours", default="24",
                   help="Comma-separated trailing windows for adverse-exit pause.")
    p.add_argument("--liq-turnover-mins", default="500000,1000000")
    p.add_argument("--hold-hours", default="6,12,24")
    p.add_argument("--exit-modes", default="fixed,state")
    p.add_argument("--impact-coef-bps", type=float, default=50.0)
    p.add_argument("--round-trip-cost-multiplier", type=float, default=1.0)
    p.add_argument("--deploy-capital-usd", type=float, default=1_000_000.0)
    p.add_argument("--continuous-gross", type=float, default=0.5)
    p.add_argument("--ls-leg-gross", type=float, default=0.25)
    p.add_argument("--max-active", type=int, default=25)
    p.add_argument("--sizing-mode", default="flat", help="Continuous sizing mode: flat or inverse_vol.")
    p.add_argument("--age-days-min", type=int, default=0, help="Minimum symbol age in days.")
    p.add_argument("--stop-loss-pct", type=float, default=0.0)
    p.add_argument("--breakeven-arm-pct", type=float, default=0.0)
    p.add_argument("--mfe-giveback-trigger-pct", type=float, default=0.0)
    p.add_argument("--mfe-giveback-retain-pct", type=float, default=0.0)
    p.add_argument("--entry-decel-lookback-h", type=int, default=0)
    p.add_argument("--entry-decel-max-ret", type=float, default=0.0)
    p.add_argument("--market-min-ret-1d", type=float, default=-1.0)
    p.add_argument(
        "--failed-fade-setup",
        default="off",
        help="Failed-fade setup: off or hours:loss_pct:min_mfe_pct, e.g. 6:0.04:0.01.",
    )
    p.add_argument("--venues", default="bybit,binance")
    p.add_argument("--skip-daily", action="store_true")
    p.add_argument("--pre-registration", default="docs/preregistration/2026-06-05-continuous-causal-rmom-vs-daily.md")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    out_root = Path(args.out).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    qs = [float(x) for x in args.rmom_quantiles.split(",") if x.strip()]
    if args.feature_sets:
        feature_sets = [
            tuple(x.strip() for x in part.split(",") if x.strip())
            for part in str(args.feature_sets).split(";")
            if part.strip()
        ]
    else:
        feature_sets = [tuple(x.strip() for x in args.feature_set.split(",") if x.strip())]
    btc_trend_gates = [x.strip() for x in args.btc_trend_gates.split(",") if x.strip()]
    entry_event_triggers = [x.strip() for x in args.entry_event_triggers.split(",") if x.strip()]
    pause_thresholds = [int(x) for x in args.entry_pause_after_adverse_exits.split(",") if x.strip()]
    pause_windows = [int(x) for x in args.entry_pause_window_hours.split(",") if x.strip()]
    liqs = [float(x) for x in args.liq_turnover_mins.split(",") if x.strip()]
    holds = [int(x) for x in args.hold_hours.split(",") if x.strip()]
    exit_modes = [x.strip() for x in args.exit_modes.split(",") if x.strip()]
    failed_fade_hours, failed_fade_loss_pct, failed_fade_min_mfe_pct = _parse_failed_fade_setups(
        args.failed_fade_setup
    )[0]

    all_rows: list[dict[str, Any]] = []
    daily_payloads: dict[str, dict[str, Any]] = {}
    daily_returns: dict[str, dict[int, float]] = {}
    max_pad = max(max(holds), 48) + 8

    for venue in venues:
        if venue not in ROOTS:
            raise SystemExit(f"unknown venue {venue}; valid: {', '.join(ROOTS)}")
        root = ROOTS[venue]
        venue_out = out_root / venue
        venue_out.mkdir(parents=True, exist_ok=True)
        print(f"[{venue}] root={root}", flush=True)

        if args.skip_daily:
            daily_payloads[venue] = {"best_scenario": {}, "run_label": "skipped"}
            daily_returns[venue] = {}
        else:
            print(f"[{venue}] running deployed daily short baseline...", flush=True)
            daily_payloads[venue], daily_returns[venue] = _run_daily_baseline(
                root, venue_out, start=args.start, end=args.end
            )

        print(f"[{venue}] loading continuous context...", flush=True)
        ctx = VenueContext(root, start=args.start, end=args.end, max_pad_hours=max_pad)

        for q in qs:
            for feature_set in feature_sets:
                # Build/cache the panel once per q/feature set before the inner loop, so slow failures surface early.
                probe = ContinuousEventConfig(
                    start_date=args.start,
                    end_date=args.end,
                    rmom_quantile=q,
                    feature_set=feature_set,
                )
                panel_rows = ctx.panel(probe).height
                print(
                    f"[{venue}] rmom q={q:.2f} features={','.join(feature_set)} panel rows={panel_rows:,}",
                    flush=True,
                )
                for liq in liqs:
                    for hold in holds:
                        for exit_mode in exit_modes:
                            for btc_trend_gate in btc_trend_gates:
                                for entry_event_trigger in entry_event_triggers:
                                    for pause_threshold in pause_thresholds:
                                        for pause_window in pause_windows:
                                            params = {
                                                "rmom_quantile": q,
                                                "liq_turnover_min": liq,
                                                "feature_set": ",".join(feature_set),
                                                "btc_trend_gate": btc_trend_gate,
                                                "entry_event_trigger": entry_event_trigger,
                                                "entry_pause_after_adverse_exits": pause_threshold,
                                                "entry_pause_window_hours": pause_window,
                                                "hold_hours": hold,
                                                "exit_mode": exit_mode,
                                                "entry_delay_hours": 1,
                                                "impact_coef_bps": args.impact_coef_bps,
                                                "round_trip_cost_multiplier": args.round_trip_cost_multiplier,
                                                "deploy_capital_usd": args.deploy_capital_usd,
                                                "gross_exposure": args.continuous_gross,
                                                "max_active": args.max_active,
                                                "sizing_mode": args.sizing_mode,
                                                "age_days_min": args.age_days_min,
                                                "stop_loss_pct": args.stop_loss_pct,
                                                "breakeven_arm_pct": args.breakeven_arm_pct,
                                                "mfe_giveback_trigger_pct": args.mfe_giveback_trigger_pct,
                                                "mfe_giveback_retain_pct": args.mfe_giveback_retain_pct,
                                                "entry_decel_lookback_h": args.entry_decel_lookback_h,
                                                "entry_decel_max_ret": args.entry_decel_max_ret,
                                                "market_min_ret_1d": args.market_min_ret_1d,
                                                "failed_fade_hours": failed_fade_hours,
                                                "failed_fade_loss_pct": failed_fade_loss_pct,
                                                "failed_fade_min_mfe_pct": failed_fade_min_mfe_pct,
                                            }
                                            cid = _cell_id(
                                                q,
                                                liq,
                                                hold,
                                                exit_mode,
                                                args.impact_coef_bps,
                                                args.deploy_capital_usd,
                                                feature_set,
                                                btc_trend_gate,
                                                entry_event_trigger,
                                                pause_threshold,
                                                pause_window,
                                                args.round_trip_cost_multiplier,
                                                args.continuous_gross,
                                                args.max_active,
                                                args.sizing_mode,
                                                args.age_days_min,
                                                failed_fade_hours,
                                                failed_fade_loss_pct,
                                                failed_fade_min_mfe_pct,
                                            )
                                            if args.stop_loss_pct > 0.0:
                                                cid += f"_sl{int(round(args.stop_loss_pct * 1000))}"
                                            if args.breakeven_arm_pct > 0.0:
                                                cid += f"_be{int(round(args.breakeven_arm_pct * 1000))}"
                                            if args.mfe_giveback_trigger_pct > 0.0:
                                                cid += (
                                                    f"_gbt{int(round(args.mfe_giveback_trigger_pct * 1000))}"
                                                    f"r{int(round(args.mfe_giveback_retain_pct * 1000))}"
                                                )
                                            if args.entry_decel_lookback_h > 0:
                                                cid += (
                                                    f"_dec{args.entry_decel_lookback_h}"
                                                    f"r{int(round(args.entry_decel_max_ret * 1000))}"
                                                )
                                            if args.market_min_ret_1d > -1.0:
                                                cid += f"_mktmin{int(round(args.market_min_ret_1d * 1000))}"
                                            cell_out = venue_out / "cells" / cid
                                            common = dict(
                                                start_date=args.start,
                                                end_date=args.end,
                                                rmom_quantile=q,
                                                feature_set=feature_set,
                                                btc_trend_gate=btc_trend_gate,
                                                entry_event_trigger=entry_event_trigger,
                                                entry_pause_after_adverse_exits=pause_threshold,
                                                entry_pause_window_hours=pause_window,
                                                liq_turnover_min=liq,
                                                hold_hours=hold,
                                                exit_mode=exit_mode,
                                                max_hold_hours=max(48, hold),
                                                entry_delay_hours=1,
                                                impact_coef_bps=args.impact_coef_bps,
                                                round_trip_cost_multiplier=args.round_trip_cost_multiplier,
                                                deploy_capital_usd=args.deploy_capital_usd,
                                                max_active=args.max_active,
                                                sizing_mode=args.sizing_mode,
                                                age_days_min=args.age_days_min,
                                                stop_loss_pct=args.stop_loss_pct,
                                                breakeven_arm_pct=args.breakeven_arm_pct,
                                                mfe_giveback_trigger_pct=args.mfe_giveback_trigger_pct,
                                                mfe_giveback_retain_pct=args.mfe_giveback_retain_pct,
                                                entry_decel_lookback_h=args.entry_decel_lookback_h,
                                                entry_decel_max_ret=args.entry_decel_max_ret,
                                                market_min_ret_1d=args.market_min_ret_1d,
                                                failed_fade_hours=failed_fade_hours,
                                                failed_fade_loss_pct=failed_fade_loss_pct,
                                                failed_fade_min_mfe_pct=failed_fade_min_mfe_pct,
                                            )
                                            short_full_cfg = ContinuousEventConfig(
                                                **common, side="short", decile=9, gross_exposure=args.continuous_gross
                                            )
                                            short_leg_cfg = ContinuousEventConfig(
                                                **common, side="short", decile=9, gross_exposure=args.ls_leg_gross
                                            )
                                            long_leg_cfg = ContinuousEventConfig(
                                                **common, side="long", decile=0, gross_exposure=args.ls_leg_gross
                                            )

                                            short_full, short_full_trades, short_full_eq, short_full_mtm = (
                                                _run_continuous(ctx, short_full_cfg, construction="short_only_d9")
                                            )
                                            short_leg, short_leg_trades, _short_leg_eq, _short_leg_mtm = (
                                                _run_continuous(ctx, short_leg_cfg, construction="ls_short_d9")
                                            )
                                            long_leg, long_leg_trades, _long_leg_eq, _long_leg_mtm = _run_continuous(
                                                ctx, long_leg_cfg, construction="ls_long_d0"
                                            )
                                            if short_leg_trades.is_empty() and long_leg_trades.is_empty():
                                                ls_trades = _empty_trades()
                                            elif short_leg_trades.is_empty():
                                                ls_trades = long_leg_trades
                                            elif long_leg_trades.is_empty():
                                                ls_trades = short_leg_trades
                                            else:
                                                ls_trades = pl.concat(
                                                    [short_leg_trades, long_leg_trades],
                                                    how="diagonal_relaxed",
                                                ).sort(["entry_ts_ms", "symbol"])
                                            ls_payload, ls_eq, ls_mtm = _payload_from_trades(
                                                config=replace(short_leg_cfg, side="short"),
                                                trades=ls_trades,
                                                klines=ctx.klines,
                                                n_fresh_entries=int(short_leg.get("n_fresh_entries", 0))
                                                + int(long_leg.get("n_fresh_entries", 0)),
                                                skips={
                                                    f"short_{k}": v
                                                    for k, v in dict(short_leg.get("skips") or {}).items()
                                                }
                                                | {
                                                    f"long_{k}": v
                                                    for k, v in dict(long_leg.get("skips") or {}).items()
                                                },
                                                construction="market_neutral_d0_d9",
                                            )

                                            _write_payload(
                                                cell_out / "short_only",
                                                payload=short_full,
                                                trades=short_full_trades,
                                                equity=short_full_eq,
                                                mtm_equity=short_full_mtm,
                                            )
                                            _write_payload(
                                                cell_out / "short_leg",
                                                payload=short_leg,
                                                trades=short_leg_trades,
                                                equity=_additive_equity(short_leg_trades),
                                                mtm_equity=_portfolio_mtm_equity(short_leg_trades, ctx.klines),
                                            )
                                            _write_payload(
                                                cell_out / "long_leg",
                                                payload=long_leg,
                                                trades=long_leg_trades,
                                                equity=_additive_equity(long_leg_trades),
                                                mtm_equity=_portfolio_mtm_equity(long_leg_trades, ctx.klines),
                                            )
                                            _write_payload(
                                                cell_out / "ls",
                                                payload=ls_payload,
                                                trades=ls_trades,
                                                equity=ls_eq,
                                                mtm_equity=ls_mtm,
                                            )
                                            row = _row(
                                                venue=venue,
                                                cell_id=cid,
                                                params=params,
                                                daily=daily_payloads[venue],
                                                daily_returns=daily_returns[venue],
                                                short_full=short_full,
                                                short_mtm_equity=short_full_mtm,
                                                ls=ls_payload,
                                                ls_mtm_equity=ls_mtm,
                                                short_leg=short_leg,
                                                long_leg=long_leg,
                                            )
                                            all_rows.append(row)
                                            print(
                                                f"[{venue}] {cid} "
                                                f"LS MAR={row.get('ls_mtm_mar')} daily MAR={row.get('daily_mar')} "
                                                f"delta={row.get('ls_delta_mar_vs_daily')} "
                                                f"corr={row.get('ls_corr_daily')}",
                                                flush=True,
                                            )

    pooled = _pooled(all_rows)
    _write_csv(out_root / "summary.csv", all_rows)
    _write_csv(out_root / "pooled_summary.csv", pooled)
    verdict = {
        "pre_registration": args.pre_registration,
        "start": args.start,
        "end": args.end,
        "out": str(out_root),
        "n_rows": len(all_rows),
        "best": pooled[0] if pooled else None,
        "accepted_pre_stress": bool(pooled and pooled[0].get("beats_rule_pre_stress")),
        "note": (
            "accepted_pre_stress only covers the frozen first grid. The pre-registered cost-stress "
            "gate must still pass before candidate status."
        ),
    }
    (out_root / "verdict.json").write_text(json.dumps(verdict, indent=2, default=str), encoding="utf-8")
    print(f"summary={out_root / 'summary.csv'}")
    print(f"pooled={out_root / 'pooled_summary.csv'}")
    print(f"verdict={out_root / 'verdict.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
