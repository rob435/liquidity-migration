"""Pin the legacy-archive manifest side-copy builder.

The script lives at scripts/build_legacy_archive_manifest.py and is Change 3
from the 2026-05-27 multi-phase research plan. It exists to give Phase 1 a
474-archive-only universe without re-downloading data — by mirroring every
subdirectory of the source root via filesystem links and rewriting just the
archive_trade_manifest partitions with a source-tag row filter.

These tests pin:
  * source-tag row filter keeps only matching rows
  * non-manifest subdirs are mirrored (symlink/junction/dir) from source
  * the reports/ subtree is local-real, not linked
  * BUILD_MANIFEST.json receipt records what was done
  * re-runs are idempotent (no error when links / manifest already exist)
  * dry-run makes no filesystem changes
  * the script errors clearly when the source root or manifest is missing
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import polars as pl
import pytest

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


def _make_source_root(root: Path) -> dict[str, int]:
    """Build a synthetic source data root mimicking ~/SHARED_DATA/bybit_full_pit.

    Returns the expected post-filter row counts so tests can assert."""
    # archive_trade_manifest with two date partitions and mixed source rows
    manifest_root = root / "archive_trade_manifest"
    expected = {"input_total": 0, "kept_total": 0}
    for date_label, archive_n, v5_n in (
        ("date=2024-01-01", 3, 2),
        ("date=2024-01-02", 4, 5),
    ):
        partition = manifest_root / date_label
        partition.mkdir(parents=True, exist_ok=True)
        rows = []
        for i in range(archive_n):
            rows.append({
                "symbol": f"ARCH{i}USDT",
                "date": date_label.split("=")[-1],
                "url": "https://example/archive",
                "source": "bybit_public_trading_archive",
            })
        for i in range(v5_n):
            rows.append({
                "symbol": f"V5{i}USDT",
                "date": date_label.split("=")[-1],
                "url": "https://example/v5",
                "source": "bybit_v5_listing",
            })
        pl.DataFrame(rows).write_parquet(partition / "part.parquet")
        expected["input_total"] += archive_n + v5_n
        expected["kept_total"] += archive_n

    # Non-manifest subdirs: each gets one dummy file so we can verify the
    # link points where it should after the build.
    for name in ("klines_1h", "funding", "open_interest", "premium_index_1h"):
        sub = root / name
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "sentinel.txt").write_text(name, encoding="utf-8")

    # _download_markers and a hidden .locks dir — also link-mirrored.
    (root / "_download_markers").mkdir(parents=True, exist_ok=True)
    (root / "_download_markers" / "marker.txt").write_text("ok", encoding="utf-8")
    (root / ".locks").mkdir(parents=True, exist_ok=True)

    # reports/ exists in the source but must NOT be link-mirrored — the
    # side-copy gets its OWN reports directory so Phase 1 outputs don't
    # mingle with the production root's reports.
    (root / "reports" / "production_run").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "production_run" / "sentinel.txt").write_text("prod", encoding="utf-8")

    return expected


def _is_link_or_junction(path: Path) -> bool:
    """Treat both symlinks and Windows directory junctions as 'linked'.

    On Windows, Path.is_symlink() is True for junctions in Python 3.13+,
    but older interpreters report False. Fall back to st_reparse on Windows
    by checking that the path exists AND was not created with mkdir."""
    if path.is_symlink():
        return True
    # Windows: a junction shows is_symlink()==False but is_dir()==True and
    # has reparse-point attributes. The simplest portable proxy: check the
    # path resolves to a DIFFERENT location than its literal parent/name.
    if path.is_dir():
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            return False
        return str(resolved) != str(path)
    return False


def test_build_legacy_archive_manifest_filters_source_tag(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    expected = _make_source_root(src)

    rc = MOD.main(["--source", str(src), "--target", str(dst)])
    assert rc == 0

    # Every kept row carries the kept source tag; every dropped row carried
    # the alternative tag. Read the rewritten partitions and verify.
    rewritten = pl.concat([
        pl.read_parquet(p) for p in sorted((dst / "archive_trade_manifest").rglob("*.parquet"))
    ])
    assert rewritten.height == expected["kept_total"]
    assert set(rewritten["source"].unique().to_list()) == {"bybit_public_trading_archive"}


def test_build_legacy_archive_manifest_mirrors_non_manifest_subdirs(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    _make_source_root(src)

    rc = MOD.main(["--source", str(src), "--target", str(dst)])
    assert rc == 0

    # The link-mirrored subdirs see their source content through the link.
    for name in ("klines_1h", "funding", "open_interest", "premium_index_1h", "_download_markers"):
        linked = dst / name
        # Either a real link (POSIX symlink / Windows junction) OR — on
        # filesystems where junction creation also failed — a same-name
        # directory whose content matches by mirroring is acceptable. The
        # contract is: the operator reads (dst/name/<file>) and sees the
        # source's content.
        if name == "_download_markers":
            assert (linked / "marker.txt").read_text(encoding="utf-8") == "ok"
        else:
            assert (linked / "sentinel.txt").read_text(encoding="utf-8") == name


def test_build_legacy_archive_manifest_reports_subtree_is_local(tmp_path: Path) -> None:
    """The reports/ subdir is intentionally NOT link-mirrored — Phase 1 cells
    land their per-cell reports under the side-copy's own reports/ tree so
    the universe-restricted runs are kept separate from the full-universe
    ones at the filesystem level."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    _make_source_root(src)

    rc = MOD.main(["--source", str(src), "--target", str(dst)])
    assert rc == 0

    # The target reports dir must exist and be a real directory but must
    # NOT see the source's production_run subdirectory through it.
    target_reports = dst / "reports"
    assert target_reports.is_dir()
    assert not _is_link_or_junction(target_reports)
    assert not (target_reports / "production_run").exists()


