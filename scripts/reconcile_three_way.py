#!/usr/bin/env python3
"""Three-way demo<->backtest<->paper reconciliation for the LONG (v11a) and
CONTINUOUS (fade) sleeves.

The two-way ``scripts/reconcile.py`` covers demo<->paper execution slippage.
This driver adds the third corner — the model — so we can answer "does a backtest, run on
freshly-downloaded point-in-time data over the live forward window, agree with
what the demo and paper books actually did?".

Pipeline:
    0. refresh PIT data on the research root (manifest+klines -> today; funding is
       opt-in via --with-funding — it only affects backtest PnL, not entries)     [--no-data-refresh]
    1. pull the live demo+paper ledgers from the VPS (read-only)                 [--no-pull]
    2. LONG (discrete-event): run the v11a backtest over the forward window on
       the fresh root, then reconcile backtest entries vs demo and vs paper by
       (symbol, side, signal-day), plus the demo<->paper execution reconcile.
    3. CONTINUOUS (rebalance book): demo<->paper execution reconcile + the
       engine-decile signal-consistency of BOTH the demo and paper live entries
       (the faithful 'model' leg for a portfolio book — a costed continuous-events
       run cannot reproduce FROZEN_FORWARD_CONFIG's ensemble+hedge, but the shared
       decile pipeline can confirm each live entry was a genuine D9 pick).
    4. one unified three-way summary across the selected sleeves.

Why the two sleeves differ: LONG is a discrete event strategy whose entries pair
1:1 against the live trade ledger by (symbol, signal-day); CONTINUOUS is a daily
rebalance/portfolio book, so its faithful model leg is decile-membership of the
live entries, not a trade-ledger pairing.

Honest by default: read-only against the VPS, demo only, never real money. The
backtest leg is execution/agreement evidence — NOT alpha proof, NOT a promotion
gate (docs/backtesting_errors_we_never_repeat.md). The LONG backtest run_label is
surfaced verbatim; a biased / PIT-failed label is flagged, never hidden.

    bash scripts/reconcile_three_way.sh                       # both sleeves, full pipeline
    bash scripts/reconcile_three_way.sh --no-data-refresh     # skip the PIT download
    bash scripts/reconcile_three_way.sh --sleeves long        # one sleeve
    bash scripts/reconcile_three_way.sh --dry-run             # print every command, run nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent))

import reconcile as rc  # noqa: E402  reuse Step/SLEEVES/pull_sleeve/refresh_rmom/reconcile_long/continuous
import reconcile_fills as rf  # noqa: E402  fill-level entry-price cross-check (the 3rd corner's prices)

REPO = rc.REPO

# Where the fill-level per-entry CSVs land (next to the agreement keys).
RECONCILE_OUT = REPO / "data" / "reconcile"

# Forward-demo start per sleeve (the window over which the backtest can have a
# live counterpart). Recorded in STATE.md / deploy/sleeves.env:
#   LONG forward demo started 2026-06-04 (re-enabled 2026-06-16 after a toggle-off);
#   CONTINUOUS v2 baseline starts at the 2026-06-18 v2 deploy boundary.
DEMO_START: dict[str, str] = {
    "long": "2026-06-04",
    "continuous": "2026-06-18",
}

# The v11a production fc_min_day_return gate (the deployed value); one value = the
# production curve. Source of truth: _v11a_long_native_config().fc_min_day_return.
LONG_FC_VALUE = 0.15

# Data-read warmup before the forward window: the backtest reads/features only
# [window_start - warmup, window_end] instead of the full multi-year root (faster,
# and it scopes the full-PIT gate to the window so an ancient kline gap can't fail
# a recent run). Must exceed the longest lookback (universe_volume_window_days=90);
# 150 leaves comfortable margin.
LONG_READ_WARMUP_DAYS = 150

# PIT refresh window. The refresh start is normally derived from the root's
# CURRENT coverage (gap-only: start a few days before the last-covered date) so we
# don't re-verify already-present partitions across the whole ~600-symbol universe
# — that re-check pass is what made the first run crawl. PIT_REFRESH_MARGIN_DAYS
# is the small safety overlap before the last-covered date. The fixed lookback is
# only the fallback when coverage is unreadable.
PIT_REFRESH_MARGIN_DAYS = 3
PIT_REFRESH_LOOKBACK_DAYS = 21

# Hard wall-clock cap per data-refresh sub-stage (manifest / klines / funding). A
# rate-limited or pathological stage is aborted rather than hanging the pipeline
# (the stall guard). The refresh is incremental, so a re-run resumes.
DATA_REFRESH_STAGE_TIMEOUT_S = 2700  # 45 min

# Pair a live entry to a backtest entry when they share (symbol, side) and fall on
# the same UTC signal day — LONG is a daily-close-stamped strategy, so day-level
# keying is the robust, tolerance-free pairing.
LONG_REPORT_SUBDIR = "long_native_v11a_threeway"
LONG_CADENCE_REFERENCE_GLOB = "backtest-runs/long_v11a_*_refreshed_*/long/long_native_trades.csv"


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _day_iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date().isoformat()


def _ms(d: dt.date) -> int:
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)


# ----------------------------------------------------------------------------- step 0: PIT refresh
def _dataset_end(root: str, dataset: str) -> dt.date | None:
    """Latest `date=YYYY-MM-DD` partition present for a dataset (None if absent).
    Reads partition dir names only — cheap, no parquet."""
    base = Path(root).expanduser() / dataset
    if not base.exists():
        return None
    best: dt.date | None = None
    for p in base.glob("date=*"):
        try:
            d = dt.date.fromisoformat(p.name.split("=", 1)[1])
        except ValueError:
            continue
        if best is None or d > best:
            best = d
    return best


def _filter_marker_path(root: str) -> Path:
    """Sentinel recording the kline coverage end the manifest was last
    filter-manifest'd against (scripts-7)."""
    return Path(root).expanduser() / "archive_trade_manifest" / ".filtered_through"


