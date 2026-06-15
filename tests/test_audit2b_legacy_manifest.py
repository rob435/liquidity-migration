"""audit2b regression: the legacy-archive manifest builder's existing-link branch.

Defect (verified): the inline comment in `_link_subdir` claimed the existing
link target was resolved and verified ("we resolve and verify the operator-
visible state below before declaring done"). No such verification exists
anywhere downstream — `main()` only resolves the --source/--target *root* path
strings, never an existing link's target. The branch unconditionally assumes
any pre-existing target is correct and returns "exists".

The fix corrects the comment to match the real assume-correct behavior (option
B in the audit). It is comment-only: zero runtime behavior changes, so the
happy path is byte-identical.

These tests pin:
  * the misleading "verify ... below" claim is gone from the source (fails on
    OLD code, passes on NEW)
  * the behavior the corrected comment now documents is real: a pre-existing
    (even STALE/wrong) target is left in place and returned as "exists" with no
    re-resolution or verification
  * the normal happy path (no pre-existing target) is unchanged: a real symlink
    is created and returns "symlink"
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "build_legacy_archive_manifest.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_legacy_archive_manifest", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_legacy_archive_manifest"] = module
    spec.loader.exec_module(module)
    return module


MOD = _load_module()


def test_misleading_verify_claim_removed_from_source():
    """Fails on OLD code: the false 'verify ... below' promise must be gone."""
    text = SCRIPT_PATH.read_text()
    # The OLD comment promised verification that does not exist downstream.
    assert "verify the operator-visible state below" not in text
    # The corrected comment must state the assume-correct / not-verified reality.
    assert "NOT re-resolved or verified" in text


def test_existing_stale_target_assumed_correct_not_verified(tmp_path: Path):
    """The corrected comment is accurate: a pre-existing target is assumed
    correct and left untouched — even when it points at the WRONG source, the
    builder does not resolve/verify or repair it."""
    correct_source = tmp_path / "correct_source"
    wrong_source = tmp_path / "wrong_source"
    correct_source.mkdir()
    wrong_source.mkdir()
    (correct_source / "marker_correct.txt").write_text("correct")
    (wrong_source / "marker_wrong.txt").write_text("wrong")

    target = tmp_path / "dst" / "subdir"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Pre-existing STALE link: points at the wrong source.
    target.symlink_to(wrong_source, target_is_directory=True)

    kind = MOD._link_subdir(target, correct_source)

    # Assume-correct behavior: reported as "exists", not rebuilt.
    assert kind == "exists"
    # No verification/repair happened: the stale link still points at wrong_source.
    assert target.is_symlink()
    assert target.resolve() == wrong_source.resolve()
    assert (target / "marker_wrong.txt").exists()
    assert not (target / "marker_correct.txt").exists()


def test_existing_plain_directory_assumed_correct(tmp_path: Path):
    """A pre-existing plain directory at the target is also assumed correct and
    left in place (returns 'exists'), not replaced by a link."""
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "dst" / "subdir"
    target.mkdir(parents=True, exist_ok=True)
    (target / "preexisting.txt").write_text("kept")

    kind = MOD._link_subdir(target, source)

    assert kind == "exists"
    assert target.is_dir()
    assert not target.is_symlink()
    assert (target / "preexisting.txt").exists()


def test_happy_path_creates_real_symlink_unchanged(tmp_path: Path):
    """Normal path (no pre-existing target): a real symlink is created pointing
    at the source and 'symlink' is returned. Comment-only fix => unchanged."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.txt").write_text("payload")

    target = tmp_path / "dst" / "subdir"  # does not exist yet

    kind = MOD._link_subdir(target, source)

    assert kind == "symlink"
    assert target.is_symlink()
    assert target.resolve() == source.resolve()
    assert (target / "data.txt").read_text() == "payload"
