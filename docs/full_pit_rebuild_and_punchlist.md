# Full PIT Rebuild + Long-Native Quant-Grade Punchlist

**Date authored:** 2026-05-24
**Authored by:** research session driven by repo owner
**Status:** handoff — nothing in this doc is executed yet
**Strategy in scope:** long-only sleeve only (`MultiStratV1` / `long_native_v11a_uni10_sniper_retrace1pct_6h_fallthru`)

## Why this exists

Two findings from the 2026-05-24 long-only FC daily-increase sweep forced this rewrite:

1. **The current `LongNativeConfig.fc_min_day_return=0.15` is dead code in v11a** — `fc_use_sigma_threshold=True` makes the per-coin sigma threshold the binding gate. Lowering or raising the 15% knob produces identical results in production. That's a config-integrity gap.
2. **The "OOS vs IS" distinction in the repo is no longer honest.** Both pre-2023 roots have been touched by multiple prior FC parameter sweeps (FC_12/15/18/20 in `docs/long_native_findings.md` + a 7-value re-sweep on 2026-05-24). Calling them OOS is multiple-testing denial.

This doc is the punchlist to fix both problems and the five other gaps between the v11a long sleeve and quant-grade evidence.

The deliverable is split in two parts:

- **Part A — Full PIT data rebuild.** Clean-slate per-venue full-history data roots; future OOS = forward demo only.
- **Part B — Six remaining work items** to take v11a from "exploratory candidate" to "honest candidate with evidence".

Each part is self-contained and can be picked up by a fresh session.

---

## Part A — Full PIT data rebuild

### A.1 Goal & final state

Replace the current 3-root patchwork:

| Old root | Coverage | Datasets | Verdict |
|---|---|---|---|
| `~/SHARED_DATA/bybit_fullpit_1h` | 2023-05 → 2026-05 | klines, funding, OI, mark/index/premium, signed_flow | partially-warmed by sweeps |
| `~/SHARED_DATA/bybit_oos_pre2023` | 2021-01 → 2023-05 | klines, manifest, signed_flow (no funding) | warmed by FC sweeps |
| `~/SHARED_DATA/binance_oos_pit` | 2020-01 → 2023-04 | klines, manifest | warmed by FC sweeps |

With two clean per-venue roots:

| New root | Coverage | Datasets to fill |
|---|---|---|
| `~/SHARED_DATA/bybit_full_pit/` | ~2021-01 → today | klines_1h, archive_trade_manifest, funding, open_interest, mark_price_1h, index_price_1h, premium_index_1h, signed_flow_1h, instruments |
| `~/SHARED_DATA/binance_full_pit/` | ~2019-09 → today | klines_1h, archive_trade_manifest, funding, open_interest, mark_price_1h, index_price_1h, premium_index_1h, instruments |

Then:
- No internal `SPLITS` / "OOS" labels. The per-venue dataset *is* the validation surface.
- Pristine OOS henceforth = forward demo + paper ledger, ticking from 2026-05-22.
- Side-by-side venue comparison every run: agreement = robust signal, disagreement = regime/microstructure artefact.

### A.2 What to keep, what to delete

**Keep — DO NOT TOUCH:**
- `data/bybit-demo-event/` — live demo ledger (operator state)
- `data/bybit-paper-event/` — paper shadow ledger (operator state)
- Anything else under `data/` (all ledgers + cycle reports; no venue data lives here)

**Archive, then destroy (full root removal):**
- `~/SHARED_DATA/bybit_fullpit_1h/` (~3 GB)
- `~/SHARED_DATA/bybit_oos_pre2023/` (~700 MB)
- `~/SHARED_DATA/binance_oos_pit/` (~1 GB)

**Archive step** (before any deletion) — preserve research history, not raw data:

```bash
ARCHIVE_DIR=~/SHARED_DATA/archive/2026-05-24_pre_full_pit_rebuild
mkdir -p "$ARCHIVE_DIR"
for root in bybit_fullpit_1h bybit_oos_pre2023 binance_oos_pit; do
  src=~/SHARED_DATA/$root
  [ -d "$src/reports" ] && (cd "$src" && tar --zstd -cf "$ARCHIVE_DIR/${root}_reports.tar.zst" reports/)
  [ -d "$src/_download_markers" ] && (cd "$src" && tar --zstd -cf "$ARCHIVE_DIR/${root}_download_markers.tar.zst" _download_markers/)
done
```

After verification gates in A.5 pass:

