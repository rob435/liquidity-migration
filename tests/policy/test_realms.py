from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from liquidity_migration.policy import realms as realm_policy
from liquidity_migration.policy.realms import (
    GENERATED_MANIFEST_REGIONS,
    REALM_FIELDS,
    funded_realms,
    main,
    realm_fields,
    realms,
    render_realm_files,
)

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
SYSTEMD = DEPLOY / "systemd"


def _bash(script: str, cwd: Path = ROOT) -> str:
    completed = subprocess.run(
        ["bash", "-c", f"set -euo pipefail; . {DEPLOY}/lib_sleeves.sh; {script}"],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def _fields_bash(script: str, root: Path) -> str:
    """The same helpers, reading a cloned checkout's own generated fields."""

    completed = subprocess.run(
        ["bash", "-c", f"set -euo pipefail; . {root}/deploy/lib_sleeves.sh; {script}"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout


# ------------------------------------------------------- exact equivalence


def test_every_generated_file_matches_the_checked_in_bytes() -> None:
    rendered = render_realm_files(ROOT)
    # 4 realms x (engine, worker, liveness service, liveness timer, engine env
    # template, worker env template) plus the fleet manifest and the realm fields.
    assert len(rendered) == len(realms()) * 6 + 2
    assert ROOT / REALM_FIELDS in rendered
    differing = [
        str(path.relative_to(ROOT))
        for path, body in sorted(rendered.items())
        if not path.is_file() or path.read_bytes() != body
    ]
    assert differing == []


def test_check_mode_passes_and_reports_drift(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "--root", str(ROOT)]) == 0

    root = _clone(tmp_path)
    target = root / "deploy" / "systemd" / "liquidity-migration-engine-mexc.service"
    target.write_text(target.read_text(encoding="utf-8") + "\n# hand edit\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["check", "--root", str(root)]) == 1
    assert "liquidity-migration-engine-mexc.service" in capsys.readouterr().out
    assert main(["render", "--root", str(root)]) == 0
    assert main(["check", "--root", str(root)]) == 0


def test_a_table_edit_without_a_render_is_drift(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The shell reads only the generated fields, so an unrendered table row is
    a realm the shell cannot see. Check mode names the file."""

    root = _clone(tmp_path)
    _append_realm(
        root,
        "spare|bybit|bybit_spare|spare|funded|stopped|false|true|false|false|50|180|95|213",
    )
    capsys.readouterr()
    assert main(["check", "--root", str(root)]) == 1
    assert REALM_FIELDS in capsys.readouterr().out
    assert _fields_bash("lm_realms", root).split() == [row.realm for row in realms()]
    assert main(["render", "--root", str(root)]) == 0
    assert main(["check", "--root", str(root)]) == 0
    assert "spare" in _fields_bash("lm_realms", root).split()


def test_generated_manifest_rows_are_exactly_the_realm_units() -> None:
    text = (DEPLOY / "fleet_manifest.tsv").read_text(encoding="utf-8")
    generated: set[str] = set()
    for region in GENERATED_MANIFEST_REGIONS:
        body = text.split(f"# BEGIN GENERATED {region} -- ", 1)[1]
        body = body.split(f"# END GENERATED {region}", 1)[0]
        for line in body.splitlines()[1:]:
            if line and not line.startswith("#"):
                generated.add(line.split("|", 1)[0])
    expected = set()
    for row in realms():
        expected.update(
            {row.engine_unit, row.worker_unit, row.liveness_service, row.liveness_timer}
        )
    assert generated == expected
    # The hand-written realm extras stay outside every generated region.
    assert "liquidity-migration-execution-study.timer" not in generated
    assert "liquidity-migration-chaos-drill.timer" not in generated


# ------------------------------------------------------------ bash parity


def test_bash_and_python_agree_on_every_field_of_every_realm() -> None:
    fields = tuple(realm_fields(realms()[0]))
    script = (
        "for realm in $(lm_realms); do for field in "
        + " ".join(fields)
        + '; do printf "%s|%s|%s\\n" "$realm" "$field" "$(lm_realm_field "$realm" "$field")"; done; done'
    )
    answered: dict[tuple[str, str], str] = {}
    for line in _bash(script).splitlines():
        realm_name, field, value = line.split("|", 2)
        answered[(realm_name, field)] = value

    expected = {
        (row.realm, field): value
        for row in realms()
        for field, value in realm_fields(row).items()
    }
    assert answered == expected


def test_bash_realm_lists_come_from_the_generated_fields() -> None:
    assert _bash("lm_realms").split() == [row.realm for row in realms()]
    assert _bash("lm_funded_realms").split() == [row.realm for row in funded_realms()]
    assert _bash("lm_realm_alternation").strip() == "|".join(row.realm for row in realms())
    assert _bash("lm_funded_alternation").strip() == "|".join(
        row.realm for row in funded_realms()
    )


@pytest.mark.parametrize(
    "script",
    [
        "lm_realm_field nope realm",
        "lm_owner_unit nope",
        "lm_signal_worker_unit nope",
        "lm_activation_units nope start",
        "lm_immediate_timer_jobs nope",
        "lm_realm_units nope",
    ],
)
def test_bash_helpers_refuse_a_realm_outside_the_generated_fields(script: str) -> None:
    completed = subprocess.run(
        ["bash", "-c", f". {DEPLOY}/lib_sleeves.sh; {script}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert completed.returncode != 0


@pytest.mark.parametrize(
    ("script", "status", "message"),
    [
        ("lm_realm_field nope realm", 2, "unknown realm: nope"),
        ("lm_realm_field demo not_a_field", 3, "unknown realm field: not_a_field"),
    ],
)
def test_the_lookup_names_what_it_could_not_answer(script: str, status: int, message: str) -> None:
    completed = subprocess.run(
        ["bash", "-c", f". {DEPLOY}/lib_sleeves.sh; {script}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == status
    assert message in completed.stderr


def test_the_lookup_refuses_a_fields_file_it_does_not_know(tmp_path: Path) -> None:
    stranger = tmp_path / "realm_fields.tsv"
    stranger.write_text(
        (DEPLOY / "realm_fields.tsv")
        .read_text(encoding="utf-8")
        .replace("# realm-fields-v1", "# realm-fields-v99", 1),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"export LM_REALM_FIELDS={stranger}; . {DEPLOY}/lib_realms.sh; lm_realm_field demo realm",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert completed.returncode != 0
    assert "unsupported schema; expected # realm-fields-v1" in completed.stderr


def test_a_lookup_answered_by_the_first_row_leaves_the_writer_alive(tmp_path: Path) -> None:
    # A table larger than a pipe holds, so the reader answers before the writer
    # has finished: a reader that quit on its match would close the pipe under
    # the writer, and pipefail would report the writer's SIGPIPE as 141.
    fields = tmp_path / "realm_fields.tsv"
    padding = "".join(f"zzpad|field_{i}|x\n" for i in range(40_000))
    fields.write_text(
        (DEPLOY / "realm_fields.tsv").read_text(encoding="utf-8") + padding,
        encoding="utf-8",
    )
    assert fields.stat().st_size > 512 * 1024
    funded = _bash("lm_funded_realms | paste -sd ' ' -").strip()
    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; export LM_REALM_FIELDS={fields}; . {DEPLOY}/lib_realms.sh; "
            "lm_realm_field demo realm; lm_is_realm zzpad; lm_funded_realms | paste -sd ' ' -",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["demo", funded]


def _field_lookup(fields: Path, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; export LM_REALM_FIELDS={fields}; "
            f". {DEPLOY}/lib_realms.sh; {script}",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def test_the_lookup_refuses_a_row_that_is_not_three_fields(tmp_path: Path) -> None:
    # The row shape is decided at the end of the table, so a malformed row after
    # the row that answers is still refused.
    table = (DEPLOY / "realm_fields.tsv").read_text(encoding="utf-8")
    padding = "".join(f"zzpad|field_{i}|x\n" for i in range(2_000))
    valid = tmp_path / "valid.tsv"
    valid.write_text(table + padding, encoding="utf-8")
    malformed = tmp_path / "malformed.tsv"
    malformed.write_text(table + padding + "demo|engine_env\n", encoding="utf-8")
    malformed_line = len((table + padding).splitlines()) + 1

    answered = _field_lookup(valid, "lm_realm_field demo realm")
    assert answered.returncode == 0, answered.stderr
    assert answered.stdout.split() == ["demo"]

    refused = _field_lookup(malformed, "lm_realm_field demo realm")
    assert refused.returncode != 0
    assert f"invalid realm field row at line {malformed_line}" in refused.stderr


# --------------------------------------------------------- adding a realm


def _clone(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "deploy").mkdir(parents=True)
    shutil.copy2(DEPLOY / "realms.tsv", root / "deploy" / "realms.tsv")
    shutil.copy2(DEPLOY / "fleet_manifest.tsv", root / "deploy" / "fleet_manifest.tsv")
    shutil.copy2(DEPLOY / "lib_sleeves.sh", root / "deploy" / "lib_sleeves.sh")
    shutil.copy2(DEPLOY / "lib_realms.sh", root / "deploy" / "lib_realms.sh")
    shutil.copy2(DEPLOY / "realm_fields.tsv", root / "deploy" / "realm_fields.tsv")
    shutil.copytree(SYSTEMD, root / "deploy" / "systemd")
    return root


def _append_realm(root: Path, row: str) -> None:
    table = root / "deploy" / "realms.tsv"
    table.write_text(table.read_text(encoding="utf-8") + row + "\n", encoding="utf-8")


def test_a_fifth_realm_is_one_table_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _clone(tmp_path)
    _append_realm(
        root,
        "binance|binance|binance_mainnet|binance_mainnet|funded|stopped|false"
        "|true|false|false|50|180|95|213",
    )
    monkeypatch.setitem(
        realm_policy.VENUE_FACTS,
        "binance",
        {
            "display": "Binance",
            "groups": ("binance_real",),
            "keeps": {"funded": ("binance_real",)},
            "takeover_vars": {
                "funded": ("BINANCE_REAL_API_KEY", "BINANCE_REAL_API_SECRET", "REAL_MONEY")
            },
        },
    )
    monkeypatch.setattr(
        realm_policy,
        "CREDENTIAL_GROUPS",
        realm_policy.CREDENTIAL_GROUPS
        + (("binance_real", ("BINANCE_REAL_API_KEY", "BINANCE_REAL_API_SECRET")),),
    )

    # The row alone is not a realm the shell can see: it reads the generated
    # fields, and those are still the four realms rendered before this edit.
    assert "binance" not in _fields_bash("lm_realms", root).split()

    rendered = render_realm_files(root)
    for path, body in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    added = realm_policy.realm("binance", root)
    units = root / "deploy" / "systemd"
    for unit in (added.engine_unit, added.worker_unit, added.liveness_service, added.liveness_timer):
        text = (units / unit).read_text(encoding="utf-8")
        assert "[Unit]" in text
        assert unit.endswith(".timer") or "[Service]" in text
        # The liveness service is started by its own timer and wants nothing.
        assert ("[Install]" in text) is (unit != added.liveness_service)
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key in {"EnvironmentFile", "ReadWritePaths", "User", "StateDirectory"}:
                assert "demo" not in value
                assert "mexc" not in value
                assert "hyperliquid" not in value
    # The realm's own names, nobody else's.
    engine = (units / added.engine_unit).read_text(encoding="utf-8")
    assert f"User={added.engine_user}" in engine
    assert f"StateDirectory={added.engine_state_dir_name}" in engine
    assert f"EnvironmentFile={added.credential_env}" in engine
    assert f"EnvironmentFile={added.engine_env}" in engine
    assert added.spool_dir in engine
    assert added.control_dir in engine
    liveness = (units / added.liveness_service).read_text(encoding="utf-8")
    assert f"--account-scope {added.realm}" in liveness
    worker = (units / added.worker_unit).read_text(encoding="utf-8")
    assert f"Before={added.engine_unit}" in worker

    assert (root / added.engine_env_template).is_file()
    assert (root / added.worker_env_template).is_file()

    # The manifest still validates, and the bash helpers answer for the realm.
    environment = {
        "LM_FLEET_MANIFEST": str(root / "deploy" / "fleet_manifest.tsv"),
        "LM_REALM_FIELDS": str(root / "deploy" / "realm_fields.tsv"),
    }
    exports = " ".join(f"{key}={value}" for key, value in environment.items())
    out = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; export {exports}; . {root}/deploy/lib_sleeves.sh; "
            "lm_validate_fleet_manifest; lm_realm_field binance engine_unit; "
            "lm_realm_field binance credential_env; lm_owner_unit binance; "
            "lm_signal_worker_unit binance; lm_funded_realms",
        ],
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert out.returncode == 0, out.stderr
    lines = out.stdout.split()
    assert added.engine_unit in lines
    assert added.credential_env in lines
    assert lines.count(added.engine_unit) == 2  # lm_realm_field and lm_owner_unit
    assert added.worker_unit in lines
    assert "binance" in lines


def test_a_second_realm_on_a_known_venue_needs_no_prose(tmp_path: Path) -> None:
    root = _clone(tmp_path)
    _append_realm(
        root,
        "spare|bybit|bybit_spare|spare|funded|stopped|false|true|false|false|50|180|95|213",
    )
    rendered = render_realm_files(root)
    for path, body in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    fields = tuple(realm_fields(realm_policy.realm("spare", root)))
    script = (
        "for field in "
        + " ".join(fields)
        + '; do printf "%s|%s\\n" "$field" "$(lm_realm_field spare "$field")"; done'
    )
    out = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail; export LM_REALM_FIELDS={root}/deploy/realm_fields.tsv "
            f"LM_FLEET_MANIFEST={root}/deploy/fleet_manifest.tsv; "
            f". {root}/deploy/lib_sleeves.sh; {script}",
        ],
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert out.returncode == 0, out.stderr
    answered = dict(line.split("|", 1) for line in out.stdout.splitlines())
    expected = realm_fields(realm_policy.realm("spare", root))
    assert answered == expected


def test_a_venue_with_no_credential_family_is_a_render_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell no longer derives credential lists, so a venue that keeps no
    family must fail where the file is written, not where it is read."""

    root = _clone(tmp_path)
    _append_realm(
        root,
        "binance|binance|binance_mainnet|binance_mainnet|funded|stopped|false"
        "|true|false|false|50|180|95|213",
    )
    monkeypatch.setitem(
        realm_policy.VENUE_FACTS, "binance", {"display": "Binance", "groups": (), "keeps": {}}
    )
    with pytest.raises(ValueError, match="declares no credential families"):
        render_realm_files(root)


def test_a_value_that_cannot_be_a_row_is_a_render_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _clone(tmp_path)
    monkeypatch.setitem(realm_policy.VENUE_FACTS["bybit"], "display", "By|bit")
    with pytest.raises(ValueError, match="cannot be a row"):
        render_realm_files(root)


def test_the_table_refuses_a_stopped_practice_realm(tmp_path: Path) -> None:
    root = _clone(tmp_path)
    table = root / "deploy" / "realms.tsv"
    table.write_text(
        table.read_text(encoding="utf-8").replace(
            "demo|bybit|bybit_demo|demo|practice|running",
            "demo|bybit|bybit_demo|demo|practice|stopped",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must run"):
        realms(root)


# ------------------------------------------------------ pinned other trees


def test_rust_realm_lists_equal_the_table() -> None:
    expected = [row.realm for row in realms()]
    for path, symbol in (
        (ROOT / "engine" / "engine-strategies" / "src" / "native_config.rs", "NATIVE_REALMS"),
        (ROOT / "engine" / "signal-worker" / "src" / "config.rs", "REALMS"),
    ):
        text = path.read_text(encoding="utf-8")
        match = re.search(
            rf"pub const {symbol}: \[&str; (\d+)\] = \[([^\]]*)\];", text
        )
        assert match is not None, f"{path} declares no {symbol}"
        listed = [value.strip().strip('"') for value in match.group(2).split(",") if value.strip()]
        assert listed == expected, f"{path}:{symbol}"
        assert int(match.group(1)) == len(expected)


def test_the_deploy_workflow_disarms_exactly_the_funded_realms() -> None:
    workflow = (ROOT / ".github" / "workflows" / "vps-deploy.yml").read_text(encoding="utf-8")
    modes = [f"disarm-{row.realm}" for row in funded_realms()]
    options = re.search(r"options: \[([^\]]*)\]", workflow)
    assert options is not None
    listed = [value.strip() for value in options.group(1).split(",")]
    assert [mode for mode in listed if mode.startswith("disarm-")] == modes
    assert f"{'|'.join(modes)}) ;;" in workflow


def test_ops_and_deploy_launcher_modes_come_from_the_funded_realms() -> None:
    modes = ["deploy", "rollback", "verify"]
    for row in funded_realms():
        modes += [f"stop-{row.realm}", f"disarm-{row.realm}"]
    launcher = subprocess.run(
        ["bash", str(ROOT / "scripts" / "deploy_vps_live.sh"), "definitely-not-a-mode"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert launcher.returncode == 2
    assert launcher.stderr.splitlines()[0] == (
        "usage: deploy_vps_live.sh {" + "|".join(modes) + "}"
    )
    ops = subprocess.run(
        ["bash", str(ROOT / "scripts" / "ops.sh"), "deploy", "definitely-not-a-mode"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert ops.returncode == 2
    assert "deploy mode must be one of: " + " ".join(modes) in ops.stderr
    # Neither script spells a realm list of its own.
    for text in (
        (ROOT / "scripts" / "deploy_vps_live.sh").read_text(encoding="utf-8"),
        (ROOT / "scripts" / "ops.sh").read_text(encoding="utf-8"),
    ):
        assert "lm_funded_realms" in text
        assert "demo|mainnet|mexc|hyperliquid" not in text


# ------------------------------------------------------------- posture


def test_posture_drives_the_deploy_and_the_table_names_the_realms_that_run() -> None:
    # The practice realm stays up for the soak; the funded postures are the
    # owner's: Bybit mainnet stopped, both alt realms running as a forward test.
    by_name = {row.realm: row for row in realms()}
    assert by_name["demo"].posture == "running"
    assert by_name["mainnet"].posture == "stopped"
    assert by_name["mexc"].posture == "running"
    assert by_name["hyperliquid"].posture == "running"
    # Demo opens nothing: every sleeve's entries are rendered off.
    assert (by_name["demo"].long_entries, by_name["demo"].carry_entries, by_name["demo"].exodus_entries) == ("false", "false", "false")

    deploy = (ROOT / "scripts" / "vps" / "deploy_remote.sh").read_text(encoding="utf-8")
    body = deploy[deploy.index("deploy_mode()") : deploy.index("\nrollback_mode()")]
    assert 'posture=stopped in deploy/realms.tsv: units stay stopped' in body
    assert 'lm_funded_realms' in body
    assert 'stop_funded_units "$realm"' in body
    # The armed, unready and unchanged reports the fleet already prints.
    assert 'real-money off: $realm units stay stopped' in body
    assert "readiness=$FUNDED_REALM_READINESS: units stay stopped" in body
    assert 'result=unchanged-left-running' in body


def test_the_remote_body_carries_the_realm_fields_and_refuses_to_run_without_them() -> None:
    """The pre-fetch steps read realm facts before the host has this commit, so
    the launcher ships the generated fields and their helpers with the script."""

    launcher = (ROOT / "scripts" / "deploy_vps_live.sh").read_text(encoding="utf-8")
    assert "printf 'LM_REALM_FIELDS_TEXT=%q\\n'" in launcher
    assert "printf 'LM_REALMS_SH=%q\\n'" in launcher

    remote = (ROOT / "scripts" / "vps" / "deploy_remote.sh").read_text(encoding="utf-8")
    bootstrap = remote[: remote.index("# ---------------------------------------------------------------- constants")]
    assert 'eval "$LM_REALMS_SH"' in bootstrap
    assert "PRACTICE_REALM=" in bootstrap
    # Every pre-fetch step's realm facts come from that eval, not the checkout.
    deploy_body = remote[remote.index("deploy_mode()") : remote.index("\nrollback_mode()")]
    before_fetch = deploy_body[: deploy_body.index("fetch_exact_commit")]
    assert "pin_funded_runtimes" in before_fetch
    assert "retain_native_checkpoint_configs" in before_fetch
    assert 'if [ -f "$REPO_DIR/deploy/realm_fields.tsv" ]; then' in deploy_body
    assert "unset LM_REALM_FIELDS_TEXT" in deploy_body

    both = ("LM_REALM_FIELDS_TEXT", "LM_REALMS_SH")
    for missing in both:
        script = "\n".join(
            [
                "MODE=verify REPO_URL= REPO_DIR=/tmp REMOTE=origin BRANCH=main",
                "EXPECTED_COMMIT=" + "a" * 40,
                "GITHUB_TOKEN=",
                *(f"{name}=x" for name in both if name != missing),
                remote,
            ]
        )
        result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
        assert result.returncode != 0
        assert "shipped no realm" in result.stderr, missing
