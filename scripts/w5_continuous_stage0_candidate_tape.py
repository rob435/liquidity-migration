#!/usr/bin/env python3
"""W5 Stage 0 candidate-tape + frozen-control reconstruction harness.

Builds the W5 audit surface: per decision cycle, the FULL eligible candidate set
(selected + rejected-but-eligible, with the exact engine reason) for the frozen
continuous control, on both venues, over the common full-PIT window. Makes NO
alpha claim — it is the gate for every later W5 stage.

The candidate tape is emitted by the same decision code that selects live entries
(``continuous_events._run_trades`` candidate sink), so the selected subset
reconciles to the rebuilt component ledgers by construction. We then cross-check
that, on the overlapping window, the W5 selections equal the existing W4
``00_frozen_no_stop`` control, rebuild the frozen ensemble-hedged ledger, run the
PIT partition gate, and attach the W4-binding path-shape features + negative
controls so Stage 1 / Stage 7 can score rejected peers, not only executed trades.

Pre-registration: docs/preregistration/2026-06-14-w5-continuous-stage0-candidate-tape.md

Run:
    POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
        scripts/w5_continuous_stage0_candidate_tape.py \
        --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
        --out ~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import statistics as st
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))  # w4 helpers import rebuild_winner_base_component_ledgers

from liquidity_migration.continuous_events import (  # noqa: E402
    _daily_pnl_metrics,
    run_continuous_event_research,
)
from liquidity_migration.continuous_forward_replay import (  # noqa: E402
    FROZEN_FORWARD_CONFIG,
    build_full_ledger,
    frozen_config_hash,
)
from scripts.w4_continuous_path_shape_measure import (  # noqa: E402
    _bars,
    _dates,
    _symbol_hash_bucket,
    _value_at_or_before,
)
from scripts.w4_continuous_stop_exit_realism import (  # noqa: E402
    COMPONENTS,
    _btc_inputs,
    _component_config,
    _load_component,
    _monthly_returns,
    _pit_partition_gate,
    _write_csv,
)

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA")))
ROOTS = {"bybit": SHARED / "bybit_full_pit", "binance": SHARED / "binance_full_pit"}
SWEEP_TAG = "w5_continuous_stage0_candidate_tape_2026-06-14"
W4_CONTROL_TAG = "w4_continuous_stage1_stop_exit_2026-06-13"
PREREG = "docs/preregistration/2026-06-14-w5-continuous-stage0-candidate-tape.md"
MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000
TOL = 1e-9


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _code_hash() -> str:
    paths = [
        REPO / "scripts" / "w5_continuous_stage0_candidate_tape.py",
        REPO / "scripts" / "w4_continuous_stop_exit_realism.py",
        REPO / "scripts" / "w4_continuous_path_shape_measure.py",
        REPO / "scripts" / "rebuild_winner_base_component_ledgers.py",
        REPO / "liquidity_migration" / "continuous_events.py",
        REPO / "liquidity_migration" / "continuous_forward_replay.py",
        REPO / "liquidity_migration" / "continuous_rebalance.py",
        REPO / "liquidity_migration" / "trade_lifecycle.py",
    ]
    payload = "|".join(f"{p.relative_to(REPO)}:{_sha256_file(p)}" for p in paths)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _date_to_ms(date: str) -> int:
    return int(dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def _month_of(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m")


def _month_hash_bucket(month: str) -> float:
    raw = int(hashlib.sha256(month.encode("utf-8")).hexdigest()[:8], 16) % 1000
    return raw / 999.0


def _pre_shape_features(venue: str, symbol: str, signal_ts: int) -> dict[str, Any]:
    """Causal path-shape features at the signal bar — identical definitions to W4 Stage 3
    (``_features_for_trade`` pre-entry half), but computed for any candidate (selected or
    rejected) because none of these require a realized fill price."""
    lo_ms = signal_ts - 30 * MS_PER_HOUR
    hi_ms = signal_ts
    bars: list[tuple[int, float, float, float]] = []
    for date in _dates(lo_ms, hi_ms):
        bars.extend(_bars(venue, symbol, date))
    bars.sort(key=lambda item: item[0])
    signal_bar = _value_at_or_before(bars, signal_ts)
    close_6 = _value_at_or_before(bars, signal_ts - 6 * MS_PER_HOUR)
    close_24 = _value_at_or_before(bars, signal_ts - 24 * MS_PER_HOUR)
    signal_close = signal_bar[3] if signal_bar else None
    feats: dict[str, Any] = {
        "pre_6h_return": None,
        "pre_24h_return": None,
        "pre_6h_upper_extension": None,
        "pre_24h_realized_vol": None,
        "signal_bar_close_location": None,
    }
    if signal_close and signal_close > 0:
        if close_6 and close_6[3] > 0:
            feats["pre_6h_return"] = signal_close / close_6[3] - 1.0
        if close_24 and close_24[3] > 0:
            feats["pre_24h_return"] = signal_close / close_24[3] - 1.0
        prior6 = [bar for bar in bars if signal_ts - 6 * MS_PER_HOUR <= bar[0] <= signal_ts]
        if prior6:
            feats["pre_6h_upper_extension"] = max(bar[1] for bar in prior6) / signal_close - 1.0
        prior24 = [bar for bar in bars if signal_ts - 24 * MS_PER_HOUR <= bar[0] <= signal_ts]
        closes = [bar[3] for bar in prior24 if bar[3] > 0]
        rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(rets) >= 6:
            feats["pre_24h_realized_vol"] = st.pstdev(rets)
        high, low, close = signal_bar[1], signal_bar[2], signal_bar[3]
        if high > low:
            feats["signal_bar_close_location"] = (close - low) / (high - low)
    return feats


def _shuffled_in_fold(rows: list[dict[str, Any]]) -> None:
    """Negative control: permute the real composite within each (component, month) fold with a
    deterministic month-seeded RNG. Preserves the marginal composite distribution, destroys the
    symbol<->score link, so a 'score' that beats this is using real cross-sectional information."""
    groups: dict[tuple[str, str], list[int]] = {}
    for idx, row in enumerate(rows):
        groups.setdefault((str(row["component"]), str(row["month"])), []).append(idx)
    for (_component, month), idxs in groups.items():
        comps = [rows[i].get("composite") for i in idxs]
        seed = int(hashlib.sha256(month.encode("utf-8")).hexdigest()[:8], 16)
        perm = np.random.RandomState(seed).permutation(len(idxs))
        for slot, i in enumerate(idxs):
            rows[i]["shuffled_in_fold_composite"] = comps[perm[slot]]


def _enrich(rows: list[dict[str, Any]], venue: str, start_ms: int) -> None:
    """Attach timing fields, path-shape features, and negative controls to every candidate row."""
    feat_cache: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        sym = str(row["symbol"])
        sig = int(row["signal_ts_ms"])
        month = _month_of(sig)
        row["venue"] = venue
        row["month"] = month
        # methodology-contract timing fields
        row["decision_ts_ms"] = sig
        row["data_available_ts_ms"] = sig
        row["order_submit_ts_ms"] = sig + MS_PER_HOUR
        row["fill_window_start_ts_ms"] = sig + MS_PER_HOUR
        row["fill_window_end_ts_ms"] = row.get("entry_bar_end_ts_ms")
        row["exit_activation_ts_ms"] = row.get("entry_bar_end_ts_ms")
        row["state_initialization_ts_ms"] = start_ms
        # negative controls (no market content)
        row["symbol_hash_bucket"] = _symbol_hash_bucket(sym)
        row["month_hash_bucket"] = _month_hash_bucket(month)
        # causal path-shape features (W4-binding definitions)
        key = (sym, sig)
        feats = feat_cache.get(key)
        if feats is None:
            feats = _pre_shape_features(venue, sym, sig)
            feat_cache[key] = feats
        row.update(feats)
    _shuffled_in_fold(rows)


def _reconcile_selected(tape_rows: list[dict[str, Any]], trades: pl.DataFrame) -> dict[str, Any]:
    """Selected candidate rows must equal the rebuilt component ledger on (symbol, signal_ts)."""
    sel = {(str(r["symbol"]), int(r["signal_ts_ms"])) for r in tape_rows if r["selected"]}
    if trades.is_empty():
        trade_keys: set[tuple[str, int]] = set()
    else:
        trade_keys = {
            (str(s), int(t))
            for s, t in trades.select("symbol", "entry_signal_ts_ms").iter_rows()
        }
    return {
        "selected_rows": len(sel),
        "trade_rows": len(trade_keys),
        "matched": len(sel & trade_keys),
        "selected_only": len(sel - trade_keys),
        "trade_only": len(trade_keys - sel),
        "exact": sel == trade_keys,
    }


def _w4_overlap(root: Path, component: str, end_ms: int, w5_trades: pl.DataFrame) -> dict[str, Any]:
    """On the overlapping window, W5 component selections must equal the W4 control trades
    restricted to entry_signal_ts_ms < end_ms (a strong external faithfulness cross-check)."""
    w4_path = root / "reports" / W4_CONTROL_TAG / "00_frozen_no_stop" / component / "continuous_trades.csv"
    out: dict[str, Any] = {"w4_control_trades_csv": str(w4_path), "available": w4_path.exists()}
    if not w4_path.exists():
        return out
    w4 = pl.read_csv(w4_path).filter(pl.col("entry_signal_ts_ms") < end_ms)
    w4_keys = {(str(s), int(t)) for s, t in w4.select("symbol", "entry_signal_ts_ms").iter_rows()}
    w5_keys = (
        set()
        if w5_trades.is_empty()
        else {(str(s), int(t)) for s, t in w5_trades.select("symbol", "entry_signal_ts_ms").iter_rows()}
    )
    out.update(
        {
            "w4_overlap_trades": len(w4_keys),
            "w5_trades": len(w5_keys),
            "matched": len(w4_keys & w5_keys),
            "w4_only": len(w4_keys - w5_keys),
            "w5_only": len(w5_keys - w4_keys),
            "exact": w4_keys == w5_keys,
        }
    )
    return out


def _component_month_counts(tape_rows: list[dict[str, Any]], trades: pl.DataFrame) -> dict[str, Any]:
    """Selection counts by month must reconcile between the tape's selected rows and the ledger."""
    tape_counts: dict[str, int] = {}
    for r in tape_rows:
        if r["selected"]:
            mm = _month_of(int(r["signal_ts_ms"]))
            tape_counts[mm] = tape_counts.get(mm, 0) + 1
    ledger_counts: dict[str, int] = {}
    if not trades.is_empty():
        m = (
            trades.with_columns(
                pl.from_epoch("entry_signal_ts_ms", time_unit="ms").dt.strftime("%Y-%m").alias("month")
            )
            .group_by("month")
            .agg(pl.len().alias("n"))
        )
        ledger_counts = {str(mm): int(n) for mm, n in m.iter_rows()}
    months = sorted(set(tape_counts) | set(ledger_counts))
    mismatches = [mm for mm in months if tape_counts.get(mm, 0) != ledger_counts.get(mm, 0)]
    return {"months": len(months), "mismatched_months": mismatches, "reconciled": not mismatches}


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    end_ms = _date_to_ms(args.end)
    start_ms = _date_to_ms(args.start)
    code_hash = _code_hash()
    (out / "code_hash.txt").write_text(code_hash + "\n", encoding="utf-8")

    payload: dict[str, Any] = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preregistration": PREREG,
        "stage": "stage0_candidate_tape",
        "window": {"start": args.start, "end_exclusive": args.end},
        "out": str(out),
        "sweep_tag": SWEEP_TAG,
        "w4_control_tag": W4_CONTROL_TAG,
        "git_head": _git_head(),
        "code_hash": code_hash,
        "frozen_forward_config": FROZEN_FORWARD_CONFIG,
        "frozen_forward_config_hash": frozen_config_hash(),
        "roots": {v: str(ROOTS[v]) for v in venues},
        "run_label": "stage0_audit",
        "components": list(COMPONENTS.keys()),
        "pit_gate": {},
        "config_hashes": {},
        "venues": {},
    }

    for venue in venues:
        root = ROOTS[venue]
        if not root.is_dir():
            payload["venues"][venue] = {"error": f"missing root {root}"}
            continue
        pit = _pit_partition_gate(root, start=args.start, end=args.end)
        payload["pit_gate"][venue] = pit
        venue_block: dict[str, Any] = {"components": {}, "candidate_count": 0, "selected_count": 0}
        pieces: dict[str, Any] = {}
        all_tape_rows: list[dict[str, Any]] = []

        for component, (_artifact_root, cell) in COMPONENTS.items():
            report_dir = root / "reports" / SWEEP_TAG / component
            tape_path = report_dir / "candidate_tape.parquet"
            cfg = _component_config(component, cell, start=args.start, end=args.end, arm_overrides={})
            payload["config_hashes"].setdefault(component, cfg.config_hash())
            t0 = time.time()
            result = run_continuous_event_research(
                root, config=cfg, report_dir=report_dir, candidate_tape_path=tape_path,
            )
            comp, trades, _report = _load_component(report_dir)
            pieces[component] = comp
            tape = pl.read_parquet(tape_path) if tape_path.exists() else pl.DataFrame()
            rows = tape.to_dicts()
            for r in rows:
                r["component"] = component
            recon = _reconcile_selected(rows, trades)
            month_recon = _component_month_counts(rows, trades)
            w4 = _w4_overlap(root, component, end_ms, trades)
            n_rej = sum(1 for r in rows if not r["selected"])
            venue_block["components"][component] = {
                "cell": cell,
                "config_hash": cfg.config_hash(),
                "report_dir": str(report_dir),
                "candidate_tape": str(tape_path),
                "n_fresh_entries": result.get("n_fresh_entries"),
                "n_candidates": len(rows),
                "n_selected": int(trades.height),
                "n_rejected": n_rej,
                "funding_mode": result.get("funding_mode"),
                "elapsed_s": round(time.time() - t0, 1),
                "selected_reconcile": recon,
                "month_reconcile": month_recon,
                "w4_overlap": w4,
            }
            venue_block["candidate_count"] += len(rows)
            venue_block["selected_count"] += int(trades.height)
            all_tape_rows.extend(rows)

        # ---- enrich the full venue candidate tape (timing + features + negative controls) ----
        _enrich(all_tape_rows, venue, start_ms)
        tape_df = pl.DataFrame(all_tape_rows) if all_tape_rows else pl.DataFrame()
        if not tape_df.is_empty():
            tape_df = tape_df.sort(["component", "signal_ts_ms", "symbol"])
            tape_df.write_parquet(out / f"candidate_tape_{venue}.parquet")
            sel_df = tape_df.filter(pl.col("selected"))
            rej_df = tape_df.filter(~pl.col("selected"))
            sel_df.write_csv(out / f"selected_entries_{venue}.csv")
            rej_df.write_csv(out / f"rejected_entries_{venue}.csv")
            venue_block["rejected_count"] = int(rej_df.height)
            venue_block["reason_breakdown"] = {
                str(k): int(v)
                for k, v in (
                    tape_df.group_by("reason").agg(pl.len().alias("n")).sort("n", descending=True).iter_rows()
                )
            }
            # coverage of path-shape features on the candidate tape
            venue_block["feature_coverage"] = {
                f: int(tape_df.filter(pl.col(f).is_not_null()).height)
                for f in ("pre_6h_return", "pre_24h_return", "pre_24h_realized_vol")
            }

        # ---- rebuild the frozen ensemble-hedged control ledger + R1 monthly ----
        ensemble_block: dict[str, Any] = {"built": False}
        if pieces:
            all_days = sorted({day for comp in pieces.values() for day in comp.days})
            btc_rets, btc_funding = _btc_inputs(root, venue, all_days)
            ledger = build_full_ledger(pieces, btc_rets, btc_funding)
            metrics = _daily_pnl_metrics(ledger.select("ts_ms", "equity", "drawdown", "basket_return"))
            arm_dir = root / "reports" / SWEEP_TAG
            arm_dir.mkdir(parents=True, exist_ok=True)
            ledger.write_csv(arm_dir / "ensemble_hedged_ledger.csv")
            monthly = _monthly_returns(ledger)
            monthly.write_csv(arm_dir / "volume_event_best_monthly.csv")
            report = {
                "preregistration": PREREG,
                "run_label": "stage0_audit",
                "date_range": {"start": args.start, "end": args.end},
                "best_scenario": {
                    "total_return": metrics.get("total_return", 0.0),
                    "max_drawdown": metrics.get("max_drawdown", 0.0),
                    "sharpe_like": metrics.get("sharpe_like", 0.0),
                    "mar": metrics.get("mar"),
                    "trades": venue_block["selected_count"],
                },
                "pit_manifest": {"full_pit_universe_pass": bool(pit["full_pit_universe_pass"]), **pit},
                "data_root": str(root),
                "venue": venue,
                "code_hash": code_hash,
                "frozen_forward_config_hash": payload["frozen_forward_config_hash"],
                "ledger": str(arm_dir / "ensemble_hedged_ledger.csv"),
            }
            (arm_dir / "volume_event_research_report.json").write_text(
                json.dumps(report, indent=2, default=str), encoding="utf-8"
            )
            ensemble_block = {
                "built": True,
                "ledger": str(arm_dir / "ensemble_hedged_ledger.csv"),
                "monthly": str(arm_dir / "volume_event_best_monthly.csv"),
                "n_ledger_days": int(ledger.height),
                "n_months": int(monthly.height),
                "metrics": metrics,
            }
        venue_block["ensemble_hedged"] = ensemble_block

        # ---- per-venue baseline reconstruction CSV ----
        recon_rows = []
        for component, cdata in venue_block["components"].items():
            recon_rows.append(
                {
                    "venue": venue,
                    "component": component,
                    "n_candidates": cdata["n_candidates"],
                    "n_selected": cdata["n_selected"],
                    "n_rejected": cdata["n_rejected"],
                    "selected_exact": cdata["selected_reconcile"]["exact"],
                    "selected_only": cdata["selected_reconcile"]["selected_only"],
                    "trade_only": cdata["selected_reconcile"]["trade_only"],
                    "month_reconciled": cdata["month_reconcile"]["reconciled"],
                    "w4_overlap_exact": cdata["w4_overlap"].get("exact"),
                    "w4_only": cdata["w4_overlap"].get("w4_only"),
                    "w5_only": cdata["w4_overlap"].get("w5_only"),
                }
            )
        _write_csv(out / f"baseline_reconstruction_{venue}.csv", recon_rows)

        # ---- per-venue verdict gates ----
        comp_vals = venue_block["components"].values()
        venue_block["gates"] = {
            "root_present": True,
            "pit_pass": bool(pit["full_pit_universe_pass"]),
            "selected_reconcile_exact": all(c["selected_reconcile"]["exact"] for c in comp_vals),
            "month_reconcile": all(c["month_reconcile"]["reconciled"] for c in comp_vals),
            "w4_overlap_exact": all(
                bool(c["w4_overlap"].get("exact")) for c in comp_vals if c["w4_overlap"].get("available")
            ),
            "ensemble_built": ensemble_block["built"],
            "rejected_available": venue_block.get("rejected_count", 0) > 0,
        }
        payload["venues"][venue] = venue_block

    # ---- overall verdict (both venues, all gates) ----
    venue_blocks = [b for b in payload["venues"].values() if "gates" in b]
    overall_pass = bool(venue_blocks) and len(venue_blocks) == len(venues) and all(
        all(b["gates"].values()) for b in venue_blocks
    )
    payload["verdict"] = {
        "pass": overall_pass,
        "venues_evaluated": len(venue_blocks),
        "venues_requested": len(venues),
        "per_venue_gates": {v: payload["venues"][v].get("gates") for v in payload["venues"]},
        "interpretation": (
            "PASS — candidate tape reconstructed and reconciled on both venues; W5 Stage 1 may "
            "proceed under its own dated preregistration."
            if overall_pass
            else "FAIL — at least one gate did not pass; do not run W5 Stage 1/2/3/4/5/7/8."
        ),
    }

    (out / "pit_gate.json").write_text(json.dumps(payload["pit_gate"], indent=2, default=str), encoding="utf-8")
    (out / "config_hashes.json").write_text(
        json.dumps(payload["config_hashes"], indent=2, default=str), encoding="utf-8"
    )
    (out / "stage0_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(payload, out / "stage0_summary.md")
    return payload


def _write_markdown(payload: dict[str, Any], path: Path) -> None:
    v = payload["verdict"]
    lines = [
        "# W5 Continuous Stage 0 — Candidate Tape & Baseline Reconstruction",
        "",
        f"- Generated UTC: `{payload['generated_utc']}`",
        f"- Git HEAD: `{payload['git_head']}`",
        f"- Code hash: `{payload['code_hash']}`",
        f"- Frozen forward config hash: `{payload['frozen_forward_config_hash']}`",
        f"- Window: `{payload['window']['start']} <= signal_ts < {payload['window']['end_exclusive']}`",
        f"- Pre-registration: `{payload['preregistration']}`",
        "",
        f"## Verdict: {'PASS' if v['pass'] else 'FAIL'}",
        "",
        v["interpretation"],
        "",
        "## Per-venue gates",
        "",
        "| Venue | PIT | Selected==Ledger | Month reconcile | W4 overlap | Ensemble | Rejected avail |",
        "|---|---|---|---|---|---|---|",
    ]
    for venue, block in payload["venues"].items():
        g = block.get("gates")
        if not g:
            lines.append(f"| `{venue}` | ERROR: {block.get('error', 'n/a')} |||||| ")
            continue
        lines.append(
            f"| `{venue}` | {g['pit_pass']} | {g['selected_reconcile_exact']} | {g['month_reconcile']} | "
            f"{g['w4_overlap_exact']} | {g['ensemble_built']} | {g['rejected_available']} |"
        )
    lines.extend(["", "## Candidate tape by component", "",
                  "| Venue | Component | Candidates | Selected | Rejected | Sel==Ledger | W4 overlap exact |",
                  "|---|---|---:|---:|---:|---|---|"])
    for venue, block in payload["venues"].items():
        for component, c in block.get("components", {}).items():
            lines.append(
                f"| `{venue}` | `{component}` | {c['n_candidates']} | {c['n_selected']} | {c['n_rejected']} | "
                f"{c['selected_reconcile']['exact']} | {c['w4_overlap'].get('exact')} |"
            )
    lines.extend(["", "## Ensemble-hedged control metrics", "",
                  "| Venue | Ledger days | Months | Total return | MAR | Max DD |",
                  "|---|---:|---:|---:|---:|---:|"])
    for venue, block in payload["venues"].items():
        eb = block.get("ensemble_hedged", {})
        if eb.get("built"):
            m = eb["metrics"]
            lines.append(
                f"| `{venue}` | {eb['n_ledger_days']} | {eb['n_months']} | "
                f"{m.get('total_return', 0.0):.4f} | {m.get('mar')} | {m.get('max_drawdown', 0.0):.4f} |"
            )
    lines.extend(["", "Stage 0 makes no alpha claim. It is the reconstructability gate for W5.", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--start", default="2023-04-01")
    parser.add_argument("--end", default="2026-05-01")
    parser.add_argument("--out", default=str(SHARED / SWEEP_TAG))
    args = parser.parse_args()
    payload = run_stage(args)
    print(json.dumps({"out": payload["out"], "verdict": payload["verdict"]}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
