#!/usr/bin/env python3
"""Grader unit tests for scripts/wiki-probe.py.

Canned harness outputs only: no test in this file invokes a real
harness CLI (the live runs are the phase-M rehearsal on a scratch
consumer).
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any

KIT_ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


wiki_probe = _load_module(
    "scripts.wiki_probe", KIT_ROOT / "scripts" / "wiki-probe.py"
)

SKILLS = ("handoff", "garden", "morning", "session-feedback")

CLEAN_OUTPUT = """\
This repo is docked to the **acme-notes** wiki (wiki-kit).

- handoff: writes a session handoff event
- garden: applies a handoff to its workstream
- morning: reviews the night-run report
- session-feedback: files structured feedback
"""


class GradeOutputTest(unittest.TestCase):
    def grade(self, output: str) -> Any:
        return wiki_probe.grade_output(output, "acme-notes", SKILLS)

    def test_clean_output_passes(self) -> None:
        grade = self.grade(CLEAN_OUTPUT)
        self.assertTrue(grade.passed)
        self.assertEqual(grade.reasons, ())

    def test_case_insensitive_matching(self) -> None:
        grade = self.grade(
            "Docked to ACME-NOTES. Skills: HANDOFF, Garden, MORNING, "
            "Session-Feedback."
        )
        self.assertTrue(grade.passed)

    def test_one_missing_skill_is_tolerated(self) -> None:
        """The one-missing tolerance covers harnesses with a capped
        skill listing."""
        output = CLEAN_OUTPUT.replace("- morning:", "- m0rning:")
        self.assertTrue(self.grade(output).passed)

    def test_two_missing_skills_fail(self) -> None:
        output = (
            "Docked to acme-notes. I can see handoff and garden."
        )
        grade = self.grade(output)
        self.assertFalse(grade.passed)
        self.assertTrue(any("skill names" in r for r in grade.reasons))
        self.assertTrue(any("morning" in r for r in grade.reasons))

    def test_missing_wiki_name_fails(self) -> None:
        grade = self.grade(
            "Skills: handoff, garden, morning, session-feedback."
        )
        self.assertFalse(grade.passed)
        self.assertTrue(any("wiki name" in r for r in grade.reasons))

    def test_empty_output_fails(self) -> None:
        for output in ("", "   \n  "):
            grade = self.grade(output)
            self.assertFalse(grade.passed)
            self.assertEqual(
                grade.reasons, ("the harness produced no output",)
            )

    def test_wiki_name_in_stderr_counts(self) -> None:
        """The transcript the grader sees is stdout+stderr combined."""
        grade = self.grade(
            "some noise\nacme-notes\nhandoff\ngarden\nmorning\n"
        )
        self.assertTrue(grade.passed)


class HarnessCommandTest(unittest.TestCase):
    """The invocations stay pinned to the recon-verified flags; a drift
    here fails loudly in review rather than at probe time."""

    def test_commands(self) -> None:
        cases = {
            "pi": ["pi", "-p", "--approve", "PROMPT"],
            "opencode": ["opencode", "run", "--auto", "PROMPT"],
            "claude-code": ["claude", "-p", "PROMPT"],
            "codex": ["codex", "exec", "--sandbox", "read-only", "PROMPT"],
        }
        for harness, expected in cases.items():
            self.assertEqual(
                wiki_probe.harness_command(harness, "PROMPT"), expected
            )


if __name__ == "__main__":
    unittest.main()
