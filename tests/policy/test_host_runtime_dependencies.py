"""Load the Python service entrypoints with only their declared host dependencies."""

from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_host_service_imports_use_only_runtime_dependencies() -> None:
    entrypoints: set[tuple[str, str]] = set()
    for unit in (ROOT / "deploy/systemd").glob("*.service"):
        text = unit.read_text(encoding="utf-8").replace("\\\n", " ")
        command = next(line.removeprefix("ExecStart=") for line in text.splitlines() if line.startswith("ExecStart="))
        words = shlex.split(command)
        if not words[0].endswith("/python"):
            continue
        entrypoints.add(("module", words[2]) if words[1] == "-m" else ("script", words[1]))
    assert len(entrypoints) == 6
    # These command handlers are imported lazily by the recorder CLI.
    entrypoints.update(("module", name) for name in ("market_tape.record", "market_tape.pack"))
    lock = (ROOT / "requirements-runtime.lock").read_text(encoding="utf-8")
    packages = {line.split("==")[0] for line in lock.splitlines() if line and not line.startswith("#")}
    assert packages == {"websocket-client"}
    spec = importlib.util.find_spec("websocket")
    assert spec is not None and spec.origin is not None
    dependency_root = str(Path(spec.origin).parents[1])
    smoke = """
import importlib.abc
import importlib
import json
import runpy
import sys

repository, dependency_root, entrypoints = json.loads(sys.argv[1])
sys.path[:0] = [repository, dependency_root]
allowed = sys.stdlib_module_names | {'liquidity_migration', 'market_tape', 'websocket'}

class RuntimeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] not in allowed:
            raise ImportError('undeclared host dependency: ' + fullname)

sys.meta_path.insert(0, RuntimeImports())
for kind, name in entrypoints:
    if kind == 'module':
        importlib.import_module(name)
    else:
        runpy.run_path(repository + '/' + name, run_name='host_import_smoke')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", smoke, json.dumps([str(ROOT), dependency_root, sorted(entrypoints)])],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
