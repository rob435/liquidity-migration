# Model Context Protocol (MCP) Specification

## 1. Purpose
Define Model Context Protocol (MCP) server integration schemas, tool registries, security permissions, and transport configurations for AI agent harnesses operating in this repository.

---

## 2. Spec Tables

### MCP Server & Tool Registry

| Server Name | Transport | Runtime Command | Available Tools | Authority / Scope | Permission Policy |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`liqmig-ops`** | stdio | `python -m liquidity_migration.cli.mcp_ops` | `get_fleet_status`, `check_heartbeats`, `read_wal_head` | VPS & local fleet inspection. | Read-only; no manual approval needed. |
| **`liqmig-research`**| stdio | `python -m liquidity_migration.cli.mcp_research`| `run_backtest_slice`, `query_pit_manifest`, `inspect_era_metrics` | Historical PIT research data. | Read-only; isolated data roots. |

### Configuration File Locations

| Environment / Client | Configuration File | Schema Format | Role |
| :--- | :--- | :--- | :--- |
| **Claude Code / Desktop** | `.mcp.json` (repo root) | JSON (`mcpServers`) | Project-scoped MCP server declarations. |
| **Claude Local Settings** | `.claude/settings.local.json` | JSON | Local permission grants and active server toggles. |
| **Codex CLI** | `.codex/config.toml` | TOML (`[mcp_servers]`) | Codex sandbox policy and tool transport hooks. |
| **Antigravity** | `.gemini/antigravity/` | JSON / YAML | IDE-level agent tool bindings. |

### Security & Permission Policy

| Operation Category | MCP Capability | Allowed Behavior | Invariant |
| :--- | :--- | :--- | :--- |
| **Account Read** | `read_account_state` | Query open orders, positions, and balances. | Permitted on demo and mainnet in read-only mode. |
| **Order Placement** | N/A | **Prohibited via MCP**. | All order placement must go through engine WAL and risk kernel. |
| **Systemd Mutation** | N/A | **Prohibited via MCP**. | Mutating operations must use `scripts/ops.sh` or `scripts/deploy_vps_live.sh`. |
| **Historical Data** | `query_pit_manifest` | Inspect point-in-time universe and kline dates. | Access restricted to read-only directories (`~/SHARED_DATA/`). |

---

## 3. Invariants

- **Must Never Expose Mutating Trade Endpoints Over MCP**: MCP servers *must never* implement methods that submit, cancel, or amend orders directly at the venue; all live orders are gated by the engine risk kernel.
- **Must Default to Read-Only Stdio Transport**: Standard input/output (`stdio`) is the only authorized transport; network-listening HTTP/SSE MCP endpoints *must not* be opened on the production host.
- **Must Isolate Credentials from Tool Context**: MCP tools *must never* return raw API secrets, private keys, or credentials in tool execution results.

---

## 4. Operational Recipes

### Verify MCP Configuration Syntax
```bash
# Validate .mcp.json configuration file syntax
python3 -m json.tool .mcp.json > /dev/null && echo "MCP JSON: Valid"
```

### Inspect Configured Permissions
```bash
# Verify allowed command patterns in Claude settings
jq '.permissions.allow' .claude/settings.local.json
```
