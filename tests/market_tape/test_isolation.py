"""The package stands alone: nothing in it reaches into the trading repository."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import market_tape

PACKAGE = Path(market_tape.__file__).resolve().parent
ROOT = PACKAGE.parent
FORBIDDEN = "liquidity_migration"


def sources() -> list[Path]:
    return sorted(path for path in PACKAGE.rglob("*.py") if "__pycache__" not in path.parts)


def imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_the_package_has_sources_to_check() -> None:
    found = {path.relative_to(PACKAGE).as_posix() for path in sources()}

    assert {"__init__.py", "schema.py", "config.py", "storage.py", "record.py", "pack.py"} <= found
    assert "venues/bybit.py" in found


def test_no_module_imports_the_trading_repository() -> None:
    offenders = {
        path.relative_to(ROOT).as_posix(): sorted(
            name for name in imported_names(path) if name == FORBIDDEN or name.startswith(FORBIDDEN + ".")
        )
        for path in sources()
    }

    assert {path: names for path, names in offenders.items() if names} == {}


def test_no_module_names_the_trading_repository_in_a_dynamic_import() -> None:
    for path in sources():
        text = path.read_text(encoding="utf-8")
        assert FORBIDDEN not in text, f"{path.relative_to(ROOT)} names {FORBIDDEN}"


def test_importing_the_package_does_not_pull_the_trading_repository_in() -> None:
    program = """
import json, sys
import market_tape
import market_tape.record
import market_tape.pack
import market_tape.venues.bybit
print(json.dumps({
    "file": market_tape.__file__,
    "leaked": sorted(name for name in sys.modules if name == "liquidity_migration" or name.startswith("liquidity_migration.")),
}))
"""
    done = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout.splitlines()[-1])
    assert report["file"] == str(PACKAGE / "__init__.py")
    assert report["leaked"] == []
