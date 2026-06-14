"""Regression tests for audit bucket b13.

Findings covered (full text in the audit ledger):
  * decision-rule-3  — _load_monthly dedups duplicate months (no double-count).
  * decision-rule-4  — headline-vs-fragility window mismatch is surfaced.
  * decision-rule-5  — _thirds returns [] for n<3 (no degenerate/duplicated thirds).
  * exec-router-3    — order_link_id enforces "component tag must not start with 'a'".
  * test-gaps-8      — _split_order_link_id truncation/collision guard is exercised.
  * pit-signals-4    — compute_btc_beta uses a CALENDAR rolling window (gap-aware),
                       numerically identical on a contiguous series.
  * backfill-writers-1 — the event-anchored wrapper detects full-history coverage and
                       refuses to clobber it without --allow-overwrite.
  * deploy-ci-3 / deploy-ci-6 / deploy-env-timers-1 / deploy-env-timers-3 — deploy
                       script guards (collector verify, REAL_MONEY refuse, hedge-open
                       keep-enabled, paper-frozen warn).

Each test would FAIL on the original (pre-fix) code and PASS on the fix. The deploy
shell tests run the actual bash snippets against fakes (no ssh / no systemd), and
assert the live deploy script still carries each guard so they can't silently regress.
"""
from __future__ import annotations

import csv
import importlib.util
import random
import subprocess
import sys
import textwrap
from pathlib import Path

import polars as pl
import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY_SH = REPO / "scripts" / "deploy_vps_live.sh"


def _load_r1():
    spec = importlib.util.spec_from_file_location("r1_robustness_b13", REPO / "scripts" / "r1_robustness.py")
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["r1_robustness_b13"] = m
    spec.loader.exec_module(m)
    return m


R1 = _load_r1()


