#!/usr/bin/env python3
"""Tests for the .wiki/ dock resolver in scripts/wiki_config.py.

Coverage per the K3 card's Stage 1.1 gate: the full docking-spec
resolution order (flag, WIKI_DOCK, bounded walk-up, common-dir
worktree fallback, legacy channel), incomplete-dock fall-through with
identity binding, the linked-worktree fixture, and the
case-insensitive-FS path comparison (the epoch/Epoch trap).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

KIT_ROOT = Path(__file__).resolve().parents[2]


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "scripts.wiki_config", KIT_ROOT / "scripts" / "wiki_config.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load scripts/wiki_config.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scripts.wiki_config"] = mod
    spec.loader.exec_module(mod)
    return mod


wiki_config = _load_module()

ConfigError = wiki_config.ConfigError
DOCK_ENV = wiki_config.DOCK_ENV
LEGACY_WIKI_ENV = wiki_config.LEGACY_WIKI_ENV

WIKI_TOML = """\
[wiki]
name = "{name}"

[companions.{companion}]
github = "acme/{companion}"
"""

MANIFEST_TOML = """\
[dock]
wiki = "{name}"
companion = "{companion}"
"""


class ResolverCase(unittest.TestCase):
    """Fixture harness: real git repos under a resolved temp dir, with
    cwd and the resolver's env channels restored after every test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # macOS temp dirs are symlinked (/var -> /private/var); the
        # resolver and git both report physical paths.
        self.base = Path(self._tmp.name).resolve()
        self.addCleanup(os.chdir, Path.cwd())
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(DOCK_ENV, None)
        os.environ.pop(LEGACY_WIKI_ENV, None)

    def git(self, repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def init_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        self.git(path, "init")
        return path

    def commit_all(self, repo: Path) -> None:
        self.git(repo, "add", "-A")
        self.git(
            repo,
            "-c",
            "user.email=fixture@example.com",
            "-c",
            "user.name=Fixture",
            "commit",
            "-m",
            "fixture",
        )

    def make_wiki(
        self, name: str = "acme-notes", companion: str = "widget"
    ) -> Path:
        root = self.init_repo(self.base / name)
        (root / "wiki.toml").write_text(
            WIKI_TOML.format(name=name, companion=companion),
            encoding="utf-8",
        )
        self.commit_all(root)
        return root

    def make_dock(
        self,
        repo: Path,
        name: str = "acme-notes",
        companion: str = "widget",
        wiki_path: Path | None = None,
    ) -> Path:
        dock = repo / ".wiki"
        dock.mkdir(exist_ok=True)
        (dock / "manifest.toml").write_text(
            MANIFEST_TOML.format(name=name, companion=companion),
            encoding="utf-8",
        )
        if wiki_path is not None:
            self.write_overlay(dock, wiki_path)
        return dock

    def write_overlay(self, dock: Path, wiki_path: Path) -> None:
        (dock / "local.toml").write_text(
            f'[dock]\npath = "{wiki_path}"\n', encoding="utf-8"
        )


class LoadDockTest(ResolverCase):
    def test_manifest_only_is_an_incomplete_dock(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        dock_dir = self.make_dock(repo)
        dock = wiki_config.load_dock(dock_dir)
        self.assertEqual(dock.wiki_name, "acme-notes")
        self.assertEqual(dock.companion, "widget")
        self.assertIsNone(dock.wiki_path)

    def test_overlay_completes_the_dock(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        wiki = self.make_wiki()
        dock_dir = self.make_dock(repo, wiki_path=wiki)
        dock = wiki_config.load_dock(dock_dir)
        self.assertEqual(dock.wiki_path, wiki)

    def test_missing_manifest_is_a_malformed_dock(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        (repo / ".wiki").mkdir()
        with self.assertRaises(ConfigError) as caught:
            wiki_config.load_dock(repo / ".wiki")
        self.assertIn("malformed", str(caught.exception))

    def test_unknown_manifest_key_fails_loud(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        dock_dir = self.make_dock(repo)
        (dock_dir / "manifest.toml").write_text(
            '[dock]\nwiki = "acme-notes"\ncompanion = "widget"\n'
            'path = "/elsewhere"\n',
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            wiki_config.load_dock(dock_dir)

    def test_manifest_requires_both_identity_keys(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        dock_dir = repo / ".wiki"
        dock_dir.mkdir()
        (dock_dir / "manifest.toml").write_text(
            '[dock]\nwiki = "acme-notes"\n', encoding="utf-8"
        )
        with self.assertRaises(ConfigError):
            wiki_config.load_dock(dock_dir)

    def test_overlay_is_allowlisted_to_dock_path(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        dock_dir = self.make_dock(repo)
        (dock_dir / "local.toml").write_text(
            '[dock]\npath = "/elsewhere"\nmode = "rw"\n', encoding="utf-8"
        )
        with self.assertRaises(ConfigError):
            wiki_config.load_dock(dock_dir)

    def test_overlay_path_must_be_absolute(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        dock_dir = self.make_dock(repo)
        (dock_dir / "local.toml").write_text(
            '[dock]\npath = "relative/wiki"\n', encoding="utf-8"
        )
        with self.assertRaises(ConfigError) as caught:
            wiki_config.load_dock(dock_dir)
        self.assertIn("absolute", str(caught.exception))

    def test_unresolvable_user_path_is_a_config_error(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        dock_dir = self.make_dock(repo)
        (dock_dir / "local.toml").write_text(
            '[dock]\npath = "~definitely_not_a_user_zz9/wiki"\n',
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            wiki_config.load_dock(dock_dir)

    def test_complete_command_names_the_dock_repo(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        dock = wiki_config.load_dock(self.make_dock(repo))
        command = wiki_config.dock_complete_command(dock)
        self.assertIn("wiki-dock.py", command)
        self.assertIn("complete", command)
        self.assertIn(str(repo), command)


class VerifyDockIdentityTest(ResolverCase):
    def test_matching_identity_passes(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        dock = wiki_config.load_dock(self.make_dock(repo, wiki_path=wiki))
        wiki_config.verify_dock_identity(dock, wiki)

    def test_name_mismatch_names_both_values(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        dock = wiki_config.load_dock(
            self.make_dock(repo, name="other-notes", wiki_path=wiki)
        )
        with self.assertRaises(ConfigError) as caught:
            wiki_config.verify_dock_identity(dock, wiki)
        message = str(caught.exception)
        self.assertIn("other-notes", message)
        self.assertIn("acme-notes", message)

    def test_unknown_companion_fails_loud(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        dock = wiki_config.load_dock(
            self.make_dock(repo, companion="gadget", wiki_path=wiki)
        )
        with self.assertRaises(ConfigError) as caught:
            wiki_config.verify_dock_identity(dock, wiki)
        self.assertIn("gadget", str(caught.exception))

    def test_wiki_name_must_be_stated_explicitly(self) -> None:
        """A wiki.toml that omits [wiki].name fails the identity chain
        even when the directory name would match by default."""
        wiki = self.init_repo(self.base / "acme-notes")
        (wiki / "wiki.toml").write_text(
            '[companions.widget]\ngithub = "acme/widget"\n', encoding="utf-8"
        )
        self.commit_all(wiki)
        repo = self.init_repo(self.base / "consumer")
        dock = wiki_config.load_dock(self.make_dock(repo, wiki_path=wiki))
        with self.assertRaises(ConfigError) as caught:
            wiki_config.verify_dock_identity(dock, wiki)
        self.assertIn("[wiki].name", str(caught.exception))


class SamePathTest(ResolverCase):
    def test_string_equality(self) -> None:
        path = self.base / "a"
        self.assertTrue(wiki_config._same_path(path, path))

    def test_symlink_alias_matches_via_inode(self) -> None:
        target = self.base / "target"
        target.mkdir()
        alias = self.base / "alias"
        alias.symlink_to(target)
        self.assertNotEqual(alias, target)
        self.assertTrue(wiki_config._same_path(alias, target))

    def test_distinct_directories_do_not_match(self) -> None:
        first = self.base / "first"
        second = self.base / "second"
        first.mkdir()
        second.mkdir()
        self.assertFalse(wiki_config._same_path(first, second))

    def test_case_variant_matches_via_inode(self) -> None:
        """The epoch/Epoch trap: string-unequal paths naming one
        directory must compare equal on a case-insensitive filesystem."""
        lower = self.base / "epoch"
        lower.mkdir()
        upper = lower.with_name("Epoch")
        if not upper.exists():
            self.skipTest("case-sensitive filesystem")
        self.assertNotEqual(lower, upper)
        self.assertTrue(wiki_config._same_path(lower, upper))


class ResolveWikiRootTest(ResolverCase):
    def test_flag_resolves_the_wiki_root_itself(self) -> None:
        wiki = self.make_wiki()
        self.assertEqual(wiki_config.resolve_wiki_root(wiki), wiki)

    def test_flag_requires_wiki_toml(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        with self.assertRaises(ConfigError):
            wiki_config.resolve_wiki_root(repo)

    def test_env_accepts_the_dock_dir_form(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        dock_dir = self.make_dock(repo, wiki_path=wiki)
        os.environ[DOCK_ENV] = str(dock_dir)
        self.assertEqual(wiki_config.resolve_wiki_root(), wiki)

    def test_env_accepts_the_parent_of_dock_form(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.make_dock(repo, wiki_path=wiki)
        os.environ[DOCK_ENV] = str(repo)
        self.assertEqual(wiki_config.resolve_wiki_root(), wiki)

    def test_env_non_dock_fails_loud(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        os.environ[DOCK_ENV] = str(repo)
        with self.assertRaises(ConfigError) as caught:
            wiki_config.resolve_wiki_root()
        self.assertIn(DOCK_ENV, str(caught.exception))

    def test_env_wins_over_walkup(self) -> None:
        env_wiki = self.make_wiki(name="env-notes", companion="widget")
        walkup_wiki = self.make_wiki()
        env_repo = self.init_repo(self.base / "env-consumer")
        self.make_dock(
            env_repo, name="env-notes", wiki_path=env_wiki
        )
        os.environ[DOCK_ENV] = str(env_repo)
        walkup_repo = self.init_repo(self.base / "walkup-consumer")
        self.make_dock(walkup_repo, wiki_path=walkup_wiki)
        os.chdir(walkup_repo)
        self.assertEqual(wiki_config.resolve_wiki_root(), env_wiki)

    def test_walkup_inside_the_wiki_repo(self) -> None:
        wiki = self.make_wiki()
        nested = wiki / "wiki" / "events"
        nested.mkdir(parents=True)
        os.chdir(nested)
        self.assertEqual(wiki_config.resolve_wiki_root(), wiki)

    def test_walkup_resolves_a_consumer_dock(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.make_dock(repo, wiki_path=wiki)
        nested = repo / "docs" / "deep"
        nested.mkdir(parents=True)
        os.chdir(nested)
        self.assertEqual(wiki_config.resolve_wiki_root(), wiki)

    def test_walkup_never_leaves_the_repo(self) -> None:
        """A dock above the git toplevel is invisible to the walk-up:
        an undocked repo must fail loud, never resolve an unrelated
        ancestor's .wiki/."""
        wiki = self.make_wiki()
        outer = self.base / "outer"
        outer.mkdir()
        self.make_dock(outer, wiki_path=wiki)
        repo = self.init_repo(outer / "repo")
        os.chdir(repo)
        with self.assertRaises(ConfigError) as caught:
            wiki_config.resolve_wiki_root()
        self.assertIn("dock this repo first", str(caught.exception))

    def test_walkup_stops_at_a_malformed_nearer_dock(self) -> None:
        """A .wiki/ dir without its manifest is a malformed dock: the
        walk-up must fail there, never pass it for an outer dock."""
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.make_dock(repo, wiki_path=wiki)
        nested = repo / "sub"
        nested.mkdir()
        (nested / ".wiki").mkdir()  # malformed: no manifest
        os.chdir(nested)
        with self.assertRaises(ConfigError) as caught:
            wiki_config.resolve_wiki_root()
        self.assertIn("malformed", str(caught.exception))

    def test_incomplete_dock_fails_loud_naming_the_remedy(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        self.make_dock(repo)
        os.chdir(repo)
        with self.assertRaises(ConfigError) as caught:
            wiki_config.resolve_wiki_root()
        message = str(caught.exception)
        self.assertIn("local.toml", message)
        self.assertIn("wiki-dock.py", message)
        self.assertIn(str(repo), message)

    def test_incomplete_env_dock_binds_identity_on_match(self) -> None:
        wiki = self.make_wiki()
        env_repo = self.init_repo(self.base / "env-consumer")
        self.make_dock(env_repo)  # incomplete: no overlay
        os.environ[DOCK_ENV] = str(env_repo)
        walkup_repo = self.init_repo(self.base / "walkup-consumer")
        self.make_dock(walkup_repo, wiki_path=wiki)
        os.chdir(walkup_repo)
        self.assertEqual(wiki_config.resolve_wiki_root(), wiki)

    def test_incomplete_env_dock_binds_identity_on_mismatch(self) -> None:
        wiki = self.make_wiki()
        env_repo = self.init_repo(self.base / "env-consumer")
        self.make_dock(env_repo, name="other-notes")
        os.environ[DOCK_ENV] = str(env_repo)
        walkup_repo = self.init_repo(self.base / "walkup-consumer")
        self.make_dock(walkup_repo, wiki_path=wiki)
        os.chdir(walkup_repo)
        with self.assertRaises(ConfigError) as caught:
            wiki_config.resolve_wiki_root()
        message = str(caught.exception)
        self.assertIn("other-notes", message)
        self.assertIn("acme-notes", message)

    def test_linked_worktree_resolves_the_main_checkout_dock(self) -> None:
        """Committed posture: the tracked manifest checks out into the
        linked worktree, the gitignored overlay does not. The worktree's
        incomplete dock falls through to the common-dir fallback, which
        reaches the main checkout's complete dock."""
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        self.make_dock(repo)
        (repo / ".gitignore").write_text(".wiki/local.toml\n")
        self.commit_all(repo)  # manifest tracked, overlay not yet written
        self.write_overlay(repo / ".wiki", wiki)
        worktree = self.base / "linked"
        self.git(repo, "worktree", "add", str(worktree))
        self.assertFalse((worktree / ".wiki" / "local.toml").exists())
        os.chdir(worktree)
        self.assertEqual(wiki_config.resolve_wiki_root(), wiki)

    def test_worktree_fallback_with_a_separate_git_dir(self) -> None:
        """A repo created with --separate-git-dir does not record its
        main checkout anywhere git can read back (worktree list reports
        the git dir itself). The fallback must fail loud naming the
        incomplete dock, never dock to the git dir's parent."""
        wiki = self.make_wiki()
        repo = self.base / "consumer"
        (self.base / "meta").mkdir()
        subprocess.run(
            [
                "git",
                "init",
                "--separate-git-dir",
                str(self.base / "meta" / "consumer.git"),
                str(repo),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.make_dock(repo)
        self.commit_all(repo)
        self.write_overlay(repo / ".wiki", wiki)
        # The pathological layout: a complete dock for the SAME wiki
        # sitting at the git dir's parent. A common-dir-parent fallback
        # would resolve through it; that directory is not a checkout.
        self.make_dock(self.base / "meta", wiki_path=wiki)
        worktree = self.base / "linked"
        self.git(repo, "worktree", "add", str(worktree))
        os.chdir(worktree)
        with self.assertRaises(ConfigError) as caught:
            wiki_config.resolve_wiki_root()
        message = str(caught.exception)
        self.assertIn("carries no local.toml overlay", message)

    def test_legacy_env_channel_is_honored(self) -> None:
        wiki = self.make_wiki()
        repo = self.init_repo(self.base / "consumer")
        os.chdir(repo)
        os.environ[LEGACY_WIKI_ENV] = str(wiki)
        self.assertEqual(wiki_config.resolve_wiki_root(), wiki)

    def test_legacy_orientation_symlink_channel_is_honored(self) -> None:
        wiki = self.make_wiki()
        (wiki / "CLAUDE.local.md").write_text("# orientation\n")
        repo = self.init_repo(self.base / "consumer")
        (repo / "CLAUDE.local.md").symlink_to(wiki / "CLAUDE.local.md")
        os.chdir(repo)
        self.assertEqual(wiki_config.resolve_wiki_root(), wiki)

    def test_outside_any_repo_fails_loud(self) -> None:
        os.chdir(self.base)
        with self.assertRaises(ConfigError) as caught:
            wiki_config.resolve_wiki_root()
        self.assertIn("not inside a git repository", str(caught.exception))

    def test_complete_dock_pointing_at_a_non_wiki_fails_loud(self) -> None:
        not_a_wiki = self.init_repo(self.base / "other")
        repo = self.init_repo(self.base / "consumer")
        self.make_dock(repo, wiki_path=not_a_wiki)
        os.chdir(repo)
        with self.assertRaises(ConfigError) as caught:
            wiki_config.resolve_wiki_root()
        message = str(caught.exception)
        self.assertIn("local.toml", message)
        self.assertIn("wiki-dock.py", message)

    def test_stale_legacy_env_is_named_in_the_terminal_error(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        os.chdir(repo)
        os.environ[LEGACY_WIKI_ENV] = str(self.base / "nowhere")
        with self.assertRaises(ConfigError) as caught:
            wiki_config.resolve_wiki_root()
        message = str(caught.exception)
        self.assertIn("dock this repo first", message)
        self.assertIn(str(self.base / "nowhere"), message)

    def test_dangling_orientation_symlink_is_named_in_the_error(self) -> None:
        target_dir = self.base / "orientation-target"
        target_dir.mkdir()
        (target_dir / "CLAUDE.local.md").write_text("# stale\n")
        repo = self.init_repo(self.base / "consumer")
        (repo / "CLAUDE.local.md").symlink_to(
            target_dir / "CLAUDE.local.md"
        )
        os.chdir(repo)
        with self.assertRaises(ConfigError) as caught:
            wiki_config.resolve_wiki_root()
        message = str(caught.exception)
        self.assertIn("orientation symlink", message)
        self.assertIn(str(target_dir), message)

    def test_missing_symlink_target_is_not_trusted(self) -> None:
        """A symlink whose target file was deleted is dangling even
        when the target's parent is a wiki root; the legacy channel
        must not resolve through it."""
        wiki = self.make_wiki()
        # No CLAUDE.local.md at the wiki root: the link dangles.
        repo = self.init_repo(self.base / "consumer")
        (repo / "CLAUDE.local.md").symlink_to(wiki / "CLAUDE.local.md")
        os.chdir(repo)
        with self.assertRaises(ConfigError) as caught:
            wiki_config.resolve_wiki_root()
        message = str(caught.exception)
        self.assertIn("dock this repo first", message)
        self.assertIn("dangling", message)

    def test_undocked_repo_fails_loud(self) -> None:
        repo = self.init_repo(self.base / "consumer")
        os.chdir(repo)
        with self.assertRaises(ConfigError) as caught:
            wiki_config.resolve_wiki_root()
        self.assertIn("dock this repo first", str(caught.exception))


FULL_WIKI_TOML = """\
[wiki]
name = "acme-notes"

[contract]
protected = ["wiki/log.md"]
external_allow = []
skills = ["garden"]
global_skills = []

[companions.widget]
github = "acme/widget"
"""


class OverlayConfigTest(ResolverCase):
    """The machine-local wiki overlay: the [memory].projects_root key
    (G14) and the allowlist's table-type guards."""

    def make_full_wiki(self) -> Path:
        root = self.init_repo(self.base / "acme-notes")
        (root / "wiki.toml").write_text(FULL_WIKI_TOML, encoding="utf-8")
        return root

    def write_wiki_overlay(self, root: Path, text: str) -> None:
        (root / "wiki.local.toml").write_text(text, encoding="utf-8")

    def test_projects_root_expands_and_round_trips_json(self) -> None:
        root = self.make_full_wiki()
        self.write_wiki_overlay(root, '[memory]\nprojects_root = "~/mem"\n')
        config = wiki_config.load_config(root)
        self.assertEqual(config.projects_root, Path.home() / "mem")
        rendered = wiki_config._config_as_json(config)
        self.assertEqual(
            rendered["projects_root"], str(Path.home() / "mem")
        )

    def test_projects_root_absent_is_none(self) -> None:
        root = self.make_full_wiki()
        config = wiki_config.load_config(root)
        self.assertIsNone(config.projects_root)
        rendered = wiki_config._config_as_json(config)
        self.assertIsNone(rendered["projects_root"])

    def test_projects_root_empty_fails_loud(self) -> None:
        root = self.make_full_wiki()
        self.write_wiki_overlay(root, '[memory]\nprojects_root = ""\n')
        with self.assertRaises(ConfigError):
            wiki_config.load_config(root)

    def test_scalar_companions_table_fails_loud(self) -> None:
        root = self.make_full_wiki()
        self.write_wiki_overlay(root, 'companions = "widget"\n')
        with self.assertRaises(ConfigError) as caught:
            wiki_config.load_config(root)
        self.assertIn("[companions]", str(caught.exception))

    def test_scalar_memory_table_fails_loud(self) -> None:
        root = self.make_full_wiki()
        self.write_wiki_overlay(root, 'memory = "projects"\n')
        with self.assertRaises(ConfigError) as caught:
            wiki_config.load_config(root)
        self.assertIn("[memory]", str(caught.exception))

    def test_companion_path_must_be_absolute(self) -> None:
        root = self.make_full_wiki()
        self.write_wiki_overlay(
            root, '[companions.widget]\npath = "relative/widget"\n'
        )
        with self.assertRaises(ConfigError) as caught:
            wiki_config.load_config(root)
        self.assertIn("absolute", str(caught.exception))

    def test_projects_root_must_be_absolute(self) -> None:
        root = self.make_full_wiki()
        self.write_wiki_overlay(
            root, '[memory]\nprojects_root = "relative/mem"\n'
        )
        with self.assertRaises(ConfigError) as caught:
            wiki_config.load_config(root)
        self.assertIn("absolute", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
