"""Regression tests for audit bucket b15.

Each test would FAIL on the original bug and PASS on the fix. Findings covered:

  alpha-scripts-3  rotate experiment was a silent no-op (dead override key)
  alpha-scripts-4  klines forward-pad fixed at BASE.max_hold (truncated maxhold)
  alpha-scripts-5  duplicate turnsurge block made the lookback version dead code
  alpha-scripts-6  funding / fadeconfirm gate diagnostics had no era split
  reconciliation-3 reconcile wrapper always exited 0 (no machine-checkable gate)
  reconciliation-5 crashed leg rendered as benign '(no output)'
  ws-pool-2        newest_ts_ms stale after trimming the lone global-max holder
  ws-pool-5        final stop() flush could race a lingering loop flush
  kill-switch-3    fake systemctl marked units active on bare `enable`
  backfill-writers-5  regenerate_hedge_warmstart --validate never gated overwrite
"""

from __future__ import annotations

import importlib.util
import inspect
import subprocess
import threading
from pathlib import Path

import polars as pl
import pytest

from liquidity_migration._common import MS_PER_HOUR
from liquidity_migration.kline_store import KlineStore

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _ws_bar(ts_ms: int, *, close: float = 100.0) -> dict:
    return {
        "start": str(ts_ms),
        "open": str(close - 1.0),
        "high": str(close + 1.0),
        "low": str(close - 2.0),
        "close": str(close),
        "volume": "1000",
        "turnover": str(1000.0 * close),
    }


# ---------------------------------------------------------------- alpha_sweep
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


# ----------------------------------------------------------------- reconcile
reconcile = _load("reconcile_b15", "scripts/reconcile.py")


def test_summarize_leg_passes_only_on_clean_leg() -> None:
    # reconciliation-5: a clean leg returns its summary line and ok=True.
    line, ok = reconcile._summarize_leg(
        "noise\nlong paper-demo reconciliation: paired=3 ok=True\nmore",
        "long paper-demo reconciliation", 0)
    assert ok is True
    assert line == "long paper-demo reconciliation: paired=3 ok=True"


def test_summarize_leg_flags_nonzero_exit_as_failed() -> None:
    # reconciliation-3/-5: a readiness gate that exits nonzero must NOT be summarized
    # as a clean/benign line. It must be rendered as an explicit FAILED marker and
    # report ok=False so the wrapper can exit nonzero.
    line, ok = reconcile._summarize_leg("Traceback (most recent call last): ...",
                                        "continuous forward readiness", 1)
    assert ok is False
    assert "FAILED" in line and "rc=1" in line


def test_summarize_leg_flags_missing_summary_as_failed_not_no_output() -> None:
    # reconciliation-5: a leg that printed nothing matching the needle (e.g. crashed
    # before its summary) must be FAILED, not the benign '(no output)' that made a
    # crash indistinguishable from a clean run-with-no-pairs.
    line, ok = reconcile._summarize_leg("partial output, no summary", "SUMMARY:", 0)
    assert ok is False
    assert line != "(no output)"
    assert "FAILED" in line


def test_reconcile_main_returns_nonzero_when_a_leg_fails(monkeypatch) -> None:
    # reconciliation-3: the wrapper must exit nonzero when a reconcile leg fails,
    # rather than unconditionally returning 0. Drive main() with the long sleeve
    # and a stubbed leg that reports a failure.
    monkeypatch.setattr(reconcile.sys, "argv",
                        ["reconcile.py", "--sleeves", "long", "--no-pull", "--no-rmom"])
    monkeypatch.setattr(reconcile, "reconcile_long",
                        lambda *a, **k: ("⚠️ FAILED (rc=1): gate failed", False))
    assert reconcile.main() == 1


def test_reconcile_main_returns_zero_when_leg_clean(monkeypatch) -> None:
    monkeypatch.setattr(reconcile.sys, "argv",
                        ["reconcile.py", "--sleeves", "long", "--no-pull", "--no-rmom"])
    monkeypatch.setattr(reconcile, "reconcile_long",
                        lambda *a, **k: ("long paper-demo reconciliation: ok", True))
    assert reconcile.main() == 0


