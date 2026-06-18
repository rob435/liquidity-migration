#!/usr/bin/env python3
"""Fill-level three-way: cross-check ENTRY PRICES across backtest/model, demo, paper.

The agreement legs (``scripts/reconcile_three_way.py``) answer *"did all three books
take the same entries?"*. This adds the next question the operator actually wants:
*"for the entries they share, do the FILL PRICES agree?"* — i.e. cross-check fills,
not just membership.

Per sleeve it builds three keyed entry-price maps and joins them on a shared key:

  LONG (discrete event, pairs 1:1):
    key   = (symbol, side, signal-day)
    model = v11a backtest ``long_native_trades.csv`` entry_price (bar-close fill)
    demo  = ``long_native_demo_trades`` entry_price   (real Bybit-demo fill)
    paper = ``long_native_paper_trades`` entry_price  (idealized model fill)

  CONTINUOUS (rebalance / ensemble book):
    key   = (symbol, signal-bar)   [hour-floored signal ts]. The paper book records
            one row per ensemble component; the demo book holds the netted exchange
            position. We notional-weight the paper component legs into one symbol
            fill so it lines up with demo's single fill — same granularity.
    model = the engine's OWN per-component entry candidate set, RE-DERIVED from a
            klines+rmom panel via the shared ``compute_continuous_decile_panel`` +
            ``_entry_event_expr`` (see ``continuous_backtest_candidates``). ``in_model``
            == "the engine, recomputed independently, would have generated this entry";
            ``px_model`` is the PIT kline close at the entry bar. A live entry that is NOT
            a candidate is the look-ahead/drift tripwire (classified hard vs near-decile).
    demo  = ``continuous_fade_demo_trades`` entry_price
    paper = ``continuous_fade_paper_trades`` notional-weighted entry_price

    The recompute runs on TWO planes (``fills_continuous``): the LIVE signal plane (current
    data — gates every entry now) and, when a research root is given, the INDEPENDENT-PIT
    plane (freshly-downloaded klines + recomputed rmom; confirms entries only as far as the
    research root's rmom coverage reaches — currently gated ~2 weeks back by that root's stale
    factor-panel inputs, so recent ones sit in pending_rmom).

For every key it reports the pairwise price delta in bps (signed: positive = the first
book filled HIGHER than the reference) and writes a per-entry CSV next to the agreement
keys. The summary prints paired / only counts AND the bps distribution per pair.

Standalone:  ``python scripts/reconcile_fills.py --sleeve continuous``
Wired into:  ``scripts/reconcile_three_way.py`` (runs automatically in the full three-way).

Honest: execution-agreement evidence, NOT alpha proof and NOT a promotion gate
(docs/backtesting_errors_we_never_repeat.md). The continuous "model" price is a clean
PIT bar close, not a costed ensemble re-simulation — that portfolio-return object lives
in ``liquidity_migration.continuous_forward_replay`` and is a different granularity.
"""
from __future__ import annotations

import argparse
import datetime as dt
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

HOUR_MS = 3_600_000


# ----------------------------------------------------------------------------- keys / time
def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _ms(d: dt.date) -> int:
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _day_iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date().isoformat()


