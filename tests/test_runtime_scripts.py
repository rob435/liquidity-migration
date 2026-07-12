from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from liquidity_migration.storage import read_dataset

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy_vps_live.sh"
VERIFY_SH = REPO_ROOT / "scripts" / "verify_vps_live.sh"
RECOVERY_SH = REPO_ROOT / "scripts" / "vps_console_recover_and_deploy.sh"


def test_continuous_hedge_timer_reconciles_within_five_minutes() -> None:
    timer = (
        REPO_ROOT / "deploy/systemd/liquidity-migration-continuous-hedge.timer"
    ).read_text(encoding="utf-8")
    assert "OnBootSec=2min" in timer
    assert "OnUnitActiveSec=5min" in timer
    assert "OnCalendar=" not in timer


def _unit_env(unit: str) -> dict[str, str]:
    text = (REPO_ROOT / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("Environment="):
            continue
        body = line.split("=", 1)[1]
        if "=" not in body:
            continue
        key, value = body.split("=", 1)
        env[key] = value
    return env


def test_deploy_verify_require_unit_env_matches_unit_files() -> None:
    units = {
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-long-paper.service",
        "liquidity-migration-bybit-continuous-demo.service",
        "liquidity-migration-bybit-continuous-paper.service",
        "liquidity-migration-bybit-risk.service",
    }
    unit_env = {unit: _unit_env(unit) for unit in units}
    pattern = re.compile(r"require_unit_env\s+([^\s]+)\s+'([^'=]+)=([^']*)'")

    for script in (DEPLOY_SH, VERIFY_SH, RECOVERY_SH):
        text = script.read_text(encoding="utf-8")
        for unit, key, expected in pattern.findall(text):
            if unit not in unit_env:
                continue
            assert key in unit_env[unit], f"{script.name}: {unit} checks missing env {key}"
            assert unit_env[unit][key] == expected, (
                f"{script.name}: {unit} checks {key}={expected!r}, "
                f"but unit file sets {unit_env[unit][key]!r}"
            )


def test_verify_vps_serializes_remote_values_without_shell_injection(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "ssh-stdin"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\ncat >\"$CAPTURE\"\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    sentinel = tmp_path / "injected"
    values = {
        "REPO_DIR": f"/tmp/o'hare; touch {sentinel}; #",
        "EXPECTED_COMMIT": f"abc$(touch {sentinel})'def",
        "EXPECTED_TELEGRAM_CHAT_ID": "id with spaces;false",
        "SYSTEMD_SETTLE_SECONDS": "0",
    }
    env = {
        **os.environ,
        **values,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CAPTURE": str(capture),
    }

    subprocess.run(["bash", str(VERIFY_SH)], env=env, check=True, timeout=10)

    assert not sentinel.exists()
    prelude = tmp_path / "prelude.sh"
    prelude.write_text("\n".join(capture.read_text().splitlines()[:4]) + "\n")
    decoded = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; source "$1"; printf "%s\\0%s\\0%s\\0%s" '
            '"$REPO_DIR" "$EXPECTED_COMMIT" "$EXPECTED_TELEGRAM_CHAT_ID" '
            '"$SYSTEMD_SETTLE_SECONDS"',
            "_",
            str(prelude),
        ],
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    assert [part.decode() for part in decoded] == list(values.values())


def test_deploy_uses_local_gh_token_without_exposing_it(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "ssh-stdin"
    gh_args = tmp_path / "gh-args"
    sentinel = tmp_path / "injected"
    token = f"ghp_test token;$(touch {sentinel})'"

    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\ncat >\"$CAPTURE\"\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s\\n' \"$*\" >\"$GH_ARGS_CAPTURE\"\n"
        "printf '%s\\n' \"$GH_TOKEN_SENTINEL\"\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CAPTURE": str(capture),
        "GH_ARGS_CAPTURE": str(gh_args),
        "GH_TOKEN_SENTINEL": token,
    }
    env.pop("GITHUB_TOKEN", None)

    result = subprocess.run(
        ["bash", str(DEPLOY_SH)],
        env=env,
        check=True,
        timeout=10,
        capture_output=True,
        text=True,
    )

    assert gh_args.read_text(encoding="utf-8").strip() == "auth token --hostname github.com"
    assert "authenticated local gh credential" in result.stdout
    assert token not in result.stdout
    assert token not in result.stderr
    assert not sentinel.exists()

    prelude = tmp_path / "prelude.sh"
    prelude.write_text("\n".join(capture.read_text().splitlines()[:8]) + "\n")
    decoded = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; source "$1"; printf "%s" "$GITHUB_TOKEN"',
            "_",
            str(prelude),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert decoded == token
    assert not sentinel.exists()


def test_verify_vps_accepts_unique_abbreviated_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "test"], cwd=repo, check=True)
    full_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "ssh-stdin"
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\ncat >\"$CAPTURE\"\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CAPTURE": str(capture),
        "REPO_DIR": str(repo),
        "EXPECTED_COMMIT": full_sha[:8],
        "SYSTEMD_SETTLE_SECONDS": "0",
    }
    subprocess.run(["bash", str(VERIFY_SH)], env=env, check=True, timeout=10)

    remote_lines = capture.read_text(encoding="utf-8").splitlines()
    python_marker = remote_lines.index("if [ -x .venv/bin/python ]; then")
    commit_check = "\n".join(remote_lines[:python_marker]) + "\n"
    result = subprocess.run(
        ["bash"],
        input=commit_check,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_reconcile_shell_exports_utf8_python_io() -> None:
    text = (REPO_ROOT / "scripts" / "reconcile.sh").read_text(encoding="utf-8")

    assert 'PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"' in text
    quick = (REPO_ROOT / "scripts" / "reconcile.py").read_text(encoding="utf-8")
    assert 'p.add_argument("--sleeves", default="long,continuous"' in quick


def test_runtime_scripts_do_not_delete_live_cycle_locks() -> None:
    repo = Path(__file__).resolve().parents[1]
    scripts = [
        repo / "scripts" / "run_bybit_demo_ws_risk_engine.sh",
    ]

    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert "rm -f \"$DATA_ROOT/.locks/" not in text
        assert "mkdir -p \"$DATA_ROOT/.locks\"" in text


def test_ws_risk_runner_requires_every_shared_account_root() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "run_bybit_demo_ws_risk_engine.sh").read_text(encoding="utf-8")

    assert "-z \"$LONG_DATA_ROOT\"" in text
    assert "-z \"$CONTINUOUS_DATA_ROOT\"" in text
    assert "-z \"$CONTINUOUS_ADDON_DATA_ROOT\"" in text
    assert "Set all three roots" in text


def test_continuous_rebalance_cycle_audit_parser_defaults() -> None:
    from liquidity_migration.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["continuous-rebalance-cycle-audit"])

    assert args.command == "continuous-rebalance-cycle-audit"
    # The audit's own root lives on a unique dest so it no longer shadows the
    # global --data-root (argparse subparser-default collision); the global stays
    # at its own default (None) unless the operator passes it.
    assert args.audit_data_root == "data/bybit-continuous-paper-event"
    assert args.data_root is None
    assert args.cycles_dataset == "continuous_fade_paper_cycles"
    assert args.orders_dataset == "continuous_fade_paper_orders"
    assert args.start_ts_ms is None
    assert args.strategy_profile is None
    assert args.strategy_id is None

    # A global --data-root before the subcommand is preserved (was silently
    # clobbered by the subparser default before the dest rename).
    args2 = parser.parse_args(["--data-root", "data/custom", "continuous-rebalance-cycle-audit"])
    assert args2.data_root == "data/custom"
    assert args2.audit_data_root == "data/bybit-continuous-paper-event"

    args3 = parser.parse_args(
        [
            "continuous-rebalance-cycle-audit",
            "--start-ts-ms",
            "1781812440000",
            "--strategy-profile",
            "continuous_ensemble_v2",
            "--strategy-id",
            "continuous_fade_v2_paper",
        ]
    )
    assert args3.start_ts_ms == 1_781_812_440_000
    assert args3.strategy_profile == "continuous_ensemble_v2"
    assert args3.strategy_id == "continuous_fade_v2_paper"


def test_continuous_forward_readiness_parser_defaults() -> None:
    from liquidity_migration.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["continuous-forward-readiness"])

    assert args.command == "continuous-forward-readiness"
    assert args.paper_data_root == "data/bybit-continuous-paper-event"
    assert args.demo_data_root == "data/bybit-continuous-demo-event"
    assert args.entry_tolerance_ms == 600_000
    assert args.min_pairs_warning == 20
    assert args.allow_unmatched is False
    assert args.paper_only is False
    assert args.start_ts_ms is None
    assert args.strategy_profile is None
    assert args.paper_strategy_id is None
    assert args.demo_strategy_id is None

    args2 = parser.parse_args(
        [
            "continuous-forward-readiness",
            "--start-ts-ms",
            "1781812440000",
            "--strategy-profile",
            "continuous_ensemble_v2",
            "--paper-strategy-id",
            "continuous_fade_v2_paper",
            "--demo-strategy-id",
            "continuous_fade_v2",
        ]
    )
    assert args2.start_ts_ms == 1_781_812_440_000
    assert args2.strategy_profile == "continuous_ensemble_v2"
    assert args2.paper_strategy_id == "continuous_fade_v2_paper"
    assert args2.demo_strategy_id == "continuous_fade_v2"


def test_continuous_forward_readiness_cli_prints_na_for_skipped_demo(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from liquidity_migration import cli

    args = cli.build_parser().parse_args(["continuous-forward-readiness", "--paper-only"])
    payload = {
        "ok": True,
        "summary": {
            "paper_only_mode": True,
            "paper_rebalance_ok": True,
            "demo_rebalance_ok": None,
            "paper_operational_ok": True,
            "demo_operational_ok": None,
            "paired": None,
            "paper_only": None,
            "demo_only": None,
            "sample_warning": None,
            "start_ts_ms": None,
            "strategy_profile": "",
            "paper_strategy_id": "",
            "demo_strategy_id": "",
        },
        "report_path": str(tmp_path / "report.md"),
    }
    monkeypatch.setattr(cli, "run_continuous_forward_readiness", lambda *args, **kwargs: payload)

    assert cli._cmd_continuous_forward_readiness(args, None, tmp_path) == 0  # type: ignore[arg-type]
    output = capsys.readouterr().out
    assert "demo_rebalance_ok=n/a" in output
    assert "demo_operational_ok=n/a" in output
    assert "paired=n/a" in output
    assert "paper_only=n/a" in output
    assert "demo_only=n/a" in output
    assert "sample_warning=n/a" in output


def test_continuous_event_demo_cycle_parser_rebalance_profile_flags() -> None:
    from liquidity_migration.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "continuous-event-demo-cycle",
            "--strategy-profile",
            "continuous_ensemble_v2",
            "--feature-set",
            "max_ret168",
            "--entry-event-trigger",
            "none",
            "--btc-trend-gate",
            "uptrend",
            "--daily-rebalance-enabled",
        ]
    )

    assert args.command == "continuous-event-demo-cycle"
    assert args.strategy_profile == "continuous_ensemble_v2"
    assert args.feature_set == "max_ret168"
    assert args.entry_event_trigger == "none"
    assert args.btc_trend_gate == "uptrend"
    assert args.daily_rebalance_enabled is True


def test_continuous_runner_wires_rebalance_profile_env() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "run_bybit_continuous_demo_event_engine.sh").read_text(encoding="utf-8")

    # 2026-06-18: the live default is the repaired v2 lifecycle.
    assert 'STRATEGY_PROFILE="${STRATEGY_PROFILE:-continuous_ensemble_v2}"' in text
    assert 'FEATURE_SET="${FEATURE_SET:-max_ret168}"' in text
    assert 'MAX_HOLD_HOURS="${MAX_HOLD_HOURS:-24}"' in text
    assert "confirmed-bar +1h membership" in text
    stale_entry_labels = ("no " + "1h", "no" + "-1h")
    assert all(label not in text.casefold() for label in stale_entry_labels)
    assert "--strategy-profile \"$STRATEGY_PROFILE\"" in text
    assert "--feature-set \"$FEATURE_SET\"" in text
    assert "--max-hold-hours \"$MAX_HOLD_HOURS\"" in text
    assert 'LEFT_DECILE_EXIT_ENABLED="${LEFT_DECILE_EXIT_ENABLED:-0}"' in text
    assert 'STOP_APPROACH_FRAC="${STOP_APPROACH_FRAC:-0}"' in text
    assert "--stop-approach-frac \"$STOP_APPROACH_FRAC\"" in text
    assert "--failed-fade-hours \"$FAILED_FADE_HOURS\"" in text
    assert "--breakeven-arm-pct \"$BREAKEVEN_ARM_PCT\"" in text
    assert 'SIZING_MODE="${SIZING_MODE:-inverse_vol}"' in text
    assert 'TARGET_VOL_PER_NAME="${TARGET_VOL_PER_NAME:-0.01}"' in text
    assert 'VOL_WEIGHT_CLAMP="${VOL_WEIGHT_CLAMP:-2}"' in text
    assert 'ENTRY_PORTFOLIO_HEAT_CAP_FRAC="${ENTRY_PORTFOLIO_HEAT_CAP_FRAC:-0}"' in text
    assert 'ENTRY_PORTFOLIO_HEAT_SHOCK_FRAC="${ENTRY_PORTFOLIO_HEAT_SHOCK_FRAC:-1}"' in text
    assert (
        'ENTRY_ACCOUNT_DRAWDOWN_KILL_SWITCH_FRAC="${ENTRY_ACCOUNT_DRAWDOWN_KILL_SWITCH_FRAC:-0}"'
        in text
    )
    assert "--sizing-mode \"$SIZING_MODE\"" in text
    assert "--target-vol-per-name \"$TARGET_VOL_PER_NAME\"" in text
    assert "--vol-weight-clamp \"$VOL_WEIGHT_CLAMP\"" in text
    assert "--entry-portfolio-heat-cap-frac \"$ENTRY_PORTFOLIO_HEAT_CAP_FRAC\"" in text
    assert "--entry-portfolio-heat-shock-frac \"$ENTRY_PORTFOLIO_HEAT_SHOCK_FRAC\"" in text
    assert (
        "--entry-account-drawdown-kill-switch-frac \"$ENTRY_ACCOUNT_DRAWDOWN_KILL_SWITCH_FRAC\""
        in text
    )
    assert 'DAILY_REBALANCE_ENABLED="${DAILY_REBALANCE_ENABLED:-0}"' in text
    assert 'DAILY_REBALANCE_TARGET_DAILY_VOL="${DAILY_REBALANCE_TARGET_DAILY_VOL:-0.045}"' in text
    assert 'DAILY_REBALANCE_STRATEGY_MOMENTUM_WINDOW_DAYS="${DAILY_REBALANCE_STRATEGY_MOMENTUM_WINDOW_DAYS:-0}"' in text
    assert "--daily-rebalance-enabled" in text
    assert "--daily-rebalance-strategy-momentum-min-return" in text
    # sniper arm switch present (default off; CONTINUOUS_SNIPER=1 arms it)
    assert "CONTINUOUS_SNIPER" in text


def test_reconcile_continuous_uses_forward_readiness_gate() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "reconcile.py").read_text(encoding="utf-8")

    assert '"continuous-forward-readiness"' in text
    assert '"--paper-only"' in text
    assert "CONTINUOUS_V2_START_MS" in text
    assert '"--strategy-profile", strategy_profile' in text
    assert '"--paper-strategy-id", paper_strategy_id' in text
    assert '"reconcile-continuous-paper-demo"' in text
    assert '"--demo-strategy-id", demo_strategy_id' in text
    assert '"--min-pairs-warning", "0"' in text
    assert '"--root", paper' in text
    assert '"--market-root", demo' in text
    assert '"--trades-dataset", "continuous_fade_paper_trades"' in text