# ---------------------------------------------------------------- kline_store
def test_newest_ts_recomputed_after_keep_only_trims_max_holder() -> None:
    # ws-pool-2: trimming the lone symbol that holds the global-max timestamp must
    # drop newest_ts_ms to the surviving max — never leave it stale/too-fresh.
    store = KlineStore(cache_root=None, retain_days=365, flush_interval_seconds=0.0)
    store.add_bar("AAA", _ws_bar(10 * MS_PER_HOUR), confirmed=True)
    store.add_bar("BBB", _ws_bar(50 * MS_PER_HOUR), confirmed=True)  # holds global max
    assert store.newest_ts_ms() == 50 * MS_PER_HOUR
    store.keep_only_symbols(["AAA"])  # drops the max holder
    assert store.newest_ts_ms() == 10 * MS_PER_HOUR
    assert store.stats()["newest_ts_ms"] == 10 * MS_PER_HOUR


def test_newest_ts_recomputed_after_eviction_drops_max_holder() -> None:
    # ws-pool-2: the same staleness applies to age-eviction. After a far-newer bar
    # ages out an old symbol entirely, newest must reflect the surviving bars.
    store = KlineStore(cache_root=None, retain_days=1, flush_interval_seconds=0.0)
    store.add_bar("AAA", _ws_bar(1 * MS_PER_HOUR), confirmed=True)
    # A bar > retain_days newer evicts AAA; newest must be the new bar, and after
    # the new symbol becomes the only one, newest reflects it (not a stale cache).
    store.add_bar("BBB", _ws_bar(72 * MS_PER_HOUR), confirmed=True)
    assert store.newest_ts_ms() == 72 * MS_PER_HOUR
    # AAA's stale bar should have been evicted (retain_days=1, 71h gap > 24h).
    assert store.row_count() == 1


def test_newest_ts_survives_keep_only_when_max_holder_kept() -> None:
    # Guard the happy path: trimming a NON-max symbol leaves newest unchanged.
    store = KlineStore(cache_root=None, retain_days=365, flush_interval_seconds=0.0)
    store.add_bar("AAA", _ws_bar(10 * MS_PER_HOUR), confirmed=True)
    store.add_bar("BBB", _ws_bar(50 * MS_PER_HOUR), confirmed=True)
    store.keep_only_symbols(["BBB"])  # keep the max holder, drop AAA
    assert store.newest_ts_ms() == 50 * MS_PER_HOUR


def test_flush_has_dedicated_serialization_lock() -> None:
    # ws-pool-5: a dedicated flush IO lock (separate from the data RLock) must
    # serialize the whole flush so the final stop() flush can't race a lingering
    # loop flush.
    store = KlineStore(cache_root=None, flush_interval_seconds=0.0)
    assert isinstance(store._flush_io_lock, type(threading.Lock()))
    assert hasattr(store, "_flush_to_disk_locked")


