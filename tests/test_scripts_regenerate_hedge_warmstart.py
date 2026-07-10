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


def test_regenerate_excludes_incomplete_and_unpaired_days(monkeypatch) -> None:
    monkeypatch.setattr(mod, "ROOTS", {"bybit": Path("unused")})
    monkeypatch.setattr(
        mod,
        "unit_series",
        lambda venue, **kwargs: {
            MS_DAY: 0.01,
            2 * MS_DAY: 0.02,
            3 * MS_DAY: 0.03,
        },
    )
    monkeypatch.setattr(mod, "daily_closes", lambda root, symbol: {})
    returns = {
        "BTCUSDT": {MS_DAY: 0.1, 2 * MS_DAY: 0.2, 3 * MS_DAY: 0.3},
        # ETH gap on day 2 means it cannot be a complete regression row.
        "ETHUSDT": {MS_DAY: -0.1, 3 * MS_DAY: -0.3},
    }
    monkeypatch.setattr(
        mod,
        "daily_returns",
        lambda closes: returns.pop(next(iter(returns))),
    )

    rows = mod.regenerate("bybit", 200, cutoff_day_ms=3 * MS_DAY)

    assert rows == [
        {"date": "1970-01-02", "unit_ret": 0.01, "btc_ret": 0.1, "eth_ret": -0.1}
    ]


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


def test_object_replacement_requires_deep_canonical_overlap_and_low_drift() -> None:
    good = {"max_drift": 1e-5, "old_rows": 200, "new_rows": 200, "overlap": 187}
    assert mod.object_replacement_blocked(good, max_drift=1e-3) is None

    shallow = {**good, "overlap": 59}
    assert "overlap" in mod.object_replacement_blocked(shallow, max_drift=1e-3)

    drifted = {**good, "max_drift": 5e-3}
    assert "exceeds" in mod.object_replacement_blocked(drifted, max_drift=1e-3)


def test_live_tape_metadata_uses_signal_boundary_and_hash(tmp_path: Path) -> None:
    component_root = tmp_path / "continuous" / "components"
    summary = component_root.parent / "bybit" / "continuous_equity_summary.json"
    summary.parent.mkdir(parents=True)
    summary.write_text('{"receipt":"canonical"}', encoding="utf-8")

    metadata = mod.live_tape_metadata(
        component_root,
        {"venue": "bybit", "end_date": "2026-07-10"},
    )

    assert metadata["data_through_date"] == "2026-07-09"
    assert metadata["source_summary_sha256"] == mod.hashlib.sha256(summary.read_bytes()).hexdigest()


def test_validate_only_exits_nonzero_when_write_would_be_refused(monkeypatch) -> None:
    monkeypatch.setattr(mod, "ROOTS", {"bybit": Path("unused")})
    monkeypatch.setattr(
        mod,
        "regenerate",
        lambda venue, days, **kwargs: [
            {"date": "2026-01-01", "unit_ret": 0.0, "btc_ret": "", "eth_ret": ""}
        ],
    )
    monkeypatch.setattr(
        mod,
        "validate",
        lambda venue, rows: {"max_drift": 5e-2, "old_rows": 100, "new_rows": 100, "overlap": 100},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "regenerate_hedge_warmstart.py",
            "--validate-only",
            "--allow-legacy-component-sources",
        ],
    )

    assert mod.main() == 1


def test_explicit_object_replacement_uses_canonical_reference_gate(
    monkeypatch, tmp_path: Path
) -> None:
    candidate = tmp_path / "candidate" / "components"
    reference = tmp_path / "reference" / "components"
    monkeypatch.setattr(mod, "ROOTS", {"bybit": Path("unused")})
    monkeypatch.setattr(
        mod,
        "validate_live_component_root",
        lambda root, venue: {"venue": venue, "end_date": "2026-07-10"},
    )
    monkeypatch.setattr(mod, "live_tape_metadata", lambda root, payload: {})
    rows = [{"date": "2026-07-09", "unit_ret": 0.0, "btc_ret": 0.0, "eth_ret": 0.0}]
    monkeypatch.setattr(mod, "regenerate", lambda venue, days, **kwargs: list(rows))
    monkeypatch.setattr(
        mod,
        "validate",
        lambda venue, candidate_rows: {
            "max_drift": 5e-2,
            "old_rows": 200,
            "new_rows": 200,
            "overlap": 187,
        },
    )
    monkeypatch.setattr(
        mod,
        "compare_unit_rows",
        lambda **kwargs: {
            "max_drift": 1e-5,
            "old_rows": 200,
            "new_rows": 200,
            "overlap": 187,
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "regenerate_hedge_warmstart.py",
            "--venues",
            "bybit",
            "--component-root",
            str(candidate),
            "--reference-component-root",
            str(reference),
            "--replace-live-object",
            "--validate-only",
        ],
    )

    assert mod.main() == 0


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


