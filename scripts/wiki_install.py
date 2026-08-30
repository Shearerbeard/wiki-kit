#!/usr/bin/env python3
"""Install the kit's machinery hooks and seed files into a wiki repo.

The kit repo holds machinery; this installer wires a TARGET wiki repo
(blank or already carrying content) to it:

- init: git repo, `wiki.toml` (kit defaults, name from the directory),
  `.gitignore` coverage for the machine-local files, the content
  skeleton, an empty quarantine ledger, the pending and log projections,
  and - when the repo has no commits yet - an initial commit of exactly
  the files the installer wrote.
- pre-commit hook: a generated wrapper that bakes no machine paths -
  it resolves the kit checkout from the deployment's machine-local
  overlay at run time (the mechanical provenance layer the
  pre-extraction installer never installed).
- kit stamp: the deployment's wiki.toml records the contract version
  and kit commit it was installed from ([kit], installer-owned); the
  machine-local overlay records where the kit checkout lives
  ([tools] kit).
- deny rules: derived from the deployment's own `[contract]` in
  `wiki.toml` - the single contract source the doctor and install-smoke
  also read - and merged into the wiki repo's `.claude/settings.json`.
- scheduler units: rendered from the kit's templates (launchd on macOS,
  systemd user timers on Linux, skipped elsewhere; `--no-scheduler`
  skips explicitly). Rendering never loads a unit: the render's
  output, unit paths plus the load commands, is relayed verbatim.
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
import re
import subprocess
import sys
from pathlib import Path

KIT_SCRIPTS = Path(__file__).resolve().parent
KIT_ROOT = KIT_SCRIPTS.parent
if str(KIT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(KIT_SCRIPTS))

from wiki_config import (  # noqa: E402
    CONFIG_FILE_NAME,
    CONTRACT_VERSION,
    DEFAULT_CONTRACT,
    HOOK_MARKER,
    OVERLAY_FILE_NAME,
    ConfigError,
    WikiConfig,
    _git_common_root,
    contract_deny_rules,
    git_hooks_dir,
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


def kit_cli(script: str, *args: str) -> str:
    return run([sys.executable, str(KIT_SCRIPTS / script), *args])


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
        "# With two or more companions, default_companion names the one",
        "# that bare #N references and repo-less workstreams resolve to:",
        '# default_companion = "widget"',
        "",
        "# One table per docked consumer repo (docs/wiki-toml-schema.md);",
        "# the overlay carries each companion's machine path.",
        "# [companions.widget]",
        '# posture = "committed"',
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


def kit_head() -> str:
    return run(["git", "-C", str(KIT_ROOT), "rev-parse", "HEAD"]).strip()


def stamp_kit(target: Path, written: list[Path]) -> None:
    """The [kit] stamp is installer-owned: the contract version and kit
    commit this deployment was installed from (boardkit's stamp
    pattern). Rewritten in place on every install; the doctor, not the
    installer, judges drift afterward."""
    path = target / CONFIG_FILE_NAME
    text = path.read_text(encoding="utf-8")
    head = kit_head()
    block = (
        "[kit]\n"
        f"contract_version = {CONTRACT_VERSION}\n"
        f'commit = "{head}"\n'
    )
    section = re.search(
        r"(?ms)^\[kit\][ \t]*(?:#[^\n]*)?\n.*?(?=^[ \t]*\[|\Z)", text
    )
    if section is None and re.search(r"(?m)^\s*\[\s*kit\s*\]", text):
        raise InstallError(
            f"{path} has a [kit] header in a form the installer cannot "
            "safely edit; canonicalize it by hand"
        )
    if section is None:
        new_text = (text if text.endswith("\n") else text + "\n") + "\n" + block
    else:
        if section.group(0).strip() == block.strip():
            note("✓ kit stamp up to date")
            return
        new_text = text[: section.start()] + block + "\n" + text[section.end():]
    path.write_text(new_text, encoding="utf-8")
    if path not in written:
        written.append(path)
    note(f"✓ kit stamp recorded (contract v{CONTRACT_VERSION}, {head[:12]})")


def _config_root(target: Path) -> Path:
    """The checkout that owns the machine-local overlay: the main
    checkout, exactly where the pre-commit wrapper looks (the parent
    of the git common dir). A layout where git cannot locate the main
    checkout fails loud at install time instead of producing a hook
    that can never resolve."""
    main = _git_common_root(target)
    if main is None:
        raise InstallError(
            f"cannot locate the main checkout for {target}; the "
            "machine-local overlay must live there for the pre-commit "
            "wrapper to find it"
        )
    return main


def ensure_kit_path(target: Path, written: list[Path]) -> None:
    """The machine-local record of where the kit checkout lives on this
    machine (knob 11's [tools] form): the pre-commit wrapper reads it
    at run time instead of baking a path. Written at the config root so
    install from a linked worktree lands where the wrapper looks."""
    path = _config_root(target) / OVERLAY_FILE_NAME
    line = f'kit = "{KIT_ROOT}"'
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    tools = re.search(
        r"(?ms)^\[tools\][ \t]*(?:#[^\n]*)?\n.*?(?=^[ \t]*\[|\Z)", text
    )
    if tools is None and re.search(r"(?m)^\s*\[\s*tools\s*\]", text):
        raise InstallError(
            f"{path} has a [tools] header in a form the installer "
            "cannot safely edit; canonicalize it by hand"
        )
    if tools is not None:
        section = tools.group(0)
        stray = re.search(r"(?m)^[ \t]*kit[ \t]*=[ \t]*\S.*$", section)
        new_section, count = re.subn(
            r'(?m)^[ \t]*kit[ \t]*=[ \t]*"[^"]*"[ \t]*$', line, section
        )
        if count == 0 and stray is not None:
            raise InstallError(
                f"{path} has a [tools] kit line in a form the installer "
                f"cannot safely rewrite: {stray.group(0).strip()!r}; "
                "put it in the canonical double-quoted form by hand"
            )
        if count == 0:
            new_section = section.rstrip("\n") + "\n" + line + "\n"
        if new_section == section:
            note("✓ kit path up to date in the overlay")
            return
        new_text = text[: tools.start()] + new_section + text[tools.end():]
    else:
        new_text = text
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        if new_text:
            new_text += "\n"
        new_text += "[tools]\n" + line + "\n"
    path.write_text(new_text, encoding="utf-8")
    # Deliberately not in `written`: the overlay is gitignored
    # machine-local state and must never reach the initial commit.
    note("✓ kit path recorded in the machine-local overlay")


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
    return git_hooks_dir(target)


def hook_wrapper_text() -> str:
    """The hook is a generated wrapper, not a symlink, and it bakes no
    machine paths: at run time it reads the kit checkout path from the
    deployment's machine-local overlay ([tools] kit) - located through
    the git common dir so a linked worktree reaches the main checkout's
    overlay - and probes for a python that can import jsonschema. Kit
    moves and interpreter changes never strand it; reinstalling
    refreshes the one overlay record."""
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER}\n"
        'COMMON_DIR=$(git rev-parse --path-format=absolute --git-common-dir) || {\n'
        '  echo "pre-commit: cannot locate the git common dir" >&2\n'
        "  exit 1\n"
        "}\n"
        'MAIN_ROOT=$(dirname "$COMMON_DIR")\n'
        f'OVERLAY="$MAIN_ROOT/{OVERLAY_FILE_NAME}"\n'
        "KIT_ROOT=$(sed -n 's/^[ \\t]*kit[ \\t]*=[ \\t]*\"\\([^\"]*\\)\""
        "[ \\t\\r]*$/\\1/p' \"$OVERLAY\" 2>/dev/null | head -n 1)\n"
        'if [ -z "$KIT_ROOT" ]; then\n'
        '  echo "pre-commit: no [tools] kit path in $OVERLAY; '
        're-run the wiki-kit installer" >&2\n'
        "  exit 1\n"
        "fi\n"
        'if [ -x "$KIT_ROOT/.venv/bin/python" ] && '
        '"$KIT_ROOT/.venv/bin/python" -c "import jsonschema" 2>/dev/null; then\n'
        '  PYTHON="$KIT_ROOT/.venv/bin/python"\n'
        'elif python3 -c "import jsonschema" 2>/dev/null; then\n'
        "  PYTHON=python3\n"
        "else\n"
        '  echo "pre-commit: no jsonschema-capable python found '
        '(tried $KIT_ROOT/.venv/bin/python and python3)" >&2\n'
        "  exit 1\n"
        "fi\n"
        'exec "$PYTHON" "$KIT_ROOT/scripts/pre-commit" "$@"\n'
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


def merge_claude_settings(config: WikiConfig, written: list[Path]) -> None:
    settings_path = config.root / ".claude" / "settings.json"
    rules = contract_deny_rules(config)
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
    # wiki.toml rides along even when a hand-written one predated the
    # install: the hook renders the staged tree through it, so an initial
    # commit without it would block itself. Everything else in the commit
    # is exactly what this run wrote (`written`); pre-existing files at
    # projection paths are the owner's content and stay uncommitted.
    if CONFIG_FILE_NAME not in rels and (target / CONFIG_FILE_NAME).exists():
        rels.append(CONFIG_FILE_NAME)
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
    system = platform.system()
    if system == "Darwin":
        rendered = kit_cli("render_scheduler.py", "--wiki", str(config.root))
        note("✓ scheduler units rendered from templates (launchd)")
    elif system == "Linux":
        rendered = kit_cli(
            "render_scheduler.py",
            "--wiki",
            str(config.root),
            "--target",
            "systemd",
        )
        note("✓ scheduler units rendered from templates (systemd user timers)")
    else:
        note(f"– scheduler skipped (no scheduler target for {system})")
        return
    # Rendering never loads a unit; the render names the unit files and
    # the load commands, and that is the operator's only cue.
    for line in rendered.rstrip().splitlines():
        note(f"  {line}")


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
    stamp_kit(target, written)
    ensure_kit_path(target, written)
    install_hook(target)
    # build-pending stamps generated_at_utc, so an unconditional run would
    # dirty the tree on every reinstall; build only when the projection is
    # absent (the doctor owns staleness detection thereafter).
    pending_dir = target / "wiki" / "pending"
    if not (pending_dir / "index.json").exists():
        kit_cli("wiki-event.py", "build-pending", "--wiki", str(target))
        written.extend([pending_dir / "index.json", pending_dir / "latest.md"])
        note("✓ pending projection built")
    else:
        note("✓ pending projection exists, left in place")
    # The log projection is rendered only where nothing pre-exists: a file
    # already at wiki/log.md is the owner's content until it matches the
    # store's projection (decision-4 non-destructive rule; the doctor
    # reports the drift and regeneration is the owner's call).
    log_path = target / "wiki" / "log.md"
    if log_path.is_symlink():
        # is_symlink() is checked first because exists() follows links
        # and is false for a dangling one, which is still owner content.
        note("✓ wiki/log.md exists (symlink), left in place")
    elif log_path.exists():
        check = subprocess.run(
            [
                sys.executable,
                str(KIT_SCRIPTS / "wiki-render.py"),
                "log",
                "--wiki",
                str(target),
                "--check",
            ],
            capture_output=True,
            text=True,
        )
        detail = check.stderr.strip() or check.stdout.strip()
        if check.returncode == 0:
            note("✓ wiki/log.md matches the store projection")
        elif "does not match rendered output" in detail:
            note(
                "! wiki/log.md exists and differs from the store "
                "projection; left in place (the doctor reports the drift; "
                "regenerate when ready)"
            )
        else:
            raise InstallError(
                f"log projection check failed to run (not a mismatch): "
                f"{detail}"
            )
    else:
        kit_cli("wiki-render.py", "log", "--wiki", str(target))
        written.append(log_path)
        note("✓ log projection rendered")
    merge_claude_settings(config, written)
    initial_commit(target, written)
    render_orientation(config)
    install_scheduler(config, no_scheduler)
    print(
        "Project skills are rendered per consumer by wiki-dock.py install "
        "--skills-dir (docs/ADOPTION.md); this step installs none."
    )
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
