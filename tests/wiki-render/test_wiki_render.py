#!/usr/bin/env python3
"""Tests for the wiki-render log projection."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

KIT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = KIT_ROOT / "scripts" / "wiki-render.py"
SCRIPTS_DIR = KIT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import wiki_config  # noqa: E402

BOUNDARY = "2026-06-04T16:30:00Z"
LEGACY_TEXT = (
    "## [2026-06-04T16:30:00Z] Last legacy entry @c9a70bc branch:main worktree:main\n"
    "- Legacy bullet kept verbatim\n"
)

# A wiki.toml satisfying the loader's floor: [contract] with a non-empty
# protected list. Zero companions, no [memory].index_line.
MINIMAL_WIKI_TOML = '[contract]\nprotected = ["wiki/log.md"]\n'

# One-companion fixture; tests read display_label back through load_config
# rather than duplicating the literal into assertions.
COMPANION_WIKI_TOML = """\
[wiki]
name = "acme-notes"

[companions.widget]
github = "acme/widget"
display_label = "widget main"

[contract]
protected = ["wiki/log.md"]
"""

TWO_COMPANION_WIKI_TOML = """\
[wiki]
name = "acme-notes"
default_companion = "widget"

[companions.gizmo]
display_label = "gizmo main"

[companions.widget]
display_label = "widget main"

[contract]
protected = ["wiki/log.md"]
"""

CLI_WIKI_TOML_TEMPLATE = """\
[wiki]
name = "acme-notes"

[memory]
index_line = "{index_line}"

[companions.widget]
github = "acme/widget"
display_label = "widget main"

