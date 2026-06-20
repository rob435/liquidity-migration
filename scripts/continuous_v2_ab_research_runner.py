#!/usr/bin/env python3
"""Continuous V2 deep A/B research foundation.

This is the checked-in dispatcher required by
docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md.

The first foundation pass is intentionally conservative:

- build a feature almanac from repo code and the full-PIT roots;
- reproduce the registered V2 control through repo-native continuous component
  ledgers plus the frozen rebalance/hedge object;
- keep serious experimental arms registered but blocked until the almanac marks
  their required features causal and covered.

No local artifact directory is imported. Old W5/W6 outputs can inform ideas, but
not this runner's evidence path.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import random
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_DAY, MS_PER_HOUR, calendar_shift  # noqa: E402
from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    _fresh_entries,
    _listing_ts_by_symbol,
    build_continuous_panel,
    run_continuous_event_research,
)
from liquidity_migration.continuous_forward_replay import (  # noqa: E402
    FROZEN_FORWARD_CONFIG,
    build_full_ledger,
    frozen_config_hash,
    frozen_hedge_regime,
    frozen_hedge_rule,
    frozen_rebalance_rule,
)
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousRebalanceRule,
    decompose_continuous_components,
)
from liquidity_migration.continuous_regime import btcvol_intensity_series  # noqa: E402
from liquidity_migration.signal_harness import (  # noqa: E402
    _autodetect_dataset_names,
    _date_str_to_ms,
    _read_window,
)
from liquidity_migration.storage import resolve_dataset_name  # noqa: E402
from liquidity_migration.trade_lifecycle import annualized_sharpe  # noqa: E402

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {
    "bybit": SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}
VENUES = tuple(ROOTS)
PREREGISTRATION = "docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md"
AMENDMENT_A4B = "docs/preregistration/2026-06-19-continuous-v2-ab-amendment-a4b-price-carry-regime.md"
AMENDMENT_BINANCE_FLOW = "docs/preregistration/2026-06-19-continuous-v2-ab-amendment-binance-only-flow.md"
AMENDMENT_C_FLOW_OVERLAY = "docs/preregistration/2026-06-19-continuous-v2-c-flow-overlay-construction.md"

# Claimed venue scope tags. Two-venue arms can become candidates; the amended
# C-book flow branch (2026-06-19) is Binance-only exploratory and can never clear
# the Tier-2 candidate bar or support Bybit demo/paper wiring.
CLAIMED_SCOPE_TWO_VENUE = "both_venue_candidate_track"
CLAIMED_SCOPE_BINANCE_FLOW = "binance_only_flow_exploratory"

CONTROL_ARM = "V2_CONTROL"
A4B_ARM = "A4B_PRICE_CARRY_REGIME_HEDGE_INTENSITY"
A4B_HASH_ARM = "A4B_PRICE_CARRY_HASH_CONTROL"
A4B_ARMS = {A4B_ARM, A4B_HASH_ARM}

# Binance-only exploratory Problem Book C flow branch (2026-06-19 amendment).
# These arm ids are scoped to venue=binance and are forced to the exploratory
# label with no Tier-2 candidate pass, regardless of headline metrics.
C0_FLOW_SCREEN_ARM = "C0_ORDERFLOW_SCREEN_BINANCE_ONLY"
C1_FLOW_SIZING_ARM = "C1_FLOW_RESID_FEATURE_SIZING_BINANCE_ONLY"
C2_FLOW_ARM = "C2_MARKET_FLOW_HEDGE_INTENSITY_BINANCE_ONLY"
C3_FLOW_ARM = "C3_FLOW_SQUEEZE_HEDGE_INTENSITY_BINANCE_ONLY"
C4_FLOW_ADMISSION_ARM = "C4_FLOW_DIVERGENCE_ADMISSION_BINANCE_ONLY"
C5_FLOW_EXIT_ARM = "C5_FLOW_UNWIND_EXIT_SHADOW_BINANCE_ONLY"
C6_FLOW_NONLINEAR_ARM = "C6_NONLINEAR_FLOW_SCORE_BINANCE_ONLY"
C7_FLOW_HASH_ARM = "C7_FLOW_HASH_CONTROL_BINANCE_ONLY"
C1H_FLOW_SIZING_HASH_ARM = "C1H_FLOW_RESID_SIZING_HASH_CONTROL_BINANCE_ONLY"
BINANCE_ONLY_FLOW_ARMS = {
    C0_FLOW_SCREEN_ARM,
    C1_FLOW_SIZING_ARM,
    C1H_FLOW_SIZING_HASH_ARM,
    C2_FLOW_ARM,
    C3_FLOW_ARM,
    C4_FLOW_ADMISSION_ARM,
    C5_FLOW_EXIT_ARM,
    C6_FLOW_NONLINEAR_ARM,
    C7_FLOW_HASH_ARM,
}

AMENDMENT_B_SIZING = "docs/preregistration/2026-06-19-continuous-v2-b-score-sizing-construction.md"
AMENDMENT_C1_FLOW_SIZING = "docs/preregistration/2026-06-19-continuous-v2-c1-flow-sizing-construction.md"
AMENDMENT_F2_EXIT_ALPHA = "docs/preregistration/2026-06-19-continuous-v2-f2-exit-alpha-construction.md"

# Problem Book F phase-2 exit-alpha lifecycle variants (two-venue candidate-track):
# re-run the components with a raised take-profit threshold and the frozen
# rebalance/hedge ledger to measure the ACTUAL both-venue MAR of the exit change
# (the per-trade sweep only saw the un-rebalanced contribution).
EXIT_TP12_ARM = "EXIT_TP12_BOTH_VENUE"
EXIT_TP15_ARM = "EXIT_TP15_BOTH_VENUE"
TP_VARIANT_SPECS: dict[str, float] = {EXIT_TP12_ARM: 0.12, EXIT_TP15_ARM: 0.15}
TP_VARIANT_ARMS = set(TP_VARIANT_SPECS)

# --- Continuous V2 next-level plan: the two frozen baseline objects ----------------
# docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md requires
# every future A/B arm to be judged vs BOTH frozen baselines:
#   * V2_CONTROL (= V2_LIVE_RESEARCH_CONTROL): post-override object, TP 0.12, daily
#     vol-target adjuster OFF (frozen_rebalance_rule().enabled is False).
#   * V2_EVIDENCE_ANCHOR: pre-override object, TP 0.10, daily vol-target adjuster ON
#     (prior max4 wiring: enabled=True, max_scale 4.0, target_daily_vol 0.045, w90,
#     ddh -0.04). Same frozen component object otherwise.
# The anchor reuses the SAME apply_rebalance_rule path via build_full_ledger's
# rebalance_rule kwarg; it does NOT touch frozen_config_hash or the live forward
# ledger (which is always built with rebalance_rule=None).
ANCHOR_ARM = "V2_EVIDENCE_ANCHOR"
AMENDMENT_NEXT_LEVEL = "docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md"
OBJECT_POLICY: dict[str, dict[str, Any]] = {
    ANCHOR_ARM: {"take_profit_pct": 0.10, "rebalance_enabled": True},
}


def tp_override_for(arm_id: str) -> float | None:
    policy = OBJECT_POLICY.get(arm_id)
    if policy is not None and "take_profit_pct" in policy:
        return float(policy["take_profit_pct"])
    return TP_VARIANT_SPECS.get(arm_id)


def _arm_rebalance_rule(arm_id: str) -> ContinuousRebalanceRule:
    """Daily vol-target adjuster state for an arm's full-ledger reconstruction.

    Default = the frozen rule (operator-override OFF). The V2_EVIDENCE_ANCHOR baseline
    re-enables it (prior max4 wiring) by flipping ``enabled`` only; every other param
    stays frozen so the anchor is the SAME object as pre-override. All other arms get
    the frozen rule unchanged (byte-identical to the prior hardwired path).
    """
    rule = frozen_rebalance_rule()
    policy = OBJECT_POLICY.get(arm_id)
    if policy is not None and policy.get("rebalance_enabled"):
        rule = replace(rule, enabled=True)
    return rule

# Two-venue candidate-track Problem Book B sizing arms. Entries unchanged; a causal
# mean-1 per-trade size multiplier from a single conviction feature is passed to the
# engine via size_mult_lookup (the daily vol-target rebalance keeps book gross fixed,
# so this is a relative within-book reweighting, not a leverage-up).
B1_SCORE_SIZING_ARM = "B1_SCORE_MARGIN_SIZING"
B1P_PATH_SIZING_ARM = "B1P_PATH_SHAPE_SIZING"
B6_SCORE_HASH_ARM = "B6_SCORE_MARGIN_HASH_CONTROL"
B6P_PATH_HASH_ARM = "B6P_PATH_SHAPE_HASH_CONTROL"

A4B_FEATURES = (
    "btc_vol_percentile_250d",
    "market_dispersion_1d",
    "market_breadth_1d",
    "alt_minus_btc_1d",
    "funding_level",
    "funding_change",
    "premium_level",
    "premium_change",
    "btc_drawdown_30d",
)
PHASE0_ARMS = {CONTROL_ARM, "V2_CONTROL_DELAYED_FEATURES", ANCHOR_ARM}
RUN_LABEL = "exploratory_registered_foundation"
AUDIT_RUN_LABEL = "exploratory"
ARTIFACT_WRITER_VERSION = "continuous_v2_ab_foundation_v2"
DEFAULT_START = "2023-04-01"
SPLIT_BOUNDARY = "2025-06-01"


@dataclass(frozen=True)
class ComponentSpec:
    key: str
    live_tag: str
    entry_event_trigger: str
    age_days_min: int
    take_profit_pct: float
    weight: float


# OPERATOR OVERRIDE 2026-06-19: component take-profit promoted 0.10 -> 0.12 system-wide
# (matches the deployed continuous_demo profile). The daily volatility adjuster is also
# disabled in FROZEN_FORWARD_CONFIG (enabled=False), so the research control now equals the
# new promoted object {TP12, vol-adjuster off}. Reversible; see
# docs/preregistration/2026-06-19-operator-override-disable-voladjuster-tp12.md.
V2_COMPONENTS: tuple[ComponentSpec, ...] = (
    ComponentSpec("turn3p3", "p3", "turn3_pop3", 240, 0.12, FROZEN_FORWARD_CONFIG["weights"]["turn3p3"]),
    ComponentSpec("turn4p3", "p4p3", "turn4_pop3", 240, 0.12, FROZEN_FORWARD_CONFIG["weights"]["turn4p3"]),
    ComponentSpec("turn4p5", "p4p5", "turn4_pop5", 240, 0.12, FROZEN_FORWARD_CONFIG["weights"]["turn4p5"]),
)


@dataclass(frozen=True)
class ArmDefinition:
    arm_id: str
    title: str
    problem_book: str
    implemented: bool
    mechanism: str
    falsifier: str
    blocked_reason: str = ""
    claimed_venue_scope: str = CLAIMED_SCOPE_TWO_VENUE
    venues_allowed: tuple[str, ...] = ("bybit", "binance")
    screen_only: bool = False


ARM_DEFINITIONS: dict[str, ArmDefinition] = {
    CONTROL_ARM: ArmDefinition(
        CONTROL_ARM,
        "Frozen Continuous V2 control",
        "Phase 0",
        True,
        "Exact v2 component book plus frozen max4 rebalance, BTC+ETH hedge, and BTC-vol overlay.",
        "Control artifacts incomplete, PIT/truncation failure, or unexplained lifecycle mismatch.",
    ),
    "V2_CONTROL_DELAYED_FEATURES": ArmDefinition(
        "V2_CONTROL_DELAYED_FEATURES",
        "Control latency sanity path",
        "Phase 0",
        True,
        "Same runnable control; current v2 uses no new uncertain external feature path.",
        "Differs from V2_CONTROL without a declared delayed feature dependency.",
    ),
    ANCHOR_ARM: ArmDefinition(
        ANCHOR_ARM,
        "Frozen Continuous V2 evidence anchor (pre-override object)",
        "Phase 0",
        True,
        "Same frozen v2 component book as V2_CONTROL but the PRE-override object: "
        "component TP 0.10 and the daily vol-target adjuster ON (prior max4 wiring, "
        "enabled=True). Judged-against baseline #2 for every future A/B arm.",
        "Differs from the registered pre-override forward baseline beyond the declared "
        "{TP10, vol-adjuster max4} object, or PIT/lifecycle mismatch.",
    ),
    "A4_REGIME_HEDGE_INTENSITY": ArmDefinition(
        "A4_REGIME_HEDGE_INTENSITY",
        "Regime hedge-intensity overlay",
        "A - Regime Scoring",
        False,
        "Keep entries unchanged; map a causal multifactor regime score into hedge intensity only.",
        "Matched by hash control, carried by one venue/month, or improves return while lowering MAR.",
        "Requires feature almanac admissibility for multifactor regime inputs before a serious A/B run.",
    ),
    "C2_MARKET_FLOW_HEDGE_INTENSITY": ArmDefinition(
        "C2_MARKET_FLOW_HEDGE_INTENSITY",
        "Market-flow hedge-intensity overlay",
        "C - Order Flow And Microstructure",
        False,
        "Keep entries unchanged; scale hedge intensity from residualized market-wide flow squeeze risk.",
        "Raw flow is not incremental after lagged returns/composite or data coverage is one-venue only.",
        "Requires residualized flow coverage and null-control proof from the feature almanac.",
    ),
    "C3_FLOW_SQUEEZE_HEDGE_INTENSITY": ArmDefinition(
        "C3_FLOW_SQUEEZE_HEDGE_INTENSITY",
        "Active-book flow squeeze hedge-intensity overlay",
        "C - Order Flow And Microstructure",
        False,
        "Keep entries unchanged; hedge more when active-book OI/funding/taker-flow squeeze risk is high.",
        "The squeeze score worsens drawdown/MAR or cannot be rebuilt causally on both venues.",
        "Requires OI/funding/flow tape coverage and active-book score construction.",
    ),
    A4B_ARM: ArmDefinition(
        A4B_ARM,
        "Price/carry regime hedge-intensity overlay",
        "A - Regime Scoring",
        True,
        "Keep entries unchanged; multiply the existing BTC-vol hedge overlay by a mild causal price/carry regime score.",
        "Matched by the A4B hash control, carried by one venue/month, or improves return while lowering MAR.",
    ),
    A4B_HASH_ARM: ArmDefinition(
        A4B_HASH_ARM,
        "Price/carry regime hedge-intensity hash control",
        "A - Regime Scoring",
        True,
        "Same entries and same marginal hedge-intensity distribution as A4B, but calendar-hash permuted by day.",
        "Beats or matches the real score, closing A4B as a likely timing/noise artifact.",
    ),
    C0_FLOW_SCREEN_ARM: ArmDefinition(
        C0_FLOW_SCREEN_ARM,
        "Binance-only residualized order-flow discovery screen",
        "C - Order Flow And Microstructure",
        False,
        "Rank residualized Binance taker-flow features (flow_resid_return, idiosyncratic_flow, flow_squeeze) "
        "against control trade/daily returns and tails; nulls included.",
        "Residualized flow is not incremental after lagged returns/composite, or symbol/calendar/shuffle nulls match it.",
        blocked_reason=(
            "C0 is a discovery screen, not a lifecycle A/B. Run `--mode screen --venues binance` after the flow "
            "almanac refresh; it ranks mechanisms only and cannot accept an alpha."
        ),
        claimed_venue_scope=CLAIMED_SCOPE_BINANCE_FLOW,
        venues_allowed=("binance",),
        screen_only=True,
    ),
    C1_FLOW_SIZING_ARM: ArmDefinition(
        C1_FLOW_SIZING_ARM,
        "Binance-only idiosyncratic-flow de-risk entry sizing",
        "C - Order Flow And Microstructure",
        True,
        "Same entries as control; causal mean-1 per-trade size tilt from idiosyncratic_flow with NEGATIVE sign "
        "(size DOWN names with high idiosyncratic taker buying = continuation risk; size UP low-flow names). "
        "Construction amendment: docs/preregistration/2026-06-19-continuous-v2-c1-flow-sizing-construction.md.",
        "Matched by the C1H hash control, raises drawdown faster than return, or is carried by one month/liquidity bucket.",
        claimed_venue_scope=CLAIMED_SCOPE_BINANCE_FLOW,
        venues_allowed=("binance",),
    ),
    C1H_FLOW_SIZING_HASH_ARM: ArmDefinition(
        C1H_FLOW_SIZING_HASH_ARM,
        "Binance-only idiosyncratic-flow sizing hash control",
        "C - Order Flow And Microstructure",
        True,
        "Same entries and same per-trade multiplier distribution as C1, permuted by symbol/calendar hash.",
        "Beats or matches C1, closing idiosyncratic-flow de-risk sizing as a noise artifact.",
        claimed_venue_scope=CLAIMED_SCOPE_BINANCE_FLOW,
        venues_allowed=("binance",),
    ),
    C2_FLOW_ARM: ArmDefinition(
        C2_FLOW_ARM,
        "Binance-only market-flow hedge-intensity overlay",
        "C - Order Flow And Microstructure",
        True,
        "Keep entries unchanged; multiply the frozen BTC-vol hedge intensity by a causal mean-1 daily score "
        "from Binance market-wide taker flow (more hedge when market-wide taker buying signals squeeze risk).",
        "Matched by the C7 flow hash control, carried by one month/liquidity bucket, or improves return while lowering MAR.",
        claimed_venue_scope=CLAIMED_SCOPE_BINANCE_FLOW,
        venues_allowed=("binance",),
    ),
    C3_FLOW_ARM: ArmDefinition(
        C3_FLOW_ARM,
        "Binance-only active-book flow-squeeze hedge-intensity overlay",
        "C - Order Flow And Microstructure",
        True,
        "Keep entries unchanged; multiply the frozen BTC-vol hedge intensity by a causal mean-1 daily score "
        "from the entry-candidate flow_squeeze aggregate (OI build-up + positive funding + aggressive taker buy).",
        "Matched by a flow hash control, carried by one month/component, or improves return while lowering MAR.",
        claimed_venue_scope=CLAIMED_SCOPE_BINANCE_FLOW,
        venues_allowed=("binance",),
    ),
    C4_FLOW_ADMISSION_ARM: ArmDefinition(
        C4_FLOW_ADMISSION_ARM,
        "Binance-only flow-divergence admission",
        "C - Order Flow And Microstructure",
        False,
        "Admit/prioritize only candidates with a predeclared price-up/weak-taker divergence pattern.",
        "Admission concentrates the tail-correlated squeeze exposure and lowers MAR (the old crowding-admission failure).",
        blocked_reason="Lower-prior admission arm; run only after C0/C2/C3 and a dated construction amendment.",
        claimed_venue_scope=CLAIMED_SCOPE_BINANCE_FLOW,
        venues_allowed=("binance",),
    ),
    C5_FLOW_EXIT_ARM: ArmDefinition(
        C5_FLOW_EXIT_ARM,
        "Binance-only flow-unwind exit shadow",
        "C - Order Flow And Microstructure",
        False,
        "No-order shadow exit when OI/flow/funding show structural squeeze completion.",
        "Shadow exit cuts TP winners, relies on unavailable live state, or overfits one month.",
        blocked_reason="No-order shadow arm; requires a dated shadow protocol and forward evidence before any order-capable claim.",
        claimed_venue_scope=CLAIMED_SCOPE_BINANCE_FLOW,
        venues_allowed=("binance",),
    ),
    C6_FLOW_NONLINEAR_ARM: ArmDefinition(
        C6_FLOW_NONLINEAR_ARM,
        "Binance-only monotone/tree flow score",
        "C - Order Flow And Microstructure",
        False,
        "Monotone or tree-based score on residualized flow, OI, funding, premium features.",
        "Nonlinear score beats linear only by overfitting one period or venue.",
        blocked_reason=(
            "Counts as two arms in the flow budget. Allowed only after C0 shows a stable linear signal "
            "with a purged, time-split training protocol in a dated amendment."
        ),
        claimed_venue_scope=CLAIMED_SCOPE_BINANCE_FLOW,
        venues_allowed=("binance",),
    ),
    C7_FLOW_HASH_ARM: ArmDefinition(
        C7_FLOW_HASH_ARM,
        "Binance-only flow hedge-intensity hash control",
        "C - Order Flow And Microstructure",
        True,
        "Same entries and same marginal hedge-intensity distribution as C2, but calendar-hash permuted by day.",
        "Beats or matches the real flow score, closing the C2 flow overlay as a likely timing/noise artifact.",
        claimed_venue_scope=CLAIMED_SCOPE_BINANCE_FLOW,
        venues_allowed=("binance",),
    ),
    B1_SCORE_SIZING_ARM: ArmDefinition(
        B1_SCORE_SIZING_ARM,
        "Score-margin conviction sizing (both venues)",
        "B - Composite Scores And Score Confidence",
        True,
        "Entries unchanged; causal mean-1 per-trade size multiplier from an expanding-prior z of score_margin_d9_d8.",
        "Matched by the B6 hash control, carried by one venue/month, raises drawdown faster than return, or ties control (no conviction signal).",
    ),
    B1P_PATH_SIZING_ARM: ArmDefinition(
        B1P_PATH_SIZING_ARM,
        "Path-shape conviction sizing (both venues)",
        "B - Composite Scores And Score Confidence",
        True,
        "Entries unchanged; causal mean-1 per-trade size multiplier from an expanding-prior z of path_ret_6h_max "
        "(the strongest within-symbol screen IC).",
        "Matched by the B6P hash control, carried by one venue/month, or repeats the W5 path-shape sizing non-harvest "
        "(strong IC but no MAR gain).",
    ),
    B6_SCORE_HASH_ARM: ArmDefinition(
        B6_SCORE_HASH_ARM,
        "Score-margin sizing hash control",
        "B - Composite Scores And Score Confidence",
        True,
        "Same entries and same per-trade multiplier distribution as B1, permuted by symbol/calendar hash.",
        "Beats or matches B1, closing score-margin sizing as a noise artifact.",
    ),
    B6P_PATH_HASH_ARM: ArmDefinition(
        B6P_PATH_HASH_ARM,
        "Path-shape sizing hash control",
        "B - Composite Scores And Score Confidence",
        True,
        "Same entries and same per-trade multiplier distribution as B1P, permuted by symbol/calendar hash.",
        "Beats or matches B1P, closing path-shape sizing as a noise artifact.",
    ),
    EXIT_TP12_ARM: ArmDefinition(
        EXIT_TP12_ARM,
        "Exit-alpha: raise component take-profit to 12% (both venues)",
        "F - Exit Timing And Risk Controls",
        True,
        "Same entries; re-run components with take_profit_pct=0.12 (vs 0.10) and the frozen "
        "rebalance/hedge ledger. Profit-conditional: lets fade winners run further, never cuts/caps early.",
        "Improves one venue but not both, worsens MAR after the vol-target rebalance, or is one-month-driven.",
    ),
    EXIT_TP15_ARM: ArmDefinition(
        EXIT_TP15_ARM,
        "Exit-alpha: raise component take-profit to 15% (both venues)",
        "F - Exit Timing And Risk Controls",
        True,
        "Same entries; re-run components with take_profit_pct=0.15 and the frozen rebalance/hedge ledger.",
        "Improves one venue but not both, worsens MAR after the rebalance, or is one-month-driven "
        "(the per-trade sweep already showed tp15 Binance gains were not majority-of-months).",
    ),
}

OVERLAY_CLIP = (0.70, 1.30)
OVERLAY_K = 0.15

# Hedge-intensity overlay arms reuse the same-run control component ledgers and only
# multiply the frozen BTC-vol hedge intensity by a causal mean-1 daily score built
# from the feature almanac tape. A4B keeps its original construction (hash_seed
# "A4B_HASH") so its committed numbers stay numerically equivalent.
HEDGE_OVERLAY_SPECS: dict[str, dict[str, Any]] = {
    A4B_ARM: {
        "features": A4B_FEATURES,
        "neg_features": ("btc_drawdown_30d",),
        "hash_control": False,
        "hash_seed": "A4B_HASH",
        "amendment": AMENDMENT_A4B,
        "claimed_venue_scope": CLAIMED_SCOPE_TWO_VENUE,
        "label": "price/carry expanding-z daily score, one-day lag, clip [0.70, 1.30], mean-1",
    },
    A4B_HASH_ARM: {
        "features": A4B_FEATURES,
        "neg_features": ("btc_drawdown_30d",),
        "hash_control": True,
        "hash_seed": "A4B_HASH",
        "amendment": AMENDMENT_A4B,
        "claimed_venue_scope": CLAIMED_SCOPE_TWO_VENUE,
        "label": "A4B price/carry score, calendar-hash permuted by day",
    },
    C2_FLOW_ARM: {
        "features": ("market_flow",),
        "neg_features": (),
        "hash_control": False,
        "hash_seed": "C2_FLOW_HASH",
        "amendment": AMENDMENT_C_FLOW_OVERLAY,
        "claimed_venue_scope": CLAIMED_SCOPE_BINANCE_FLOW,
        "label": "Binance market-wide taker-flow expanding-z daily score, one-day lag, clip [0.70, 1.30], mean-1",
    },
    C3_FLOW_ARM: {
        "features": ("flow_squeeze",),
        "neg_features": (),
        "hash_control": False,
        "hash_seed": "C3_FLOW_HASH",
        "amendment": AMENDMENT_C_FLOW_OVERLAY,
        "claimed_venue_scope": CLAIMED_SCOPE_BINANCE_FLOW,
        "label": "Binance entry-candidate flow_squeeze expanding-z daily score, one-day lag, clip [0.70, 1.30], mean-1",
    },
    C7_FLOW_HASH_ARM: {
        "features": ("market_flow",),
        "neg_features": (),
        "hash_control": True,
        "hash_seed": "C7_FLOW_HASH",
        "amendment": AMENDMENT_C_FLOW_OVERLAY,
        "claimed_venue_scope": CLAIMED_SCOPE_BINANCE_FLOW,
        "label": "C2 market-flow score, calendar-hash permuted by day",
    },
}
OVERLAY_ARMS = set(HEDGE_OVERLAY_SPECS)

SIZING_CLIP = (0.5, 2.0)
SIZING_K = 0.25

# Problem Book B sizing arms: a causal mean-1 per-trade size multiplier from a single
# conviction feature, passed to the engine as size_mult_lookup. These arms RE-RUN the
# component ledgers (they do not reuse control components) because they change sizing.
SIZING_ARM_SPECS: dict[str, dict[str, Any]] = {
    B1_SCORE_SIZING_ARM: {"feature": "score_margin_d9_d8", "sign": 1.0, "hash_control": False, "hash_seed": "B1_SIZE_HASH"},
    B1P_PATH_SIZING_ARM: {"feature": "path_ret_6h_max", "sign": 1.0, "hash_control": False, "hash_seed": "B1P_SIZE_HASH"},
    B6_SCORE_HASH_ARM: {"feature": "score_margin_d9_d8", "sign": 1.0, "hash_control": True, "hash_seed": "B6_SIZE_HASH"},
    B6P_PATH_HASH_ARM: {"feature": "path_ret_6h_max", "sign": 1.0, "hash_control": True, "hash_seed": "B6P_SIZE_HASH"},
    # Binance-only exploratory flow sizing: NEGATIVE sign (idiosyncratic_flow has a
    # negative within-symbol IC vs the short net_return -> size DOWN high-buy-flow names).
    C1_FLOW_SIZING_ARM: {"feature": "idiosyncratic_flow", "sign": -1.0, "hash_control": False, "hash_seed": "C1_FLOW_SIZE_HASH"},
    C1H_FLOW_SIZING_HASH_ARM: {"feature": "idiosyncratic_flow", "sign": -1.0, "hash_control": True, "hash_seed": "C1H_FLOW_SIZE_HASH"},
}
SIZING_ARMS = set(SIZING_ARM_SPECS)


def claimed_scope_for(arm_id: str) -> str:
    if arm_id in BINANCE_ONLY_FLOW_ARMS:
        return CLAIMED_SCOPE_BINANCE_FLOW
    if arm_id in ARM_DEFINITIONS:
        return ARM_DEFINITIONS[arm_id].claimed_venue_scope
    return CLAIMED_SCOPE_TWO_VENUE


def amendment_for(arm_id: str) -> str | None:
    if arm_id == ANCHOR_ARM:
        return AMENDMENT_NEXT_LEVEL
    if arm_id in HEDGE_OVERLAY_SPECS:
        return str(HEDGE_OVERLAY_SPECS[arm_id]["amendment"])
    if arm_id in {C1_FLOW_SIZING_ARM, C1H_FLOW_SIZING_HASH_ARM}:
        return AMENDMENT_C1_FLOW_SIZING
    if arm_id in SIZING_ARM_SPECS:
        return AMENDMENT_B_SIZING
    if arm_id in TP_VARIANT_ARMS:
        return AMENDMENT_F2_EXIT_ALPHA
    if arm_id in BINANCE_ONLY_FLOW_ARMS:
        return AMENDMENT_BINANCE_FLOW
    return None


ALMANAC_FEATURES: tuple[dict[str, str], ...] = (
    {"feature": "current_composite", "source_table": "klines_1h + residual_momentum", "family": "current"},
    {"feature": "score_margin_d9_d8", "source_table": "continuous panel", "family": "score"},
    {"feature": "score_margin_d9_median", "source_table": "continuous panel", "family": "score"},
    {"feature": "rank_distance", "source_table": "continuous panel", "family": "score"},
    {"feature": "feature_agreement", "source_table": "continuous panel", "family": "score"},
    {"feature": "btc_ret_30d", "source_table": "klines_1h", "family": "regime"},
    {"feature": "btc_rv_30d", "source_table": "klines_1h", "family": "regime"},
    {"feature": "btc_vol_percentile_250d", "source_table": "klines_1h", "family": "regime"},
    {"feature": "btc_drawdown_30d", "source_table": "klines_1h", "family": "regime"},
    {"feature": "btc_trend_flip_age_days", "source_table": "klines_1h", "family": "regime"},
    {"feature": "market_breadth_1d", "source_table": "klines_1h", "family": "regime"},
    {"feature": "market_dispersion_1d", "source_table": "klines_1h", "family": "regime"},
    {"feature": "alt_minus_btc_1d", "source_table": "klines_1h", "family": "regime"},
    {"feature": "funding_level", "source_table": "funding", "family": "funding"},
    {"feature": "funding_change", "source_table": "funding", "family": "funding"},
    {"feature": "premium_level", "source_table": "premium_index", "family": "premium"},
    {"feature": "premium_change", "source_table": "premium_index", "family": "premium"},
    {"feature": "oi_level", "source_table": "open_interest", "family": "oi"},
    {"feature": "oi_change_24h", "source_table": "open_interest", "family": "oi"},
    {"feature": "oi_acceleration", "source_table": "open_interest", "family": "oi"},
    {"feature": "taker_imbalance_1h", "source_table": "taker_flow", "family": "flow"},
    {"feature": "taker_imbalance_6h", "source_table": "taker_flow", "family": "flow"},
    {"feature": "taker_imbalance_24h", "source_table": "taker_flow", "family": "flow"},
    {"feature": "market_flow", "source_table": "taker_flow", "family": "flow"},
    {"feature": "idiosyncratic_flow", "source_table": "taker_flow", "family": "flow"},
    {"feature": "flow_resid_return", "source_table": "taker_flow + returns", "family": "flow"},
    {"feature": "flow_squeeze", "source_table": "taker_flow + oi + funding", "family": "flow"},
    {"feature": "long_short_ratio", "source_table": "long_short_ratio", "family": "positioning"},
    {"feature": "liquidation_cluster", "source_table": "liquidations", "family": "liquidations"},
    {"feature": "book_thinning", "source_table": "depth", "family": "depth"},
    {"feature": "spread_depth_proxy", "source_table": "depth", "family": "depth"},
    {"feature": "liquidity_turnover", "source_table": "klines_1h", "family": "liquidity"},
    {"feature": "realized_slippage_proxy", "source_table": "klines_1h/depth", "family": "execution"},
    {"feature": "path_ret_1h", "source_table": "klines_1h", "family": "path"},
    {"feature": "path_ret_6h_max", "source_table": "klines_1h", "family": "path"},
    {"feature": "path_rv_168h", "source_table": "klines_1h", "family": "path"},
    {"feature": "path_max_ret168", "source_table": "klines_1h", "family": "path"},
    {"feature": "path_giveback_from_prior6_high", "source_table": "klines_1h", "family": "path"},
)

VALUE_BUILT_FEATURES = {
    "current_composite",
    "score_margin_d9_d8",
    "score_margin_d9_median",
    "rank_distance",
    "feature_agreement",
    "btc_ret_30d",
    "btc_rv_30d",
    "btc_vol_percentile_250d",
    "btc_drawdown_30d",
    "btc_trend_flip_age_days",
    "market_breadth_1d",
    "market_dispersion_1d",
    "alt_minus_btc_1d",
    "liquidity_turnover",
    "realized_slippage_proxy",
    "path_ret_1h",
    "path_ret_6h_max",
    "path_rv_168h",
    "path_max_ret168",
    "path_giveback_from_prior6_high",
    "funding_level",
    "funding_change",
    "premium_level",
    "premium_change",
    "oi_level",
    "oi_change_24h",
    "oi_acceleration",
    "taker_imbalance_1h",
    "taker_imbalance_6h",
    "taker_imbalance_24h",
    "market_flow",
    "idiosyncratic_flow",
    "flow_resid_return",
    "flow_squeeze",
}

FEATURE_ADMISSIBILITY_BLOCKERS = {
    "market_flow": "candidate-symbol aggregate only; full-market flow proof still required before C2",
    "idiosyncratic_flow": "candidate-symbol market-flow residual only; return-residual proof still required before C2",
}

SCREEN_GROUPS: dict[str, tuple[str, ...]] = {
    "regime": (
        "btc_ret_30d",
        "btc_rv_30d",
        "btc_vol_percentile_250d",
        "btc_drawdown_30d",
        "btc_trend_flip_age_days",
        "market_breadth_1d",
        "market_dispersion_1d",
        "alt_minus_btc_1d",
        "funding_level",
        "funding_change",
        "premium_level",
        "premium_change",
    ),
    "composite": (
        "current_composite",
        "score_margin_d9_d8",
        "score_margin_d9_median",
        "rank_distance",
        "liquidity_turnover",
        "path_ret_1h",
        "path_ret_6h_max",
        "path_rv_168h",
        "path_max_ret168",
        "path_giveback_from_prior6_high",
    ),
    "orderflow": (
        "taker_imbalance_1h",
        "taker_imbalance_6h",
        "taker_imbalance_24h",
        "market_flow",
        "idiosyncratic_flow",
        "flow_resid_return",
    ),
    "squeeze": (
        "funding_level",
        "funding_change",
        "premium_level",
        "premium_change",
        "oi_level",
        "oi_change_24h",
        "oi_acceleration",
        "taker_imbalance_24h",
        "flow_squeeze",
    ),
}
SCREEN_NULL_COLUMNS = ("symbol_hash", "calendar_hash", "shuffled_within_symbol", "shuffled_within_day")


def _today_tag() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def _parse_csv(value: str | Iterable[str]) -> list[str]:
    if isinstance(value, str):
        raw = value.replace(" ", ",").split(",")
    else:
        raw = []
        for item in value:
            raw.extend(str(item).replace(" ", ",").split(","))
    return [item.strip() for item in raw if item.strip()]


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n", encoding="utf-8")


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:  # noqa: BLE001 - git metadata is an audit field, not run-critical
        return "unknown"


def _stable_hash(payload: Any, length: int = 12) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _hash_int(*parts: Any) -> int:
    raw = "|".join(str(p) for p in parts)
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12], 16)


def _date_ms_to_iso(ts_ms: int | None) -> str | None:
    if ts_ms is None:
        return None
    return dt.datetime.fromtimestamp(int(ts_ms) / 1000, tz=dt.timezone.utc).date().isoformat()


def resolve_run_root(out_root: str | None, *, kind: str, date_tag: str) -> Path:
    if out_root:
        root = Path(out_root).expanduser()
    else:
        if kind == "almanac":
            suffix = "feature_almanac"
        elif kind == "screen":
            suffix = "feature_screens"
        else:
            suffix = "ab"
        name = f"continuous_v2_{suffix}_{date_tag}"
        root = REPO / "backtest-runs" / name
    if not root.is_absolute():
        root = REPO / root
    return root


def v2_component_config(
    spec: ComponentSpec, *, start_date: str, end_date: str, take_profit_pct: float | None = None
) -> ContinuousEventConfig:
    """Repo-native component config for the registered V2 control.

    This is deliberately explicit. The older component-rebuild helper is useful
    history but does not encode every current v2 lifecycle knob. ``take_profit_pct``
    overrides the component TP for exit-alpha variant arms (Problem Book F phase 2);
    None keeps the frozen control value.
    """
    tp = spec.take_profit_pct if take_profit_pct is None else float(take_profit_pct)
    return ContinuousEventConfig(
        start_date=start_date,
        end_date=end_date,
        side="short",
        decile=9,
        rmom_quantile=0.25,
        feature_set=("max_ret168",),
        liq_turnover_min=500_000.0,
        entry_delay_hours=1,
        exit_mode="fixed",
        hold_hours=24,
        max_hold_hours=48,
        take_profit_pct=tp,
        stop_loss_pct=0.0,
        stop_approach_frac=0.0,
        failed_fade_hours=0,
        failed_fade_loss_pct=0.0,
        failed_fade_min_mfe_pct=0.0,
        breakeven_arm_pct=0.0,
        btc_trend_gate="uptrend",
        sizing_mode="inverse_vol",
        target_vol_per_name=0.01,
        vol_weight_clamp=2.0,
        age_days_min=spec.age_days_min,
        entry_event_trigger=spec.entry_event_trigger,
        entry_pause_after_adverse_exits=8,
        entry_pause_window_hours=24,
        entry_crowding_max_fresh=2,
        use_funding=True,
    )


def arm_config_payload(
    arm_id: str,
    *,
    start_date: str,
    end_date: str,
    almanac_root: Path | None = None,
) -> dict[str, Any]:
    definition = ARM_DEFINITIONS[arm_id]
    tp_override = tp_override_for(arm_id)
    components = {
        spec.key: {
            "live_tag": spec.live_tag,
            "weight": spec.weight,
            "config": asdict(
                v2_component_config(spec, start_date=start_date, end_date=end_date, take_profit_pct=tp_override)
            ),
        }
        for spec in V2_COMPONENTS
    }
    payload = {
        "arm_id": arm_id,
        "arm": asdict(definition),
        "run_label": RUN_LABEL,
        "preregistration": PREREGISTRATION,
        "start_date": start_date,
        "end_date_exclusive": end_date,
        "continuous_profile_hash": frozen_config_hash(),
        "frozen_forward_config": FROZEN_FORWARD_CONFIG,
        "rebalance_rule": asdict(_arm_rebalance_rule(arm_id)),
        "hedge_rule": asdict(frozen_hedge_rule()),
        "components": components,
        "methodology_timestamps": methodology_timestamps(),
        "claimed_venue_scope": claimed_scope_for(arm_id),
        "known_boundaries": [
            "Component-ledger reconstruction path; sniper add-on is excluded unless separately registered.",
            "No daemon/server stop stack is present.",
            "Forward demo/paper remains the arbiter; this is not real-money evidence.",
        ],
    }
    if arm_id in BINANCE_ONLY_FLOW_ARMS:
        payload["amendment"] = AMENDMENT_BINANCE_FLOW
        payload["single_venue_exploratory"] = True
        payload["no_tier2_candidate_pass"] = True
    spec = HEDGE_OVERLAY_SPECS.get(arm_id)
    if spec is not None:
        payload["amendment"] = spec["amendment"]
        payload["component_source"] = f"same-run {CONTROL_ARM} component artifacts"
        payload["hedge_intensity_overlay"] = {
            "base": "existing frozen BTC-vol hedge intensity",
            "extra": spec["label"],
            "hash_control": bool(spec["hash_control"]),
            "clip": list(OVERLAY_CLIP),
            "k": OVERLAY_K,
            "almanac_root": str(almanac_root) if almanac_root is not None else None,
            "features": list(spec["features"]),
            "neg_features": list(spec["neg_features"]),
        }
    sizing = SIZING_ARM_SPECS.get(arm_id)
    if sizing is not None:
        payload["amendment"] = amendment_for(arm_id)
        payload["component_source"] = "re-run components with size_mult_lookup (not control reuse)"
        payload["sizing_intervention"] = {
            "feature": sizing["feature"],
            "sign": float(sizing.get("sign", 1.0)),
            "transform": "per-symbol expanding-prior z -> clip(1 + sign*k*z, *clip); strictly causal, no rescale",
            "k": SIZING_K,
            "clip": list(SIZING_CLIP),
            "hash_control": bool(sizing["hash_control"]),
            "almanac_root": str(almanac_root) if almanac_root is not None else None,
        }
    return payload


def methodology_timestamps() -> dict[str, str]:
    return {
        "decision_ts": "component signal bar close after the trailing input window is closed",
        "data_available_ts": "closed-bar features at decision_ts; residual momentum is day-lagged and causal",
        "order_submit_ts": "entry bar close after the configured +1h confirmation delay",
        "fill_window": "historical hourly bar model with explicit taker/spread/impact and funding where available",
        "exit_activation_ts": "venue take-profit at entry and 24h max-hold timer; no daemon/server stop stack",
        "state_initialization_ts": "run start plus warmup for listing age, rmom, BTC trend, vol, rebalance, and hedge",
    }


def validate_arms(arms: list[str]) -> None:
    unknown = sorted(set(arms) - set(ARM_DEFINITIONS))
    if unknown:
        raise ValueError(f"unknown arm(s): {', '.join(unknown)}")


def control_completed(out_root: Path, venues: Iterable[str]) -> bool:
    for venue in venues:
        if not (out_root / CONTROL_ARM / venue / "summary.json").exists():
            return False
    return True


def enforce_control_guard(arms: list[str], venues: list[str], out_root: Path) -> None:
    experimental = [arm for arm in arms if arm not in PHASE0_ARMS]
    if experimental and CONTROL_ARM not in arms and not control_completed(out_root, venues):
        raise RuntimeError(
            "refusing to run experimental arm(s) without V2_CONTROL in the same run directory: "
            f"{', '.join(experimental)}. Add --arms V2_CONTROL,{','.join(experimental)} "
            "or resume from a run root that already contains V2_CONTROL for the selected venues."
        )


def _assert_implemented(arm_id: str) -> None:
    definition = ARM_DEFINITIONS[arm_id]
    if not definition.implemented:
        raise RuntimeError(f"{arm_id} is registered but not runnable yet: {definition.blocked_reason}")


def _component_dir(arm_dir: Path, component: ComponentSpec) -> Path:
    return arm_dir / "components" / component.key


def _component_current(report_path: Path, cfg: ContinuousEventConfig, cache_key_extra: str = "") -> bool:
    if not report_path.exists():
        return False
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if payload.get("config_hash") != cfg.config_hash():
        return False
    sig_path = report_path.parent / "size_mult_signature.txt"
    stored_sig = sig_path.read_text(encoding="utf-8").strip() if sig_path.exists() else ""
    return stored_sig == cache_key_extra


def run_component(
    *,
    data_root: Path,
    out_dir: Path,
    cfg: ContinuousEventConfig,
    resume: bool,
    size_mult_lookup: dict[tuple[str, int], float] | None = None,
    cache_key_extra: str = "",
) -> dict[str, Any]:
    report_path = out_dir / "continuous_report.json"
    if resume and _component_current(report_path, cfg, cache_key_extra):
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        payload["resumed"] = True
        return payload
    payload = run_continuous_event_research(
        data_root, config=cfg, report_dir=out_dir, size_mult_lookup=size_mult_lookup
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "size_mult_signature.txt").write_text(cache_key_extra + "\n", encoding="utf-8")
    payload["resumed"] = False
    return payload


def _load_component_piece(component_dir: Path) -> tuple[Any, int, dict[str, Any]]:
    report_path = component_dir / "continuous_report.json"
    trades_path = component_dir / "continuous_trades.csv"
    mtm_path = component_dir / "continuous_mtm_equity.csv"
    if not report_path.exists() or not trades_path.exists() or not mtm_path.exists():
        raise FileNotFoundError(f"missing component artifacts under {component_dir}")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    trades = pl.read_csv(trades_path)
    mtm = pl.read_csv(mtm_path).select("ts_ms", "basket_return").sort("ts_ms")
    return decompose_continuous_components(trades, mtm, payload["config"]), int(trades.height), payload["config"]


def _component_source_dir(arm_dir: Path, out_root: Path, arm_id: str, venue: str, component: ComponentSpec) -> Path:
    if arm_id in OVERLAY_ARMS:
        control_component = out_root / CONTROL_ARM / venue / "components" / component.key
        if not control_component.exists():
            raise RuntimeError(
                f"{arm_id} requires same-run {CONTROL_ARM} component artifacts for {venue}: {control_component}"
            )
        return control_component
    return _component_dir(arm_dir, component)


def _component_trades(component_dir: Path, spec: ComponentSpec) -> pl.DataFrame:
    path = component_dir / "continuous_trades.csv"
    if not path.exists():
        return pl.DataFrame()
    trades = pl.read_csv(path)
    if trades.is_empty():
        return trades.with_columns(
            pl.lit(spec.key).alias("component"),
            pl.lit(spec.live_tag).alias("component_live_tag"),
            pl.lit(spec.weight).alias("component_weight"),
        )
    trades = trades.with_columns(
        pl.lit(spec.key).alias("component"),
        pl.lit(spec.live_tag).alias("component_live_tag"),
        pl.lit(spec.weight).alias("component_weight"),
    )
    if "notional_weight" in trades.columns:
        trades = trades.with_columns((pl.col("notional_weight") * spec.weight).alias("ensemble_notional_weight"))
    return trades


_INSTRUMENT_RETURNS_CACHE: dict[tuple[str, str, str], dict[int, float]] = {}


def _instrument_daily_returns(data_root: Path, venue: str, symbol: str) -> dict[int, float]:
    """Full-history daily close-to-close returns for a hedge instrument (cached).

    Independent of the requested ``days``, so it is memoized per (root, venue, symbol)
    for the process: the full klines_1h scan is the dominant cost of a multi-arm run,
    and every arm/venue needs the identical BTC/ETH daily-return series."""
    key = (str(data_root), venue, symbol)
    cached = _INSTRUMENT_RETURNS_CACHE.get(key)
    if cached is not None:
        return cached
    kdir = data_root / "klines_1h"
    closes = (
        pl.scan_parquet(str(kdir / "**" / "*.parquet"))
        .filter(pl.col("symbol") == symbol)
        .select("ts_ms", "close")
        .collect()
        .with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day"))
        .group_by("day")
        .agg(pl.col("close").last())
        .sort("day")
    )
    returns: dict[int, float] = {}
    prev_day: int | None = None
    prev_close: float | None = None
    for day, close in closes.iter_rows():
        day_i = int(day)
        close_f = float(close)
        if prev_day is not None and day_i - prev_day == MS_PER_DAY and prev_close and prev_close > 0.0:
            returns[day_i] = close_f / prev_close - 1.0
        prev_day, prev_close = day_i, close_f
    _INSTRUMENT_RETURNS_CACHE[key] = returns
    return returns


def instrument_inputs(
    data_root: Path,
    venue: str,
    days: list[int],
    symbol: str,
) -> tuple[dict[int, float], dict[int, float]]:
    """Daily hedge-instrument close returns and funding sums."""
    if not days:
        return {}, {}
    returns = _instrument_daily_returns(data_root, venue, symbol)
    funding: dict[int, float] = {}
    fdir = data_root / ("funding" if venue == "bybit" else "binance_usdm_funding")
    for day in days:
        date = _date_ms_to_iso(day)
        part = fdir / f"date={date}" / f"symbol={symbol}"
        if part.exists():
            try:
                funding[day] = float(pl.read_parquet(part, columns=["funding_rate"])["funding_rate"].sum())
            except Exception:  # noqa: BLE001 - missing one hedge funding partition is recorded as zero
                funding[day] = 0.0
    return returns, funding


def _expanding_prior_z(values: list[float | None], *, min_obs: int = 30) -> list[float]:
    out: list[float] = []
    hist: list[float] = []
    for value in values:
        if value is None or not math.isfinite(float(value)) or len(hist) < min_obs:
            out.append(0.0)
        else:
            arr = np.asarray(hist, dtype=float)
            std = float(arr.std(ddof=1)) if arr.size > 1 else 0.0
            out.append((float(value) - float(arr.mean())) / std if std > 1e-12 else 0.0)
        if value is not None and math.isfinite(float(value)):
            hist.append(float(value))
    return out


def _overlay_extra_intensity(
    *,
    arm_id: str,
    days: list[int],
    venue: str,
    almanac_root: Path,
) -> tuple[dict[int, float], pl.DataFrame]:
    """Causal mean-1 daily hedge-intensity multiplier from the almanac tape.

    Generalized from the A4B price/carry overlay. Each overlay arm declares a
    feature list (and optional sign-flipped features) in HEDGE_OVERLAY_SPECS. The
    daily score is the mean of expanding-prior z-scores of the day-aggregated
    features, lagged one day, mapped to ``1 + OVERLAY_K * score`` clipped to
    OVERLAY_CLIP and renormalized to mean-1. A4B keeps its original numbers
    (features/neg-set/clip/k/hash_seed unchanged).
    """
    spec = HEDGE_OVERLAY_SPECS[arm_id]
    features = tuple(spec["features"])
    neg_features = set(spec["neg_features"])
    hash_control = bool(spec["hash_control"])
    hash_seed = str(spec["hash_seed"])
    clip_lo, clip_hi = OVERLAY_CLIP
    tape_path = almanac_root / f"feature_tape_{venue}.parquet"
    if not tape_path.exists():
        raise FileNotFoundError(f"{arm_id} requires feature almanac tape: {tape_path}")
    tape = pl.read_parquet(tape_path)
    present = [feature for feature in features if feature in tape.columns]
    if not present:
        raise RuntimeError(f"{arm_id} feature tape has none of the required features {features}: {tape_path}")
    daily = (
        tape.group_by("day_ts")
        .agg([pl.col(feature).mean().alias(feature) for feature in present])
        .sort("day_ts")
        .with_columns((pl.col("day_ts") + MS_PER_DAY).alias("intensity_day_ts"))
        .select(["intensity_day_ts", *present])
    )
    day_df = pl.DataFrame({"day_ts": days}).sort("day_ts")
    ctx = day_df.join_asof(
        daily.sort("intensity_day_ts"),
        left_on="day_ts",
        right_on="intensity_day_ts",
        strategy="backward",
    )
    score_parts: list[list[float]] = []
    diagnostics: dict[str, list[float]] = {}
    for feature in present:
        values = [None if x is None else float(x) for x in ctx[feature].to_list()]
        if feature in neg_features:
            values = [None if x is None else -x for x in values]
        z = _expanding_prior_z(values)
        diagnostics[f"{feature}_z"] = z
        score_parts.append(z)
    scores: list[float] = []
    for idx in range(len(days)):
        vals = [part[idx] for part in score_parts if math.isfinite(part[idx])]
        scores.append(float(sum(vals) / len(vals)) if vals else 0.0)
    extra = [min(clip_hi, max(clip_lo, 1.0 + OVERLAY_K * score)) for score in scores]
    if hash_control and extra:
        sorted_extra = sorted(extra)
        hashed_days = sorted(range(len(days)), key=lambda i: _hash_int(hash_seed, venue, days[i]))
        permuted = [1.0] * len(days)
        for rank, idx in enumerate(hashed_days):
            permuted[idx] = sorted_extra[rank]
        extra = permuted
    mean_extra = float(np.mean(extra)) if extra else 1.0
    if mean_extra > 1e-12:
        extra = [x / mean_extra for x in extra]
    rows = {
        "day_ts": days,
        "overlay_arm_id": [arm_id] * len(days),
        "overlay_score": scores,
        "overlay_extra_intensity": extra,
        "overlay_hash_control": [hash_control] * len(days),
    }
    rows.update(diagnostics)
    return {int(day): float(val) for day, val in zip(days, extra)}, pl.DataFrame(rows)


def _arm_hedge_intensity(
    *,
    arm_id: str,
    venue: str,
    days: list[int],
    base: dict[int, float] | None,
    almanac_root: Path | None,
    out_path: Path,
) -> dict[int, float] | None:
    if arm_id not in OVERLAY_ARMS:
        return base
    if almanac_root is None:
        raise RuntimeError(f"{arm_id} requires --almanac-root or the default date-tag almanac root")
    extra, diag = _overlay_extra_intensity(
        arm_id=arm_id,
        days=days,
        venue=venue,
        almanac_root=almanac_root,
    )
    final = {day: float((base or {}).get(day, 1.0) * extra.get(day, 1.0)) for day in days}
    diag = diag.with_columns(
        pl.Series("base_hedge_intensity", [float((base or {}).get(day, 1.0)) for day in days]),
        pl.Series("final_hedge_intensity", [final[day] for day in days]),
    )
    diag.write_csv(out_path)
    return final


def _sizing_cache_key(arm_id: str, component_key: str) -> str:
    spec = SIZING_ARM_SPECS[arm_id]
    return _stable_hash(
        {
            "arm": arm_id,
            "component": component_key,
            "feature": spec["feature"],
            "sign": float(spec.get("sign", 1.0)),
            "hash_control": bool(spec["hash_control"]),
            "k": SIZING_K,
            "clip": list(SIZING_CLIP),
            "hash_seed": spec["hash_seed"],
        }
    )


def _sizing_mult_lookup(
    *,
    arm_id: str,
    venue: str,
    component_key: str,
    almanac_root: Path,
) -> tuple[dict[tuple[str, int], float], dict[str, Any]]:
    """Causal mean-1 per-trade size multiplier keyed by (symbol, signal_ts_ms).

    multiplier = clip(1 + SIZING_K * z, *SIZING_CLIP) where z is the per-symbol
    expanding-prior z-score (strictly prior rows, min 10 obs) of the arm's conviction
    feature. No full-sample rescaling is applied, so each multiplier is strictly causal;
    the ~mean-0 z keeps the tilt ~mean-1 and the daily vol-target rebalance enforces
    book gross. The hash control permutes the multiplier multiset across
    (symbol, signal_ts) by hash, preserving the distribution but destroying the
    feature->trade alignment.
    """
    spec = SIZING_ARM_SPECS[arm_id]
    feature = str(spec["feature"])
    sign = float(spec.get("sign", 1.0))
    hash_control = bool(spec["hash_control"])
    hash_seed = str(spec["hash_seed"])
    clip_lo, clip_hi = SIZING_CLIP
    tape_path = almanac_root / f"feature_tape_{venue}.parquet"
    if not tape_path.exists():
        raise FileNotFoundError(f"{arm_id} requires feature almanac tape: {tape_path}")
    tape = pl.read_parquet(tape_path).filter(pl.col("component") == component_key)
    empty_diag = {"arm_id": arm_id, "component": component_key, "venue": venue, "rows": 0, "feature": feature}
    if tape.is_empty() or feature not in tape.columns:
        return {}, empty_diag
    sub = tape.select("symbol", "signal_ts_ms", feature).sort(["symbol", "signal_ts_ms"])
    symbols = sub["symbol"].to_list()
    sigts = sub["signal_ts_ms"].to_list()
    vals = sub[feature].to_list()
    n = len(symbols)
    mult: list[float] = [1.0] * n
    start = 0
    while start < n:
        end = start
        while end < n and symbols[end] == symbols[start]:
            end += 1
        idx = list(range(start, end))
        z = _expanding_prior_z_series([vals[i] for i in idx], min_obs=10)
        for k, i in enumerate(idx):
            zi = z[k]
            if zi is not None and math.isfinite(zi):
                mult[i] = min(clip_hi, max(clip_lo, 1.0 + sign * SIZING_K * zi))
        start = end
    # No full-sample rescaling: each multiplier depends only on prior rows (strictly
    # causal). The per-symbol z is ~mean-0 so 1+k*z is ~mean-1 by construction, and the
    # daily vol-target rebalance enforces exact book gross downstream, so this stays a
    # relative within-book reweighting rather than a leverage change.
    if hash_control and mult:
        sorted_m = sorted(mult)
        order = sorted(range(n), key=lambda i: _hash_int(hash_seed, venue, component_key, symbols[i], sigts[i]))
        permuted = [1.0] * n
        for rank, i in enumerate(order):
            permuted[i] = sorted_m[rank]
        mult = permuted
    lookup = {(str(symbols[i]), int(sigts[i])): float(mult[i]) for i in range(n)}
    diag = {
        "arm_id": arm_id,
        "component": component_key,
        "venue": venue,
        "feature": feature,
        "rows": n,
        "mean_mult": float(np.mean(mult)) if mult else 1.0,
        "min_mult": float(np.min(mult)) if mult else 1.0,
        "max_mult": float(np.max(mult)) if mult else 1.0,
        "nontrivial": int(sum(1 for m in mult if abs(m - 1.0) > 1e-9)),
        "hash_control": hash_control,
    }
    return lookup, diag


def _metrics_from_equity(equity: pl.DataFrame) -> dict[str, Any]:
    if equity.is_empty():
        return {
            "total_return": 0.0,
            "annualized_return": 0.0,
            "max_drawdown": 0.0,
            "mar": None,
            "sharpe_like": 0.0,
            "worst_day_return": 0.0,
        }
    eq = equity.sort("ts_ms")
    returns = [float(x) for x in eq["basket_return"].fill_null(0.0).to_list()]
    total = float(eq["equity"][-1] - 1.0)
    max_dd = float(eq["drawdown"].min())
    first, last = int(eq["ts_ms"][0]), int(eq["ts_ms"][-1])
    years = max((last - first) / (365.25 * MS_PER_DAY), 1e-9)
    annualized = total / years
    return {
        "total_return": total,
        "annualized_return": annualized,
        "max_drawdown": max_dd,
        "mar": (annualized / abs(max_dd)) if abs(max_dd) > 1e-12 else None,
        "sharpe_like": annualized_sharpe(returns),
        "worst_day_return": float(min(returns)) if returns else 0.0,
    }


def _monthly_returns(equity: pl.DataFrame, trades: pl.DataFrame) -> pl.DataFrame:
    schema = {
        "month": pl.String,
        "strategy_return": pl.Float64,
        "trades": pl.Int64,
    }
    if equity.is_empty():
        return pl.DataFrame(schema=schema)
    eq = equity.with_columns(
        pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y-%m").alias("month")
    )
    monthly = (
        eq.group_by("month")
        .agg(((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"))
        .sort("month")
    )
    if trades.is_empty() or "entry_ts_ms" not in trades.columns:
        return monthly.with_columns(pl.lit(0, dtype=pl.Int64).alias("trades")).select(list(schema))
    counts = (
        trades.with_columns(pl.from_epoch("entry_ts_ms", time_unit="ms").dt.strftime("%Y-%m").alias("month"))
        .group_by("month")
        .agg(pl.len().alias("trades"))
    )
    return (
        monthly.join(counts, on="month", how="left")
        .with_columns(pl.col("trades").fill_null(0).cast(pl.Int64))
        .select(list(schema))
        .sort("month")
    )


def _split_metrics(equity: pl.DataFrame) -> dict[str, Any]:
    out = {"full": _metrics_from_equity(equity)}
    if equity.is_empty():
        return out
    split_ms = _date_str_to_ms(SPLIT_BOUNDARY)
    early = equity.filter(pl.col("ts_ms") < split_ms)
    recent = equity.filter(pl.col("ts_ms") >= split_ms)
    out["early"] = _metrics_from_equity(early)
    out["recent"] = _metrics_from_equity(recent)
    first, last = int(equity["ts_ms"].min()), int(equity["ts_ms"].max())
    span = max(last - first, 1)
    thirds = {}
    for idx in range(3):
        lo = first + idx * span // 3
        hi = first + (idx + 1) * span // 3 if idx < 2 else last + 1
        thirds[f"third_{idx + 1}"] = _metrics_from_equity(
            equity.filter((pl.col("ts_ms") >= lo) & (pl.col("ts_ms") < hi))
        )
    out["thirds"] = thirds
    return out


def _write_fill_model(path: Path, component_payloads: dict[str, dict[str, Any]]) -> None:
    rows: list[dict[str, Any]] = []
    for key, payload in component_payloads.items():
        cfg = payload["config"]
        rows.append(
            {
                "scope": f"component:{key}",
                "order_type": "market",
                "fill_window": "hourly historical bar model",
                "taker_fee_bps": cfg["taker_fee_bps"],
                "spread_bps": cfg["spread_bps"],
                "impact_coef_bps": cfg["impact_coef_bps"],
                "impact_exponent": cfg["impact_exponent"],
                "funding": "modeled from full-PIT funding dataset when available",
            }
        )
    rows.append(
        {
            "scope": "portfolio_rebalance",
            "order_type": "resize",
            "fill_window": "daily rebalance accounting",
            "taker_fee_bps": None,
            "spread_bps": None,
            "impact_coef_bps": None,
            "impact_exponent": None,
            "funding": f"resize_cost_bps={FROZEN_FORWARD_CONFIG['rebalance']['resize_cost_bps']}",
        }
    )
    rows.append(
        {
            "scope": "hedge",
            "order_type": "BTCUSDT+ETHUSDT long hedge",
            "fill_window": "daily hedge accounting",
            "taker_fee_bps": None,
            "spread_bps": None,
            "impact_coef_bps": None,
            "impact_exponent": None,
            "funding": f"hedge_cost_bps={FROZEN_FORWARD_CONFIG['hedge']['cost_bps']}",
        }
    )
    pl.DataFrame(rows).write_csv(path)


def _write_run_report(path: Path, summary: dict[str, Any]) -> None:
    metrics = summary["metrics"]
    splits = summary.get("split_metrics", {})
    scope = summary.get("claimed_venue_scope", CLAIMED_SCOPE_TWO_VENUE)
    scope_note = (
        "\n\n> Binance-only exploratory flow branch (2026-06-19 amendment): single-venue mechanism "
        "research only. It cannot clear the Tier-2 candidate bar or support Bybit demo/paper wiring."
        if summary.get("single_venue_exploratory")
        else ""
    )

    def fmt(value: Any, digits: int = 6) -> str:
        if value is None:
            return "n/a"
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            return str(value)

    split_rows = []
    for name in ("full", "early", "recent"):
        row = splits.get(name)
        if row:
            split_rows.append((name, row))
    third_rows = []
    for name, row in splits.get("thirds", {}).items() if isinstance(splits.get("thirds"), dict) else []:
        third_rows.append((name, row))

    text = f"""# {summary['arm_id']} - {summary['venue']}

