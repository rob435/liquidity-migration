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
    show_legend: bool = True,
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
    legend = {"displayMode": "list", "placement": "bottom", "showLegend": show_legend, "calcs": []}
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
    overrides: list[dict[str, Any]] | None = None,
    sparkline: bool = False,
    text_mode: str = "value_and_name",
    justify_mode: str = "center",
    orientation: str = "auto",
    text: dict[str, int] | None = None,
    color_mode: str | None = None,
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
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "colorMode": color_mode or ("background" if mappings else ("value" if thresholds else "none")),
            "graphMode": "area" if sparkline else "none",
            "justifyMode": justify_mode,
            "orientation": orientation,
            "text": text or {},
            "textMode": text_mode,
            "wideLayout": True,
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
        "targets": _targets(rows, instant=not sparkline),
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
    overrides: list[dict[str, Any]] | None = None,
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
            "overrides": overrides or [],
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


def _names(rows: list[tuple[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "matcher": {"id": "byRegexp", "options": f"/^{source}$/"},
            "properties": [{"id": "displayName", "value": display}],
        }
        for source, display in rows
    ]


def _name_colors(rows: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "matcher": {"id": "byRegexp", "options": f"/^{source}$/"},
            "properties": [
                {"id": "displayName", "value": display},
                {"id": "color", "value": {"mode": "fixed", "fixedColor": color}},
            ],
        }
        for source, display, color in rows
    ]


def _padded_sparkline_bounds(selector: str) -> tuple[str, str]:
    low = f"min_over_time({selector}[$__range])"
    high = f"max_over_time({selector}[$__range])"
    pad = f"(({high}) - ({low})) * 0.10 + ({high}) * 0.01"
    return f"clamp_min(({low}) - ({pad}), 0)", f"({high}) + ({pad})"


def _config_from_query(ref_id: str, field_name: str, handler: str, apply_to: str) -> dict[str, Any]:
    return {
        "id": "configFromData",
        "options": {
            "configRefId": ref_id,
            "applyTo": {"id": "byName", "options": apply_to},
            "mappings": [
                {"fieldName": "Time", "handlerKey": "__ignore"},
                {"fieldName": field_name, "handlerKey": handler, "reducerId": "lastNotNull"},
            ],
        },
    }


