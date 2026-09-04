#!/usr/bin/env python3
"""Render `liquidity-migration-fleet.json`, the Grafana view of the minute samples.

The JSON next to this file is what gets imported; this script is where it is
written, so a panel is added in twenty lines of Python rather than two hundred
of hand-edited JSON. `--check` fails when the committed JSON is not what this
script renders, which is how the test keeps them together.

Every expression here charts a field `scripts/runtime/record_equity.py` pushes.
The realm variable is fed by `lm_engine_up`, so recorder panels, whose realm is
a venue, deliberately do not filter on it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

OUT = Path(__file__).with_name("liquidity-migration-fleet.json")

DS = {"type": "prometheus", "uid": "${DS_METRICS}"}
REALM = 'realm=~"$realm"'

Panel = dict[str, Any]


def _target(expr: str, legend: str, *, instant: bool = False, ref: str = "A") -> dict[str, Any]:
    return {
        "datasource": DS,
        "editorMode": "code",
        "expr": expr,
        "instant": instant,
        "legendFormat": legend,
        "range": not instant,
        "refId": ref,
    }


def _targets(rows: list[tuple[str, str]], *, instant: bool = False) -> list[dict[str, Any]]:
    return [
        _target(expr, legend, instant=instant, ref=chr(ord("A") + index)) for index, (expr, legend) in enumerate(rows)
    ]


def _grid(x: int, y: int, w: int, h: int) -> dict[str, int]:
    return {"x": x, "y": y, "w": w, "h": h}


def row(panel_id: int, title: str, y: int) -> Panel:
    return {
        "type": "row",
        "id": panel_id,
        "title": title,
        "collapsed": False,
        "gridPos": _grid(0, y, 24, 1),
        "panels": [],
    }


def timeseries(
    panel_id: int,
    title: str,
    description: str,
    grid: dict[str, int],
    rows: list[tuple[str, str]],
    *,
    unit: str = "short",
    decimals: int | None = None,
    points: bool = False,
    bars: bool = False,
    min_zero: bool = True,
    legend_table: bool = False,
    line_width: int = 2,
    fill_opacity: int | None = None,
    overrides: list[dict[str, Any]] | None = None,
) -> Panel:
    defaults: dict[str, Any] = {
        "color": {"mode": "palette-classic"},
        "unit": unit,
        "custom": {
            "drawStyle": "bars" if bars else "line",
            "lineWidth": line_width,
            "fillOpacity": fill_opacity if fill_opacity is not None else (60 if bars else 8),
            "showPoints": "always" if points else "never",
            "pointSize": 6 if points else 4,
            "spanNulls": False,
            "axisSoftMin": 0 if min_zero else None,
        },
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    legend = (
        {"displayMode": "table", "placement": "right", "showLegend": True, "calcs": ["lastNotNull", "max"]}
        if legend_table
        else {"displayMode": "list", "placement": "bottom", "showLegend": True, "calcs": ["lastNotNull"]}
    )
    return {
        "type": "timeseries",
        "id": panel_id,
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": grid,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {"legend": legend, "tooltip": {"mode": "multi", "sort": "desc"}},
        "targets": _targets(rows),
    }


def stat(
    panel_id: int,
    title: str,
    description: str,
    grid: dict[str, int],
    rows: list[tuple[str, str]],
    *,
    unit: str = "short",
    decimals: int | None = None,
    thresholds: list[tuple[str, float | None]] | None = None,
    mappings: dict[str, tuple[str, str]] | None = None,
) -> Panel:
    steps = [{"color": color, "value": value} for color, value in (thresholds or [("text", None)])]
    defaults: dict[str, Any] = {
        "color": {"mode": "thresholds"},
        "thresholds": {"mode": "absolute", "steps": steps},
        "unit": unit,
        "mappings": [],
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    if mappings:
        defaults["mappings"] = [
            {
                "type": "value",
                "options": {
                    key: {"text": text, "color": color, "index": index}
                    for index, (key, (text, color)) in enumerate(mappings.items())
                },
            }
        ]
    return {
        "type": "stat",
        "id": panel_id,
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": grid,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "colorMode": "background" if mappings else ("value" if thresholds else "none"),
            "graphMode": "none",
            "justifyMode": "center",
            "textMode": "value_and_name",
            "wideLayout": True,
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
        "targets": _targets(rows, instant=True),
    }


def bar_gauge(
    panel_id: int,
    title: str,
    description: str,
    grid: dict[str, int],
    rows: list[tuple[str, str]],
    *,
    unit: str = "percentunit",
    decimals: int = 0,
    thresholds: list[tuple[str, float | None]] | None = None,
) -> Panel:
    steps = [
        {"color": color, "value": value}
        for color, value in (thresholds or [("green", None), ("orange", 0.75), ("red", 1.0)])
    ]
    return {
        "type": "bargauge",
        "id": panel_id,
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": grid,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": steps},
                "unit": unit,
                "decimals": decimals,
                "min": 0,
            },
            "overrides": [],
        },
        "options": {
            "displayMode": "gradient",
            "orientation": "horizontal",
            "minVizHeight": 10,
            "minVizWidth": 0,
            "showUnfilled": True,
            "valueMode": "color",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
        "targets": _targets(rows, instant=True),
    }


def _increase(metric: str, *, window: str = "15m", realm: bool = True) -> str:
    selector = f"{{{REALM}}}" if realm else ""
    return f"increase({metric}{selector}[{window}])"


def _mainnet_axis() -> list[dict[str, Any]]:
    return [
        {
            "matcher": {"id": "byRegexp", "options": "/mainnet/"},
            "properties": [
                {"id": "custom.axisPlacement", "value": "right"},
                {"id": "custom.axisColorMode", "value": "series"},
                {"id": "custom.lineWidth", "value": 3},
                {"id": "color", "value": {"mode": "fixed", "fixedColor": "yellow"}},
            ],
        }
    ]


def panels() -> list[Panel]:
    out: list[Panel] = []
    y = 0

    out.append(
        stat(
            1,
            "Engines",
            "Current engine heartbeat state for each selected realm. Red means the minute sampler could not "
            "read a live engine heartbeat.",
            _grid(0, y, 6, 5),
            [(f"lm_engine_up{{{REALM}}}", "{{realm}}")],
            mappings={"0": ("DOWN", "red"), "1": ("ONLINE", "green")},
        )
    )
    out.append(
        stat(
            2,
            "New orders",
            "The effective account-level entry gate. Paused engines may still reduce or close exposure.",
            _grid(6, y, 6, 5),
            [(f"lm_engine_may_open{{{REALM}}}", "{{realm}}")],
            mappings={"0": ("PAUSED", "orange"), "1": ("OPEN", "green")},
        )
    )
    out.append(
        stat(
            3,
            "Signal workers",
            "The worker verdict and ticker coverage. Starting and bounded recovery count as healthy; a "
            "coverage miss remains visible beside the verdict.",
            _grid(12, y, 6, 5),
            [
                (f"lm_worker_status_healthy{{{REALM}}}", "{{realm}} verdict"),
                (f"lm_worker_ticker_coverage_complete{{{REALM}}}", "{{realm}} coverage"),
            ],
            mappings={"0": ("ATTENTION", "red"), "1": ("HEALTHY", "green")},
        )
    )
    out.append(
        stat(
            4,
            "Tape recorders",
            "The minute sampler can read each recorder's current status file. Detailed capacity and gap "
            "signals are below.",
            _grid(18, y, 6, 5),
            [("lm_recorder_up", "{{realm}}")],
            mappings={"0": ("DOWN", "red"), "1": ("RECORDING", "green")},
        )
    )
    y += 5

    out.append(
        timeseries(
            11,
            "Equity",
            "Current venue account equity. Demo uses the left axis and mainnet the right; both retain their "
            "real USDT values but scale independently so each account's movement stays readable.",
            _grid(0, y, 12, 8),
            [(f"lm_engine_equity_usdt{{{REALM}}}", "{{realm}}")],
            unit="currencyUSD",
            decimals=2,
            min_zero=False,
            legend_table=True,
            fill_opacity=14,
            overrides=_mainnet_axis(),
        )
    )
    out.append(
        timeseries(
            12,
            "Open exposure",
            "Current absolute entry notional across open positions. Demo uses the left axis and mainnet the "
            "right; both retain their real USDT values but scale independently. This is exposure, not margin "
            "or PnL.",
            _grid(12, y, 12, 8),
            [(f"lm_engine_position_entry_notional_usdt{{{REALM}}}", "{{realm}}")],
            unit="currencyUSD",
            decimals=2,
            legend_table=True,
            fill_opacity=14,
            overrides=_mainnet_axis(),
        )
    )
    y += 8

    out.append(row(30, "Execution", y))
    y += 1
    out.append(
        timeseries(
            31,
            "Execution activity · 15m",
            "New orders, fills, and recovered private-stream resets in each 15-minute window. Since-boot "
            "counters are read as increases so a restart does not draw a false cliff.",
            _grid(0, y, 10, 9),
            [
                (_increase("lm_engine_orders_sent"), "{{realm}} orders"),
                (_increase("lm_engine_fills"), "{{realm}} fills"),
                (_increase("lm_engine_stream_resets"), "{{realm}} stream resets"),
            ],
            decimals=0,
            bars=True,
        )
    )
    out.append(
        timeseries(
            32,
            "Order-path latency · p99",
            "Measured order-path windows only. The emphasized end-to-end line contains decision, durability, "
            "dispatch, venue work, and the return to the strategy core.",
            _grid(10, y, 14, 9),
            [
                (f"lm_engine_end_to_end_p99_ns{{{REALM}}} / 1000000", "{{realm}} · end-to-end"),
                (f"lm_engine_ack_p99_ns{{{REALM}}} / 1000000", "{{realm}} · venue ack"),
                (f"lm_engine_durable_p99_ns{{{REALM}}} / 1000000", "{{realm}} · durable"),
                (f"lm_engine_decide_p99_ns{{{REALM}}} / 1000000", "{{realm}} · decide"),
            ],
            unit="ms",
            decimals=2,
            points=True,
            legend_table=True,
            fill_opacity=4,
            overrides=[
                {
                    "matcher": {"id": "byRegexp", "options": "/end-to-end/"},
                    "properties": [
                        {"id": "custom.lineWidth", "value": 4},
                        {"id": "custom.fillOpacity", "value": 18},
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}},
                    ],
                }
            ],
        )
    )
    y += 9

    out.append(row(40, "Data pipeline", y))
    y += 1
    out.append(
        timeseries(
            41,
            "Data freshness",
            "Age of the account reading, the worker's last market frame, any open repair gap, and each "
            "recorder's last received frame. Low and flat is healthy.",
            _grid(0, y, 10, 8),
            [
                (f"lm_engine_account_age_ms{{{REALM}}}", "engine {{realm}} · account"),
                (f"lm_worker_ws_last_frame_age_ms{{{REALM}}}", "worker {{realm}} · market frame"),
                (f"lm_worker_ws_gap_age_ms{{{REALM}}}", "worker {{realm}} · repair gap"),
                ("lm_recorder_receive_age_ms", "recorder {{realm}} · market frame"),
            ],
            unit="ms",
            legend_table=True,
        )
    )
    out.append(
        bar_gauge(
            42,
            "Pipeline pressure",
            "Current worker queues, durable spool, recorder queue, and projected monthly traffic. Orange means "
            "headroom is shrinking; red means the cap is near.",
            _grid(10, y, 7, 8),
            [
                (f"lm_worker_ws_queue_fill{{{REALM}}}", "{{realm}} · WebSocket queue"),
                (f"lm_worker_spool_file_fill{{{REALM}}}", "{{realm}} · spool files"),
                (f"lm_worker_spool_byte_fill{{{REALM}}}", "{{realm}} · spool bytes"),
                (
                    "sum by (realm) (last_over_time(lm_recorder_projected_month_gb[24h])) / "
                    "sum by (realm) (last_over_time(lm_recorder_monthly_gb[24h]))",
                    "recorder {{realm}} · monthly traffic",
                ),
                ("lm_recorder_queue_fill", "recorder {{realm}} · writer queue"),
            ],
            thresholds=[("green", None), ("orange", 0.75), ("red", 0.9)],
        )
    )
    out.append(
        timeseries(
            43,
            "Recorder faults · 1h",
            "Disconnected shards now, plus dropped frames and reconnects during the last hour. Every value "
            "should be zero.",
            _grid(17, y, 7, 8),
            [
                ("lm_recorder_shards - lm_recorder_shards_connected", "{{realm}} · shards down"),
                (_increase("lm_recorder_dropped_frames", window="1h", realm=False), "{{realm}} · dropped"),
                (
                    _increase("lm_recorder_disk_dropped_frames", window="1h", realm=False),
                    "{{realm}} · disk drops",
                ),
                (_increase("lm_recorder_reconnects", window="1h", realm=False), "{{realm}} · reconnects"),
            ],
            decimals=0,
            bars=True,
        )
    )
    return out


def dashboard() -> dict[str, Any]:
    return {
        "title": "liquidity-migration fleet",
        "uid": "liqmig-fleet",
        "description": (
            "The funded and demo engines, signal workers, and tape recorders, sampled once a minute by "
            "scripts/runtime/record_equity.py. The JSONL under /var/lib/liquidity-migration/equity on the host "
            "is the record; this is a view. Rendered by deploy/grafana/render_dashboard.py."
        ),
        "tags": ["liquidity-migration"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 4,
        "editable": True,
        "graphTooltip": 1,
        "refresh": "1m",
        "time": {"from": "now-6h", "to": "now"},
        "templating": {
            "list": [
                {
                    "name": "DS_METRICS",
                    "label": "Metrics source",
                    "type": "datasource",
                    "query": "prometheus",
                    "current": {"text": "grafanacloud-proudtortoise1017-prom", "value": "grafanacloud-prom"},
                    "hide": 2,
                    "refresh": 1,
                },
                {
                    "name": "realm",
                    "label": "Realm",
                    "type": "query",
                    "datasource": DS,
                    "query": "label_values(lm_engine_up, realm)",
                    "current": {"text": "All", "value": "$__all"},
                    "includeAll": True,
                    "multi": True,
                    "refresh": 2,
                    "sort": 1,
                },
            ]
        },
        "annotations": {"list": []},
        "panels": panels(),
    }


def render() -> str:
    return json.dumps(dashboard(), indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed JSON differs from the render")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != text:
            print(f"{args.out} is stale: run {Path(__file__).name} to re-render it", file=sys.stderr)
            return 1
        print(f"{args.out} matches the render")
        return 0
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(dashboard()['panels'])} panels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
