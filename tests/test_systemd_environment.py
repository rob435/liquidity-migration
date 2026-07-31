from __future__ import annotations

import grp
import os
import subprocess
import sys
from pathlib import Path

import pytest

from liquidity_migration.policy.systemd_environment import (
    load_group_systemd_environment,
    load_private_systemd_environment,
    parse_systemd_environment_bytes,
    selected_environment_payload,
)


def test_parser_preserves_shell_metacharacters_as_literal_data() -> None:
    values = parse_systemd_environment_bytes(
        b'KEY="$(touch /tmp/not-run)"\nTICK="`id`"\nEMPTY=\n',
        label="test environment",
    )

    assert values == {
        "KEY": "$(touch /tmp/not-run)",
        "TICK": "`id`",
        "EMPTY": "",
    }


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"export KEY=value\n", "strict KEY=VALUE"),
        (b"KEY=one\nKEY=two\n", "repeats KEY"),
        (b"KEY=one two\n", "ambiguous value"),
        (b"KEY='unterminated\n", "malformed quoting"),
        (b"KEY=one\\ two\n", "unsupported escape syntax"),
        (b"KEY=value\0tail\n", "forbidden control"),
    ],
)
def test_parser_rejects_non_strict_environment_syntax(
    data: bytes,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_systemd_environment_bytes(data, label="test environment")


def test_private_loader_requires_absolute_owner_only_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "private.env"
    path.write_text("KEY=value\n", encoding="utf-8")
    path.chmod(0o600)

    assert load_private_systemd_environment(path) == {"KEY": "value"}
    path.chmod(0o640)
    with pytest.raises(ValueError, match="mode 0600"):
        load_private_systemd_environment(path)
    with pytest.raises(ValueError, match="must be absolute"):
        load_private_systemd_environment(Path("private.env"))


def test_group_loader_requires_named_group_and_exact_0640(tmp_path: Path) -> None:
    path = tmp_path / "paper.env"
    path.write_text("KEY=value\n", encoding="utf-8")
    path.chmod(0o640)
    group_name = grp.getgrgid(path.stat().st_gid).gr_name

    assert load_group_systemd_environment(path, group_name=group_name) == {
        "KEY": "value"
    }
    path.chmod(0o600)
    with pytest.raises(ValueError, match="mode 0640"):
        load_group_systemd_environment(path, group_name=group_name)


def test_selected_payload_is_nul_delimited_and_omits_missing_keys() -> None:
    payload = selected_environment_payload(
        {"FIRST": "a b", "EMPTY": "", "IGNORED": "x"},
        names=["FIRST", "MISSING", "EMPTY"],
    )

    assert payload == b"FIRST\0a b\0EMPTY\0\0"


def test_cli_emits_no_payload_when_private_file_validation_fails(tmp_path: Path) -> None:
    path = tmp_path / "private.env"
    path.write_text("KEY=value\n", encoding="utf-8")
    path.chmod(0o644)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "liquidity_migration.policy.systemd_environment",
            "--path",
            str(path),
            "--name",
            "KEY",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert result.stdout == b""
    assert b"mode 0600" in result.stderr
