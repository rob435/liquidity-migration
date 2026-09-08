from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(
    os.geteuid() == 0 and Path("/run/systemd/system").is_dir(),
    "requires a root Linux systemd host",
)
class BackupServiceNamespaceTests(unittest.TestCase):
    def test_sealed_wals_can_link_inside_the_service_mount_namespace(self) -> None:
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.read(os.environ.get(
            "BACKUP_UNIT", str(ROOT / "deploy/systemd/liquidity-migration-backup.service")
        ))
        with tempfile.TemporaryDirectory(prefix="lm-backup-namespace-", dir="/var/lib") as directory:
            root = Path(directory)
            source = root / "source"
            stage = root / "backup/stage"
            source.mkdir()
            for index in (1, 2):
                (source / f"engine.wal.{index:06d}").write_bytes(bytes([index]) * 8192)
            copy = stage / source.relative_to("/")
            shutil.copytree(source, copy)
            helper = root / "link_sealed_backup_wals.py"
            shutil.copy2(ROOT / "scripts/runtime/link_sealed_backup_wals.py", helper)
            states = " ".join(
                str(root.relative_to("/var/lib") / Path(path).name)
                for path in unit["Service"].get("StateDirectory", "").split()
            )
            result = subprocess.run([
                "systemd-run", "--quiet", "--wait", "--pipe", "--collect",
                f"--unit={root.name}", "--property=Type=oneshot",
                f"--property=PrivateTmp={unit['Service']['PrivateTmp']}",
                f"--property=StateDirectory={states}",
                "python3", str(helper), "--stage", str(stage), str(source),
            ], check=False, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((source / "engine.wal.000001").samefile(copy / "engine.wal.000001"))
            self.assertFalse((source / "engine.wal.000002").samefile(copy / "engine.wal.000002"))


if __name__ == "__main__":
    unittest.main()