def _filter_marker_current(root: str, kline_end: dt.date | None) -> bool:
    """True if the manifest was filtered against klines covering at least
    ``kline_end`` — i.e. the on-disk manifest is consistent with the current klines.
    A missing/older marker (or an interrupted prior run) returns False so the caller
    re-runs filter-manifest instead of trusting a stale, unfiltered manifest."""
    if kline_end is None:
        return False
    try:
        marked = dt.date.fromisoformat(_filter_marker_path(root).read_text().strip())
    except (OSError, ValueError):
        return False
    return marked >= kline_end


def _write_filter_marker(step: rc.Step, root: str) -> None:
    """Record the current kline coverage end after a successful filter-manifest."""
    if step.dry_run:
        return
    kline_end = _dataset_end(root, "klines_1h")
    if kline_end is None:
        return
    try:
        _filter_marker_path(root).write_text(kline_end.isoformat() + "\n")
    except OSError as exc:  # pragma: no cover - diagnostics only
        print(f"  (could not write filter marker: {exc})", flush=True)


def _coverage_ends(root: str) -> dict[str, dt.date | None]:
    """Latest covered date for each PIT dataset that gates the backtest
    (manifest, klines, funding)."""
    try:
        from liquidity_migration.pit_coverage import coverage_status
        cov = coverage_status(str(Path(root).expanduser()))
        manifest_end, kline_end = cov.manifest_end, cov.kline_end
    except Exception:
        manifest_end = _dataset_end(root, "archive_trade_manifest")
        kline_end = _dataset_end(root, "klines_1h")
    # Resolve funding through the venue-aware resolver: a Binance full-PIT root stores
    # funding as `binance_usdm_funding`, so a literal "funding" scan returns None even
    # when funding is fully populated, under-refreshing the funding window
    # (audit-iter1 scripts-4). Bybit roots carry a canonical `funding/` dir, so this is
    # a no-op there.
    try:
        from liquidity_migration.storage import resolve_dataset_name
        funding_name = resolve_dataset_name(Path(root).expanduser(), "funding")
    except Exception:
        funding_name = "funding"
    return {"manifest": manifest_end, "klines": kline_end, "funding": _dataset_end(root, funding_name)}


def _refresh_start(root: str, sleeves: list[str], today: dt.date, *, include_funding: bool) -> dt.date:
    """Pick the PIT-refresh start as a GAP-ONLY window: a few days before the
    STALEST gating dataset, so we re-download only what's missing instead of
    re-verifying already-present partitions for the whole universe (that re-check
    pass is what stalled the first run). Funding is only considered when it will
    actually be refreshed (--with-funding) — otherwise stale funding must NOT drag
    the manifest/kline window back. Falls back to a fixed lookback from the
    earliest sleeve demo-start when nothing can be read."""
    cov = _coverage_ends(root)
    keys = ["manifest", "klines"] + (["funding"] if include_funding else [])
    ends = [cov[k] for k in keys if cov.get(k) is not None]
    if ends:
        return min(ends) - dt.timedelta(days=PIT_REFRESH_MARGIN_DAYS)
    starts = [_date(DEMO_START[s]) for s in sleeves if s in DEMO_START]
    return (min(starts) if starts else today) - dt.timedelta(days=PIT_REFRESH_LOOKBACK_DAYS)


