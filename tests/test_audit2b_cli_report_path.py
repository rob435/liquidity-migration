"""Regression tests for audit2b unit ``cli_report_path``.

Defect: ``discover-universe`` / ``archive-*`` printed a WRONG report path for a
non-trivial ``--name``. They concatenated the RAW ``args.name`` into the
"path=..." line, but the on-disk report file is named with the slugified
``_safe_name(name)`` (runs of non ``[a-zA-Z0-9_.-]`` collapsed to ``-`` and
trimmed). So ``--name "My Universe"`` advertised ``universe_My Universe.md``
while the file actually written was ``universe_My-Universe.md``.

Each "slug" test below FAILS on the old code (it printed the raw name) and PASSES
on the fix. The "normal input unchanged" tests pin byte-identity on the happy
path: a name that is already a clean slug must print exactly as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from liquidity_migration import cli
from liquidity_migration.archive_manifest import _safe_name as _archive_safe_name
from liquidity_migration.universe import _safe_name as _universe_safe_name


def _run(monkeypatch, capsys, tmp_path: Path, argv: list[str]) -> str:
    rc = cli.main(["--data-root", str(tmp_path), *argv])
    assert rc == 0
    return capsys.readouterr().out


def _patch_universe(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "run_discover_universe",
        lambda *a, **k: {"rows": 3, "symbol_csv": "BTCUSDT,ETHUSDT"},
    )


def _patch_archive(monkeypatch, func_name: str) -> None:
    # The archive handlers print a "rows=/path=" line built from the payload; give
    # them a minimal payload with every key each formatter reads.
    payload = {
        "rows": 7,
        "symbols": 2,
        "downloaded": 0,
        "cached": 0,
        "empty": 0,
        "failures": 0,
        "archives_deleted": 0,
        "survivorship_warning": None,
    }
    monkeypatch.setattr(cli, func_name, lambda *a, **k: payload)


# --------------------------------------------------------------------------- #
# discover-universe
# --------------------------------------------------------------------------- #
def test_discover_universe_prints_slugged_path_for_nontrivial_name(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    _patch_universe(monkeypatch)
    out = _run(monkeypatch, capsys, tmp_path, ["discover-universe", "--name", "My Universe"])
    # The slug the writer actually uses on disk:
    expected_file = f"universe_{_universe_safe_name('My Universe')}.md"
    assert expected_file == "universe_My-Universe.md"  # guards the slug rule itself
    assert expected_file in out
    # The buggy raw-name path must NOT appear (this is what the old code printed).
    assert "universe_My Universe.md" not in out


def test_discover_universe_normal_name_path_unchanged(monkeypatch, capsys, tmp_path: Path) -> None:
    # A name that is already a clean slug must print byte-identically to before.
    _patch_universe(monkeypatch)
    out = _run(monkeypatch, capsys, tmp_path, ["discover-universe", "--name", "auto"])
    assert str(tmp_path / "reports" / "universe_auto.md") in out


# --------------------------------------------------------------------------- #
# archive-manifest
# --------------------------------------------------------------------------- #
def test_archive_manifest_prints_slugged_path_for_nontrivial_name(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    _patch_archive(monkeypatch, "run_archive_manifest")
    out = _run(monkeypatch, capsys, tmp_path, ["archive-manifest", "--name", "Q3 run/A"])
    expected_file = f"archive_manifest_{_archive_safe_name('Q3 run/A')}.md"
    assert expected_file == "archive_manifest_Q3-run-A.md"
    assert expected_file in out
    assert "archive_manifest_Q3 run/A.md" not in out


def test_archive_manifest_normal_name_path_unchanged(monkeypatch, capsys, tmp_path: Path) -> None:
    _patch_archive(monkeypatch, "run_archive_manifest")
    out = _run(
        monkeypatch, capsys, tmp_path, ["archive-manifest", "--name", "bybit-public-trading"]
    )
    assert str(tmp_path / "reports" / "archive_manifest_bybit-public-trading.md") in out


# --------------------------------------------------------------------------- #
# archive-download-klines (1m / 1h / 1h-api) — all share the raw-name bug
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("command", "func_name", "stem"),
    [
        ("archive-download-klines", "run_archive_klines_download", "archive_klines"),
        ("archive-download-klines-1h", "run_archive_hourly_klines_download", "archive_klines_1h"),
        (
            "archive-download-klines-1h-api",
            "run_archive_hourly_klines_api_download",
            "archive_klines_1h_api",
        ),
    ],
)
def test_archive_klines_print_slugged_path(
    monkeypatch, capsys, tmp_path: Path, command: str, func_name: str, stem: str
) -> None:
    _patch_archive(monkeypatch, func_name)
    out = _run(monkeypatch, capsys, tmp_path, [command, "--name", "My Klines"])
    expected_file = f"{stem}_{_archive_safe_name('My Klines')}.md"
    assert expected_file == f"{stem}_My-Klines.md"
    assert expected_file in out
    assert f"{stem}_My Klines.md" not in out