def test_build_legacy_archive_manifest_writes_receipt(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    expected = _make_source_root(src)

    rc = MOD.main(["--source", str(src), "--target", str(dst)])
    assert rc == 0

    receipt_path = dst / "BUILD_MANIFEST.json"
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["source_tag_kept"] == "bybit_public_trading_archive"
    assert receipt["source_root"] == str(src.resolve())
    assert receipt["target_root"] == str(dst.resolve())
    assert receipt["manifest_filter_totals"]["input"] == expected["input_total"]
    assert receipt["manifest_filter_totals"]["kept"] == expected["kept_total"]
    # Receipt loudly records the biased_benchmark warning so anyone glancing
    # at the side-copy in the future doesn't mistake it for a production root.
    assert "biased_benchmark" in receipt["warning"]


def test_build_legacy_archive_manifest_is_idempotent(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    _make_source_root(src)

    rc1 = MOD.main(["--source", str(src), "--target", str(dst)])
    assert rc1 == 0

    # Second invocation must not raise — existing links/manifest are kept
    # in place, manifest is re-filtered. This lets re-runs after data
    # additions pick up new partitions cleanly.
    rc2 = MOD.main(["--source", str(src), "--target", str(dst)])
    assert rc2 == 0


def test_build_legacy_archive_manifest_dry_run_makes_no_changes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    _make_source_root(src)

    rc = MOD.main(["--source", str(src), "--target", str(dst), "--dry-run"])
    assert rc == 0
    # Dry-run must not have created the target root.
    assert not dst.exists()


def test_build_legacy_archive_manifest_errors_on_missing_source(tmp_path: Path) -> None:
    rc = MOD.main(["--source", str(tmp_path / "does_not_exist"), "--target", str(tmp_path / "dst")])
    assert rc == 2


def test_build_legacy_archive_manifest_errors_on_missing_manifest(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    # source exists but no archive_trade_manifest subdir
    rc = MOD.main(["--source", str(src), "--target", str(tmp_path / "dst")])
    assert rc == 2


def test_build_legacy_archive_manifest_errors_when_partition_schema_lacks_source(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    bad_partition = src / "archive_trade_manifest" / "date=2024-01-01"
    bad_partition.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([{"symbol": "BADUSDT", "date": "2024-01-01", "url": "https://example"}]).write_parquet(
        bad_partition / "part.parquet"
    )
    with pytest.raises(RuntimeError, match="missing 'source' column"):
        MOD.main(["--source", str(src), "--target", str(dst)])
