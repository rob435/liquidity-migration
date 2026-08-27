"""Freeze the measured package import order of ``liquidity_migration/``.

The README's import order was measured from the tree, not asserted by any
check, so one wrong dependency would silently rot it. This test re-measures
the order from the AST on every run and fails on the first edge that points
the wrong way.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = "liquidity_migration"

#: The documented order (liquidity_migration/README.md §Import order). An
#: import may only point at a strictly lower rank. ``ops`` and ``cli`` share a
#: rank and, measured from the tree, have no edges between them in either
#: direction — so a same-rank cross-package import is a violation too.
_RANKS = {
    "core": 0,
    "marketdata": 1,
    "data": 2,
    "rules": 3,
    "research": 4,
    "venue": 5,
    "policy": 6,
    "runtime": 7,
    "strategy": 8,
    "ops": 9,
    "cli": 9,
}


def _imported_packages(tree: ast.Module) -> set[str]:
    """Subpackages of ``liquidity_migration`` imported anywhere in a module.

    House style is absolute imports, so both real forms are covered:
    ``from liquidity_migration.x.y import z`` and
    ``import liquidity_migration.x.y``. Function-local imports count — they
    are order edges like any other.
    """
    packages: set[str] = set()
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(_PACKAGE):
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names if alias.name.startswith(_PACKAGE))
        for module in modules:
            parts = module.split(".")
            if len(parts) > 1:
                packages.add(parts[1])
    return packages


def import_order_violations(package_root: Path) -> list[str]:
    """Every package-level import edge that points upward or sideways.

    Modules directly at the package root (``__init__.py``, ``__main__.py``)
    are the entry shim above the whole order and are skipped as sources; a
    module in a package the order does not name is itself a violation, so a
    new package must be added to the documented order deliberately.
    """
    violations: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root)
        if len(relative.parts) == 1:
            continue
        source = relative.parts[0]
        if source not in _RANKS:
            violations.append(
                f"{relative}: package {source!r} has no rank in the documented import order"
            )
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for target in sorted(_imported_packages(tree)):
            if target == source:
                continue
            target_rank = _RANKS.get(target)
            if target_rank is None:
                violations.append(f"{relative}: imports unranked package {target!r}")
            elif target_rank >= _RANKS[source]:
                violations.append(
                    f"{relative}: {source} (rank {_RANKS[source]}) imports {target} "
                    f"(rank {target_rank}); edges must point strictly down the order"
                )
    return violations


def test_real_tree_has_zero_bad_edges() -> None:
    assert import_order_violations(_REPO_ROOT / _PACKAGE) == []


def test_checker_flags_synthetic_bad_edges(tmp_path: Path) -> None:
    """The checker must bite: upward and sideways edges in a synthetic tree
    are flagged, and a legal downward edge is not."""
    root = tmp_path / _PACKAGE
    (root / "core").mkdir(parents=True)
    (root / "core" / "bad.py").write_text(
        "from liquidity_migration.strategy.foo import bar\n"
        "import liquidity_migration.venue.baz\n"
        "from liquidity_migration.core.sibling import fine\n",
        encoding="utf-8",
    )
    (root / "ops").mkdir()
    (root / "ops" / "sideways.py").write_text(
        "import liquidity_migration.cli.commands\n",
        encoding="utf-8",
    )
    (root / "strategy").mkdir()
    (root / "strategy" / "good.py").write_text(
        "from liquidity_migration.core.substrate import helper\n",
        encoding="utf-8",
    )
    violations = import_order_violations(root)
    assert len(violations) == 3
    assert any("bad.py" in item and "strategy" in item for item in violations)
    assert any("bad.py" in item and "venue" in item for item in violations)
    assert any("sideways.py" in item and "cli" in item for item in violations)
    assert not any("good.py" in item for item in violations)
