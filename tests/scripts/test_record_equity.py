"""Deployment and dashboard contracts for the Rust fleet sampler."""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from liquidity_migration.policy.realms import realms as realm_table

ROOT = Path(__file__).resolve().parents[2]
RUST_SOURCE = ROOT / "engine/engine-tools/src/equity_recorder.rs"
ORACLE = ROOT / "engine/engine-tools/tests/fixtures/equity_recorder_oracle.json"
SYSTEMD = ROOT / "deploy/systemd"
MANIFEST = ROOT / "deploy/fleet_manifest.tsv"


def test_sample_freshness_uses_the_deployed_liveness_limits() -> None:
    spec = importlib.util.spec_from_file_location("equity_liveness", ROOT / "scripts/runtime/check_fleet_liveness.py")
    assert spec and spec.loader
    liveness = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = liveness
    spec.loader.exec_module(liveness)
    limits = json.loads(ORACLE.read_text())["freshness_limits_ms"]
    defaults = liveness.build_arg_parser().parse_args([])
    assert limits["engine"] == limits["worker"] == defaults.max_heartbeat_age_sec * 1_000
    unit = (SYSTEMD / "liquidity-migration-host-liveness.service").read_text().replace("\\\n", " ")
    command = next(line.removeprefix("ExecStart=") for line in unit.splitlines() if line.startswith("ExecStart="))
    args = liveness.build_arg_parser().parse_args(shlex.split(command)[2:])
    assert limits["recorder"] == args.max_heartbeat_age_sec * 1_000


def test_the_recorder_unit_is_sandboxed_and_holds_no_venue_credentials() -> None:
    unit = (SYSTEMD / "liquidity-migration-equity-recorder.service").read_text(encoding="utf-8")

    assert "User=liquidity-observer" in unit
    assert "Group=liquidity-migration" in unit
    for setting in (
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "ProtectProc=invisible",
        "StateDirectory=liquidity-migration/equity",
        "ReadWritePaths=/var/lib/liquidity-migration/equity",
    ):
        assert setting in unit, setting
    assert (
        "ExecStart=/opt/liquidity-migration-engine/bin/engine-tools record-equity "
        "--manifest /opt/liquidity-migration/deploy/fleet_manifest.tsv"
    ) in unit
    # Stronger than unsetting keys it was handed: it is handed none. Every
    # bybit-*.env carries live account credentials.
    assert "bybit" not in unit
    assert unit.count("EnvironmentFile=") == 1
    assert "EnvironmentFile=-/etc/liquidity-migration/observability.env" in unit


def test_the_timer_samples_every_minute_and_never_replays_a_missed_one() -> None:
    timer = (SYSTEMD / "liquidity-migration-equity-recorder.timer").read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* *:*:20" in timer
    assert "Persistent=false" in timer
    assert "WantedBy=timers.target" in timer

    row = next(
        line
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.startswith("liquidity-migration-equity-recorder.timer|")
    ).split("|")
    assert row[11] == "60" and row[12] == "60", "the manifest cadence must match OnCalendar"


def test_the_sink_variables_are_documented_and_templated() -> None:
    template = (ROOT / "deploy" / "observability.env.template").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    script = RUST_SOURCE.read_text(encoding="utf-8")
    for key in ("METRICS_PUSH_URL", "METRICS_PUSH_USER", "METRICS_PUSH_TOKEN"):
        assert f"{key}=" in template, key
        assert key in doc, key
        assert key in script, key
    # A token in a template is a token in git.
    assert "METRICS_PUSH_TOKEN=\n" in template


def _dashboard() -> dict[str, Any]:
    return json.loads((ROOT / "deploy" / "grafana" / "liquidity-migration-fleet.json").read_text(encoding="utf-8"))


def _expressions(dashboard: dict[str, Any]) -> list[str]:
    return [str(target.get("expr", "")) for panel in dashboard["panels"] for target in panel.get("targets", [])]