def test_continuous_units_target_rebalance_profile_but_stay_kill_switch_controlled() -> None:
    repo = Path(__file__).resolve().parents[1]
    # 2026-06-10: both units run the validated continuous_ensemble_v2 ensemble
    # (the profile owns triggers/age/TP per component; unit-level trigger is none).
    for unit_name in (
        "liquidity-migration-bybit-continuous-demo.service",
        "liquidity-migration-bybit-continuous-paper.service",
    ):
        text = (repo / "deploy" / "systemd" / unit_name).read_text(encoding="utf-8")
        assert "Environment=STRATEGY_PROFILE=continuous_ensemble_v2" in text
        assert "Environment=FEATURE_SET=max_ret168" in text
        assert "Environment=ENTRY_EVENT_TRIGGER=none" in text
        assert "Environment=BTC_TREND_GATE=uptrend" in text
        assert "Environment=LEFT_DECILE_EXIT_ENABLED=0" in text
        assert "Environment=STOP_APPROACH_FRAC=0" in text
        assert "Environment=FAILED_FADE_HOURS=0" in text
        assert "Environment=BREAKEVEN_ARM_PCT=0" in text
        assert "Environment=DAILY_REBALANCE_ENABLED=0" in text
        assert "Environment=DAILY_REBALANCE_TARGET_DAILY_VOL=0.045" in text
        assert "Environment=DAILY_REBALANCE_MAX_SCALE=4" in text
        assert "Environment=DAILY_REBALANCE_STRATEGY_MOMENTUM_WINDOW_DAYS=0" in text
        assert "Environment=SIZING_MODE=inverse_vol" in text
        assert "Environment=TARGET_VOL_PER_NAME=0.01" in text
        assert "Environment=VOL_WEIGHT_CLAMP=2" in text
        assert "Environment=ENTRY_PORTFOLIO_HEAT_CAP_FRAC=0" in text
        assert "Environment=ENTRY_PORTFOLIO_HEAT_SHOCK_FRAC=1" in text
        assert "Environment=ENTRY_ACCOUNT_DRAWDOWN_KILL_SWITCH_FRAC=0" in text
        assert "CTRL_BTC_RISK_70_90_35" in text
    demo_text = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-continuous-demo.service").read_text(encoding="utf-8")
    paper_text = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-continuous-paper.service").read_text(encoding="utf-8")
    # 2026-07-10 risk rollback: demo-only adverse adds are explicitly off on
    # both units until new two-venue + forward evidence earns reactivation.
    assert "Environment=CONTINUOUS_SNIPER=0" in demo_text
    assert "Environment=CONTINUOUS_SNIPER=0" in paper_text
    # 2f hedge submit armed (demo-only; runner enforces demo credentials + confirm flag)
    hedge_text = (repo / "deploy" / "systemd" / "liquidity-migration-continuous-hedge.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/liquidity-migration/sleeves.resolved.env" in hedge_text
    assert "Environment=HEDGE_MODE=2f" in hedge_text
    assert "Environment=SUBMIT_HEDGE=1" in hedge_text
    assert "Environment=CONFIRM_DEMO_ORDERS=1" in hedge_text


def test_continuous_live_overlay_defaults_are_pinned_for_demo_paper_parity() -> None:
    repo = Path(__file__).resolve().parents[1]
    required = (
        "ENTRY_PORTFOLIO_HEAT_CAP_FRAC=0",
        "ENTRY_PORTFOLIO_HEAT_SHOCK_FRAC=1",
        "ENTRY_ACCOUNT_DRAWDOWN_KILL_SWITCH_FRAC=0",
    )
    units = (
        "liquidity-migration-bybit-continuous-demo.service",
        "liquidity-migration-bybit-continuous-paper.service",
    )

    for script_name in (
        "deploy_vps_live.sh",
        "verify_vps_live.sh",
        "vps_console_recover_and_deploy.sh",
    ):
        text = (repo / "scripts" / script_name).read_text(encoding="utf-8")
        for unit in units:
            for assignment in required:
                assert f"require_unit_env {unit} '{assignment}'" in text


def test_long_units_pin_descriptive_v11a_profile() -> None:
    repo = Path(__file__).resolve().parents[1]
    demo_text = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-long-demo.service").read_text(encoding="utf-8")
    paper_text = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-long-paper.service").read_text(encoding="utf-8")

    assert "Environment=STRATEGY_PROFILE=LongV11aDivWeekendVol" in demo_text
    assert "Environment=SUBMIT_ORDERS=1" in demo_text
    assert "Environment=STRATEGY_PROFILE=LongV11aDivWeekendVol" in paper_text
    assert "Environment=SUBMIT_ORDERS=0" in paper_text
    assert "Environment=PAPER_MODE=1" in paper_text


def test_continuous_rmom_refresh_rebuilds_each_active_sleeve_root() -> None:
    """audit2 (deploy-env-timers-3 follow-up): since 7d39d61 the paper shadow streams
    its OWN kline pool (KLINES_FOLLOW_ROOT dropped from the paper unit), so it reads
    its rmom gate from its OWN root. The refresh must therefore rebuild EACH on
    sleeve's own root — refreshing only the demo root left the paper book reading a
    gate nothing builds, so it emitted zero entries forever and the paper<->demo cost
    reconcile had nothing to pair."""
    repo = Path(__file__).resolve().parents[1]
    service = (
        repo / "deploy" / "systemd" / "liquidity-migration-continuous-rmom-refresh.service"
    ).read_text(encoding="utf-8")
    script = (repo / "scripts" / "run_continuous_rmom_refresh.sh").read_text(encoding="utf-8")

    assert "run_continuous_rmom_refresh.sh" in service
    # Sleeve-aware: each root is rebuilt only when ITS sleeve is on.
    assert 'sleeve_on "${CONTINUOUS_SLEEVE' in script
    assert 'sleeve_on "${CONTINUOUS_PAPER_SLEEVE' in script
    assert "--root data/bybit-continuous-demo-event" in script
    assert "--root data/bybit-continuous-paper-event" in script  # the fix
    assert "--full-rewrite" in script  # live roots are rolling stores; append overlap can drift
    deploy = (repo / "scripts" / "deploy_vps_live.sh").read_text(encoding="utf-8")
    assert '_check_rmom_root "demo" "data/bybit-continuous-demo-event/residual_momentum.parquet"' in deploy
    assert '_check_rmom_root "paper" "data/bybit-continuous-paper-event/residual_momentum.parquet"' in deploy
    assert "run_continuous_rmom_refresh.sh never writes the paper root" not in deploy


def _active_lines(unit_text: str) -> list[str]:
    """Non-comment, non-blank directive lines of a systemd unit."""
    return [ln.strip() for ln in unit_text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_continuous_paper_unit_streams_its_own_kline_plane() -> None:
    """audit2: since 7d39d61 the paper unit no longer FOLLOWS the demo kline plane —
    KLINES_FOLLOW_ROOT was dropped so the shadow stays live even when the demo (leader)
    sleeve is off. Guard against a regression that re-adds an ACTIVE follow directive
    and against the optional drop-in mechanism being deleted. Both continuous units still
    pin their threadpools."""
    repo = Path(__file__).resolve().parents[1]
    paper = (
        repo / "deploy" / "systemd" / "liquidity-migration-bybit-continuous-paper.service"
    ).read_text(encoding="utf-8")
    demo = (
        repo / "deploy" / "systemd" / "liquidity-migration-bybit-continuous-demo.service"
    ).read_text(encoding="utf-8")
    run_script = (
        repo / "scripts" / "run_bybit_continuous_demo_event_engine.sh"
    ).read_text(encoding="utf-8")

    # No ACTIVE follow directive on the paper unit (comment-only mention is fine).
    assert not any(ln.startswith("Environment=KLINES_FOLLOW_ROOT=") for ln in _active_lines(paper))
    # the LEADER must never follow anyone
    assert not any(ln.startswith("Environment=KLINES_FOLLOW_ROOT=") for ln in _active_lines(demo))
    # the run script keeps the (now dormant) passthrough so a drop-in can re-enable it.
    assert "--klines-follow-root" in run_script
    for unit in (paper, demo):
        assert "Environment=POLARS_MAX_THREADS=1" in unit
        assert "Environment=OMP_NUM_THREADS=1" in unit
        assert "Environment=OPENBLAS_NUM_THREADS=1" in unit


def test_liveness_watchdog_checks_continuous_paper_evidence_root() -> None:
    repo = Path(__file__).resolve().parents[1]
    service = (repo / "deploy" / "systemd" / "liquidity-migration-demo-liveness.service").read_text(
        encoding="utf-8"
    )
    script = (repo / "scripts" / "check_demo_liveness.py").read_text(encoding="utf-8")

    assert "--continuous-paper-root /opt/liquidity-migration/data/bybit-continuous-paper-event" in service
    assert not any("--continuous-stop-check" in line for line in _active_lines(service))
    assert "liquidity-migration-bybit-continuous-paper.service" in script
    assert "_sleeve_on(\"CONTINUOUS_PAPER_SLEEVE\")" in script
    assert "continuous_stop_check" in script


def test_continuous_rmom_timer_wired_to_paper_evidence_gate() -> None:
    repo = Path(__file__).resolve().parents[1]
    deploy = (repo / "scripts" / "deploy_vps_live.sh").read_text(encoding="utf-8")
    verify = (repo / "scripts" / "verify_vps_live.sh").read_text(encoding="utf-8")
    recovery = (repo / "scripts" / "vps_console_recover_and_deploy.sh").read_text(encoding="utf-8")
    lib = (repo / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8")

    for text in (deploy, verify, recovery):
        assert "continuous_rmom_refresh_on" in text
        assert "apply_timer_enable" in text or "verify_timer" in text
        assert "CONTINUOUS_FORWARD_REPORT_TIMERS" not in text
    assert "CONTINUOUS_FORWARD_REPORT_TIMERS" not in lib
    assert 'CONTINUOUS_SLEEVE_TIMERS="liquidity-migration-continuous-rmom-refresh.timer"' in lib
    assert 'CONTINUOUS_HEDGE_TIMERS="liquidity-migration-continuous-hedge.timer"' in lib
    assert 'CONTINUOUS_HEDGE_SERVICES="liquidity-migration-continuous-hedge.service"' in lib


def test_combined_book_report_includes_continuous_roots_and_sleeve_toggles() -> None:
    repo = Path(__file__).resolve().parents[1]
    service = (repo / "deploy" / "systemd" / "liquidity-migration-combined-book-report.service").read_text(
        encoding="utf-8"
    )

    assert "EnvironmentFile=/etc/liquidity-migration/sleeves.resolved.env" in service
    assert "EnvironmentFile=-/etc/liquidity-migration/sleeves.env" not in service
    # Guard against the compatibility root drifting back into the active report.
    assert "--short-data-root" not in service
    assert "--long-data-root data/bybit-long-demo-event" in service
    assert "--continuous-data-root data/bybit-continuous-demo-event" in service
    assert "--continuous-paper-data-root data/bybit-continuous-paper-event" in service
    assert "--continuous-hedge-data-root data/bybit-continuous-hedge-event" in service


def test_long_units_lookback_days_satisfies_validation_floor() -> None:
    """ls-4: the deployed long demo/paper units MUST pass _validate_long_demo_config's
    lookback_days floor (>=95) — else every long cycle crash-fails (ValueError) and the sleeve
    silently stops trading. The env override broke this once on deploy (LOOKBACK_DAYS=90 < 95);
    pin the unit env to the code's requirement so the two can never drift apart again."""
    import re

    repo = Path(__file__).resolve().parents[1]
    for unit in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-long-paper.service",
    ):
        text = (repo / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        m = re.search(r"^Environment=LOOKBACK_DAYS=(\d+)", text, re.MULTILINE)
        assert m is not None, f"{unit}: no LOOKBACK_DAYS env"
        assert int(m.group(1)) >= 95, (
            f"{unit}: LOOKBACK_DAYS={m.group(1)} < 95 — _validate_long_demo_config would "
            "crash-fail every long cycle"
        )


def test_paper_services_enable_record_dry_run() -> None:
    """Paper services must set RECORD_DRY_RUN=1 so their dry-run cycles
    persist trades — otherwise paper-vs-demo reconciliation has no paper-side
    data to pair against the live demo ledger."""
    repo = Path(__file__).resolve().parents[1]
    for unit in (
        "liquidity-migration-bybit-long-paper.service",
    ):
        text = (repo / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        assert "Environment=SUBMIT_ORDERS=0" in text, f"{unit}: paper service must not submit orders"
        assert "Environment=RECORD_DRY_RUN=1" in text, f"{unit}: paper service must enable RECORD_DRY_RUN"


def test_long_paper_service_enables_paper_mode() -> None:
    """Long-paper writes need to land in long_native_paper_* datasets so the
    reconcile-long-paper-demo CLI can pair them against the live long-demo
    ledger. The long reconciler looks for paper_dataset='long_native_paper_trades'
    specifically; without PAPER_MODE=1 the long-paper service writes to
    long_native_demo_trades and reconciliation silently returns paired=0."""
    repo = Path(__file__).resolve().parents[1]
    text = (
        repo / "deploy" / "systemd" / "liquidity-migration-bybit-long-paper.service"
    ).read_text(encoding="utf-8")
    assert "Environment=PAPER_MODE=1" in text, (
        "long-paper service must set PAPER_MODE=1 so writes route to the "
        "long_native_paper_* dataset family the reconciler expects."
    )


def test_long_runner_wires_paper_mode() -> None:
    """The long-sleeve bash runner must surface --paper-mode via the
    PAPER_MODE env var so the long-paper service can route writes to the
    paper dataset family."""
    repo = Path(__file__).resolve().parents[1]
    text = (
        repo / "scripts" / "run_bybit_long_demo_event_engine.sh"
    ).read_text(encoding="utf-8")
    assert 'PAPER_MODE:-0}" == "1"' in text, "long runner missing PAPER_MODE gate"
    assert "--paper-mode" in text, "long runner does not pass --paper-mode"


def test_long_runner_and_units_default_to_safe_1x_sizing() -> None:
    repo = Path(__file__).resolve().parents[1]
    runner = (repo / "scripts" / "run_bybit_long_demo_event_engine.sh").read_text(encoding="utf-8")
    assert 'NOTIONAL_MULTIPLIER="${NOTIONAL_MULTIPLIER:-1}"' in runner
    assert 'MAX_PROJECTED_INITIAL_MARGIN_PCT_EQUITY="${MAX_PROJECTED_INITIAL_MARGIN_PCT_EQUITY:-0.5}"' in runner
    assert "--max-projected-initial-margin-pct-equity" in runner
    for unit in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-long-paper.service",
    ):
        text = (repo / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        assert "Environment=NOTIONAL_MULTIPLIER=1" in text, f"{unit} must not default to 10x"
        assert "Environment=MAX_PROJECTED_INITIAL_MARGIN_PCT_EQUITY=0.5" in text


def test_services_enable_ws_klines() -> None:
    """Long demo/paper services must enable the WS
    kline manager. WS_KLINES_ENABLED=1 flips the daemon onto the in-memory
    store, eliminating the per-cycle REST kline burst that caused 3-4h late
    entries on the legacy path."""
    repo = Path(__file__).resolve().parents[1]
    for unit in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-long-paper.service",
    ):
        text = (repo / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        assert "Environment=WS_KLINES_ENABLED=1" in text, f"{unit}: WS_KLINES_ENABLED not set"


def test_bash_runners_wire_ws_klines_env() -> None:
    """The long bash runner must expose the WS_KLINES_* env vars as CLI args.
    Without this, the systemd Environment lines are silently dropped and the
    daemon stays on the legacy REST path."""
    repo = Path(__file__).resolve().parents[1]
    for script_name in (
        "run_bybit_long_demo_event_engine.sh",
    ):
        text = (repo / "scripts" / script_name).read_text(encoding="utf-8")
        # Env vars are read with defaults.
        assert 'WS_KLINES_ENABLED="${WS_KLINES_ENABLED:-1}"' in text, f"{script_name}: missing WS_KLINES_ENABLED default"
        assert "WS_KLINES_BOOTSTRAP_WORKERS" in text, f"{script_name}: missing WS_KLINES_BOOTSTRAP_WORKERS"
        assert "WS_KLINES_LOOKBACK_DAYS" in text, f"{script_name}: missing WS_KLINES_LOOKBACK_DAYS"
        assert "WS_KLINES_UNIVERSE_REFRESH_SECONDS" in text, f"{script_name}: missing WS_KLINES_UNIVERSE_REFRESH_SECONDS"
        assert "WS_KLINES_TOPICS_PER_CONNECTION" in text, f"{script_name}: missing WS_KLINES_TOPICS_PER_CONNECTION"
        assert "WS_KLINES_STALE_WARNING_SECONDS" in text, f"{script_name}: missing WS_KLINES_STALE_WARNING_SECONDS"
        # And they're passed through the CLI.
        assert "--ws-klines-enabled" in text, f"{script_name}: missing --ws-klines-enabled"
        assert "--no-ws-klines" in text, f"{script_name}: missing --no-ws-klines kill-switch"
        assert "--ws-klines-bootstrap-workers" in text
        assert "--ws-klines-lookback-days" in text
        assert "--ws-klines-universe-refresh-seconds" in text
        assert "--ws-klines-topics-per-connection" in text
        assert "--ws-klines-stale-warning-seconds" in text
        assert "--ws-klines-stale-reconnect-seconds" in text


def test_live_runners_do_not_write_repo_bytecode() -> None:
    repo = Path(__file__).resolve().parents[1]
    paths = [
        repo / "scripts" / "run_bybit_demo_ws_risk_engine.sh",
        repo / "deploy" / "systemd" / "liquidity-migration-bybit-risk.service",
    ]

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "PYTHONDONTWRITEBYTECODE" in text


def test_vps_deploy_script_verifies_promoted_live_settings() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "deploy_vps_live.sh").read_text(encoding="utf-8")

    assert "EXPECTED_COMMIT" in text
    assert "BatchMode=yes" in text
    assert "git remote set-url" in text
    assert "GITHUB_TOKEN" in text
    assert "http.https://github.com/.extraheader" in text
    assert "x-access-token:%s" in text
    assert 'git checkout -B "$BRANCH" "$REMOTE/$BRANCH"' in text
    # The deploy gate pins the active LONG profile constants.
    assert "long_cfg.universe_size == 50" in text
    assert "long_cfg.weekend_size_mult == 1.5" in text
    assert "TELEGRAM_CHAT_ID" in text
    assert "bybit-demo.env.backup" in text
    assert "sed -i \"s/^TELEGRAM_CHAT_ID=" in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    lib = (repo / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8")
    assert "apply_timer_enable" in text
    assert "systemctl disable --now" in lib
    retired_unit_marker = "model" "050426"
    assert retired_unit_marker not in text
    # Deploy actively removes retired units.
    assert "RETIRED_SLEEVE_UNITS" in text
    assert "liquidity-migration-bybit-risk.service" in text
    # The risk service has NO sleeve toggle — it is the shared reconcile authority for
    # the whole demo account and must always enable/restart/verify regardless of which
    # entry sleeves are on (turning a sleeve off must never stop position protection).
    assert "systemctl enable liquidity-migration-bybit-risk.service" in text
    assert "systemctl restart liquidity-migration-bybit-risk.service" in text
    assert "systemctl is-active --quiet liquidity-migration-bybit-risk.service" in text
    assert "systemctl is-enabled --quiet liquidity-migration-bybit-risk.service" in text
    # Per-sleeve kill-switch: the deploy sources the shared lib, loads the toggles, then
    # enables/restarts/verifies each sleeve THROUGH the toggle-aware helpers (an off
    # sleeve gets `disable --now`d and is not expected up). The exact unit set each
    # sleeve owns is pinned in deploy/lib_sleeves.sh (asserted just below) so a deploy
    # still can't silently drop a unit — the names just live in one canonical place.
    assert "lib_sleeves.sh" in text
    assert "lm_load_sleeve_toggles" in text
    assert "lm_write_resolved_sleeve_toggles" in text
    assert "lm_verify_resolved_sleeve_toggles" in text
    assert "lm_cleanup_unknown_liqmig_units" in text
    for sleeve in ("LONG", "CONTINUOUS", "CONTINUOUS_PAPER"):
        assert f'apply_sleeve_enable "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
        assert f'verify_sleeve "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
    # The canonical unit set (what each sleeve enables/restarts/verifies, and what the
    # liveness watchdog/recovery must bring up) lives in the lib — pin it there.
    lib = (repo / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8")
    assert "liquidity-migration-bybit-demo.service" in lib
    assert "liquidity-migration-bybit-paper.service" in lib
    assert "liquidity-migration-continuous-forward-report.service" in lib
    assert "liquidity-migration-continuous-forward-report.timer" in lib
    assert 'LONG_SLEEVE_UNITS="liquidity-migration-bybit-long-demo.service liquidity-migration-bybit-long-paper.service"' in lib
    assert 'CONTINUOUS_SLEEVE_UNITS="liquidity-migration-bybit-continuous-demo.service"' in lib
    assert 'CONTINUOUS_PAPER_SLEEVE_UNITS="liquidity-migration-bybit-continuous-paper.service"' in lib
    assert 'CONTINUOUS_HEDGE_TIMERS="liquidity-migration-continuous-hedge.timer"' in lib
    assert 'CONTINUOUS_HEDGE_SERVICES="liquidity-migration-continuous-hedge.service"' in lib
    assert "apply_timer_enable()" in lib
    assert "verify_timer()" in lib
    assert "apply_hedge_timer_enable()" in lib
    assert "verify_hedge_timer_enable()" in lib
    assert "LM_HOST_SLEEVES_ENV" in lib
    assert "LM_RESOLVED_SLEEVES_ENV" in lib
    assert "lm_write_resolved_sleeve_toggles()" in lib
    assert "lm_verify_resolved_sleeve_toggles()" in lib
    assert "Host overrides may only turn repo-on sleeves off" in lib
    assert "timer is OFF in sleeves.env but still enabled" in lib
    assert "continuous_rmom_refresh_on()" in lib
    assert "is OFF in sleeves.env but still enabled" in lib
    sleeves = (repo / "deploy" / "sleeves.env").read_text(encoding="utf-8")
    # Continuous demo orders are ON; long is controlled by its own sleeve toggle.
    assert "CONTINUOUS_SLEEVE=on" in sleeves
    assert "CONTINUOUS_PAPER_SLEEVE=on" in sleeves
    # Timers ship with the unit files but `systemctl enable` is required to
    # actually schedule them. Pin both timers so a deploy can't silently leave
    # the demo-health watchdog or hourly combined-book report inactive.
    assert "systemctl enable --now liquidity-migration-demo-liveness.timer" in text
    assert "systemctl enable --now liquidity-migration-combined-book-report.timer" in text
    assert "systemctl is-enabled --quiet liquidity-migration-demo-liveness.timer" in text
    assert "systemctl is-enabled --quiet liquidity-migration-combined-book-report.timer" in text
    assert "systemctl is-active --quiet liquidity-migration-demo-liveness.timer" in text
    assert "systemctl is-active --quiet liquidity-migration-combined-book-report.timer" in text
    assert "telegram-quiet.conf" in text
    assert "/etc/systemd/system/liquidity-migration-bybit-continuous-demo.service.d" in text
    assert "liquidity-migration-combined-book-report.service.d" in text
    assert "require_unit_env()" in text
    assert "systemctl cat" not in text
    assert "require_unit_env liquidity-migration-bybit-risk.service 'ORDER_SUBMIT_MODE=ws_then_rest'" in text
    # Continuous-fade sleeve (live on demo 2026-06-01): brought up like the other
    # live daemons, plus its rmom timer; risk service wired to read its ledger.
    assert "liquidity-migration-bybit-continuous-demo.service" in text
    assert "liquidity-migration-bybit-continuous-paper.service" in text
    assert 'CONTINUOUS_SLEEVE_TIMERS="liquidity-migration-continuous-rmom-refresh.timer"' in lib
    # deploy-env-timers-1: the hedge timer is gated on a computed _hedge_timer_state
    # (not raw CONTINUOUS_SLEEVE) so retiring continuous does NOT orphan an open,
    # stopless hedge leg — when continuous is off it keeps the timer enabled (and
    # pages CRITICAL) while the hedge addon ledger holds open rows, disabling only
    # once flat. apply and verify must use the SAME computed state.
    assert 'CONTINUOUS_HEDGE_TIMER="$_hedge_timer_state"' in text
    assert 'apply_hedge_timer_enable "$_hedge_timer_state"' in text
    assert 'verify_hedge_timer_enable "$_hedge_timer_state"' in text
    assert "data/bybit-continuous-hedge-event" in text  # the open-hedge ledger check
    assert "_hedge_timer_state=on" in text              # fail-safe keeps it enabled while open
    assert "continuous_rmom_refresh_on" in text
    assert "require_unit_env liquidity-migration-bybit-risk.service 'LONG_DATA_ROOT=data/bybit-long-demo-event'" in text
    assert "require_unit_env liquidity-migration-bybit-risk.service 'CONTINUOUS_DATA_ROOT=data/bybit-continuous-demo-event'" in text
    assert "require_unit_env liquidity-migration-bybit-risk.service 'CONTINUOUS_ADDON_DATA_ROOT=data/bybit-continuous-hedge-event'" in text
    assert "require_unit_env liquidity-migration-bybit-long-demo.service 'SUBMIT_ORDERS=1'" in text
    assert "require_unit_env liquidity-migration-bybit-long-demo.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'" in text
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'SUBMIT_ORDERS=0'" in text
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'PAPER_MODE=1'" in text
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'SUBMIT_ORDERS=1'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'FEATURE_SET=max_ret168'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_EVENT_TRIGGER=none'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'BTC_TREND_GATE=uptrend'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'MAX_HOLD_HOURS=24'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'SIZING_MODE=inverse_vol'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'TARGET_VOL_PER_NAME=0.01'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'VOL_WEIGHT_CLAMP=2'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_PORTFOLIO_HEAT_CAP_FRAC=0'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_PORTFOLIO_HEAT_SHOCK_FRAC=1'" in text
    assert (
        "require_unit_env liquidity-migration-bybit-continuous-demo.service "
        "'ENTRY_ACCOUNT_DRAWDOWN_KILL_SWITCH_FRAC=0'"
    ) in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_REALIZED_VOL_WINDOW_DAYS=90'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_TARGET_DAILY_VOL=0.045'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_MAX_SCALE=4'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_ENABLED=0'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_DRAWDOWN_HALF_THRESHOLD=-0.04'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_RESIZE_COST_BPS=10'" in text
    assert 'cont.sizing_mode == "inverse_vol"' in text
    assert "cont.target_vol_per_name == 0.01" in text
    assert "cont.daily_rebalance_enabled is False" in text
    assert "cont.daily_rebalance_target_daily_vol == 0.045" in text
    assert "cont.entry_btc_risk_low == 0.70" in text
    assert "cont.entry_btc_risk_high == 0.90" in text
    assert "cont.entry_btc_risk_tail_mult == 0.35" in text
    assert "c[0]: c[3] for c in cont.ensemble_components" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'STOP_LOSS_PCT=0'" in text
    # Deploy must seed missing/stale gates (or rebuild after relevant code changes),
    # but must not spend minutes rebuilding identical healthy state on every deploy.
    # Any required seed still runs before the continuous daemons restart.
    assert "systemctl start liquidity-migration-continuous-rmom-refresh.service" in text
    assert 'git diff --quiet "$previous_commit" HEAD --' in text
    assert 'if [ "$_rmom_needs_seed" -eq 1 ]; then' in text
    assert "skipping seed" in text
    for rmom_dependency in (
        "pyproject.toml",
        "requirements.lock",
        "deploy/systemd/liquidity-migration-continuous-rmom-refresh.service",
        "scripts/run_continuous_rmom_refresh.sh",
        "scripts/precompute_residual_momentum.py",
        "scripts/check_residual_momentum_gate.py",
        "liquidity_migration/_common.py",
        "liquidity_migration/risk_model.py",
        "liquidity_migration/daily_feature_panel.py",
        "liquidity_migration/storage.py",
    ):
        assert rmom_dependency in text
    first_continuous_restart = min(
        text.index("systemctl restart liquidity-migration-bybit-continuous-demo.service"),
        text.index("systemctl restart liquidity-migration-bybit-continuous-paper.service"),
    )
    assert text.index("systemctl start liquidity-migration-continuous-rmom-refresh.service") < first_continuous_restart
    assert "rmom gate is EMPTY, provisional-only, or stale after deploy gate check" in text
    assert "scripts/check_residual_momentum_gate.py" in text
    assert "ALLOW_EMPTY_RMOM_GATE" in text
    # Reboot-safety invariant (audit 2026-06-02 #51): the risk service (the single
    # reconcile authority that tracks the continuous sleeve's positions) must come
    # up BEFORE the continuous daemon, else the continuous sleeve's live positions
    # look untracked and get flattened. Pin both the enable and restart order.
    assert (
        text.index("systemctl enable liquidity-migration-bybit-risk.service")
        < text.index('apply_sleeve_enable "$CONTINUOUS_SLEEVE"')
    )
    assert (
        text.index("systemctl restart liquidity-migration-bybit-risk.service")
        < text.index("systemctl restart liquidity-migration-bybit-continuous-demo.service")
    )
    assert "deploy-verify-ok commit=" in text
    assert "--property=Environment --value" in text
    # Daemons no longer fire startup telegrams (default off as of the
    # rapid-deploy-spam fix), so the deploy script owns the single
    # "deploy succeeded" signal — one telegram per deploy regardless of
    # how many daemons restarted.
    assert "api.telegram.org/bot" in text
    assert "deploy-verify-ok commit=$python_commit" in text
    assert "TELEGRAM_BOT_TOKEN" in text
    # Best-effort: a curl failure must not flip the deploy result. The
    # `|| echo WARN` clause keeps the script from exit-1-ing if Telegram is
    # down; verify already passed before this line runs.
    assert "deploy-confirm telegram send failed" in text


def test_vps_deploy_script_pytest_nodeids_still_collect() -> None:
    """The deploy + disaster-recovery scripts run pinned pytest subsets as
    pre-restart smoke tests. Because they `set -euo pipefail`, a stale node-id
    (e.g. a test moved by a test-file split, or deleted in a purge) makes pytest
    exit non-zero and aborts the deploy/recovery. String-presence tests can't
    catch a moved path, so verify every `tests/...` node-id BOTH scripts
    reference actually collects."""
    import re
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    for script in ("deploy_vps_live.sh", "vps_console_recover_and_deploy.sh"):
        text = (repo / "scripts" / script).read_text(encoding="utf-8")
        nodeids = re.findall(r"tests/[^\s\\]+\.py(?:::\w+)?", text)
        assert nodeids, f"expected {script} to pin a pytest smoke subset"
        for nodeid in nodeids:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "--collect-only", "-q", nodeid],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert proc.returncode == 0 and "no tests ran" not in proc.stdout.lower(), (
                f"{script} smoke-test node-id no longer collects: {nodeid}\n"
                f"{proc.stdout}\n{proc.stderr}"
            )


def test_vps_verify_script_is_read_only_and_checks_live_state() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "verify_vps_live.sh").read_text(encoding="utf-8")

    assert "git pull" not in text
    assert "systemctl restart" not in text
    retired_unit_marker = "model" "050426"
    assert retired_unit_marker not in text
    # Verify must pin active configs and must not import removed strategy hubs.
    assert "liquidity_migration.volume_events" not in text
    assert "_demo_event_config" not in text
    assert "_v11a_long_native_config" in text
    assert "ContinuousDemoCycleConfig" in text
    assert "TELEGRAM_CHAT_ID" in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    assert "require_unit_env()" in text
    assert "systemctl cat" not in text
    assert "require_unit_env liquidity-migration-bybit-risk.service 'ORDER_SUBMIT_MODE=ws_then_rest'" in text
    # Per-sleeve kill-switch: verify is toggle-aware — it sources the shared lib, loads
    # the toggles, and routes per-sleeve active+enabled checks through verify_sleeve (so
    # an intentionally-off sleeve is required DOWN, not flagged as a failed deploy). The
    # risk service is NOT toggled — always verified up (it protects every sleeve).
    assert "lib_sleeves.sh" in text
    assert "lm_load_sleeve_toggles" in text
    assert "lm_verify_resolved_sleeve_toggles" in text
    assert "lm_verify_no_unknown_liqmig_units" in text
    assert "systemctl is-enabled --quiet liquidity-migration-bybit-risk.service" in text
    assert "systemctl is-active --quiet liquidity-migration-bybit-risk.service" in text
    assert "systemctl is-enabled --quiet liquidity-migration-liquidation-collector.service" in text
    assert "systemctl is-active --quiet liquidity-migration-liquidation-collector.service" in text
    for sleeve in ("LONG", "CONTINUOUS", "CONTINUOUS_PAPER"):
        assert f'verify_sleeve "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
    # The exact unit set each sleeve must bring up is pinned in the shared lib, so a
    # regression that stops/disables a sleeve's daemon still fails verify.
    lib = (repo / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8")
    assert 'LONG_SLEEVE_UNITS="liquidity-migration-bybit-long-demo.service liquidity-migration-bybit-long-paper.service"' in lib
    assert 'CONTINUOUS_SLEEVE_UNITS="liquidity-migration-bybit-continuous-demo.service"' in lib
    assert 'CONTINUOUS_PAPER_SLEEVE_UNITS="liquidity-migration-bybit-continuous-paper.service"' in lib
    assert 'CONTINUOUS_HEDGE_TIMERS="liquidity-migration-continuous-hedge.timer"' in lib
    assert 'CONTINUOUS_HEDGE_SERVICES="liquidity-migration-continuous-hedge.service"' in lib
    # Read-only verify must catch a missing-timer regression that the deploy
    # script would have caused — parity check, no-write semantics.
    assert "systemctl is-enabled --quiet liquidity-migration-demo-liveness.timer" in text
    assert "systemctl is-enabled --quiet liquidity-migration-combined-book-report.timer" in text
    assert "systemctl is-active --quiet liquidity-migration-demo-liveness.timer" in text
    assert "systemctl is-active --quiet liquidity-migration-combined-book-report.timer" in text
    assert "emergency Telegram mute still installed" in text
    assert "/etc/systemd/system/liquidity-migration-bybit-continuous-demo.service.d" in text
    assert "liquidity-migration-combined-book-report.service.d" in text
    # Continuous-fade sleeve (live on demo 2026-06-01): its daily rmom-refresh timer is
    # verified only when the sleeve is on (guarded by sleeve_on); the daemon's own
    # active+enabled state is covered by the verify_sleeve loop above. The risk service
    # stays wired to read the continuous ledger even when the sleeve is off (asserted
    # below, unconditional) — else its open positions would silently flatten.
    assert "continuous_rmom_refresh_on" in text
    assert "verify_timer on $CONTINUOUS_SLEEVE_TIMERS" in text
    assert "_verify_rmom_root" in text
    assert "ALLOW_EMPTY_RMOM_GATE" in text
    assert "CONTINUOUS_FORWARD_REPORT_TIMERS" not in text
    assert "_hedge_timer_state" in text
    assert 'CONTINUOUS_HEDGE_TIMER="$_hedge_timer_state"' in text
    assert 'verify_hedge_timer_enable "$_hedge_timer_state"' in text
    assert 'verify_timer "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS' not in text
    assert "require_unit_env liquidity-migration-bybit-risk.service 'LONG_DATA_ROOT=data/bybit-long-demo-event'" in text
    assert "require_unit_env liquidity-migration-bybit-risk.service 'CONTINUOUS_DATA_ROOT=data/bybit-continuous-demo-event'" in text
    assert "require_unit_env liquidity-migration-bybit-risk.service 'CONTINUOUS_ADDON_DATA_ROOT=data/bybit-continuous-hedge-event'" in text
    assert "require_unit_env liquidity-migration-bybit-long-demo.service 'SUBMIT_ORDERS=1'" in text
    assert "require_unit_env liquidity-migration-bybit-long-demo.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'" in text
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'SUBMIT_ORDERS=0'" in text
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'PAPER_MODE=1'" in text
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'SUBMIT_ORDERS=1'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'FEATURE_SET=max_ret168'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_EVENT_TRIGGER=none'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'BTC_TREND_GATE=uptrend'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'MAX_HOLD_HOURS=24'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'SIZING_MODE=inverse_vol'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'TARGET_VOL_PER_NAME=0.01'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'VOL_WEIGHT_CLAMP=2'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_PORTFOLIO_HEAT_CAP_FRAC=0'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_PORTFOLIO_HEAT_SHOCK_FRAC=1'" in text
    assert (
        "require_unit_env liquidity-migration-bybit-continuous-demo.service "
        "'ENTRY_ACCOUNT_DRAWDOWN_KILL_SWITCH_FRAC=0'"
    ) in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_REALIZED_VOL_WINDOW_DAYS=90'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_TARGET_DAILY_VOL=0.045'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_MAX_SCALE=4'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_ENABLED=0'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_DRAWDOWN_HALF_THRESHOLD=-0.04'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_RESIZE_COST_BPS=10'" in text
    assert 'cont.sizing_mode == "inverse_vol"' in text
    assert "cont.target_vol_per_name == 0.01" in text
    assert "cont.daily_rebalance_enabled is False" in text
    assert "cont.daily_rebalance_target_daily_vol == 0.045" in text
    assert "cont.entry_btc_risk_low == 0.70" in text
    assert "cont.entry_btc_risk_high == 0.90" in text
    assert "cont.entry_btc_risk_tail_mult == 0.35" in text
    assert "c[0]: c[3] for c in cont.ensemble_components" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'STOP_LOSS_PCT=0'" in text
    assert "verify-ok commit=" in text
    assert "--property=Environment --value" in text


def test_github_vps_deploy_workflow_uses_checked_scripts_and_host_key() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / ".github" / "workflows" / "vps-deploy.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch" in text
    assert "push:" in text
    assert "branches:" in text
    assert '"deploy/systemd/*.service"' in text
    assert '"deploy/systemd/**"' not in text
    assert '"scripts/**"' not in text
    # Per-sleeve kill-switch files must be in the push path filter, else flipping a
    # toggle in deploy/sleeves.env wouldn't trigger a redeploy and the sleeve would
    # never actually stop/start (the deploy sources both at runtime).
    assert '"deploy/sleeves.env"' in text
    assert '"deploy/lib_sleeves.sh"' in text
    assert "wait-deploy" in text
    assert "wait_timeout_seconds" in text
    assert "wait_interval_seconds" in text
    assert "github.event_name == 'push' || inputs.mode == 'deploy'" in text
    assert (
        "github.event_name == 'workflow_dispatch' && inputs.mode == 'wait-deploy'"
        in text
    )
    assert "github.event_name == 'workflow_dispatch' && inputs.mode == 'verify'" in text
    assert "VPS_SSH_PRIVATE_KEY" in text
    assert "permissions:" in text
    assert "contents: read" in text
    assert "GITHUB_TOKEN: ${{ github.token }}" in text
    assert "GITHUB_ACTIONS_DEPLOY_KEY_FINGERPRINT" in text
    # Pin the CI deploy key fingerprint so accidental rotations or tampering
    # of the workflow file get flagged. When you intentionally rotate the
    # deploy key, update this constant in lockstep with the
    # GITHUB_ACTIONS_DEPLOY_KEY_FINGERPRINT line in
    # .github/workflows/vps-deploy.yml AND the public key in
    # /root/.ssh/authorized_keys on the VPS AND the VPS_SSH_PRIVATE_KEY
    # secret in GitHub.
    # 2026-06-04: rotated for the 116.202.15.128 migration (old SHA256:KpDkvlvm…).
    assert "SHA256:Gki6YjdsUksh/TozZ/55sxSwimK7T9MOf2pgWSbqFNU" in text
    assert "ssh-keygen -y -f ~/.ssh/vps_deploy_key" in text
    assert "ssh-keygen -lf ~/.ssh/vps_deploy_key.pub -E sha256" in text
    # Host key is PINNED directly (no live keyscan — GitHub runners can't reliably
    # keyscan this box); the pinned key is fail-closed against the fingerprint.
    assert 'grep -F "$VPS_ED25519_FINGERPRINT"' in text
    # VPS host key fingerprint — update in lockstep with the rebuild/migration.
    # 2026-05-25 rebuild: SHA256:zQjT3bst... → SHA256:RzhZupfx...
    # 2026-06-04 migrate to new box 116.202.15.128 (old 5.223.42.109 decommissioned for cost):
    #   SHA256:RzhZupfx... → SHA256:2Jw88AJV...
    # 2026-06-09 operator full rebuild of 116.202.15.128 (fresh Ubuntu 24.04; rescue-mode key
    #   restore performed from the research box): SHA256:2Jw88AJV... → SHA256:TJRbvgB8...
    assert "SHA256:TJRbvgB8nfhwmNDv4hM3jDkPXnRv6BGLQ3cPst2PfE4" in text
    assert "scripts/deploy_vps_live.sh" in text
    assert "scripts/verify_vps_live.sh" in text
    assert "scripts/wait_for_vps_recovery_and_deploy.sh" in text
    assert "scripts/vps_restore_ssh_access.sh" in text
    assert "scripts/vps_rescue_restore_ssh_access.sh" in text
    assert "scripts/vps_console_recover_and_deploy.sh" in text
    # The deploy script runs this smoke subset on the VPS. If a test-only fix is
    # needed to unblock deploy, the push must trigger the workflow again.
    assert "tests/test_runtime_scripts.py" in text
    assert "tests/test_promoted_profiles.py" in text
    assert "EXPECTED_COMMIT=\"$GITHUB_SHA\"" in text
    assert "EXPECTED_TELEGRAM_CHAT_ID" in text


def test_vps_recovery_command_printer_uses_pinned_commit_url() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "print_vps_recovery_command.sh").read_text(
        encoding="utf-8"
    )

    assert "git rev-parse" in text
    assert "--recommended-only" in text
    assert "--rescue-only" in text
    assert "recommended_only" in text
    assert "rescue_only" in text
    assert "recommended_command=" in text
    assert "rescue_command=" in text
    assert "raw.githubusercontent.com/rob435/liquidity-migration" in text
    assert "scripts/vps_restore_ssh_access.sh" in text
    assert "scripts/vps_rescue_restore_ssh_access.sh" in text
    assert "scripts/vps_console_recover_and_deploy.sh" in text
    assert "scripts/deploy_vps_live.sh" in text
    assert "scripts/wait_for_vps_recovery_and_deploy.sh" in text
    assert "Wait locally for restored SSH access" in text
    assert "Hetzner Rescue SSH-key restore" in text
    assert "Recommended full Hetzner Cloud console recovery" in text
    assert "Open the Hetzner Cloud web console for 116.202.15.128" in text
    assert "enable" in text
    assert "Hetzner Rescue" in text
    assert "Strict full recovery" in text
    assert "CLEAN_DIRTY_CHECKOUT=1" in text
    assert 'EXPECTED_COMMIT="$commit_sha" CLEAN_DIRTY_CHECKOUT=1 bash' in text
    assert 'curl -fsSL $rescue_script_url | bash' in text
    assert 'EXPECTED_COMMIT="$commit_sha" bash' in text
    assert "scripts/verify_vps_live.sh" in text


def test_wait_for_vps_recovery_script_waits_then_runs_checked_deploy_and_verify() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "wait_for_vps_recovery_and_deploy.sh").read_text(
        encoding="utf-8"
    )

    assert "WAIT_TIMEOUT_SECONDS" in text
    assert "WAIT_INTERVAL_SECONDS" in text
    assert "BatchMode=yes" in text
    assert "ssh-ready" in text
    assert "ssh-not-ready" in text
    assert "accept SSH public-key auth" in text
    assert "scripts/print_vps_recovery_command.sh --rescue-only" in text
    assert "scripts/deploy_vps_live.sh" in text
    assert "scripts/verify_vps_live.sh" in text
    assert "EXPECTED_COMMIT" in text
    assert "EXPECTED_TELEGRAM_CHAT_ID" in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    assert "wait-deploy-verify-ok" in text
    assert "systemctl restart" not in text


def test_vps_ssh_restore_script_only_restores_access() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "vps_restore_ssh_access.sh").read_text(
        encoding="utf-8"
    )

    assert "/root/.ssh/authorized_keys" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAICWcgpE3GLy65yWFuh5RAH5CEgyLqRPAGvROXGwAxmVv" in text
    assert "PermitRootLogin prohibit-password" in text
    assert "AuthenticationMethods publickey" in text
    assert "Include /etc/ssh/sshd_config.d/*.conf" in text
    assert "sshd_config.liquidity-migration-backup" in text
    assert "Restored authorized key fingerprints:" in text
    assert 'ssh-keygen -lf "$tmp_public_key" -E sha256' in text
    assert "effective_sshd_config" in text
    assert "grep -Eq '^authenticationmethods publickey$'" in text
    assert "mkdir -p /run/sshd" in text
    assert 'sshd_root_context="user=root,host=localhost,addr=127.0.0.1"' in text
    assert 'sshd -T -C "$sshd_root_context"' in text
    assert "systemctl restart ssh.service" in text
    assert "ssh-restore-ok" in text
    assert "liquidity-migration-bybit-demo.service" not in text
    assert "pip install" not in text


def test_vps_rescue_restore_script_mounts_installed_root_and_restores_keys() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "vps_rescue_restore_ssh_access.sh").read_text(
        encoding="utf-8"
    )

    assert "TARGET_ROOT" in text
    assert "MOUNT_ROOT" in text
    assert "is_installed_root" in text
    assert "lsblk -rpno NAME,FSTYPE,TYPE,MOUNTPOINT" in text
    assert "vgchange -ay" in text
    assert 'mount "$device" "$MOUNT_ROOT"' in text
    assert "/root/.ssh/authorized_keys" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAICWcgpE3GLy65yWFuh5RAH5CEgyLqRPAGvROXGwAxmVv" in text
    assert "chroot \"$target_root\" usermod -U root" in text
    assert "99-liquidity-migration-recovery.conf" in text
    assert "PermitRootLogin prohibit-password" in text
    assert "AuthenticationMethods publickey" in text
    assert "Include /etc/ssh/sshd_config.d/*.conf" in text
    assert "sshd_config.liquidity-migration-backup" in text
    assert "Restored authorized key fingerprints" in text
    assert "rescue-ssh-restore-ok" in text
    assert "Reboot the VPS from local disk" in text
    assert "liquidity-migration-bybit-demo.service" not in text
    assert "pip install" not in text