```bash
rm -rf ~/SHARED_DATA/bybit_fullpit_1h
rm -rf ~/SHARED_DATA/bybit_oos_pre2023
rm -rf ~/SHARED_DATA/binance_oos_pit
```

### A.3 Build commands — Bybit

Verified CLI signatures (see `python -m liquidity_migration <cmd> --help`). All commands idempotent and resumable.

Predecessor reference (now deleted): `scripts/build_oos_roots.sh`. The new script is structurally identical but with extended date ranges and added ancillary-dataset stages.

```bash
#!/usr/bin/env bash
# scripts/build_full_pit_bybit.sh
set -euo pipefail

ROOT="${BYBIT_FULL_ROOT:-$HOME/SHARED_DATA/bybit_full_pit}"
START="${BYBIT_START:-2021-01-01}"
END="${BYBIT_END:-$(date -u +%Y-%m-%d)}"   # today, end-exclusive
MANIFEST_WORKERS="${MANIFEST_WORKERS:-16}"
KLINE_WORKERS="${KLINE_WORKERS:-8}"
ANCILLARY_WORKERS="${ANCILLARY_WORKERS:-4}"   # gentler on REST rate limit
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"

cd "$(dirname "$0")/.."
mkdir -p "$ROOT"

echo "[1/5] Bybit — PIT manifest ($START → $END exclusive)"
"$PYTHON_BIN" -m liquidity_migration --data-root "$ROOT" \
  archive-manifest --start "$START" --end "$END" --workers "$MANIFEST_WORKERS"

echo "[2/5] Bybit — 1h klines via v5 API (manifest-gated)"
"$PYTHON_BIN" -m liquidity_migration --data-root "$ROOT" \
  archive-download-klines-1h-api --start "$START" --end "$END" --workers "$KLINE_WORKERS"

echo "[3/5] Bybit — filter manifest to ≥20-bar coverage"
"$PYTHON_BIN" -m liquidity_migration.binance_vision \
  filter-manifest --data-root "$ROOT"

# Derive symbol list from the filtered manifest for the ancillary REST pulls.
SYMBOLS=$("$PYTHON_BIN" - <<'PY'
import polars as pl, pathlib, os
root = pathlib.Path(os.environ["ROOT"]).expanduser()
df = pl.read_parquet(str(root / "archive_trade_manifest" / "**" / "*.parquet"))
print(",".join(sorted(df["symbol"].unique().to_list())))
PY
)
echo "[4/5] Bybit — funding + open_interest + mark/index/premium for $(echo "$SYMBOLS" | tr ',' '\n' | wc -l) symbols"
"$PYTHON_BIN" -m liquidity_migration --data-root "$ROOT" \
  download-data \
    --symbols "$SYMBOLS" \
    --start "$START" --end "$END" \
    --datasets funding,open_interest,mark_price_1h,index_price_1h,premium_index_1h \
    --workers "$ANCILLARY_WORKERS"

echo "[5/5] Bybit — signed flow (optional but cheap)"
"$PYTHON_BIN" -m liquidity_migration --data-root "$ROOT" \
  download-data \
    --symbols "$SYMBOLS" \
    --start "$START" --end "$END" \
    --datasets archive_klines_1m \
    --workers "$KLINE_WORKERS"
# Signed-flow build follows naturally from raw_public_trades; see binance_vision.py for the recipe.
# If signed_flow_1h has its own CLI stage in the future, slot it here.

echo
echo "Bybit full PIT root ready at: $ROOT"
```

Notes:
- `END` defaults to today (end-exclusive), so the script naturally captures the full history available at run time.
- Steps 4-5 use lower worker count (`ANCILLARY_WORKERS=4`) because Bybit REST is stricter than the archive path.
- Step 4 may report `funding_missing` for pre-2023 dates on some symbols — that's expected, Bybit's funding history doesn't reach back that far for every symbol. The strategy degrades gracefully.

### A.4 Build commands — Binance

