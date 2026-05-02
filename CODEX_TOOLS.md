# Codex Companion Tools

This repo keeps agent tooling outside the research runtime. The alpha code should not depend on GitHub, Composio, Graphify, or AO packages.

## Installed Here

- Codex CLI: present as the app-managed `codex` command.
- Composio skills in `~/.codex/skills`: `gh-fix-ci`, `sentry-triage`, `changelog-generator`, `connect`.
- Caveman skills in `~/.codex/skills`: `caveman`, `compress`, `caveman-commit`, `caveman-review`, `caveman-help`.
- Graphify: installed from the official `graphifyy` PyPI package. The CLI command is `graphify`.
- Agent Orchestrator: installed as `ao` under `~/.local/share/npm/bin`.
- Composio CLI: installed under `~/.composio`.
- GitHub CLI and tmux: installed with Homebrew for CI/AO workflows.

If `ao`, `graphify`, or `composio` are not found in a fresh shell, add:

```bash
export PATH="$HOME/.local/share/npm/bin:$HOME/Library/Python/3.11/bin:$HOME/.composio:$PATH"
```

## Reuse In Future Repos

Copy or run this from any checkout:

```bash
python deploy/setup_codex_tools.py
```

That installs or updates the same global Codex skills, installs Graphify, installs AO into a user-owned npm prefix, installs the Composio CLI, and writes Graphify's Codex instructions into the current repo.

Use skip flags when a repo only needs part of the stack:

```bash
python deploy/setup_codex_tools.py --skip-ao
python deploy/setup_codex_tools.py --skip-composio
python deploy/setup_codex_tools.py --no-overwrite-skills
```

## How To Use

`gh-fix-ci`: ask Codex to fix a failing GitHub Actions PR check. Requires `gh auth login`. Example: "Use gh-fix-ci on PR 123 and summarize the failing logs before editing."

`sentry-triage`: ask Codex to inspect a Sentry issue. Requires `composio login` and `composio link sentry`. Example: "Use sentry-triage on PROJ-1F4 and map the stack frames to this repo."

`changelog-generator`: ask for release notes from git history. Example: "Generate a changelog since the last tag, customer-facing, no internal refactor noise."

`connect`: ask Codex to perform a real app action through Composio. Requires `composio login` plus `composio link <toolkit>`, such as `github`, `slack`, `notion`, or `gmail`.

`caveman`: opt-in only. Use it when quota is tight or you want terse answers. Example: "Use caveman lite for the rest of this thread." Do not make it the default for trading-system design reviews; compressed language can hide important risk detail.

`graphify`: build a codebase map, then query it before broad grep-heavy exploration.

```bash
graphify update .
graphify query "how does volume alpha use kline data?"
graphify explain "run_volume_alpha"
```

In Codex, the skill trigger is `$graphify .`. If the command is not on PATH, use `python3 -m graphify ...`.

`ao`: run parallel agent sessions across worktrees. Requires `tmux` and `gh` for the default useful path.

```bash
ao doctor
ao start .
```

This repo has [agent-orchestrator.yaml](agent-orchestrator.yaml) configured for Codex workers, tmux runtime, GitHub tracking, and worktree isolation. It intentionally does not symlink `.env` into worker worktrees.

## Auth Still Required

Local binaries are installed, but account auth is still yours to perform:

```bash
gh auth login
composio login
composio link github
composio link sentry
```

AO is installed and `ao status` runs. `ao doctor` currently errors in the packaged `@aoagents/ao@0.3.0` build because it looks for a missing bundled `scripts/ao-doctor.sh`; do not treat that as proof the CLI itself is absent.

## Reality Check

The star counts in the original tool list were not all accurate when checked on May 1, 2026. Treat these tools as useful because of capability, not because of quoted popularity.
