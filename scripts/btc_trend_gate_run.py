#!/usr/bin/env python3
"""Run the DEPLOYED SHORT profile ± the BTC-30d-trend regime gate and emit the
equity-vs-BTC PNG + trade ledger. EXPLORATORY scouting for the gate's pre-reg
(docs/preregistration/2026-06-04-btc-trend-regime-gate.md) — NOT promotion evidence.

The profile is `liquidity_migration.promoted.short_profile` (the exact live fade);
only `btc_trend_gate` is overridden, so "off" reproduces the deployed baseline and
"uptrend"/"downtrend" partition it by regime. Run both to read the gate's effect.

    .venv/bin/python scripts/btc_trend_gate_run.py --gate uptrend \
        --root ~/SHARED_DATA/bybit_full_pit --start 2023-04-01 --end 2026-05-28
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration import promoted  # noqa: E402
from liquidity_migration.config import load_config  # noqa: E402
from liquidity_migration.volume_events import run_volume_event_research  # noqa: E402

DEFAULT_ROOT = "~/SHARED_DATA/bybit_full_pit"
DEFAULT_CONFIG = "configs/volume_alpha.default.yaml"


def _find(out: Path, *patterns: str) -> Path | None:
    for pat in patterns:
        hits = sorted(out.rglob(pat))
        if hits:
            return hits[-1]
    return None


def _metric(summary: dict, *keys: str):
    for k in keys:
        if k in summary and isinstance(summary[k], (int, float)):
            return summary[k]
    return None


def _delisted_traded(ledger: Path | None, root: str) -> int | None:
    """Count traded symbols absent from the last 30d of klines. >0 PROVES the run used the
    delisted-inclusive PIT universe (no survivorship) — the honest read of a
    current_universe label. Mirrors scripts/equity_curves.py._delisted_traded."""
    import glob
    import os

    import polars as pl
    kroot = os.path.join(os.path.expanduser(root), "klines_1h")
    if not ledger or not os.path.isdir(kroot):
        return None
    try:
        syms = set(pl.read_csv(ledger)["symbol"].unique().to_list())
    except Exception:  # noqa: BLE001
        return None
    recent: set[str] = set()
    for d in sorted(os.listdir(kroot))[-30:]:
        for s in glob.glob(os.path.join(kroot, d, "symbol=*")):
            recent.add(s.split("symbol=")[-1])
    return len(syms - recent)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gate", choices=("off", "uptrend", "downtrend"), required=True)
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--out", default=None, help="Report dir (default <root>/reports/btc_trend_gate/<gate>).")
    args = p.parse_args()

    root = str(Path(args.root).expanduser())
    out = Path(args.out).expanduser() if args.out else Path(root) / "reports" / "btc_trend_gate" / args.gate
    out.mkdir(parents=True, exist_ok=True)
    costs = load_config(args.config).costs

    cfg = replace(promoted.short_profile(start=args.start, end=args.end), btc_trend_gate=args.gate)
    print(f"[btc_trend_gate] gate={args.gate} root={root} window {args.start}->{args.end}", flush=True)
    payload = run_volume_event_research(root, event_config=cfg, cost_config=costs, report_dir=out)

    summary = payload.get("best_scenario") or payload.get("summary") or payload.get("metrics") or {}
    label = payload.get("run_label") or summary.get("run_label") or "—"
    ret = _metric(summary, "total_return")
    dd = _metric(summary, "max_drawdown")
    sharpe = _metric(summary, "sharpe_like", "sharpe")
    trades = _metric(summary, "trades", "n_trades", "num_trades")
    full_pit = summary.get("full_pit_universe_pass")
    png = _find(out, "*equity*btc*.png", "*equity*.png")
    ledger = _find(out, "*best_trades.csv", "*trades*.csv")
    delisted = _delisted_traded(ledger, root)
    # return/|DD| ratio: a quick read, NOT the Tier-2 MAR (that comes from r1_robustness.py).
    ret_dd = (ret / abs(dd)) if isinstance(ret, (int, float)) and isinstance(dd, (int, float)) and dd else None

    def _pct(x):
        return f"{x:+.1%}" if isinstance(x, (int, float)) else "—"

    def _f(x, fmt="{:.2f}"):
        return fmt.format(x) if isinstance(x, (int, float)) else "—"

    pit_read = ""
    if full_pit is False and isinstance(delisted, int):
        pit_read = (
            f"  → {delisted} delisted names traded ⇒ delisted-inclusive, NO survivorship "
            "(label conservative)" if delisted > 0 else
            "  → 0 delisted names traded ⇒ POSSIBLE survivorship; treat as biased"
        )

    print("\n=== RESULT ===")
    print(f"  run_label : {label}")
    print(f"  full_pit  : {full_pit}{pit_read}")
    print(f"  trades    : {_f(trades, '{:.0f}')}")
    print(f"  return    : {_pct(ret)}")
    print(f"  max_DD    : {_pct(dd)}")
    print(f"  ret/|DD|  : {_f(ret_dd)}   (quick read; not the Tier-2 MAR)")
    print(f"  Sharpe    : {_f(sharpe)}")
    print(f"  PNG       : {png or '(none)'}")
    print(f"  ledger    : {ledger or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
