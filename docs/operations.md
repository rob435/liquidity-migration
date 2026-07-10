# Operations

Use `scripts/ops.sh` as the single human-facing entry point for routine
demo/paper operations. It is a thin router: the existing canonical scripts
still own all strategy, reconciliation, research-integrity, reset, and deploy
logic.

Running it with no arguments, `help`, `-h`, or `--help` only prints help.

```bash
scripts/ops.sh
scripts/ops.sh status
scripts/ops.sh reconcile quick
scripts/ops.sh reconcile full
scripts/ops.sh equity --sleeves long,continuous
scripts/ops.sh data-audit --venue both
scripts/ops.sh test -q
```

## Safety boundary

- This interface never sets `REAL_MONEY` and never enables mainnet trading.
- `status` calls the read-only VPS verifier.
- A ledger `reset` is an explicit remote dry-run unless `--execute` is passed.
  The reset script still independently refuses mainnet credentials, concurrent
  resets, non-flat positions, open orders, and mismatched systemd credential
  files before it removes anything.
- `deploy` refuses unless the first argument after the command is exactly
  `--execute`. The checked deploy script retains its own commit, test, config,
  credential, and service gates.
- A tail experiment produces research artifacts only. No command here promotes
  a strategy, changes a decision rule, or authorizes real money.
- `data-build` refuses unless its first argument is `--execute`; the underlying
  tool also requires explicit datasets, window, symbol authority, and a new
  immutable receipt outside both data roots.

## Commands

| Command | Canonical route | Operational effect |
|---|---|---|
| `status` | `scripts/verify_vps_live.sh` | Read-only VPS checkout, config, credential, service, and liveness checks. |
| `reconcile quick` | `scripts/reconcile.sh --quick` | Fast paper/demo execution reconciliation; reads VPS ledgers and writes local reports. |
| `reconcile full` | `scripts/reconcile.sh` | Full PIT refresh/backtest/demo/paper reconciliation. This can download data and is substantially slower. |
| `equity` | `scripts/equity_curves.sh` | Official LONG/CONTINUOUS equity runner; forwards every option unchanged. |
| `reset` | VPS `scripts/reset_demo_paper_ledgers.sh` | Dry-run preview by default; `--execute` is the only mutation opt-in. |
| `data-audit` | `scripts/granular_data_surface.py` | Read-only PIT-manifest-anchored granular/alternative-data coverage and schema audit. |
| `data-build --execute` | `scripts/granular_data_surface.py --execute` | Explicit resume-safe granular backfill; refused without the handshake and immutable receipt path. |
| `tail-plan` | `continuous_tail_survival_2026_07_10.py --plan` | Checks the frozen preregistration, worktree, roots, partitions, and both-venue readiness without running cells. |
| `tail-run` | `continuous_tail_survival_2026_07_10.py` | Runs only the preregistered cells and preserves the dispatcher's integrity/refusal gates. |
| `test` | `python -m pytest` | Runs all tests, or only the forwarded pytest selection. |
| `deploy --execute` | `scripts/deploy_vps_live.sh` | Checked demo/paper VPS deploy. Refused without the explicit handshake. |

Every argument after the command (and, for reconciliation, after `quick` or
`full`) is forwarded as its own argument. Do not put a Python command plus
flags into `PYTHON`; it must name one executable or executable path.

## VPS status and reconciliation

```bash
# Read-only production verification.
scripts/ops.sh status

# Quick execution-plane comparison for both sleeves.
scripts/ops.sh reconcile quick --sleeves long,continuous

# Full three-way PIT reconciliation, with the canonical dry-run option.
scripts/ops.sh reconcile full --dry-run
scripts/ops.sh reconcile full --with-funding
```

Quick reconciliation is not alpha evidence. Full reconciliation is an
execution/data-integrity check and does not promote CONTINUOUS or authorize
mainnet trading.

## Safe ledger reset

Preview exactly what would be archived and removed on the VPS:

```bash
scripts/ops.sh reset --sleeves all --label new-forward-window
```

Only after reviewing that preview, request the guarded mutation explicitly:

```bash
scripts/ops.sh reset --execute --sleeves all --label new-forward-window
```

The remote checkout defaults to `/opt/liquidity-migration`. Reset archives the
selected ledgers before removal and preserves the continuous account-equity
high-water state. It does not cancel orders or close positions; it refuses until
the demo account is already flat with no open orders.

## Granular data

Audit first; this performs no network writes:

```bash
scripts/ops.sh data-audit --venue both \
  --start 2023-04-01 --end 2026-07-10 \
  --output research/granular_adverse_risk/readiness.json
```

Backfill only a declared surface with explicit authority and a new receipt:

```bash
scripts/ops.sh data-build --execute \
  --venue both \
  --datasets funding,open_interest,premium_index_1h \
  --start 2026-07-01 --end 2026-07-10 \
  --all-pit-symbols \
  --output research/granular_adverse_risk/download-2026-07-10.json
```

Receipts are immutable and must live outside the data roots. Equal, nested,
symlink-aliased, or shared-child Bybit/Binance roots are refused. A readiness
receipt is data evidence only, never an alpha or promotion result.

## Tail-survival research

Run the plan on the larger research machine first:

```bash
scripts/ops.sh tail-plan
scripts/ops.sh tail-run
```

The dispatcher itself remains authoritative. It requires the fixed windows,
both Bybit and Binance, stable residual momentum, complete PIT/funding inputs,
registered cells, matching configuration hashes, byte-level root identity, a
matching full-PIT verification receipt, and a clean relevant worktree. Missing
or stale root receipts force diagnostic-only output; diagnostic overrides
cannot become a positive registered verdict.

## Deploy

Deploy is intentionally awkward enough to avoid an accidental keystroke:

```bash
# Refused:
scripts/ops.sh deploy

# Explicit checked deploy:
scripts/ops.sh deploy --execute
```

Use environment variables rather than positional deploy arguments when
selecting a target or pinning a commit:

```bash
SSH_TARGET=root@host \
REPO_DIR=/opt/liquidity-migration \
EXPECTED_COMMIT="$(git rev-parse HEAD)" \
scripts/ops.sh deploy --execute
```

## Overrides

```bash
SSH_TARGET=root@host scripts/ops.sh status
REPO_DIR=/opt/liquidity-migration scripts/ops.sh reset --sleeves continuous
PYTHON=/path/to/python scripts/ops.sh test -q tests/test_runtime_scripts.py
```

- `SSH_TARGET` defaults to `root@116.202.15.128`.
- `REPO_DIR` defaults to `/opt/liquidity-migration`.
- `PYTHON` applies to `tail-plan`, `tail-run`, and `test`. When unset, the
  wrapper prefers the repository virtual environment, then `python3`.
