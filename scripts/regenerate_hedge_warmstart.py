"""Regenerate the immutable Bybit hedge model prior from historical data.

Run this after refreshing the data root and code-defined component equity.

Construction:
- components = the code-defined ``continuous_ensemble_v2`` TP12 base
  component ledgers combined at the active weights;
- unit_ret[day] = gross + funding + scale-1 entry costs per LEDGER day (the
  scale-independent day return `apply_rebalance_rule` scales);
- btc_ret/eth_ret = same-calendar-day daily close-to-close from klines_1h.

The overwrite gate compares overlapping unit returns and refuses excessive
drift, a shorter series, or a disjoint replacement unless explicitly forced.

    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/regenerate_hedge_warmstart.py --component-root PATH \
        [--validate-only] [--days 200] \
        [--max-unit-drift 1e-3] [--force]

``--component-root`` should point at the ``components`` directory emitted by
the standard continuous equity runner. Those historical base-component
artifacts intentionally do not reproduce the stateful accepted-decision
BTC-risk sizing overlay used by the forward target producer.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import polars as pl  # noqa: E402

from liquidity_migration.continuous_component_sources import (  # noqa: E402
    ContinuousComponentSource,
    load_continuous_component_source,
)
from liquidity_migration.continuous_profile import (  # noqa: E402
    ACTIVE_CONTINUOUS_COMPONENT_BY_KEY,
    CONTINUOUS_EQUITY_EVIDENCE_LABEL,
    CONTINUOUS_HISTORICAL_RUN_LABEL,
    CONTINUOUS_HISTORY_START_DATE,
    CONTINUOUS_PROFILE_ID,
    CONTINUOUS_PROFILE_REVISION,
    ACTIVE_CONTINUOUS_CONFIG,
)
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousHedgeRule,
    combine_continuous_components,
    compute_hedge_betas_2f,
    scaled_entry_cost,
)

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit"}
OUT_DIR = Path(__file__).resolve().parent.parent / "deploy" / "hedge_warmstart"
WINNER = dict(ACTIVE_CONTINUOUS_CONFIG["weights"])
MS_DAY = 86_400_000
MIN_OBJECT_REFERENCE_OVERLAP = 60
# Bound on how far the estimated (b1, b2) may move between the deployed and a
# regenerated prior without an explicit --force review. 0.25 of hedge-ratio
# units is roughly a quarter of the per-leg cap.
MAX_PRIOR_BETA_DRIFT = 0.25


def utc_day_start_ms(now: dt.datetime | None = None) -> int:
    """UTC start of the current (therefore still incomplete) calendar day."""
    current = now or dt.datetime.now(tz=dt.timezone.utc)
    current = current.astimezone(dt.timezone.utc)
    return int(
        dt.datetime(current.year, current.month, current.day, tzinfo=dt.timezone.utc).timestamp()
        * 1000
    )


def _component_funding_failures_before(
    component_root: Path,
    venue: str,
    *,
    cutoff_day_ms: int,
) -> list[str]:
    """Return component funding defects that can enter the model prior.

    A standard run may aggregate to ``partial`` solely because its final rows
    represent positions that are still open today.  Those rows are excluded by
    ``regenerate``.  Any non-modeled trade whose exit day is already complete is
    a real historical coverage defect and remains a hard failure.
    """
    failures: list[str] = []
    seen_cells: set[str] = set()
    for src in WINNER:
        cell = ACTIVE_CONTINUOUS_COMPONENT_BY_KEY[src].artifact_cell
        if cell in seen_cells:
            continue
        seen_cells.add(cell)
        trades_path = component_root / venue / cell / "continuous_trades.csv"
        if not trades_path.exists():
            failures.append(f"missing component trades={trades_path}")
            continue
        with trades_path.open(newline="") as fh:
            rows = csv.DictReader(fh)
            for row in rows:
                mode = str(row.get("funding_mode") or "missing").lower()
                try:
                    exit_ts_ms = int(row.get("exit_ts_ms") or 0)
                except ValueError:
                    failures.append(f"{cell}: invalid exit_ts_ms={row.get('exit_ts_ms')!r}")
                    continue
                exit_day_ms = (exit_ts_ms // MS_DAY) * MS_DAY
                if exit_day_ms < cutoff_day_ms and mode != "modeled":
                    failures.append(
                        f"{cell}: historical funding_mode={mode!r} "
                        f"at exit_ts_ms={exit_ts_ms}"
                    )
    return failures


def _component_report_payloads(component_root: Path, venue: str) -> dict[str, dict]:
    payloads: dict[str, dict] = {}
    for src in WINNER:
        cell = ACTIVE_CONTINUOUS_COMPONENT_BY_KEY[src].artifact_cell
        if cell in payloads:
            continue
        report_path = component_root / venue / cell / "continuous_report.json"
        if not report_path.exists():
            raise RuntimeError(f"missing component report: {report_path}")
        payloads[cell] = json.loads(report_path.read_text(encoding="utf-8"))
    return payloads


def validate_current_component_root(
    component_root: Path,
    venue: str,
    *,
    cutoff_day_ms: int | None = None,
    as_of_date: dt.date | None = None,
) -> dict:
    """Require base-component artifacts from the code-defined active profile."""
    summary_path = component_root.parent / venue / "continuous_equity_summary.json"
    if not summary_path.exists():
        raise RuntimeError(f"missing continuous equity summary: {summary_path}")
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    failures = []
    try:
        component_payloads = _component_report_payloads(component_root, venue)
    except (OSError, ValueError, RuntimeError) as exc:
        component_payloads = {}
        failures.append(str(exc))
    if payload.get("run_label") != CONTINUOUS_EQUITY_EVIDENCE_LABEL:
        failures.append(f"run_label={payload.get('run_label')!r}")
    if payload.get("strategy_run_label") != CONTINUOUS_HISTORICAL_RUN_LABEL:
        failures.append(f"strategy_run_label={payload.get('strategy_run_label')!r}")
    if payload.get("strategy_profile") != CONTINUOUS_PROFILE_ID:
        failures.append(f"strategy_profile={payload.get('strategy_profile')!r}")
    if payload.get("profile_revision") != CONTINUOUS_PROFILE_REVISION:
        failures.append(f"profile_revision={payload.get('profile_revision')!r}")
    expected_tp = next(
        iter(ACTIVE_CONTINUOUS_COMPONENT_BY_KEY.values())
    ).take_profit_pct
    if abs(float(payload.get("component_take_profit_pct") or 0.0) - expected_tp) > 1e-12:
        failures.append(f"component_take_profit_pct={payload.get('component_take_profit_pct')!r}")
    if abs(float(payload.get("backtest_leverage") or 0.0) - 1.0) > 1e-12:
        failures.append(f"backtest_leverage={payload.get('backtest_leverage')!r}")
    if payload.get("btc_trend_gate") not in (None, "uptrend"):
        failures.append(f"btc_trend_gate={payload.get('btc_trend_gate')!r}")
    expected_root = ROOTS.get(venue)
    data_root = payload.get("data_root")
    if expected_root is None or not data_root or Path(data_root).expanduser().resolve() != expected_root.resolve():
        failures.append(f"data_root={data_root!r}")
    summary_end_date = str(payload.get("end_date") or "")
    if not summary_end_date:
        failures.append(f"end_date={payload.get('end_date')!r}")
    else:
        try:
            boundary = dt.date.fromisoformat(summary_end_date)
        except ValueError:
            failures.append(f"end_date={summary_end_date!r}")
        else:
            current_date = as_of_date or dt.datetime.now(tz=dt.timezone.utc).date()
            if boundary > current_date:
                failures.append(
                    f"end_date={summary_end_date!r} exceeds complete UTC boundary "
                    f"{current_date.isoformat()!r}"
                )

    expected_by_cell = {
        profile.artifact_cell: profile
        for profile in ACTIVE_CONTINUOUS_COMPONENT_BY_KEY.values()
    }
    component_modes: list[str] = []
    for cell, component in component_payloads.items():
        cfg = component.get("config") or {}
        profile = expected_by_cell[cell]
        if cfg.get("profile_id") != CONTINUOUS_PROFILE_ID:
            failures.append(f"{cell}: profile_id={cfg.get('profile_id')!r}")
        if cfg.get("profile_revision") != CONTINUOUS_PROFILE_REVISION:
            failures.append(f"{cell}: profile_revision={cfg.get('profile_revision')!r}")
        if cfg.get("component_key") != profile.key:
            failures.append(f"{cell}: component_key={cfg.get('component_key')!r}")
        if cfg.get("entry_event_trigger") != profile.entry_event_trigger:
            failures.append(f"{cell}: entry_event_trigger={cfg.get('entry_event_trigger')!r}")
        if cfg.get("age_days_min") != profile.age_days_min:
            failures.append(f"{cell}: age_days_min={cfg.get('age_days_min')!r}")
        if abs(float(cfg.get("take_profit_pct") or 0.0) - profile.take_profit_pct) > 1e-12:
            failures.append(f"{cell}: take_profit_pct={cfg.get('take_profit_pct')!r}")
        if cfg.get("btc_trend_gate") != "uptrend":
            failures.append(f"{cell}: btc_trend_gate={cfg.get('btc_trend_gate')!r}")
        if cfg.get("use_funding") is not True:
            failures.append(f"{cell}: use_funding={cfg.get('use_funding')!r}")
        if cfg.get("start_date") != CONTINUOUS_HISTORY_START_DATE:
            failures.append(f"{cell}: start_date={cfg.get('start_date')!r}")
        if summary_end_date and cfg.get("end_date") != summary_end_date:
            failures.append(f"{cell}: end_date={cfg.get('end_date')!r}")
        component_modes.append(str(component.get("funding_mode") or "missing").lower())

    funding_modes = [
        *[str(mode).lower() for mode in payload.get("funding_modes") or []],
        *component_modes,
    ]
    if not funding_modes or any("missing" in mode for mode in funding_modes):
        failures.append(f"funding_modes={payload.get('funding_modes')!r}")
    elif any("partial" in mode for mode in funding_modes):
        failures.extend(
            _component_funding_failures_before(
                component_root,
                venue,
                cutoff_day_ms=cutoff_day_ms or utc_day_start_ms(),
            )
        )
    if failures:
        raise RuntimeError(
            f"component root does not match the code-defined TP12 base historical profile for {venue}; "
            "accepted-decision BTC-risk sizing is intentionally out of scope: "
            + ", ".join(failures)
        )
    return payload


def daily_closes(root: Path, symbol: str) -> dict[int, float]:
    df = (
        pl.scan_parquet(str(root / "klines_1h" / "**" / "*.parquet"))
        .filter(pl.col("symbol") == symbol)
        .select("ts_ms", "close")
        .collect()
        .with_columns(((pl.col("ts_ms") // MS_DAY) * MS_DAY).alias("day"))
        .group_by("day").agg(pl.col("close").last())
        .sort("day")
    )
    return {int(d): float(c) for d, c in df.iter_rows()}


def daily_returns(closes: dict[int, float]) -> dict[int, float]:
    days = sorted(closes)
    out: dict[int, float] = {}
    for prev, cur in zip(days, days[1:]):
        # Do not mislabel a multi-day move as a one-day return across a gap.
        if cur - prev == MS_DAY and closes[prev] > 0:
            out[cur] = closes[cur] / closes[prev] - 1.0
    return out


def load_component_for_warmstart(
    src: str,
    venue: str,
    *,
    component_root: Path,
):
    cell = ACTIVE_CONTINUOUS_COMPONENT_BY_KEY[src].artifact_cell
    return load_continuous_component_source(
        ContinuousComponentSource(component_root, cell),
        venue,
    )


def unit_series(venue: str, *, component_root: Path) -> dict[int, float]:
    comps = {
        src: load_component_for_warmstart(src, venue, component_root=component_root)[0]
        for src in WINNER
    }
    combined = combine_continuous_components(comps, WINNER)
    out: dict[int, float] = {}
    for day in combined.days:
        ret = combined.gross_by_day.get(day, 0.0) + combined.funding_by_day.get(day, 0.0)
        ret += scaled_entry_cost(combined.cost_events.get(day, []), 1.0, combined.impact_exponent)
        out[int(day)] = float(ret)
    return out


def regenerate(
    venue: str,
    n_days: int,
    *,
    component_root: Path,
    cutoff_day_ms: int | None = None,
) -> list[dict]:
    root = ROOTS[venue]
    unit = unit_series(venue, component_root=component_root)
    btc = daily_returns(daily_closes(root, "BTCUSDT"))
    eth = daily_returns(daily_closes(root, "ETHUSDT"))
    cutoff = cutoff_day_ms or utc_day_start_ms()
    eligible_days = [
        day
        for day in sorted(unit)
        if day < cutoff and day in btc and day in eth
    ]
    rows = []
    for day in eligible_days[-n_days:]:
        rows.append({
            "date": dt.datetime.fromtimestamp(day / 1000, tz=dt.timezone.utc).date().isoformat(),
            "unit_ret": unit[day],
            "btc_ret": btc[day],
            "eth_ret": eth[day],
        })
    return rows


def validate(venue: str, rows: list[dict]) -> dict:
    """Compare the regenerated series against the deployed CSV on overlapping dates.

    Returns a dict the overwrite gate consumes:
      max_drift : max |delta_unit_ret| over the overlap (0.0 when no CSV/overlap)
      old_rows  : row count of the existing CSV (0 when none)
      new_rows  : row count of the regenerated series
      overlap   : number of shared dates
    """
    path = OUT_DIR / f"{venue}_warmstart.csv"
    new_rows = len(rows)
    if not path.exists():
        print(f"  [{venue}] no existing CSV to validate against")
        return {"max_drift": 0.0, "old_rows": 0, "new_rows": new_rows, "overlap": 0}
    with path.open() as fh:
        old_rows = list(csv.DictReader(fh))
    return compare_unit_rows(
        reference_rows=old_rows,
        candidate_rows=rows,
        label=f"[{venue}] deployed tape",
    )


def compare_unit_rows(
    *,
    reference_rows: list[dict],
    candidate_rows: list[dict],
    label: str,
) -> dict:
    """Compare unit-return rows by date."""
    old = {r["date"]: float(r["unit_ret"]) for r in reference_rows}
    new = {r["date"]: float(r["unit_ret"]) for r in candidate_rows}
    overlap = sorted(set(old) & set(new))
    if not overlap:
        print(f"  {label}: no date overlap")
        return {
            "max_drift": 0.0,
            "beta_drift": 0.0,
            "old_rows": len(old),
            "new_rows": len(new),
            "overlap": 0,
        }
    diffs = [abs(old[d] - new[d]) for d in overlap]
    import statistics
    max_drift = max(diffs)
    beta_drift = _beta_drift(reference_rows, candidate_rows)
    print(
        f"  {label}: overlap {len(overlap)}d, max|delta_unit| {max_drift:.2e}, "
        f"mean|delta| {statistics.mean(diffs):.2e}, max|delta_beta| {beta_drift:.3f}"
    )
    return {
        "max_drift": max_drift,
        "beta_drift": beta_drift,
        "old_rows": len(old),
        "new_rows": len(new),
        "overlap": len(overlap),
    }


def _series_betas(rows: list[dict]) -> tuple[float, float]:
    """Runtime-identical trailing 2f betas for one date-ordered row series."""
    raw = [float(r["unit_ret"]) for r in rows]
    h1: list[float | None] = [float(r["btc_ret"]) for r in rows]
    h2: list[float | None] = [float(r["eth_ret"]) for r in rows]
    return compute_hedge_betas_2f(raw, h1, h2, len(raw), ContinuousHedgeRule())


def _beta_drift(reference_rows: list[dict], candidate_rows: list[dict]) -> float:
    """Max coefficient move between the deployed and regenerated priors.

    The unit-return drift gate catches input revisions; this catches the
    consequence that actually resizes the live hedge — the estimated betas
    jumping between vintages — even when every overlapping input row agrees
    (for example a window that merely slides forward over a regime break).
    """
    old_b1, old_b2 = _series_betas(reference_rows)
    new_b1, new_b2 = _series_betas(candidate_rows)
    return max(abs(new_b1 - old_b1), abs(new_b2 - old_b2))


def component_tape_metadata(component_root: Path, payload: dict) -> dict[str, str]:
    """Self-contained causal-boundary/provenance fields repeated in each row."""
    end_date = dt.date.fromisoformat(str(payload["end_date"]))
    summary_path = component_root.parent / str(payload["venue"]) / "continuous_equity_summary.json"
    return {
        "data_through_date": (end_date - dt.timedelta(days=1)).isoformat(),
        "source_summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
    }


def overwrite_blocked(venue: str, report: dict, *, max_drift: float, force: bool) -> str | None:
    """Reason the overwrite must be refused, or None to allow it.

    Guards the runtime 2f hedge model prior: a regenerated
    series that diverges materially from the deployed one, or that has fewer rows
    (a short/regressed run), must not silently overwrite + auto-deploy. --force
    is the explicit escape hatch for a known-good data-vintage shift.
    """
    if force:
        return None
    if report["old_rows"] and not report["overlap"]:
        return "regeneration has no date overlap with the existing CSV"
    if report["overlap"] and report["max_drift"] > max_drift:
        return (f"max|delta_unit| {report['max_drift']:.2e} over {report['overlap']}d exceeds "
                f"--max-unit-drift {max_drift:.2e}")
    if report["old_rows"] and report["new_rows"] < report["old_rows"]:
        return (f"regeneration has {report['new_rows']} rows < existing {report['old_rows']} "
                f"(short/regressed run)")
    if report["overlap"] and report.get("beta_drift", 0.0) > MAX_PRIOR_BETA_DRIFT:
        return (f"max|delta_beta| {report['beta_drift']:.3f} exceeds the "
                f"coefficient-drift bound {MAX_PRIOR_BETA_DRIFT:.2f}; a vintage shift "
                f"that resizes the live hedge this much needs --force and a review "
                f"per docs/trading_logic.md")
    return None


def object_replacement_blocked(report: dict, *, max_drift: float) -> str | None:
    """Gate an intentional component-object replacement."""
    if report["overlap"] < MIN_OBJECT_REFERENCE_OVERLAP:
        return (
            f"reference component overlap {report['overlap']}d < "
            f"required {MIN_OBJECT_REFERENCE_OVERLAP}d"
        )
    return overwrite_blocked("reference", report, max_drift=max_drift, force=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument(
        "--component-root",
        required=True,
        help=(
            "components directory from the standard equity runner; it contains base historical "
            "TP12 artifacts, not accepted-decision BTC-risk overlay state"
        ),
    )
    ap.add_argument(
        "--reference-component-root",
        default=None,
        help="prior code-defined TP12 component root used to authorize an object replacement",
    )
    ap.add_argument(
        "--replace-component-object",
        action="store_true",
        help="replace an obsolete model prior only when --reference-component-root parity passes",
    )
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--max-unit-drift", type=float, default=1e-3,
                    help="Refuse the overwrite when max|delta_unit_ret| over the overlap exceeds this "
                         "(unless --force). Default 1e-3 admits ledger-rebuild vintage drift.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite even when the drift/row-count gate would refuse (known-good "
                         "data-vintage shift).")
    args = ap.parse_args()
    venues = ("bybit",)
    component_root = Path(args.component_root).expanduser()
    reference_root = (
        Path(args.reference_component_root).expanduser()
        if args.reference_component_root
        else None
    )
    if args.replace_component_object and reference_root is None:
        ap.error("--replace-component-object requires --reference-component-root")
    refused = False
    for venue in venues:
        component_payload = validate_current_component_root(component_root, venue)
        rows = regenerate(venue, args.days, component_root=component_root)
        metadata = component_tape_metadata(component_root, component_payload)
        for row in rows:
            row.update(metadata)
        report = validate(venue, rows)
        last = rows[-1]["date"] if rows else "none"
        block = overwrite_blocked(venue, report, max_drift=args.max_unit_drift, force=args.force)
        if reference_root is not None:
            validate_current_component_root(reference_root, venue)
            reference_rows = regenerate(
                venue,
                args.days,
                component_root=reference_root,
            )
            reference_report = compare_unit_rows(
                reference_rows=reference_rows,
                candidate_rows=rows,
                label=f"[{venue}] code-defined TP12 base-component object",
            )
            reference_block = object_replacement_blocked(
                reference_report,
                max_drift=args.max_unit_drift,
            )
            if args.replace_component_object:
                if reference_block is not None:
                    block = reference_block
                else:
                    if block is not None:
                        print(
                            f"  [{venue}] deployed-tape drift is expected for the explicit "
                            "prior-object replacement; reference component parity passed"
                        )
                    block = None
        if args.validate_only:
            print(f"  [{venue}] would write {len(rows)} rows, last day {last}")
            if block is not None:
                print(f"  [{venue}] WOULD REFUSE overwrite: {block}. Re-run with --force to override.")
                refused = True
            continue
        if block is not None:
            print(f"  [{venue}] REFUSING overwrite: {block}. Re-run with --force to override.")
            refused = True
            continue
        path = OUT_DIR / f"{venue}_warmstart.csv"
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        with tmp_path.open("w", newline="") as fh:
            w = csv.DictWriter(
                fh,
                lineterminator="\n",
                fieldnames=[
                    "date",
                    "unit_ret",
                    "btc_ret",
                    "eth_ret",
                    "data_through_date",
                    "source_summary_sha256",
                ],
            )
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp_path, path)
        through = rows[-1].get("data_through_date", "unknown") if rows else "none"
        print(
            f"  [{venue}] wrote {len(rows)} rows -> {path.name}, "
            f"last unit day {last}, data through {through}"
        )
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