```bash
#!/usr/bin/env bash
# scripts/build_full_pit_binance.sh
set -euo pipefail

ROOT="${BINANCE_FULL_ROOT:-$HOME/SHARED_DATA/binance_full_pit}"
END="${BINANCE_END:-$(date -u +%Y-%m-%d)}"
WORKERS="${BINANCE_WORKERS:-24}"
ANCILLARY_WORKERS="${ANCILLARY_WORKERS:-4}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"

cd "$(dirname "$0")/.."
mkdir -p "$ROOT"

echo "[1/2] Binance — full PIT root from data.binance.vision (start → $END exclusive)"
"$PYTHON_BIN" -m liquidity_migration.binance_vision \
  build-binance-oos --data-root "$ROOT" --end "$END" --workers "$WORKERS"

# Derive symbol list for ancillary REST pulls.
SYMBOLS=$("$PYTHON_BIN" - <<'PY'
import polars as pl, pathlib, os
root = pathlib.Path(os.environ["ROOT"]).expanduser()
df = pl.read_parquet(str(root / "archive_trade_manifest" / "**" / "*.parquet"))
print(",".join(sorted(df["symbol"].unique().to_list())))
PY
)
echo "[2/2] Binance — funding + open_interest + mark/index/premium + taker_flow"
"$PYTHON_BIN" -m liquidity_migration --data-root "$ROOT" \
  download-binance-proxy \
    --symbols "$SYMBOLS" \
    --start 2019-09-01 --end "$END" \
    --datasets funding,open_interest,mark_price_1h,index_price_1h,premium_index_1h,taker_flow_1h \
    --workers "$ANCILLARY_WORKERS"

echo
echo "Binance full PIT root ready at: $ROOT"
```

Notes:
- `binance_vision build-binance-oos` is already coverage-aware. The current default `--end` is `2023-05-01`; we override to today.
- Binance USDM perpetuals launched in September 2019 — the build automatically picks the earliest available month per symbol.

### A.5 Orchestrator script

```bash
#!/usr/bin/env bash
# scripts/build_full_pit_roots.sh
# Top-level driver: archive old roots → build both new roots → verify → (manual) delete.
set -euo pipefail

cd "$(dirname "$0")/.."

ARCHIVE_DIR="${ARCHIVE_DIR:-$HOME/SHARED_DATA/archive/$(date -u +%Y-%m-%d)_pre_full_pit_rebuild}"
SKIP_ARCHIVE="${SKIP_ARCHIVE:-0}"
SKIP_BYBIT="${SKIP_BYBIT:-0}"
SKIP_BINANCE="${SKIP_BINANCE:-0}"
SKIP_VERIFY="${SKIP_VERIFY:-0}"

if [ "$SKIP_ARCHIVE" = "0" ]; then
  echo "=== [0/4] Archiving old roots' reports/ + _download_markers/ ==="
  bash scripts/archive_pre_rebuild_reports.sh
fi

if [ "$SKIP_BYBIT" = "0" ]; then
  echo "=== [1/4] Bybit full PIT build ==="
  bash scripts/build_full_pit_bybit.sh
fi

if [ "$SKIP_BINANCE" = "0" ]; then
  echo "=== [2/4] Binance full PIT build ==="
  bash scripts/build_full_pit_binance.sh
fi

if [ "$SKIP_VERIFY" = "0" ]; then
  echo "=== [3/4] Verification gates ==="
  bash scripts/verify_full_pit_rebuild.sh
fi

echo
echo "=== [4/4] Old roots can now be deleted (manual step):"
echo "      rm -rf ~/SHARED_DATA/bybit_fullpit_1h"
echo "      rm -rf ~/SHARED_DATA/bybit_oos_pre2023"
echo "      rm -rf ~/SHARED_DATA/binance_oos_pit"
echo "Run the deletion yourself — the script will not destroy the old roots automatically."
```

### A.6 Verification gates

