---
name: pit-reconcile
description: Assess current engine-WAL, authenticated venue, PIT, and model evidence. Use for WAL/venue mismatches, fill or P&L evidence, archive_trade_manifest coverage, pit_membership_fail, or execution-accounting claims. Keep PIT validity separate from operational authorization and never treat projections as account authority.
---

# Point-in-Time & Account Reconciliation

## 1. Purpose
Define the reconciliation protocol, verification hierarchy, and diagnostic workflows for resolving discrepancies between the engine WAL, authenticated exchange state, and Point-in-Time historical datasets.

---

## 2. Spec Tables

### State Authority Hierarchy

| Precedence | Domain / Layer | Primary Artifact | Authority Scope | Non-Authoritative Derivatives |
| :---: | :--- | :--- | :--- | :--- |
| **1** | **Exchange Venue** | Authenticated REST/WS API | Final truth on open positions, cash balance, and execution IDs. | Local trade approximations, order estimates. |
| **2** | **Engine Core WAL** | `engine.wal` (append-only log) | Authoritative local record of sent orders, acknowledged fills, and state. | Strategy parquet, dashboard metrics, Telegram alerts. |
| **3** | **Strategy Projections**| Heartbeats, trade JSONL, logs | Ephemeral operational view of sleeve state. | Cannot overrule WAL or venue. |
| **4** | **PIT Data Roots** | Archive trade & kline manifests | Authoritative historical universe membership at timestamp $T$. | Current-listing inferences, live ticker snapshots. |

### Reconciliation Tooling Reference

| Tool | Syntax | Scope | Role |
| :--- | :--- | :--- | :--- |
| **Flat Attestation** | `scripts/ops.sh attest-flat --environment <realm>` | Venue Account | Two-scan proof verifying the entire account holds zero exposure. |
| **WAL Replay** | `engine replay --wal <path>` | Engine WAL | Replays deterministic state transitions and identifies pending items. |
| **Fills Audit** | `engine fills --wal <path>` | Engine WAL | Computes exact fees, maker share, arrival shortfall, and closed P&L. |
| **Venue Capture** | `python scripts/research/capture_bybit_account_history.py` | Venue API | Authenticated capture of exchange-side executions and balance history. |
| **Venue-WAL Join**| `python scripts/research/reconcile_venue_wal.py` | Venue + WAL | Cross-reconciliation of WAL orders against venue execution IDs. |
| **PIT Manifest** | `python -m liquidity_migration archive-manifest` | Dataset Root | Rebuilds and verifies strict PIT universe listing manifests. |

### Discrepancy Failure Modes & Remediation

| Symptom | Probable Cause | Action Protocol |
| :--- | :--- | :--- |
| **WAL / Venue Mismatch** | Unfilled resting order, venue-side liquidation, or manual order. | Stop engine; run `engine attest-flat`; join WAL against venue history. |
| **May-Open Latch Blocked**| Engine detected unreconciled positions on boot. | Run `engine reconcile-clear --execute` after verifying venue flat. |
| **Missing PIT Rows** | Kline gaps, unlisted tokens, or coverage boundary error. | Rebuild manifest with end-exclusive date; mark gaps as non-gradeable. |
| **Double-Counted Fee** | Strategy applied fee before engine WAL recorded venue execution. | Rely solely on `engine fills` venue execution report. |

---

## 3. Invariants

- **Must Never Rely on Projections as Truth**: Heartbeats, Parquet files, and status messages are secondary projections; the engine WAL and venue API *must* be consulted for accounting proof.
- **Must Never Overwrite Contradictions**: When WAL and venue disagree, preserve the conflicting records and stop the writer; *must never* forcibly wipe or overwrite the WAL to mask an error.
- **Must Treat Manifest Ends as Exclusive**: In all archive manifest builds, `--end` dates are strictly *exclusive* (`[start, end)`).
- **Proving Flatness Does Not Prove P&L**: `attest-flat` confirms zero current positions; it *must not* be cited as proof of historical fills, fees, or realized profitability.

---

## 4. Operational Recipes

### Verify Credential Flatness on Host
```bash
# Attest demo account is completely flat across all symbols
scripts/ops.sh attest-flat --environment demo

# Attest funded account is flat
scripts/ops.sh attest-flat --environment mainnet
```

### Audit Engine WAL Fills & P&L
```bash
# Decode engine WAL history and open exposures
/opt/liquidity-migration-engine/bin/engine replay \
  --wal /var/lib/liquidity-migration-engine-mainnet/engine.wal

# Report exact executed fees, slippage, and realized trade P&L
/opt/liquidity-migration-engine/bin/engine fills \
  --wal /var/lib/liquidity-migration-engine-mainnet/engine.wal
```

### Rebuild Historical PIT Universe Manifest
```bash
# Build Point-in-Time symbol manifest for date window (end is exclusive)
python -m liquidity_migration --data-root ~/SHARED_DATA/bybit_full_pit archive-manifest \
  --start 2024-01-01 --end 2025-01-01
```
