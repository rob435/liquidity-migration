"""D2 Stage-A: downtrend-gated cross-sectional reversal L/S simulator.

Pre-registered in docs/preregistration/downtrend-reversal-ls-2026-06-09.md.
Vectorized daily simulator with real per-symbol funding, 12bps round-trip costs on
traded notional, causal signals (d-1 close) applied to day-d returns, regime gate =
BTC trailing 30d return through d-1 < 0.

Usage:
    PYTHONIOENCODING=utf-8 .venv/bin/python scripts/downtrend_reversal_ls.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import continuous_rs_squeeze_probe as probe  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
OUT = SHARED / "downtrend_reversal_ls_2026-06-09"
START = "2023-01-01"
COST_PER_SIDE_BPS = 6.0
ANN = 365.25
STABLES = {
    "BUSDUSDT", "DAIUSDT", "FDUSDUSDT", "FRAXUSDT", "GUSDUSDT", "LUSDUSDT", "PYUSDUSDT",
    "SUSDUSDT", "TUSDUSDT", "USD1USDT", "USDCUSDT", "USDDUSDT", "USDEUSDT", "USDPUSDT",
    "USTCUSDT", "USDYUSDT",
}
FUNDING_ROOT = {
    "bybit": SHARED / "bybit_full_pit" / "funding",
    "binance": SHARED / "binance_full_pit" / "binance_usdm_funding",
}
LIQ_FLOORS = {"liq500k": 500_000.0, "liq2m": 2_000_000.0}
SIGNALS = ("ret_7d", "blend")
MEMBERSHIPS = ("daily", "hyst")


def build_day_table(venue: str) -> pl.DataFrame:
    panel = probe.load_daily_panel(venue).filter(pl.col("date") >= START)
    p = panel.sort(["symbol", "d"]).with_columns(
        pl.col("close").shift(1).over("symbol").alias("c1"),
        pl.col("d").shift(1).over("symbol").alias("d_prev"),
        pl.col("turnover_quote").shift(1).over("symbol").alias("turn_prev"),
    )
    p = p.filter(((pl.col("d") - pl.col("d_prev")).dt.total_days() == 1) & (pl.col("c1") > 0))
    p = p.with_columns((pl.col("close") / pl.col("c1") - 1.0).alias("ret"))
    p = p.sort(["symbol", "d"]).with_columns(
        pl.col("ret").shift(1).over("symbol").alias("ret_1d"),
        (pl.col("close").shift(1) / pl.col("close").shift(8) - 1.0).over("symbol").alias("ret_7d"),
    )
    btc = p.filter(pl.col("symbol") == "BTCUSDT").select(
        "d", (pl.col("close").shift(1) / pl.col("close").shift(31) - 1.0).alias("btc30")
    )
    alts = p.filter((pl.col("symbol") != "BTCUSDT") & (~pl.col("symbol").is_in(list(STABLES))))
    alts = alts.join(btc, on="d", how="inner").drop_nulls(["btc30", "ret_7d", "ret_1d"])
    return alts.select(["symbol", "d", "ret", "ret_1d", "ret_7d", "turn_prev", "btc30"])


def load_funding(venue: str, needed: dict[str, set[str]]) -> dict[tuple[str, str], float]:
    root = FUNDING_ROOT[venue]
    out: dict[tuple[str, str], float] = {}
    for date, syms in needed.items():
        for sym in syms:
            part = root / f"date={date}" / f"symbol={sym}"
            if part.exists():
                out[(date, sym)] = float(pl.read_parquet(part, columns=["funding_rate"])["funding_rate"].sum())
    return out


def run_cell(
    days: list,
    by_day: dict,
    liq: float,
    signal: str,
    membership: str,
) -> tuple[list, dict[str, dict[str, float]]]:
    """Returns (daily rows, weights by day) — weights decided at d-1 close for day d."""
    rows = []
    weights_by_day: dict[str, dict[str, float]] = {}
    prev_w: dict[str, float] = {}
    short_hold: set[str] = set()
    long_hold: set[str] = set()
    for d in days:
        recs = by_day[d]
        regime_down = recs[0][6] < 0  # btc30 constant per day
        if not regime_down:
            w: dict[str, float] = {}
            short_hold, long_hold = set(), set()
        else:
            # row tuple: 0 symbol, 1 d, 2 ret, 3 ret_1d, 4 ret_7d, 5 turn_prev, 6 btc30
            elig = [r for r in recs if r[5] is not None and r[5] >= liq]
            if signal == "ret_7d":
                sig = {r[0]: r[4] for r in elig if r[4] is not None}
            else:  # blend of ranks(ret_1d, ret_7d)
                arr = [(r[0], r[3], r[4]) for r in elig if r[3] is not None and r[4] is not None]
                n = len(arr)
                r1 = {s: i / max(n - 1, 1) for i, (s, _, _) in enumerate(sorted(arr, key=lambda t: t[1]))}
                r7 = {s: i / max(n - 1, 1) for i, (s, _, _) in enumerate(sorted(arr, key=lambda t: t[2]))}
                sig = {s: 0.5 * r1[s] + 0.5 * r7[s] for s, _, _ in arr}
            syms = sorted(sig, key=lambda s: sig[s])
            n = len(syms)
            if n < 50:
                w = {}
                short_hold, long_hold = set(), set()
            else:
                dec = max(int(n * 0.1), 5)
                quin = max(int(n * 0.2), dec)
                top_dec, bot_dec = set(syms[-dec:]), set(syms[:dec])
                top_quin, bot_quin = set(syms[-quin:]), set(syms[:quin])
                if membership == "daily":
                    short_set, long_set = top_dec, bot_dec
                else:  # hysteresis: stay while in quintile, enter from decile
                    short_set = (short_hold & top_quin) | top_dec
                    long_set = (long_hold & bot_quin) | bot_dec
                short_hold, long_hold = set(short_set), set(long_set)
                w = {}
                for s in short_set:
                    w[s] = -0.5 / len(short_set)
                for s in long_set:
                    w[s] = w.get(s, 0.0) + 0.5 / len(long_set)
        # pnl for day d with weights w (decided from d-1 data)
        ret_map = {r[0]: r[2] for r in recs}
        gross_long = sum(wt * ret_map.get(s, 0.0) for s, wt in w.items() if wt > 0)
        gross_short = sum(wt * ret_map.get(s, 0.0) for s, wt in w.items() if wt < 0)
        turnover = sum(abs(w.get(s, 0.0) - prev_w.get(s, 0.0)) for s in set(w) | set(prev_w))
        cost = -turnover * COST_PER_SIDE_BPS / 1e4
        rows.append({"d": d, "regime_down": regime_down, "gross_long": gross_long, "gross_short": gross_short, "cost": cost, "turnover": turnover, "n_pos": len(w)})
        weights_by_day[str(d)] = dict(w)
        prev_w = w
    return rows, weights_by_day


def attach_funding_and_score(rows: list, weights_by_day: dict, fund_map: dict, *, cost_mult: float = 1.0, funding_on: bool = True) -> dict:
    daily = []
    for r in rows:
        w = weights_by_day[str(r["d"])]
        date = str(r["d"])
        fund_pnl = -sum(wt * fund_map.get((date, s), 0.0) for s, wt in w.items()) if funding_on else 0.0
        net = r["gross_long"] + r["gross_short"] + r["cost"] * cost_mult + fund_pnl
        daily.append((r["d"], r["regime_down"], net, r["gross_long"], r["gross_short"], fund_pnl, r["cost"] * cost_mult, r["turnover"]))
    reg = [x for x in daily if x[1]]
    net = np.array([x[2] for x in reg])
    eq = np.cumprod(1 + net)
    peak = np.maximum.accumulate(eq)
    dd = float((eq / peak - 1).min()) if len(eq) else 0.0
    years = {}
    for x in reg:
        years.setdefault(x[0].year, []).append(x[2])
    per_year = {str(y): round(float(np.sum(v)) * 100, 2) for y, v in years.items() if len(v) > 60}
    return {
        "regime_days": len(reg),
        "net_ret_pct": round(float(eq[-1] - 1) * 100, 2) if len(eq) else 0.0,
        "sharpe": round(float(net.mean() / net.std() * np.sqrt(ANN)), 2) if len(net) > 2 and net.std() > 0 else 0.0,
        "dd_pct": round(dd * 100, 2),
        "gross_long_pct": round(sum(x[3] for x in reg) * 100, 2),
        "gross_short_pct": round(sum(x[4] for x in reg) * 100, 2),
        "funding_pct": round(sum(x[5] for x in reg) * 100, 2),
        "cost_pct": round(sum(x[6] for x in reg) * 100, 2),
        "avg_turnover": round(float(np.mean([x[7] for x in reg])), 2) if reg else 0.0,
        "per_year_net_pct": per_year,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {"preregistration": "docs/preregistration/downtrend-reversal-ls-2026-06-09.md"}
    for venue in ["bybit", "binance"]:
        tbl = build_day_table(venue)
        days = sorted(tbl["d"].unique().to_list())
        by_day: dict = defaultdict(list)
        for r in tbl.iter_rows():  # symbol, d, ret, ret_1d, ret_7d, turn_prev, btc30
            by_day[r[1]].append(r)
        print(f"[{venue}] {len(days)} days, {tbl.height} rows")

        cells: dict = {}
        all_weights: dict = {}
        needed: dict[str, set] = defaultdict(set)
        for liq_name, liq in LIQ_FLOORS.items():
            for signal in SIGNALS:
                for membership in MEMBERSHIPS:
                    cid = f"{liq_name}_{signal}_{membership}"
                    rows, wbd = run_cell(days, by_day, liq, signal, membership)
                    cells[cid] = rows
                    all_weights[cid] = wbd
                    for dstr, w in wbd.items():
                        if w:
                            needed[dstr].update(w.keys())
        n_pairs = sum(len(v) for v in needed.values())
        print(f"[{venue}] loading funding for {n_pairs} (date,symbol) pairs ...")
        fund_map = load_funding(venue, needed)
        print(f"[{venue}] funding loaded: {len(fund_map)} pairs found")

        vres = {}
        for cid, rows in cells.items():
            vres[cid] = attach_funding_and_score(rows, all_weights[cid], fund_map)
        report[venue] = vres
        report[f"{venue}_sens"] = {}
        print(json.dumps(vres, indent=2))
    with open(OUT / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"wrote {OUT / 'report.json'}")


if __name__ == "__main__":
    main()
