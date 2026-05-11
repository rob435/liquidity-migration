from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import write_research_manifest as manifest_script


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    output_dir = Path(args.output_dir)
    record = build_research_run_record(
        repo_root=repo_root,
        name=args.name,
        strategy=args.strategy,
        status=args.status,
        bias=args.bias,
        intent=args.intent,
        constraints=args.constraint or (),
        decision=args.decision,
        next_step=args.next_step,
        notes=args.note or (),
        tags=args.tag or (),
        data_roots=args.data_root or (),
        config_paths=args.config or (),
        artifact_paths=args.artifact or (),
        artifact_globs=tuple(args.artifact_glob or ()),
    )
    write_research_run_record(record, output_dir=output_dir)
    missing = sum(1 for row in record["artifacts"] if row["status"] == "missing")
    print(
        f"research run record artifacts={len(record['artifacts'])} missing={missing} "
        f"path={output_dir / 'runs' / (record['run_id'] + '.md')}"
    )
    return 2 if args.strict and missing else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Append an auditable research run record and artifact manifest.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output-dir", default="data/research_reports/research_log")
    parser.add_argument("--name", required=True)
    parser.add_argument("--strategy", required=True)
    parser.add_argument(
        "--status",
        default="benchmark",
        choices=("idea", "benchmark", "candidate", "promoted", "rejected", "blocked"),
    )
    parser.add_argument(
        "--bias",
        default="unknown",
        choices=("unknown", "current_universe_biased", "point_in_time", "forward_paper", "bybit_demo"),
    )
    parser.add_argument("--intent", default="")
    parser.add_argument("--constraint", action="append")
    parser.add_argument("--decision", default="")
    parser.add_argument("--next-step", default="")
    parser.add_argument("--note", action="append")
    parser.add_argument("--tag", action="append")
    parser.add_argument("--data-root", action="append")
    parser.add_argument("--config", action="append")
    parser.add_argument("--artifact", action="append")
    parser.add_argument("--artifact-glob", action="append")
    parser.add_argument("--strict", action="store_true", help="Exit 2 if any requested artifact is missing.")
    return parser.parse_args()


def build_research_run_record(
    *,
    repo_root: Path,
    name: str,
    strategy: str,
    status: str,
    bias: str,
    intent: str,
    constraints: tuple[str, ...],
    decision: str,
    next_step: str,
    notes: tuple[str, ...],
    tags: tuple[str, ...],
    data_roots: tuple[str, ...],
    config_paths: tuple[str, ...],
    artifact_paths: tuple[str, ...],
    artifact_globs: tuple[str, ...],
) -> dict[str, Any]:
    generated_at = datetime.now(UTC).isoformat()
    run_id = _run_id(generated_at, name)
    configs = [manifest_script.artifact_row(repo_root, Path(path)) for path in config_paths]
    artifact_list = manifest_script._artifact_path_list(
        repo_root,
        artifact_paths=list(artifact_paths),
        artifact_globs=artifact_globs,
    )
    artifacts = [manifest_script.artifact_row(repo_root, path) for path in artifact_list]
    return {
        "run_id": run_id,
        "generated_at": generated_at,
        "name": name,
        "strategy": strategy,
        "status": status,
        "bias": bias,
        "intent": intent,
        "constraints": list(constraints),
        "decision": decision,
        "next_step": next_step,
        "notes": list(notes),
        "tags": list(tags),
        "repo_root": str(repo_root),
        "git": manifest_script.git_metadata(repo_root),
        "data_roots": [_data_root_row(repo_root, root) for root in data_roots],
        "configs": configs,
        "gates": _gate_rows(repo_root, artifacts),
        "artifacts": artifacts,
    }


def write_research_run_record(record: dict[str, Any], *, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = output_dir / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{record['run_id']}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    (run_dir / f"{record['run_id']}.md").write_text(format_research_run_record(record), encoding="utf-8")

    log_path = output_dir / "research_log.jsonl"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_compact_record(record), sort_keys=True) + "\n")

    records = _read_log_records(log_path)
    (output_dir / "research_log.md").write_text(format_research_log(records), encoding="utf-8")


