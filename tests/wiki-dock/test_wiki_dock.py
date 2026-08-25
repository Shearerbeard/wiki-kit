#!/usr/bin/env python3
"""Tests for scripts/wiki_dock.py (install / complete / status).

The end-to-end spine: install docks a fixture consumer, the resolver
then resolves from inside it with no --wiki; a manifest-only dock
fails the resolver loud, the fail-loud message names `complete`, and
running complete makes resolution succeed.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
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
        for target in (".agents/skills", ".claude/skills"):
            for name in SKILL_NAMES:
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

    def test_changed_kit_render_updates_on_reinstall(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.install_with_skills(repo, wiki, "--skills-dir", ".agents/skills")
        dest = repo / ".agents" / "skills" / "garden" / "SKILL.md"
        dest.write_text(dest.read_text() + "\nstale line\n")
        code, out = self.install_with_skills(
            repo, wiki, "--skills-dir", ".agents/skills"
        )
        self.assertEqual(code, 0)
        self.assertIn("skill 'garden' re-rendered", out)
        self.assertNotIn("stale line", dest.read_text())

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
