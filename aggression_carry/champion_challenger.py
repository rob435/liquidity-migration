from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ACTIVE_ORDER_SUBMITTING_PROFILE = "demo_relaxed"
ACTIVE_ORDER_SUBMITTING_STRATEGY_ID = "demo_relaxed_liqmig_q40_h3_tp25_g100_qsqueeze"


@dataclass(frozen=True, slots=True)
class ChallengerSpec:
    challenger_id: str
    role: str
    surface: str
    submits_orders: bool
    command: str
    purpose: str
    promotion_gate: str


def champion_challenger_specs() -> tuple[ChallengerSpec, ...]:
    return (
        ChallengerSpec(
            challenger_id="champion_demo_relaxed_submit",
            role="trade_champion",
            surface="bybit_demo_event_service",
            submits_orders=True,
            command=(
                "STRATEGY_PROFILE=demo_relaxed SUBMIT_ORDERS=1 CONFIRM_DEMO_ORDERS=1 "
                "scripts/run_bybit_demo_event_engine.sh"
            ),
            purpose="Only live order-submitting entry stack; higher-frequency demo observation profile.",
            promotion_gate="Already selected as demo-only champion; not real-money promoted.",
        ),
        ChallengerSpec(
            challenger_id="shadow_current_promoted",
            role="shadow_challenger",
            surface="event_demo_cycle_dry_run",
            submits_orders=False,
            command=(
                "python -m aggression_carry --config configs/volume_alpha.default.yaml "
                "--data-root data/shadow-current-promoted event-demo-cycle --strategy-profile promoted --record-dry-run"
            ),
            purpose="Sparse promoted-grade signal stream for live-vs-backtest drift without orders.",
            promotion_gate="Must clear Model Court with execution drift before it can submit orders.",
        ),
        ChallengerSpec(
            challenger_id="shadow_demo_relaxed_without_crowding",
            role="shadow_challenger",
            surface="volume_events_research",
            submits_orders=False,
            command=(
                "python -m aggression_carry --config configs/volume_alpha.default.yaml --data-root DATA_ROOT "
                "volume-events --liquidity-migration-crowding-filter none "
                "--report-dir DATA_ROOT/reports/challenger_relaxed_no_crowding"
            ),
            purpose="Quantifies whether the crowding veto is still adding value versus raw relaxed flow.",
            promotion_gate="Requires better full-PIT, stress, and live shadow evidence than union_pathology.",
        ),
        ChallengerSpec(
            challenger_id="shadow_tiered_execution_sniper",
            role="shadow_challenger",
            surface="volume_events_research",
            submits_orders=False,
            command=(
                "python -m aggression_carry --config configs/volume_alpha.default.yaml --data-root DATA_ROOT "
                "volume-events --entry-policy tiered_execution_sniper "
                "--report-dir DATA_ROOT/reports/challenger_tiered_execution_sniper"
            ),
            purpose="Keeps testing causal sniper-entry variants without touching the order path.",
            promotion_gate="Must beat promoted_quality_squeeze after causal fallback, costs, and stress.",
        ),
        ChallengerSpec(
            challenger_id="shadow_execution_pullback_guard",
            role="shadow_challenger",
            surface="volume_events_research",
            submits_orders=False,
            command=(
                "python -m aggression_carry --config configs/volume_alpha.default.yaml --data-root DATA_ROOT "
                "volume-events --entry-policy execution_pullback_guard "
                "--report-dir DATA_ROOT/reports/challenger_execution_pullback_guard"
            ),
            purpose="Execution-only pullback/giveback challenger for entry sharpness research.",
            promotion_gate="Rejected until it improves both promoted and relaxed ledgers after Model Court.",
        ),
        ChallengerSpec(
            challenger_id="shadow_volume_shelf_hedge_overlay",
            role="shadow_challenger",
            surface="portfolio_hedge_research",
            submits_orders=False,
            command=(
                "python -m aggression_carry portfolio-hedge --short-report-dir SHORT_REPORT "
                "--long-report-dir LONG_VOLUME_SHELF_REPORT --hedge-weights 0.25,0.5,1.0 "
                "--report-dir DATA_ROOT/reports/challenger_volume_shelf_hedge_overlay"
            ),
            purpose="Tracks the best current long hedge candidate as a portfolio overlay only.",
            promotion_gate="Standalone long or combined book must clear stress and Model Court.",
        ),
    )


def run_champion_challenger_audit(output_dir: str | Path) -> dict[str, Any]:
    target = Path(output_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    specs = champion_challenger_specs()
    violations = _audit_violations(specs)
    status = "PASS" if not violations else "FAIL"
    payload = {
        "status": status,
        "active_order_submitting_profile": ACTIVE_ORDER_SUBMITTING_PROFILE,
        "active_order_submitting_strategy_id": ACTIVE_ORDER_SUBMITTING_STRATEGY_ID,
        "specs": [asdict(spec) for spec in specs],
        "violations": violations,
        "output_files": {
            "json": str(target / "champion_challenger_manifest.json"),
            "markdown": str(target / "champion_challenger_manifest.md"),
        },
    }
    Path(payload["output_files"]["json"]).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    Path(payload["output_files"]["markdown"]).write_text(format_champion_challenger_manifest(payload), encoding="utf-8")
    return payload


def format_champion_challenger_manifest(payload: dict[str, Any]) -> str:
    lines = [
        "# Champion / Challenger Manifest",
        "",
        f"- Status: `{payload['status']}`",
        f"- Active order-submitting profile: `{payload['active_order_submitting_profile']}`",
        f"- Active order-submitting strategy id: `{payload['active_order_submitting_strategy_id']}`",
        "",
        "| ID | Role | Surface | Submits Orders | Purpose | Promotion Gate |",
        "|---|---|---|---:|---|---|",
    ]
    for spec in payload["specs"]:
        lines.append(
            f"| `{spec['challenger_id']}` | {spec['role']} | {spec['surface']} | "
            f"{str(spec['submits_orders']).lower()} | {spec['purpose']} | {spec['promotion_gate']} |"
        )
    lines.extend(["", "## Commands", ""])
    for spec in payload["specs"]:
        lines.extend([f"### `{spec['challenger_id']}`", "", "```bash", spec["command"], "```", ""])
    if payload["violations"]:
        lines.extend(["## Violations", ""])
        for violation in payload["violations"]:
            lines.append(f"- {violation}")
    else:
        lines.extend(
            [
                "## Audit",
                "",
                "Exactly one stack is allowed to submit orders. Every challenger command is dry-run/research-only and contains no order-submission flag.",
                "",
            ]
        )
    return "\n".join(lines)


def _audit_violations(specs: tuple[ChallengerSpec, ...]) -> list[str]:
    violations = []
    submitters = [spec for spec in specs if spec.submits_orders]
    if len(submitters) != 1:
        violations.append(f"Expected exactly one order-submitting champion; found {len(submitters)}.")
    elif submitters[0].challenger_id != "champion_demo_relaxed_submit":
        violations.append(f"Unexpected order-submitting champion: {submitters[0].challenger_id}.")
    for spec in specs:
        command = spec.command
        if spec.role == "shadow_challenger" and spec.submits_orders:
            violations.append(f"{spec.challenger_id} is a shadow challenger but submits_orders=true.")
        if spec.role == "shadow_challenger" and ("--submit-orders" in command or "SUBMIT_ORDERS=1" in command):
            violations.append(f"{spec.challenger_id} command contains an order-submission flag.")
        if spec.submits_orders and ACTIVE_ORDER_SUBMITTING_PROFILE not in command:
            violations.append(f"{spec.challenger_id} submits orders without the active profile.")
    return violations
