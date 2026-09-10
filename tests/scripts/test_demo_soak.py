from __future__ import annotations

import os
import shlex
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from liquidity_migration.policy.realms import funded_realms, realms

ROOT = Path(__file__).resolve().parents[2]


def remote() -> str:
    return Path(os.environ.get("R3_DEPLOY_SOURCE", ROOT / "scripts/vps/deploy_remote.sh")).read_text()


def function(name: str) -> str:
    body = remote().split(f"{name}() {{", 1)[1]
    return f"{name}() {{" + body.split("\n}\n", 1)[0] + "\n}\n"


#: The realm table's helpers, as the shipped remote body loads them.
REALM_PREAMBLE = "\n".join([
    f'LM_REALM_TABLE="{ROOT}/deploy/realms.tsv"',
    f'. "{ROOT}/deploy/lib_realms.sh"',
    'PRACTICE_REALM="$(lm_practice_realm)"',
    "DIALS_REALM=mainnet",
])


@pytest.mark.parametrize("soak_status", [0, 1])
def test_demo_gate_precedes_every_mainnet_candidate_change(tmp_path: Path, soak_status: int) -> None:
    (tmp_path / "deploy").mkdir()
    for name in ("lib_sleeves.sh", "lib_systemd_environment.sh"):
        (tmp_path / "deploy" / name).write_text("")
    noop = (
        "seed_generation_record retain_native_checkpoint_configs build_engine fetch_exact_commit ensure_runtime_identities "
        "install_python_environment seed_realm_fingerprints prepare_oncall_inputs install_units "
        "start_independent_units prepare_demo_inputs record_generation verify_mode "
        "stage_demo_candidate pin_funded_runtimes clear_demo_candidate_override clear_recorder_runtime"
    ).split()
    harness = "\n".join([
        "set -euo pipefail",
        REALM_PREAMBLE,
        'fail() { echo "$*" >&2; exit 1; }',
        *[f'{name}() {{ echo {name}; }}' for name in noop],
        "realm_armed() { return 0; }",
        "realm_run_ready() { return 0; }",
        'stop_funded_units() { echo "stopped-$1"; }',
        "realm_unchanged() { return 1; }",
        "handover_realm() { echo handover-$1; }",
        "install_release() { echo candidate-shared-install; }",
        'provision_funded_realm() { echo "$1-config"; }',
        f"wait_demo_soak() {{ echo demo-soak; return {soak_status}; }}",
        function("deploy_mode"),
        "deploy_mode",
    ])
    result = subprocess.run(
        ["bash", "-c", harness],
        env={**os.environ, "REPO_DIR": str(tmp_path), "EXPECTED_COMMIT": "a" * 40,
             "RELEASE_DIR": str(tmp_path), "ENGINE_BINARY": "old", "CANDIDATE_RELEASE_DIR": "candidate"},
        capture_output=True, text=True, check=False,
    )
    trace = result.stdout.splitlines()
    funded = list(funded_realms())
    if soak_status:
        for row in funded:
            for line in (f"handover-{row.realm}", f"{row.realm}-config"):
                assert line not in trace, trace
        assert "candidate-shared-install" not in trace, trace
        assert result.returncode != 0
    else:
        assert result.returncode == 0, result.stderr
        assert "demo-soak" in trace, trace
        practice = next(row.realm for row in realms() if not row.funded)
        assert (
            trace.index(f"handover-{practice}")
            < trace.index("demo-soak")
            < trace.index("candidate-shared-install")
        )
        # Every funded realm is configured after the shared install, and only
        # then does the table's posture decide between a handover and a stop.
        for row in funded:
            after = "handover" if row.posture == "running" else "stopped"
            assert (
                trace.index("candidate-shared-install")
                < trace.index(f"{row.realm}-config")
                < trace.index(f"{after}-{row.realm}")
            ), (row.realm, trace)


