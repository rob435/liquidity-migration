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
    overrides: list[dict[str, Any]] | None = None,
) -> Panel:
    defaults: dict[str, Any] = {
        "color": {"mode": "palette-classic"},
        "unit": unit,
        "custom": {
            "drawStyle": "bars" if bars else "line",
            "lineWidth": 2,
            "fillOpacity": 60 if bars else 8,
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
    sparkline: bool = False,
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
            "graphMode": "area" if sparkline else "none",
            "justifyMode": "center",
            "textMode": "value_and_name",
            "wideLayout": True,
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
        "targets": _targets(rows, instant=True),
    }


def state_timeline(
    panel_id: int,
    title: str,
    description: str,
    grid: dict[str, int],
    rows: list[tuple[str, str]],
    *,
    on_text: str,
    off_text: str,
) -> Panel:
    return {
        "type": "state-timeline",
        "id": panel_id,
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": grid,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "thresholds": {
                    "mode": "absolute",
                    "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}],
                },
                "custom": {"fillOpacity": 80, "lineWidth": 0},
                "mappings": [
                    {
                        "type": "value",
                        "options": {
                            "0": {"text": off_text, "color": "red", "index": 0},
                            "1": {"text": on_text, "color": "green", "index": 1},
                        },
                    }
                ],
            },
            "overrides": [],
        },
        "options": {
            "mergeValues": True,
            "showValue": "never",
            "alignValue": "center",
            "rowHeight": 0.8,
            "legend": {"showLegend": False},
            "tooltip": {"mode": "single", "sort": "none"},
        },
        "targets": _targets(rows),
    }


def _sleeve(metric_suffix: str) -> str:
    """Pull the sleeve name out of `lm_engine_sleeve_<name>_<suffix>` as a label."""
    return (
        f'label_replace({{__name__=~"lm_engine_sleeve_.*_{metric_suffix}", {REALM}}}, '
        f'"sleeve", "$1", "__name__", "lm_engine_sleeve_(.*)_{metric_suffix}")'
    )


def _rate_5m(metric: str, realm: bool = True) -> str:
    selector = f"{{{REALM}}}" if realm else ""
    return f"increase({metric}{selector}[5m])"


