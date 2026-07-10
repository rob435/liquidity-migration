#!/usr/bin/env python3
"""Fast demo-forward execution reconciliation for selected sleeves.

Pulls the live ledgers and reconciles each selected sleeve:

    LONG   (v11a)        : paper <-> demo

CONTINUOUS (fade) is research-stage demo/paper only. The quick path covers both
active sleeves by default; use `--sleeves long` only for an explicitly narrowed
diagnostic.

Pipeline:
    1. pull        — rsync the live demo+paper ledgers for every selected sleeve  [--no-pull]
    2. rmom        — auto-recompute residual_momentum when continuous selected    [--no-rmom]
    3. reconcile   — per-sleeve paper/demo (+ continuous signal check)            [--sleeves]
    4. summary     — one consolidated headline across selected sleeves

Manifest refresh / kline-fill / coverage / backtest provisioning is not part of
the quick path; refresh the PIT manifest manually when needed:
`python -m liquidity_migration --data-root <root> archive-manifest`.

Safe by default: read-only against the VPS, demo only, never real money.

    bash scripts/reconcile.sh --quick                         # both sleeves
    bash scripts/reconcile.sh --quick --sleeves long          # explicit narrow check
    bash scripts/reconcile.sh --dry-run             # print every command, run nothing
    bash scripts/reconcile.sh --help                # all options

See docs/pit_gate.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

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

# rmom factor panel needs a few months of history for the 6-factor fit + 7d momentum.
RMOM_WARMUP_DAYS = 150

# Per-sleeve ledger layout. Each of demo/paper = (remote_subdir, local_dir, datasets).
# extra_files are pulled from the DEMO root (rmom panel + the WS kline store the
# continuous signal-check replays).
SLEEVES: dict[str, dict[str, object]] = {
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

# Exact v2-forward boundary. The 2026-06-18 deploy reset the continuous control to
# continuous_ensemble_v2; old v1 rows remain in pulled ledgers only so the executor can
# wind them down. Reconcile must not let those rows poison the v2 baseline.
CONTINUOUS_V2_START = dt.datetime(2026, 6, 18, 19, 54, tzinfo=dt.timezone.utc)
CONTINUOUS_V2_START_MS = int(CONTINUOUS_V2_START.timestamp() * 1000)
CONTINUOUS_V2_PROFILE = "continuous_ensemble_v2"
CONTINUOUS_V2_DEMO_STRATEGY_ID = "continuous_fade_v2"
CONTINUOUS_V2_PAPER_STRATEGY_ID = "continuous_fade_v2_paper"


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
    candidates = (
        REPO / ".venv" / "Scripts" / "python.exe",
        REPO / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _cli(*args: str) -> list[str]:
    return [_py(), "-m", "liquidity_migration", *args]


def _script(name: str, *args: str) -> list[str]:
    return [_py(), str(REPO / "scripts" / name), *args]


def _have_rsync() -> bool:
    return shutil.which("rsync") is not None


def _have_scp() -> bool:
    return shutil.which("scp") is not None


def _scp_ssh_options() -> list[str]:
    raw_key = os.environ.get("LIQMIG_VPS_SSH_KEY", "").strip()
    key = Path(raw_key).expanduser() if raw_key else Path.home() / ".ssh" / "liqmig_deploy_20260609"
    identity = ["-i", str(key), "-o", "IdentitiesOnly=yes"] if key.exists() else []
    return [*identity, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


def _remote_dir_state(step: Step, host: str, path: str, ssh_options: list[str]) -> str:
    """Return nonempty/empty/absent/error for a remote dataset directory."""
    if getattr(step, "dry_run", False):
        return "nonempty"
    quoted = shlex.quote(path)
    cmd = (
        f"if [ ! -d {quoted} ]; then echo absent; "
        f"elif find {quoted} -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then echo nonempty; "
        "else echo empty; fi"
    )
    rc, out = step.run_capture(["ssh", *ssh_options, host, cmd], check=False)
    if rc != 0:
        return "error"
    state = out.strip().splitlines()[-1] if out.strip() else ""
    return state if state in {"nonempty", "empty", "absent"} else "error"


def _remote_nonempty_dir(step: Step, host: str, path: str, ssh_options: list[str]) -> bool:
    return _remote_dir_state(step, host, path, ssh_options) == "nonempty"


def _remove_local_dataset(dest: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    if dest.exists():
        shutil.rmtree(dest)


def _remote_file_exists(step: Step, host: str, path: str, ssh_options: list[str]) -> bool:
    if getattr(step, "dry_run", False):
        return True
    return step.run(["ssh", *ssh_options, host, f"test -f {shlex.quote(path)}"], check=False) == 0


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
    use_rsync = _have_rsync()
    use_scp = not use_rsync and _have_scp()
    if not use_rsync and not use_scp and not step.dry_run:
        raise SystemExit(
            "neither rsync nor scp found; refusing to use possibly stale local ledgers. "
            "Re-run with --no-pull to use local ledgers explicitly."
        )
    if not use_rsync and not use_scp:
        print("⚠️  neither rsync nor scp found — skipping pull; using local ledgers as-is.")
        return
    if use_scp:
        print("rsync not found; using scp fallback for VPS ledger pull.")
    scp_options = _scp_ssh_options()
    for role in ("demo", "paper"):
        remote_sub, local, datasets = spec[role]  # type: ignore[misc]
        for ds in datasets:
            remote_path = f"{VPS_BASE}/{remote_sub}/{ds}"
            remote_dir = f"{host}:{remote_path}/"
            dest = REPO / local / ds
            state = _remote_dir_state(step, host, remote_path, scp_options)
            if state == "error":
                raise SystemExit(f"could not inspect remote dataset: {remote_path}")
            if state in {"absent", "empty"}:
                _remove_local_dataset(dest, dry_run=step.dry_run)
                print(f"  remote dataset {state}: {remote_path} — local mirror cleared")
                continue
            _remove_local_dataset(dest, dry_run=step.dry_run)
            dest.mkdir(parents=True, exist_ok=True)
            if use_rsync:
                # -a archive, -z compress, -q quiet; --delete makes the local
                # live-ledger mirror match the VPS after reset/compaction.
                # -q (not --info=stats0) for macOS openrsync 2.6.9 + Linux rsync 3.x.
                step.run(["rsync", "-azq", "--delete", remote_dir, f"{dest}/"], check=True)
            else:
                step.run(
                    ["scp", "-q", *scp_options, "-r", f"{remote_dir}*", str(dest)],
                    check=True,
                )
    # extra single files (rmom panel, WS kline store) live under the demo root.
    demo_remote, demo_local, _ = spec["demo"]  # type: ignore[misc]
    for rel in spec["extra_files"]:  # type: ignore[union-attr]
        remote_path = f"{VPS_BASE}/{demo_remote}/{rel}"
        remote = f"{host}:{remote_path}"
        dest = REPO / demo_local / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if use_rsync:
            step.run(["rsync", "-azq", remote, str(dest)], check=True)
        else:
            if not _remote_file_exists(step, host, remote_path, scp_options):
                if not step.dry_run and (dest.exists() or dest.is_symlink()):
                    dest.unlink()
                raise SystemExit(
                    f"required remote continuous market-plane file is absent: {remote_path}; "
                    "stale local mirror cleared and reconciliation refused"
                )
            step.run(
                ["scp", "-q", *scp_options, remote, str(dest)],
                check=True,
            )


def refresh_rmom(step: Step, root: str, today: dt.date) -> None:
    """Recompute residual_momentum.parquet on the research root so the continuous
    engine/signal tooling never trips over a stale panel. Bounded window keeps it
    cheap. The live continuous signal-check uses the (fresher) live-root panel;
    this keeps the *research* root usable for an engine backtest too."""
    step.banner(f"Auto-recompute residual_momentum on {root}")
    start = (today - dt.timedelta(days=RMOM_WARMUP_DAYS)).isoformat()
    end = (today + dt.timedelta(days=1)).isoformat()
    step.run(_script("precompute_residual_momentum.py", "--root", root, "--start", start, "--end", end))


def reconcile_long(step: Step, *, paper: str, demo: str) -> tuple[str, bool]:
    step.banner("Reconcile LONG: paper <-> demo")
    rc, out = step.run_capture(_cli("reconcile-long-paper-demo", "--paper-data-root", paper, "--demo-data-root", demo))
    return _summarize_leg(out, "long paper-demo reconciliation", rc)


def reconcile_continuous(
    step: Step,
    *,
    paper: str,
    demo: str,
    start_ts_ms: int = CONTINUOUS_V2_START_MS,
    strategy_profile: str = CONTINUOUS_V2_PROFILE,
    paper_strategy_id: str = CONTINUOUS_V2_PAPER_STRATEGY_ID,
    demo_strategy_id: str = CONTINUOUS_V2_DEMO_STRATEGY_ID,
) -> tuple[str, bool]:
    step.banner("Reconcile CONTINUOUS: paper-readiness + signal-consistency")
    rc_pd, out = step.run_capture(
        _cli(
            "continuous-forward-readiness",
            "--paper-data-root", paper,
            "--demo-data-root", demo,
            "--paper-only",
            "--start-ts-ms", str(start_ts_ms),
            "--strategy-profile", strategy_profile,
            "--paper-strategy-id", paper_strategy_id,
            "--demo-strategy-id", demo_strategy_id,
        )
    )
    pd_summary, pd_ok = _summarize_leg(out, "continuous forward readiness", rc_pd)
    rc_exec, exec_out = step.run_capture(
        _cli(
            "reconcile-continuous-paper-demo",
            "--paper-data-root", paper,
            "--demo-data-root", demo,
            "--start-ts-ms", str(start_ts_ms),
            "--paper-strategy-id", paper_strategy_id,
            "--demo-strategy-id", demo_strategy_id,
            "--min-pairs-warning", "0",
        )
    )
    exec_summary, exec_ok = _summarize_leg(exec_out, "continuous paper-demo reconciliation", rc_exec)
    # Signal-consistency: are the no-order paper entries genuine engine D9 picks?
    rc_sig, sig = step.run_capture(
        _script(
            "continuous_demo_signal_check.py",
            "--root", paper,
            "--market-root", demo,
            "--trades-dataset", "continuous_fade_paper_trades",
            "--start-ts-ms", str(start_ts_ms),
            "--strategy-id", paper_strategy_id,
        )
    )
    sig_summary, sig_ok = _summarize_leg(sig, "SUMMARY:", rc_sig)
    return f"{pd_summary}  ||  paper-demo: {exec_summary}  ||  signal: {sig_summary}", (
        pd_ok and exec_ok and sig_ok
    )


def _summarize_leg(out: str, needle: str, rc: int) -> tuple[str, bool]:
    """One-line summary for a reconcile leg + whether it passed.

    A leg passes only when it exited 0 AND printed its summary line. A nonzero
    exit (a readiness gate failing, or a crash before the summary — e.g. the
    continuous signal-check raising SystemExit on a missing WS kline store) is
    rendered as an explicit FAILED marker rather than the benign '(no output)',
    so a crashed leg is never indistinguishable from a clean run-with-no-pairs
    in the unified headline (reconciliation-3, reconciliation-5).
    """
    line = next((ln.strip() for ln in out.splitlines() if needle in ln), None)
    if rc != 0:
        return f"⚠️ FAILED (rc={rc}): {line or '(no summary line)'}", False
    if line is None:
        return "⚠️ FAILED (rc=0 but no summary line printed)", False
    return line, True


# ----------------------------------------------------------------------------- main
def main() -> int:
    p = argparse.ArgumentParser(
        description="Fast paper<->demo execution reconciliation for selected sleeves.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--sleeves", default="long,continuous",
                   help="Comma list of sleeves to reconcile; quick mode defaults to both active sleeves.")
    p.add_argument("--bybit-root", default=DEFAULT_BYBIT_ROOT, help="Research root for the backtest + provisioning.")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="Strategy config (the promoted profile).")
    p.add_argument("--vps", default=VPS_HOST, help="VPS ssh target for the ledger pull.")
    p.add_argument("--no-pull", action="store_true", help="Skip the VPS ledger rsync; use local ledgers.")
    p.add_argument("--no-rmom", action="store_true", help="Skip the automatic residual_momentum recompute.")
    p.add_argument("--dry-run", action="store_true", help="Print every command without running anything.")
    args = p.parse_args()

    sleeves = [s.strip() for s in args.sleeves.split(",") if s.strip()]
    bad = [s for s in sleeves if s not in SLEEVES]
    if bad:
        raise SystemExit(f"unknown sleeve(s) {bad}; valid: {', '.join(ALL_SLEEVES)}")
    today = _today()
    root = args.bybit_root
    step = Step(args.dry_run)

    print(f"liquidity-migration reconcile — {today.isoformat()} (UTC)")
    print(f"  sleeves       : {', '.join(sleeves)}")
    print(f"  research root : {root}")

    # 1. Pull every selected sleeve's ledgers.
    if not args.no_pull:
        for s in sleeves:
            pull_sleeve(step, args.vps, s)

    # 2. Research-data provisioning: the quick path only needs the continuous
    # signal-check's rmom refresh.
    if "continuous" in sleeves and not args.no_rmom:
        refresh_rmom(step, root, today)

    # 3. Per-sleeve reconcile. (The continuous signal-check uses the pulled
    # live-root rmom panel, so a continuous-only run needs no research provisioning.)
    # Track each sleeve's pass/fail so the wrapper exit code is a machine-checkable
    # tripwire — a failed readiness gate or a crashed leg must NOT exit 0
    # (reconciliation-3).
    summary: dict[str, str] = {}
    ok: dict[str, bool] = {}
    if "long" in sleeves:
        lp = SLEEVES["long"]["paper"][1]  # type: ignore[index]
        ld = SLEEVES["long"]["demo"][1]  # type: ignore[index]
        summary["long"], ok["long"] = reconcile_long(step, paper=lp, demo=ld)
    if "continuous" in sleeves:
        cp = SLEEVES["continuous"]["paper"][1]  # type: ignore[index]
        cd = SLEEVES["continuous"]["demo"][1]  # type: ignore[index]
        summary["continuous"], ok["continuous"] = reconcile_continuous(step, paper=cp, demo=cd)

    # 4. Unified headline. Dry-run never actually ran a leg, so it has no pass/fail
    # signal — it always succeeds (it only echoes commands).
    if args.dry_run:
        print("\n✅ done.")
        return 0
    print(f"\n{'=' * 72}\nRECONCILIATION SUMMARY — SELECTED SLEEVES\n{'=' * 72}")
    for s in sleeves:
        print(f"\n## {SLEEVES[s]['label']}")
        print(f"  {summary.get(s, '(skipped)')}")
    print(f"\nReports under: {REPO / 'data' / 'reconcile'}")
    failed = [s for s in sleeves if not ok.get(s, False)]
    if failed:
        print(f"\n❌ RECONCILE FAILED — sleeve(s) with a failed/crashed leg: {', '.join(failed)}")
        return 1
    print("\n✅ done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
