#!/usr/bin/env python3
"""Tests for the garden checkpoint helper."""

from __future__ import annotations

import importlib.util
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "scripts"
ARCHIVE_SOURCE = "workstreams/source\n:(glob)[1].md"
ARCHIVE_DESTINATION = "workstreams/-archive\n:(glob)[2].md"
ARCHIVE_CONTENT = "source\n"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import wiki_event  # noqa: E402


def load_checkpoint_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "scripts.wiki_checkpoint", SCRIPT_DIR / "wiki_checkpoint.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load scripts/wiki_checkpoint.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["scripts.wiki_checkpoint"] = module
    spec.loader.exec_module(module)
    return module


wiki_checkpoint = load_checkpoint_module()


def run_git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    ).stdout


class WikiCheckpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "wiki"
        self.state_dir = base / "state"
        self.root.mkdir()
        run_git(self.root, "init", "-q")
        run_git(self.root, "config", "user.email", "test@example.com")
        run_git(self.root, "config", "user.name", "Checkpoint Test")
        run_git(self.root, "config", "commit.gpgsign", "false")
        run_git(self.root, "config", "core.hooksPath", "/dev/null")
        for directory in (
            "wiki/events",
            "wiki/pending",
            "workstreams",
            "wiki/feedback",
            "docs",
            "planning",
        ):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        files = {
            "CLAUDE.local.md": "local\n",
            "wiki/log.md": "log\n",
            "wiki/pending/index.json": "{}\n",
            "wiki/pending/latest.md": "pending\n",
            "workstreams/existing.md": "existing\n",
            "docs/existing.md": "docs\n",
            "planning/existing.md": "plan\n",
        }
        for rel, text in files.items():
            (self.root / rel).write_text(text, encoding="utf-8")
        run_git(self.root, "add", ".")
        run_git(self.root, "commit", "-q", "-m", "initial")

    def command(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = wiki_checkpoint.main(list(args))
        return result, stdout.getvalue(), stderr.getvalue()

    def preflight(self) -> tuple[int, str, str]:
        return self.command(
            "preflight",
            "--repo-root",
            str(self.root),
            "--state-dir",
            str(self.state_dir),
        )

    def approve(self, *paths: str, initial: bool = False) -> tuple[int, str, str]:
        args = ["approve", "--state-dir", str(self.state_dir)]
        if initial:
            args.append("--initial")
        args.extend(paths)
        return self.command(*args)

    def adopt(self, *paths: str) -> tuple[int, str, str]:
        return self.command(
            "adopt-handoffs", "--state-dir", str(self.state_dir), *paths
        )

    def prepare(self) -> tuple[int, str, str]:
        return self.command("prepare", "--state-dir", str(self.state_dir))

    def write_handoff(self) -> str:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        event = {
            "schema_version": 2,
            "event_id": wiki_event.uuid7(),
            "event_type": "handoff",
            "timestamp_utc": now,
            "tool": "manual",
            "status": "pending_garden",
            "summary": "Checkpoint fixture",
            "repo": {"name": "fixture", "branch": "main", "sha": "abcdef0"},
            "sources": [],
            "proposed_workstreams": [
                {
                    "name": "fixture",
                    "relationship": "primary",
                    "proposed_action": "update",
                }
            ],
        }
        path = wiki_event.event_path(self.root / "wiki" / "events", event)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        return str(path.relative_to(self.root))

    def write_garden_apply(self, target_path: str) -> str:
        target = json.loads((self.root / target_path).read_text(encoding="utf-8"))
        event = {
            "schema_version": 1,
            "event_id": wiki_event.uuid7(),
            "event_type": "garden-apply",
            "timestamp_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "target_event_id": target["event_id"],
            "status": "applied",
        }
        path = wiki_event.event_path(self.root / "wiki" / "events", event)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        return str(path.relative_to(self.root))

    def commit_workstream(self, path: str, content: str = ARCHIVE_CONTENT) -> None:
        (self.root / path).write_text(content, encoding="utf-8")
        run_git(self.root, "--literal-pathspecs", "add", "--", path)
        run_git(self.root, "commit", "-q", "-m", "add archive source")

    def test_preflight_rejects_staged_file(self) -> None:
        (self.root / "CLAUDE.local.md").write_text("changed\n", encoding="utf-8")
        run_git(self.root, "add", "CLAUDE.local.md")

        result, _, error = self.preflight()

        self.assertEqual(result, 1)
        self.assertIn("staged changes are not allowed", error)

    def test_preflight_rejects_sync_doc_dirt(self) -> None:
        (self.root / "docs" / "synced.md").write_text("sync\n", encoding="utf-8")

        result, _, error = self.preflight()

        self.assertEqual(result, 1)
        self.assertIn("sync-doc dirt", error)
        self.assertIn("review and commit or restore", error)

    def test_initial_handoff_ingress_can_be_approved_and_prepared(self) -> None:
        event_path = self.write_handoff()
        (self.root / "wiki/log.md").write_text("rendered\n", encoding="utf-8")

        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        result, _, error = self.approve(initial=True)
        self.assertEqual((result, error), (0, ""))
        result, _, error = self.prepare()

        self.assertEqual((result, error), (0, ""))
        self.assertEqual(self.staged_paths(), {event_path, "wiki/log.md"})

    def test_prepare_stages_spaces_and_pathspec_magic_literally(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        paths = ["workstreams/a file.md", "workstreams/:(glob)trap[1].md"]
        for path in paths:
            (self.root / path).write_text("approved\n", encoding="utf-8")
        result, _, error = self.approve(*paths)
        self.assertEqual((result, error), (0, ""))

        result, _, error = self.prepare()

        self.assertEqual((result, error), (0, ""))
        self.assertEqual(self.staged_paths(), set(paths))
        manifest = (self.state_dir / "path-manifest.z").read_bytes()
        self.assertIn(b"workstreams/:(glob)trap[1].md\0", manifest)

    def test_status_round_trips_newline_and_leading_hyphen_paths(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        paths = ["workstreams/line\nbreak.md", "workstreams/-leading.md"]
        for path in paths:
            (self.root / path).write_text("approved\n", encoding="utf-8")

        result, output, error = self.command(
            "status", "--state-dir", str(self.state_dir)
        )

        self.assertEqual((result, error), (0, ""))
        changes = {item["path"]: item for item in json.loads(output)["changes"]}
        for path in paths:
            argv = shlex.split(changes[path]["approval_command"])
            self.assertEqual(argv[-2:], ["--", path])

        result, _, error = self.approve(*paths)
        self.assertEqual((result, error), (0, ""))
        result, _, error = self.prepare()
        self.assertEqual((result, error), (0, ""))
        self.assertEqual(self.staged_paths(), set(paths))

    def test_new_handoff_after_preflight_is_a_concurrency_failure(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        event_path = self.write_handoff()

        result, _, error = self.prepare()

        self.assertEqual(result, 1)
        self.assertIn("concurrent handoff", error)
        self.assertIn(event_path, error)
        self.assertEqual(self.staged_paths(), set())

    def test_reviewed_concurrent_handoff_can_join_durable_garden_batch(self) -> None:
        target_path = self.write_handoff()
        run_git(self.root, "add", target_path)
        run_git(self.root, "commit", "-q", "-m", "add target handoff")
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        workstream = "workstreams/existing.md"
        (self.root / workstream).write_text("garden output\n", encoding="utf-8")
        garden_event = self.write_garden_apply(target_path)
        result, _, error = self.approve(workstream, garden_event)
        self.assertEqual((result, error), (0, ""))
        concurrent = self.write_handoff()

        result, _, error = self.adopt(concurrent)
        self.assertEqual((result, error), (0, ""))
        result, _, error = self.prepare()

        self.assertEqual((result, error), (0, ""))
        self.assertEqual(self.staged_paths(), {workstream, garden_event, concurrent})
        state = json.loads((self.state_dir / "checkpoint.json").read_text())
        self.assertEqual(set(state["adopted_handoffs"]), {concurrent})

    def test_unreviewed_concurrent_handoff_blocks_adoption_atomically(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        reviewed = self.write_handoff()
        unreviewed = self.write_handoff()
        before = (self.state_dir / "checkpoint.json").read_bytes()

        result, _, error = self.adopt(reviewed)

        self.assertEqual(result, 1)
        self.assertIn("concurrent handoff", error)
        self.assertIn(unreviewed, error)
        self.assertEqual((self.state_dir / "checkpoint.json").read_bytes(), before)

    def test_status_routes_concurrent_handoff_to_adoption(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        concurrent = self.write_handoff()

        result, output, error = self.command(
            "status", "--state-dir", str(self.state_dir)
        )

        self.assertEqual((result, error), (0, ""))
        change = next(
            item for item in json.loads(output)["changes"] if item["path"] == concurrent
        )
        self.assertTrue(change["requires_adoption"])
        self.assertIsNone(change["approval_command"])
        self.assertEqual(
            shlex.split(change["adoption_command"])[-2:], ["--", concurrent]
        )

    def test_status_emits_one_atomic_command_for_all_concurrent_handoffs(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        concurrent = [self.write_handoff(), self.write_handoff()]

        result, output, error = self.command(
            "status", "--state-dir", str(self.state_dir)
        )

        self.assertEqual((result, error), (0, ""))
        changes = {item["path"]: item for item in json.loads(output)["changes"]}
        commands = {changes[path]["adoption_command"] for path in concurrent}
        self.assertEqual(len(commands), 1)
        argv = shlex.split(commands.pop())
        self.assertEqual(argv[-3:], ["--", *sorted(concurrent)])

    def test_status_commands_invoke_the_kit_checkout(self) -> None:
        # The machinery runs from the kit checkout; the content repo (the
        # fixture root) carries no scripts/ and need not be a uv project.
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        path = "workstreams/pending-review.md"
        (self.root / path).write_text("changed\n", encoding="utf-8")
        concurrent = self.write_handoff()

        result, output, error = self.command(
            "status", "--state-dir", str(self.state_dir)
        )

        self.assertEqual((result, error), (0, ""))
        changes = {item["path"]: item for item in json.loads(output)["changes"]}
        kit_prefix = [
            "uv",
            "run",
            "--project",
            str(REPO_ROOT),
            str(SCRIPT_DIR / "wiki_checkpoint.py"),
        ]
        approval_argv = shlex.split(changes[path]["approval_command"])
        self.assertEqual(approval_argv[:5], kit_prefix)
        self.assertNotIn(str(self.root), approval_argv[:5])
        adoption_argv = shlex.split(changes[concurrent]["adoption_command"])
        self.assertEqual(adoption_argv[:5], kit_prefix)

    def test_adoption_rejects_non_pending_handoff(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        concurrent = self.write_handoff()
        path = self.root / concurrent
        event = json.loads(path.read_text())
        event["status"] = "applied"
        path.write_text(json.dumps(event) + "\n")

        result, _, error = self.adopt(concurrent)

        self.assertEqual(result, 1)
        self.assertIn("not pending_garden", error)

    def test_adoption_rejects_garden_apply_selection(self) -> None:
        target_path = self.write_handoff()
        run_git(self.root, "add", target_path)
        run_git(self.root, "commit", "-q", "-m", "add target handoff")
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        garden_event = self.write_garden_apply(target_path)

        result, _, error = self.adopt(garden_event)

        self.assertEqual(result, 1)
        self.assertIn("not an approved garden apply", error)

    def test_adoption_rejects_duplicate_selection(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        concurrent = self.write_handoff()

        result, _, error = self.adopt(concurrent, concurrent)

        self.assertEqual(result, 1)
        self.assertIn("duplicate path", error)

    def test_handoff_after_adoption_remains_a_concurrency_failure(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        adopted = self.write_handoff()
        result, _, error = self.adopt(adopted)
        self.assertEqual((result, error), (0, ""))
        later = self.write_handoff()

        result, _, error = self.prepare()

        self.assertEqual(result, 1)
        self.assertIn("concurrent handoff", error)
        self.assertIn(later, error)

    def test_adoption_rejects_staged_index_without_state_change(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        concurrent = self.write_handoff()
        (self.root / "planning/existing.md").write_text("staged\n")
        run_git(self.root, "add", "planning/existing.md")
        before = (self.state_dir / "checkpoint.json").read_bytes()

        result, _, error = self.adopt(concurrent)

        self.assertEqual(result, 1)
        self.assertIn("staged changes are not allowed", error)
        self.assertEqual((self.state_dir / "checkpoint.json").read_bytes(), before)

    def test_unexpected_semantic_write_cannot_be_approved_or_prepared(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        path = "planning/unexpected.md"
        (self.root / path).write_text("unexpected\n", encoding="utf-8")

        result, _, error = self.approve(path)
        self.assertEqual(result, 1)
        self.assertIn("outside the garden checkpoint scope", error)
        result, _, error = self.prepare()
        self.assertEqual(result, 1)
        self.assertIn("lack recorded approval", error)
        self.assertEqual(self.staged_paths(), set())

    def test_verify_detects_unstaged_concurrent_write(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        path = "workstreams/approved.md"
        (self.root / path).write_text("approved\n", encoding="utf-8")
        result, _, error = self.approve(path)
        self.assertEqual((result, error), (0, ""))
        result, _, error = self.prepare()
        self.assertEqual((result, error), (0, ""))
        (self.root / "planning" / "race.md").write_text("race\n", encoding="utf-8")

        result, _, error = self.command("verify", "--state-dir", str(self.state_dir))

        self.assertEqual(result, 1)
        self.assertIn("unstaged changes remain", error)

    def test_verify_detects_same_path_staged_content_replacement(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        path = "workstreams/approved.md"
        target = self.root / path
        target.write_text("reviewed\n", encoding="utf-8")
        result, _, error = self.approve(path)
        self.assertEqual((result, error), (0, ""))
        result, _, error = self.prepare()
        self.assertEqual((result, error), (0, ""))
        target.write_text("unreviewed replacement\n", encoding="utf-8")
        run_git(self.root, "add", "--", path)

        result, _, error = self.command("verify", "--state-dir", str(self.state_dir))

        self.assertEqual(result, 1)
        self.assertIn("staged index content changed", error)

    def test_prepare_cleanup_preserves_concurrently_staged_path(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        path = "workstreams/approved.md"
        (self.root / path).write_text("approved\n", encoding="utf-8")
        result, _, error = self.approve(path)
        self.assertEqual((result, error), (0, ""))
        race_path = "planning/concurrent.md"
        original_staged_changes = wiki_checkpoint.staged_changes
        calls = 0

        def staged_changes_with_race(root: Path) -> list[wiki_checkpoint.Change]:
            nonlocal calls
            calls += 1
            if calls == 2:
                (root / race_path).write_text("other workflow\n", encoding="utf-8")
                run_git(root, "add", "--", race_path)
            return original_staged_changes(root)

        with mock.patch.object(
            wiki_checkpoint, "staged_changes", side_effect=staged_changes_with_race
        ):
            result, _, error = self.prepare()

        self.assertEqual(result, 1)
        self.assertIn("unexpected staged paths remain", error)
        self.assertEqual(self.staged_paths(), {race_path})
        self.assertEqual(
            run_git(self.root, "show", f":{race_path}"), b"other workflow\n"
        )

    def test_filesystem_archive_move_prepares_verifies_and_unstages(self) -> None:
        source = ARCHIVE_SOURCE
        destination = ARCHIVE_DESTINATION
        self.commit_workstream(source)
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        (self.root / source).rename(self.root / destination)
        result, _, error = self.approve(source, destination)
        self.assertEqual((result, error), (0, ""))

        result, _, error = self.prepare()

        self.assertEqual((result, error), (0, ""))
        self.assertEqual(
            set(wiki_checkpoint.staged_changes(self.root)),
            {
                wiki_checkpoint.Change("D", source),
                wiki_checkpoint.Change("A", destination),
            },
        )
        state = json.loads(
            (self.state_dir / "checkpoint.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            state["index_records"][source],
            {"kind": "deletion", "mode": None, "oid": None},
        )
        destination_record = state["index_records"][destination]
        self.assertEqual(destination_record["kind"], "entry")
        self.assertIsInstance(destination_record["mode"], str)
        self.assertIsInstance(destination_record["oid"], str)
        result, _, error = self.command("verify", "--state-dir", str(self.state_dir))
        self.assertEqual((result, error), (0, ""))

        result, _, error = self.command("unstage", "--state-dir", str(self.state_dir))

        self.assertEqual((result, error), (0, ""))
        self.assertEqual(self.staged_paths(), set())
        self.assertFalse((self.root / source).exists())
        self.assertEqual(
            (self.root / destination).read_text(encoding="utf-8"), ARCHIVE_CONTENT
        )

    def test_filesystem_archive_move_failure_cleans_both_paths(self) -> None:
        source = ARCHIVE_SOURCE
        destination = ARCHIVE_DESTINATION
        self.commit_workstream(source)
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        (self.root / source).rename(self.root / destination)
        result, _, error = self.approve(source, destination)
        self.assertEqual((result, error), (0, ""))

        with mock.patch.object(
            wiki_checkpoint,
            "staged_index_records",
            side_effect=wiki_checkpoint.CheckpointError("forced fingerprint failure"),
        ):
            result, _, error = self.prepare()

        self.assertEqual(result, 1)
        self.assertIn("forced fingerprint failure", error)
        self.assertEqual(self.staged_paths(), set())
        self.assertEqual(
            set(wiki_checkpoint.working_changes(self.root)),
            {
                wiki_checkpoint.Change("D", source),
                wiki_checkpoint.Change("?", destination),
            },
        )
        self.assertFalse((self.root / source).exists())
        self.assertEqual(
            (self.root / destination).read_text(encoding="utf-8"), ARCHIVE_CONTENT
        )

    def test_generated_deletion_cannot_be_approved(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        path = "wiki/log.md"
        (self.root / path).unlink()

        result, _, error = self.approve(path)

        self.assertEqual(result, 1)
        self.assertIn("must remain a tracked modification", error)

    def test_malformed_events_fail_without_traceback(self) -> None:
        invalid_json = self.root / "wiki/events/2026/07/bad.json"
        invalid_json.parent.mkdir(parents=True, exist_ok=True)
        invalid_json.write_text("{bad\n", encoding="utf-8")

        result, _, error = self.preflight()

        self.assertEqual(result, 1)
        self.assertIn("invalid event", error)
        self.assertNotIn("Traceback", error)
        self.assertEqual(error.count("\n"), 1)

    def test_schema_invalid_event_fails_without_traceback(self) -> None:
        event_path = self.write_handoff()
        path = self.root / event_path
        event = json.loads(path.read_text(encoding="utf-8"))
        del event["summary"]
        path.write_text(json.dumps(event) + "\n", encoding="utf-8")

        result, _, error = self.preflight()

        self.assertEqual(result, 1)
        self.assertIn("invalid event", error)
        self.assertNotIn("Traceback", error)
        self.assertEqual(error.count("\n"), 1)

    def test_unstage_preserves_working_files(self) -> None:
        result, _, error = self.preflight()
        self.assertEqual((result, error), (0, ""))
        path = "workstreams/declined.md"
        target = self.root / path
        target.write_text("keep me\n", encoding="utf-8")
        result, _, error = self.approve(path)
        self.assertEqual((result, error), (0, ""))
        result, _, error = self.prepare()
        self.assertEqual((result, error), (0, ""))

        result, _, error = self.command("unstage", "--state-dir", str(self.state_dir))

        self.assertEqual((result, error), (0, ""))
        self.assertEqual(self.staged_paths(), set())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")

    def test_preflight_resolves_wiki_root_when_repo_root_omitted(self) -> None:
        # The kit CLI's default is no longer a self-derived Path(__file__)
        # guess at the wiki repo's location: omitting --repo-root falls
        # back to wiki_config.resolve_wiki_root(--wiki), which walks up
        # from cwd looking for wiki.toml. Point --wiki straight at the
        # fixture repo and confirm the same preflight succeeds without
        # --repo-root.
        (self.root / "wiki.toml").write_text("[wiki]\n", encoding="utf-8")
        run_git(self.root, "add", "wiki.toml")
        run_git(self.root, "commit", "-q", "-m", "add wiki.toml")
        result, _, error = self.command(
            "preflight",
            "--wiki",
            str(self.root),
            "--state-dir",
            str(self.state_dir),
        )

        self.assertEqual((result, error), (0, ""))
        state = json.loads((self.state_dir / "checkpoint.json").read_text())
        self.assertEqual(Path(state["repo_root"]), self.root.resolve())

    def staged_paths(self) -> set[str]:
        raw = run_git(
            self.root,
            "diff",
            "--cached",
            "--no-renames",
            "--name-only",
            "-z",
        )
        return {value.decode() for value in raw.split(b"\0") if value}


if __name__ == "__main__":
    unittest.main()
