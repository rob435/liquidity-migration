"""Apply the Strictness Manifesto decision rule to a sweep summary CSV.

The Manifesto is the pre-registered candidate / falsifier / inconclusive
rule from
`docs/preregistration/2026-05-27-rank-direction-edge-and-universe-isolation-research-plan.md`.

Usage:
    python scripts/apply_decision_rule.py SUMMARY.csv [--control 00_baseline] \\
        [--rule manifesto|legacy] [--min-trades-bybit 50] [--min-trades-binance 30] \\
        [--sharpe-delta 0.5] [--dd-delta-pp 5]

Output (stdout):
    | cell_id | bybit_sharpe_d | binance_sharpe_d | bybit_dd_d | binance_dd_d | verdict | reason |
    ...

Exit codes:
    0  — analysis ran cleanly (verdicts printed)
    2  — control cell missing / CSV malformed / other usage error

The script is venue-agnostic: it expects a CSV with at least the columns
`cell_id`, `venue`, `sharpe_like`, `max_drawdown`, `trades`, `total_return`,
and treats `bybit` + `binance` as the two venues to check. Missing venue
rows for a non-control cell are treated as falsifiers.
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

EXIT_OK = 0
EXIT_USAGE = 2


@dataclass(frozen=True, slots=True)
class CellMetrics:
    cell_id: str
    venue: str
    sharpe_like: float
    max_drawdown: float
    trades: int
    total_return: float


@dataclass(frozen=True, slots=True)
class CellVerdict:
    cell_id: str
    bybit_sharpe_d: float
    binance_sharpe_d: float
    bybit_dd_d: float
    binance_dd_d: float
    bybit_trades: int
    binance_trades: int
    bybit_ret: float
    binance_ret: float
    verdict: str  # "candidate" | "reject" | "inconclusive" | "skip_control"
    reasons: tuple[str, ...]


def _read_csv(path: Path) -> list[CellMetrics]:
    if not path.exists():
        raise SystemExit(f"summary csv not found: {path}")
    out: list[CellMetrics] = []
    with path.open() as fh:
        reader = csv.DictReader(fh)
        required = {"cell_id", "venue", "sharpe_like", "max_drawdown", "trades", "total_return"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"csv missing columns: {sorted(missing)}")
        for row in reader:
            try:
                out.append(
                    CellMetrics(
                        cell_id=row["cell_id"].strip(),
                        venue=row["venue"].strip().lower(),
                        sharpe_like=float(row["sharpe_like"]),
                        max_drawdown=float(row["max_drawdown"]),
                        trades=int(row["trades"]),
                        total_return=float(row["total_return"]),
                    )
                )
            except (ValueError, KeyError) as exc:
                # Skip malformed rows with a stderr note; do not crash the whole run
                print(f"WARNING: skipping malformed row {row}: {exc}", file=sys.stderr)
    return out


def _index_by_cell(rows: list[CellMetrics]) -> dict[str, dict[str, CellMetrics]]:
    """Return {cell_id: {venue: CellMetrics}}."""
    out: dict[str, dict[str, CellMetrics]] = {}
    for row in rows:
        out.setdefault(row.cell_id, {})[row.venue] = row
    return out


def evaluate_cell(
    cell_id: str,
    cell_rows: dict[str, CellMetrics],
    control_rows: dict[str, CellMetrics],
    *,
    sharpe_delta_min: float,
    dd_delta_pp_max: float,  # negative direction is better (e.g. -5pp DD = improvement)
    min_trades_bybit: int,
    min_trades_binance: int,
) -> CellVerdict:
    """Apply the Manifesto rule to one cell vs control.

    A cell is a CANDIDATE only if ALL of:
      - sharpe Δ ≥ +sharpe_delta_min on BOTH venues, AND
      - DD Δ ≤ -dd_delta_pp_max on BOTH venues (i.e. DD improves by ≥ that amount), AND
      - return sign ≥ 0 on BOTH venues, AND
      - trade count ≥ min_trades_* on each venue.

    A cell is a FALSIFIER if ANY of:
      - sharpe Δ ≤ -sharpe_delta_min on either venue, OR
      - sign flip between venues (one positive, one negative), OR
      - DD on either venue > 60% (0.60 absolute, regardless of control).

    Cells failing CANDIDATE but not FALSIFIER are INCONCLUSIVE.
    """
    if cell_id == _SKIP_CONTROL_SENTINEL:
        return CellVerdict(
            cell_id=cell_id,
            bybit_sharpe_d=0.0,
            binance_sharpe_d=0.0,
            bybit_dd_d=0.0,
            binance_dd_d=0.0,
            bybit_trades=0,
            binance_trades=0,
            bybit_ret=0.0,
            binance_ret=0.0,
            verdict="skip_control",
            reasons=("this is the control cell",),
        )

    by = cell_rows.get("bybit")
    bn = cell_rows.get("binance")
    cby = control_rows.get("bybit")
    cbn = control_rows.get("binance")

    reasons: list[str] = []

    if cby is None or cbn is None:
        return CellVerdict(
            cell_id=cell_id,
            bybit_sharpe_d=0.0,
            binance_sharpe_d=0.0,
            bybit_dd_d=0.0,
            binance_dd_d=0.0,
            bybit_trades=0,
            binance_trades=0,
            bybit_ret=0.0,
            binance_ret=0.0,
            verdict="reject",
            reasons=("control cell missing on bybit or binance",),
        )

    bybit_sharpe_d = (by.sharpe_like - cby.sharpe_like) if by else 0.0
    binance_sharpe_d = (bn.sharpe_like - cbn.sharpe_like) if bn else 0.0
    # Drawdowns are negative numbers (e.g. -0.42 = -42%). Express "DD Δ" the way
    # the Strictness Manifesto does: |cell_DD| - |control_DD| in percentage points.
    # Negative = improvement (cell |DD| smaller than control |DD|). Matches the
    # plan's "DD Δ ≤ -5pp" candidate criterion = improvement by at least 5pp.
    bybit_dd_d = ((abs(by.max_drawdown) - abs(cby.max_drawdown)) * 100.0) if by else 0.0
    binance_dd_d = ((abs(bn.max_drawdown) - abs(cbn.max_drawdown)) * 100.0) if bn else 0.0
    bybit_trades = by.trades if by else 0
    binance_trades = bn.trades if bn else 0
    bybit_ret = by.total_return if by else 0.0
    binance_ret = bn.total_return if bn else 0.0

    # Falsifier checks first (clearest signal)
    if by is None:
        reasons.append("missing bybit row")
    if bn is None:
        reasons.append("missing binance row")
    if by and by.max_drawdown < -0.60:
        reasons.append(f"falsifier: bybit DD {by.max_drawdown:.1%} worse than -60%")
    if bn and bn.max_drawdown < -0.60:
        reasons.append(f"falsifier: binance DD {bn.max_drawdown:.1%} worse than -60%")
    if by and bn and ((by.total_return > 0) != (bn.total_return > 0)):
        reasons.append("falsifier: return sign flip between venues")
    if bybit_sharpe_d <= -sharpe_delta_min:
        reasons.append(f"falsifier: bybit sharpe Δ {bybit_sharpe_d:+.2f} ≤ -{sharpe_delta_min}")
    if binance_sharpe_d <= -sharpe_delta_min:
        reasons.append(f"falsifier: binance sharpe Δ {binance_sharpe_d:+.2f} ≤ -{sharpe_delta_min}")

    is_falsifier = any(r.startswith("falsifier:") or r.startswith("missing ") for r in reasons)

    # Candidate checks
    cand_reasons: list[str] = []
    if bybit_sharpe_d < sharpe_delta_min:
        cand_reasons.append(f"bybit sharpe Δ {bybit_sharpe_d:+.2f} < +{sharpe_delta_min}")
    if binance_sharpe_d < sharpe_delta_min:
        cand_reasons.append(f"binance sharpe Δ {binance_sharpe_d:+.2f} < +{sharpe_delta_min}")
    # Improvement requires DD shrink of at least dd_delta_pp_max percentage
    # points; with the |cell|-|control| convention an improvement is negative.
    if bybit_dd_d > -dd_delta_pp_max:
        cand_reasons.append(f"bybit DD Δ {bybit_dd_d:+.1f}pp (need ≤ -{dd_delta_pp_max}pp improvement)")
    if binance_dd_d > -dd_delta_pp_max:
        cand_reasons.append(f"binance DD Δ {binance_dd_d:+.1f}pp (need ≤ -{dd_delta_pp_max}pp improvement)")
    if by and by.total_return < 0:
        cand_reasons.append(f"bybit return {by.total_return:+.2f}× negative")
    if bn and bn.total_return < 0:
        cand_reasons.append(f"binance return {bn.total_return:+.2f}× negative")
    if bybit_trades < min_trades_bybit:
        cand_reasons.append(f"bybit trades {bybit_trades} < {min_trades_bybit}")
    if binance_trades < min_trades_binance:
        cand_reasons.append(f"binance trades {binance_trades} < {min_trades_binance}")

    is_candidate = not cand_reasons and not is_falsifier

    if is_falsifier:
        verdict = "reject"
    elif is_candidate:
        verdict = "candidate"
    else:
        verdict = "inconclusive"
        reasons.extend(cand_reasons)

    return CellVerdict(
        cell_id=cell_id,
        bybit_sharpe_d=bybit_sharpe_d,
        binance_sharpe_d=binance_sharpe_d,
        bybit_dd_d=bybit_dd_d,
        binance_dd_d=binance_dd_d,
        bybit_trades=bybit_trades,
        binance_trades=binance_trades,
        bybit_ret=bybit_ret,
        binance_ret=binance_ret,
        verdict=verdict,
        reasons=tuple(reasons),
    )


_SKIP_CONTROL_SENTINEL = "__control__"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("summary_csv", help="Sweep summary CSV (cell_id, venue, sharpe_like, max_drawdown, trades, total_return)")
    parser.add_argument("--control", default="00_baseline", help="Control cell_id (default: 00_baseline)")
    parser.add_argument("--rule", default="manifesto", choices=["manifesto", "legacy"], help="Decision-rule preset")
    parser.add_argument("--sharpe-delta", type=float, default=None, help="Min sharpe Δ vs control (preset-dependent default)")
    parser.add_argument("--dd-delta-pp", type=float, default=None, help="Min DD-improvement in pp vs control (preset-dependent default)")
    parser.add_argument("--min-trades-bybit", type=int, default=None)
    parser.add_argument("--min-trades-binance", type=int, default=None)
    args = parser.parse_args(argv)

    # Preset defaults
    if args.rule == "manifesto":
        sharpe_delta = args.sharpe_delta if args.sharpe_delta is not None else 0.5
        dd_delta_pp = args.dd_delta_pp if args.dd_delta_pp is not None else 5.0
        min_by = args.min_trades_bybit if args.min_trades_bybit is not None else 50
        min_bn = args.min_trades_binance if args.min_trades_binance is not None else 30
    else:  # legacy = the 2026-05-28 sweep's softer rule
        sharpe_delta = args.sharpe_delta if args.sharpe_delta is not None else 0.5
        dd_delta_pp = args.dd_delta_pp if args.dd_delta_pp is not None else -5.0  # +5pp tolerance (legacy: DD may worsen by up to 5pp)
        min_by = args.min_trades_bybit if args.min_trades_bybit is not None else 30
        min_bn = args.min_trades_binance if args.min_trades_binance is not None else 0

    rows = _read_csv(Path(args.summary_csv))
    if not rows:
        print(f"no rows read from {args.summary_csv}", file=sys.stderr)
        return EXIT_USAGE

    indexed = _index_by_cell(rows)
    if args.control not in indexed:
        print(
            f"control cell '{args.control}' not in csv. Available: {sorted(indexed)}",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)
    control_rows = indexed[args.control]

    verdicts: list[CellVerdict] = []
    for cell_id in sorted(indexed):
        if cell_id == args.control:
            verdicts.append(
                CellVerdict(
                    cell_id=cell_id,
                    bybit_sharpe_d=0.0,
                    binance_sharpe_d=0.0,
                    bybit_dd_d=0.0,
                    binance_dd_d=0.0,
                    bybit_trades=control_rows.get("bybit").trades if "bybit" in control_rows else 0,
                    binance_trades=control_rows.get("binance").trades if "binance" in control_rows else 0,
                    bybit_ret=control_rows.get("bybit").total_return if "bybit" in control_rows else 0.0,
                    binance_ret=control_rows.get("binance").total_return if "binance" in control_rows else 0.0,
                    verdict="skip_control",
                    reasons=("this is the control cell",),
                )
            )
            continue
        verdicts.append(
            evaluate_cell(
                cell_id=cell_id,
                cell_rows=indexed[cell_id],
                control_rows=control_rows,
                sharpe_delta_min=sharpe_delta,
                dd_delta_pp_max=dd_delta_pp,
                min_trades_bybit=min_by,
                min_trades_binance=min_bn,
            )
        )

    # Pretty-print table
    header = (
        "cell_id", "by_sh_d", "bn_sh_d", "by_dd_d", "bn_dd_d",
        "by_tr", "bn_tr", "by_ret", "bn_ret", "verdict",
    )
    widths = [max(len(h), 18) for h in header]
    rows_fmt = [header]
    for v in verdicts:
        rows_fmt.append((
            v.cell_id,
            f"{v.bybit_sharpe_d:+.2f}",
            f"{v.binance_sharpe_d:+.2f}",
            f"{v.bybit_dd_d:+.1f}pp",
            f"{v.binance_dd_d:+.1f}pp",
            str(v.bybit_trades),
            str(v.binance_trades),
            f"{v.bybit_ret:+.2f}x",
            f"{v.binance_ret:+.2f}x",
            v.verdict,
        ))
    for r in rows_fmt:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(f"# rule: {args.rule}  control: {args.control}  sharpe_delta_min: +{sharpe_delta}  dd_delta_max: -{dd_delta_pp}pp  min_trades: bybit={min_by} binance={min_bn}")
    for r in rows_fmt:
        print(fmt.format(*r))
    print()
    candidates = [v for v in verdicts if v.verdict == "candidate"]
    rejects = [v for v in verdicts if v.verdict == "reject"]
    inconc = [v for v in verdicts if v.verdict == "inconclusive"]
    print(f"# summary: candidates={len(candidates)} rejects={len(rejects)} inconclusive={len(inconc)} skip_control=1")
    if candidates:
        print("# CANDIDATES (per Manifesto FDR ceiling, pick top-3 by combined-venue sharpe):")
        for v in sorted(candidates, key=lambda c: -(c.bybit_sharpe_d + c.binance_sharpe_d)):
            print(f"#   {v.cell_id}  combined Δsharpe={v.bybit_sharpe_d + v.binance_sharpe_d:+.2f}")
    if rejects:
        print()
        print("# REJECT reasons (first 5):")
        for v in rejects[:5]:
            print(f"#   {v.cell_id}: {'; '.join(v.reasons)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
