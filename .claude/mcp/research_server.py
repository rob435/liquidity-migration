#!/usr/bin/env python3
"""Stdio MCP server for the liquidity-migration research repo.

Pure standard library: no third-party dependencies, no network access.
Exposes filesystem research tooling so Claude can discover data roots,
locate research reports, parse report metrics, and audit run artifacts
against the repo's mandatory backtest-integrity standard.

Transport: Model Context Protocol over stdio (newline-delimited JSON-RPC 2.0).
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
import traceback

# .claude/mcp/research_server.py -> repo root is three levels up.
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "liqmig-research"
SERVER_VERSION = "0.1.0"

RUN_LABELS = (
    "invalid",
    "exploratory",
    "biased_benchmark",
    "candidate",
    "paper_ready",
)

DATA_ROOTS = [
    {
        "id": "bybit_full_pit",
        "path": "~/SHARED_DATA/bybit_full_pit",
        "purpose": (
            "Bybit USDT linear perpetuals full-PIT working dataset (~2021-01.."
            "today). Default config resolves DATA_ROOT here; required for "
            "serious research and as one of the two venue working datasets."
        ),
        "boundary": (
            "End-exclusive on today's UTC date; rebuild idempotently via "
            "bash scripts/build_full_pit_roots.sh."
        ),
    },
    {
        "id": "binance_full_pit",
        "path": "~/SHARED_DATA/binance_full_pit",
        "purpose": (
            "Binance USD-M perpetuals full-PIT working dataset (~2019-09.."
            "today). The cross-venue agreement check vs bybit_full_pit; "
            "disagreement flags regime / microstructure artefact."
        ),
        "boundary": (
            "End-exclusive on today's UTC date; rebuild via "
            "scripts/build_full_pit_binance.sh."
        ),
    },
    {
        "id": "live_demo",
        "path": "data/bybit-demo-event",
        "purpose": (
            "Live Bybit demo operational root: order/trade ledgers, cycle "
            "reports, risk-watchdog reports. Never point a research run here."
        ),
        "boundary": "Operational forward ledgers only, not a research dataset.",
    },
    {
        "id": "paper_shadow",
        "path": "data/bybit-paper-event",
        "purpose": (
            "Paper-shadow ledger: same profile as live_demo with no order "
            "submission and idealised fills. Used by reconcile-paper-demo / "
            "reconcile-long-paper-demo to measure execution slippage."
        ),
        "boundary": "Operational forward ledger, not a research dataset.",
    },
]


def log(message):
    sys.stderr.write("[%s] %s\n" % (SERVER_NAME, message))
    sys.stderr.flush()


def send(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


class RpcError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def resolve_path(raw):
    """Resolve a data-root id, ~user path, or repo-relative path to an abspath."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("a non-empty 'path' (or 'root') argument is required")
    raw = raw.strip()
    for root in DATA_ROOTS:
        if raw == root["id"]:
            return os.path.expanduser(root["path"])
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        expanded = os.path.join(REPO_ROOT, expanded)
    return os.path.normpath(expanded)


# --- tool: data_roots -------------------------------------------------------

def tool_data_roots(args):
    out = ["# Data roots (liquidity-migration)", ""]
    for root in DATA_ROOTS:
        resolved = os.path.expanduser(root["path"])
        present = os.path.isdir(resolved)
        out.append("## %s  [%s]" % (root["id"], "PRESENT" if present else "ABSENT"))
        out.append("- declared path : %s" % root["path"])
        out.append("- resolved path : %s" % resolved)
        out.append("- purpose       : %s" % root["purpose"])
        out.append("- boundary      : %s" % root["boundary"])
        if present:
            try:
                entries = sorted(os.listdir(resolved))
                shown = ", ".join(entries[:14]) or "(empty)"
                more = "" if len(entries) <= 14 else " (+%d more)" % (len(entries) - 14)
                out.append("- entries       : %s%s" % (shown, more))
            except OSError as exc:
                out.append("- entries       : (unreadable: %s)" % exc)
        out.append("")
    out.append(
        "Rule: serious research and promotion evidence MUST use "
        "canonical_research. Never run research against live_demo. OOS roots "
        "are validation-only and no longer pristine. See docs/data_roots.md."
    )
    return "\n".join(out)


