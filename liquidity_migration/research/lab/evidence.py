"""The six-item evidence note from docs/research/governance.md, as markdown.

Pure formatting. The grid prints every cell it is given, the era table every
row, and costs sit beside the gross numbers rather than in a footnote.
"""
from __future__ import annotations

import math
from typing import Mapping, Sequence

Row = Mapping[str, object]

SECTIONS: tuple[str, ...] = (
    "1. Claim, and the decision it informs",
    "2. What data shaped the idea; what data graded it",
    "3. Scope",
    "4. Effect size and uncertainty, costs next to gross",
    "5. Where the artifacts and config commit live",
    "6. What this does not show",
)
GRID_HEADING = "Every cell"
ERA_HEADING = "By era"
COST_HEADING = "Costs next to gross"


def _cell(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return "—" if math.isnan(value) else f"{value:.4g}"
    return str(value).replace("|", "\\|")


def render_table(rows: Sequence[Row]) -> str:
    """A markdown table whose columns are the keys in first-seen order; a missing key prints as a dash."""
    if not rows:
        return "(no rows)"
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(row.get(c)) for c in columns) + " |")
    return "\n".join(lines)


def evidence_note(
    *,
    title: str,
    claim: str,
    decision: str,
    shaped_by: str,
    graded_on: str,
    scope: str,
    effect: str,
    grid: Sequence[Row],
    eras: Sequence[Row],
    cost_model: str,
    artifacts: str,
    boundary: str,
    costs: Sequence[Row] | None = None,
) -> str:
    """Render the note. ``grid`` is every cell run, ``eras`` the per-era split, ``costs`` gross-versus-net rows."""
    parts = [
        f"# {title}",
        "",
        f"## {SECTIONS[0]}",
        "",
        claim.strip(),
        "",
        f"Decision: {decision.strip()}",
        "",
        f"## {SECTIONS[1]}",
        "",
        f"Shaped by: {shaped_by.strip()}",
        "",
        f"Graded on: {graded_on.strip()}",
        "",
        f"## {SECTIONS[2]}",
        "",
        scope.strip(),
        "",
        f"## {SECTIONS[3]}",
        "",
        effect.strip(),
        "",
        f"### {GRID_HEADING}",
        "",
        render_table(grid),
        "",
        f"### {ERA_HEADING}",
        "",
        render_table(eras),
        "",
        f"### {COST_HEADING}",
        "",
        f"Cost model: {cost_model.strip()}",
    ]
    if costs:
        parts += ["", render_table(costs)]
    parts += [
        "",
        f"## {SECTIONS[4]}",
        "",
        artifacts.strip(),
        "",
        f"## {SECTIONS[5]}",
        "",
        boundary.strip(),
        "",
    ]
    return "\n".join(parts)
