# Python Research & Operations Package (`liquidity_migration`)

Python research plane, data pipeline, and operational observability package. **Has zero live order or execution authority.**

---

## 1. Package Architecture

| Subpackage | Primary Responsibility |
| :--- | :--- |
| **`core/`** | Time math, typed configs, durable JSON/parquet serializers, venue realm types. |
| **`marketdata/`** | Public historical downloads, REST clients, and raw data caches. |
| **`data/`** | Point-in-time universe manifests, dataset partitions, and coverage audits. |
| **`rules/`** | Registered JSON rule loaders, takeover source decoders, Rust replay clients. |
| **`research/`** | Factor backtesting, study harness (`lab/`), metrics, and evidence generation. |
| **`policy/`** | Preflight checks, credential validation, and operational profile rendering. |
| **`ops/`** | Telegram notifications, liveness checkers, and operator command helpers. |
| **`cli/`** | CLI dispatcher (`python -m liquidity_migration`). |

---

## 2. Layered Import Dependency Ranks

Enforced strictly by `tests/repo/test_import_order.py`:

```text
Rank 1: core/
Rank 2: marketdata/
Rank 3: data/
Rank 4: rules/
Rank 5: research/
Rank 6: policy/
Rank 7: ops/
Rank 8: cli/
```
* **Strict Rule**: A lower layer may **never import from a higher layer**. Registered rules cannot depend on research engines. Absolute imports are mandatory.

---

## 3. Data Flow & Read-Only Seams

```text
Historical Data ────────> Python Research ───> Rust strategy_contract ───> Evidence Report
Public Tape ────────────> Immutable zstd archive (Google Drive)
Engine WAL / Trades ────> Python Liveness & Telegram Notifier
Operator Telegram ──────> Sudo Helper ───────> Engine Control Spool
```
