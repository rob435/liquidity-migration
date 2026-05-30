#!/usr/bin/env python3
"""One-command demo-forward reconciliation for the liquidity-migration SHORT sleeve.

Runs the whole backtest<->paper<->demo(<->Bybit) reconciliation pipeline that used
to be a fragile multi-step manual dance:

    1. pull      — rsync the live demo + paper trade ledgers down from the VPS
    2. manifest  — refresh the archive_trade_manifest (PIT membership) on the
                   research root so the recent tail is covered (the step
                   `download-data` silently does NOT do)
    3. coverage  — print a PIT coverage table; refuse the strict backtest if the
                   manifest cannot validate the most recent signal day
    4. backtest  — run the promoted `volume-events` profile over the forward
                   window and emit volume_event_best_trades.csv
    5. reconcile — `reconcile-all` (backtest<->paper<->demo, +Bybit on request)
    6. summary   — print the headline (paired / backtest-only / paper-only / slip)

Design goals: dead simple to run, safe by default (read-only against the VPS,
demo only, never real money), and loud about staleness instead of failing
mysteriously.

    bash scripts/reconcile.sh                     # the whole thing, sane defaults
    bash scripts/reconcile.sh --dry-run           # print every command, run nothing
    bash scripts/reconcile.sh --no-pull           # use the local ledgers as-is
    bash scripts/reconcile.sh --diagnostic        # current-universe membership
    bash scripts/reconcile.sh --with-bybit        # also reconcile demo<->Bybit

The off-by-one PIT bug (membership keyed on the signal stamp date instead of the
trading day) is fixed in the engine; this tool keeps the *plumbing* honest.
See docs/pit_gate.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
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
VPS_HOST = "root@5.223.42.109"
VPS_BASE = "/opt/liquidity-migration/data"
LEDGER_DATASETS = ("event_demo_trades", "event_demo_orders")

DEFAULT_BYBIT_ROOT = "~/SHARED_DATA/bybit_full_pit"
DEFAULT_CONFIG = "configs/volume_alpha.default.yaml"
DEFAULT_DEMO_ROOT = "data/bybit-demo-event"
DEFAULT_PAPER_ROOT = "data/bybit-paper-event"

# Forward window warm-up. The reconcile auto-windows to the paper ledger's first
# signal anyway; we only need the backtest to cover the forward period plus enough
# history for the 30-day features and ~5-day cooldown/max-active state to be exact.
BACKTEST_WARMUP_DAYS = 150


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


def _quote(s: str) -> str:
    return f'"{s}"' if (" " in s or "*" in s) else s


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _py() -> str:
    venv = REPO / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def _cli(*args: str) -> list[str]:
    return [_py(), "-m", "liquidity_migration", *args]


# ----------------------------------------------------------------------------- steps
def pull_ledgers(step: Step, host: str) -> None:
    step.banner(f"Pull demo + paper ledgers from {host}")
    if not shutil.which("rsync"):
        print("⚠️  rsync not found — skipping pull; using local ledgers as-is.")
        return
    for sleeve, local in (("bybit-demo-event", DEFAULT_DEMO_ROOT), ("bybit-paper-event", DEFAULT_PAPER_ROOT)):
        for ds in LEDGER_DATASETS:
            remote = f"{host}:{VPS_BASE}/{sleeve}/{ds}/"
            dest = REPO / local / ds
            dest.mkdir(parents=True, exist_ok=True)
            # -a archive, -z compress; no --delete (never clobber local-only history).
            step.run(["rsync", "-az", "--info=stats0", remote, f"{dest}/"], check=False)


def refresh_manifest(step: Step, root: str, today: dt.date) -> None:
    step.banner(f"Refresh archive_trade_manifest (PIT membership) on {root}")
    end = (today + dt.timedelta(days=2)).isoformat()  # exclusive; v5 fill covers the tail to `today`
    step.run(_cli("--data-root", root, "archive-manifest", "--end", end))


def print_coverage(root: str, today: dt.date) -> pc.CoverageStatus:
    status = pc.coverage_status(root, today=today)
    print("\n" + pc.format_coverage(status))
    return status


def run_backtest(step: Step, *, root: str, config: str, today: dt.date, diagnostic: bool) -> Path:
    step.banner("Backtest the promoted profile over the forward window")
    start = (today - dt.timedelta(days=BACKTEST_WARMUP_DAYS)).isoformat()
    end = (today + dt.timedelta(days=1)).isoformat()  # end-exclusive -> includes today's signals
    report_dir = REPO / "data" / "reconcile" / f"backtest_{today.isoformat()}"
    report_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "--config", config, "--data-root", root, "volume-events",
        "--start", start, "--end", end,
        "--report-dir", str(report_dir),
        # The reconcile only cares about the recent forward window, so the
        # universe-completeness gate is irrelevant here; per-trade PIT membership
        # (the trading-day fix) still applies. This keeps the run bounded + cheap.
        "--allow-partial-pit",
    ]
    if diagnostic:
        # Same-day current-universe diagnostic (labeled biased; never promotion).
        args += ["--pit-membership", "current-universe"]
    step.run(_cli(*args))
    return report_dir


def find_backtest_csv(report_dir: Path) -> Path | None:
    hits = sorted(report_dir.rglob("volume_event_best_trades.csv"))
    return hits[-1] if hits else None


def run_reconcile(step: Step, *, paper: str, demo: str, csv: Path | None, with_bybit: bool, today: dt.date) -> Path:
    step.banner("Reconcile backtest <-> paper <-> demo" + (" <-> Bybit" if with_bybit else ""))
    out = REPO / "data" / "reconcile" / f"report_{today.isoformat()}"
    out.mkdir(parents=True, exist_ok=True)
    args = [
        "reconcile-all",
        "--paper-data-root", paper,
        "--demo-data-root", demo,
        "--output-dir", str(out),
    ]
    if csv is not None:
        args += ["--backtest-trades-csv", str(csv)]
    else:
        print("⚠️  no backtest CSV — running paper<->demo(<->Bybit) only.")
    if not with_bybit:
        args += ["--skip-bybit"]
    # The headline leg aborts on a genuine failure (like the other steps) so a
    # crashed reconcile can't masquerade as success. reconcile-all returns 0 on
    # success even when it finds mismatches (those are the expected output, not
    # an error), so this only fires on a real fault (missing input, exception).
    step.run(_cli(*args))
    return out


def print_summary(out: Path) -> None:
    print(f"\n{'=' * 72}\nRECONCILIATION SUMMARY\n{'=' * 72}")
    reports = sorted(out.rglob("*.md"))
    if not reports:
        print(f"(no report markdown found under {out})")
        return
    wanted = ("paired", "backtest-only", "backtest_only", "paper-only", "paper_only",
              "demo-only", "demo_only", "slip", "orphan", "unmatched", "PnL", "verdict")
    for rep in reports:
        try:
            lines = rep.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        hits = [ln.strip() for ln in lines if any(w.lower() in ln.lower() for w in wanted)]
        if hits:
            print(f"\n--- {rep.relative_to(out)} ---")
            for ln in hits[:12]:
                print(f"  {ln}")
    print(f"\nFull reports: {out}")


# ----------------------------------------------------------------------------- main
def main() -> int:
    p = argparse.ArgumentParser(
        description="One-command demo-forward reconciliation (short sleeve).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--bybit-root", default=DEFAULT_BYBIT_ROOT, help="Research root for the backtest leg.")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="Strategy config (the promoted profile).")
    p.add_argument("--paper-root", default=DEFAULT_PAPER_ROOT)
    p.add_argument("--demo-root", default=DEFAULT_DEMO_ROOT)
    p.add_argument("--vps", default=VPS_HOST, help="VPS ssh target for the ledger pull.")
    p.add_argument("--no-pull", action="store_true", help="Skip the VPS ledger rsync; use local ledgers.")
    p.add_argument("--no-manifest", action="store_true", help="Skip the archive-manifest refresh.")
    p.add_argument("--no-backtest", action="store_true", help="Skip the backtest; reconcile paper<->demo only.")
    p.add_argument("--diagnostic", action="store_true",
                   help="Use current-universe membership for the backtest (labeled biased; same-day check).")
    p.add_argument("--with-bybit", action="store_true", help="Also reconcile demo<->Bybit (needs API creds).")
    p.add_argument("--force", action="store_true", help="Run the backtest even if the manifest is stale.")
    p.add_argument("--dry-run", action="store_true", help="Print every command without running anything.")
    args = p.parse_args()

    today = _today()
    root = args.bybit_root
    step = Step(args.dry_run)

    print(f"liquidity-migration reconcile — {today.isoformat()} (UTC)")
    print(f"  research root : {root}")
    print(f"  paper / demo  : {args.paper_root}  /  {args.demo_root}")

    if not args.no_pull:
        pull_ledgers(step, args.vps)

    csv: Path | None = None
    if not args.no_backtest:
        if not args.no_manifest:
            refresh_manifest(step, root, today)
        step.banner("PIT coverage check")
        status = print_coverage(root, today)
        if status.is_stale and not (args.diagnostic or args.force or args.dry_run):
            raise SystemExit(
                "\n❌ archive manifest is stale — recent signals would hard-reject with "
                "pit_membership_fail.\n   Re-run with the manifest refresh enabled, or pass "
                "--diagnostic (current-universe, biased) / --force to proceed anyway."
            )
        report_dir = run_backtest(step, root=root, config=args.config, today=today, diagnostic=args.diagnostic)
        csv = find_backtest_csv(report_dir)
        if csv is None and not args.dry_run:
            print(f"⚠️  no volume_event_best_trades.csv under {report_dir}; reconciling without the backtest leg.")

    out = run_reconcile(step, paper=args.paper_root, demo=args.demo_root, csv=csv,
                        with_bybit=args.with_bybit, today=today)
    if not args.dry_run:
        print_summary(out)
    print("\n✅ done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
