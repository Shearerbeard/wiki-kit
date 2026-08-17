#!/usr/bin/env python3
"""Install the kit's machinery hooks and seed files into a wiki repo.

The kit repo holds machinery; this installer wires a TARGET wiki repo
(blank or already carrying content) to it:

- init: git repo, `wiki.toml` (kit defaults, name from the directory),
  `.gitignore` coverage for the machine-local files, the content
  skeleton, an empty quarantine ledger, the pending and log projections,
  and - when the repo has no commits yet - an initial commit of exactly
  the files the installer wrote.
- pre-commit hook: symlinked to the kit's `scripts/pre-commit` (the
  mechanical provenance layer recon 03 found the old installer never
  installed).
- deny rules: derived from the deployment's own `[contract]` in
  `wiki.toml` - the single contract source the doctor and install-smoke
  also read - and merged into the wiki repo's `.claude/settings.json`.
- scheduler units: rendered from the kit's templates on macOS (skipped
  elsewhere; `--no-scheduler` skips explicitly).
- orientation skeleton: an empty-state `CLAUDE.local.md` rendered
  through the real renderer, so carry-forward works from the first
  session (the blank-repo boot floor, charter decision 4).

Init is NON-DESTRUCTIVE around pre-existing content (charter decision
4's tweak): a repo that already carries docs directories or an Obsidian
vault is installed around, never overwritten; every existing path is
left in place and reported.

Idempotent: a second run reports every step as up to date and changes
nothing.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path

KIT_SCRIPTS = Path(__file__).resolve().parent
KIT_ROOT = KIT_SCRIPTS.parent
if str(KIT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(KIT_SCRIPTS))

from wiki_config import (  # noqa: E402
    CONFIG_FILE_NAME,
    DEFAULT_CONTRACT,
    OVERLAY_FILE_NAME,
    ConfigError,
    WikiConfig,
    load_config,
)

ORIENTATION_TEMPLATE = KIT_ROOT / "templates" / "orientation-quickstart.md"
GITIGNORE_LINES = ("CLAUDE.local.md", OVERLAY_FILE_NAME)
SKELETON_DIRS = ("wiki/events", "wiki/sources", "wiki/entities", "workstreams")
EMPTY_QUARANTINE = {
    "schema_version": 1,
    "note": (
        "Events excluded from projections: known-corrupt store entries "
        "that pre-commit immutability forbids deleting. Each names its "
        "correcting event; loaders must verify corrected_by exists in "
        "the store."
    ),
    "quarantined": [],
}


class InstallError(Exception):
    pass


def note(message: str) -> None:
    print(f"  {message}")


def run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise InstallError(
            f"{' '.join(cmd)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def kit_cli(script: str, *args: str) -> None:
    run([sys.executable, str(KIT_SCRIPTS / script), *args])


def ensure_git_repo(target: Path) -> None:
    if (target / ".git").exists():
        note("✓ git repo already present")
        return
    run(["git", "init", "-q"], cwd=target)
    note("✓ git repo initialized")


def head_exists(target: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--verify", "-q", "HEAD"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def default_wiki_toml(name: str) -> str:
    def toml_list(values: list[str]) -> str:
        inner = ", ".join(json.dumps(value) for value in values)
        return f"[{inner}]"

    lines = [
        "# Wiki deployment config. Semantic, machine-independent values only;",
        f"# machine-local facts live in the gitignored {OVERLAY_FILE_NAME}",
        "# overlay beside this file. Schema: the kit's",
        "# docs/wiki-toml-schema.md.",
        "",
        "[wiki]",
        f'name = "{name}"',
        "",
        "# Consumer repos dock here as [companions.<name>] tables; the",
        "# overlay carries each companion's machine path.",
        "",
        "[contract]",
        "# The single deny-rule and skill contract source. The installer,",
        "# the doctor, and install-smoke all read THIS table.",
        f"protected = {toml_list(DEFAULT_CONTRACT['protected'])}",
        f"external_allow = {toml_list(DEFAULT_CONTRACT['external_allow'])}",
        f"skills = {toml_list(DEFAULT_CONTRACT['skills'])}",
        "# Adoption-only; the pre-adoption installer ignores this key and",
        "# installs project-scoped regardless.",
        f"global_skills = {toml_list(DEFAULT_CONTRACT['global_skills'])}",
        "",
        "[schedule]",
        'night = "03:00"',
        'morning = "08:30"',
        'garden_reminder = "16:00"',
        "",
        "[night]",
        'report_dir = "reports/night"',
        'commit_prefix = "night:"',
    ]
    return "\n".join(lines) + "\n"


def seed_file(path: Path, content: str, label: str, written: list[Path]) -> None:
    """Non-destructive write: an existing path is left in place, whatever
    it holds (decision-4 tweak: install around pre-existing content)."""
    if path.exists():
        note(f"✓ {label} exists, left in place")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    written.append(path)
    note(f"✓ {label} written")


def ensure_gitignore(target: Path, written: list[Path]) -> None:
    path = target / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    missing = [line for line in GITIGNORE_LINES if line not in lines]
    if not missing:
        note("✓ .gitignore already covers the machine-local files")
        return
    text = existing
    if text and not text.endswith("\n"):
        text += "\n"
    text += "\n".join(missing) + "\n"
    path.write_text(text, encoding="utf-8")
    if path not in written:
        written.append(path)
    note(f"✓ .gitignore gained: {', '.join(missing)}")


def ensure_skeleton(target: Path, written: list[Path]) -> None:
    for rel in SKELETON_DIRS:
        directory = target / rel
        keep = directory / ".gitkeep"
        if directory.is_dir() and any(directory.iterdir()):
            note(f"✓ {rel}/ exists with content, left in place")
            continue
        directory.mkdir(parents=True, exist_ok=True)
        if not keep.exists():
            keep.write_text("", encoding="utf-8")
            written.append(keep)
        note(f"✓ {rel}/ ready")


def hooks_dir(target: Path) -> Path:
    raw = run(["git", "-C", str(target), "rev-parse", "--git-path", "hooks"]).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = target / path
    return path


HOOK_MARKER = "# wiki-kit pre-commit wrapper"


def hook_wrapper_text() -> str:
    """The hook is a generated wrapper, not a symlink: the kit script
    needs a python that can import jsonschema, and `#!/usr/bin/env
    python3` under git would resolve to whatever the machine's PATH
    says. The wrapper pins the interpreter that ran the install and
    exec's the kit's CURRENT script, so kit updates apply without a
    reinstall. .git/hooks is machine-local, so the baked path is legal."""
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER}\n"
        f'exec "{sys.executable}" "{KIT_SCRIPTS / "pre-commit"}" "$@"\n'
    )


def install_hook(target: Path) -> None:
    dest = hooks_dir(target) / "pre-commit"
    dest.parent.mkdir(parents=True, exist_ok=True)
    wrapper = hook_wrapper_text()
    if dest.exists() or dest.is_symlink():
        current = dest.read_text(encoding="utf-8") if dest.is_file() else ""
        if current == wrapper:
            note("✓ pre-commit hook wrapper up to date")
            return
        if HOOK_MARKER not in current:
            raise InstallError(
                f"pre-commit hook exists and is not a kit wrapper: {dest}; "
                "resolve it by hand before reinstalling"
            )
        # A kit wrapper from another interpreter or kit location: rewrite.
    dest.write_text(wrapper, encoding="utf-8")
    dest.chmod(0o755)
    note("✓ pre-commit hook wrapper installed")


def claude_rules(specifiers: list[str]) -> list[str]:
    return [
        f"{tool}({specifier})"
        for specifier in specifiers
        for tool in ("Write", "Edit", "NotebookEdit")
    ]


def contract_specifiers(config: WikiConfig) -> list[str]:
    """Wiki-repo-relative deny specifiers derived from [contract].protected.
    'CLAUDE.local.md' stays a bare name (matches at any depth, covering
    linked worktrees); every other entry anchors to the repo root."""
    specifiers = []
    for rel in config.contract.protected:
        if rel == "CLAUDE.local.md":
            specifiers.append(rel)
        else:
            specifiers.append(f"/{rel}")
    return specifiers


def merge_claude_settings(config: WikiConfig, written: list[Path]) -> None:
    settings_path = config.root / ".claude" / "settings.json"
    rules = claude_rules(contract_specifiers(config))
    settings = (
        json.loads(settings_path.read_text(encoding="utf-8"))
        if settings_path.exists()
        else {}
    )
    permissions = settings.setdefault("permissions", {})
    deny = permissions.setdefault("deny", [])
    missing = [rule for rule in rules if rule not in deny]
    if not missing:
        note("✓ Claude deny rules up to date")
        return
    deny.extend(missing)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(settings, indent=2) + "\n", encoding="utf-8"
    )
    if settings_path not in written:
        written.append(settings_path)
    note(f"✓ Claude deny rules updated ({len(missing)} added)")


def initial_commit(target: Path, written: list[Path]) -> None:
    if head_exists(target):
        note("✓ repo already has commits; nothing to commit")
        return
    rels = sorted(
        str(path.relative_to(target)) for path in written if path.exists()
    )
    tracked_projections = [
        "wiki/log.md",
        "wiki/pending/index.json",
        "wiki/pending/latest.md",
    ]
    for rel in tracked_projections:
        if (target / rel).exists() and rel not in rels:
            rels.append(rel)
    if not rels:
        raise InstallError(
            "no commits and nothing installer-written to commit; the "
            "initial-commit boot requirement cannot be met"
        )
    run(["git", "-C", str(target), "add", "--", *rels])
    run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=wiki-kit installer",
            "-c",
            "user.email=installer@wiki-kit.local",
            "commit",
            "-q",
            "-m",
            "wiki-kit: initialize deployment",
            "--",
            *rels,
        ]
    )
    note(f"✓ initial commit created ({len(rels)} installer-written files)")


def render_orientation(config: WikiConfig) -> None:
    output = config.root / "CLAUDE.local.md"
    if output.exists():
        note("✓ orientation file exists; renders carry it forward")
        return
    kit_cli(
        "wiki-render.py",
        "claude-local",
        "--wiki",
        str(config.root),
        "--quickstart-file",
        str(ORIENTATION_TEMPLATE),
        "--no-lock",
    )
    note("✓ orientation skeleton rendered (empty-state Quickstart)")


def install_scheduler(config: WikiConfig, skip: bool) -> None:
    if skip:
        note("– scheduler skipped (--no-scheduler)")
        return
    if platform.system() != "Darwin":
        note("– scheduler skipped (launchd templates are macOS; this host is not)")
        return
    kit_cli("render_scheduler.py", "--wiki", str(config.root))
    note("✓ scheduler units rendered from templates")


def install(target: Path, no_scheduler: bool) -> None:
    target = target.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    print(f"Installing wiki-kit machinery from {KIT_ROOT} into {target}")
    written: list[Path] = []
    ensure_git_repo(target)
    seed_file(
        target / CONFIG_FILE_NAME,
        default_wiki_toml(target.name),
        CONFIG_FILE_NAME,
        written,
    )
    ensure_gitignore(target, written)
    ensure_skeleton(target, written)
    seed_file(
        target / "wiki" / "quarantine.json",
        json.dumps(EMPTY_QUARANTINE, indent=2) + "\n",
        "wiki/quarantine.json",
        written,
    )
    config = load_config(target)
    install_hook(target)
    # build-pending stamps generated_at_utc, so an unconditional run would
    # dirty the tree on every reinstall; build only when the projection is
    # absent (the doctor owns staleness detection thereafter).
    if not (target / "wiki" / "pending" / "index.json").exists():
        kit_cli("wiki-event.py", "build-pending", "--wiki", str(target))
        note("✓ pending projection built")
    else:
        note("✓ pending projection exists, left in place")
    kit_cli("wiki-render.py", "log", "--wiki", str(target))
    note("✓ log projection rendered (deterministic; rewrite is byte-stable)")
    merge_claude_settings(config, written)
    initial_commit(target, written)
    render_orientation(config)
    install_scheduler(config, no_scheduler)
    print("Done. Verify:")
    print(f"  ls -la {hooks_dir(target) / 'pre-commit'}")
    print(f"  {sys.executable} {KIT_SCRIPTS / 'wiki-doctor.py'} --wiki {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wiki",
        type=Path,
        required=True,
        help="the wiki repo to install into (created when absent)",
    )
    parser.add_argument(
        "--no-scheduler",
        action="store_true",
        help="skip rendering scheduler units even on macOS",
    )
    args = parser.parse_args(argv)
    try:
        install(args.wiki, args.no_scheduler)
    except (InstallError, ConfigError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
