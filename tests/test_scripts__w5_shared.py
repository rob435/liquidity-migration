"""Tests for scripts/_w5_shared.py provenance tagging.

Relocated from tests/test_audit_fix_b09.py (audit bucket b09, w4-w5-stages-6):
the W4 path-shape stage tags its (in-sample, survivorship) provenance in code so
the not-deployment-evidence fence is a tested invariant.
"""
from __future__ import annotations


def test_w4_path_shape_tags_in_sample_provenance() -> None:
    from scripts._w5_shared import FEATURE_PROVENANCE, NOT_DEPLOYMENT_EVIDENCE_NOTE

    assert FEATURE_PROVENANCE == "w4_executed_in_sample"
    assert FEATURE_PROVENANCE != "stage7_residual"
    note = NOT_DEPLOYMENT_EVIDENCE_NOTE
    assert "not deployment" in note.lower()
    assert "stage7_residual" in note
    assert "full candidate tape" in note
