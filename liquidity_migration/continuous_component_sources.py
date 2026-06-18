"""Current frozen component source metadata for the continuous ensemble."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from .continuous_rebalance import ContinuousRebalanceComponents, decompose_continuous_components

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))


@dataclass(frozen=True)
class ContinuousComponentSource:
    root: Path
    cell: str


CONTINUOUS_COMPONENT_SOURCES = {
    "turn3p3": ContinuousComponentSource(
        SHARED / "continuous_merged_signal_raw_2026-06-07",
        "merged_signal",
    ),
    "turn4p3": ContinuousComponentSource(
        SHARED / "independent_continuous_entry_filter_sweep_exploratory_2026-06-07",
        "age240_turn4pop3_crowd2",
    ),
    "turn4p5": ContinuousComponentSource(
        SHARED / "independent_continuous_entry_filter_sweep_exploratory_2026-06-07",
        "age240_turn4pop5_crowd2",
    ),
}


def component_source_paths(
    spec: ContinuousComponentSource,
    venue: str,
) -> tuple[Path, Path, Path]:
    cell = spec.root / venue / spec.cell
    return (
        cell / "continuous_report.json",
        cell / "continuous_trades.csv",
        cell / "continuous_mtm_equity.csv",
    )


def load_continuous_component_source(
    spec: ContinuousComponentSource,
    venue: str,
) -> tuple[ContinuousRebalanceComponents, int, dict[str, Any]]:
    report_path, trades_path, mtm_path = component_source_paths(spec, venue)
    if not report_path.exists() or not trades_path.exists() or not mtm_path.exists():
        raise FileNotFoundError(f"missing source artifacts: {report_path.parent}")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    config = payload["config"]
    trades = pl.read_csv(trades_path)
    mtm = pl.read_csv(mtm_path).select("ts_ms", "basket_return").sort("ts_ms")
    return decompose_continuous_components(trades, mtm, config), int(trades.height), config
