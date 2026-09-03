#!/usr/bin/env python3
"""Smoke tests for the wiki-event CLI.

The JSON fixtures in this directory are v1-format events and carry the
legacy `"aura"` envelope key by design: v1 is the documented permanent
legacy shim (V1_ENVELOPE_KEY / V1_REPO_NAME in wiki_event.py), and these
fixtures plus the envelope-boundary tests below are the one allowed
enclave for that string in the kit. Everything v2 is fixtured to the
fictional acme-notes deployment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "wiki-event.py"
TEST_DIR = REPO_ROOT / "tests" / "wiki-event"

sys.path.insert(0, str(REPO_ROOT))

from scripts import wiki_event  # noqa: E402


class CliHarness(unittest.TestCase):
    """Shared subprocess helpers for every CLI test class."""

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def make_uuid7(self, ms: int) -> str:
        """Canonical UUIDv7 with the given ms timestamp and zero random bits."""
        value = (ms & ((1 << 48) - 1)) << 80
        value |= 0x7 << 76
        value |= 0b10 << 62
        hex_value = f"{value:032x}"
        return (
            f"{hex_value[0:8]}-{hex_value[8:12]}-{hex_value[12:16]}-"
            f"{hex_value[16:20]}-{hex_value[20:32]}"
        )

    def write_garden_apply_event(self, tmp: Path, **overrides: str) -> Path:
        event = {
            "schema_version": 1,
            "event_id": "019e9a43-7c86-7fe2-8bd8-0aa544f396d9",
            "event_type": "garden-apply",
            "timestamp_utc": "2026-06-06T12:00:00Z",
            "target_event_id": "019e9a42-641a-7b85-884e-8bbc83acfa87",
            "status": "applied",
            **overrides,
        }
        event_path = tmp / "garden-apply.json"
        event_path.write_text(json.dumps(event, indent=2) + "\n")
        return event_path

    def create_event(self, events_dir: Path) -> Path:
        result = self.assert_command_ok(
            "new-handoff",
            "--events-dir",
            events_dir,
            "--tool",
            "manual",
            "--summary",
            "Generated event validates.",
            "--repo-name",
            "acme-notes",
            "--repo-branch",
            "main",
            "--repo-sha",
            "4c0f549",
            "--source",
            "plan=plans/example.md",
            "--workstream",
            "wiki-system:primary:update",
        )
        return Path(result.stdout.strip())

    def assert_command_ok(
        self, *args: object, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = self.run_command(*args, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def assert_command_fails(
        self, *args: object, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = self.run_command(*args, cwd=cwd)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("error:", result.stderr)
        return result

    def run_command(
        self, *args: object, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        # Lock timeout pinned to 0 so held-lock tests fail immediately
        # instead of waiting out the production short-wait default.
        env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "WIKI_EVENT_LOCK_TIMEOUT_SECONDS": "0",
        }
        return subprocess.run(
            [str(SCRIPT), *(str(arg) for arg in args)],
            check=False,
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
        )


class WikiEventCliTest(CliHarness):
    def test_valid_fixture_passes(self) -> None:
        self.assert_command_ok("validate", TEST_DIR / "valid-handoff-event.json")

    def test_invalid_fixtures_fail(self) -> None:
        invalid_fixtures = [
            "invalid-bad-enum.json",
            "invalid-bad-event-type.json",
            "invalid-bad-tool.json",
            "invalid-bad-status.json",
            "invalid-missing-required.json",
            "invalid-bool-schema-version.json",
            "invalid-uppercase-event-id.json",
            "invalid-non-slug-source-kind.json",
        ]
        for fixture in invalid_fixtures:
            with self.subTest(fixture=fixture):
                self.assert_command_fails("validate", TEST_DIR / fixture)

    def test_pi_tool_rejected_for_v2_events(self) -> None:
        # K2 audit outcome: the family-specific "pi" member is dropped from
        # the kit's tool enum (schema and Python in lockstep); a v2 event
        # carrying it must fail validation.
        self.assertNotIn("pi", wiki_event.enum_values(wiki_event.Tool))
        event = {
            "schema_version": 2,
            "event_id": self.make_uuid7(self.now_ms() - 1000),
            "event_type": "handoff",
            "timestamp_utc": "2026-06-01T12:00:00Z",
            "tool": "pi",
            "repo": {"name": "acme-notes", "branch": "main", "sha": "4c0f549"},
            "sources": [],
            "proposed_workstreams": [
                {
                    "name": "wiki-system",
                    "relationship": "primary",
                    "proposed_action": "update",
                }
            ],
            "summary": "The dropped pi tool value must be rejected.",
            "status": "pending_garden",
        }
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "pi-tool.json"
            event_path.write_text(json.dumps(event))
            self.assert_command_fails("validate", event_path)

    def test_generated_event_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            result = self.assert_command_ok(
                "new-handoff",
                "--events-dir",
                events_dir,
                "--tool",
                "manual",
                "--summary",
                "Generated event validates.",
                "--repo-name",
                "acme-notes",
                "--repo-branch",
                "main",
                "--repo-sha",
                "4c0f549",
                "--source",
                "plan=plans/example.md",
                "--workstream",
                "wiki-system:primary:update",
            )
            event_path = Path(result.stdout.strip())
            self.assertTrue(event_path.is_file())
            self.assert_command_ok("validate", event_path)
            self.assertTrue((events_dir.parent / "pending" / "index.json").is_file())

    def test_capture_sources_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            event_path = self.create_event(events_dir)
            sources_dir = Path(tmp) / "sources"
            result = self.assert_command_ok(
                "capture-sources",
                "--event",
                event_path,
                "--events-dir",
                events_dir,
                "--sources-dir",
                sources_dir,
                "--source",
                f"plan={TEST_DIR / 'source-note.md'}",
            )
            manifest_path = Path(result.stdout.strip())
            self.assertTrue(manifest_path.is_file())
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["event_id"], event_path.stem)
            self.assertEqual(
                Path(manifest["event_path"]).resolve(),
                event_path.resolve(),
            )
            self.assertEqual(len(manifest["captures"]), 1)
            captured_path = REPO_ROOT / manifest["captures"][0]["captured_path"]
            if not captured_path.exists():
                captured_path = manifest_path.parent / "files" / captured_path.name
            self.assertTrue(captured_path.is_file())
            sidecar_path = captured_path.with_suffix(captured_path.suffix + ".sha256")
            self.assertTrue(sidecar_path.is_file())
            self.assertEqual(
                sidecar_path.read_text().strip(),
                manifest["captures"][0]["sha256"],
            )

            second = self.assert_command_ok(
                "capture-sources",
                "--event",
                event_path,
                "--events-dir",
                events_dir,
                "--sources-dir",
                sources_dir,
                "--source",
                f"plan={TEST_DIR / 'source-note.md'}",
            )
            self.assertEqual(second.stdout.strip(), str(manifest_path))

            captured_path.unlink()
            self.assert_command_fails(
                "capture-sources",
                "--event",
                event_path,
                "--events-dir",
                events_dir,
                "--sources-dir",
                sources_dir,
                "--source",
                f"plan={TEST_DIR / 'source-note.md'}",
            )

    def test_capture_sources_accepts_a_bare_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            event_path = self.create_event(events_dir)
            result = self.assert_command_ok(
                "capture-sources",
                "--event",
                event_path.stem,
                "--events-dir",
                events_dir,
                "--sources-dir",
                Path(tmp) / "sources",
                "--source",
                f"plan={TEST_DIR / 'source-note.md'}",
            )
            manifest = json.loads(Path(result.stdout.strip()).read_text())
            self.assertEqual(manifest["event_id"], event_path.stem)
            self.assertEqual(
                Path(manifest["event_path"]).resolve(), event_path.resolve()
            )

    def test_capture_sources_refuses_an_ambiguous_bare_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            event_path = self.create_event(events_dir)
            twin = events_dir / "2001" / "01" / event_path.name
            twin.parent.mkdir(parents=True)
            twin.write_text(event_path.read_text())
            result = self.assert_command_fails(
                "capture-sources",
                "--event",
                event_path.stem,
                "--events-dir",
                events_dir,
                "--sources-dir",
                Path(tmp) / "sources",
                "--source",
                f"plan={TEST_DIR / 'source-note.md'}",
            )
            self.assertIn("is ambiguous: 2 files", result.stderr)

    def test_validate_accepts_a_bare_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            event_path = self.create_event(events_dir)
            result = self.assert_command_ok(
                "validate", "--events-dir", events_dir, event_path.stem
            )
            self.assertIn(f"valid: {event_path}", result.stdout)

    def test_validate_all_walks_the_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            first = self.create_event(events_dir)
            second = self.create_event(events_dir)
            result = self.assert_command_ok(
                "validate", "--events-dir", events_dir, "--all"
            )
            self.assertIn(f"valid: {first}", result.stdout)
            self.assertIn(f"valid: {second}", result.stdout)
            second.write_text("{}")
            result = self.assert_command_fails(
                "validate", "--events-dir", events_dir, "--all"
            )
            self.assertIn(str(second), result.stderr)

    def test_validate_without_an_event_names_both_forms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.assert_command_fails(
                "validate", "--events-dir", Path(tmp) / "events"
            )
            self.assertIn("--all", result.stderr)
            self.assertIn("bare event id", result.stderr)

    def seed_checkout(self, checkout: Path) -> str:
        """A one-commit git checkout on branch trunk; returns its full sha."""
        checkout.mkdir()
        git = ["git", "-C", str(checkout)]
        subprocess.run([*git, "init", "-q", "-b", "trunk"], check=True)
        subprocess.run(
            [
                *git,
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "seed",
            ],
            check=True,
        )
        return subprocess.run(
            [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()

    def new_handoff_args(self, events_dir: Path, *repo_flags: object) -> list[object]:
        return [
            "new-handoff",
            "--events-dir",
            events_dir,
            "--tool",
            "manual",
            "--summary",
            "Repo identity from git.",
            "--repo-name",
            "acme-notes",
            *repo_flags,
            "--source",
            "plan=plans/example.md",
            "--workstream",
            "wiki-system:primary:update",
        ]

    def test_new_handoff_derives_repo_identity_from_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            sha = self.seed_checkout(Path(tmp) / "checkout")
            derived = self.assert_command_ok(
                *self.new_handoff_args(
                    events_dir, "--repo-from-git", Path(tmp) / "checkout"
                )
            )
            event = json.loads(Path(derived.stdout.strip()).read_text())
            self.assertEqual(event["repo"], {
                "name": "acme-notes", "branch": "trunk", "sha": sha
            })
            overridden = self.assert_command_ok(
                *self.new_handoff_args(
                    events_dir,
                    "--repo-from-git",
                    Path(tmp) / "checkout",
                    "--repo-sha",
                    "deadbee7",
                )
            )
            event = json.loads(Path(overridden.stdout.strip()).read_text())
            self.assertEqual(event["repo"]["branch"], "trunk")
            self.assertEqual(event["repo"]["sha"], "deadbee7")

    def test_new_handoff_without_repo_identity_names_the_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.assert_command_fails(
                *self.new_handoff_args(Path(tmp) / "events", "--repo-branch", "main")
            )
            self.assertIn("--repo-from-git", result.stderr)
            result = self.assert_command_fails(
                *self.new_handoff_args(
                    Path(tmp) / "events", "--repo-from-git", Path(tmp) / "nowhere"
                )
            )
            self.assertIn("git rev-parse", result.stderr)
            detached = Path(tmp) / "detached"
            sha = self.seed_checkout(detached)
            subprocess.run(
                ["git", "-C", str(detached), "checkout", "-q", "--detach", sha],
                check=True,
            )
            result = self.assert_command_fails(
                *self.new_handoff_args(
                    Path(tmp) / "events", "--repo-from-git", Path(tmp) / "detached"
                )
            )
            self.assertIn("detached HEAD", result.stderr)

    def test_new_handoff_keeps_stdout_to_the_path_and_names_next_steps(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            result = self.assert_command_ok(
                *self.new_handoff_args(
                    events_dir, "--repo-branch", "main", "--repo-sha", "4c0f549"
                )
            )
            lines = result.stdout.strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertTrue(Path(lines[0]).is_file())
            self.assertIn("wiki-render.py log", result.stderr)

    def test_capture_sources_names_both_event_forms_on_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            self.create_event(events_dir)
            result = self.assert_command_fails(
                "capture-sources",
                "--event",
                "not-an-event-id",
                "--events-dir",
                events_dir,
                "--sources-dir",
                Path(tmp) / "sources",
                "--source",
                f"plan={TEST_DIR / 'source-note.md'}",
            )
            self.assertIn("event JSON path or an event id", result.stderr)

    def test_capture_sources_fails_on_corrupt_existing_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            event_path = self.create_event(events_dir)
            sources_dir = Path(tmp) / "sources"
            result = self.assert_command_ok(
                "capture-sources",
                "--event",
                event_path,
                "--events-dir",
                events_dir,
                "--sources-dir",
                sources_dir,
                "--source",
                f"plan={TEST_DIR / 'source-note.md'}",
            )
            manifest_path = Path(result.stdout.strip())
            manifest = json.loads(manifest_path.read_text())
            captured_path = Path(manifest["captures"][0]["captured_path"])
            captured_path.write_text("corrupted\n")
            self.assert_command_fails(
                "capture-sources",
                "--event",
                event_path,
                "--events-dir",
                events_dir,
                "--sources-dir",
                sources_dir,
                "--source",
                f"plan={TEST_DIR / 'source-note.md'}",
            )

    def test_capture_sources_fails_on_corrupt_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            event_path = self.create_event(events_dir)
            sources_dir = Path(tmp) / "sources"
            result = self.assert_command_ok(
                "capture-sources",
                "--event",
                event_path,
                "--events-dir",
                events_dir,
                "--sources-dir",
                sources_dir,
                "--source",
                f"plan={TEST_DIR / 'source-note.md'}",
            )
            manifest_path = Path(result.stdout.strip())
            manifest = json.loads(manifest_path.read_text())
            captured_path = Path(manifest["captures"][0]["captured_path"])
            sidecar_path = captured_path.with_suffix(captured_path.suffix + ".sha256")
            sidecar_path.write_text("bad-digest\n")
            self.assert_command_fails(
                "capture-sources",
                "--event",
                event_path,
                "--events-dir",
                events_dir,
                "--sources-dir",
                sources_dir,
                "--source",
                f"plan={TEST_DIR / 'source-note.md'}",
            )

    def test_build_pending_writes_index_and_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_path = self.create_event(root / "events")
            self.assert_command_ok(
                "capture-sources",
                "--event",
                event_path,
                "--events-dir",
                root / "events",
                "--sources-dir",
                root / "sources",
                "--source",
                f"plan={TEST_DIR / 'source-note.md'}",
            )
            self.assert_command_ok(
                "build-pending",
                "--events-dir",
                root / "events",
                "--sources-dir",
                root / "sources",
                "--pending-dir",
                root / "pending",
            )
            index_path = root / "pending" / "index.json"
            latest_path = root / "pending" / "latest.md"
            self.assertTrue(index_path.is_file())
            self.assertTrue(latest_path.is_file())
            index = json.loads(index_path.read_text())
            self.assertEqual(index["event_count"], 1)
            self.assertEqual(index["events"][0]["event_id"], event_path.stem)
            self.assertEqual(index["events"][0]["review_status"], "unreviewed")
            self.assertTrue(index["events"][0]["capture_manifests"])
            latest = latest_path.read_text()
            self.assertIn("Pending events: 1", latest)
            self.assertIn("Treat this file as provisional", latest)
            # Atomic tmp+rename leaves no residue behind.
            self.assertEqual(list((root / "pending").glob("*.tmp")), [])

    def test_build_pending_fails_fast_on_held_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.create_event(root / "events")
            (root / "events" / ".wiki-event.lock").write_text(f"pid={os.getpid()}\n")
            self.assert_command_fails(
                "build-pending",
                "--events-dir",
                root / "events",
                "--pending-dir",
                root / "pending-2",
            )
            self.assertFalse((root / "pending-2").exists())

    def test_capture_sources_fails_fast_on_held_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_path = self.create_event(root / "events")
            (root / "events" / ".wiki-event.lock").write_text(f"pid={os.getpid()}\n")
            self.assert_command_fails(
                "capture-sources",
                "--event",
                event_path,
                "--events-dir",
                root / "events",
                "--sources-dir",
                root / "sources",
                "--source",
                f"plan={TEST_DIR / 'source-note.md'}",
            )
            self.assertFalse((root / "sources").exists())

    def test_write_pending_files_validates_before_touching_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.create_event(root / "events")
            index_path = root / "pending" / "index.json"
            latest_path = root / "pending" / "latest.md"
            index_before = index_path.read_text()
            latest_before = latest_path.read_text()
            with self.assertRaises(wiki_event.ValidationError):
                wiki_event.write_pending_files(
                    root / "pending", {"not": "a pending index"}
                )
            self.assertEqual(index_path.read_text(), index_before)
            self.assertEqual(latest_path.read_text(), latest_before)
            self.assertEqual(list((root / "pending").glob("*.tmp")), [])

    def test_write_pending_files_crash_mid_pair_keeps_old_latest(self) -> None:
        # Discriminates tmp+rename from direct write_text: a crash while
        # writing the second file of the pair must leave the previous
        # latest.md complete (new index.json + old latest.md is the
        # documented acceptable torn-pair state).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.create_event(root / "events")
            index_path = root / "pending" / "index.json"
            latest_path = root / "pending" / "latest.md"
            index = json.loads(index_path.read_text())
            latest_before = latest_path.read_text()

            original_write_text = Path.write_text

            def crash_on_latest_tmp(path: Path, text: str) -> int:
                if path.name == "latest.md.tmp":
                    raise OSError("injected crash")
                return original_write_text(path, text)

            with (
                mock.patch.object(Path, "write_text", crash_on_latest_tmp),
                self.assertRaises(OSError),
            ):
                wiki_event.write_pending_files(root / "pending", index)
            self.assertEqual(latest_path.read_text(), latest_before)
            self.assertEqual(list((root / "pending").glob("*.tmp")), [])

    def test_build_pending_fails_loudly_on_unknown_event_type(self) -> None:
        # The shared loader refuses unknown event types everywhere, the
        # pending path included — no silent passthrough.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.create_event(root / "events")
            bad_dir = root / "events" / "2026" / "01"
            bad_dir.mkdir(parents=True, exist_ok=True)
            (bad_dir / "mystery.json").write_text(
                json.dumps({"schema_version": 1, "event_type": "mystery"})
            )
            result = self.assert_command_fails(
                "build-pending", "--events-dir", root / "events"
            )
            self.assertIn("unknown event_type", result.stderr)

    def test_build_pending_skips_quarantined_event(self) -> None:
        # A quarantined corrupt file is excluded from the pending index by
        # path; its corrected_by must exist in the store.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_path = self.create_event(root / "events")
            bad_dir = root / "events" / "2026" / "01"
            bad_dir.mkdir(parents=True, exist_ok=True)
            bad_id = "00000000-0000-0000-0000-000000000000"
            (bad_dir / f"{bad_id}.json").write_text("{not json")
            (root / "quarantine.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "quarantined": [
                            {
                                "event_id": bad_id,
                                "path": f"wiki/events/2026/01/{bad_id}.json",
                                "reason": "test corruption",
                                "corrected_by": event_path.stem,
                            }
                        ],
                    }
                )
            )
            self.assert_command_ok(
                "build-pending",
                "--events-dir",
                root / "events",
                "--pending-dir",
                root / "pending",
            )
            index = json.loads((root / "pending" / "index.json").read_text())
            self.assertEqual(index["event_count"], 1)
            self.assertEqual(index["events"][0]["event_id"], event_path.stem)

    def test_generated_event_with_bad_relationship_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assert_command_fails(
                "new-handoff",
                "--events-dir",
                Path(tmp) / "events",
                "--tool",
                "manual",
                "--summary",
                "Invalid generated event.",
                "--repo-name",
                "acme-notes",
                "--repo-branch",
                "main",
                "--repo-sha",
                "4c0f549",
                "--workstream",
                "wiki-system:bad_relationship:update",
            )

    def test_valid_with_workstream_state(self) -> None:
        self.assert_command_ok(
            "validate", TEST_DIR / "valid-with-workstream-state.json"
        )

    def test_valid_partial_workstream_state(self) -> None:
        self.assert_command_ok(
            "validate", TEST_DIR / "valid-partial-workstream-state.json"
        )

    def test_invalid_empty_string_in_ws_array(self) -> None:
        self.assert_command_fails(
            "validate", TEST_DIR / "invalid-empty-string-in-ws-array.json"
        )

    def test_invalid_ws_wrong_type(self) -> None:
        self.assert_command_fails("validate", TEST_DIR / "invalid-ws-wrong-type.json")

    def test_new_handoff_with_workstream_state_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.assert_command_ok(
                "new-handoff",
                "--events-dir",
                Path(tmp) / "events",
                "--tool",
                "manual",
                "--summary",
                "Event with ws args",
                "--repo-name",
                "acme-notes",
                "--repo-branch",
                "main",
                "--repo-sha",
                "4c0f549",
                "--workstream",
                "wiki-system:primary:update",
                "--current-state",
                "In progress",
                "--what-was-done",
                "Did something",
                "--next",
                "Do more",
                "--blocker",
                "PR #160 awaiting review",
                "--continuation-context",
                "Context here",
            )
            event_path = Path(result.stdout.strip())
            event = json.loads(event_path.read_text())
            self.assertIn("workstream_state", event)
            self.assertEqual(
                event["workstream_state"]["current_state"], ["In progress"]
            )

    def test_new_handoff_without_workstream_state_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.assert_command_ok(
                "new-handoff",
                "--events-dir",
                Path(tmp) / "events",
                "--tool",
                "manual",
                "--summary",
                "Event without ws args",
                "--repo-name",
                "acme-notes",
                "--repo-branch",
                "main",
                "--repo-sha",
                "4c0f549",
                "--workstream",
                "wiki-system:primary:update",
            )
            event_path = Path(result.stdout.strip())
            event = json.loads(event_path.read_text())
            self.assertNotIn("workstream_state", event)

    def test_new_handoff_with_timestamp_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.assert_command_ok(
                "new-handoff",
                "--events-dir",
                Path(tmp) / "events",
                "--tool",
                "manual",
                "--summary",
                "Backfill event",
                "--repo-name",
                "acme-notes",
                "--repo-branch",
                "main",
                "--repo-sha",
                "4c0f549",
                "--workstream",
                "wiki-system:primary:update",
                "--timestamp",
                "2026-05-29T04:07:18Z",
            )
            event_path = Path(result.stdout.strip())
            event = json.loads(event_path.read_text())
            self.assertEqual(event["timestamp_utc"], "2026-05-29T04:07:18Z")

    def test_new_handoff_with_invalid_timestamp_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assert_command_fails(
                "new-handoff",
                "--events-dir",
                Path(tmp) / "events",
                "--tool",
                "manual",
                "--summary",
                "Bad timestamp",
                "--repo-name",
                "acme-notes",
                "--repo-branch",
                "main",
                "--repo-sha",
                "4c0f549",
                "--workstream",
                "wiki-system:primary:update",
                "--timestamp",
                "not-a-timestamp",
            )

    def test_stored_event_files_validate_via_cli(self) -> None:
        # Rewritten from the source repo's live-store regression sweep
        # (which validated its real wiki/events/ history): the kit ships no
        # store, so the property becomes a generated round-trip — every
        # file the CLI writes must validate back through the CLI.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handoff_path = self.create_event(root / "events")
            self.assert_command_ok(
                "new-garden-apply",
                "--target",
                handoff_path.stem,
                "--status",
                "applied-manually",
                "--note",
                "Round trip.",
                "--events-dir",
                root / "events",
                "--pending-dir",
                root / "pending",
            )
            event_files = sorted((root / "events").glob("**/*.json"))
            self.assertGreaterEqual(len(event_files), 2)
            for path in event_files:
                with self.subTest(path=str(path)):
                    self.assert_command_ok("validate", path)

    def test_count_pending_fails_when_index_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "events").mkdir()
            self.assert_command_fails(
                "count-pending",
                "--pending-dir",
                root / "does-not-exist",
                "--events-dir",
                root / "events",
            )

    def test_count_pending_returns_correct_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.create_event(root / "events")
            result = self.assert_command_ok(
                "count-pending",
                "--pending-dir",
                root / "pending",
                "--events-dir",
                root / "events",
            )
            self.assertEqual(result.stdout.strip(), "1")

    def test_count_pending_fails_on_malformed_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pending_dir = Path(tmp) / "pending"
            pending_dir.mkdir()
            (pending_dir / "index.json").write_text("not json")
            self.assert_command_fails(
                "count-pending",
                "--pending-dir",
                pending_dir,
                "--events-dir",
                Path(tmp) / "events",
            )

    def test_count_pending_fails_when_index_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.create_event(root / "events")
            index_path = root / "pending" / "index.json"
            index = json.loads(index_path.read_text())
            index["events"] = []
            index["event_count"] = 0
            index_path.write_text(json.dumps(index))

            result = self.assert_command_fails(
                "count-pending",
                "--events-dir",
                root / "events",
                "--pending-dir",
                root / "pending",
            )
            self.assertIn("differs from the event store", result.stderr)

    def test_new_garden_apply_dispositions_target_and_rebuilds_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handoff_path = self.create_event(root / "events")
            result = self.assert_command_ok(
                "new-garden-apply",
                "--target",
                handoff_path.stem,
                "--status",
                "applied-manually",
                "--note",
                "Curated with user approval.",
                "--events-dir",
                root / "events",
                "--pending-dir",
                root / "pending",
            )
            disposition_path = Path(result.stdout.strip())
            disposition = json.loads(disposition_path.read_text())
            self.assertEqual(disposition["target_event_id"], handoff_path.stem)
            self.assertEqual(disposition["status"], "applied-manually")
            self.assertEqual(disposition["note"], "Curated with user approval.")
            pending = json.loads((root / "pending" / "index.json").read_text())
            self.assertEqual(pending["event_count"], 0)

    def test_new_garden_apply_rejects_missing_or_non_handoff_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handoff_path = self.create_event(root / "events")
            missing = self.make_uuid7(self.now_ms() - 10_000)
            self.assert_command_fails(
                "new-garden-apply",
                "--target",
                missing,
                "--status",
                "rejected",
                "--events-dir",
                root / "events",
            )

            disposition = self.assert_command_ok(
                "new-garden-apply",
                "--target",
                handoff_path.stem,
                "--status",
                "rejected",
                "--events-dir",
                root / "events",
            )
            self.assert_command_fails(
                "new-garden-apply",
                "--target",
                Path(disposition.stdout.strip()).stem,
                "--status",
                "superseded",
                "--events-dir",
                root / "events",
            )

    def test_new_garden_apply_rejects_empty_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handoff_path = self.create_event(root / "events")
            result = self.assert_command_fails(
                "new-garden-apply",
                "--target",
                handoff_path.stem,
                "--status",
                "rejected",
                "--note",
                "   ",
                "--events-dir",
                root / "events",
            )
            self.assertIn("must not be empty", result.stderr)

    def test_new_garden_apply_reports_durable_projection_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handoff_path = self.create_event(root / "events")
            broken_pending = root / "broken-pending"
            broken_pending.write_text("not a directory")

            result = self.assert_command_fails(
                "new-garden-apply",
                "--target",
                handoff_path.stem,
                "--status",
                "rejected",
                "--events-dir",
                root / "events",
                "--pending-dir",
                broken_pending,
            )
            self.assertIn("disposition stands", result.stderr)
            dispositions = [
                json.loads(path.read_text())
                for path in (root / "events").glob("**/*.json")
                if path != handoff_path
            ]
            self.assertEqual(len(dispositions), 1)
            self.assertEqual(dispositions[0]["target_event_id"], handoff_path.stem)

    def test_lock_files_fail_closed(self) -> None:
        lock_cases = {
            "live pid": f"pid={os.getpid()}\n",
            "malformed": "not a pid file yet\n",
            "empty": "",
        }
        for label, lock_text in lock_cases.items():
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory() as tmp,
            ):
                events_dir = Path(tmp) / "events"
                events_dir.mkdir()
                (events_dir / ".wiki-event.lock").write_text(lock_text)
                self.assert_command_fails(
                    "new-handoff",
                    "--events-dir",
                    events_dir,
                    "--tool",
                    "manual",
                    "--summary",
                    "Lock should not be stolen.",
                    "--repo-name",
                    "acme-notes",
                    "--repo-branch",
                    "main",
                    "--repo-sha",
                    "4c0f549",
                    "--workstream",
                    "wiki-system:primary:update",
                )

    def test_valid_garden_apply_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = self.write_garden_apply_event(Path(tmp))
            self.assert_command_ok("validate", event_path)

    def test_manual_garden_apply_can_record_selected_workstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = self.write_garden_apply_event(
                Path(tmp), status="applied-manually", workstream="ci-reduction"
            )
            self.assert_command_ok("validate", event_path)

    def test_garden_apply_workstream_must_be_a_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = self.write_garden_apply_event(
                Path(tmp), status="applied-manually", workstream="Not A Slug"
            )
            result = self.assert_command_fails("validate", event_path)
            self.assertNotIn("Traceback", result.stderr)

    def test_garden_apply_workstream_requires_manual_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = self.write_garden_apply_event(
                Path(tmp), status="applied", workstream="ci-reduction"
            )
            result = self.assert_command_fails("validate", event_path)
            self.assertIn("only allowed for applied-manually", result.stderr)

    def test_status_displays_manual_route_workstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.write_garden_apply_event(
                root, status="applied-manually", workstream="ci-reduction"
            )
            event = json.loads(source.read_text())
            event_path = root / "events" / "2026" / "06" / f"{event['event_id']}.json"
            event_path.parent.mkdir(parents=True)
            event_path.write_text(json.dumps(event) + "\n")

            result = self.assert_command_ok(
                "status", event["event_id"], "--events-dir", root / "events"
            )

            self.assertIn("workstream: ci-reduction", result.stdout)

    def test_garden_apply_jsonschema_path_resolves_refs_locally(self) -> None:
        # jsonschema is a hard dependency: validation always runs the
        # jsonschema path against the SchemaCache-resolved schema; this
        # passing proves no remote $id resolution is attempted.
        with tempfile.TemporaryDirectory() as tmp:
            event_path = self.write_garden_apply_event(Path(tmp))
            self.assert_command_ok("validate", event_path)

    def test_garden_apply_invalid_uuid_fails_without_traceback(self) -> None:
        # Regression: this used to crash with an uncaught
        # _WrappedReferencingError when jsonschema tried to resolve the
        # schema's relative $ref against its remote $id.
        with tempfile.TemporaryDirectory() as tmp:
            event_path = self.write_garden_apply_event(
                Path(tmp), event_id="019ebb7f-aee4-7ee4-c421-cbe5e5451232"
            )
            result = self.assert_command_fails("validate", event_path)
            self.assertNotIn("Traceback", result.stderr)

    def test_garden_apply_bad_status_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = self.write_garden_apply_event(Path(tmp), status="done")
            result = self.assert_command_fails("validate", event_path)
            self.assertNotIn("Traceback", result.stderr)

    def test_garden_apply_unknown_field_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = self.write_garden_apply_event(
                Path(tmp), reviewer="not-a-schema-field"
            )
            result = self.assert_command_fails("validate", event_path)
            self.assertNotIn("Traceback", result.stderr)

    # --- v1 legacy-shim boundary tests -----------------------------------
    # These exercise the documented permanent v1 shim and keep their aura
    # literals by design (design contract decision 3): the envelope KEY on
    # disk is the string "aura" and the tests must prove version dispatch
    # around exactly that key.

    def test_v2_event_with_v1_envelope_rejected(self) -> None:
        # Declaring v2 while carrying the v1 `aura` envelope must fail —
        # version dispatch validates against the declared version's schema.
        handoff = json.loads((TEST_DIR / "valid-handoff-event.json").read_text())
        handoff["schema_version"] = 2
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "v2-with-v1-envelope.json"
            event_path.write_text(json.dumps(handoff))
            self.assert_command_fails("validate", event_path)

    def test_v1_event_with_repo_envelope_rejected(self) -> None:
        handoff = json.loads((TEST_DIR / "valid-handoff-event.json").read_text())
        handoff["repo"] = {
            "name": "acme-notes",
            **handoff.pop("aura"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "v1-with-repo.json"
            event_path.write_text(json.dumps(handoff))
            self.assert_command_fails("validate", event_path)

    def test_unknown_schema_version_rejected(self) -> None:
        handoff = json.loads((TEST_DIR / "valid-handoff-event.json").read_text())
        handoff["schema_version"] = 3
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "v3.json"
            event_path.write_text(json.dumps(handoff))
            result = self.assert_command_fails("validate", event_path)
            self.assertNotIn("Traceback", result.stderr)

    def test_generated_v2_event_has_repo_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = self.create_event(Path(tmp) / "events")
            event = json.loads(event_path.read_text())
            self.assertEqual(event["schema_version"], 2)
            self.assertEqual(
                event["repo"],
                {"name": "acme-notes", "branch": "main", "sha": "4c0f549"},
            )
            self.assertNotIn(wiki_event.V1_ENVELOPE_KEY, event)

    # --- end of the v1 legacy-shim boundary tests -------------------------

    def test_future_dated_event_id_fails(self) -> None:
        # The hole the 2026-06-10 audit's fabrications walked through: ids
        # are generated, so an id decoding past wall clock + skew is
        # hand-typed. Both event types share the check.
        future_id = self.make_uuid7(self.now_ms() + 2 * 60 * 60 * 1000)
        with tempfile.TemporaryDirectory() as tmp:
            garden_path = self.write_garden_apply_event(Path(tmp), event_id=future_id)
            result = self.assert_command_fails("validate", garden_path)
            self.assertIn("future", result.stderr)

            handoff = json.loads((TEST_DIR / "valid-handoff-event.json").read_text())
            handoff["event_id"] = future_id
            handoff_path = Path(tmp) / "future-handoff.json"
            handoff_path.write_text(json.dumps(handoff))
            result = self.assert_command_fails("validate", handoff_path)
            self.assertIn("future", result.stderr)

    def test_event_id_within_clock_skew_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = self.write_garden_apply_event(
                Path(tmp), event_id=self.make_uuid7(self.now_ms() + 30 * 60 * 1000)
            )
            self.assert_command_ok("validate", event_path)

    def test_backfill_id_postdating_declared_timestamp_passes(self) -> None:
        # Backfills and corrections carry ids minted long after their
        # declared timestamp_utc; the skew check compares against the wall
        # clock only.
        with tempfile.TemporaryDirectory() as tmp:
            event_path = self.write_garden_apply_event(
                Path(tmp),
                event_id=self.make_uuid7(self.now_ms()),
                timestamp_utc="2026-06-06T00:57:50Z",
            )
            self.assert_command_ok("validate", event_path)

    def test_placeholder_blocker_rejected_at_write_time(self) -> None:
        # Write-time strictness only: legacy store events with a literal
        # 'None' blocker stay loadable (the load path stays tolerant), but
        # new-handoff refuses to write placeholders.
        for placeholder in ("None", "none", "N/A", "n/a"):
            with (
                self.subTest(placeholder=placeholder),
                tempfile.TemporaryDirectory() as tmp,
            ):
                result = self.assert_command_fails(
                    "new-handoff",
                    "--events-dir",
                    Path(tmp) / "events",
                    "--tool",
                    "manual",
                    "--summary",
                    "Placeholder blocker must fail",
                    "--repo-name",
                    "acme-notes",
                    "--repo-branch",
                    "main",
                    "--repo-sha",
                    "4c0f549",
                    "--workstream",
                    "wiki-system:primary:update",
                    "--blocker",
                    placeholder,
                )
                self.assertIn("placeholder", result.stderr)

    def test_status_pending_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            event_path = self.create_event(events_dir)
            result = self.assert_command_ok(
                "status", event_path.stem, "--events-dir", events_dir
            )
            self.assertIn("pending garden", result.stdout)
            self.assertIn("declared: pending_garden", result.stdout)

    def test_status_join_derived_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            event_path = self.create_event(events_dir)
            early_id = self.make_uuid7(self.now_ms() - 2000)
            late_id = self.make_uuid7(self.now_ms() - 1000)
            for apply_id, timestamp in (
                (early_id, "2026-06-07T00:00:00Z"),
                (late_id, "2026-06-08T00:00:00Z"),
            ):
                apply_event = {
                    "schema_version": 1,
                    "event_id": apply_id,
                    "event_type": "garden-apply",
                    "timestamp_utc": timestamp,
                    "target_event_id": event_path.stem,
                    "status": "applied",
                }
                apply_path = events_dir / "2026" / "06" / f"{apply_id}.json"
                apply_path.parent.mkdir(parents=True, exist_ok=True)
                apply_path.write_text(json.dumps(apply_event))
            result = self.assert_command_ok(
                "status", event_path.stem, "--events-dir", events_dir
            )
            self.assertIn("dispositions (latest wins):", result.stdout)
            latest_line = [
                line
                for line in result.stdout.splitlines()
                if "join-derived disposition" in line
            ]
            self.assertEqual(len(latest_line), 1)
            self.assertIn(late_id, latest_line[0])

            # The garden-apply event itself reports its target.
            result = self.assert_command_ok(
                "status", late_id, "--events-dir", events_dir
            )
            self.assertIn(f"target:   {event_path.stem}", result.stdout)

    def test_status_quarantined_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_path = self.create_event(root / "events")
            bad_dir = root / "events" / "2026" / "01"
            bad_dir.mkdir(parents=True, exist_ok=True)
            bad_id = "00000000-0000-0000-0000-000000000000"
            (bad_dir / f"{bad_id}.json").write_text("{not json")
            (root / "quarantine.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "quarantined": [
                            {
                                "event_id": bad_id,
                                "path": f"wiki/events/2026/01/{bad_id}.json",
                                "reason": "test corruption",
                                "corrected_by": event_path.stem,
                            }
                        ],
                    }
                )
            )
            result = self.assert_command_ok(
                "status", bad_id, "--events-dir", root / "events"
            )
            self.assertIn("QUARANTINED", result.stdout)
            self.assertIn(event_path.stem, result.stdout)

    def test_status_unknown_event_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            self.create_event(events_dir)
            result = self.assert_command_fails(
                "status",
                self.make_uuid7(self.now_ms() - 5000),
                "--events-dir",
                events_dir,
            )
            self.assertIn("not found", result.stderr)

    def test_status_flags_deprecated_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            handoff = json.loads((TEST_DIR / "valid-handoff-event.json").read_text())
            handoff["status"] = "applied"
            event_dir = events_dir / "2026" / "06"
            event_dir.mkdir(parents=True)
            (event_dir / f"{handoff['event_id']}.json").write_text(json.dumps(handoff))
            result = self.assert_command_ok(
                "status", handoff["event_id"], "--events-dir", events_dir
            )
            self.assertIn("DEPRECATED", result.stdout)


class PendingMismatchTest(CliHarness):
    """The shared pending_mismatch helper (design contract decision 13):
    doctor and the pre-commit hook consume this one comparison."""

    def mismatches(self, root: Path) -> list[str]:
        return wiki_event.pending_mismatch(
            root / "events",
            root / "sources",
            root / "pending" / "index.json",
            root / "pending" / "latest.md",
        )

    def test_fresh_projection_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.create_event(root / "events")
            self.assertEqual(self.mismatches(root), [])

    def test_tampered_index_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.create_event(root / "events")
            index_path = root / "pending" / "index.json"
            index = json.loads(index_path.read_text())
            index["events"] = []
            index["event_count"] = 0
            index_path.write_text(json.dumps(index))
            found = self.mismatches(root)
            self.assertEqual(len(found), 1)
            self.assertIn("index.json", found[0])
            self.assertIn("differs", found[0])

    def test_tampered_latest_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.create_event(root / "events")
            latest_path = root / "pending" / "latest.md"
            latest_path.write_text(latest_path.read_text() + "tampered\n")
            found = self.mismatches(root)
            self.assertEqual(len(found), 1)
            self.assertIn("latest.md", found[0])
            self.assertIn("differs", found[0])

    def test_missing_index_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.create_event(root / "events")
            (root / "pending" / "index.json").unlink()
            found = self.mismatches(root)
            self.assertTrue(
                any("index.json" in item and "missing" in item for item in found),
                found,
            )

    def test_missing_latest_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.create_event(root / "events")
            (root / "pending" / "latest.md").unlink()
            found = self.mismatches(root)
            self.assertEqual(len(found), 1)
            self.assertIn("latest.md", found[0])
            self.assertIn("missing", found[0])

    def test_unparseable_index_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.create_event(root / "events")
            (root / "pending" / "index.json").write_text("{not json")
            found = self.mismatches(root)
            self.assertTrue(
                any(
                    "index.json" in item and "not valid JSON" in item
                    for item in found
                ),
                found,
            )

    def test_rebuild_restores_the_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.create_event(root / "events")
            (root / "pending" / "latest.md").write_text("tampered\n")
            self.assertNotEqual(self.mismatches(root), [])
            self.assert_command_ok(
                "build-pending",
                "--events-dir",
                root / "events",
                "--sources-dir",
                root / "sources",
                "--pending-dir",
                root / "pending",
            )
            self.assertEqual(self.mismatches(root), [])


class ConventionalStoreProjectionTest(CliHarness):
    """A store at <root>/wiki/events is written with repo-relative event
    paths; every library-side rebuild must stamp the same way, or the
    night runner and the doctor disagree about one projection."""

    def create_conventional_store(self, root: Path) -> Path:
        events_dir = root / "wiki" / "events"
        self.create_event(events_dir)
        return events_dir

    def test_verified_loader_accepts_cli_written_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = self.create_conventional_store(Path(tmp).resolve())
            index = wiki_event.load_verified_pending_index(events_dir)
            self.assertEqual(index["event_count"], 1)
            self.assertFalse(Path(index["events"][0]["event_path"]).is_absolute())

    def test_verified_loader_and_mismatch_check_agree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            events_dir = self.create_conventional_store(root)
            self.assertEqual(
                wiki_event.pending_mismatch(
                    events_dir,
                    root / "wiki" / "sources",
                    root / "wiki" / "pending" / "index.json",
                    root / "wiki" / "pending" / "latest.md",
                ),
                [],
            )
            wiki_event.load_verified_pending_index(events_dir)

    def test_rebuild_restores_the_callers_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            events_dir = self.create_conventional_store(root)
            self.assertIsNone(wiki_event._wiki_root)
            wiki_event.rebuild_pending_index(events_dir, root / "wiki" / "sources")
            self.assertIsNone(wiki_event._wiki_root)


class WikiRootResolutionTest(CliHarness):
    """--wiki resolution: content-path defaults derive from the resolved
    root; failure outside a wiki is a clear error naming the flag."""

    def test_explicit_wiki_without_config_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.assert_command_fails("build-pending", "--wiki", tmp)
            self.assertIn("wiki.toml", result.stderr)

    def test_walk_up_outside_git_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.assert_command_fails("build-pending", cwd=Path(tmp))
            self.assertIn("--wiki", result.stderr)

    def test_wiki_flag_derives_content_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "wiki-repo"
            root.mkdir()
            (root / "wiki.toml").write_text(
                '[contract]\nprotected = ["wiki/events/**"]\n'
            )
            event_path = self.create_event(root / "wiki" / "events")
            self.assert_command_ok("build-pending", "--wiki", root)
            index = json.loads(
                (root / "wiki" / "pending" / "index.json").read_text()
            )
            self.assertEqual(index["event_count"], 1)
            self.assertEqual(index["events"][0]["event_id"], event_path.stem)
            # With a resolved root, stored paths are repo-relative to it.
            self.assertEqual(
                index["events"][0]["event_path"],
                f"wiki/events/{event_path.parent.parent.name}/"
                f"{event_path.parent.name}/{event_path.name}",
            )


class EventsDirGuardTest(unittest.TestCase):
    """default_sources_dir/default_pending_dir must reject a relative
    --events-dir, since a relative path's sibling computation depends on
    the process's CWD and has produced stray top-level
    events/pending/sources directories in the past."""

    def test_absolute_events_dir_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "wiki" / "events"
            self.assertEqual(
                wiki_event.default_sources_dir(events_dir),
                events_dir.parent / "sources",
            )
            self.assertEqual(
                wiki_event.default_pending_dir(events_dir),
                events_dir.parent / "pending",
            )

    def test_relative_events_dir_is_rejected(self) -> None:
        relative = Path("wiki") / "events"
        with self.assertRaisesRegex(wiki_event.ValidationError, "absolute"):
            wiki_event.default_sources_dir(relative)
        with self.assertRaisesRegex(wiki_event.ValidationError, "absolute"):
            wiki_event.default_pending_dir(relative)


if __name__ == "__main__":
    unittest.main(verbosity=2)