# --- tool: list_reports -----------------------------------------------------

REPORT_NAME_HINTS = ("_report.md", "research_report.md", "tribunal_report.md")


def tool_list_reports(args):
    root_arg = args.get("root") or "canonical_research"
    base = resolve_path(root_arg)
    if not os.path.isdir(base):
        raise ValueError("data root not found on disk: %s" % base)
    reports_dir = os.path.join(base, "reports")
    search_dir = reports_dir if os.path.isdir(reports_dir) else base
    found = {}
    for dirpath, dirnames, filenames in os.walk(search_dir):
        for fn in filenames:
            low = fn.lower()
            if low.endswith(".md") and any(h in low for h in REPORT_NAME_HINTS):
                full = os.path.join(dirpath, fn)
                try:
                    found[full] = os.path.getmtime(full)
                except OSError:
                    found[full] = 0.0
        if len(found) > 400:
            break
    if not found:
        return "No report files (*_report.md) found under %s" % search_dir
    ordered = sorted(found.items(), key=lambda kv: kv[1], reverse=True)
    out = [
        "# Reports under %s" % search_dir,
        "%d found (newest first, capped at 100):" % len(ordered),
        "",
    ]
    for full, mtime in ordered[:100]:
        stamp = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
            if mtime else "unknown-time"
        )
        out.append("- [%s] %s" % (stamp, full))
    return "\n".join(out)


# --- tool: parse_report -----------------------------------------------------

# key:value field name (lowercased) -> canonical headline metric
FIELD_METRIC_MAP = {
    "trades": "trades",
    "candidate events": "candidate_events",
    "total return": "total_return",
    "return": "total_return",
    "max drawdown": "max_drawdown",
    "max dd": "max_drawdown",
    "drawdown": "max_drawdown",
    "max no-new-high stretch": "max_underwater_days",
    "max no new high stretch": "max_underwater_days",
    "worst 90d": "worst_90d_return",
    "worst 90d return": "worst_90d_return",
    "worst split": "min_split_return",
    "worst split return": "min_split_return",
    "min split": "min_split_return",
    "minimum split": "min_split_return",
    "average split sharpe-like": "avg_split_sharpe",
    "average split sharpe": "avg_split_sharpe",
    "avg split sharpe": "avg_split_sharpe",
    "oos return": "oos_return",
    "oos": "oos_return",
    "train return": "train_return",
    "validation return": "validation_return",
    "promotion gate": "promotion_gate",
    "verdict": "verdict",
}

# markdown table header cell (lowercased) -> canonical headline metric
COLUMN_METRIC_MAP = {
    "trades": "trades",
    "return": "total_return",
    "total return": "total_return",
    "max dd": "max_drawdown",
    "max drawdown": "max_drawdown",
    "max uw days": "max_underwater_days",
    "worst 90d": "worst_90d_return",
    "sharpe": "sharpe",
    "min split": "min_split_return",
    "avg split sharpe": "avg_split_sharpe",
    "pos splits": "positive_splits",
    "promote": "promote",
    "reason": "promotion_reason",
}


def _find_report_file(path):
    if os.path.isfile(path):
        return path
    if os.path.isdir(path):
        patterns = ("*research_report.md", "*tribunal_report.md", "*_report.md", "*.md")
        for pat in patterns:
            hits = sorted(glob.glob(os.path.join(path, pat)))
            if hits:
                return hits[0]
        for pat in patterns:
            hits = sorted(glob.glob(os.path.join(path, "**", pat), recursive=True))
            if hits:
                return hits[0]
        raise ValueError("no .md report file found in directory: %s" % path)
    raise ValueError("path does not exist: %s" % path)


def _clean_value(value):
    value = value.strip().replace("`", "")
    if len(value) >= 4 and value.startswith("**") and value.endswith("**"):
        value = value[2:-2].strip()
    return value.strip()


def _split_table_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _is_table_separator(line):
    line = line.strip()
    if "-" not in line or "|" not in line:
        return False
    return re.match(r"^\|?[\s:|-]+\|?$", line) is not None


