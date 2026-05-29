"""R1 robustness analyzer — sub-period stability + concentration + bootstrap.

Reads the per-cell ledgers a sweep dropped under
``~/SHARED_DATA/{venue}_full_pit/reports/<sweep_tag>/<cell>/`` and, for every
non-control cell vs the ``00_baseline`` control, reports:

  * full-window compounded return + monthly-resolution max drawdown + MAR
  * sub-period thirds (compounded return per third) + positive-third check
  * monthly-delta concentration: share of the positive-side monthly delta
    contributed by the top-k months (how few months carry the edge)
  * leave-one-month-out: the single most load-bearing month — does removing
    it flip the sign of the return delta?
  * block-bootstrap CI on the annualized-return delta and MAR delta
    (resample 3-month blocks with replacement; report p5/p50/p95 and
    P(delta > 0))

Native engine sub-period splits are YAML-only and the config is no-touch, so
this computes stability from the monthly P&L ledger instead. Monthly-resolution
DD is coarser than the engine's daily DD but is the right granularity for
"how much does the edge depend on a handful of months".

Usage:
    .venv/bin/python scripts/r1_robustness.py \\
        --sweep-tag r1_filter_audit_2026-05-28 \\
        [--control 00_baseline] [--n-boot 5000] [--block 3] [--seed 0]
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from pathlib import Path

# Windows cp1252 stdout crashes on the box-drawing / Δ / ⚠️ chars this script
# prints; force UTF-8 (matches scripts/_sweep_runtime.py).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

SHARED = Path.home() / "SHARED_DATA"
VENUES = {"bybit": "bybit_full_pit", "binance": "binance_full_pit"}


def _load_monthly(cell_dir: Path) -> list[tuple[str, float]]:
    p = cell_dir / "volume_event_best_monthly.csv"
    rows: list[tuple[str, float]] = []
    with open(p) as f:
        for d in csv.DictReader(f):
            rows.append((d["month"], float(d["strategy_return"])))
    rows.sort(key=lambda r: r[0])
    return rows


def _load_json_metrics(cell_dir: Path) -> dict:
    try:
        d = json.loads((cell_dir / "volume_event_research_report.json").read_text())
    except (OSError, ValueError) as exc:
        # An OOM-killed / interrupted cell leaves a missing or truncated report
        # (json.JSONDecodeError is a ValueError). Surface it as a per-cell
        # load_error so the caller skips just that cell instead of crashing the
        # whole multi-cell robustness run on one bad file.
        return {
            "total_return": 0.0, "max_drawdown": 0.0, "sharpe_like": 0.0,
            "trades": 0, "start": None, "end": None,
            "full_pit_pass": False, "run_label": "",
            "load_error": f"{type(exc).__name__}: {exc}",
        }
    b = d.get("best_scenario", {})
    dr = d.get("date_range", {})
    pit = d.get("pit_manifest", {})
    return {
        "total_return": float(b.get("total_return", 0.0)),
        "max_drawdown": float(b.get("max_drawdown", 0.0)),
        "sharpe_like": float(b.get("sharpe_like", b.get("sharpe", 0.0))),
        "trades": int(b.get("trades", 0)),
        "start": dr.get("start"),
        "end": dr.get("end"),
        # PIT integrity: a run is clean evidence only if it covered the full PIT
        # universe. A partial-PIT run is current-universe (survivorship) biased.
        "full_pit_pass": bool(pit.get("full_pit_universe_pass", False)),
        "run_label": str(d.get("run_label", "")),
        "load_error": None,
    }


def _window_years(metrics: dict) -> float:
    """True backtest span in years from the JSON date_range (so MAR
    annualization matches the engine window, not the count of active months)."""
    from datetime import date
    try:
        s = date.fromisoformat(metrics["start"])
        e = date.fromisoformat(metrics["end"])
        return max((e - s).days / 365.25, 1e-9)
    except (TypeError, ValueError):
        return 1.0


def _compound(returns: list[float]) -> float:
    eq = 1.0
    for r in returns:
        eq *= (1.0 + r)
    return eq - 1.0


def _max_dd_monthly(returns: list[float]) -> float:
    """Monthly-resolution max drawdown from a sequence of monthly returns."""
    eq = 1.0
    peak = 1.0
    worst = 0.0
    for r in returns:
        eq *= (1.0 + r)
        peak = max(peak, eq)
        worst = min(worst, eq / peak - 1.0)
    return worst


def _annualize(total_return: float, n_months: int) -> float:
    if n_months <= 0:
        return 0.0
    years = n_months / 12.0
    return (1.0 + total_return) ** (1.0 / years) - 1.0


def _mar(returns: list[float]) -> float:
    n = len(returns)
    ann = _annualize(_compound(returns), n)
    dd = abs(_max_dd_monthly(returns))
    # A ~zero-drawdown series has UNMEASURABLE risk-adjusted return (MAR -> inf is a
    # divide-by-~0 artifact, not "infinitely good"). Return nan, not inf, so it is
    # excluded by the math.isfinite() guards rather than spuriously dominating /
    # passing a MAR gate.
    return ann / dd if dd > 1e-9 else float("nan")


def _thirds(months: list[str], cell_r: list[float], base_r: list[float]) -> list[dict]:
    n = len(months)
    k = n // 3
    bounds = [(0, k), (k, 2 * k), (2 * k, n)]
    out = []
    for a, b in bounds:
        out.append({
            "label": f"{months[a]}..{months[b - 1]}",
            "base": _compound(base_r[a:b]),
            "cell": _compound(cell_r[a:b]),
        })
    return out


def _concentration(cell_r: list[float], base_r: list[float], months: list[str], top_k: int = 3) -> dict:
    deltas = sorted(
        ((cell_r[i] - base_r[i], months[i]) for i in range(len(months))),
        reverse=True,
    )
    pos_sum = sum(d for d, _ in deltas if d > 0)
    top = deltas[:top_k]
    top_sum = sum(d for d, _ in top)
    total_delta = sum(d for d, _ in deltas)
    return {
        "total_delta": total_delta,
        "pos_sum": pos_sum,
        "top_k": top_k,
        "top_sum": top_sum,
        "top_share_of_pos": (top_sum / pos_sum) if pos_sum > 1e-9 else float("nan"),
        "top_months": [(m, round(d, 4)) for d, m in top],
    }


def _leave_one_out(cell_r: list[float], base_r: list[float], months: list[str]) -> dict:
    full = _compound(cell_r) - _compound(base_r)
    worst_label, worst_delta_wo = None, None
    for i in range(len(months)):
        cr = cell_r[:i] + cell_r[i + 1:]
        br = base_r[:i] + base_r[i + 1:]
        d_wo = _compound(cr) - _compound(br)
        if worst_delta_wo is None or d_wo < worst_delta_wo:
            worst_delta_wo, worst_label = d_wo, months[i]
    return {
        "full_return_delta": full,
        "min_loo_return_delta": worst_delta_wo,
        "most_loadbearing_month": worst_label,
        "loo_flips_sign": (full > 0) and (worst_delta_wo is not None and worst_delta_wo <= 0),
    }


def _block_bootstrap(cell_r: list[float], base_r: list[float], *, n_boot: int, block: int, seed: int) -> dict:
    rng = random.Random(seed)
    n = len(cell_r)
    ann_deltas, mar_deltas = [], []
    for _ in range(n_boot):
        idx: list[int] = []
        while len(idx) < n:
            start = rng.randrange(0, n)
            idx.extend((start + j) % n for j in range(block))
        idx = idx[:n]
        cr = [cell_r[i] for i in idx]
        br = [base_r[i] for i in idx]
        ann_deltas.append(_annualize(_compound(cr), n) - _annualize(_compound(br), n))
        mar_deltas.append(_mar(cr) - _mar(br))

    def pct(xs: list[float], q: float) -> float:
        ys = sorted(v for v in xs if math.isfinite(v))
        if not ys:
            return float("nan")
        pos = q * (len(ys) - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(ys) - 1)
        return ys[lo] + (ys[hi] - ys[lo]) * (pos - lo)

    return {
        "ann_delta_p5": pct(ann_deltas, 0.05),
        "ann_delta_p50": pct(ann_deltas, 0.50),
        "ann_delta_p95": pct(ann_deltas, 0.95),
        "ann_delta_p_gt0": sum(1 for d in ann_deltas if d > 0) / len(ann_deltas),
        "mar_delta_p5": pct(mar_deltas, 0.05),
        "mar_delta_p50": pct(mar_deltas, 0.50),
        "mar_delta_p95": pct(mar_deltas, 0.95),
        "mar_delta_p_gt0": sum(1 for d in mar_deltas if math.isfinite(d) and d > 0) / len(mar_deltas),
    }


def _engine_mar(total_return: float, max_drawdown: float, years: float) -> float:
    """MAR = annualized return / |daily max DD|, matching the STATE/doc numbers
    (uses the engine's reported daily max_drawdown + the true window span, not
    the monthly-resolution DD / active-month count the bootstrap uses)."""
    dd = abs(max_drawdown)
    ann = (1.0 + total_return) ** (1.0 / years) - 1.0
    # nan (not inf) for a ~zero-DD cell: MAR is a divide-by-~0 artifact there, and
    # inf would let a degenerate (e.g. too-few-trades, no down day) cell spuriously
    # clear the pooled-MAR demo-eligibility gate. _tier2_verdict treats a non-finite
    # MAR Δ as non-eligible.
    return ann / dd if dd > 1e-9 else float("nan")


def _tier2_verdict(by_d: float, bn_d: float, pooled_d: float, *,
                   by_ret: float, bn_ret: float, by_dd: float, bn_dd: float,
                   by_tr: int, bn_tr: int, full_pit: bool = True) -> str:
    """Tier 2 Demo-candidate bar (see Round 2 pre-reg 'Decision framework')."""
    # PIT integrity gate, FIRST: a partial-PIT run is current-universe
    # (survivorship) biased and is not valid evidence regardless of how good the
    # numbers look. Requires BOTH the cell and the control to have run full-PIT.
    if not full_pit:
        return "INVALID (partial-PIT — current-universe/survivorship biased)"
    # MAR observability gate: a non-finite MAR Δ means a venue's drawdown was
    # ~zero (MAR undefined — degenerate/too-few-trades), so the cell carries no
    # measurable risk-adjusted-return evidence and must not be demo-eligible.
    if not (math.isfinite(by_d) and math.isfinite(bn_d) and math.isfinite(pooled_d)):
        return "descriptive (MAR undefined — ~zero DD a venue, unmeasurable)"
    # Falsify
    if by_ret <= 0 or bn_ret <= 0:
        return "FALSIFY (return ≤0 a venue)"
    if pooled_d <= 0:
        return "FALSIFY (pooled MAR Δ ≤0)"
    if by_dd < -0.70 or bn_dd < -0.70:
        return "FALSIFY (DD >70% a venue)"
    # Demo-eligible
    if pooled_d > 0.1 and min(by_d, bn_d) >= -0.5 and by_tr >= 30 and bn_tr >= 20:
        return "DEMO-ELIGIBLE"
    return "descriptive"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep-tag", required=True)
    ap.add_argument("--control", default="00_baseline")
    ap.add_argument("--n-boot", type=int, default=5000)
    ap.add_argument("--block", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    # collected[cell][venue] = dict(mar_d, ret, dd, trades, full_pit) for the
    # cross-venue Tier 2 verdict printed after the per-venue fragility sections.
    collected: dict[str, dict[str, dict]] = {}
    # control_full_pit[venue] — a cell is valid Tier-2 evidence only if BOTH it
    # and the control ran full-PIT on BOTH venues.
    control_full_pit: dict[str, bool] = {}

    for venue, root_name in VENUES.items():
        sweep_root = SHARED / root_name / "reports" / args.sweep_tag
        if not sweep_root.exists():
            print(f"SKIP {venue}: {sweep_root} not found")
            continue
        control_dir = sweep_root / args.control
        base_json = _load_json_metrics(control_dir)
        if base_json.get("load_error"):
            print(f"SKIP {venue}: control ({args.control}) report unreadable "
                  f"({base_json['load_error']}) — cannot evaluate this venue.")
            continue
        base_monthly = _load_monthly(control_dir)
        base_months = [m for m, _ in base_monthly]
        control_full_pit[venue] = base_json["full_pit_pass"]
        if not base_json["full_pit_pass"]:
            print(f"  ⚠️  {venue} control ({args.control}) is NOT full-PIT "
                  f"(run_label={base_json['run_label']!r}) — every {venue} Tier-2 verdict is "
                  f"INVALID until the sweep is re-run full-PIT.")
        base_mar = _engine_mar(base_json["total_return"], base_json["max_drawdown"], _window_years(base_json))

        print(f"\n{'=' * 78}\n{venue.upper()}  (control={args.control}, {len(base_months)} months, "
              f"daily-DD={base_json['max_drawdown']:.1%}, ret={base_json['total_return']:.2f}x, "
              f"MAR={base_mar:.2f})\n{'=' * 78}")

        cells = sorted(p.name for p in sweep_root.iterdir() if p.is_dir() and p.name != args.control)
        for cell in cells:
            cdir = sweep_root / cell
            if not (cdir / "volume_event_best_monthly.csv").exists():
                print(f"  {cell}: no ledger")
                continue
            cj = _load_json_metrics(cdir)
            if cj.get("load_error"):
                print(f"  {cell}: report unreadable ({cj['load_error']}) — skipped (OOM-killed cell?)")
                continue
            cm = _load_monthly(cdir)
            # align months to the control's month set (intersection, sorted)
            cell_map = dict(cm)
            months = [m for m in base_months if m in cell_map]
            cr = [cell_map[m] for m in months]
            br = [dict(base_monthly)[m] for m in months]
            cell_mar = _engine_mar(cj["total_return"], cj["max_drawdown"], _window_years(cj))

            collected.setdefault(cell, {})[venue] = {
                "mar_d": cell_mar - base_mar, "ret": cj["total_return"],
                "dd": cj["max_drawdown"], "trades": cj["trades"],
                "full_pit": cj["full_pit_pass"],
            }

            thirds = _thirds(months, cr, br)
            conc = _concentration(cr, br, months)
            loo = _leave_one_out(cr, br, months)
            boot = _block_bootstrap(cr, br, n_boot=args.n_boot, block=args.block, seed=args.seed)

            all_pos = all(t["cell"] > 0 for t in thirds)
            print(f"\n  ── {cell}  (trades {base_json['trades']}→{cj['trades']}, "
                  f"daily-DD {base_json['max_drawdown']:.1%}→{cj['max_drawdown']:.1%}, "
                  f"MAR {base_mar:.2f}→{cell_mar:.2f}, ret {base_json['total_return']:.2f}x→{cj['total_return']:.2f}x)")
            print("     thirds (base→cell):  " + "   ".join(
                f"{t['label']}: {t['base']:+.0%}→{t['cell']:+.0%}" for t in thirds)
                + f"   [all cell-thirds>0: {'YES' if all_pos else 'NO'}]")
            tm = ", ".join(f"{m}({d:+.2f})" for m, d in conc["top_months"])
            print(f"     concentration:  top-{conc['top_k']} months = "
                  f"{conc['top_share_of_pos']:.0%} of positive-side monthly Δ   [{tm}]")
            print(f"     leave-one-out:  full Δret={loo['full_return_delta']:+.2f}x  "
                  f"min-LOO Δret={loo['min_loo_return_delta']:+.2f}x  "
                  f"(most load-bearing: {loo['most_loadbearing_month']})  "
                  f"flips-sign={loo['loo_flips_sign']}")
            print(f"     bootstrap ann-ret Δ:  p5={boot['ann_delta_p5']:+.1%}  "
                  f"p50={boot['ann_delta_p50']:+.1%}  p95={boot['ann_delta_p95']:+.1%}  "
                  f"P(Δ>0)={boot['ann_delta_p_gt0']:.0%}")
            print(f"     bootstrap MAR Δ:      p5={boot['mar_delta_p5']:+.2f}  "
                  f"p50={boot['mar_delta_p50']:+.2f}  p95={boot['mar_delta_p95']:+.2f}  "
                  f"P(Δ>0)={boot['mar_delta_p_gt0']:.0%}")

    # ── Cross-venue Tier 2 Demo-candidate verdict ──
    print(f"\n{'=' * 78}\nTIER 2 — DEMO-CANDIDATE VERDICT (pooled MAR Δ > +0.1, positive both venues,\n"
          f"  neither venue worse than -0.5 MAR, ≥30 by / ≥20 bn trades).\n"
          f"  INVALID unless cell AND control ran FULL-PIT on both venues "
          f"(partial-PIT = survivorship-biased).\n{'=' * 78}")
    hdr = f"  {'cell':<26}{'by MARΔ':>9}{'bn MARΔ':>9}{'pooled':>9}{'by/bn ret':>14}{'by/bn tr':>11}  verdict"
    print(hdr)
    for cell in sorted(collected):
        vens = collected[cell]
        if "bybit" not in vens or "binance" not in vens:
            print(f"  {cell:<26}{'(missing a venue)':>52}")
            continue
        by, bn = vens["bybit"], vens["binance"]
        pooled = (by["mar_d"] + bn["mar_d"]) / 2.0
        full_pit = (
            by["full_pit"] and bn["full_pit"]
            and control_full_pit.get("bybit", False)
            and control_full_pit.get("binance", False)
        )
        verdict = _tier2_verdict(by["mar_d"], bn["mar_d"], pooled,
                                 by_ret=by["ret"], bn_ret=bn["ret"],
                                 by_dd=by["dd"], bn_dd=bn["dd"],
                                 by_tr=by["trades"], bn_tr=bn["trades"],
                                 full_pit=full_pit)
        print(f"  {cell:<26}{by['mar_d']:>+9.2f}{bn['mar_d']:>+9.2f}{pooled:>+9.2f}"
              f"{by['ret']:>6.1f}x/{bn['ret']:<6.1f}x{by['trades']:>5}/{bn['trades']:<5}  {verdict}")
    print("\n  NOTE: fragility diagnostics above are REPORTED, non-blocking at Tier 2 —"
          "\n  they set demo order. The strict bootstrap p5 ≥ 0 + residual-Sharpe gates"
          "\n  apply only at Tier 3 (real money).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
