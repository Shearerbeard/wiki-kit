#!/usr/bin/env python3
"""Tests for the garden-lock CLI: token ownership, TTL staleness, and the
--wiki root resolution that replaced the file-location-derived lock path."""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "garden-lock.py"


def run_lock(*args: object, cwd: Path | None = None, env: dict | None = None):
    return subprocess.run(
        [str(SCRIPT), *(str(arg) for arg in args)],
        check=False,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
    )


class GardenLockCliTest(unittest.TestCase):
    def make_wiki(self, tmp: Path) -> Path:
        root = tmp / "wiki-repo"
        (root / "workstreams").mkdir(parents=True)
        (root / "wiki.toml").write_text('[contract]\nprotected = ["wiki/events/**"]\n')
        return root

    def test_acquire_check_release_cycle(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self.make_wiki(Path(tmp))
            lock_path = root / "workstreams" / ".garden.lock"

            acquired = run_lock("acquire", "--wiki", root)
            self.assertEqual(acquired.returncode, 0, acquired.stderr)
            match = re.search(r"locked token=([0-9a-f]+)", acquired.stdout)
            self.assertIsNotNone(match, acquired.stdout)
            self.assertTrue(lock_path.is_file())

            held = run_lock("check", "--wiki", root)
            self.assertEqual(held.returncode, 1)
            self.assertIn("held", held.stdout)

            released = run_lock("release", match.group(1), "--wiki", root)
            self.assertEqual(released.returncode, 0, released.stderr)
            self.assertFalse(lock_path.exists())

            free = run_lock("check", "--wiki", root)
            self.assertEqual(free.returncode, 0)
            self.assertIn("free", free.stdout)

    def test_release_with_wrong_token_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self.make_wiki(Path(tmp))
            acquired = run_lock("acquire", "--wiki", root)
            self.assertEqual(acquired.returncode, 0, acquired.stderr)
            refused = run_lock("release", "not-the-token", "--wiki", root)
            self.assertEqual(refused.returncode, 1)
            self.assertIn("does not match", refused.stderr)
            self.assertTrue((root / "workstreams" / ".garden.lock").is_file())

    def test_second_acquire_fails_while_held(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self.make_wiki(Path(tmp))
            first = run_lock("acquire", "--wiki", root)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = run_lock("acquire", "--wiki", root)
            self.assertEqual(second.returncode, 1)
            self.assertIn("held", second.stderr)

    def test_stale_lock_is_cleared_on_acquire(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self.make_wiki(Path(tmp))
            first = run_lock("acquire", "--wiki", root)
            self.assertEqual(first.returncode, 0, first.stderr)
            # TTL forced to 0: any existing lock is stale.
            second = run_lock(
                "acquire", "--wiki", root, env={"GARDEN_LOCK_TTL_SECONDS": "0"}
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("clearing stale lock", second.stdout)
            self.assertIn("locked token=", second.stdout)

    def test_explicit_wiki_without_config_fails_clearly(self) -> None:
        with TemporaryDirectory() as tmp:
            result = run_lock("check", "--wiki", tmp)
            self.assertEqual(result.returncode, 1)
            self.assertIn("wiki.toml", result.stderr)

    def test_walk_up_outside_git_fails_clearly(self) -> None:
        with TemporaryDirectory() as tmp:
            result = run_lock("check", cwd=Path(tmp))
            self.assertEqual(result.returncode, 1)
            self.assertIn("--wiki", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
