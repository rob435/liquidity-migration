"""R1 continuous risk-intensity: member math + forward-ledger chain tests."""

from __future__ import annotations

from scripts.research_v3.r1_forward_scorer import (
    canonical_row,
    chain_hash,
    deployed_weight,
    r1_weight,
)
from scripts.research_v3.r1_intensity_lane1 import m_risk, m_trend, member_weight


class TestTrendMembers:
    def test_binary_matches_deployed_gate(self) -> None:
        assert m_trend("binary", 0.001) == 1.0
        assert m_trend("binary", 0.0) == 0.0
        assert m_trend("binary", -0.2) == 0.0

    def test_linear10_is_clipped_ramp(self) -> None:
        assert m_trend("linear10", -0.05) == 0.0
        assert m_trend("linear10", 0.05) == 0.5
        assert m_trend("linear10", 0.10) == 1.0
        assert m_trend("linear10", 0.25) == 1.0

    def test_missing_trend_fails_closed_everywhere(self) -> None:
        assert m_trend("binary", None) == 0.0
        assert m_trend("linear10", None) == 0.0


class TestRiskMembers:
    def test_discrete35_matches_ctrl_band(self) -> None:
        assert m_risk("discrete35", 0.69, False) == 1.0
        assert m_risk("discrete35", 0.70, False) == 0.35
        assert m_risk("discrete35", 0.899, False) == 0.35
        assert m_risk("discrete35", 0.90, False) == 1.0  # deployed band is middle-only

    def test_ramp_is_monotone_with_floor(self) -> None:
        assert m_risk("ramp", 0.70, False) == 1.0
        mid = m_risk("ramp", 0.80, False)
        assert 0.35 < mid < 1.0
        assert m_risk("ramp", 0.90, False) == 0.35
        assert m_risk("ramp", 0.99, False) == 0.35  # floor, no bounce back to 1.0
        values = [m_risk("ramp", s / 100, False) for s in range(50, 100)]
        assert all(a >= b for a, b in zip(values, values[1:])), "ramp must be monotone"

    def test_warmup_deactivates_overlay(self) -> None:
        assert m_risk("discrete35", 0.80, True) == 1.0
        assert m_risk("ramp", 0.95, True) == 1.0


class TestComposite:
    def test_deployed_vs_r1_shapes(self) -> None:
        # strong uptrend, calm score: both full size
        assert deployed_weight(0.2, 0.4, False) == 1.0
        assert r1_weight(0.2, 0.4, False) == 1.0
        # weak uptrend: deployed full, r1 scaled — the divergence regime
        assert deployed_weight(0.03, 0.4, False) == 1.0
        assert r1_weight(0.03, 0.4, False) == 0.3
        # downtrend: both zero
        assert deployed_weight(-0.05, 0.4, False) == 0.0
        assert r1_weight(-0.05, 0.4, False) == 0.0

    def test_member_weight_matrix_bounds(self) -> None:
        for trend in (None, -0.2, 0.0, 0.03, 0.1, 0.5):
            for score in (None, 0.1, 0.75, 0.95):
                for warm in (True, False):
                    for member in (
                        "baseline_ungated", "binary", "binary_discrete35", "binary_ramp",
                        "linear10", "linear10_discrete35", "linear10_ramp",
                    ):
                        w = member_weight(member, trend, score, warm)
                        assert 0.0 <= w <= 1.0


class TestForwardChain:
    def _row(self, date: str, prev: str) -> dict:
        row = {
            "date": date, "btc_trend_30d": 0.05, "btc_risk_score": 0.5,
            "score_warmup": False, "m_deployed": 1.0, "m_r1": 0.5,
            "divergence_day": True, "n_entries": 3, "n_pending": 0,
            "net_unweighted": 0.01, "net_deployed": 0.01, "net_r1": 0.005,
            "prev_hash": prev,
        }
        row["row_hash"] = chain_hash(prev, row)
        return row

    def test_chain_is_deterministic_and_tamper_evident(self) -> None:
        genesis = "a" * 64
        r1 = self._row("2026-07-21", genesis)
        r2 = self._row("2026-07-22", r1["row_hash"])
        assert r1["row_hash"] == chain_hash(genesis, r1)
        tampered = dict(r1, net_r1=0.006)
        assert chain_hash(genesis, tampered) != r1["row_hash"]
        assert r2["prev_hash"] == r1["row_hash"]

    def test_canonical_row_excludes_hash_fields(self) -> None:
        row = self._row("2026-07-21", "b" * 64)
        text = canonical_row(row)
        assert "prev_hash" not in text and "row_hash" not in text
