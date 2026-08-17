#!/usr/bin/env python3
"""Tests for build-index.py's tree-field truncation."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

KIT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = KIT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "scripts.build_index", KIT_ROOT / "scripts" / "build-index.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load scripts/build-index.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scripts.build_index"] = mod
    spec.loader.exec_module(mod)
    return mod


build_index = _load_module()


def _make_stream(**overrides: Any) -> Any:
    defaults: dict[str, Any] = dict(
        name="example",
        status="active",
        branch="main",
        sha="abc1234",
        last_updated="2026-07-28",
        blocker="",
        epic="",
        tier="",
        pr="",
        issue="",
        repo="acme/widget",
        next_actions=[],
        done_items=[],
        body_lines=20,
        sessions_since=0,
        is_stale_candidate=False,
        is_thin=False,
        path=Path("workstreams/example.md"),
    )
    defaults.update(overrides)
    return build_index.Workstream(**defaults)


class TruncateTest(unittest.TestCase):
    def test_short_text_is_unchanged(self) -> None:
        self.assertEqual(build_index._truncate("short"), "short")

    def test_exact_limit_is_unchanged(self) -> None:
        text = "x" * build_index.TREE_FIELD_MAX_CHARS
        self.assertEqual(build_index._truncate(text), text)

    def test_over_limit_is_truncated_with_ellipsis(self) -> None:
        text = "x" * (build_index.TREE_FIELD_MAX_CHARS + 50)
        result = build_index._truncate(text)
        self.assertEqual(len(result), build_index.TREE_FIELD_MAX_CHARS)
        self.assertTrue(result.endswith("…"))


class BuildTreeTruncationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workstreams_dir = Path(self._tmp.name)

    def build_tree(self, streams: list[Any]) -> str:
        return build_index.build_tree(streams, self.workstreams_dir)

    def test_long_blocker_is_truncated_in_tree(self) -> None:
        long_blocker = "B" * 200
        stream = _make_stream(blocker=long_blocker, next_actions=["do the thing"])
        tree = self.build_tree([stream])
        for line in tree.splitlines():
            self.assertLessEqual(len(line.split("·")[-1].strip()), 100)
        self.assertNotIn(long_blocker, tree)

    def test_long_next_action_is_truncated_in_tree(self) -> None:
        long_next = "N" * 200
        stream = _make_stream(next_actions=[long_next])
        tree = self.build_tree([stream])
        next_line = next(
            line for line in tree.splitlines() if line.strip().startswith("Next:")
        )
        self.assertNotIn(long_next, next_line)
        self.assertTrue(next_line.rstrip().endswith("…"))

    def test_short_blocker_and_next_survive_verbatim(self) -> None:
        stream = _make_stream(blocker="short blocker", next_actions=["short next"])
        tree = self.build_tree([stream])
        self.assertIn("short blocker", tree)
        self.assertIn("short next", tree)

    def test_satellite_fields_are_also_truncated(self) -> None:
        board = _make_stream(
            name="epic-board",
            tier="board-page",
            epic="my-epic",
            next_actions=["board next"],
        )
        satellite = _make_stream(
            name="satellite-one",
            tier="satellite",
            epic="my-epic",
            blocker="S" * 200,
            next_actions=["T" * 200],
        )
        tree = self.build_tree([board, satellite])
        self.assertNotIn("S" * 200, tree)
        self.assertNotIn("T" * 200, tree)


if __name__ == "__main__":
    unittest.main(verbosity=2)
