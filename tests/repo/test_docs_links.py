"""Every markdown link in the tracked tree must resolve.

The docs are a cross-referenced web; a file move breaks inbound links
silently and the break is found weeks later by a reader. This ran by hand
twice during the 2026-08-03 docs redesign; now it runs in every gate.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_SKIP_PREFIXES = ("http://", "https://", "mailto:", "#", "~")


def _tracked_markdown() -> list[Path]:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", "*.md"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    return [ROOT / name for name in completed.stdout.split("\0") if name]


def test_every_markdown_link_resolves() -> None:
    files = _tracked_markdown()
    assert files, "git ls-files returned no markdown files"
    dangling: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _LINK.finditer(text):
            target = match.group(1)
            if target.startswith(_SKIP_PREFIXES):
                continue
            target = target.split("#", 1)[0]
            if not target:
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                line = text[: match.start()].count("\n") + 1
                dangling.append(f"{path.relative_to(ROOT)}:{line} -> {target}")
    assert not dangling, "dangling markdown links:\n" + "\n".join(dangling)


# Docs name repo paths in backticks far more often than they link them, and a
# moved module leaves the claim behind. CHANGELOG.md is exempt: history names
# what a change replaced, and those paths are meant to be gone.
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_REPO_PATH = re.compile(
    r"^(?:configs|data|deploy|docs|engine|liquidity_migration|market_tape|scripts|tests)"
    r"/[A-Za-z0-9_./@-]+$"
)
_PATH_CLAIM_EXEMPT = frozenset({"CHANGELOG.md"})


def test_every_repo_path_a_doc_names_exists() -> None:
    missing: list[str] = []
    for path in _tracked_markdown():
        if str(path.relative_to(ROOT)) in _PATH_CLAIM_EXEMPT:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _INLINE_CODE.finditer(text):
            claim = match.group(1).strip()
            if not _REPO_PATH.match(claim) or (ROOT / claim).exists():
                continue
            line = text[: match.start()].count("\n") + 1
            missing.append(f"{path.relative_to(ROOT)}:{line} -> {claim}")
    assert not missing, "docs name repo paths that do not exist:\n" + "\n".join(missing)


# Same rot, one level up: a doc naming an operator verb the router does not
# implement sends a reader to a usage error in the middle of an incident.
_OPS = ROOT / "scripts" / "ops.sh"
_OPS_CALL = re.compile(r"scripts/ops\.sh\s+([a-z][a-z-]*)")
_FENCE = re.compile(r"```[a-z]*\n(.*?)```", re.S)


def _ops_verbs() -> set[str]:
    body = _OPS.read_text(encoding="utf-8")
    start = body.index('case "$command" in')
    case = body[start : body.index("\nesac", start)]
    verbs: set[str] = set()
    for match in re.finditer(r"^  ([a-z|_-]+(?:\|[a-z|_-]+)*)\)", case, re.M):
        verbs.update(match.group(1).split("|"))
    assert "deploy" in verbs and "status" in verbs, "the router's case arms did not parse"
    return verbs


def test_every_operator_verb_a_doc_names_is_one_the_router_has() -> None:
    verbs = _ops_verbs()
    unknown: list[str] = []
    for path in _tracked_markdown():
        if str(path.relative_to(ROOT)) in _PATH_CLAIM_EXEMPT:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        # Command spans only. Prose mentioning the router is not a claim about
        # a verb: "invoking scripts/ops.sh so data roots come from help".
        for span in [*_INLINE_CODE.finditer(text), *_FENCE.finditer(text)]:
            for match in _OPS_CALL.finditer(span.group(1)):
                verb = match.group(1)
                if verb in verbs:
                    continue
                line = text[: span.start()].count("\n") + 1
                unknown.append(f"{path.relative_to(ROOT)}:{line} -> ops.sh {verb}")
    assert not unknown, "docs name operator verbs that do not exist:\n" + "\n".join(unknown)
