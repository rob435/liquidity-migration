"""TA2: funding & cross-book entry context — atlas follow-on (3 fixed features).

Pre-registered in docs/preregistration/trade-atlas-2-funding-2026-06-11.md.
The LAST atlas on this window regardless of outcome.

Usage:
    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python scripts/trade_atlas2.py
"""

from __future__ import annotations

import bisect
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
from trade_atlas import LEDGERS, OUT, boot_median_diff  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
FUNDING = {"bybit": SHARED / "bybit_full_pit" / "funding",
           "binance": SHARED / "binance_full_pit" / "binance_usdm_funding"}
MS_30D = 30 * 86_400_000


def load_trades() -> pl.DataFrame:
    frames = []
    for name, (venue, book, path) in LEDGERS.items():
        if not path.exists():
            continue
        t = pl.read_csv(path)
        cols = {c.lower(): c for c in t.columns}
        frames.append(t.select(
            pl.lit(name).alias("ledger"), pl.lit(venue).alias("venue"), pl.lit(book).alias("book"),
            pl.col(cols["symbol"]).cast(pl.Utf8).alias("symbol"),
            pl.col(cols["entry_ts_ms"]).cast(pl.Int64).alias("ets"),
            pl.col(cols["net_return"]).cast(pl.Float64).alias("net"),
        ))
    return pl.concat(frames)


def funding_series(venue: str, symbols: set[str]) -> dict[str, tuple[list[int], list[float]]]:
    """Per symbol: sorted (ts list, 8h-equivalent rate list)."""
    lf = pl.scan_parquet(str(FUNDING[venue] / "date=*" / "symbol=*" / "*.parquet"))
    cols = lf.collect_schema().names()
    rate = (pl.col("funding_rate_8h_equiv") if "funding_rate_8h_equiv" in cols
            else pl.col("funding_rate") * (480.0 / pl.col("funding_interval_min").clip(1)))
    df = (lf.filter(pl.col("symbol").is_in(list(symbols)))
            .select("symbol", "ts_ms", rate.alias("r8h"))
            .collect().sort(["symbol", "ts_ms"]))
    out: dict[str, tuple[list[int], list[float]]] = {}
    for sym, g in df.group_by("symbol"):
        key = sym[0] if isinstance(sym, tuple) else sym
        out[str(key)] = (g["ts_ms"].to_list(), g["r8h"].to_list())
    return out


def main() -> None:
    trades = load_trades()
    rows = []
    other_entries: dict[tuple[str, str, str], list[int]] = {}
    for r in trades.iter_rows(named=True):
        other_entries.setdefault((r["venue"], r["book"], r["symbol"]), []).append(r["ets"])
    for v in other_entries.values():
        v.sort()
    fcache: dict[str, dict] = {}
    for venue in ("bybit", "binance"):
        syms = set(trades.filter(pl.col("venue") == venue)["symbol"].unique().to_list())
        if syms:
            fcache[venue] = funding_series(venue, syms)
    for r in trades.iter_rows(named=True):
        ts_list, rate_list = fcache[r["venue"]].get(r["symbol"], ([], []))
        i = bisect.bisect_left(ts_list, r["ets"])  # rates strictly before entry
        f_at, f_pct = None, None
        if i > 0:
            f_at = rate_list[i - 1]
            j = bisect.bisect_left(ts_list, r["ets"] - MS_30D)
            window = rate_list[j:i]
            if len(window) >= 30:
                f_pct = float(np.mean(np.asarray(window) <= f_at))
        other_book = "long" if r["book"] == "short" else "short"
        ob = other_entries.get((r["venue"], other_book, r["symbol"]), [])
        k = bisect.bisect_left(ob, r["ets"])
        ob_recent = k > 0 and (r["ets"] - ob[k - 1]) <= MS_30D
        rows.append({**r, "win": r["net"] > 0, "funding_at_entry": f_at,
                     "funding_pctile_30d": f_pct, "otherbook_recent": ob_recent})
    atlas = pl.DataFrame(rows)
    atlas.write_parquet(OUT / "atlas2.parquet")

    report: dict = {"preregistration": "docs/preregistration/trade-atlas-2-funding-2026-06-11.md"}
    for name in atlas["ledger"].unique().to_list():
        sub = atlas.filter(pl.col("ledger") == name)
        res: dict = {"n": sub.height}
        for f in ("funding_at_entry", "funding_pctile_30d"):
            s = sub.filter(pl.col(f).is_not_null())
            w = s.filter(pl.col("win"))[f].to_numpy().astype(float)
            l = s.filter(~pl.col("win"))[f].to_numpy().astype(float)  # noqa: E741
            res[f] = boot_median_diff(w, l)
        buckets = {}
        for key, g in sub.group_by("otherbook_recent"):
            k = key[0] if isinstance(key, tuple) else key
            wr = float(g["win"].mean())
            buckets[str(k)] = {"n": g.height, "win_rate": round(wr, 3),
                               "se": round((wr * (1 - wr) / g.height) ** 0.5, 3) if g.height else None,
                               "mean_net": round(float(g["net"].mean()), 5)}
        res["otherbook_recent"] = buckets
        report[name] = res
    with open(OUT / "report2.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(json.dumps(report, indent=1, default=str))
    print(f"wrote {OUT / 'report2.json'}")


if __name__ == "__main__":
    main()