def test_vps_console_recovery_script_restores_key_and_deploys() -> None:
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts" / "vps_console_recover_and_deploy.sh").read_text(
        encoding="utf-8"
    )

    assert "/root/.ssh/authorized_keys" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIFwJNtc1cVhkzNKmxmq6mogten+Q/5yfLulf9wxZxMNp" in text
    assert "AAAAC3NzaC1lZDI1NTE5AAAAICWcgpE3GLy65yWFuh5RAH5CEgyLqRPAGvROXGwAxmVv" in text
    assert "GITHUB_ACTIONS_SSH_PUBLIC_KEY" in text
    assert "for binary in git python3 sshd" in text
    assert "apt-get install -y ca-certificates git openssh-server python3 python3-venv python3-pip" in text
    assert "CLEAN_DIRTY_CHECKOUT" in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    assert "bybit-demo.env.backup" in text
    assert "sed -i \"s/^TELEGRAM_CHAT_ID=" in text
    assert "99-liquidity-migration-recovery.conf" in text
    assert "chmod 700 /root" in text
    assert "usermod -U root" in text
    assert "PermitRootLogin prohibit-password" in text
    assert "PubkeyAuthentication yes" in text
    assert "AuthenticationMethods publickey" in text
    assert "Include /etc/ssh/sshd_config.d/*.conf" in text
    assert "sshd_config.liquidity-migration-backup" in text
    assert "Restored authorized key fingerprints:" in text
    assert 'ssh-keygen -lf "$tmp_public_key" -E sha256' in text
    assert "effective_sshd_config" in text
    assert "grep -Eq '^authenticationmethods publickey$'" in text
    assert "mkdir -p /run/sshd" in text
    assert 'sshd_root_context="user=root,host=localhost,addr=127.0.0.1"' in text
    assert 'sshd -T -C "$sshd_root_context"' in text
    assert "systemctl restart ssh.service" in text
    assert "liquidity-migration-deploy-backups" in text
    assert "non-git-checkout-" in text
    assert 'mv "$REPO_DIR" "$backup_path"' in text
    assert "git reset --hard" in text
    assert "git clean -fd" in text
    assert "git ls-files --others --exclude-standard -z" in text
    assert 'tar --null -czf "$untracked_archive" --files-from "$untracked_nul"' in text
    assert "git_with_optional_github_token clone" in text
    assert "git remote set-url" in text
    assert "GITHUB_TOKEN" in text
    assert "http.https://github.com/.extraheader" in text
    assert 'git checkout -B "$BRANCH" "$REMOTE/$BRANCH"' in text
    assert "pip install -e \".[dev]\"" in text
    # Recovery pins active configs and does not import removed strategy hubs.
    assert "liquidity_migration.volume_events" not in text
    assert "_demo_event_config" not in text
    assert "_v11a_long_native_config" in text
    assert "ContinuousDemoCycleConfig" in text
    lib = (repo / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8")
    assert "apply_timer_enable" in text
    assert "systemctl disable --now" in lib
    retired_unit_marker = "model" "050426"
    assert retired_unit_marker not in text
    # Deploy actively removes retired units.
    assert "RETIRED_SLEEVE_UNITS" in text
    assert "liquidity-migration-bybit-risk.service" in text
    # Recovery routes sleeve enable/restart/verify through the SAME kill-switch as
    # deploy_vps_live.sh (single source of truth) — NO hardcoded per-sleeve enables that
    # could resurrect an OFF sleeve (e.g. the look-ahead-disabled continuous sleeve).
    assert "lib_sleeves.sh" in text
    assert "lm_load_sleeve_toggles" in text
    assert "lm_write_resolved_sleeve_toggles" in text
    assert "lm_verify_resolved_sleeve_toggles" in text
    assert "lm_cleanup_unknown_liqmig_units" in text
    assert "systemctl enable liquidity-migration-bybit-risk.service" in text
    assert "systemctl restart liquidity-migration-bybit-risk.service" in text
    assert "systemctl is-enabled --quiet liquidity-migration-bybit-risk.service" in text
    assert "liquidity-migration-liquidation-collector.service" in text
    for sleeve in ("LONG", "CONTINUOUS", "CONTINUOUS_PAPER"):
        assert f'apply_sleeve_enable "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
        assert f'verify_sleeve "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
    # The continuous rmom timer + its go-live asserts are gated behind the toggle, so a
    # recovery with CONTINUOUS_SLEEVE=off cannot bring the disabled sleeve back.
    assert "continuous_rmom_refresh_on" in text
    assert "require_unit_env()" in text
    assert "systemctl cat" not in text
    assert "telegram-quiet.conf" in text
    assert "/etc/systemd/system/liquidity-migration-bybit-continuous-demo.service.d" in text
    assert "liquidity-migration-combined-book-report.service.d" in text
    assert "require_unit_env liquidity-migration-bybit-risk.service 'ORDER_SUBMIT_MODE=ws_then_rest'" in text
    # Continuous-fade sleeve (live on demo 2026-06-01): brought up like the other
    # live daemons, plus its rmom timer; risk service wired to read its ledger.
    assert "liquidity-migration-bybit-continuous-demo.service" in text
    assert 'CONTINUOUS_SLEEVE_TIMERS="liquidity-migration-continuous-rmom-refresh.timer"' in lib
    assert "_hedge_timer_state" in text
    assert 'CONTINUOUS_HEDGE_TIMER="$_hedge_timer_state"' in text
    assert 'apply_hedge_timer_enable "$_hedge_timer_state"' in text
    assert 'verify_hedge_timer_enable "$_hedge_timer_state"' in text
    assert 'apply_timer_enable "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS' not in text
    assert 'verify_timer "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS' not in text
    assert "require_unit_env liquidity-migration-bybit-risk.service 'LONG_DATA_ROOT=data/bybit-long-demo-event'" in text
    assert "require_unit_env liquidity-migration-bybit-risk.service 'CONTINUOUS_DATA_ROOT=data/bybit-continuous-demo-event'" in text
    assert "require_unit_env liquidity-migration-bybit-risk.service 'CONTINUOUS_ADDON_DATA_ROOT=data/bybit-continuous-hedge-event'" in text
    assert "require_unit_env liquidity-migration-bybit-long-demo.service 'SUBMIT_ORDERS=1'" in text
    assert "require_unit_env liquidity-migration-bybit-long-demo.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'" in text
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'SUBMIT_ORDERS=0'" in text
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'PAPER_MODE=1'" in text
    assert "require_unit_env liquidity-migration-bybit-long-paper.service 'STRATEGY_PROFILE=LongV11aDivWeekendVol'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'SUBMIT_ORDERS=1'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'FEATURE_SET=max_ret168'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_EVENT_TRIGGER=none'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'BTC_TREND_GATE=uptrend'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'MAX_HOLD_HOURS=24'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'SIZING_MODE=inverse_vol'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'TARGET_VOL_PER_NAME=0.01'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'VOL_WEIGHT_CLAMP=2'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_PORTFOLIO_HEAT_CAP_FRAC=0'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'ENTRY_PORTFOLIO_HEAT_SHOCK_FRAC=1'" in text
    assert (
        "require_unit_env liquidity-migration-bybit-continuous-demo.service "
        "'ENTRY_ACCOUNT_DRAWDOWN_KILL_SWITCH_FRAC=0'"
    ) in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_REALIZED_VOL_WINDOW_DAYS=90'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_TARGET_DAILY_VOL=0.045'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_MAX_SCALE=4'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_ENABLED=0'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_DRAWDOWN_HALF_THRESHOLD=-0.04'" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'DAILY_REBALANCE_RESIZE_COST_BPS=10'" in text
    assert 'cont.sizing_mode == "inverse_vol"' in text
    assert "cont.target_vol_per_name == 0.01" in text
    assert "cont.daily_rebalance_enabled is False" in text
    assert "cont.daily_rebalance_target_daily_vol == 0.045" in text
    assert "cont.entry_btc_risk_low == 0.70" in text
    assert "cont.entry_btc_risk_high == 0.90" in text
    assert "cont.entry_btc_risk_tail_mult == 0.35" in text
    assert "c[0]: c[3] for c in cont.ensemble_components" in text
    assert "require_unit_env liquidity-migration-bybit-continuous-demo.service 'STOP_LOSS_PCT=0'" in text
    assert "deploy-verify-ok commit=" in text
    assert "--property=Environment --value" in text


_LEDGER_RESET_ACTIVE_UNITS = (
    "liquidity-migration-bybit-risk.service",
    "liquidity-migration-bybit-long-demo.service",
    "liquidity-migration-bybit-long-paper.service",
    "liquidity-migration-bybit-continuous-demo.service",
    "liquidity-migration-bybit-continuous-paper.service",
    "liquidity-migration-continuous-rmom-refresh.timer",
    "liquidity-migration-continuous-hedge.timer",
    "liquidity-migration-combined-book-report.timer",
    "liquidity-migration-demo-liveness.timer",
)


def _ledger_reset_harness(
    tmp_path: Path,
    *,
    real_money: str = "false",
    account_guard_rc: int = 0,
    active_units: tuple[str, ...] = _LEDGER_RESET_ACTIVE_UNITS,
) -> tuple[Path, Path, dict[str, str], Path]:
    """Create deterministic systemctl/account guards around the VPS-only script."""
    (tmp_path / "liquidity_migration").mkdir(exist_ok=True)
    (tmp_path / "data").mkdir(exist_ok=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    state = tmp_path / "systemctl.state"
    state.write_text("".join(f"{unit}\n" for unit in active_units), encoding="utf-8")
    log = tmp_path / "systemctl.log"

    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
cmd="$1"
shift
printf '%s %s\\n' "$cmd" "$*" >> "$SYSTEMCTL_LOG"
case "$cmd" in
  show)
    unit="$1"
    if [[ "$*" == *"--property=EnvironmentFiles"* ]]; then
      env_file="$FAKE_SYSTEMD_ENV_FILE"
      if [[ "${FAKE_SYSTEMD_ENV_MISMATCH_UNIT:-}" == "$unit" ]]; then
        env_file="$FAKE_SYSTEMD_ENV_MISMATCH_FILE"
      fi
      printf '%s (ignore_errors=no)\n' "$env_file"
      if [[ "${FAKE_SYSTEMD_EXTRA_ENV_UNIT:-}" == "$unit" ]]; then
        printf '%s (ignore_errors=no)\n' "$FAKE_SYSTEMD_EXTRA_ENV_FILE"
      fi
    elif [[ "$*" == *"--property=Environment"* ]]; then
      printf '%s\n' "${FAKE_SYSTEMD_DIRECT_ENVIRONMENT:-}"
    else
      echo loaded
    fi
    ;;
  is-active)
    [[ "${1:-}" == "--quiet" ]] && shift
    grep -Fqx "$1" "$SYSTEMCTL_STATE"
    ;;
  stop)
    for unit in "$@"; do
      grep -Fxv "$unit" "$SYSTEMCTL_STATE" > "$SYSTEMCTL_STATE.tmp" || true
      mv "$SYSTEMCTL_STATE.tmp" "$SYSTEMCTL_STATE"
    done
    ;;
  start)
    for unit in "$@"; do
      if [[ -n "${FAKE_START_WAIT_FILE:-}" && \
            "${FAKE_START_WAIT_UNIT:-}" == "$unit" ]]; then
        while [[ ! -e "$FAKE_START_WAIT_FILE" ]]; do
          sleep 0.02
        done
      fi
      grep -Fqx "$unit" "$SYSTEMCTL_STATE" || echo "$unit" >> "$SYSTEMCTL_STATE"
    done
    ;;
  *)
    exit 2
    ;;