[contract]
protected = ["wiki/log.md"]
"""

FIXTURE_INDEX_LINE = "Memory: the fixture memory index"
# A configured index line that does NOT carry the legacy "Memory: " prefix,
# to prove the configured-value strip works independently of the prefix.
ALT_INDEX_LINE = "Memory index: kept in the fixture harness"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "scripts.wiki_render", KIT_ROOT / "scripts" / "wiki_render.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load scripts/wiki_render.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scripts.wiki_render"] = mod
    spec.loader.exec_module(mod)
    return mod


wiki_render = _load_module()


def make_handoff(
    event_id: str,
    timestamp: str,
    summary: str,
    what_was_done: list[str] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema_version": 2,
        "event_id": event_id,
        "event_type": "handoff",
        "timestamp_utc": timestamp,
        "tool": "claude-code",
        "repo": {"name": "widget", "branch": "main", "sha": "abc1234"},
        "sources": [{"kind": "plan", "path": "plans/example.md"}],
        "proposed_workstreams": [
            {
                "name": "widget-docs",
                "relationship": "primary",
                "proposed_action": "update",
            }
        ],
        "summary": summary,
        "status": "pending_garden",
    }
    if what_was_done is not None:
        event["workstream_state"] = {"what_was_done": what_was_done}
    return event


def make_garden_apply(
    event_id: str, timestamp: str, target: str, status: str = "applied"
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": "garden-apply",
        "timestamp_utc": timestamp,
        "status": status,
        "target_event_id": target,
    }


class RenderLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.events_dir = root / "events"
        self.events_dir.mkdir()
        self.epoch_path = root / "log-epoch.json"
        self.epoch_path.write_text(
            json.dumps({"schema_version": 1, "render_epoch_start": BOUNDARY}),
            encoding="utf-8",
        )
        self.legacy_path = root / "log-legacy.md"
        self.legacy_path.write_text(LEGACY_TEXT, encoding="utf-8")
        self.quarantine_path = root / "quarantine.json"
        self.output_path = root / "log.md"

    def write_event(self, event: dict[str, Any]) -> None:
        path = self.events_dir / f"{event['event_id']}.json"
        path.write_text(json.dumps(event, indent=2), encoding="utf-8")

    def render(self) -> str:
        return wiki_render.render_log(
            events_dir=self.events_dir,
            epoch_path=self.epoch_path,
            legacy_path=self.legacy_path,
            quarantine_path=self.quarantine_path,
        )

    def render_blank(self) -> str:
        return wiki_render.render_log(
            events_dir=self.events_dir,
            epoch_path=None,
            legacy_path=None,
            quarantine_path=self.quarantine_path,
        )

    def quarantine(self, event_id: str, corrected_by: str) -> None:
        self.quarantine_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "quarantined": [
                        {
                            "event_id": event_id,
                            "path": f"events/{event_id}.json",
                            "reason": "hand-written id, invalid UUIDv7 bits",
                            "corrected_by": corrected_by,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_legacy_text_verbatim_and_header_present(self) -> None:
        output = self.render()
        self.assertIn(LEGACY_TEXT, output)
        self.assertTrue(output.startswith("# Session Log\n"))
        self.assertIn("Never hand-edit", output)
        self.assertIn(BOUNDARY, output)

    def test_pre_boundary_events_excluded(self) -> None:
        self.write_event(
            make_handoff(
                "019e937b-3f6a-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-03T00:01:11Z",
                "Backfilled event already in legacy text",
            )
        )
        # Boundary timestamp itself is legacy, not rendered.
        self.write_event(
            make_handoff(
                "019e937b-3f6b-7aaa-8aaa-aaaaaaaaaaab",
                BOUNDARY,
                "Boundary event",
            )
        )
        output = self.render()
        self.assertNotIn("Backfilled event", output)
        self.assertNotIn("Boundary event", output)

    def test_entry_renders_header_bullets_and_event_id(self) -> None:
        self.write_event(
            make_handoff(
                "019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-06T01:22:57Z",
                "Root-caused token misreporting",
                what_was_done=["Found 3 bugs", "Proved provider ground truth"],
            )
        )
        output = self.render()
        self.assertIn(
            "## [2026-06-06T01:22:57Z] Root-caused token misreporting "
            "@abc1234 branch:main tool:claude-code "
            "event:019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa",
            output,
        )
        self.assertIn("- Found 3 bugs\n- Proved provider ground truth\n", output)

    def test_event_without_workstream_state_is_header_only(self) -> None:
        self.write_event(
            make_handoff(
                "019e9a42-641a-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-06T00:08:22Z",
                "Header-only entry",
            )
        )
        output = self.render()
        entry = output.split("Header-only entry")[1]
        self.assertNotIn("- ", entry.split("\n##")[0])

    def test_garden_disposition_joined(self) -> None:
        self.write_event(
            make_handoff(
                "019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-06T01:22:57Z",
                "Applied entry",
            )
        )
        self.write_event(
            make_garden_apply(
                "019eaad2-0000-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-09T05:00:00Z",
                "019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa",
            )
        )
        output = self.render()
        self.assertIn(
            "- Garden: applied 2026-06-09 (event 019eaad2-0000-7aaa-8aaa-aaaaaaaaaaaa)",
            output,
        )

    def test_rejected_disposition_rendered(self) -> None:
        self.write_event(
            make_handoff(
                "019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-06T01:22:57Z",
                "Rejected entry",
            )
        )
        self.write_event(
            make_garden_apply(
                "019eaad2-0000-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-09T05:00:00Z",
                "019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa",
                status="rejected",
            )
        )
        self.assertIn("- Garden: rejected 2026-06-09", self.render())

    def test_multiple_applies_latest_wins(self) -> None:
        target = "019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa"
        self.write_event(
            make_handoff(target, "2026-06-06T01:22:57Z", "Double-applied entry")
        )
        self.write_event(
            make_garden_apply(
                "019eaad2-0000-7aaa-8aaa-aaaaaaaaaaaa", "2026-06-07T00:00:00Z", target
            )
        )
        self.write_event(
            make_garden_apply(
                "019eaad2-1111-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-09T00:00:00Z",
                target,
                status="superseded",
            )
        )
        output = self.render()
        self.assertIn("- Garden: superseded 2026-06-09", output)
        self.assertNotIn("- Garden: applied 2026-06-07", output)

    def test_orphan_garden_apply_is_ignored(self) -> None:
        # Applies targeting pre-boundary (legacy/backfilled) handoffs have no
        # rendered entry to annotate; they must not crash or render.
        self.write_event(
            make_garden_apply(
                "019eaad2-0000-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-09T05:00:00Z",
                "019e937b-3f6a-7aaa-8aaa-aaaaaaaaaaaa",
            )
        )
        self.assertNotIn("Garden:", self.render())

    def test_entries_ordered_by_timestamp_then_event_id(self) -> None:
        self.write_event(
            make_handoff(
                "019e9b02-0002-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-06T03:38:36Z",
                "Tie B",
            )
        )
        self.write_event(
            make_handoff(
                "019e9b02-0001-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-06T03:38:36Z",
                "Tie A",
            )
        )
        self.write_event(
            make_handoff(
                "019e9a42-641a-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-06T00:08:22Z",
                "Earliest",
            )
        )
        output = self.render()
        self.assertLess(output.index("Earliest"), output.index("Tie A"))
        self.assertLess(output.index("Tie A"), output.index("Tie B"))

    def test_render_is_deterministic(self) -> None:
        self.write_event(
            make_handoff(
                "019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-06T01:22:57Z",
                "Deterministic entry",
                what_was_done=["One thing"],
            )
        )
        self.assertEqual(self.render(), self.render())

    def test_malformed_handoff_fails_loudly(self) -> None:
        event = make_handoff(
            "019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa",
            "2026-06-06T01:22:57Z",
            "Bad event",
        )
        del event["repo"]
        self.write_event(event)
        with self.assertRaises(wiki_render.ValidationError):
            self.render()

    def test_malformed_garden_apply_fails_loudly(self) -> None:
        event = make_garden_apply(
            "019eaad2-0000-7aaa-8aaa-aaaaaaaaaaaa",
            "2026-06-09T05:00:00Z",
            "019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa",
        )
        del event["target_event_id"]
        self.write_event(event)
        with self.assertRaises(wiki_render.ValidationError):
            self.render()

    def test_unknown_event_type_fails_loudly(self) -> None:
        event = make_handoff(
            "019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa",
            "2026-06-06T01:22:57Z",
            "Mystery event",
        )
        event["event_type"] = "correction"
        self.write_event(event)
        with self.assertRaises(wiki_render.ValidationError):
            self.render()

    def test_malformed_json_event_fails_loudly(self) -> None:
        (self.events_dir / "broken.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(wiki_render.ValidationError) as ctx:
            self.render()
        self.assertIn("broken.json", str(ctx.exception))

    def test_missing_events_dir_fails_loudly(self) -> None:
        # A typo'd events dir must not silently truncate the projection.
        self.events_dir.rmdir()
        with self.assertRaises(wiki_render.ValidationError):
            self.render()

    def test_quarantined_invalid_event_is_skipped(self) -> None:
        # An invalid-id garden-apply renders fine once quarantined with a
        # real correction; without quarantine it must fail.
        bad = make_garden_apply(
            "019ebb7f-aee4-7ee4-c421-cbe5e5451232",  # invalid variant nibble
            "2026-06-06T00:57:50Z",
            "019e937b-3f6d-7aa0-b453-a5494184e348",
            status="applied-manually",
        )
        self.write_event(bad)
        correction = make_garden_apply(
            "019eb4e3-bacc-7aaa-8aaa-aaaaaaaaaaaa",
            "2026-06-06T00:57:50Z",
            "019e937b-3f6d-7aa0-b453-a5494184e348",
            status="applied-manually",
        )
        self.write_event(correction)
        with self.assertRaises(wiki_render.ValidationError):
            self.render()
        self.quarantine(bad["event_id"], corrected_by=correction["event_id"])
        output = self.render()
        self.assertNotIn(bad["event_id"], output)

    def test_quarantined_id_missing_from_store_fails(self) -> None:
        self.quarantine(
            "019ebb7f-aee4-7ee4-c421-cbe5e5451232",
            corrected_by="019eb4e3-bacc-7aaa-8aaa-aaaaaaaaaaaa",
        )
        with self.assertRaises(wiki_render.ValidationError):
            self.render()

    def test_quarantine_without_real_correction_fails(self) -> None:
        # Quarantining a good event with a fictional corrected_by id must be
        # rejected — suppression requires a real replacement in the store.
        bad = make_garden_apply(
            "019ebb7f-aee4-7ee4-c421-cbe5e5451232",
            "2026-06-06T00:57:50Z",
            "019e937b-3f6d-7aa0-b453-a5494184e348",
            status="applied-manually",
        )
        self.write_event(bad)
        self.quarantine(
            bad["event_id"],
            corrected_by="019eb400-0000-7aaa-8aaa-aaaaaaaaaaaa",  # nonexistent
        )
        with self.assertRaises(wiki_render.ValidationError):
            self.render()

    def test_quarantine_entry_missing_fields_fails(self) -> None:
        self.quarantine_path.write_text(
            json.dumps({"quarantined": [{"event_id": "x"}]}), encoding="utf-8"
        )
        with self.assertRaises(wiki_render.ValidationError):
            self.render()

    def test_quarantine_path_event_id_mismatch_fails(self) -> None:
        self.quarantine_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "quarantined": [
                        {
                            "event_id": "019ebb7f-aee4-7ee4-c421-cbe5e5451232",
                            "path": "events/some-other-file.json",
                            "reason": "r",
                            "corrected_by": "c",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(wiki_render.ValidationError):
            self.render()

    def test_legacy_file_with_header_rejected(self) -> None:
        self.legacy_path.write_text("# Session Log\n\n" + LEGACY_TEXT, encoding="utf-8")
        with self.assertRaises(wiki_render.ValidationError):
            self.render()

    def test_full_output_byte_layout(self) -> None:
        # Byte layout IS the contract: the pre-commit hook compares rendered
        # output byte-for-byte.
        self.write_event(
            make_handoff(
                "019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-06T01:22:57Z",
                "First",
                what_was_done=["Did a thing"],
            )
        )
        self.write_event(
            make_handoff(
                "019e9b02-ddff-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-06T03:38:36Z",
                "Second",
            )
        )
        output = self.render()
        header = wiki_render.LOG_HEADER_TEMPLATE.format(boundary=BOUNDARY)
        expected = (
            header
            + "\n"
            + LEGACY_TEXT
            + "\n"
            + "## [2026-06-06T01:22:57Z] First @abc1234 branch:main "
            + "tool:claude-code event:019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa\n"
            + "- Did a thing\n"
            + "\n"
            + "## [2026-06-06T03:38:36Z] Second @abc1234 branch:main "
            + "tool:claude-code event:019e9b02-ddff-7aaa-8aaa-aaaaaaaaaaaa\n"
        )
        self.assertEqual(output, expected)
        for line in output.splitlines():
            if "First" in line or "Second" in line or "legacy entry" in line:
                self.assertTrue(line.startswith("## ["), line)
        self.assertTrue(output.endswith("\n"))
        self.assertFalse(output.endswith("\n\n"))

    def test_epoch_with_unknown_field_fails(self) -> None:
        # additionalProperties: false — a typo'd boundary key must not be
        # silently ignored.
        self.epoch_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "render_epoch_start": BOUNDARY,
                    "render_epoch_strat": BOUNDARY,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(wiki_render.ValidationError):
            self.render()

    def test_quarantine_missing_schema_version_fails(self) -> None:
        self.quarantine_path.write_text(
            json.dumps({"quarantined": []}), encoding="utf-8"
        )
        with self.assertRaises(wiki_render.ValidationError):
            self.render()

    def test_missing_render_epoch_key_fails(self) -> None:
        self.epoch_path.write_text(
            json.dumps({"legacy_epoch_entries": 100}), encoding="utf-8"
        )
        with self.assertRaises(wiki_render.ValidationError):
            self.render()

    def test_missing_legacy_file_fails(self) -> None:
        # render_log takes None for "absent"; a Path that does not exist is
        # a caller error, not a mode switch (the CLI wiring maps it to None).
        self.legacy_path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.render()

    # Blank deployment (design decision 5): no legacy log, no epoch — every
    # handoff event renders and the header drops the boundary sentence.

    def test_blank_mode_renders_all_events(self) -> None:
        self.write_event(
            make_handoff(
                "019e937b-3f6a-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-03T00:01:11Z",
                "Pre-boundary event",
            )
        )
        self.write_event(
            make_handoff(
                "019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-06T01:22:57Z",
                "Post-boundary event",
            )
        )
        output = self.render_blank()
        self.assertIn("Pre-boundary event", output)
        self.assertIn("Post-boundary event", output)
        self.assertNotIn("frozen legacy record", output)
        self.assertNotIn(LEGACY_TEXT, output)
        self.assertTrue(output.startswith(wiki_render.LOG_HEADER_NO_LEGACY))

    def test_blank_mode_zero_events_is_header_only(self) -> None:
        self.assertEqual(self.render_blank(), wiki_render.LOG_HEADER_NO_LEGACY)

    def test_blank_mode_byte_layout(self) -> None:
        self.write_event(
            make_handoff(
                "019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa",
                "2026-06-06T01:22:57Z",
                "First",
                what_was_done=["Did a thing"],
            )
        )
        expected = (
            wiki_render.LOG_HEADER_NO_LEGACY
            + "\n"
            + "## [2026-06-06T01:22:57Z] First @abc1234 branch:main "
            + "tool:claude-code event:019e9a86-ac51-7aaa-8aaa-aaaaaaaaaaaa\n"
            + "- Did a thing\n"
        )
        self.assertEqual(self.render_blank(), expected)

    def test_legacy_without_epoch_fails_loudly(self) -> None:
        with self.assertRaises(wiki_render.ValidationError) as ctx:
            wiki_render.render_log(
                events_dir=self.events_dir,
                epoch_path=None,
                legacy_path=self.legacy_path,
                quarantine_path=self.quarantine_path,
            )
        message = str(ctx.exception)
        self.assertIn("log-legacy.md", message)
        self.assertIn("log-epoch.json", message)

    def test_epoch_without_legacy_fails_loudly(self) -> None:
        with self.assertRaises(wiki_render.ValidationError) as ctx:
            wiki_render.render_log(
                events_dir=self.events_dir,
                epoch_path=self.epoch_path,
                legacy_path=None,
                quarantine_path=self.quarantine_path,
            )
        message = str(ctx.exception)
        self.assertIn("log-legacy.md", message)
        self.assertIn("log-epoch.json", message)


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "events").mkdir()
        (self.root / "log-epoch.json").write_text(
            json.dumps({"schema_version": 1, "render_epoch_start": BOUNDARY}),
            encoding="utf-8",
        )
        (self.root / "log-legacy.md").write_text(LEGACY_TEXT, encoding="utf-8")
        (self.root / "wiki.toml").write_text(MINIMAL_WIKI_TOML, encoding="utf-8")

    def run_cli(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "log",
                "--wiki",
                str(self.root),
                "--events-dir",
                str(self.root / "events"),
                "--epoch-file",
                str(self.root / "log-epoch.json"),
                "--legacy-file",
                str(self.root / "log-legacy.md"),
                "--quarantine-file",
                str(self.root / "quarantine.json"),
                "--output",
                str(self.root / "log.md"),
                *extra,
            ],
            capture_output=True,
            text=True,
        )

    def test_write_then_check_round_trip(self) -> None:
        write = self.run_cli()
        self.assertEqual(write.returncode, 0, write.stderr)
        check = self.run_cli("--check")
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_check_fails_on_stale_output(self) -> None:
        write = self.run_cli()
        self.assertEqual(write.returncode, 0, write.stderr)
        log_path = self.root / "log.md"
        log_path.write_text(
            log_path.read_text(encoding="utf-8") + "## hand-edit\n",
            encoding="utf-8",
        )
        check = self.run_cli("--check")
        self.assertEqual(check.returncode, 1)
        self.assertIn("does not match", check.stderr)

    def test_check_fails_cleanly_when_output_missing(self) -> None:
        check = self.run_cli("--check")
        self.assertEqual(check.returncode, 1)
        self.assertIn("error:", check.stderr)

    def test_malformed_event_exits_nonzero_with_clean_error(self) -> None:
        (self.root / "events" / "broken.json").write_text("{not json", encoding="utf-8")
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_wiki_toml_exits_nonzero(self) -> None:
        (self.root / "wiki.toml").unlink()
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("wiki.toml", result.stderr)

    def test_blank_deployment_renders_all_events(self) -> None:
        (self.root / "log-epoch.json").unlink()
        (self.root / "log-legacy.md").unlink()
        event = make_handoff(
            "019e937b-3f6a-7aaa-8aaa-aaaaaaaaaaaa",
            "2026-06-01T00:00:00Z",
            "Before the old boundary",
        )
        (self.root / "events" / f"{event['event_id']}.json").write_text(
            json.dumps(event, indent=2), encoding="utf-8"
        )
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (self.root / "log.md").read_text(encoding="utf-8")
        self.assertIn("Before the old boundary", text)
        self.assertNotIn("frozen legacy record", text)

    def test_lone_pair_member_fails_loudly(self) -> None:
        (self.root / "log-epoch.json").unlink()
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("log-legacy.md", result.stderr)
        self.assertIn("log-epoch.json", result.stderr)


PENDING_FIXED_LINES = [
    "- Read `wiki/pending/latest.md` before broad planning.",
    "- Treat pending framing as provisional.",
]


class PendingSectionTest(unittest.TestCase):
    def test_zero_pending_with_gardened_date(self) -> None:
        handoff = make_handoff(
            "019eb000-0000-7000-8000-000000000001",
            "2026-06-10T10:00:00Z",
            "Done work",
        )
        apply_event = make_garden_apply(
            "019eb000-0000-7000-8000-000000000002",
            "2026-06-10T18:00:00Z",
            handoff["event_id"],
        )
        lines = wiki_render.render_pending_lines([handoff, apply_event])
        self.assertEqual(lines, ["None — gardened 2026-06-10."])

    def test_zero_events_reports_no_applies(self) -> None:
        self.assertEqual(
            wiki_render.render_pending_lines([]),
            ["None — no garden applies recorded."],
        )

    def test_single_pending_is_singular(self) -> None:
        handoff = make_handoff(
            "019eb000-0000-7000-8000-000000000001",
            "2026-06-10T10:00:00Z",
            "Done work",
        )
        lines = wiki_render.render_pending_lines([handoff])
        self.assertEqual(
            lines,
            ["- 1 event since last garden apply.", *PENDING_FIXED_LINES],
        )

    def test_multiple_pending_is_plural(self) -> None:
        events = [
            make_handoff(
                f"019eb000-0000-7000-8000-00000000000{i}",
                f"2026-06-1{i}T10:00:00Z",
                f"Work {i}",
            )
            for i in range(1, 3)
        ]
        lines = wiki_render.render_pending_lines(events)
        self.assertEqual(lines[0], "- 2 events since last garden apply.")

    def test_rejected_apply_clears_pending(self) -> None:
        handoff = make_handoff(
            "019eb000-0000-7000-8000-000000000001",
            "2026-06-10T10:00:00Z",
            "Done work",
        )
        rejected = make_garden_apply(
            "019eb000-0000-7000-8000-000000000002",
            "2026-06-10T18:00:00Z",
            handoff["event_id"],
            status="rejected",
        )
        lines = wiki_render.render_pending_lines([handoff, rejected])
        self.assertEqual(lines, ["None — gardened 2026-06-10."])

    def test_latest_apply_date_wins(self) -> None:
        handoff = make_handoff(
            "019eb000-0000-7000-8000-000000000001",
            "2026-06-09T10:00:00Z",
            "Done work",
        )
        applies = [
            make_garden_apply(
                "019eb000-0000-7000-8000-000000000002",
                "2026-06-09T18:00:00Z",
                handoff["event_id"],
            ),
            make_garden_apply(
                "019eb000-0000-7000-8000-000000000003",
                "2026-06-11T18:00:00Z",
                handoff["event_id"],
            ),
        ]
        lines = wiki_render.render_pending_lines([handoff, *applies])
        self.assertEqual(lines, ["None — gardened 2026-06-11."])


class RecentSessionsTest(unittest.TestCase):
    def test_tail_of_log_headings(self) -> None:
        headings = [
            f"## [2026-06-0{i}T00:00:00Z] Entry {i} @abc1234 branch:main"
            for i in range(1, 8)
        ]
        log_text = "\n- bullet\n".join(headings)
        lines = wiki_render.render_recent_session_lines(log_text)
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[0], "- " + headings[2].removeprefix("## "))
        self.assertEqual(lines[-1], "- " + headings[-1].removeprefix("## "))

    def test_fewer_headings_than_window_keeps_all(self) -> None:
        log_text = "# Session Log\n\n## [2026-06-01T00:00:00Z] Only entry\n- bullet\n"
        lines = wiki_render.render_recent_session_lines(log_text)
        self.assertEqual(lines, ["- [2026-06-01T00:00:00Z] Only entry"])


class QuickstartTest(unittest.TestCase):
    def test_validate_rejects_section_heading(self) -> None:
        with self.assertRaises(wiki_render.ValidationError):
            wiki_render.validate_quickstart(
                "fine line\n## Workstreams\n", FIXTURE_INDEX_LINE
            )

    def test_validate_rejects_empty(self) -> None:
        with self.assertRaises(wiki_render.ValidationError):
            wiki_render.validate_quickstart("  \n\n", None)

    def test_validate_rejects_renderer_owned_lines(self) -> None:
        # Anything extract_quickstart would strip must be rejected on input,
        # or carry-forward silently drops LLM-supplied content.
        for owned in (
            FIXTURE_INDEX_LINE,
            wiki_render.DETAIL_LINE,
            wiki_render.ENTITIES_LINE,
            wiki_render.LEGACY_DETAIL_LINE,
            "Memory: some stale pointer",
            "Pending events: 3 (stale).",
        ):
            with (
                self.subTest(owned=owned),
                self.assertRaises(wiki_render.ValidationError),
            ):
                wiki_render.validate_quickstart(
                    f"fine line\n{owned}", FIXTURE_INDEX_LINE
                )

    def test_validate_rejects_configured_line_without_memory_prefix(self) -> None:
        with self.assertRaises(wiki_render.ValidationError):
            wiki_render.validate_quickstart(
                f"fine line\n{ALT_INDEX_LINE}", ALT_INDEX_LINE
            )

    def test_validate_rejects_memory_prefix_even_when_unset(self) -> None:
        with self.assertRaises(wiki_render.ValidationError):
            wiki_render.validate_quickstart("fine line\nMemory: stale pointer", None)

    def test_extract_strips_renderer_owned_lines(self) -> None:
        existing = "\n".join(
            [
                "> Rebuilt by /garden — 2026-06-12T06:15:43Z",
                "",
                "## Quickstart",
                "",
                "**Hot:** thing one.",
                "Next: thing two.",
                "Pending events: 0 (gardened 2026-06-12).",
                FIXTURE_INDEX_LINE,
                wiki_render.DETAIL_LINE,
                wiki_render.ENTITIES_LINE,
                "",
                "## Workstreams",
                "",
                "ACTIVE",
            ]
        )
        carried = wiki_render.extract_quickstart(existing, FIXTURE_INDEX_LINE)
        self.assertEqual(carried, "**Hot:** thing one.\nNext: thing two.")

    def test_extract_strips_stale_memory_line_after_config_change(self) -> None:
        # The configured pointer changed since the last render; the old
        # "Memory: " line must not survive the carry-forward.
        existing = "\n".join(
            [
                "## Quickstart",
                "",
                "**Hot:** thing one.",
                "Memory: /home/alex/notes/MEMORY.md",
                ALT_INDEX_LINE,
                "",
                "## Workstreams",
            ]
        )
        carried = wiki_render.extract_quickstart(existing, ALT_INDEX_LINE)
        self.assertEqual(carried, "**Hot:** thing one.")

    def test_extract_strips_memory_prefix_when_unset(self) -> None:
        existing = "\n".join(
            [
                "## Quickstart",
                "",
                "**Hot:** thing one.",
                "Memory: /home/alex/notes/MEMORY.md",
                "",
                "## Workstreams",
            ]
        )
        carried = wiki_render.extract_quickstart(existing, None)
        self.assertEqual(carried, "**Hot:** thing one.")

    def test_extract_missing_heading_fails(self) -> None:
        with self.assertRaises(wiki_render.ValidationError):
            wiki_render.extract_quickstart("# Something else\n\nbody\n", None)

    def test_extract_empty_quickstart_fails(self) -> None:
        existing = "## Quickstart\n\n\n## Workstreams\nACTIVE\n"
        with self.assertRaises(wiki_render.ValidationError):
            wiki_render.extract_quickstart(existing, None)


class WorkstreamsTreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def fake_script(self, body: str) -> Path:
        path = self.root / "fake-build-index.py"
        path.write_text(body, encoding="utf-8")
        return path

    def test_returns_script_stdout(self) -> None:
        script = self.fake_script('print("ACTIVE\\n├─ one\\n└─ two")\n')
        tree = wiki_render.workstreams_tree(self.root, script)
        self.assertEqual(tree, "ACTIVE\n├─ one\n└─ two")

    def test_passes_wiki_root_flag(self) -> None:
        script = self.fake_script("import sys\nprint(' '.join(sys.argv[1:]))\n")
        tree = wiki_render.workstreams_tree(self.root, script)
        self.assertEqual(tree, f"--wiki {self.root}")

    def test_nonzero_exit_fails_loudly(self) -> None:
        script = self.fake_script(
            'import sys\nprint("bad frontmatter", file=sys.stderr)\nsys.exit(2)\n'
        )
        with self.assertRaises(wiki_render.ValidationError):
            wiki_render.workstreams_tree(self.root, script)


class ClaudeLocalAssemblyTest(unittest.TestCase):
    def assemble(self, **overrides: Any) -> str:
        kwargs: dict[str, Any] = {
            "quickstart": "**Hot:** thing one.\nNext: thing two.",
            "pending_lines": ["None — gardened 2026-06-10."],
            "tree": "ACTIVE\n└─ notes-system",
            "recent_lines": ["- [2026-06-11T00:00:00Z] Entry"],
            "uncommitted_lines": ["- wiki repo: clean"],
            "now": "2026-06-12T00:00:00Z",
            "memory_index_line": FIXTURE_INDEX_LINE,
        }
        kwargs.update(overrides)
        return wiki_render.render_claude_local(**kwargs)

    def test_full_byte_layout(self) -> None:
        expected = (
            "> Generated by scripts/wiki-render.py — 2026-06-12T00:00:00Z\n"
            "\n"
            "## Quickstart\n"
            "\n"
            "**Hot:** thing one.\n"
            "Next: thing two.\n"
            "\n"
            f"{FIXTURE_INDEX_LINE}\n"
            f"{wiki_render.DETAIL_LINE}\n"
            f"{wiki_render.ENTITIES_LINE}\n"
            "\n"
            "## Pending unreviewed handoffs\n"
            "\n"
            "None — gardened 2026-06-10.\n"
            "\n"
            "## Workstreams\n"
            "\n"
            "ACTIVE\n"
            "└─ notes-system\n"
            "\n"
            "## Recent Sessions (last 5 log entries — log.md is the "
            "generated projection)\n"
            "\n"
            "- [2026-06-11T00:00:00Z] Entry\n"
            "\n"
            "## Uncommitted Changes\n"
            "\n"
            "- wiki repo: clean\n"
        )
        self.assertEqual(self.assemble(), expected)

    def test_index_line_omitted_when_unset(self) -> None:
        # [memory].index_line absent = the line is absent, not blank.
        expected = (
            "> Generated by scripts/wiki-render.py — 2026-06-12T00:00:00Z\n"
            "\n"
            "## Quickstart\n"
            "\n"
            "**Hot:** thing one.\n"
            "Next: thing two.\n"
            "\n"
            f"{wiki_render.DETAIL_LINE}\n"
            f"{wiki_render.ENTITIES_LINE}\n"
            "\n"
            "## Pending unreviewed handoffs\n"
            "\n"
            "None — gardened 2026-06-10.\n"
            "\n"
            "## Workstreams\n"
            "\n"
            "ACTIVE\n"
            "└─ notes-system\n"
            "\n"
            "## Recent Sessions (last 5 log entries — log.md is the "
            "generated projection)\n"
            "\n"
            "- [2026-06-11T00:00:00Z] Entry\n"
            "\n"
            "## Uncommitted Changes\n"
            "\n"
            "- wiki repo: clean\n"
        )
        self.assertEqual(self.assemble(memory_index_line=None), expected)

    def test_render_then_extract_round_trip(self) -> None:
        quickstart = "**Hot:** thing one.\nNext: thing two."
        rendered = self.assemble(quickstart=quickstart)
        self.assertEqual(
            wiki_render.extract_quickstart(rendered, FIXTURE_INDEX_LINE), quickstart
        )

    def test_round_trip_without_index_line(self) -> None:
        quickstart = "**Hot:** thing one.\nNext: thing two."
        rendered = self.assemble(quickstart=quickstart, memory_index_line=None)
        self.assertEqual(wiki_render.extract_quickstart(rendered, None), quickstart)

    def test_bad_timestamp_fails(self) -> None:
        with self.assertRaises(wiki_render.ValidationError):
            self.assemble(now="2026-06-12 00:00:00")

    def test_non_canonical_timestamp_fails(self) -> None:
        # strptime alone is lenient about zero-padding; the banner embeds
        # the string verbatim, so only canonical form is acceptable.
        with self.assertRaises(wiki_render.ValidationError):
            self.assemble(now="2026-6-1T0:0:0Z")

    def test_quickstart_with_heading_fails(self) -> None:
        with self.assertRaises(wiki_render.ValidationError):
            self.assemble(quickstart="fine\n## Workstreams sneaking in")

    def test_line_budget_overrun(self) -> None:
        within = "\n".join(["x"] * wiki_render.LINE_BUDGET)
        over = "\n".join(["x"] * (wiki_render.LINE_BUDGET + 3))
        self.assertEqual(wiki_render.line_budget_overrun(within), 0)
        self.assertEqual(wiki_render.line_budget_overrun(over), 3)

    def test_estimate_token_count_matches_doctor_heuristic(self) -> None:
        text = "x" * 12_004
        self.assertEqual(wiki_render.estimate_token_count(text), 3001)
        self.assertEqual(wiki_render.estimate_token_count(""), 0)


# Isolate scratch repos from the developer's global/system git config
# (gpgsign, hooksPath, init templates would otherwise break them).
GIT_TEST_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        env=GIT_TEST_ENV,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "seed")
    return repo


class GitFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def load_config(self, wiki_toml: str, overlay: str | None = None) -> Any:
        (self.root / "wiki.toml").write_text(wiki_toml, encoding="utf-8")
        if overlay is not None:
            (self.root / "wiki.local.toml").write_text(overlay, encoding="utf-8")
        return wiki_config.load_config(self.root)

    def test_clean_repo_facts(self) -> None:
        repo = make_repo(self.root, "clean")
        facts = wiki_render.tree_facts("clean repo", repo)
        self.assertEqual(facts.branch, "main")
        self.assertEqual((facts.dirty, facts.staged), (0, 0))
        self.assertIsNone(facts.unpushed)  # no upstream configured

    def test_dirty_and_staged_counts(self) -> None:
        repo = make_repo(self.root, "dirty")
        (repo / "staged.txt").write_text("a\n", encoding="utf-8")
        (repo / "unstaged.txt").write_text("b\n", encoding="utf-8")
        git(repo, "add", "staged.txt")
        facts = wiki_render.tree_facts("dirty repo", repo)
        self.assertEqual((facts.dirty, facts.staged), (2, 1))

    def test_unstaged_modification_first_in_porcelain_not_staged(self) -> None:
        # ' M README.md' is the first porcelain line; a stripped leading
        # space would miscount it as staged (a past must-fix regression).
        repo = make_repo(self.root, "mod")
        (repo / "README.md").write_text("changed\n", encoding="utf-8")
        facts = wiki_render.tree_facts("mod repo", repo)
        self.assertEqual((facts.dirty, facts.staged), (1, 0))

    def test_unpushed_counted_against_upstream(self) -> None:
        origin = self.root / "origin.git"
        origin.mkdir()
        git(origin, "init", "-q", "--bare", "-b", "main")
        repo = make_repo(self.root, "tracked")
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "-u", "origin", "main")
        facts = wiki_render.tree_facts("tracked", repo)
        self.assertEqual(facts.unpushed, 0)
        # Upstream exists: the remote-reachability probe is not run.
        self.assertIsNone(facts.remote_has_head)
        (repo / "new.txt").write_text("c\n", encoding="utf-8")
        git(repo, "add", "new.txt")
        git(repo, "commit", "-q", "-m", "ahead")
        self.assertEqual(wiki_render.tree_facts("tracked", repo).unpushed, 1)

    def test_no_upstream_no_remote_is_unpushed(self) -> None:
        repo = make_repo(self.root, "loner")
        facts = wiki_render.tree_facts("loner", repo)
        self.assertIsNone(facts.unpushed)
        self.assertIs(facts.remote_has_head, False)

    def test_no_upstream_but_head_on_remote_is_pushed(self) -> None:
        # A branch pushed with an explicit refspec but no tracking config
        # must not read as unpushed.
        origin = self.root / "origin.git"
        origin.mkdir()
        git(origin, "init", "-q", "--bare", "-b", "main")
        repo = make_repo(self.root, "pushed-untracked")
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")  # no -u on purpose
        facts = wiki_render.tree_facts("pushed-untracked", repo)
        self.assertIsNone(facts.unpushed)
        self.assertTrue(facts.remote_has_head)
        # A new local commit flips it back to genuinely unpushed.
        (repo / "new.txt").write_text("c\n", encoding="utf-8")
        git(repo, "add", "new.txt")
        git(repo, "commit", "-q", "-m", "local-only")
        self.assertIs(
            wiki_render.tree_facts("pushed-untracked", repo).remote_has_head,
            False,
        )

    def test_detached_head_uses_remote_reachability(self) -> None:
        # A detached worktree: @{upstream} fails on detached HEAD, routing
        # into the probe.
        origin = self.root / "origin.git"
        origin.mkdir()
        git(origin, "init", "-q", "--bare", "-b", "main")
        repo = make_repo(self.root, "detached")
        git(repo, "remote", "add", "origin", str(origin))
        git(repo, "push", "-q", "origin", "main")
        git(repo, "checkout", "-q", "--detach")
        facts = wiki_render.tree_facts("detached", repo)
        self.assertEqual(facts.branch, "detached")
        self.assertIsNone(facts.unpushed)
        self.assertIs(facts.remote_has_head, True)

    def test_worktrees_enumerated_without_main_duplicate(self) -> None:
        widget = make_repo(self.root, "widget")
        wiki = make_repo(self.root, "wiki")
        git(
            widget,
            "worktree",
            "add",
            "-q",
            str(self.root / "wt-feature"),
            "-b",
            "feature",
        )
        config = self.load_config(
            COMPANION_WIKI_TOML,
            f'[companions.widget]\npath = "{widget}"\n',
        )
        facts = wiki_render.collect_uncommitted_facts(
            wiki, list(config.companions.values())
        )
        labels = [f.label for f in facts]
        self.assertEqual(
            labels,
            [
                "wiki repo",
                config.companion("widget").display_label,
                "worktree wt-feature",
            ],
        )

    def test_missing_companion_checkout_fails_loudly(self) -> None:
        wiki = make_repo(self.root, "wiki")
        config = self.load_config(
            COMPANION_WIKI_TOML,
            f'[companions.widget]\npath = "{self.root / "nope"}"\n',
        )
        with self.assertRaises(wiki_render.ValidationError):
            wiki_render.collect_uncommitted_facts(
                wiki, list(config.companions.values())
            )

    def test_registered_but_missing_worktree_fails_loudly(self) -> None:
        widget = make_repo(self.root, "widget")
        wiki = make_repo(self.root, "wiki")
        gone = self.root / "wt-gone"
        git(widget, "worktree", "add", "-q", str(gone), "-b", "gone")
        shutil.rmtree(gone)
        config = self.load_config(
            COMPANION_WIKI_TOML,
            f'[companions.widget]\npath = "{widget}"\n',
        )
        with self.assertRaises(wiki_render.ValidationError):
            wiki_render.collect_uncommitted_facts(
                wiki, list(config.companions.values())
            )

    def test_companion_without_local_checkout_gets_fixed_note(self) -> None:
        wiki = make_repo(self.root, "wiki")
        config = self.load_config(COMPANION_WIKI_TOML)  # no overlay = no path
        facts = wiki_render.collect_uncommitted_facts(
            wiki, list(config.companions.values())
        )
        label = config.companion("widget").display_label
        self.assertEqual([f.label for f in facts], ["wiki repo", label])
        self.assertIsInstance(facts[1], wiki_render.MissingCheckout)
        lines = wiki_render.render_uncommitted_lines(facts)
        self.assertIn(
            f"- {label}: no local checkout configured on this machine", lines
        )

    def test_zero_companions_renders_only_wiki_row(self) -> None:
        wiki = make_repo(self.root, "wiki")
        config = self.load_config(MINIMAL_WIKI_TOML)
        facts = wiki_render.collect_uncommitted_facts(
            wiki, list(config.companions.values())
        )
        self.assertEqual([f.label for f in facts], ["wiki repo"])

    def test_companions_render_in_config_order(self) -> None:
        widget = make_repo(self.root, "widget")
        wiki = make_repo(self.root, "wiki")
        config = self.load_config(
            TWO_COMPANION_WIKI_TOML,
            f'[companions.widget]\npath = "{widget}"\n',
        )
        facts = wiki_render.collect_uncommitted_facts(
            wiki, list(config.companions.values())
        )
        self.assertEqual(
            [f.label for f in facts],
            [
                "wiki repo",
                config.companion("gizmo").display_label,
                config.companion("widget").display_label,
            ],
        )
        self.assertIsInstance(facts[1], wiki_render.MissingCheckout)


class UncommittedLinesTest(unittest.TestCase):
    def test_dirty_staged_unpushed_line(self) -> None:
        facts = [wiki_render.TreeFacts("widget main", "main", "abc1234", 48, 3, 2)]
        self.assertEqual(
            wiki_render.render_uncommitted_lines(facts),
            ["- widget main (main @ abc1234): 48 dirty (3 staged), 2 unpushed"],
        )

    def test_clean_trees_collapse(self) -> None:
        facts = [
            wiki_render.TreeFacts("wiki repo", "main", "abc1234", 0, 0, 0),
            wiki_render.TreeFacts("worktree x", "feat", "def5678", 0, 0, 0),
        ]
        self.assertEqual(
            wiki_render.render_uncommitted_lines(facts),
            ["- clean: wiki repo, worktree x"],
        )

    def test_no_upstream_distinguishes_pushed_from_unpushed(self) -> None:
        unpushed = [
            wiki_render.TreeFacts(
                "worktree y",
                "local",
                "abc1234",
                0,
                0,
                None,
                remote_has_head=False,
            )
        ]
        self.assertEqual(
            wiki_render.render_uncommitted_lines(unpushed),
            ["- worktree y (local @ abc1234): no upstream (unpushed)"],
        )
        pushed = [
            wiki_render.TreeFacts(
                "worktree z", "feat", "def5678", 0, 0, None, remote_has_head=True
            )
        ]
        self.assertEqual(
            wiki_render.render_uncommitted_lines(pushed),
            ["- worktree z (feat @ def5678): no upstream (pushed)"],
        )

    def test_no_upstream_without_probe_fails_loudly(self) -> None:
        # unpushed=None with remote_has_head=None means tree_facts skipped
        # the reachability probe — render must refuse, not guess.
        facts = [wiki_render.TreeFacts("worktree q", "local", "abc1234", 0, 0, None)]
        with self.assertRaises(wiki_render.ValidationError):
            wiki_render.render_uncommitted_lines(facts)

    def test_missing_checkout_renders_fixed_note(self) -> None:
        lines = wiki_render.render_uncommitted_lines(
            [wiki_render.MissingCheckout("widget main")]
        )
        self.assertEqual(
            lines, ["- widget main: no local checkout configured on this machine"]
        )


class GardenLockWrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._original_script = wiki_render.GARDEN_LOCK_SCRIPT
        self.addCleanup(
            setattr, wiki_render, "GARDEN_LOCK_SCRIPT", self._original_script
        )

    def install_lock_script(self, body: str) -> None:
        path = self.root / "fake-garden-lock.py"
        path.write_text(body, encoding="utf-8")
        wiki_render.GARDEN_LOCK_SCRIPT = path

    def test_acquire_parses_token_after_stale_clear_preamble(self) -> None:
        self.install_lock_script(
            'print("clearing stale lock (pid=1 age=9000s)")\n'
            'print("locked token=deadbeef")\n'
        )
        self.assertEqual(wiki_render.acquire_garden_lock(self.root), "deadbeef")

    def test_acquire_passes_wiki_root_flag(self) -> None:
        marker = self.root / "argv.txt"
        self.install_lock_script(
            "import sys\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text(' '.join(sys.argv[1:]))\n"
            'print("locked token=t1")\n'
        )
        wiki_render.acquire_garden_lock(self.root)
        self.assertEqual(marker.read_text(), f"acquire --wiki {self.root}")

    def test_acquire_held_raises(self) -> None:
        self.install_lock_script(
            'import sys\nprint("held (pid=2 age=10s)", file=sys.stderr)\nsys.exit(1)\n'
        )
        with self.assertRaises(wiki_render.ValidationError):
            wiki_render.acquire_garden_lock(self.root)

    def test_acquire_unrecognized_output_raises(self) -> None:
        self.install_lock_script('print("ok")\n')
        with self.assertRaises(wiki_render.ValidationError):
            wiki_render.acquire_garden_lock(self.root)

    def test_release_failure_raises(self) -> None:
        self.install_lock_script(
            'import sys\nprint("token does not match", file=sys.stderr)\nsys.exit(1)\n'
        )
        with self.assertRaises(wiki_render.ValidationError):
            wiki_render.release_garden_lock("deadbeef", self.root)

    def test_lock_released_when_render_fails(self) -> None:
        marker = self.root / "released.txt"
        self.install_lock_script(
            "import sys\nfrom pathlib import Path\n"
            'if sys.argv[1] == "acquire":\n'
            '    print("locked token=t1")\n'
            'elif sys.argv[1] == "release":\n'
            f"    Path({str(marker)!r}).write_text(sys.argv[2])\n"
        )
        (self.root / "wiki.toml").write_text(MINIMAL_WIKI_TOML, encoding="utf-8")
        quickstart = self.root / "quickstart.md"
        quickstart.write_text("**Hot:** text.\n", encoding="utf-8")
        args = wiki_render.build_parser().parse_args(
            [
                "claude-local",
                "--wiki",
                str(self.root),
                "--events-dir",
                str(self.root / "missing-events"),
                "--quickstart-file",
                str(quickstart),
                "--output",
                str(self.root / "CLAUDE.local.md"),
            ]
        )
        with self.assertRaises(wiki_render.ValidationError):
            wiki_render.cmd_claude_local(args)
        self.assertEqual(marker.read_text(), "t1")


class ClaudeLocalCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "events").mkdir()
        (self.root / "log-epoch.json").write_text(
            json.dumps({"schema_version": 1, "render_epoch_start": BOUNDARY}),
            encoding="utf-8",
        )
        (self.root / "log-legacy.md").write_text(LEGACY_TEXT, encoding="utf-8")
        self.widget = make_repo(self.root, "widget")
        self.wiki = make_repo(self.root, "wiki")
        (self.root / "wiki.toml").write_text(
            CLI_WIKI_TOML_TEMPLATE.format(index_line=FIXTURE_INDEX_LINE),
            encoding="utf-8",
        )
        (self.root / "wiki.local.toml").write_text(
            f'[companions.widget]\npath = "{self.widget}"\n', encoding="utf-8"
        )
        self.config = wiki_config.load_config(self.root)
        self.build_index = self.root / "fake-build-index.py"
        self.build_index.write_text(
            'print("ACTIVE\\n└─ test-stream")\n', encoding="utf-8"
        )
        self.output = self.root / "CLAUDE.local.md"

    def cli_args(self, *extra: str) -> list[str]:
        return [
            sys.executable,
            str(SCRIPT),
            "claude-local",
            "--wiki",
            str(self.root),
            "--events-dir",
            str(self.root / "events"),
            "--epoch-file",
            str(self.root / "log-epoch.json"),
            "--legacy-file",
            str(self.root / "log-legacy.md"),
            "--quarantine-file",
            str(self.root / "quarantine.json"),
            "--output",
            str(self.output),
            "--wiki-repo",
            str(self.wiki),
            "--build-index-script",
            str(self.build_index),
            "--no-lock",
            *extra,
        ]

    def run_cli(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.cli_args("--now", "2026-06-12T00:00:00Z", *extra),
            capture_output=True,
            text=True,
        )

    def write_quickstart(self, text: str) -> Path:
        path = self.root / "quickstart.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_renders_all_sections_from_quickstart_file(self) -> None:
        qs = self.write_quickstart("**Hot:** stage 2 underway.\n")
        result = self.run_cli("--quickstart-file", str(qs))
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.output.read_text(encoding="utf-8")
        self.assertTrue(
            text.startswith(
                "> Generated by scripts/wiki-render.py — 2026-06-12T00:00:00Z\n"
            )
        )
        for heading in (
            wiki_render.QUICKSTART_HEADING,
            wiki_render.PENDING_HEADING,
            wiki_render.WORKSTREAMS_HEADING,
            wiki_render.RECENT_HEADING,
            wiki_render.UNCOMMITTED_HEADING,
        ):
            self.assertIn(f"\n{heading}\n", text)
        self.assertIn("**Hot:** stage 2 underway.", text)
        # The memory pointer comes from the fixture config, not a constant.
        self.assertIsNotNone(self.config.memory_index_line)
        self.assertIn(f"\n{self.config.memory_index_line}\n", text)
        self.assertIn("None — no garden applies recorded.", text)
        self.assertIn("└─ test-stream", text)
        self.assertIn("- [2026-06-04T16:30:00Z] Last legacy entry", text)
        # The companion row label re-anchors to the config display_label.
        label = self.config.companion("widget").display_label
        self.assertIn(f"- {label} (main @ ", text)

    def test_render_is_deterministic(self) -> None:
        qs = self.write_quickstart("**Hot:** same text.\n")
        first = self.run_cli("--quickstart-file", str(qs))
        self.assertEqual(first.returncode, 0, first.stderr)
        first_bytes = self.output.read_bytes()
        second = self.run_cli("--quickstart-file", str(qs))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first_bytes, self.output.read_bytes())

    def test_carry_forward_preserves_quickstart(self) -> None:
        qs = self.write_quickstart("**Hot:** carried text.\n")
        first = self.run_cli("--quickstart-file", str(qs))
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_cli()  # no --quickstart-file: carry forward
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn(
            "**Hot:** carried text.",
            self.output.read_text(encoding="utf-8"),
        )

    def test_carry_forward_refreshes_pending_count(self) -> None:
        qs = self.write_quickstart("**Hot:** carried text.\n")
        first = self.run_cli("--quickstart-file", str(qs))
        self.assertEqual(first.returncode, 0, first.stderr)
        event = make_handoff(
            "019eb000-0000-7000-8000-000000000001",
            "2026-06-11T10:00:00Z",
            "New session work",
            what_was_done=["Did a thing"],
        )
        (self.root / "events" / f"{event['event_id']}.json").write_text(
            json.dumps(event, indent=2), encoding="utf-8"
        )
        second = self.run_cli()
        self.assertEqual(second.returncode, 0, second.stderr)
        text = self.output.read_text(encoding="utf-8")
        self.assertIn("- 1 event since last garden apply.", text)
        self.assertIn("**Hot:** carried text.", text)
        self.assertIn("New session work", text)  # recent sessions updated

    def test_carry_forward_strips_stale_memory_line(self) -> None:
        # A config change to [memory].index_line must not leave the old
        # pointer behind on carry-forward.
        qs = self.write_quickstart("**Hot:** carried text.\n")
        first = self.run_cli("--quickstart-file", str(qs))
        self.assertEqual(first.returncode, 0, first.stderr)
        old_line = self.config.memory_index_line
        self.assertIn(old_line, self.output.read_text(encoding="utf-8"))
        (self.root / "wiki.toml").write_text(
            CLI_WIKI_TOML_TEMPLATE.format(
                index_line="Memory: the replacement pointer"
            ),
            encoding="utf-8",
        )
        new_config = wiki_config.load_config(self.root)
        second = self.run_cli()  # carry forward under the new config
        self.assertEqual(second.returncode, 0, second.stderr)
        text = self.output.read_text(encoding="utf-8")
        self.assertIn("**Hot:** carried text.", text)
        self.assertNotIn(old_line, text)
        self.assertIn(f"\n{new_config.memory_index_line}\n", text)

    def test_carry_forward_without_existing_file_fails(self) -> None:
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_quickstart_with_heading_rejected(self) -> None:
        qs = self.write_quickstart("fine\n## Workstreams\n")
        result = self.run_cli("--quickstart-file", str(qs))
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)

    def test_missing_companion_checkout_exits_nonzero(self) -> None:
        qs = self.write_quickstart("**Hot:** text.\n")
        (self.root / "wiki.local.toml").write_text(
            f'[companions.widget]\npath = "{self.root / "missing"}"\n',
            encoding="utf-8",
        )
        result = self.run_cli("--quickstart-file", str(qs))
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)
        self.assertFalse(self.output.exists())

    def test_companion_without_checkout_renders_note(self) -> None:
        qs = self.write_quickstart("**Hot:** text.\n")
        (self.root / "wiki.local.toml").unlink()
        result = self.run_cli("--quickstart-file", str(qs))
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.output.read_text(encoding="utf-8")
        label = self.config.companion("widget").display_label
        self.assertIn(
            f"- {label}: no local checkout configured on this machine", text
        )

    def test_token_warning_fires_when_over_budget(self) -> None:
        padding = "x" * 13_000
        qs = self.write_quickstart(f"**Hot:** {padding}\n")
        result = self.run_cli("--quickstart-file", str(qs))
        self.assertEqual(result.returncode, 0)
        self.assertIn("token", result.stderr)
        self.assertIn("doctor will FAIL", result.stderr)

    def test_no_token_warning_when_within_budget(self) -> None:
        qs = self.write_quickstart("**Hot:** small.\n")
        result = self.run_cli("--quickstart-file", str(qs))
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("token", result.stderr)


class BlankDeploymentCliTest(unittest.TestCase):
    """A blank deployment: zero events history conventions — no legacy log,
    no epoch, zero companions, no [memory].index_line."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "events").mkdir()
        (self.root / "wiki.toml").write_text(MINIMAL_WIKI_TOML, encoding="utf-8")
        event = make_handoff(
            "019e937b-3f6a-7aaa-8aaa-aaaaaaaaaaaa",
            "2026-06-01T00:00:00Z",
            "Founding session",
            what_was_done=["Booted the wiki"],
        )
        (self.root / "events" / f"{event['event_id']}.json").write_text(
            json.dumps(event, indent=2), encoding="utf-8"
        )
        self.wiki = make_repo(self.root, "wiki")
        self.build_index = self.root / "fake-build-index.py"
        self.build_index.write_text('print("ACTIVE\\n└─ first")\n', encoding="utf-8")
        self.output = self.root / "CLAUDE.local.md"

    def test_log_renders_every_event(self) -> None:
        # Derived --epoch-file/--legacy-file paths do not exist: blank mode.
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "log",
                "--wiki",
                str(self.root),
                "--events-dir",
                str(self.root / "events"),
                "--quarantine-file",
                str(self.root / "quarantine.json"),
                "--output",
                str(self.root / "log.md"),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (self.root / "log.md").read_text(encoding="utf-8")
        self.assertIn("Founding session", text)
        self.assertNotIn("frozen legacy record", text)

    def test_claude_local_renders_without_memory_line(self) -> None:
        qs = self.root / "quickstart.md"
        qs.write_text("**Hot:** first boot.\n", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "claude-local",
                "--wiki",
                str(self.root),
                "--events-dir",
                str(self.root / "events"),
                "--quarantine-file",
                str(self.root / "quarantine.json"),
                "--output",
                str(self.output),
                "--wiki-repo",
                str(self.wiki),
                "--build-index-script",
                str(self.build_index),
                "--quickstart-file",
                str(qs),
                "--now",
                "2026-06-12T00:00:00Z",
                "--no-lock",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.output.read_text(encoding="utf-8")
        self.assertIn("**Hot:** first boot.", text)
        # No [memory].index_line configured = no memory pointer line at all.
        self.assertNotIn("\nMemory: ", text)
        self.assertIn(f"\n{wiki_render.DETAIL_LINE}\n", text)
        # Zero companions: only the wiki repo row, no checkout notes.
        self.assertNotIn("no local checkout", text)
        self.assertIn("Founding session", text)
        self.assertIn("- 1 event since last garden apply.", text)


if __name__ == "__main__":
    unittest.main()
