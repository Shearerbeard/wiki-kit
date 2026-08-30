#!/usr/bin/env python3
"""Grader unit tests for scripts/wiki-probe.py.

Canned harness outputs only: no test in this file invokes a real
harness CLI (the live runs are the phase-M rehearsal on a scratch
consumer).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

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


def _docked_repo(tmp: str, renders: dict[str, str] | None) -> Path:
    repo = Path(tmp)
    dock = repo / ".wiki"
    dock.mkdir()
    (dock / "manifest.toml").write_text(
        '[dock]\nwiki = "acme-notes"\ncompanion = "widget"\n'
    )
    if renders is not None:
        (dock / "rendered-skills.json").write_text(
            json.dumps({"version": 1, "renders": renders})
        )
    return repo


class RunHarnessTest(unittest.TestCase):
    def test_absent_binary_is_a_named_failure(self) -> None:
        with mock.patch.object(
            wiki_probe,
            "harness_command",
            return_value=["wiki-probe-no-such-binary", "PROMPT"],
        ):
            transcript, failure = wiki_probe.run_harness(
                "codex", Path.cwd(), "PROMPT"
            )
        self.assertEqual(transcript, "")
        self.assertEqual(failure, "wiki-probe-no-such-binary is not installed")

    def test_timeout_is_a_failure_that_keeps_the_partial_transcript(
        self,
    ) -> None:
        with (
            mock.patch.object(
                wiki_probe,
                "harness_command",
                return_value=["sh", "-c", "echo partial; exec sleep 5"],
            ),
            mock.patch.object(wiki_probe, "PROBE_TIMEOUT", 0.5),
        ):
            transcript, failure = wiki_probe.run_harness(
                "codex", Path.cwd(), "PROMPT"
            )
        self.assertIn("partial", transcript)
        self.assertEqual(failure, "timed out after 0.5s")


class LoadExpectationsTest(unittest.TestCase):
    def test_skill_names_come_from_the_rendered_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _docked_repo(
                tmp,
                {
                    ".claude/skills/handoff/SKILL.md": "sha",
                    ".agents/skills/handoff/SKILL.md": "sha",
                    ".agents/skills/garden/SKILL.md": "sha",
                },
            )
            wiki_name, skills = wiki_probe.load_expectations(repo)
        self.assertEqual(wiki_name, "acme-notes")
        self.assertEqual(skills, ("garden", "handoff"))

    def test_missing_record_fails_loud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _docked_repo(tmp, None)
            with self.assertRaisesRegex(
                wiki_probe.ConfigError, "run wiki-dock install with --skills-dir"
            ):
                wiki_probe.load_expectations(repo)

    def test_wrong_record_version_fails_loud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _docked_repo(
                tmp, {".agents/skills/handoff/SKILL.md": "x"}
            )
            record = repo / ".wiki" / "rendered-skills.json"
            record.write_text(
                record.read_text().replace('"version": 1', '"version": 2')
            )
            with self.assertRaisesRegex(
                wiki_probe.ConfigError, "version 1 rendered-skills record"
            ):
                wiki_probe.load_expectations(repo)

    def test_empty_record_fails_loud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _docked_repo(tmp, {})
            with self.assertRaisesRegex(
                wiki_probe.ConfigError, "records no rendered skills"
            ):
                wiki_probe.load_expectations(repo)


class HarnessSelectionTest(unittest.TestCase):
    """`all` means the supported harnesses this machine has; the skipped
    ones are named, and an explicit --harness still fails on an absent
    binary (run_harness reports it as not installed)."""

    def run_main(self, harness: str, present: set[str]) -> tuple[int, str]:
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _docked_repo(
                tmp, {".agents/skills/handoff/SKILL.md": "sha"}
            )
            with (
                mock.patch.object(
                    wiki_probe.shutil,
                    "which",
                    side_effect=lambda name: (
                        f"/bin/{name}" if name in present else None
                    ),
                ),
                mock.patch.object(wiki_probe, "probe", return_value=True),
                contextlib.redirect_stdout(out),
            ):
                code = wiki_probe.main(
                    ["--repo", str(repo), "--harness", harness]
                )
        return code, out.getvalue()

    def test_all_skips_absent_harnesses_by_name(self) -> None:
        code, out = self.run_main("all", {"claude", "codex"})
        self.assertEqual(code, 0)
        self.assertIn("SKIP pi - pi is not installed", out)
        self.assertIn("SKIP opencode - opencode is not installed", out)
        self.assertNotIn("SKIP claude-code", out)
        self.assertIn("2/2 harness(es) passed", out)

    def test_all_with_nothing_installed_fails(self) -> None:
        code, out = self.run_main("all", set())
        self.assertEqual(code, 1)
        self.assertEqual(out.count("SKIP "), len(wiki_probe.HARNESSES))

    def test_explicit_harness_is_never_skipped(self) -> None:
        code, out = self.run_main("pi", set())
        self.assertEqual(code, 0)
        self.assertNotIn("SKIP", out)


if __name__ == "__main__":
    unittest.main()
