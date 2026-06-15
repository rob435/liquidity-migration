#!/usr/bin/env python3
"""One command for official equity curves.

LONG runs from the exact promoted-in-code profile in `liquidity_migration.promoted`.
CONTINUOUS runs from the research/demo-stage continuous ensemble reconstruction
(`continuous_ensemble_v1` style winner_base + 2f hedge), not from promoted profiles
and not as a promotion claim.

    bash scripts/equity_curves.sh                      # promoted LONG sleeve, last 3 years, bybit_full_pit
    bash scripts/equity_curves.sh --sleeves continuous # research-stage continuous book
    bash scripts/equity_curves.sh --sleeves long,continuous
    bash scripts/equity_curves.sh --root ~/SHARED_DATA/binance_full_pit --venue binance

The promoted LONG profile lives in ONE place: `liquidity_migration/promoted.py`.
Continuous is deliberately outside `promoted.PROFILES`; the forward demo is its arbiter.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration import promoted  # noqa: E402
from liquidity_migration.config import load_config  # noqa: E402

DEFAULT_ROOT = "~/SHARED_DATA/bybit_full_pit"
DEFAULT_CONFIG = "configs/volume_alpha.default.yaml"
DEFAULT_CONTINUOUS_FROZEN_FALLBACK = "~/SHARED_DATA/continuous_deployed_equity_refresh_2026-06-12"
VALID_CONTINUOUS_VENUES = {"bybit", "binance"}


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _shift_years(date: dt.date, years: int) -> dt.date:
    # audit2: date.replace(year=...) raises ValueError for Feb 29 when the
    # target year is not a leap year; clamp Feb 29 -> Feb 28 in that case.
    try:
        return date.replace(year=date.year - years)
    except ValueError:
        return date.replace(year=date.year - years, day=28)


def _run_long(
    root: str,
    costs: Any,
    start: str,
    end: str,
    out: Path,
    pit_tol: float,
    long_notional: float | None = None,
) -> dict[str, Any]:
    # long_native has its own PIT label (require_full_pit_universe=False -> it reports,
    # does not abort); pit_tol does not apply to its engine.
    del pit_tol
    from liquidity_migration.long_native import run_long_native_research

    cfg = promoted.long_profile(start=start, end=end)
    if long_notional is not None:
        # Research convention is 1x; this option draws pure leverage on the same signal.
        cfg = replace(cfg, notional_multiplier=float(long_notional))
    return run_long_native_research(root, config=cfg, cost_config=costs, report_dir=out)


def _infer_venue_from_root(root: str | Path, explicit: str | None = None) -> str:
    if explicit:
        venue = explicit.lower()
        if venue not in VALID_CONTINUOUS_VENUES:
            raise ValueError(f"unknown continuous venue {explicit!r}; valid: {', '.join(sorted(VALID_CONTINUOUS_VENUES))}")
        return venue
    raw = str(Path(root).expanduser()).lower().replace("\\", "/")
    if "binance" in raw:
        return "binance"
    if "bybit" in raw:
        return "bybit"
    raise ValueError("could not infer continuous venue from --root; pass --venue bybit or --venue binance")


def _continuous_payload_from_summary(summary: dict[str, Any], *, report_dir: Path) -> dict[str, Any]:
    one_x = summary.get("1x") or {}
    normalized: dict[str, float] = {}
    if isinstance(one_x.get("total_return_pct"), (int, float)):
        normalized["total_return"] = float(one_x["total_return_pct"]) / 100.0
    if isinstance(one_x.get("max_drawdown_pct"), (int, float)):
        normalized["max_drawdown"] = float(one_x["max_drawdown_pct"]) / 100.0
    if isinstance(one_x.get("mar"), (int, float)):
        normalized["mar"] = float(one_x["mar"])
    if isinstance(one_x.get("sharpe_daily_ann"), (int, float)):
        normalized["sharpe_like"] = float(one_x["sharpe_daily_ann"])
    return {
        "run_label": "continuous_research_stage_not_promoted",
        "summary": normalized,
        "continuous_summary": summary,
        "report_dir": str(report_dir),
    }


def _run_continuous(
    root: str,
    costs: Any,
    start: str | None,  # audit2c: None preserves the frozen continuous start
    end: str,
    out: Path,
    pit_tol: float,
    *,
    venue: str | None = None,
    render_only: bool = False,
    frozen_fallback: str | Path | None = None,
    chart_leverage: float | None = 4.0,
) -> dict[str, Any]:
    del costs, pit_tol
    scripts_dir = REPO / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    import continuous_deployed_equity_refresh as continuous_refresh

    data_root = Path(root).expanduser()
    venue_name = _infer_venue_from_root(data_root, venue)
    fallback = Path(frozen_fallback).expanduser() if frozen_fallback else None
    summary = continuous_refresh.run_venue(
        venue_name,
        output_root=out,
        start_date=start,
        end_date=end,
        render_only=render_only,
        frozen_fallback=fallback,
        data_root=data_root,
        chart_leverage=chart_leverage,
    )
    return _continuous_payload_from_summary(summary, report_dir=out / venue_name)


RUNNERS = {"long": _run_long, "continuous": _run_continuous}


def _find_png(out: Path) -> Path | None:
    hits = sorted(out.rglob("*equity*btc*.png")) or sorted(out.rglob("*equity*.png"))
    if not hits:
        return None

    def score(path: Path) -> tuple[int, int, int, int, str]:
        rel_parts = tuple(part.lower() for part in path.relative_to(out).parts)
        is_component = "components" in rel_parts
        is_levered = "_4x" in path.stem.lower()
        is_official_name = path.name in {"long_native_equity_btc.png", "continuous_equity_btc.png"}
        return (
            1 if is_component else 0,
            1 if is_levered else 0,
            0 if is_official_name else 1,
            len(rel_parts),
            str(path),
        )

    return min(hits, key=score)


def _plot_equity_csv(out: Path, sleeve: str) -> Path | None:
    """Fallback: plot a cumulative-equity curve from the engine's equity CSV."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import polars as pl

    csvs = sorted(out.rglob("*equity*.csv"))
    if not csvs:
        return None
    primary = [p for p in csvs if "_4x" not in p.stem.lower()]
    df = pl.read_csv((primary or csvs)[-1])
    cols = {c.lower(): c for c in df.columns}
    eq = next((cols[c] for c in ("equity", "equity_usdt", "cum_return", "cumulative_return", "nav") if c in cols), None)
    xc = next((cols[c] for c in ("date", "ts_ms", "day", "timestamp") if c in cols), None)
    if eq is None:
        return None
    y = df[eq].to_list()
    x = list(range(len(y))) if xc is None else df[xc].to_list()
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(x, y, lw=1.3)
    ax.set_title(f"{sleeve} sleeve - equity ({eq})")
    ax.set_ylabel(eq)
    ax.grid(alpha=0.3)
    png = out / f"{sleeve}_equity.png"
    fig.tight_layout()
    fig.savefig(png, dpi=110)
    plt.close(fig)
    return png


