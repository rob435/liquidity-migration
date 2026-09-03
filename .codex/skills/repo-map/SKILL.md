---
name: repo-map
description: Orient in the liquidity-migration codebase, locate ownership, and trace cross-module behavior. Use for architecture, where-does-this-live, dependency-path, or cross-module change questions. Verify every material claim against current source, tests, runtime config, and artifacts; do not treat this skill as authority.
---

# Repository Navigation Map

## 1. Purpose
Define repository ownership, language boundaries, authority hierarchy, and cross-module entry points across the trading engine, signal worker, research lab, and operational tooling.

---

## 2. Spec Tables

### System Architecture & Codebase Ownership

| Subsystem | Primary Path | Language | Ownership & Scope | Source Authority |
| :--- | :--- | :--- | :--- | :--- |
| **Execution Engine** | `engine/engine-core/` | Rust | Durable state machine, order admission, WAL, scheduler, risk kernel. | Primary for execution logic. |
| **Venue Adapters** | `engine/engine-venue/` | Rust | Authenticated venue WebSocket/REST I/O, account leases, signing. | Primary for venue interaction. |
| **Strategy Reducers** | `engine/engine-strategies/` | Rust | Native pure reducers (CARRY, LONG, EXODUS, MAKER). | Primary for decision logic. |
| **Signal Worker** | `engine/signal-worker/` | Rust | Feature calculation, ticker normalizer, klines, signal spooling. | Primary for signal generation. |
| **Market Tape** | `market_tape/` | Python | High-fidelity trade/order-book recording, Drive archival, Parquet bars. | Primary for market data recording. |
| **Research Lab** | `liquidity_migration/research/` | Python | Point-in-time backtesting, panel generation, strategy simulation. | Descriptive research only. |
| **Data Ingestion** | `liquidity_migration/data/` | Python | Historical data roots, PIT manifests, kline/funding storage. | Primary for PIT historical data. |
| **Operations CLI** | `scripts/ops.sh` | Bash | Fleet lifecycle, status, deploy, flatten, attestation. | Primary operator interface. |
| **Dev / Build CLI** | `scripts/dev.sh` | Bash | Test runner, linting, toolchain doctor, CI gate replication. | Developer workflow entry point. |

### Operational Truth Hierarchy

| Precedence | Document / Artifact | Scope & Authority | Invariant |
| :---: | :--- | :--- | :--- |
| **1** | Code, tests, deploy files, WAL | Implemented behavior and live execution reality. | Overrules all documentation. |
| **2** | `STATE.md` | Operational snapshot of live services, commits, and dials. | Current truth; dated history in `CHANGELOG.md`. |
| **3** | `docs/research/governance.md` | Evidence grading rules, registration, and promotion standards. | Binds all research claims. |
| **4** | `docs/research/research_findings.md` | Durable index of established research findings and negative priors. | Decision-useful research index. |
| **5** | `.codex/skills/` (`.claude/skills/`) | Navigation aids and workflow recipes. | Never factual authority; verify against source. |

---

## 3. Invariants

- **Must Never Rely on Narrative History**: Verify current code, unit tests, and live configuration directly; do not cite deleted modules or historical assertions.
- **Must Respect Module Boundaries**: Python may replay Rust contracts for research, parse the WAL, or generate reports, but Python *must never* decide or submit live trading orders.
- **Must Preserve Unrelated Work**: Always preserve uncommitted work in dirty trees; inspect git diffs before running destructive operations.
- **Must Verify Local Tests Before Remote Deploy**: Always run `cargo test -p <crate>` and `scripts/dev.sh test` locally prior to VPS deployment.

---

## 4. Operational Recipes

### Codebase Health & Diagnostic Doctor
```bash
# Comprehensive diagnostic of Git, Python, virtualenv, skills, and deploy toggles
scripts/dev.sh doctor

# JSON output for machine parsing
scripts/dev.sh doctor --json
```

### Full Quality Gate (Lints, Types, Tests)
```bash
# Run Ruff, ShellCheck, mypy, pytest, and Rust engine verification
scripts/dev.sh check

# Fast Rust engine test suite (~3 seconds)
cargo test --manifest-path engine/Cargo.toml
```

### Fast Codebase Discovery
```bash
# Find files by name pattern
rg --files | grep -E 'strategy|signal'

# Search exact identifiers across codebase
rg 'signal_spool_path' engine/ docs/ configs/
```
