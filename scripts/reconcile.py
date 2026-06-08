#!/usr/bin/env python3
"""One-command, self-provisioning demo-forward reconciliation for the promoted sleeves.

This is the zero-friction reconcile: one command pulls the live ledgers, AUTO-
provisions the research data it needs (PIT manifest + recent klines, plus the
residual-momentum panel when continuous diagnostics are selected), runs a
MINIMAL-window backtest (only as far back as the forward ledger needs — not a
fixed 150-day slab), and reconciles each promoted sleeve:

    SHORT  (event/daily) : backtest <-> paper <-> demo  (+ Bybit on request)
    LONG   (v11a)        : paper <-> demo

CONTINUOUS (fade) is no longer promoted/deployed (de-promoted 2026-06-05, look-ahead
invalidated; sleeve OFF). It is NOT reconciled by default; pass `--sleeves continuous`
for diagnostics only (paper <-> demo + signal-consistency vs the engine).

Pipeline (each step maps to an opt-out flag):
    1. pull        — rsync the live demo+paper ledgers for every selected sleeve
    2. manifest    — refresh archive_trade_manifest (PIT membership)            [--no-manifest]
    3. kline-fill  — auto-download the recent klines the manifest now covers but [--no-kline-fill]
                     the local root is missing (the old "think for hours" gap)
    4. rmom        — auto-recompute residual_momentum when continuous selected  [--no-rmom]
    5. coverage    — print the PIT coverage table; refuse a stale strict backtest
    6. backtest    — promoted profile over the MINIMAL forward window            [--full-window]
    7. reconcile   — per-sleeve paper/demo (+ backtest, + signal check)          [--sleeves]
    8. summary     — one consolidated headline across selected sleeves

Safe by default: read-only against the VPS, demo only, never real money.

    bash scripts/reconcile.sh                       # promoted sleeves (short + long), fully auto
    bash scripts/reconcile.sh --sleeves continuous  # continuous diagnostics only
    bash scripts/reconcile.sh --sleeves short        # just the short sleeve
    bash scripts/reconcile.sh --dry-run              # print every command, run nothing
    bash scripts/reconcile.sh --full-window          # 150-day backtest (old behaviour)
    bash scripts/reconcile.sh --with-bybit           # also reconcile demo<->Bybit
    bash scripts/reconcile.sh --help                 # all options

See docs/pit_gate.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration import pit_coverage as pc  # noqa: E402


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (without overriding
    anything already set). The live daemons get these via systemd EnvironmentFile;
    a plain CLI run doesn't, so the demo<->Bybit leg can't authenticate otherwise.
    Existing env wins, so DEMO/REAL_MONEY exported in the shell still take priority.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(REPO / ".env")

# ----------------------------------------------------------------------------- defaults
VPS_HOST = "root@116.202.15.128"
VPS_BASE = "/opt/liquidity-migration/data"

DEFAULT_BYBIT_ROOT = "~/SHARED_DATA/bybit_full_pit"
DEFAULT_CONFIG = "configs/volume_alpha.default.yaml"

# Minimal backtest warm-up. The backtest<->paper reconcile auto-windows the
# COMPARISON to the paper ledger's first signal, so warm-up trades never produce
# false "backtest-only" rows. The strategy's deepest KLINE lookback is 30d
# features + 5d cooldown + 3d hold = ~38d; the 300d age gate is MANIFEST-derived
# (volume_events_features: first_manifest_date) so it needs ZERO extra klines.
# 45d is exact with margin. --full-window restores the old conservative slab.
MINIMAL_WARMUP_DAYS = 45
FULL_WARMUP_DAYS = 150
# rmom factor panel needs a few months of history for the 6-factor fit + 7d momentum.
RMOM_WARMUP_DAYS = 150

# Per-sleeve ledger layout. Each of demo/paper = (remote_subdir, local_dir, datasets).
# extra_files are pulled from the DEMO root (rmom panel + the WS kline store the
# continuous signal-check replays).
SLEEVES: dict[str, dict[str, object]] = {
    "short": {
        "label": "SHORT (event/daily)",
        "demo": ("bybit-demo-event", "data/bybit-demo-event", ("event_demo_trades", "event_demo_orders")),
        "paper": ("bybit-paper-event", "data/bybit-paper-event", ("event_demo_trades", "event_demo_orders")),
        "extra_files": (),
    },
    "long": {
        "label": "LONG (v11a)",
        "demo": ("bybit-long-demo-event", "data/bybit-long-demo-event",
                 ("long_native_demo_trades", "long_native_demo_orders", "long_native_demo_cycles")),
        "paper": ("bybit-long-paper-event", "data/bybit-long-paper-event",
                  ("long_native_paper_trades", "long_native_paper_orders", "long_native_paper_cycles")),
        "extra_files": (),
    },
    "continuous": {
        "label": "CONTINUOUS (fade)",
        "demo": ("bybit-continuous-demo-event", "data/bybit-continuous-demo-event",
                 ("continuous_fade_demo_trades", "continuous_fade_demo_orders", "continuous_fade_demo_cycles")),
        "paper": ("bybit-continuous-paper-event", "data/bybit-continuous-paper-event",
                  ("continuous_fade_paper_trades", "continuous_fade_paper_orders", "continuous_fade_paper_cycles")),
        "extra_files": ("residual_momentum.parquet", ".cache/ws_klines/store.parquet"),
    },
}
ALL_SLEEVES = tuple(SLEEVES.keys())


# ----------------------------------------------------------------------------- helpers
class Step:
    """Pretty, predictable step banners + command echo."""

    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.n = 0

    def banner(self, title: str) -> None:
        self.n += 1
        print(f"\n{'=' * 72}\n[{self.n}] {title}\n{'=' * 72}", flush=True)

    def run(self, cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> int:
        printable = " ".join(_quote(c) for c in cmd)
        print(f"$ {printable}", flush=True)
        if self.dry_run:
            return 0
        proc = subprocess.run(cmd, cwd=str(cwd or REPO))
        if check and proc.returncode != 0:
            raise SystemExit(f"\n❌ command failed (exit {proc.returncode}): {printable}")
        return proc.returncode

    def run_capture(self, cmd: list[str], *, cwd: Path | None = None, check: bool = False) -> tuple[int, str]:
        """Run a command, echo it, stream+capture stdout. Used for the per-sleeve
        reconcile legs so the unified headline can quote each one-line summary."""
        printable = " ".join(_quote(c) for c in cmd)
        print(f"$ {printable}", flush=True)
        if self.dry_run:
            return 0, ""
        proc = subprocess.run(cmd, cwd=str(cwd or REPO), capture_output=True, text=True)
        out = (proc.stdout or "") + (proc.stderr or "")
        if out.strip():
            print(out.rstrip(), flush=True)
        if check and proc.returncode != 0:
            raise SystemExit(f"\n❌ command failed (exit {proc.returncode}): {printable}")
        return proc.returncode, out


def _quote(s: str) -> str:
    return f'"{s}"' if (" " in s or "*" in s) else s


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _py() -> str:
    venv = REPO / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def _cli(*args: str) -> list[str]:
    return [_py(), "-m", "liquidity_migration", *args]


def _script(name: str, *args: str) -> list[str]:
    return [_py(), str(REPO / "scripts" / name), *args]


def _have_rsync() -> bool:
    return shutil.which("rsync") is not None


def _read_signal_days(root: Path, dataset: str) -> list[int]:
    """Return signal_ts_ms values from a sleeve trade ledger (empty list if absent)."""
    try:
        from liquidity_migration.storage import read_dataset
        df = read_dataset(root, dataset)
    except Exception:
        return []
    if df.is_empty() or "signal_ts_ms" not in df.columns:
        return []
    return [int(v) for v in df["signal_ts_ms"].drop_nulls().to_list()]


# ----------------------------------------------------------------------------- steps
def pull_sleeve(step: Step, host: str, sleeve: str) -> None:
    spec = SLEEVES[sleeve]
    step.banner(f"Pull {spec['label']} demo+paper ledgers from {host}")
    if not _have_rsync():
        print("⚠️  rsync not found — skipping pull; using local ledgers as-is.")
        return
    for role in ("demo", "paper"):
        remote_sub, local, datasets = spec[role]  # type: ignore[misc]
        for ds in datasets:
            remote = f"{host}:{VPS_BASE}/{remote_sub}/{ds}/"
            dest = REPO / local / ds
            dest.mkdir(parents=True, exist_ok=True)
            # -a archive, -z compress, -q quiet; no --delete (never clobber local-only
            # history). -q (not --info=stats0) for macOS openrsync 2.6.9 + Linux rsync 3.x.
            step.run(["rsync", "-azq", remote, f"{dest}/"], check=False)
    # extra single files (rmom panel, WS kline store) live under the demo root.
    demo_remote, demo_local, _ = spec["demo"]  # type: ignore[misc]
    for rel in spec["extra_files"]:  # type: ignore[union-attr]
        remote = f"{host}:{VPS_BASE}/{demo_remote}/{rel}"
        dest = REPO / demo_local / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        step.run(["rsync", "-azq", remote, str(dest)], check=False)


def refresh_manifest(step: Step, root: str, today: dt.date) -> None:
    step.banner(f"Refresh archive_trade_manifest (PIT membership) on {root}")
    end = (today + dt.timedelta(days=2)).isoformat()  # exclusive; v5 fill covers the tail to `today`
    step.run(_cli("--data-root", root, "archive-manifest", "--end", end))


def auto_kline_fill(step: Step, root: str, status: pc.CoverageStatus, today: dt.date) -> None:
    """Fill the recent klines the manifest now covers but the local root lacks.

    The manifest (PIT membership) is refreshed to ~today, but `klines_1h` is only
    as fresh as the last download — so a same-day reconcile would otherwise miss
    every forward trade whose signal day is newer than the local klines (exactly
    the gap that used to need a hand-run archive-download-klines-1h-api). Fill from
    the kline end (inclusive, to rebuild a possibly-partial last day) to today+1.
    """
    step.banner("Auto-fill recent klines (close the forward coverage gap)")
    target = today  # we want klines through today (the current, in-progress day)
    kline_end = status.kline_end
    if kline_end is not None and kline_end >= target:
        print(f"✅ klines_1h end {kline_end.isoformat()} already covers today ({target.isoformat()}); no fill needed.")
        return
    # Rebuild from the last present day (it may be partial) through today inclusive.
    start = (kline_end or (target - dt.timedelta(days=MINIMAL_WARMUP_DAYS))).isoformat()
    end = (target + dt.timedelta(days=1)).isoformat()  # exclusive
    print(f"klines_1h end={kline_end} < today={target}; filling {start}..{end} (manifest-gated, v5 API).")
    step.run(_cli("--data-root", root, "archive-download-klines-1h-api",
                  "--start", start, "--end", end, "--include-existing", "--workers", "8"))


def refresh_rmom(step: Step, root: str, today: dt.date) -> None:
    """Recompute residual_momentum.parquet on the research root so the continuous
    engine/signal tooling never trips over a stale panel. Bounded window keeps it
    cheap. The live continuous signal-check uses the (fresher) live-root panel;
    this keeps the *research* root usable for an engine backtest too."""
    step.banner(f"Auto-recompute residual_momentum on {root}")
    start = (today - dt.timedelta(days=RMOM_WARMUP_DAYS)).isoformat()
    end = (today + dt.timedelta(days=1)).isoformat()
    step.run(_script("precompute_residual_momentum.py", "--root", root, "--start", start, "--end", end))


def print_coverage(root: str, today: dt.date) -> pc.CoverageStatus:
    status = pc.coverage_status(root, today=today)
    print("\n" + pc.format_coverage(status))
    return status


def backtest_window(short_paper: Path, short_demo: Path, *, warmup_days: int, today: dt.date) -> tuple[str, str]:
    """[start, end) for the SHORT backtest. Start = earliest forward ledger signal
    minus warm-up (minimal & exact); fall back to today-(warmup+14) if the ledgers
    are empty. End = today+1 (exclusive) so today's signals are included."""
    sigs = _read_signal_days(short_paper, "event_demo_trades") + _read_signal_days(short_demo, "event_demo_trades")
    if sigs:
        earliest = dt.datetime.fromtimestamp(min(sigs) / 1000, dt.timezone.utc).date()
        start = earliest - dt.timedelta(days=warmup_days)
    else:
        start = today - dt.timedelta(days=warmup_days + 14)
    end = today + dt.timedelta(days=1)
    return start.isoformat(), end.isoformat()