def _run_timed(step: rc.Step, cmd: list[str], *, timeout_s: int, label: str) -> None:
    """Run a refresh sub-stage with a hard wall-clock cap. A rate-limited or
    pathological stage is aborted rather than hanging the whole pipeline — the
    refresh is incremental, so a re-run resumes from the current coverage."""
    printable = " ".join(c if (" " not in c and "*" not in c) else f'"{c}"' for c in cmd)
    print(f"$ {printable}", flush=True)
    if step.dry_run:
        return
    try:
        proc = subprocess.run(cmd, cwd=str(REPO), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"\n❌ PIT refresh stage '{label}' exceeded the {timeout_s}s stall guard and was "
            f"aborted. Re-run (it resumes from current coverage), raise --data-refresh-timeout "
            f"if the stage legitimately needs longer, or pass --no-data-refresh to skip it."
        )
    if proc.returncode != 0:
        raise SystemExit(f"\n❌ PIT refresh stage '{label}' failed (exit {proc.returncode})")


def refresh_pit_data(step: rc.Step, root: str, start: dt.date, end: dt.date,
                     *, stage_timeout_s: int = DATA_REFRESH_STAGE_TIMEOUT_S,
                     refresh_funding: bool = False) -> None:
    """Bring the research root current over a bounded tail window: archive manifest
    + 1h klines (+ funding only when refresh_funding). An INCREMENTAL tail refresh
    (the full build_full_pit_bybit.sh is for a clean 2021-> rebuild). Without the
    manifest+kline refresh the backtest leg would run on stale data and PIT-fail
    (or survivorship-bias) the forward window.

    Funding is OFF by default: the three-way ENTRY agreement (which entries the
    model vs live took) depends on klines+manifest+signals, NOT funding — funding
    only changes the backtest's PnL/cost (the funding_partial label). A
    full-universe funding backfill is slow (retry-on-empty across ~800 symbols), so
    it's opt-in via --with-funding for when costed PnL is actually wanted.

    Two anti-stall guards (the first run churned 3h+ re-checking already-present
    partitions for the whole ~600-symbol universe):
      * COVERAGE SHORT-CIRCUIT — each dataset group is skipped when it already
        covers the target day, so a current root does ~no work.
      * STALL GUARD — every sub-stage runs under a hard wall-clock timeout.

    The manifest stage passes --allow-degraded on purpose: a bounded window covers
    fewer symbols than the all-time persisted manifest, tripping the (full-rebuild)
    survivorship "universe shrank" guard. The writer UNIONS with the persisted
    manifest (archive_manifest._union_with_persisted_manifest), so a narrow rebuild
    AUGMENTS coverage and never drops a once-covered (symbol, date) — the
    documented "intentional narrower rebuild" path, not a real degradation."""
    step.banner(f"Refresh PIT data on {root}  ({start} .. {end}, exclusive)")
    s, e = start.isoformat(), end.isoformat()
    target = end - dt.timedelta(days=1)  # latest day the window needs (end is exclusive)
    ends = _coverage_ends(root)

    def _current(name: str) -> bool:
        d = ends.get(name)
        return d is not None and d >= target

    def _filter_cmd() -> list[str]:
        # Filter the manifest to >=20-bar kline coverage so manifest membership and
        # klines stay consistent (else the full-PIT gate trips pit_membership_fail).
        return [rc._py(), "-m", "liquidity_migration.binance_vision",
                "filter-manifest", "--data-root", root]

    # Manifest + klines (+ filter): the slow universe-wide legs. Skip ENTIRELY when
    # both already cover the target — this is what stops the multi-hour re-check churn.
    # But "partitions present" != "manifest filtered against them": a prior run that
    # aborted after klines but before filter-manifest leaves an UNFILTERED manifest
    # that the date-based coverage check can't detect, which then trips
    # pit_membership_fail in the backtest leg. So the skip also requires the
    # filtered-through marker to cover the current klines (scripts-7).
    klines_current = _current("manifest") and _current("klines")
    if klines_current and _filter_marker_current(root, ends.get("klines")):
        print(f"  manifest+klines already cover {target} and the manifest is filtered "
              f"through them — skipping manifest/kline/filter refresh.", flush=True)
    elif klines_current:
        # Partitions cover target but the manifest was never filtered against the
        # current klines (interrupted prior run). Run ONLY the cheap filter step
        # rather than the full universe-wide archive scrape.
        print("  manifest+klines cover target but the filter marker is stale/missing "
              "— running filter-manifest only.", flush=True)
        _run_timed(step, _filter_cmd(), timeout_s=stage_timeout_s, label="filter-manifest")
        _write_filter_marker(step, root)
    else:
        # 1. PIT (symbol, date) membership manifest (archive scrape + v5 listing merge).
        _run_timed(step, rc._cli("--data-root", root, "archive-manifest",
                                 "--start", s, "--end", e, "--workers", "16", "--allow-degraded"),
                   timeout_s=stage_timeout_s, label="archive-manifest")
        # 2. 1h klines via the v5 API, manifest-gated, missing-only.
        _run_timed(step, rc._cli("--data-root", root, "archive-download-klines-1h-api",
                                 "--category", "linear", "--start", s, "--end", e, "--workers", "8"),
                   timeout_s=stage_timeout_s, label="klines-1h")
        # 3. Filter the manifest to >=20-bar kline coverage (see _filter_cmd).
        _run_timed(step, _filter_cmd(), timeout_s=stage_timeout_s, label="filter-manifest")
        _write_filter_marker(step, root)

    # 4. Funding (+ OI / mark / index / premium) — OFF by default; only feeds the
    #    backtest's carry cost (PnL), not the entry agreement. Opt in for costed PnL.
    if not refresh_funding:
        print("  funding refresh OFF — not needed for entry agreement; "
              "pass --with-funding for costed PnL.", flush=True)
    elif _current("funding"):
        print(f"  funding already covers {target} — skipping funding refresh.", flush=True)
    else:
        syms = _manifest_symbols(root)
        if syms:
            print(f"  (funding refresh for {syms.count(',') + 1} manifest symbols — slow)", flush=True)
            _run_timed(step, rc._cli("--data-root", root, "download-data", "--symbols", syms,
                                     "--start", s, "--end", e,
                                     "--datasets",
                                     "funding,open_interest,mark_price_1h,index_price_1h,premium_index_1h",
                                     "--workers", "8"),
                       timeout_s=stage_timeout_s, label="funding")
    if not step.dry_run:
        _print_coverage(root)


