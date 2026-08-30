#!/usr/bin/env python3
"""Render the maker canary's economic TOML fields from its registered rule."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTERED = ROOT / "configs" / "lane2_toxic_flow_quoter_v1.json"
DEFAULT_TEMPLATE = ROOT / "deploy" / "engine.mainnet.toml.template"
BEGIN = "# BEGIN GENERATED MAKER CANARY RULE -- render_maker_canary_config.py"
END = "# END GENERATED MAKER CANARY RULE"

RULE_FIELDS = {
    "symbol",
    "quote_notional_usdt",
    "max_position_usdt",
    "half_spread_bps",
    "requote_bps",
    "skew_bps",
    "stop_loss_fraction",
    "maker_fee_bps",
    "min_edge_bps",
    "volatility_multiplier",
    "book_lean_bps",
    "signal_half_life_ms",
    "queue_reprice_edge_bps",
    "flow",
}
FLOW_FIELDS = {
    "fast_half_life_ms",
    "slow_half_life_ms",
    "fast_weight",
    "slow_weight",
    "response_bps",
    "max_widen_bps",
    "pull_score",
    "near_depth_bps",
    "volatility_depth_multiplier",
    "max_score",
}


def _scalar(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value)
    raise ValueError(f"cannot render TOML scalar {value!r}")


def render_rule(rule: dict[str, Any]) -> str:
    unknown = set(rule) ^ RULE_FIELDS
    if unknown:
        raise ValueError(f"registered rule fields changed: {sorted(unknown)}")
    flow = rule["flow"]
    unknown_flow = set(flow) ^ FLOW_FIELDS
    if unknown_flow:
        raise ValueError(f"registered flow fields changed: {sorted(unknown_flow)}")

    rows: list[tuple[str, Any]] = [
        ("symbols", [rule["symbol"]]),
        ("qty_usdt", rule["quote_notional_usdt"]),
        ("max_position_usdt", rule["max_position_usdt"]),
        ("half_spread_bps", rule["half_spread_bps"]),
        ("requote_bps", rule["requote_bps"]),
        ("skew_bps", rule["skew_bps"]),
        ("stop_loss_fraction", rule["stop_loss_fraction"]),
        ("maker_fee_bps", rule["maker_fee_bps"]),
        ("min_edge_bps", rule["min_edge_bps"]),
        ("volatility_multiplier", rule["volatility_multiplier"]),
        ("book_lean_bps", rule["book_lean_bps"]),
        ("signal_half_life_ms", rule["signal_half_life_ms"]),
        ("flow_fast_half_life_ms", flow["fast_half_life_ms"]),
        ("flow_slow_half_life_ms", flow["slow_half_life_ms"]),
        ("flow_fast_weight", flow["fast_weight"]),
        ("flow_slow_weight", flow["slow_weight"]),
        ("flow_response_bps", flow["response_bps"]),
        ("flow_max_widen_bps", flow["max_widen_bps"]),
        ("flow_depth_bps", flow["near_depth_bps"]),
        (
            "flow_volatility_depth_multiplier",
            flow["volatility_depth_multiplier"],
        ),
        ("flow_max_score", flow["max_score"]),
        ("queue_reprice_edge_bps", rule["queue_reprice_edge_bps"]),
    ]
    if flow["pull_score"] is not None:
        rows.insert(18, ("flow_pull_score", flow["pull_score"]))

    lines = [BEGIN]
    for key, value in rows:
        if key == "symbols":
            rendered = "[" + ", ".join(_scalar(item) for item in value) + "]"
        else:
            rendered = _scalar(value)
        lines.append(f"{key} = {rendered}")
    lines.append(END)
    return "\n".join(lines)


def expected_template(template: str, generated: str) -> str:
    pattern = re.compile(f"{re.escape(BEGIN)}.*?{re.escape(END)}", re.DOTALL)
    if not pattern.search(template):
        raise ValueError("maker canary generated boundaries are missing")
    return pattern.sub(generated, template, count=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registered-config", type=Path, default=DEFAULT_REGISTERED)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    registered = json.loads(args.registered_config.read_text())
    generated = render_rule(registered["rule"])
    current = args.template.read_text()
    expected = expected_template(current, generated)
    if args.check:
        if current == expected:
            return 0
        sys.stderr.writelines(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile=str(args.template),
                tofile="generated from registered maker rule",
            )
        )
        return 1
    if current != expected:
        args.template.write_text(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