@pytest.fixture
def recorder_handover(tmp_path: Path):
    import json

    repo, release, units = (tmp_path / name for name in ("repo", "release", "units"))
    for path in (repo / "deploy/systemd", repo / "scripts/runtime", release / "bin", units):
        path.mkdir(parents=True)
    unit = "liquidity-migration-equity-recorder.service"
    script = repo / "scripts/runtime/record_equity.py"
    manifest = repo / "deploy/fleet_manifest.tsv"
    manifest.write_text("# recorder handover fixture\n")
    for name in ("lib_sleeves.sh", "lib_systemd_environment.sh"):
        (repo / "deploy" / name).write_text("")

    sample = (
        "import json,os,pathlib,sys\n"
        "with pathlib.Path(os.environ['RECORDER_SAMPLES']).open('a') as output:\n"
        " output.write(json.dumps({'phase':os.environ.get('RECORDER_PHASE','handover'),"
        "'implementation':IMPLEMENTATION})+'\\n')\n"
    )
    script.write_text("IMPLEMENTATION='python'\n" + sample)
    incumbent_unit = (
        "[Service]\nType=oneshot\n"
        f"WorkingDirectory={repo}\n"
        f"ExecStart={sys.executable} scripts/runtime/record_equity.py\n"
    )
    (repo / "deploy/systemd" / unit).write_text(incumbent_unit)
    (units / unit).write_text(incumbent_unit)

    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "recorder fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("add", ".")
    git("commit", "-qm", "Python recorder")
    python_commit = git("rev-parse", "HEAD")
    script.unlink()
    candidate_unit = (ROOT / "deploy/systemd" / unit).read_text().replace(
        "/opt/liquidity-migration-engine", str(release),
    ).replace("/opt/liquidity-migration", str(repo))
    (repo / "deploy/systemd" / unit).write_text(candidate_unit)
    git("add", "-A")
    git("commit", "-qm", "Rust recorder")
    rust_commit = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", rust_commit)
    git("checkout", "-q", "-B", "main", python_commit)

    artifacts = tmp_path / "artifacts"
    for commit, implementation in ((python_commit, "unsupported"), (rust_commit, "rust")):
        directory = artifacts / commit
        directory.mkdir(parents=True)
        for binary in ("engine", "engine-tools", "signal-worker"):
            executable = directory / binary
            executable.write_text(
                f"#!{sys.executable}\nIMPLEMENTATION={implementation!r}\n"
                "import sys\n"
                "if len(sys.argv)<2 or sys.argv[1]!='record-equity' or IMPLEMENTATION=='unsupported':\n"
                " print('unsupported companion command',file=sys.stderr);sys.exit(2)\n"
                + sample
            )
            executable.chmod(0o755)
            (release / "bin" / binary).write_bytes((artifacts / python_commit / binary).read_bytes())
            (release / "bin" / binary).chmod(0o755)
    mock_bin = tmp_path / "mock-bin"
    mock_bin.mkdir()
    systemctl = mock_bin / "systemctl"
    systemctl.write_text(
        f"#!{sys.executable}\n"
        "import os,pathlib,shlex,subprocess,sys\n"
        "if sys.argv[1]=='daemon-reload': sys.exit(0)\n"
        "if sys.argv[1]!='start': sys.exit('unexpected systemctl command')\n"
        "unit=pathlib.Path(os.environ['LM_SYSTEMD_UNIT_DIR'])/sys.argv[2]\n"
        "command=''\n"
        "for source in [unit,*sorted(unit.with_name(unit.name+'.d').glob('*.conf'))]:\n"
        " for line in source.read_text().splitlines():\n"
        "  if line.startswith('ExecStart='): command=line.removeprefix('ExecStart=')\n"
        "sys.exit(subprocess.run(shlex.split(command),cwd=os.environ['REPO_DIR']).returncode)\n"
    )
    systemctl.chmod(0o755)
    samples = tmp_path / "samples.jsonl"
    environment = {
        "PATH": f"{mock_bin}:{os.environ['PATH']}", "REPO_DIR": str(repo),
        "RELEASE_DIR": str(release), "ENGINE_TOOLS_BINARY": str(release / "bin/engine-tools"),
        "ENGINE_BINARY": str(release / "bin/engine"), "LM_SYSTEMD_UNIT_DIR": str(units),
        "RUNTIME_GROUP": subprocess.check_output(["id", "-gn"], text=True).strip(),
        "RECORDER_SAMPLES": str(samples), "FIXTURE_ARTIFACTS": str(artifacts),
        "SOAK_OVERRIDE": "20-demo-soak.conf", "REMOTE": "origin", "BRANCH": "main",
    }

    def run(commit: str, soak_status: int, *, compatible: bool = True):
        baseline = {path.name: path.read_bytes() for path in (release / "bin").iterdir()}
        expected = tmp_path / "incumbent"
        expected.mkdir(exist_ok=True)
        for name, data in baseline.items():
            (expected / name).write_bytes(data)
        noop = (
            "seed_generation_record retain_native_checkpoint_configs pin_funded_runtimes install_python_environment "
            "seed_realm_fingerprints prepare_oncall_inputs prepare_demo_inputs record_generation "
            "verify_mode clear_demo_candidate_override"
        ).split()
        definitions = [function(name) for name in (
            "cleanup_release", "fetch_exact_commit", "stage_demo_candidate", "deploy_mode",
            "prepare_recorder_runtime", "clear_recorder_runtime",
        ) if f"{name}() {{" in remote()]
        harness = "\n".join([
            "set -euo pipefail", REALM_PREAMBLE,
            "QUALIFIED_RELEASE_DIR= INCUMBENT_STAGE= CANDIDATE_RELEASE_DIR=",
            'fail() { echo "$*" >&2; exit 1; }',
            *[f"{name}() {{ :; }}" for name in noop],
            'install() { local last="${!#}"; if [ "$1" = -d ]; then mkdir -p "$last"; '
            'else local before=$(( $# - 1 )); cp "${!before}" "$last"; chmod 0755 "$last"; fi; }',
            "git_authorized() { :; }",
            'realm_armed() { [ "$1" = mainnet ]; }', "realm_run_ready() { return 0; }",
            "realm_unchanged() { return 1; }",
            f"rollback_runtime_compatible() {{ return {0 if compatible else 1}; }}",
            'build_engine() { QUALIFIED_RELEASE_DIR="$(mktemp -d "$RELEASE_DIR/.qualified.XXXXXX")"; '
            'cp "$FIXTURE_ARTIFACTS/$EXPECTED_COMMIT/"* "$QUALIFIED_RELEASE_DIR/"; }',
            f'tick() {{ RECORDER_PHASE="$1" systemctl start {unit}; }}',
            'ensure_runtime_identities() { tick after-checkout; }',
            f'install_units() {{ cp "$REPO_DIR/deploy/systemd/{unit}" "$LM_SYSTEMD_UNIT_DIR/{unit}"; }}',
            "start_independent_units() { tick new-unit; }", "handover_realm() { :; }",
            "stop_funded_units() { :; }",
            'wait_demo_soak() { for binary in engine engine-tools signal-worker; do '
            'cmp "$RELEASE_DIR/bin/$binary" "$FIXTURE_INCUMBENT/$binary"; done; '
            f'tick soak; return {soak_status}; }}',
            'install_release() { cp "$QUALIFIED_RELEASE_DIR/"* "$RELEASE_DIR/bin/"; }',
            "provision_funded_realm() { tick after-install; }",
            *definitions, "trap cleanup_release EXIT", "deploy_mode",
        ])
        result = subprocess.run(
            ["bash", "-c", harness],
            env={**environment, "EXPECTED_COMMIT": commit, "FIXTURE_INCUMBENT": str(expected)},
            text=True, capture_output=True, check=False,
        )
        rows = [json.loads(line) for line in samples.read_text().splitlines()] if samples.exists() else []
        return result, rows, baseline

    return run, environment, python_commit, rust_commit


