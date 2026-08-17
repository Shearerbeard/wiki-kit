#!/usr/bin/env python3
"""Tests for the wiki-garden dispatch logic."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.wiki_garden import (  # noqa: E402
    SESSION_UPDATES_HEADING,
    AlreadyDispositioned,
    AppliedEvent,
    DurableApplyNeedsRepair,
    GardenApplyError,
    ManualApplyRequired,
    UnknownSchemaVersion,
    ValidationError,
    apply_event,
)

WS_CURATED_SECTIONS = (
    "## Test Workstream\n\n"
    "### Current State\n"
    "- Old state\n\n"
    "### What Was Done\n"
    "#### 2026-01-01\n"
    "1. Old thing\n\n"
    "### Next\n"
    "- Old next\n\n"
    "### Blockers\n"
    "- Old blocker\n\n"
    "### Continuation Context\n"
    "Old context.\n"
)

WS_FILE_TEMPLATE = (
    "---\n"
    "status: active\n"
    "branch: old-branch\n"
    "sha: oldsha1\n"
    "last_updated: 2026-01-01\n"
    "session_id: pending\n"
    'blocker: ""\n'
    "---\n\n" + WS_CURATED_SECTIONS + "\n" + SESSION_UPDATES_HEADING + "\n"
)

EVENT_DATE = "2026-06-04"


def make_event(workstream: str, **overrides: object) -> dict:
    event = {
        "schema_version": 2,
        "event_id": "019e937b-3f6a-773a-845d-f13a3ac36bf4",
        "event_type": "handoff",
        "timestamp_utc": f"{EVENT_DATE}T12:00:00Z",
        "tool": "manual",
        "repo": {"name": "widget", "branch": "main", "sha": "4c0f549"},
        "sources": [],
        "proposed_workstreams": [
            {
                "name": workstream,
                "relationship": "primary",
                "proposed_action": "update",
            }
        ],
        "summary": "Test event",
        "status": "pending_garden",
        "workstream_state": {
            "current_state": ["New state 1", "New state 2"],
            "what_was_done": [
                "Created composed schemas",
                "Added SchemaCache class",
            ],
            "next": [
                "Implement E.5 test cases",
                "Rewrite handoff commands",
            ],
            "blockers": ["Provider missing"],
            "continuation_context": "Phase E.1-E.4 complete. Ready for E.5 tests.",
        },
        **overrides,
    }
    return event


class WikiGardenTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workstreams_dir = self.tmp_path / "workstreams"
        self.workstreams_dir.mkdir()
        self.events_dir = self.tmp_path / "wiki" / "events"
        self.events_dir.mkdir(parents=True)
        # Lock timeout pinned to 0 so held-lock tests fail immediately
        # instead of waiting out the production short-wait default.
        env_pin = mock.patch.dict(os.environ, {"WIKI_EVENT_LOCK_TIMEOUT_SECONDS": "0"})
        env_pin.start()
        self.addCleanup(env_pin.stop)

    def write_workstream(self, name: str) -> Path:
        ws_file = self.workstreams_dir / f"{name}.md"
        ws_file.write_text(WS_FILE_TEMPLATE)
        return ws_file

    def apply(
        self,
        event: dict,
        force: bool = False,
        workstream: str | None = None,
    ) -> AppliedEvent:
        return apply_event(
            event,
            repo_root=self.tmp_path,
            events_dir=self.events_dir,
            force=force,
            workstream=workstream,
        )

    def garden_events(self) -> list[dict]:
        return [
            json.loads(path.read_text())
            for path in sorted(self.events_dir.glob("**/*.json"))
        ]

    def test_unknown_schema_version_fails_loudly(self) -> None:
        with self.assertRaises(UnknownSchemaVersion):
            self.apply({"schema_version": 99})

    def test_apply_uses_custom_repo_envelope_values(self) -> None:
        ws_file = self.write_workstream("test-ws")
        event = make_event("test-ws")
        event["repo"] = {
            "name": "widget",
            "branch": "wiki-branch",
            "sha": "deadbee",
        }
        self.apply(event)
        text = ws_file.read_text()
        self.assertIn("branch: wiki-branch", text)
        self.assertIn("sha: deadbee", text)
        self.assertIn("Branch: wiki-branch @ deadbee", text)

    def test_non_handoff_event_is_refused(self) -> None:
        with self.assertRaises(GardenApplyError):
            self.apply({"schema_version": 1, "event_type": "garden-apply"})

    def test_invalid_handoff_event_is_refused(self) -> None:
        event = make_event("test-ws", tool="not-a-tool")
        with self.assertRaises(ValidationError):
            self.apply(event)

    def test_event_without_workstream_state_fails_loudly(self) -> None:
        # Pre-Phase-E events (e.g. the backfilled gap events) have no
        # workstream_state; apply must fail, never silently exit 0.
        self.write_workstream("test-ws")
        event = make_event("test-ws")
        del event["workstream_state"]
        with self.assertRaises(ManualApplyRequired) as ctx:
            self.apply(event)
        self.assertIn("apply_from_sources", str(ctx.exception))
        # Nothing was recorded: no garden-apply event, workstream untouched.
        self.assertEqual(self.garden_events(), [])

    def test_user_approved_related_workstream_can_be_applied(self) -> None:
        ws_file = self.write_workstream("test-ws")
        event = make_event(
            "test-ws",
            proposed_workstreams=[
                {
                    "name": "test-ws",
                    "relationship": "related",
                    "proposed_action": "needs_review",
                }
            ],
        )
        original_event = json.loads(json.dumps(event))

        result = self.apply(event, workstream="test-ws")

        self.assertEqual(event, original_event)
        self.assertEqual(result.workstream, "test-ws")
        self.assertIn(event["event_id"], ws_file.read_text())
        garden_events = self.garden_events()
        self.assertEqual(len(garden_events), 1)
        self.assertEqual(garden_events[0]["status"], "applied-manually")
        self.assertEqual(garden_events[0]["workstream"], "test-ws")

    def test_override_can_create_a_proposed_candidate_workstream(self) -> None:
        event = make_event(
            "new-ws",
            proposed_workstreams=[
                {
                    "name": "new-ws",
                    "relationship": "candidate_new",
                    "proposed_action": "candidate_new",
                }
            ],
        )

        result = self.apply(event, workstream="new-ws")

        self.assertEqual(result.workstream, "new-ws")
        self.assertTrue(result.workstream_path.is_file())
        self.assertIn(event["event_id"], result.workstream_path.read_text())
        garden_events = self.garden_events()
        self.assertEqual(garden_events[0]["status"], "applied-manually")
        self.assertEqual(garden_events[0]["workstream"], "new-ws")
        pending = json.loads(
            (self.tmp_path / "wiki" / "pending" / "index.json").read_text()
        )
        self.assertEqual(pending["event_count"], 0)

    def test_workstream_override_must_be_a_unique_event_proposal(self) -> None:
        self.write_workstream("test-ws")
        event = make_event(
            "test-ws",
            proposed_workstreams=[
                {
                    "name": "test-ws",
                    "relationship": "related",
                    "proposed_action": "needs_review",
                }
            ],
        )

        with self.assertRaisesRegex(ValueError, "not a unique event proposal"):
            self.apply(event, workstream="other-ws")

        self.assertEqual(self.garden_events(), [])

    def test_workstream_override_rejects_duplicate_matching_proposals(self) -> None:
        self.write_workstream("test-ws")
        event = make_event(
            "test-ws",
            proposed_workstreams=[
                {
                    "name": "test-ws",
                    "relationship": "related",
                    "proposed_action": "needs_review",
                },
                {
                    "name": "test-ws",
                    "relationship": "candidate_new",
                    "proposed_action": "candidate_new",
                },
            ],
        )

        with self.assertRaisesRegex(ValueError, "not a unique event proposal"):
            self.apply(event, workstream="test-ws")

        self.assertEqual(self.garden_events(), [])

    def test_workstream_override_cannot_replace_existing_primary(self) -> None:
        self.write_workstream("test-ws")
        event = make_event("test-ws")

        with self.assertRaisesRegex(ValueError, "event has no primary"):
            self.apply(event, workstream="test-ws")

        self.assertEqual(self.garden_events(), [])

    def test_event_with_workstream_state_appends_session_block(self) -> None:
        ws_file = self.write_workstream("test-ws")
        event = make_event("test-ws")

        result = self.apply(event)
        self.assertEqual(result.workstream, "test-ws")
        self.assertEqual(result.event_id, event["event_id"])
        self.assertEqual(result.workstream_path, ws_file)
        self.assertTrue(result.garden_event_path.is_file())

        updated_text = ws_file.read_text()

        # Frontmatter is stamped with the event's session date, not the
        # apply-time wall clock.
        self.assertIn(f"last_updated: {EVENT_DATE}", updated_text)
        self.assertIn("branch: main", updated_text)
        self.assertIn("sha: 4c0f549", updated_text)
        self.assertIn('blocker: ""', updated_text)

        # Curated sections are byte-identical: everything before the
        # Session updates heading except the frontmatter is untouched.
        curated_part = updated_text.split(SESSION_UPDATES_HEADING)[0]
        self.assertIn(WS_CURATED_SECTIONS.rstrip(), curated_part)

        # Session state landed as an uncurated block, dated and stamped
        # with the source event id, after the section heading.
        block_part = updated_text.split(SESSION_UPDATES_HEADING)[1]
        self.assertIn(
            f"### {EVENT_DATE} — event {event['event_id']} (manual)",
            block_part,
        )
        self.assertIn("Branch: main @ 4c0f549", block_part)
        self.assertIn("- New state 1", block_part)
        self.assertIn("- New state 2", block_part)
        self.assertIn("1. Created composed schemas", block_part)
        self.assertIn("2. Added SchemaCache class", block_part)
        self.assertIn("- Implement E.5 test cases", block_part)
        self.assertIn("- Rewrite handoff commands", block_part)
        self.assertIn("- Provider missing", block_part)
        self.assertIn("Phase E.1-E.4 complete. Ready for E.5 tests.", block_part)

        # The curated What Was Done was NOT touched.
        self.assertIn("1. Old thing", updated_text)
        self.assertNotIn(f"#### {EVENT_DATE}", updated_text)

        # Garden-apply event written and well-formed
        garden_events = self.garden_events()
        self.assertEqual(len(garden_events), 1)
        garden_event = garden_events[0]
        self.assertEqual(garden_event["schema_version"], 1)
        self.assertEqual(garden_event["event_type"], "garden-apply")
        self.assertEqual(garden_event["target_event_id"], event["event_id"])
        self.assertEqual(garden_event["status"], "applied")
        # Apply time is recorded on the garden-apply event itself
        # (not the session date; tolerate a midnight rollover mid-test).
        self.assertNotEqual(garden_event["timestamp_utc"][:10], EVENT_DATE)
        apply_date = garden_event["timestamp_utc"][:10]
        now = datetime.now(UTC).strftime("%Y-%m-%d")
        self.assertLessEqual(apply_date, now)

        # Pending index rebuilt alongside the apply
        pending_index = self.tmp_path / "wiki" / "pending" / "index.json"
        self.assertTrue(pending_index.is_file())

    def test_section_is_created_when_missing(self) -> None:
        # Unmigrated workstream file: the apply creates the Session updates
        # section at end of file instead of failing.
        ws_file = self.workstreams_dir / "test-ws.md"
        ws_file.write_text(WS_FILE_TEMPLATE.split(SESSION_UPDATES_HEADING)[0])
        event = make_event("test-ws")

        self.apply(event)

        updated_text = ws_file.read_text()
        self.assertEqual(updated_text.count(SESSION_UPDATES_HEADING), 1)
        heading_idx = updated_text.index(SESSION_UPDATES_HEADING)
        self.assertIn(event["event_id"], updated_text[heading_idx:])

    def test_two_applies_append_blocks_in_order(self) -> None:
        ws_file = self.write_workstream("test-ws")
        first = make_event("test-ws")
        second = make_event(
            "test-ws",
            event_id="019e937b-3f6a-773a-845d-f13a3ac36bf5",
            timestamp_utc="2026-06-05T12:00:00Z",
        )
        second["workstream_state"] = {
            "current_state": ["Even newer state"],
            "what_was_done": ["Second session work"],
            "next": ["Next next"],
            "blockers": [],
            "continuation_context": "Second session context.",
        }

        self.apply(first)
        self.apply(second)

        updated_text = ws_file.read_text()
        first_idx = updated_text.index(first["event_id"])
        second_idx = updated_text.index(second["event_id"])
        self.assertLess(first_idx, second_idx)
        # Both blocks live inside the Session updates section.
        self.assertLess(updated_text.index(SESSION_UPDATES_HEADING), first_idx)
        # Neither apply touched the curated sections.
        self.assertIn("- Old state", updated_text)
        self.assertIn("- Old next", updated_text)

    def test_candidate_new_event_creates_workstream(self) -> None:
        event = make_event("new-ws")
        event["proposed_workstreams"] = [
            {
                "name": "new-ws",
                "relationship": "candidate_new",
                "proposed_action": "needs_review",
            }
        ]

        result = self.apply(event)

        self.assertEqual(result.workstream, "new-ws")
        ws_file = self.workstreams_dir / "new-ws.md"
        self.assertTrue(ws_file.is_file())
        updated_text = ws_file.read_text()
        self.assertIn("## New Ws", updated_text)
        self.assertIn("candidate-new handoff event", updated_text)
        self.assertIn('blocker: ""', updated_text)
        self.assertIn(event["event_id"], updated_text)
        self.assertIn("- New state 1", updated_text)
        self.assertEqual(len(self.garden_events()), 1)

    def test_candidate_new_events_append_to_created_workstream(self) -> None:
        first = make_event("new-ws")
        first["proposed_workstreams"] = [
            {
                "name": "new-ws",
                "relationship": "candidate_new",
                "proposed_action": "needs_review",
            }
        ]
        second = make_event(
            "new-ws",
            event_id="019e937b-3f6a-773a-845d-f13a3ac36bf5",
            timestamp_utc="2026-06-05T12:00:00Z",
        )
        second["proposed_workstreams"] = first["proposed_workstreams"]
        second["workstream_state"]["current_state"] = ["Second candidate state"]

        self.apply(first)
        self.apply(second)

        updated_text = (self.workstreams_dir / "new-ws.md").read_text()
        self.assertLess(
            updated_text.index(first["event_id"]),
            updated_text.index(second["event_id"]),
        )
        self.assertEqual(updated_text.count(SESSION_UPDATES_HEADING), 1)
        self.assertEqual(len(self.garden_events()), 2)

    def test_missing_non_candidate_workstream_still_fails(self) -> None:
        event = make_event("missing-ws")

        with self.assertRaises(FileNotFoundError):
            self.apply(event)

        self.assertFalse((self.workstreams_dir / "missing-ws.md").exists())
        self.assertEqual(self.garden_events(), [])

    def test_inline_heading_mention_does_not_capture_block(self) -> None:
        # Curated prose may quote the heading inline (wiki-system.md's Next
        # bullet does). The block must land under the real line-anchored
        # heading, never at the prose mention.
        ws_file = self.workstreams_dir / "test-ws.md"
        mention = (
            "### Next\n- add '## Session updates (uncurated)' section to all files\n\n"
        )
        ws_file.write_text(
            WS_FILE_TEMPLATE.replace("### Next\n- Old next\n\n", mention)
        )
        event = make_event("test-ws")

        self.apply(event)

        updated_text = ws_file.read_text()
        heading_idx = updated_text.splitlines().index(SESSION_UPDATES_HEADING)
        block_idx = next(
            i
            for i, line in enumerate(updated_text.splitlines())
            if event["event_id"] in line
        )
        self.assertLess(heading_idx, block_idx)
        # The prose mention is untouched and the curated Next intact.
        self.assertIn("- add '## Session updates (uncurated)' section", updated_text)

    def test_block_lands_inside_mid_file_section(self) -> None:
        # Session updates section followed by another H2: the block must be
        # spliced before the next H2, not appended at end of file.
        ws_file = self.workstreams_dir / "test-ws.md"
        ws_file.write_text(WS_FILE_TEMPLATE + "\n## Trailing notes\n\n- keep me last\n")
        event = make_event("test-ws")

        self.apply(event)

        updated_text = ws_file.read_text()
        self.assertLess(
            updated_text.index(SESSION_UPDATES_HEADING),
            updated_text.index(event["event_id"]),
        )
        self.assertLess(
            updated_text.index(event["event_id"]),
            updated_text.index("## Trailing notes"),
        )
        self.assertIn("- keep me last", updated_text)

    def test_mechanical_apply_never_changes_frontmatter_blocker(self) -> None:
        ws_file = self.workstreams_dir / "test-ws.md"
        ws_file.write_text(
            WS_FILE_TEMPLATE.replace('blocker: ""', 'blocker: "PR #1 open"')
        )
        event = make_event("test-ws")
        self.apply(event)

        updated_text = ws_file.read_text()
        self.assertIn('blocker: "PR #1 open"', updated_text)
        block_part = updated_text.split(SESSION_UPDATES_HEADING, 1)[1]
        self.assertIn("**Blockers:**\n- Provider missing", block_part)

    def test_empty_blockers_render_none_without_changing_frontmatter(self) -> None:
        ws_file = self.write_workstream("test-ws")
        event = make_event("test-ws")
        event["workstream_state"]["blockers"] = []

        self.apply(event)

        updated_text = ws_file.read_text()
        block_part = updated_text.split(SESSION_UPDATES_HEADING)[1]
        self.assertIn("**Blockers:**\nnone", block_part)
        self.assertIn('blocker: ""', updated_text)

    def test_reported_blockers_stay_uncurated_until_interactive_garden(self) -> None:
        ws_file = self.write_workstream("test-ws")
        ws_file.write_text(
            WS_FILE_TEMPLATE.replace('blocker: ""', 'blocker: "Existing blocker"')
        )
        event = make_event("test-ws")
        event["workstream_state"]["blockers"] = [
            "Provider missing",
            "PR #155 awaiting review",
        ]
        self.apply(event)
        updated_text = ws_file.read_text()
        self.assertIn('blocker: "Existing blocker"', updated_text)
        self.assertIn("- Provider missing", updated_text)
        self.assertIn("- PR #155 awaiting review", updated_text)

    def test_double_apply_is_refused_without_force(self) -> None:
        ws_file = self.write_workstream("test-ws")
        event = make_event("test-ws")
        self.apply(event)
        text_after_first = ws_file.read_text()

        with self.assertRaises(AlreadyDispositioned) as ctx:
            self.apply(event)
        self.assertIn("--force", str(ctx.exception))
        # Second apply changed nothing.
        self.assertEqual(ws_file.read_text(), text_after_first)
        self.assertEqual(len(self.garden_events()), 1)

    def test_force_allows_reapply(self) -> None:
        ws_file = self.write_workstream("test-ws")
        event = make_event("test-ws")
        self.apply(event)
        self.apply(event, force=True)
        garden_events = self.garden_events()
        self.assertEqual(len(garden_events), 2)
        # Forced re-apply does duplicate the What Was Done entry — that is
        # the explicit cost the flag opts into.
        self.assertEqual(ws_file.read_text().count("1. Created composed schemas"), 2)

    def test_apply_refused_when_store_has_corrupt_event(self) -> None:
        # The shared quarantine-aware loader runs in the disposition check
        # BEFORE anything mutates: a corrupt store file refuses the apply
        # loudly, with the workstream untouched and no garden-apply written.
        ws_file = self.write_workstream("test-ws")
        original_text = ws_file.read_text()
        bad_dir = self.events_dir / "2026" / "01"
        bad_dir.mkdir(parents=True)
        (bad_dir / "0000.json").write_text(
            json.dumps({"schema_version": 1, "event_type": "handoff"})
        )

        event = make_event("test-ws")
        with self.assertRaises(ValidationError):
            self.apply(event)
        self.assertEqual(ws_file.read_text(), original_text)
        applied = [
            e for e in self.garden_events() if e.get("event_type") == "garden-apply"
        ]
        self.assertEqual(applied, [])

    def test_apply_stands_when_pending_rebuild_fails(self) -> None:
        # Sabotage the pending rebuild only: the pending path exists as a
        # FILE, so write_pending_files fails after the garden-apply event
        # lands. The apply must stand — workstream mutated, event written —
        # and the error must say so instead of rolling back.
        ws_file = self.write_workstream("test-ws")
        (self.events_dir.parent / "pending").write_text("not a directory")

        event = make_event("test-ws")
        with self.assertRaises(DurableApplyNeedsRepair) as ctx:
            self.apply(event)
        self.assertIn("the apply stands", str(ctx.exception))
        self.assertIn("do not re-apply", str(ctx.exception))
        self.assertEqual(ctx.exception.workstream_path, ws_file)
        self.assertTrue(ctx.exception.garden_event_path.is_file())
        self.assertIn(f"last_updated: {EVENT_DATE}", ws_file.read_text())
        applied = [
            e for e in self.garden_events() if e.get("event_type") == "garden-apply"
        ]
        self.assertEqual(len(applied), 1)
        # The lock was released despite the failure.
        self.assertFalse((self.events_dir / ".wiki-event.lock").exists())

    def test_workstream_restored_when_event_write_fails(self) -> None:
        # Make write_event itself fail INSIDE the lock: the garden-apply
        # event's year directory is pre-created as a regular file, so the
        # mkdir in write_event raises after the workstream was already
        # mutated. The restore branch must put the workstream back
        # byte-for-byte, record no event, and release the lock.
        ws_file = self.write_workstream("test-ws")
        original_text = ws_file.read_text()
        year = datetime.now(UTC).strftime("%Y")
        (self.events_dir / year).write_text("not a directory\n")

        event = make_event("test-ws")
        with self.assertRaises(NotADirectoryError):
            self.apply(event)

        self.assertEqual(ws_file.read_text(), original_text)
        self.assertEqual(self.garden_events(), [])
        self.assertFalse((self.events_dir / ".wiki-event.lock").exists())

    def test_apply_event_dispatches_correctly(self) -> None:
        ws_file = self.write_workstream("dispatch-ws")
        event = make_event("dispatch-ws")
        event["workstream_state"] = {
            "current_state": ["Dispatched"],
            "what_was_done": ["Did dispatch"],
            "next": ["Verify dispatch"],
            "blockers": [],
            "continuation_context": "Dispatch verified.",
        }

        result = self.apply(event)
        self.assertEqual(result.workstream, "dispatch-ws")

        updated_text = ws_file.read_text()
        self.assertIn("- Dispatched", updated_text)
        self.assertIn("1. Did dispatch", updated_text)
        self.assertIn("- Verify dispatch", updated_text)
        self.assertIn("Dispatch verified.", updated_text)

        # Mechanical apply leaves frontmatter blocker unchanged.
        self.assertIn('blocker: ""', updated_text)

        garden_events = self.garden_events()
        self.assertEqual(len(garden_events), 1)
        self.assertEqual(garden_events[0]["event_type"], "garden-apply")
        self.assertEqual(garden_events[0]["target_event_id"], event["event_id"])

    def test_apply_with_workstream_state_is_private(self) -> None:
        # Rank 1.6: the lower-level entry point runs inside apply_event's
        # lock acquisition and must not be importable as public API — an
        # external caller would bypass the lock entirely.
        import scripts.wiki_garden as wiki_garden_module

        self.assertFalse(hasattr(wiki_garden_module, "apply_with_workstream_state"))
        self.assertTrue(hasattr(wiki_garden_module, "_apply_with_workstream_state"))

    def test_apply_writes_nothing_when_lock_held(self) -> None:
        # The lock is taken before the disposition check and the workstream
        # read-modify-write: a held lock means NOTHING is touched, closing
        # the same-workstream block-loss race (two applies of different
        # events to one workstream).
        ws_file = self.write_workstream("test-ws")
        original_text = ws_file.read_text()
        lock_path = self.events_dir / ".wiki-event.lock"
        lock_path.write_text(f"pid={os.getpid()}\n")

        for label, event in (
            ("first event", make_event("test-ws")),
            (
                "different event",
                make_event(
                    "test-ws",
                    event_id="019e937b-3f6a-773a-845d-f13a3ac36bf5",
                ),
            ),
        ):
            with self.subTest(label=label):
                with self.assertRaises(RuntimeError):
                    self.apply(event)
                self.assertEqual(ws_file.read_text(), original_text)
        self.assertEqual(self.garden_events(), [])
        lock_path.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
