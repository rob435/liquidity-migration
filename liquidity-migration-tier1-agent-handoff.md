# Liquidity Migration audit handoff

## Purpose

Index the local audit implementation, verification evidence and retained limitations without implying production deployment.

## Spec Tables

| Document | Authority / scope |
| --- | --- |
| [Audit](docs/tier1-audit.md) | Current resolutions for A-001–A-004, all 22 CL findings and all 30 original LM-T1 tickets |
| [Baseline verification](docs/tier1-audit-verification.json) | Accepted f69a audit source, compiler and original exit-loss reproduction |
| [Resolution evidence](docs/tier1-audit-resolution.json) | Fail-before probes, candidate checks, exact reducer comparisons, compatibility scope and build measurements |
| [Engine specification](docs/engine.md) | Current core/worker/venue/public ownership, durability and producer protocol |
| [CHANGELOG.md](CHANGELOG.md) | Dated local checkpoints and verification outcomes |
| [STATE.md](STATE.md) | Operational snapshot; local work does not establish current deployed behavior |
| [Research governance](docs/research/governance.md) | Engineering checks do not promote a strategy or arm funds |

| Boundary | Implemented resolution | Remaining limitation |
| --- | --- | --- |
| A-001/A-004 | Retained cooperative effects, caller-bound order/stop operations, durable stateful transitions and restart suffix | Trusted synchronous callbacks and output allocation are not globally bounded; ordinary order-only callbacks retain optimistic durability |
| A-002/A-003 | Consumed/rejected/retained input outcomes, accepted-payload backpressure, prefix slot and fresh producer frontier | Historical identity metadata remains; erased legacy history cannot be reconstructed |
| CL-01–CL-15 | Typed selected wire/error/output boundaries, public/private capabilities and explicit admission/completion/boot/worker/reducer phases | Dynamic inner venue rows and broad later accounting handlers remain; file moves are not presented as architecture fixes |
| CL-16–CL-22 | Pure risk targets share one process, workspace dependencies preserve resolved features, ownership maps are current | Venue environment tests remain separate processes; no unsupported whole-workspace performance claim |
| Portfolio / numeric proposals | Retain exclusive symbols, dense durable identity and current quantization/accounting | Reconsider with a concrete shared-symbol or adapter requirement and coherent allocation/fees/stops/legacy migration |
| Operations | Local engineering checks and checkpoints | No funded deployment, credentials, capital, host permissions or live-state changes |

## Invariants

- Must retain the full audit scope and preserve unrelated work.
- Must preserve WAL compatibility, reconciliation, dense ID agreement, protective stops and reductions.
- Must distinguish implementation, deliberate retention, unimplemented capability and measured performance.
- Must require a fail-before/pass-after regression for each behavioral fix; baseline-compatible and candidate-fault probes have separate source scopes.
- Must preserve test discovery and assertions; engineering equivalence covers tested inputs, not live account parity.
- Must not infer funded authorization from the audit, local checks or owner-granted architecture authority.

## Operational Recipes

Run from the repository root; these checks are local and do not consume GitHub Actions minutes.

```bash
git status --short --branch
git log -3 --oneline
audit_rust_bin="$(dirname "$(rustup which --toolchain 1.90.0 rustc)")"
export PATH="$audit_rust_bin:$PATH"
export RUSTC="$audit_rust_bin/rustc"
export RUSTDOC="$audit_rust_bin/rustdoc"
cargo test --manifest-path engine/Cargo.toml --workspace --all-targets --locked
cargo test --manifest-path engine/Cargo.toml --workspace --doc --locked
cargo test --manifest-path engine/Cargo.toml --workspace --all-targets --release --locked
cargo test --manifest-path engine/Cargo.toml --workspace --doc --release --locked
scripts/dev.sh check
```