All gates must pass before any deletion. `scripts/verify_full_pit_rebuild.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
NEW_BYBIT=~/SHARED_DATA/bybit_full_pit
NEW_BINANCE=~/SHARED_DATA/binance_full_pit
OLD_BYBIT=~/SHARED_DATA/bybit_fullpit_1h
OLD_BYBIT_OOS=~/SHARED_DATA/bybit_oos_pre2023

echo "[gate 1] Data-layer audit — Bybit"
"$PYTHON_BIN" -m liquidity_migration --data-root "$NEW_BYBIT" data-layer-audit

echo "[gate 2] Data-layer audit — Binance"
"$PYTHON_BIN" -m liquidity_migration --data-root "$NEW_BINANCE" data-layer-audit

echo "[gate 3] Coverage parity vs old canonical (overlap window 2023-05-03 → 2026-05-17)"
"$PYTHON_BIN" - <<'PY'
import polars as pl, pathlib, os
new = pathlib.Path(os.path.expanduser("~/SHARED_DATA/bybit_full_pit/klines_1h"))
old = pathlib.Path(os.path.expanduser("~/SHARED_DATA/bybit_fullpit_1h/klines_1h"))
nd = pl.read_parquet(str(new / "**" / "*.parquet")).filter(
    (pl.col("date") >= "2023-05-03") & (pl.col("date") <= "2026-05-17"))
od = pl.read_parquet(str(old / "**" / "*.parquet"))
n_rows, o_rows = nd.height, od.height
n_syms = nd["symbol"].n_unique()
o_syms = od["symbol"].n_unique()
print(f"  old: {o_rows:,} rows / {o_syms} symbols")
print(f"  new: {n_rows:,} rows / {n_syms} symbols")
assert n_rows >= o_rows * 0.98, "new root has <98% of old row count in overlap window"
assert n_syms >= o_syms * 0.98, "new root has <98% of old symbol count in overlap window"
print("  PASS")
PY

echo "[gate 4] Coverage parity vs old Bybit OOS (overlap window 2021-01 → 2023-05-02)"
# Same check, different window. Snipped for brevity.

echo "[gate 5] Smoke FC sweep at baseline 0.15 on new Bybit root"
"$PYTHON_BIN" scripts/long_native_sweep_fc_min_day.py \
  --data-root "$NEW_BYBIT" --values 0.15

echo "[gate 6] Tests"
"$PYTHON_BIN" -m pytest -q

echo "[gate 7] Lint"
.venv/bin/ruff check liquidity_migration tests

echo
echo "All gates PASSED. Safe to delete old roots."
```

### A.7 Code + config changes that follow the data swap

**Files that reference the old `bybit_fullpit_1h` path:**

| File | Change |
|---|---|
| [docs/data_roots.md](docs/data_roots.md) | Rewrite the "Canonical Research Root" + "Out-of-Sample Roots" sections. Replace with: `bybit_full_pit` + `binance_full_pit` as the two per-venue datasets; explicit "no internal OOS — forward demo is pristine OOS". |
| [configs/volume_alpha.default.yaml](configs/volume_alpha.default.yaml) | `data_root: ~/SHARED_DATA/bybit_full_pit` |
| [scripts/run_fullpit_volume_overnight.sh](scripts/run_fullpit_volume_overnight.sh) | Update `BYBIT_FULLPIT_ROOT` env var default |
| [scripts/backtest_profile.py](scripts/backtest_profile.py) | `--data-root` default |
| [scripts/equity_overlay.py](scripts/equity_overlay.py) | Check for hardcoded paths |
| [liquidity_migration/long_native.py:61-65](liquidity_migration/long_native.py:61) | Remove hardcoded `SPLITS`; either drop split reporting or make split bounds configurable on `LongNativeConfig` (e.g. `splits: tuple[tuple[str, str, str], ...] = ()` defaulting to no splits = whole-period only) |
| [liquidity_migration/volume_events.py](liquidity_migration/volume_events.py) | Same SPLITS treatment if it has its own |
| [.claude/skills/run-strategy/SKILL.md](.claude/skills/run-strategy/SKILL.md) | Update "Pick the right data root" section |
| [.claude/skills/repo-map/SKILL.md](.claude/skills/repo-map/SKILL.md) | Update root references |
| MCP `liqmig-research` `data_roots` tool | Update returned root list |

**Memory updates** (`~/.claude/projects/.../memory/`):

| Memory | Action |
|---|---|
| `oos-vs-walkforward.md` | Delete — concept no longer applies |
| `demo-deployment.md` | No change |
| `strategy-status-contested.md` | No change |
| `cleanup-collaboration-style.md` | No change |
| `feedback-run-ci-before-push.md` | No change |
| Add: `data-root-structure.md` | Describe the new two-root setup; pristine-OOS = forward only |

### A.8 Rollback

- Old roots are intact until you manually `rm -rf` them after gate pass. Reverting is a `git revert` on the config/script changes.
- Mid-build crash: every parquet write is atomic (temp-file + rename). Resume by re-running the failed stage — completed partitions are skipped.
- Catastrophic data drift detected post-deletion: rebuild script is fully reproducible from public archives. ~24h to restore.

### A.9 Estimates

