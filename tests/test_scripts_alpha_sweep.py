"""Regression tests for scripts/alpha_sweep.py (audit bucket b15).

Findings covered:

  alpha-scripts-3  rotate experiment was a silent no-op (dead override key)
  alpha-scripts-4  klines forward-pad fixed at BASE.max_hold (truncated maxhold)
  alpha-scripts-5  duplicate turnsurge block made the lookback version dead code
  alpha-scripts-6  funding / fadeconfirm gate diagnostics had no era split
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


alpha_sweep = _load("alpha_sweep_b15", "scripts/alpha_sweep.py")


def test_rotate_experiment_no_longer_silent_noop() -> None:
    # alpha-scripts-3: `rotate` returned an override key that is neither a config
    # field nor consumed by entry-resolution, so every cell ran base-equivalent
    # and manufactured a flat null. The branch is removed -> it must now raise
    # 'unknown experiment' instead of silently returning base-equivalent cells.
    with pytest.raises(SystemExit) as ei:
        alpha_sweep._experiment("rotate")
    assert "unknown experiment" in str(ei.value)


def test_no_experiment_emits_a_dead_override_key() -> None:
    # alpha-scripts-3 (general guard): no experiment cell may carry an override
    # key that the main loop would silently drop. A key must be either a
    # ContinuousEventConfig field OR one of the harness keys the entry-resolution
    # branches actually consume; otherwise the cell runs base-equivalent.
    from dataclasses import fields

    from liquidity_migration.continuous_events import ContinuousEventConfig

    cfg_fields = {f.name for f in fields(ContinuousEventConfig)}
    entry_resolved = {"rmom_quantile", "liq_turnover_min", "turnover_surge_min"}
    # Every generic (plan-based) experiment the dispatcher will route through the
    # cfg_fields filter. Special branches return before the plan loop.
    generic = ["mfe", "mfefine", "liq", "turnsurge", "delay", "ff6", "bkeven",
               "rmom", "best", "stack", "maxhold", "maxactive", "sizing"]
    for exp in generic:
        for label, ov in alpha_sweep._experiment(exp):
            dead = [k for k in ov if k not in cfg_fields and k not in entry_resolved]
            assert not dead, f"{exp}:{label} has dead override key(s) {dead}"


def test_turnsurge_returns_working_k_only_cells_not_dead_lookback() -> None:
    # alpha-scripts-5: there were two `if name == 'turnsurge'` blocks; the second
    # (k x lookback via turnover_surge_lookback_h) was unreachable AND emitted a
    # dead key. _experiment('turnsurge') must return the reachable k-only cells,
    # and none of them may carry turnover_surge_lookback_h.
    cells = alpha_sweep._experiment("turnsurge")
    labels = [lbl for lbl, _ in cells]
    assert labels == ["base", "k1.25", "k1.5", "k2.0", "k3.0", "k5.0"]
    for _lbl, ov in cells:
        assert "turnover_surge_lookback_h" not in ov


def test_forward_pad_covers_longest_swept_max_hold() -> None:
    # alpha-scripts-4: the pad was sized off BASE.max_hold_hours (48h), truncating
    # the maxhold sweep's long-hold cells (up to 168h). The pad must now cover the
    # LONGEST max_hold any requested experiment actually runs.
    assert alpha_sweep._max_swept_max_hold_hours("maxhold") == 168
    # an experiment that never sweeps max_hold falls back to BASE.
    assert alpha_sweep._max_swept_max_hold_hours("mfe") == int(alpha_sweep.BASE.max_hold_hours)
    # comma-joined experiments take the max across all of them.
    assert alpha_sweep._max_swept_max_hold_hours("mfe,maxhold") == 168


def test_funding_and_fadeconfirm_carry_era_split() -> None:
    # alpha-scripts-6: the funding and fadeconfirm gate diagnostics picked/applied
    # gates full-sample and reported only pooled MAR. Both must now emit an
    # era1/era2 MAR split (split on entry_ts_ms vs BASE.split_date) like `regime`.
    src = inspect.getsource(alpha_sweep.main)
    funding_block = src.split('args.experiment == "funding"', 1)[1].split("DONE", 1)[0]
    fadeconfirm_block = src.split('args.experiment == "fadeconfirm"', 1)[1].split("DONE", 1)[0]
    for block, name in ((funding_block, "funding"), (fadeconfirm_block, "fadeconfirm")):
        assert "era1_mar" in block and "era2_mar" in block, f"{name} lacks an era split"
        assert "BASE.split_date" in block, f"{name} must split on BASE.split_date"