def _parse_tables(text):
    """Return a list of {header: [...], rows: [{header: cell}, ...]} tables."""
    lines = text.splitlines()
    tables = []
    i = 0
    while i < len(lines) - 1:
        line = lines[i].strip()
        if line.startswith("|") and _is_table_separator(lines[i + 1]):
            header = _split_table_row(line)
            rows = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = _split_table_row(lines[j])
                row = {}
                for k in range(len(header)):
                    cell = cells[k] if k < len(cells) else ""
                    row[header[k]] = _clean_value(cell)[:200]
                rows.append(row)
                j += 1
                if len(rows) >= 60:
                    break
            tables.append({"header": header, "rows": rows})
            i = j
        else:
            i += 1
    return tables


def _parse_fields(text):
    """Return key:value pairs from bullet or plain 'Key: value' lines."""
    fields = {}
    field_rx = re.compile(r"^\s*[-*]?\s*([A-Za-z][^:|\n]{0,48}?)\s*:\s+(\S.*?)\s*$")
    for raw in text.splitlines():
        stripped = raw.lstrip()
        if stripped.startswith("|") or stripped.startswith("#") or stripped.startswith("```"):
            continue
        match = field_rx.match(raw)
        if not match:
            continue
        key = match.group(1).strip()
        if key and key not in fields:
            fields[key] = _clean_value(match.group(2))[:300]
        if len(fields) >= 120:
            break
    return fields


def _scenario_table(tables):
    for table in tables:
        low = [h.lower() for h in table["header"]]
        has_trades = any("trades" in h for h in low)
        has_return = any("return" in h for h in low)
        if has_trades and has_return and table["rows"]:
            return table
    return None


def _pick_scenario_row(table):
    rows = table["rows"]
    for row in rows:
        for key, value in row.items():
            if key.lower() == "promote" and value.lower() in ("true", "yes"):
                return row
    for row in rows:
        for key, value in row.items():
            if key.lower() == "rank" and value.strip() == "1":
                return row
    return rows[0]


def _build_headline(tables, fields):
    headline = {}
    table = _scenario_table(tables)
    if table is not None:
        row = _pick_scenario_row(table)
        for header_cell, value in row.items():
            canon = COLUMN_METRIC_MAP.get(header_cell.strip().lower())
            if canon and value:
                headline[canon] = value
    for key, value in fields.items():
        canon = FIELD_METRIC_MAP.get(key.strip().lower())
        if canon and value and canon not in headline:
            headline[canon] = value
    return headline


def _scan_methodology_label(text):
    explicit = re.search(
        r"label\s*[:=]\s*[`\"'*]*(" + "|".join(RUN_LABELS) + r")\b",
        text,
        re.IGNORECASE,
    )
    if explicit:
        return explicit.group(1).lower()
    seen = [
        lbl for lbl in RUN_LABELS
        if re.search(r"\b" + lbl + r"\b", text, re.IGNORECASE)
    ]
    if seen:
        return "none stated (label keywords present in text: %s)" % ", ".join(seen)
    return "none stated"


