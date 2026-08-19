#!/usr/bin/env python3
"""One command for standard equity curves.

LONG runs from ``long_native.long_v11a_profile``.
CARRY renders the deployed rule ``configs/lane2_carry_hold_v6.json``
through the same --research-config path (cross-venue panel, settlement-exact
scorer). That is the registered research shape, not a demo daemon replay.

    bash scripts/research/equity_curves.sh                      # LONG sleeve, last 3 years, bybit_full_pit
    bash scripts/research/equity_curves.sh --sleeves long,carry
    bash scripts/research/equity_curves.sh --root ~/SHARED_DATA/binance_full_pit

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

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from liquidity_migration.core.config import load_config  # noqa: E402
from liquidity_migration.core.symbol_codec import (  # noqa: E402
    SymbolIdentityError,
    decode_symbol_partition,
)

DEFAULT_ROOT = "~/SHARED_DATA/bybit_full_pit"
DEFAULT_CONFIG = "configs/volume_alpha.default.yaml"
DEFAULT_PANEL_ROOT = "~/SHARED_DATA/cross_venue_panel_v1"

#: Columns a registered financed-longs config needs from the cross-venue panel.
RESEARCH_PANEL_COLUMNS = (
    "symbol", "bar_ts_ms", "by_close", "by_turnover_quote", "by_funding",
    "by_funding_age_h", "bn_close", "bn_turnover_quote", "bn_funding", "bn_funding_age_h",
)
#: Present only on panels built with --metrics-root; kept when every shard has
#: them so v5 renders, while older panels still render v1..v4.
OPTIONAL_RESEARCH_PANEL_COLUMNS = ("bn_tt_ls", "bn_tt_ls_age_h")


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
    long_profile: str = "v12",
) -> dict[str, Any]:
    # LONG records its own PIT pass/taint label; pit_tol does not apply.
    del pit_tol
    from liquidity_migration.research.backtest.long_native import run_long_native_research
    from liquidity_migration.rules.long_native import long_v11a_profile, long_v12_profile

    profile = {"v11a": long_v11a_profile, "v12": long_v12_profile}[long_profile]
    cfg = replace(profile(), start_date=start, end_date=end)
    if long_notional is not None:
        # Research convention is 1x; this option draws pure leverage on the same signal.
        cfg = replace(cfg, notional_multiplier=float(long_notional))
    return run_long_native_research(root, config=cfg, cost_config=costs, report_dir=out)


def _load_research_panel(panel_root: str | Path) -> Any:
    """Load the cross-venue panel columns every research-config render needs."""
    import polars as pl

    root = Path(panel_root).expanduser()
    shards = sorted(str(x) for x in root.glob("*/panel.parquet"))
    if not shards:
        raise RuntimeError(f"no cross-venue panel shards under {root}")
    scans = [pl.scan_parquet(s) for s in shards]
    optional = [
        c
        for c in OPTIONAL_RESEARCH_PANEL_COLUMNS
        if all(c in s.collect_schema().names() for s in scans)
    ]
    cols = list(RESEARCH_PANEL_COLUMNS) + optional
    return (
        pl.concat([s.select(cols) for s in scans])
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

    The carry runtime replays ``configs/lane2_carry_hold_v6.json``, so its
    standard curve is that same config through the --research-config path. It
    reads the cross-venue panel, not the demo cycle record.
    """
    from liquidity_migration.strategy.carry_demo import CARRY_CONFIG_PATH
    from liquidity_migration.research.backtest.financed_longs import research_equity_chart

    panel = _load_research_panel(panel_root)
    return research_equity_chart(panel, CARRY_CONFIG_PATH, out, start=start, end=end)


RUNNERS = {"long": _run_long, "carry": _run_carry}


def _find_png(out: Path) -> Path | None:
    hits = sorted(out.rglob("*equity*btc*.png")) or sorted(out.rglob("*equity*.png"))
    if not hits:
        return None

    def score(path: Path) -> tuple[int, int, int, int, str]:
        rel_parts = tuple(part.lower() for part in path.relative_to(out).parts)
        is_component = "components" in rel_parts
        is_levered = "_4x" in path.stem.lower()
        is_official_name = path.name == "long_native_equity_btc.png"
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
    # A kernel replay tape binds to the window that wrote it; a rerun with a
    # different window resumed onto it dies with "strategy event clock cannot
    # move backward". The replay state is derived scratch — always rebuilt.
    replay_state = out / "common_kernel_execution"
    if replay_state.is_dir():
        shutil.rmtree(replay_state)


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
        description="Standard equity curves for the active LONG profile and the registered CARRY config.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--sleeves",
        default="long",
        help=(
            "Comma list: long, carry. 'carry' renders the registered "
            "research config (lane2_carry_hold_v6) from the cross-venue panel — "
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
    p.add_argument(
        "--long-profile",
        choices=("v11a", "v12"),
        default="v12",
        help=(
            "Which LONG profile to render. v12 wide-stop is the deployed one "
            "(STATE.md change point 2026-08-03); v11a is its predecessor, kept "
            "for comparison."
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

    today = _today()
    end = args.end or (today + dt.timedelta(days=1)).isoformat()
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
        }[s]
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
                    long_profile=args.long_profile,
                )
            else:
                payload = _run_carry(args.panel_root, start, end, out)
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
            from liquidity_migration.research.backtest.financed_longs import research_equity_chart

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
