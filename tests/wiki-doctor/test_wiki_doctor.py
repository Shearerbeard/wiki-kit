#!/usr/bin/env python3
"""Tests for the wiki doctor checks.

Ported from the source suite with these recorded deltas:
- config-drift cases dropped: the check is deleted in the kit (the
  ledger deletes STALE_README_PATTERNS outright, and the source suite's
  model-table case referenced a check shape absent at the freeze).
- opencode external_directory case dropped: consumer-side opencode
  config is the dock card's scope; the kit doctor at this stage checks
  the wiki repo only.
- new coverage: config strictness (machine paths, overlay allowlist),
  the capture sha256 audit, and the install check (hook wrapper +
  contract-derived deny rules).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

KIT_ROOT = Path(__file__).resolve().parents[2]


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "scripts.wiki_doctor", KIT_ROOT / "scripts" / "wiki_doctor.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load scripts/wiki_doctor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scripts.wiki_doctor"] = mod
    spec.loader.exec_module(mod)
    return mod


wiki_doctor = _load_module()

MINIMAL_CONFIG = """\
[wiki]
name = "acme-notes"

[contract]
protected = ["CLAUDE.local.md", "wiki/log.md", "wiki/events/**"]
external_allow = []
skills = ["garden"]
global_skills = []

[companions.widget]
github = "acme/widget"
"""

WORKSTREAM_OK = (
    "---\n"
    "status: active\n"
    "branch: main\n"
    "sha: abc1234\n"
    "repo: acme/widget\n"
    "last_updated: 2026-07-03\n"
    "blocker: none\n"
    "---\n"
    "## Session updates (uncurated)\n"
)


def _forefront(active: int, cap: int | None, listed: int, pinned: int = 0) -> Any:
    return wiki_doctor.Forefront(active, cap, listed, pinned, 68, 37)


class OrientationLeverTest(unittest.TestCase):
    """The lever text sizes both actions from the forefront facts."""

    def test_capped_view_sizes_both_actions(self) -> None:
        # 100 over: a lowered entry saves 68 - 37 = 31, so four; an
        # archived row saves 37, so three.
        text = wiki_doctor.orientation_lever(3100, 3000, _forefront(30, 8, 8))
        self.assertIn(
            "Lever: 30 active, 8 listed in full (workstreams_in_view 8);", text
        )
        self.assertIn("about 68 tokens", text)
        self.assertIn("about 37", text)
        self.assertIn("Set workstreams_in_view to 4 (about 124 tokens)", text)
        self.assertIn("archive 3 workstreams (about 111 tokens)", text)
        self.assertNotIn("short of", text)
        self.assertNotIn("pinned", text)

    def test_overrun_boundaries(self) -> None:
        one = wiki_doctor.orientation_lever(3001, 3000, _forefront(30, 8, 8))
        self.assertIn("Set workstreams_in_view to 7 (about 31 tokens)", one)
        self.assertIn("archive 1 workstreams (about 37 tokens)", one)
        exact = wiki_doctor.orientation_lever(3037, 3000, _forefront(30, 8, 8))
        self.assertIn("Set workstreams_in_view to 6 (about 62 tokens)", exact)
        self.assertIn("archive 1 workstreams (about 37 tokens)", exact)

    def test_no_cap_reads_as_all_listed(self) -> None:
        text = wiki_doctor.orientation_lever(3100, 3000, _forefront(12, None, 12))
        self.assertIn("12 active, all listed in full (no cap);", text)
        self.assertIn("Set workstreams_in_view to 8 ", text)

    def test_cap_above_the_active_count_lowers_from_the_listed_count(self) -> None:
        text = wiki_doctor.orientation_lever(3100, 3000, _forefront(5, 8, 5))
        self.assertIn("5 active, all listed in full (workstreams_in_view 8);", text)
        self.assertIn("Set workstreams_in_view to 1 ", text)

    def test_pins_raise_the_floor(self) -> None:
        # Eight pinned under a cap of eight: lowering the cap changes
        # nothing, and the text says so instead of promising a saving.
        text = wiki_doctor.orientation_lever(3051, 3000, _forefront(30, 8, 8, pinned=8))
        self.assertIn("8 listed in full (workstreams_in_view 8), 8 pinned", text)
        self.assertIn("workstreams_in_view is at its floor of 8 (8 pinned by", text)
        self.assertNotIn("Set workstreams_in_view", text)
        # Three pinned: the view lowers to three at most.
        text = wiki_doctor.orientation_lever(3400, 3000, _forefront(30, 8, 8, pinned=3))
        self.assertIn("Set workstreams_in_view to 3 (about 155 tokens, short of", text)

    def test_an_action_that_cannot_cover_the_overrun_says_so(self) -> None:
        text = wiki_doctor.orientation_lever(3200, 3000, _forefront(3, 3, 3))
        self.assertIn(
            "Set workstreams_in_view to 1 (about 62 tokens, short of the 200 over)",
            text,
        )
        self.assertIn(
            "archive 3 workstreams (about 111 tokens, short of the 200 over)", text
        )

    def test_pins_floor_a_cap_proposed_from_no_cap(self) -> None:
        forefront = _forefront(8, None, 8, pinned=8)
        text = wiki_doctor.orientation_lever(3051, 3000, forefront)
        self.assertIn("8 active, all listed in full (no cap), 8 pinned", text)
        self.assertIn("workstreams_in_view is at its floor of 8 (8 pinned by", text)
        self.assertNotIn("Set workstreams_in_view", text)

    def test_the_view_never_drops_to_zero(self) -> None:
        for cap in (1, None):
            text = wiki_doctor.orientation_lever(3200, 3000, _forefront(1, cap, 1))
            self.assertIn(
                "workstreams_in_view is at its floor of 1 (0 means no cap)", text
            )
            self.assertNotIn("Set workstreams_in_view to 0", text)
            self.assertIn("archive 1 workstreams", text)

    def test_no_active_workstreams_points_at_the_fixed_sections(self) -> None:
        text = wiki_doctor.orientation_lever(3100, 3000, _forefront(0, 8, 0))
        self.assertIn("No active workstreams to collapse or archive", text)
        self.assertNotIn("Set workstreams_in_view", text)

    def test_needs_an_overrun(self) -> None:
        with self.assertRaises(ValueError):
            wiki_doctor.orientation_lever(3000, 3000, _forefront(30, 8, 8))


class DoctorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "wiki"
        self.root.mkdir()
        self.projects = Path(self._tmp.name) / "projects"
        self.projects.mkdir()
        self.write("wiki.toml", MINIMAL_CONFIG)
        self._rebuild_ctx()

    def _rebuild_ctx(self) -> None:
        config = wiki_doctor.load_config(self.root)
        self.ctx = wiki_doctor.DoctorContext(config=config, projects_root=self.projects)

    def write(self, path: str, text: str) -> Path:
        out = self.root / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return out

    # -- config ------------------------------------------------------------

    def test_config_machine_path_in_committed_file_fails(self) -> None:
        self.write(
            "wiki.toml",
            MINIMAL_CONFIG + '\n[night]\nreport_dir = "/home/alex/reports"\n',
        )
        self._rebuild_ctx()

        result = wiki_doctor.check_config(self.ctx)

        self.assertTrue(result.failed)
        self.assertIn("machine path", result.findings[0].message)

    def test_config_index_line_is_exempt_from_the_path_rule(self) -> None:
        self.write(
            "wiki.toml",
            MINIMAL_CONFIG + '\n[memory]\nindex_line = "Memory: ~/notes/MEMORY.md"\n',
        )
        self._rebuild_ctx()

        result = wiki_doctor.check_config(self.ctx)

        self.assertFalse(result.failed)

    def test_overlay_allowlist_violation_fails_loud_at_load(self) -> None:
        self.write("wiki.local.toml", '[contract]\nprotected = []\n')

        with self.assertRaises(wiki_doctor.ConfigError):
            wiki_doctor.load_config(self.root)

    def test_companion_base_branch_must_be_a_string(self) -> None:
        self.write(
            "wiki.toml",
            MINIMAL_CONFIG.replace(
                'github = "acme/widget"',
                'github = "acme/widget"\nbase_branch = 42',
            ),
        )

        with self.assertRaises(wiki_doctor.ConfigError):
            wiki_doctor.load_config(self.root)

    # -- projections -------------------------------------------------------

    def test_render_log_detects_stale_projection(self) -> None:
        self.write(
            "wiki/log-epoch.json",
            json.dumps(
                {"schema_version": 1, "render_epoch_start": "2026-06-04T16:30:00Z"}
            ),
        )
        self.write(
            "wiki/log-legacy.md",
            "## [2026-06-04T16:30:00Z] Legacy\n- kept\n",
        )
        (self.root / "wiki" / "events").mkdir(parents=True, exist_ok=True)
        self.write("wiki/quarantine.json", json.dumps(
            {"schema_version": 1, "note": "empty", "quarantined": []}
        ))
        self.write("wiki/log.md", "stale\n")

        result = wiki_doctor.check_render_log(self.ctx)

        self.assertTrue(result.failed)
        self.assertIn("differs", result.findings[0].message)

    def test_render_log_reports_half_present_legacy_pair_as_finding(self) -> None:
        """A ValidationError from the renderer is a FAIL finding, never a
        doctor crash (Gate S probe finding)."""
        self.write("wiki/log-legacy.md", "## [2026-06-04T16:30:00Z] Legacy\n")
        (self.root / "wiki" / "events").mkdir(parents=True, exist_ok=True)
        self.write("wiki/quarantine.json", json.dumps(
            {"schema_version": 1, "note": "empty", "quarantined": []}
        ))
        self.write("wiki/log.md", "anything\n")

        result = wiki_doctor.check_render_log(self.ctx)

        self.assertTrue(result.failed)

    def test_repo_names_skips_malformed_workstream_file(self) -> None:
        """A FrontmatterError file is check_validate_workstreams' finding;
        repo-names must not crash on it (Gate S probe finding)."""
        self.write("workstreams/broken.md", "no frontmatter at all\n")

        result = wiki_doctor.check_repo_names(self.ctx)

        self.assertFalse(result.failed)

    def test_pending_index_detects_projection_mismatch(self) -> None:
        (self.root / "wiki" / "events").mkdir(parents=True, exist_ok=True)
        (self.root / "wiki" / "sources").mkdir(parents=True, exist_ok=True)
        self.write("wiki/quarantine.json", json.dumps(
            {"schema_version": 1, "note": "empty", "quarantined": []}
        ))
        self.write(
            "wiki/pending/index.json",
            json.dumps(
                {
                    "schema_version": 2,
                    "generated_at_utc": "2026-07-03T00:00:00Z",
                    "event_count": 1,
                    "events": [],
                }
            ),
        )
        self.write("wiki/pending/latest.md", "stale\n")

        result = wiki_doctor.check_pending_index(self.ctx)

        self.assertTrue(result.failed)

    # -- workstreams -------------------------------------------------------

    def test_frontmatter_roundtrip_detects_noncanonical_file(self) -> None:
        self.write(
            "workstreams/example.md",
            "---\n"
            "status: active\n"
            "branch: main\n"
            "sha: abc1234\n"
            "last_updated: 2026-07-03\n"
            "blocker: needs review\n"
            "---\n"
            "## Session updates (uncurated)\n",
        )

        result = wiki_doctor.check_frontmatter_roundtrip(self.ctx)

        self.assertTrue(result.failed)
        self.assertIn("canonical writer form", result.findings[0].message)

    def test_validate_workstreams_detects_missing_session_updates(self) -> None:
        self.write(
            "workstreams/example.md",
            "---\n"
            "status: active\n"
            "branch: main\n"
            "sha: abc1234\n"
            "last_updated: 2026-07-03\n"
            "---\n"
            "## Notes\n",
        )

        result = wiki_doctor.check_validate_workstreams(self.ctx)

        self.assertTrue(result.failed)
        self.assertIn("Session updates", result.findings[0].message)

    def test_validate_workstreams_covers_archive_subdir(self) -> None:
        """The _archive no-op fix: an invalid archived workstream is
        caught; _reference stays out of scope."""
        self.write(
            "workstreams/_archive/old.md",
            "---\nbogus: true\n---\n\nbody\n",
        )
        self.write("workstreams/_reference/free-form.md", "anything goes\n")

        result = wiki_doctor.check_validate_workstreams(self.ctx)

        self.assertTrue(result.failed)
        self.assertTrue(
            all("_reference" not in (f.path or "") for f in result.findings)
        )
        self.assertTrue(
            any("_archive" in (f.path or "") for f in result.findings)
        )

    def test_validate_workstreams_detects_incomplete_prose_v1(self) -> None:
        self.write(
            "workstreams/example.md",
            "---\n"
            "status: active\n"
            "branch: main\n"
            "sha: abc1234\n"
            "last_updated: 2026-07-03\n"
            "template: prose-v1\n"
            "---\n"
            "## Notes\n\n"
            "### Current State\n\n"
            "## Session updates (uncurated)\n",
        )

        result = wiki_doctor.check_validate_workstreams(self.ctx)

        self.assertTrue(result.failed)
        self.assertTrue(
            any("Curated State" in finding.message for finding in result.findings)
        )

    # -- links, budgets, board --------------------------------------------

    def test_link_check_detects_broken_markdown_link(self) -> None:
        self.write("README.md", "See [missing](docs/missing.md).\n")

        result = wiki_doctor.check_links(self.ctx)

        self.assertTrue(result.failed)
        self.assertIn("broken markdown link", result.findings[0].message)

    def test_link_check_includes_wiki_index_when_present(self) -> None:
        self.write("wiki/index.md", "See [gone](nowhere.md).\n")

        result = wiki_doctor.check_links(self.ctx)

        self.assertTrue(result.failed)

    def test_token_budget_detects_hard_memory_overrun(self) -> None:
        self.write("CLAUDE.local.md", "small\n")
        slug = self.ctx.config.project_slug(self.root)
        memory = self.projects / slug / "memory" / "MEMORY.md"
        memory.parent.mkdir(parents=True)
        memory.write_text("x" * 9000, encoding="utf-8")

        result = wiki_doctor.check_token_budgets(self.ctx)

        self.assertTrue(result.failed)
        self.assertIn("MEMORY.md", result.findings[0].message)

    def test_token_budget_reads_the_deployments_budgets_table(self) -> None:
        # 400 bytes is ~100 tokens: fine against the default 3000-token
        # hard cap, over a deployment-set cap of 50.
        self.write("CLAUDE.local.md", "x" * 400 + "\n")
        self.assertFalse(wiki_doctor.check_token_budgets(self.ctx).failed)

        self.write(
            "wiki.toml",
            MINIMAL_CONFIG + "\n[budgets]\norientation_index_tokens = 50\n"
            "orientation_index_warn_tokens = 25\n",
        )
        self._rebuild_ctx()
        result = wiki_doctor.check_token_budgets(self.ctx)
        self.assertTrue(result.failed)
        self.assertIn("hard=50", result.findings[0].message)

    def _budgets(self, table: str) -> None:
        self.write("wiki.toml", MINIMAL_CONFIG + "\n[budgets]\n" + table)
        self._rebuild_ctx()

    def _active_workstreams(self, count: int) -> None:
        (self.root / "wiki" / "events").mkdir(parents=True, exist_ok=True)
        for day in range(1, count + 1):
            self.write(
                f"workstreams/stream-{day:02d}.md",
                WORKSTREAM_OK.replace("2026-07-03", f"2026-07-{day:02d}"),
            )

    def _pin(self, names: list[str]) -> None:
        import wiki_event

        event = {
            "event_id": wiki_event.uuid7(),
            "event_type": "handoff",
            "schema_version": 2,
            "timestamp_utc": "2026-07-20T00:00:00Z",
            "tool": "manual",
            "status": "pending_garden",
            "summary": "pins",
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
                {"name": name, "relationship": "primary", "proposed_action": "u"}
                for name in names
            ],
        }
        wiki_event.validate_event(event)
        wiki_event.write_event(self.root / "wiki" / "events", event)

    def _tree(self) -> str:
        result = subprocess.run(
            [
                sys.executable,
                str(KIT_ROOT / "scripts" / "build-index.py"),
                "--wiki",
                str(self.root),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def _orientation_message(self) -> str:
        result = wiki_doctor.check_token_budgets(self.ctx)
        finding = result.findings[0]
        self.assertIn("CLAUDE.local.md", finding.message)
        return finding.message

    def test_token_budget_orientation_fail_names_the_lever(self) -> None:
        # 1201 bytes is 301 tokens: 101 over a 200 ceiling, so the lever
        # lowers the view by four (31 saved each) or archives three (37).
        self._budgets("orientation_index_tokens = 200\n")
        self._active_workstreams(12)
        self.write("CLAUDE.local.md", "x" * 1200 + "\n")

        result = wiki_doctor.check_token_budgets(self.ctx)

        self.assertTrue(result.failed)
        message = result.findings[0].message
        self.assertIn("CLAUDE.local.md estimates 301 tokens", message)
        self.assertIn(
            "Lever: 12 active, 8 listed in full (workstreams_in_view 8);", message
        )
        self.assertIn("Set workstreams_in_view to 4 (about 124 tokens)", message)
        self.assertIn("archive 3 workstreams (about 111 tokens)", message)
        self.assertNotIn("park", message)

    def test_token_budget_lever_actions_shrink_the_rendered_tree(self) -> None:
        # The promise is checked against this renderer: the recommended
        # cap collapses four more entries, and archiving three removes
        # three rows; both renders are smaller than the one measured.
        self._budgets("orientation_index_tokens = 200\n")
        self._active_workstreams(12)
        self.write("CLAUDE.local.md", "x" * 1200 + "\n")
        self.assertIn("Set workstreams_in_view to 4 ", self._orientation_message())
        before = self._tree()
        self.assertIn("NOT IN THE FOREFRONT (4)", before)

        self._budgets("orientation_index_tokens = 200\nworkstreams_in_view = 4\n")
        lowered = self._tree()
        self.assertIn("NOT IN THE FOREFRONT (8)", lowered)
        self.assertLess(
            wiki_doctor.estimate_tokens(lowered.encode()),
            wiki_doctor.estimate_tokens(before.encode()),
        )

        self._budgets("orientation_index_tokens = 200\n")
        archive = self.root / "workstreams" / "_archive"
        archive.mkdir()
        for day in (1, 2, 3):
            page = self.root / "workstreams" / f"stream-{day:02d}.md"
            page.rename(archive / page.name)
        archived = self._tree()
        self.assertIn("NOT IN THE FOREFRONT (1)", archived)
        self.assertLess(
            wiki_doctor.estimate_tokens(archived.encode()),
            wiki_doctor.estimate_tokens(before.encode()),
        )

    def test_token_budget_lever_floors_at_the_pinned_count(self) -> None:
        # Three pins with a cap of three: the view cannot lower, and a
        # cap of one renders the same tree.
        self._budgets("orientation_index_tokens = 200\nworkstreams_in_view = 3\n")
        self._active_workstreams(12)
        self._pin(["stream-01", "stream-02", "stream-03"])
        self.write("CLAUDE.local.md", "x" * 1200 + "\n")

        message = self._orientation_message()

        self.assertIn(
            "3 listed in full (workstreams_in_view 3), 3 pinned by the newest "
            "handoff;",
            message,
        )
        self.assertIn(
            "workstreams_in_view is at its floor of 3 (3 pinned by the newest "
            "handoff), or archive 3 workstreams",
            message,
        )
        self.assertNotIn("Set workstreams_in_view", message)
        before = self._tree()
        self._budgets("orientation_index_tokens = 200\nworkstreams_in_view = 1\n")
        self.assertEqual(self._tree(), before)

    def test_token_budget_lever_floors_at_the_pins_with_no_cap(self) -> None:
        # Eight active, all pinned, no cap: a cap proposed from here
        # could not collapse anything, and the tree at a cap of six is
        # byte-identical to the uncapped one.
        self._budgets("orientation_index_tokens = 200\nworkstreams_in_view = 0\n")
        self._active_workstreams(8)
        self._pin([f"stream-{day:02d}" for day in range(1, 9)])
        self.write("CLAUDE.local.md", "x" * 1200 + "\n")

        message = self._orientation_message()

        self.assertIn(
            "8 active, all listed in full (no cap), 8 pinned by the newest handoff;",
            message,
        )
        self.assertIn("workstreams_in_view is at its floor of 8 (8 pinned by", message)
        self.assertNotIn("Set workstreams_in_view", message)
        before = self._tree()
        self._budgets("orientation_index_tokens = 200\nworkstreams_in_view = 6\n")
        self.assertEqual(self._tree(), before)

    def test_token_budget_orientation_warn_levers_against_the_warn(self) -> None:
        # 601 bytes is 151 tokens: under the 200 ceiling, 51 over the
        # derived warn of 100, so the WARN sizes the way back under 100.
        self._budgets("orientation_index_tokens = 200\nworkstreams_in_view = 0\n")
        self._active_workstreams(12)
        self.write("CLAUDE.local.md", "x" * 600 + "\n")

        result = wiki_doctor.check_token_budgets(self.ctx)

        self.assertFalse(result.failed)
        self.assertTrue(result.warned)
        message = result.findings[0].message
        self.assertIn("Lever: 12 active, all listed in full (no cap);", message)
        self.assertIn("Set workstreams_in_view to 10 (about 62 tokens)", message)
        self.assertIn("archive 2 workstreams (about 74 tokens)", message)

    def test_token_budget_lever_says_when_build_index_cannot_run(self) -> None:
        # No wiki/events: build-index fails loud on a wiki outside the
        # kit's contract, and the finding still reports the overrun.
        self._budgets("orientation_index_tokens = 200\n")
        self.write("workstreams/stream-01.md", WORKSTREAM_OK)
        self.write("CLAUDE.local.md", "x" * 1200 + "\n")

        result = wiki_doctor.check_token_budgets(self.ctx)

        self.assertTrue(result.failed)
        message = result.findings[0].message
        self.assertIn("estimates 301 tokens", message)
        self.assertIn("Lever unavailable: build-index exited 1", message)

    def test_token_budget_other_surfaces_carry_no_lever(self) -> None:
        self.write("workstreams/huge.md", WORKSTREAM_OK + "x" * 20000 + "\n")

        result = wiki_doctor.check_token_budgets(self.ctx)

        self.assertTrue(result.failed)
        self.assertNotIn("Lever", result.findings[0].message)

    def test_token_budget_covers_nested_entity_pages(self) -> None:
        self.write("wiki/entities/people/alex.md", "# Alex\n" + "x" * 20000 + "\n")
        self.write("wiki/entities/widget.md", "# Widget\n")

        result = wiki_doctor.check_token_budgets(self.ctx)

        self.assertTrue(result.failed)
        self.assertIn("entity page", result.findings[0].message)
        self.assertIn("people/alex.md", result.findings[0].path or "")
        self.assertIn("2 surfaces checked", result.summary)

    def test_token_budget_silent_when_no_memory_index_exists(self) -> None:
        self.write("CLAUDE.local.md", "small\n")

        result = wiki_doctor.check_token_budgets(self.ctx)

        self.assertFalse(result.failed)
        self.assertFalse(result.warned)

    def test_board_check_skips_cleanly_when_absent(self) -> None:
        result = wiki_doctor.check_board(self.ctx)

        self.assertFalse(result.failed)
        self.assertIn("optional", result.summary)

    def test_board_check_detects_unowned_in_progress_card(self) -> None:
        self.write(
            "planning/board.md",
            "## In progress\n\n"
            "- [S1-doctor](cards/S1-doctor.md) - gates: S\n\n"
            "## Ready\n\n(empty)\n\n"
            "## Done\n\n(empty)\n",
        )
        self.write(
            "planning/cards/S1-doctor.md",
            "---\n"
            "id: S1-doctor\n"
            "status: in-progress\n"
            "owner: (unclaimed)\n"
            "---\n\n"
            "## Log (append-only; newest last; always append before stopping)\n\n"
            "- 2026-07-03: claimed.\n",
        )

        result = wiki_doctor.check_board(self.ctx)

        self.assertTrue(result.failed)
        self.assertIn("no owner", result.findings[0].message)

    def test_board_check_honors_boardkit_cards_dir(self) -> None:
        self.write("boardkit.toml", '[board]\ncards_dir = "docs/board/cards"\n')
        self.write(
            "docs/board/board.md",
            "## In progress\n\n"
            "- [K9-missing](cards/K9-missing.md) - gates: S\n\n"
            "## Ready\n\n(empty)\n\n"
            "## Done\n\n(empty)\n",
        )

        result = wiki_doctor.check_board(self.ctx)

        self.assertTrue(result.failed)
        self.assertIn("broken", result.findings[0].message)
        self.assertIn("docs/board/cards", str(result.findings[0].path))

    def test_board_check_boardkit_layout_passes_when_healthy(self) -> None:
        self.write("boardkit.toml", '[board]\ncards_dir = "docs/board/cards"\n')
        self.write(
            "docs/board/board.md",
            "## In progress\n\n"
            "- [K1-ok](cards/K1-ok.md) - gates: S\n\n"
            "## Ready\n\n(empty)\n\n"
            "## Done\n\n(empty)\n",
        )
        self.write(
            "docs/board/cards/K1-ok.md",
            "---\n"
            "id: K1-ok\n"
            "status: in-progress\n"
            "owner: kimi\n"
            "---\n\n"
            "## Log\n\n"
            "- 2026-08-24: claimed.\n",
        )

        result = wiki_doctor.check_board(self.ctx)

        self.assertFalse(result.failed)
        self.assertIn("1 in-progress", result.summary)

    def test_board_check_reports_unreadable_boardkit_toml(self) -> None:
        self.write("boardkit.toml", "[board\n")

        result = wiki_doctor.check_board(self.ctx)

        self.assertTrue(result.failed)
        self.assertIn("boardkit.toml", str(result.findings[0].path))

    # -- kit stamp -----------------------------------------------------------

    def stamp(self, version: int = 1, commit: str = "abc123") -> None:
        self.write(
            "wiki.toml",
            MINIMAL_CONFIG
            + f'\n[kit]\ncontract_version = {version}\ncommit = "{commit}"\n',
        )
        self._rebuild_ctx()

    def test_kit_stamp_absent_warns(self) -> None:
        result = wiki_doctor.check_kit_stamp(self.ctx)

        self.assertFalse(result.failed)
        self.assertTrue(result.warned)
        self.assertIn("no [kit] stamp", result.findings[0].message)

    def test_kit_stamp_unsupported_version_fails(self) -> None:
        self.stamp(version=99)

        result = wiki_doctor.check_kit_stamp(self.ctx)

        self.assertTrue(result.failed)
        self.assertIn("99", result.findings[0].message)

    def test_kit_stamp_commit_drift_warns(self) -> None:
        self.stamp(commit="0" * 40)

        result = wiki_doctor.check_kit_stamp(self.ctx)

        self.assertFalse(result.failed)
        self.assertTrue(result.warned)
        self.assertIn("stamped at", result.findings[0].message)

    def test_kit_stamp_clean_when_aligned(self) -> None:
        head = subprocess.run(
            ["git", "-C", str(KIT_ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.stamp(commit=head)

        result = wiki_doctor.check_kit_stamp(self.ctx)

        self.assertFalse(result.failed)
        self.assertFalse(result.warned)
        self.assertIn("contract v1", result.summary)

    def test_board_check_rejects_invalid_cards_dir(self) -> None:
        self.write("boardkit.toml", '[board]\ncards_dir = 3\n')
        result = wiki_doctor.check_board(self.ctx)
        self.assertTrue(result.failed)
        self.assertIn("cards_dir", result.findings[0].message)

        self.write("boardkit.toml", '[board]\ncards_dir = ""\n')
        result = wiki_doctor.check_board(self.ctx)
        self.assertTrue(result.failed)
        self.assertIn("cards_dir", result.findings[0].message)

    def test_board_check_rejects_non_table_board(self) -> None:
        self.write("boardkit.toml", 'board = "docs/board"\n')

        result = wiki_doctor.check_board(self.ctx)

        self.assertTrue(result.failed)
        self.assertIn("[board]", result.findings[0].message)

    def test_board_check_falls_back_when_boardkit_has_no_cards_dir(
        self,
    ) -> None:
        self.write("boardkit.toml", "[board]\n")
        self.write(
            "planning/board.md",
            "## In progress\n\n"
            "- [S1-gone](cards/S1-gone.md) - gates: S\n\n"
            "## Ready\n\n(empty)\n\n"
            "## Done\n\n(empty)\n",
        )

        result = wiki_doctor.check_board(self.ctx)

        self.assertTrue(result.failed)
        self.assertIn("broken", result.findings[0].message)
        self.assertIn("planning/cards", str(result.findings[0].path))

    # -- repo names --------------------------------------------------------

    def test_repo_names_404_fails_and_names_configured_companions(self) -> None:
        self.write("workstreams/bad-repo.md", WORKSTREAM_OK.replace(
            "repo: acme/widget", "repo: acme/nonexistent-repo"
        ))
        fake_result = subprocess.CompletedProcess(
            args=["gh", "api", "repos/acme/nonexistent-repo"],
            returncode=1,
            stdout="",
            stderr="HTTP 404: Not Found (https://api.github.com/graphql)",
        )

        with patch("subprocess.run", return_value=fake_result):
            result = wiki_doctor.check_repo_names(self.ctx)

        self.assertTrue(result.failed)
        self.assertIn("does not exist", result.findings[0].message)
        self.assertIn("acme/widget", result.findings[0].message)

    def test_repo_names_auth_error_warns(self) -> None:
        self.write("workstreams/auth-fail.md", WORKSTREAM_OK)
        fake_result = subprocess.CompletedProcess(
            args=["gh", "api", "repos/acme/widget"],
            returncode=1,
            stdout="",
            stderr="HTTP 401: Bad credentials",
        )

        with patch("subprocess.run", return_value=fake_result):
            result = wiki_doctor.check_repo_names(self.ctx)

        self.assertTrue(result.warned)
        self.assertFalse(result.failed)

    def test_repo_names_dedupes_repeated_repos(self) -> None:
        for name in ("stream-a", "stream-b"):
            self.write(f"workstreams/{name}.md", WORKSTREAM_OK)
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout='{"full_name": "acme/widget"}',
                stderr="",
            )

        with patch("subprocess.run", side_effect=fake_run):
            wiki_doctor.check_repo_names(self.ctx)

        self.assertEqual(len(calls), 1)

    # -- captures ----------------------------------------------------------

    def _write_capture(self, content: bytes) -> Path:
        captured = self.write("wiki/sources/note/2026/07/evt/source-note.md", "x")
        captured.write_bytes(content)
        digest = wiki_doctor.sha256_file(captured)
        self.write(
            "wiki/sources/note/2026/07/evt/manifest.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "event_id": "01a00000-0000-7000-8000-000000000000",
                    "event_path": "wiki/events/2026/07/x.json",
                    "captured_at_utc": "2026-07-03T00:00:00Z",
                    "captures": [
                        {
                            "kind": "note",
                            "source_path": "notes/source-note.md",
                            "captured_path": (
                                "wiki/sources/note/2026/07/evt/source-note.md"
                            ),
                            "sha256": digest,
                            "size_bytes": len(content),
                        }
                    ],
                }
            ),
        )
        return captured

    def test_capture_audit_passes_on_intact_capture(self) -> None:
        self._write_capture(b"captured content\n")

        result = wiki_doctor.check_captures(self.ctx)

        self.assertFalse(result.failed)

    def test_capture_audit_detects_tampered_capture(self) -> None:
        captured = self._write_capture(b"captured content\n")
        captured.write_bytes(b"tampered\n")

        result = wiki_doctor.check_captures(self.ctx)

        self.assertTrue(result.failed)
        self.assertTrue(
            any("mismatch" in finding.message for finding in result.findings)
        )

    def test_capture_audit_detects_missing_capture_file(self) -> None:
        captured = self._write_capture(b"captured content\n")
        captured.unlink()

        result = wiki_doctor.check_captures(self.ctx)

        self.assertTrue(result.failed)
        self.assertTrue(
            any("missing" in finding.message for finding in result.findings)
        )

    # -- install -----------------------------------------------------------

    def _init_git(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)

    def test_install_check_reports_missing_hook_and_rules(self) -> None:
        self._init_git()

        result = wiki_doctor.check_install(self.ctx)

        self.assertTrue(result.failed)
        messages = [finding.message for finding in result.findings]
        self.assertTrue(any("pre-commit hook" in message for message in messages))
        self.assertTrue(any("deny rule" in message for message in messages))

    def test_install_check_on_non_git_root_reports_finding_not_traceback(self) -> None:
        # No _init_git(): git_hooks_dir raises ConfigError, which must
        # surface as a finding rather than abort the doctor run.
        result = wiki_doctor.check_install(self.ctx)

        self.assertTrue(result.failed)
        self.assertTrue(
            any("not a git repository" in f.message for f in result.findings)
        )

    def test_install_check_passes_after_wrapper_and_rules(self) -> None:
        self._init_git()
        hooks = self.root / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            f"{wiki_doctor.HOOK_MARKER}\n"
            'OVERLAY="$MAIN_ROOT/wiki.local.toml"\n'
            'exec "$PYTHON" "$KIT_ROOT/scripts/pre-commit" "$@"\n'
        )
        hook.chmod(0o755)
        self.write("wiki.local.toml", f'[tools]\nkit = "{KIT_ROOT}"\n')
        rules = wiki_doctor.contract_deny_rules(self.ctx.config)
        self.write(
            ".claude/settings.json",
            json.dumps({"permissions": {"deny": rules}}),
        )
        self._rebuild_ctx()

        result = wiki_doctor.check_install(self.ctx)

        self.assertFalse(result.failed, [f.message for f in result.findings])

    def test_install_check_reports_missing_overlay_kit_path(self) -> None:
        self._init_git()
        hooks = self.root / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            f"{wiki_doctor.HOOK_MARKER}\n"
            'OVERLAY="$MAIN_ROOT/wiki.local.toml"\n'
            'exec "$PYTHON" "$KIT_ROOT/scripts/pre-commit" "$@"\n'
        )
        hook.chmod(0o755)
        rules = wiki_doctor.contract_deny_rules(self.ctx.config)
        self.write(
            ".claude/settings.json",
            json.dumps({"permissions": {"deny": rules}}),
        )

        result = wiki_doctor.check_install(self.ctx)

        self.assertTrue(result.failed)
        self.assertTrue(
            any("[tools] kit" in f.message for f in result.findings)
        )

    def test_install_check_warns_when_main_checkout_unfindable(self) -> None:
        """Exotic layout (separate git dir): tools read the root's own
        overlay, but the wrapper can never resolve it - the doctor
        warns rather than passing silently."""
        meta = Path(self._tmp.name) / "meta"
        meta.mkdir()
        subprocess.run(
            [
                "git",
                "init",
                "--separate-git-dir",
                str(meta / "wiki.git"),
                str(self.root),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        hooks_dir = Path(
            subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "--git-path", "hooks"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        if not hooks_dir.is_absolute():
            hooks_dir = self.root / hooks_dir
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            f"{wiki_doctor.HOOK_MARKER}\n"
            'OVERLAY="$MAIN_ROOT/wiki.local.toml"\n'
            'exec "$PYTHON" "$KIT_ROOT/scripts/pre-commit" "$@"\n'
        )
        hook.chmod(0o755)
        self.write("wiki.local.toml", f'[tools]\nkit = "{KIT_ROOT}"\n')
        rules = wiki_doctor.contract_deny_rules(self.ctx.config)
        self.write(
            ".claude/settings.json",
            json.dumps({"permissions": {"deny": rules}}),
        )
        self._rebuild_ctx()

        result = wiki_doctor.check_install(self.ctx)

        self.assertFalse(result.failed)
        self.assertTrue(result.warned)
        self.assertTrue(
            any(
                "cannot locate the main checkout" in f.message
                for f in result.findings
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