def format_research_run_record(record: dict[str, Any]) -> str:
    git = record.get("git", {})
    artifacts = record.get("artifacts", [])
    configs = record.get("configs", [])
    data_roots = record.get("data_roots", [])
    gates = record.get("gates", [])
    present = sum(1 for row in artifacts if row.get("status") == "present")
    missing = sum(1 for row in artifacts if row.get("status") == "missing")
    lines = [
        f"# Research Run: {record.get('name', '')}",
        "",
        f"Run ID: `{record.get('run_id')}`",
        f"Generated: {record.get('generated_at')}",
        f"Strategy: `{record.get('strategy')}`",
        f"Status: `{record.get('status')}`",
        f"Bias label: `{record.get('bias')}`",
        f"Git: `{git.get('branch', 'unknown')}` @ `{str(git.get('commit', 'unknown'))[:12]}`",
        f"Git dirty: {git.get('dirty', False)}",
        f"Artifacts: {present} present, {missing} missing",
        "",
        "## Intent",
        "",
        record.get("intent") or "n/a",
        "",
        "## Decision",
        "",
        record.get("decision") or "n/a",
        "",
        "## Next Step",
        "",
        record.get("next_step") or "n/a",
        "",
    ]
    lines.extend(_bullet_section("Constraints", record.get("constraints", [])))
    lines.extend(_bullet_section("Notes", record.get("notes", [])))
    lines.extend(
        [
            "## Data Roots",
            "",
            "| Status | Path |",
            "|---|---|",
        ]
    )
    for row in data_roots:
        lines.append(f"| {row.get('status')} | `{row.get('path')}` |")
    if not data_roots:
        lines.append("|  |  |")
    lines.extend(
        [
            "",
            "## Configs",
            "",
            "| Status | Bytes | SHA256 | Path |",
            "|---|---:|---|---|",
        ]
    )
    for row in configs:
        lines.append(_artifact_line(row))
    if not configs:
        lines.append("|  |  |  |  |")
    lines.extend(
        [
            "",
            "## Gates",
            "",
            "| Gate | Status | Rows | Promotable | Path |",
            "|---|---|---:|---:|---|",
        ]
    )
    for row in gates:
        lines.append(
            f"| {row.get('name')} | {row.get('status')} | {row.get('rows', '')} | "
            f"{row.get('promotable_rows', '')} | `{row.get('path')}` |"
        )
    if not gates:
        lines.append("|  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            "| Status | Bytes | SHA256 | Path |",
            "|---|---:|---|---|",
        ]
    )
    for row in artifacts:
        lines.append(_artifact_line(row))
    if not artifacts:
        lines.append("|  |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def format_research_log(records: list[dict[str, Any]]) -> str:
    lines = [
        "# Research Log",
        "",
        "Append-only index of serious research runs. Use this before promoting or retesting an idea.",
        "",
        "| Time | Strategy | Status | Bias | Intent | Decision | Git | Missing | Run |",
        "|---|---|---|---|---|---|---|---:|---|",
    ]
    for row in sorted(records, key=lambda item: str(item.get("generated_at", "")), reverse=True)[:100]:
        lines.append(
            f"| {row.get('generated_at', '')} | {row.get('strategy', '')} | {row.get('status', '')} | "
            f"{row.get('bias', '')} | {_short(row.get('intent', ''))} | {_short(row.get('decision', ''))} | "
            f"`{str(row.get('git_commit', ''))[:12]}` | {row.get('missing_artifacts', 0)} | "
            f"`runs/{row.get('run_id')}.md` |"
        )
    lines.append("")
    return "\n".join(lines)


def _compact_record(record: dict[str, Any]) -> dict[str, Any]:
    artifacts = record.get("artifacts", [])
    git = record.get("git", {})
    return {
        "run_id": record.get("run_id"),
        "generated_at": record.get("generated_at"),
        "name": record.get("name"),
        "strategy": record.get("strategy"),
        "status": record.get("status"),
        "bias": record.get("bias"),
        "intent": record.get("intent"),
        "decision": record.get("decision"),
        "next_step": record.get("next_step"),
        "tags": record.get("tags", []),
        "git_commit": git.get("commit", "unknown"),
        "git_branch": git.get("branch", "unknown"),
        "git_dirty": git.get("dirty", False),
        "artifact_count": len(artifacts),
        "missing_artifacts": sum(1 for row in artifacts if row.get("status") == "missing"),
    }


def _read_log_records(path: Path) -> list[dict[str, Any]]:
    records = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))
    return records


