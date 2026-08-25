"""Contract checks for the ported workflow skills (K3 stage 1.4).

The morning skill's mutation-sensitive flow has its own pinning module
under tests/notifications/; this module pins the shared template
contract for all four skills plus the structural invariants of the
other three. These pin structure, not prose: frontmatter shape, the
token set, the absence of machine paths, and the commands each
procedure must keep."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = KIT_ROOT / ".agents" / "skills"

SKILL_NAMES = ("garden", "handoff", "morning", "session-feedback")
HARNESSES = "pi opencode claude-code codex"
# Any {{...}} span is a template placeholder; matching the dock's own
# leftover check, no case/charset restriction.
TOKEN_RE = re.compile(r"\{\{[^}]*\}\}")

# Machine-specific strings the ported text must never carry. The family
# string is assembled, never spelled out: the zero-family sweep
# (tests/sweep/) forbids the literal repo-wide, this file included.
_FAMILY = "au" + "ra"
FORBIDDEN_SNIPPETS = (
    "~/workspace/",
    "/" + "Users" + "/",
    f"com.{_FAMILY}.wiki-",
    f"{_FAMILY}-session-docs",
)


def skill_text(name: str) -> str:
    path = SKILLS_DIR / name / "SKILL.md"
    assert path.is_file(), f"missing ported skill: {path}"
    return path.read_text(encoding="utf-8")


def frontmatter(text: str) -> str:
    assert text.startswith("---\n")
    return text.split("---\n", 2)[1]


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_frontmatter_carries_the_shared_contract(name: str) -> None:
    front = frontmatter(skill_text(name))
    assert f"name: {name}" in front
    assert "description: |" in front
    assert f"compatibility: {HARNESSES}" in front


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_only_the_kit_root_token_appears(name: str) -> None:
    text = skill_text(name)
    assert "{{KIT_ROOT}}" in text
    assert set(TOKEN_RE.findall(text)) == {"{{KIT_ROOT}}"}


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_no_machine_paths_or_hardcoded_labels(name: str) -> None:
    text = skill_text(name)
    for snippet in FORBIDDEN_SNIPPETS:
        assert snippet not in text, f"{name} still carries {snippet!r}"


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_commands_invoke_kit_scripts_through_the_resolver(name: str) -> None:
    """Kit CLIs run via `uv run --project {{KIT_ROOT}}`; the Stage 1.1
    dock resolver finds the wiki root from the caller's dock, so no
    command may carry a wiki-path prefix."""
    text = skill_text(name)
    for invocation in re.findall(r"uv run --project \S+", text):
        assert invocation == "uv run --project {{KIT_ROOT}}"


def test_garden_keeps_its_approval_gated_procedure() -> None:
    text = skill_text("garden")
    assert "wiki_checkpoint.py preflight" in text
    assert "wiki-event.py count-pending" in text
    assert "wiki-garden.py <event_path>" in text
    assert "wiki-render.py claude-local --quickstart-file" in text
    assert "Never merge without explicit user confirmation" in text
    # The commit boundary stays checkpoint-owned.
    prepare = text.index("wiki_checkpoint.py prepare")
    verify = text.index("wiki_checkpoint.py verify", prepare)
    commit_gate = text.index("git-commit", verify)
    assert prepare < verify < commit_gate


def test_handoff_keeps_its_scope_boundary_and_event_flow() -> None:
    text = skill_text("handoff")
    assert "wiki-event.py new-handoff" in text
    assert "candidate_new" in text
    assert "--repo-name <REPO_NAME>" in text
    assert "Do NOT hand-edit CLAUDE.local.md" in text
    # The repo-scope stop check survives the port.
    assert "out-of-family repo" in text
    # Handoff ends by surfacing the pending queue, never by committing.
    assert "wiki-event.py count-pending" in text
    normalized = " ".join(text.split())
    assert "Do not stage or commit any handoff output" in normalized


def test_session_feedback_keeps_its_record_format_rules() -> None:
    text = skill_text("session-feedback")
    assert "wiki/feedback/" in text
    assert "triaged: false" in text
    assert "parse_frontmatter" in text
    assert "Do NOT use `validate_frontmatter`" in text
    assert "wiki-render.py claude-local" in text
    # The reporter is the harness, not a hardcoded tool.
    assert "The reporter tool is your harness" in text
