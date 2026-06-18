from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

DEPLOY_SH = Path(__file__).resolve().parents[1] / "scripts" / "deploy_vps_live.sh"


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
    assert "--strategy-profile \"$STRATEGY_PROFILE\"" in text
    assert 'LEFT_DECILE_EXIT_ENABLED="${LEFT_DECILE_EXIT_ENABLED:-0}"' in text
    assert 'STOP_APPROACH_FRAC="${STOP_APPROACH_FRAC:-0}"' in text
    assert "--stop-approach-frac \"$STOP_APPROACH_FRAC\"" in text
    assert "--failed-fade-hours \"$FAILED_FADE_HOURS\"" in text
    assert "--breakeven-arm-pct \"$BREAKEVEN_ARM_PCT\"" in text
    assert 'SIZING_MODE="${SIZING_MODE:-inverse_vol}"' in text
    assert 'TARGET_VOL_PER_NAME="${TARGET_VOL_PER_NAME:-0.01}"' in text
    assert 'VOL_WEIGHT_CLAMP="${VOL_WEIGHT_CLAMP:-2}"' in text
    assert "--sizing-mode \"$SIZING_MODE\"" in text
    assert "--target-vol-per-name \"$TARGET_VOL_PER_NAME\"" in text
    assert "--vol-weight-clamp \"$VOL_WEIGHT_CLAMP\"" in text
    assert 'DAILY_REBALANCE_ENABLED="${DAILY_REBALANCE_ENABLED:-1}"' in text
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
    assert '"--root", paper' in text
    assert '"--trades-dataset", "continuous_fade_paper_trades"' in text
    assert '"reconcile-continuous-paper-demo"' not in text


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
        assert "Environment=DAILY_REBALANCE_ENABLED=1" in text
        assert "Environment=DAILY_REBALANCE_TARGET_DAILY_VOL=0.045" in text
        assert "Environment=DAILY_REBALANCE_MAX_SCALE=4" in text
        assert "Environment=DAILY_REBALANCE_STRATEGY_MOMENTUM_WINDOW_DAYS=0" in text
        assert "Environment=SIZING_MODE=inverse_vol" in text
        assert "Environment=TARGET_VOL_PER_NAME=0.01" in text
        assert "Environment=VOL_WEIGHT_CLAMP=2" in text
    demo_text = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-continuous-demo.service").read_text(encoding="utf-8")
    paper_text = (repo / "deploy" / "systemd" / "liquidity-migration-bybit-continuous-paper.service").read_text(encoding="utf-8")
    # sniper armed on the DEMO unit only (paper is a no-order shadow)
    assert "Environment=CONTINUOUS_SNIPER=1" in demo_text
    assert "CONTINUOUS_SNIPER=1" not in paper_text
    # 2f hedge submit armed (demo-only; runner enforces demo credentials + confirm flag)
    hedge_text = (repo / "deploy" / "systemd" / "liquidity-migration-continuous-hedge.service").read_text(encoding="utf-8")
    assert "Environment=HEDGE_MODE=2f" in hedge_text
    assert "Environment=SUBMIT_HEDGE=1" in hedge_text
    assert "Environment=CONFIRM_DEMO_ORDERS=1" in hedge_text


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


