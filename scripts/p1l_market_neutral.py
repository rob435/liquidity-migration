"""Phase 1 / Avenue C — a MARKET-NEUTRAL continuous L/S on the liquid universe (an untried angle).

Every portfolio proxy so far was SHORT-ONLY (short-beta risk; flattered by the recent alt-bear). The mission
flags "a market-neutral expression" as untried. A beta-neutral L/S (long bottom-composite D0, short top-
composite D9, within the LIQUID rmom-low universe, dollar-neutral) removes market beta — potentially lower DD
/ higher MAR, and uncorrelated to the directional daily short. This is something the event-driven daily short
CAN'T easily be. Test: per-ts L/S vs short-only on liquid names (cached panel + kline turnover), same method,
additive daily series -> MAR/DD/Sharpe, both venues, early/recent. If L/S MAR > short-only MAR -> the
market-neutral continuous book is a genuinely better (uncorrelated) product. EXPLORATORY; NOT promotion evidence.

Dispatch: POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/p1l_market_neutral.py
"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.signal_harness import (  # noqa: E402
    _autodetect_dataset_names,
    _date_str_to_ms,
    _read_window,
)

SHARED = Path.home() / "SHARED_DATA"
START_DATE, END_DATE = "2023-04-01", "2026-05-28"
MS_DAY, MS_H = 86_400_000, 3_600_000
SPLIT = _date_str_to_ms("2025-06-01")
LIQ = 500_000
COST = 30 / 1e4
YEARS = (_date_str_to_ms(END_DATE) - _date_str_to_ms(START_DATE)) / (365.25 * MS_DAY)
VENUES = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}


def _daily_stats(daily: dict[int, float]) -> dict:
    if not daily:
        return {"n": 0}
    days = sorted(daily)
    pnl = [daily[d] for d in days]
    eq = peak = mdd = 0.0
    for p in pnl:
        eq += p
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    ann = eq / YEARS
    sharpe = (st.mean(pnl) / st.pstdev(pnl) * (365 ** 0.5)) if len(pnl) > 2 and st.pstdev(pnl) > 0 else None
    return {"ann_ret_pct": round(ann * 100, 1), "max_dd_pct": round(mdd * 100, 2),
            "mar": round(ann / mdd, 2) if mdd > 0 else None, "sharpe": round(sharpe, 2) if sharpe else None,
            "early_pct": round(sum(daily[d] for d in days if d < SPLIT) * 100, 1),
            "recent_pct": round(sum(daily[d] for d in days if d >= SPLIT) * 100, 1)}


def _series(perts: pl.DataFrame, col: str) -> dict:
    """per-ts return col -> daily-mean additive series stats."""
    d = perts.with_columns(((pl.col("ts_ms") // MS_DAY) * MS_DAY).alias("day")).group_by("day").agg(pl.col(col).mean().alias("r"))
    return _daily_stats({int(r["day"]): float(r["r"]) for r in d.iter_rows(named=True)})


def main() -> int:
    out: dict = {}
    for venue, root in VENUES.items():
        if not (root / "_p1_continuous_panel.parquet").exists():
            print(f"SKIP {venue}")
            continue
        print(f"[{venue}] cached panel + kline turnover ...", flush=True)
        kname = _autodetect_dataset_names(root)["klines_dataset"]
        k = _read_window(root, kname, start_ms=_date_str_to_ms(START_DATE), end_ms=_date_str_to_ms(END_DATE),
                         columns=["ts_ms", "symbol", "turnover_quote"])
        panel = pl.read_parquet(root / "_p1_continuous_panel.parquet")  # symbol, ts_ms, decile, fwd(24h)
        panel = panel.join(k, on=["symbol", "ts_ms"], how="left").filter(pl.col("turnover_quote") >= LIQ)
        # per-ts: mean fwd for D9 (top, short) and D0 (bottom, long)
        dt = panel.filter(pl.col("decile").is_in([0, 9])).group_by(["ts_ms", "decile"]).agg(
            pl.col("fwd").mean().alias("m"), pl.len().alias("n"))
        top = dt.filter(pl.col("decile") == 9).select("ts_ms", pl.col("m").alias("d9"), pl.col("n").alias("n9"))
        bot = dt.filter(pl.col("decile") == 0).select("ts_ms", pl.col("m").alias("d0"), pl.col("n").alias("n0"))
        j = top.join(bot, on="ts_ms", how="inner")
        # short-only net (1 leg) and beta-neutral L/S net (2 legs)
        j = j.with_columns((-pl.col("d9") - COST).alias("short_ret"),
                           (pl.col("d0") - pl.col("d9") - 2 * COST).alias("ls_ret"))
        res = {"liquid_D9_per_ts": int(j["n9"].mean()), "liquid_D0_per_ts": int(j["n0"].mean()), "n_ts": j.height,
               "short_only": _series(j, "short_ret"), "market_neutral_LS": _series(j, "ls_ret")}
        out[venue] = res
        print(f"[{venue}] liquid D9/ts={res['liquid_D9_per_ts']} D0/ts={res['liquid_D0_per_ts']} n_ts={res['n_ts']}", flush=True)
        for arm in ("short_only", "market_neutral_LS"):
            r = res[arm]
            print(f"[{venue}]  {arm:18s} MAR={r.get('mar')} ann={r.get('ann_ret_pct')}%/yr DD={r.get('max_dd_pct')}% "
                  f"Sh={r.get('sharpe')} early={r.get('early_pct')}% recent={r.get('recent_pct')}%", flush=True)
        print(flush=True)
    (SHARED / "p1l_market_neutral_2026-06-01.json").write_text(json.dumps(out, indent=2))
    print("DONE -> p1l_market_neutral_2026-06-01.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
