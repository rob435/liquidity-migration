"""Regression tests for scripts/reconcile.py (audit bucket b15).

Findings covered:

  reconciliation-3 reconcile wrapper always exited 0 (no machine-checkable gate)
  reconciliation-5 crashed leg rendered as benign '(no output)'
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


reconcile = _load("reconcile_b15", "scripts/reconcile.py")
continuous_signal_check = _load("continuous_signal_check_b15", "scripts/continuous_demo_signal_check.py")


def test_reconcile_shell_accepts_quick_flag_in_any_position(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    venv_bin = repo / ".venv" / "bin"
    scripts.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    wrapper = scripts / "reconcile.sh"
    wrapper.write_text((REPO / "scripts" / "reconcile.sh").read_text(encoding="utf-8"), encoding="utf-8")
    fake_python = venv_bin / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf '%s\\n' \"$@\" >\"$CAPTURE\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    capture = tmp_path / "args"

    subprocess.run(
        ["bash", str(wrapper), "--dry-run", "--quick", "--sleeves", "long"],
        env={**os.environ, "CAPTURE": str(capture)},
        check=True,
        timeout=10,
    )

    assert capture.read_text(encoding="utf-8").splitlines() == [
        str(scripts / "reconcile.py"),
        "--dry-run",
        "--sleeves",
        "long",
    ]


def test_summarize_leg_passes_only_on_clean_leg() -> None:
    # reconciliation-5: a clean leg returns its summary line and ok=True.
    line, ok = reconcile._summarize_leg(
        "noise\nlong paper-demo reconciliation: paired=3 ok=True\nmore",
        "long paper-demo reconciliation", 0)
    assert ok is True
    assert line == "long paper-demo reconciliation: paired=3 ok=True"


def test_summarize_leg_flags_nonzero_exit_as_failed() -> None:
    # reconciliation-3/-5: a readiness gate that exits nonzero must NOT be summarized
    # as a clean/benign line. It must be rendered as an explicit FAILED marker and
    # report ok=False so the wrapper can exit nonzero.
    line, ok = reconcile._summarize_leg("Traceback (most recent call last): ...",
                                        "continuous forward readiness", 1)
    assert ok is False
    assert "FAILED" in line and "rc=1" in line


def test_summarize_leg_flags_missing_summary_as_failed_not_no_output() -> None:
    # reconciliation-5: a leg that printed nothing matching the needle (e.g. crashed
    # before its summary) must be FAILED, not the benign '(no output)' that made a
    # crash indistinguishable from a clean run-with-no-pairs.
    line, ok = reconcile._summarize_leg("partial output, no summary", "SUMMARY:", 0)
    assert ok is False
    assert line != "(no output)"
    assert "FAILED" in line


def test_continuous_signal_check_exits_nonzero_on_hard_miss() -> None:
    assert continuous_signal_check._signal_check_exit_code(
        checked=3,
        hard_off_decile=1,
        no_panel=0,
    ) == 1
    assert continuous_signal_check._signal_check_exit_code(
        checked=3,
        hard_off_decile=0,
        no_panel=1,
    ) == 0


def test_continuous_signal_check_treats_d8_as_soft_boundary() -> None:
    assert continuous_signal_check._classify_decile(9) == "hit"
    assert continuous_signal_check._classify_decile(8) == "near"
    assert continuous_signal_check._classify_decile(7) == "hard"


def test_continuous_signal_check_allows_empty_or_clean_windows() -> None:
    assert continuous_signal_check._signal_check_exit_code(
        checked=0,
        hard_off_decile=0,
        no_panel=0,
    ) == 0
    assert continuous_signal_check._signal_check_exit_code(
        checked=3,
        hard_off_decile=0,
        no_panel=0,
    ) == 0


def test_reconcile_main_returns_nonzero_when_a_leg_fails(monkeypatch) -> None:
    # reconciliation-3: the wrapper must exit nonzero when a reconcile leg fails,
    # rather than unconditionally returning 0. Drive main() with the long sleeve
    # and a stubbed leg that reports a failure.
    monkeypatch.setattr(reconcile.sys, "argv",
                        ["reconcile.py", "--sleeves", "long", "--no-pull", "--no-rmom"])
    monkeypatch.setattr(reconcile, "reconcile_long",
                        lambda *a, **k: ("⚠️ FAILED (rc=1): gate failed", False))
    assert reconcile.main() == 1


def test_reconcile_main_returns_zero_when_leg_clean(monkeypatch) -> None:
    monkeypatch.setattr(reconcile.sys, "argv",
                        ["reconcile.py", "--sleeves", "long", "--no-pull", "--no-rmom"])
    monkeypatch.setattr(reconcile, "reconcile_long",
                        lambda *a, **k: ("long paper-demo reconciliation: ok", True))
    assert reconcile.main() == 0


def test_quick_reconcile_skips_research_rmom_unless_explicit(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        reconcile,
        "reconcile_continuous",
        lambda *a, **k: ("continuous clean", True),
    )
    monkeypatch.setattr(
        reconcile,
        "refresh_rmom",
        lambda *a, **k: calls.append("refresh"),
    )

    monkeypatch.setattr(
        reconcile.sys,
        "argv",
        ["reconcile.py", "--sleeves", "continuous", "--no-pull"],
    )
    assert reconcile.main() == 0
    assert calls == []

    monkeypatch.setattr(
        reconcile.sys,
        "argv",
        [
            "reconcile.py",
            "--sleeves",
            "continuous",
            "--no-pull",
            "--refresh-rmom",
        ],
    )
    assert reconcile.main() == 0
    assert calls == ["refresh"]


def test_reconcile_continuous_runs_paper_demo_gate() -> None:
    class FakeStep:
        def __init__(self) -> None:
            self.commands: list[list[str]] = []

        def banner(self, title: str) -> None:
            self.title = title

        def run_capture(self, cmd: list[str]) -> tuple[int, str]:
            self.commands.append(cmd)
            joined = " ".join(cmd)
            if "continuous-forward-readiness" in joined:
                return 0, "continuous forward readiness ok=True\n"
            if "reconcile-continuous-paper-demo" in joined:
                return 1, "continuous paper-demo reconciliation paper_only=1 hard_failures=paper_only=1\n"
            if "continuous_demo_signal_check.py" in joined:
                return 0, (
                    "SUMMARY: 0/0 confirmed D9 at signal bar; 0 near-decile (≥D8); "
                    "0 hard off-decile (≤D7); 0 no-panel-row.\n"
                )
            raise AssertionError(f"unexpected command: {cmd}")

    step = FakeStep()
    summary, ok = reconcile.reconcile_continuous(step, paper="paper-root", demo="demo-root")

    paper_demo_cmd = next(cmd for cmd in step.commands if "reconcile-continuous-paper-demo" in cmd)
    assert ok is False
    assert "paper-demo:" in summary
    assert "--start-ts-ms" in paper_demo_cmd
    assert "--paper-strategy-id" in paper_demo_cmd
    assert "--demo-strategy-id" in paper_demo_cmd
    assert paper_demo_cmd[paper_demo_cmd.index("--min-pairs-warning") + 1] == "0"


def test_py_prefers_windows_venv_python(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / ".venv" / "Scripts"
    unix_bin = repo / ".venv" / "bin"
    scripts.mkdir(parents=True)
    unix_bin.mkdir(parents=True)
    win_python = scripts / "python.exe"
    unix_python = unix_bin / "python"
    win_python.write_text("", encoding="utf-8")
    unix_python.write_text("", encoding="utf-8")

    monkeypatch.setattr(reconcile, "REPO", repo)

    assert reconcile._py() == str(win_python)


def test_pull_sleeve_uses_scp_fallback_when_rsync_missing(monkeypatch, tmp_path) -> None:
    class FakeStep:
        def __init__(self) -> None:
            self.dry_run = True
            self.commands: list[tuple[list[str], dict]] = []

        def banner(self, title: str) -> None:
            self.title = title

        def run(self, cmd: list[str], **kwargs) -> int:
            self.commands.append((cmd, kwargs))
            return 0

    monkeypatch.setattr(reconcile, "REPO", tmp_path)
    monkeypatch.setattr(reconcile, "_have_rsync", lambda: False)
    monkeypatch.setattr(reconcile, "_have_scp", lambda: True)
    monkeypatch.setattr(reconcile, "_scp_ssh_options", lambda: ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"])

    step = FakeStep()
    reconcile.pull_sleeve(step, "root@example", "long")

    commands = [cmd for cmd, _ in step.commands]
    assert commands
    assert all(cmd[0] == "scp" for cmd in commands)
    assert commands[0][:7] == ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-r"]
    assert commands[0][7] == "root@example:/opt/liquidity-migration/data/bybit-long-demo-event/long_native_demo_trades/*"
    assert commands[0][8] == str(tmp_path / "data/bybit-long-demo-event/long_native_demo_trades")


def test_pull_sleeve_uses_rsync_delete_to_mirror_live_ledgers(monkeypatch, tmp_path) -> None:
    class FakeStep:
        def __init__(self) -> None:
            self.dry_run = True
            self.commands: list[tuple[list[str], dict]] = []

        def banner(self, title: str) -> None:
            self.title = title

        def run(self, cmd: list[str], **kwargs) -> int:
            self.commands.append((cmd, kwargs))
            return 0

    monkeypatch.setattr(reconcile, "REPO", tmp_path)
    monkeypatch.setattr(reconcile, "_have_rsync", lambda: True)
    monkeypatch.setattr(reconcile, "_have_scp", lambda: False)

    step = FakeStep()
    reconcile.pull_sleeve(step, "root@example", "long")

    rsync_commands = [cmd for cmd, _ in step.commands if cmd[0] == "rsync"]
    assert rsync_commands
    assert all("--delete" in cmd for cmd in rsync_commands)


def test_pull_sleeve_refuses_stale_local_mirror_without_transfer_tool(monkeypatch, tmp_path) -> None:
    class FakeStep:
        dry_run = False

        def banner(self, title: str) -> None:
            self.title = title

    monkeypatch.setattr(reconcile, "REPO", tmp_path)
    monkeypatch.setattr(reconcile, "_have_rsync", lambda: False)
    monkeypatch.setattr(reconcile, "_have_scp", lambda: False)

    with pytest.raises(SystemExit, match="refusing to use possibly stale local ledgers"):
        reconcile.pull_sleeve(FakeStep(), "root@example", "long")


def test_pull_sleeve_clears_local_mirror_when_remote_dataset_empty(monkeypatch, tmp_path) -> None:
    class FakeStep:
        dry_run = False

        def banner(self, title: str) -> None:
            self.title = title

        def run_capture(self, cmd: list[str], **kwargs) -> tuple[int, str]:
            return 0, "empty\n"

        def run(self, cmd: list[str], **kwargs) -> int:
            raise AssertionError(f"copy should not run for empty remote dataset: {cmd}")

    monkeypatch.setattr(reconcile, "REPO", tmp_path)
    monkeypatch.setattr(reconcile, "_have_rsync", lambda: False)
    monkeypatch.setattr(reconcile, "_have_scp", lambda: True)
    monkeypatch.setattr(reconcile, "_scp_ssh_options", lambda: [])

    stale = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    stale.mkdir(parents=True)
    (stale / "old.parquet").write_bytes(b"stale")

    reconcile.pull_sleeve(FakeStep(), "root@example", "long")

    assert not stale.exists(), "empty remote ledger must clear stale local mirror"


def test_pull_sleeve_clears_continuous_mirrors_before_required_plane_refusal(
    monkeypatch, tmp_path
) -> None:
    class FakeStep:
        dry_run = False

        def banner(self, title: str) -> None:
            self.title = title

        def run(self, cmd: list[str], **kwargs) -> int:
            raise AssertionError(f"copy should not run for absent remote dataset: {cmd}")

    monkeypatch.setattr(reconcile, "REPO", tmp_path)
    monkeypatch.setattr(reconcile, "_have_rsync", lambda: False)
    monkeypatch.setattr(reconcile, "_have_scp", lambda: True)
    monkeypatch.setattr(reconcile, "_scp_ssh_options", lambda: [])
    monkeypatch.setattr(reconcile, "_remote_dir_state", lambda step, host, path, ssh_options: "absent")
    monkeypatch.setattr(reconcile, "_remote_file_exists", lambda step, host, path, ssh_options: False)

    stale = tmp_path / "data" / "bybit-continuous-paper-event" / "continuous_fade_paper_trades"
    stale.mkdir(parents=True)
    (stale / "old.parquet").write_bytes(b"stale")

    with pytest.raises(SystemExit, match="required remote continuous market-plane file is absent"):
        reconcile.pull_sleeve(FakeStep(), "root@example", "continuous")

    assert not stale.exists(), "absent remote continuous paper ledger must clear stale local mirror"


def test_scp_pull_refuses_and_clears_stale_market_plane_when_remote_file_absent(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeStep:
        dry_run = False

        def banner(self, title: str) -> None:
            self.title = title

        def run(self, cmd: list[str], **kwargs) -> int:
            raise AssertionError(f"copy should not run for absent remote file: {cmd}")

    monkeypatch.setattr(reconcile, "REPO", tmp_path)
    monkeypatch.setattr(reconcile, "_have_rsync", lambda: False)
    monkeypatch.setattr(reconcile, "_have_scp", lambda: True)
    monkeypatch.setattr(reconcile, "_scp_ssh_options", lambda: [])
    monkeypatch.setattr(
        reconcile,
        "_remote_dir_state",
        lambda step, host, path, ssh_options: "empty",
    )
    monkeypatch.setattr(
        reconcile,
        "_remote_file_exists",
        lambda step, host, path, ssh_options: False,
    )
    stale = (
        tmp_path
        / "data"
        / "bybit-continuous-demo-event"
        / "residual_momentum.parquet"
    )
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")

    with pytest.raises(SystemExit, match="market-plane file is absent"):
        reconcile.pull_sleeve(FakeStep(), "root@example", "continuous")

    assert not stale.exists()