def panels() -> list[Panel]:
    out: list[Panel] = []
    y = 0

    out.append(
        stat(
            1,
            "Engines",
            "Heartbeat.",
            _grid(0, y, 6, 5),
            [(f"lm_engine_up{{{REALM}}}", "{{realm}}")],
            mappings={"0": ("DOWN", "red"), "1": ("ONLINE", "green")},
            overrides=_names([("demo", "D"), ("mainnet", "M")]),
        )
    )
    out.append(
        stat(
            2,
            "New orders",
            "Entry gate.",
            _grid(6, y, 6, 5),
            [(f"lm_engine_may_open{{{REALM}}}", "{{realm}}")],
            mappings={"0": ("PAUSED", "orange"), "1": ("OPEN", "green")},
            overrides=_names([("demo", "D"), ("mainnet", "M")]),
        )
    )
    out.append(
        stat(
            3,
            "Signal workers",
            "Health and coverage.",
            _grid(12, y, 6, 5),
            [
                (f"lm_worker_status_healthy{{{REALM}}}", "{{realm}} verdict"),
                (f"lm_worker_ticker_coverage_complete{{{REALM}}}", "{{realm}} coverage"),
            ],
            mappings={"0": ("ATTENTION", "red"), "1": ("HEALTHY", "green")},
            overrides=_names(
                [
                    ("demo verdict", "D"),
                    ("mainnet verdict", "M"),
                    ("demo coverage", "D cov"),
                    ("mainnet coverage", "M cov"),
                ]
            ),
        )
    )
    out.append(
        stat(
            4,
            "Tape recorders",
            "Heartbeat.",
            _grid(18, y, 6, 5),
            [("lm_recorder_up", "{{realm}}")],
            mappings={"0": ("DOWN", "red"), "1": ("RECORDING", "green")},
            overrides=_names([("binance", "BN"), ("bybit", "BY")]),
        )
    )
    y += 5

    for panel_id, x, panel_y, realm, metric, label, description, color in (
        (11, 0, y, "demo", "lm_engine_equity_usdt", "D · Equity", "Current account equity · USDT.", "green"),
        (
            13,
            12,
            y,
            "mainnet",
            "lm_engine_equity_usdt",
            "M · Equity",
            "Current account equity · USDT.",
            "yellow",
        ),
        (
            12,
            0,
            y + 4,
            "demo",
            "lm_engine_position_entry_notional_usdt",
            "D · OI",
            "Entry notional of the current open position · USDT.",
            "green",
        ),
        (
            14,
            12,
            y + 4,
            "mainnet",
            "lm_engine_position_entry_notional_usdt",
            "M · OI",
            "Entry notional of the current open position · USDT.",
            "yellow",
        ),
    ):
        selector = f'{metric}{{realm="{realm}",{REALM}}}'
        lower, upper = _padded_sparkline_bounds(selector)
        panel = stat(
            panel_id,
            "",
            description,
            _grid(x, panel_y, 12, 4),
            [(selector, label)],
            unit="currencyUSD",
            decimals=2,
            sparkline=True,
            text_mode="value_and_name",
            justify_mode="auto",
            orientation="horizontal",
            text={"titleSize": 14, "valueSize": 14},
            color_mode="value",
            overrides=_name_colors([(label, label, color)]),
        )
        panel["targets"].extend(
            [
                _target(lower, "Min", instant=True, ref="B"),
                _target(upper, "Max", instant=True, ref="C"),
            ]
        )
        panel["transformations"] = [
            _config_from_query("B", "Min", "min", label),
            _config_from_query("C", "Max", "max", label),
        ]
        out.append(panel)
    y += 8

    out.append(row(30, "Execution", y))
    y += 1
    out.append(
        stat(
            31,
            "Execution activity · 15m",
            "Orders, fills, resets.",
            _grid(0, y, 10, 9),
            [
                (_increase("lm_engine_orders_sent"), "{{realm}} orders"),
                (_increase("lm_engine_fills"), "{{realm}} fills"),
                (_increase("lm_engine_stream_resets"), "{{realm}} stream resets"),
            ],
            decimals=0,
            sparkline=True,
            overrides=_name_colors(
                [
                    ("demo orders", "D orders", "blue"),
                    ("mainnet orders", "M orders", "blue"),
                    ("demo fills", "D fills", "green"),
                    ("mainnet fills", "M fills", "green"),
                    ("demo stream resets", "D resets", "red"),
                    ("mainnet stream resets", "M resets", "red"),
                ]
            ),
        )
    )
    out.append(
        timeseries(
            32,
            "Order-path latency · p99",
            "Milliseconds · E2E emphasized.",
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
            fill_opacity=4,
            overrides=_names(
                [
                    (f"{realm} · {source}", f"{short} · {display}")
                    for realm, short in (("demo", "D"), ("mainnet", "M"))
                    for source, display in (
                        ("end-to-end", "E2E"),
                        ("venue ack", "ACK"),
                        ("durable", "WAL"),
                        ("decide", "DEC"),
                    )
                ]
            )
            + [
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
        stat(
            41,
            "Data freshness",
            "Current age.",
            _grid(0, y, 8, 8),
            [
                (f"lm_engine_account_age_ms{{{REALM}}}", "engine {{realm}} · account"),
                (f"lm_worker_ws_last_frame_age_ms{{{REALM}}}", "worker {{realm}} · market frame"),
                ("lm_recorder_receive_age_ms", "recorder {{realm}} · market frame"),
            ],
            unit="ms",
            decimals=0,
            thresholds=[("green", None), ("orange", 60_000), ("red", 180_000)],
            overrides=_names(
                [
                    ("engine demo · account", "D · acct"),
                    ("engine mainnet · account", "M · acct"),
                    ("worker demo · market frame", "D · feed"),
                    ("worker mainnet · market frame", "M · feed"),
                    ("recorder binance · market frame", "BN · feed"),
                    ("recorder bybit · market frame", "BY · feed"),
                ]
            ),
        )
    )
    out.append(
        bar_gauge(
            42,
            "Load",
            "Queue and byte-budget use.",
            _grid(8, y, 7, 8),
            [
                (
                    f"max by (realm) (lm_worker_ws_queue_fill{{{REALM}}} or "
                    f"lm_worker_spool_file_fill{{{REALM}}} or lm_worker_spool_byte_fill{{{REALM}}})",
                    "{{realm}} · worker",
                ),
                (
                    "sum by (realm) (last_over_time(lm_recorder_projected_month_gb[24h])) / "
                    "sum by (realm) (last_over_time(lm_recorder_monthly_gb[24h]))",
                    "recorder {{realm}} · monthly traffic",
                ),
                ("lm_recorder_queue_fill", "recorder {{realm}} · writer queue"),
            ],
            thresholds=[("green", None), ("orange", 0.9), ("red", 1.0)],
            overrides=_names(
                [
                    ("demo · worker", "D · worker"),
                    ("mainnet · worker", "M · worker"),
                    ("recorder binance · monthly traffic", "BN · traffic"),
                    ("recorder bybit · monthly traffic", "BY · traffic"),
                    ("recorder binance · writer queue", "BN · queue"),
                    ("recorder bybit · writer queue", "BY · queue"),
                ]
            ),
        )
    )
    out.append(
        timeseries(
            43,
            "Tape loss · 5m",
            "Dropped frames and reconnect gaps.",
            _grid(15, y, 9, 8),
            [
                (
                    f"{_increase('lm_recorder_dropped_frames', window='5m', realm=False)} + "
                    f"{_increase('lm_recorder_disk_dropped_frames', window='5m', realm=False)}",
                    "{{realm}} · loss",
                ),
                (_increase("lm_recorder_reconnects", window="5m", realm=False), "{{realm}} · gap"),
            ],
            decimals=0,
            points=True,
            fill_opacity=4,
            overrides=_name_colors(
                [
                    (f"{realm} · {source}", f"{short} {display}", color)
                    for realm, short, color in (("binance", "BN", "blue"), ("bybit", "BY", "orange"))
                    for source, display in (("loss", "loss"), ("gap", "gap"))
                ]
            )
            + [
                {
                    "matcher": {"id": "byRegexp", "options": "/ · gap$/"},
                    "properties": [
                        {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 6]}},
                        {"id": "custom.lineWidth", "value": 1},
                    ],
                }
            ],
        )
    )
    return out


def dashboard() -> dict[str, Any]:
    return {
        "title": "liquidity-migration fleet",
        "uid": "liqmig-fleet",
        "description": "D/M engines, workers, and tape recorders.",
        "tags": ["liquidity-migration"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 11,
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