def test_the_dashboard_json_is_what_its_renderer_renders() -> None:
    spec = importlib.util.spec_from_file_location(
        "render_dashboard", ROOT / "deploy" / "grafana" / "render_dashboard.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    committed = (ROOT / "deploy" / "grafana" / "liquidity-migration-fleet.json").read_text(encoding="utf-8")
    assert module.render() == committed, "run deploy/grafana/render_dashboard.py and commit the JSON"


def test_the_dashboard_defaults_to_the_fleet_prometheus_source() -> None:
    dashboard = _dashboard()
    metrics_source = dashboard["templating"]["list"][0]
    assert metrics_source["name"] == "DS_METRICS"
    assert metrics_source["current"] == {
        "text": "grafanacloud-proudtortoise1017-prom",
        "value": "grafanacloud-prom",
    }


def test_worker_health_display_requires_a_live_source() -> None:
    panel = next(panel for panel in _dashboard()["panels"] if panel["title"] == "Signal workers")
    expressions = [target["expr"] for target in panel["targets"]]
    assert len(expressions) == 2
    assert all('* on(realm) lm_worker_up{realm=~"$realm"}' in expression for expression in expressions)


def test_account_cards_show_stale_instead_of_last_known_account_values() -> None:
    panels = {panel["id"]: panel for panel in _dashboard()["panels"]}
    for panel_id, realm in ((11, "demo"), (12, "demo"), (13, "mainnet"), (14, "mainnet")):
        panel = panels[panel_id]
        up = f'lm_engine_up{{realm="{realm}",realm=~"$realm"}}'
        expression = panel["targets"][0]["expr"]
        assert f"and on(realm) ({up} == 1)" in expression
        assert expression.endswith(f"or on(realm) (({up} == 0) / 0)")
        assert panel["options"]["reduceOptions"]["calcs"] == ["last"]
        assert panel["fieldConfig"]["defaults"]["mappings"] == [
            {"type": "special", "options": {"match": "null+nan", "result": {"text": "STALE", "color": "orange"}}}
        ]
    assert 'and on(realm) (lm_engine_up{realm=~"$realm"} == 1)' in panels[2]["targets"][0]["expr"]


def test_the_dashboard_charts_only_fields_the_sampler_actually_pushes() -> None:
    import re

    expressions = " ".join(_expressions(_dashboard()))
    cases = json.loads(ORACLE.read_text())["cases"]
    lines = {case["kind"]: case["line_protocol"] for case in cases if case["name"] == "all_fields"}
    assert set(lines) == {"engine", "worker", "recorder"}
    fields = {
        kind: {pair.split("=", 1)[0] for pair in line.split(" ")[1].split(",")}
        for kind, line in lines.items()
    }
    engine_fields, recorder_fields, worker_fields = (fields[kind] for kind in ("engine", "recorder", "worker"))

    charted_engine = set(re.findall(r"lm_engine_([a-z0-9_]+)", expressions))
    charted_recorder = set(re.findall(r"lm_recorder_([a-z0-9_]+)", expressions))
    charted_worker = set(re.findall(r"lm_worker_([a-z0-9_]+)", expressions))
    assert charted_engine <= engine_fields, sorted(charted_engine - engine_fields)
    assert charted_recorder <= recorder_fields, sorted(charted_recorder - recorder_fields)
    assert charted_worker <= worker_fields, sorted(charted_worker - worker_fields)
    for field in (
        "equity_usdt",
        "position_entry_notional_usdt",
        "may_open",
        "end_to_end_p99_ns",
        "ack_p99_ns",
        "durable_p99_ns",
        "decide_p99_ns",
        "end_to_end_p999_ns",
        "ack_p999_ns",
        "durable_p999_ns",
        "decide_p999_ns",
    ):
        assert field in charted_engine, field
    for field in ("projected_month_gb", "dropped_frames", "reconnects", "queue_fill"):
        assert field in charted_recorder, field
    for field in ("status_healthy", "ticker_coverage_complete", "ws_last_frame_age_ms", "spool_byte_fill"):
        assert field in charted_worker, field


def test_stats_read_the_instant_and_sparklines_read_the_range() -> None:
    dashboard = _dashboard()
    stats = [panel for panel in dashboard["panels"] if panel["type"] == "stat"]
    assert stats
    for panel in stats:
        sparkline = panel["options"]["graphMode"] == "area"
        for target in panel["targets"]:
            if panel["id"] in {11, 12, 13, 14} and target["refId"] in {"B", "C"}:
                assert target.get("instant") is True, panel["title"]
                assert target.get("range") is False, panel["title"]
                continue
            assert target.get("instant") is not sparkline, panel["title"]
            assert target.get("range") is sparkline, panel["title"]
    # A since-boot counter drawn raw is a cliff at every restart; the view
    # reads them as increases so a restart is a flat line.
    for counter in (
        "lm_engine_fills",
        "lm_engine_stream_resets",
        "lm_recorder_dropped_frames",
        "lm_recorder_reconnects",
    ):
        assert f"increase({counter}" in " ".join(_expressions(dashboard)), counter
    # The realm variable is fed by lm_engine_up; worker realms match it, while
    # recorder realms are venues and a realm filter would hide every recorder.
    for expr in _expressions(dashboard):
        if "lm_recorder_" in expr:
            assert "$realm" not in expr, expr


def test_account_cards_isolate_demo_and_mainnet_sparklines() -> None:
    dashboard = _dashboard()
    by_id = {panel["id"]: panel for panel in dashboard["panels"]}
    for panel_id, label, x, y in (
        (11, "D · Equity", 0, 5),
        (13, "M · Equity", 12, 5),
        (12, "D · OI", 0, 9),
        (14, "M · OI", 12, 9),
    ):
        panel = by_id[panel_id]
        assert panel["type"] == "stat"
        assert panel["title"] == ""
        assert panel["options"]["graphMode"] == "area"
        assert panel["options"]["textMode"] == "value_and_name"
        assert panel["options"]["justifyMode"] == "auto"
        assert panel["options"]["orientation"] == "horizontal"
        assert panel["options"]["text"] == {"titleSize": 14, "valueSize": 14}
        assert panel["options"]["colorMode"] == "value"
        assert panel["gridPos"] == {"x": x, "y": y, "w": 12, "h": 4}
        assert panel["targets"][0]["legendFormat"] == label
        assert [target["legendFormat"] for target in panel["targets"]] == [label, "Min", "Max"]
        assert "min_over_time" in panel["targets"][1]["expr"]
        assert "max_over_time" in panel["targets"][2]["expr"]
        assert [transform["options"]["mappings"][1]["handlerKey"] for transform in panel["transformations"]] == [
            "min",
            "max",
        ]
        assert all(transform["options"]["applyTo"]["options"] == label for transform in panel["transformations"])


def test_time_series_legends_do_not_squeeze_the_plots() -> None:
    dashboard = _dashboard()
    for panel in dashboard["panels"]:
        if panel["type"] != "timeseries":
            continue
        legend = panel["options"]["legend"]
        assert legend["displayMode"] == "list"
        assert legend["placement"] == "bottom"
        assert legend["calcs"] == []


def test_tape_loss_is_a_five_minute_time_series_for_both_venues() -> None:
    dashboard = _dashboard()
    panel = next(panel for panel in dashboard["panels"] if panel["title"] == "Tape loss · 5m")
    expressions = [target["expr"] for target in panel["targets"]]

    assert panel["type"] == "timeseries"
    assert panel["gridPos"] == {"x": 15, "y": 24, "w": 9, "h": 8}
    assert all("[5m]" in expression for expression in expressions)
    assert "lm_recorder_dropped_frames" in expressions[0]
    assert "lm_recorder_disk_dropped_frames" in expressions[0]
    assert "lm_recorder_reconnects" in expressions[1]
    names = {
        property_["value"]
        for override in panel["fieldConfig"]["overrides"]
        for property_ in override["properties"]
        if property_["id"] == "displayName"
    }
    assert names == {"BN loss", "BY loss", "BN gap", "BY gap"}


def test_execution_activity_legend_uses_plain_metric_names() -> None:
    dashboard = _dashboard()
    panel = next(panel for panel in dashboard["panels"] if panel["title"] == "Execution activity · 15m")
    assert panel["type"] == "stat"
    assert panel["options"]["graphMode"] == "area"
    names = {
        property_["value"]
        for override in panel["fieldConfig"]["overrides"]
        for property_ in override["properties"]
        if property_["id"] == "displayName"
    }
    assert names == {"D orders", "M orders", "D fills", "M fills", "D resets", "M resets"}


def test_the_realm_variable_lists_only_the_realms_the_table_holds_running() -> None:
    # A stopped realm pushes up=0 every minute by design; the view is not where
    # that is read, so its card is not a red DOWN beside the running fleet.
    running = [row.realm for row in realm_table() if row.posture == "running"]
    variable = next(item for item in _dashboard()["templating"]["list"] if item["name"] == "realm")
    assert variable["regex"] == "/^(" + "|".join(running) + ")$/"
    assert variable["allValue"] == "|".join(running)
    for row in realm_table():
        if row.posture != "running":
            assert row.realm not in variable["regex"]
            assert row.realm not in variable["allValue"]