def tool_parse_report(args):
    path = resolve_path(args.get("path"))
    report = _find_report_file(path)
    try:
        with open(report, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        raise ValueError("could not read report: %s" % exc)
    tables = _parse_tables(text)
    fields = _parse_fields(text)
    headline = _build_headline(tables, fields)
    scenario = _scenario_table(tables)
    result = {
        "report_file": report,
        "size_bytes": len(text),
        "headline": headline,
        "fields": fields,
        "tables": [
            {
                "header": t["header"],
                "row_count": len(t["rows"]),
                "rows": t["rows"][:30],
            }
            for t in tables[:12]
        ],
        "scenario_row_count": 0 if scenario is None else len(scenario["rows"]),
        "methodology_run_label": _scan_methodology_label(text),
    }
    note = (
        "NOTE: 'headline' / 'fields' / 'tables' are best-effort captures from "
        "markdown tables and Key: value lines - verify against the report "
        "body. Metric presence is not validity; apply the backtest-integrity "
        "gates before trusting any result. 'methodology_run_label' is the "
        "repo's invalid/exploratory/biased_benchmark/candidate/paper_ready "
        "tag, distinct from any 'Run label' field a report carries."
    )
    return json.dumps(result, indent=2) + "\n\n" + note


# --- tool: audit_run_artifacts ----------------------------------------------

CORE_ARTIFACTS = (
    "research report (.md)",
    "trade ledger",
    "drawdown",
    "cost model",
    "data-root identity",
    "config / param identity",
)


def tool_audit_run_artifacts(args):
    target = resolve_path(args.get("path"))
    if not os.path.exists(target):
        raise ValueError("path does not exist: %s" % target)

    if os.path.isfile(target):
        run_dir = os.path.dirname(target) or REPO_ROOT
        report_file = target if target.lower().endswith(".md") else None
    else:
        run_dir = target
        report_file = None

    files = []
    for dirpath, dirnames, filenames in os.walk(run_dir):
        for fn in filenames:
            files.append(os.path.join(dirpath, fn))
        if len(files) > 5000:
            break
    rel_lower = [os.path.relpath(f, run_dir).lower() for f in files]

    if report_file is None:
        for f in files:
            base = os.path.basename(f).lower()
            if base.endswith("_report.md") or base.endswith("report.md"):
                report_file = f
                break

    text = ""
    if report_file and os.path.isfile(report_file):
        try:
            with open(report_file, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
    low = text.lower()

    def has_file(*subs):
        return any(all(s in name for s in subs) for name in rel_lower)

    def in_report(*subs):
        return any(s in low for s in subs)

    checks = []

    def add(name, ok, detail):
        checks.append((name, bool(ok), detail))

    add(
        "research report (.md)",
        report_file is not None,
        report_file or "no *_report.md found in run dir",
    )
    add(
        "trade ledger",
        has_file("trade", ".csv") or has_file("trade", ".parquet")
        or in_report("trade ledger", "best trades", "best_trades"),
        "a *trade*.csv/.parquet ledger file or a trade-ledger report section",
    )
    add(
        "equity curve",
        has_file("equity") or in_report("equity curve", "equity"),
        "an *equity* file or an equity-curve report section",
    )
    add(
        "split metrics",
        in_report("split"),
        "split / per-window stability metrics in the report",
    )
    add(
        "drawdown",
        in_report("drawdown", "draw-down", "max dd", "max uw"),
        "max drawdown reported",
    )
    add(
        "out-of-sample window",
        in_report("oos", "out-of-sample", "out of sample",
                  "pre-registered", "preregistered", "pre_registered"),
        "an OOS / pre-registered window result",
    )
    add(
        "cost model",
        in_report("cost", "fee", "slippage", "funding"),
        "fees / slippage / funding stated",
    )
    add(
        "config / param identity",
        has_file("config") or has_file(".yaml") or has_file(".yml")
        or has_file(".json")
        or in_report("config hash", "parameter hash", "param hash",
                     "scenario:", "threshold"),
        "a saved config/scenario file or a config/param hash",
    )
    add(
        "data-root identity",
        in_report("data root", "data-root", "data_root", "shared_data",
                  "source:", "pit manifest", "manifest rows", "manifest symbols",
                  "date range"),
        "which data root / dataset produced the run",
    )
    add(
        "run label",
        any(re.search(r"\b" + lbl + r"\b", low) for lbl in RUN_LABELS),
        "one of: " + ", ".join(RUN_LABELS),
    )

    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    core_ok = all(ok for name, ok, _ in checks if name in CORE_ARTIFACTS)
    label_ok = any(ok for name, ok, _ in checks if name == "run label")
    is_tribunal = (
        "model court" in low or "strategy tribunal" in low
        or "strategy_tribunal" in low
    )

    out = [
        "# Run artifact audit",
        "",
        "run dir     : %s" % run_dir,
        "report file : %s" % (report_file or "(none)"),
        "files seen  : %d" % len(files),
        "score       : %d/%d artifacts present" % (passed, total),
        "",
    ]
    for name, ok, detail in checks:
        out.append("[%s] %s" % ("OK  " if ok else "MISS", name))
        if not ok:
            out.append("       expected: %s" % detail)
    out.append("")

    if is_tribunal:
        verdict = (
            "This looks like a model-court / strategy-tribunal report (an "
            "adversarial audit OF a strategy run, not a run itself). The "
            "checks above assume a strategy run directory - point "
            "audit_run_artifacts at the underlying volume-events report dir "
            "for a meaningful artifact verdict."
        )
    elif not core_ok:
        verdict = (
            "INCOMPLETE - core artifacts missing. Per error #23, a run without "
            "saved config, data identity, trade ledger and a cost model is 'a "
            "screenshot', not evidence. Do not cite it."
        )
    elif not label_ok:
        verdict = (
            "Core artifacts present but no methodology run label is stated. "
            "Assign one (invalid / exploratory / biased_benchmark / candidate "
            "/ paper_ready) - see the backtest-integrity skill."
        )
    else:
        verdict = (
            "Core artifacts present and a run label is stated. ARTIFACT CHECK "
            "ONLY - this does not verify PIT universe coverage, causal "
            "features, warm-started exit state, or untouched-OOS hygiene. "
            "Apply the full backtest-integrity gates before trusting it."
        )
    out.append("VERDICT: " + verdict)
    return "\n".join(out)


# --- tool: current_state ----------------------------------------------------

def tool_current_state(args):
    """Return STATE.md verbatim. The repo's single-page research-program state."""
    path = os.path.join(REPO_ROOT, "STATE.md")
    if not os.path.exists(path):
        return (
            "STATE.md not found at repo root. The research-program state file "
            "should always exist; ask the operator to restore or create it. "
            "Falling back to reading the master plan at "
            "docs/research_summary.md."
        )
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception as exc:  # noqa: BLE001 - mcp must not crash on read error
        return "STATE.md read failed: %s" % exc


# --- tool: apply_decision_rule ----------------------------------------------

def tool_apply_decision_rule(args):
    """Apply the Strictness Manifesto per-cell to a sweep summary CSV.

    Imports scripts.apply_decision_rule (pure stdlib) so the verdict logic
    is single-sourced. Returns the same per-cell table + summary the CLI
    prints, suitable for inclusion in a phase verdict pre-reg.
    """
    csv_arg = args.get("summary_csv") or args.get("path") or args.get("csv")
    control = args.get("control") or args.get("control_cell") or "00_baseline"
    rule = args.get("rule") or "manifesto"
    if not isinstance(csv_arg, str) or not csv_arg.strip():
        raise RpcError(-32602, "summary_csv (path) is required")

    # Import the script as a module so we reuse the audited logic
    script_path = os.path.join(REPO_ROOT, "scripts", "apply_decision_rule.py")
    if not os.path.exists(script_path):
        raise RpcError(-32603, "scripts/apply_decision_rule.py missing — repo state is broken")

    # Resolve the CSV path against the repo root, data roots, or absolute path
    csv_path = resolve_path(csv_arg) if not csv_arg.startswith("/") else csv_arg
    if not os.path.exists(csv_path):
        raise RpcError(-32602, "summary CSV not found at: %s" % csv_path)

    # Capture stdout from main(); main() prints to stdout and exits with
    # an int. Use subprocess to keep the analyzer's main() exit semantics
    # intact (avoids muddling sys.exit with the long-lived MCP loop).
    import subprocess  # local import: stdlib + only on tool invocation
    proc = subprocess.run(
        [
            sys.executable, script_path, csv_path,
            "--control", str(control),
            "--rule", str(rule),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode == 2:
        raise RpcError(-32602, "decision-rule usage error: %s" % proc.stderr.strip())
    if proc.returncode != 0:
        raise RpcError(-32603, "decision-rule failed (exit %d): %s" % (proc.returncode, proc.stderr.strip()))
    return proc.stdout


# --- MCP plumbing -----------------------------------------------------------

TOOLS = [
    {
        "name": "data_roots",
        "description": (
            "List the repo's data roots (canonical research, live demo, two "
            "OOS roots) with purpose, date boundary, on-disk presence and "
            "top-level contents. Use before choosing a --data-root."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": tool_data_roots,
    },
    {
        "name": "list_reports",
        "description": (
            "Find research/backtest report files (*_report.md) under a data "
            "root, newest first. 'root' accepts a data-root id "
            "(canonical_research, oos_bybit_pre2023, oos_binance_pit, "
            "live_demo) or an absolute/repo-relative path; default "
            "canonical_research."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {
                    "type": "string",
                    "description": "Data-root id or path. Default canonical_research.",
                }
            },
            "additionalProperties": False,
        },
        "handler": tool_list_reports,
    },
    {
        "name": "parse_report",
        "description": (
            "Parse a research report .md (volume_event or strategy_tribunal) "
            "into JSON: a 'headline' of mapped metrics (trades, return, "
            "drawdown, worst 90d, splits, OOS, promotion gate, verdict), all "
            "Key: value 'fields', every markdown 'table', and the methodology "
            "run label. 'path' is a report file or a directory containing one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Report .md file, or a directory containing one.",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "handler": tool_parse_report,
    },
    {
        "name": "audit_run_artifacts",
        "description": (
            "Audit a strategy run/report directory for the artifacts the "
            "repo's backtest-integrity standard requires (ledger, equity "
            "curve, splits, drawdown, OOS, cost model, config/data identity, "
            "run label) and return a completeness verdict. Artifact check "
            "only - does not verify PIT/causal correctness."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "A report file or a run/report directory.",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "handler": tool_audit_run_artifacts,
    },
    {
        "name": "current_state",
        "description": (
            "Return the repo's STATE.md verbatim — the single-page summary of "
            "what research phases have completed, what's running, what's "
            "next, current decision rules, and pre-committed non-negotiables. "
            "FIRST READ for any new session: a 60-second orient that points "
            "at the right docs for whatever task is at hand. No arguments."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": tool_current_state,
    },
    {
        "name": "apply_decision_rule",
        "description": (
            "Apply the Strictness Manifesto decision rule (pre-registered in "
            "docs/research_summary.md) to a sweep "
            "summary CSV. Returns the per-cell verdict table "
            "(candidate / reject / inconclusive) plus a candidate ranking "
            "by combined-venue Sharpe (honours the FDR ceiling). Single-"
            "sourced from scripts/apply_decision_rule.py; equivalent to "
            "running that script directly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary_csv": {
                    "type": "string",
                    "description": "Path to the sweep summary CSV (data-root id, repo-relative, or absolute).",
                },
                "control": {
                    "type": "string",
                    "description": "Control cell_id. Default: 00_baseline.",
                },
                "rule": {
                    "type": "string",
                    "enum": ["manifesto", "legacy"],
                    "description": "Decision-rule preset. Default: manifesto (stricter).",
                },
            },
            "required": ["summary_csv"],
            "additionalProperties": False,
        },
        "handler": tool_apply_decision_rule,
    },
]

TOOL_BY_NAME = dict((t["name"], t) for t in TOOLS)


def dispatch(method, params):
    if method == "initialize":
        client_proto = params.get("protocolVersion")
        proto = client_proto if isinstance(client_proto, str) else PROTOCOL_VERSION
        return {
            "protocolVersion": proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": t["inputSchema"],
                }
                for t in TOOLS
            ]
        }
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        tool = TOOL_BY_NAME.get(name)
        if tool is None:
            return {
                "content": [{"type": "text", "text": "unknown tool: %r" % (name,)}],
                "isError": True,
            }
        try:
            text = tool["handler"](arguments)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as exc:  # tool-level failure surfaces as isError
            log("tool '%s' failed: %s" % (name, traceback.format_exc()))
            return {
                "content": [{"type": "text", "text": "tool error: %s" % exc}],
                "isError": True,
            }
    if method == "ping":
        return {}
    raise RpcError(-32601, "method not found: %s" % method)


def handle_message(msg):
    if not isinstance(msg, dict):
        return
    method = msg.get("method")
    msg_id = msg.get("id")
    if method is None:
        return  # a response frame or malformed input; nothing to do
    if msg_id is None:
        return  # notification (e.g. notifications/initialized); no reply
    try:
        result = dispatch(method, msg.get("params") or {})
        send({"jsonrpc": "2.0", "id": msg_id, "result": result})
    except RpcError as exc:
        send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": exc.code, "message": exc.message},
        })
    except Exception as exc:
        log("internal error on %s: %s" % (method, traceback.format_exc()))
        send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32603, "message": "internal error: %s" % exc},
        })


def main():
    log("started; repo_root=%s" % REPO_ROOT)
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                log("skipped non-JSON line")
                continue
            handle_message(msg)
    except KeyboardInterrupt:
        pass
    log("stopped")


if __name__ == "__main__":
    main()