@pytest.mark.parametrize("soak_status", [0, 1])
def test_recorder_runs_after_python_deletion_and_failed_soak_cleanup(recorder_handover, soak_status: int) -> None:
    run, environment, _python, rust = recorder_handover
    result, rows, baseline = run(rust, soak_status)
    phases = {row["phase"]: row["implementation"] for row in rows}
    assert phases.get("after-checkout") == "rust", f"recorder cannot run after Python deletion:\n{result.stderr}"
    assert phases["new-unit"] == phases["soak"] == "rust"
    release = Path(environment["RELEASE_DIR"])
    assert not list(release.glob(".qualified.*"))
    if soak_status:
        assert result.returncode != 0
        assert {path.name: path.read_bytes() for path in (release / "bin").iterdir()} == baseline
    else:
        assert result.returncode == 0, result.stderr
        assert phases["after-install"] == "rust"
    subprocess.run(
        ["systemctl", "start", "liquidity-migration-equity-recorder.service"],
        env=environment, text=True, capture_output=True, check=True,
    )


def test_recorder_retry_keeps_the_permanent_candidate_and_finishes_handover(recorder_handover) -> None:
    run, environment, _python, rust = recorder_handover
    first, first_rows, _ = run(rust, 1)
    assert any(row["phase"] == "soak" for row in first_rows), first.stderr
    assert first.returncode != 0
    second, rows, _ = run(rust, 0)
    assert second.returncode == 0, second.stderr
    assert rows[-1] == {"phase": "after-install", "implementation": "rust"}
    assert not list(Path(environment["LM_SYSTEMD_UNIT_DIR"]).glob("*.service.d/*recorder*.conf"))


