#!/usr/bin/env python3
"""Install Codex companion tools for this repo and future checkouts.

This script intentionally keeps these tools outside the trading runtime
dependencies. They are operator/agent tools, not bot dependencies.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


CODEX_SKILLS = {
    "ComposioHQ/awesome-codex-skills": {
        "ref": "master",
        "paths": [
            "gh-fix-ci",
            "sentry-triage",
            "changelog-generator",
            "connect",
        ],
    },
    "JuliusBrussee/caveman": {
        "ref": "main",
        "paths": [
            "skills/caveman",
            "skills/compress",
            "skills/caveman-commit",
            "skills/caveman-review",
            "skills/caveman-help",
        ],
    },
}

NPM_PREFIX = Path.home() / ".local" / "share" / "npm"
USER_PY_BIN = Path.home() / "Library" / "Python" / f"{sys.version_info.major}.{sys.version_info.minor}" / "bin"


def run(cmd: list[str], *, check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd, check=check, text=True)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def install_skills(overwrite: bool) -> None:
    skills_root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)

    for repo, spec in CODEX_SKILLS.items():
        ref = spec["ref"]
        archive_url = f"https://github.com/{repo}/archive/{ref}.zip"
        print(f"Downloading skills from {repo}@{ref}")
        with tempfile.TemporaryDirectory(prefix="codex-skills-") as tmp:
            archive = Path(tmp) / "repo.zip"
            urllib.request.urlretrieve(archive_url, archive)
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tmp)
            roots = [p for p in Path(tmp).iterdir() if p.is_dir()]
            if not roots:
                raise RuntimeError(f"No extracted root for {repo}")
            repo_root = roots[0]

            for rel in spec["paths"]:
                src = repo_root / rel
                if not src.exists():
                    raise RuntimeError(f"Missing skill path {repo}:{rel}")
                dst = skills_root / src.name
                if dst.exists():
                    if not overwrite:
                        print(f"Skill exists, skipping: {dst.name}")
                        continue
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                print(f"Installed skill: {dst}")


def install_graphify(repo_root: Path) -> None:
    run([sys.executable, "-m", "pip", "install", "--user", "graphifyy"])
    run([sys.executable, "-m", "graphify", "install", "--platform", "codex"])
    run([sys.executable, "-m", "graphify", "codex", "install"], cwd=repo_root)

    spec = importlib.util.find_spec("graphify")
    if spec and spec.origin:
        skill_src = Path(spec.origin).parent / "skill-codex.md"
        skill_dst = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills" / "graphify" / "SKILL.md"
        skill_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_src, skill_dst)
        print(f"Installed graphify Codex skill mirror: {skill_dst}")


def install_ao() -> None:
    if not command_exists("npm"):
        print("npm not found; skipping Agent Orchestrator install.")
        return
    run(["npm", "install", "-g", "--prefix", str(NPM_PREFIX), "@aoagents/ao"])


def install_composio() -> None:
    if command_exists("composio") or (Path.home() / ".composio" / "composio").exists():
        print("Composio CLI already present.")
        return
    run(["bash", "-lc", "curl -fsSL https://composio.dev/install | bash"])


def print_status() -> None:
    print("\nStatus:")
    tools = {
        "codex": "Codex CLI",
        "gh": "GitHub CLI, needed for gh-fix-ci and AO GitHub workflows",
        "tmux": "tmux, needed by AO default runtime",
        "npm": "npm, needed for AO",
        "composio": "Composio CLI, needed for connect and sentry-triage",
        "graphify": "Graphify CLI",
        "ao": "Agent Orchestrator CLI",
    }
    extra_bins = [str(NPM_PREFIX / "bin"), str(USER_PY_BIN), str(Path.home() / ".composio")]
    env_path = os.environ.get("PATH", "")
    path = os.pathsep.join(extra_bins + [env_path])
    for cmd, note in tools.items():
        found = shutil.which(cmd, path=path)
        marker = "ok" if found else "missing"
        print(f"- {marker}: {cmd} ({note})")

    print("\nAdd these to PATH if your shell cannot find ao/graphify/composio:")
    print(f"  export PATH=\"{NPM_PREFIX / 'bin'}:{USER_PY_BIN}:$HOME/.composio:$PATH\"")
    print("\nManual auth still required:")
    print("  gh auth login")
    print("  composio login")
    print("  composio link github")
    print("  composio link sentry")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install Codex companion tools.")
    parser.add_argument("--no-overwrite-skills", action="store_true", help="Do not replace existing skill folders.")
    parser.add_argument("--skip-skills", action="store_true", help="Skip Codex skill installation.")
    parser.add_argument("--skip-graphify", action="store_true", help="Skip graphify install and repo hook setup.")
    parser.add_argument("--skip-ao", action="store_true", help="Skip Agent Orchestrator install.")
    parser.add_argument("--skip-composio", action="store_true", help="Skip Composio CLI install.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]

    if not args.skip_skills:
        install_skills(overwrite=not args.no_overwrite_skills)
    if not args.skip_graphify:
        install_graphify(repo_root)
    if not args.skip_ao:
        install_ao()
    if not args.skip_composio:
        install_composio()
    print_status()


if __name__ == "__main__":
    main()