def _label(payload: dict[str, Any]) -> str:
    return str(payload.get("run_label") or (payload.get("summary") or {}).get("run_label") or "-")


def _delisted_traded(out: Path, root: str) -> int | None:
    """Count traded symbols absent from the last 30d of klines.

    A value > 0 proves the run used a delisted-inclusive PIT universe. A
    current-universe survivorship-biased run would trade zero delisted names.
    """
    import glob
    import os

    import polars as pl

    tcsv = sorted(out.rglob("*best_trades.csv")) or sorted(out.rglob("*trades*.csv"))
    kroot = os.path.join(os.path.expanduser(root), "klines_1h")
    if not tcsv or not os.path.isdir(kroot):
        return None
    try:
        syms = set(pl.read_csv(tcsv[-1])["symbol"].unique().to_list())
    except Exception:  # noqa: BLE001
        return None
    recent: set[str] = set()
    for d in sorted(os.listdir(kroot))[-30:]:
        for s in glob.glob(os.path.join(kroot, d, "symbol=*")):
            recent.add(s.split("symbol=")[-1])
    return len(syms - recent)


def _pit_verdict(label: str, delisted: int | None) -> str:
    if "missing_manifest" in label:
        return "  [!] NOT clean full-PIT (manifest empty - do not cite)"
    if "current_universe" in label:
        if delisted and delisted > 0:
            return (
                f"  [OK] effectively full-PIT - {delisted} delisted names traded "
                "(no survivorship; label is conservative over a listing-boundary gap)"
            )
        return "  [!] current-universe (no delisted names traded - possible survivorship; treat as biased)"
    return ""


