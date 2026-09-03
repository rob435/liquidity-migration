# liquidity-migration

Quantitative research, market data capture, and low-latency algorithmic trading platform for crypto perpetuals (Bybit & Binance).

---

## 1. Core Subsystems

| Subsystem | Primary Tech | Authority & Mandate | Entry Point |
| :--- | :--- | :--- | :--- |
| **Trading Engine** | Rust (`engine/`) | Low-latency single-threaded execution loop, WAL ledger, risk kernel, venue orders | [`docs/engine.md`](docs/engine.md) |
| **Signal Worker** | Rust (`engine/signal-worker/`) | Credential-free public data ingestion, feature computation, AF_UNIX streaming | [`docs/architecture.md`](docs/architecture.md) |
| **Market Tape** | Rust / Python (`market_tape/`) | High-throughput tick/L2 recording, zstd compression, Google Drive archive | [`market_tape/README.md`](market_tape/README.md) |
| **Operations** | Shell / Python (`scripts/`) | Deployment orchestration, safety stops, liveness monitoring, Telegram alerts | [`docs/operations.md`](docs/operations.md) |

---

## 2. Active Strategy Sleeves

| Sleeve | Rule | Strategy ID | State | Core Strategy Profile |
| :--- | :--- | :---: | :--- | :--- |
| **CARRY** | `configs/lane2_carry_hold_v7.json` | `0` | Active | Sticky 48h hold on extreme negative funding crowd fees ($\le -10\text{ bp}$). |
| **LONG** | `configs/long_native_v12.json` | `1` | Active | Momentum breakouts on top liquid perpetuals with decaying ATR stops. |
| **EXODUS** | `configs/lane2_exodus_short_v1.json` | `2` | Active | Event-driven short on distressed CARRY pairs prior to settlement. |
| **MAKER** | `configs/lane2_toxic_flow_quoter_v1.json` | `3` | Disabled | Microstructural two-sided quoting canary (`quote_enabled = false`). |

---

## 3. Quick Start & Development Checks

```bash
# Diagnostic environment check
scripts/dev.sh doctor

# Full codebase verification (formatting, lints, rust tests, python tests)
scripts/dev.sh check

# Fast targeted tests
pytest -q
cargo test --manifest-path engine/Cargo.toml --workspace --all-targets
```

---

## 4. Documentation Index (Spec-First Architecture)

| Document | Primary Contents |
| :--- | :--- |
| **[STATE.md](STATE.md)** | Operational snapshot: host specifications, deployed commit, armed status, unit heartbeats. |
| **[CHANGELOG.md](CHANGELOG.md)** | Historical operational ledger: deployments, incidents, repairs, and migrations. |
| **[docs/architecture.md](docs/architecture.md)** | System topology, Demo/Mainnet realms, AF_UNIX IPC, durability barriers, trade diagnostics. |
| **[docs/engine.md](docs/engine.md)** | Engine internals: crate map, boot sequence, memory budget, risk kernel, takeover protocol. |
| **[docs/operations.md](docs/operations.md)** | Runbook: VPS specifications, `scripts/ops.sh` command reference, deploy/rollback recipes. |
| **[docs/data.md](docs/data.md)** | Data roots, tape capture tiers, 1,300 GB byte budget, shedding order, timestamp semantics. |
| **[docs/trading_logic.md](docs/trading_logic.md)** | Strategy rules: entry/exit formulas, universes, sizing multipliers, LLM entry gate. |
| **[docs/notifications.md](docs/notifications.md)** | Telegram surfaces, trade alert schemas, liveness matrices, interactive bot commands. |
| **[docs/strategy_template.md](docs/strategy_template.md)** | Developer contract and boilerplate for implementing new native Rust strategies. |
| **[docs/research/governance.md](docs/research/governance.md)** | Progressive Evidence Model: Lane-1 exploration vs Lane-2 promotion rules. |
| **[docs/research/research_findings.md](docs/research/research_findings.md)** | Empirical research findings and historical evidence log. |
