#!/usr/bin/env python3
"""Tests for build-index.py's item cleaning, the blocker-only tag column,
and the long_tags curation signal on the --json surface."""

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

KIT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = KIT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "scripts.build_index_curation", KIT_ROOT / "scripts" / "build-index.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load scripts/build-index.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scripts.build_index_curation"] = mod
    spec.loader.exec_module(mod)
    return mod


build_index = _load_module()


def _stream(name: str, blocker: str = "", **overrides: Any) -> Any:
    defaults: dict[str, Any] = dict(
        name=name,
        status="active",
        branch="main",
        sha="abc1234",
        last_updated="2026-07-05",
        blocker=blocker,
        epic="",
        tier="",
        pr="",
        issue="",
        repo="acme/widget",
        next_actions=[f"next step for {name}"],
        done_items=[],
        body_lines=20,
        sessions_since=0,
        is_stale_candidate=False,
        is_thin=False,
        path=Path(f"workstreams/{name}.md"),
    )
    defaults.update(overrides)
    return build_index.Workstream(**defaults)


PAGE = """\
---
status: active
branch: main
sha: abc1234
last_updated: 2026-07-05
---

### What Was Done
- **2026-07-05 - landed the thing** (event 01a0abcd)
2. second done item

### Next
- **Mike: rule on the cutover** - then push
1. Rebase the branch
* star bullet
+ plus bullet
- None
plain line without a marker

### Blockers
None
"""


class CleanItemTest(unittest.TestCase):
    def test_marker_numbering_and_emphasis_go(self) -> None:
        self.assertEqual(
            build_index.clean_item("- **Mike: rule** on it"), "Mike: rule on it"
        )
        self.assertEqual(build_index.clean_item("3. numbered"), "numbered")
        self.assertEqual(build_index.clean_item("* star"), "star")
        self.assertEqual(build_index.clean_item("+ plus"), "plus")

    def test_inline_code_and_single_emphasis_survive(self) -> None:
        self.assertEqual(
            build_index.clean_item("- run `uv sync` *now*"), "run `uv sync` *now*"
        )

    def test_emphasis_inside_inline_code_is_literal(self) -> None:
        self.assertEqual(
            build_index.clean_item("- **scan** `wiki/entities/**/*.md` **now**"),
            "scan `wiki/entities/**/*.md` now",
        )
        self.assertEqual(build_index.clean_item("- run `x ** 2`"), "run `x ** 2`")
        # An unclosed backtick opens no code span, so the ** still goes.
        self.assertEqual(build_index.clean_item("- a ` b **c**"), "a ` b c")

    def test_longer_backtick_runs_delimit_a_span(self) -> None:
        # CommonMark: a span closes on a backtick run of the opening's
        # length, so inner backticks and ** stay literal.
        for line in (
            "``x ** 2``",
            "``**``",
            "``outer `inner` **literal**``",
            "`**x**`",
            "`a``b`",
        ):
            self.assertEqual(build_index.clean_item("- " + line), line, line)
        self.assertEqual(
            build_index.clean_item("- `a` **b** `c` ``d **e**``"),
            "`a` b `c` ``d **e**``",
        )

    def test_a_dash_inside_the_text_is_not_a_marker(self) -> None:
        self.assertEqual(build_index.clean_item("a - b"), "a - b")
        self.assertEqual(build_index.clean_item("-not a bullet"), "-not a bullet")


class ExtractSectionItemsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.page = Path(self._tmp.name) / "page.md"
        self.page.write_text(PAGE, encoding="utf-8")

    def test_next_items_are_phrases(self) -> None:
        self.assertEqual(
            build_index.extract_section_items(self.page, "Next"),
            [
                "Mike: rule on the cutover - then push",
                "Rebase the branch",
                "star bullet",
                "plus bullet",
                "plain line without a marker",
            ],
        )

    def test_a_none_bullet_is_no_item(self) -> None:
        items = build_index.extract_section_items(self.page, "Next")
        self.assertNotIn("None", items)

    def test_done_items_are_cleaned_the_same_way(self) -> None:
        self.assertEqual(
            build_index.extract_section_items(self.page, "What Was Done"),
            [
                "2026-07-05 - landed the thing (event 01a0abcd)",
                "second done item",
            ],
        )

    def test_stale_next_matches_through_extraction(self) -> None:
        # Both sides are cleaned at extraction, so a Next bullet that
        # another page's What Was Done carries in bold still matches.
        workstreams = Path(self._tmp.name) / "workstreams"
        workstreams.mkdir()
        head = PAGE.split("### What Was Done")[0]
        (workstreams / "done.md").write_text(
            head + "### What Was Done\n- **Rebase the branch**\n", encoding="utf-8"
        )
        (workstreams / "todo.md").write_text(
            head + "### Next\n- Rebase the branch\n", encoding="utf-8"
        )
        streams, errors = build_index.load_workstreams(
            workstreams, workstreams / "no-log.md", None
        )
        self.assertEqual(errors, [])
        candidates = build_index.find_stale_next_candidates(streams)
        self.assertEqual(
            [(c["workstream"], c["next_item"]) for c in candidates],
            [("todo", "Rebase the branch")],
        )


