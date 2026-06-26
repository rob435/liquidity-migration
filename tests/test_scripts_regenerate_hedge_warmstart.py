"""audit2 regression: regenerate_hedge_warmstart.daily_returns gap-guard.

The naive builder paired adjacent PRESENT kline days with no calendar guard, so
a missing UTC day made a multi-day close-to-close move be mislabeled as a single
day's return -> corrupted the BTC/ETH beta in deploy/hedge_warmstart/*.csv. The
fix only emits a return when cur - prev == one calendar day; contiguous series
are numerically identical to the naive computation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "scripts" / "regenerate_hedge_warmstart.py"
_spec = importlib.util.spec_from_file_location("_audit2_regen_warmstart", _SRC)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

MS_DAY = mod.MS_DAY


def _naive(closes: dict[int, float]) -> dict[int, float]:
    """The pre-fix builder: pairs adjacent present days, no calendar guard."""
    days = sorted(closes)
    out: dict[int, float] = {}
    for prev, cur in zip(days, days[1:]):
        if closes[prev] > 0:
            out[cur] = closes[cur] / closes[prev] - 1.0
    return out


def test_gap_day_pair_is_dropped() -> None:
    # day0, day1 present, day2 MISSING, day3 present -> the (day1, day3) pair
    # spans 2 calendar days and must NOT emit a return.
    d0 = 0
    d1 = MS_DAY
    d3 = 3 * MS_DAY  # day2 (2*MS_DAY) absent -> a 1-day gap
    closes = {d0: 100.0, d1: 110.0, d3: 132.0}

    out = mod.daily_returns(closes)

    # The contiguous d0->d1 return survives.
    assert d1 in out
    assert out[d1] == 110.0 / 100.0 - 1.0
    # The post-gap day is dropped: no multi-day return mislabeled as one day.
    assert d3 not in out

    # The naive builder DID mislabel it (guards the regression direction).
    naive = _naive(closes)
    assert d3 in naive
    assert naive[d3] == 132.0 / 110.0 - 1.0


def test_contiguous_series_identical_to_naive() -> None:
    # Fully calendar-consecutive days: the gap-guard is a no-op, output must
    # match the naive computation exactly (numerical equivalence on happy path).
    closes = {i * MS_DAY: 100.0 + i for i in range(6)}

    out = mod.daily_returns(closes)
    naive = _naive(closes)

    assert out == naive
    assert set(out) == {i * MS_DAY for i in range(1, 6)}
    # Spot-check a concrete value to anchor the equivalence.
    assert out[MS_DAY] == 101.0 / 100.0 - 1.0


# --- audit bucket b15: overwrite drift/row gate (backfill-writers-5) ---
def test_overwrite_allowed_within_drift_tolerance() -> None:
    report = {"max_drift": 1e-5, "old_rows": 100, "new_rows": 100, "overlap": 100}
    assert mod.overwrite_blocked("bybit", report, max_drift=1e-3, force=False) is None


def test_overwrite_refused_on_large_drift() -> None:
    # backfill-writers-5: a regenerated series that diverges materially from the
    # banked one must be REFUSED (it feeds the live 2f hedge beta + auto-deploys).
    report = {"max_drift": 5e-2, "old_rows": 100, "new_rows": 100, "overlap": 100}
    reason = mod.overwrite_blocked("bybit", report, max_drift=1e-3, force=False)
    assert reason is not None and "exceeds" in reason


def test_overwrite_refused_on_fewer_rows() -> None:
    # backfill-writers-5: a short/regressed regeneration (fewer rows than the banked
    # CSV) must be refused without --force.
    report = {"max_drift": 1e-5, "old_rows": 100, "new_rows": 80, "overlap": 80}
    reason = mod.overwrite_blocked("bybit", report, max_drift=1e-3, force=False)
    assert reason is not None and "rows" in reason


def test_overwrite_refused_when_existing_csv_has_no_overlap() -> None:
    report = {"max_drift": 0.0, "old_rows": 100, "new_rows": 200, "overlap": 0}
    reason = mod.overwrite_blocked("bybit", report, max_drift=1e-3, force=False)
    assert reason is not None and "no date overlap" in reason


def test_force_overrides_the_gate() -> None:
    report = {"max_drift": 5e-2, "old_rows": 100, "new_rows": 80, "overlap": 80}
    assert mod.overwrite_blocked("bybit", report, max_drift=1e-3, force=True) is None


def test_validate_only_exits_nonzero_when_write_would_be_refused(monkeypatch) -> None:
    monkeypatch.setattr(mod, "ROOTS", {"bybit": Path("unused")})
    monkeypatch.setattr(
        mod,
        "regenerate",
        lambda venue, days: [{"date": "2026-01-01", "unit_ret": 0.0, "btc_ret": "", "eth_ret": ""}],
    )
    monkeypatch.setattr(
        mod,
        "validate",
        lambda venue, rows: {"max_drift": 5e-2, "old_rows": 100, "new_rows": 100, "overlap": 100},
    )
    monkeypatch.setattr(sys, "argv", ["regenerate_hedge_warmstart.py", "--validate-only"])

    assert mod.main() == 1


def test_no_existing_csv_is_not_blocked() -> None:
    report = {"max_drift": 0.0, "old_rows": 0, "new_rows": 200, "overlap": 0}
    assert mod.overwrite_blocked("bybit", report, max_drift=1e-3, force=False) is None


def test_component_loader_falls_back_to_deployed_refresh_root(monkeypatch, tmp_path: Path) -> None:
    primary = tmp_path / "missing_source"
    fallback_root = tmp_path / "continuous_deployed_equity_refresh_2026-06-12" / "components"
    monkeypatch.setitem(
        mod.CONTINUOUS_COMPONENT_SOURCES,
        "turn3p3",
        mod.ContinuousComponentSource(primary, "merged_signal"),
    )
    monkeypatch.setattr(mod, "FALLBACK_COMPONENT_ROOT", fallback_root)

    seen: list[Path] = []

    def fake_load(spec, venue):
        seen.append(spec.root)
        if spec.root == primary:
            raise FileNotFoundError("primary missing")
        assert spec.root == fallback_root
        assert spec.cell == "merged_signal"
        assert venue == "bybit"
        return "component", 1, {"config": "ok"}

    monkeypatch.setattr(mod, "load_continuous_component_source", fake_load)

    out = mod.load_component_for_warmstart("turn3p3", "bybit")

    assert out == ("component", 1, {"config": "ok"})
    assert seen == [primary, fallback_root]