def run_backtest(step: Step, *, root: str, config: str, start: str, end: str, diagnostic: bool) -> Path:
    span_days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
    step.banner(f"Backtest the promoted profile over the forward window ({start}..{end}, {span_days}d)")
    report_dir = REPO / "data" / "reconcile" / f"backtest_{_today().isoformat()}"
    report_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "--config", config, "--data-root", root, "volume-events",
        "--start", start, "--end", end,
        "--report-dir", str(report_dir),
        # Per-trade PIT membership (the trading-day fix) still applies; the
        # universe-completeness gate is irrelevant to a recent-window reconcile.
        "--allow-partial-pit",
    ]
    if diagnostic:
        args += ["--pit-membership", "current-universe"]
    step.run(_cli(*args))
    return report_dir


def find_backtest_csv(report_dir: Path) -> Path | None:
    hits = sorted(report_dir.rglob("volume_event_best_trades.csv"))
    return hits[-1] if hits else None


def reconcile_short(step: Step, *, paper: str, demo: str, csv: Path | None, with_bybit: bool, today: dt.date) -> str:
    step.banner("Reconcile SHORT: backtest <-> paper <-> demo" + (" <-> Bybit" if with_bybit else ""))
    out = REPO / "data" / "reconcile" / f"report_{today.isoformat()}"
    out.mkdir(parents=True, exist_ok=True)
    args = ["reconcile-all", "--paper-data-root", paper, "--demo-data-root", demo, "--output-dir", str(out)]
    if csv is not None:
        args += ["--backtest-trades-csv", str(csv)]
    else:
        print("⚠️  no backtest CSV — running paper<->demo(<->Bybit) only.")
    if not with_bybit:
        args += ["--skip-bybit"]
    step.run(_cli(*args))
    # Pull the two headline lines out of the combined report for the unified summary.
    combined = out / "full_reconciliation.md"
    lines = []
    if combined.exists():
        for ln in combined.read_text(encoding="utf-8").splitlines():
            s = ln.strip()
            if s.startswith("- **backtest↔paper**") or s.startswith("- **paper↔demo**") or s.startswith("- **demo↔Bybit**"):
                lines.append(s.lstrip("- "))
    return " | ".join(lines) if lines else "(see report)"