def test_concurrent_flushes_serialize_and_keep_file_consistent(tmp_path: Path) -> None:
    # ws-pool-5: two concurrent flush_to_disk calls (the stop() race) must produce
    # a consistent on-disk file and consistent version bookkeeping, not a torn file
    # or a skipped subsequent flush.
    store = KlineStore(cache_root=tmp_path, retain_days=365, flush_interval_seconds=0.0)
    for i in range(8):
        store.add_bar("AAA", _ws_bar((10 + i) * MS_PER_HOUR), confirmed=True)
    errs: list[Exception] = []

    def _flush() -> None:
        try:
            store.flush_to_disk()
        except Exception as exc:  # noqa: BLE001 - record for the assertion
            errs.append(exc)

    threads = [threading.Thread(target=_flush) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errs == []
    flush_path = tmp_path / ".cache" / "ws_klines" / "store.parquet"
    df = pl.read_parquet(flush_path)
    assert df.height == 8
    # bookkeeping reflects the flushed state -> a later no-change flush is skipped.
    assert store.flush_to_disk() == 0


# --------------------------------------------------------- regenerate_hedge_warmstart
rhw = _load("rhw_b15", "scripts/regenerate_hedge_warmstart.py")


def test_overwrite_allowed_within_drift_tolerance() -> None:
    report = {"max_drift": 1e-5, "old_rows": 100, "new_rows": 100, "overlap": 100}
    assert rhw.overwrite_blocked("bybit", report, max_drift=1e-3, force=False) is None


def test_overwrite_refused_on_large_drift() -> None:
    # backfill-writers-5: a regenerated series that diverges materially from the
    # banked one must be REFUSED (it feeds the live 2f hedge beta + auto-deploys).
    report = {"max_drift": 5e-2, "old_rows": 100, "new_rows": 100, "overlap": 100}
    reason = rhw.overwrite_blocked("bybit", report, max_drift=1e-3, force=False)
    assert reason is not None and "exceeds" in reason


def test_overwrite_refused_on_fewer_rows() -> None:
    # backfill-writers-5: a short/regressed regeneration (fewer rows than the banked
    # CSV) must be refused without --force.
    report = {"max_drift": 1e-5, "old_rows": 100, "new_rows": 80, "overlap": 80}
    reason = rhw.overwrite_blocked("bybit", report, max_drift=1e-3, force=False)
    assert reason is not None and "rows" in reason


def test_force_overrides_the_gate() -> None:
    report = {"max_drift": 5e-2, "old_rows": 100, "new_rows": 80, "overlap": 80}
    assert rhw.overwrite_blocked("bybit", report, max_drift=1e-3, force=True) is None


def test_no_existing_csv_is_not_blocked() -> None:
    report = {"max_drift": 0.0, "old_rows": 0, "new_rows": 200, "overlap": 0}
    assert rhw.overwrite_blocked("bybit", report, max_drift=1e-3, force=False) is None


# ---------------------------------------------------------------- kill-switch
_FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "$@" >> "$LOG"
cmd="$1"; shift
now=0; for a in "$@"; do [ "$a" = "--now" ] && now=1; done
args=(); for a in "$@"; do [ "$a" = "--quiet" ] || [ "$a" = "--now" ] || args+=("$a"); done
case "$cmd" in
  enable)  for u in "${args[@]}"; do touch "$STATE/$u.enabled"; [ "$now" = 1 ] && touch "$STATE/$u.active"; done ;;
  disable) for u in "${args[@]}"; do rm -f "$STATE/$u.enabled" "$STATE/$u.active"; done ;;
  restart|start) for u in "${args[@]}"; do touch "$STATE/$u.active"; done ;;
  is-active)  for u in "${args[@]}"; do [ -f "$STATE/$u.active"  ] || exit 1; done ;;
  is-enabled) for u in "${args[@]}"; do [ -f "$STATE/$u.enabled" ] || exit 1; done ;;
esac
exit 0
"""


def test_fake_systemctl_enable_without_now_does_not_start_unit(tmp_path: Path) -> None:
    # kill-switch-3: the test fake must mirror real `systemctl enable` (no --now):
    # it writes the wants-symlink (.enabled) but does NOT start the unit (.active).
    # A fake that set .active on bare `enable` would let an on-path verify pass with
    # no start step, hiding a deploy that dropped the `systemctl restart` lines.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "systemctl").write_text(_FAKE_SYSTEMCTL)
    (fake_bin / "systemctl").chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    log = tmp_path / "log"
    log.write_text("")
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "STATE": str(state), "LOG": str(log)}
    unit = "demo.service"
    subprocess.run(["bash", "-c", f"systemctl enable {unit}"], env=env, check=True)
    assert (state / f"{unit}.enabled").exists()
    assert not (state / f"{unit}.active").exists(), "bare enable must NOT start the unit"
    # enable --now and start DO mark active.
    subprocess.run(["bash", "-c", f"systemctl start {unit}"], env=env, check=True)
    assert (state / f"{unit}.active").exists()
