"""Unit tests for scripts/w5_continuous_stage0_candidate_tape.py.

Relocated from tests/test_audit_fix_b08.py (audit bucket b08, finding w4-w5-stages-1):
the W5 Stage 0 W4-overlap falsifier must be fail-closed -- it passed VACUOUSLY when
the W4 control artifacts were absent (all(exact for c if available) over an
all-unavailable set == all([]) == True).
"""
from __future__ import annotations

from scripts.w5_continuous_stage0_candidate_tape import _w4_overlap_gate


def _comp(*, available: bool, exact: bool | None = None) -> dict[str, object]:
    overlap: dict[str, object] = {"available": available}
    if exact is not None:
        overlap["exact"] = exact
    return {"w4_overlap": overlap}


def test_w4_overlap_gate_fails_closed_when_artifacts_absent() -> None:
    # PRE-FIX idiom: all(exact for c if available) over an all-unavailable set == all([]) == True.
    comp_vals = [_comp(available=False), _comp(available=False)]
    gate = _w4_overlap_gate(comp_vals)
    assert gate["w4_artifacts_present"] is False
    assert gate["w4_overlap_exact"] is False  # MUST be False, not a vacuous True
    assert gate["available_count"] == 0
    assert gate["component_count"] == 2


def test_w4_overlap_gate_passes_only_when_all_present_and_exact() -> None:
    gate = _w4_overlap_gate([_comp(available=True, exact=True), _comp(available=True, exact=True)])
    assert gate["w4_artifacts_present"] is True
    assert gate["w4_overlap_exact"] is True
    assert gate["available_count"] == 2


def test_w4_overlap_gate_fails_on_partial_availability() -> None:
    # one component missing its W4 control -> not all present -> fail-closed
    gate = _w4_overlap_gate([_comp(available=True, exact=True), _comp(available=False)])
    assert gate["w4_artifacts_present"] is False
    assert gate["w4_overlap_exact"] is False


def test_w4_overlap_gate_fails_on_a_real_mismatch() -> None:
    gate = _w4_overlap_gate([_comp(available=True, exact=True), _comp(available=True, exact=False)])
    assert gate["w4_artifacts_present"] is True
    assert gate["w4_overlap_exact"] is False  # a genuine mismatch still fails


def test_w4_overlap_gate_empty_component_set_is_not_a_pass() -> None:
    gate = _w4_overlap_gate([])
    assert gate["w4_artifacts_present"] is False
    assert gate["w4_overlap_exact"] is False
