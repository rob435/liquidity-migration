"""audit2 — robustness fixes for the decision-rule + block-bootstrap tooling.

Covers two independent defects:

(1) ``scripts/apply_decision_rule.py``: the investigation tier must not crash on
    a malformed ``window_days=0`` row. Such a row carries no measurable window,
    so MAR is UNMEASURABLE (nan) and the cell is non-qualifying (``descriptive``),
    NOT an exception. Valid (``window_days>0``) rows are unchanged.

(2) ``scripts/alpha_sweep.py``: the moving-block bootstrap drew block starts from
    ``[0, len(r) - block)``, so the maximum start was ``len(r) - block - 1`` and the
    FINAL observation (index ``len(r) - 1``) was never reachable in any resample
    (off-by-one). The corrected range ``[0, len(r) - block + 1)`` makes the max
    start ``len(r) - block`` so a block ``[n-block, n-1]`` includes the last point,
    mirroring ``r1_robustness._resample_block_indices`` (max_start = n - block + 1).
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(mod_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(mod_name, REPO / rel_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: a frozen `@dataclass(slots=True)` resolves cls.__module__
    # via sys.modules during class creation, so the module must be present first.
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


adr = _load("apply_decision_rule_audit2", "scripts/apply_decision_rule.py")


# --------------------------------------------------------------------------- #
# (1) apply_decision_rule: window_days<=0 must not crash the investigation tier
# --------------------------------------------------------------------------- #


def test_compute_mar_window_days_zero_is_unmeasurable_not_crash():
    """A window_days=0 row yields nan (unmeasurable), never an exception."""
    # OLD code: compute_annualized_return raises ValueError("window_days must be > 0")
    # which propagated out of compute_mar and crashed the whole verdict.
    out = adr.compute_mar(total_return=0.5, max_drawdown=-0.20, window_days=0.0)
    assert math.isnan(out)
    # negative window is equally degenerate
    assert math.isnan(adr.compute_mar(0.5, -0.20, -3.0))


def test_evaluate_cell_investigation_window_days_zero_no_crash_non_qualifying():
    """Feed a window_days=0 cell row: no exception, and a sane non-qualifying
    (descriptive) verdict — not investigation_positive, not a crash."""
    Cell = adr.CellMetrics
    # control with a real window; the candidate cell is the malformed window_days=0 one.
    control = {
        "bybit": Cell("00_baseline", "bybit", 1.0, -0.20, 100, 0.5, window_days=365.0),
        "binance": Cell("00_baseline", "binance", 1.0, -0.20, 100, 0.5, window_days=365.0),
    }
    cell = {
        "bybit": Cell("01_bad", "bybit", 1.5, -0.18, 100, 0.6, window_days=0.0),
        "binance": Cell("01_bad", "binance", 1.5, -0.18, 100, 0.6, window_days=0.0),
    }
    # Must not raise.
    v = adr.evaluate_cell_investigation(
        "01_bad", cell, control, window_days=365.0,
    )
    # nan MAR on both venues → neither positive nor falsified → descriptive.
    assert v.verdict == "descriptive"
    assert math.isnan(v.bybit_mar)
    assert math.isnan(v.binance_mar)
    # it specifically did NOT pass as a positive on tainted/unmeasurable numbers
    assert v.verdict != "investigation_positive"


def test_evaluate_cell_investigation_valid_window_unchanged():
    """A normal (window_days>0) cell still evaluates exactly as before: a clear
    MAR improvement on both venues with healthy trades is investigation_positive."""
    Cell = adr.CellMetrics
    control = {
        "bybit": Cell("00_baseline", "bybit", 1.0, -0.40, 100, 0.5, window_days=365.0),
        "binance": Cell("00_baseline", "binance", 1.0, -0.40, 100, 0.5, window_days=365.0),
    }
    # Higher return AND shallower drawdown → higher MAR on both venues.
    cell = {
        "bybit": Cell("01_good", "bybit", 1.5, -0.20, 100, 1.0, window_days=365.0),
        "binance": Cell("01_good", "binance", 1.5, -0.20, 100, 1.0, window_days=365.0),
    }
    v = adr.evaluate_cell_investigation("01_good", cell, control, window_days=365.0)
    assert v.verdict == "investigation_positive"
    assert v.bybit_mar_d > 0
    assert v.binance_mar_d > 0
    # Sanity: MAR is finite and matches the direct compute_mar (no behavior drift).
    expected_by = adr.compute_mar(1.0, -0.20, 365.0)
    assert math.isclose(v.bybit_mar, expected_by, rel_tol=1e-12)


# --------------------------------------------------------------------------- #
# (2) alpha_sweep block-bootstrap: final observation must be reachable
# --------------------------------------------------------------------------- #

# The three sweep sites all use the idiom
#   rng.integers(0, max(1, len(r) - block + 1), nb)
# i.e. the start is drawn from the half-open interval [0, n - block + 1), so the
# maximum start index is n - block and a block [n-block, n-1] reaches the last
# observation. The pre-fix idiom used (n - block), dropping the final point.


def _max_start_corrected(n: int, block: int) -> int:
    """The corrected exclusive upper bound used by the sweep bootstrap."""
    return max(1, n - block + 1)


def _max_start_old(n: int, block: int) -> int:
    """The pre-fix (buggy) exclusive upper bound — kept to prove the regression."""
    return max(1, n - block)


def test_corrected_range_reaches_final_observation():
    """With the corrected upper bound, the max block start == n - block, so the
    block [n-block, n-1] includes the final index n-1. The old range did NOT."""
    n, block = 30, 21
    rng = np.random.default_rng(0)
    starts = rng.integers(0, _max_start_corrected(n, block), 100_000)
    max_start = int(starts.max())
    # exclusive upper bound is n-block+1 → reachable max start is exactly n-block
    assert max_start == n - block  # == 9
    # the final observation index (n-1) is covered by a block starting at n-block
    last_covered = max_start + block - 1
    assert last_covered == n - 1

    # Regression guard: the OLD range could never reach n-1.
    old_starts = rng.integers(0, _max_start_old(n, block), 100_000)
    old_max_start = int(old_starts.max())
    assert old_max_start == n - block - 1  # == 8, one short
    assert old_max_start + block - 1 == n - 2  # last point (n-1) unreachable


def test_alpha_sweep_source_uses_corrected_upper_bound():
    """All block-bootstrap call sites in alpha_sweep.py use the +1 (corrected)
    upper bound; none retain the pre-fix `len(r) - block)` form."""
    src = (REPO / "scripts" / "alpha_sweep.py").read_text()
    # every integers() bootstrap draw is now the corrected form
    assert "len(r) - block + 1" in src
    n_corrected = src.count("rng.integers(0, max(1, len(r) - block + 1), nb)")
    assert n_corrected == 3, f"expected 3 corrected sites, found {n_corrected}"
    # no buggy site survives (the corrected substring contains '- block', so we
    # must check the exact buggy call form, not a loose '- block)' fragment)
    assert "rng.integers(0, max(1, len(r) - block), nb)" not in src


def test_final_observation_appears_over_many_draws():
    """End-to-end: over many seeded resamples, the FINAL observation value is
    actually included at least once (it never could be under the off-by-one)."""
    n, block = 30, 21
    r = np.arange(n, dtype=float)  # distinct values; r[-1] == 29.0 is the sentinel
    rng = np.random.default_rng(0)
    seen_last = False
    for _ in range(2000):
        starts = rng.integers(0, _max_start_corrected(n, block), int(np.ceil(n / block)))
        samp = np.concatenate([r[s:s + block] for s in starts])[:n]
        if (samp == r[-1]).any():
            seen_last = True
            break
    assert seen_last, "corrected bootstrap must be able to include the final observation"

    # And prove the OLD bound could not have produced it.
    rng_old = np.random.default_rng(0)
    starts_old = rng_old.integers(0, _max_start_old(n, block), 1)
    assert int(starts_old.max()) <= n - block - 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
