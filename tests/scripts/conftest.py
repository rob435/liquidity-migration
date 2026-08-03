"""Hermetic git for every test in this directory.

The bash harnesses these tests extract from the deploy and reset scripts run
git. Without a fence they inherit pytest's working directory — the real
checkout — and on 2026-08-03 a run from a linked worktree followed the `.git`
redirect file into the shared gitdir and mutated the real repository (flipped
`core.bare`, moved a branch onto a test commit, left a replace ref) during a
push. Each test now runs chdir'd into its own tmp dir, git discovery cannot
climb above it, and no host or user git config leaks in. Tests that build
their own repositories with `git init` + `-C` are unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("HOME", str(tmp_path))