Run label: `{summary.get('audit_run_label', AUDIT_RUN_LABEL)}`

Run context: `{summary['run_label']}`

Preregistration: `{summary['preregistration']}`

Window: `{summary['start_date']}` through `{summary['end_date_exclusive']}` exclusive.

Data root: `{summary['data_root']}`

Data root role: `{summary.get('data_root_role', 'full-PIT working dataset')}`

Claimed venue scope: `{scope}`{scope_note}

Config hash: `{summary['config_hash']}`

Git commit: `{summary['git_commit']}`

## Metrics

- Total return: {metrics['total_return']:.6f}
- Max drawdown: {metrics['max_drawdown']:.6f}
- MAR: {metrics['mar']}
- Sharpe-like: {metrics['sharpe_like']:.6f}
- Worst day return: {metrics['worst_day_return']:.6f}
- Trades: {summary['n_trades']}

## Split Metrics

Split boundary: `{summary.get('split_boundary', SPLIT_BOUNDARY)}`

| Split | Total return | Max drawdown | MAR | Sharpe-like | Worst day |
| --- | ---: | ---: | ---: | ---: | ---: |
"""
    for name, row in split_rows:
        text += (
            f"| {name} | {fmt(row.get('total_return'))} | {fmt(row.get('max_drawdown'))} | "
            f"{fmt(row.get('mar'))} | {fmt(row.get('sharpe_like'))} | {fmt(row.get('worst_day_return'))} |\n"
        )
    if third_rows:
        text += "\n### Thirds\n\n"
        text += "| Split | Total return | Max drawdown | MAR | Sharpe-like | Worst day |\n"
        text += "| --- | ---: | ---: | ---: | ---: | ---: |\n"
        for name, row in third_rows:
            text += (
                f"| {name} | {fmt(row.get('total_return'))} | {fmt(row.get('max_drawdown'))} | "
                f"{fmt(row.get('mar'))} | {fmt(row.get('sharpe_like'))} | {fmt(row.get('worst_day_return'))} |\n"
            )
    text += f"""
