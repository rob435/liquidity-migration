#!/usr/bin/env python3
"""CONTINUOUS admission-variant research renders.

Reproduction and forward scorer for `docs/research_findings.md`. Runs component
variants of the deployed single funding-gated cell through the real engine
(`run_continuous_equity_component`, existing config fields only) and renders
hedged books through the deployed overlay stack imported from
`scripts/research/continuous_deployed_equity_refresh.py`.

The registered Lane-2 formulation is `fund0_venue_scoped`: the settled-funding
admission floor applies only to symbols in the both-venue cross-venue-panel
universe; Bybit-only contracts admit regardless of funding sign. Its scorer is
this script re-run with a later --end-date, compared against `fund0_base` re-run
identically. `venue_scoped` and `reject_unknown` patch
`_funding_admission_filter` in-process, recorded in each cell's
`variant_meta.json`; the deployed profile is untouched.

    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python \
        scripts/research/render_continuous_admission_variants.py all --end-date 2026-07-17

Groups: parity | cells | admission | renders | all. Cell runs are idempotent
(a cell with an existing report is skipped), so interrupted runs resume.
Artifacts land under
$SHARED_DATA/bybit_full_pit/reports/continuous_ladder_mech_<end-date>/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from continuous_deployed_equity_refresh import (  # noqa: E402
    active_hedge_intensity,
    instrument_inputs,
    load_extended_panel,
    pad_flat_tail,
    winner_rule,
)
from liquidity_migration.continuous_component_sources import (  # noqa: E402
    ContinuousComponentSource,
    load_continuous_component_source,
)
from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    run_continuous_equity_component,
)
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousHedgeRule,
    apply_rebalance_rule,
    combine_continuous_components,
)

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA"))).expanduser()
DATA_ROOT = SHARED / "bybit_full_pit"
CROSS_VENUE_PANEL = SHARED / "cross_venue_panel_v1"
# Session-local artifacts; renders that need them skip gracefully when absent.
RD = DATA_ROOT / "reports" / "continuous_redesign_2026-07-26"
BENCH_COMPONENTS = DATA_ROOT / "reports" / "equity_curves_sl35_2026-07-26" / "continuous" / "components"
WINDOW_START = dt.date(2023, 3, 13)
ANN = 365.25

_PANEL_CACHE: dict[str, pl.DataFrame] = {}


def base_config(end_date: str, **overrides: Any) -> ContinuousEventConfig:
    """Deployed single_fund0 shape; overrides limited to existing engine fields."""
    cfg = ContinuousEventConfig(
        component_key=str(overrides.pop("component_key", "research_cell")),
        end_date=end_date,
        entry_event_trigger=str(overrides.pop("entry_event_trigger", "turn3_pop3")),
        age_days_min=240,
        take_profit_pct=0.12,
        stop_loss_pct=0.35,
        funding_min_at_entry=overrides.pop("funding_min_at_entry", 0.0),
    )
    return replace(cfg, **overrides) if overrides else cfg


class Runner:
    def __init__(self, end_date: str) -> None:
        self.end_date = end_date
        self.out = DATA_ROOT / "reports" / f"continuous_ladder_mech_{end_date}"
        boundary = dt.date.fromisoformat(end_date)
        self.window_end = boundary - dt.timedelta(days=1)
        self.cal_days = (self.window_end - WINDOW_START).days + 1

    # ---------- component cells ----------

    def run_cell(self, cell: str, **overrides: Any) -> Path:
        out_dir = self.out / "components" / "bybit" / cell
        if (out_dir / "continuous_report.json").exists():
            print(f"[cell {cell}] exists, skipping")
            return out_dir
        cfg = base_config(self.end_date, component_key=cell, **overrides)
        print(
            f"[cell {cell}] running: trigger={cfg.entry_event_trigger}"
            f" funding_min={cfg.funding_min_at_entry} delay={cfg.entry_delay_hours}"
            f" crowd={cfg.entry_crowding_max_fresh} hold={cfg.hold_hours}"
        )
        payload = run_continuous_equity_component(DATA_ROOT, config=cfg, report_dir=out_dir)
        self._print_cell(cell, payload)
        return out_dir

    def run_cell_admission_variant(self, cell: str, mode: str) -> Path:
        """Run fund0 with a patched admission filter (research inputs transform)."""
        import liquidity_migration.continuous_events as ce

        out_dir = self.out / "components" / "bybit" / cell
        if (out_dir / "continuous_report.json").exists():
            print(f"[cell {cell}] exists, skipping")
            return out_dir
        orig = ce._funding_admission_filter
        both = both_venue_symbols()
        print(f"[cell {cell}] mode={mode} both_venue_universe={len(both)} symbols")
        ce._funding_admission_filter = admission_variant_filter(mode, orig, both)
        try:
            cfg = base_config(self.end_date, component_key=cell)
            payload = run_continuous_equity_component(DATA_ROOT, config=cfg, report_dir=out_dir)
        finally:
            ce._funding_admission_filter = orig
        (out_dir / "variant_meta.json").write_text(
            json.dumps(
                {
                    "note": "Lane-1 research render; _funding_admission_filter patched in-process",
                    "mode": mode,
                    "both_venue_universe_size": len(both),
                    "window": [WINDOW_START.isoformat(), self.end_date],
                },
                indent=1,
            )
        )
        self._print_cell(cell, payload)
        return out_dir

    @staticmethod
    def _print_cell(cell: str, payload: dict[str, Any]) -> None:
        m = payload["metrics"]["full"]
        mm = payload["metrics_mtm"]  # flat dict (no full/early split)
        print(
            f"[cell {cell}] trades={payload['n_trades']} total={m['total_return']:+.2%}"
            f" mtm_sharpe={mm['sharpe_like']:.2f} maxDD={mm['max_drawdown']:.2%}"
            f" skips={payload['skips']} admission={payload.get('funding_admission')}"
        )

    # ---------- hedged renders ----------

    def stats(self, df: pl.DataFrame) -> dict[str, Any]:
        r = df["basket_return"].fill_null(0.0).to_numpy()
        mu, sd = r.mean(), r.std(ddof=1)
        eq = np.cumprod(1 + r)
        dd = float((eq / np.maximum.accumulate(eq) - 1).min())
        rz = np.concatenate([r, np.zeros(max(0, self.cal_days - len(r)))])
        sfc = float(rz.mean() / rz.std(ddof=1) * np.sqrt(ANN)) if rz.std(ddof=1) > 0 else float("nan")
        total = float(eq[-1] - 1)
        annualized = (1 + total) ** (ANN / self.cal_days) - 1
        return {
            "days_active": int(len(r)),
            "total_return_pct": round(total * 100, 2),
            "annualized_pct": round(annualized * 100, 2),
            "max_drawdown_pct": round(dd * 100, 2),
            "mar": round(annualized / abs(dd), 2) if dd < 0 else None,
            "sharpe_active": round(float(mu / sd * np.sqrt(ANN)), 2) if sd > 0 else None,
            "sharpe_fullcal": round(sfc, 2),
        }

    def hedged_render(self, label: str, cells: dict[str, tuple[Path, str, float]]) -> dict[str, Any] | None:
        """cells: key -> (components_root, cell_dirname, weight). Roots may differ."""
        out_csv = self.out / f"hedged_{label}.csv"
        if out_csv.exists():
            summary = {"label": label, **self.stats(pl.read_csv(out_csv))}
            print(f"[render {label}] cached: {summary}")
            return summary
        pieces: dict[str, Any] = {}
        weights: dict[str, float] = {}
        n_trades = 0
        for key, (root, cell, weight) in cells.items():
            report = root / "bybit" / cell / "continuous_report.json"
            if not report.exists():
                print(f"[render {label}] SKIPPED — missing source {report.parent}")
                return None
            comp, n, _cfg = load_continuous_component_source(ContinuousComponentSource(root, cell), "bybit")
            pieces[key] = comp
            weights[key] = weight
            n_trades += n
        combined = combine_continuous_components(pieces, weights)
        if "panel" not in _PANEL_CACHE:
            _PANEL_CACHE["panel"] = load_extended_panel("bybit", end_date=self.end_date, root=DATA_ROOT)
        panel = _PANEL_CACHE["panel"]
        btc_ret, btc_fund = instrument_inputs("bybit", combined.days, "BTCUSDT", panel, data_root=DATA_ROOT)
        eth_ret, eth_fund = instrument_inputs("bybit", combined.days, "ETHUSDT", panel, data_root=DATA_ROOT)
        df = apply_rebalance_rule(
            combined,
            winner_rule(),
            ContinuousHedgeRule(90, 60, 2.0, 5.0),
            btc_ret,
            btc_fund,
            eth_ret,
            eth_fund,
            hedge_intensity=active_hedge_intensity(combined.days, btc_ret),
        )
        df = pad_flat_tail(df, through_date=self.window_end)
        df.write_csv(out_csv)
        summary = {"label": label, "n_trades": n_trades, **self.stats(df)}
        print(f"[render {label}] {summary}")
        return summary


def admission_variant_filter(
    mode: str,
    original: Any,
    both_venue: set[str],
) -> Any:
    """Build the patched ``_funding_admission_filter`` for one admission variant.

    ``venue_scoped``: the settled-funding floor applies only to symbols in the
    both-venue cross-venue-panel universe; Bybit-only contracts admit regardless
    of funding sign.
    ``reject_unknown``: symbols with no settled print at the decision timestamp
    are rejected rather than admitted.

    A top-level function, not a closure, so the forward scorer is unit-testable:
    drift in the +1h bisect boundary, the concat ordering, or the counter names
    silently corrupts the forward comparison.
    """
    if mode not in {"venue_scoped", "reject_unknown"}:
        raise ValueError(f"unknown admission variant mode {mode!r}")

    def patched(
        entries: pl.DataFrame,
        funding_lookup: dict[str, dict[str, Any]] | None,
        config: ContinuousEventConfig,
    ) -> tuple[pl.DataFrame, dict[str, int]]:
        if config.funding_min_at_entry is None or entries.is_empty():
            return original(entries, funding_lookup, config)
        if mode == "venue_scoped":
            mask = pl.col("symbol").is_in(sorted(both_venue))
            kept, counters = original(entries.filter(mask), funding_lookup, config)
            bybit_only = entries.filter(~mask)
            counters = dict(counters)
            counters["bybit_only_admitted_unfiltered"] = int(bybit_only.height)
            return pl.concat([kept, bybit_only]).sort(["ts_ms", "symbol"]), counters
        import bisect

        kept, counters = original(entries, funding_lookup, config)
        floor = float(config.funding_min_at_entry)
        keep: list[bool] = []
        unknown_rejected = 0
        for sym, sig_ts in kept.select("symbol", "ts_ms").iter_rows():
            series = (funding_lookup or {}).get(str(sym))
            rate = None
            if series is not None:
                # Same backward as-of cutoff as the engine: the decision
                # timestamp is the signal-bar close, ts_ms + 1h.
                idx = bisect.bisect_right(series["events_ts"], int(sig_ts) + 3_600_000) - 1
                if idx >= 0:
                    rate = float(series["events_rate"][idx])
            if rate is None:
                unknown_rejected += 1
            keep.append(rate is not None and rate >= floor)
        counters = dict(counters)
        counters["unknown_rejected"] = unknown_rejected
        counters["unknown_admitted"] = 0
        return kept.filter(pl.Series(keep)), counters

    return patched


def both_venue_symbols() -> set[str]:
    """Union of symbols in the cross-venue panel (the both-venue population)."""
    shards = glob.glob(str(CROSS_VENUE_PANEL / "*" / "panel.parquet"))
    if not shards:
        raise FileNotFoundError(f"cross-venue panel not found under {CROSS_VENUE_PANEL}")
    syms: set[str] = set()
    for shard in shards:
        syms |= set(pl.read_parquet(shard, columns=["symbol"])["symbol"].unique().to_list())
    return syms


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", nargs="?", default="all", choices=["parity", "cells", "admission", "renders", "all"])
    parser.add_argument("--end-date", default="2026-07-17", help="exclusive signal end date (YYYY-MM-DD)")
    args = parser.parse_args()
    runner = Runner(args.end_date)
    runner.out.mkdir(parents=True, exist_ok=True)
    comp_root = runner.out / "components"
    summaries: list[dict[str, Any]] = []

    def add(summary: dict[str, Any] | None) -> None:
        if summary is not None:
            summaries.append(summary)

    if args.group in ("all", "parity"):
        add(runner.hedged_render("PARITY_deployed_3cell", {
            "p3": (BENCH_COMPONENTS, "merged_signal", 1 / 3),
            "p4a": (BENCH_COMPONENTS, "age240_turn4pop3_crowd2", 2 / 9),
            "p4b": (BENCH_COMPONENTS, "age240_turn4pop5_crowd2", 4 / 9),
        }))
        add(runner.hedged_render("PARITY_V3_fund0", {"p3": (RD / "_shim" / "V3_proposal", "cell", 1.0)}))

    if args.group in ("all", "cells"):
        runner.run_cell("fund0_base")
        runner.run_cell("fund0_delay2", entry_delay_hours=2)
        runner.run_cell("fund0_delay3", entry_delay_hours=3)
        runner.run_cell("fund0_crowd3", entry_crowding_max_fresh=3)
        runner.run_cell("fund0_crowd0", entry_crowding_max_fresh=0)
        runner.run_cell("fund0_hold12", hold_hours=12)
        runner.run_cell("fund0_hold48", hold_hours=48)

    if args.group in ("all", "admission"):
        runner.run_cell("fund0_base")
        runner.run_cell_admission_variant("fund0_venue_scoped", "venue_scoped")
        runner.run_cell_admission_variant("fund0_reject_unknown", "reject_unknown")
        add(runner.hedged_render("VENUE_SCOPED_fund0", {"c": (comp_root, "fund0_venue_scoped", 1.0)}))
        add(runner.hedged_render("REJECT_UNKNOWN_fund0", {"c": (comp_root, "fund0_reject_unknown", 1.0)}))

    if args.group in ("all", "renders"):
        add(runner.hedged_render("BASE_fund0_shipped", {"c": (comp_root, "fund0_base", 1.0)}))
        add(runner.hedged_render("TWAP_fund0_3tranche", {
            "d1": (comp_root, "fund0_base", 1 / 3),
            "d2": (comp_root, "fund0_delay2", 1 / 3),
            "d3": (comp_root, "fund0_delay3", 1 / 3),
        }))
        add(runner.hedged_render("CROWD3_fund0", {"c": (comp_root, "fund0_crowd3", 1.0)}))
        add(runner.hedged_render("CROWD0_fund0", {"c": (comp_root, "fund0_crowd0", 1.0)}))
        add(runner.hedged_render("HOLD12_fund0", {"c": (comp_root, "fund0_hold12", 1.0)}))
        add(runner.hedged_render("HOLD48_fund0", {"c": (comp_root, "fund0_hold48", 1.0)}))
        add(runner.hedged_render("LADDER_fund0_inverted", {
            "p3": (RD / "_shim" / "FUND0_ensemble_turn3p3", "cell", 4 / 9),
            "p4a": (RD / "_shim" / "FUND0_ensemble_turn4p3", "cell", 2 / 9),
            "p4b": (RD / "_shim" / "FUND0_ensemble_turn4p5", "cell", 1 / 3),
        }))
        add(runner.hedged_render("LADDER_fund0_equal", {
            "p3": (RD / "_shim" / "FUND0_ensemble_turn3p3", "cell", 1 / 3),
            "p4a": (RD / "_shim" / "FUND0_ensemble_turn4p3", "cell", 1 / 3),
            "p4b": (RD / "_shim" / "FUND0_ensemble_turn4p5", "cell", 1 / 3),
        }))
        add(runner.hedged_render("V9_turn4p3_fund0", {"c": (RD / "_shim" / "FUND0_ensemble_turn4p3", "cell", 1.0)}))
        add(runner.hedged_render("V10_turn4p5_fund0", {"c": (RD / "_shim" / "FUND0_ensemble_turn4p5", "cell", 1.0)}))

    if summaries:
        path = runner.out / "summary.json"
        existing = json.loads(path.read_text()) if path.exists() else []
        by_label = {s["label"]: s for s in existing}
        for s in summaries:
            by_label[s["label"]] = s
        path.write_text(json.dumps(list(by_label.values()), indent=1))
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