esac
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    # The production script runs an embedded Python demo-account flat check.
    # A fake interpreter keeps this unit test offline while preserving the
    # subprocess ordering and failure/recovery behaviour.
    python = fake_bin / "python3"
    python.write_text(
        """#!/usr/bin/env bash
if [[ "${1:-}" == "-c" ]]; then
  exec "$REAL_PYTHON" "$@"
fi
if [[ "${1:-}" == "-" && "${2:-}" == "--write-reset-boundary" ]]; then
  exec "$REAL_PYTHON" "$@"
fi
cat >/dev/null
if [[ "${FAKE_ACCOUNT_GUARD_RC:-0}" == "0" ]]; then
  echo "  demo-account-flat-ok positions=0 open_orders=0"
  exit 0
fi
echo "ERROR: synthetic demo account is not flat" >&2
exit "$FAKE_ACCOUNT_GUARD_RC"
""",
        encoding="utf-8",
    )
    python.chmod(0o755)

    env_file = tmp_path / "bybit-demo.env"
    env_file.write_text(
        f"DEMO=true\nREAL_MONEY={real_money}\n"
        "BYBIT_DEMO_API_KEY=fake\nBYBIT_DEMO_API_SECRET=fake\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("REAL_MONEY", None)
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
            "SYSTEMCTL_BIN": str(systemctl),
            "SYSTEMCTL_STATE": str(state),
            "SYSTEMCTL_LOG": str(log),
            "FAKE_ACCOUNT_GUARD_RC": str(account_guard_rc),
            "FAKE_SYSTEMD_ENV_FILE": str(env_file),
            "REAL_PYTHON": sys.executable,
            "PYTHONPATH": (
                f"{REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
            ),
            "LEDGER_RESET_LOCK_FILE": str(tmp_path / "ledger-reset.lock"),
            "LEDGER_RESET_SETTLE_SECONDS": "0",
        }
    )
    script = REPO_ROOT / "scripts" / "reset_demo_paper_ledgers.sh"
    return script, env_file, env, log