def panels() -> list[Panel]:
    out: list[Panel] = []
    y = 0

    out.append(
        state_timeline(
            1,
            "Health",
            "One lane per fact, sampled every minute. Green is the good state: a readable heartbeat, "
            "may_open true, the 24h loss breaker not tripped, a healthy signal worker, a live recorder. A red "
            "minute is a real minute: the sampler writes the line whether or not a producer is up.",
            _grid(0, y, 16, 8),
            [
                (f"lm_engine_up{{{REALM}}}", "{{realm}} · engine heartbeat"),
                (f"lm_engine_may_open{{{REALM}}}", "{{realm}} · may open"),
                (f"1 - lm_engine_rolling_loss_tripped{{{REALM}}}", "{{realm}} · loss breaker clear"),
                (f"lm_worker_status_healthy{{{REALM}}}", "{{realm}} · signal worker healthy"),
                ("lm_recorder_up", "recorder {{realm}} · live"),
            ],
            on_text="ok",
            off_text="NOT ok",
        )
    )
    out.append(
        stat(
            2,
            "Equity",
            "Mark to market from the venue's own account reading, as the heartbeat carries it.",
            _grid(16, y, 8, 8),
            [(f"lm_engine_equity_usdt{{{REALM}}}", "{{realm}}")],
            unit="currencyUSD",
            decimals=2,
            sparkline=True,
        )
    )
    y += 8

    out.append(row(10, "Account", y))
    y += 1
    out.append(
        timeseries(
            11,
            "Equity and available margin",
            "Sampled once a minute from the heartbeat. A gap is a minute with no live heartbeat. "
            "Available under equity is margin posted against open positions; negative available is normal "
            "while the owner holds hand positions.",
            _grid(0, y, 12, 8),
            [
                (f"lm_engine_equity_usdt{{{REALM}}}", "{{realm}} equity"),
                (f"lm_engine_available_usdt{{{REALM}}}", "{{realm}} available"),
            ],
            unit="currencyUSD",
            min_zero=False,
        )
    )
    out.append(
        timeseries(
            12,
            "Rolling 24h loss against its ceiling",
            "Net realized loss in the trailing 24 hours next to the breaker's limit. Touching the limit trips "
            "the breaker: every sleeve shows entries_enabled false until it clears.",
            _grid(12, y, 12, 8),
            [
                (f"lm_engine_rolling_loss_net_usdt{{{REALM}}}", "{{realm}} net"),
                (f"lm_engine_rolling_loss_limit_usdt{{{REALM}}}", "{{realm}} limit"),
            ],
            unit="currencyUSD",
            min_zero=False,
            overrides=[
                {
                    "matcher": {"id": "byRegexp", "options": ".* limit"},
                    "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}],
                }
            ],
        )
    )
    y += 8

    out.append(row(20, "Sleeves", y))
    y += 1
    out.append(
        timeseries(
            21,
            "Open positions by sleeve",
            "How many positions each configured sleeve holds, zero included, so a flat sleeve is a line at "
            "zero rather than a missing one. `unattributed` is exposure no sleeve owns: the owner's hand trades.",
            _grid(0, y, 8, 8),
            [(_sleeve("positions"), "{{realm}} {{sleeve}}")],
            decimals=0,
        )
    )
    out.append(
        state_timeline(
            22,
            "Entries enabled by sleeve",
            "The effective entry gate per sleeve after committed config and the newest durable runtime "
            "override. Off means the sleeve may exit and settle but not open or grow.",
            _grid(8, y, 8, 8),
            [(_sleeve("entries_enabled"), "{{realm}} {{sleeve}}")],
            on_text="on",
            off_text="off",
        )
    )
    out.append(
        timeseries(
            23,
            "Entry blockers by sleeve",
            "Per-symbol reasons a sleeve is not opening right now, counted per sleeve. Expected trading state "
            "(inside a resize band, entry window closed), not faults; faults are strategy errors below.",
            _grid(16, y, 8, 8),
            [(_sleeve("blockers"), "{{realm}} {{sleeve}}")],
            decimals=0,
        )
    )
    y += 8

    out.append(row(30, "Order path", y))
    y += 1
    out.append(
        stat(
            31,
            "Order path last measured",
            "How long since the engine's latency ledger last held an order. The ledger is a 60-second window: "
            "it is only populated in a minute an order went out. The demo probe rests one order every 15 "
            "minutes, so demo should never read above about 16 minutes; mainnet reads the time since the "
            "funded engine last sent anything.",
            _grid(0, y, 6, 8),
            # A subquery, not `timestamp(last_over_time(...))`: a range-vector
            # function stamps its result at evaluation time, so that reads 0
            # forever. Inside a subquery `timestamp()` is evaluated at each
            # step and returns the sample's own time, which `max_over_time`
            # then takes the newest of.
            [(f"time() - max_over_time(timestamp(lm_engine_end_to_end_p50_ns{{{REALM}}})[7d:1m])", "{{realm}}")],
            unit="s",
            decimals=0,
            thresholds=[("green", None), ("orange", 1_200), ("red", 3_600)],
        )
    )
    out.append(
        stat(
            32,
            "Orders sent since boot",
            "The engine's own since-boot counter. It resets to zero on every restart, so read it next to uptime.",
            _grid(6, y, 6, 8),
            [(f"lm_engine_orders_sent{{{REALM}}}", "{{realm}}")],
            decimals=0,
        )
    )
    out.append(
        timeseries(
            33,
            "Order path, end to end",
            "The engine's own `end_to_end`: market event to submitted order, p50 and p99 over the ledger's "
            "60-second window. Plotted as points: there is a value only in a minute an order went out. It stops "
            "at the submit, so the venue's round trip is not in it — read `ack` below for that, and note that "
            "`ack` is empty on a venue path that leaves no transport stamp.",
            _grid(12, y, 12, 8),
            [
                (f"lm_engine_end_to_end_p50_ns{{{REALM}}}", "{{realm}} p50"),
                (f"lm_engine_end_to_end_p99_ns{{{REALM}}}", "{{realm}} p99"),
            ],
            unit="ns",
            points=True,
            legend_table=True,
        )
    )
    y += 8
    out.append(
        timeseries(
            34,
            "Order path by step, p99",
            "Where the time goes. decide is the strategy; durable is the log barrier; wire is the whole venue "
            "task, decision to completion, so it contains the round trip; ack is the round trip on its own and "
            "is recorded only when the adapter stamped the socket write — mainnet's places carry that stamp, "
            "demo's do not, so demo shows no ack line and its round trip sits inside wire. dispatch queue, "
            "venue task and core resume are the hand-offs. barrier wait is what the order actually waited on "
            "the disk; quota hold is time the adapter held a command back to stay inside the request quota. "
            "`engine latency --wal PATH` is the authority and says how many commands carry stamps.",
            _grid(0, y, 16, 9),
            [
                (f"lm_engine_decide_p99_ns{{{REALM}}}", "{{realm}} decide"),
                (f"lm_engine_durable_p99_ns{{{REALM}}}", "{{realm}} durable"),
                (f"lm_engine_wire_p99_ns{{{REALM}}}", "{{realm}} wire"),
                (f"lm_engine_ack_p99_ns{{{REALM}}}", "{{realm}} ack"),
                (f"lm_engine_dispatch_queue_p99_ns{{{REALM}}}", "{{realm}} dispatch queue"),
                (f"lm_engine_venue_task_p99_ns{{{REALM}}}", "{{realm}} venue task"),
                (f"lm_engine_core_resume_p99_ns{{{REALM}}}", "{{realm}} core resume"),
                (f"lm_engine_barrier_wait_p99_ns{{{REALM}}}", "{{realm}} barrier wait"),
                (f"lm_engine_quota_hold_p99_ns{{{REALM}}}", "{{realm}} quota hold"),
            ],
            unit="ns",
            points=True,
            legend_table=True,
        )
    )
    out.append(
        timeseries(
            35,
            "Working orders, blockers, faults",
            "Orders resting at the venue right now, entry blockers across every sleeve, flatten requests not "
            "yet acknowledged, and strategy errors. A blocker is ordinary trading state; a strategy error "
            "means a reducer could not reduce its inputs, and is the one to read first.",
            _grid(16, y, 8, 9),
            [
                (f"lm_engine_working_entries{{{REALM}}}", "{{realm}} working orders"),
                (f"lm_engine_entry_blockers{{{REALM}}}", "{{realm}} entry blockers"),
                (f"lm_engine_pending_flatten_requests{{{REALM}}}", "{{realm}} pending flattens"),
                (f"lm_engine_strategy_errors{{{REALM}}}", "{{realm}} strategy errors"),
            ],
            decimals=0,
        )
    )
    y += 9

    out.append(row(36, "Signal workers", y))
    y += 1
    out.append(
        state_timeline(
            37,
            "Worker transport and verdict",
            "The worker's own bounded verdict next to its raw transport facts. Starting is healthy while cold "
            "fill is inside its bound; recovering is healthy for two minutes during a later gap, repair, or "
            "coverage miss. Socket, topic quarantine, and spool faults remain immediate.",
            _grid(0, y, 8, 8),
            [
                (f"lm_worker_up{{{REALM}}}", "{{realm}} · heartbeat"),
                (f"lm_worker_status_healthy{{{REALM}}}", "{{realm}} · verdict"),
                (f"lm_worker_ws_connected{{{REALM}}}", "{{realm}} · socket"),
                (f"lm_worker_ticker_coverage_complete{{{REALM}}}", "{{realm}} · ticker coverage"),
                (f"1 - lm_worker_spool_backpressured{{{REALM}}}", "{{realm}} · spool writable"),
                (
                    f"1 - clamp_max(lm_worker_ticker_topics_quarantined{{{REALM}}} + "
                    f"lm_worker_kline_topics_quarantined{{{REALM}}}, 1)",
                    "{{realm}} · topics clean",
                ),
            ],
            on_text="ok",
            off_text="NOT ok",
        )
    )
    out.append(
        timeseries(
            38,
            "Worker freshness and repair age",
            "Heartbeat and last-frame age should stay low. Repair-gap age exists while causal history is being "
            "closed; LONG and carry cycle age show whether reducers continue completing during that repair.",
            _grid(8, y, 8, 8),
            [
                (f"lm_worker_heartbeat_age_ms{{{REALM}}}", "{{realm}} heartbeat"),
                (f"lm_worker_ws_last_frame_age_ms{{{REALM}}}", "{{realm}} last frame"),
                (f"lm_worker_ws_gap_age_ms{{{REALM}}}", "{{realm}} repair gap"),
                (f"lm_worker_long_cycle_age_ms{{{REALM}}}", "{{realm}} LONG cycle"),
                (f"lm_worker_carry_cycle_age_ms{{{REALM}}}", "{{realm}} carry cycle"),
            ],
            unit="ms",
            legend_table=True,
        )
    )
    out.append(
        timeseries(
            39,
            "Worker queue and durable spool fill",
            "Fraction of each bounded buffer in use. The WebSocket queue absorbs bursts; the durable spool holds "
            "observations until the engine acknowledges them. One means the corresponding hard cap.",
            _grid(16, y, 8, 8),
            [
                (f"lm_worker_ws_queue_fill{{{REALM}}}", "{{realm}} WebSocket queue"),
                (f"lm_worker_spool_file_fill{{{REALM}}}", "{{realm}} spool files"),
                (f"lm_worker_spool_byte_fill{{{REALM}}}", "{{realm}} spool bytes"),
            ],
            unit="percentunit",
        )
    )
    y += 8

    out.append(row(40, "Engine", y))
    y += 1
    out.append(
        timeseries(
            41,
            "Heartbeat and account reading age",
            "Age of the heartbeat file when sampled (written every 5 s) and of the venue's account reading "
            "inside it. Both climbing together is the engine wedged; only the account age climbing is the "
            "private stream gone quiet.",
            _grid(0, y, 8, 8),
            [
                (f"lm_engine_heartbeat_age_ms{{{REALM}}}", "{{realm}} heartbeat"),
                (f"lm_engine_account_age_ms{{{REALM}}}", "{{realm}} account reading"),
            ],
            unit="ms",
        )
    )
    out.append(
        timeseries(
            42,
            "Orders, fills, stream resets (per 5 min)",
            "Since-boot counters read as increases, so a restart is a flat line, not a cliff. A stream reset is "
            "a recovered private-stream gap.",
            _grid(8, y, 8, 8),
            [
                (_rate_5m("lm_engine_orders_sent"), "{{realm}} orders"),
                (_rate_5m("lm_engine_fills"), "{{realm}} fills"),
                (_rate_5m("lm_engine_stream_resets"), "{{realm}} stream resets"),
            ],
            decimals=0,
            bars=True,
        )
    )
    out.append(
        timeseries(
            43,
            "Market events per second and venue clock offset",
            "Public market messages the engine consumed, as a rate; and the venue's clock minus this box's, "
            "off the freshest quote. A box that drifts makes every venue-stamp comparison quietly wrong.",
            _grid(16, y, 8, 8),
            [
                (f"{_rate_5m('lm_engine_market_events')} / 300", "{{realm}} market events/s"),
                (f"lm_engine_venue_clock_offset_ms{{{REALM}}}", "{{realm}} clock offset (ms)"),
            ],
            min_zero=False,
            overrides=[
                {
                    "matcher": {"id": "byRegexp", "options": ".*clock offset.*"},
                    "properties": [{"id": "unit", "value": "ms"}, {"id": "custom.axisPlacement", "value": "right"}],
                }
            ],
        )
    )
    y += 8

    out.append(row(50, "Tape recorders", y))
    y += 1
    out.append(
        timeseries(
            51,
            "Recorder: projected month against allowance",
            "What the month's inbound bytes project to at the current pace, next to the budget's allowance. "
            "Over the line, the budget controller sheds feeds in its configured order.",
            _grid(0, y, 8, 8),
            [
                ("lm_recorder_projected_month_gb", "{{realm}} projected"),
                ("lm_recorder_monthly_gb", "{{realm}} allowance"),
            ],
            unit="decgbytes",
            overrides=[
                {
                    "matcher": {"id": "byRegexp", "options": ".* allowance"},
                    "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}],
                }
            ],
        )
    )
    out.append(
        timeseries(
            52,
            "Recorder: frames dropped and reconnects (per 5 min)",
            "Since-boot counters read as increases. A dropped frame is the writer queue overrunning; each "
            "overrun reconnects the shard for fresh snapshots, so drops and reconnects rise together when the "
            "recorder is starved. Dropped at disk is storage refusing writes.",
            _grid(8, y, 8, 8),
            [
                (_rate_5m("lm_recorder_dropped_frames", realm=False), "{{realm}} dropped"),
                (_rate_5m("lm_recorder_disk_dropped_frames", realm=False), "{{realm}} dropped at disk"),
                (_rate_5m("lm_recorder_reconnects", realm=False), "{{realm}} reconnects"),
            ],
            decimals=0,
            bars=True,
        )
    )
    out.append(
        timeseries(
            53,
            "Recorder: queue fill, shards, shed feeds",
            "How full the writer queue was at the sample (1.0 is an overrun), shards connected of shards "
            "configured, and feeds currently shed by the budget controller.",
            _grid(16, y, 8, 8),
            [
                ("lm_recorder_queue_fill", "{{realm}} queue fill"),
                ("lm_recorder_shards_connected / lm_recorder_shards", "{{realm}} shards connected"),
                ("lm_recorder_shed_feeds", "{{realm}} shed feeds"),
            ],
            unit="percentunit",
            overrides=[
                {
                    "matcher": {"id": "byRegexp", "options": ".* shed feeds"},
                    "properties": [
                        {"id": "unit", "value": "short"},
                        {"id": "decimals", "value": 0},
                        {"id": "custom.axisPlacement", "value": "right"},
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
        "description": (
            "The funded and demo engines, signal workers, and tape recorders, sampled once a minute by "
            "scripts/runtime/record_equity.py. The JSONL under /var/lib/liquidity-migration/equity on the host "
            "is the record; this is a view. Rendered by deploy/grafana/render_dashboard.py."
        ),
        "tags": ["liquidity-migration"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 2,
        "editable": True,
        "graphTooltip": 1,
        "refresh": "1m",
        "time": {"from": "now-24h", "to": "now"},
        "templating": {
            "list": [
                {
                    "name": "DS_METRICS",
                    "label": "Metrics source",
                    "type": "datasource",
                    "query": "prometheus",
                    "current": {},
                    "hide": 0,
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