| Stage | Time (local Mac, broadband) | Disk |
|---|---|---|
| Archive old reports | <2 min | <100 MB |
| Bybit klines (5y, ~500 symbols) | 6-10 h | ~5-6 GB |
| Bybit funding/OI/mark/index/premium | 3-6 h | ~3-4 GB |
| Bybit signed flow (optional, via raw trades) | 1-2 h | ~1-2 GB |
| **Bybit total** | **10-18 h** | **~10-12 GB** |
| Binance klines (6y, ~200-400 symbols) | 4-8 h | ~10-15 GB |
| Binance ancillary REST | 3-5 h | ~3-5 GB |
| **Binance total** | **7-13 h** | **~15-20 GB** |
| Verification gates | ~30 min | — |
| **Grand total** | **17-31 h unattended** | **~25-32 GB** |

Disk pre-check: `df -h ~/SHARED_DATA` should show ≥40 GB free (new roots + old roots during overlap + archive tars).

### A.10 Demo coexistence

- Live demo runs on the VPS (singapore 5.223.42.109). Local rebuild does not contend.
- Strategy code still works against the existing old roots during the local rebuild, so research can continue if needed.
- After deletion + config swap, the live demo's `data/bybit-demo-event/` operator root is unaffected (it never points at the research root).
- **Downstream:** when the strategy is later run on the new `bybit_full_pit` root for a future promoted profile, the VPS deployment will need a config push to match. Not in scope for this doc.

### A.11 Open knobs to confirm before kickoff

These had sensible defaults proposed but should be confirmed before someone executes:

1. **Binance start date:** assumed 2019-09-01 (Binance USDM launch). Confirm or override.
2. **Bybit signed_flow inclusion:** assumed yes (~1-2h extra). Confirm or skip.
3. **Bybit REST mainnet vs testnet:** assumed mainnet (testnet has no historical depth). Confirm.
4. **Parallelism:** kline workers 8, ancillary workers 4. Confirm or adjust.
5. **Schedule:** kick off when manually triggered. Confirm timezone preference (US/EU off-peak ≈ 22:00-06:00 UTC).

---

## Part B — Long-native quant-grade punchlist

Six remaining items to take v11a from "exploratory candidate" to "honest candidate with evidence". Each is self-contained and can be done independently except where noted.

### B.1 Fix Sharpe annualisation bug

**Why:** every Sharpe number on every sparse-firing strategy in this repo is inflated 2-3×. Documented in [docs/long_native_findings.md:56-72](docs/long_native_findings.md:56). The repo helper assumes the strategy trades at `365 / hold_days` periods/year, which holds for the short sleeve (158 trades/year ≈ 121 implied) but not the long sleeve (15-30 trades/year vs implied 121).

**Where:** [liquidity_migration/trade_lifecycle.py:114-124](liquidity_migration/trade_lifecycle.py:114)

Current (broken) calculation:

```python
basket_returns = np.asarray(baskets["basket_return"].to_list(), dtype=float)
mean_return = float(np.mean(basket_returns)) if basket_returns.size else 0.0
vol = float(np.std(basket_returns, ddof=1)) if basket_returns.size > 1 else 0.0
annual_periods = 365.0 / config.rebalance_days if config.rebalance_days > 0 else 0.0
# ...
"sharpe_like": float(mean_return / vol * math.sqrt(annual_periods)) if vol > 1e-12 else 0.0,
```

**Fix:** compute daily-aligned Sharpe from the equity curve, not basket-returns-with-assumed-frequency.

```python
def _daily_sharpe(equity: pl.DataFrame) -> float:
    """Sharpe from the daily equity series. Honest across firing frequencies."""
    if equity.is_empty() or "equity" not in equity.columns:
        return 0.0
    eq = equity.sort("date")["equity"].to_numpy()
    if eq.size < 2:
        return 0.0
    daily_ret = np.diff(eq) / eq[:-1]
    daily_ret = daily_ret[np.isfinite(daily_ret)]
    if daily_ret.size < 2:
        return 0.0
    mu, sd = float(daily_ret.mean()), float(daily_ret.std(ddof=1))
    if sd <= 1e-12:
        return 0.0
    return mu / sd * math.sqrt(365.0)
```

Then in `summarize_trade_backtest`:

```python
"sharpe_like": _daily_sharpe(equity),
# Also retain the legacy basket-frequency Sharpe under a new key for backward-compat:
"sharpe_basket_frequency_legacy": float(mean_return / vol * math.sqrt(annual_periods)) if vol > 1e-12 else 0.0,
```

Same fix needed in `_split_rows` in [liquidity_migration/long_native.py:1259-1282](liquidity_migration/long_native.py:1259) — currently uses identical broken formula.