def _manifest_symbols(root: str) -> str:
    """Comma-joined unique symbols from the persisted archive_trade_manifest
    (the symbol allowlist download-data needs). Empty string if unreadable."""
    try:
        import polars as pl
        base = Path(root).expanduser() / "archive_trade_manifest"
        files = [str(p) for p in base.glob("**/*.parquet")]
        if not files:
            return ""
        df = pl.read_parquet(files, columns=["symbol"])
        return ",".join(sorted(df["symbol"].unique().to_list()))
    except Exception as exc:  # pragma: no cover - diagnostics only
        print(f"  (could not derive manifest symbols for funding refresh: {exc})", flush=True)
        return ""


def _print_coverage(root: str) -> None:
    try:
        from liquidity_migration.pit_coverage import coverage_status, format_coverage
        print(format_coverage(coverage_status(str(Path(root).expanduser()))), flush=True)
    except Exception as exc:  # pragma: no cover - diagnostics only
        print(f"(coverage check skipped: {exc})", flush=True)


# ----------------------------------------------------------------------------- step 2: LONG backtest leg
def run_long_backtest(step: rc.Step, root: str, start: dt.date, end: dt.date) -> tuple[Path, str]:
    """Run the v11a long backtest over the forward window on the fresh root.
    Returns (trades_csv_path, run_label)."""
    step.banner(f"LONG backtest (v11a) over {start} .. {end} on {root}")
    step.run(rc._script(
        "long_native_sweep_fc_min_day.py",
        "--data-root", root,
        "--values", f"{LONG_FC_VALUE}",
        "--read-warmup-days", str(LONG_READ_WARMUP_DAYS),
        "--start", start.isoformat(),
        "--end", end.isoformat(),
        "--report-subdir", LONG_REPORT_SUBDIR,
    ))
    run_dir = Path(root).expanduser() / "reports" / LONG_REPORT_SUBDIR / f"fc_min_day_{_fc_tag(LONG_FC_VALUE)}"
    trades_csv = run_dir / "long_native_trades.csv"
    label = "unknown"
    report_json = run_dir / "long_native_research_report.json"
    if report_json.exists():
        try:
            label = json.loads(report_json.read_text()).get("run_label", "unknown")
        except Exception:
            pass
    return trades_csv, label


def _fc_tag(value: float) -> str:
    # mirror long_native_sweep_fc_min_day._fmt_pct_tag
    return f"{value:.4f}".replace(".", "p").replace("-", "m")


def _backtest_keys(trades_csv: Path, start_ms: int, end_ms: int) -> set[tuple[str, str, str]]:
    if not trades_csv.exists():
        return set()
    import polars as pl
    df = pl.read_csv(trades_csv)
    return _key_set(df, "entry_signal_ts_ms", start_ms, end_ms)


def _live_keys(root: str, dataset: str, start_ms: int, end_ms: int) -> set[tuple[str, str, str]]:
    try:
        from liquidity_migration.storage import read_dataset
        df = read_dataset(REPO / root, dataset)
    except Exception:
        return set()
    return _key_set(df, "signal_ts_ms", start_ms, end_ms)


