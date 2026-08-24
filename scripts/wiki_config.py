#!/usr/bin/env python3
"""Wiki root resolution and `wiki.toml` loading for every kit CLI.

The kit repo holds machinery; a wiki repo holds content plus its config
pair (`wiki.toml` committed, `wiki.local.toml` gitignored overlay). This
module is the single place that finds the wiki root and turns the config
pair into typed values. Scripts never self-derive content paths from
their own location: `Path(__file__)` points into the kit checkout, which
is the right base for the kit's own `schemas/` and `templates/` and the
wrong base for everything the wiki owns.

Resolution order (docking spec): flag, dock env, walk-up, common-dir,
legacy. First hit wins; an incomplete dock (manifest present, overlay
missing - the normal state of a committed-posture linked worktree)
falls through, but the nearest manifest's identity still binds
whichever later step completes, and resolution never silently passes a
COMPLETE dock. The legacy channel (env variable named in
`wiki_legacy.py`, and the orientation-symlink convention) is honored
read-only until an adoption ruling retires it.

The overlay is allowlisted, not open: it may set exactly
`companions.<name>.path`, `[memory.triage].extra_dirs`,
`[memory].projects_root`, and `[tools].*`.
Any other key is a ConfigError - a machine overlay must never rewrite
identity, contract, or protection semantics on one machine.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from wiki_legacy import LEGACY_ORIENTATION_NAME, LEGACY_WIKI_ENV  # noqa: E402

KIT_ROOT = SCRIPT_DIR.parent

CONFIG_FILE_NAME = "wiki.toml"
OVERLAY_FILE_NAME = "wiki.local.toml"

DOCK_DIR_NAME = ".wiki"
DOCK_MANIFEST_NAME = "manifest.toml"
DOCK_OVERLAY_NAME = "local.toml"
DOCK_ENV = "WIKI_DOCK"

# The marker the installer's generated pre-commit wrapper carries and
# the doctor verifies: one source, two consumers (knob 13's rule).
HOOK_MARKER = "# wiki-kit pre-commit wrapper"

# The only slug rule v1 ships: an absolute path dash-encodes to the
# per-project memory directory name (`/home/alex/src/widget` ->
# `-home-alex-src-widget`).
SLUG_RULE_DASH_ENCODED = "dash-encoded-absolute-path"

POSTURES = ("committed", "gitignored", "invisible")

# The deployment contract version (boardkit's stamp pattern): a
# deployment's [kit] table records which contract version and kit
# commit stamped it. Type-validated at load; whether the version is
# SUPPORTED is the doctor's call, so an unknown future version reads
# as a doctor finding rather than a crash in every tool.
CONTRACT_VERSION = 1
SUPPORTED_CONTRACT_VERSIONS = frozenset({1})

# Kit defaults the init step writes into a new deployment's wiki.toml.
# Runtime consumers (installer, doctor, smoke) read the deployment's own
# [contract], never this constant: config is the single source once a
# deployment exists.
DEFAULT_CONTRACT = {
    "protected": [
        "CLAUDE.local.md",
        "wiki/log.md",
        "wiki/log-legacy.md",
        "wiki/events/**",
        "wiki/pending/**",
        "wiki/quarantine.json",
        "wiki/log-epoch.json",
        ".wiki/orientation.md",
    ],
    "external_allow": [
        "workstreams/**",
        "wiki/log.md",
        "wiki/pending/**",
        "wiki/entities/**",
        "CLAUDE.local.md",
    ],
    "skills": ["garden", "handoff", "morning", "session-feedback"],
    "global_skills": ["garden", "handoff", "morning"],
}

_WIKI_KEYS = {"name", "default_companion"}
_MEMORY_KEYS = {"index_line", "project_slug_rule"}
_COMPANION_KEYS = {
    "github",
    "base_branch",
    "branch_glob",
    "ticket_regex",
    "docs_subpath",
    "display_label",
    "posture",
    "memory_triage",
}
_CONTRACT_KEYS = {"protected", "external_allow", "skills", "global_skills"}
_SCHEDULE_KEYS = {"night", "morning", "garden_reminder"}
_NIGHT_KEYS = {"report_dir", "commit_prefix"}
_KIT_KEYS = {"contract_version", "commit"}
_TOP_LEVEL_TABLES = {
    "wiki",
    "memory",
    "companions",
    "contract",
    "schedule",
    "night",
    "kit",
}


class ConfigError(Exception):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _reject_unknown(keys: set[str], allowed: set[str], label: str) -> None:
    unknown = keys - allowed
    _require(not unknown, f"{label} has unknown keys: {sorted(unknown)}")


def _expand(raw: str, label: str) -> Path:
    """expanduser that fails loud as a ConfigError: an unresolvable
    ~user raises RuntimeError, which is a config problem, not a crash."""
    try:
        return Path(raw).expanduser()
    except RuntimeError as exc:
        raise ConfigError(f"{label}: {exc}") from exc


def _require_absolute_overlay_path(raw: object, label: str) -> None:
    """Overlay machine paths must be absolute after ~ expansion: a
    relative path would resolve against whatever directory the CLI
    runs from."""
    _require(
        isinstance(raw, str) and raw != "",
        f"{label} must be a non-empty string path",
    )
    _require(
        _expand(raw, label).is_absolute(),
        f"{label} must be absolute after ~ expansion (got {raw!r})",
    )


@dataclass(frozen=True)
class Companion:
    """One consumer repo's semantic config (knobs 2-6, 10, 14).

    `path` is the machine half, set only by the overlay; None means this
    machine has no local checkout of the companion.
    """

    name: str
    github: str | None
    base_branch: str
    branch_glob: str | None
    ticket_regex: str | None
    docs_subpath: str | None
    display_label: str
    posture: str | None
    memory_triage: bool
    path: Path | None


@dataclass(frozen=True)
class Contract:
    """Knob 13: the single deny-rule/skill contract source consumed by
    installer, doctor, and install-smoke."""

    protected: tuple[str, ...]
    external_allow: tuple[str, ...]
    skills: tuple[str, ...]
    # Adoption-only: ignored by the pre-adoption installer, which installs
    # project-scoped regardless. Activates only at an adoption the program
    # board rules to proceed.
    global_skills: tuple[str, ...]


@dataclass(frozen=True)
class Schedule:
    """Knob 12's time half; unit labels derive from [wiki].name."""

    night: str
    morning: str
    garden_reminder: str


