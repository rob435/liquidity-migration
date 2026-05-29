"""Apply a decision rule to a sweep summary CSV.

Two rules are supported:

- ``--rule manifesto`` (default; the Round 1 / R10-R11 Promotion bar):
  Sharpe Δ ≥ +0.5 BOTH venues + DD Δ ≤ -5pp BOTH + return ≥ 0 BOTH +
  ≥ 50 by / ≥ 30 bn trades. Pre-registered in
  ``docs/research_summary.md``.

- ``--rule investigation`` (Round 2's R1-R8 sub-phase tier, MAR-based):
  A cell is **investigation-positive** if MAR Δ > 0 on majority of
  venues (2/2 OR 1/2 with the other not worse than -0.5 MAR) AND no
  return sign-flip vs control AND ≥ 30 by / ≥ 20 bn trades.
  Falsifier: MAR Δ ≤ -1.0 on either venue OR return goes negative on
  a venue that was positive in control OR DD > 70% on either OR
  trades < 30 on either (≈ 10 / sub-period at 3-thirds windows).
  Pre-registered in
  ``docs/research_summary.md``.

MAR is computed as ``annualized_return / |max_drawdown|`` where
``annualized_return = (1 + total_return) ** (365.25 / window_days) - 1``.
``window_days`` is read from the summary CSV's optional ``window_days``
column when present (preferred — the sweep runtime emits it), else from
the ``--window-days`` CLI flag. Cells with ambiguous or missing
window_days fall through with a clear error.

Usage:
    python scripts/apply_decision_rule.py SUMMARY.csv [--control 00_baseline] \\
        [--rule manifesto|investigation|legacy] \\
        [--min-trades-bybit N] [--min-trades-binance N] \\
        [--sharpe-delta 0.5] [--dd-delta-pp 5] \\
        [--mar-delta-min 0.0] [--mar-falsify -1.0] [--window-days N]

Output (stdout): per-cell verdict table + summary counts. The columns
shown vary by rule (Sharpe Δ for manifesto/legacy, MAR Δ for
investigation), with the metric set common to both (return, DD, trades)
always present for cross-rule comparison.

Exit codes:
    0  — analysis ran cleanly (verdicts printed)
    2  — control cell missing / CSV malformed / other usage error

The script is venue-agnostic: it expects a CSV with at least the columns
``cell_id``, ``venue``, ``sharpe_like``, ``max_drawdown``, ``trades``,
``total_return``, and treats ``bybit`` + ``binance`` as the two venues
to check. Missing venue rows for a non-control cell are treated as
falsifiers. Investigation mode additionally requires either a
``window_days`` column or a ``--window-days`` CLI flag.
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, replace
from pathlib import Path

# Force UTF-8 stdout so the verdict-table prints (which contain Δ chars) don't
# crash on Windows cp1252-encoded captured pipes — observed during Phase 0
# verdict generation.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

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
    # Optional — emitted by `_sweep_runtime.run_cell` for Round 2 sweeps so the
    # investigation rule can compute MAR per cell without a CLI flag. Missing
    # for legacy summary CSVs.
    window_days: float | None = None
    # Optional PIT-integrity flag from the report payload. None = column absent
    # (legacy CSV); False = the run was NOT full-PIT (survivorship-tainted —
    # must not be cited as promotion evidence).
    full_pit_universe_pass: bool | None = None


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
    verdict: str  # "candidate" | "investigation_positive" | "reject" | "inconclusive" | "descriptive" | "skip_control" | "non_full_pit"
    reasons: tuple[str, ...]
    # Optional MAR-axis fields (populated by the investigation evaluator;
    # default 0.0 for manifesto/legacy verdicts so the dataclass stays frozen
    # but doesn't require touching every construction site).
    bybit_mar: float = 0.0
    binance_mar: float = 0.0
    bybit_mar_d: float = 0.0
    binance_mar_d: float = 0.0


def compute_annualized_return(total_return: float, window_days: float) -> float:
    """Geometric annualization. `total_return` is the cumulative multiplier-1
    (e.g. 5.1876 = +518.76%). `window_days` is calendar days inclusive."""
    if window_days <= 0:
        raise ValueError(f"window_days must be > 0, got {window_days}")
    growth = 1.0 + total_return
    if growth <= 0:
        # Cell lost more than 100% (cumulative). Annualization is undefined in
        # the geometric sense; return -1.0 as the floor (lost everything).
        return -1.0
    return growth ** (365.25 / window_days) - 1.0


def compute_mar(total_return: float, max_drawdown: float, window_days: float) -> float:
    """MAR = annualized_return / |max_drawdown|.

    Convention: max_drawdown is a negative number (-0.42 = -42%). MAR is
    signed by annualized_return: positive when profitable, negative when
    cumulative return is negative, zero when there is no drawdown
    (degenerate; treat as 0 to avoid divide-by-zero — the cell is rejected
    on trade-count or other gates anyway)."""
    if max_drawdown == 0.0:
        return 0.0
    ann = compute_annualized_return(total_return, window_days)
    return ann / abs(max_drawdown)


def _read_csv(path: Path) -> tuple[list[CellMetrics], list[dict[str, str]]]:
    """Return ``(metrics, excluded)``.

    ``excluded`` holds cells that produced no usable metrics — a crashed/failed
    sweep cell (``status`` != ``ok``) or a genuinely malformed row. Previously
    these were silently skipped, so an operator scanning the verdict table saw
    only the cells that happened to complete; a cell that failed for a
    methodology-relevant reason (e.g. a partial-PIT abort) vanished without a
    trace (M6). The caller surfaces ``excluded`` explicitly."""
    if not path.exists():
        raise SystemExit(f"summary csv not found: {path}")
    out: list[CellMetrics] = []
    excluded: list[dict[str, str]] = []
    with path.open() as fh:
        reader = csv.DictReader(fh)
        required = {"cell_id", "venue", "sharpe_like", "max_drawdown", "trades", "total_return"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"csv missing columns: {sorted(missing)}")
        has_window_days = "window_days" in (reader.fieldnames or [])
        has_status = "status" in (reader.fieldnames or [])
        has_full_pit = "full_pit_universe_pass" in (reader.fieldnames or [])
        for row in reader:
            status = (row.get("status") or "ok").strip().lower() if has_status else "ok"
            if status not in {"", "ok"}:
                # A failed / no-report cell carries no metrics — record it as an
                # explicit exclusion (a falsifier), never a silent drop.
                excluded.append({
                    "cell_id": (row.get("cell_id") or "").strip(),
                    "venue": (row.get("venue") or "").strip().lower(),
                    "status": status,
                    "error": (row.get("error") or "").strip()[:300],
                })
                continue
            try:
                window_days_val: float | None = None
                if has_window_days:
                    raw = row.get("window_days", "").strip()
                    if raw:
                        window_days_val = float(raw)
                full_pit: bool | None = None
                if has_full_pit:
                    raw_pit = (row.get("full_pit_universe_pass") or "").strip().lower()
                    if raw_pit in {"true", "false"}:
                        full_pit = raw_pit == "true"
                out.append(
                    CellMetrics(
                        cell_id=row["cell_id"].strip(),
                        venue=row["venue"].strip().lower(),
                        sharpe_like=float(row["sharpe_like"]),
                        max_drawdown=float(row["max_drawdown"]),
                        trades=int(row["trades"]),
                        total_return=float(row["total_return"]),
                        window_days=window_days_val,
                        full_pit_universe_pass=full_pit,
                    )
                )
            except (ValueError, KeyError) as exc:
                # Malformed row: surface as an exclusion too, not a silent skip.
                print(f"WARNING: skipping malformed row {row}: {exc}", file=sys.stderr)
                excluded.append({
                    "cell_id": (row.get("cell_id") or "").strip(),
                    "venue": (row.get("venue") or "").strip().lower(),
                    "status": "malformed",
                    "error": str(exc)[:300],
                })
    return out, excluded


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


def evaluate_cell_investigation(
    cell_id: str,
    cell_rows: dict[str, CellMetrics],
    control_rows: dict[str, CellMetrics],
    *,
    window_days: float,
    mar_delta_positive: float = 0.0,
    mar_delta_tolerance: float = 0.5,
    mar_falsify: float = -1.0,
    dd_falsify_abs: float = 0.70,
    min_trades_bybit: int = 30,
    min_trades_binance: int = 20,
) -> CellVerdict:
    """Round 2 Investigation-tier rule (MAR-primary).

    A cell is **investigation-positive** if ALL of:
      - MAR Δ > ``mar_delta_positive`` on the majority of venues (2/2 OR
        1/2 with the other not worse than -``mar_delta_tolerance``)
      - No return sign-flip vs control (both venues remain same-signed)
      - Trade counts ≥ min_trades_* on each venue

    A cell **falsifies** if ANY of:
      - MAR Δ ≤ ``mar_falsify`` on either venue (default: ≤ -1.0)
      - Return goes negative on a venue that was positive in control
      - |DD| > ``dd_falsify_abs`` on either venue (default: 70%)
      - Trade count < 30 (Bybit) or < 20 (Binance) — signal-population
        sanity floor; the plan's per-sub-period floor of 10 / third
        approximates to 30 / 20 at 3-thirds windows.

    Cells failing investigation-positive but not falsifying are
    **descriptive** — recorded for context, not acted on.

    ``window_days`` is required to compute MAR (annualized_return /
    |max_drawdown|). The caller is responsible for sourcing it from the
    CSV column or the CLI flag.
    """
    by = cell_rows.get("bybit")
    bn = cell_rows.get("binance")
    cby = control_rows.get("bybit")
    cbn = control_rows.get("binance")

    reasons: list[str] = []
    if cby is None or cbn is None:
        return CellVerdict(
            cell_id=cell_id,
            bybit_sharpe_d=0.0, binance_sharpe_d=0.0,
            bybit_dd_d=0.0, binance_dd_d=0.0,
            bybit_trades=0, binance_trades=0,
            bybit_ret=0.0, binance_ret=0.0,
            verdict="reject",
            reasons=("control cell missing on bybit or binance",),
        )

    bybit_sharpe_d = (by.sharpe_like - cby.sharpe_like) if by else 0.0
    binance_sharpe_d = (bn.sharpe_like - cbn.sharpe_like) if bn else 0.0
    bybit_dd_d = ((abs(by.max_drawdown) - abs(cby.max_drawdown)) * 100.0) if by else 0.0
    binance_dd_d = ((abs(bn.max_drawdown) - abs(cbn.max_drawdown)) * 100.0) if bn else 0.0
    bybit_trades = by.trades if by else 0
    binance_trades = bn.trades if bn else 0
    bybit_ret = by.total_return if by else 0.0
    binance_ret = bn.total_return if bn else 0.0

    # MAR computations (per-row window_days takes precedence over the caller's
    # default if present — keeps cells with venue-specific windows correct).
    def _win(metrics: CellMetrics | None) -> float:
        if metrics is None:
            return window_days
        return metrics.window_days if metrics.window_days is not None else window_days

    bybit_mar_control = compute_mar(cby.total_return, cby.max_drawdown, _win(cby))
    binance_mar_control = compute_mar(cbn.total_return, cbn.max_drawdown, _win(cbn))
    bybit_mar = compute_mar(by.total_return, by.max_drawdown, _win(by)) if by else 0.0
    binance_mar = compute_mar(bn.total_return, bn.max_drawdown, _win(bn)) if bn else 0.0
    bybit_mar_d = bybit_mar - bybit_mar_control
    binance_mar_d = binance_mar - binance_mar_control

    # Falsifier checks
    if by is None:
        reasons.append("missing bybit row")
    if bn is None:
        reasons.append("missing binance row")
    if by and abs(by.max_drawdown) > dd_falsify_abs:
        reasons.append(f"falsifier: bybit DD {by.max_drawdown:.1%} worse than -{dd_falsify_abs:.0%}")
    if bn and abs(bn.max_drawdown) > dd_falsify_abs:
        reasons.append(f"falsifier: binance DD {bn.max_drawdown:.1%} worse than -{dd_falsify_abs:.0%}")
    if by and cby and (cby.total_return > 0) and (by.total_return < 0):
        reasons.append("falsifier: bybit return went negative vs positive control")
    if bn and cbn and (cbn.total_return > 0) and (bn.total_return < 0):
        reasons.append("falsifier: binance return went negative vs positive control")
    if bybit_mar_d <= mar_falsify:
        reasons.append(f"falsifier: bybit MAR Δ {bybit_mar_d:+.2f} ≤ {mar_falsify}")
    if binance_mar_d <= mar_falsify:
        reasons.append(f"falsifier: binance MAR Δ {binance_mar_d:+.2f} ≤ {mar_falsify}")
    if bybit_trades < min_trades_bybit:
        reasons.append(f"falsifier: bybit trades {bybit_trades} < {min_trades_bybit}")
    if binance_trades < min_trades_binance:
        reasons.append(f"falsifier: binance trades {binance_trades} < {min_trades_binance}")

    is_falsifier = any(
        r.startswith("falsifier:") or r.startswith("missing ") for r in reasons
    )

    # Investigation-positive: majority-venue MAR Δ > 0, with tolerance on the
    # other venue. Specifically:
    #   2/2 positive → investigation-positive
    #   1/2 positive AND other venue's MAR Δ ≥ -tolerance → investigation-positive
    #   else → descriptive (not positive, not falsifier)
    pos_reasons: list[str] = []
    by_positive = bybit_mar_d > mar_delta_positive
    bn_positive = binance_mar_d > mar_delta_positive
    venues_positive = int(by_positive) + int(bn_positive)
    if venues_positive == 0:
        pos_reasons.append(
            f"no venue positive: bybit MAR Δ {bybit_mar_d:+.2f}, binance MAR Δ {binance_mar_d:+.2f}"
        )
    elif venues_positive == 1:
        # Check the other venue's tolerance
        other_d = bybit_mar_d if not by_positive else binance_mar_d
        other_venue = "bybit" if not by_positive else "binance"
        if other_d < -mar_delta_tolerance:
            pos_reasons.append(
                f"only 1 venue positive, {other_venue} MAR Δ {other_d:+.2f} < -{mar_delta_tolerance}"
            )
    # Sign-flip in returns (no positive direction loss between venues)
    if by and bn and ((by.total_return > 0) != (bn.total_return > 0)):
        pos_reasons.append("return sign-flip between venues")

    is_positive = (not pos_reasons) and (not is_falsifier)

    if is_falsifier:
        verdict = "reject"
    elif is_positive:
        verdict = "investigation_positive"
    else:
        verdict = "descriptive"
        reasons.extend(pos_reasons)

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
        bybit_mar=bybit_mar,
        binance_mar=binance_mar,
        bybit_mar_d=bybit_mar_d,
        binance_mar_d=binance_mar_d,
    )


_SKIP_CONTROL_SENTINEL = "__control__"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("summary_csv", help="Sweep summary CSV (cell_id, venue, sharpe_like, max_drawdown, trades, total_return, optional window_days)")
    parser.add_argument("--control", default="00_baseline", help="Control cell_id (default: 00_baseline)")
    parser.add_argument(
        "--rule", default="manifesto",
        choices=["manifesto", "legacy", "investigation"],
        help="Decision-rule preset",
    )
    parser.add_argument("--sharpe-delta", type=float, default=None, help="Min sharpe Δ vs control (manifesto/legacy)")
    parser.add_argument("--dd-delta-pp", type=float, default=None, help="Min DD-improvement in pp vs control (manifesto/legacy)")
    parser.add_argument("--mar-delta-min", type=float, default=None, help="Min MAR Δ for investigation-positive (investigation; default 0.0)")
    parser.add_argument("--mar-delta-tolerance", type=float, default=None, help="Max negative MAR Δ on the non-positive venue (investigation; default 0.5)")
    parser.add_argument("--mar-falsify", type=float, default=None, help="MAR Δ ≤ this is a falsifier (investigation; default -1.0)")
    parser.add_argument("--dd-falsify", type=float, default=None, help="|DD| > this is a falsifier (investigation; default 0.70 = 70%)")
    parser.add_argument("--window-days", type=float, default=None, help="Window length in calendar days for MAR annualization (investigation; only used when summary CSV lacks a window_days column)")
    parser.add_argument("--min-trades-bybit", type=int, default=None)
    parser.add_argument("--min-trades-binance", type=int, default=None)
    args = parser.parse_args(argv)

    # Preset defaults
    if args.rule == "manifesto":
        sharpe_delta = args.sharpe_delta if args.sharpe_delta is not None else 0.5
        dd_delta_pp = args.dd_delta_pp if args.dd_delta_pp is not None else 5.0
        min_by = args.min_trades_bybit if args.min_trades_bybit is not None else 50
        min_bn = args.min_trades_binance if args.min_trades_binance is not None else 30
    elif args.rule == "legacy":  # legacy = the 2026-05-28 sweep's softer rule
        sharpe_delta = args.sharpe_delta if args.sharpe_delta is not None else 0.5
        dd_delta_pp = args.dd_delta_pp if args.dd_delta_pp is not None else -5.0  # +5pp tolerance (legacy: DD may worsen by up to 5pp)
        min_by = args.min_trades_bybit if args.min_trades_bybit is not None else 30
        min_bn = args.min_trades_binance if args.min_trades_binance is not None else 0
    else:  # investigation = Round 2 R1-R8 tier
        sharpe_delta = 0.0  # unused
        dd_delta_pp = 0.0   # unused
        min_by = args.min_trades_bybit if args.min_trades_bybit is not None else 30
        min_bn = args.min_trades_binance if args.min_trades_binance is not None else 20

    rows, excluded = _read_csv(Path(args.summary_csv))
    if excluded:
        # M6: never let a crashed/failed cell vanish from the verdict — a
        # partial-PIT abort or a methodology-relevant failure is a falsifier,
        # not an absence. Surface them explicitly in the verdict output.
        print(f"\n## EXCLUDED CELLS (no metrics — treated as FAILED, not omitted): {len(excluded)}")
        for ex in excluded:
            print(f"  - {ex['cell_id']}/{ex['venue']}: status={ex['status']} {ex['error']}".rstrip())
    # A cell whose run was NOT full-PIT is current-universe (survivorship) biased
    # and is not valid promotion evidence. The volume-events backtest fails such a
    # run by default, so this only fires for an explicitly --allow-partial-pit
    # EXPLORATORY cell. Warning alone is not enough — such a cell must be HARD-
    # EXCLUDED from any positive verdict below (it previously could still receive
    # investigation_positive/candidate on good-looking but tainted numbers).
    non_full_pit = sorted({m.cell_id for m in rows if m.full_pit_universe_pass is False})
    if non_full_pit:
        print(
            f"\n## WARNING — NON-FULL-PIT CELLS (survivorship-tainted, HARD-EXCLUDED from promotion): "
            f"{', '.join(non_full_pit)}"
        )
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

    # Investigation mode needs window_days. Source priority: per-row CSV column,
    # then CLI --window-days, else error.
    inferred_window_days: float | None = None
    if args.rule == "investigation":
        csv_windows = {
            m.window_days
            for cell in indexed.values()
            for m in cell.values()
            if m.window_days is not None
        }
        if csv_windows:
            if len(csv_windows) > 1:
                print(
                    f"WARNING: multiple window_days values in CSV ({sorted(csv_windows)}); "
                    "per-row values are used per cell, --window-days only applies as fallback.",
                    file=sys.stderr,
                )
            inferred_window_days = next(iter(csv_windows))
        if inferred_window_days is None and args.window_days is None:
            print(
                "ERROR: investigation rule requires window_days. "
                "Either emit a window_days column from the sweep runtime, "
                "or pass --window-days N to this script.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        window_days_fallback = args.window_days if args.window_days is not None else inferred_window_days
        mar_delta_min = args.mar_delta_min if args.mar_delta_min is not None else 0.0
        mar_delta_tolerance = args.mar_delta_tolerance if args.mar_delta_tolerance is not None else 0.5
        mar_falsify = args.mar_falsify if args.mar_falsify is not None else -1.0
        dd_falsify = args.dd_falsify if args.dd_falsify is not None else 0.70

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
        if args.rule == "investigation":
            assert window_days_fallback is not None  # narrowed above
            verdicts.append(
                evaluate_cell_investigation(
                    cell_id=cell_id,
                    cell_rows=indexed[cell_id],
                    control_rows=control_rows,
                    window_days=window_days_fallback,
                    mar_delta_positive=mar_delta_min,
                    mar_delta_tolerance=mar_delta_tolerance,
                    mar_falsify=mar_falsify,
                    dd_falsify_abs=dd_falsify,
                    min_trades_bybit=min_by,
                    min_trades_binance=min_bn,
                )
            )
        else:
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

    # Hard-exclude survivorship-tainted (non-full-PIT) cells: override their
    # verdict so a tainted cell can NEVER land in the promotion-positive bucket,
    # regardless of how good its (biased) numbers look. The deltas stay in the
    # table for transparency; the verdict becomes non-promotable.
    if non_full_pit:
        nfp = set(non_full_pit)
        verdicts = [
            replace(
                v,
                verdict="non_full_pit",
                reasons=("survivorship-tainted: run was not full-PIT — excluded from promotion",) + v.reasons,
            )
            if (v.cell_id in nfp and v.verdict != "skip_control")
            else v
            for v in verdicts
        ]

    # Pretty-print table (manifesto/legacy keep the original sharpe-Δ shape;
    # investigation adds MAR Δ columns).
    if args.rule == "investigation":
        header = (
            "cell_id", "by_mar_d", "bn_mar_d", "by_dd_d", "bn_dd_d",
            "by_tr", "bn_tr", "by_ret", "bn_ret", "verdict",
        )
    else:
        header = (
            "cell_id", "by_sh_d", "bn_sh_d", "by_dd_d", "bn_dd_d",
            "by_tr", "bn_tr", "by_ret", "bn_ret", "verdict",
        )
    widths = [max(len(h), 18) for h in header]
    rows_fmt = [header]
    for v in verdicts:
        if args.rule == "investigation":
            primary_a = f"{v.bybit_mar_d:+.2f}"
            primary_b = f"{v.binance_mar_d:+.2f}"
        else:
            primary_a = f"{v.bybit_sharpe_d:+.2f}"
            primary_b = f"{v.binance_sharpe_d:+.2f}"
        rows_fmt.append((
            v.cell_id,
            primary_a,
            primary_b,
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
    if args.rule == "investigation":
        print(
            f"# rule: investigation  control: {args.control}  "
            f"mar_delta_min: +{mar_delta_min}  mar_delta_tolerance: -{mar_delta_tolerance}  "
            f"mar_falsify: {mar_falsify}  dd_falsify: -{dd_falsify*100:.0f}%  "
            f"min_trades: bybit={min_by} binance={min_bn}  "
            f"window_days_fallback: {window_days_fallback}"
        )
    else:
        print(f"# rule: {args.rule}  control: {args.control}  sharpe_delta_min: +{sharpe_delta}  dd_delta_max: -{dd_delta_pp}pp  min_trades: bybit={min_by} binance={min_bn}")
    for r in rows_fmt:
        print(fmt.format(*r))
    print()
    positives_label = "investigation_positive" if args.rule == "investigation" else "candidate"
    positives = [v for v in verdicts if v.verdict == positives_label]
    rejects = [v for v in verdicts if v.verdict == "reject"]
    other_label = "descriptive" if args.rule == "investigation" else "inconclusive"
    other = [v for v in verdicts if v.verdict == other_label]
    print(f"# summary: {positives_label}={len(positives)} rejects={len(rejects)} {other_label}={len(other)} skip_control=1")
    if positives:
        if args.rule == "investigation":
            print("# INVESTIGATION-POSITIVE cells (ranked by combined-venue MAR Δ; forward to R10 candidate queue):")
            for v in sorted(positives, key=lambda c: -(c.bybit_mar_d + c.binance_mar_d)):
                print(f"#   {v.cell_id}  combined ΔMAR={v.bybit_mar_d + v.binance_mar_d:+.2f}")
        else:
            print("# CANDIDATES (per Manifesto FDR ceiling, pick top-3 by combined-venue sharpe):")
            for v in sorted(positives, key=lambda c: -(c.bybit_sharpe_d + c.binance_sharpe_d)):
                print(f"#   {v.cell_id}  combined Δsharpe={v.bybit_sharpe_d + v.binance_sharpe_d:+.2f}")
    if rejects:
        print()
        print("# REJECT reasons (first 5):")
        for v in rejects[:5]:
            print(f"#   {v.cell_id}: {'; '.join(v.reasons)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