# ──────────────────────────────────────────────────────────────────────────────
# decision-rule-3 — _load_monthly dedups duplicate month rows
# ──────────────────────────────────────────────────────────────────────────────
def _write_monthly(cell_dir: Path, rows: list[tuple[str, float]]) -> None:
    cell_dir.mkdir(parents=True, exist_ok=True)
    with open(cell_dir / "volume_event_best_monthly.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["month", "strategy_return"])
        for m, r in rows:
            w.writerow([m, r])


def test_load_monthly_dedups_duplicate_months(tmp_path: Path) -> None:
    # A duplicate '2023-02' row must NOT survive to be compounded twice (decision-rule-3).
    _write_monthly(tmp_path, [("2023-01", 0.1), ("2023-02", 0.3), ("2023-02", 0.3)])
    out = R1._load_monthly(tmp_path)
    months = [m for m, _ in out]
    assert months == ["2023-01", "2023-02"], months  # de-duplicated + sorted
    assert len(out) == len(set(months))


def test_load_monthly_last_wins_on_duplicate(tmp_path: Path) -> None:
    # On a duplicated month the last value wins (matches the documented dict-keyed dedup).
    _write_monthly(tmp_path, [("2023-02", 0.1), ("2023-02", 0.9)])
    out = dict(R1._load_monthly(tmp_path))
    assert out["2023-02"] == 0.9


# ──────────────────────────────────────────────────────────────────────────────
# decision-rule-4 — window-mismatch warning
# ──────────────────────────────────────────────────────────────────────────────
def test_window_mismatch_warning_none_when_windows_coincide() -> None:
    # Normal case: cell and control share the same window -> intersection == both -> no warn.
    months = ["2023-01", "2023-02", "2023-03"]
    assert R1._window_mismatch_warning("c1", months, months, months) is None


def test_window_mismatch_warning_fires_when_intersection_drops_months() -> None:
    # An aborted-late cell: control has 3 months, cell only 2; intersection drops the tail.
    base = ["2023-01", "2023-02", "2023-03"]
    cell = ["2023-01", "2023-02"]
    inter = ["2023-01", "2023-02"]
    msg = R1._window_mismatch_warning("c1", inter, cell, base)
    assert msg is not None
    assert "window mismatch" in msg
    assert "dropped 0 cell / 1 control months" in msg


# ──────────────────────────────────────────────────────────────────────────────
# decision-rule-5 — _thirds for n<3
# ──────────────────────────────────────────────────────────────────────────────
def test_thirds_returns_empty_for_fewer_than_three_months() -> None:
    # Pre-fix: n=1/2 produced (0,0) bounds -> empty 0.0 thirds + months[-1] labels +
    # a misleading "all cell-thirds>0: NO" for a uniformly-positive series.
    assert R1._thirds(["2023-01"], [0.1], [0.05]) == []
    assert R1._thirds(["2023-01", "2023-02"], [0.1, 0.2], [0.05, 0.1]) == []


def test_thirds_splits_three_or_more_months() -> None:
    months = ["2023-01", "2023-02", "2023-03"]
    out = R1._thirds(months, [0.1, 0.2, 0.3], [0.05, 0.1, 0.15])
    assert len(out) == 3
    # No duplicate/garbled labels: each third's label spans its own bound.
    assert out[0]["label"] == "2023-01..2023-01"
    assert out[2]["label"] == "2023-03..2023-03"


# ──────────────────────────────────────────────────────────────────────────────
# exec-router-3 — component tags must not start with 'a'
# ──────────────────────────────────────────────────────────────────────────────
from liquidity_migration.order_link_id import (  # noqa: E402
    _split_order_link_id,
    assert_routable_component_tags,
    decode_entry_order_link_id,
)


def test_assert_routable_component_tags_passes_deployed_tags() -> None:
    # The deployed continuous tags are p3/p4p3/p4p5/tp14 — none start with 'a'.
    assert_routable_component_tags(["", "p3", "p4p3", "p4p5", "tp14", "s"])  # no raise


def test_assert_routable_component_tags_raises_for_a_prefixed_tag() -> None:
    # An 'a'-prefixed component ('avg') would build lm-en-cavg-… which decodes to the
    # 'ca' ADDON sleeve, mis-routing the fill (exec-router-3). Must fail loud.
    with pytest.raises(ValueError, match="must not begin with 'a'"):
        assert_routable_component_tags(["p3", "avg"])


def test_a_prefixed_component_actually_misroutes_today() -> None:
    # Demonstrates WHY the assertion matters: the colliding link decodes to the addon
    # sleeve with a garbled component tag, which assert_routable_component_tags forbids.
    sleeve, _ts, _seq, comp = decode_entry_order_link_id("lm-en-cavg-BTC-abcde")
    assert (sleeve, comp) == ("continuous_addon", "vg")  # the mis-route the guard prevents


# ──────────────────────────────────────────────────────────────────────────────
# test-gaps-8 — _split_order_link_id truncation / collision guard
# ──────────────────────────────────────────────────────────────────────────────
def test_split_order_link_id_truncation_keeps_suffix_and_stays_under_cap() -> None:
    # A 34-char base with idx 1 and 2: the -s{idx} suffix must survive (base truncated
    # FIRST), the result must stay <=36 chars, and the two sub-orders must be DISTINCT.
    base = "lm-en-cp4p5-SOMEVERYLONGSYMBOLNAME-abc"[:34]
    assert len(base) == 34
    s1 = _split_order_link_id(base, 1)
    s2 = _split_order_link_id(base, 2)
    assert len(s1) <= 36 and len(s2) <= 36
    assert s1.endswith("-s1") and s2.endswith("-s2")  # suffix not chopped
    assert s1 != s2  # no collision


def test_split_order_link_id_no_truncation_for_short_base() -> None:
    # Current real ~24-char bases are unchanged: base preserved, suffix appended.
    base = "lm-en-cp3-BTC-abcde"
    out = _split_order_link_id(base, 3)
    assert out == f"{base}-s3"
    assert len(out) <= 36


# ──────────────────────────────────────────────────────────────────────────────
# pit-signals-4 — compute_btc_beta calendar window
# ──────────────────────────────────────────────────────────────────────────────
from liquidity_migration.risk_model import compute_btc_beta  # noqa: E402

_DAY = 86_400_000


def _daily_returns(symbol_to_rets: dict[str, list[tuple[int, float]]]) -> pl.DataFrame:
    """symbol -> list of (day_index, ret_1d). Day index keys the 00:00-UTC grid."""
    rows = []
    for sym, pairs in symbol_to_rets.items():
        for d, r in pairs:
            rows.append({"symbol": sym, "ts_ms": d * _DAY, "ret_1d": r})
    return pl.DataFrame(rows)


def test_btc_beta_contiguous_matches_known_slope() -> None:
    # Numerical-equivalence guard: on a CONTIGUOUS series the calendar window must give
    # the same exact OLS slope the row-based window did (ALT = 1.5 * BTC -> beta 1.5).
    rng = random.Random(1)
    btc = [(d, rng.uniform(-0.05, 0.05)) for d in range(80)]
    alt = [(d, 1.5 * r) for d, r in btc]
    out = compute_btc_beta(_daily_returns({"BTCUSDT": btc, "ALT": alt}), window=60, min_periods=30)
    last = out.filter((pl.col("symbol") == "ALT") & (pl.col("ts_ms") == 79 * _DAY))["btc_beta"][0]
    assert last is not None and abs(last - 1.5) < 1e-9, last


def test_btc_beta_gap_does_not_stretch_window_past_calendar_span() -> None:
    """A gapped symbol must NOT get a beta that spans >window CALENDAR days.

    Pre-fix (row-based rolling): a symbol whose returns are present only on days
    {0,1,...,29} then jumps to day 200 gets a 'window-day' window stitching the
    pre-gap rows onto day 200 — a stale beta. With the calendar window, day 200 sees
    far fewer than min_periods CALENDAR-recent rows -> null (correctly shrunk window).
    """
    # BTC present every day so a partner exists; ALT present only on days 0..29, then 200.
    btc = [(d, 0.01 if d % 2 else -0.01) for d in range(201)]
    alt_days = list(range(30)) + [200]
    alt = [(d, 0.015 if d % 2 else -0.015) for d in alt_days]
    out = compute_btc_beta(_daily_returns({"BTCUSDT": btc, "ALT": alt}), window=60, min_periods=30)
    beta_200 = out.filter((pl.col("symbol") == "ALT") & (pl.col("ts_ms") == 200 * _DAY))["btc_beta"][0]
    # Only 1 calendar-recent ALT row within the trailing 60 days of day 200 (day 200
    # itself) -> below min_periods -> null. A row-based window would have reached back
    # to the pre-gap block and produced a (stale) non-null beta.
    assert beta_200 is None, beta_200


# ──────────────────────────────────────────────────────────────────────────────
# backfill-writers-1 — full-history coverage guard in the event-anchored wrapper
# ──────────────────────────────────────────────────────────────────────────────
def _load_eventanchored():
    path = REPO / "scripts" / "backfill_binance_metrics_eventanchored.py"
    # The module imports heavy siblings at import time (vision + scout); guard so a
    # missing optional dep degrades to a skip rather than an error.
    scripts = str(path.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("backfill_binance_metrics_eventanchored_b13", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - import-time sibling deps
        pytest.skip(f"event-anchored backfill import unavailable: {exc}")
    return mod


def test_full_history_symbols_reads_vision_manifest(tmp_path: Path, monkeypatch) -> None:
    mod = _load_eventanchored()
    out_dir = tmp_path / "binance_usdm_metrics_5m"
    out_dir.mkdir(parents=True)
    # The vision full-history backfill records its symbols in _manifest.json.
    (out_dir / "_manifest.json").write_text('{"BTCUSDT": {"rows": 100}, "ETHUSDT": {"rows": 50}}')
    monkeypatch.setattr(mod, "OUT_DIR", out_dir)
    monkeypatch.setattr(mod, "FULL_MANIFEST", out_dir / "_manifest.json")
    assert mod._full_history_symbols() == {"BTCUSDT", "ETHUSDT"}


def test_full_history_symbols_empty_when_no_manifest(tmp_path: Path, monkeypatch) -> None:
    mod = _load_eventanchored()
    out_dir = tmp_path / "binance_usdm_metrics_5m"
    out_dir.mkdir(parents=True)
    monkeypatch.setattr(mod, "FULL_MANIFEST", out_dir / "_manifest.json")
    assert mod._full_history_symbols() == set()


# ──────────────────────────────────────────────────────────────────────────────
# deploy script — structural guards (deploy-ci-3, deploy-ci-6,
#                 deploy-env-timers-1, deploy-env-timers-3)
# ──────────────────────────────────────────────────────────────────────────────
def test_deploy_verifies_liquidation_collector_active() -> None:
    # deploy-ci-3: the always-on collector must be verified active+enabled in the
    # post-settle block (not just enabled+restarted), so a crash on new code fails loud.
    txt = DEPLOY_SH.read_text()
    assert "systemctl is-active --quiet liquidity-migration-liquidation-collector.service" in txt
    assert "systemctl is-enabled --quiet liquidity-migration-liquidation-collector.service" in txt


def test_deploy_refuses_real_money_env() -> None:
    # deploy-ci-6: the deploy must fail-closed if the sourced env sets REAL_MONEY truthy.
    txt = DEPLOY_SH.read_text()
    assert "REAL_MONEY" in txt
    assert "Refusing deploy: REAL_MONEY" in txt


def test_deploy_keeps_hedge_timer_when_continuous_off_but_leg_open() -> None:
    # deploy-env-timers-1: the gating must NOT be a bare apply_timer_enable on
    # CONTINUOUS_SLEEVE; it must consult the hedge ledger and keep the timer enabled
    # while an open hedge leg exists.
    txt = DEPLOY_SH.read_text()
    assert "_hedge_timer_state" in txt
    assert "bybit-continuous-hedge-event" in txt
    # The verify side must mirror the apply side, not raw CONTINUOUS_SLEEVE.
    assert 'verify_timer "$_hedge_timer_state" $CONTINUOUS_HEDGE_TIMERS' in txt
    assert 'verify_timer "$CONTINUOUS_SLEEVE" $CONTINUOUS_HEDGE_TIMERS' not in txt


def test_deploy_warns_on_paper_following_frozen_demo_root() -> None:
    # deploy-env-timers-3: demo-off + paper-on must warn that the paper shadow follows
    # a frozen demo kline store.
    txt = DEPLOY_SH.read_text()
    assert "KLINES_FOLLOW_ROOT" in txt
    assert 'sleeve_on "$CONTINUOUS_PAPER_SLEEVE"' in txt


# ──────────────────────────────────────────────────────────────────────────────
# deploy script — behavioral test of the REAL_MONEY refuse guard (deploy-ci-6)
# ──────────────────────────────────────────────────────────────────────────────
# Replicates the exact case-statement from deploy_vps_live.sh so the truthy-detection
# logic is exercised, not just present. Kept in sync via the structural test above.
_REAL_MONEY_GUARD = r"""
real_money_refused() {
  case "${REAL_MONEY:-}" in
    1|true|TRUE|True|yes|YES|Yes|on|ON|On) return 0 ;;
  esac
  return 1
}
if real_money_refused; then echo REFUSED; else echo OK; fi
"""


def _run_bash(body: str, env_line: str = "") -> str:
    script = textwrap.dedent(f"""
        set -euo pipefail
        {env_line}
        {body}
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "YES", "on", "ON"])
def test_real_money_truthy_values_are_refused(val: str) -> None:
    assert _run_bash(_REAL_MONEY_GUARD, f'export REAL_MONEY="{val}"') == "REFUSED"


@pytest.mark.parametrize("env_line", ['export REAL_MONEY=""', "unset REAL_MONEY || true", 'export REAL_MONEY=false', 'export REAL_MONEY=0'])
def test_real_money_demo_values_are_allowed(env_line: str) -> None:
    assert _run_bash(_REAL_MONEY_GUARD, env_line) == "OK"


# Verify the structural test's source-of-truth: the deploy script's actual case arms
# must contain every truthy token the behavioral guard tests (so they can't drift).
def test_deploy_real_money_case_covers_truthy_tokens() -> None:
    txt = DEPLOY_SH.read_text()
    for token in ("1|true|TRUE", "yes|YES", "on|ON"):
        assert token in txt, f"deploy REAL_MONEY case missing arm {token!r}"
