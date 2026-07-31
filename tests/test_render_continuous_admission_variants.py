"""Hermetic tests for the registered ``fund0_venue_scoped`` forward scorer: the +1h bisect
boundary, the concat ordering, and the counter names in the patched admission filter,
which any forward comparison depends on.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import polars as pl
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "research" / "render_continuous_admission_variants.py"

from liquidity_migration.research.backtest.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    _funding_admission_filter,
)


def _load():
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "research"))
    sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "render_continuous_admission_variants", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_continuous_admission_variants"] = module
    spec.loader.exec_module(module)
    return module


M = _load()
HOUR_MS = 3_600_000


def _entries(rows: list[tuple[str, int]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol for symbol, _ in rows],
            "ts_ms": [ts for _, ts in rows],
        }
    )


def _lookup(rates: dict[str, list[tuple[int, float]]]) -> dict[str, dict[str, list]]:
    return {
        symbol: {
            "events_ts": [ts for ts, _ in series],
            "events_rate": [rate for _, rate in series],
        }
        for symbol, series in rates.items()
    }


def _config() -> ContinuousEventConfig:
    return ContinuousEventConfig(funding_min_at_entry=0.0)


class TestVenueScoped:
    def test_bybit_only_symbols_admit_regardless_of_funding_sign(self) -> None:
        patched = M.admission_variant_filter(
            "venue_scoped", _funding_admission_filter, {"BOTHUSDT"}
        )
        entries = _entries([("BOTHUSDT", 0), ("ONLYUSDT", 0)])
        lookup = _lookup(
            {
                "BOTHUSDT": [(0, -0.0001)],
                "ONLYUSDT": [(0, -0.0001)],
            }
        )
        kept, counters = patched(entries, lookup, _config())
        # The both-venue symbol is funding-rejected; the Bybit-only one is not.
        assert kept["symbol"].to_list() == ["ONLYUSDT"]
        assert counters["rejected"] == 1
        assert counters["bybit_only_admitted_unfiltered"] == 1

    def test_both_venue_symbols_still_face_the_floor(self) -> None:
        patched = M.admission_variant_filter(
            "venue_scoped", _funding_admission_filter, {"BOTHUSDT"}
        )
        entries = _entries([("BOTHUSDT", 0)])
        kept, counters = patched(entries, _lookup({"BOTHUSDT": [(0, 0.0001)]}), _config())
        assert kept["symbol"].to_list() == ["BOTHUSDT"]
        assert counters["rejected"] == 0
        assert counters["bybit_only_admitted_unfiltered"] == 0

    def test_output_is_ordered_by_signal_then_symbol(self) -> None:
        """Concat order is not the engine's order; the frame must be re-sorted or
        the entry sequence (and therefore crowding/capacity) silently changes."""
        patched = M.admission_variant_filter(
            "venue_scoped", _funding_admission_filter, {"BOTHUSDT"}
        )
        entries = _entries(
            [("ONLYUSDT", 2 * HOUR_MS), ("BOTHUSDT", HOUR_MS), ("AONLYUSDT", HOUR_MS)]
        )
        lookup = _lookup({"BOTHUSDT": [(0, 0.0001)]})
        kept, _counters = patched(entries, lookup, _config())
        assert list(zip(kept["ts_ms"].to_list(), kept["symbol"].to_list())) == [
            (HOUR_MS, "AONLYUSDT"),
            (HOUR_MS, "BOTHUSDT"),
            (2 * HOUR_MS, "ONLYUSDT"),
        ]

    def test_no_floor_or_empty_entries_delegates_untouched(self) -> None:
        patched = M.admission_variant_filter(
            "venue_scoped", _funding_admission_filter, {"BOTHUSDT"}
        )
        no_floor = ContinuousEventConfig(funding_min_at_entry=None)
        entries = _entries([("ONLYUSDT", 0)])
        kept, counters = patched(entries, _lookup({}), no_floor)
        assert kept["symbol"].to_list() == ["ONLYUSDT"]
        assert "bybit_only_admitted_unfiltered" not in counters


class TestRejectUnknown:
    def test_symbols_without_a_settled_print_are_rejected(self) -> None:
        patched = M.admission_variant_filter("reject_unknown", _funding_admission_filter, set())
        entries = _entries([("KNOWNUSDT", 0), ("UNKNOWNUSDT", 0)])
        kept, counters = patched(entries, _lookup({"KNOWNUSDT": [(0, 0.0001)]}), _config())
        assert kept["symbol"].to_list() == ["KNOWNUSDT"]
        assert counters["unknown_rejected"] == 1
        assert counters["unknown_admitted"] == 0

    def test_the_as_of_cutoff_is_the_signal_bar_close_plus_one_hour(self) -> None:
        """A settlement stamped inside (ts_ms, ts_ms + 1h] is visible at the decision; one
        stamped after it is not. Moving this boundary changes what the forward comparison
        scored.
        """
        patched = M.admission_variant_filter("reject_unknown", _funding_admission_filter, set())
        entries = _entries([("AUSDT", 0)])

        visible = _lookup({"AUSDT": [(HOUR_MS, 0.0001)]})
        kept, counters = patched(entries, visible, _config())
        assert kept["symbol"].to_list() == ["AUSDT"]
        assert counters["unknown_rejected"] == 0

        future = _lookup({"AUSDT": [(HOUR_MS + 1, 0.0001)]})
        kept, counters = patched(entries, future, _config())
        assert kept.is_empty()
        assert counters["unknown_rejected"] == 1


def test_unknown_mode_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown admission variant mode"):
        M.admission_variant_filter("nonsense", _funding_admission_filter, set())