def _key_set(df, sig_col: str, start_ms: int, end_ms: int) -> set[tuple[str, str, str]]:
    """Set of (symbol, side, signal-day) entries inside [start_ms, end_ms)."""
    if df is None or df.is_empty() or sig_col not in df.columns or "symbol" not in df.columns:
        return set()
    out: set[tuple[str, str, str]] = set()
    have_side = "side" in df.columns
    cols = ["symbol", sig_col] + (["side"] if have_side else [])
    for r in df.select(cols).drop_nulls(subset=[sig_col]).iter_rows(named=True):
        ts = int(r[sig_col])
        if ts < start_ms or ts >= end_ms:
            continue
        side = (str(r["side"]).lower() if have_side and r["side"] is not None else "")
        out.add((r["symbol"], side, _day_iso(ts)))
    return out


def _quantile_nearest(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(q * len(ordered)))
    return int(ordered[idx])


def _venue_from_reference_path(path: Path) -> str:
    text = str(path).replace("\\", "/").lower()
    if "binance" in text:
        return "binance"
    if "bybit" in text:
        return "bybit"
    return path.parent.parent.name


def _long_cadence_stats(path: Path, *, as_of: dt.date, forward_start: dt.date) -> dict[str, object] | None:
    """Sparse-cadence context for LONG zero-trade reconciles."""
    if not path.exists():
        return None
    import polars as pl
    try:
        trades = pl.read_csv(path)
    except Exception:
        return None
    if trades.is_empty() or "entry_ts_ms" not in trades.columns:
        return None
    try:
        entries = (
            trades
            .with_columns(pl.col("entry_ts_ms").cast(pl.Int64).alias("_entry_ts_ms"))
            .with_columns(pl.from_epoch(pl.col("_entry_ts_ms"), time_unit="ms").dt.date().alias("_entry_date"))
            .sort("_entry_ts_ms")
        )
    except Exception:
        return None
    dates = entries.select("_entry_date").drop_nulls().unique().sort("_entry_date")["_entry_date"].to_list()
    if not dates:
        return None
    gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    first_ms = int(entries["_entry_ts_ms"].min())
    last_ms = int(entries["_entry_ts_ms"].max())
    span_days = max((last_ms - first_ms) / 86_400_000.0, 1.0)
    current_gap = max((as_of - dates[-1]).days, 0)
    p95 = _quantile_nearest(gaps, 0.95)
    max_gap = max(gaps) if gaps else current_gap
    if current_gap > max_gap:
        status = "above_historical_max"
    elif p95 and current_gap >= max(p95 - 2, int(0.9 * p95)):
        status = "near_or_above_p95"
    else:
        status = "inside_history"
    return {
        "venue": _venue_from_reference_path(path),
        "trades": int(entries.height),
        "entry_days": len(dates),
        "first_entry": dates[0].isoformat(),
        "last_entry": dates[-1].isoformat(),
        "trades_per_30d": int(entries.height) / span_days * 30.0,
        "p95_gap_days": int(p95),
        "max_gap_days": int(max_gap),
        "current_gap_days": int(current_gap),
        "trades_since_forward_start": int(entries.filter(pl.col("_entry_ts_ms") >= _ms(forward_start)).height),
        "status": status,
    }


def _format_long_cadence_diagnostic(
    reference_paths: list[Path],
    *,
    as_of: dt.date,
    forward_start: dt.date,
) -> str | None:
    stats = [
        s for s in (
            _long_cadence_stats(path, as_of=as_of, forward_start=forward_start)
            for path in reference_paths
        )
        if s is not None
    ]
    if not stats:
        return None
    order = {"above_historical_max": 3, "near_or_above_p95": 2, "inside_history": 1}
    overall = max((str(s["status"]) for s in stats), key=lambda s: order.get(s, 0))
    pieces = []
    for s in sorted(stats, key=lambda x: str(x["venue"])):
        pieces.append(
            f"{s['venue']}: gap={s['current_gap_days']}d p95={s['p95_gap_days']}d "
            f"max={s['max_gap_days']}d trades_since_forward_start={s['trades_since_forward_start']} "
            f"rate={float(s['trades_per_30d']):.2f}/30d"
        )
    return f"LONG cadence diagnostic [{overall}]: " + "; ".join(pieces)


def _long_cadence_reference_paths() -> list[Path]:
    return sorted(REPO.glob(LONG_CADENCE_REFERENCE_GLOB))