**Test plan:**
- New test `tests/test_trade_lifecycle_sharpe.py`:
  - Equity curve with constant +0.1% daily return → daily Sharpe = (0.001 / 0) → 0 (no vol)
  - Equity curve with realistic sparse trades (10 baskets/year) → assert `sharpe_like` ≠ `sharpe_basket_frequency_legacy`, with the basket-frequency one being ~2-3× higher
- Update [tests/test_trade_lifecycle.py](tests/test_trade_lifecycle.py) expected values
- Update [tests/test_liquidity_migration_long_native.py](tests/test_liquidity_migration_long_native.py) expected values

**Downstream:**
- [docs/long_native_findings.md](docs/long_native_findings.md) — drop the "Sharpe annualisation gotcha" section (becomes historical), add a footnote that the bug is fixed
- Re-run all prior FC sweep summaries — every Sharpe number will drop ~2-3×, which is what the doc already says is the honest value
- Update `_evaluate_promotion` threshold ([liquidity_migration/long_native.py:1298](liquidity_migration/long_native.py:1298)) — currently `avg_sharpe < 1.0` blocks promotion; with honest Sharpe, the threshold should probably drop to 0.7 or be re-derived from the short sleeve's honest Sharpe

**Effort:** 1-2 hours including tests + report regeneration.

### B.2 Validate regime gate empirically

**Why:** the v11a strategy depends on BTC and ETH being above 30d SMA at entry time. What happens to *held* positions when the regime flips mid-trade is not measured. If a meaningful fraction of MaxDD comes from trades that crossed a regime flip, the regime gate isn't doing its job.

**Method:**

1. Identify all BTC 30d SMA crossings (above→below and below→above) on the new `bybit_full_pit` root.
2. For each trade in the v11a backtest, label whether it (a) entered after a `regime_off → regime_on` flip within N days, (b) was held through a `regime_on → regime_off` flip, (c) was never near a flip.
3. Group net trade returns by label. Run a simple t-test (`scipy.stats.ttest_ind`) comparing each group.
4. Repeat with ETH SMA crossings.

**Output:** new section in [docs/long_native_findings.md](docs/long_native_findings.md):

```
## Regime durability (added 2026-XX-XX)

| Cohort | Trades | Mean net return | Median | Win rate | t-stat vs baseline |
|---|---:|---:|---:|---:|---:|
| Entered fresh-regime (≤7d post-flip on) | ... | ... | ... | ... | ... |
| Held through regime-off flip | ... | ... | ... | ... | ... |
| Standard (no flip nearby) | ... | ... | ... | ... | ... |
```

**Where:** new module `liquidity_migration/regime_durability.py` + CLI subcommand `regime-durability` that takes a trade ledger CSV and a klines dataset.

**Test plan:**
- Tests in `tests/test_regime_durability.py` using synthetic BTC kline + trade ledger with known flip points.

**Depends on:** A (new data root makes the sample big enough to be meaningful), B.1 (so the comparison is on honest returns).

**Effort:** 3-5 hours including tests + finding integration.

### B.3 Per-asset and per-sector concentration caps

**Why:** v11a's trade ledger shows top symbols concentrated in 1000PEPE, WIF, SUI, XRP, DOGE. In live deployment with `notional_multiplier=10` and `entry_leverage=10`, five concurrent positions ≈ full margin budget — a correlated drawdown in alt-meme coins can clean out the account.

**Where:** [liquidity_migration/long_native.py](liquidity_migration/long_native.py)

**Config additions** to `LongNativeConfig`:

```python
# --- concentration limits ---
max_per_symbol_concurrent: int = 1        # already implicit via "skipped_already_held"; make explicit
max_per_sector_concurrent: int = 0        # 0 = disabled; e.g. 2 caps meme-coin concurrency at 2
max_per_symbol_weight: float = 0.30       # caps single-symbol weight at 30% of gross
sector_map_path: str | None = None        # optional JSON: {"WIFUSDT": "meme", "ETHUSDT": "L1", ...}
```

**Pipeline change** in `_run_long_pipeline` ([liquidity_migration/long_native.py:1079-1096](liquidity_migration/long_native.py:1079)) — between the existing `skipped_already_held` / `skipped_cooldown` / `skipped_capacity` checks and the entry call, add:

```python
if config.max_per_sector_concurrent > 0 and sector_map:
    cand_sector = sector_map.get(symbol, "unknown")
    held_in_sector = sum(1 for p in open_positions.values()
                         if sector_map.get(p["symbol"], "unknown") == cand_sector)
    if held_in_sector >= config.max_per_sector_concurrent:
        stats["skipped_sector_cap"] += 1
        continue
```

**Sector map**: ship a default at `configs/sector_map.json` covering the top-50 USDT perps. Examples:

```json
{
  "BTCUSDT": "core_l1", "ETHUSDT": "core_l1",
  "SOLUSDT": "smart_contract_l1", "AVAXUSDT": "smart_contract_l1",
  "1000PEPEUSDT": "meme", "WIFUSDT": "meme", "1000SHIBUSDT": "meme", "DOGEUSDT": "meme",
  "XRPUSDT": "payment", "LTCUSDT": "payment", "BCHUSDT": "payment"
}
```

**Test plan:**
- `tests/test_long_native_concentration_caps.py` — synthetic candidates, assert sector cap fires when 2 meme candidates same day

**Sweep to quantify the cost:**
- Re-run the FC v11a backtest on `bybit_full_pit` with `max_per_sector_concurrent=2` and compare net Sharpe / MaxDD vs uncapped. Document in `docs/long_native_findings.md`.

**Effort:** 2-3 hours including tests + sector map + cost-of-cap sweep.

### B.4 Long-side reconcile-paper-demo analyzer

**Why:** the short sleeve has `reconcile-paper-demo` which measures execution slippage by diffing the paper (idealised-fill) ledger against the demo (real-Bybit-fill) ledger. The long sleeve has nothing equivalent. Without it, we can't quantify demo→live execution drift on the long side.

**Where:**
- The short version lives in [liquidity_migration/reconciliation.py](liquidity_migration/reconciliation.py) + CLI parser in [liquidity_migration/cli.py](liquidity_migration/cli.py) (`reconcile-paper-demo` subcommand).
- New: `liquidity_migration/long_reconciliation.py` + CLI subcommand `reconcile-long-paper-demo`.

**Inputs:**
- Demo trade ledger dataset name: `long_native_demo_trades` (per [liquidity_migration/long_native_event_demo.py:95](liquidity_migration/long_native_event_demo.py:95))
- Paper trade ledger: the long sleeve doesn't yet have a paper-shadow runner. Either:
  - **(a)** add a paper runner (mirrors `_long_demo_event_config` but with `submit_orders=False, record_dry_run=True` to a separate `long_native_paper_trades` dataset), OR
  - **(b)** synthesize a paper baseline at reconcile time from the same signal stream the demo saw, fill at signal price.

Option (b) is faster to scaffold but option (a) is structurally cleaner. Recommend (a).

**Output**: same structure as the short reconciler — symbol-level slippage in bps, entry vs exit decomposition, sample-size warnings.

**Effort:** 3-4 hours for option (a), 1-2 hours for option (b). Either way, no useful numbers until the long demo ledger has ≥30 trades (currently has ~0 — demo started 2026-05-22). Scaffold now, run when the ledger ripens.

### B.5 Parameter pre-registration workflow