## OOS / Forward Status

- Internal OOS window: none claimed; the per-venue full-PIT roots are working datasets.
- Clean OOS arbiter: forward demo/paper only.
- Evidence status: `{summary.get('audit_run_label', AUDIT_RUN_LABEL)}` control/foundation artifact, not a promotion result.

## Methodology Timestamps

"""
    for key, value in summary["methodology_timestamps"].items():
        text += f"- `{key}`: {value}\n"
    text += """
## Boundaries

- Demo/paper research only. No real-money claim.
- Full-PIT roots are required; operational demo/paper roots are not used.
- Old W5/W6 artifacts are hypothesis material only and are not imported.
- Sniper and literal daemon state-machine replay are excluded from this component-ledger control path.
"""
    path.write_text(text, encoding="utf-8")


def _refresh_existing_arm_summary(arm_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    """Refresh report-facing metadata without recomputing component ledgers."""
    equity_path = arm_dir / "equity.csv"
    trades_path = arm_dir / "trades.csv"
    splits_path = arm_dir / "splits.json"
    if equity_path.exists():
        equity = pl.read_csv(equity_path)
        summary["metrics"] = _metrics_from_equity(equity)
        splits = _split_metrics(equity)
        _write_json(splits_path, splits)
    else:
        splits = json.loads(splits_path.read_text(encoding="utf-8")) if splits_path.exists() else {}
    if trades_path.exists():
        summary["n_trades"] = int(pl.read_csv(trades_path).height)
    summary["audit_run_label"] = AUDIT_RUN_LABEL
    summary["data_root_role"] = "full-PIT working dataset"
    summary["claimed_venue_scope"] = claimed_scope_for(str(summary.get("arm_id", "")))
    summary["single_venue_exploratory"] = str(summary.get("arm_id", "")) in BINANCE_ONLY_FLOW_ARMS
    summary["split_boundary"] = SPLIT_BOUNDARY
    summary["split_metrics"] = splits
    summary["artifact_writer_version"] = ARTIFACT_WRITER_VERSION
    summary["oos_status"] = {
        "internal_oos_claimed": False,
        "clean_oos_arbiter": "forward demo/paper only",
        "note": "No internal OOS result is claimed for this research-stage foundation/control artifact.",
    }
    summary.setdefault("paths", {})
    summary["paths"].update(
        {
            "trades": str(trades_path),
            "orders_or_fill_model": str(arm_dir / "orders_or_fill_model.csv"),
            "mtm": str(arm_dir / "mtm.csv"),
            "equity": str(equity_path),
            "monthly": str(arm_dir / "monthly.csv"),
            "splits": str(splits_path),
            "summary": str(arm_dir / "summary.json"),
            "run_report": str(arm_dir / "run_report.md"),
        }
    )
    _write_json(arm_dir / "summary.json", summary)
    _write_run_report(arm_dir / "run_report.md", summary)
    _write_json(
        arm_dir / "checkpoint.json",
        {
            "status": "complete",
            "arm_id": summary["arm_id"],
            "venue": summary["venue"],
            "summary": str(arm_dir / "summary.json"),
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "artifact_writer_version": ARTIFACT_WRITER_VERSION,
        },
    )
    return summary


def run_arm_venue(
    arm_id: str,
    venue: str,
    *,
    out_root: Path,
    start_date: str,
    end_date: str,
    resume: bool,
    almanac_root: Path | None = None,
) -> dict[str, Any]:
    _assert_implemented(arm_id)
    definition = ARM_DEFINITIONS[arm_id]
    if venue not in definition.venues_allowed:
        raise RuntimeError(
            f"{arm_id} is scoped to venues {definition.venues_allowed} "
            f"({definition.claimed_venue_scope}); refusing venue={venue}."
        )
    data_root = ROOTS[venue]
    if not data_root.is_dir():
        raise FileNotFoundError(f"full-PIT data root missing for {venue}: {data_root}")

    arm_dir = out_root / arm_id / venue
    summary_path = arm_dir / "summary.json"
    config_payload = arm_config_payload(
        arm_id,
        start_date=start_date,
        end_date=end_date,
        almanac_root=almanac_root,
    )
    config_hash = _stable_hash(config_payload)
    if resume and summary_path.exists():
        existing_summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if existing_summary.get("config_hash") == config_hash:
            return _refresh_existing_arm_summary(arm_dir, existing_summary)

    arm_dir.mkdir(parents=True, exist_ok=True)
    config_payload["config_hash"] = config_hash
    config_payload["git_commit"] = _git_commit()
    _write_json(arm_dir / "config.json", config_payload)
    (arm_dir / "config_hash.txt").write_text(config_hash + "\n", encoding="utf-8")

    component_payloads: dict[str, dict[str, Any]] = {}
    pieces: dict[str, Any] = {}
    trade_frames: list[pl.DataFrame] = []
    sizing_diags: dict[str, Any] = {}
    for spec in V2_COMPONENTS:
        cfg = v2_component_config(
            spec, start_date=start_date, end_date=end_date, take_profit_pct=tp_override_for(arm_id)
        )
        out_dir = _component_dir(arm_dir, spec)
        source_dir = _component_source_dir(arm_dir, out_root, arm_id, venue, spec)
        if source_dir == out_dir:
            size_mult_lookup: dict[tuple[str, int], float] | None = None
            cache_key_extra = ""
            if arm_id in SIZING_ARMS:
                if almanac_root is None:
                    raise RuntimeError(f"{arm_id} requires --almanac-root for the size_mult_lookup feature tape")
                size_mult_lookup, size_diag = _sizing_mult_lookup(
                    arm_id=arm_id, venue=venue, component_key=spec.key, almanac_root=almanac_root
                )
                cache_key_extra = _sizing_cache_key(arm_id, spec.key)
                sizing_diags[spec.key] = size_diag
            payload = run_component(
                data_root=data_root,
                out_dir=out_dir,
                cfg=cfg,
                resume=resume,
                size_mult_lookup=size_mult_lookup,
                cache_key_extra=cache_key_extra,
            )
        else:
            report_path = source_dir / "continuous_report.json"
            if not report_path.exists():
                raise FileNotFoundError(f"missing source component report: {report_path}")
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            payload["resumed"] = True
            payload["component_source"] = str(source_dir)
        component_payloads[spec.key] = payload
        piece, _n, _cfg = _load_component_piece(source_dir)
        pieces[spec.key] = piece
        trade_frames.append(_component_trades(source_dir, spec))

    combined_trades = pl.concat([df for df in trade_frames if not df.is_empty()], how="diagonal") if any(
        not df.is_empty() for df in trade_frames
    ) else pl.DataFrame()
    combined_trades.write_csv(arm_dir / "trades.csv")
    if sizing_diags:
        _write_json(
            arm_dir / "sizing_lookup.json",
            {"arm_id": arm_id, "venue": venue, "components": sizing_diags},
        )

    all_days = sorted({day for piece in pieces.values() for day in piece.days})
    btc_ret, btc_fund = instrument_inputs(data_root, venue, all_days, "BTCUSDT")
    eth_ret, eth_fund = instrument_inputs(data_root, venue, all_days, "ETHUSDT")
    regime = frozen_hedge_regime()
    base_hedge_intensity = (
        btcvol_intensity_series(all_days, btc_ret, regime["lam"], regime["vol_window"], regime["pct_window"])
        if regime
        else None
    )
    hedge_intensity = _arm_hedge_intensity(
        arm_id=arm_id,
        venue=venue,
        days=all_days,
        base=base_hedge_intensity,
        almanac_root=almanac_root,
        out_path=arm_dir / "hedge_intensity.csv",
    )
    ledger = build_full_ledger(
        pieces,
        btc_ret,
        btc_fund,
        eth_ret,
        eth_fund,
        hedge_intensity=hedge_intensity,
        rebalance_rule=_arm_rebalance_rule(arm_id),
    )
    ledger.write_csv(arm_dir / "mtm.csv")
    equity = ledger.select([c for c in ("ts_ms", "basket_return", "equity", "drawdown") if c in ledger.columns])
    equity.write_csv(arm_dir / "equity.csv")
    monthly = _monthly_returns(equity, combined_trades)
    monthly.write_csv(arm_dir / "monthly.csv")
    splits = _split_metrics(equity)
    _write_json(arm_dir / "splits.json", splits)
    _write_fill_model(arm_dir / "orders_or_fill_model.csv", component_payloads)

    metrics = _metrics_from_equity(equity)
    summary = {
        "arm_id": arm_id,
        "venue": venue,
        "run_label": RUN_LABEL,
        "audit_run_label": AUDIT_RUN_LABEL,
        "preregistration": PREREGISTRATION,
        "start_date": start_date,
        "end_date_exclusive": end_date,
        "data_root": str(data_root),
        "data_root_role": "full-PIT working dataset",
        "git_commit": config_payload["git_commit"],
        "config_hash": config_hash,
        "continuous_profile_hash": frozen_config_hash(),
        "amendment": amendment_for(arm_id),
        "claimed_venue_scope": claimed_scope_for(arm_id),
        "single_venue_exploratory": arm_id in BINANCE_ONLY_FLOW_ARMS,
        "almanac_root": str(almanac_root) if almanac_root is not None else None,
        "metrics": metrics,
        "n_trades": int(combined_trades.height),
        "split_boundary": SPLIT_BOUNDARY,
        "split_metrics": splits,
        "artifact_writer_version": ARTIFACT_WRITER_VERSION,
        "oos_status": {
            "internal_oos_claimed": False,
            "clean_oos_arbiter": "forward demo/paper only",
            "note": "No internal OOS result is claimed for this research-stage foundation/control artifact.",
        },
        "component_summaries": {
            key: {
                "config_hash": payload.get("config_hash"),
                "n_trades": payload.get("n_trades"),
                "funding_mode": payload.get("funding_mode"),
                "resumed": payload.get("resumed", False),
                "skips": payload.get("skips", {}),
            }
            for key, payload in component_payloads.items()
        },
        "methodology_timestamps": methodology_timestamps(),
        "paths": {
            "config": str(arm_dir / "config.json"),
            "trades": str(arm_dir / "trades.csv"),
            "orders_or_fill_model": str(arm_dir / "orders_or_fill_model.csv"),
            "mtm": str(arm_dir / "mtm.csv"),
            "equity": str(arm_dir / "equity.csv"),
            "monthly": str(arm_dir / "monthly.csv"),
            "splits": str(arm_dir / "splits.json"),
            "summary": str(arm_dir / "summary.json"),
            "run_report": str(arm_dir / "run_report.md"),
            "hedge_intensity": str(arm_dir / "hedge_intensity.csv") if (arm_dir / "hedge_intensity.csv").exists() else None,
        },
        "known_gaps": [
            "This reconstructs the frozen component-ledger portfolio object, not sniper fills.",
            "Experimental A/B arms remain blocked until the feature almanac admits their features.",
            "No real-money promotion or deployment claim is made.",
        ],
    }
    _write_json(summary_path, summary)
    _write_run_report(arm_dir / "run_report.md", summary)
    _write_json(
        arm_dir / "checkpoint.json",
        {
            "status": "complete",
            "arm_id": arm_id,
            "venue": venue,
            "summary": str(summary_path),
            "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
    )
    return summary


def write_pooled_tables(out_root: Path, rows: list[dict[str, Any]]) -> None:
    flat_rows = []
    for row in rows:
        metrics = row["metrics"]
        flat_rows.append(
            {
                "arm_id": row["arm_id"],
                "venue": row["venue"],
                "run_label": row["run_label"],
                "total_return": metrics["total_return"],
                "max_drawdown": metrics["max_drawdown"],
                "mar": metrics["mar"],
                "sharpe_like": metrics["sharpe_like"],
                "worst_day_return": metrics["worst_day_return"],
                "n_trades": row["n_trades"],
                "config_hash": row["config_hash"],
                "summary_path": row["paths"]["summary"],
                "monthly_path": row["paths"]["monthly"],
            }
        )
    table = pl.DataFrame(flat_rows)
    if table.is_empty():
        table.write_csv(out_root / "ab_table.csv")
        table.write_csv(out_root / "decision_rule_input.csv")
        return
    table.sort(["arm_id", "venue"]).write_csv(out_root / "ab_table.csv")
    pooled = (
        table.group_by("arm_id")
        .agg(
            pl.col("total_return").mean().alias("pooled_mean_return"),
            pl.col("total_return").min().alias("pooled_min_return"),
            pl.col("max_drawdown").mean().alias("pooled_mean_drawdown"),
            pl.col("n_trades").sum().alias("pooled_trades"),
        )
        .sort("arm_id")
    )
    pooled.write_csv(out_root / "pooled_ab_table.csv")
    table.rename({"arm_id": "cell_id"}).write_csv(out_root / "decision_rule_input.csv")


def _compound_returns(returns: list[float]) -> float:
    equity = 1.0
    for value in returns:
        equity *= 1.0 + float(value)
    return equity - 1.0


def _max_drawdown_from_returns(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= 1.0 + float(value)
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def _annualize_monthly(total_return: float, n_months: int) -> float:
    if n_months <= 0:
        return 0.0
    growth = 1.0 + total_return
    if growth <= 0.0:
        return -1.0
    return growth ** (12.0 / n_months) - 1.0


def _monthly_mar(returns: list[float]) -> float | None:
    dd = abs(_max_drawdown_from_returns(returns))
    if dd <= 1e-9:
        return None
    return _annualize_monthly(_compound_returns(returns), len(returns)) / dd


def _percentile(values: list[float], q: float) -> float | None:
    finite = sorted(v for v in values if math.isfinite(v))
    if not finite:
        return None
    pos = q * (len(finite) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(finite) - 1)
    return finite[lo] + (finite[hi] - finite[lo]) * (pos - lo)


def _load_monthly_map(path: Path) -> dict[str, float]:
    monthly: dict[str, float] = {}
    if not path.exists():
        raise FileNotFoundError(f"missing monthly ledger: {path}")
    for row in pl.read_csv(path).iter_rows(named=True):
        monthly[str(row["month"])] = float(row["strategy_return"])
    return dict(sorted(monthly.items()))


def _third_delta_rows(months: list[str], cell_r: list[float], base_r: list[float]) -> list[dict[str, Any]]:
    if len(months) < 3:
        return []
    k = len(months) // 3
    bounds = [(0, k), (k, 2 * k), (2 * k, len(months))]
    rows = []
    for idx, (lo, hi) in enumerate(bounds, start=1):
        base_ret = _compound_returns(base_r[lo:hi])
        cell_ret = _compound_returns(cell_r[lo:hi])
        rows.append(
            {
                "third": idx,
                "label": f"{months[lo]}..{months[hi - 1]}",
                "control_return": base_ret,
                "cell_return": cell_ret,
                "return_delta": cell_ret - base_ret,
            }
        )
    return rows


def _monthly_concentration(months: list[str], cell_r: list[float], base_r: list[float]) -> dict[str, Any]:
    deltas = sorted(((cell_r[i] - base_r[i], months[i]) for i in range(len(months))), reverse=True)
    pos_sum = sum(delta for delta, _month in deltas if delta > 0.0)
    top = deltas[:3]
    top_sum = sum(delta for delta, _month in top)
    return {
        "total_monthly_delta_sum": sum(delta for delta, _month in deltas),
        "positive_monthly_delta_sum": pos_sum,
        "top_3_share_of_positive": (top_sum / pos_sum) if pos_sum > 1e-12 else None,
        "top_3_months": [{"month": month, "delta": delta} for delta, month in top],
    }


def _leave_one_month_out(months: list[str], cell_r: list[float], base_r: list[float]) -> dict[str, Any]:
    full_delta = _compound_returns(cell_r) - _compound_returns(base_r)
    worst_label: str | None = None
    worst_delta: float | None = None
    for idx, month in enumerate(months):
        cell_without = cell_r[:idx] + cell_r[idx + 1 :]
        base_without = base_r[:idx] + base_r[idx + 1 :]
        delta = _compound_returns(cell_without) - _compound_returns(base_without)
        if worst_delta is None or delta < worst_delta:
            worst_delta = delta
            worst_label = month
    return {
        "full_return_delta": full_delta,
        "min_leave_one_month_out_delta": worst_delta,
        "most_loadbearing_month": worst_label,
        "loo_flips_sign": bool(full_delta > 0.0 and worst_delta is not None and worst_delta <= 0.0),
    }


def _resample_block_indices(n: int, block: int, rng: random.Random) -> list[int]:
    max_start = max(n - block + 1, 1)
    idx: list[int] = []
    while len(idx) < n:
        start = rng.randrange(0, max_start)
        idx.extend(min(start + offset, n - 1) for offset in range(block))
    return idx[:n]


def _bootstrap_monthly_delta(
    cell_r: list[float],
    base_r: list[float],
    *,
    n_boot: int,
    block: int,
    seed: int,
) -> dict[str, Any]:
    rng = random.Random(seed)
    ann_deltas: list[float] = []
    mar_deltas: list[float] = []
    for _idx in range(max(1, n_boot)):
        sample = _resample_block_indices(len(cell_r), max(1, block), rng)
        cell_sample = [cell_r[i] for i in sample]
        base_sample = [base_r[i] for i in sample]
        ann_deltas.append(
            _annualize_monthly(_compound_returns(cell_sample), len(cell_sample))
            - _annualize_monthly(_compound_returns(base_sample), len(base_sample))
        )
        cell_mar = _monthly_mar(cell_sample)
        base_mar = _monthly_mar(base_sample)
        if cell_mar is not None and base_mar is not None:
            mar_deltas.append(cell_mar - base_mar)

    def frac_gt0(values: list[float]) -> float | None:
        finite = [value for value in values if math.isfinite(value)]
        if not finite:
            return None
        return sum(1 for value in finite if value > 0.0) / len(finite)

    return {
        "ann_delta_p5": _percentile(ann_deltas, 0.05),
        "ann_delta_p50": _percentile(ann_deltas, 0.50),
        "ann_delta_p95": _percentile(ann_deltas, 0.95),
        "ann_delta_p_gt0": frac_gt0(ann_deltas),
        "monthly_mar_delta_p5": _percentile(mar_deltas, 0.05),
        "monthly_mar_delta_p50": _percentile(mar_deltas, 0.50),
        "monthly_mar_delta_p95": _percentile(mar_deltas, 0.95),
        "monthly_mar_delta_p_gt0": frac_gt0(mar_deltas),
    }


def _table_rows_by_arm_venue(table: pl.DataFrame) -> dict[tuple[str, str], dict[str, Any]]:
    return {(str(row["arm_id"]), str(row["venue"])): row for row in table.iter_rows(named=True)}


def _loose_backtest_verdict(rows: list[dict[str, Any]]) -> str:
    by_venue = {row["venue"]: row for row in rows}
    if not {"bybit", "binance"}.issubset(by_venue):
        return "descriptive (missing a venue)"
    bybit = by_venue["bybit"]
    binance = by_venue["binance"]
    pooled_mar_delta = (float(bybit["mar_delta"]) + float(binance["mar_delta"])) / 2.0
    if float(bybit["total_return"]) <= 0.0 or float(binance["total_return"]) <= 0.0:
        return "FALSIFY (return <=0 a venue)"
    if not math.isfinite(pooled_mar_delta) or pooled_mar_delta <= 0.0:
        return "FALSIFY (pooled MAR delta <=0)"
    if float(bybit["max_drawdown"]) < -0.70 or float(binance["max_drawdown"]) < -0.70:
        return "FALSIFY (DD >70% a venue)"
    if (
        pooled_mar_delta > 0.1
        and min(float(bybit["mar_delta"]), float(binance["mar_delta"])) >= -0.5
        and int(bybit["n_trades"]) >= 30
        and int(binance["n_trades"]) >= 20
    ):
        return "DEMO-ELIGIBLE by loose backtest rule; still exploratory until forward/demo evidence"
    return "descriptive"


def _write_robustness_markdown(path: Path, summary: dict[str, Any]) -> None:
    def fmt(value: Any) -> str:
        if value is None:
            return "nan"
        try:
            return f"{float(value):.6f}"
        except (TypeError, ValueError):
            return "nan"

    lines = [
        "# Continuous V2 A/B Robustness",
        "",
        "Computed from this runner's per-arm monthly ledgers. The legacy `scripts/r1_robustness.py` layout is volume-event-specific, so this is the continuous-runner equivalent diagnostic.",
        "",
        f"- Control: `{summary['control']}`",
        f"- Bootstrap samples: {summary['n_boot']}",
        f"- Block length: {summary['block']}",
        "",
        "## Cross-Venue Verdicts",
        "",
        "| Arm | Pooled MAR delta | Bybit MAR delta | Binance MAR delta | Verdict |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in summary["cross_venue"]:
        lines.append(
            f"| `{row['arm_id']}` | {fmt(row['pooled_mar_delta'])} | "
            f"{fmt(row.get('bybit_mar_delta'))} | "
            f"{fmt(row.get('binance_mar_delta'))} | {row['verdict']} |"
        )
    lines.extend(
        [
            "",
            "## Per-Venue Diagnostics",
            "",
            "| Arm | Venue | Return delta | MAR delta | Top-3 pos-month share | Min LOO return delta | Boot MAR delta p5 | Boot P(MAR delta>0) |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in summary["rows"]:
        top_share = row["top_3_share_of_positive"]
        min_loo = row["min_leave_one_month_out_delta"]
        boot_p5 = row["monthly_mar_delta_p5"]
        boot_prob = row["monthly_mar_delta_p_gt0"]
        lines.append(
            f"| `{row['arm_id']}` | {row['venue']} | {fmt(row['return_delta'])} | {fmt(row['mar_delta'])} | "
            f"{fmt(top_share)} | {fmt(min_loo)} | {fmt(boot_p5)} | {fmt(boot_prob)} |"
        )
    lines.extend(
        [
            "",
            "Run label remains `exploratory`; these diagnostics do not create real-money evidence.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_robustness_report(
    out_root: Path,
    *,
    control: str = CONTROL_ARM,
    n_boot: int = 5000,
    block: int = 3,
    seed: int = 0,
) -> dict[str, Any]:
    table_path = out_root / "ab_table.csv"
    if not table_path.exists():
        raise FileNotFoundError(f"missing A/B table: {table_path}")
    table = pl.read_csv(table_path)
    by_key = _table_rows_by_arm_venue(table)
    arms = sorted(str(arm) for arm in table["arm_id"].unique() if str(arm) != control)
    rows: list[dict[str, Any]] = []
    for arm in arms:
        for venue in sorted(str(v) for v in table.filter(pl.col("arm_id") == arm)["venue"].unique()):
            key = (arm, venue)
            base_key = (control, venue)
            if base_key not in by_key:
                continue
            cell = by_key[key]
            base = by_key[base_key]
            cell_monthly = _load_monthly_map(Path(str(cell["monthly_path"])))
            base_monthly = _load_monthly_map(Path(str(base["monthly_path"])))
            months = [month for month in base_monthly if month in cell_monthly]
            cell_r = [cell_monthly[month] for month in months]
            base_r = [base_monthly[month] for month in months]
            thirds = _third_delta_rows(months, cell_r, base_r)
            concentration = _monthly_concentration(months, cell_r, base_r)
            loo = _leave_one_month_out(months, cell_r, base_r)
            boot = _bootstrap_monthly_delta(
                cell_r,
                base_r,
                n_boot=n_boot,
                block=block,
                seed=seed + (_hash_int(arm, venue) % 1_000_000),
            )
            row = {
                "arm_id": arm,
                "venue": venue,
                "control": control,
                "months": len(months),
                "total_return": float(cell["total_return"]),
                "control_total_return": float(base["total_return"]),
                "return_delta": float(cell["total_return"]) - float(base["total_return"]),
                "max_drawdown": float(cell["max_drawdown"]),
                "control_max_drawdown": float(base["max_drawdown"]),
                "max_drawdown_delta": float(cell["max_drawdown"]) - float(base["max_drawdown"]),
                "mar": float(cell["mar"]),
                "control_mar": float(base["mar"]),
                "mar_delta": float(cell["mar"]) - float(base["mar"]),
                "sharpe_like": float(cell["sharpe_like"]),
                "control_sharpe_like": float(base["sharpe_like"]),
                "sharpe_like_delta": float(cell["sharpe_like"]) - float(base["sharpe_like"]),
                "n_trades": int(cell["n_trades"]),
                "control_n_trades": int(base["n_trades"]),
                "thirds": thirds,
                "all_cell_thirds_positive": bool(thirds and all(float(t["cell_return"]) > 0.0 for t in thirds)),
                "all_third_deltas_positive": bool(thirds and all(float(t["return_delta"]) > 0.0 for t in thirds)),
                **concentration,
                **loo,
                **boot,
            }
            rows.append(row)
    csv_rows = []
    for row in rows:
        flat = dict(row)
        flat["thirds"] = json.dumps(row["thirds"], default=_json_default)
        flat["top_3_months"] = json.dumps(row["top_3_months"], default=_json_default)
        csv_rows.append(flat)
    pl.DataFrame(csv_rows, infer_schema_length=None).write_csv(out_root / "robustness.csv")

    cross_venue = []
    for arm in arms:
        arm_rows = [row for row in rows if row["arm_id"] == arm]
        if not arm_rows:
            continue
        by_venue = {row["venue"]: row for row in arm_rows}
        pooled = sum(float(row["mar_delta"]) for row in arm_rows) / len(arm_rows)
        cross_venue.append(
            {
                "arm_id": arm,
                "pooled_mar_delta": pooled,
                "bybit_mar_delta": by_venue.get("bybit", {}).get("mar_delta"),
                "binance_mar_delta": by_venue.get("binance", {}).get("mar_delta"),
                "claimed_venue_scope": claimed_scope_for(arm),
                "verdict": (
                    "EXPLORATORY single-venue flow (no Tier-2 candidate pass)"
                    if arm in BINANCE_ONLY_FLOW_ARMS
                    else _loose_backtest_verdict(arm_rows)
                ),
            }
        )
    summary = {
        "run_label": "continuous_v2_ab_robustness",
        "control": control,
        "source_ab_table": str(table_path),
        "n_boot": n_boot,
        "block": block,
        "seed": seed,
        "rows": rows,
        "cross_venue": cross_venue,
        "outputs": {
            "csv": str(out_root / "robustness.csv"),
            "json": str(out_root / "robustness.json"),
            "report": str(out_root / "robustness_report.md"),
        },
        "note": (
            "Continuous-runner equivalent of r1_robustness monthly diagnostics; "
            "legacy scripts/r1_robustness.py expects volume_event_* report layout."
        ),
    }
    _write_json(out_root / "robustness.json", summary)
    _write_robustness_markdown(out_root / "robustness_report.md", summary)
    return summary


def _score_context(panel: pl.DataFrame) -> pl.DataFrame:
    if panel.is_empty():
        return pl.DataFrame()
    base = panel.filter(pl.col("composite").is_not_null())
    d8 = base.filter(pl.col("decile") == 8).group_by("ts_ms").agg(pl.col("composite").max().alias("d8_max"))
    ctx = base.group_by("ts_ms").agg(
        pl.col("composite").median().alias("median_composite"),
        pl.len().alias("xsec_count"),
    )
    return ctx.join(d8, on="ts_ms", how="left")


def _load_klines_context(root: Path, *, start_date: str, end_date: str) -> pl.DataFrame:
    kname = _autodetect_dataset_names(root)["klines_dataset"]
    start_ms = _date_str_to_ms(start_date)
    end_ms = _date_str_to_ms(end_date)
    return _read_window(
        root,
        kname,
        start_ms=start_ms - 420 * MS_PER_DAY,
        end_ms=end_ms,
        columns=["ts_ms", "symbol", "close", "turnover_quote"],
    )


def _rolling_percentile(values: list[float | None], window: int) -> list[float | None]:
    out: list[float | None] = []
    hist: list[float] = []
    for value in values:
        if value is None or not math.isfinite(value):
            out.append(None)
        elif len(hist) < max(30, window // 3):
            out.append(None)
        else:
            subset = hist[-window:]
            out.append(sum(1 for x in subset if x <= value) / len(subset))
        if value is not None and math.isfinite(value):
            hist.append(float(value))
    return out


def _btc_and_market_context(klines: pl.DataFrame) -> pl.DataFrame:
    schema = {
        "day_ts": pl.Int64,
        "btc_ret_30d": pl.Float64,
        "btc_rv_30d": pl.Float64,
        "btc_vol_percentile_250d": pl.Float64,
        "btc_drawdown_30d": pl.Float64,
        "btc_trend_flip_age_days": pl.Int64,
        "market_breadth_1d": pl.Float64,
        "market_dispersion_1d": pl.Float64,
        "alt_minus_btc_1d": pl.Float64,
    }
    if klines.is_empty():
        return pl.DataFrame(schema=schema)
    daily = (
        klines.with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"))
        .group_by(["symbol", "day_ts"])
        .agg(pl.col("close").last().alias("close"))
        .sort(["symbol", "day_ts"])
        .with_columns((pl.col("close") / calendar_shift(pl.col("close"), 1, time_col="day_ts") - 1.0).alias("ret_1d"))
    )
    btc = daily.filter(pl.col("symbol") == "BTCUSDT").sort("day_ts")
    if btc.is_empty():
        return pl.DataFrame(schema=schema)
    btc = btc.with_columns(
        pl.col("ret_1d").shift(1).rolling_sum(window_size=30, min_samples=30).alias("btc_ret_30d"),
        pl.col("ret_1d").shift(1).rolling_std(window_size=30, min_samples=20).alias("btc_rv_30d"),
        (
            pl.col("close").shift(1)
            / pl.col("close").shift(1).rolling_max(window_size=30, min_samples=20)
            - 1.0
        ).alias("btc_drawdown_30d"),
    )
    pct = _rolling_percentile([x for x in btc["btc_rv_30d"].to_list()], 250)
    btc = btc.with_columns(pl.Series("btc_vol_percentile_250d", pct))
    flip_ages: list[int | None] = []
    last_sign: bool | None = None
    age = 0
    for value in btc["btc_ret_30d"].to_list():
        sign = None if value is None else float(value) > 0.0
        if sign is None:
            flip_ages.append(None)
            continue
        if last_sign is None or sign != last_sign:
            age = 0
        else:
            age += 1
        flip_ages.append(age)
        last_sign = sign
    btc = btc.with_columns(pl.Series("btc_trend_flip_age_days", flip_ages))
    btc_ctx = btc.select(
        "day_ts",
        "btc_ret_30d",
        "btc_rv_30d",
        "btc_vol_percentile_250d",
        "btc_drawdown_30d",
        "btc_trend_flip_age_days",
    )

    market = (
        daily.filter(pl.col("ret_1d").is_not_null())
        .group_by("day_ts")
        .agg(
            (pl.col("ret_1d") > 0.0).mean().alias("market_breadth_raw"),
            pl.col("ret_1d").std().alias("market_dispersion_raw"),
            pl.col("ret_1d").mean().alias("market_ret_raw"),
        )
        .sort("day_ts")
    )
    btc_ret = btc.select("day_ts", pl.col("ret_1d").alias("btc_ret_1d_raw"))
    market = (
        market.join(btc_ret, on="day_ts", how="left")
        .with_columns((pl.col("market_ret_raw") - pl.col("btc_ret_1d_raw")).alias("alt_minus_btc_raw"))
        .select(
            "day_ts",
            pl.col("market_breadth_raw").shift(1).alias("market_breadth_1d"),
            pl.col("market_dispersion_raw").shift(1).alias("market_dispersion_1d"),
            pl.col("alt_minus_btc_raw").shift(1).alias("alt_minus_btc_1d"),
        )
    )
    return btc_ctx.join(market, on="day_ts", how="left").select(list(schema))


def _candidate_symbol_date_pairs(tape: pl.DataFrame, *, lookback_days: int) -> set[tuple[str, str]]:
    if tape.is_empty():
        return set()
    pairs: set[tuple[str, str]] = set()
    for symbol, decision_ts in tape.select("symbol", "decision_ts_ms").unique().iter_rows():
        day = dt.datetime.fromtimestamp(int(decision_ts) / 1000, tz=dt.timezone.utc).date()
        for offset in range(lookback_days + 1):
            pairs.add((str(symbol), (day - dt.timedelta(days=offset)).isoformat()))
    return pairs


def _read_candidate_partitions(
    root: Path,
    dataset: str,
    tape: pl.DataFrame,
    *,
    lookback_days: int,
    columns: list[str],
) -> pl.DataFrame:
    try:
        resolved = resolve_dataset_name(root, dataset)
    except FileNotFoundError:
        resolved = dataset
    base = root / resolved
    if not base.exists():
        return pl.DataFrame()
    files: list[Path] = []
    for symbol, date_str in sorted(_candidate_symbol_date_pairs(tape, lookback_days=lookback_days)):
        part = base / f"date={date_str}" / f"symbol={symbol}"
        if part.is_dir():
            files.extend(sorted(part.glob("*.parquet")))
    if not files:
        return pl.DataFrame()
    wanted = list(dict.fromkeys(columns))
    try:
        return pl.read_parquet([str(p) for p in files], columns=wanted)
    except Exception:  # noqa: BLE001 - tolerate schema differences across venues
        schema = pl.scan_parquet(str(files[0])).collect_schema()
        present = [col for col in wanted if col in schema.names()]
        return pl.read_parquet([str(p) for p in files], columns=present) if present else pl.DataFrame()


def _read_symbol_files(
    root: Path,
    dataset: str,
    tape: pl.DataFrame,
    *,
    lookback_days: int,
    columns: list[str],
    all_symbols: bool = False,
) -> pl.DataFrame:
    base = root / dataset
    if not base.exists() or tape.is_empty():
        return pl.DataFrame()
    if all_symbols:
        files = [p for p in sorted(base.glob("*.parquet")) if p.stat().st_size > 0]
    else:
        symbols = sorted(str(s) for s in tape["symbol"].unique().to_list())
        files = [base / f"{symbol}.parquet" for symbol in symbols]
        files = [p for p in files if p.exists() and p.stat().st_size > 0]
    if not files:
        return pl.DataFrame()
    start_ms = int(tape["decision_ts_ms"].min()) - lookback_days * MS_PER_DAY
    end_ms = int(tape["decision_ts_ms"].max())
    wanted = list(dict.fromkeys(columns))
    try:
        schema = pl.scan_parquet(str(files[0])).collect_schema()
        present = [col for col in wanted if col in schema.names()]
        if not present:
            return pl.DataFrame()
        return (
            pl.scan_parquet([str(p) for p in files], missing_columns="insert", extra_columns="ignore")
            .filter((pl.col("ts_ms") >= start_ms) & (pl.col("ts_ms") <= end_ms))
            .select(present)
            .collect()
        )
    except Exception:  # noqa: BLE001 - tolerate a stale/corrupt symbol file while auditing coverage
        return pl.DataFrame()


def _binance_metrics_hourly(root: Path, tape: pl.DataFrame) -> pl.DataFrame:
    if not (root / "binance_usdm_metrics_5m").is_dir():
        return pl.DataFrame()
    raw = _read_symbol_files(
        root,
        "binance_usdm_metrics_5m",
        tape,
        lookback_days=3,
        columns=[
            "ts_ms",
            "symbol",
            "sum_open_interest",
            "sum_open_interest_value",
            "sum_taker_long_short_vol_ratio",
        ],
        all_symbols=True,
    )
    if raw.is_empty() or not {"ts_ms", "symbol"}.issubset(set(raw.columns)):
        return pl.DataFrame()
    ratio = pl.col("sum_taker_long_short_vol_ratio").cast(pl.Float64)
    with_features = raw.with_columns(
        ((pl.col("ts_ms") // MS_PER_HOUR) * MS_PER_HOUR).alias("hour_ts_ms"),
        pl.col("sum_open_interest").cast(pl.Float64).alias("open_interest"),
        pl.col("sum_open_interest_value").cast(pl.Float64).alias("open_interest_value"),
        pl.when(ratio.is_finite() & (ratio > 0.0))
        .then((ratio - 1.0) / (ratio + 1.0))
        .otherwise(None)
        .alias("taker_imbalance_base"),
    )
    hourly = (
        with_features.group_by(["symbol", "hour_ts_ms"])
        .agg(
            pl.col("open_interest").drop_nulls().last().alias("open_interest"),
            pl.col("open_interest_value").drop_nulls().last().alias("open_interest_value"),
            pl.col("taker_imbalance_base").mean().alias("taker_imbalance_base"),
        )
        .sort(["symbol", "hour_ts_ms"])
    )
    market = hourly.group_by("hour_ts_ms").agg(pl.col("taker_imbalance_base").mean().alias("market_flow"))
    return hourly.join(market, on="hour_ts_ms", how="left")


def _attach_asof_values(
    tape: pl.DataFrame,
    values: pl.DataFrame,
    *,
    feature_cols: list[str],
    available_col: str = "available_ts_ms",
) -> pl.DataFrame:
    present = [col for col in feature_cols if col in values.columns]
    if tape.is_empty() or values.is_empty() or not present:
        return tape
    right = values.select(["symbol", available_col, *present]).rename(
        {col: f"__ext_{col}" for col in present}
    )
    joined = tape.sort(["symbol", "decision_ts_ms"]).join_asof(
        right.sort(["symbol", available_col]),
        left_on="decision_ts_ms",
        right_on=available_col,
        by="symbol",
        strategy="backward",
    )
    updates: list[pl.Expr] = []
    drops = [available_col]
    for feature in present:
        ext = f"__ext_{feature}"
        avail = f"{feature}_available_ts_ms"
        updates.append(pl.coalesce(pl.col(ext), pl.col(feature)).alias(feature))
        updates.append(
            pl.when(pl.col(ext).is_not_null())
            .then(pl.col(available_col))
            .otherwise(pl.col(avail))
            .alias(avail)
        )
        drops.append(ext)
    return joined.with_columns(updates).drop([col for col in drops if col in joined.columns])


def _funding_feature_values(root: Path, tape: pl.DataFrame) -> pl.DataFrame:
    names = _autodetect_dataset_names(root)
    raw = _read_candidate_partitions(
        root,
        names["funding_dataset"],
        tape,
        lookback_days=2,
        columns=["ts_ms", "symbol", "funding_rate_8h_equiv", "funding_rate"],
    )
    if raw.is_empty() or not {"ts_ms", "symbol"}.issubset(set(raw.columns)):
        return pl.DataFrame()
    rate_col = "funding_rate_8h_equiv" if "funding_rate_8h_equiv" in raw.columns else "funding_rate"
    if rate_col not in raw.columns:
        return pl.DataFrame()
    return (
        raw.sort(["symbol", "ts_ms"])
        .with_columns(
            pl.col(rate_col).cast(pl.Float64).alias("funding_level"),
            (pl.col(rate_col).cast(pl.Float64) - pl.col(rate_col).cast(pl.Float64).shift(1).over("symbol")).alias(
                "funding_change"
            ),
            pl.col("ts_ms").cast(pl.Int64).alias("available_ts_ms"),
        )
        .select("symbol", "available_ts_ms", "funding_level", "funding_change")
    )


def _premium_feature_values(root: Path, tape: pl.DataFrame) -> pl.DataFrame:
    names = _autodetect_dataset_names(root)
    raw = _read_candidate_partitions(
        root,
        names["premium_dataset"],
        tape,
        lookback_days=2,
        columns=["ts_ms", "symbol", "close"],
    )
    if raw.is_empty() or not {"ts_ms", "symbol", "close"}.issubset(set(raw.columns)):
        return pl.DataFrame()
    return (
        raw.sort(["symbol", "ts_ms"])
        .with_columns(
            pl.col("close").cast(pl.Float64).alias("premium_level"),
            (pl.col("close").cast(pl.Float64) - pl.col("close").cast(pl.Float64).shift(24).over("symbol")).alias(
                "premium_change"
            ),
            (pl.col("ts_ms").cast(pl.Int64) + MS_PER_HOUR).alias("available_ts_ms"),
        )
        .select("symbol", "available_ts_ms", "premium_level", "premium_change")
    )


def _oi_feature_values(root: Path, tape: pl.DataFrame, metrics_hourly: pl.DataFrame | None = None) -> pl.DataFrame:
    names = _autodetect_dataset_names(root)
    if metrics_hourly is not None and not metrics_hourly.is_empty():
        raw = metrics_hourly.rename({"hour_ts_ms": "ts_ms"}).select(
            "ts_ms", "symbol", "open_interest_value", "open_interest"
        )
    else:
        raw = _read_candidate_partitions(
            root,
            names["open_interest_dataset"],
            tape,
            lookback_days=3,
            columns=["ts_ms", "symbol", "open_interest_value", "open_interest"],
        )
    if raw.is_empty() or not {"ts_ms", "symbol"}.issubset(set(raw.columns)):
        return pl.DataFrame()
    value_col = "open_interest_value" if "open_interest_value" in raw.columns else "open_interest"
    if value_col not in raw.columns:
        return pl.DataFrame()
    with_level = raw.sort(["symbol", "ts_ms"]).with_columns(
        pl.col(value_col).cast(pl.Float64).log1p().alias("oi_level")
    )
    with_change = with_level.with_columns(
        (pl.col("oi_level") - pl.col("oi_level").shift(24).over("symbol")).alias("oi_change_24h")
    )
    return (
        with_change.with_columns(
            (pl.col("oi_change_24h") - pl.col("oi_change_24h").shift(24).over("symbol")).alias(
                "oi_acceleration"
            ),
            (pl.col("ts_ms").cast(pl.Int64) + MS_PER_HOUR).alias("available_ts_ms"),
        )
        .select("symbol", "available_ts_ms", "oi_level", "oi_change_24h", "oi_acceleration")
    )


def _flow_dataset_name(root: Path) -> str | None:
    if (root / "taker_flow_5m").is_dir():
        return "taker_flow_5m"
    if (root / "binance_usdm_taker_flow_1h").is_dir():
        return "binance_usdm_taker_flow_1h"
    return None


def _flow_feature_values(root: Path, tape: pl.DataFrame, metrics_hourly: pl.DataFrame | None = None) -> pl.DataFrame:
    if metrics_hourly is not None and not metrics_hourly.is_empty() and "taker_imbalance_base" in metrics_hourly.columns:
        hourly = metrics_hourly.select("symbol", "hour_ts_ms", "taker_imbalance_base", "market_flow").sort(
            ["symbol", "hour_ts_ms"]
        )
        hourly = hourly.with_columns(
            pl.col("taker_imbalance_base").alias("taker_imbalance_1h"),
            pl.col("taker_imbalance_base")
            .rolling_mean(window_size=6, min_samples=1)
            .over("symbol")
            .alias("taker_imbalance_6h"),
            pl.col("taker_imbalance_base")
            .rolling_mean(window_size=24, min_samples=1)
            .over("symbol")
            .alias("taker_imbalance_24h"),
        )
        return (
            hourly.with_columns(
                (pl.col("taker_imbalance_1h") - pl.col("market_flow")).alias("idiosyncratic_flow"),
                (pl.col("hour_ts_ms").cast(pl.Int64) + MS_PER_HOUR).alias("available_ts_ms"),
            )
            .select(
                "symbol",
                "available_ts_ms",
                "taker_imbalance_1h",
                "taker_imbalance_6h",
                "taker_imbalance_24h",
                "market_flow",
                "idiosyncratic_flow",
            )
        )
    dataset = _flow_dataset_name(root)
    if not dataset:
        return pl.DataFrame()
    raw = _read_candidate_partitions(
        root,
        dataset,
        tape,
        lookback_days=2,
        columns=[
            "ts_ms",
            "symbol",
            "taker_buy_quote",
            "taker_sell_quote",
            "buy_volume_base",
            "sell_volume_base",
        ],
    )
    if raw.is_empty() or not {"ts_ms", "symbol"}.issubset(set(raw.columns)):
        return pl.DataFrame()
    if {"taker_buy_quote", "taker_sell_quote"}.issubset(set(raw.columns)):
        buy_col, sell_col = "taker_buy_quote", "taker_sell_quote"
    elif {"buy_volume_base", "sell_volume_base"}.issubset(set(raw.columns)):
        buy_col, sell_col = "buy_volume_base", "sell_volume_base"
    else:
        return pl.DataFrame()
    hourly = (
        raw.with_columns(((pl.col("ts_ms") // MS_PER_HOUR) * MS_PER_HOUR).alias("hour_ts_ms"))
        .group_by(["symbol", "hour_ts_ms"])
        .agg(
            pl.col(buy_col).cast(pl.Float64).sum().alias("buy"),
            pl.col(sell_col).cast(pl.Float64).sum().alias("sell"),
        )
        .sort(["symbol", "hour_ts_ms"])
        .with_columns(
            pl.col("buy").rolling_sum(window_size=6, min_samples=1).over("symbol").alias("buy_6h"),
            pl.col("sell").rolling_sum(window_size=6, min_samples=1).over("symbol").alias("sell_6h"),
            pl.col("buy").rolling_sum(window_size=24, min_samples=1).over("symbol").alias("buy_24h"),
            pl.col("sell").rolling_sum(window_size=24, min_samples=1).over("symbol").alias("sell_24h"),
        )
    )
    hourly = hourly.with_columns(
        pl.when((pl.col("buy") + pl.col("sell")) > 0.0)
        .then((pl.col("buy") - pl.col("sell")) / (pl.col("buy") + pl.col("sell")))
        .otherwise(None)
        .alias("taker_imbalance_1h"),
        pl.when((pl.col("buy_6h") + pl.col("sell_6h")) > 0.0)
        .then((pl.col("buy_6h") - pl.col("sell_6h")) / (pl.col("buy_6h") + pl.col("sell_6h")))
        .otherwise(None)
        .alias("taker_imbalance_6h"),
        pl.when((pl.col("buy_24h") + pl.col("sell_24h")) > 0.0)
        .then((pl.col("buy_24h") - pl.col("sell_24h")) / (pl.col("buy_24h") + pl.col("sell_24h")))
        .otherwise(None)
        .alias("taker_imbalance_24h"),
    )
    market = hourly.group_by("hour_ts_ms").agg(pl.col("taker_imbalance_1h").mean().alias("market_flow"))
    return (
        hourly.join(market, on="hour_ts_ms", how="left")
        .with_columns(
            (pl.col("taker_imbalance_1h") - pl.col("market_flow")).alias("idiosyncratic_flow"),
            (pl.col("hour_ts_ms").cast(pl.Int64) + MS_PER_HOUR).alias("available_ts_ms"),
        )
        .select(
            "symbol",
            "available_ts_ms",
            "taker_imbalance_1h",
            "taker_imbalance_6h",
            "taker_imbalance_24h",
            "market_flow",
            "idiosyncratic_flow",
        )
    )


def _attach_external_features(root: Path, tape: pl.DataFrame) -> pl.DataFrame:
    metrics_hourly = _binance_metrics_hourly(root, tape)
    for values, cols in (
        (_funding_feature_values(root, tape), ["funding_level", "funding_change"]),
        (_premium_feature_values(root, tape), ["premium_level", "premium_change"]),
        (_oi_feature_values(root, tape, metrics_hourly), ["oi_level", "oi_change_24h", "oi_acceleration"]),
        (
            _flow_feature_values(root, tape, metrics_hourly),
            [
                "taker_imbalance_1h",
                "taker_imbalance_6h",
                "taker_imbalance_24h",
                "market_flow",
                "idiosyncratic_flow",
            ],
        ),
    ):
        tape = _attach_asof_values(tape, values, feature_cols=cols)
    return tape


def _expanding_prior_z_series(values: list[float | None], *, min_obs: int) -> list[float | None]:
    """Per-position expanding-prior z-score using strictly prior finite values.

    Returns None during warm-up (fewer than ``min_obs`` prior finite values), so a
    feature's coverage honestly reflects when it is actually defined (unlike
    ``_expanding_prior_z`` which returns a neutral 0.0 for the overlay score).
    """
    out: list[float | None] = []
    hist: list[float] = []
    for value in values:
        if value is None or not math.isfinite(float(value)) or len(hist) < min_obs:
            out.append(None)
        else:
            arr = np.asarray(hist, dtype=float)
            std = float(arr.std(ddof=1)) if arr.size > 1 else 0.0
            out.append((float(value) - float(arr.mean())) / std if std > 1e-12 else 0.0)
        if value is not None and math.isfinite(float(value)):
            hist.append(float(value))
    return out


def _expanding_resid_series(
    y: list[float | None], x: list[float | None], *, min_obs: int
) -> list[float | None]:
    """Causal residual of y on x via an expanding-prior OLS (prior rows only).

    The slope/intercept for row i use only rows < i where both y and x are finite,
    so the residual is strictly causal (no look-ahead). None until ``min_obs`` prior
    pairs exist or when the row's y/x is missing.
    """
    out: list[float | None] = []
    n = 0
    sx = sy = sxx = sxy = 0.0
    for yi, xi in zip(y, x):
        y_ok = yi is not None and math.isfinite(float(yi))
        x_ok = xi is not None and math.isfinite(float(xi))
        if y_ok and x_ok and n >= min_obs:
            mean_x = sx / n
            mean_y = sy / n
            var_x = sxx / n - mean_x * mean_x
            cov = sxy / n - mean_x * mean_y
            slope = cov / var_x if var_x > 1e-12 else 0.0
            intercept = mean_y - slope * mean_x
            out.append(float(yi) - (intercept + slope * float(xi)))
        else:
            out.append(None)
        if y_ok and x_ok:
            fx, fy = float(xi), float(yi)
            sx += fx
            sy += fy
            sxx += fx * fx
            sxy += fx * fy
            n += 1
    return out


def _attach_flow_residual_and_squeeze(tape: pl.DataFrame) -> pl.DataFrame:
    """Value-build causal flow_squeeze and flow_resid_return from the joined tape.

    flow_squeeze: mean of causal expanding-prior z-scores of {oi_change_24h,
    funding_level, taker_imbalance_24h} per symbol (OI build-up + positive funding
    + aggressive taker buy). flow_resid_return: 24h taker imbalance residualized
    (causal expanding per-symbol OLS) against ``path_max_ret168`` — the recent
    run-up the D9 fade targets — a pragmatic translation of the order-flow paper's
    lagged-return reversal control to this short-fade lifecycle. Both are null on a
    venue without value-built OI/flow (e.g. Bybit), matching the venue-scope blocker.
    """
    if tape.is_empty():
        return tape
    for col in ("taker_imbalance_24h", "oi_change_24h", "funding_level", "path_max_ret168"):
        if col not in tape.columns:
            tape = tape.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))
    ordered = tape.sort(["symbol", "signal_ts_ms"])
    symbols = ordered["symbol"].to_list()
    ti = ordered["taker_imbalance_24h"].to_list()
    oi = ordered["oi_change_24h"].to_list()
    fund = ordered["funding_level"].to_list()
    runup = ordered["path_max_ret168"].to_list()
    n = len(symbols)
    squeeze_out: list[float | None] = [None] * n
    resid_out: list[float | None] = [None] * n
    start = 0
    while start < n:
        end = start
        while end < n and symbols[end] == symbols[start]:
            end += 1
        idx = list(range(start, end))
        z_oi = _expanding_prior_z_series([oi[i] for i in idx], min_obs=10)
        z_fund = _expanding_prior_z_series([fund[i] for i in idx], min_obs=10)
        z_ti = _expanding_prior_z_series([ti[i] for i in idx], min_obs=10)
        resid = _expanding_resid_series([ti[i] for i in idx], [runup[i] for i in idx], min_obs=20)
        for k, i in enumerate(idx):
            # flow_squeeze is gated on the defining taker-flow input so it stays
            # Binance-only by construction (a venue without value-built taker flow,
            # e.g. Bybit, must not produce a funding-only pseudo-squeeze).
            if ti[i] is None or not math.isfinite(float(ti[i])):
                squeeze_out[i] = None
            else:
                zs = [z for z in (z_oi[k], z_fund[k], z_ti[k]) if z is not None and math.isfinite(z)]
                squeeze_out[i] = float(sum(zs) / len(zs)) if zs else None
            resid_out[i] = resid[k]
        start = end
    return ordered.with_columns(
        pl.Series("flow_squeeze", squeeze_out, dtype=pl.Float64),
        pl.Series("flow_resid_return", resid_out, dtype=pl.Float64),
    )


def _empty_feature_tape() -> pl.DataFrame:
    cols: dict[str, Any] = {
        "venue": pl.String,
        "component": pl.String,
        "component_live_tag": pl.String,
        "symbol": pl.String,
        "signal_ts_ms": pl.Int64,
        "decision_ts_ms": pl.Int64,
        "order_submit_ts_ms": pl.Int64,
        "day_ts": pl.Int64,
    }
    for item in ALMANAC_FEATURES:
        cols[item["feature"]] = pl.Float64
        cols[f"{item['feature']}_available_ts_ms"] = pl.Int64
    return pl.DataFrame(schema=cols)


def _component_candidates(
    root: Path,
    venue: str,
    spec: ComponentSpec,
    *,
    start_date: str,
    end_date: str,
) -> pl.DataFrame:
    cfg = v2_component_config(spec, start_date=start_date, end_date=end_date)
    panel = build_continuous_panel(root, cfg)
    if panel.is_empty():
        return _empty_feature_tape()
    entries = _fresh_entries(panel, cfg)
    if entries.is_empty():
        return _empty_feature_tape()
    feature_cols = [
        "symbol",
        "ts_ms",
        "decile",
        "composite",
        "turnover_quote",
        "rv_168h",
        "ret1",
        "max_ret168",
        "prior6_ret1_max",
        "giveback_from_prior6_high",
    ]
    present = [c for c in feature_cols if c in panel.columns]
    tape = entries.select("symbol", "ts_ms", "spell_end_ts").join(
        panel.select(present),
        on=["symbol", "ts_ms"],
        how="left",
    )
    ctx = _score_context(panel)
    if not ctx.is_empty():
        tape = tape.join(ctx, on="ts_ms", how="left")
    listings = _listing_ts_by_symbol(root)
    if listings:
        listing_df = pl.DataFrame({"symbol": list(listings), "listing_ts_ms": list(listings.values())})
        tape = tape.join(listing_df, on="symbol", how="left")
    else:
        tape = tape.with_columns(pl.lit(None, dtype=pl.Int64).alias("listing_ts_ms"))

    tape = tape.with_columns(
        pl.lit(venue).alias("venue"),
        pl.lit(spec.key).alias("component"),
        pl.lit(spec.live_tag).alias("component_live_tag"),
        pl.col("ts_ms").alias("signal_ts_ms"),
        (pl.col("ts_ms") + MS_PER_HOUR).alias("decision_ts_ms"),
        (pl.col("ts_ms") + (1 + cfg.entry_delay_hours) * MS_PER_HOUR).alias("order_submit_ts_ms"),
        ((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"),
        pl.col("composite").alias("current_composite"),
        (pl.col("composite") - pl.col("d8_max")).alias("score_margin_d9_d8"),
        (pl.col("composite") - pl.col("median_composite")).alias("score_margin_d9_median"),
        pl.when(pl.col("xsec_count") > 1)
        .then((pl.col("decile").cast(pl.Float64) / 9.0).clip(0.0, 1.0))
        .otherwise(None)
        .alias("rank_distance"),
        pl.lit(1.0).alias("feature_agreement"),
        pl.col("turnover_quote").alias("liquidity_turnover"),
        pl.col("turnover_quote").alias("realized_slippage_proxy"),
        pl.col("ret1").alias("path_ret_1h"),
        pl.col("prior6_ret1_max").alias("path_ret_6h_max"),
        pl.col("rv_168h").alias("path_rv_168h"),
        pl.col("max_ret168").alias("path_max_ret168"),
        pl.col("giveback_from_prior6_high").alias("path_giveback_from_prior6_high"),
    )
    for item in ALMANAC_FEATURES:
        feature = item["feature"]
        if feature not in tape.columns:
            tape = tape.with_columns(pl.lit(None, dtype=pl.Float64).alias(feature))
        available_col = f"{feature}_available_ts_ms"
        if available_col not in tape.columns:
            available = pl.col("decision_ts_ms") if feature in VALUE_BUILT_FEATURES else pl.lit(None, dtype=pl.Int64)
            tape = tape.with_columns(available.alias(available_col))
    wanted = [c for c in _empty_feature_tape().columns if c in tape.columns]
    return tape.select(wanted).sort(["signal_ts_ms", "component", "symbol"])


def _feature_tape(root: Path, venue: str, *, start_date: str, end_date: str) -> pl.DataFrame:
    frames = [
        _component_candidates(root, venue, spec, start_date=start_date, end_date=end_date)
        for spec in V2_COMPONENTS
    ]
    non_empty = [df for df in frames if not df.is_empty()]
    if not non_empty:
        return _empty_feature_tape()
    tape = pl.concat(non_empty, how="diagonal")
    klines = _load_klines_context(root, start_date=start_date, end_date=end_date)
    ctx = _btc_and_market_context(klines)
    if not ctx.is_empty():
        tape = tape.join(ctx, on="day_ts", how="left", suffix="_ctx")
        for col in ctx.columns:
            if col == "day_ts":
                continue
            ctx_col = f"{col}_ctx"
            if ctx_col in tape.columns:
                tape = tape.with_columns(pl.coalesce(pl.col(ctx_col), pl.col(col)).alias(col)).drop(ctx_col)
            tape = tape.with_columns(
                pl.when(pl.col(col).is_not_null())
                .then(pl.col("decision_ts_ms"))
                .otherwise(None)
                .alias(f"{col}_available_ts_ms")
            )
    tape = _attach_external_features(root, tape)
    tape = _attach_flow_residual_and_squeeze(tape)
    return tape.sort(["signal_ts_ms", "component", "symbol"])


def _inventory_rows(tape: pl.DataFrame, venue: str) -> list[dict[str, Any]]:
    rows = []
    n = max(tape.height, 1)
    for item in ALMANAC_FEATURES:
        feature = item["feature"]
        if feature in tape.columns:
            coverage = float(tape[feature].is_not_null().sum()) / n if tape.height else 0.0
            avail_col = f"{feature}_available_ts_ms"
            earliest = int(tape[avail_col].min()) if avail_col in tape.columns and tape[avail_col].drop_nulls().len() else None
            latest = int(tape[avail_col].max()) if avail_col in tape.columns and tape[avail_col].drop_nulls().len() else None
        else:
            coverage, earliest, latest = 0.0, None, None
        built = feature in VALUE_BUILT_FEATURES
        blocker = FEATURE_ADMISSIBILITY_BLOCKERS.get(feature, "")
        if venue == "binance" and feature in {"market_flow", "idiosyncratic_flow"}:
            blocker = ""
        admissible = built and coverage >= 0.95 and not blocker
        rows.append(
            {
                "feature": feature,
                "source_table": item["source_table"],
                "family": item["family"],
                "venue": venue,
                "candidate_rows": tape.height,
                "coverage": coverage,
                "earliest_available_ts": earliest,
                "latest_available_ts": latest,
                "earliest_available_date": _date_ms_to_iso(earliest),
                "latest_available_date": _date_ms_to_iso(latest),
                "decision_lag": "closed-bar causal" if built else "not yet value-built",
                "admissible_for_full_ab": admissible,
                "known_gaps": "" if admissible else (blocker or "data-gated or not yet value-built in foundation almanac"),
            }
        )
    return rows


def _coverage_by_symbol_year(tape: pl.DataFrame) -> pl.DataFrame:
    if tape.is_empty():
        return pl.DataFrame(
            schema={
                "venue": pl.String,
                "symbol": pl.String,
                "year": pl.Int32,
                "candidate_rows": pl.Int64,
                "current_composite_coverage": pl.Float64,
            }
        )
    return (
        tape.with_columns(pl.from_epoch("signal_ts_ms", time_unit="ms").dt.year().alias("year"))
        .group_by(["venue", "symbol", "year"])
        .agg(
            pl.len().alias("candidate_rows"),
            pl.col("current_composite").is_not_null().mean().alias("current_composite_coverage"),
        )
        .sort(["venue", "symbol", "year"])
    )


def _coverage_by_component(tape: pl.DataFrame) -> pl.DataFrame:
    if tape.is_empty():
        return pl.DataFrame(
            schema={
                "venue": pl.String,
                "component": pl.String,
                "candidate_rows": pl.Int64,
                "first_signal_ts_ms": pl.Int64,
                "last_signal_ts_ms": pl.Int64,
                "current_composite_coverage": pl.Float64,
            }
        )
    return (
        tape.group_by(["venue", "component"])
        .agg(
            pl.len().alias("candidate_rows"),
            pl.col("signal_ts_ms").min().alias("first_signal_ts_ms"),
            pl.col("signal_ts_ms").max().alias("last_signal_ts_ms"),
            pl.col("current_composite").is_not_null().mean().alias("current_composite_coverage"),
        )
        .sort(["venue", "component"])
    )


def _latency_audit(tape: pl.DataFrame, venue: str) -> pl.DataFrame:
    rows = []
    if tape.is_empty():
        return pl.DataFrame(
            schema={
                "venue": pl.String,
                "feature": pl.String,
                "rows": pl.Int64,
                "coverage": pl.Float64,
                "delayed_coverage": pl.Float64,
                "same_rate": pl.Float64,
                "max_abs_delta": pl.Float64,
                "latency_copy": pl.String,
            }
        )
    ordered = tape.sort(["symbol", "signal_ts_ms"])
    for feature in sorted(VALUE_BUILT_FEATURES & set(tape.columns)):
        delayed = ordered.with_columns(pl.col(feature).shift(1).over("symbol").alias("_delayed"))
        both = delayed.filter(pl.col(feature).is_not_null() & pl.col("_delayed").is_not_null())
        same_rate = None
        max_delta = None
        if not both.is_empty():
            diff = (both[feature] - both["_delayed"]).abs()
            same_rate = float((diff <= 1e-12).mean())
            max_delta = float(diff.max())
        rows.append(
            {
                "venue": venue,
                "feature": feature,
                "rows": tape.height,
                "coverage": float(ordered[feature].is_not_null().sum()) / max(ordered.height, 1),
                "delayed_coverage": float(delayed["_delayed"].is_not_null().sum()) / max(delayed.height, 1),
                "same_rate": same_rate,
                "max_abs_delta": max_delta,
                "latency_copy": "one prior candidate within symbol",
            }
        )
    return pl.DataFrame(rows)


def _negative_controls(tape: pl.DataFrame) -> pl.DataFrame:
    schema = {
        "venue": pl.String,
        "component": pl.String,
        "symbol": pl.String,
        "signal_ts_ms": pl.Int64,
        "symbol_hash": pl.Int64,
        "calendar_hash": pl.Int64,
        "shuffled_within_symbol": pl.Int64,
        "shuffled_within_day": pl.Int64,
    }
    if tape.is_empty():
        return pl.DataFrame(schema=schema)
    rows = []
    for row in tape.select("venue", "component", "symbol", "signal_ts_ms", "day_ts").iter_rows(named=True):
        rows.append(
            {
                "venue": row["venue"],
                "component": row["component"],
                "symbol": row["symbol"],
                "signal_ts_ms": int(row["signal_ts_ms"]),
                "symbol_hash": _hash_int(row["symbol"]) % 1_000_000,
                "calendar_hash": _hash_int(row["day_ts"]) % 1_000_000,
                "shuffled_within_symbol": _hash_int(row["symbol"], row["day_ts"]) % 1_000_000,
                "shuffled_within_day": _hash_int(row["day_ts"], row["symbol"]) % 1_000_000,
            }
        )
    return pl.DataFrame(rows, schema=schema)


def _feature_corr(tape: pl.DataFrame, venue: str) -> pl.DataFrame:
    rows = []
    numeric = [f for f in VALUE_BUILT_FEATURES if f in tape.columns]
    for i, left in enumerate(numeric):
        x = tape[left].to_numpy()
        for right in numeric[i + 1 :]:
            y = tape[right].to_numpy()
            mask = np.isfinite(x) & np.isfinite(y)
            if int(mask.sum()) < 3:
                corr = None
            elif float(np.std(x[mask])) <= 1e-12 or float(np.std(y[mask])) <= 1e-12:
                corr = None
            else:
                corr = float(np.corrcoef(x[mask], y[mask])[0, 1])
            rows.append({"venue": venue, "feature_left": left, "feature_right": right, "corr": corr})
    return pl.DataFrame(rows)


def _write_almanac_readme(path: Path, inventory: pl.DataFrame) -> None:
    admissible = []
    gated = []
    for row in inventory.iter_rows(named=True):
        target = admissible if row["admissible_for_full_ab"] else gated
        target.append(f"- {row['venue']} `{row['feature']}`: coverage={row['coverage']:.3f}")
    text = "# Continuous V2 Feature Almanac\n\n"
    text += "This is a data proof, not an alpha test. It does not approve any arm for real money.\n\n"
    text += "## Admissible Foundation Features\n\n"
    text += "\n".join(admissible) if admissible else "- None\n"
    text += "\n\n## Data-Gated Or Not Yet Value-Built\n\n"
    text += "\n".join(gated) if gated else "- None\n"
    text += "\n\nExperimental arms remain blocked unless their required features are admissible on the claimed venues.\n"
    path.write_text(text, encoding="utf-8")


def build_feature_almanac(
    *,
    out_root: Path,
    venues: list[str],
    start_date: str,
    end_date: str,
    resume: bool,
) -> dict[str, Any]:
    out_root.mkdir(parents=True, exist_ok=True)
    inventory_rows: list[dict[str, Any]] = []
    symbol_year_frames = []
    component_frames = []
    latency_frames = []
    negative_frames = []
    corr_frames = []
    tape_paths: dict[str, str] = {}
    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            raise FileNotFoundError(f"full-PIT data root missing for {venue}: {root}")
        tape_path = out_root / f"feature_tape_{venue}.parquet"
        if resume and tape_path.exists():
            tape = pl.read_parquet(tape_path)
        else:
            tape = _feature_tape(root, venue, start_date=start_date, end_date=end_date)
            tape.write_parquet(tape_path)
        tape_paths[venue] = str(tape_path)
        inventory_rows.extend(_inventory_rows(tape, venue))
        symbol_year_frames.append(_coverage_by_symbol_year(tape))
        component_frames.append(_coverage_by_component(tape))
        latency_frames.append(_latency_audit(tape, venue))
        negative_frames.append(_negative_controls(tape))
        corr_frames.append(_feature_corr(tape, venue))

    inventory = pl.DataFrame(inventory_rows)
    inventory.write_csv(out_root / "feature_inventory.csv")
    pl.concat(symbol_year_frames, how="diagonal").write_csv(out_root / "coverage_by_symbol_year.csv")
    pl.concat(component_frames, how="diagonal").write_csv(out_root / "coverage_by_component.csv")
    pl.concat(latency_frames, how="diagonal").write_csv(out_root / "latency_audit.csv")
    pl.concat(negative_frames, how="diagonal").write_csv(out_root / "negative_controls.csv")
    pl.concat(corr_frames, how="diagonal").write_csv(out_root / "feature_corr.csv")
    _write_almanac_readme(out_root / "readme.md", inventory)
    summary = {
        "run_label": "feature_almanac_data_proof",
        "preregistration": PREREGISTRATION,
        "venues": venues,
        "start_date": start_date,
        "end_date_exclusive": end_date,
        "git_commit": _git_commit(),
        "tape_paths": tape_paths,
        "feature_inventory": str(out_root / "feature_inventory.csv"),
        "admissible_features": [
            {"venue": r["venue"], "feature": r["feature"]}
            for r in inventory.filter(pl.col("admissible_for_full_ab")).iter_rows(named=True)
        ],
        "blocked_candidate_arms": {
            arm: ARM_DEFINITIONS[arm].blocked_reason
            for arm in ("A4_REGIME_HEDGE_INTENSITY", "C2_MARKET_FLOW_HEDGE_INTENSITY", "C3_FLOW_SQUEEZE_HEDGE_INTENSITY")
        },
    }
    _write_json(out_root / "summary.json", summary)
    return summary


def _resolve_artifact_root(value: str | None, *, kind: str, date_tag: str) -> Path:
    if value:
        root = Path(value).expanduser()
        return root if root.is_absolute() else REPO / root
    return resolve_run_root(None, kind=kind, date_tag=date_tag)


def _rank_array(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.size, dtype=float)
    i = 0
    while i < values.size:
        j = i + 1
        while j < values.size and values[order[j]] == values[order[i]]:
            j += 1
        ranks[order[i:j]] = (i + j - 1) / 2.0 + 1.0
        i = j
    return ranks


def _pearson(x: np.ndarray, y: np.ndarray) -> float | None:
    mask = np.isfinite(x) & np.isfinite(y)
    if int(mask.sum()) < 30:
        return None
    xx = x[mask]
    yy = y[mask]
    if float(np.std(xx)) <= 1e-12 or float(np.std(yy)) <= 1e-12:
        return None
    return float(np.corrcoef(xx, yy)[0, 1])


def _rank_corr(df: pl.DataFrame, feature: str, target: str) -> float | None:
    if df.is_empty() or feature not in df.columns or target not in df.columns:
        return None
    sub = df.select(feature, target).drop_nulls()
    if sub.height < 30:
        return None
    x = sub[feature].to_numpy().astype(float)
    y = sub[target].to_numpy().astype(float)
    mask = np.isfinite(x) & np.isfinite(y)
    if int(mask.sum()) < 30:
        return None
    return _pearson(_rank_array(x[mask]), _rank_array(y[mask]))


def _demeaned_rank_corr(df: pl.DataFrame, feature: str, target: str, group: str) -> float | None:
    if df.is_empty() or not {feature, target, group}.issubset(set(df.columns)):
        return None
    sub = (
        df.select(group, feature, target)
        .drop_nulls()
        .with_columns(
            (pl.col(feature) - pl.col(feature).mean().over(group)).alias("_x"),
            (pl.col(target) - pl.col(target).mean().over(group)).alias("_y"),
        )
    )
    if sub.height < 30:
        return None
    return _rank_corr(sub, "_x", "_y")


def _top_bottom_delta(df: pl.DataFrame, feature: str, target: str, *, q: float = 0.2) -> float | None:
    if df.is_empty() or feature not in df.columns or target not in df.columns:
        return None
    sub = df.select(feature, target).drop_nulls().sort(feature)
    n = sub.height
    k = max(1, int(n * q))
    if n < 30 or k < 5:
        return None
    top = float(sub.tail(k)[target].mean())
    bottom = float(sub.head(k)[target].mean())
    return top - bottom


def _null_max_rank_ic(df: pl.DataFrame, target: str) -> float | None:
    values = []
    for col in SCREEN_NULL_COLUMNS:
        if col in df.columns:
            corr = _rank_corr(df, col, target)
            if corr is not None and math.isfinite(corr):
                values.append(abs(corr))
    return max(values) if values else None


def _screen_rows(
    df: pl.DataFrame,
    *,
    venue: str,
    inventory: dict[tuple[str, str], dict[str, Any]],
    daily: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if df.is_empty():
        return rows
    target = "basket_return" if daily else "net_return"
    tail_target = "negative_basket_return" if daily else "mae"
    group = "month" if daily else "symbol"
    null_max = _null_max_rank_ic(df, target)
    for book, features in SCREEN_GROUPS.items():
        for feature in features:
            if feature not in df.columns:
                continue
            inv = inventory.get((venue, feature), {})
            n_rows = int(df.height)
            n_feature = int(df[feature].is_not_null().sum())
            coverage = n_feature / max(n_rows, 1)
            rows.append(
                {
                    "screen_type": "daily" if daily else "trade",
                    "venue": venue,
                    "book": book,
                    "feature": feature,
                    "rows": n_rows,
                    "feature_rows": n_feature,
                    "executed_coverage": coverage,
                    "candidate_coverage": inv.get("coverage"),
                    "admissible_for_full_ab": inv.get("admissible_for_full_ab"),
                    "known_gaps": inv.get("known_gaps", ""),
                    f"rank_ic_{target}": _rank_corr(df, feature, target),
                    f"{group}_demeaned_rank_ic_{target}": _demeaned_rank_corr(df, feature, target, group),
                    f"top_minus_bottom_{target}": _top_bottom_delta(df, feature, target),
                    f"rank_ic_{tail_target}": _rank_corr(df, feature, tail_target),
                    f"top_minus_bottom_{tail_target}": _top_bottom_delta(df, feature, tail_target),
                    f"null_max_abs_rank_ic_{target}": null_max,
                }
            )
    return rows


def _load_inventory(almanac_root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    path = almanac_root / "feature_inventory.csv"
    if not path.exists():
        raise FileNotFoundError(f"missing almanac inventory: {path}")
    return {
        (str(row["venue"]), str(row["feature"])): row
        for row in pl.read_csv(path).iter_rows(named=True)
    }


def _executed_feature_tape(almanac_root: Path, ab_root: Path, venue: str) -> pl.DataFrame:
    tape_path = almanac_root / f"feature_tape_{venue}.parquet"
    trades_path = ab_root / CONTROL_ARM / venue / "trades.csv"
    neg_path = almanac_root / "negative_controls.csv"
    if not tape_path.exists():
        raise FileNotFoundError(f"missing feature tape: {tape_path}")
    if not trades_path.exists():
        raise FileNotFoundError(f"missing control trades: {trades_path}")
    tape = pl.read_parquet(tape_path)
    trades = pl.read_csv(trades_path).with_columns(
        pl.lit(venue).alias("venue"),
        pl.col("entry_signal_ts_ms").alias("signal_ts_ms"),
    )
    keys = ["venue", "component", "symbol", "signal_ts_ms"]
    joined = trades.join(tape, on=keys, how="left", suffix="_feature")
    if neg_path.exists():
        neg = pl.read_csv(neg_path)
        joined = joined.join(neg, on=keys, how="left")
    return joined


def _daily_feature_tape(executed: pl.DataFrame, ab_root: Path, venue: str) -> pl.DataFrame:
    equity_path = ab_root / CONTROL_ARM / venue / "equity.csv"
    if not equity_path.exists():
        raise FileNotFoundError(f"missing control equity: {equity_path}")
    equity = pl.read_csv(equity_path).with_columns(
        ((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"),
        (-pl.col("basket_return")).alias("negative_basket_return"),
        pl.from_epoch("ts_ms", time_unit="ms").dt.strftime("%Y-%m").alias("month"),
    )
    feature_cols = sorted({f for features in SCREEN_GROUPS.values() for f in features if f in executed.columns})
    null_cols = [col for col in SCREEN_NULL_COLUMNS if col in executed.columns]
    if not feature_cols:
        return equity
    daily_features = (
        executed.with_columns(((pl.col("entry_ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"))
        .group_by("day_ts")
        .agg(
            [pl.col(col).mean().alias(col) for col in feature_cols]
            + [pl.col(col).mean().alias(col) for col in null_cols]
            + [pl.len().alias("trade_rows")]
        )
    )
    return equity.join(daily_features, on="day_ts", how="left")


def _write_screen_readme(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Continuous V2 Feature Screens",
        "",
        "Discovery only. These screens do not accept an alpha mechanism and do not approve real money.",
        "",
        f"- Run label: `{summary['run_label']}`",
        f"- Preregistration: `{summary['preregistration']}`",
        f"- Control root: `{summary['control_root']}`",
        f"- Almanac root: `{summary['almanac_root']}`",
        "",
        "## Outputs",
        "",
        "- `trade_feature_screen.csv`: executed-control-trade screens.",
        "- `daily_feature_screen.csv`: daily control-return screens.",
        "- `executed_feature_tape_<venue>.parquet`: executed control trades joined to causal feature values.",
        "- `daily_feature_tape_<venue>.parquet`: daily control returns joined to daily feature aggregates.",
        "",
        "## Current Blockers",
        "",
    ]
    for arm, reason in summary["blocked_candidate_arms"].items():
        lines.append(f"- `{arm}`: {reason}")
    lines.append("")
    lines.append("Use these tables to choose the next pre-registered lifecycle A/B; do not cite a screen as a lifecycle result.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_feature_screens(
    *,
    out_root: Path,
    venues: list[str],
    almanac_root: Path,
    ab_root: Path,
) -> dict[str, Any]:
    out_root.mkdir(parents=True, exist_ok=True)
    inventory = _load_inventory(almanac_root)
    trade_rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    venue_summaries: dict[str, Any] = {}
    for venue in venues:
        executed = _executed_feature_tape(almanac_root, ab_root, venue)
        executed_path = out_root / f"executed_feature_tape_{venue}.parquet"
        executed.write_parquet(executed_path)
        daily = _daily_feature_tape(executed, ab_root, venue)
        daily_path = out_root / f"daily_feature_tape_{venue}.parquet"
        daily.write_parquet(daily_path)
        trade_rows.extend(_screen_rows(executed, venue=venue, inventory=inventory, daily=False))
        daily_rows.extend(_screen_rows(daily, venue=venue, inventory=inventory, daily=True))
        venue_summaries[venue] = {
            "executed_rows": int(executed.height),
            "daily_rows": int(daily.height),
            "executed_feature_tape": str(executed_path),
            "daily_feature_tape": str(daily_path),
            "unmatched_feature_rows": int(executed["decision_ts_ms"].is_null().sum())
            if "decision_ts_ms" in executed.columns
            else int(executed.height),
        }
    trade_table = pl.DataFrame(trade_rows, infer_schema_length=None)
    daily_table = pl.DataFrame(daily_rows, infer_schema_length=None)
    trade_table.write_csv(out_root / "trade_feature_screen.csv")
    daily_table.write_csv(out_root / "daily_feature_screen.csv")
    summary = {
        "run_label": "feature_screen_discovery",
        "preregistration": PREREGISTRATION,
        "venues": venues,
        "git_commit": _git_commit(),
        "control_root": str(ab_root),
        "almanac_root": str(almanac_root),
        "outputs": {
            "trade_feature_screen": str(out_root / "trade_feature_screen.csv"),
            "daily_feature_screen": str(out_root / "daily_feature_screen.csv"),
        },
        "venue_summaries": venue_summaries,
        "blocked_candidate_arms": {
            "A4_REGIME_HEDGE_INTENSITY": (
                "blocked as written until aggregate OI/taker-flow squeeze inputs are admissible, "
                "or a dated amendment defines a narrower regime arm"
            ),
            "C2_MARKET_FLOW_HEDGE_INTENSITY": "blocked until full-market residualized flow has both-venue coverage",
            "C3_FLOW_SQUEEZE_HEDGE_INTENSITY": "blocked until OI/taker-flow/flow_squeeze coverage is sufficient",
        },
    }
    _write_json(out_root / "summary.json", summary)
    _write_screen_readme(out_root / "readme.md", summary)
    return summary


def run_ab(
    *,
    out_root: Path,
    arms: list[str],
    venues: list[str],
    start_date: str,
    end_date: str,
    resume: bool,
    max_workers: int,
    almanac_root: Path | None = None,
) -> list[dict[str, Any]]:
    validate_arms(arms)
    enforce_control_guard(arms, venues, out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    for arm in arms:
        _assert_implemented(arm)
    tasks = [(arm, venue) for arm in arms for venue in venues]
    rows: list[dict[str, Any]] = []
    workers = max(1, int(max_workers))
    if workers == 1:
        for arm, venue in tasks:
            rows.append(
                run_arm_venue(
                    arm,
                    venue,
                    out_root=out_root,
                    start_date=start_date,
                    end_date=end_date,
                    resume=resume,
                    almanac_root=almanac_root,
                )
            )
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(
                    run_arm_venue,
                    arm,
                    venue,
                    out_root=out_root,
                    start_date=start_date,
                    end_date=end_date,
                    resume=resume,
                    almanac_root=almanac_root,
                ): (arm, venue)
                for arm, venue in tasks
            }
            for fut in as_completed(futs):
                rows.append(fut.result())
    write_pooled_tables(out_root, rows)
    _write_json(
        out_root / "summary.json",
        {
            "run_label": RUN_LABEL,
            "preregistration": PREREGISTRATION,
            "arms": arms,
            "venues": venues,
            "start_date": start_date,
            "end_date_exclusive": end_date,
            "git_commit": _git_commit(),
            "tables": {
                "ab_table": str(out_root / "ab_table.csv"),
                "pooled_ab_table": str(out_root / "pooled_ab_table.csv"),
                "decision_rule_input": str(out_root / "decision_rule_input.csv"),
            },
        },
    )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["ab", "almanac", "screen", "robustness"], default="ab")
    parser.add_argument("--arms", default=CONTROL_ARM, help="comma-separated arms for --mode ab")
    parser.add_argument("--venues", default="bybit,binance", help="comma-separated venues")
    parser.add_argument("--start-date", default=DEFAULT_START)
    parser.add_argument("--end-date", required=True, help="exclusive UTC date boundary")
    parser.add_argument("--out-root", default=None)
    parser.add_argument("--ab-root", default=None, help="control A/B root for --mode screen")
    parser.add_argument("--almanac-root", default=None, help="feature almanac root for --mode screen")
    parser.add_argument("--control", default=CONTROL_ARM, help="control arm for --mode robustness")
    parser.add_argument("--n-boot", type=int, default=5000, help="bootstrap samples for --mode robustness")
    parser.add_argument("--block", type=int, default=3, help="monthly block length for --mode robustness")
    parser.add_argument("--seed", type=int, default=0, help="bootstrap seed for --mode robustness")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--date-tag", default=_today_tag())
    args = parser.parse_args()

    venues = _parse_csv(args.venues)
    bad_venues = sorted(set(venues) - set(VENUES))
    if bad_venues:
        parser.error(f"unknown venue(s): {', '.join(bad_venues)}")

    if args.mode == "almanac":
        out_root = resolve_run_root(args.out_root, kind="almanac", date_tag=args.date_tag)
        summary = build_feature_almanac(
            out_root=out_root,
            venues=venues,
            start_date=args.start_date,
            end_date=args.end_date,
            resume=args.resume,
        )
        print(json.dumps(summary, indent=2, default=_json_default))
        return 0

    if args.mode == "screen":
        out_root = resolve_run_root(args.out_root, kind="screen", date_tag=args.date_tag)
        summary = build_feature_screens(
            out_root=out_root,
            venues=venues,
            almanac_root=_resolve_artifact_root(args.almanac_root, kind="almanac", date_tag=args.date_tag),
            ab_root=_resolve_artifact_root(args.ab_root, kind="ab", date_tag=args.date_tag),
        )
        print(json.dumps(summary, indent=2, default=_json_default))
        return 0

    if args.mode == "robustness":
        ab_root = _resolve_artifact_root(args.ab_root or args.out_root, kind="ab", date_tag=args.date_tag)
        summary = write_robustness_report(
            ab_root,
            control=args.control,
            n_boot=args.n_boot,
            block=args.block,
            seed=args.seed,
        )
        print(json.dumps({"out_root": str(ab_root), "rows": len(summary["rows"])}, indent=2))
        return 0

    arms = _parse_csv(args.arms)
    out_root = resolve_run_root(args.out_root, kind="ab", date_tag=args.date_tag)
    rows = run_ab(
        out_root=out_root,
        arms=arms,
        venues=venues,
        start_date=args.start_date,
        end_date=args.end_date,
        resume=args.resume,
        max_workers=args.max_workers,
        almanac_root=_resolve_artifact_root(args.almanac_root, kind="almanac", date_tag=args.date_tag)
        if any(arm in OVERLAY_ARMS or arm in SIZING_ARMS for arm in arms)
        else None,
    )
    print(json.dumps({"out_root": str(out_root), "runs": len(rows)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