def reconcile_long_three_way(step: rc.Step, *, trades_csv: Path, run_label: str,
                             demo_root: str, paper_root: str,
                             start: dt.date, end: dt.date) -> tuple[str, bool]:
    """Pair backtest / demo / paper LONG entries by (symbol, side, signal-day)."""
    step.banner("LONG three-way: backtest <-> demo <-> paper (entry agreement)")
    start_ms, end_ms = _ms(start), _ms(end)
    model = _backtest_keys(trades_csv, start_ms, end_ms)
    demo = _live_keys(demo_root, "long_native_demo_trades", start_ms, end_ms)
    paper = _live_keys(paper_root, "long_native_paper_trades", start_ms, end_ms)

    confirmed = model & demo & paper
    model_demo = model & demo
    model_paper = model & paper
    demo_paper = demo & paper
    model_only = model - demo - paper
    demo_not_in_model = demo - model           # red flag: live entered, model did not
    paper_not_in_model = paper - model          # red flag: live entered, model did not

    # Persist the union with membership flags for audit.
    out_dir = REPO / "data" / "reconcile" / "long_three_way"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "long_three_way_keys.csv"
    rows = ["symbol,side,signal_day,in_backtest,in_demo,in_paper"]
    for key in sorted(model | demo | paper):
        sym, side, day = key
        rows.append(f"{sym},{side},{day},{int(key in model)},{int(key in demo)},{int(key in paper)}")
    csv_path.write_text("\n".join(rows) + "\n")

    label_flag = ""
    if run_label not in ("full_pit_universe", "full_pit_universe_funding_partial",
                         "full_pit_universe_funding_missing"):
        label_flag = f"  ⚠️ backtest run_label={run_label} (NOT clean full-PIT — treat as biased diagnostic)"

    summary = (
        f"long three-way: backtest={len(model)} demo={len(demo)} paper={len(paper)} | "
        f"confirmed(all3)={len(confirmed)} model∩demo={len(model_demo)} model∩paper={len(model_paper)} "
        f"demo∩paper={len(demo_paper)} | model_only={len(model_only)} "
        f"demo_not_in_model={len(demo_not_in_model)} paper_not_in_model={len(paper_not_in_model)} "
        f"| backtest_run_label={run_label} window={start}..{end} keys_csv={csv_path}{label_flag}"
    )
    cadence_summary = _format_long_cadence_diagnostic(
        _long_cadence_reference_paths(),
        as_of=end - dt.timedelta(days=1),
        forward_start=start,
    )
    if cadence_summary:
        summary = f"{summary}\n  {cadence_summary}"
    print("\n" + summary, flush=True)
    if demo_not_in_model:
        print(f"  ⚠️ {len(demo_not_in_model)} demo entries with NO matching backtest signal "
              f"(possible look-ahead in live / stale-PIT in backtest / threshold drift): "
              f"{sorted(demo_not_in_model)[:10]}", flush=True)
    if model_only and not demo and not paper:
        print("  ℹ️ live LONG book took no in-window entries (sleeve re-enabled 2026-06-16); "
              "model_only entries are expected, not a drift signal.", flush=True)
    # A leg "passes" the agreement gate only when no LIVE entry lacks a model
    # justification. model_only is expected (sleeve off / just enabled), so it is
    # NOT a failure.
    ok = (len(demo_not_in_model) == 0 and len(paper_not_in_model) == 0)
    return summary, ok


# ----------------------------------------------------------------------------- step 3: CONTINUOUS model leg
def continuous_signal_leg(
    step: rc.Step,
    *,
    demo_root: str,
    paper_root: str,
    start_ts_ms: int = rc.CONTINUOUS_V2_START_MS,
) -> tuple[str, bool]:
    """Engine-decile signal-consistency of the live continuous entries — the
    faithful 'model' leg for the rebalance book. Run against BOTH demo and paper."""
    step.banner("CONTINUOUS model leg: engine-decile signal-consistency (demo + paper)")
    rc_d, out_d = step.run_capture(
        rc._script(
            "continuous_demo_signal_check.py",
            "--root", demo_root,
            "--trades-dataset", "continuous_fade_demo_trades",
            "--start-ts-ms", str(start_ts_ms),
            "--strategy-id", rc.CONTINUOUS_V2_DEMO_STRATEGY_ID,
        )
    )
    demo_sum, demo_ok = rc._summarize_leg(out_d, "SUMMARY:", rc_d)
    rc_p, out_p = step.run_capture(
        rc._script(
            "continuous_demo_signal_check.py",
            "--root", paper_root,
            "--trades-dataset", "continuous_fade_paper_trades",
            "--start-ts-ms", str(start_ts_ms),
            "--strategy-id", rc.CONTINUOUS_V2_PAPER_STRATEGY_ID,
        )
    )
    paper_sum, paper_ok = rc._summarize_leg(out_p, "SUMMARY:", rc_p)
    summary = f"demo: {demo_sum}  ||  paper: {paper_sum}"
    return summary, (demo_ok and paper_ok)