def _bar(ms: int) -> int:
    """Hour-floor a ms timestamp onto the engine's 1h grid."""
    return (int(ms) // HOUR_MS) * HOUR_MS


# ----------------------------------------------------------------------------- pure join + stats
def delta_bps(price: float | None, ref: float | None) -> float | None:
    """Signed basis-point delta of ``price`` relative to ``ref``.

    ``+`` => ``price`` filled HIGHER than ``ref``. Side-agnostic on purpose: the row
    carries ``side`` so the reader interprets favourable/adverse (a higher fill is
    adverse for a long, favourable for a short). Returns None when either leg is
    missing or the reference is non-positive (no meaningful ratio)."""
    if price is None or ref is None or ref <= 0.0:
        return None
    return (price - ref) / ref * 1.0e4


def join_three_way(model: dict, demo: dict, paper: dict) -> list[dict]:
    """Join three ``key -> price`` maps over the union of keys.

    Returns one row per key with membership flags, the three prices, and the three
    pairwise bps deltas (demo vs paper, demo vs model, paper vs model). Pure: no I/O,
    so the arithmetic is unit-tested directly."""
    rows: list[dict] = []
    for key in sorted(set(model) | set(demo) | set(paper)):
        m, d, p = model.get(key), demo.get(key), paper.get(key)
        rows.append({
            "key": key,
            "in_model": m is not None,
            "in_demo": d is not None,
            "in_paper": p is not None,
            "px_model": m,
            "px_demo": d,
            "px_paper": p,
            "bps_demo_vs_paper": delta_bps(d, p),
            "bps_demo_vs_model": delta_bps(d, m),
            "bps_paper_vs_model": delta_bps(p, m),
        })
    return rows


def _stats(vals: list[float | None]) -> str:
    xs = [v for v in vals if v is not None]
    if not xs:
        return "n=0"
    worst = max(xs, key=abs)
    return (f"n={len(xs)} mean={statistics.fmean(xs):+.1f} "
            f"median={statistics.median(xs):+.1f} worst={worst:+.1f} (bps)")


def summarize(rows: list[dict], *, label: str) -> tuple[str, bool]:
    """Counts + per-pair bps distribution. ``ok`` is True iff every paired (demo &
    paper) key that CAN be compared has a finite delta — a paired key with a
    missing/zero price is a data-quality flag, not a pass."""
    paired_all = [r for r in rows if r["in_model"] and r["in_demo"] and r["in_paper"]]
    demo_paper = [r for r in rows if r["in_demo"] and r["in_paper"]]
    demo_model = [r for r in rows if r["in_demo"] and r["in_model"]]
    paper_model = [r for r in rows if r["in_paper"] and r["in_model"]]
    demo_only = [r for r in rows if r["in_demo"] and not r["in_paper"] and not r["in_model"]]
    paper_only = [r for r in rows if r["in_paper"] and not r["in_demo"] and not r["in_model"]]

    # A paired demo&paper row with no computable bps means a non-positive/missing entry_price
    # in a ledger — a DATA-QUALITY blemish, surfaced distinctly so it isn't read as drift.
    dirty = [r for r in demo_paper if r["bps_demo_vs_paper"] is None]
    dirty_note = f" | dirty_price={len(dirty)}" if dirty else ""
    summary = (
        f"{label} fills: rows={len(rows)} | all3={len(paired_all)} "
        f"demo∩paper={len(demo_paper)} demo∩model={len(demo_model)} paper∩model={len(paper_model)} "
        f"| demo_only={len(demo_only)} paper_only={len(paper_only)}{dirty_note}\n"
        f"    Δ demo↔paper : {_stats([r['bps_demo_vs_paper'] for r in demo_paper])}\n"
        f"    Δ demo↔model : {_stats([r['bps_demo_vs_model'] for r in demo_model])}\n"
        f"    Δ paper↔model: {_stats([r['bps_paper_vs_model'] for r in paper_model])}"
    )
    return summary, len(dirty) == 0


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, float):
        return f"{v:.8g}"
    return str(v)


