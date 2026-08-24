#!/usr/bin/env python3
"""Dock a consumer repo to a wiki: install, complete, status.

A dock is the consumer's `.wiki/` directory: a trackable identity
manifest plus a machine-local overlay (the ratified contract is the
kit's docs/docking-spec.md). This CLI is the only writer of
consumer-side docks:

- install: write the manifest and overlay, apply the posture's ignore
  mechanics, and render the generated wiring (post-commit hook,
  opencode handoff plugin) for companions with an outbox subpath.
- complete: create or update the overlay's [dock].path for an existing
  manifest - the command the resolver's fail-loud message names.
- status: report what the resolver sees at a repo, read-only. Exit 1
  when a dock exists but cannot resolve (incomplete overlay, broken
  path, identity mismatch); an undocked repo reports cleanly.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from wiki_config import (  # noqa: E402
    DOCK_DIR_NAME,
    DOCK_MANIFEST_NAME,
    DOCK_OVERLAY_NAME,
    KIT_ROOT,
    POSTURES,
    Companion,
    ConfigError,
    dock_complete_command,
    git_hooks_dir,
    load_config,
    load_dock,
    verify_dock_identity,
)

KIT_SCRIPTS = KIT_ROOT / "scripts"
HANDOFF_PLUGIN_TEMPLATE = KIT_ROOT / "templates" / "handoff.ts.template"

# The ignore lines each posture applies: committed tracks the manifest
# and ignores only the overlay; gitignored and invisible ignore the
# whole dock (tracked .gitignore vs the per-clone exclude file).
# Generated wiring joins these lines for the untracked postures - the
# spec's exclusion set covers everything the install step writes.
IGNORE_LINES = {
    "committed": (f"{DOCK_DIR_NAME}/{DOCK_OVERLAY_NAME}",),
    "gitignored": (f"{DOCK_DIR_NAME}/",),
    "invisible": (f"{DOCK_DIR_NAME}/",),
}

PLUGIN_REPO_PATH = ".opencode/plugins/handoff.ts"
PLUGIN_MARKER = "OpenCode session.idle handoff plugin"

POST_COMMIT_MARKER = "# wiki-kit post-commit wrapper"


class DockError(Exception):
    pass


def note(message: str) -> None:
    print(f"  {message}")


def _require_wiki_root(wiki: Path) -> Path:
    root = wiki.expanduser().resolve()
    if not (root / "wiki.toml").is_file():
        raise DockError(f"--wiki {root} does not contain wiki.toml")
    return root


def manifest_text(wiki_name: str, companion: str) -> str:
    return (
        "# Dock identity manifest (docking spec): identity only, no\n"
        "# machine paths. The machine-local overlay lives in local.toml\n"
        "# beside this file and is never committed.\n"
        "[dock]\n"
        f"wiki = {json.dumps(wiki_name)}\n"
        f"companion = {json.dumps(companion)}\n"
    )


def overlay_text(wiki_root: Path) -> str:
    return (
        "# Machine-local dock overlay: never committed; the posture's\n"
        "# ignore mechanism covers it. [dock].path is the one allowlisted\n"
        "# key.\n"
        "[dock]\n"
        f"path = {json.dumps(str(wiki_root))}\n"
    )


def _check_manifest_slot(dock_dir: Path, wiki_name: str, companion: str) -> None:
    """The manifest is the dock's identity: a conflict with what is
    already docked here fails loud rather than re-pointing silently."""
    path = dock_dir / DOCK_MANIFEST_NAME
    if not path.exists():
        return
    current = path.read_text(encoding="utf-8")
    if current != manifest_text(wiki_name, companion):
        raise DockError(
            f"{path} already docks this repo to a different wiki or "
            f"companion; inspect it and resolve by hand before "
            f"reinstalling:\n{current}"
        )


def write_manifest(dock_dir: Path, wiki_name: str, companion: str) -> None:
    path = dock_dir / DOCK_MANIFEST_NAME
    _check_manifest_slot(dock_dir, wiki_name, companion)
    if path.exists():
        note("✓ manifest up to date")
        return
    dock_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest_text(wiki_name, companion), encoding="utf-8")
    note("✓ manifest written")


def write_overlay(dock_dir: Path, wiki_root: Path) -> None:
    """The overlay is machine-local: writing the path this machine
    resolves through is the command's purpose, create or update."""
    path = dock_dir / DOCK_OVERLAY_NAME
    content = overlay_text(wiki_root)
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current == content:
            note("✓ overlay up to date")
            return
        path.write_text(content, encoding="utf-8")
        note(f"✓ overlay re-pointed at {wiki_root}")
        return
    dock_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    note("✓ overlay written")