@dataclass(frozen=True)
class NightConventions:
    """Knob 15: report-path and commit-message conventions, stated once."""

    report_dir: str
    commit_prefix: str


@dataclass(frozen=True)
class KitStamp:
    """The [kit] stamp: which contract version and kit commit stamped
    this deployment. Absent on pre-stamp deployments."""

    contract_version: int
    commit: str


@dataclass(frozen=True)
class WikiConfig:
    root: Path
    name: str
    default_companion_name: str | None
    memory_index_line: str | None
    project_slug_rule: str
    contract: Contract
    schedule: Schedule
    night: NightConventions
    companions: dict[str, Companion] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)
    extra_triage_dirs: tuple[str, ...] = ()
    # Overlay-only machine path: where per-project agent memory lives on
    # this machine. None means the consumer's platform default applies.
    projects_root: Path | None = None
    kit_stamp: KitStamp | None = None

    def companion(self, name: str | None = None) -> Companion:
        if name is None:
            if self.default_companion_name is None:
                raise ConfigError(
                    "no companion requested and no default_companion is "
                    "configured; name one in wiki.toml "
                    "[wiki].default_companion or pass the companion "
                    "explicitly"
                )
            name = self.default_companion_name
        _require(
            name in self.companions,
            f"unknown companion {name!r}; configured: "
            f"{sorted(self.companions) or 'none'}",
        )
        return self.companions[name]

    def project_slug(self, path: Path) -> str:
        """The dash-encoded-absolute-path rule (knobs 7/8)."""
        return str(path.resolve()).replace("/", "-")

    def triage_project_dirs(self) -> tuple[str, ...]:
        """Knob 7's derived triage set: the wiki's own project slug (it is
        not a companion and needs no listing), every companion opted in
        via memory_triage, and the overlay's extra_dirs."""
        dirs = [self.project_slug(self.root)]
        for companion in self.companions.values():
            if not companion.memory_triage:
                continue
            if companion.path is None:
                raise ConfigError(
                    f"companion {companion.name!r} has memory_triage = true "
                    f"but no path in {OVERLAY_FILE_NAME} on this machine; "
                    f"add [companions.{companion.name}] path = ... to the "
                    "overlay"
                )
            dirs.append(self.project_slug(companion.path))
        dirs.extend(self.extra_triage_dirs)
        return tuple(dict.fromkeys(dirs))

    def tool(self, name: str, default: str) -> str:
        """Knob 11: overlay [tools] binary location, or the bare command
        name for PATH resolution."""
        return self.tools.get(name, default)


