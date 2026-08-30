#!/usr/bin/env python3
"""Tests for scripts/wiki_dock.py (install / complete / status).

The end-to-end spine: install docks a fixture consumer, the resolver
then resolves from inside it with no --wiki; a manifest-only dock
fails the resolver loud, the fail-loud message names `complete`, and
running complete makes resolution succeed.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

KIT_ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


wiki_config = _load_module(
    "scripts.wiki_config", KIT_ROOT / "scripts" / "wiki_config.py"
)
wiki_dock = _load_module(
    "scripts.wiki_dock", KIT_ROOT / "scripts" / "wiki_dock.py"
)

DOCK_ENV = wiki_config.DOCK_ENV
LEGACY_WIKI_ENV = wiki_config.LEGACY_WIKI_ENV

WIKI_TOML = """\
[wiki]
name = "acme-notes"
default_companion = "widget"

[contract]
protected = ["CLAUDE.local.md", "wiki/log.md"]
external_allow = []
skills = ["garden", "handoff", "morning", "session-feedback"]
global_skills = []

[companions.widget]
github = "acme/widget"
docs_subpath = "docs/internal"

[companions.plain]
github = "acme/plain"

[companions.recorded]
github = "acme/recorded"
posture = "committed"
"""


def run_cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = wiki_dock.main(list(argv))
    return code, out.getvalue()


class DockCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.addCleanup(os.chdir, Path.cwd())
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(DOCK_ENV, None)
        os.environ.pop(LEGACY_WIKI_ENV, None)

    def git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def init_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        self.git(path, "init")
        return path

    def make_wiki(self) -> Path:
        root = self.init_repo(self.base / "acme-notes")
        (root / "wiki.toml").write_text(WIKI_TOML, encoding="utf-8")
        return root


class InstallTest(DockCase):
    def install(self, repo: Path, wiki: Path, *extra: str) -> tuple[int, str]:
        return run_cli(
            "install",
            "--wiki",
            str(wiki),
            "--repo",
            str(repo),
            "--companion",
            "widget",
            *extra,
        )

    def test_committed_posture_writes_the_full_dock(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = self.install(repo, wiki, "--posture", "committed")
        self.assertEqual(code, 0)

        manifest = (repo / ".wiki" / "manifest.toml").read_text()
        self.assertIn('wiki = "acme-notes"', manifest)
        self.assertIn('companion = "widget"', manifest)
        overlay = (repo / ".wiki" / "local.toml").read_text()
        self.assertIn(f'path = "{wiki}"', overlay)
        gitignore = (repo / ".gitignore").read_text()
        self.assertIn(".wiki/local.toml", gitignore.splitlines())
        self.assertIn(".wiki/rendered-skills.json", gitignore.splitlines())
        self.assertIn(".wiki/probes/", gitignore.splitlines())

        hook = repo / ".git" / "hooks" / "post-commit"
        text = hook.read_text()
        self.assertIn(f"WIKI_KIT_ROOT={KIT_ROOT}", text)
        self.assertIn(f"WIKI_ROOT={wiki}", text)
        self.assertIn("WIKI_DOCS_SUBPATH=docs/internal", text)
        self.assertTrue(os.access(hook, os.X_OK))

        plugin = repo / ".opencode" / "plugins" / "handoff.ts"
        rendered = plugin.read_text()
        self.assertIn("docs/internal", rendered)
        self.assertNotIn("{{DOCS_SUBPATH}}", rendered)

    def test_gitignored_posture_covers_everything_install_writes(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = self.install(repo, wiki, "--posture", "gitignored")
        self.assertEqual(code, 0)
        lines = (repo / ".gitignore").read_text().splitlines()
        self.assertIn(".wiki/", lines)
        self.assertIn(".opencode/plugins/handoff.ts", lines)
        # Only the tracked .gitignore itself may surface in git status.
        status = self.git(repo, "status", "--porcelain")
        self.assertEqual(status, "?? .gitignore\n")

    def test_invisible_posture_leaves_git_status_clean(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = self.install(repo, wiki, "--posture", "invisible")
        self.assertEqual(code, 0)
        exclude = (repo / ".git" / "info" / "exclude").read_text()
        self.assertIn(".wiki/", exclude.splitlines())
        self.assertIn(".opencode/plugins/handoff.ts", exclude.splitlines())
        self.assertFalse((repo / ".gitignore").exists())
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")

    def test_companion_without_outbox_skips_generated_wiring(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, out = run_cli(
            "install",
            "--wiki",
            str(wiki),
            "--repo",
            str(repo),
            "--companion",
            "plain",
            "--posture",
            "committed",
        )
        self.assertEqual(code, 0)
        self.assertIn("docs_subpath", out)
        self.assertTrue((repo / ".wiki" / "manifest.toml").exists())
        self.assertFalse((repo / ".git" / "hooks" / "post-commit").exists())
        self.assertFalse((repo / ".opencode").exists())

    def test_unknown_companion_writes_nothing(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = run_cli(
            "install",
            "--wiki",
            str(wiki),
            "--repo",
            str(repo),
            "--companion",
            "gadget",
            "--posture",
            "committed",
        )
        self.assertEqual(code, 1)
        self.assertFalse((repo / ".wiki").exists())

    def test_conflicting_manifest_fails_loud(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = self.install(repo, wiki, "--posture", "committed")
        self.assertEqual(code, 0)
        manifest = repo / ".wiki" / "manifest.toml"
        manifest.write_text(
            '[dock]\nwiki = "other-notes"\ncompanion = "widget"\n'
        )
        code, _ = self.install(repo, wiki, "--posture", "committed")
        self.assertEqual(code, 1)
        self.assertIn("other-notes", manifest.read_text())

    def test_foreign_post_commit_hook_blocks_install_before_any_write(
        self,
    ) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        hook = repo / ".git" / "hooks" / "post-commit"
        hook.write_text("#!/bin/sh\necho foreign\n")
        code, _ = self.install(repo, wiki, "--posture", "committed")
        self.assertEqual(code, 1)
        self.assertEqual(hook.read_text(), "#!/bin/sh\necho foreign\n")
        # The preflight ordering: nothing half-applied.
        self.assertFalse((repo / ".wiki").exists())
        self.assertFalse((repo / ".opencode").exists())
        self.assertFalse((repo / ".gitignore").exists())

    def test_posture_flag_must_not_contradict_the_recorded_posture(
        self,
    ) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = run_cli(
            "install",
            "--wiki",
            str(wiki),
            "--repo",
            str(repo),
            "--companion",
            "recorded",
            "--posture",
            "gitignored",
        )
        self.assertEqual(code, 1)
        self.assertFalse((repo / ".wiki").exists())

    def test_posture_defaults_from_the_companion_table(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = run_cli(
            "install",
            "--wiki",
            str(wiki),
            "--repo",
            str(repo),
            "--companion",
            "recorded",
        )
        self.assertEqual(code, 0)
        gitignore = (repo / ".gitignore").read_text()
        self.assertIn(".wiki/local.toml", gitignore.splitlines())

    def test_no_flag_and_no_recorded_posture_fails_loud(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = run_cli(
            "install",
            "--wiki",
            str(wiki),
            "--repo",
            str(repo),
            "--companion",
            "widget",
        )
        self.assertEqual(code, 1)
        self.assertFalse((repo / ".wiki").exists())

    def test_plugin_re_renders_when_the_kit_render_changes(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.install(repo, wiki, "--posture", "committed")
        plugin = repo / ".opencode" / "plugins" / "handoff.ts"
        plugin.write_text(
            plugin.read_text().replace("docs/internal", "docs/old")
        )
        code, out = self.install(repo, wiki, "--posture", "committed")
        self.assertEqual(code, 0)
        self.assertIn("re-rendered", out)
        self.assertIn("docs/internal", plugin.read_text())

    def test_reinstall_is_idempotent(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.install(repo, wiki, "--posture", "committed")
        code, out = self.install(repo, wiki, "--posture", "committed")
        self.assertEqual(code, 0)
        self.assertIn("up to date", out)

    def test_wiki_flag_requires_wiki_toml(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        not_a_wiki = self.init_repo(self.base / "other")
        code, _ = self.install(repo, not_a_wiki, "--posture", "committed")
        self.assertEqual(code, 1)
        self.assertFalse((repo / ".wiki").exists())

    def test_install_makes_the_resolver_see_the_wiki(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = self.install(repo, wiki, "--posture", "committed")
        self.assertEqual(code, 0)
        os.chdir(repo)
        self.assertEqual(wiki_config.resolve_wiki_root(), wiki)


SKILL_NAMES = ("garden", "handoff", "morning", "session-feedback")


class OnboardingWiringTest(DockCase):
    """The cold-start surface install produces: .wiki/orientation.md,
    the AGENTS.md marker block, and the CLAUDE.md shim."""

    def install(self, repo: Path, wiki: Path, *extra: str) -> tuple[int, str]:
        return run_cli(
            "install",
            "--wiki",
            str(wiki),
            "--repo",
            str(repo),
            "--companion",
            "widget",
            "--posture",
            "committed",
            *extra,
        )

    def test_absent_files_are_created(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = self.install(repo, wiki, "--skills-dir", ".agents/skills")
        self.assertEqual(code, 0)

        orientation = (repo / ".wiki" / "orientation.md").read_text()
        self.assertIn("acme-notes", orientation)
        self.assertIn("widget", orientation)
        self.assertIn(str(wiki), orientation)
        self.assertIn(".agents/skills/", orientation)
        self.assertIn(f"uv run --project {KIT_ROOT}", orientation)
        self.assertNotIn("{{", orientation)

        agents = (repo / "AGENTS.md").read_text()
        self.assertIn(wiki_dock.DOCK_BLOCK_START, agents)
        self.assertIn(wiki_dock.DOCK_BLOCK_END, agents)
        self.assertIn("acme-notes", agents)
        self.assertIn(".wiki/orientation.md", agents)

        claude = (repo / "CLAUDE.md").read_text()
        self.assertIn("# CLAUDE.md", claude)
        self.assertIn(wiki_dock.CLAUDE_SHIM_LINE, claude)

    def test_existing_content_outside_the_block_is_preserved_byte_exact(
        self,
    ) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        agents_before = "# Consumer AGENTS\n\nHand-written rules.\n"
        (repo / "AGENTS.md").write_text(agents_before)
        claude_before = "# Notes\n\nConsumer-authored claude file.\n"
        (repo / "CLAUDE.md").write_text(claude_before)
        code, _ = self.install(repo, wiki)
        self.assertEqual(code, 0)

        agents = (repo / "AGENTS.md").read_text()
        self.assertTrue(agents.startswith(agents_before))
        self.assertIn(wiki_dock.DOCK_BLOCK_START, agents)
        # The block sits after a separating blank line.
        self.assertIn(
            agents_before + "\n" + wiki_dock.DOCK_BLOCK_START, agents
        )
        claude = (repo / "CLAUDE.md").read_text()
        self.assertTrue(claude.startswith(claude_before))
        self.assertIn(wiki_dock.DOCK_BLOCK_START, claude)

    def test_reinstall_is_byte_identical(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.install(repo, wiki, "--skills-dir", ".agents/skills")
        watched = (
            repo / ".wiki" / "orientation.md",
            repo / "AGENTS.md",
            repo / "CLAUDE.md",
        )
        before = {path: path.read_bytes() for path in watched}
        code, out = self.install(repo, wiki, "--skills-dir", ".agents/skills")
        self.assertEqual(code, 0)
        self.assertIn("orientation up to date", out)
        self.assertIn("dock block up to date", out)
        self.assertEqual(before, {path: path.read_bytes() for path in watched})

    def test_changed_wiki_name_replaces_only_the_block(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        path = repo / "AGENTS.md"
        original = (
            "above\n"
            f"{wiki_dock.DOCK_BLOCK_START}\nold wiki pointer\n"
            f"{wiki_dock.DOCK_BLOCK_END}\nbelow\n"
        )
        path.write_text(original)
        wiki_dock.update_marked_block(
            path, wiki_dock.dock_block_text("new-notes"), "AGENTS.md"
        )
        text = path.read_text()
        self.assertTrue(text.startswith("above\n"))
        self.assertTrue(text.endswith("below\n"))
        self.assertIn("new-notes", text)
        self.assertNotIn("old wiki pointer", text)

    def test_foreign_edit_inside_the_markers_is_replaced(self) -> None:
        """The marked region is kit-owned: consumer edits inside it do
        not survive a reinstall; edits outside it do, byte-exact."""
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.install(repo, wiki)
        path = repo / "AGENTS.md"
        text = path.read_text()
        edited = text.replace("acme-notes", "consumer scribble")
        path.write_text("prepended line\n" + edited)
        code, _ = self.install(repo, wiki)
        self.assertEqual(code, 0)
        result = path.read_text()
        self.assertIn("acme-notes", result)
        self.assertNotIn("consumer scribble", result)
        self.assertTrue(result.startswith("prepended line\n"))

    def test_orientation_is_ignored_in_committed_posture(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = self.install(repo, wiki)
        self.assertEqual(code, 0)
        gitignore = (repo / ".gitignore").read_text().splitlines()
        self.assertIn(".wiki/orientation.md", gitignore)

    def test_untracked_postures_exclude_the_entry_shims(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = run_cli(
            "install",
            "--wiki",
            str(wiki),
            "--repo",
            str(repo),
            "--companion",
            "widget",
            "--posture",
            "invisible",
        )
        self.assertEqual(code, 0)
        exclude = (repo / ".git" / "info" / "exclude").read_text()
        self.assertIn("AGENTS.md", exclude.splitlines())
        self.assertIn("CLAUDE.md", exclude.splitlines())
        self.assertTrue((repo / "AGENTS.md").exists())
        self.assertTrue((repo / "CLAUDE.md").exists())
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")

    def test_unknown_template_token_fails_before_any_write(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        bad = self.base / "bad-orientation.md.template"
        bad.write_text("wiki {{WIKI_NAME}} at {{WIKI_ROT}}\n")
        with mock.patch.object(wiki_dock, "ORIENTATION_TEMPLATE", bad):
            code, _ = self.install(repo, wiki)
        self.assertEqual(code, 1)
        self.assertFalse((repo / ".wiki").exists())
        self.assertFalse((repo / "AGENTS.md").exists())

    def test_claude_md_already_pointing_at_agents_is_left_alone(
        self,
    ) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        existing = "# CLAUDE.md\n\nProject notes.\n@AGENTS.md\n"
        (repo / "CLAUDE.md").write_text(existing)
        code, out = self.install(repo, wiki)
        self.assertEqual(code, 0)
        self.assertIn("already points at AGENTS.md", out)
        self.assertEqual((repo / "CLAUDE.md").read_text(), existing)

    def test_claude_md_mentioning_agents_in_prose_gains_the_block(
        self,
    ) -> None:
        """A mention is not a pointer: claude-code follows @AGENTS.md
        imports and the kit shim, not prose."""
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        existing = "# CLAUDE.md\n\nDo not read AGENTS.md; it is stale.\n"
        (repo / "CLAUDE.md").write_text(existing)
        code, out = self.install(repo, wiki)
        self.assertEqual(code, 0)
        self.assertIn("CLAUDE.md gained the wiki-kit dock block", out)
        text = (repo / "CLAUDE.md").read_text()
        self.assertTrue(text.startswith(existing))
        self.assertIn(wiki_dock.DOCK_BLOCK_START, text)

    def test_malformed_markers_fail_before_any_write(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        (repo / "AGENTS.md").write_text(
            f"{wiki_dock.DOCK_BLOCK_START}\nno end marker\n"
        )
        code, _ = self.install(repo, wiki)
        self.assertEqual(code, 1)
        self.assertFalse((repo / ".wiki").exists())
        self.assertFalse((repo / ".gitignore").exists())
        self.assertFalse((repo / "CLAUDE.md").exists())

    def test_markers_quoted_in_a_fence_are_documentation(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        quoted = (
            "# Notes\n\nThe kit's markers look like this:\n\n```\n"
            f"{wiki_dock.DOCK_BLOCK_START}\nexample\n"
            f"{wiki_dock.DOCK_BLOCK_END}\n```\n"
        )
        (repo / "AGENTS.md").write_text(quoted)
        code, out = self.install(repo, wiki)
        self.assertEqual(code, 0)
        self.assertIn("AGENTS.md gained the wiki-kit dock block", out)
        text = (repo / "AGENTS.md").read_text()
        self.assertTrue(text.startswith(quoted))
        self.assertEqual(text.count(wiki_dock.DOCK_BLOCK_START), 2)
        code, out = self.install(repo, wiki)
        self.assertEqual(code, 0)
        self.assertIn("AGENTS.md dock block up to date", out)
        self.assertEqual((repo / "AGENTS.md").read_text(), text)

    def test_crlf_file_keeps_its_line_endings(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        existing = "# Agents\r\n\r\nWindows-authored notes.\r\n"
        with (repo / "AGENTS.md").open("w", newline="") as handle:
            handle.write(existing)
        code, _ = self.install(repo, wiki)
        self.assertEqual(code, 0)
        with (repo / "AGENTS.md").open(newline="") as handle:
            text = handle.read()
        self.assertTrue(text.startswith(existing))
        self.assertNotIn("\n", text.replace("\r\n", ""))
        code, out = self.install(repo, wiki)
        self.assertEqual(code, 0)
        self.assertIn("AGENTS.md dock block up to date", out)

    def test_brace_text_in_a_value_renders_literally(self) -> None:
        template = self.base / "t.md.template"
        template.write_text("root {{WIKI_ROOT}} kit {{KIT_ROOT}}\n")
        rendered = wiki_dock.render_template(
            template, {"WIKI_ROOT": "/w/{{KIT_ROOT}}", "KIT_ROOT": "/k"}
        )
        self.assertEqual(rendered, "root /w/{{KIT_ROOT}} kit /k\n")

    def test_complete_with_a_template_bug_leaves_the_overlay_alone(
        self,
    ) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        dock = repo / ".wiki"
        dock.mkdir()
        (dock / "manifest.toml").write_text(
            '[dock]\nwiki = "acme-notes"\ncompanion = "widget"\n',
            encoding="utf-8",
        )
        stale = '[dock]\npath = "/stale/acme-notes"\n'
        (dock / "local.toml").write_text(stale, encoding="utf-8")
        bad = self.base / "bad-orientation.md.template"
        bad.write_text("wiki {{WIKI_NAME}} at {{WIKI_ROT}}\n")
        with mock.patch.object(wiki_dock, "ORIENTATION_TEMPLATE", bad):
            code, _ = run_cli(
                "complete", "--wiki", str(wiki), "--repo", str(repo)
            )
        self.assertEqual(code, 1)
        self.assertEqual((dock / "local.toml").read_text(), stale)
        self.assertFalse((dock / "orientation.md").exists())

    def test_complete_renders_the_orientation(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        dock = repo / ".wiki"
        dock.mkdir()
        (dock / "manifest.toml").write_text(
            '[dock]\nwiki = "acme-notes"\ncompanion = "widget"\n',
            encoding="utf-8",
        )
        code, _ = run_cli(
            "complete", "--wiki", str(wiki), "--repo", str(repo)
        )
        self.assertEqual(code, 0)
        orientation = (dock / "orientation.md").read_text()
        self.assertIn("acme-notes", orientation)
        self.assertIn(str(wiki), orientation)
        self.assertIn("none rendered", orientation)

    def test_complete_orientation_names_the_recorded_skill_dirs(
        self,
    ) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.install(repo, wiki, "--skills-dir", ".agents/skills")
        (repo / ".wiki" / "orientation.md").unlink()
        code, _ = run_cli(
            "complete", "--wiki", str(wiki), "--repo", str(repo)
        )
        self.assertEqual(code, 0)
        orientation = (repo / ".wiki" / "orientation.md").read_text()
        self.assertIn(".agents/skills/", orientation)


class SkillRenderTest(DockCase):
    def install_with_skills(
        self, repo: Path, wiki: Path, *extra: str
    ) -> tuple[int, str]:
        return run_cli(
            "install",
            "--wiki",
            str(wiki),
            "--repo",
            str(repo),
            "--companion",
            "widget",
            "--posture",
            "committed",
            *extra,
        )

    def rendered(self, repo: Path, target: str, name: str) -> str:
        return (repo / target / name / "SKILL.md").read_text()

    def test_renders_all_four_skills_into_every_chosen_dir(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = self.install_with_skills(
            repo,
            wiki,
            "--skills-dir",
            ".agents/skills",
            "--skills-dir",
            ".claude/skills",
        )
        self.assertEqual(code, 0)
        contracted = wiki_config.load_config(wiki).contract.skills
        self.assertEqual(sorted(contracted), sorted(SKILL_NAMES))
        for target in (".agents/skills", ".claude/skills"):
            for name in contracted:
                text = self.rendered(repo, target, name)
                self.assertIn(f"name: {name}", text)
                # Fold shell line continuations before matching.
                normalized = " ".join(
                    text.replace("\\\n", " ").split()
                )
                self.assertIn(
                    f"uv run --project {KIT_ROOT} {KIT_ROOT}/scripts/",
                    normalized,
                )
                self.assertNotIn("{{", text)
                self.assertNotIn("~/workspace/", text)

    def test_kit_root_renders_from_the_overlay_tools_key(self) -> None:
        wiki = self.make_wiki()
        (wiki / "wiki.local.toml").write_text(
            '[tools]\nkit = "/opt/custom-kit"\n', encoding="utf-8"
        )
        repo = self.init_repo(self.base / "consumer")
        code, _ = self.install_with_skills(
            repo, wiki, "--skills-dir", ".agents/skills"
        )
        self.assertEqual(code, 0)
        text = self.rendered(repo, ".agents/skills", "morning")
        normalized = " ".join(text.replace("\\\n", " ").split())
        self.assertIn(
            "uv run --project /opt/custom-kit "
            "/opt/custom-kit/scripts/wiki-event.py",
            normalized,
        )

    def test_machine_global_target_is_refused_before_any_write(
        self,
    ) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        for target in ("~/.agents/skills", "/tmp/absolute-skills"):
            code, _ = self.install_with_skills(
                repo, wiki, "--skills-dir", target
            )
            self.assertEqual(code, 1, target)
        self.assertFalse((repo / ".wiki").exists())

    def test_target_escaping_the_repo_is_refused(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = self.install_with_skills(
            repo, wiki, "--skills-dir", "../outside"
        )
        self.assertEqual(code, 1)
        self.assertFalse((repo / ".wiki").exists())
        self.assertFalse((self.base / "outside").exists())

    def test_reinstall_over_an_existing_render_is_idempotent(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.install_with_skills(repo, wiki, "--skills-dir", ".agents/skills")
        code, out = self.install_with_skills(
            repo, wiki, "--skills-dir", ".agents/skills"
        )
        self.assertEqual(code, 0)
        self.assertIn("skill 'garden' up to date", out)

    def test_hand_edited_render_survives_reinstall_untouched(self) -> None:
        """Digest-based provenance: a render the consumer edited is no
        longer a pristine kit render, so reinstall leaves it alone."""
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.install_with_skills(repo, wiki, "--skills-dir", ".agents/skills")
        dest = repo / ".agents" / "skills" / "garden" / "SKILL.md"
        edited = dest.read_text() + "\nconsumer note\n"
        dest.write_text(edited)
        code, out = self.install_with_skills(
            repo, wiki, "--skills-dir", ".agents/skills"
        )
        self.assertEqual(code, 0)
        self.assertIn("left in place", out)
        self.assertEqual(dest.read_text(), edited)

    def test_pristine_older_render_updates_on_reinstall(self) -> None:
        """A byte-pristine render of older content (the dock's provenance
        manifest still vouches for it) is kit-owned and re-renders."""
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        dest_dir = repo / ".agents" / "skills" / "garden"
        dest_dir.mkdir(parents=True)
        old = "---\nname: garden\n---\nold render\n"
        (dest_dir / "SKILL.md").write_text(old)
        digest = hashlib.sha256(old.encode("utf-8")).hexdigest()
        dock = repo / ".wiki"
        dock.mkdir()
        (dock / "rendered-skills.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "renders": {".agents/skills/garden/SKILL.md": digest},
                }
            )
            + "\n"
        )
        code, out = self.install_with_skills(
            repo, wiki, "--skills-dir", ".agents/skills"
        )
        self.assertEqual(code, 0)
        self.assertIn("skill 'garden' re-rendered", out)
        rendered = (dest_dir / "SKILL.md").read_text()
        self.assertNotEqual(rendered, old)
        self.assertIn("uv run --project", rendered)

    def test_fabricated_provenance_artifact_is_never_trusted(self) -> None:
        """Provenance lives only in the dock's rendered-skills.json. A
        foreign file carrying a fabricated consumer-side digest (the
        retired sidecar scheme) has no manifest entry and is never
        overwritten."""
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        dest_dir = repo / ".agents" / "skills" / "garden"
        dest_dir.mkdir(parents=True)
        foreign = (
            "---\nname: garden\n# Rendered from the wiki-kit template\n"
            "---\nforged content\n"
        )
        (dest_dir / "SKILL.md").write_text(foreign)
        forged = hashlib.sha256(foreign.encode("utf-8")).hexdigest()
        (dest_dir / ".wiki-kit-sha256").write_text(forged + "\n")
        code, out = self.install_with_skills(
            repo, wiki, "--skills-dir", ".agents/skills"
        )
        self.assertEqual(code, 0)
        self.assertIn("no provenance entry", out)
        self.assertIn("left in place", out)
        self.assertEqual((dest_dir / "SKILL.md").read_text(), foreign)

    def test_missing_manifest_entry_on_older_render_is_loud(self) -> None:
        """A rendered-looking skill with no valid manifest entry gets a
        prominent note naming the recovery path, and is left in place."""
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.install_with_skills(repo, wiki, "--skills-dir", ".agents/skills")
        (repo / ".wiki" / "rendered-skills.json").unlink()
        dest = repo / ".agents" / "skills" / "garden" / "SKILL.md"
        dest.write_text(dest.read_text() + "\nolder state\n")
        code, out = self.install_with_skills(
            repo, wiki, "--skills-dir", ".agents/skills"
        )
        self.assertEqual(code, 0)
        self.assertIn("no provenance entry", out)
        self.assertIn("rerun wiki-dock install", out)
        self.assertIn("older state", dest.read_text())

    def test_symlinked_provenance_manifest_is_refused(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        dock = repo / ".wiki"
        dock.mkdir()
        outside = self.base / "hostage.json"
        outside.write_text("{}\n")
        (dock / "rendered-skills.json").symlink_to(outside)
        code, _ = self.install_with_skills(
            repo, wiki, "--skills-dir", ".agents/skills"
        )
        self.assertEqual(code, 1)
        self.assertEqual(outside.read_text(), "{}\n")
        self.assertFalse((repo / ".agents").exists())

    def test_symlinked_skill_dir_is_refused_before_any_write(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        outside = self.base / "elsewhere"
        outside.mkdir()
        skills_link = repo / ".agents" / "skills"
        skills_link.parent.mkdir()
        skills_link.symlink_to(outside)
        code, _ = self.install_with_skills(
            repo, wiki, "--skills-dir", ".agents/skills"
        )
        self.assertEqual(code, 1)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((repo / ".wiki").exists())

    def test_symlinked_skill_file_is_refused_and_untouched(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.install_with_skills(repo, wiki, "--skills-dir", ".agents/skills")
        dest = repo / ".agents" / "skills" / "garden" / "SKILL.md"
        outside = self.base / "hostage.md"
        outside.write_text("do not touch\n")
        dest.unlink()
        dest.symlink_to(outside)
        code, _ = self.install_with_skills(
            repo, wiki, "--skills-dir", ".agents/skills"
        )
        self.assertEqual(code, 1)
        self.assertEqual(outside.read_text(), "do not touch\n")

    def test_unrenderable_template_fails_loud_in_preflight(self) -> None:
        """A leftover placeholder in ANY case/charset (here a lowercase
        token) is a template bug: preflight catches it before the dock
        wiring or any skill write lands."""
        templates = self.base / "bad-templates"
        for name in SKILL_NAMES:
            skill_dir = templates / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {name}\n---\nbody\n"
            )
        bad = templates / "garden" / "SKILL.md"
        bad.write_text(bad.read_text() + "run {{kit_root}}/scripts/x.py\n")
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        with mock.patch.object(wiki_dock, "SKILLS_TEMPLATE_DIR", templates):
            code, _ = self.install_with_skills(
                repo, wiki, "--skills-dir", ".agents/skills"
            )
        self.assertEqual(code, 1)
        self.assertFalse((repo / ".wiki").exists())
        self.assertFalse((repo / ".agents").exists())

    def test_foreign_same_name_skill_is_left_in_place(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        dest = repo / ".agents" / "skills" / "garden"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("---\nname: garden\n---\nmine\n")
        code, out = self.install_with_skills(
            repo, wiki, "--skills-dir", ".agents/skills"
        )
        self.assertEqual(code, 0)
        self.assertIn("left in place", out)
        self.assertEqual(
            (dest / "SKILL.md").read_text(), "---\nname: garden\n---\nmine\n"
        )

    def ignored(self, repo: Path, path: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", path],
            capture_output=True,
        )
        return result.returncode == 0

    def test_untracked_postures_exclude_the_rendered_skills(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = run_cli(
            "install",
            "--wiki",
            str(wiki),
            "--repo",
            str(repo),
            "--companion",
            "widget",
            "--posture",
            "invisible",
            "--skills-dir",
            ".agents/skills",
        )
        self.assertEqual(code, 0)
        exclude = (repo / ".git" / "info" / "exclude").read_text()
        self.assertIn(".agents/skills/", exclude.splitlines())
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")

    def test_committed_posture_excludes_the_rendered_skills(self) -> None:
        """Rendered skills are generated wiring in EVERY posture: they
        embed the machine-local kit path, so committed posture excludes
        them and tracks only the dock manifest."""
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = self.install_with_skills(
            repo, wiki, "--skills-dir", ".agents/skills"
        )
        self.assertEqual(code, 0)
        gitignore = (repo / ".gitignore").read_text().splitlines()
        self.assertIn(".agents/skills/", gitignore)
        self.assertTrue(self.ignored(repo, ".agents/skills/garden/SKILL.md"))
        self.assertTrue(self.ignored(repo, ".wiki/rendered-skills.json"))
        self.assertTrue(self.ignored(repo, ".wiki/local.toml"))
        # Probe transcripts quote the orientation's machine paths.
        self.assertTrue(self.ignored(repo, ".wiki/probes/claude-code-1.txt"))
        self.assertFalse(self.ignored(repo, ".wiki/manifest.toml"))

    def test_fresh_clone_rerenders_skills_without_stale_refusal(
        self,
    ) -> None:
        """The second-clone flow: a committed-posture clone carries the
        tracked dock manifest but neither the renders nor the provenance
        manifest; install re-renders per clone with no stale-file
        refusal."""
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = self.install_with_skills(
            repo, wiki, "--skills-dir", ".agents/skills"
        )
        self.assertEqual(code, 0)
        self.git(repo, "add", "-A")
        self.git(
            repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "dock",
        )
        tracked = self.git(repo, "ls-files")
        self.assertIn(".wiki/manifest.toml", tracked)
        self.assertNotIn(".agents", tracked)
        self.assertNotIn("rendered-skills.json", tracked)

        clone = self.base / "clone"
        self.git(self.base, "clone", str(repo), str(clone))
        self.assertFalse((clone / ".agents").exists())
        self.assertFalse((clone / ".wiki" / "rendered-skills.json").exists())
        code, out = self.install_with_skills(
            clone, wiki, "--skills-dir", ".agents/skills"
        )
        self.assertEqual(code, 0)
        self.assertNotIn("no provenance entry", out)
        self.assertIn("skill 'garden' rendered", out)
        rendered = (
            clone / ".agents" / "skills" / "garden" / "SKILL.md"
        ).read_text()
        self.assertIn("uv run --project", rendered)
        self.assertTrue((clone / ".wiki" / "rendered-skills.json").exists())
        self.assertEqual(self.git(clone, "status", "--porcelain"), "")


class CompleteTest(DockCase):
    def manifest_only_dock(self, repo: Path, wiki_name: str = "acme-notes") -> None:
        dock = repo / ".wiki"
        dock.mkdir()
        (dock / "manifest.toml").write_text(
            f'[dock]\nwiki = "{wiki_name}"\ncompanion = "widget"\n',
            encoding="utf-8",
        )

    def test_complete_fixes_the_incomplete_dock_end_to_end(self) -> None:
        """The resolver's fail-loud flow: incomplete dock fails naming
        the complete command; running it makes resolution succeed."""
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.manifest_only_dock(repo)
        os.chdir(repo)

        with self.assertRaises(wiki_config.ConfigError) as caught:
            wiki_config.resolve_wiki_root()
        message = str(caught.exception)
        self.assertIn("wiki-dock.py", message)
        self.assertIn("complete", message)

        code, _ = run_cli(
            "complete", "--wiki", str(wiki), "--repo", str(repo)
        )
        self.assertEqual(code, 0)
        self.assertEqual(wiki_config.resolve_wiki_root(), wiki)

    def test_complete_requires_an_existing_manifest(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        code, _ = run_cli(
            "complete", "--wiki", str(wiki), "--repo", str(repo)
        )
        self.assertEqual(code, 1)
        self.assertFalse((repo / ".wiki" / "local.toml").exists())

    def test_complete_reverifies_identity_before_writing(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.manifest_only_dock(repo, wiki_name="other-notes")
        code, _ = run_cli(
            "complete", "--wiki", str(wiki), "--repo", str(repo)
        )
        self.assertEqual(code, 1)
        self.assertFalse((repo / ".wiki" / "local.toml").exists())


class StatusTest(DockCase):
    def test_no_dock_reports_cleanly(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        code, out = run_cli("status", "--repo", str(repo))
        self.assertEqual(code, 0)
        self.assertIn("no dock", out)

    def test_incomplete_dock_reports_the_remedy(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        dock = repo / ".wiki"
        dock.mkdir()
        (dock / "manifest.toml").write_text(
            '[dock]\nwiki = "acme-notes"\ncompanion = "widget"\n',
            encoding="utf-8",
        )
        code, out = run_cli("status", "--repo", str(repo))
        self.assertEqual(code, 1)
        self.assertIn("MISSING", out)
        self.assertIn("complete", out)

    def test_complete_dock_verifies_the_identity_chain(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        run_cli(
            "install",
            "--wiki",
            str(wiki),
            "--repo",
            str(repo),
            "--companion",
            "widget",
            "--posture",
            "committed",
        )
        code, out = run_cli("status", "--repo", str(repo))
        self.assertEqual(code, 0)
        self.assertIn("identity chain verified", out)

    def test_broken_overlay_path_reports_broken(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        dock = repo / ".wiki"
        dock.mkdir()
        (dock / "manifest.toml").write_text(
            '[dock]\nwiki = "acme-notes"\ncompanion = "widget"\n',
            encoding="utf-8",
        )
        (dock / "local.toml").write_text(
            '[dock]\npath = "/nonexistent/wiki"\n', encoding="utf-8"
        )
        code, out = run_cli("status", "--repo", str(repo))
        self.assertEqual(code, 1)
        self.assertIn("BROKEN", out)


if __name__ == "__main__":
    unittest.main()
