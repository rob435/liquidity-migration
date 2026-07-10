from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _skill_files(root: Path) -> dict[str, Path]:
    return {path.parent.name: path for path in root.glob("*/SKILL.md")}


def test_codex_and_claude_project_skills_are_identical() -> None:
    codex = _skill_files(REPO / ".codex" / "skills")
    claude = _skill_files(REPO / ".claude" / "skills")

    assert codex, "no canonical Codex project skills found"
    assert codex.keys() == claude.keys()

    mismatched = [
        name
        for name in sorted(codex)
        if codex[name].read_bytes() != claude[name].read_bytes()
    ]
    assert not mismatched, (
        "Claude skills must be mechanical mirrors of .codex/skills; "
        f"mismatched: {mismatched}"
    )