def contract_deny_rules(config: WikiConfig) -> list[str]:
    """The Claude deny rules derived from [contract].protected: the ONE
    derivation the installer writes, the doctor verifies, and
    install-smoke asserts. 'CLAUDE.local.md' stays a bare name (matches
    at any depth, covering linked worktrees); every other entry anchors
    to the repo root."""
    specifiers = [
        rel if rel == "CLAUDE.local.md" else f"/{rel}"
        for rel in config.contract.protected
    ]
    return [
        f"{tool}({specifier})"
        for specifier in specifiers
        for tool in ("Write", "Edit", "NotebookEdit")
    ]


def git_hooks_dir(repo: Path) -> Path:
    """The repo's effective hooks dir (honors core.hooksPath; git returns
    a relative path like `.git/hooks` for a plain repo)."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", "hooks"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ConfigError(
            f"{repo} is not a git repository: {result.stderr.strip()}"
        )
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else repo / path


def _git_toplevel(start: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if "not a git repository" in result.stderr:
            return None
        raise ConfigError(
            f"git rev-parse --show-toplevel failed in {start}: "
            f"{result.stderr.strip()}"
        )
    return Path(result.stdout.strip())


def _git_common_root(start: Path) -> Path | None:
    """The main checkout's root - the first entry of git worktree list,
    which git always orders first - or None outside a git repository.

    The common .git dir's parent is NOT the checkout (a repo may keep
    its git dir elsewhere, and submodule git dirs nest under the
    superproject's). But git itself does not record the main checkout
    in those layouts: worktree list reports the GIT DIR as the first
    worktree. That is detectable (first entry == common dir) and means
    the main checkout is unfindable - None, so resolution fails loud
    later instead of docking to an unrelated directory."""
    worktrees = subprocess.run(
        ["git", "-C", str(start), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if worktrees.returncode != 0:
        if "not a git repository" in worktrees.stderr:
            return None
        raise ConfigError(
            f"git worktree list failed in {start}: "
            f"{worktrees.stderr.strip()}"
        )
    first: Path | None = None
    for line in worktrees.stdout.splitlines():
        if line.startswith("worktree "):
            first = Path(line.removeprefix("worktree "))
            break
    if first is None:
        return None
    common = subprocess.run(
        [
            "git",
            "-C",
            str(start),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
    )
    if common.returncode != 0:
        # The probe the first==common guard depends on failed (e.g. git
        # too old for --path-format): unfindable is the honest answer,
        # and the wrapper's identical probe fails loud there too.
        return None
    if _same_path(first, Path(common.stdout.strip())):
        return None
    return first


def _same_path(a: Path, b: Path) -> bool:
    """Path identity that survives case-insensitive filesystems and
    symlink aliases: string equality first, inode identity second."""
    if a == b:
        return True
    try:
        return a.samefile(b)
    except OSError:
        return False


@dataclass(frozen=True)
class Dock:
    """One consumer repo's `.wiki/` dock: tracked identity manifest plus
    the machine-local overlay. `wiki_path` is None while the overlay is
    missing - the normal state of a freshly checked-out committed-posture
    clone or linked worktree."""

    dock_dir: Path
    wiki_name: str
    companion: str
    wiki_path: Path | None


def dock_complete_command(dock: Dock) -> str:
    """The command the resolver's fail-loud message names when no step
    yields a complete dock (spec: the error names the incomplete dock
    and the command that creates its overlay)."""
    return (
        f"python3 {KIT_ROOT / 'scripts' / 'wiki-dock.py'} complete "
        f"--wiki /path/to/{dock.wiki_name} --repo {dock.dock_dir.parent}"
    )


def load_dock(dock_dir: Path) -> Dock:
    """Parse one dock directory fail-loud: manifest identity keys are
    mandatory and exact; the overlay allows exactly [dock].path."""
    manifest_path = dock_dir / DOCK_MANIFEST_NAME
    _require(
        manifest_path.is_file(),
        f"{manifest_path} not found; a {DOCK_DIR_NAME}/ directory "
        "without its manifest is a malformed dock",
    )
    manifest = _load_toml(manifest_path)
    _reject_unknown(set(manifest), {"dock"}, f"{manifest_path}")
    table = manifest.get("dock")
    _require(
        isinstance(table, dict), f"{manifest_path} is missing its [dock] table"
    )
    _reject_unknown(set(table), {"wiki", "companion"}, f"{manifest_path} [dock]")
    wiki_name = table.get("wiki")
    companion = table.get("companion")
    _require(
        isinstance(wiki_name, str) and wiki_name != "",
        f"{manifest_path} [dock].wiki must be a non-empty string",
    )
    _require(
        isinstance(companion, str) and companion != "",
        f"{manifest_path} [dock].companion must be a non-empty string",
    )

    overlay_path = dock_dir / DOCK_OVERLAY_NAME
    wiki_path: Path | None = None
    if overlay_path.is_file():
        overlay = _load_toml(overlay_path)
        _reject_unknown(set(overlay), {"dock"}, f"{overlay_path}")
        overlay_table = overlay.get("dock", {})
        _require(
            isinstance(overlay_table, dict),
            f"{overlay_path} [dock] must be a table",
        )
        _reject_unknown(set(overlay_table), {"path"}, f"{overlay_path} [dock]")
        raw_path = overlay_table.get("path")
        _require(
            isinstance(raw_path, str) and raw_path != "",
            f"{overlay_path} [dock].path must be a non-empty string",
        )
        wiki_path = _expand(raw_path, f"{overlay_path} [dock].path")
        _require(
            wiki_path.is_absolute(),
            f"{overlay_path} [dock].path must be absolute (got "
            f"{raw_path!r}); a relative path would resolve against "
            "whatever directory the CLI runs from",
        )
    return Dock(
        dock_dir=dock_dir,
        wiki_name=wiki_name,
        companion=companion,
        wiki_path=wiki_path,
    )


def _dock_at(directory: Path) -> Dock | None:
    """The spec's walk-up stops at the first directory CONTAINING a
    .wiki/ - a dock dir without its manifest is a malformed dock, and
    load_dock fails loud on it rather than the walk silently passing."""
    dock_dir = directory / DOCK_DIR_NAME
    if dock_dir.is_dir():
        return load_dock(dock_dir)
    return None


def verify_dock_identity(dock: Dock, root: Path) -> None:
    """The identity chain, both ways: the wiki at `root` must carry the
    name the manifest claims, and must define the companion table the
    manifest names. Either mismatch fails loud naming both values."""
    raw = _load_toml(root / CONFIG_FILE_NAME)
    wiki_table = raw.get("wiki", {})
    _require(
        isinstance(wiki_table, dict),
        f"{root / CONFIG_FILE_NAME} [wiki] must be a table",
    )
    name = wiki_table.get("name")
    _require(
        isinstance(name, str) and name != "",
        f"{root / CONFIG_FILE_NAME} carries no [wiki].name; the dock "
        "identity chain needs the wiki's name stated explicitly",
    )
    _require(
        name == dock.wiki_name,
        f"dock {dock.dock_dir} names wiki {dock.wiki_name!r} but "
        f"{root / CONFIG_FILE_NAME} carries [wiki].name {name!r}",
    )
    companions = raw.get("companions", {})
    _require(
        isinstance(companions, dict),
        f"{root / CONFIG_FILE_NAME} [companions] must be a table",
    )
    _require(
        dock.companion in companions,
        f"dock {dock.dock_dir} names companion {dock.companion!r} but "
        f"{root / CONFIG_FILE_NAME} defines no "
        f"[companions.{dock.companion}] table (configured: "
        f"{sorted(companions) or 'none'})",
    )


def resolve_wiki_root(explicit: Path | str | None = None) -> Path:
    """First hit wins, per the docking spec's resolution order:

    1. the explicit --wiki flag (the wiki repo root itself);
    2. the dock env variable (a dock dir, or a dir directly holding one);
    3. walk-up from cwd bounded at the git toplevel - wiki.toml means we
       are inside the wiki repo itself, a `.wiki/` dock means a consumer;
    4. the git common-dir fallback (a linked worktree reaches the main
       checkout's dock with zero per-worktree setup);
    5. the legacy channel, read-only (env name in wiki_legacy.py, and a
       toplevel orientation-file symlink into a wiki root).

    An incomplete dock falls through, but the NEAREST manifest's identity
    binds whichever dock or root completes; a complete dock is never
    silently passed.
    """
    if explicit is not None:
        root = _expand(str(explicit), "--wiki").resolve()
        _require(
            (root / CONFIG_FILE_NAME).is_file(),
            f"--wiki {root} does not contain {CONFIG_FILE_NAME}; pass the "
            "wiki repo root itself",
        )
        return root

    binds: list[Dock] = []

    def finish(root: Path) -> Path:
        root = root.resolve()
        _require(
            (root / CONFIG_FILE_NAME).is_file(),
            f"resolved wiki root {root} does not contain {CONFIG_FILE_NAME}",
        )
        for dock in binds:
            verify_dock_identity(dock, root)
        return root

    def from_dock(dock: Dock) -> Path:
        assert dock.wiki_path is not None
        binds.append(dock)
        root = dock.wiki_path.resolve()
        _require(
            (root / CONFIG_FILE_NAME).is_file(),
            f"{dock.dock_dir / DOCK_OVERLAY_NAME} points at {root} which "
            f"does not contain {CONFIG_FILE_NAME}; re-run "
            f"{dock_complete_command(dock)}",
        )
        return finish(root)

    # Step 2: the dock env variable names a specific dock directory.
    env_value = os.environ.get(DOCK_ENV)
    if env_value:
        candidate = _expand(env_value, DOCK_ENV).resolve()
        if (candidate / DOCK_MANIFEST_NAME).is_file():
            dock_dir = candidate
        else:
            dock_dir = candidate / DOCK_DIR_NAME
        _require(
            (dock_dir / DOCK_MANIFEST_NAME).is_file(),
            f"{DOCK_ENV}={env_value} is neither a dock directory nor a "
            f"directory containing {DOCK_DIR_NAME}/{DOCK_MANIFEST_NAME}",
        )
        dock = load_dock(dock_dir)
        if dock.wiki_path is not None:
            return from_dock(dock)
        binds.append(dock)  # incomplete: fall through, identity binds

    cwd = Path.cwd().resolve()
    toplevel = _git_toplevel(cwd)

    # Step 3: bounded walk-up. The first directory carrying wiki.toml is
    # the wiki root itself (a wiki does not dock to itself); the first
    # carrying a dock is the nearest dock, and the walk stops there.
    if toplevel is not None:
        current = cwd
        while True:
            if (current / CONFIG_FILE_NAME).is_file():
                return finish(current)
            dock = _dock_at(current)
            if dock is not None:
                if dock.wiki_path is not None:
                    return from_dock(dock)
                binds.append(dock)
                break  # nearest dock found; incomplete falls to step 4
            if _same_path(current, toplevel) or current == current.parent:
                break
            current = current.parent

    # Step 4: a linked worktree reaches the main checkout's dock.
    if toplevel is not None:
        common_root = _git_common_root(cwd)
        if common_root is not None and not _same_path(common_root, toplevel):
            dock = _dock_at(common_root)
            if dock is not None:
                if dock.wiki_path is not None:
                    return from_dock(dock)
                binds.append(dock)

    # Step 5: the legacy channel, honored read-only until adoption.
    legacy_note = ""
    legacy_value = os.environ.get(LEGACY_WIKI_ENV)
    if legacy_value:
        root = _expand(legacy_value, LEGACY_WIKI_ENV).resolve()
        if (root / CONFIG_FILE_NAME).is_file():
            return finish(root)
        legacy_note = (
            f"; the legacy {LEGACY_WIKI_ENV} channel is set to "
            f"{legacy_value} but that path contains no {CONFIG_FILE_NAME}"
        )
    if toplevel is not None:
        orientation = toplevel / LEGACY_ORIENTATION_NAME
        if orientation.is_symlink():
            target = orientation.resolve()
            root = target.parent
            if target.exists() and (root / CONFIG_FILE_NAME).is_file():
                return finish(root)
            reason = (
                "is dangling"
                if not target.exists()
                else f"has no {CONFIG_FILE_NAME} beside it"
            )
            legacy_note += (
                f"; the legacy orientation symlink {orientation} "
                f"resolves to {target}, which {reason}"
            )

    incomplete = binds[0] if binds else None
    if incomplete is not None:
        raise ConfigError(
            f"dock {incomplete.dock_dir} names wiki "
            f"{incomplete.wiki_name!r} but carries no {DOCK_OVERLAY_NAME} "
            "overlay on this machine, and no other channel resolved a "
            "wiki; create the overlay: "
            f"{dock_complete_command(incomplete)}{legacy_note}"
        )
    if toplevel is None:
        raise ConfigError(
            f"not inside a git repository and no --wiki given; the walk-up "
            f"never leaves a repository, so pass --wiki /path/to/wiki "
            f"(the directory containing {CONFIG_FILE_NAME}), set "
            f"{DOCK_ENV} to a dock directory, or run from a docked repo"
            f"{legacy_note}"
        )
    raise ConfigError(
        f"no {CONFIG_FILE_NAME} and no {DOCK_DIR_NAME}/ dock between {cwd} "
        f"and the repository toplevel {toplevel}; pass --wiki "
        f"/path/to/wiki or dock this repo first{legacy_note}"
    )


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _check_overlay_allowlist(overlay: dict, path: Path) -> None:
    allowed_tables = {"companions", "memory", "tools"}
    _reject_unknown(set(overlay), allowed_tables, f"{path}")
    companions = overlay.get("companions", {})
    _require(
        isinstance(companions, dict), f"{path} [companions] must be a table"
    )
    for name, table in companions.items():
        _require(
            isinstance(table, dict),
            f"{path}: [companions.{name}] must be a table",
        )
        _reject_unknown(set(table), {"path"}, f"{path} [companions.{name}]")
        if "path" in table:
            _require_absolute_overlay_path(
                table["path"], f"{path} [companions.{name}].path"
            )
    memory = overlay.get("memory", {})
    _require(isinstance(memory, dict), f"{path} [memory] must be a table")
    _reject_unknown(set(memory), {"triage", "projects_root"}, f"{path} [memory]")
    projects_root = memory.get("projects_root")
    if projects_root is not None:
        _require_absolute_overlay_path(
            projects_root, f"{path} [memory].projects_root"
        )
    triage = memory.get("triage", {})
    _require(
        isinstance(triage, dict), f"{path} [memory.triage] must be a table"
    )
    _reject_unknown(set(triage), {"extra_dirs"}, f"{path} [memory.triage]")
    tools = overlay.get("tools", {})
    _require(isinstance(tools, dict), f"{path} [tools] must be a table")
    for key, value in tools.items():
        _require(
            isinstance(value, str),
            f"{path} [tools].{key} must be a string path",
        )


def _string_or_none(table: dict, key: str, label: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    _require(
        isinstance(value, str) and value != "",
        f"{label}.{key} must be a non-empty string",
    )
    return value


def _build_companion(name: str, table: dict, overlay_path: str | None) -> Companion:
    label = f"[companions.{name}]"
    _reject_unknown(set(table), _COMPANION_KEYS, label)
    posture = _string_or_none(table, "posture", label)
    if posture is not None:
        _require(
            posture in POSTURES,
            f"{label}.posture must be one of {POSTURES}, got {posture!r}",
        )
    memory_triage = table.get("memory_triage", False)
    _require(
        isinstance(memory_triage, bool), f"{label}.memory_triage must be a boolean"
    )
    return Companion(
        name=name,
        github=_string_or_none(table, "github", label),
        base_branch=_string_or_none(table, "base_branch", label) or "main",
        branch_glob=_string_or_none(table, "branch_glob", label),
        ticket_regex=_string_or_none(table, "ticket_regex", label),
        docs_subpath=_string_or_none(table, "docs_subpath", label),
        display_label=_string_or_none(table, "display_label", label) or name,
        posture=posture,
        memory_triage=memory_triage,
        path=_expand(overlay_path, f"{label}.path") if overlay_path else None,
    )


def _string_tuple(table: dict, key: str, label: str) -> tuple[str, ...]:
    value = table.get(key, [])
    _require(
        isinstance(value, list) and all(isinstance(item, str) for item in value),
        f"{label}.{key} must be an array of strings",
    )
    return tuple(value)


def machine_path_violations(root: Path) -> list[str]:
    """Machine paths in the COMMITTED config (RULE-2's wiki-repo mirror).

    The doctor fails a committed wiki.toml containing an absolute or
    home-relative filesystem path. One documented exemption:
    [memory].index_line is display text rendered verbatim, never
    resolved, and is exempt.
    """
    raw = _load_toml(root / CONFIG_FILE_NAME)
    violations: list[str] = []

    def walk(value: object, crumb: str) -> None:
        if crumb == "memory.index_line":
            return
        if isinstance(value, str) and value.startswith(("/", "~/")):
            violations.append(f"{crumb} = {value!r}")
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{crumb}.{key}" if crumb else key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{crumb}[{index}]")

    walk(raw, "")
    return violations


def load_config(root: Path) -> WikiConfig:
    root = root.resolve()
    config_path = root / CONFIG_FILE_NAME
    _require(config_path.is_file(), f"{config_path} not found")
    raw = _load_toml(config_path)
    _reject_unknown(set(raw), _TOP_LEVEL_TABLES, f"{config_path}")

    wiki_table = raw.get("wiki", {})
    _reject_unknown(set(wiki_table), _WIKI_KEYS, f"{config_path} [wiki]")
    name = wiki_table.get("name", root.name)
    _require(
        isinstance(name, str) and name != "",
        f"{config_path} [wiki].name must be a non-empty string",
    )

    memory_table = raw.get("memory", {})
    _reject_unknown(set(memory_table), _MEMORY_KEYS, f"{config_path} [memory]")
    slug_rule = memory_table.get("project_slug_rule", SLUG_RULE_DASH_ENCODED)
    _require(
        slug_rule == SLUG_RULE_DASH_ENCODED,
        f"{config_path} [memory].project_slug_rule: {slug_rule!r} is not a "
        f"rule this kit version ships ({SLUG_RULE_DASH_ENCODED!r} is the "
        "only one)",
    )

    # The overlay lives at the MAIN checkout, exactly where the
    # pre-commit wrapper looks: a linked worktree never has its own
    # (an overlay at the worktree root is ignored, never merged). A
    # reader in a layout where git cannot locate the main checkout
    # falls back to the root's own path - the doctor flags the hook
    # mismatch there.
    overlay_root = _git_common_root(root) or root
    overlay_path = overlay_root / OVERLAY_FILE_NAME
    overlay = _load_toml(overlay_path) if overlay_path.is_file() else {}
    _check_overlay_allowlist(overlay, overlay_path)
    overlay_companions = overlay.get("companions", {})

    companion_tables = raw.get("companions", {})
    for stray in set(overlay_companions) - set(companion_tables):
        raise ConfigError(
            f"{overlay_path} names companion {stray!r} that {config_path} "
            "does not define; the overlay holds machine paths for declared "
            "companions only"
        )
    companions: dict[str, Companion] = {}
    for companion_name, table in companion_tables.items():
        _require(
            isinstance(table, dict),
            f"{config_path} [companions.{companion_name}] must be a table",
        )
        companions[companion_name] = _build_companion(
            companion_name,
            table,
            overlay_companions.get(companion_name, {}).get("path"),
        )

    default_companion = wiki_table.get("default_companion")
    if default_companion is not None:
        _require(
            default_companion in companions,
            f"{config_path} [wiki].default_companion {default_companion!r} "
            f"names no [companions.*] table",
        )
    elif len(companions) == 1:
        default_companion = next(iter(companions))
    elif len(companions) > 1:
        raise ConfigError(
            f"{config_path} configures {len(companions)} companions but no "
            "[wiki].default_companion; bare #N references and repo-less "
            "workstreams need one"
        )

    _require("contract" in raw, f"{config_path} is missing [contract]")
    contract_table = raw["contract"]
    _reject_unknown(set(contract_table), _CONTRACT_KEYS, f"{config_path} [contract]")
    contract = Contract(
        protected=_string_tuple(contract_table, "protected", "[contract]"),
        external_allow=_string_tuple(contract_table, "external_allow", "[contract]"),
        skills=_string_tuple(contract_table, "skills", "[contract]"),
        global_skills=_string_tuple(contract_table, "global_skills", "[contract]"),
    )
    _require(bool(contract.protected), f"{config_path} [contract].protected is empty")

    schedule_table = raw.get("schedule", {})
    _reject_unknown(set(schedule_table), _SCHEDULE_KEYS, f"{config_path} [schedule]")
    schedule = Schedule(
        night=schedule_table.get("night", "03:00"),
        morning=schedule_table.get("morning", "08:30"),
        garden_reminder=schedule_table.get("garden_reminder", "16:00"),
    )

    night_table = raw.get("night", {})
    _reject_unknown(set(night_table), _NIGHT_KEYS, f"{config_path} [night]")
    night = NightConventions(
        report_dir=night_table.get("report_dir", "reports/night"),
        commit_prefix=night_table.get("commit_prefix", "night:"),
    )

    tools = dict(overlay.get("tools", {}))
    overlay_projects_root = overlay.get("memory", {}).get("projects_root")
    extra_dirs = overlay.get("memory", {}).get("triage", {}).get("extra_dirs", [])
    _require(
        isinstance(extra_dirs, list)
        and all(isinstance(item, str) for item in extra_dirs),
        f"{overlay_path} [memory.triage].extra_dirs must be an array of strings",
    )

    kit_stamp: KitStamp | None = None
    kit_table = raw.get("kit")
    if kit_table is not None:
        _require(
            isinstance(kit_table, dict),
            f"{config_path} [kit] must be a table",
        )
        _reject_unknown(set(kit_table), _KIT_KEYS, f"{config_path} [kit]")
        stamp_version = kit_table.get("contract_version")
        _require(
            isinstance(stamp_version, int) and not isinstance(stamp_version, bool),
            f"{config_path} [kit].contract_version must be an integer",
        )
        stamp_commit = kit_table.get("commit")
        _require(
            isinstance(stamp_commit, str) and stamp_commit != "",
            f"{config_path} [kit].commit must be a non-empty string",
        )
        kit_stamp = KitStamp(
            contract_version=stamp_version, commit=stamp_commit
        )

    return WikiConfig(
        root=root,
        name=name,
        default_companion_name=default_companion,
        memory_index_line=_string_or_none(memory_table, "index_line", "[memory]"),
        project_slug_rule=slug_rule,
        companions=companions,
        contract=contract,
        schedule=schedule,
        night=night,
        tools=tools,
        extra_triage_dirs=tuple(extra_dirs),
        projects_root=(
            _expand(overlay_projects_root, "[memory].projects_root")
            if overlay_projects_root
            else None
        ),
        kit_stamp=kit_stamp,
    )


def _config_as_json(config: WikiConfig) -> dict:
    return {
        "root": str(config.root),
        "name": config.name,
        "default_companion": config.default_companion_name,
        "memory_index_line": config.memory_index_line,
        "project_slug_rule": config.project_slug_rule,
        "companions": {
            companion.name: {
                "github": companion.github,
                "base_branch": companion.base_branch,
                "branch_glob": companion.branch_glob,
                "ticket_regex": companion.ticket_regex,
                "docs_subpath": companion.docs_subpath,
                "display_label": companion.display_label,
                "posture": companion.posture,
                "memory_triage": companion.memory_triage,
                "path": str(companion.path) if companion.path else None,
            }
            for companion in config.companions.values()
        },
        "contract": {
            "protected": list(config.contract.protected),
            "external_allow": list(config.contract.external_allow),
            "skills": list(config.contract.skills),
            "global_skills": list(config.contract.global_skills),
        },
        "schedule": {
            "night": config.schedule.night,
            "morning": config.schedule.morning,
            "garden_reminder": config.schedule.garden_reminder,
        },
        "night": {
            "report_dir": config.night.report_dir,
            "commit_prefix": config.night.commit_prefix,
        },
        "tools": config.tools,
        "projects_root": (
            str(config.projects_root) if config.projects_root else None
        ),
        "kit": (
            {
                "contract_version": config.kit_stamp.contract_version,
                "commit": config.kit_stamp.commit,
            }
            if config.kit_stamp
            else None
        ),
        "triage_project_dirs": _triage_dirs_or_none(config),
    }


def _triage_dirs_or_none(config: WikiConfig) -> list[str] | None:
    try:
        return list(config.triage_project_dirs())
    except ConfigError:
        return None  # print-config stays usable; the doctor reports the gap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["print-config"])
    parser.add_argument("--wiki", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        config = load_config(resolve_wiki_root(args.wiki))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(_config_as_json(config), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
