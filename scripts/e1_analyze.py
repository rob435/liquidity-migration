"""E1 execution-premium analysis (read-only).

Pre-reg: docs/preregistration/e1-execution-premium-2026-05-29.md

For each venue, loads the A (fixed_delay / 00_baseline) and B
(promoted_quality_squeeze / 01_quality_squeeze) trade ledgers from a sweep tag and:

  1. Portfolio headline (return / DD / MAR / Sharpe / trades) — from the report JSON.
  2. B's entry_rule distribution (how often the fade-confirmation path actually fires).
  3. PAIRED-TRADE micro-test: match A and B trades on (symbol, entry_signal_ts_ms).
     Most pairs enter identically; the *divergent* pairs (B waited for a giveback)
     are where the execution-timing signal lives. Compare net_return immediate (A)
     vs giveback (B) on exactly those names — far higher power than the
     91%-identical portfolio aggregate.

Usage:
    .venv/bin/python scripts/e1_analyze.py --sweep-tag e1_exec_premium_2026-05-29
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics as st
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

SHARED = Path.home() / "SHARED_DATA"
VENUES = {"bybit": "bybit_full_pit", "binance": "binance_full_pit"}


def _report(cell_dir: Path) -> dict:
    d = json.loads((cell_dir / "volume_event_research_report.json").read_text())
    b = d.get("best_scenario", {})
    ret = float(b.get("total_return", 0.0))
    dd = float(b.get("max_drawdown", 0.0))
    return {
        "ret": ret, "dd": dd, "mar": ret / abs(dd) if dd else float("nan"),
        "sharpe": float(b.get("sharpe_like", b.get("sharpe", 0.0))),
        "trades": int(b.get("trades", 0)),
        "full_pit": bool(d.get("pit_manifest", {}).get("full_pit_universe_pass", False)),
    }


def _ledger(cell_dir: Path) -> dict[tuple, dict]:
    """Keyed by (symbol, entry_signal_ts_ms)."""
    out = {}
    p = cell_dir / "volume_event_best_trades.csv"
    if not p.exists():
        return out
    for r in csv.DictReader(open(p)):
        key = (r["symbol"], r["entry_signal_ts_ms"])
        out[key] = r
    return out


def _f(r, k):
    try:
        return float(r[k])
    except (KeyError, ValueError, TypeError):
        return float("nan")


def analyze(venue: str, root: Path, tag: str) -> None:
    base = root / "reports" / tag
    a_dir = base / "00_baseline"
    if not (a_dir / "volume_event_research_report.json").exists():
        print(f"\n### {venue}: A cell missing ({a_dir}) — skip")
        return
    # treatment = the one non-control cell with a report (01_quality_squeeze,
    # 01_engage_all, ...). Pick the lexicographically-first such dir.
    cand = sorted(d for d in base.glob("01_*")
                  if (d / "volume_event_research_report.json").exists())
    if not cand:
        print(f"\n### {venue}: treatment cell missing under {base} — skip")
        return
    b_dir = cand[0]
    ra, rb = _report(a_dir), _report(b_dir)
    print(f"\n================ {venue} ================")
    print(f"A fixed_delay      : ret={ra['ret']:+.4f} DD={ra['dd']:+.4f} MAR={ra['mar']:+.3f} "
          f"Sh={ra['sharpe']:+.3f} n={ra['trades']} pit={ra['full_pit']}")
    print(f"B quality_squeeze  : ret={rb['ret']:+.4f} DD={rb['dd']:+.4f} MAR={rb['mar']:+.3f} "
          f"Sh={rb['sharpe']:+.3f} n={rb['trades']} pit={rb['full_pit']}")
    print(f"PREMIUM B-A        : ret {rb['ret']-ra['ret']:+.4f}  MAR {rb['mar']-ra['mar']:+.3f}  "
          f"Sh {rb['sharpe']-ra['sharpe']:+.3f}")

    la, lb = _ledger(a_dir), _ledger(b_dir)
    import collections
    er = collections.Counter(r.get("entry_rule", "?") for r in lb.values())
    print(f"B entry_rule       : {dict(er)}")

    # Paired micro-test on divergent entries
    common = set(la) & set(lb)
    only_a, only_b = set(la) - set(lb), set(lb) - set(la)
    divergent = []  # (key, a_row, b_row) where entry timestamp differs
    for k in common:
        if la[k]["entry_ts_ms"] != lb[k]["entry_ts_ms"]:
            divergent.append((k, la[k], lb[k]))
    print(f"pairing            : common={len(common)} only_A={len(only_a)} only_B={len(only_b)} "
          f"divergent_entry={len(divergent)}")

    if divergent:
        a_net = [_f(a, "net_return") for _, a, _ in divergent]
        b_net = [_f(b, "net_return") for _, _, b in divergent]
        a_gross = [_f(a, "gross_return") for _, a, _ in divergent]
        b_gross = [_f(b, "gross_return") for _, _, b in divergent]
        dly = [_f(b, "actual_entry_delay_hours") for _, _, b in divergent]
        diff = [bn - an for an, bn in zip(a_net, b_net)]
        print(f"  divergent trades (B waited for giveback) — paired immediate(A) vs giveback(B):")
        print(f"    A net mean={st.mean(a_net):+.5f}  B net mean={st.mean(b_net):+.5f}  "
              f"Δ(B-A) mean={st.mean(diff):+.5f}  sum={sum(diff):+.4f}")
        print(f"    A gross mean={st.mean(a_gross):+.5f}  B gross mean={st.mean(b_gross):+.5f}")
        print(f"    B entry delay on divergent: med={st.median(dly):.2f}h max={max(dly):.2f}h")
        wins = sum(1 for x in diff if x > 0)
        print(f"    B-better on {wins}/{len(diff)} divergent trades ({100*wins/len(diff):.0f}%)")
        if len(diff) > 1:
            tval = st.mean(diff) / (st.pstdev(diff) / math.sqrt(len(diff))) if st.pstdev(diff) else float("nan")
            print(f"    paired t-stat (Δnet)≈ {tval:+.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep-tag", required=True)
    args = ap.parse_args()
    for venue, sub in VENUES.items():
        analyze(venue, SHARED / sub, args.sweep_tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
