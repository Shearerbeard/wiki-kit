#!/usr/bin/env python3
"""Tests for the memory triage tool. Fully hermetic: every test builds a
tempdir projects-root and never touches the real ~/.claude. The event loader
is mocked at the module boundary — load_events itself is covered by the
wiki-event suite, so these tests exercise only the triage logic. The triage
scope is config-derived (wiki.toml), so scope-sensitive tests write a fixture
wiki.toml (the fictional acme-notes deployment) into tmp_path and pass the
derived set explicitly — never a patched module constant."""

from __future__ import annotations

import contextlib
import io
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

from scripts import wiki_memory_triage as wmt  # noqa: E402
from scripts.wiki_config import ConfigError, load_config  # noqa: E402


def write_memory_file(
    memory_path: Path,
    name: str,
    mtime: datetime | None = None,
    content: str = "x\n",
) -> Path:
    memory_path.mkdir(parents=True, exist_ok=True)
    path = memory_path / name
    path.write_text(content)
    if mtime is not None:
        stamp = mtime.timestamp()
        os.utime(path, (stamp, stamp))
    return path


def write_wiki(root: Path, extra_dirs: list[str] | None = None) -> Path:
    """A minimal fixture wiki: zero companions, optional overlay extras."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "wiki.toml").write_text(
        '[wiki]\nname = "acme-notes"\n\n[contract]\nprotected = ["wiki/log.md"]\n'
    )
    if extra_dirs is not None:
        (root / "wiki.local.toml").write_text(
            "[memory.triage]\nextra_dirs = " + json.dumps(extra_dirs) + "\n"
        )
    return root


def garden_event(timestamp_utc: str) -> dict[str, str]:
    return {"event_type": "garden-apply", "timestamp_utc": timestamp_utc}


def handoff_event(timestamp_utc: str) -> dict[str, str]:
    return {"event_type": "handoff", "timestamp_utc": timestamp_utc}


class ClassifyPrefixTests(unittest.TestCase):
    def test_known_prefixes_map_to_their_class(self):
        cases = {
            "user-style.md": wmt.PrefixClass.USER,
            "feedback-no-fallbacks.md": wmt.PrefixClass.FEEDBACK,
            "project-widget-status.md": wmt.PrefixClass.PROJECT,
            "reference-dashboards.md": wmt.PrefixClass.REFERENCE,
            "session-2026-06.md": wmt.PrefixClass.SESSION,
        }
        for name, expected in cases.items():
            self.assertEqual(wmt.classify_prefix(name), expected)

    def test_unknown_and_unprefixed_collapse_to_other(self):
        for name in (
            "future-work-roadmap.md",  # known prefix, not in keep/migrate sets
            "wid123-kickoff.md",
            "scratchpad.md",  # no '-' at all
            "sre-synthesis-lossiness.md",
            "tracker-conventions.md",
        ):
            self.assertEqual(wmt.classify_prefix(name), wmt.PrefixClass.OTHER, name)

    def test_underscore_delimited_prefixes(self):
        # Some project dirs use '_' instead of '-' as the prefix
        # delimiter; both must classify identically.
        cases = {
            "feedback_commit_standards.md": wmt.PrefixClass.FEEDBACK,
            "project_widget.md": wmt.PrefixClass.PROJECT,
            "reference_infra.md": wmt.PrefixClass.REFERENCE,
            "user_role.md": wmt.PrefixClass.USER,
        }
        for name, expected in cases.items():
            self.assertEqual(wmt.classify_prefix(name), expected, name)

    def test_suffix_stripped_before_prefix_extraction(self):
        # A single-token name (no second '-' segment): the .md must be stripped
        # first, else the prefix would read as "user.md" and fall to OTHER.
        self.assertEqual(wmt.classify_prefix("user.md"), wmt.PrefixClass.USER)
        self.assertEqual(wmt.classify_prefix("reference.md"), wmt.PrefixClass.REFERENCE)


class DispositionTests(unittest.TestCase):
    def test_keep_classes(self):
        for cls in (wmt.PrefixClass.USER, wmt.PrefixClass.FEEDBACK):
            self.assertIs(wmt.disposition_for(cls), wmt.Disposition.KEEP)

    def test_migrate_classes(self):
        for cls in (
            wmt.PrefixClass.PROJECT,
            wmt.PrefixClass.REFERENCE,
            wmt.PrefixClass.SESSION,
        ):
            self.assertIs(wmt.disposition_for(cls), wmt.Disposition.MIGRATE)

    def test_other_flags(self):
        self.assertIs(wmt.disposition_for(wmt.PrefixClass.OTHER), wmt.Disposition.FLAG)


class MemoryFilesTests(unittest.TestCase):
    def test_excludes_index_and_sorts(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp) / "mem"
            write_memory_file(mem, "MEMORY.md")
            write_memory_file(mem, "project-b.md")
            write_memory_file(mem, "feedback-a.md")
            names = [p.name for p in wmt.memory_files(mem)]
            self.assertEqual(names, ["feedback-a.md", "project-b.md"])

    def test_missing_dir_fails_loud(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaises(FileNotFoundError),
        ):
            wmt.memory_files(Path(tmp) / "does-not-exist")


class TriageSetDerivationTests(unittest.TestCase):
    """The knob-7 derivation the CLI feeds into walk_triage."""

    def test_derived_set_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            widget = base / "src" / "widget"
            widget.mkdir(parents=True)
            root = base / "acme-notes"
            root.mkdir()
            (root / "wiki.toml").write_text(
                "[wiki]\n"
                'name = "acme-notes"\n'
                "\n"
                "[companions.widget]\n"
                'github = "acme/widget"\n'
                "memory_triage = true\n"
                "\n"
                "[contract]\n"
                'protected = ["wiki/log.md"]\n'
            )
            (root / "wiki.local.toml").write_text(
                "[companions.widget]\n"
                f'path = "{widget}"\n'
                "\n"
                "[memory.triage]\n"
                'extra_dirs = ["-home-alex-src-widget-docs"]\n'
            )
            config = load_config(root)

            def slug(path: Path) -> str:
                # The documented dash-encoded-absolute-path rule.
                return str(path.resolve()).replace("/", "-")

            self.assertEqual(
                config.triage_project_dirs(),
                (slug(root), slug(widget), "-home-alex-src-widget-docs"),
            )

    def test_triage_companion_without_overlay_path_fails_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acme-notes"
            root.mkdir()
            (root / "wiki.toml").write_text(
                "[wiki]\n"
                'name = "acme-notes"\n'
                "\n"
                "[companions.widget]\n"
                "memory_triage = true\n"
                "\n"
                "[contract]\n"
                'protected = ["wiki/log.md"]\n'
            )
            config = load_config(root)
            with self.assertRaises(ConfigError) as ctx:
                config.triage_project_dirs()
            self.assertIn("widget", str(ctx.exception))


class WalkTriageScanTests(unittest.TestCase):
    def test_full_scan_one_disposition_per_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mem = wmt.memory_dir(root, "projA")
            write_memory_file(mem, "user-me.md")
            write_memory_file(mem, "feedback-x.md")
            write_memory_file(mem, "project-y.md")
            write_memory_file(mem, "reference-z.md")
            write_memory_file(mem, "session-s.md")
            write_memory_file(mem, "scratchpad.md")  # OTHER -> FLAG
            write_memory_file(mem, "MEMORY.md")  # excluded

            entries = wmt.walk_triage(root, family_dirs=("projA",))

            # Manifest invariant: exactly the non-index files, each with one
            # valid disposition, and is_exception iff that disposition is FLAG.
            self.assertEqual(len(entries), 6)
            for entry in entries:
                self.assertIn(entry.disposition, set(wmt.Disposition))
                self.assertEqual(
                    entry.is_exception, entry.disposition is wmt.Disposition.FLAG
                )
            counts = {
                disp: sum(1 for e in entries if e.disposition is disp)
                for disp in wmt.Disposition
            }
            self.assertEqual(counts[wmt.Disposition.KEEP], 2)
            self.assertEqual(counts[wmt.Disposition.MIGRATE], 3)
            self.assertEqual(counts[wmt.Disposition.FLAG], 1)

    def test_scope_is_limited_to_family_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_memory_file(wmt.memory_dir(root, "projA"), "project-a.md")
            write_memory_file(wmt.memory_dir(root, "dotfiles"), "project-out.md")
            entries = wmt.walk_triage(root, family_dirs=("projA",))
            self.assertEqual([e.project_dir for e in entries], ["projA"])
            self.assertEqual(entries[0].filename, "project-a.md")

    def test_index_only_dir_contributes_nothing(self):
        # An in-scope dir holding only MEMORY.md is empty of facts, not an error.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_memory_file(wmt.memory_dir(root, "projA"), "MEMORY.md")
            self.assertEqual(wmt.walk_triage(root, family_dirs=("projA",)), [])

    def test_dirs_walked_in_given_order_files_sorted_within(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_memory_file(wmt.memory_dir(root, "projB"), "project-b2.md")
            write_memory_file(wmt.memory_dir(root, "projB"), "project-b1.md")
            write_memory_file(wmt.memory_dir(root, "projA"), "project-a.md")
            entries = wmt.walk_triage(root, family_dirs=("projA", "projB"))
            self.assertEqual(
                [(e.project_dir, e.filename) for e in entries],
                [
                    ("projA", "project-a.md"),
                    ("projB", "project-b1.md"),
                    ("projB", "project-b2.md"),
                ],
            )


class WalkTriageSinceGardenTests(unittest.TestCase):
    def test_keeps_only_files_modified_after_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mem = wmt.memory_dir(root, "projA")
            write_memory_file(mem, "project-old.md", datetime(2026, 6, 9, tzinfo=UTC))
            write_memory_file(
                mem, "project-at-baseline.md", datetime(2026, 6, 10, tzinfo=UTC)
            )
            write_memory_file(mem, "project-new.md", datetime(2026, 6, 11, tzinfo=UTC))
            baseline = "2026-06-10T00:00:00Z"
            entries = wmt.walk_triage(
                root, family_dirs=("projA",), modified_after=baseline
            )
            # Strictly-later only: the at-baseline file is excluded.
            self.assertEqual([e.filename for e in entries], ["project-new.md"])


class LatestGardenTimestampTests(unittest.TestCase):
    def test_returns_latest_garden_apply(self):
        events = [
            handoff_event("2026-06-05T00:00:00Z"),
            garden_event("2026-06-06T00:09:34Z"),
            handoff_event("2026-06-07T00:00:00Z"),
            garden_event("2026-06-08T12:00:00Z"),
        ]
        with mock.patch.object(wmt, "load_events", return_value=events):
            self.assertEqual(
                wmt.latest_garden_timestamp(Path("ignored")),
                "2026-06-08T12:00:00Z",
            )

    def test_no_garden_event_fails_loud(self):
        events = [handoff_event("2026-06-07T00:00:00Z")]
        with (
            mock.patch.object(wmt, "load_events", return_value=events),
            self.assertRaises(ValueError),
        ):
            wmt.latest_garden_timestamp(Path("ignored"))


class MainTests(unittest.TestCase):
    """CLI-level tests: the scope comes from a fixture wiki.toml via --wiki
    (the config-derived set replaced the old patched module constant)."""

    def _run(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wmt.main(argv)
        return code, out.getvalue()

    def _fixture(self, tmp: Path) -> tuple[Path, Path]:
        """A wiki whose derived triage set is (own slug, "projA"), plus a
        projects-root already holding the wiki's own (empty) memory dir."""
        root = write_wiki(tmp / "acme-notes", extra_dirs=["projA"])
        config = load_config(root)
        projects_root = tmp / "projects"
        wmt.memory_dir(projects_root, config.project_slug(root)).mkdir(parents=True)
        return root, projects_root

    def test_scan_json_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, projects_root = self._fixture(Path(tmp))
            write_memory_file(wmt.memory_dir(projects_root, "projA"), "feedback-x.md")
            write_memory_file(wmt.memory_dir(projects_root, "projA"), "project-y.md")
            code, stdout = self._run(
                [
                    "scan",
                    "--json",
                    "--wiki",
                    str(root),
                    "--projects-root",
                    str(projects_root),
                ]
            )
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertIsNone(payload["baseline"])
            dispositions = {
                row["filename"]: row["disposition"] for row in payload["manifest"]
            }
            self.assertEqual(dispositions["feedback-x.md"], "keep")
            self.assertEqual(dispositions["project-y.md"], "migrate")

    def test_since_garden_uses_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, projects_root = self._fixture(Path(tmp))
            mem = wmt.memory_dir(projects_root, "projA")
            write_memory_file(mem, "project-old.md", datetime(2026, 6, 1, tzinfo=UTC))
            write_memory_file(mem, "project-new.md", datetime(2026, 6, 12, tzinfo=UTC))
            events = [garden_event("2026-06-06T00:00:00Z")]
            with mock.patch.object(wmt, "load_events", return_value=events):
                code, stdout = self._run(
                    [
                        "since-garden",
                        "--json",
                        "--wiki",
                        str(root),
                        "--projects-root",
                        str(projects_root),
                        "--events-dir",
                        str(root),
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["baseline"], "2026-06-06T00:00:00Z")
            names = [row["filename"] for row in payload["manifest"]]
            self.assertEqual(names, ["project-new.md"])

    def test_since_garden_no_baseline_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, projects_root = self._fixture(Path(tmp))
            write_memory_file(wmt.memory_dir(projects_root, "projA"), "project-y.md")
            err = io.StringIO()
            with (
                mock.patch.object(wmt, "load_events", return_value=[]),
                contextlib.redirect_stderr(err),
            ):
                code, _ = self._run(
                    [
                        "since-garden",
                        "--wiki",
                        str(root),
                        "--projects-root",
                        str(projects_root),
                        "--events-dir",
                        str(root),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("no garden-apply event", err.getvalue())

    def test_listed_but_missing_dir_fails_loud(self):
        # The derived set names projA but the projects-root has no such
        # memory dir: a config/reality mismatch, reported, exit 1.
        with tempfile.TemporaryDirectory() as tmp:
            root, projects_root = self._fixture(Path(tmp))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, _ = self._run(
                    [
                        "scan",
                        "--wiki",
                        str(root),
                        "--projects-root",
                        str(projects_root),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("memory dir missing", err.getvalue())


class FrontmatterTypeTests(unittest.TestCase):
    def test_top_level_type(self):
        text = "---\nname: x\ntype: reference\n---\nbody\n"
        self.assertEqual(wmt.frontmatter_type(text), "reference")

    def test_nested_metadata_type(self):
        text = "---\nname: x\nmetadata:\n  node_type: memory\n  type: project\n---\n"
        self.assertEqual(wmt.frontmatter_type(text), "project")

    def test_node_type_alone_not_matched(self):
        self.assertIsNone(wmt.frontmatter_type("---\nnode_type: memory\n---\n"))

    def test_no_frontmatter(self):
        self.assertIsNone(wmt.frontmatter_type("just a body line\n"))

    def test_type_after_closing_fence_ignored(self):
        text = "---\nname: x\n---\ntype: reference in prose\n"
        self.assertIsNone(wmt.frontmatter_type(text))


class ClassFromFrontmatterTypeTests(unittest.TestCase):
    def test_valid(self):
        self.assertIs(
            wmt.class_from_frontmatter_type("reference"), wmt.PrefixClass.REFERENCE
        )

    def test_unknown_and_none(self):
        self.assertIsNone(wmt.class_from_frontmatter_type("widget"))
        self.assertIsNone(wmt.class_from_frontmatter_type(None))


class EphemeralMarkerTests(unittest.TestCase):
    def test_strong_markers_match(self):
        self.assertEqual(
            wmt.ephemeral_marker("...Delete this memory once done."),
            "self-delete note",
        )
        self.assertEqual(wmt.ephemeral_marker("- **PID:** 14148"), "live PID")

    def test_bare_tmp_log_is_not_a_marker(self):
        # A durable note may cite a /tmp/*.log path; that alone must NOT trip
        # ephemeral (review finding: 3:1 false positives on a real corpus).
        self.assertIsNone(wmt.ephemeral_marker("see the run output at /tmp/run.log"))

    def test_clean_text_no_marker(self):
        self.assertIsNone(wmt.ephemeral_marker("a durable note on the branch model"))


class ResolveDispositionTests(unittest.TestCase):
    def test_confident_prefix_wins_over_frontmatter(self):
        # prefix is recognized -> frontmatter is not even consulted
        text = "---\ntype: feedback\n---\nbody"
        d = wmt.resolve_disposition(wmt.PrefixClass.PROJECT, text)
        self.assertEqual(d.disposition, wmt.Disposition.MIGRATE)
        self.assertEqual(d.reason, "prefix:project")

    def test_frontmatter_tiebreaks_other(self):
        text = "---\ntype: reference\n---\nbody"
        d = wmt.resolve_disposition(wmt.PrefixClass.OTHER, text)
        self.assertEqual(d.disposition, wmt.Disposition.MIGRATE)
        self.assertEqual(d.reason, "frontmatter:type=reference")

    def test_other_without_type_flags(self):
        d = wmt.resolve_disposition(wmt.PrefixClass.OTHER, "no frontmatter here")
        self.assertEqual(d.disposition, wmt.Disposition.FLAG)
        self.assertTrue(d.reason.startswith("unresolved"))

    def test_ephemeral_demotes_migrate_to_drop(self):
        text = "---\ntype: project\n---\nrun status, **PID:** 9912 still going"
        d = wmt.resolve_disposition(wmt.PrefixClass.OTHER, text)
        self.assertEqual(d.disposition, wmt.Disposition.DROP)
        self.assertTrue(d.reason.startswith("ephemeral:"))

    def test_ephemeral_overrides_confident_project_prefix(self):
        d = wmt.resolve_disposition(
            wmt.PrefixClass.PROJECT, "delete this memory when reviewed"
        )
        self.assertEqual(d.disposition, wmt.Disposition.DROP)

    def test_keep_never_dropped_by_ephemeral(self):
        # the safety asymmetry: a real ephemeral marker on a KEEP file is ignored
        d = wmt.resolve_disposition(
            wmt.PrefixClass.FEEDBACK, "delete this memory when reviewed"
        )
        self.assertEqual(d.disposition, wmt.Disposition.KEEP)


class TriageEntryReasonTests(unittest.TestCase):
    def test_entry_carries_reason_across_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mem = wmt.memory_dir(root, "projA")
            write_memory_file(mem, "feedback-style.md", content="how to work\n")
            write_memory_file(
                mem, "tracker-reference.md", content="---\ntype: reference\n---\nids\n"
            )
            write_memory_file(
                mem,
                "project-e2e-run.md",
                content="---\ntype: project\n---\n**PID:** 14148\n"
                "Delete this memory once reviewed.\n",
            )
            by_name = {
                e.filename: e for e in wmt.walk_triage(root, family_dirs=("projA",))
            }
            self.assertEqual(
                by_name["feedback-style.md"].disposition, wmt.Disposition.KEEP
            )
            self.assertEqual(by_name["feedback-style.md"].reason, "prefix:feedback")
            self.assertEqual(
                by_name["tracker-reference.md"].disposition, wmt.Disposition.MIGRATE
            )
            self.assertEqual(
                by_name["tracker-reference.md"].reason, "frontmatter:type=reference"
            )
            self.assertEqual(
                by_name["project-e2e-run.md"].disposition, wmt.Disposition.DROP
            )
            self.assertTrue(
                by_name["project-e2e-run.md"].reason.startswith("ephemeral:")
            )


if __name__ == "__main__":
    unittest.main()
