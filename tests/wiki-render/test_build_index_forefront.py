#!/usr/bin/env python3
"""Tests for build-index.py's forefront selection: the [budgets]
parallel_workstreams_target knob and the blocker guard."""

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
        "scripts.build_index_forefront", KIT_ROOT / "scripts" / "build-index.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load scripts/build-index.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scripts.build_index_forefront"] = mod
    spec.loader.exec_module(mod)
    return mod


build_index = _load_module()


def _stream(name: str, last_updated: str, blocker: str = "", **overrides: Any) -> Any:
    defaults: dict[str, Any] = dict(
        name=name,
        status="active",
        branch="main",
        sha="abc1234",
        last_updated=last_updated,
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


FIVE = [
    _stream("newest", "2026-07-05"),
    _stream("fourth", "2026-07-04"),
    _stream("third", "2026-07-03"),
    _stream("second", "2026-07-02"),
    _stream("oldest", "2026-07-01"),
]


class SelectForefrontTest(unittest.TestCase):
    def test_no_target_keeps_every_active_in_full(self) -> None:
        forefront, overflow = build_index.select_forefront(FIVE, None)
        self.assertEqual([s.name for s in forefront], [s.name for s in FIVE])
        self.assertEqual(overflow, [])

    def test_target_keeps_the_newest_and_overflows_the_rest(self) -> None:
        forefront, overflow = build_index.select_forefront(FIVE, 2)
        self.assertEqual([s.name for s in forefront], ["newest", "fourth"])
        self.assertEqual([s.name for s in overflow], ["third", "second", "oldest"])

    def test_target_at_and_above_the_count_overflows_nothing(self) -> None:
        for target in (5, 6):
            forefront, overflow = build_index.select_forefront(FIVE, target)
            self.assertEqual(len(forefront), 5, target)
            self.assertEqual(overflow, [], target)

    def test_target_of_one_keeps_exactly_the_newest(self) -> None:
        forefront, overflow = build_index.select_forefront(FIVE, 1)
        self.assertEqual([s.name for s in forefront], ["newest"])
        self.assertEqual(len(overflow), 4)

    def test_recency_alone_decides_and_the_newest_is_the_baton(self) -> None:
        # The baton is the workstream the latest handoff targeted; garden
        # apply stamps its last_updated, so it sorts first and survives
        # any target of one or more. A blocker on an older stream does
        # not promote it: its stop point rides the collapsed row instead.
        streams = FIVE[:-1] + [
            _stream("oldest", "2026-07-01", blocker="Mike: rule on the cutover")
        ]
        forefront, overflow = build_index.select_forefront(streams, 2)
        self.assertEqual([s.name for s in forefront], ["newest", "fourth"])
        self.assertEqual([s.name for s in overflow], ["third", "second", "oldest"])


class TreeRenderingTest(unittest.TestCase):
    def render(self, streams: list[Any], target: int | None) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            return build_index.build_tree(streams, Path(tmp), target)

    def test_overflow_rows_are_one_line_each(self) -> None:
        tree = self.render(FIVE, 2)
        self.assertIn("ACTIVE, NOT IN THE FOREFRONT (3)", tree)
        section = tree.split("ACTIVE, NOT IN THE FOREFRONT (3)")[1].split("PARKED")[0]
        rows = [line for line in section.splitlines() if line.startswith("- ")]
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertIn("Next:", row)
            self.assertIn("2026-07-0", row)
        # The full entries carry a branch line; overflow rows never do.
        self.assertNotIn("main @ abc1234", section)

    def test_no_overflow_section_without_a_target(self) -> None:
        tree = self.render(FIVE, None)
        self.assertNotIn("NOT IN THE FOREFRONT", tree)
        self.assertEqual(tree.count("main @ abc1234"), 5)

    def test_overflow_detail_is_capped_at_sixty_chars(self) -> None:
        long_next = "x" * 200
        streams = FIVE[:2] + [_stream("wordy", "2026-07-01", next_actions=[long_next])]
        tree = self.render(streams, 2)
        row = next(line for line in tree.splitlines() if line.startswith("- wordy"))
        detail = row.split("Next: ", 1)[1]
        self.assertEqual(len(detail), 60)
        self.assertTrue(detail.endswith("…"))
        # Shorter than the 85-char cap the full tree entries use.
        self.assertLess(len(detail), build_index.TREE_FIELD_MAX_CHARS)

    def test_collapsed_row_shows_the_blocker_over_the_next_step(self) -> None:
        streams = FIVE[:2] + [
            _stream("stuck", "2026-07-01", blocker="Mike: rule on the cutover")
        ]
        tree = self.render(streams, 2)
        row = next(line for line in tree.splitlines() if line.startswith("- stuck"))
        self.assertIn("Blocked: Mike: rule on the cutover", row)
        self.assertNotIn("next step for stuck", row)

    def test_render_is_byte_stable(self) -> None:
        self.assertEqual(self.render(FIVE, 2), self.render(FIVE, 2))

    def full_entries(self, tree: str) -> list[str]:
        forefront = tree.split("ACTIVE, NOT IN THE FOREFRONT")[0]
        return [
            line.split()[1]
            for line in forefront.splitlines()
            if line.startswith(("├─", "└─"))
        ]

    def test_unsorted_input_is_ordered_by_recency_before_selection(self) -> None:
        shuffled = [FIVE[3], FIVE[0], FIVE[4], FIVE[1], FIVE[2]]
        tree = self.render(shuffled, 2)
        self.assertEqual(self.full_entries(tree), ["newest", "fourth"])

    def test_pinned_baton_survives_a_same_day_tie(self) -> None:
        # Three streams share the newest date; filename order would put
        # "zeta" last. The newest handoff targeted it, so it is pinned.
        streams = [
            _stream("alpha", "2026-07-05"),
            _stream("beta", "2026-07-05"),
            _stream("zeta", "2026-07-05"),
            _stream("older", "2026-07-01"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tree = build_index.build_tree(streams, Path(tmp), 2, frozenset({"zeta"}))
        entries = self.full_entries(tree)
        self.assertIn("zeta", entries)
        self.assertEqual(len(entries), 2)

    def test_satellite_baton_under_a_collapsed_epic_renders_in_full(self) -> None:
        streams = [
            _stream("hot-satellite", "2026-07-06", tier="satellite", epic="old-epic"),
            _stream("newest", "2026-07-05"),
            _stream("fourth", "2026-07-04"),
            _stream("old-epic", "2026-07-01", tier="board-page", epic="old-epic"),
        ]
        tree = self.render(streams, 2)
        entries = self.full_entries(tree)
        self.assertEqual(entries, ["hot-satellite", "newest"])
        self.assertIn("hot-satellite (satellite of old-epic)", tree)
        # The collapsed epic is still accounted for as a row.
        self.assertIn("- old-epic", tree)

    def test_satellite_nests_when_its_epic_is_in_the_forefront(self) -> None:
        streams = [
            _stream("epic", "2026-07-06", tier="board-page", epic="epic"),
            _stream("sat", "2026-07-05", tier="satellite", epic="epic"),
            _stream("older", "2026-07-01"),
        ]
        tree = self.render(streams, 2)
        self.assertNotIn("(satellite of", tree)
        self.assertIn("epic [epic]", tree)
        self.assertIn("- older", tree)


class LatestHandoffTargetsTest(unittest.TestCase):
    def test_newest_handoff_names_the_baton(self) -> None:
        import wiki_event

        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "wiki" / "events"
            for stamp, name, ms in (
                ("2026-07-01T00:00:00Z", "older", 1),
                ("2026-07-02T00:00:00Z", "baton", 2),
            ):
                event = {
                    "event_id": wiki_event.uuid7(),
                    "event_type": "handoff",
                    "schema_version": 2,
                    "timestamp_utc": stamp,
                    "tool": "manual",
                    "status": "pending_garden",
                    "summary": f"handoff {ms}",
                    "repo": {"name": "widget", "branch": "main", "sha": "abc1234"},
                    "sources": [],
                    "workstream_state": {
                        "current_state": ["s"],
                        "what_was_done": ["d"],
                        "next": ["n"],
                        "blockers": [],
                        "continuation_context": "c",
                    },
                    "proposed_workstreams": [
                        {
                            "name": name,
                            "relationship": "primary",
                            "proposed_action": "u",
                        },
                        {
                            "name": "side",
                            "relationship": "related",
                            "proposed_action": "u",
                        },
                    ],
                }
                wiki_event.validate_event(event)
                wiki_event.write_event(events_dir, event)
            self.assertEqual(build_index.latest_handoff_targets(events_dir), {"baton"})

    def test_empty_store_pins_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "wiki" / "events"
            events_dir.mkdir(parents=True)
            self.assertEqual(build_index.latest_handoff_targets(events_dir), set())


if __name__ == "__main__":
    unittest.main()