def _active_lines(unit_text: str) -> list[str]:
    """Non-comment, non-blank directive lines of a systemd unit."""
    return [ln.strip() for ln in unit_text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_continuous_paper_unit_streams_its_own_kline_plane() -> None:
    """audit2: since 7d39d61 the paper unit no longer FOLLOWS the demo kline plane —
    KLINES_FOLLOW_ROOT was dropped so the shadow stays live even when the demo (leader)
    sleeve is off. Guard against a regression that re-adds an ACTIVE follow directive
    (the old string survives only in an explanatory comment), and against the optional
    drop-in mechanism being deleted. Both continuous units still pin their threadpools."""
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


def test_combined_book_report_includes_continuous_roots_and_sleeve_toggles() -> None:
    repo = Path(__file__).resolve().parents[1]
    service = (repo / "deploy" / "systemd" / "liquidity-migration-combined-book-report.service").read_text(
        encoding="utf-8"
    )

    assert "EnvironmentFile=-/opt/liquidity-migration/deploy/sleeves.env" in service
    assert "EnvironmentFile=-/etc/liquidity-migration/sleeves.env" in service
    # deploy-ci-4: the daily-SHORT sleeve was ERASED 2026-06-11, so the report
    # unit no longer wires its --short-data-root (the erased root). Guard against
    # the dead-sleeve arg drifting back in.
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
    specifically (unlike the short reconciler which reads the same dataset
    name from both roots); without PAPER_MODE=1 the long-paper service
    writes to long_native_demo_trades and reconciliation silently returns
    paired=0."""
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
    """All four demo services (short+long, demo+paper) must enable the WS
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
    # The daily-short sleeve was erased (2026-06-11): the deploy gate now pins the
    # LONG profile (the one promoted sleeve) instead of the old short scenario ids.
    assert "long_cfg.universe_size == 50" in text
    assert "long_cfg.weekend_size_mult == 1.5" in text
    assert "TELEGRAM_CHAT_ID" in text
    assert "bybit-demo.env.backup" in text
    assert "sed -i \"s/^TELEGRAM_CHAT_ID=" in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    lib = (repo / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8")
    assert "apply_timer_enable" in text
    assert "systemctl disable --now" in lib
    # 2026-06-09 audit: the model050426 retired-unit cleanup/assertions were removed —
    # the 2026-06-04 box (116.202.15.128) is a fresh host that never ran those units.
    assert "model050426" not in text
    # Erased daily-short sleeve: the deploy actively removes the retired units.
    assert "RETIRED_SLEEVE_UNITS" in text
    assert "liquidity-migration-bybit-risk.service" in text
    # The risk service has NO sleeve toggle — it is the shared reconcile authority for
    # all three sleeves and must always enable/restart/verify regardless of which
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
    for sleeve in ("LONG", "CONTINUOUS", "CONTINUOUS_PAPER"):
        assert f'apply_sleeve_enable "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
        assert f'verify_sleeve "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
    # The canonical unit set (what each sleeve enables/restarts/verifies, and what the
    # liveness watchdog/recovery must bring up) lives in the lib — pin it there.
    lib = (repo / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8")
    assert 'RETIRED_SLEEVE_UNITS="liquidity-migration-bybit-demo.service liquidity-migration-bybit-paper.service"' in lib
    assert 'LONG_SLEEVE_UNITS="liquidity-migration-bybit-long-demo.service liquidity-migration-bybit-long-paper.service"' in lib
    assert 'CONTINUOUS_SLEEVE_UNITS="liquidity-migration-bybit-continuous-demo.service"' in lib
    assert 'CONTINUOUS_PAPER_SLEEVE_UNITS="liquidity-migration-bybit-continuous-paper.service"' in lib
    assert 'CONTINUOUS_HEDGE_TIMERS="liquidity-migration-continuous-hedge.timer"' in lib
    assert "apply_timer_enable()" in lib
    assert "verify_timer()" in lib
    assert "continuous_rmom_refresh_on()" in lib
    sleeves = (repo / "deploy" / "sleeves.env").read_text(encoding="utf-8")
    # Continuous demo orders are ON; long is controlled by its own sleeve toggle.
    assert "CONTINUOUS_SLEEVE=on" in sleeves
    assert "CONTINUOUS_PAPER_SLEEVE=on" in sleeves
    # Timers ship with the unit files but `systemctl enable` is required to
    # actually schedule them. Pin both timers so a deploy can't silently leave
    # the demo-health watchdog or daily combined-book report inactive.
    assert "systemctl enable --now liquidity-migration-demo-liveness.timer" in text
    assert "systemctl enable --now liquidity-migration-combined-book-report.timer" in text
    assert "systemctl is-enabled --quiet liquidity-migration-demo-liveness.timer" in text
    assert "systemctl is-enabled --quiet liquidity-migration-combined-book-report.timer" in text
    assert "systemctl is-active --quiet liquidity-migration-demo-liveness.timer" in text
    assert "systemctl is-active --quiet liquidity-migration-combined-book-report.timer" in text
    assert "Environment=ORDER_SUBMIT_MODE=ws_then_rest" in text
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
    assert 'apply_timer_enable "$_hedge_timer_state" $CONTINUOUS_HEDGE_TIMERS' in text
    assert 'verify_timer "$_hedge_timer_state" $CONTINUOUS_HEDGE_TIMERS' in text
    assert "data/bybit-continuous-hedge-event" in text  # the open-hedge ledger check
    assert "_hedge_timer_state=on" in text              # fail-safe keeps it enabled while open
    assert "continuous_rmom_refresh_on" in text
    assert "Environment=LONG_DATA_ROOT=data/bybit-long-demo-event" in text
    assert "Environment=CONTINUOUS_DATA_ROOT=data/bybit-continuous-demo-event" in text
    assert "Environment=SUBMIT_ORDERS=1" in text
    assert "Environment=SIZING_MODE=inverse_vol" in text
    assert "Environment=TARGET_VOL_PER_NAME=0.01" in text
    assert "Environment=VOL_WEIGHT_CLAMP=2" in text
    assert "Environment=DAILY_REBALANCE_TARGET_DAILY_VOL=0.045" in text
    assert "Environment=DAILY_REBALANCE_MAX_SCALE=4" in text
    assert 'cont.sizing_mode == "inverse_vol"' in text
    assert "cont.target_vol_per_name == 0.01" in text
    assert "cont.daily_rebalance_target_daily_vol == 0.045" in text
    assert "Environment=STOP_LOSS_PCT=0" in text
    # The deploy must SEED the rmom gate (start the oneshot service), not just enable the
    # daily timer — else a fresh deploy starts the continuous daemon into an empty gate and
    # blacks out until 00:20 UTC (the 2026-06-02 incident). Seed must run BEFORE the
    # continuous daemon restart so the parquet exists when it starts; empty-gate WARN is fail-safe.
    assert "systemctl start liquidity-migration-continuous-rmom-refresh.service" in text
    first_continuous_restart = min(
        text.index("systemctl restart liquidity-migration-bybit-continuous-demo.service"),
        text.index("systemctl restart liquidity-migration-bybit-continuous-paper.service"),
    )
    assert text.index("systemctl start liquidity-migration-continuous-rmom-refresh.service") < first_continuous_restart
    assert "rmom gate is EMPTY after seed" in text
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
    assert "--property=Environment" not in text
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
    reference actually collects. (The 2026-06-11 short-sleeve erasure left the
    recovery script pinning two deleted node-ids; only deploy was scanned then.)"""
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
    # 2026-06-09 audit: retired-unit (model050426) checks removed — fresh 2026-06-04 box.
    assert "model050426" not in text
    # 2026-06-11 erasure: verify must pin the SURVIVING configs and must NOT import
    # the erased short engine — a dead import made every verify invocation fail.
    assert "liquidity_migration.volume_events" not in text
    assert "_demo_event_config" not in text
    assert "_v11a_long_native_config" in text
    assert "ContinuousDemoCycleConfig" in text
    assert "TELEGRAM_CHAT_ID" in text
    assert "SYSTEMD_SETTLE_SECONDS" in text
    assert "Environment=ORDER_SUBMIT_MODE=ws_then_rest" in text
    # Per-sleeve kill-switch: verify is toggle-aware — it sources the shared lib, loads
    # the toggles, and routes per-sleeve active+enabled checks through verify_sleeve (so
    # an intentionally-off sleeve is required DOWN, not flagged as a failed deploy). The
    # risk service is NOT toggled — always verified up (it protects every sleeve).
    assert "lib_sleeves.sh" in text
    assert "lm_load_sleeve_toggles" in text
    assert "systemctl is-enabled --quiet liquidity-migration-bybit-risk.service" in text
    assert "systemctl is-active --quiet liquidity-migration-bybit-risk.service" in text
    for sleeve in ("LONG", "CONTINUOUS", "CONTINUOUS_PAPER"):
        assert f'verify_sleeve "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
    # The exact unit set each sleeve must bring up is pinned in the shared lib, so a
    # regression that stops/disables a sleeve's daemon still fails verify.
    lib = (repo / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8")
    assert 'LONG_SLEEVE_UNITS="liquidity-migration-bybit-long-demo.service liquidity-migration-bybit-long-paper.service"' in lib
    assert 'CONTINUOUS_SLEEVE_UNITS="liquidity-migration-bybit-continuous-demo.service"' in lib
    assert 'CONTINUOUS_PAPER_SLEEVE_UNITS="liquidity-migration-bybit-continuous-paper.service"' in lib
    assert 'CONTINUOUS_HEDGE_TIMERS="liquidity-migration-continuous-hedge.timer"' in lib
    # Read-only verify must catch a missing-timer regression that the deploy
    # script would have caused — parity check, no-write semantics.
    assert "systemctl is-enabled --quiet liquidity-migration-demo-liveness.timer" in text
    assert "systemctl is-enabled --quiet liquidity-migration-combined-book-report.timer" in text
    assert "systemctl is-active --quiet liquidity-migration-demo-liveness.timer" in text
    assert "systemctl is-active --quiet liquidity-migration-combined-book-report.timer" in text
    # Continuous-fade sleeve (live on demo 2026-06-01): its daily rmom-refresh timer is
    # verified only when the sleeve is on (guarded by sleeve_on); the daemon's own
    # active+enabled state is covered by the verify_sleeve loop above. The risk service
    # stays wired to read the continuous ledger even when the sleeve is off (asserted
    # below, unconditional) — else its open positions would silently flatten.
    assert "continuous_rmom_refresh_on" in text
    assert "verify_timer on $CONTINUOUS_SLEEVE_TIMERS" in text
    assert "CONTINUOUS_FORWARD_REPORT_TIMERS" not in text
    assert 'verify_timer "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS' in text
    assert "Environment=LONG_DATA_ROOT=data/bybit-long-demo-event" in text
    assert "Environment=CONTINUOUS_DATA_ROOT=data/bybit-continuous-demo-event" in text
    assert "Environment=SUBMIT_ORDERS=1" in text
    assert "Environment=SIZING_MODE=inverse_vol" in text
    assert "Environment=TARGET_VOL_PER_NAME=0.01" in text
    assert "Environment=VOL_WEIGHT_CLAMP=2" in text
    assert "Environment=DAILY_REBALANCE_TARGET_DAILY_VOL=0.045" in text
    assert "Environment=DAILY_REBALANCE_MAX_SCALE=4" in text
    assert 'cont.sizing_mode == "inverse_vol"' in text
    assert "cont.target_vol_per_name == 0.01" in text
    assert "cont.daily_rebalance_target_daily_vol == 0.045" in text
    assert "Environment=STOP_LOSS_PCT=0" in text
    assert "verify-ok commit=" in text
    assert "--property=Environment" not in text


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
    # 2026-06-11 erasure: recovery pins the SURVIVING configs, never the erased engine.
    assert "liquidity_migration.volume_events" not in text
    assert "_demo_event_config" not in text
    assert "_v11a_long_native_config" in text
    assert "ContinuousDemoCycleConfig" in text
    lib = (repo / "deploy" / "lib_sleeves.sh").read_text(encoding="utf-8")
    assert "apply_timer_enable" in text
    assert "systemctl disable --now" in lib
    # 2026-06-09 audit: retired-unit (model050426) cleanup removed — fresh 2026-06-04 box.
    assert "model050426" not in text
    # Erased daily-short sleeve: the deploy actively removes the retired units.
    assert "RETIRED_SLEEVE_UNITS" in text
    assert "liquidity-migration-bybit-risk.service" in text
    # Recovery routes sleeve enable/restart/verify through the SAME kill-switch as
    # deploy_vps_live.sh (single source of truth) — NO hardcoded per-sleeve enables that
    # could resurrect an OFF sleeve (e.g. the look-ahead-disabled continuous sleeve).
    assert "lib_sleeves.sh" in text
    assert "lm_load_sleeve_toggles" in text
    assert "systemctl enable liquidity-migration-bybit-risk.service" in text
    assert "systemctl restart liquidity-migration-bybit-risk.service" in text
    assert "systemctl is-enabled --quiet liquidity-migration-bybit-risk.service" in text
    for sleeve in ("LONG", "CONTINUOUS", "CONTINUOUS_PAPER"):
        assert f'apply_sleeve_enable "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
        assert f'verify_sleeve "${sleeve}_SLEEVE" ${sleeve}_SLEEVE_UNITS' in text
    # The continuous rmom timer + its go-live asserts are gated behind the toggle, so a
    # recovery with CONTINUOUS_SLEEVE=off cannot bring the disabled sleeve back.
    assert "continuous_rmom_refresh_on" in text
    assert "Environment=ORDER_SUBMIT_MODE=ws_then_rest" in text
    # Continuous-fade sleeve (live on demo 2026-06-01): brought up like the other
    # live daemons, plus its rmom timer; risk service wired to read its ledger.
    assert "liquidity-migration-bybit-continuous-demo.service" in text
    assert 'CONTINUOUS_SLEEVE_TIMERS="liquidity-migration-continuous-rmom-refresh.timer"' in lib
    assert 'apply_timer_enable "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS' in text
    assert 'verify_timer "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS' in text
    assert "Environment=LONG_DATA_ROOT=data/bybit-long-demo-event" in text
    assert "Environment=CONTINUOUS_DATA_ROOT=data/bybit-continuous-demo-event" in text
    assert "Environment=SUBMIT_ORDERS=1" in text
    assert "Environment=SIZING_MODE=inverse_vol" in text
    assert "Environment=TARGET_VOL_PER_NAME=0.01" in text
    assert "Environment=VOL_WEIGHT_CLAMP=2" in text
    assert "Environment=DAILY_REBALANCE_TARGET_DAILY_VOL=0.045" in text
    assert "Environment=DAILY_REBALANCE_MAX_SCALE=4" in text
    assert 'cont.sizing_mode == "inverse_vol"' in text
    assert "cont.target_vol_per_name == 0.01" in text
    assert "cont.daily_rebalance_target_daily_vol == 0.045" in text
    assert "Environment=STOP_LOSS_PCT=0" in text
    assert "deploy-verify-ok commit=" in text
    assert "--property=Environment" not in text


def test_reset_demo_paper_ledgers_archives_then_wipes_only_ledgers(tmp_path: Path) -> None:
    """scripts/reset_demo_paper_ledgers.sh must (a) preview without touching
    anything under --dry-run, and (b) archive-then-wipe ONLY the trade/order/
    cycle ledgers while preserving the WS kline stores (wiping those would force
    a slow multi-day re-bootstrap)."""
    import shutil
    import subprocess

    if shutil.which("bash") is None:
        pytest.skip("bash unavailable")

    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "reset_demo_paper_ledgers.sh"

    # Synthetic repo root: the safety check requires liquidity_migration/ + data/.
    (tmp_path / "liquidity_migration").mkdir()
    ledger = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_trades"
    cycles = tmp_path / "data" / "bybit-long-demo-event" / "long_native_demo_cycles"
    klines = tmp_path / "data" / "bybit-long-demo-event" / "event_demo_klines_1h"  # must survive
    for d in (ledger, cycles, klines):
        d.mkdir(parents=True)
        (d / "part.parquet").write_bytes(b"x")

    # --dry-run touches nothing.
    dry = subprocess.run(
        ["bash", str(script), "--dry-run"], cwd=tmp_path, capture_output=True, text=True
    )
    assert dry.returncode == 0, dry.stderr
    assert "long_native_demo_trades" in dry.stdout
    assert ledger.exists() and cycles.exists() and klines.exists()
    assert not (tmp_path / "data" / "_archive").exists()

    # Real run: archive created, ledgers gone, kline store preserved.
    real = subprocess.run(
        ["bash", str(script)], cwd=tmp_path, capture_output=True, text=True
    )
    assert real.returncode == 0, real.stderr
    assert not ledger.exists(), "trade ledger must be wiped"
    assert not cycles.exists(), "cycle ledger must be wiped"
    assert klines.exists(), "WS kline store must be preserved"
    archives = list((tmp_path / "data" / "_archive").glob("ledger-reset-*.tar.gz"))
    assert len(archives) == 1, f"expected one archive tarball, got {archives}"


def test_reset_demo_paper_ledgers_covers_continuous_sleeve(tmp_path: Path) -> None:
    """The continuous-fade sleeve is still covered by the reset —
    omitting it would leave stale continuous trades that contaminate the clean pre/post
    forward-demo split on a strategy overhaul (audit 2026-06-02 #10)."""
    import shutil
    import subprocess

    if shutil.which("bash") is None:
        pytest.skip("bash unavailable")

    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "reset_demo_paper_ledgers.sh"

    (tmp_path / "liquidity_migration").mkdir()
    cont = tmp_path / "data" / "bybit-continuous-demo-event" / "continuous_fade_demo_trades"
    cont_klines = tmp_path / "data" / "bybit-continuous-demo-event" / "continuous_fade_demo_klines_1h"
    for d in (cont, cont_klines):
        d.mkdir(parents=True)
        (d / "part.parquet").write_bytes(b"x")

    real = subprocess.run(["bash", str(script)], cwd=tmp_path, capture_output=True, text=True)
    assert real.returncode == 0, real.stderr
    assert not cont.exists(), "continuous trade ledger must be wiped"
    assert cont_klines.exists(), "continuous WS kline store must be preserved"


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

    # Daemon-mode wrappers exec the stub and return immediately; the legacy
    # single-cycle loop (USE_DAEMON=0, the long paper unit) loops forever, so a
    # timeout there is expected — the first iteration already captured argv.
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


def test_deploy_refuses_real_money_env() -> None:
    # deploy-ci-6: the deploy must fail-closed if the sourced env sets REAL_MONEY truthy.
    txt = DEPLOY_SH.read_text()
    assert "REAL_MONEY" in txt
    assert "Refusing deploy: REAL_MONEY" in txt


def test_deploy_keeps_hedge_timer_when_continuous_off_but_leg_open() -> None:
    # deploy-env-timers-1: the gating must NOT be a bare apply_timer_enable on
    # CONTINUOUS_SLEEVE; it must consult the hedge ledger and keep the timer enabled
    # while an open hedge leg exists.
    txt = DEPLOY_SH.read_text()
    assert "_hedge_timer_state" in txt
    assert "bybit-continuous-hedge-event" in txt
    # The verify side must mirror the apply side, not raw CONTINUOUS_SLEEVE.
    assert 'verify_timer "$_hedge_timer_state" $CONTINUOUS_HEDGE_TIMERS' in txt
    assert 'verify_timer "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS' not in txt


def test_deploy_warns_on_paper_following_frozen_demo_root() -> None:
    # deploy-env-timers-3: demo-off + paper-on must warn that the paper shadow follows
    # a frozen demo kline store.
    txt = DEPLOY_SH.read_text()
    assert "KLINES_FOLLOW_ROOT" in txt
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
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
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
    subprocess.run(["bash", "-c", f"systemctl enable {unit}"], env=env, check=True)
    assert (state / f"{unit}.enabled").exists()
    assert not (state / f"{unit}.active").exists(), "bare enable must NOT start the unit"
    # enable --now and start DO mark active.
    subprocess.run(["bash", "-c", f"systemctl start {unit}"], env=env, check=True)
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
# deploy-ci-4: the combined-book report systemd unit once wired
# ``--short-data-root data/bybit-demo-event`` for the daily-SHORT sleeve that was
# ERASED 2026-06-11. These guard that the dead-sleeve concept stays out of the
# live report unit while every surviving sleeve root stays wired.
_REPORT_UNIT = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "systemd"
    / "liquidity-migration-combined-book-report.service"
)


def _report_unit_text() -> str:
    return _REPORT_UNIT.read_text(encoding="utf-8")


def test_report_unit_no_longer_wires_erased_short_data_root() -> None:
    """deploy-ci-4: the erased daily-SHORT sleeve's root must not appear in the
    live combined-book report ExecStart. Both the flag and the erased root path
    are gone, so a reader/operator can't mistake a dead short book for a reported
    one."""
    text = _report_unit_text()
    assert "--short-data-root" not in text, (
        "report unit still wires --short-data-root for the erased daily-SHORT sleeve"
    )
    assert "bybit-demo-event" not in text, (
        "report unit still references the erased daily-SHORT data root"
    )


def test_report_unit_still_wires_every_surviving_sleeve_root() -> None:
    """Dropping the short arg must not disturb the surviving sleeves: long demo,
    continuous demo, continuous paper, and the continuous hedge ledger all stay
    wired so the daily aggregate keeps covering the live books."""
    text = _report_unit_text()
    assert "combined-book-telegram-report" in text
    for arg in (
        "--long-data-root data/bybit-long-demo-event",
        "--continuous-data-root data/bybit-continuous-demo-event",
        "--continuous-paper-data-root data/bybit-continuous-paper-event",
        "--continuous-hedge-data-root data/bybit-continuous-hedge-event",
        "--include-live-positions",
    ):
        assert arg in text, f"report unit dropped a surviving-sleeve arg: {arg}"


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
# deploy-env-timers-3: the continuous-PAPER systemd unit hard-coded
# ``Environment=KLINES_FOLLOW_ROOT=data/bybit-continuous-demo-event`` so the paper
# shadow followed the demo (leader) sleeve's flushed kline snapshot read-only to
# halve WS decode CPU. That follow is only safe while the demo sleeve is ON. The
# documented-valid ``CONTINUOUS_SLEEVE=off`` + ``CONTINUOUS_PAPER_SLEEVE=on`` combo
# stops the demo daemon and freezes its kline store, leaving the shadow following a
# stale snapshot for up to ~2 days while it appears healthy. The robust completion
# drops the follow override from the PAPER unit so the shadow always streams its own
# kline pool. These tests pin that the override is gone, that no reference to the
# demo root survives in the paper unit's environment, and that the rest of the
# (load-bearing) paper config is undisturbed.
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