def _headline(payload: dict[str, Any]) -> str:
    s = payload.get("summary") or payload.get("metrics") or {}
    bits = []
    for k, fmt in (
        ("total_return", "ret {:+.1%}"),
        ("max_drawdown", "DD {:.1%}"),
        ("sharpe_like", "Sharpe {:.2f}"),
        ("mar", "MAR {:.2f}"),
        ("trades", "{:.0f} trades"),
    ):
        if k in s and isinstance(s[k], (int, float)):
            bits.append(fmt.format(s[k]))
    return " | ".join(bits) if bits else "(see report)"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Official equity curves for LONG and the research-stage continuous book.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--sleeves", default="long", help="Comma list: long, continuous.")
    p.add_argument(
        "--long-notional-multiplier",
        type=float,
        default=None,
        help=(
            "Override the long sleeve's notional_multiplier. Research default is 1x; "
            "e.g. 5 draws pure leverage on the same signal."
        ),
    )
    p.add_argument("--venue", choices=sorted(VALID_CONTINUOUS_VENUES), default=None, help="Continuous venue override.")
    p.add_argument(
        "--continuous-render-only",
        action="store_true",
        help="Re-render continuous PNGs/stats from an existing continuous_equity.csv; no component rerun.",
    )
    p.add_argument(
        "--continuous-frozen-fallback",
        default=DEFAULT_CONTINUOUS_FROZEN_FALLBACK,
        help="Component-report fallback root used to recover frozen continuous configs.",
    )
    p.add_argument(
        "--continuous-chart-leverage",
        "--chart-leverage",
        dest="continuous_chart_leverage",
        type=float,
        default=4.0,
        help="Extra pure-leverage continuous chart to render alongside 1x. Use 1 to suppress the extra chart.",
    )
    # audit2c: default --years to a sentinel so an unset window can preserve the
    # frozen continuous start instead of forcing a rolling 3y override.
    p.add_argument(
        "--years",
        type=int,
        default=None,
        help="Window length in years (default 3; ignored if --start given).",
    )
    p.add_argument("--start", default=None, help="Window start YYYY-MM-DD (overrides --years).")
    p.add_argument("--end", default=None, help="Window end YYYY-MM-DD (exclusive; default tomorrow UTC).")
    p.add_argument("--root", default=DEFAULT_ROOT, help="Per-venue full-PIT data root.")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="Cost-model config.")
    p.add_argument("--out", default=None, help="Report dir (default <root>/reports/equity_curves).")
    args = p.parse_args()

    sleeves = [s.strip() for s in args.sleeves.split(",") if s.strip()]
    bad = [s for s in sleeves if s not in RUNNERS]
    if bad:
        raise SystemExit(f"unknown sleeve(s) {bad}; valid: {', '.join(RUNNERS)}")

    today = _today()
    end = args.end or (today + dt.timedelta(days=1)).isoformat()
    # audit2c: only treat the window as explicit when the user passed --start or
    # --years; otherwise the continuous path must inherit the frozen deployed
    # start (None) rather than a rolling 3y override.
    explicit_window = args.start is not None or args.years is not None
    years = 3 if args.years is None else args.years
    start = args.start or _shift_years(today, years).isoformat()
    root = str(Path(args.root).expanduser())
    out_root = Path(args.out).expanduser() if args.out else Path(root) / "reports" / "equity_curves"
    costs = load_config(args.config).costs

    print(f"equity-curves - window {start} -> {end} | root {root} | sleeves {', '.join(sleeves)}\n")
    results: dict[str, dict[str, Any]] = {}
    for s in sleeves:
        out = out_root / s
        out.mkdir(parents=True, exist_ok=True)
        heading = "promoted LONG profile" if s == "long" else "research-stage continuous demo book"
        print(f"=== {s.upper()} ({heading}) ===", flush=True)
        try:
            if s == "long":
                payload = _run_long(
                    root,
                    costs,
                    start,
                    end,
                    out,
                    0.0,
                    long_notional=args.long_notional_multiplier,
                )
            else:
                # audit2c: pass the rolling/explicit start only when the user asked
                # for a window; otherwise None preserves the frozen continuous start.
                payload = _run_continuous(
                    root,
                    costs,
                    start if explicit_window else None,
                    end,
                    out,
                    0.0,
                    venue=args.venue,
                    render_only=args.continuous_render_only,
                    frozen_fallback=args.continuous_frozen_fallback,
                    chart_leverage=args.continuous_chart_leverage,
                )
        except Exception as exc:  # noqa: BLE001 - report per-sleeve, keep going
            print(f"  [X] {s} failed: {type(exc).__name__}: {exc}\n", flush=True)
            results[s] = {"error": str(exc)}
            continue
        png = _find_png(out) or _plot_equity_csv(out, s)
        label = _label(payload)
        needs_pit_detail = "current_universe" in label or "missing_manifest" in label
        verdict = _pit_verdict(label, _delisted_traded(out, root) if needs_pit_detail else None)
        print(f"  run_label = {label}{verdict}")
        print(f"  {_headline(payload)}")
        print(f"  PNG: {png or '(none - no equity csv/png emitted)'}\n", flush=True)
        results[s] = {"png": str(png) if png else None, "run_label": label}

    print("=" * 64)
    print("EQUITY CURVES - SUMMARY")
    for s in sleeves:
        r = results.get(s, {})
        if r.get("error"):
            print(f"  {s:11} [X] {r['error'][:80]}")
        else:
            print(f"  {s:11} {r.get('run_label', '-'):42} {r.get('png') or '(no png)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