def test_component_loader_uses_explicit_official_component_root(monkeypatch, tmp_path: Path) -> None:
    component_root = tmp_path / "official" / "continuous" / "components"
    seen = []

    def fake_load(spec, venue):
        seen.append((spec.root, spec.cell, venue))
        return "current-component", 2, {"take_profit_pct": 0.12}

    monkeypatch.setattr(mod, "load_continuous_component_source", fake_load)

    out = mod.load_component_for_warmstart(
        "turn3p3",
        "bybit",
        component_root=component_root,
    )

    assert out == ("current-component", 2, {"take_profit_pct": 0.12})
    assert seen == [(component_root, "merged_signal", "bybit")]


def _write_component_funding_rows(
    component_root: Path,
    *,
    historical_mode: str,
    tail_mode: str = "partial",
) -> None:
    for spec in mod.CONTINUOUS_COMPONENT_SOURCES.values():
        path = component_root / "bybit" / spec.cell / "continuous_trades.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "exit_ts_ms,funding_mode\n"
            f"{MS_DAY},{historical_mode}\n"
            f"{10 * MS_DAY},{tail_mode}\n",
            encoding="utf-8",
        )


def test_live_component_root_requires_tp12_btc_risk_and_complete_tape_funding(
    tmp_path: Path,
) -> None:
    component_root = tmp_path / "continuous" / "components"
    summary = component_root.parent / "bybit" / "continuous_equity_summary.json"
    summary.parent.mkdir(parents=True)
    payload = {
        "run_label": "exploratory",
        "strategy_run_label": "continuous_demo_paper_research_stage",
        "component_take_profit_pct": 0.12,
        "btc_risk_sizing": True,
        "backtest_leverage": 1.0,
        "btc_trend_gate": "uptrend",
        "funding_modes": ["full"],
        "data_root": str(mod.ROOTS["bybit"]),
        "end_date": "2026-07-10",
    }
    _write_component_funding_rows(component_root, historical_mode="modeled")
    for spec in mod.CONTINUOUS_COMPONENT_SOURCES.values():
        report = component_root / "bybit" / spec.cell / "continuous_report.json"
        report.write_text(
            mod.json.dumps(
                {
                    "funding_mode": "modeled",
                    "config": {
                        "take_profit_pct": 0.12,
                        "btc_trend_gate": "uptrend",
                        "use_funding": True,
                        "start_date": "2023-04-01",
                        "end_date": "2026-07-10",
                    },
                }
            ),
            encoding="utf-8",
        )
    summary.write_text(mod.json.dumps(payload), encoding="utf-8")

    assert mod.validate_live_component_root(component_root, "bybit") == payload

    payload["end_date"] = "2026-07-11"
    for spec in mod.CONTINUOUS_COMPONENT_SOURCES.values():
        report = component_root / "bybit" / spec.cell / "continuous_report.json"
        component = mod.json.loads(report.read_text(encoding="utf-8"))
        component["config"]["end_date"] = "2026-07-11"
        report.write_text(mod.json.dumps(component), encoding="utf-8")
    summary.write_text(mod.json.dumps(payload), encoding="utf-8")
    try:
        mod.validate_live_component_root(
            component_root,
            "bybit",
            as_of_date=mod.dt.date(2026, 7, 10),
        )
    except RuntimeError as exc:
        assert "exceeds complete UTC boundary" in str(exc)
    else:
        raise AssertionError("future/incomplete official boundary must be refused")

    payload["end_date"] = "2026-07-10"
    for spec in mod.CONTINUOUS_COMPONENT_SOURCES.values():
        report = component_root / "bybit" / spec.cell / "continuous_report.json"
        component = mod.json.loads(report.read_text(encoding="utf-8"))
        component["config"]["end_date"] = "2026-07-10"
        report.write_text(mod.json.dumps(component), encoding="utf-8")

    # Aggregate partial is acceptable when it comes only from the excluded
    # current-day tail and every historical tape row is fully modeled.
    payload["funding_modes"] = ["partial"]
    _write_component_funding_rows(component_root, historical_mode="modeled")
    summary.write_text(mod.json.dumps(payload), encoding="utf-8")
    assert (
        mod.validate_live_component_root(
            component_root,
            "bybit",
            cutoff_day_ms=10 * MS_DAY,
        )
        == payload
    )

    # A partial row on a completed day is a hard defect.
    _write_component_funding_rows(component_root, historical_mode="partial")
    try:
        mod.validate_live_component_root(
            component_root,
            "bybit",
            cutoff_day_ms=10 * MS_DAY,
        )
    except RuntimeError as exc:
        assert "historical funding_mode" in str(exc)
    else:
        raise AssertionError("historically partial official root must be refused")

    payload["component_take_profit_pct"] = 0.10
    payload["funding_modes"] = ["missing"]
    summary.write_text(mod.json.dumps(payload), encoding="utf-8")
    try:
        mod.validate_live_component_root(component_root, "bybit")
    except RuntimeError as exc:
        assert "component_take_profit_pct" in str(exc)
        assert "funding_modes" in str(exc)
    else:
        raise AssertionError("stale/partial official root must be refused")
