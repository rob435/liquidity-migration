"""Regenerate deploy/hedge_warmstart/{venue}_warmstart.csv from current data.

THE refresh mechanism for the 2f hedge's beta warm-start (operator queue item:
"regenerate the CSVs and define refresh cadence"). Cadence = run this script at
every data-root refresh and commit the CSVs (they sit in the deploy paths
filter, so the commit auto-deploys them to the live units).

Construction (matches the engine the betas were banked on):
- components = the three current frozen continuous_ensemble_v2 cells (the parity-verified rebuilt
  ledgers; `scripts/rebuild_continuous_component_ledgers.py`) combined on the
  frozen receipt weights;
- unit_ret[day] = gross + funding + scale-1 entry costs per LEDGER day (the
  scale-independent day return `apply_rebalance_rule` scales);
- btc_ret/eth_ret = same-calendar-day daily close-to-close from klines_1h.

--validate compares the regenerated series against the existing CSV on
overlapping dates and GATES the overwrite (semantics check; small diffs are the
rebuilt-ledger vintage, e.g. p3 858 vs 857 trades). The warm-start CSV feeds the
live 2f hedge beta (continuous_hedge_manager.load_warmstart_2f) and auto-deploys
on commit, so a regression must not be written silently: if the max |delta_unit_ret|
over the overlap exceeds --max-unit-drift, or the regeneration has FEWER rows
than the banked CSV, the overwrite is REFUSED unless --force is given. --force
keeps the manual escape hatch for a legitimate data-vintage shift.

    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/regenerate_hedge_warmstart.py [--validate-only] [--days 200] \
        [--venues bybit,binance] [--component-root PATH] \
        [--max-unit-drift 1e-3] [--force]

``--component-root`` should point at the ``components`` directory emitted by
the official continuous equity runner. This lets the live Bybit warm-start be
rebuilt from the current TP/sizing object instead of silently falling back to
the older consolidated receipt configs.
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
    CONTINUOUS_COMPONENT_SOURCES,
    ContinuousComponentSource,
    load_continuous_component_source,
)
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    combine_continuous_components,
    scaled_entry_cost,
)

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
OUT_DIR = Path(__file__).resolve().parent.parent / "deploy" / "hedge_warmstart"
FALLBACK_COMPONENT_ROOT = Path(
    os.environ.get(
        "CONTINUOUS_COMPONENT_FALLBACK_ROOT",
        str(SHARED / "continuous_deployed_equity_refresh_2026-06-12" / "components"),
    )
).expanduser()
# Current three-component object frozen 2026-06-18; renorm = old/0.90.
WINNER = {"turn3p3": 0.3333333333333333, "turn4p3": 0.2222222222222222, "turn4p5": 0.4444444444444444}
MS_DAY = 86_400_000
LIVE_COMPONENT_TAKE_PROFIT_PCT = 0.12
LIVE_STRATEGY_RUN_LABEL = "continuous_demo_paper_research_stage"
LIVE_START_DATE = "2023-04-01"
MIN_OBJECT_REFERENCE_OVERLAP = 60


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
    """Return component funding defects that can enter the warm-start tape.

    An official run may aggregate to ``partial`` solely because its final rows
    represent positions that are still open today.  Those rows are excluded by
    ``regenerate``.  Any non-modeled trade whose exit day is already complete is
    a real historical coverage defect and remains a hard failure.
    """
    failures: list[str] = []
    audited_cells: set[str] = set()
    for src in WINNER:
        cell = CONTINUOUS_COMPONENT_SOURCES[src].cell
        if cell in audited_cells:
            continue
        audited_cells.add(cell)
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
        cell = CONTINUOUS_COMPONENT_SOURCES[src].cell
        if cell in payloads:
            continue
        report_path = component_root / venue / cell / "continuous_report.json"
        if not report_path.exists():
            raise RuntimeError(f"missing official component report: {report_path}")
        payloads[cell] = json.loads(report_path.read_text(encoding="utf-8"))
    return payloads


def validate_live_component_root(
    component_root: Path,
    venue: str,
    *,
    cutoff_day_ms: int | None = None,
    as_of_date: dt.date | None = None,
) -> dict:
    """Require an official current-object receipt before touching the live tape."""
    summary_path = component_root.parent / venue / "continuous_equity_summary.json"
    if not summary_path.exists():
        raise RuntimeError(f"missing official continuous summary: {summary_path}")
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    failures = []
    try:
        component_payloads = _component_report_payloads(component_root, venue)
    except (OSError, ValueError, RuntimeError) as exc:
        component_payloads = {}
        failures.append(str(exc))
    if payload.get("run_label") != "exploratory":
        failures.append(f"run_label={payload.get('run_label')!r}")
    if payload.get("strategy_run_label") != LIVE_STRATEGY_RUN_LABEL:
        failures.append(f"strategy_run_label={payload.get('strategy_run_label')!r}")
    if abs(float(payload.get("component_take_profit_pct") or 0.0) - LIVE_COMPONENT_TAKE_PROFIT_PCT) > 1e-12:
        failures.append(f"component_take_profit_pct={payload.get('component_take_profit_pct')!r}")
    if payload.get("btc_risk_sizing") is not True:
        failures.append(f"btc_risk_sizing={payload.get('btc_risk_sizing')!r}")
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

    component_modes: list[str] = []
    for cell, component in component_payloads.items():
        cfg = component.get("config") or {}
        if abs(float(cfg.get("take_profit_pct") or 0.0) - LIVE_COMPONENT_TAKE_PROFIT_PCT) > 1e-12:
            failures.append(f"{cell}: take_profit_pct={cfg.get('take_profit_pct')!r}")
        if cfg.get("btc_trend_gate") != "uptrend":
            failures.append(f"{cell}: btc_trend_gate={cfg.get('btc_trend_gate')!r}")
        if cfg.get("use_funding") is not True:
            failures.append(f"{cell}: use_funding={cfg.get('use_funding')!r}")
        if cfg.get("start_date") != LIVE_START_DATE:
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
            f"official component root is not the live TP12/BTC-risk object for {venue}: "
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
        # audit2: only emit a calendar-consecutive return; a missing UTC day
        # would otherwise mislabel a multi-day move as one day (mirrors the
        # gap-guarded twin in continuous_forward_replay_orchestrator.btc_inputs).
        if cur - prev == MS_DAY and closes[prev] > 0:
            out[cur] = closes[cur] / closes[prev] - 1.0
    return out


def load_component_for_warmstart(
    src: str,
    venue: str,
    *,
    component_root: Path | None = None,
):
    spec = CONTINUOUS_COMPONENT_SOURCES[src]
    if component_root is not None:
        return load_continuous_component_source(
            ContinuousComponentSource(component_root, spec.cell),
            venue,
        )
    try:
        return load_continuous_component_source(spec, venue)
    except FileNotFoundError as original_error:
        fallback = ContinuousComponentSource(FALLBACK_COMPONENT_ROOT, spec.cell)
        try:
            return load_continuous_component_source(fallback, venue)
        except FileNotFoundError:
            raise original_error


def unit_series(venue: str, *, component_root: Path | None = None) -> dict[int, float]:
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
    component_root: Path | None = None,
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
    """Compare the regenerated series against the banked CSV on overlapping dates.

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
    """Compare unit-return rows by date for an old tape or canonical object."""
    old = {r["date"]: float(r["unit_ret"]) for r in reference_rows}
    new = {r["date"]: float(r["unit_ret"]) for r in candidate_rows}
    overlap = sorted(set(old) & set(new))
    if not overlap:
        print(f"  {label}: no date overlap")
        return {"max_drift": 0.0, "old_rows": len(old), "new_rows": len(new), "overlap": 0}
    diffs = [abs(old[d] - new[d]) for d in overlap]
    import statistics
    max_drift = max(diffs)
    print(
        f"  {label}: overlap {len(overlap)}d, max|delta_unit| {max_drift:.2e}, "
        f"mean|delta| {statistics.mean(diffs):.2e} (vintage drift expected at ledger-rebuild scale)"
    )
    return {
        "max_drift": max_drift,
        "old_rows": len(old),
        "new_rows": len(new),
        "overlap": len(overlap),
    }


