#!/usr/bin/env python3
"""One command for standard equity curves.

LONG runs from ``long_native.long_v11a_profile``.
CONTINUOUS is reconstructed from the continuous entry book
(`continuous_ensemble_v2`, code-defined TP12 components, 2f hedge, BTC-vol
regime) via the continuous refresh runner.
CARRY renders the registered research config ``configs/lane2_carry_hold_v3.json``
through the same --research-config path (cross-venue panel, settlement-exact
scorer). That is the registered research shape, not a demo/paper daemon replay.

    bash scripts/equity_curves.sh                      # LONG sleeve, last 3 years, bybit_full_pit
    bash scripts/equity_curves.sh --sleeves continuous # retired-from-publication profile
    bash scripts/equity_curves.sh --sleeves long,continuous,carry
    bash scripts/equity_curves.sh --root ~/SHARED_DATA/binance_full_pit --venue binance

The strategy modules own their active configurations.
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.config import load_config  # noqa: E402
from liquidity_migration.continuous_profile import CONTINUOUS_HISTORICAL_RUN_LABEL  # noqa: E402
from liquidity_migration.symbol_codec import (  # noqa: E402
    SymbolIdentityError,
    decode_symbol_partition,
)

DEFAULT_ROOT = "~/SHARED_DATA/bybit_full_pit"
DEFAULT_CONFIG = "configs/volume_alpha.default.yaml"
DEFAULT_PANEL_ROOT = "~/SHARED_DATA/cross_venue_panel_v1"
VALID_CONTINUOUS_VENUES = {"bybit", "binance"}

#: Columns a registered financed-longs config needs from the cross-venue panel.
RESEARCH_PANEL_COLUMNS = (
    "symbol", "bar_ts_ms", "by_close", "by_turnover_quote", "by_funding",
    "by_funding_age_h", "bn_close", "bn_turnover_quote", "bn_funding", "bn_funding_age_h",
)


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _shift_years(date: dt.date, years: int) -> dt.date:
    # Clamp Feb 29 to Feb 28 when the target year is not a leap year.
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
    # LONG records its own PIT pass/taint label; pit_tol does not apply.
    del pit_tol
    from liquidity_migration.long_native import long_v11a_profile, run_long_native_research

    cfg = replace(long_v11a_profile(), start_date=start, end_date=end)
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
        "run_label": CONTINUOUS_HISTORICAL_RUN_LABEL,
        "summary": normalized,
        "continuous_summary": summary,
        "report_dir": str(report_dir),
    }


def _run_continuous(
    root: str,
    costs: Any,
    start: str | None,  # None preserves the active profile's full history
    end: str,
    out: Path,
    pit_tol: float,
    *,
    venue: str | None = None,
    render_only: bool = False,
    chart_leverage: float | None = 4.0,
    backtest_leverage: float = 1.0,
    research_disable_btc_gate: bool = False,
) -> dict[str, Any]:
    del costs, pit_tol
    scripts_dir = REPO / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    import continuous_deployed_equity_refresh as continuous_refresh

    data_root = Path(root).expanduser()
    venue_name = _infer_venue_from_root(data_root, venue)
    summary = continuous_refresh.run_venue(
        venue_name,
        output_root=out,
        start_date=start,
        end_date=end,
        render_only=render_only,
        data_root=data_root,
        chart_leverage=chart_leverage,
        backtest_leverage=backtest_leverage,
        research_disable_btc_gate=research_disable_btc_gate,
    )
    return _continuous_payload_from_summary(summary, report_dir=out / venue_name)


def _load_research_panel(panel_root: str | Path) -> Any:
    """Load the cross-venue panel columns every research-config render needs."""
    import polars as pl

    root = Path(panel_root).expanduser()
    shards = sorted(str(x) for x in root.glob("*/panel.parquet"))
    if not shards:
        raise RuntimeError(f"no cross-venue panel shards under {root}")
    return (
        pl.scan_parquet(shards)
        .select(list(RESEARCH_PANEL_COLUMNS))
        .collect()
        .sort(["symbol", "bar_ts_ms"])
    )


def _run_carry(
    panel_root: str,
    start: str,
    end: str,
    out: Path,
) -> dict[str, Any]:
    """Render the CARRY sleeve's registered research shape.

    The carry runtime replays ``configs/lane2_carry_hold_v3.json``, so its
    standard curve is that same config through the --research-config path. It
    reads the cross-venue panel, not the demo/paper cycle record.
    """
    from liquidity_migration.carry_demo import CARRY_CONFIG_PATH
    from liquidity_migration.financed_longs import research_equity_chart

    panel = _load_research_panel(panel_root)
    return research_equity_chart(panel, CARRY_CONFIG_PATH, out, start=start, end=end)


RUNNERS = {"long": _run_long, "continuous": _run_continuous, "carry": _run_carry}


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


def _prepare_sleeve_output(out: Path, *, fresh: bool) -> None:
    """Create one sleeve directory, optionally discarding only derived output."""

    if fresh and (out.exists() or out.is_symlink()):
        if out.is_symlink() or not out.is_dir():
            raise RuntimeError(f"refusing to replace non-directory sleeve output: {out}")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)


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
            try:
                recent.add(decode_symbol_partition(s.split("symbol=")[-1]))
            except SymbolIdentityError:
                continue
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
        description="Standard equity curves for the active LONG and CONTINUOUS profiles.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--sleeves",
        default="long",
        help=(
            "Comma list: long, continuous, carry. 'carry' renders the registered "
            "research config (lane2_carry_hold_v3) from the cross-venue panel — "
            "a research-shape simulation, not a daemon replay."
        ),
    )
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
        "--continuous-chart-leverage",
        "--chart-leverage",
        dest="continuous_chart_leverage",
        type=float,
        default=4.0,
        help="Extra pure-leverage continuous chart to render alongside 1x. Use 1 to suppress the extra chart.",
    )
    p.add_argument(
        "--continuous-backtest-leverage",
        type=float,
        default=1.0,
        help="Modeled continuous leverage: scales component gross exposure before costs/funding and scales hedge cap.",
    )
    p.add_argument(
        "--research-disable-btc-gate",
        action="store_true",
        help=(
            "RESEARCH RENDER ONLY (T-A ablation): render the continuous sleeve with the "
            "BTC uptrend entry gate off. Requires an explicit --out; never touches the "
            "runtime demo/paper producers or the hedge service."
        ),
    )
    # Default --years to a sentinel so an unset window preserves the active
    # profile's full history instead of forcing a rolling 3y override.
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
    p.add_argument(
        "--fresh-output",
        action="store_true",
        help=(
            "Remove each requested sleeve's derived report directory before running. "
            "Use for isolated research-run outputs; raw market data is never removed."
        ),
    )
    p.add_argument(
        "--research-config",
        action="append",
        default=None,
        metavar="CONFIG_JSON",
        help=(
            "Registered financed-longs config JSON (repeatable) to render through the "
            "SAME standard chart, labelled RESEARCH / simulation-on-seen-data. This is "
            "the supported way to put a Lane-2 research config in the standard format; "
            "never hand-build a lookalike chart. Reads the cross-venue panel."
        ),
    )
    p.add_argument(
        "--panel-root",
        default=DEFAULT_PANEL_ROOT,
        help="Cross-venue panel root for --research-config renders.",
    )
    args = p.parse_args()

    sleeves = [s.strip() for s in args.sleeves.split(",") if s.strip()]
    bad = [s for s in sleeves if s not in RUNNERS]
    if bad:
        raise SystemExit(f"unknown sleeve(s) {bad}; valid: {', '.join(RUNNERS)}")
    if args.research_disable_btc_gate and not args.out:
        raise SystemExit("--research-disable-btc-gate requires an isolated --out directory")

    today = _today()
    end = args.end or (today + dt.timedelta(days=1)).isoformat()
    # Without an explicit window the continuous path uses its full active history.
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
        _prepare_sleeve_output(out, fresh=args.fresh_output)
        heading = {
            "long": "active LONG profile",
            "carry": "registered CARRY research config, simulation on seen data",
        }.get(s, "active CONTINUOUS profile")
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
            elif s == "carry":
                payload = _run_carry(args.panel_root, start, end, out)
            else:
                # Pass a start only when the user explicitly requests a window.
                payload = _run_continuous(
                    root,
                    costs,
                    start if explicit_window else None,
                    end,
                    out,
                    0.0,
                    venue=args.venue,
                    render_only=args.continuous_render_only,
                    chart_leverage=args.continuous_chart_leverage,
                    backtest_leverage=args.continuous_backtest_leverage,
                    research_disable_btc_gate=args.research_disable_btc_gate,
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

    panel = None
    if args.research_config:
        try:
            panel = _load_research_panel(args.panel_root)
        except Exception as exc:  # noqa: BLE001 - every research render fails together
            for raw_path in args.research_config:
                results[f"research:{Path(raw_path).stem}"] = {"error": str(exc)}
            print(f"  [X] research panel load failed: {type(exc).__name__}: {exc}\n", flush=True)
    research_paths = list(args.research_config or []) if panel is not None else []
    for raw_path in research_paths:
        cfg_path = Path(raw_path).expanduser()
        name = cfg_path.stem
        key = f"research:{name}"
        out = out_root / "research" / name
        _prepare_sleeve_output(out, fresh=args.fresh_output)
        print(f"=== RESEARCH ({name}) ===", flush=True)
        try:
            from liquidity_migration.financed_longs import research_equity_chart

            payload = research_equity_chart(panel, cfg_path, out, start=start, end=end)
        except Exception as exc:  # noqa: BLE001 - report per-config, keep going
            print(f"  [X] {name} failed: {type(exc).__name__}: {exc}\n", flush=True)
            results[key] = {"error": str(exc)}
            continue
        print(f"  run_label = {payload['run_label']}")
        print(f"  {_headline(payload)}")
        print(f"  PNG: {payload.get('png') or '(none)'}\n", flush=True)
        results[key] = {"png": payload.get("png"), "run_label": payload["run_label"]}

    print("=" * 64)
    print("EQUITY CURVES - SUMMARY")
    for s in [*sleeves, *(k for k in results if k.startswith("research:"))]:
        r = results.get(s, {})
        if r.get("error"):
            print(f"  {s:11} [X] {r['error'][:80]}")
        else:
            print(f"  {s:11} {r.get('run_label', '-'):42} {r.get('png') or '(no png)'}")
    # Keep going across sleeves, but exit non-zero so a driver cannot accept a
    # partial benchmark as complete.
    return 1 if any(result.get("error") for result in results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
