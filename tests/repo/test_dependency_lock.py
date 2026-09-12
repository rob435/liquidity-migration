"""Every pinned dependency is owned: declared in pyproject.toml or required by one that is."""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _name(spec: str) -> str:
    return re.split(r"[<>=!~ ;\[]", spec.strip())[0].lower().replace("_", "-")


def _lock_names(path: Path) -> list[str]:
    return [_name(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]


def test_every_lock_entry_is_declared_or_required_by_a_declared_one() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = {_name(spec) for spec in project["dependencies"]}
    declared |= {_name(spec) for specs in project.get("optional-dependencies", {}).values() for spec in specs}
    locked = _lock_names(ROOT / "requirements.lock")
    assert len(locked) == len(set(locked)), "a package is pinned twice"

    required_by: dict[str, set[str]] = {}
    for name in locked:
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            pytest.skip(f"{name} is pinned but not installed here; install requirements.lock to judge the graph")
        for requirement in dist.requires or []:
            if "extra ==" in requirement:
                continue
            required_by.setdefault(_name(requirement), set()).add(name)

    orphans = sorted(name for name in locked if name not in declared and not (required_by.get(name, set()) - {name}))
    assert orphans == [], f"pinned but owned by nothing: {orphans}"
    undeclared_roots = sorted(declared - set(locked))
    assert undeclared_roots == [], f"declared but not pinned: {undeclared_roots}"