def live_tape_metadata(component_root: Path, payload: dict) -> dict[str, str]:
    """Self-contained freshness/provenance fields repeated in each CSV row."""
    end_date = dt.date.fromisoformat(str(payload["end_date"]))
    summary_path = component_root.parent / str(payload["venue"]) / "continuous_equity_summary.json"
    return {
        "data_through_date": (end_date - dt.timedelta(days=1)).isoformat(),
        "source_summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
    }


def overwrite_blocked(venue: str, report: dict, *, max_drift: float, force: bool) -> str | None:
    """Reason the overwrite must be refused, or None to allow it.

    Guards the live 2f-hedge warm-start CSV (backfill-writers-5): a regenerated
    series that diverges materially from the banked one, or that has FEWER rows
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
    return None


def object_replacement_blocked(report: dict, *, max_drift: float) -> str | None:
    """Gate an intentional old-tape -> current-live-object replacement."""
    if report["overlap"] < MIN_OBJECT_REFERENCE_OVERLAP:
        return (
            f"canonical current-object overlap {report['overlap']}d < "
            f"required {MIN_OBJECT_REFERENCE_OVERLAP}d"
        )
    return overwrite_blocked("reference", report, max_drift=max_drift, force=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument(
        "--venues",
        default=",".join(ROOTS),
        help="comma-separated venues to refresh; use bybit for the deployed live hedge",
    )
    ap.add_argument(
        "--component-root",
        default=None,
        help="optional official-runner components directory containing <venue>/<cell>/ artifacts",
    )
    ap.add_argument(
        "--reference-component-root",
        default=None,
        help="canonical prior TP12/BTC-risk components used to authorize an object replacement",
    )
    ap.add_argument(
        "--replace-live-object",
        action="store_true",
        help="replace an obsolete live tape only when --reference-component-root parity passes",
    )
    ap.add_argument(
        "--allow-legacy-component-sources",
        action="store_true",
        help="allow historical source/fallback ledgers instead of an official current-object receipt",
    )
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--max-unit-drift", type=float, default=1e-3,
                    help="Refuse the overwrite when max|delta_unit_ret| over the overlap exceeds this "
                         "(unless --force). Default 1e-3 admits ledger-rebuild vintage drift.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite even when the drift/row-count gate would refuse (known-good "
                         "data-vintage shift).")
    args = ap.parse_args()
    venues = [venue.strip() for venue in args.venues.split(",") if venue.strip()]
    unknown = sorted(set(venues) - set(ROOTS))
    if unknown:
        ap.error(f"unknown venue(s): {', '.join(unknown)}")
    component_root = Path(args.component_root).expanduser() if args.component_root else None
    reference_root = (
        Path(args.reference_component_root).expanduser()
        if args.reference_component_root
        else None
    )
    if component_root is None and not args.allow_legacy_component_sources:
        ap.error(
            "--component-root is required for the live hedge tape; run the official continuous "
            "equity workflow with TP12 + BTC-risk sizing first"
        )
    if args.replace_live_object and (component_root is None or reference_root is None):
        ap.error("--replace-live-object requires --component-root and --reference-component-root")
    if reference_root is not None and component_root is None:
        ap.error("--reference-component-root requires --component-root")
    refused = False
    for venue in venues:
        live_payload = None
        if component_root is not None:
            live_payload = validate_live_component_root(component_root, venue)
        rows = regenerate(venue, args.days, component_root=component_root)
        if component_root is not None and live_payload is not None:
            metadata = live_tape_metadata(component_root, live_payload)
            for row in rows:
                row.update(metadata)
        report = validate(venue, rows)
        last = rows[-1]["date"] if rows else "none"
        block = overwrite_blocked(venue, report, max_drift=args.max_unit_drift, force=args.force)
        if reference_root is not None:
            validate_live_component_root(reference_root, venue)
            reference_rows = regenerate(
                venue,
                args.days,
                component_root=reference_root,
            )
            reference_report = compare_unit_rows(
                reference_rows=reference_rows,
                candidate_rows=rows,
                label=f"[{venue}] canonical TP12/BTC-risk object",
            )
            reference_block = object_replacement_blocked(
                reference_report,
                max_drift=args.max_unit_drift,
            )
            if args.replace_live_object:
                if reference_block is not None:
                    block = reference_block
                else:
                    if block is not None:
                        print(
                            f"  [{venue}] deployed-tape drift is expected for the explicit "
                            "old-object replacement; canonical current-object parity passed"
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
