from __future__ import annotations

import json
from pathlib import Path

import pytest

from liquidity_migration.policy.real_money_arming import main, preflight
from liquidity_migration.policy.real_money_profile import (
    RealMoneyDials,
    dial_environment_keys,
    parse_real_money_dials,
    render_real_money_profile,
)
from liquidity_migration.policy.systemd_environment import parse_systemd_environment_bytes

REPO = Path(__file__).resolve().parents[2]
CREDENTIAL_TEMPLATE = REPO / "deploy" / "bybit-mainnet.env.template"
PRODUCER_TEMPLATE = REPO / "deploy" / "producer-mainnet-source.env.template"


def _private_file(path: Path, body: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode() if isinstance(body, str) else body)
    path.chmod(0o600)
    return path


def _credential(tmp_path: Path, **overrides: str) -> Path:
    values = parse_systemd_environment_bytes(CREDENTIAL_TEMPLATE.read_bytes(), label="template")
    values.update(
        {
            "BYBIT_REAL_API_KEY": "test-key",
            "BYBIT_REAL_API_SECRET": "test-secret",
            "REAL_MONEY": "true",
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "test-chat",
            **overrides,
        }
    )
    return _private_file(
        tmp_path / "bybit-mainnet.env",
        "".join(f"{key}={value}\n" for key, value in values.items()),
    )


def _producer_source(tmp_path: Path, profile: Path, *, realm: str = "mainnet") -> Path:
    candidate = _private_file(tmp_path / "candidate.json", "{}\n")
    rules = _private_file(tmp_path / "rules.json", "{}\n")
    return _private_file(
        tmp_path / "producer-mainnet-source.env",
        (
            f"PRODUCER_REALM={realm}\n"
            f"CANDIDATE_UNIVERSE_FILE={candidate}\n"
            f"VENUE_RULES_FILE={rules}\n"
            f"OPERATIONAL_PROFILE_FILE={profile}\n"
        ),
    )


def test_committed_profile_is_the_default_render() -> None:
    data, _profile = render_real_money_profile()
    assert data == (REPO / "configs" / "operational.mainnet.json").read_bytes()


def test_templates_are_strict_and_ship_disarmed_without_secrets() -> None:
    credentials = parse_systemd_environment_bytes(
        CREDENTIAL_TEMPLATE.read_bytes(), label=str(CREDENTIAL_TEMPLATE)
    )
    producer = parse_systemd_environment_bytes(
        PRODUCER_TEMPLATE.read_bytes(), label=str(PRODUCER_TEMPLATE)
    )
    assert credentials["REAL_MONEY"] == "false"
    assert credentials["BYBIT_REAL_API_KEY"] == ""
    assert credentials["BYBIT_REAL_API_SECRET"] == ""
    assert producer["PRODUCER_REALM"] == "mainnet"
    assert {
        "CANDIDATE_UNIVERSE_FILE",
        "VENUE_RULES_FILE",
        "OPERATIONAL_PROFILE_FILE",
    } <= producer.keys()
    keys = "\n".join(producer)
    assert "ACCOUNT_EXECUTION" not in keys
    assert "ACCOUNT_INTENT" not in keys


def test_template_names_every_profile_dial() -> None:
    body = CREDENTIAL_TEMPLATE.read_text(encoding="utf-8")
    for key in dial_environment_keys():
        assert f"{key}=" in body


def test_profile_dials_are_parsed_and_proved() -> None:
    dials = parse_real_money_dials({"RM_CARRY_STOP_LOSS_FRACTION": "0.25"})
    assert dials.carry_stop_loss_fraction == 0.25
    _data, profile = render_real_money_profile(dials)
    assert profile.carry.declared_stop_loss_fraction == 0.25
    with pytest.raises(ValueError, match="must sit in"):
        render_real_money_profile(RealMoneyDials(carry_stop_loss_fraction=1.0))


def test_preflight_accepts_exact_profile_and_neutral_producer_inputs(tmp_path: Path) -> None:
    credential = _credential(tmp_path)
    rendered, _profile = render_real_money_profile()
    profile = _private_file(tmp_path / "profile.json", rendered)
    producer = _producer_source(tmp_path, profile)

    rows = preflight(credential_env=credential, producer_env=producer)

    assert rows
    assert all(row.ok for row in rows), [row.render() for row in rows]
    assert all("test-secret" not in row.render() for row in rows)


def test_preflight_rejects_wrong_realm_missing_input_and_profile_drift(tmp_path: Path) -> None:
    credential = _credential(tmp_path)
    profile = _private_file(tmp_path / "profile.json", "{}\n")
    producer = _producer_source(tmp_path, profile, realm="demo")
    (tmp_path / "rules.json").unlink()

    rows = preflight(credential_env=credential, producer_env=producer)

    failed = {row.name for row in rows if not row.ok}
    assert {"PRODUCER_REALM", "VENUE_RULES_FILE", "profile matches dials"} <= failed


def test_preflight_json_is_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main(
        [
            "preflight",
            "--credential-env",
            str(tmp_path / "missing-credential"),
            "--producer-env",
            str(tmp_path / "missing-producer"),
            "--json",
        ]
    )
    assert status == 1
    rows = json.loads(capsys.readouterr().out)
    assert [row["ok"] for row in rows] == [False, False]


def test_render_refuses_silent_overwrite(tmp_path: Path) -> None:
    credential = _credential(tmp_path)
    output = tmp_path / "profile.json"
    args = [
        "render-profile",
        "--from-env",
        str(credential),
        "--execute",
        "--output",
        str(output),
    ]
    assert main(args) == 0
    original = output.read_bytes()
    assert main(args) == 2
    assert output.read_bytes() == original


def test_default_telegram_replaces_empty_values_without_printing_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = _private_file(
        tmp_path / "funded.env",
        "BYBIT_REAL_API_KEY=x\nTELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n",
    )
    source = _private_file(
        tmp_path / "demo.env",
        "TELEGRAM_BOT_TOKEN=secret-token\nTELEGRAM_CHAT_ID=secret-chat\n",
    )
    assert (
        main(
            [
                "default-telegram",
                "--credential-env",
                str(destination),
                "--from-env",
                str(source),
                "--execute",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "secret-token" not in output and "secret-chat" not in output
    values = parse_systemd_environment_bytes(destination.read_bytes(), label="result")
    assert values["TELEGRAM_BOT_TOKEN"] == "secret-token"
    assert values["TELEGRAM_CHAT_ID"] == "secret-chat"
