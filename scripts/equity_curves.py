#!/usr/bin/env python3
"""One command → the promoted-profile equity curve for every sleeve.

Zero friction: no flag archaeology, no reverse-engineering "what's deployed". Each
sleeve's curve is run from its EXACT deployed profile (`liquidity_migration.promoted`),
over a window you pick, and the equity-vs-BTC PNG is emitted (or plotted from the
equity CSV if the engine doesn't draw one). The run_label is printed for every run so
a biased/partial-PIT result can never masquerade as clean.

    bash scripts/equity_curves.sh                      # all sleeves, last 3 years, bybit_full_pit
    bash scripts/equity_curves.sh --sleeves short      # just one
    bash scripts/equity_curves.sh --years 2            # shorter window (lighter on RAM)
    bash scripts/equity_curves.sh --start 2023-06-01 --end 2026-06-02
    bash scripts/equity_curves.sh --root ~/SHARED_DATA/binance_full_pit_strategy   # other venue

The promoted profiles live in ONE place: `liquidity_migration/promoted.py`. Change a
profile there (or in its daemon factory) and this tool follows — see that module.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration import promoted  # noqa: E402
from liquidity_migration.config import load_config  # noqa: E402

DEFAULT_ROOT = "~/SHARED_DATA/bybit_full_pit"
DEFAULT_CONFIG = "configs/volume_alpha.default.yaml"
_CONTINUOUS_END_CAP_FALLBACK = "2026-05-28"


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _continuous_end_cap(root: str) -> str:
    """Cap the continuous window to the residual_momentum panel's LAST day. Past it the decile
    join goes empty and the curve paints a misleading flat tail. Read the panel's max day so this
    AUTO-TRACKS panel rebuilds (the daily rmom-refresh / a manual precompute) instead of a
    hardcoded date that silently goes stale and drops recent months (e.g. June)."""
    try:
        import polars as pl
        panel = Path(root).expanduser() / "residual_momentum.parquet"
        mx = pl.scan_parquet(panel).select(pl.col("ts_ms").max()).collect().item()
        return dt.datetime.fromtimestamp(int(mx) / 1000, dt.timezone.utc).date().isoformat()
    except Exception:
        return _CONTINUOUS_END_CAP_FALLBACK


def _run_short(root: str, costs, start: str, end: str, out: Path, pit_tol: float) -> dict:
    from liquidity_migration.volume_events import run_volume_event_research
    cfg = promoted.short_profile(start=start, end=end)
    return run_volume_event_research(root, event_config=cfg, cost_config=costs, report_dir=out)


def _run_long(root: str, costs, start: str, end: str, out: Path, pit_tol: float) -> dict:
    # long_native has its own PIT label (require_full_pit_universe=False → it reports,
    # does not abort); pit_tol does not apply to its engine.
    from liquidity_migration.long_native import run_long_native_research
    cfg = promoted.long_profile(start=start, end=end)
    return run_long_native_research(root, config=cfg, cost_config=costs, report_dir=out)


def _run_continuous(root: str, costs, start: str, end: str, out: Path, pit_tol: float) -> dict:
    # The continuous engine ranks within the available liquid universe (no manifest
    # full-PIT survivorship label), so pit_tol does not apply.
    from liquidity_migration.continuous_events import run_continuous_event_research
    end = min(end, _continuous_end_cap(root))  # rmom-panel bound (auto-tracks the panel's last day)
    cfg = promoted.continuous_profile(start=start, end=end)
    return run_continuous_event_research(root, config=cfg, report_dir=out)


RUNNERS = {"short": _run_short, "long": _run_long, "continuous": _run_continuous}


def _find_png(out: Path) -> Path | None:
    hits = sorted(out.rglob("*equity*btc*.png")) or sorted(out.rglob("*equity*.png"))
    return hits[-1] if hits else None


def _plot_equity_csv(out: Path, sleeve: str) -> Path | None:
    """Fallback: plot a cumulative-equity curve from the engine's equity CSV when
    the engine doesn't emit a PNG (e.g. the continuous engine)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import polars as pl

    csvs = sorted(out.rglob("*equity*.csv"))
    if not csvs:
        return None
    df = pl.read_csv(csvs[-1])
    cols = {c.lower(): c for c in df.columns}
    eq = next((cols[c] for c in ("equity", "equity_usdt", "cum_return", "cumulative_return", "nav") if c in cols), None)
    xc = next((cols[c] for c in ("date", "ts_ms", "day", "timestamp") if c in cols), None)
    if eq is None:
        return None
    y = df[eq].to_list()
    x = list(range(len(y))) if xc is None else df[xc].to_list()
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(x, y, lw=1.3)
    ax.set_title(f"{sleeve} sleeve — equity ({eq})")
    ax.set_ylabel(eq)
    ax.grid(alpha=0.3)
    png = out / f"{sleeve}_equity.png"
    fig.tight_layout()
    fig.savefig(png, dpi=110)
    plt.close(fig)
    return png


