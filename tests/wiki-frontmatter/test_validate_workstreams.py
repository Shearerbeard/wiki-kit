#!/usr/bin/env python3
"""Tests for the validate-workstreams CLI: the recursive scope rule
(design contract decision 12) and --wiki root resolution."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-workstreams.py"

VALID_FILE = (
    "---\n"
    "status: active\n"
    "branch: alex/widget-work\n"
    "sha: 4c0f549\n"
    "last_updated: 2026-06-12\n"
    "---\n"
    "\n"
    "## Session updates (uncurated)\n"
)


def run_validator(*args: object, cwd: Path | None = None):
    return subprocess.run(
        [str(SCRIPT), *(str(arg) for arg in args)],
        check=False,
        cwd=cwd,
        capture_output=True,
        text=True,
    )


class ValidateWorkstreamsScopeTest(unittest.TestCase):
    def write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_valid_tree_passes_archive_included(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workstreams"
            self.write(ws / "widget-platform.md", VALID_FILE)
            self.write(ws / "_archive" / "widget-launch.md", VALID_FILE)
            result = run_validator("--workstreams-dir", ws)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("2/2", result.stdout)

    def test_invalid_file_inside_archive_is_caught(self) -> None:
        # The source repo's non-recursive glob made _archive/ validation a
        # silent no-op; the kit validates it (decision 12).
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workstreams"
            self.write(ws / "widget-platform.md", VALID_FILE)
            self.write(ws / "_archive" / "broken.md", "no frontmatter here\n")
            result = run_validator("--workstreams-dir", ws)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("_archive/broken.md", result.stderr)

    def test_reference_dir_is_ignored(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workstreams"
            self.write(ws / "widget-platform.md", VALID_FILE)
            self.write(ws / "_reference" / "free-form.md", "no frontmatter here\n")
            result = run_validator("--workstreams-dir", ws)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("1/1", result.stdout)

    def test_index_md_is_ignored_everywhere(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workstreams"
            self.write(ws / "widget-platform.md", VALID_FILE)
            self.write(ws / "index.md", "a catalog, no frontmatter\n")
            self.write(ws / "_archive" / "index.md", "another catalog\n")
            result = run_validator("--workstreams-dir", ws)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("1/1", result.stdout)

    def test_every_failure_is_listed(self) -> None:
        with TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workstreams"
            self.write(ws / "bad-status.md", VALID_FILE.replace("active", "paused"))
            self.write(ws / "_archive" / "broken.md", "no frontmatter here\n")
            result = run_validator("--workstreams-dir", ws)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("bad-status.md", result.stderr)
            self.assertIn("_archive/broken.md", result.stderr)

    def test_missing_directory_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            result = run_validator("--workstreams-dir", Path(tmp) / "absent")
            self.assertEqual(result.returncode, 1)
            self.assertIn("not found", result.stderr)


class ValidateWorkstreamsRootResolutionTest(unittest.TestCase):
    def test_wiki_flag_resolves_workstreams_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "wiki-repo"
            (root / "workstreams").mkdir(parents=True)
            (root / "wiki.toml").write_text(
                '[contract]\nprotected = ["wiki/events/**"]\n'
            )
            (root / "workstreams" / "widget-platform.md").write_text(VALID_FILE)
            result = run_validator("--wiki", root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("1/1", result.stdout)

    def test_explicit_wiki_without_config_fails_clearly(self) -> None:
        with TemporaryDirectory() as tmp:
            result = run_validator("--wiki", tmp)
            self.assertEqual(result.returncode, 1)
            self.assertIn("wiki.toml", result.stderr)

    def test_walk_up_outside_git_fails_clearly(self) -> None:
        with TemporaryDirectory() as tmp:
            result = run_validator(cwd=Path(tmp))
            self.assertEqual(result.returncode, 1)
            self.assertIn("--wiki", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