def reconcile_long(step: Step, *, paper: str, demo: str) -> str:
    step.banner("Reconcile LONG: paper <-> demo")
    _, out = step.run_capture(_cli("reconcile-long-paper-demo", "--paper-data-root", paper, "--demo-data-root", demo))
    return _first_summary_line(out, "long paper-demo reconciliation")


def reconcile_continuous(step: Step, *, paper: str, demo: str) -> tuple[str, str]:
    step.banner("Reconcile CONTINUOUS: paper-readiness + signal-consistency")
    _, out = step.run_capture(
        _cli("continuous-forward-readiness", "--paper-data-root", paper, "--demo-data-root", demo, "--paper-only")
    )
    pd_summary = _first_summary_line(out, "continuous forward readiness")
    # Signal-consistency: are the no-order paper entries genuine engine D9 picks?
    _, sig = step.run_capture(_script("continuous_demo_signal_check.py", "--root", paper))
    sig_summary = _first_summary_line(sig, "SUMMARY:")
    return pd_summary, sig_summary


def _first_summary_line(out: str, needle: str) -> str:
    for ln in out.splitlines():
        if needle in ln:
            return ln.strip()
    return "(no output)"


# ----------------------------------------------------------------------------- main
def main() -> int:
    p = argparse.ArgumentParser(
        description="One-command self-provisioning reconciliation for promoted sleeves.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--sleeves", default="short,long",
                   help="Comma list of sleeves to reconcile (short,long); add 'continuous' "
                        "explicitly for diagnostics (de-promoted, OFF).")
    p.add_argument("--bybit-root", default=DEFAULT_BYBIT_ROOT, help="Research root for the backtest + provisioning.")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="Strategy config (the promoted profile).")
    p.add_argument("--vps", default=VPS_HOST, help="VPS ssh target for the ledger pull.")
    p.add_argument("--no-pull", action="store_true", help="Skip the VPS ledger rsync; use local ledgers.")
    p.add_argument("--no-manifest", action="store_true", help="Skip the archive-manifest refresh.")
    p.add_argument("--no-kline-fill", action="store_true", help="Skip the automatic recent-kline download.")
    p.add_argument("--no-rmom", action="store_true", help="Skip the automatic residual_momentum recompute.")
    p.add_argument("--no-backtest", action="store_true", help="Skip the SHORT backtest; reconcile ledgers only.")
    p.add_argument("--full-window", action="store_true",
                   help=f"Backtest a fixed {FULL_WARMUP_DAYS}-day window instead of the minimal forward window.")
    p.add_argument("--warmup-days", type=int, default=None,
                   help=f"Override the backtest warm-up (default {MINIMAL_WARMUP_DAYS}; --full-window uses {FULL_WARMUP_DAYS}).")
    p.add_argument("--diagnostic", action="store_true",
                   help="Use current-universe membership for the backtest (labeled biased; same-day check).")
    p.add_argument("--with-bybit", action="store_true", help="Also reconcile demo<->Bybit (needs API creds).")
    p.add_argument("--force", action="store_true", help="Run the backtest even if the manifest is stale.")
    p.add_argument("--dry-run", action="store_true", help="Print every command without running anything.")
    args = p.parse_args()

    sleeves = [s.strip() for s in args.sleeves.split(",") if s.strip()]
    bad = [s for s in sleeves if s not in SLEEVES]
    if bad:
        raise SystemExit(f"unknown sleeve(s) {bad}; valid: {', '.join(ALL_SLEEVES)}")
    today = _today()
    root = args.bybit_root
    step = Step(args.dry_run)
    warmup = args.warmup_days if args.warmup_days is not None else (FULL_WARMUP_DAYS if args.full_window else MINIMAL_WARMUP_DAYS)

    print(f"liquidity-migration reconcile — {today.isoformat()} (UTC)")
    print(f"  sleeves       : {', '.join(sleeves)}")
    print(f"  research root : {root}")
    print(f"  backtest warm : {warmup}d {'(full window)' if args.full_window else '(minimal)'}")

    # 1. Pull every selected sleeve's ledgers.
    if not args.no_pull:
        for s in sleeves:
            pull_sleeve(step, args.vps, s)

    # 2-5. Provision research data (only the SHORT backtest needs the research root).
    short_active = "short" in sleeves and not args.no_backtest
    csv: Path | None = None
    if short_active:
        if not args.no_manifest:
            refresh_manifest(step, root, today)
        if not args.no_kline_fill:
            status = print_coverage(root, today)
            auto_kline_fill(step, root, status, today)
        if "continuous" in sleeves and not args.no_rmom:
            refresh_rmom(step, root, today)
        step.banner("PIT coverage check")
        status = print_coverage(root, today)
        if status.is_stale and not (args.diagnostic or args.force or args.dry_run):
            raise SystemExit(
                "\n❌ archive manifest is stale — recent signals would hard-reject with "
                "pit_membership_fail.\n   Re-run with the manifest refresh enabled, or pass "
                "--diagnostic (current-universe, biased) / --force to proceed anyway."
            )
        s_paper = REPO / SLEEVES["short"]["paper"][1]  # type: ignore[index]
        s_demo = REPO / SLEEVES["short"]["demo"][1]  # type: ignore[index]
        start, end = backtest_window(s_paper, s_demo, warmup_days=warmup, today=today)
        report_dir = run_backtest(step, root=root, config=args.config, start=start, end=end, diagnostic=args.diagnostic)
        csv = find_backtest_csv(report_dir)
        if csv is None and not args.dry_run:
            print(f"⚠️  no volume_event_best_trades.csv under {report_dir}; reconciling without the backtest leg.")

    # 7. Per-sleeve reconcile. (The continuous signal-check uses the pulled
    # live-root rmom panel, so a continuous-only run needs no research provisioning.)
    summary: dict[str, str] = {}
    if "short" in sleeves:
        sp = SLEEVES["short"]["paper"][1]  # type: ignore[index]
        sd = SLEEVES["short"]["demo"][1]  # type: ignore[index]
        if args.no_backtest:
            csv = None
        summary["short"] = reconcile_short(step, paper=sp, demo=sd, csv=csv, with_bybit=args.with_bybit, today=today)
    if "long" in sleeves:
        lp = SLEEVES["long"]["paper"][1]  # type: ignore[index]
        ld = SLEEVES["long"]["demo"][1]  # type: ignore[index]
        summary["long"] = reconcile_long(step, paper=lp, demo=ld)
    if "continuous" in sleeves:
        cp = SLEEVES["continuous"]["paper"][1]  # type: ignore[index]
        cd = SLEEVES["continuous"]["demo"][1]  # type: ignore[index]
        pd_sum, sig_sum = reconcile_continuous(step, paper=cp, demo=cd)
        summary["continuous"] = f"{pd_sum}  ||  signal: {sig_sum}"

    # 8. Unified headline.
    if not args.dry_run:
        print(f"\n{'=' * 72}\nRECONCILIATION SUMMARY — SELECTED SLEEVES\n{'=' * 72}")
        for s in sleeves:
            print(f"\n## {SLEEVES[s]['label']}")
            print(f"  {summary.get(s, '(skipped)')}")
        print(f"\nReports under: {REPO / 'data' / 'reconcile'}")
    print("\n✅ done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