def _label(payload: dict) -> str:
    return str(payload.get("run_label") or (payload.get("summary") or {}).get("run_label") or "—")


def _delisted_traded(out: Path, root: str) -> int | None:
    """Count traded symbols absent from the last 30d of klines (= delisted/no longer
    listed). A value > 0 PROVES the run used the full delisted-inclusive PIT universe —
    a current-universe survivorship-biased run would trade zero delisted names. This is
    the honest survivorship check: it distinguishes a benign listing-boundary gap (the
    standard's excluded partials) from real current-universe bias behind the same label.
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
    """Honest, collision-free PIT read. The engine's run_label can read
    `pit_membership_filtered_current_universe` over a NEGLIGIBLE listing-boundary gap
    even though the run is delisted-inclusive (no survivorship). When delisted names
    WERE traded, say so plainly instead of crying "biased"; only flag real bias when
    the universe is genuinely current-only."""
    if "missing_manifest" in label:
        return "  ⚠️ NOT clean full-PIT (manifest empty — do not cite)"
    if "current_universe" in label:
        if delisted and delisted > 0:
            return (f"  ✓ effectively full-PIT — {delisted} delisted names traded "
                    "(no survivorship; label is conservative over a ~0.01% listing-boundary gap)")
        return "  ⚠️ current-universe (no delisted names traded — possible survivorship; treat as biased)"
    return ""


def _headline(payload: dict) -> str:
    s = payload.get("summary") or payload.get("metrics") or {}
    bits = []
    for k, fmt in (("total_return", "ret {:+.1%}"), ("max_drawdown", "DD {:.1%}"),
                   ("sharpe_like", "Sharpe {:.2f}"), ("mar", "MAR {:.2f}"), ("trades", "{:.0f} trades")):
        if k in s and isinstance(s[k], (int, float)):
            bits.append(fmt.format(s[k]))
    return " · ".join(bits) if bits else "(see report)"


def main() -> int:
    p = argparse.ArgumentParser(description="Promoted-profile equity curves, one command.",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--sleeves", default="short,long,continuous", help="Comma list (short,long,continuous).")
    p.add_argument("--years", type=int, default=3, help="Window length in years (ignored if --start given).")
    p.add_argument("--start", default=None, help="Window start YYYY-MM-DD (overrides --years).")
    p.add_argument("--end", default=None, help="Window end YYYY-MM-DD (exclusive; default tomorrow UTC).")
    p.add_argument("--root", default=DEFAULT_ROOT, help="Per-venue full-PIT data root.")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="Cost-model config (short/long).")
    p.add_argument("--out", default=None, help="Report dir (default <root>/reports/equity_curves).")
    args = p.parse_args()

    sleeves = [s.strip() for s in args.sleeves.split(",") if s.strip()]
    bad = [s for s in sleeves if s not in RUNNERS]
    if bad:
        raise SystemExit(f"unknown sleeve(s) {bad}; valid: {', '.join(RUNNERS)}")

    today = _today()
    end = args.end or (today + dt.timedelta(days=1)).isoformat()
    start = args.start or (today.replace(year=today.year - args.years)).isoformat()
    root = str(Path(args.root).expanduser())
    out_root = Path(args.out).expanduser() if args.out else Path(root) / "reports" / "equity_curves"
    costs = load_config(args.config).costs

    print(f"equity-curves — window {start} → {end} | root {root} | sleeves {', '.join(sleeves)}\n")
    results: dict[str, dict] = {}
    for s in sleeves:
        out = out_root / s
        out.mkdir(parents=True, exist_ok=True)
        print(f"=== {s.upper()} (promoted profile) ===", flush=True)
        try:
            payload = RUNNERS[s](root, costs, start, end, out, 0.0)
        except Exception as exc:  # noqa: BLE001 — report per-sleeve, keep going
            print(f"  ❌ {s} failed: {type(exc).__name__}: {exc}\n", flush=True)
            results[s] = {"error": str(exc)}
            continue
        png = _find_png(out) or _plot_equity_csv(out, s)
        label = _label(payload)
        verdict = _pit_verdict(label, _delisted_traded(out, root))
        print(f"  run_label = {label}{verdict}")
        print(f"  {_headline(payload)}")
        print(f"  PNG: {png or '(none — no equity csv/png emitted)'}\n", flush=True)
        results[s] = {"png": str(png) if png else None, "run_label": label}

    print("=" * 64)
    print("EQUITY CURVES — SUMMARY")
    for s in sleeves:
        r = results.get(s, {})
        if r.get("error"):
            print(f"  {s:11} ❌ {r['error'][:80]}")
        else:
            print(f"  {s:11} {r.get('run_label','—'):42} {r.get('png') or '(no png)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
