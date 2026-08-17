"""Contract checks for the mutation-sensitive /morning recovery flow.

The morning skill itself ports at K3; these checks activate the moment
the ported SKILL.md lands at its kit path. Until then they are skipped,
not deleted, so the contract they pin is not silently dropped."""

from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[2]
SKILL = KIT_ROOT / ".agents" / "skills" / "morning" / "SKILL.md"

pytestmark = pytest.mark.skipif(
    not SKILL.is_file(), reason="morning skill ports at K3"
)


def section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_durable_partial_repairs_all_projections_before_doctor() -> None:
    text = SKILL.read_text()
    repair = section(
        text, "## Repair a durable partial apply", "## Supersede a bad apply"
    )

    commands = [
        "wiki-event.py build-pending",
        "wiki-render.py log",
        "wiki-render.py claude-local",
        "wiki_doctor.py",
    ]
    positions = [repair.index(command) for command in commands]
    assert positions == sorted(positions)
    assert "DO NOT REAPPLY" in repair
    assert "retry that projection step, not the apply" in repair
    assert "git-commit" in repair
    assert "Do not push" in repair


def test_supersede_approval_covers_both_mutations_before_disposition() -> None:
    text = SKILL.read_text()
    supersede = section(text, "## Supersede a bad apply", "## Safety")

    approval = supersede.index("Obtain one explicit approval covering both")
    disposition = supersede.index("wiki-event.py new-garden-apply")
    repair = supersede.index("Immediately perform the already-approved repair")
    assert approval < disposition < repair
    assert "second user confirmation" not in supersede
    assert "do not issue another disposition" in supersede


def test_supersede_failure_repairs_workstream_before_completion_pipeline() -> None:
    text = SKILL.read_text()
    supersede = section(text, "## Supersede a bad apply", "## Safety")

    disposition = supersede.index("wiki-event.py new-garden-apply")
    projection_failure = supersede.index("pending rebuild failed", disposition)
    repair = supersede.index("Immediately perform the already-approved repair")
    build_pending = supersede.index("wiki-event.py build-pending", repair)
    render_log = supersede.index("wiki-render.py log", build_pending)
    render_local = supersede.index("wiki-render.py claude-local", render_log)
    doctor = supersede.index("wiki_doctor.py", render_local)
    commit_gate = supersede.index("git-commit", doctor)

    assert projection_failure < repair
    assert repair < build_pending < render_log < render_local < doctor < commit_gate
    before_repair = supersede[projection_failure:repair]
    assert "do not render" in before_repair
    assert "or commit yet" in before_repair


def test_manual_action_wording_says_apply_did_not_run() -> None:
    text = SKILL.read_text()
    assert "mechanical apply did not run for this handoff" in text
    assert "Leave it pending and route it to interactive `/garden`" in text
