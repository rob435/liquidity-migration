#!/usr/bin/env python3
"""Problem Book F — no-order exit-timing shadow for continuous v2 (both venues).

Pre-registration:
docs/preregistration/2026-06-19-continuous-v2-f-exit-timing-shadow-construction.md

For each V2_CONTROL short trade, reconstruct the causal hourly path from entry and
simulate alternative exit rules (shorter hold / MFE-giveback / time-decay) with the
10% take-profit honored first, then account honestly for the trades each rule would
cut before they hit TP (the failure mode that killed the old daemon exits). A
random-exit rule is the negative control. NO-ORDER SHADOW: not a candidate, not
real-money evidence; the frozen v2 forward ledger is untouched.

Per-trade path shadow on klines_1h: does not re-run the daily vol-target rebalance or
re-solve concurrency under changed holds. It is the gate that decides whether a full
lifecycle Book F A/B + forward shadow is worth building.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
from pathlib import Path
from typing import Any, Callable

import polars as pl

HOUR_MS = 3_600_000
TP_PCT = 0.10            # frozen v2 component take-profit
CONTROL_HOLD_H = 24      # frozen v2 hold target (max_hold cap 48h)
PATH_HOURS = 26          # read a small buffer past the 24h hold


def _short_ret(entry: float, exit_: float) -> float:
    return (entry - exit_) / entry if entry > 0 else 0.0


def _simulate(path: list[tuple[int, float, float, float]], entry: float, rule_hour: int | None,
              rule_kind: str, mfe_arm: float, mfe_give: float, rng_hour: int | None) -> dict[str, Any]:
    """Walk the causal hourly path (h, open, high, low, close) from entry.

    Returns control and shadow exits. TP (short, entry*(1-TP_PCT)) fires intrabar when
    low <= tp and always takes precedence. Rules only ever exit at or before the 24h hold.
    """
    tp = entry * (1.0 - TP_PCT)
    min_low = float("inf")
    control: tuple[int, str, float] | None = None
    shadow: tuple[int, str, float] | None = None
    last_close = entry
    for (h, _open, _high, low, close) in path:
        min_low = min(min_low, low)
        last_close = close
        tp_fired = low <= tp
        if control is None:
            if tp_fired:
                control = (h, "tp", tp)
            elif h >= CONTROL_HOLD_H:
                control = (h, "hold", close)
        if shadow is None:
            if tp_fired:
                shadow = (h, "tp", tp)
            else:
                trig = False
                if rule_kind == "shorter_hold" and rule_hour is not None and h >= rule_hour:
                    trig = True
                elif rule_kind == "time_decay" and rule_hour is not None and h >= rule_hour:
                    trig = True
                elif rule_kind == "random" and rng_hour is not None and h >= rng_hour:
                    trig = True
                elif rule_kind == "mfe_giveback":
                    peak = _short_ret(entry, min_low)
                    now = _short_ret(entry, close)
                    if peak >= mfe_arm and now <= (1.0 - mfe_give) * peak:
                        trig = True
                if trig:
                    shadow = (h, "rule", close)
        if control is not None and shadow is not None:
            break
    if control is None:
        control = (len(path), "hold", last_close)
    if shadow is None:
        shadow = control
    return {"control": control, "shadow": shadow}


def evaluate_exit_rules(
    trades: list[dict[str, Any]],
    fetch_path: Callable[[str, int], list[tuple[int, float, float, float]] | None],
    *,
    rules: list[dict[str, Any]],
    seed: int = 0,
) -> dict[str, Any]:
    rng = random.Random(seed)
    prepared = []
    for tr in trades:
        path = fetch_path(str(tr["symbol"]), int(tr["entry_ts_ms"]))
        if not path:
            continue
        prepared.append((tr, path))
    n = len(prepared)
    out: dict[str, Any] = {"n_trades_input": len(trades), "n_with_path": n, "rules": {}}
    if n == 0:
        return out
    # control raw reconstructed from the path (validated against the recorded ledger)
    ctrl_raw: dict[int, float] = {}
    recon_err = 0.0
    control_total = 0.0
    for i, (tr, path) in enumerate(prepared):
        entry = float(tr["entry_price"])
        sim = _simulate(path, entry, None, "none", 0.0, 0.0, None)
        cr = _short_ret(entry, sim["control"][2])
        ctrl_raw[i] = cr
        recon_err += abs(cr - _short_ret(entry, float(tr["exit_price"])))
        control_total += cr * float(tr["notional_weight"]) + float(tr.get("cost_return", 0.0) or 0.0) + float(
            tr.get("funding_return", 0.0) or 0.0
        )
    out["path_control_recon_mean_abs_raw_err"] = recon_err / n
    out["control_total_net_contrib_path"] = control_total
    for rule in rules:
        kind = rule["kind"]
        name = rule["name"]
        rule_hour = rule.get("hour")
        mfe_arm = rule.get("mfe_arm", 0.0)
        mfe_give = rule.get("mfe_give", 0.0)
        delta_contrib = 0.0
        tp_cut_n = 0
        tp_cut_forgone = 0.0
        loser_saved_gain = 0.0
        n_early = 0
        for i, (tr, path) in enumerate(prepared):
            entry = float(tr["entry_price"])
            nw = float(tr["notional_weight"])
            rng_hour = rng.randint(1, CONTROL_HOLD_H) if kind == "random" else None
            sim = _simulate(path, entry, rule_hour, kind, mfe_arm, mfe_give, rng_hour)
            cexit, sexit = sim["control"], sim["shadow"]
            sraw = _short_ret(entry, sexit[2])
            craw = ctrl_raw[i]
            d = (sraw - craw) * nw
            delta_contrib += d
            if sexit[0] < cexit[0]:
                n_early += 1
                if cexit[1] == "tp":  # cut a trade that control rode to TP
                    tp_cut_n += 1
                    tp_cut_forgone += (craw - sraw) * nw
                elif d > 0:
                    loser_saved_gain += d
        out["rules"][name] = {
            "kind": kind,
            "n_exited_early": n_early,
            "net_effect_contrib": delta_contrib,
            "net_effect_pct_of_control": (delta_contrib / control_total) if abs(control_total) > 1e-12 else None,
            "tp_winners_cut_n": tp_cut_n,
            "tp_winners_cut_forgone_contrib": tp_cut_forgone,
            "losers_saved_gain_contrib": loser_saved_gain,
        }
    return out


def _date_str(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def _make_path_fetcher(klines1h_root: Path) -> Callable[[str, int], list[tuple[int, float, float, float]] | None]:
    cache: dict[tuple[str, str], pl.DataFrame | None] = {}

    def _day(symbol: str, date: str) -> pl.DataFrame | None:
        key = (symbol, date)
        if key not in cache:
            part = klines1h_root / f"date={date}" / f"symbol={symbol}"
            try:
                cache[key] = pl.read_parquet(part).select("ts_ms", "open", "high", "low", "close") if part.exists() else None
            except Exception:  # noqa: BLE001
                cache[key] = None
        return cache[key]

    def fetch(symbol: str, entry_ts_ms: int) -> list[tuple[int, float, float, float]] | None:
        end = entry_ts_ms + PATH_HOURS * HOUR_MS
        dates = {_date_str(entry_ts_ms), _date_str(end), _date_str(entry_ts_ms + HOUR_MS * 12)}
        frames = [d for d in (_day(symbol, dd) for dd in sorted(dates)) if d is not None]
        if not frames:
            return None
        df = pl.concat(frames).unique(subset=["ts_ms"]).filter(
            (pl.col("ts_ms") >= entry_ts_ms) & (pl.col("ts_ms") < end)
        ).sort("ts_ms")
        if df.is_empty():
            return None
        rows = []
        for r in df.iter_rows(named=True):
            h = int((int(r["ts_ms"]) - entry_ts_ms) // HOUR_MS) + 1
            rows.append((h, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
        return rows

    return fetch


RULES = [
    {"name": "shorter_hold_12h", "kind": "shorter_hold", "hour": 12},
    {"name": "shorter_hold_18h", "kind": "shorter_hold", "hour": 18},
    {"name": "time_decay_11h", "kind": "time_decay", "hour": 11},
    {"name": "mfe_giveback_3pct_50", "kind": "mfe_giveback", "mfe_arm": 0.03, "mfe_give": 0.5},
    {"name": "mfe_giveback_5pct_50", "kind": "mfe_giveback", "mfe_arm": 0.05, "mfe_give": 0.5},
    {"name": "random_exit_null", "kind": "random"},
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ab-root", required=True, help="A/B run root containing V2_CONTROL/<venue>/trades.csv")
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--shared-root", default=str(Path.home() / "SHARED_DATA"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    roots = {"bybit": Path(args.shared_root) / "bybit_full_pit", "binance": Path(args.shared_root) / "binance_full_pit"}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"run_label": "exploratory_shadow", "claimed_venue_scope": "both_venue_no_order_shadow", "venues": {}}
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        tp = Path(args.ab_root) / "V2_CONTROL" / venue / "trades.csv"
        trades = pl.read_csv(tp).filter(pl.col("side") == "short")
        if args.limit:
            trades = trades.head(args.limit)
        fetch = _make_path_fetcher(roots[venue] / "klines_1h")
        res = evaluate_exit_rules(trades.to_dicts(), fetch, rules=RULES, seed=args.seed)
        summary["venues"][venue] = res
    (out / "f_exit_timing_shadow.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    # compact report
    lines = ["# Continuous V2 Book F Exit-Timing Shadow (both-venue no-order)", "",
             "Shadow only; not a candidate; not real-money evidence. Pre-reg: "
             "`docs/preregistration/2026-06-19-continuous-v2-f-exit-timing-shadow-construction.md`", ""]
    for venue, res in summary["venues"].items():
        lines.append(f"## {venue} (n_with_path={res.get('n_with_path')}, recon_err={res.get('path_control_recon_mean_abs_raw_err', float('nan')):.4f})")
        lines.append("")
        lines.append("| rule | net effect (contrib) | % of control | TP winners cut | forgone (TP cut) | losers saved |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for name, r in res.get("rules", {}).items():
            pct = r["net_effect_pct_of_control"]
            lines.append(f"| {name} | {r['net_effect_contrib']:+.6f} | {'n/a' if pct is None else f'{pct:+.2%}'} | "
                         f"{r['tp_winners_cut_n']} | {r['tp_winners_cut_forgone_contrib']:+.6f} | {r['losers_saved_gain_contrib']:+.6f} |")
        lines.append("")
    (out / "f_exit_timing_shadow_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({v: {"n": r.get("n_with_path"), "rules": {k: round(x["net_effect_contrib"], 5) for k, x in r.get("rules", {}).items()}} for v, r in summary["venues"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