**Why:** [docs/backtesting_errors_we_never_repeat.md](docs/backtesting_errors_we_never_repeat.md) calls out parameter mining (error #17), OOS reuse (#18), multiple-testing denial (#19). Pre-registration is the standard antidote: write down what you're going to test, expected effect, and which roots you'll touch — before running anything.

**Where:** new doc `docs/parameter_pre_registration.md` + a workflow note in [AGENTS.md](AGENTS.md).

**Template:**

```markdown
# Pre-registration: <change name>

**Date:** YYYY-MM-DD
**Author:** <name>
**Stage:** proposed | run-pending | run-complete | rejected | accepted

## What's changing
Single sentence: e.g. "Lower fc_sigma_mult from 2.5 to 2.0 on v11a."

## Hypothesis
Why this might work. Specific mechanism, not "should improve Sharpe".

## Predicted direction + magnitude
- Sharpe Δ: +/- range
- Trade count Δ: +/- N
- Failure mode if hypothesis wrong: what would falsify

## Roots that will be touched
- [ ] bybit_full_pit (per-venue working dataset)
- [ ] binance_full_pit (per-venue working dataset)
- [ ] forward demo/paper (always, by virtue of being live)

## Decision rule (a priori)
"If post-run Sharpe Δ < +0.3 on either venue OR sign flips between venues, reject."

## Run command
```bash
... exact CLI ...
```

## Post-run results
(fill in after run)

## Verdict
accepted / rejected / inconclusive — with one-paragraph why.
```

**Workflow note for [AGENTS.md](AGENTS.md):**

```markdown
## Parameter pre-registration

Every parameter change that will touch a venue dataset gets a pre-registration
entry under `docs/preregistration/` before the run. The receipt is committed
to git in the same PR as the code change. Skipping pre-registration is
allowed only for `exploratory` runs — those must not be cited as evidence
in any decision to promote, deploy, or accept a parameter as alpha.
```

**Effort:** 30 min (doc + AGENTS.md note + one example pre-reg for an upcoming planned change).

### B.6 ≥150 trades per regime via genuinely-additive patterns

**Why:** v11a fires ~50-100 trades over the full canonical window. Per-split sample size (14-37 baskets) is too small for any Sharpe estimate to be statistically meaningful. The findings doc shows the other 5 patterns (cap rebound, funding squeeze, oversold bounce, volume resurrection, uptrend dip) systematically lose money — but those evaluations are from a *narrower* universe size and pre-sigma-threshold era. Worth a fresh look on the new bigger root.

**Method:**

1. On the new `bybit_full_pit` root, re-run each disabled pattern individually with current ATR/regime/cost framework. Use the same v11a config but flip only one pattern at a time.
2. For any pattern with honest daily Sharpe > 0.5 *and* low correlation to FC trade timing, evaluate a multi-pattern ensemble.
3. Multi-pattern config: enable surviving patterns; portfolio rules unchanged; track per-pattern contribution in the trade ledger (already supported via the `pattern` column).
4. Target: a config that fires ≥150 trades/year combined with honest Sharpe ≥ 1.5.

**Where:** orchestration via a new sweep script `scripts/long_native_sweep_patterns.py` — same shape as `scripts/long_native_sweep_fc_min_day.py` but iterating over which pattern is enabled.

**Risk:** this is the squishiest item. It's pattern selection, which is parameter mining unless pre-registered (see B.5). Each pattern test costs one OOS touch on the new full-PIT roots — which is fine because they're now defined as the working dataset, not OOS, but document the intent.

**Effort:** 4-6 hours including the sweep + ensemble evaluation + write-up.

---

## Sequencing recommendation

Dependencies are mostly soft. Suggested order:

1. **B.1 Sharpe fix** (foundational; all numbers downstream change) — 1-2 h, no data dependency
2. **B.5 Pre-registration workflow** (so steps 3+ are pre-registered) — 30 min
3. **A Data rebuild** (the big bet) — 17-31 h unattended
4. **A code-config swap** + memory updates — 1-2 h
5. **B.2 Regime durability** on new root — 3-5 h
6. **B.3 Concentration caps** + cost-of-cap sweep — 2-3 h
7. **B.4 Long reconcile scaffold** — 3-4 h (results land later as demo ripens)
8. **B.6 Multi-pattern exploration** — 4-6 h (pre-registered)

Total active work: ~16-25 hours engineering + 17-31 hours unattended download. Roughly one focused engineer-week.

---

## What this doc does NOT cover

- Real-money deployment criteria — out of scope. v11a stays demo until B.1-B.6 yield a `candidate` label and the forward demo accumulates ≥3 months of clean PnL.
- Short sleeve work — this doc is long-only. The short sleeve has its own status in [docs/research_findings.md](docs/research_findings.md).
- Infrastructure (VPS deployment, kline WS stack) — handled by separate runbooks under [docs/](docs/).
- The legacy / retired roots in [docs/data_roots.md](docs/data_roots.md) "Retired Roots" section — already addressed there.

---

## Status as of writing

- Long demo running on Singapore VPS 5.223.42.109 since 2026-05-22 with v11a / MultiStratV1 + close 0.30 (short sleeve in parallel)
- Demo ledger: `data/bybit-demo-event/long_native_demo_trades/` — 0 trades so far (signal-light recent window)
- Most recent FC sweep results: `~/SHARED_DATA/{root}/reports/long_native_fc_sweep{,_nosigma}/sweep_summary.md`
- Aggregated cross-root sweep report: `/tmp/long_native_fc_sweep_nosigma_report/long_native_fc_sweep_report.md`

After this work completes, [docs/long_native_findings.md](docs/long_native_findings.md) should be updated with a new dated section reflecting the honest Sharpe + new-root results. [docs/system_status.md](docs/system_status.md) should reflect any change in promotion status.