def _data_root_row(repo_root: Path, value: str) -> dict[str, Any]:
    path = Path(value)
    absolute = path if path.is_absolute() else repo_root / path
    return {
        "path": manifest_script._relative_path(repo_root, absolute),
        "status": "present" if absolute.exists() and absolute.is_dir() else "missing",
    }


def _gate_rows(repo_root: Path, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in artifacts:
        if row.get("status") != "present":
            continue
        path = repo_root / str(row.get("path"))
        recognized_json = (
            path.name == "daily_close_fade_sharpe_target.json"
            or path.name == "daily_close_fade_report.json"
            or "promotion" in path.name
            or "readiness" in path.name
        )
        if not path.name.endswith(".json") or not recognized_json:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if path.name == "daily_close_fade_sharpe_target.json":
            rows_payload = payload.get("rows", {})
            candidates = payload.get("candidates", [])
            rows.append(
                {
                    "name": "daily_close_fade_sharpe_target",
                    "path": row.get("path"),
                    "status": "pass" if payload.get("status") == "target_hit" and candidates else "fail",
                    "rows": int(float(rows_payload.get("grid") or 0)) if isinstance(rows_payload, dict) else "",
                    "promotable_rows": len(candidates) if isinstance(candidates, list) else 0,
                }
            )
        elif path.name == "daily_close_fade_report.json" and "backtest_validity" in payload:
            validity = payload.get("backtest_validity", {})
            rows_payload = payload.get("rows", {})
            rows.append(
                {
                    "name": path.parent.name,
                    "path": row.get("path"),
                    "status": "pass" if validity.get("label") == "candidate" else "fail",
                    "rows": int(float(rows_payload.get("trades") or 0)) if isinstance(rows_payload, dict) else "",
                    "promotable_rows": 1 if validity.get("can_support_promotion") else 0,
                }
            )
        elif "promotable_rows" in payload:
            rows.append(
                {
                    "name": path.parent.name if path.parent.name != "promotion" else path.parent.parent.name,
                    "path": row.get("path"),
                    "status": "pass" if int(float(payload.get("promotable_rows") or 0)) > 0 else "fail",
                    "rows": int(float(payload.get("rows") or 0)),
                    "promotable_rows": int(float(payload.get("promotable_rows") or 0)),
                }
            )
        elif "overall_status" in payload:
            rows.append(
                {
                    "name": "readiness",
                    "path": row.get("path"),
                    "status": payload.get("overall_status"),
                    "rows": "",
                    "promotable_rows": "",
                }
            )
    return rows


def _artifact_line(row: dict[str, Any]) -> str:
    return (
        f"| {row.get('status')} | {row.get('bytes', '')} | "
        f"`{str(row.get('sha256', ''))[:16]}` | `{row.get('path')}` |"
    )


def _bullet_section(title: str, items: list[str]) -> list[str]:
    lines = [f"## {title}", ""]
    if items:
        lines.extend(f"- {item}" for item in items)
    else:
        lines.append("- n/a")
    lines.append("")
    return lines


def _run_id(generated_at: str, name: str) -> str:
    timestamp = generated_at.replace("-", "").replace(":", "").split(".")[0].replace("T", "-")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-") or "research-run"
    return f"{timestamp}-{slug}"


def _short(value: Any, limit: int = 96) -> str:
    text = str(value or "").replace("|", "/").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


if __name__ == "__main__":
    raise SystemExit(main())