class TagColumnTest(unittest.TestCase):
    def render(self, streams: list[Any]) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            return build_index.build_tree(streams, Path(tmp))

    def entry_line(self, tree: str, name: str) -> str:
        return next(
            line
            for line in tree.splitlines()
            if line.lstrip("│ ").startswith(("├─ " + name, "└─ " + name))
        )

    def test_no_blocker_means_no_tag_and_no_next_fragment(self) -> None:
        tree = self.render([_stream("free")])
        line = self.entry_line(tree, "free")
        self.assertTrue(line.endswith("·"), line)
        self.assertNotIn("next step", line)
        self.assertEqual(line, line.rstrip())
        # The next step appears exactly once, on its own line.
        self.assertEqual(tree.count("next step for free"), 1)
        self.assertIn("Next: next step for free", tree)

    def test_blocker_is_the_tag(self) -> None:
        tree = self.render([_stream("stuck", blocker="Mike: rule on the cutover")])
        self.assertTrue(
            self.entry_line(tree, "stuck").endswith("· Mike: rule on the cutover")
        )

    def test_satellite_follows_the_same_rule(self) -> None:
        board = _stream("epic", tier="board-page", epic="epic")
        free = _stream("free-sat", tier="satellite", epic="epic")
        stuck = _stream("stuck-sat", tier="satellite", epic="epic", blocker="held")
        tree = self.render([board, free, stuck])
        free_line = self.entry_line(tree, "free-sat")
        self.assertTrue(free_line.endswith("·"), free_line)
        self.assertNotIn("next step", free_line)
        self.assertTrue(self.entry_line(tree, "stuck-sat").endswith("· held"))


class LongTagsTest(unittest.TestCase):
    def test_boundary_is_the_curation_cap(self) -> None:
        cap = build_index.TAG_CURATION_MAX_CHARS
        at_cap = _stream("at-cap", blocker="b" * cap, next_actions=["n" * cap])
        over = _stream("over", blocker="b" * (cap + 1), next_actions=["n" * (cap + 1)])
        tags = build_index.find_long_tags([at_cap, over])
        self.assertEqual(
            [(t["workstream"], t["field"], t["length"]) for t in tags],
            [("over", "blocker", cap + 1), ("over", "next", cap + 1)],
        )
        self.assertEqual(tags[0]["text"], "b" * (cap + 1))

    def test_parked_blocker_counts_and_archived_does_not(self) -> None:
        long = "x" * 150
        parked = _stream("parked", status="parked", blocker=long, next_actions=[long])
        archived = _stream("gone", status="archived", blocker=long)
        tags = build_index.find_long_tags([parked, archived])
        self.assertEqual(
            [(t["workstream"], t["field"]) for t in tags],
            [("parked", "blocker"), ("parked", "next")],
        )

    def test_no_next_is_no_finding(self) -> None:
        bare = _stream("n", next_actions=[])
        self.assertEqual(build_index.find_long_tags([bare]), [])


WIKI_TOML = """\
[wiki]
name = "acme-notes"

[contract]
protected = ["wiki/log.md"]
external_allow = []
skills = ["garden"]
global_skills = []
"""


class JsonSurfaceTest(unittest.TestCase):
    """The --json surface through build-index's own entry point: cleaned
    items and the long_tags list the garden skill reads."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "wiki"
        (self.root / "workstreams").mkdir(parents=True)
        (self.root / "wiki.toml").write_text(WIKI_TOML, encoding="utf-8")
        long_next = "- **Mike: " + "decide " * 20 + "**"
        (self.root / "workstreams" / "wordy.md").write_text(
            "---\nstatus: active\nbranch: main\nsha: abc1234\n"
            "last_updated: 2026-07-05\n---\n"
            f"### Next\n{long_next}\n\n## Session updates (uncurated)\n",
            encoding="utf-8",
        )
        (self.root / "workstreams" / "terse.md").write_text(
            "---\nstatus: active\nbranch: main\nsha: abc1234\n"
            "last_updated: 2026-07-04\n---\n"
            "### Next\n- **Ship it**\n\n## Session updates (uncurated)\n",
            encoding="utf-8",
        )

    def json(self) -> dict[str, Any]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(build_index.main(["--wiki", str(self.root), "--json"]), 0)
        return json.loads(out.getvalue())

    def test_next_actions_are_cleaned_and_long_tags_named(self) -> None:
        expected = "Mike: " + " ".join(["decide"] * 20)
        data = self.json()
        by_name = {s["name"]: s for s in data["active"]}
        self.assertEqual(by_name["terse"]["next_actions"], ["Ship it"])
        self.assertEqual(by_name["wordy"]["next_actions"], [expected])
        self.assertEqual(
            data["long_tags"],
            [
                {
                    "workstream": "wordy",
                    "field": "next",
                    "length": len(expected),
                    "text": expected,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