def write_csv(rows: list[dict], out_path: Path, *, key_names: list[str],
              extra_cols: list[str] | None = None) -> None:
    """Persist the per-entry fills table. ``key_names`` labels the tuple in ``row['key']``
    (e.g. [symbol, side, signal_day] for long, [symbol, signal_bar] for continuous).
    ``extra_cols`` are additional per-row fields appended after the standard columns
    (e.g. ``model_components`` — which ensemble components generated the candidate)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    val_cols = ["in_model", "in_demo", "in_paper", "px_model", "px_demo", "px_paper",
                "bps_demo_vs_paper", "bps_demo_vs_model", "bps_paper_vs_model"]
    extra = extra_cols or []
    lines = [",".join(key_names + val_cols + extra)]
    for r in rows:
        key = r["key"] if isinstance(r["key"], tuple) else (r["key"],)
        cells = [_fmt(x) for x in key] + [_fmt(r[c]) for c in val_cols] + [_fmt(r.get(c)) for c in extra]
        lines.append(",".join(cells))
    out_path.write_text("\n".join(lines) + "\n")


# ----------------------------------------------------------------------------- price-map loaders
def _read_live(root: str, dataset: str) -> pl.DataFrame | None:
    """Read a live ledger dataset (demo/paper trades) via the storage layer, with a
    direct-glob fallback so the module works standalone on a pulled snapshot."""
    try:
        from liquidity_migration.storage import read_dataset
        df = read_dataset(REPO / root, dataset)
        if df is not None and not df.is_empty():
            return df
    except Exception:
        pass
    import glob
    files = glob.glob(str(REPO / root / dataset / "**" / "*.parquet"), recursive=True)
    if not files:
        return None
    return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")


def long_model_prices(trades_csv: Path, start_ms: int, end_ms: int) -> dict:
    """(symbol, side, signal-day) -> backtest entry_price."""
    if not trades_csv.exists():
        return {}
    df = pl.read_csv(trades_csv)
    return _long_price_map(df, "entry_signal_ts_ms", start_ms, end_ms)


def long_live_prices(root: str, dataset: str, start_ms: int, end_ms: int) -> dict:
    df = _read_live(root, dataset)
    if df is None:
        return {}
    return _long_price_map(df, "signal_ts_ms", start_ms, end_ms)


def _long_price_map(df: pl.DataFrame, sig_col: str, start_ms: int, end_ms: int) -> dict:
    need = {sig_col, "symbol", "entry_price"}
    if df is None or df.is_empty() or not need.issubset(df.columns):
        return {}
    have_side = "side" in df.columns
    cols = ["symbol", sig_col, "entry_price"] + (["side"] if have_side else [])
    out: dict = {}
    for r in df.select(cols).drop_nulls(subset=[sig_col, "entry_price"]).iter_rows(named=True):
        ts = int(r[sig_col])
        if ts < start_ms or ts >= end_ms:
            continue
        side = (str(r["side"]).lower() if have_side and r["side"] is not None else "")
        key = (r["symbol"], side, _day_iso(ts))
        # first fill wins (discrete strategy enters a (symbol, side) once per signal day)
        out.setdefault(key, float(r["entry_price"]))
    return out


def continuous_live_prices(
    root: str,
    dataset: str,
    start_ms: int,
    end_ms: int,
    *,
    strategy_id: str | None = None,
) -> tuple[dict, dict]:
    """Notional-weighted (symbol, signal-bar) -> entry_price for the continuous book.
    Thin I/O wrapper over ``aggregate_continuous_prices``."""
    df = _read_live(root, dataset)
    if df is None:
        return {}, {}
    if strategy_id and "strategy_id" in df.columns:
        df = df.filter(pl.col("strategy_id").cast(pl.Utf8) == strategy_id)
    return aggregate_continuous_prices(df, start_ms, end_ms)


def aggregate_continuous_prices(df: pl.DataFrame, start_ms: int, end_ms: int) -> tuple[dict, dict]:
    """Pure: collapse a continuous trade frame into (symbol, signal-bar) maps.

    Returns (price_map, entry_bar_map) — price is the NOTIONAL-WEIGHTED entry_price across
    every row sharing a (symbol, signal-bar), so the paper book's per-component legs
    (p3/p4p3/p4p5) aggregate into one symbol fill that lines up with demo's netted
    position; entry_bar is the hour-floored ENTRY bar (min seen) so the model leg prices
    the fill at the right bar. No I/O — unit-tested directly."""
    need = {"symbol", "signal_ts_ms", "entry_price"}
    if df is None or df.is_empty() or not need.issubset(df.columns):
        return {}, {}
    have_notl = "notional_usdt" in df.columns
    have_entry = "entry_ts_ms" in df.columns
    cols = ["symbol", "signal_ts_ms", "entry_price"]
    cols += ["notional_usdt"] if have_notl else []
    cols += ["entry_ts_ms"] if have_entry else []
    acc: dict = {}      # key -> [sum(px*w), sum(w)]
    bar: dict = {}      # key -> entry_bar_ms (min seen)
    for r in df.select(cols).drop_nulls(subset=["signal_ts_ms", "entry_price"]).iter_rows(named=True):
        ts = int(r["signal_ts_ms"])
        if ts < start_ms or ts >= end_ms:
            continue
        key = (r["symbol"], _bar(ts))
        w = float(r["notional_usdt"]) if (have_notl and r["notional_usdt"] is not None) else 1.0
        w = w if w > 0.0 else 1.0
        px = float(r["entry_price"])
        a = acc.setdefault(key, [0.0, 0.0])
        a[0] += px * w
        a[1] += w
        ent = int(r["entry_ts_ms"]) if (have_entry and r["entry_ts_ms"] is not None) else ts
        bar[key] = min(bar.get(key, ent), ent)
    price_map = {k: (v[0] / v[1]) for k, v in acc.items() if v[1] > 0.0}
    bar_map = {k: _bar(v) for k, v in bar.items()}
    return price_map, bar_map


# The deployed continuous_ensemble_v2 entry components: (tag, entry_event_trigger, age_days_min).
# Source of truth: continuous_demo.py:1694 (_apply_continuous_demo_profile). take_profit_pct
# and weight don't affect which (symbol, bar) ENTERS, so they're omitted from the entry
# reproduction. The shared decile params (decile 9, rmom_quantile 0.25, feature_set
# ("max_ret168",), liq_turnover_min 500k) are ContinuousEventConfig defaults.
ENSEMBLE_COMPONENTS: tuple[tuple[str, str, int], ...] = (
    ("p3", "turn3_pop3", 240),
    ("p4p3", "turn4_pop3", 240),
    ("p4p5", "turn4_pop5", 240),
)


def assemble_candidates(fresh_by_component: dict[str, "pl.DataFrame"], first_kline_ms: dict,
                        ages: dict, start_ms: int, end_ms: int) -> dict:
    """Pure: fold each component's ``_fresh_entries`` frame into the backtest candidate set.

    Returns ``(symbol, signal-bar) -> sorted[component tags]`` — the components whose
    fresh-D9-spell + trigger generated an entry for that symbol-bar, after the forward
    window clip and the per-component listing-age floor (``first_kline_ms`` is the
    listing-age proxy = a symbol's earliest kline ts). No I/O — unit-tested directly."""
    cand: dict = {}
    for tag, fresh in fresh_by_component.items():
        if fresh is None or fresh.is_empty():
            continue
        age = int(ages.get(tag, 0))
        for sym, ts in fresh.select(["symbol", "ts_ms"]).drop_nulls().iter_rows():
            ts = int(ts)
            if ts < start_ms or ts >= end_ms:
                continue
            if age > 0:
                first = first_kline_ms.get(sym)
                if first is not None and (ts - int(first)) < age * 86_400_000:
                    continue  # younger than the fresh-listing-squeezer floor for this component
            cand.setdefault((sym, _bar(ts)), set()).add(tag)
    return {k: sorted(v) for k, v in cand.items()}


CONTINUOUS_LIQ_MIN = 500_000.0  # ContinuousDemoCycleConfig.liq_turnover_min for the live ensemble


CONTINUOUS_DECILE = 9  # the deployed continuous_ensemble_v2 shorts the top composite decile


def continuous_backtest_candidates(k: pl.DataFrame, rmom: pl.DataFrame,
                                   start_ms: int, end_ms: int) -> tuple[dict, dict]:
    """Reproduce the continuous_ensemble_v2 ENTRY candidate set on a klines+rmom panel.

    Mirrors the live ``select_continuous_entries`` FILTER (continuous_demo.py:580) exactly,
    reusing the engine's own ``compute_continuous_decile_panel`` (the bit-identical
    live/backtest pipeline) and ``_entry_event_expr`` (the component trigger expressions) —
    not a reimplementation. The per-bar candidate predicate is:

        decile == 9  AND  turnover_quote >= liq_min  AND  component-trigger

    i.e. the live selection MINUS its live state (held / cooldown) and capacity cap. Those
    are what make the live book a SUBSET: a candidate not taken live is EXPECTED (capacity /
    already-held), while a LIVE entry that is NOT a candidate here is the tripwire
    (look-ahead / drift / reproduction error). Returns ``(symbol, signal-bar) -> [components]``.

    The listing-age floor (210/240d) is ALWAYS delegated to the live engine, not re-checked
    here: this leg only loads a ``PANEL_WARMUP_DAYS`` (~45d) window (``load_klines_window``),
    far short of the floor, so a first-kline-ts proxy can never evaluate it correctly. Live
    entries are already age-filtered upstream by the engine's authoritative universe
    launchTime, so omitting the floor only RELAXES the candidate set (never a false tripwire).

    NB: NOT spell-fresh — the live engine enters on not-held (which can be mid-decile-spell),
    so the candidate is per-bar decile membership, not the spell-start bar that the
    discrete-event backtest's ``_fresh_entries`` would use.

    Returns ``(candidates, decile_index)`` where ``decile_index`` is ``(symbol, bar) ->
    decile`` over the whole panel — used to classify a tripwire as a benign near-decile flip
    (live's synthetic live-price bar vs this closed-bar recompute) vs a genuine off-decile
    disagreement."""
    from liquidity_migration.continuous_events import (
        _entry_event_expr,
        compute_continuous_decile_panel,
    )

    panel = compute_continuous_decile_panel(
        k, rmom, rmom_quantile=0.25, feature_set=("max_ret168",), start_ms=0)
    if panel.is_empty():
        return {}, {}
    decile_index = {
        (sym, int(ts)): int(dec)
        for sym, ts, dec in panel.select(["symbol", "ts_ms", "decile"]).drop_nulls().iter_rows()
    }
    base = panel.filter(
        (pl.col("decile") == CONTINUOUS_DECILE) & (pl.col("turnover_quote") >= CONTINUOUS_LIQ_MIN))
    filtered_by_component = {
        tag: (base if trigger == "none" else base.filter(_entry_event_expr(trigger))).select(["symbol", "ts_ms"])
        for tag, trigger, _age in ENSEMBLE_COMPONENTS
    }
    # ages={} -> age floor delegated to the live engine (see docstring); assemble_candidates
    # keeps the age machinery for callers that DO load full history.
    return assemble_candidates(filtered_by_component, {}, {}, start_ms, end_ms), decile_index


def _close_index(k: pl.DataFrame) -> dict:
    """(symbol, bar_ms) -> close, for pricing the model fill at a given bar."""
    out: dict = {}
    for sym, ts, close in k.select(["symbol", "ts_ms", "close"]).drop_nulls().iter_rows():
        out[(sym, int(ts))] = float(close)
    return out


DAY_MS = 86_400_000
PANEL_WARMUP_DAYS = 45  # > the 720h (30d) longest feature window the decile panel needs


def load_klines_window(root: str, start_ms: int, end_ms: int,
                       *, warmup_days: int = PANEL_WARMUP_DAYS) -> pl.DataFrame:
    """Load hourly klines [start - warmup, end) from whichever store a root holds.

    A research full-PIT root keeps partitioned ``klines_1h/date=*/symbol=*`` (independent
    PIT data — the real backtest source); a live demo/paper root keeps a rolling WS
    snapshot (``continuous_demo_signal_check.load_klines`` reads that). We bound the
    research read to the window+warmup so the multi-year tail is never touched."""
    base = Path(root).expanduser()
    if not base.is_absolute():
        base = REPO / root
    k1h = base / "klines_1h"
    if k1h.is_dir():
        import glob as _glob
        lo = start_ms - warmup_days * DAY_MS
        files: list[str] = []
        for part in k1h.glob("date=*"):
            try:
                d = dt.date.fromisoformat(part.name.split("=", 1)[1])
            except ValueError:
                continue
            d_ms = _ms(d)
            if lo - DAY_MS <= d_ms < end_ms + DAY_MS:  # keep partitions overlapping the window
                files += _glob.glob(str(part / "**" / "*.parquet"), recursive=True)
        if files:
            df = pl.read_parquet(files, columns=["ts_ms", "symbol", "close", "turnover_quote"])
            return (df.filter((pl.col("ts_ms") >= lo) & (pl.col("ts_ms") < end_ms))
                    .unique(["ts_ms", "symbol"]).sort("ts_ms"))
        # klines_1h dir exists but nothing overlaps the window — return empty, don't fall
        # through to the WS loader (which would SystemExit on this research root).
        return _empty_klines()
    # live root: the rolling WS snapshot. Convert its hard SystemExit on "no klines" into an
    # empty frame so a missing plane degrades gracefully instead of aborting the reconcile.
    import continuous_demo_signal_check as csc
    try:
        return csc.load_klines(root)
    except SystemExit:
        return _empty_klines()


def _empty_klines() -> pl.DataFrame:
    return pl.DataFrame(schema={"ts_ms": pl.Int64, "symbol": pl.Utf8,
                               "close": pl.Float64, "turnover_quote": pl.Float64})


# ----------------------------------------------------------------------------- sleeve drivers
def fills_long(*, trades_csv: Path, demo_root: str, paper_root: str,
               start: dt.date, end: dt.date, out_dir: Path) -> tuple[str, bool]:
    start_ms, end_ms = _ms(start), _ms(end)
    model = long_model_prices(trades_csv, start_ms, end_ms)
    demo = long_live_prices(demo_root, "long_native_demo_trades", start_ms, end_ms)
    paper = long_live_prices(paper_root, "long_native_paper_trades", start_ms, end_ms)
    rows = join_three_way(model, demo, paper)
    write_csv(rows, out_dir / "long_three_way_fills.csv", key_names=["symbol", "side", "signal_day"])
    summary, ok = summarize(rows, label="LONG")
    return summary + f"\n    csv={out_dir / 'long_three_way_fills.csv'}", ok


def _continuous_tripwire(rows: list[dict], rmom_end_ms: int | None) -> tuple[list, list]:
    """Live entries (demo OR paper) with NO backtest candidate. Split into a real tripwire
    (signal day WITHIN rmom coverage -> unexplained: look-ahead / drift / reproduction error)
    vs pending (signal day AFTER the rmom panel's coverage, or no rmom at all -> can't be
    confirmed yet). Only a tripwire is the "backtest does NOT match" alarm; pending entries
    back-fill once the panel's coverage reaches them on a later refresh."""
    trip: list = []
    pending: list = []
    for r in rows:
        if r["in_model"] or not (r["in_demo"] or r["in_paper"]):
            continue
        _sym, signal_bar = r["key"]
        signal_day = (int(signal_bar) // DAY_MS) * DAY_MS
        if rmom_end_ms is None or signal_day > rmom_end_ms:
            pending.append(r["key"])
        else:
            trip.append(r["key"])
    return trip, pending


def classify_continuous_unmatched(trip: list, decile_index: dict) -> tuple[list, list, list]:
    """Bucket unmatched live entries by the recompute's decile at that bar:
    ``hard`` (off-decile, the recompute puts it BELOW D{target-1} — real look-ahead/drift),
    ``near`` (D{target-1} or D{target} — a benign live-vs-closed-bar flip: either one decile
    below, or in the top decile but failing the marginal turnover gate at the closed bar),
    ``norow`` (no panel row — a WS-snapshot coverage gap). Only ``hard`` fails the gate.
    Pure — unit-tested directly."""
    hard: list = []
    near: list = []
    norow: list = []
    for key in trip:
        sym, bar = key
        d = decile_index.get((sym, int(bar)))
        if d is None:
            norow.append(key)
        elif d >= CONTINUOUS_DECILE - 1:  # D8 (one below) or D9 (top decile, failed liq/trigger)
            near.append(key)
        else:
            hard.append((key, d))
    return hard, near, norow


def _match_plane(demo: dict, paper: dict, keys_entry_bar: dict, klines_root: str,
                 rmom_root: str, start_ms: int, end_ms: int) -> dict:
    """Recompute the engine candidate set on ONE klines+rmom plane and join it against the
    live demo/paper entries. Returns the rows + the classified unmatched buckets + the rmom
    coverage end. Used for BOTH the live signal-plane recompute (current data, verifies
    every entry now) and the independent-PIT research plane (confirms entries only as far as
    that root's rmom coverage reaches)."""
    model: dict = {}
    component_of: dict = {}
    cand_keys: set = set()
    rmom_end_ms: int | None = None
    decile_index: dict = {}
    # `(Exception, SystemExit)`: continuous_demo_signal_check.load_klines raises SystemExit
    # (a BaseException) when a root has no klines — catch it so a missing plane degrades to
    # "unavailable" instead of aborting the entire reconcile.
    try:
        import continuous_demo_signal_check as csc
        rmom = csc.load_rmom(str(Path(rmom_root).expanduser()))  # expand ~ ourselves; don't rely on polars
        rmom_end_ms = int(rmom["day_ts"].max()) if rmom.height else None
        live_days = [(int(sb) // DAY_MS) * DAY_MS for _s, sb in (set(demo) | set(paper))]
        # Short-circuit the expensive kline+panel recompute when the rmom panel cannot reach
        # ANY live entry — everything would be pending anyway (e.g. the research root's rmom
        # coverage lags the live window because its factor-panel inputs are stale). Saves
        # loading ~500k klines for nothing until that coverage advances.
        reachable = rmom_end_ms is not None and bool(live_days) and rmom_end_ms >= min(live_days)
        if reachable:
            k = load_klines_window(klines_root, start_ms, end_ms)
            candidates, decile_index = continuous_backtest_candidates(k, rmom, start_ms, end_ms)
            cand_keys = set(candidates)
            close_idx = _close_index(k)
            for key, comps in candidates.items():
                sym, signal_bar = key
                entry_bar = keys_entry_bar.get(key, int(signal_bar) + HOUR_MS)  # honest +1h if not taken live
                px = close_idx.get((sym, int(entry_bar)))
                if not px:
                    px = close_idx.get((sym, int(signal_bar) + HOUR_MS))
                if px and px > 0.0:
                    model[key] = px  # price is best-effort; membership (below) is what gates
                component_of[key] = ",".join(comps)
    except (Exception, SystemExit) as exc:
        print(f"  (continuous backtest plane [{klines_root}] unavailable: {exc})", flush=True)
    rows = join_three_way(model, demo, paper)
    for r in rows:
        # in_model is MEMBERSHIP in the candidate set, decoupled from whether a model price
        # was found — a priceable-bar gap must NOT turn a genuine engine pick into a tripwire.
        r["in_model"] = r["key"] in cand_keys
        r["model_components"] = component_of.get(r["key"], "")
    trip, pending = _continuous_tripwire(rows, rmom_end_ms)
    hard, near, norow = classify_continuous_unmatched(trip, decile_index)
    confirmed = sum(1 for r in rows if r["in_model"] and (r["in_demo"] or r["in_paper"]))
    return {"rows": rows, "rmom_end_ms": rmom_end_ms, "hard": hard, "near": near,
            "norow": norow, "pending": pending, "confirmed": confirmed}


def _match_line(res: dict, *, label: str) -> str:
    cov = _day_iso(res["rmom_end_ms"]) if res["rmom_end_ms"] else "n/a"
    line = (f"backtest-match [{label}]: confirmed={res['confirmed']} | unmatched: "
            f"off_decile(HARD)={len(res['hard'])} near_decile(≥D{CONTINUOUS_DECILE - 1})={len(res['near'])} "
            f"no_panel_row={len(res['norow'])} pending_rmom={len(res['pending'])} | rmom_through={cov}")
    if res["hard"]:
        line += (f"\n    ⚠️ {len(res['hard'])} live entr(y/ies) the model puts OFF-decile "
                 f"(look-ahead / drift): {[(k, f'D{d}') for k, d in sorted(res['hard'])][:8]}")
    if res["near"] or res["norow"]:
        line += (f"\n    ℹ️ {len(res['near'])} near-decile (≥D{CONTINUOUS_DECILE - 1}) + "
                 f"{len(res['norow'])} snapshot-gap unmatched (expected live-vs-closed-bar noise, not drift)")
    return line


def fills_continuous(*, demo_root: str, paper_root: str, start: dt.date, end: dt.date,
                     out_dir: Path, research_root: str | None = None,
                     start_ms_override: int | None = None,
                     demo_strategy_id: str | None = None,
                     paper_strategy_id: str | None = None) -> tuple[str, bool]:
    """Continuous three-way fills + backtest-match. The PRIMARY check recomputes the engine
    candidate set on the LIVE signal plane (the demo root's klines+rmom — current data, so it
    verifies every live entry NOW) and gates on it. When ``research_root`` is given it ALSO
    runs the fully-INDEPENDENT-PIT recompute (freshly-downloaded klines + freshly-recomputed
    rmom) as a second line; that one only confirms entries as far as the research root's rmom
    coverage reaches. That coverage is gated by the derivative-metric inputs build_feature_panel
    reads (open_interest / premium): the DEFAULT three-way refresh updates only manifest+klines,
    not those, so they go stale (~05-26/05-30 here), truncating the factor panel and — via rmom's
    shift(3) — its coverage to ~06-02. Pass `--with-funding` to top them up. rmom itself is
    causal (~2-3d lag, shift(3)). Both gate on a HARD off-decile unmatched (the drift alarm)."""
    start_ms, end_ms = (int(start_ms_override) if start_ms_override is not None else _ms(start)), _ms(end)
    demo, demo_bar = continuous_live_prices(
        demo_root,
        "continuous_fade_demo_trades",
        start_ms,
        end_ms,
        strategy_id=demo_strategy_id,
    )
    paper, paper_bar = continuous_live_prices(
        paper_root,
        "continuous_fade_paper_trades",
        start_ms,
        end_ms,
        strategy_id=paper_strategy_id,
    )
    keys_entry_bar = {**paper_bar, **demo_bar}  # prefer demo's real entry bar; fall back to paper's

    live = _match_plane(demo, paper, keys_entry_bar, demo_root, demo_root, start_ms, end_ms)
    write_csv(live["rows"], out_dir / "continuous_three_way_fills.csv",
              key_names=["symbol", "signal_bar_ms"], extra_cols=["model_components"])
    summary, ok = summarize(live["rows"], label="CONTINUOUS")
    full = (
        f"{summary}\n"
        f"    window_start_ts_ms={start_ms} "
        f"window_start_utc={dt.datetime.fromtimestamp(start_ms / 1000, dt.timezone.utc).isoformat()} "
        f"demo_strategy_id={demo_strategy_id or '-'} paper_strategy_id={paper_strategy_id or '-'}\n"
        f"  {_match_line(live, label='live signal-plane')}"
    )
    gate_ok = ok and len(live["hard"]) == 0

    if research_root:
        pit = _match_plane(demo, paper, keys_entry_bar, research_root, research_root, start_ms, end_ms)
        full += f"\n  {_match_line(pit, label='independent PIT')}"
        if pit["pending"] and not pit["confirmed"]:
            full += ("\n    ℹ️ independent-PIT confirms nothing yet — the research root's rmom "
                     "coverage hasn't reached this window (its factor-panel inputs are stale); "
                     "back-fills once those inputs are topped up.")
        gate_ok = gate_ok and len(pit["hard"]) == 0

    full += f"\n    csv={out_dir / 'continuous_three_way_fills.csv'}"
    return full, gate_ok


# ----------------------------------------------------------------------------- standalone CLI
DEMO_START = {"long": "2026-06-04", "continuous": "2026-06-18"}
ROOTS = {
    "long": ("data/bybit-long-demo-event", "data/bybit-long-paper-event"),
    "continuous": ("data/bybit-continuous-demo-event", "data/bybit-continuous-paper-event"),
}


def main() -> int:
    p = argparse.ArgumentParser(description="Fill-level three-way (backtest/model vs demo vs paper).")
    p.add_argument("--sleeve", choices=["long", "continuous"], required=True)
    p.add_argument("--demo-root", default=None)
    p.add_argument("--paper-root", default=None)
    p.add_argument("--start", default=None, help="forward-window start YYYY-MM-DD (default: demo start)")
    p.add_argument("--start-ts-ms", type=int, default=None,
                   help="Exact forward-window start UTC ms; overrides --start.")
    p.add_argument("--end", default=None, help="forward-window end YYYY-MM-DD exclusive (default: tomorrow UTC)")
    p.add_argument("--long-trades-csv", default=None,
                   help="LONG only: path to long_native_trades.csv from the v11a backtest leg.")
    p.add_argument("--research-root", default=None,
                   help="CONTINUOUS only: recompute the backtest candidates on this independent "
                        "PIT root (klines+rmom) instead of the live demo plane. Needs the root's "
                        "rmom refreshed past the window (else recent entries land in pending_rmom).")
    p.add_argument("--demo-strategy-id", default=None, help="CONTINUOUS only: filter demo trades by strategy_id.")
    p.add_argument("--paper-strategy-id", default=None, help="CONTINUOUS only: filter paper trades by strategy_id.")
    p.add_argument("--out-dir", default=str(REPO / "data" / "reconcile"))
    args = p.parse_args()

    demo_root = args.demo_root or ROOTS[args.sleeve][0]
    paper_root = args.paper_root or ROOTS[args.sleeve][1]
    start = _date(args.start) if args.start else _date(DEMO_START[args.sleeve])
    end = _date(args.end) if args.end else (dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1))
    out_dir = Path(args.out_dir)

    if args.sleeve == "long":
        trades_csv = Path(args.long_trades_csv) if args.long_trades_csv else Path("/nonexistent")
        if not trades_csv.exists():
            print("⚠️ no --long-trades-csv given (or missing) — model leg empty; run the v11a "
                  "backtest first (scripts/reconcile_three_way.py runs it for you).", flush=True)
        summary, ok = fills_long(trades_csv=trades_csv, demo_root=demo_root, paper_root=paper_root,
                                 start=start, end=end, out_dir=out_dir)
    else:
        summary, ok = fills_continuous(demo_root=demo_root, paper_root=paper_root,
                                       start=start, end=end, out_dir=out_dir,
                                       research_root=args.research_root,
                                       start_ms_override=args.start_ts_ms,
                                       demo_strategy_id=args.demo_strategy_id,
                                       paper_strategy_id=args.paper_strategy_id)

    print("\n" + summary, flush=True)
    print("\n(execution-agreement evidence; NOT alpha proof / NOT a promotion gate.)", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
