# Liquidity Migration audit handoff

## Purpose

Index the local audit implementation, verification evidence and retained limitations without implying production deployment.

## Spec Tables

| Current task | Status |
| --- | --- |
| Full architecture completion and shared tickers | Active; the owner authorizes same and opposing virtual sleeves and local root-cause decisions |
| Checkpoint evidence below | Applies to `584844fa`; it does not qualify the changing worktree |
| Work owners | Root: portfolio/risk/identity/integration; effects: isolated callbacks/durability; inputs: sealed generations/recovery; venues: exact numbers/typed wire contracts |
| Completion | Requires failing-before/passing-after regressions and new integrated local debug/release/developer suites |

| Document | Authority / scope |
| --- | --- |
| [Audit](docs/tier1-audit.md) | Current resolutions for A-001–A-004, all 22 CL findings and all 30 original LM-T1 tickets |
| [Baseline verification](docs/tier1-audit-verification.json) | Accepted f69a audit source, compiler and original exit-loss reproduction |
| [Resolution evidence](docs/tier1-audit-resolution.json) | Fail-before probes, candidate checks, exact reducer comparisons, compatibility scope and build measurements |
| [Engine specification](docs/engine.md) | Current core/worker/venue/public ownership, durability and producer protocol |
| [CHANGELOG.md](CHANGELOG.md) | Dated local checkpoints and verification outcomes |
| [STATE.md](STATE.md) | Operational snapshot; local work does not establish current deployed behavior |
| [Research governance](docs/research/governance.md) | Engineering checks do not promote a strategy or arm funds |

| Boundary | Implemented local behavior | Remaining work |
| --- | --- | --- |
| Callback execution | Registered native runtime runs in child processes; complete private state/effect transactions; asynchronous durability; Linux memory/CPU/process bounds | Global retained-state pools, bounded process concurrency and durable retry of full callback inboxes are isolated pending integration |
| Every order dispatch | Atomic order/outbox acceptance, attempted marker durable before wire, independent read-only lookup lane, ambiguous outcomes block growth | Shared physical order translation and exact owned reduction recheck |
| Producer lifecycle | Managed epochs, sealed tails, durable terminal consumption, bounded retirement and exact blocked route demand | Named sleeve binding across config reorder; erased legacy tails remain explicit unknowns |
| Virtual inventory/accounting | Exact per-sleeve quantities, basis and asset fees; prepared atomic allocation of real emergency fills; serialized rotation/recovery | Shared admission, independent stops, emergency cancellation and internal offset settlement |
| Portfolio risk | Separate portfolio API counts opposing gross and owning-sleeve exits | Integrate tested numeric corrections, pending margin ownership and core admission |
| Venue numeric boundaries | Lexical execution values and exact instrument/order terms for five supported adapters; explicit unavailable metadata | Typed amend/standalone stop path and remaining dynamic account/reference rows |
| Durable identities | Pure namespaced registry migration and dense-slot preservation tested separately | Wire assembly, boot/rotation/dynamic admission, passive removed sleeves and producer binding |
| Verification | macOS 1,997 / Linux 2,000 workspace debug tests, Python 1,499 and strict Clippy pass; [foundation evidence](docs/tier1-foundation-evidence.json) preserves regression scopes | Final integrated debug/release/developer suites remain required |
| Operations | All work is local | No push, funded deployment, credentials, capital or live-state mutation |

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