def test_reset_demo_paper_ledgers_is_dry_run_by_default_and_execute_is_archival(
    tmp_path: Path,
) -> None:
    import tarfile

    script, env_file, env, log = _ledger_reset_harness(tmp_path)
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    cycles = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_cycles"
    klines = tmp_path / "data" / "bybit-long-demo-event" / "event_demo_klines_1h"
    cache = tmp_path / "data" / "bybit-long-demo-event" / ".cache"
    reports = tmp_path / "data" / "bybit-long-demo-event" / "reports"
    for directory in (ledger, cycles, klines, cache, reports):
        directory.mkdir(parents=True)
        (directory / "part.parquet").write_bytes(b"x")

    preview = subprocess.run(
        ["bash", str(script), "--sleeves", "long"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert preview.returncode == 0, preview.stderr
    assert "mode: dry-run" in preview.stdout.lower()
    assert "no services or files were changed" in preview.stdout
    assert ledger.exists() and cycles.exists() and klines.exists()
    assert cache.exists() and reports.exists()
    assert not log.exists(), "dry-run must not even query or stop systemd units"
    assert not (tmp_path / "data" / "_archive").exists()

    # systemd may expose the canonical path while an operator supplies a safe
    # symlink alias. The account-binding guard compares resolved paths.
    env_alias = tmp_path / "bybit-demo-alias.env"
    env_alias.symlink_to(env_file)

    executed = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--sleeves",
            "long",
            "--env-file",
            str(env_alias),
            "--label",
            "exit-overhaul",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert executed.returncode == 0, executed.stderr
    assert not ledger.exists() and not cycles.exists()
    assert klines.exists(), "root-level market data must be preserved"
    assert cache.exists(), "cache removal requires --include-caches"
    assert reports.exists(), "report removal requires --include-reports"
    archives = list((tmp_path / "data" / "_archive").glob("ledger-reset-*-exit-overhaul.tar.gz"))
    assert len(archives) == 1
    digest = archives[0].with_name(archives[0].name + ".sha256")
    assert digest.exists()
    assert archives[0].name in digest.read_text(encoding="utf-8")
    with tarfile.open(archives[0]) as archive:
        names = archive.getnames()
    assert "ledger-reset-manifest.txt" in names
    assert any(name.startswith("data/bybit-long-demo-event/long_native_demo_trades") for name in names)

    systemctl_log = log.read_text(encoding="utf-8")
    assert "stop liquidity-migration-bybit-long-demo.service" in systemctl_log
    assert "stop liquidity-migration-bybit-continuous-demo.service" in systemctl_log
    assert "stop liquidity-migration-bybit-risk.service" in systemctl_log
    assert "start liquidity-migration-bybit-risk.service" in systemctl_log
    assert "is-active --quiet liquidity-migration-bybit-risk.service" in systemctl_log
    assert "service state: restored" in executed.stdout


@pytest.mark.parametrize("via_symlink", [False, True])
def test_reset_demo_paper_ledgers_refuses_archive_inside_reset_target_after_canonicalization(
    tmp_path: Path,
    via_symlink: bool,
) -> None:
    script, _env_file, env, log = _ledger_reset_harness(tmp_path)
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")
    if via_symlink:
        alias = tmp_path / "archive-alias"
        alias.symlink_to(ledger, target_is_directory=True)
        archive_dir = alias / "_archive"
    else:
        archive_dir = Path(
            "data/bybit-long-demo-event/./long_native_demo_trades/_archive"
        )

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--sleeves",
            "long",
            "--archive-dir",
            str(archive_dir),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "--archive-dir must be outside reset targets" in result.stderr
    assert ledger.exists()
    assert not log.exists(), "containment refusal must precede all systemd access"


def test_reset_demo_paper_ledgers_continuous_selection_includes_hedge_and_cache_is_opt_in(
    tmp_path: Path,
) -> None:
    import tarfile

    script, env_file, env, _ = _ledger_reset_harness(tmp_path)
    long_ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    continuous_ledger = (
        tmp_path / "data" / "bybit-continuous-demo-event" / "continuous_fade_demo_trades"
    )
    paper_ledger = (
        tmp_path / "data" / "bybit-continuous-paper-event" / "continuous_fade_paper_trades"
    )
    hedge_ledger = (
        tmp_path / "data" / "bybit-continuous-hedge-event" / "continuous_fade_demo_trades"
    )
    cache = tmp_path / "data" / "bybit-continuous-demo-event" / ".cache"
    demo_equity_state = (
        tmp_path
        / "data"
        / "bybit-continuous-demo-event"
        / "continuous_account_equity_state.json"
    )
    paper_equity_state = (
        tmp_path
        / "data"
        / "bybit-continuous-paper-event"
        / "continuous_account_equity_state.json"
    )
    demo_risk_events = continuous_ledger.parent / "continuous_risk_events.jsonl"
    demo_lifecycle_events = continuous_ledger.parent / "continuous_lifecycle_events.jsonl"
    demo_dynexit_shadow = continuous_ledger.parent / "continuous_dynexit_shadow.jsonl"
    paper_risk_events = paper_ledger.parent / "continuous_risk_events.jsonl"
    paper_lifecycle_events = paper_ledger.parent / "continuous_lifecycle_events.jsonl"
    paper_dynexit_shadow = paper_ledger.parent / "continuous_dynexit_shadow.jsonl"
    for directory in (long_ledger, continuous_ledger, paper_ledger, hedge_ledger, cache):
        directory.mkdir(parents=True)
        (directory / "part.parquet").write_bytes(b"x")
    for events in (
        demo_risk_events,
        demo_lifecycle_events,
        demo_dynexit_shadow,
        paper_risk_events,
        paper_lifecycle_events,
        paper_dynexit_shadow,
    ):
        events.write_text('{"event":"old-forward-window"}\n', encoding="utf-8")
    for state in (demo_equity_state, paper_equity_state):
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"high_water_usdt":10039.68}', encoding="utf-8")

    executed = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--sleeves",
            "continuous",
            "--env-file",
            str(env_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert executed.returncode == 0, executed.stderr
    assert long_ledger.exists(), "continuous-only reset must preserve long ledgers"
    assert not continuous_ledger.exists() and not paper_ledger.exists()
    assert not hedge_ledger.exists(), "continuous selection must include its submit-armed hedge ledger"
    assert not demo_risk_events.exists() and not demo_lifecycle_events.exists()
    assert not paper_risk_events.exists() and not paper_lifecycle_events.exists(), (
        "old operational failures must not contaminate the post-reset forward window"
    )
    assert not demo_dynexit_shadow.exists() and not paper_dynexit_shadow.exists(), (
        "pre-reset dynamic-exit shadow evidence must not leak into the new forward window"
    )
    assert "reset-boundary-heartbeats-ok" in executed.stdout
    demo_boundary = read_dataset(
        continuous_ledger.parent,
        "continuous_fade_demo_cycles",
    )
    paper_boundary = read_dataset(
        paper_ledger.parent,
        "continuous_fade_paper_cycles",
    )
    assert demo_boundary.height == 1 and paper_boundary.height == 1
    assert demo_boundary.select("reason").item() == "verified_flat_ledger_reset"
    assert paper_boundary.select("account_flat_verified").item() is True
    assert cache.exists(), "cache is preserved unless explicitly selected"
    assert demo_equity_state.exists() and paper_equity_state.exists(), (
        "a ledger reset must not erase the account drawdown high-water risk memory"
    )
    archives = list((tmp_path / "data" / "_archive").glob("ledger-reset-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        names = archive.getnames()
        manifest = archive.extractfile("ledger-reset-manifest.txt")
        assert manifest is not None
        manifest_text = manifest.read().decode("utf-8")
    assert str(demo_equity_state.relative_to(tmp_path)) in names
    assert str(paper_equity_state.relative_to(tmp_path)) in names
    assert str(demo_risk_events.relative_to(tmp_path)) in names
    assert str(paper_lifecycle_events.relative_to(tmp_path)) in names
    assert str(demo_dynexit_shadow.relative_to(tmp_path)) in names
    assert str(paper_dynexit_shadow.relative_to(tmp_path)) in names
    assert f"preserved_risk_state={demo_equity_state.relative_to(tmp_path)}" in manifest_text

    cache_reset = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--sleeves",
            "continuous",
            "--include-caches",
            "--env-file",
            str(env_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert cache_reset.returncode == 0, cache_reset.stderr
    assert not cache.exists()
    assert demo_equity_state.exists() and paper_equity_state.exists()


def test_reset_demo_paper_ledgers_all_includes_shared_risk_compatibility_ledger(
    tmp_path: Path,
) -> None:
    script, env_file, env, _ = _ledger_reset_harness(tmp_path)
    shared_trade = tmp_path / "data" / "bybit-demo-event" / "event_demo_trades"
    shared_order = tmp_path / "data" / "bybit-demo-event" / "event_demo_orders"
    shared_state = tmp_path / "data" / "bybit-demo-event" / "cross_sleeve_account_state"
    for directory in (shared_trade, shared_order, shared_state):
        directory.mkdir(parents=True)
        (directory / "part.parquet").write_bytes(b"x")

    result = subprocess.run(
        ["bash", str(script), "--execute", "--sleeves", "all", "--env-file", str(env_file)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not shared_trade.exists() and not shared_order.exists()
    assert shared_state.exists(), "derived account state is not a trade ledger and stays preserved"
    assert "shared-compat" in result.stdout


def test_reset_demo_paper_ledgers_refuses_real_money_before_service_mutation(tmp_path: Path) -> None:
    script, env_file, env, log = _ledger_reset_harness(tmp_path, real_money="true")
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")

    result = subprocess.run(
        ["bash", str(script), "--execute", "--sleeves", "long", "--env-file", str(env_file)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "REAL_MONEY='true'" in result.stderr
    assert "demo/paper only" in result.stderr
    assert ledger.exists()
    assert not log.exists(), "mainnet refusal must happen before any systemd mutation"


def test_reset_demo_paper_ledgers_refuses_concurrent_execute_before_systemd_query(
    tmp_path: Path,
) -> None:
    import fcntl

    script, env_file, env, log = _ledger_reset_harness(tmp_path)
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")

    lock_path = Path(env["LEDGER_RESET_LOCK_FILE"])
    with lock_path.open("w", encoding="utf-8") as held_lock:
        fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [
                "bash",
                str(script),
                "--execute",
                "--sleeves",
                "long",
                "--env-file",
                str(env_file),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert result.returncode != 0
    assert "another demo/paper ledger reset is already executing" in result.stderr
    assert ledger.exists()
    assert not log.exists(), "lock contention must refuse before querying or mutating systemd"


def test_reset_demo_paper_ledgers_refuses_systemd_env_mismatch_before_service_mutation(
    tmp_path: Path,
) -> None:
    script, env_file, env, log = _ledger_reset_harness(tmp_path)
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")
    other_env = tmp_path / "different-demo-account.env"
    other_env.write_text(
        "DEMO=true\nREAL_MONEY=false\n"
        "BYBIT_DEMO_API_KEY=other\nBYBIT_DEMO_API_SECRET=other\n",
        encoding="utf-8",
    )
    mismatch_unit = "liquidity-migration-bybit-continuous-demo.service"
    env["FAKE_SYSTEMD_ENV_MISMATCH_UNIT"] = mismatch_unit
    env["FAKE_SYSTEMD_ENV_MISMATCH_FILE"] = str(other_env)

    result = subprocess.run(
        ["bash", str(script), "--execute", "--sleeves", "long", "--env-file", str(env_file)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert mismatch_unit in result.stderr
    assert "ambiguous credential environment" in result.stderr
    assert "refusing before stopping services" in result.stderr
    assert ledger.exists()
    systemctl_log = log.read_text(encoding="utf-8")
    assert f"show {mismatch_unit} --property=EnvironmentFiles --value" in systemctl_log
    assert not any(
        line.startswith(("stop ", "start ")) for line in systemctl_log.splitlines()
    ), "credential-file mismatch must refuse before service mutation"


@pytest.mark.parametrize("override_kind", ["later_file", "direct"])
def test_reset_demo_paper_ledgers_refuses_later_credential_override(
    tmp_path: Path,
    override_kind: str,
) -> None:
    script, env_file, env, log = _ledger_reset_harness(tmp_path)
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")
    unit = "liquidity-migration-bybit-long-demo.service"
    if override_kind == "later_file":
        override = tmp_path / "later-credentials.env"
        override.write_text(
            "BYBIT_DEMO_API_KEY=different\nREAL_MONEY=false\n",
            encoding="utf-8",
        )
        env["FAKE_SYSTEMD_EXTRA_ENV_UNIT"] = unit
        env["FAKE_SYSTEMD_EXTRA_ENV_FILE"] = str(override)
    else:
        env["FAKE_SYSTEMD_DIRECT_ENVIRONMENT"] = "BYBIT_DEMO_API_KEY=different"

    result = subprocess.run(
        ["bash", str(script), "--execute", "--sleeves", "long", "--env-file", str(env_file)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "ambiguous credential environment" in result.stderr
    assert ledger.exists()
    systemctl_log = log.read_text(encoding="utf-8")
    assert not any(
        line.startswith(("stop ", "start ")) for line in systemctl_log.splitlines()
    )


def test_reset_demo_paper_ledgers_lock_stays_held_during_failure_recovery_restart(
    tmp_path: Path,
) -> None:
    import time

    risk_unit = "liquidity-migration-bybit-risk.service"
    script, env_file, env, log = _ledger_reset_harness(
        tmp_path,
        account_guard_rc=7,
        active_units=(risk_unit,),
    )
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")
    release_restart = tmp_path / "release-restart"
    env["FAKE_START_WAIT_FILE"] = str(release_restart)
    env["FAKE_START_WAIT_UNIT"] = risk_unit

    first = subprocess.Popen(
        ["bash", str(script), "--execute", "--sleeves", "long", "--env-file", str(env_file)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if log.exists() and f"start {risk_unit}" in log.read_text(encoding="utf-8"):
                break
            time.sleep(0.02)
        else:
            pytest.fail("first reset never reached its failure-recovery restart")

        overlapping = subprocess.run(
            [
                "bash",
                str(script),
                "--execute",
                "--sleeves",
                "long",
                "--env-file",
                str(env_file),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert overlapping.returncode != 0
        assert "another demo/paper ledger reset is already executing" in overlapping.stderr
    finally:
        release_restart.touch()
        first_stdout, first_stderr = first.communicate(timeout=10)

    assert first.returncode != 0, first_stdout
    assert "synthetic demo account is not flat" in first_stderr
    assert ledger.exists()


def test_reset_demo_paper_ledgers_flat_check_failure_restores_services_without_deleting(
    tmp_path: Path,
) -> None:
    active = (
        "liquidity-migration-bybit-risk.service",
        "liquidity-migration-bybit-continuous-demo.service",
    )
    script, env_file, env, log = _ledger_reset_harness(
        tmp_path, account_guard_rc=7, active_units=active
    )
    ledger = tmp_path / "data" / "bybit-continuous-demo-event" / "continuous_fade_demo_trades"
    ledger.mkdir(parents=True)
    (ledger / "part.parquet").write_bytes(b"x")

    result = subprocess.run(
        [
            "bash",
            str(script),
            "--execute",
            "--sleeves",
            "continuous",
            "--env-file",
            str(env_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "synthetic demo account is not flat" in result.stderr
    assert ledger.exists(), "flat-account guard must run before archive/removal"
    assert not (tmp_path / "data" / "_archive").exists()
    systemctl_log = log.read_text(encoding="utf-8")
    assert "stop liquidity-migration-bybit-risk.service" in systemctl_log
    assert "start liquidity-migration-bybit-risk.service" in systemctl_log
    assert "start liquidity-migration-bybit-continuous-demo.service" in systemctl_log


def test_unit_execstart_args_parse_against_their_script_parsers() -> None:
    """THE class-test for the 2026-06-11 demo-liveness crash-loop: the unit kept
    passing --data-root after the purge dropped that argparse argument, every
    string-presence unit test stayed green, and only the VPS journal noticed the
    watchdog dying every 3 minutes. For every unit whose ExecStart invokes a repo
    python script or module with flags, parse the unit's actual argv against the
    target's actual parser — argv↔argparse drift fails HERE, not on the box.
    (run_*.sh wrapper units are env-driven, not argv-driven — out of scope.)"""
    import shlex

    repo = Path(__file__).resolve().parents[1]

    def _execstart_tokens(unit_text: str) -> list[str]:
        lines = unit_text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("ExecStart="):
                block = [line[len("ExecStart="):]]
                while block[-1].rstrip().endswith("\\"):
                    block[-1] = block[-1].rstrip()[:-1]
                    i += 1
                    block.append(lines[i])
                return shlex.split(" ".join(block))
        return []

    import importlib.util
    import sys as _sys

    def _script_parser(script: str):
        spec = importlib.util.spec_from_file_location(f"_parity_{Path(script).stem}", repo / script)
        module = importlib.util.module_from_spec(spec)
        _sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    checked = 0
    for unit_path in sorted((repo / "deploy" / "systemd").glob("*.service")):
        tokens = _execstart_tokens(unit_path.read_text(encoding="utf-8"))
        assert tokens, f"{unit_path.name}: no ExecStart found"
        # locate the target: a scripts/*.py path, a -m module, or a wrapper (skip)
        argv: list[str] | None = None
        parse = None
        for idx, tok in enumerate(tokens):
            if tok.endswith(".sh"):
                break  # env-driven wrapper
            if tok.endswith(".py"):
                script_rel = tok.removeprefix("/opt/liquidity-migration/")
                mod = _script_parser(script_rel)
                argv = tokens[idx + 1:]

                def parse(a, m=mod):
                    fn = m.build_arg_parser().parse_args if hasattr(m, "build_arg_parser") else m.parse_args
                    return fn(a)
                break
            if tok == "-m":
                target = tokens[idx + 1]
                argv = tokens[idx + 2:]
                if target == "liquidity_migration":
                    from liquidity_migration.cli import build_parser

                    def parse(a, _build=build_parser):
                        return _build().parse_args(a)
                else:
                    module = __import__(target, fromlist=["build_arg_parser"])

                    def parse(a, m=module):
                        return m.build_arg_parser().parse_args(a)
                break
        if parse is None or argv is None:
            continue
        try:
            parse(argv)
        except SystemExit as exc:
            raise AssertionError(
                f"{unit_path.name}: ExecStart args do not parse against the target's "
                f"argparse (exit {exc.code}): {argv}"
            ) from exc
        checked += 1
    # the units this test exists for must actually be covered
    assert checked >= 4, f"expected at least 4 argv-driven units, checked {checked}"


def test_vps_deploy_paths_filter_covers_every_unit_invoked_script() -> None:
    """Round 4: the workflow paths-filter class bit twice (configs/ in round 2,
    hedge warmstart CSVs in round 3) because the filter is hand-listed with no
    structural guard. Derive the required entries from the units themselves —
    every repo-relative script referenced by a unit (ExecStart + continuation
    lines) and every scripts/* file a run_*.sh wrapper invokes must be in the
    workflow paths filter, else a change to it deploys NOTHING."""
    import re

    repo = Path(__file__).resolve().parents[1]
    workflow = (repo / ".github" / "workflows" / "vps-deploy.yml").read_text(encoding="utf-8")
    script_ref = re.compile(r"(?:/opt/liquidity-migration/)?(scripts/[A-Za-z0-9_./-]+\.(?:py|sh))")

    required: set[str] = set()
    for unit in sorted((repo / "deploy" / "systemd").glob("liquidity-migration-*.service")):
        for line in unit.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for match in script_ref.findall(line):
                if (repo / match).exists():
                    required.add(match)
    for wrapper in sorted((repo / "scripts").glob("run_*.sh")):
        for line in wrapper.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for match in script_ref.findall(line):
                if (repo / match).exists():
                    required.add(match)
    # Runtime data/config files units read at startup or on every timer run.
    required |= {"deploy/sleeves.env", "deploy/lib_sleeves.sh", "configs/volume_alpha.default.yaml"}

    missing = sorted(p for p in required if f'"{p}"' not in workflow)
    assert not missing, f"vps-deploy.yml paths filter is missing unit-invoked files: {missing}"
    # Globbed entries the derivation above can't see.
    assert '"deploy/systemd/*.service"' in workflow
    assert '"deploy/systemd/*.timer"' in workflow
    # The armed hedge reads these CSVs every run; the operator-pending
    # warmstart-refresh commit deploys ONLY if this entry stays (round 3/4).
    assert '"deploy/hedge_warmstart/*.csv"' in workflow


def test_vps_deploy_workflow_has_full_suite_ci_gate() -> None:
    """deploy-ci-2 (folded from test_audit_fix_b05.py): the deploy workflow must run a
    server-side ruff + full-pytest CI job, and the deploy job must depend on it — so an
    uninstalled local pre-push hook, a --no-verify, or a GitHub web edit can no longer
    auto-deploy untested code."""
    repo = Path(__file__).resolve().parents[1]
    wf = (repo / ".github" / "workflows" / "vps-deploy.yml").read_text(encoding="utf-8")

    # A dedicated CI job running the FULL gate (ruff over all three trees + pytest -q).
    assert "ruff check liquidity_migration tests scripts" in wf
    assert "pytest -q" in wf
    # The deploy job gates on it.
    assert "needs: ci" in wf
    # CI runs on PRs too (the deploy steps stay push/dispatch-guarded).
    assert "pull_request:" in wf
    # The deploy job must not touch the box on a PR.
    assert "github.event_name != 'pull_request'" in wf


def _unit_environment(unit_path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in unit_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("Environment=") and "=" in line[len("Environment="):]:
            key, value = line[len("Environment="):].split("=", 1)
            env[key] = value.strip('"')
    return env


@pytest.mark.parametrize(
    ("unit_name", "wrapper_name"),
    [
        ("liquidity-migration-bybit-continuous-demo.service", "run_bybit_continuous_demo_event_engine.sh"),
        ("liquidity-migration-bybit-continuous-paper.service", "run_bybit_continuous_demo_event_engine.sh"),
        ("liquidity-migration-bybit-long-demo.service", "run_bybit_long_demo_event_engine.sh"),
        ("liquidity-migration-bybit-long-paper.service", "run_bybit_long_demo_event_engine.sh"),
        ("liquidity-migration-bybit-risk.service", "run_bybit_demo_ws_risk_engine.sh"),
    ],
)
def test_wrapper_unit_env_builds_argv_that_parses(unit_name: str, wrapper_name: str, tmp_path: Path) -> None:
    """Round 4: the ExecStart<->argparse parity test deliberately skips the
    env-driven run_*.sh wrapper units — so a dropped/renamed CLI flag bricked
    the ORDER-SUBMITTING daemon at restart instead of failing the pre-restart
    smoke gate. Run each wrapper with PYTHON_BIN pointed at an argv-capturing
    stub under the unit's own Environment= values, then parse the captured argv
    with the real CLI parser."""
    import os
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    unit_path = repo / "deploy" / "systemd" / unit_name
    if not unit_path.exists():
        pytest.skip(f"{unit_name} not present")
    argv_out = tmp_path / "argv.bin"
    stub = tmp_path / "python_stub.sh"
    # as_posix() + quoting: a raw WindowsPath embeds backslashes into the bash
    # script/redirect, which bash strips — the stub then writes to a mangled
    # filename and the test false-fails on any Windows dev box.
    stub.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\0' \"$@\" > '{argv_out.as_posix()}'\n", encoding="utf-8"
    )
    stub.chmod(0o755)

    env = {**os.environ, **_unit_environment(unit_path)}
    env["PYTHON_BIN"] = stub.as_posix()
    # The wrappers fail loud on missing telegram/API creds (correct on the box,
    # where the EnvironmentFile provides them) — supply dummies here. The stub
    # never reaches the network.
    env.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
    env.setdefault("TELEGRAM_CHAT_ID", "1")
    env.setdefault("BYBIT_DEMO_API_KEY", "test-key")
    env.setdefault("BYBIT_DEMO_API_SECRET", "test-secret")
    env["RUN_ONCE"] = "1"

    # Daemon-mode wrappers exec the stub and return immediately; the legacy
    # single-cycle loop (USE_DAEMON=0, the long paper unit) honors RUN_ONCE here
    # so this smoke gate remains deterministic.
    try:
        result = subprocess.run(
            ["bash", str(repo / "scripts" / wrapper_name)],
            env=env, cwd=repo, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"{wrapper_name} failed under {unit_name} env: {result.stderr}"
    except subprocess.TimeoutExpired:
        assert argv_out.exists(), f"{wrapper_name} looped without ever invoking PYTHON_BIN"
    raw = argv_out.read_bytes().decode("utf-8")
    tokens = [t for t in raw.split("\0") if t]
    assert tokens[:2] == ["-m", "liquidity_migration"], tokens[:4]

    from liquidity_migration.cli import build_parser

    try:
        build_parser().parse_args(tokens[2:])
    except SystemExit as exc:
        raise AssertionError(
            f"{unit_name} -> {wrapper_name}: wrapper argv does not parse against the CLI "
            f"(exit {exc.code}): {tokens[2:]}"
        ) from exc


# ---------------------------------------------------------------------------
# audit2b: sh_nsymbols — N_SYMBOLS empty-list miscount in
# scripts/build_full_pit_bybit.sh.
#
# The build script derives a count of symbols from a comma-separated string for
# a build-log line. The original logic ``echo "$SYMBOLS" | tr ',' '\n' | wc -l``
# miscounts an EMPTY list as 1, because ``echo ""`` emits a single newline that
# ``wc -l`` then counts. The fix guards the empty case to produce 0 while leaving
# every non-empty (happy-path) count byte-identical.
# ---------------------------------------------------------------------------

_NSYMBOLS_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "build_full_pit_bybit.sh"
)

# The buggy formulation, preserved verbatim to prove the regression existed.
OLD_SNIPPET = 'N_SYMBOLS=$(echo "$SYMBOLS" | tr \',\' \'\\n\' | wc -l)'


def _count_with_new_logic(symbols: str) -> int:
    """Run the script's current N_SYMBOLS logic in isolation via bash."""
    script = (
        f"SYMBOLS={symbols!r}\n"
        "if [ -z \"$SYMBOLS\" ]; then\n"
        "  N_SYMBOLS=0\n"
        "else\n"
        "  N_SYMBOLS=$(echo \"$SYMBOLS\" | tr ',' '\\n' | wc -l)\n"
        "fi\n"
        'echo "$N_SYMBOLS"\n'
    )
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    return int(out.stdout.strip())


def _count_with_old_logic(symbols: str) -> int:
    """Run the original buggy N_SYMBOLS logic, for the failing-on-old assertion."""
    script = f"SYMBOLS={symbols!r}\n" + OLD_SNIPPET + "\n" + 'echo "$N_SYMBOLS"\n'
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    return int(out.stdout.strip())


def test_empty_list_counts_zero_not_one() -> None:
    # OLD code is wrong: blank line counted as one symbol.
    assert _count_with_old_logic("") == 1
    # NEW code: empty list -> 0.
    assert _count_with_new_logic("") == 0


def test_happy_path_counts_unchanged() -> None:
    # Non-empty inputs are byte-identical between old and new logic.
    for symbols in ("BTCUSDT", "BTCUSDT,ETHUSDT", "BTCUSDT,ETHUSDT,SOLUSDT"):
        old = _count_with_old_logic(symbols)
        new = _count_with_new_logic(symbols)
        assert old == new, f"happy path changed for {symbols!r}: {old} != {new}"
    assert _count_with_new_logic("BTCUSDT") == 1
    assert _count_with_new_logic("BTCUSDT,ETHUSDT") == 2
    assert _count_with_new_logic("BTCUSDT,ETHUSDT,SOLUSDT") == 3


def test_script_carries_the_guard() -> None:
    text = _NSYMBOLS_SCRIPT.read_text()
    # The empty-list guard is present and the bare buggy one-liner is gone.
    assert 'if [ -z "$SYMBOLS" ]; then' in text
    assert "N_SYMBOLS=0" in text
    assert not re.search(
        r"^N_SYMBOLS=\$\(echo \"\$SYMBOLS\" \| tr",
        text,
        flags=re.MULTILINE,
    ), "the unguarded buggy N_SYMBOLS one-liner is still present"


# ---------------------------------------------------------------------------
# audit2b: sh_ruff — gate-7 ruff fallback in scripts/verify_full_pit_rebuild.sh.
#
# The verification script runs ``set -euo pipefail`` and, in gate 7, linted with::
#
#     .venv/bin/ruff check liquidity_migration tests || ruff check liquidity_migration tests
#
# The ``||`` was intended only as a fallback for a *missing* ``.venv/bin/ruff``
# (exit 127), but it fires on ANY non-zero exit — including a genuine lint
# failure (ruff exits 1 when it finds errors). So if the canonical venv ruff found
# a real lint error, the gate silently re-checked against a different PATH ruff;
# when that one passed (version/config drift), the gate reported PASS and the
# script printed "All gates PASSED" despite a real lint failure.
#
# The fix selects the ruff binary up-front (prefer ``.venv/bin/ruff`` if
# executable, else PATH ``ruff``) and runs it exactly once, so its exit code —
# including a lint failure — propagates and fails the gate. When the venv binary
# is absent the fallback to PATH ruff is preserved, and the happy path (venv ruff
# present and clean) is byte-identical.
# ---------------------------------------------------------------------------

_RUFF_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "verify_full_pit_rebuild.sh"
)

# Stub ruff binaries: a passing stub (exit 0) and a failing stub (exit 1,
# mimicking ruff finding a lint error).
_PASS_STUB = '#!/usr/bin/env bash\nexit 0\n'
_FAIL_STUB = '#!/usr/bin/env bash\necho "F401 unused import"\nexit 1\n'


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _run_old_gate(tmp_path: Path, venv_body: str, path_body: str | None) -> int:
    """Model the OLD gate-7 lint line: ``$VENV check || ruff check``.

    Returns the exit code under ``set -euo pipefail`` (what the script as a whole
    would have done at that line). ``path_body=None`` means no PATH ruff exists.
    """
    venv = tmp_path / "venv_ruff"
    _make_stub(venv, venv_body)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if path_body is not None:
        _make_stub(bindir / "ruff", path_body)
    script = (
        "set -euo pipefail\n"
        f'"{venv}" check liquidity_migration tests'
        " || ruff check liquidity_migration tests\n"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
        timeout=5,
    ).returncode


def _run_new_gate(tmp_path: Path, venv_body: str | None, path_body: str | None) -> int:
    """Model the NEW gate-7 lint logic: pick the binary, then run it once.

    ``venv_body=None`` means ``.venv/bin/ruff`` is absent (fallback to PATH).
    """
    venv = tmp_path / "venv_ruff"
    if venv_body is not None:
        _make_stub(venv, venv_body)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if path_body is not None:
        _make_stub(bindir / "ruff", path_body)
    script = (
        "set -euo pipefail\n"
        f'if [ -x "{venv}" ]; then\n'
        f'  RUFF_BIN="{venv}"\n'
        "else\n"
        '  RUFF_BIN="ruff"\n'
        "fi\n"
        '"$RUFF_BIN" check liquidity_migration tests\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
        timeout=5,
    ).returncode


def test_old_logic_masks_a_real_lint_failure(tmp_path: Path) -> None:
    # Canonical venv ruff finds a lint error (exit 1); PATH ruff passes.
    # OLD: the `||` swallows the failure -> gate exits 0 (masked).
    assert _run_old_gate(tmp_path, _FAIL_STUB, _PASS_STUB) == 0


def test_new_logic_fails_the_gate_on_a_real_lint_failure(tmp_path: Path) -> None:
    # Same inputs as above. NEW: venv ruff is chosen and its exit 1 propagates.
    assert _run_new_gate(tmp_path, _FAIL_STUB, _PASS_STUB) != 0


def test_new_logic_happy_path_unchanged(tmp_path: Path) -> None:
    # Venv ruff present and clean -> gate passes, identical to old behavior.
    assert _run_old_gate(tmp_path, _PASS_STUB, _PASS_STUB) == 0
    assert _run_new_gate(tmp_path, _PASS_STUB, _PASS_STUB) == 0


def test_new_logic_falls_back_when_venv_ruff_absent(tmp_path: Path) -> None:
    # The original fallback intent is preserved: missing venv ruff -> PATH ruff.
    assert _run_new_gate(tmp_path, None, _PASS_STUB) == 0
    # And a PATH-ruff lint failure still fails the gate.
    assert _run_new_gate(tmp_path, None, _FAIL_STUB) != 0


def test_script_carries_the_fix() -> None:
    text = _RUFF_SCRIPT.read_text()
    # The single-binary selection is present.
    assert 'if [ -x .venv/bin/ruff ]; then' in text
    assert 'RUFF_BIN=".venv/bin/ruff"' in text
    assert '"$RUFF_BIN" check liquidity_migration tests' in text
    # The masking `||` fallback one-liner is gone.
    assert ".venv/bin/ruff check liquidity_migration tests || ruff check" not in text


# ──────────────────────────────────────────────────────────────────────────────
# deploy script — structural guards (from audit b13; deploy-ci-3, deploy-ci-6,
#                 deploy-env-timers-1, deploy-env-timers-3)
# ──────────────────────────────────────────────────────────────────────────────
def test_deploy_verifies_liquidation_collector_active() -> None:
    # deploy-ci-3: the always-on collector must be verified active+enabled in the
    # post-settle block (not just enabled+restarted), so a crash on new code fails loud.
    txt = DEPLOY_SH.read_text()
    assert "systemctl is-active --quiet liquidity-migration-liquidation-collector.service" in txt
    assert "systemctl is-enabled --quiet liquidity-migration-liquidation-collector.service" in txt


def _assert_depth_collector_operator_gated_but_verified(text: str, *, success_marker: str) -> None:
    unit = "liquidity-migration-depth-collector.service"
    assert f"systemctl enable {unit}" not in text
    assert f"systemctl enable --now {unit}" not in text
    assert f"systemctl is-enabled --quiet {unit} 2>/dev/null" in text
    assert f"systemctl is-active --quiet {unit}" in text
    assert "is active but not enabled" in text
    verify_block = text[text.index('if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then') : text.index(success_marker)]
    assert f"systemctl is-enabled --quiet {unit} 2>/dev/null" in verify_block
    assert f"systemctl is-active --quiet {unit}" in verify_block


def test_deploy_depth_collector_is_operator_gated_but_verified_if_enabled() -> None:
    # Bybit historical book depth is unbuyable: deploy must not enable this data
    # collector by surprise, but once an operator enables it, success must require
    # the enabled unit to be active.
    text = DEPLOY_SH.read_text()
    assert "systemctl restart liquidity-migration-depth-collector.service" in text
    _assert_depth_collector_operator_gated_but_verified(text, success_marker='echo "deploy-verify-ok')


def test_verify_depth_collector_is_operator_gated_but_verified_if_enabled() -> None:
    _assert_depth_collector_operator_gated_but_verified(
        VERIFY_SH.read_text(),
        success_marker='echo "verify-ok',
    )


def test_recovery_depth_collector_is_operator_gated_but_verified_if_enabled() -> None:
    text = RECOVERY_SH.read_text()
    assert "systemctl restart liquidity-migration-depth-collector.service" in text
    _assert_depth_collector_operator_gated_but_verified(text, success_marker='echo "deploy-verify-ok')


def test_deploy_refuses_real_money_env() -> None:
    # deploy-ci-6: the deploy must fail-closed if the sourced env sets REAL_MONEY truthy.
    txt = DEPLOY_SH.read_text()
    assert "REAL_MONEY" in txt
    assert "Refusing deploy: REAL_MONEY" in txt


def test_deploy_and_verify_check_bybit_order_permissions_after_env_guard() -> None:
    # The VPS verifier previously passed with a read-only demo key; the order
    # daemons then failed later at set_leverage. Pin a live permission probe in
    # every deploy/verify path after the REAL_MONEY guard has resolved the env.
    for path, token in [
        (DEPLOY_SH, "--context deploy"),
        (VERIFY_SH, "--context verify"),
        (REPO_ROOT / "scripts" / "vps_console_recover_and_deploy.sh", "--context recovery-deploy"),
    ]:
        text = path.read_text(encoding="utf-8")
        source_idx = text.index(". /etc/liquidity-migration/bybit-demo.env")
        guard_idx = text.index('case "${REAL_MONEY:-}" in')
        check_idx = text.index("scripts/check_bybit_order_permissions.py")
        assert source_idx < guard_idx < check_idx
        assert token in text


def test_order_submitting_runners_fail_fast_on_bybit_order_permissions() -> None:
    # A read-only demo key can pass wallet/position reads and then fail only at
    # set_leverage. Every submit-enabled wrapper should detect that at startup.
    for script_name, confirm_token, context in [
        ("run_bybit_long_demo_event_engine.sh", "CONFIRM_DEMO_ORDERS=1", "--context long-demo"),
        ("run_bybit_continuous_demo_event_engine.sh", "CONFIRM_DEMO_ORDERS=1", "--context continuous-demo"),
        ("run_bybit_demo_ws_risk_engine.sh", "CONFIRM_DEMO_ORDERS=1", "--context ws-risk"),
        ("run_continuous_hedge.sh", "CONFIRM_DEMO_ORDERS=1", "--context continuous-hedge"),
    ]:
        text = (REPO_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
        confirm_idx = text.index(confirm_token)
        check_idx = text.index("scripts/check_bybit_order_permissions.py")
        assert confirm_idx < check_idx
        assert context in text


def test_order_permission_checker_fails_cleanly_without_demo_credentials() -> None:
    env = os.environ.copy()
    for key in [
        "BYBIT_DEMO_API_KEY",
        "BYBIT_DEMO_API_SECRET",
        "BYBIT_REAL_API_KEY",
        "BYBIT_REAL_API_SECRET",
        "REAL_MONEY",
    ]:
        env.pop(key, None)
    env["DEMO"] = "true"

    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_bybit_order_permissions.py"),
            "--context",
            "unit-test",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 1
    assert "missing BYBIT_DEMO_API_KEY/BYBIT_DEMO_API_SECRET" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_deploy_keeps_hedge_timer_when_continuous_off_but_leg_open() -> None:
    # deploy-env-timers-1: the gating must NOT be a bare apply_timer_enable on
    # CONTINUOUS_SLEEVE; it must consult the hedge ledger and keep the timer enabled
    # while an open hedge leg exists.
    txt = DEPLOY_SH.read_text()
    assert "_hedge_timer_state" in txt
    assert "bybit-continuous-hedge-event" in txt
    # The verify side must mirror the apply side, not raw CONTINUOUS_SLEEVE.
    assert 'CONTINUOUS_HEDGE_TIMER="$_hedge_timer_state"' in txt
    assert 'verify_hedge_timer_enable "$_hedge_timer_state"' in txt
    assert 'verify_timer "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS' not in txt


def test_deploy_no_longer_warns_on_paper_following_frozen_demo_root() -> None:
    # deploy-env-timers-3 follow-up: paper no longer follows the demo kline/rmom
    # root, so deploy must validate the paper root instead of printing the stale
    # frozen-demo warning.
    txt = DEPLOY_SH.read_text()
    assert "KLINES_FOLLOW_ROOT still points at the now-FROZEN demo kline store" not in txt
    assert "data/bybit-continuous-paper-event/residual_momentum.parquet" in txt
    assert 'sleeve_on "$CONTINUOUS_PAPER_SLEEVE"' in txt


# ──────────────────────────────────────────────────────────────────────────────
# deploy script — behavioral test of the REAL_MONEY refuse guard (from audit b13;
#                 deploy-ci-6)
# Replicates the exact case-statement from deploy_vps_live.sh so the truthy-detection
# logic is exercised, not just present. Kept in sync via the structural test above.
# ──────────────────────────────────────────────────────────────────────────────
_REAL_MONEY_GUARD = r"""
real_money_refused() {
  case "${REAL_MONEY:-}" in
    1|true|TRUE|True|yes|YES|Yes|on|ON|On) return 0 ;;
  esac
  return 1
}
if real_money_refused; then echo REFUSED; else echo OK; fi
"""


def _run_bash(body: str, env_line: str = "") -> str:
    script = textwrap.dedent(f"""
        set -euo pipefail
        {env_line}
        {body}
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "YES", "on", "ON"])
def test_real_money_truthy_values_are_refused(val: str) -> None:
    assert _run_bash(_REAL_MONEY_GUARD, f'export REAL_MONEY="{val}"') == "REFUSED"


@pytest.mark.parametrize("env_line", ['export REAL_MONEY=""', "unset REAL_MONEY || true", 'export REAL_MONEY=false', 'export REAL_MONEY=0'])
def test_real_money_demo_values_are_allowed(env_line: str) -> None:
    assert _run_bash(_REAL_MONEY_GUARD, env_line) == "OK"


# Verify the structural test's source-of-truth: the deploy script's actual case arms
# must contain every truthy token the behavioral guard tests (so they can't drift).
def test_deploy_real_money_case_covers_truthy_tokens() -> None:
    txt = DEPLOY_SH.read_text()
    for token in ("1|true|TRUE", "yes|YES", "on|ON"):
        assert token in txt, f"deploy REAL_MONEY case missing arm {token!r}"


# --- audit bucket b15: kill-switch fake-systemctl semantics (kill-switch-3) ---
_FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "$@" >> "$LOG"
cmd="$1"; shift
now=0; for a in "$@"; do [ "$a" = "--now" ] && now=1; done
args=(); for a in "$@"; do [ "$a" = "--quiet" ] || [ "$a" = "--now" ] || args+=("$a"); done
case "$cmd" in
  enable)  for u in "${args[@]}"; do touch "$STATE/$u.enabled"; [ "$now" = 1 ] && touch "$STATE/$u.active"; done ;;
  disable) for u in "${args[@]}"; do rm -f "$STATE/$u.enabled" "$STATE/$u.active"; done ;;
  restart|start) for u in "${args[@]}"; do touch "$STATE/$u.active"; done ;;
  is-active)  for u in "${args[@]}"; do [ -f "$STATE/$u.active"  ] || exit 1; done ;;
  is-enabled) for u in "${args[@]}"; do [ -f "$STATE/$u.enabled" ] || exit 1; done ;;
esac
exit 0
"""


def test_fake_systemctl_enable_without_now_does_not_start_unit(tmp_path: Path) -> None:
    # kill-switch-3: the test fake must mirror real `systemctl enable` (no --now):
    # it writes the wants-symlink (.enabled) but does NOT start the unit (.active).
    # A fake that set .active on bare `enable` would let an on-path verify pass with
    # no start step, hiding a deploy that dropped the `systemctl restart` lines.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "systemctl").write_text(_FAKE_SYSTEMCTL)
    (fake_bin / "systemctl").chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    log = tmp_path / "log"
    log.write_text("")
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "STATE": str(state), "LOG": str(log)}
    unit = "demo.service"
    subprocess.run(["bash", "-c", f"systemctl enable {unit}"], env=env, check=True, timeout=5)
    assert (state / f"{unit}.enabled").exists()
    assert not (state / f"{unit}.active").exists(), "bare enable must NOT start the unit"
    # enable --now and start DO mark active.
    subprocess.run(["bash", "-c", f"systemctl start {unit}"], env=env, check=True, timeout=5)
    assert (state / f"{unit}.active").exists()


# ==========================================================================
# Relocated from tests/test_audit_int_iI.py (audit bucket iI): cross-file
# integration-completion regression tests for the deploy-gate fixes whose
# owned-file side (scripts/deploy_vps_live.sh) landed in another bucket. Both
# scripts here are SSH/systemctl deploy plumbing that cannot run in CI, so —
# matching the existing deploy-script regression style above — these tests
# assert the static content of the fail-closed guards.
#
# Findings covered:
#   deploy-ci-6  verify_vps_live.sh and vps_console_recover_and_deploy.sh now
#                carry the same fail-closed `case "${REAL_MONEY:-}" in 1|true|...)
#                exit 1` guard as deploy_vps_live.sh.
#   deploy-ci-3  The console-recovery verify block now asserts the always-on
#                liquidation collector is active+enabled before 'deploy-verify-ok'.
# ==========================================================================

REPO = Path(__file__).resolve().parents[1]
VERIFY = REPO / "scripts" / "verify_vps_live.sh"
RECOVERY = REPO / "scripts" / "vps_console_recover_and_deploy.sh"

COLLECTOR = "liquidity-migration-liquidation-collector.service"
# The truthy-REAL_MONEY case arm shared verbatim with deploy_vps_live.sh.
REAL_MONEY_CASE_ARM = "1|true|TRUE|True|yes|YES|Yes|on|ON|On)"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# deploy-ci-6 : fail-closed REAL_MONEY guard in BOTH owned scripts
# --------------------------------------------------------------------------


def _assert_real_money_guard(text: str, *, refusal_token: str) -> None:
    """A `case "${REAL_MONEY:-}"` guard whose truthy arm exits non-zero."""
    assert 'case "${REAL_MONEY:-}" in' in text, "missing REAL_MONEY case guard"
    assert REAL_MONEY_CASE_ARM in text, "REAL_MONEY guard does not match the truthy arm set"
    # The guard must be fail-closed: the truthy arm exits 1, and it references
    # the env file in the refusal so an operator knows what to fix.
    guard = text.split('case "${REAL_MONEY:-}" in', 1)[1].split("esac", 1)[0]
    assert REAL_MONEY_CASE_ARM in guard
    assert "exit 1" in guard, "REAL_MONEY guard must exit 1 on a truthy value"
    assert "REAL_MONEY" in guard
    assert refusal_token in guard
    assert "/etc/liquidity-migration/bybit-demo.env" in guard


def test_verify_script_fails_closed_on_real_money() -> None:
    text = _read(VERIFY)
    _assert_real_money_guard(text, refusal_token="Verification failed")
    # The guard must come AFTER the env is sourced, else ${REAL_MONEY} is unset.
    source_idx = text.index(". /etc/liquidity-migration/bybit-demo.env")
    guard_idx = text.index('case "${REAL_MONEY:-}" in')
    assert source_idx < guard_idx, "REAL_MONEY guard must be after sourcing the env"


def test_recovery_script_fails_closed_on_real_money() -> None:
    text = _read(RECOVERY)
    _assert_real_money_guard(text, refusal_token="Refusing deploy")
    source_idx = text.index(". /etc/liquidity-migration/bybit-demo.env")
    guard_idx = text.index('case "${REAL_MONEY:-}" in')
    assert source_idx < guard_idx, "REAL_MONEY guard must be after sourcing the env"


def test_real_money_guard_does_not_accept_demo_or_unset() -> None:
    """The fail-closed arm must only match truthy spellings — demo / false /
    unset must NOT trip it (that would block every legitimate demo deploy)."""
    truthy = {"1", "true", "TRUE", "True", "yes", "YES", "Yes", "on", "ON", "On"}
    benign = {"", "0", "false", "FALSE", "False", "no", "NO", "demo", "DEMO"}
    arm = REAL_MONEY_CASE_ARM.rstrip(")")
    patterns = arm.split("|")
    for value in truthy:
        assert value in patterns, f"truthy {value!r} must trip the guard"
    for value in benign:
        assert value not in patterns, f"benign {value!r} must NOT trip the guard"


# --------------------------------------------------------------------------
# deploy-ci-3 : recovery verify block asserts the liquidation collector is up
# --------------------------------------------------------------------------


def test_recovery_enables_and_restarts_the_collector() -> None:
    """Sanity precondition for the finding: the recovery path DOES bring the
    always-on collector up, so the verify block owes it an is-active check."""
    text = _read(RECOVERY)
    assert f"systemctl enable {COLLECTOR}" in text
    assert f"systemctl restart {COLLECTOR}" in text


def test_recovery_verify_block_checks_collector_active_and_enabled() -> None:
    text = _read(RECOVERY)
    assert f"systemctl is-active --quiet {COLLECTOR}" in text, (
        "recovery verify must assert the liquidation collector is active "
        "(catches a crash-loop reaching 'failed')"
    )
    assert f"systemctl is-enabled --quiet {COLLECTOR}" in text, (
        "recovery verify must assert the liquidation collector is enabled"
    )


def test_recovery_collector_verify_is_in_the_post_settle_block_before_verify_ok() -> None:
    """The collector check must sit in the POST-settle verify block (after the
    sleep) and BEFORE 'deploy-verify-ok' is emitted — otherwise a broken
    collector still reaches the success message + Telegram."""
    text = _read(RECOVERY)
    # Post-settle block begins at the settle sleep guard.
    settle_idx = text.index('if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then')
    # Anchor on the actual success echo, NOT any mention of the string (the
    # deploy-ci-3 comment block also references 'deploy-verify-ok').
    verify_ok_idx = text.index('echo "deploy-verify-ok')
    is_active_idx = text.index(f"systemctl is-active --quiet {COLLECTOR}")
    is_enabled_idx = text.index(f"systemctl is-enabled --quiet {COLLECTOR}")
    assert settle_idx < is_active_idx < verify_ok_idx
    assert settle_idx < is_enabled_idx < verify_ok_idx


def test_recovery_collector_verify_matches_risk_service_pattern() -> None:
    """Parity check: the collector is verified the SAME way as the risk service
    (both is-active and is-enabled, --quiet), so the gate fails loud."""
    text = _read(RECOVERY)
    risk = "liquidity-migration-bybit-risk.service"
    for unit in (risk, COLLECTOR):
        assert re.search(
            rf"^\s*systemctl is-active --quiet {re.escape(unit)}\s*$", text, re.MULTILINE
        ), f"missing is-active --quiet for {unit}"
        assert re.search(
            rf"^\s*systemctl is-enabled --quiet {re.escape(unit)}\s*$", text, re.MULTILINE
        ), f"missing is-enabled --quiet for {unit}"


# --- relocated from tests/test_audit_int_iJ.py (audit integration bucket iJ) ---
# deploy-ci-4: the combined-book report systemd unit must keep compatibility
# roots out of the active report while every active sleeve root stays wired.
_REPORT_UNIT = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "systemd"
    / "liquidity-migration-combined-book-report.service"
)
_REPORT_TIMER = _REPORT_UNIT.with_suffix(".timer")
_LIVENESS_UNIT = _REPORT_UNIT.with_name("liquidity-migration-demo-liveness.service")
_RISK_UNIT = _REPORT_UNIT.with_name("liquidity-migration-bybit-risk.service")


def _report_unit_text() -> str:
    return _REPORT_UNIT.read_text(encoding="utf-8")


def test_telegram_report_is_hourly_and_operational_repeats_are_bounded() -> None:
    timer = _REPORT_TIMER.read_text(encoding="utf-8")
    liveness = _LIVENESS_UNIT.read_text(encoding="utf-8")
    risk = _RISK_UNIT.read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* *:05:00 UTC" in timer
    assert "--cooldown-min 360" in liveness
    assert "TELEGRAM_POSITION_LOSS_LEVELS=0.05,0.10,0.20,0.40" in risk


def test_report_unit_no_longer_wires_compatibility_short_data_root() -> None:
    """Compatibility short roots must not appear in the live report ExecStart."""
    text = _report_unit_text()
    assert "--short-data-root" not in text, (
        "report unit still wires --short-data-root for the compatibility root"
    )
    assert "bybit-demo-event" not in text, (
        "report unit still references the compatibility data root"
    )


def test_report_unit_still_wires_every_active_sleeve_root() -> None:
    """Long, continuous, paper, and hedge roots stay wired for the active books."""
    text = _report_unit_text()
    assert "combined-book-telegram-report" in text
    for arg in (
        "--long-data-root data/bybit-long-demo-event",
        "--continuous-data-root data/bybit-continuous-demo-event",
        "--continuous-paper-data-root data/bybit-continuous-paper-event",
        "--continuous-hedge-data-root data/bybit-continuous-hedge-event",
        "--include-live-positions",
    ):
        assert arg in text, f"report unit dropped an active-sleeve arg: {arg}"


def test_report_unit_execstart_still_well_formed() -> None:
    """The multi-line ExecStart continuation must stay intact after the edit:
    every line but the last in the ExecStart block ends with a backslash, and
    the unit still declares a single ExecStart."""
    lines = _report_unit_text().splitlines()
    exec_indices = [i for i, ln in enumerate(lines) if ln.startswith("ExecStart=")]
    assert len(exec_indices) == 1, "expected exactly one ExecStart in the report unit"

    start = exec_indices[0]
    # Walk the continued command; every continued line ends with a trailing '\'.
    i = start
    saw_continuation = False
    while lines[i].rstrip().endswith("\\"):
        saw_continuation = True
        i += 1
        assert i < len(lines), "ExecStart continuation runs off the end of the unit"
    assert saw_continuation, "ExecStart should span multiple continued lines"
    # The final command line of the block must not dangle a continuation.
    assert not lines[i].rstrip().endswith("\\")


# --- relocated from tests/test_audit_int_iM.py (audit bucket iM) ---------------
# deploy-env-timers-3: the continuous-PAPER systemd unit must stream its own
# kline pool. These tests pin that the follow override is absent, no paper
# Environment assignment points at the demo root, and the rest of the
# load-bearing paper config is undisturbed.
_PAPER_UNIT = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "systemd"
    / "liquidity-migration-bybit-continuous-paper.service"
)


def _paper_unit_text() -> str:
    return _PAPER_UNIT.read_text(encoding="utf-8")


def _paper_environment_assignments() -> dict[str, str]:
    """Parse the unit's active ``Environment=KEY=VALUE`` lines (skip comments)."""
    env: dict[str, str] = {}
    for raw in _paper_unit_text().splitlines():
        line = raw.strip()
        if line.startswith("#") or not line.startswith("Environment="):
            continue
        assignment = line[len("Environment=") :]
        key, _, value = assignment.partition("=")
        env[key] = value
    return env


def test_paper_unit_no_longer_follows_demo_kline_root() -> None:
    """deploy-env-timers-3: the PAPER shadow must not carry a KLINES_FOLLOW_ROOT
    override, so it always runs its own kline pool and never follows a frozen demo
    snapshot when CONTINUOUS_SLEEVE=off + CONTINUOUS_PAPER_SLEEVE=on."""
    env = _paper_environment_assignments()
    assert "KLINES_FOLLOW_ROOT" not in env, (
        "PAPER unit still sets KLINES_FOLLOW_ROOT — it would follow the demo "
        "kline store and freeze when the demo sleeve is toggled off"
    )


def test_paper_unit_environment_never_points_at_demo_root() -> None:
    """No active Environment= assignment in the PAPER unit may reference the demo
    data root: the shadow's market-data plane must be self-contained."""
    env = _paper_environment_assignments()
    offenders = {
        key: value
        for key, value in env.items()
        if "bybit-continuous-demo-event" in value
    }
    assert not offenders, (
        f"PAPER unit Environment assignments still point at the demo root: {offenders}"
    )


def test_paper_unit_keeps_its_own_paper_data_root() -> None:
    """The paper sleeve must still write/read its own dataset root so reconcile can
    pair it against the demo ledger — only the follow override was removed."""
    env = _paper_environment_assignments()
    assert env.get("DATA_ROOT") == "data/bybit-continuous-paper-event", (
        "PAPER unit lost or changed its own DATA_ROOT"
    )


def test_paper_unit_load_bearing_paper_knobs_intact() -> None:
    """Dropping the follow line must not disturb the knobs that make this a true
    no-submit shadow of the demo book (PAPER_MODE/dry-run routing + the mirrored
    strategy knobs)."""
    env = _paper_environment_assignments()
    for key, expected in (
        ("SUBMIT_ORDERS", "0"),
        ("RECORD_DRY_RUN", "1"),
        ("PAPER_MODE", "1"),
        ("STRATEGY_PROFILE", "continuous_ensemble_v2"),
        ("CONTINUOUS_SNIPER", "0"),
        ("LEFT_DECILE_EXIT_ENABLED", "0"),
        ("STOP_APPROACH_FRAC", "0"),
        ("FAILED_FADE_HOURS", "0"),
        ("BREAKEVEN_ARM_PCT", "0"),
    ):
        assert env.get(key) == expected, (
            f"PAPER unit knob {key} changed: expected {expected!r}, got {env.get(key)!r}"
        )


def test_paper_unit_documents_the_dropped_follow_override() -> None:
    """The removal is documented in-unit (audit id + rationale) so an operator
    re-adding the follow knob understands the demo-off hazard."""
    text = _paper_unit_text()
    assert "deploy-env-timers-3" in text, (
        "the dropped KLINES_FOLLOW_ROOT override should be documented with its audit id"
    )