# ----------------------------------------------------------------------------- main
def main() -> int:
    p = argparse.ArgumentParser(
        description="Three-way demo<->backtest<->paper reconciliation for LONG + CONTINUOUS.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--sleeves", default="long,continuous",
                   help="Comma list of sleeves (long,continuous).")
    p.add_argument("--bybit-root", default=rc.DEFAULT_BYBIT_ROOT,
                   help="Research root for the PIT refresh + backtest leg.")
    p.add_argument("--vps", default=rc.VPS_HOST, help="VPS ssh target for the ledger pull.")
    p.add_argument("--no-pull", action="store_true", help="Skip the VPS ledger rsync; use local ledgers.")
    p.add_argument("--no-data-refresh", action="store_true",
                   help="Skip the PIT data download (use the root as-is — backtest may be stale).")
    p.add_argument("--with-funding", action="store_true",
                   help="Also refresh funding (slow, full-universe). OFF by default: funding "
                        "affects backtest PnL only, not the entry agreement. Use for costed PnL.")
    p.add_argument("--no-rmom", action="store_true",
                   help="Skip the research-root residual_momentum recompute. By DEFAULT the continuous "
                        "backtest-match runs on the research root (independent PIT klines_1h + freshly "
                        "recomputed rmom); --no-rmom (or --no-data-refresh) falls back to the live signal "
                        "plane — current data, but not an independent recompute.")
    p.add_argument("--with-rmom", action="store_true",
                   help="Deprecated no-op — the research-root rmom recompute is ON by default now "
                        "(use --no-rmom to skip). Accepted so old invocations don't error.")
    p.add_argument("--backtest-start", default=None,
                   help="Override the backtest/forward-window start (YYYY-MM-DD). "
                        "Default: per-sleeve demo start.")
    p.add_argument("--data-refresh-timeout", type=int, default=DATA_REFRESH_STAGE_TIMEOUT_S,
                   help="Hard wall-clock cap (seconds) per PIT-refresh sub-stage (stall guard).")
    p.add_argument("--dry-run", action="store_true", help="Print every command without running anything.")
    args = p.parse_args()

    sleeves = [s.strip() for s in args.sleeves.split(",") if s.strip()]
    bad = [s for s in sleeves if s not in rc.SLEEVES]
    if bad:
        raise SystemExit(f"unknown sleeve(s) {bad}; valid: {', '.join(rc.ALL_SLEEVES)}")
    today = rc._today()
    root = args.bybit_root
    step = rc.Step(args.dry_run)
    # window end is exclusive; +1 day so today's signal day is covered.
    win_end = today + dt.timedelta(days=1)

    print(f"liquidity-migration THREE-WAY reconcile — {today.isoformat()} (UTC)")
    print(f"  sleeves       : {', '.join(sleeves)}")
    print(f"  research root : {root}")
    print(f"  data refresh  : {'OFF (--no-data-refresh)' if args.no_data_refresh else 'ON'}")

    # 0. Refresh PIT data on the research root (shared by the LONG backtest + the
    #    continuous research-root rmom panel). Only needed when a backtest leg runs.
    if not args.no_data_refresh:
        refresh_from = _refresh_start(root, sleeves, today, include_funding=args.with_funding)
        refresh_pit_data(step, root, refresh_from, win_end,
                         stage_timeout_s=args.data_refresh_timeout, refresh_funding=args.with_funding)

    # 1. Pull every selected sleeve's live demo+paper ledgers.
    if not args.no_pull:
        for s in sleeves:
            rc.pull_sleeve(step, args.vps, s)

    # 1b. Research-root residual_momentum recompute — DEFAULT ON for continuous so the
    #     INDEPENDENT-PIT backtest-match runs on a fresh research rmom panel. Skipped under
    #     --no-rmom (use the on-disk panel) or --no-data-refresh (don't pair a heavy rmom
    #     recompute with a "skip the slow stuff" request). The independent-PIT check itself
    #     ALWAYS runs for continuous (it's cheap and informational); it just confirms fewer
    #     entries when the research panel's coverage lags. That lag is the derivative-metric
    #     inputs build_feature_panel reads (open_interest/premium): the default refresh updates
    #     only manifest+klines, so they go stale and truncate rmom (here to ~06-02; --with-funding
    #     tops them up). rmom itself is causal (~2-3d, shift(3)); those entries sit in pending_rmom.
    do_rmom_recompute = ("continuous" in sleeves) and not args.no_rmom and not args.no_data_refresh
    if do_rmom_recompute:
        rc.refresh_rmom(step, root, today)

    summary: dict[str, str] = {}
    ok: dict[str, bool] = {}

    # 2. LONG: backtest leg + three-way entry agreement + demo<->paper execution.
    if "long" in sleeves:
        lp = rc.SLEEVES["long"]["paper"][1]   # type: ignore[index]
        ld = rc.SLEEVES["long"]["demo"][1]    # type: ignore[index]
        bt_start = _date(args.backtest_start) if args.backtest_start else _date(DEMO_START["long"])
        exec_summary, exec_ok = rc.reconcile_long(step, paper=lp, demo=ld)
        if args.dry_run:
            # still echo the backtest commands in dry-run
            run_long_backtest(step, root, bt_start, win_end)
            step.banner("LONG three-way: backtest <-> demo <-> paper (entry agreement)")
            print("$ (pair backtest/demo/paper entries by symbol+side+signal-day)")
            step.banner("LONG fills: backtest <-> demo <-> paper (entry-price cross-check)")
            print("$ (join entry_price across the 3 books; write long_three_way_fills.csv)")
            summary["long"], ok["long"] = "(dry-run)", True
        else:
            trades_csv, label = run_long_backtest(step, root, bt_start, win_end)
            tw_summary, tw_ok = reconcile_long_three_way(
                step, trades_csv=trades_csv, run_label=label,
                demo_root=ld, paper_root=lp, start=bt_start, end=win_end)
            step.banner("LONG fills: backtest <-> demo <-> paper (entry-price cross-check)")
            fills_summary, fills_ok = rf.fills_long(
                trades_csv=trades_csv, demo_root=ld, paper_root=lp,
                start=bt_start, end=win_end, out_dir=RECONCILE_OUT)
            print("\n" + fills_summary, flush=True)
            summary["long"] = f"{tw_summary}\n  exec(paper↔demo): {exec_summary}\n  {fills_summary}"
            ok["long"] = tw_ok and exec_ok and fills_ok

    # 3. CONTINUOUS: demo<->paper execution + engine-decile model leg (demo + paper).
    if "continuous" in sleeves:
        cp = rc.SLEEVES["continuous"]["paper"][1]   # type: ignore[index]
        cd = rc.SLEEVES["continuous"]["demo"][1]    # type: ignore[index]
        cont_start = _date(args.backtest_start) if args.backtest_start else _date(DEMO_START["continuous"])
        cont_start_ms = _ms(cont_start) if args.backtest_start else rc.CONTINUOUS_V2_START_MS
        exec_summary, exec_ok = rc.reconcile_continuous(
            step,
            paper=cp,
            demo=cd,
            start_ts_ms=cont_start_ms,
            strategy_profile=rc.CONTINUOUS_V2_PROFILE,
            paper_strategy_id=rc.CONTINUOUS_V2_PAPER_STRATEGY_ID,
            demo_strategy_id=rc.CONTINUOUS_V2_DEMO_STRATEGY_ID,
        )
        # The backtest-match always recomputes on the LIVE signal plane (primary gate) AND
        # on the independent-PIT research root (secondary; back-fills as the rmom horizon ages).
        cont_kroot = root  # inside `if "continuous" in sleeves` -> the research root
        if args.dry_run:
            continuous_signal_leg(step, demo_root=cd, paper_root=cp, start_ts_ms=cont_start_ms)
            step.banner("CONTINUOUS backtest-match: engine recompute <-> demo <-> paper (entries + fills)")
            print("$ (recompute the per-component engine candidates on the live signal-plane (primary, "
                  "gates now) + independent-PIT research root (secondary, back-fills as rmom ages); "
                  "cross-check entries + entry-price; write continuous_three_way_fills.csv)")
            summary["continuous"], ok["continuous"] = "(dry-run)", True
        else:
            model_summary, model_ok = continuous_signal_leg(step, demo_root=cd, paper_root=cp, start_ts_ms=cont_start_ms)
            step.banner("CONTINUOUS backtest-match: engine recompute <-> demo <-> paper (entries + fills)")
            fills_summary, fills_ok = rf.fills_continuous(
                demo_root=cd, paper_root=cp, start=cont_start, end=win_end, out_dir=RECONCILE_OUT,
                research_root=cont_kroot, start_ms_override=cont_start_ms,
                demo_strategy_id=rc.CONTINUOUS_V2_DEMO_STRATEGY_ID,
                paper_strategy_id=rc.CONTINUOUS_V2_PAPER_STRATEGY_ID)
            print("\n" + fills_summary, flush=True)
            summary["continuous"] = (f"model(decile): {model_summary}\n  exec(paper↔demo): {exec_summary}"
                                     f"\n  {fills_summary}")
            ok["continuous"] = model_ok and exec_ok and fills_ok

    # 4. Unified headline.
    if args.dry_run:
        print("\n✅ done (dry-run).")
        return 0
    print(f"\n{'=' * 72}\nTHREE-WAY RECONCILIATION SUMMARY — demo ↔ backtest ↔ paper\n{'=' * 72}")
    for s in sleeves:
        print(f"\n## {rc.SLEEVES[s]['label']}")
        print(f"  {summary.get(s, '(skipped)')}")
    print(f"\nReports under: {REPO / 'data' / 'reconcile'}")
    print("\nNote: the backtest leg is agreement/execution evidence, NOT alpha proof and NOT a "
          "promotion gate (docs/backtesting_errors_we_never_repeat.md).")
    failed = [s for s in sleeves if not ok.get(s, False)]
    if failed:
        print(f"\n❌ THREE-WAY RECONCILE FLAGGED — sleeve(s) with an unexplained drift/crash: "
              f"{', '.join(failed)}")
        return 1
    print("\n✅ done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