def _git_exclude_file(repo: Path) -> Path:
    """The repo's per-clone exclude file, resolved by git: in a linked
    worktree .git is a file, and git knows where the shared info/ dir
    lives."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DockError(
            f"{repo} is not a git repository: {result.stderr.strip()}"
        )
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else repo / path


def apply_posture(repo: Path, posture: str, wiring_written: bool) -> None:
    lines = list(IGNORE_LINES[posture])
    if wiring_written and posture != "committed":
        # Generated shims follow the posture (docking spec): tracked
        # when committed, covered by the same exclusion set otherwise.
        lines.append(PLUGIN_REPO_PATH)
    if posture == "invisible":
        path = _git_exclude_file(repo)
        label = "info/exclude"
    else:
        path = repo / ".gitignore"
        label = ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    have = existing.splitlines()
    missing = [line for line in lines if line not in have]
    if not missing:
        note(f"✓ {label} already covers the posture ({posture})")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    text = existing
    if text and not text.endswith("\n"):
        text += "\n"
    text += "\n".join(missing) + "\n"
    path.write_text(text, encoding="utf-8")
    note(f"✓ {label} gained: {', '.join(missing)} ({posture} posture)")


def post_commit_wrapper_text(wiki_root: Path, docs_subpath: str) -> str:
    """A generated wrapper, not a copy: it renders the env the kit's
    post-commit requires and exec's the kit's CURRENT script, so kit
    updates apply without a reinstall. .git/hooks is machine-local, so
    the baked paths are legal."""
    return (
        "#!/bin/sh\n"
        f"{POST_COMMIT_MARKER}\n"
        f"WIKI_KIT_ROOT={shlex.quote(str(KIT_ROOT))}\n"
        f"WIKI_ROOT={shlex.quote(str(wiki_root))}\n"
        f"WIKI_DOCS_SUBPATH={shlex.quote(docs_subpath)}\n"
        "export WIKI_KIT_ROOT WIKI_ROOT WIKI_DOCS_SUBPATH\n"
        f'exec {shlex.quote(str(KIT_SCRIPTS / "post-commit"))} "$@"\n'
    )


def _check_hook_slot(dest: Path) -> None:
    """A foreign hook is never clobbered: the conflict fails loud, and
    the preflight ordering means it fails before any write lands."""
    if dest.exists() and not dest.is_file():
        raise DockError(f"post-commit hook path is not a file: {dest}")
    if (dest.exists() or dest.is_symlink()) and dest.is_file():
        current = dest.read_text(encoding="utf-8")
        if POST_COMMIT_MARKER not in current:
            raise DockError(
                f"post-commit hook exists and is not a kit wrapper: "
                f"{dest}; resolve it by hand before reinstalling"
            )


def install_post_commit_hook(
    repo: Path, wiki_root: Path, docs_subpath: str
) -> None:
    dest = git_hooks_dir(repo) / "post-commit"
    _check_hook_slot(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    wrapper = post_commit_wrapper_text(wiki_root, docs_subpath)
    if dest.exists() and dest.read_text(encoding="utf-8") == wrapper:
        note("✓ post-commit hook wrapper up to date")
        return
    # An existing slot holds a kit wrapper from another wiki or kit
    # location (the slot check passed): rewrite.
    dest.write_text(wrapper, encoding="utf-8")
    dest.chmod(0o755)
    note("✓ post-commit hook wrapper installed")


def render_handoff_plugin(repo: Path, docs_subpath: str) -> None:
    dest = repo / PLUGIN_REPO_PATH
    content = HANDOFF_PLUGIN_TEMPLATE.read_text(encoding="utf-8").replace(
        "{{DOCS_SUBPATH}}", docs_subpath
    )
    if dest.exists():
        current = dest.read_text(encoding="utf-8")
        if current == content:
            note("✓ opencode handoff plugin up to date")
            return
        if PLUGIN_MARKER not in current:
            note(
                "✓ opencode handoff plugin exists and is not a kit "
                "render, left in place"
            )
            return
        # A kit render from another docs_subpath: rewrite so the plugin
        # and the hook wrapper never point at different outboxes.
        dest.write_text(content, encoding="utf-8")
        note("✓ opencode handoff plugin re-rendered")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    note("✓ opencode handoff plugin rendered")


def _resolve_posture(flag: str | None, companion: Companion) -> str:
    """The companion table is the posture's one home (docking spec): an
    explicit flag may agree with it or supply it, never contradict."""
    recorded = companion.posture
    if flag is not None and recorded is not None and flag != recorded:
        raise DockError(
            f"--posture {flag} contradicts the posture recorded for "
            f"companion {companion.name!r} in wiki.toml ({recorded!r}); "
            "the companion table is the posture's one home - change it "
            "there or omit the flag"
        )
    posture = flag or recorded
    if posture is None:
        raise DockError(
            f"no --posture given and companion {companion.name!r} "
            f"records none in wiki.toml; pass --posture "
            f"{'|'.join(POSTURES)} or set posture in the companion table"
        )
    return posture


def cmd_install(args: argparse.Namespace) -> int:
    wiki_root = _require_wiki_root(args.wiki)
    repo = args.repo.expanduser().resolve()
    hooks = git_hooks_dir(repo)  # raises ConfigError unless a git repo
    config = load_config(wiki_root)
    companion = config.companion(args.companion)
    posture = _resolve_posture(args.posture, companion)
    dock_dir = repo / DOCK_DIR_NAME
    wired = companion.docs_subpath is not None

    # Every conflict check runs before any write: a failed install
    # leaves nothing half-applied.
    _check_manifest_slot(dock_dir, config.name, companion.name)
    if wired:
        _check_hook_slot(hooks / "post-commit")

    write_manifest(dock_dir, config.name, companion.name)
    write_overlay(dock_dir, wiki_root)
    apply_posture(repo, posture, wiring_written=wired)
    if wired:
        install_post_commit_hook(repo, wiki_root, companion.docs_subpath)
        render_handoff_plugin(repo, companion.docs_subpath)
    else:
        note(
            "- companion has no docs_subpath; post-commit hook and "
            "opencode plugin skipped"
        )

    dock = load_dock(dock_dir)
    verify_dock_identity(dock, wiki_root)
    print(f"Docked {repo} to wiki {config.name!r} ({posture} posture).")
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    wiki_root = _require_wiki_root(args.wiki)
    repo = args.repo.expanduser().resolve()
    dock_dir = repo / DOCK_DIR_NAME
    if not (dock_dir / DOCK_MANIFEST_NAME).is_file():
        raise DockError(
            f"no dock manifest at {dock_dir / DOCK_MANIFEST_NAME}; "
            "complete only writes the overlay for an existing manifest - "
            "run install with an explicit --posture to create the dock"
        )
    dock = load_dock(dock_dir)
    verify_dock_identity(dock, wiki_root)
    write_overlay(dock_dir, wiki_root)
    print(f"Dock at {dock_dir} now resolves to {wiki_root}.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve()
    dock_dir = repo / DOCK_DIR_NAME
    if not (dock_dir / DOCK_MANIFEST_NAME).is_file():
        print(f"{repo}: no dock (no {DOCK_DIR_NAME}/{DOCK_MANIFEST_NAME})")
        return 0
    dock = load_dock(dock_dir)
    print(f"dock:      {dock_dir}")
    print(f"wiki:      {dock.wiki_name}")
    print(f"companion: {dock.companion}")
    if dock.wiki_path is None:
        print("overlay:   MISSING (incomplete dock)")
        print(f"complete:  {dock_complete_command(dock)}")
        return 1
    print(f"overlay:   {dock.wiki_path}")
    root = dock.wiki_path.resolve()
    if not (root / "wiki.toml").is_file():
        print(f"resolves:  BROKEN - {root} does not contain wiki.toml")
        return 1
    verify_dock_identity(dock, root)
    print(f"resolves:  {root} (identity chain verified)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser(
        "install", help="dock a consumer repo to a wiki"
    )
    install_parser.add_argument(
        "--wiki", type=Path, required=True, help="wiki repo root"
    )
    install_parser.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="consumer repo to dock (default: cwd)",
    )
    install_parser.add_argument("--companion", required=True)
    install_parser.add_argument(
        "--posture",
        choices=POSTURES,
        default=None,
        help="dock posture; defaults to the companion table's recorded "
        "posture and must not contradict it",
    )
    install_parser.set_defaults(func=cmd_install)

    complete_parser = subparsers.add_parser(
        "complete",
        help="create or update the overlay for an existing manifest",
    )
    complete_parser.add_argument(
        "--wiki", type=Path, required=True, help="wiki repo root"
    )
    complete_parser.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="consumer repo carrying the manifest (default: cwd)",
    )
    complete_parser.set_defaults(func=cmd_complete)

    status_parser = subparsers.add_parser(
        "status", help="report what the resolver sees at a repo"
    )
    status_parser.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="repo to inspect (default: cwd)",
    )
    status_parser.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, DockError) as exc:
        print(f"wiki-dock: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
