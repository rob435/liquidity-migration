"""The minute sampler that gives the fleet an equity history, and its unit."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "runtime" / "record_equity.py"
SYSTEMD = ROOT / "deploy" / "systemd"
MANIFEST = ROOT / "deploy" / "fleet_manifest.tsv"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("record_equity", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["record_equity"] = module
    spec.loader.exec_module(module)
    return module


record_equity = _module()


def _heartbeat(now_ms: int, **overrides: Any) -> dict[str, Any]:
    beat = {
        "wall_ts_ms": now_ms - 3_000,
        "account_observed_wall_ts_ms": now_ms - 4_000,
        "account_equity_usdt": 130.28,
        "account_available_usdt": 118.29,
        "may_open": True,
        "mode": "live",
        "realm": "mainnet",
        "venue": "bybit",
        "engine_commit": "14383fd5",
        "rolling_loss_net_usdt": 1.59,
        "rolling_loss_limit_usdt": 13.0,
        "rolling_loss_tripped": False,
        "positions": [
            {"symbol": "NEARUSDT", "side": "long", "qty": 20.7, "entry_px": 2.0, "strategy": "long"},
            {"symbol": "AAVEUSDT", "side": "short", "qty": -1.0, "entry_px": 100.0, "strategy": "carry"},
        ],
        "strategies": ["carry", "long", "exodus"],
        "strategy_entries_enabled": [
            {"strategy": "carry", "entries_enabled": True},
            {"strategy": "long", "entries_enabled": True},
            {"strategy": "exodus", "entries_enabled": False},
        ],
        "entry_blockers": [{"strategy": "long", "symbol": "NEARUSDT", "reason": "inside_resize_band"}],
        "strategy_errors": [],
        "working_entries": [{"strategy": "carry", "symbol": "AAVEUSDT"}],
        "pending_flatten_requests": [],
        "decide_p50_ns": 41_000,
        "decide_p99_ns": 90_000,
        "durable_p99_ns": 2_100_000,
        "wire_p99_ns": 310_000,
        "ack_p99_ns": 48_000_000,
        "end_to_end_p50_ns": 44_000_000,
        "end_to_end_p99_ns": 51_000_000,
        "quota_hold_p99_ns": None,
    }
    beat.update(overrides)
    return beat


def _worker_heartbeat(now_ms: int, **overrides: Any) -> dict[str, Any]:
    beat = {
        "updated_at_ms": now_ms - 2_000,
        "status": "starting",
        "source_generation": "g123",
        "last_long_cycle_completed_wall_ts_ms": now_ms - 5_000,
        "last_carry_cycle_completed_wall_ts_ms": None,
        "rest_ticker_success_count": 3,
        "rest_ticker_failure_count": 1,
        "bybit_ws_connected": True,
        "bybit_ws_gap_open": True,
        "bybit_ws_gap_open_since_wall_ts_ms": now_ms - 10_000,
        "bybit_ws_last_frame_ts_ms": now_ms - 100,
        "bybit_ws_ticker_rows": 171,
        "bybit_ws_ticker_capacity": 171,
        "bybit_ws_ticker_coverage_complete": True,
        "bybit_ws_ticker_topics_accepted": 171,
        "bybit_ws_ticker_topics_quarantined": 0,
        "bybit_ws_kline_topics_accepted": 171,
        "bybit_ws_kline_topics_quarantined": 0,
        "bybit_ws_queued_frames": 2,
        "bybit_ws_queue_capacity": 342,
        "spool_files": 1,
        "spool_bytes": 120_981,
        "spool_file_cap": 4_096,
        "spool_byte_cap": 2_147_483_648,
        "spool_backpressured": False,
        "spool_backpressured_classes": [],
        "replaceable_outputs_coalesced": 7,
    }
    beat.update(overrides)
    return beat


def test_realms_and_paths_come_from_the_fleet_manifest() -> None:
    sources = record_equity.read_sources(MANIFEST)
    engines = {source.realm: source.path for source in sources if source.kind == "engine"}
    assert engines == {
        "demo": Path("/var/lib/liquidity-migration-engine/heartbeat.json"),
        "mainnet": Path("/var/lib/liquidity-migration-engine-mainnet/heartbeat.json"),
    }
    recorders = {source.realm for source in sources if source.kind == "recorder"}
    assert recorders == {"bybit", "binance"}
    workers = {source.realm: source.path for source in sources if source.kind == "worker"}
    assert workers == {
        "demo": Path("/var/lib/liquidity-migration-signal-worker-demo/heartbeat.json"),
        "mainnet": Path("/var/lib/liquidity-migration-signal-worker-mainnet/heartbeat.json"),
    }


def test_a_live_heartbeat_becomes_the_numbers_a_curve_needs(tmp_path: Path) -> None:
    now_ms = 1_788_000_000_000
    beat = tmp_path / "heartbeat.json"
    beat.write_text(json.dumps(_heartbeat(now_ms)), encoding="utf-8")

    sample = record_equity.engine_sample("mainnet", beat, now_ms)

    assert sample["state"] == "live"
    assert sample["equity_usdt"] == 130.28
    assert sample["available_usdt"] == 118.29
    assert sample["heartbeat_age_ms"] == 3_000
    assert sample["account_age_ms"] == 4_000
    assert sample["position_count"] == 2
    # Absolute exposure at entry: a short counts as size held, not negative.
    assert sample["position_entry_notional_usdt"] == 141.4
    assert sample["sleeve_positions"] == {"carry": 1, "long": 1, "exodus": 0}
    assert sample["may_open"] == 1.0
    assert sample["rolling_loss_tripped"] == 0.0
    assert sample["entry_blockers"] == 1
    assert sample["strategy_errors"] == 0


def test_a_hand_position_the_engine_does_not_own_is_still_counted_once(tmp_path: Path) -> None:
    # The heartbeat writes `strategy: null` for exposure no single sleeve owns.
    # Recording it as `unattributed` keeps the account's total honest without
    # crediting a sleeve that does not hold it.
    now_ms = 1_788_000_000_000
    beat = tmp_path / "heartbeat.json"
    positions = [{"symbol": "1000PEPEUSDT", "side": "long", "qty": 100.0, "entry_px": 0.004, "strategy": None}]
    beat.write_text(json.dumps(_heartbeat(now_ms, positions=positions)), encoding="utf-8")

    sample = record_equity.engine_sample("mainnet", beat, now_ms)

    assert sample["sleeve_positions"] == {"carry": 0, "long": 0, "exodus": 0, "unattributed": 1}
    assert sample["position_count"] == 1


def test_every_configured_sleeve_is_a_series_even_while_it_holds_nothing(tmp_path: Path) -> None:
    # Exodus holds nothing most of the month. A chart that only shows sleeves
    # with positions cannot show that; a zero can.
    now_ms = 1_788_000_000_000
    beat = tmp_path / "heartbeat.json"
    beat.write_text(json.dumps(_heartbeat(now_ms)), encoding="utf-8")

    sample = record_equity.engine_sample("mainnet", beat, now_ms)
    line = record_equity.line_protocol(sample)

    assert sample["sleeve_entries_enabled"] == {"carry": 1, "long": 1, "exodus": 0}
    assert sample["sleeve_blockers"] == {"carry": 0, "long": 1, "exodus": 0}
    for field in (
        "sleeve_exodus_positions=0.0",
        "sleeve_exodus_entries_enabled=0.0",
        "sleeve_long_entries_enabled=1.0",
        "sleeve_long_blockers=1.0",
        "sleeve_exodus_blockers=0.0",
    ):
        assert f",{field}," in f",{line.split(' ')[1]},", field


def test_the_order_path_steps_pass_through_and_an_empty_window_is_absent_not_zero(tmp_path: Path) -> None:
    now_ms = 1_788_000_000_000
    beat = tmp_path / "heartbeat.json"
    beat.write_text(json.dumps(_heartbeat(now_ms)), encoding="utf-8")

    sample = record_equity.engine_sample("mainnet", beat, now_ms)
    fields = dict(pair.split("=", 1) for pair in record_equity.line_protocol(sample).split(" ")[1].split(","))

    assert sample["decide_p99_ns"] == 90_000.0
    assert sample["end_to_end_p99_ns"] == 51_000_000.0
    assert sample["working_entries"] == 1
    assert sample["pending_flatten_requests"] == 0
    assert fields["ack_p99_ns"] == "48000000.0"
    # The ledger wrote null: nothing was measured, and nothing must be charted.
    assert sample["quota_hold_p99_ns"] is None
    assert "quota_hold_p99_ns" not in fields
    assert "wire_p50_ns" not in fields
    assert set(record_equity.ORDER_PATH_FIELDS) <= set(sample)


def test_a_realm_with_no_heartbeat_is_recorded_and_pushed_as_down(tmp_path: Path) -> None:
    # The whole point of the file: an engine that was down for two hours has
    # 120 lines saying so. A curve that simply stops cannot be told apart from
    # a sampler that stopped, locally or in the remote.
    now_ms = 1_788_000_000_000
    sample = record_equity.engine_sample("demo", tmp_path / "gone.json", now_ms)

    assert sample == {"ts_ms": now_ms, "realm": "demo", "kind": "engine", "state": "absent"}
    line = record_equity.line_protocol(sample)
    assert line.startswith("lm_engine,realm=demo up=0")
    assert line.endswith(str(now_ms * 1_000_000))


def test_an_unparsable_heartbeat_is_a_sample_not_a_crash(tmp_path: Path) -> None:
    beat = tmp_path / "heartbeat.json"
    beat.write_text("{half a li", encoding="utf-8")

    sample = record_equity.engine_sample("mainnet", beat, 1_788_000_000_000)

    assert sample["state"] == "unparsable"
    assert sample["error"]
    assert record_equity.line_protocol(sample).count("up=0") == 1


def test_every_live_sample_field_becomes_one_metric_field(tmp_path: Path) -> None:
    now_ms = 1_788_000_000_000
    beat = tmp_path / "heartbeat.json"
    beat.write_text(json.dumps(_heartbeat(now_ms)), encoding="utf-8")

    line = record_equity.line_protocol(record_equity.engine_sample("mainnet", beat, now_ms))
    head, fields, stamp = line.split(" ")

    assert head == "lm_engine,realm=mainnet"
    assert stamp == str(now_ms * 1_000_000)
    keys = {pair.split("=", 1)[0] for pair in fields.split(",")}
    assert {"up", "equity_usdt", "available_usdt", "position_count", "may_open"} <= keys
    # Strings are tags or dropped; a field must always parse as a number.
    for pair in fields.split(","):
        float(pair.split("=", 1)[1])


def test_samples_land_in_one_monthly_file_per_realm_and_append(tmp_path: Path) -> None:
    stamp = int(time.mktime((2026, 9, 3, 23, 15, 0, 0, 0, 0)) * 1000)
    first = {"ts_ms": stamp, "realm": "mainnet", "kind": "engine", "state": "live", "equity_usdt": 1.0}
    second = {**first, "ts_ms": stamp + 60_000, "equity_usdt": 2.0}

    path = record_equity.append(tmp_path, first)
    assert record_equity.append(tmp_path, second) == path
    assert path.name.startswith("engine-mainnet-2026-09")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["equity_usdt"] for line in lines] == [1.0, 2.0]


def test_a_sample_too_large_to_append_atomically_is_refused(tmp_path: Path) -> None:
    # One append is one line and this is the only writer, which is only safe
    # while the line stays under the atomic write size.
    huge = {
        "ts_ms": 1_788_000_000_000,
        "realm": "mainnet",
        "kind": "engine",
        "state": "live",
        "error": "x" * 5_000,
    }
    try:
        record_equity.append(tmp_path, huge)
    except ValueError as error:
        assert "append cap" in str(error)
    else:
        raise AssertionError("an oversized sample must be refused, not silently torn")


def test_tag_values_with_line_protocol_metacharacters_are_escaped() -> None:
    sample = {"ts_ms": 1, "realm": "one two,three=four", "kind": "engine", "state": "live"}
    line = record_equity.line_protocol(sample)
    assert r"realm=one\ two\,three\=four" in line


def test_realm_is_the_only_tag_so_one_realm_is_one_series_through_an_outage() -> None:
    # A tag that changes value starts a new series. Tagging `state`, or a
    # `venue` only known while the engine is up, would split a realm's history
    # in two at the moment it went down -- the moment the history is for.
    now_ms = 1_788_000_000_000
    down = record_equity.engine_sample("mainnet", Path("/nonexistent"), now_ms)
    up_head = "lm_engine,realm=mainnet"
    assert record_equity.line_protocol(down).split(" ")[0] == up_head

    beat = Path(__file__).parent / "does-not-exist"
    assert not beat.exists()
    live = {
        "ts_ms": now_ms,
        "realm": "mainnet",
        "kind": "engine",
        "state": "live",
        "venue": "bybit",
        "mode": "live",
        "equity_usdt": 130.0,
    }
    assert record_equity.line_protocol(live).split(" ")[0] == up_head


def test_the_recorder_sample_carries_the_budget_and_the_drops(tmp_path: Path) -> None:
    now_ms = 1_788_000_000_000
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "venue": "bybit",
                "recorded_at_ns": (now_ms - 1_000) * 1_000_000,
                "last_receive_ns": (now_ms - 500) * 1_000_000,
                "received_frames": 12_566_753,
                "written_rows": 13_825_572,
                "dropped_frames": 0,
                "disk_dropped_frames": 0,
                "queued_frames": 2,
                "snapshot_failures": 0,
                "free_disk_bytes": 54_422_888_448,
                "disk_blocked": False,
                "queue_capacity": 131072,
                "budget": {"projected_month_gb": 1670.2, "monthly_gb": 2400.0, "over": False, "shed": []},
                "shards": [
                    {"index": 0, "connected": True, "reconnects": 2},
                    {"index": 1, "connected": True, "reconnects": 499},
                    {"index": 2, "connected": False, "reconnects": 0},
                ],
                "bytes": {"received_24h": 53_045_936_545, "window_seconds": 60_197},
            }
        ),
        encoding="utf-8",
    )

    sample = record_equity.recorder_sample("bybit", status, now_ms)

    assert sample["state"] == "live"
    assert sample["projected_month_gb"] == 1670.2
    assert sample["queue_capacity"] == 131072.0
    assert sample["queue_fill"] == round(2 / 131072, 6)
    assert sample["shards"] == 3
    assert sample["shards_connected"] == 2
    assert sample["reconnects"] == 501
    assert sample["bytes_24h"] == 53_045_936_545.0
    assert sample["disk_blocked"] == 0.0
    assert sample["monthly_gb"] == 2400.0
    assert sample["budget_over"] == 0.0
    assert sample["shed_feeds"] == 0
    assert sample["status_age_ms"] == 1_000
    assert sample["receive_age_ms"] == 500
    line = record_equity.line_protocol(sample)
    assert line.startswith("lm_recorder,realm=bybit ")
    assert ",up=1.0," in line


def test_the_worker_sample_carries_the_verdict_and_the_supporting_facts(tmp_path: Path) -> None:
    now_ms = 1_788_000_000_000
    beat = tmp_path / "heartbeat.json"
    beat.write_text(json.dumps(_worker_heartbeat(now_ms)), encoding="utf-8")

    sample = record_equity.worker_sample("mainnet", beat, now_ms)

    assert sample["state"] == "live"
    assert sample["heartbeat_age_ms"] == 2_000
    assert sample["status_healthy"] == 1.0
    assert sample["status_ready"] == 0.0
    assert sample["status_starting"] == 1.0
    assert sample["status_recovering"] == 0.0
    assert sample["ws_connected"] == 1.0
    assert sample["ws_gap_open"] == 1.0
    assert sample["ws_gap_age_ms"] == 10_000
    assert sample["ws_last_frame_age_ms"] == 100
    assert sample["ticker_coverage_complete"] == 1.0
    assert sample["ticker_topics_accepted"] == 171.0
    assert sample["kline_topics_accepted"] == 171.0
    assert sample["long_cycle_age_ms"] == 5_000
    assert sample["carry_cycle_age_ms"] is None
    assert sample["ws_queue_fill"] == round(2 / 342, 6)
    assert sample["spool_backpressured"] == 0.0
    assert sample["spool_byte_fill"] == round(120_981 / 2_147_483_648, 6)
    line = record_equity.line_protocol(sample)
    assert line.startswith("lm_worker,realm=mainnet ")
    assert ",up=1.0," in line

    beat.write_text(json.dumps(_worker_heartbeat(now_ms, status="recovering")), encoding="utf-8")
    recovering = record_equity.worker_sample("mainnet", beat, now_ms)
    assert recovering["status_healthy"] == 1.0
    assert recovering["status_recovering"] == 1.0


def test_a_run_with_no_sink_configured_records_and_exits_zero(tmp_path: Path, capsys, monkeypatch) -> None:
    for key in ("METRICS_PUSH_URL", "METRICS_PUSH_USER", "METRICS_PUSH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        "# fleet-manifest-v2\n"
        "liquidity-migration-engine-mainnet.service|service|mainnet|owner|20|mainnet|funded|-|"
        f"active|{tmp_path / 'absent.json'}|-|-|-|-|-|-\n",
        encoding="utf-8",
    )
    state = tmp_path / "equity"

    code = record_equity.main(["--state-dir", str(state), "--manifest", str(manifest)])

    assert code == 0
    assert "no metrics sink configured" in capsys.readouterr().out
    written = list(state.glob("engine-mainnet-*.jsonl"))
    assert len(written) == 1
    assert json.loads(written[0].read_text(encoding="utf-8").splitlines()[0])["state"] == "absent"


def test_a_failed_push_warns_and_still_exits_zero(tmp_path: Path, capsys, monkeypatch) -> None:
    # The remote is a view. Losing it must never cost the local record or
    # leave a oneshot unit in `failed`, which the host watchdog would page on.
    monkeypatch.setenv("METRICS_PUSH_URL", "http://127.0.0.1:1/write")
    monkeypatch.setenv("METRICS_PUSH_USER", "12345")
    monkeypatch.setenv("METRICS_PUSH_TOKEN", "token")
    monkeypatch.setattr(
        record_equity,
        "push",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("connection refused")),
    )
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        "# fleet-manifest-v2\n"
        "liquidity-migration-engine.service|service|demo|owner|10|always|direct|-|"
        f"active|{tmp_path / 'absent.json'}|-|-|-|-|-|-\n",
        encoding="utf-8",
    )
    state = tmp_path / "equity"

    code = record_equity.main(["--state-dir", str(state), "--manifest", str(manifest)])

    assert code == 0
    assert "WARNING: metrics push failed" in capsys.readouterr().err
    assert list(state.glob("engine-demo-*.jsonl"))


def test_the_curve_shows_the_range_and_names_the_gap(tmp_path: Path) -> None:
    base = 1_788_000_000_000
    for index in range(10):
        sample: dict[str, Any] = {
            "ts_ms": base + index * 60_000,
            "realm": "mainnet",
            "kind": "engine",
            "state": "absent" if index in (4, 5) else "live",
        }
        if sample["state"] == "live":
            sample.update({"equity_usdt": 100.0 + index, "available_usdt": 90.0, "position_count": 1, "may_open": 1.0})
        record_equity.append(tmp_path, sample)

    curve = record_equity.render_curve(tmp_path, "mainnet", 240)

    assert "samples=10" in curve
    assert "equity 100.00 .. 109.00 USDT" in curve
    assert "net +9.00" in curve
    assert "2 of 10 samples had no live heartbeat" in curve


def test_an_empty_state_directory_says_so_rather_than_failing(tmp_path: Path) -> None:
    assert "no equity samples yet" in record_equity.render_curve(tmp_path, "mainnet", 60)


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
    assert "ExecStart=/opt/liquidity-migration/.venv/bin/python scripts/runtime/record_equity.py" in unit
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
    script = SCRIPT.read_text(encoding="utf-8")
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


def test_the_dashboard_charts_only_fields_the_sampler_actually_pushes(tmp_path: Path) -> None:
    import re

    dashboard = _dashboard()
    expressions = " ".join(_expressions(dashboard))
    now_ms = 1_788_000_000_000
    beat = tmp_path / "heartbeat.json"
    # A heartbeat with every field present, so the set of series the sampler
    # can produce is the full one.
    counters = {
        "uptime_s": 1,
        "market_events": 1,
        "orders_sent": 1,
        "fills": 1,
        "stream_resets": 0,
        "amends_confirmed": 0,
        "amends_pulled_unconfirmed": 0,
        "rolling_loss_trades": 0,
        "venue_clock_offset_ms": 1,
        "fills_maker_share": 1.0,
        "fill_all_in_arrival_bps": 1.0,
        "fill_arrival_shortfall_bps": 1.0,
        "fill_fee_coverage": 1.0,
        "fill_markout_1m_our_way_bps": 1.0,
    }
    ledger = {key: 1 for key in record_equity.ORDER_PATH_FIELDS}
    beat.write_text(json.dumps(_heartbeat(now_ms, **counters, **ledger)), encoding="utf-8")
    engine_line = record_equity.line_protocol(record_equity.engine_sample("mainnet", beat, now_ms))
    engine_fields = {pair.split("=", 1)[0] for pair in engine_line.split(" ")[1].split(",")}
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "venue": "bybit",
                "recorded_at_ns": now_ms * 1_000_000,
                "last_receive_ns": now_ms * 1_000_000,
                "received_frames": 1,
                "written_rows": 1,
                "queued_frames": 0,
                "dropped_frames": 0,
                "disk_dropped_frames": 0,
                "snapshot_failures": 0,
                "free_disk_bytes": 1,
                "queue_capacity": 1,
                "budget": {"projected_month_gb": 1.0, "monthly_gb": 2.0, "over": False, "shed": []},
                "shards": [{"connected": True, "reconnects": 0}],
                "bytes": {"received_24h": 1},
            }
        ),
        encoding="utf-8",
    )
    recorder_line = record_equity.line_protocol(record_equity.recorder_sample("bybit", status, now_ms))
    recorder_fields = {pair.split("=", 1)[0] for pair in recorder_line.split(" ")[1].split(",")}
    worker_beat = tmp_path / "worker-heartbeat.json"
    worker_beat.write_text(
        json.dumps(_worker_heartbeat(now_ms, last_carry_cycle_completed_wall_ts_ms=now_ms - 6_000)),
        encoding="utf-8",
    )
    worker_line = record_equity.line_protocol(record_equity.worker_sample("mainnet", worker_beat, now_ms))
    worker_fields = {pair.split("=", 1)[0] for pair in worker_line.split(" ")[1].split(",")}

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
    ):
        assert field in charted_engine, field
    for field in ("projected_month_gb", "dropped_frames", "reconnects", "queue_fill"):
        assert field in charted_recorder, field
    for field in ("status_healthy", "ticker_coverage_complete", "ws_gap_age_ms", "spool_byte_fill"):
        assert field in charted_worker, field


def test_status_panels_read_the_instant_and_counters_are_charted_as_increases() -> None:
    dashboard = _dashboard()
    stats = [panel for panel in dashboard["panels"] if panel["type"] == "stat"]
    assert stats
    for panel in stats:
        for target in panel["targets"]:
            assert target.get("instant") is True, panel["title"]
            assert target.get("range") is False, panel["title"]
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


def test_account_charts_give_mainnet_an_independent_axis() -> None:
    dashboard = _dashboard()
    by_title = {panel["title"]: panel for panel in dashboard["panels"]}
    for title in ("Equity", "Open exposure"):
        overrides = by_title[title]["fieldConfig"]["overrides"]
        mainnet = next(override for override in overrides if override["matcher"]["options"] == "/mainnet/")
        properties = {row["id"]: row["value"] for row in mainnet["properties"]}
        assert properties["custom.axisPlacement"] == "right"
