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
legacy. K2 ships the subset a wiki repo needs of itself: the explicit
flag and the walk-up to `wiki.toml` bounded at the git toplevel. The
dock steps (`WIKI_DOCK`, `.wiki/` manifests, the common-dir worktree
fallback, the legacy channel) are K3's resolver card and slot in where
the comment below marks.

The overlay is allowlisted, not open: it may set exactly
`companions.<name>.path`, `[memory.triage].extra_dirs`, and `[tools].*`.
Any other key is a ConfigError - a machine overlay must never rewrite
identity, contract, or protection semantics on one machine.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_FILE_NAME = "wiki.toml"
OVERLAY_FILE_NAME = "wiki.local.toml"

# The only slug rule v1 ships: an absolute path dash-encodes to the
# per-project memory directory name (`/home/alex/src/widget` ->
# `-home-alex-src-widget`).
SLUG_RULE_DASH_ENCODED = "dash-encoded-absolute-path"

POSTURES = ("committed", "gitignored", "invisible")

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
_TOP_LEVEL_TABLES = {"wiki", "memory", "companions", "contract", "schedule", "night"}


class ConfigError(Exception):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _reject_unknown(keys: set[str], allowed: set[str], label: str) -> None:
    unknown = keys - allowed
    _require(not unknown, f"{label} has unknown keys: {sorted(unknown)}")


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
class WikiConfig:
    root: Path
    name: str
    default_companion_name: str | None
    memory_index_line: str | None
    project_slug_rule: str
    companions: dict[str, Companion] = field(default_factory=dict)
    contract: Contract = None  # type: ignore[assignment]
    schedule: Schedule = None  # type: ignore[assignment]
    night: NightConventions = None  # type: ignore[assignment]
    tools: dict[str, str] = field(default_factory=dict)
    extra_triage_dirs: tuple[str, ...] = ()

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


def _git_toplevel(start: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def resolve_wiki_root(explicit: Path | str | None = None) -> Path:
    """First hit wins, per the docking spec's resolution order.

    K2 subset: step 1 (the --wiki flag) and the wiki-repo case of step 3
    (walk-up to wiki.toml, bounded at the git toplevel; no walk-up
    outside a git repo). Steps 2, 4, and 5 (WIKI_DOCK, dock manifests,
    the common-dir worktree fallback, the legacy channel) are the K3
    resolver card and slot in here.
    """
    if explicit is not None:
        root = Path(explicit).expanduser().resolve()
        _require(
            (root / CONFIG_FILE_NAME).is_file(),
            f"--wiki {root} does not contain {CONFIG_FILE_NAME}; pass the "
            "wiki repo root itself",
        )
        return root
    cwd = Path.cwd().resolve()
    toplevel = _git_toplevel(cwd)
    if toplevel is None:
        raise ConfigError(
            f"not inside a git repository and no --wiki given; the walk-up "
            f"never leaves a repository, so pass --wiki /path/to/wiki "
            f"(the directory containing {CONFIG_FILE_NAME})"
        )
    current = cwd
    while True:
        if (current / CONFIG_FILE_NAME).is_file():
            return current
        if current == toplevel or current == current.parent:
            break
        current = current.parent
    raise ConfigError(
        f"no {CONFIG_FILE_NAME} between {cwd} and the repository toplevel "
        f"{toplevel}; pass --wiki /path/to/wiki"
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
    for name, table in overlay.get("companions", {}).items():
        _require(
            isinstance(table, dict),
            f"{path}: [companions.{name}] must be a table",
        )
        _reject_unknown(set(table), {"path"}, f"{path} [companions.{name}]")
    memory = overlay.get("memory", {})
    _reject_unknown(set(memory), {"triage"}, f"{path} [memory]")
    triage = memory.get("triage", {})
    _reject_unknown(set(triage), {"extra_dirs"}, f"{path} [memory.triage]")
    tools = overlay.get("tools", {})
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
        base_branch=table.get("base_branch", "main"),
        branch_glob=_string_or_none(table, "branch_glob", label),
        ticket_regex=_string_or_none(table, "ticket_regex", label),
        docs_subpath=_string_or_none(table, "docs_subpath", label),
        display_label=table.get("display_label", name),
        posture=posture,
        memory_triage=memory_triage,
        path=Path(overlay_path).expanduser() if overlay_path else None,
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

    overlay_path = root / OVERLAY_FILE_NAME
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
    extra_dirs = overlay.get("memory", {}).get("triage", {}).get("extra_dirs", [])
    _require(
        isinstance(extra_dirs, list)
        and all(isinstance(item, str) for item in extra_dirs),
        f"{overlay_path} [memory.triage].extra_dirs must be an array of strings",
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
        "triage_project_dirs": list(config.triage_project_dirs())
        if all(
            companion.path is not None
            for companion in config.companions.values()
            if companion.memory_triage
        )
        else None,
    }


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