def test_recorder_python_unit_rollback_does_not_run_unsupported_candidate(recorder_handover) -> None:
    run, environment, python, rust = recorder_handover
    first, rows, _ = run(rust, 1)
    assert any(row["phase"] == "soak" for row in rows), first.stderr
    result, rows, _ = run(python, 0)
    assert result.returncode == 0, result.stderr
    assert rows[-1] == {"phase": "after-install", "implementation": "python"}
    assert not list(Path(environment["LM_SYSTEMD_UNIT_DIR"]).glob("*.service.d/*recorder*.conf"))


def test_recorder_rejected_backward_target_keeps_the_existing_override(recorder_handover) -> None:
    run, environment, python, rust = recorder_handover
    first, rows, _ = run(rust, 1)
    assert any(row["phase"] == "soak" for row in rows), first.stderr
    units = Path(environment["LM_SYSTEMD_UNIT_DIR"])
    before = {path: path.read_bytes() for path in units.glob("*.service.d/*.conf")}
    result, after_rows, _ = run(python, 0, compatible=False)
    assert result.returncode != 0
    assert "older deploy has the same compatibility requirements" in result.stderr
    assert after_rows == rows
    assert {path: path.read_bytes() for path in units.glob("*.service.d/*.conf")} == before


def test_runtime_install_removes_an_already_installed_research_package(tmp_path: Path) -> None:
    runtime = tmp_path / ".venv"
    venv.EnvBuilder(with_pip=True).create(runtime)
    python = runtime / "bin/python"
    site = Path(subprocess.check_output([str(python), "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"], text=True).strip())
    (site / "stale_research.py").write_text("AVAILABLE = True\n")
    metadata = site / "stale_research-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: stale-research\nVersion: 1.0\n")
    (metadata / "RECORD").write_text("stale_research.py,,\nstale_research-1.0.dist-info/METADATA,,\nstale_research-1.0.dist-info/RECORD,,\n")
    (tmp_path / "requirements-runtime.lock").write_text("# stdlib-only test runtime\n")
    scripts = tmp_path / "scripts/vps"
    scripts.mkdir(parents=True)
    (scripts / "sync_runtime_dependencies.py").write_bytes((ROOT / "scripts/vps/sync_runtime_dependencies.py").read_bytes())
    result = subprocess.run(
        ["bash", "-c", "\n".join([
            "set -euo pipefail", 'fail() { echo "$*" >&2; exit 1; }',
            function("python_requirements_path"), function("install_python_environment"), "install_python_environment",
        ])],
        env={**os.environ, "REPO_DIR": str(tmp_path), "PYTHON": str(python), "PIP_NO_INDEX": "1"},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (site / "stale_research.py").exists(), "host retains a research package after the runtime-only install"


@pytest.fixture
def resources(tmp_path: Path, monkeypatch):
    import importlib.util
    import json
    import sys
    import time

    spec = importlib.util.spec_from_file_location("soak_liveness", ROOT / "scripts/runtime/check_fleet_liveness.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    heartbeat = tmp_path / "heartbeat.json"
    wal = tmp_path / "engine.wal"
    wal.write_bytes(b"baseline")
    observed = {"now": time.time(), "pid": 101.0, "restarts": 0.0, "rss": 1_000.0, "errors": 0}
    row = module.FleetUnit("liquidity-migration-engine.service", "service", "demo", "always", "active", str(heartbeat))
    monkeypatch.setattr(module, "load_fleet_manifest", lambda: [row])
    monkeypatch.setattr(module, "unit_states", lambda units: {unit: "active" for unit in units})
    monkeypatch.setattr(module, "engine_service_sample", lambda _: {key: observed[key] for key in ("pid", "restarts", "rss")})
    monkeypatch.setattr(module, "engine_error_count", lambda *_: observed["errors"])
    monkeypatch.setattr(module.time, "time", lambda: observed["now"])
    monkeypatch.setattr(module.time, "monotonic", lambda: observed["now"])

    def heartbeat_update():
        heartbeat.write_text(json.dumps({"may_open": True, "rolling_loss_tripped": observed.get("loss", False), "strategy_errors": []}))
        os.utime(heartbeat, (observed["now"], observed["now"]))

    def advance(seconds):
        observed["now"] += seconds
        heartbeat_update()

    heartbeat_update()
    monkeypatch.setattr(module.time, "sleep", advance)
    return module, row, wal, observed, advance


def test_fifteen_megabytes_per_second_refuses_and_pages_in_ten_seconds(resources, monkeypatch) -> None:
    module, _row, wal, observed, advance = resources
    started = observed["now"]
    pages = []
    monkeypatch.setattr(module, "send_telegram_message", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "unit_journal_tail", lambda *_: "constructed local writer")
    monkeypatch.setattr(module, "fire_incident_routine", lambda _url, _token, text: pages.append((observed["now"], text)))
    monkeypatch.setenv(module.INCIDENT_FIRE_URL_ENV, "https://example.invalid/incident")
    monkeypatch.setenv(module.INCIDENT_FIRE_TOKEN_ENV, "fixture-token")

    def write_and_advance(seconds):
        with wal.open("r+b") as handle:
            handle.truncate(wal.stat().st_size + int(15_000_000 * seconds))
        advance(seconds)

    monkeypatch.setattr(module.time, "sleep", write_and_advance)
    assert module.run_demo_soak() == 1
    assert len(pages) == 1
    assert pages[0][0] - started == 10
    assert "engine-wal-rate:" in pages[0][1]
    assert "15000000 bytes/s" in pages[0][1]


def test_demo_soak_requires_all_five_minutes(resources) -> None:
    module, _row, _wal, observed, _advance = resources
    started = observed["now"]
    assert module.run_demo_soak() == 0
    assert observed["now"] - started == 300


def test_demo_soak_retains_risk_halt_without_mistaking_it_for_a_runtime_fault(resources, monkeypatch, capsys) -> None:
    module, row, _wal, observed, advance = resources
    observed["loss"] = True
    advance(0)
    monkeypatch.setattr(module, "send_telegram_message", lambda *_args, **_kwargs: False)
    started = observed["now"]
    assert module.run_demo_soak() == 0
    assert observed["now"] - started == 300
    alerts = module.evaluate_engine_heartbeat(row.unit, Path(row.output_artifact), now=observed["now"])
    assert [(alert.key, alert.severity) for alert in alerts] == [(f"rolling-loss:{row.unit}", "NOTICE")]
    assert "entries refused" in capsys.readouterr().out


@pytest.mark.parametrize("may_open,expected", [(True, 0), (False, 1)])
def test_started_process_retains_rolling_loss_restriction(resources, monkeypatch, capsys, may_open, expected) -> None:
    import json
    import sys

    module, row, _wal, observed, _advance = resources
    heartbeat = Path(row.output_artifact)
    payload = {"pid": 101, "wall_ts_ms": int(observed["now"] * 1000), "may_open": may_open,
               "rolling_loss_tripped": True, "rolling_loss_net_usdt": -164.54,
               "rolling_loss_limit_usdt": 162.70, "rolling_loss_window_ms": 86_400_000, "strategy_errors": []}
    heartbeat.write_text(json.dumps(payload))
    original = heartbeat.read_bytes()
    monkeypatch.setattr(sys, "argv", ["liveness", "--check-heartbeat", row.unit, str(heartbeat), "101", str(observed["now"] - 1)])
    assert module.main() == expected
    assert heartbeat.read_bytes() == original
    assert "entries refused" in capsys.readouterr().out


@pytest.mark.parametrize("fault, expected", [
    ("rss", "engine-rss:"), ("pid", "engine-restarts:"), ("restarts", "engine-restarts:"),
    ("errors", "engine-error-rate:"), ("truncate", "engine-resource-sample:"),
])
def test_resource_faults_are_detected_with_an_active_healthy_unit(resources, fault: str, expected: str) -> None:
    module, row, wal, observed, advance = resources
    counters = {}
    assert module.evaluate_engine_rates([row], now=observed["now"], counters=counters) == []
    advance(10)
    if fault == "truncate":
        wal.write_bytes(b"")
    else:
        observed[fault] += module._ENGINE_RSS_BYTES if fault == "rss" else 1
    alerts = module.evaluate_engine_rates([row], now=observed["now"], counters=counters)
    assert any(alert.key.startswith(expected) for alert in alerts), alerts


def test_resource_sampling_counts_rotation_without_double_counting_old_segments(resources) -> None:
    module, row, wal, observed, advance = resources
    counters = {}
    assert module.evaluate_engine_rates([row], now=observed["now"], counters=counters) == []
    wal.with_name("engine.wal.000002").write_bytes(b"new-segment")
    advance(10)
    assert module.evaluate_engine_rates([row], now=observed["now"], counters=counters) == []
    advance(10)
    assert module.evaluate_engine_rates([row], now=observed["now"], counters=counters) == []


def pin_fixture(tmp_path: Path) -> tuple[dict[str, str], list[Path]]:
    release = tmp_path / "release"
    (release / "bin").mkdir(parents=True)
    for name in ("engine", "signal-worker"):
        executable = release / "bin" / name
        executable.write_text(
            "#!/usr/bin/env python3\nimport pathlib,sys\nprint('incumbent')\n"
            "for flag in ('--config','--signal-config','--long-rule','--carry-config','--operational-config','--engine-config'):\n"
            " if flag in sys.argv: print(pathlib.Path(sys.argv[sys.argv.index(flag)+1]).read_text())\n"
        )
        executable.chmod(0o755)
    inputs = []
    for name in ("signal", "long", "carry", "engine", "operational"):
        path = tmp_path / (name + ".json")
        path.write_text("incumbent-" + name)
        inputs.append(path)
    worker_environment = tmp_path / "worker.env"
    worker_environment.write_text(f"OPERATIONAL_PROFILE_FILE={inputs[4]}\n")
    worker_environment.chmod(0o600)
    mock_bin = tmp_path / "mock-bin"
    mock_bin.mkdir()
    systemctl = mock_bin / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\ncase \"$*\" in\n*MainPID*) echo 0;;\n*Environment*) "
        + "printf '%s\\n' " + shlex.quote(" ".join(
            f"{key}={path}" for key, path in zip(
                ("SIGNAL_WORKER_CONFIG_FILE", "LONG_NATIVE_RULE_FILE", "CARRY_SIGNAL_CONFIG_FILE"), inputs[:3], strict=True
            )
        )) + ";;\nesac\n"
    )
    systemctl.chmod(0o755)
    return {
        **os.environ, "PATH": f"{mock_bin}:{os.environ['PATH']}", "REPO_DIR": str(ROOT),
        "RELEASE_DIR": str(release), "RUNTIME_GROUP": subprocess.check_output(["id", "-gn"], text=True).strip(),
        "LM_SYSTEMD_UNIT_DIR": str(tmp_path / "units"), "SOAK_OVERRIDE": "20-demo-soak.conf",
        "ENGINE_MAINNET_CONFIG": str(inputs[3]), "SIGNAL_WORKER_MAINNET_ENV": str(worker_environment),
        "PYTHON": sys.executable,
    }, inputs


def run_pin(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    harness = "\n".join([
        "set -euo pipefail", "INCUMBENT_STAGE=", "QUALIFIED_RELEASE_DIR=",
        'fail() { echo "$*" >&2; exit 1; }',
        # Preserve install's actual copies and directory creation without requiring root in a local test.
        'install() { local last="${!#}"; if [ "$1" = -d ]; then mkdir -p "$last"; '
        'else local before=$(( $# - 1 )); cp "${!before}" "$last"; chmod 0755 "$last"; fi; }',
        # The realm is armed and its credential path is irrelevant here: what
        # this exercises is the frozen snapshot, not the arming read.
        "funded_credential_env() { echo credential; }", "credential_armed() { return 0; }",
        # The realm's names, as deploy/realms.tsv answers them, with this
        # fixture's own incumbent inputs.
        'lm_realm_field() { case "$2" in '
        'kind) echo funded ;; '
        'engine_config) echo "$ENGINE_MAINNET_CONFIG" ;; '
        'worker_env) echo "$SIGNAL_WORKER_MAINNET_ENV" ;; '
        'engine_unit) echo liquidity-migration-engine-mainnet.service ;; '
        'worker_unit) echo liquidity-migration-signal-worker-mainnet.service ;; '
        'esac; }',
        function("cleanup_release"), "trap cleanup_release EXIT",
        function("pin_realm_runtime"), "pin_realm_runtime mainnet",
    ])
    return subprocess.run(["bash", "-c", harness], env=environment, text=True, capture_output=True, check=False)


def test_incumbent_restart_uses_frozen_binary_and_all_five_inputs_after_candidate_files_change(tmp_path: Path) -> None:
    environment, inputs = pin_fixture(tmp_path)
    result = run_pin(environment)
    assert result.returncode == 0, result.stderr
    for path in inputs:
        path.write_text("candidate-input")
    for path in (Path(environment["RELEASE_DIR"]) / "bin").iterdir():
        path.write_text("#!/bin/sh\necho candidate-binary\n")
    for unit in ("engine-mainnet", "signal-worker-mainnet"):
        override = Path(environment["LM_SYSTEMD_UNIT_DIR"]) / f"liquidity-migration-{unit}.service.d/20-demo-soak.conf"
        command = next(line.removeprefix("ExecStart=") for line in override.read_text().splitlines()
                       if line.startswith("ExecStart=") and line != "ExecStart=")
        executed = subprocess.run(shlex.split(command), env=environment, capture_output=True, text=True, check=True)
        assert "candidate" not in executed.stdout
        assert "incumbent-engine" in executed.stdout
        if unit.startswith("signal"):
            for name in ("signal", "long", "carry", "operational"):
                assert "incumbent-" + name in executed.stdout


def test_failed_incumbent_snapshot_does_not_publish_a_partial_directory_and_retry_recovers(tmp_path: Path) -> None:
    environment, inputs = pin_fixture(tmp_path)
    inputs[1].unlink()
    result = run_pin(environment)
    assert result.returncode != 0
    release = Path(environment["RELEASE_DIR"])
    assert not (release / "incumbent-mainnet").exists()
    assert not list(release.glob(".incumbent-mainnet.*"))
    inputs[1].write_text("incumbent-long")
    result = run_pin(environment)
    assert result.returncode == 0, result.stderr
    assert (release / "incumbent-mainnet/worker-inputs.conf").exists()


def test_checkpoint_source_config_survives_deploy_retries(tmp_path: Path) -> None:
    deployed = tmp_path / "deployed"
    deployed.write_text("a" * 40)
    source = tmp_path / "engine.toml"
    source.write_text("incumbent bytes\n")
    harness = "\n".join([
        "set -euo pipefail",
        REALM_PREAMBLE,
        # Every realm's rendered config is this one fixture file.
        'lm_realm_field() { printf \'%s\\n\' "$CHECKPOINT_SOURCE"; }',
        'fail() { echo "$*" >&2; exit 1; }',
        'install() { local -a args=(); while [ "$#" -gt 0 ]; do '
        'case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac; '
        'done; command install "${args[@]}"; }',
        function("retain_native_checkpoint_configs"),
        "retain_native_checkpoint_configs",
    ])
    env = {**os.environ, "DEPLOYED_COMMIT_FILE": str(deployed), "RELEASE_DIR": str(tmp_path),
           "CHECKPOINT_SOURCE": str(source), "RUNTIME_GROUP": "unused"}
    subprocess.run(["bash", "-c", harness], env=env, check=True, capture_output=True, text=True)
    source.write_text("candidate bytes\n")
    subprocess.run(["bash", "-c", harness], env=env, check=True, capture_output=True, text=True)
    for row in realms():
        saved = tmp_path / "checkpoint-configs" / ("a" * 40) / f"engine.{row.realm}.toml"
        assert saved.read_text() == "incumbent bytes\n"
    deployed.write_text("b" * 40)
    subprocess.run(["bash", "-c", harness], env=env, check=True, capture_output=True, text=True)
    assert (tmp_path / "checkpoint-configs" / ("b" * 40) / "engine.demo.toml").read_text() == "candidate bytes\n"
