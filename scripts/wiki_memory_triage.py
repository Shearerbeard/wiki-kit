#!/usr/bin/env python3
"""Memory triage: classify Claude auto-memory files in the configured project
dirs and propose a disposition, so a one-time migration and the recurring
garden memory-triage step can drain project / reference knowledge out of
memory into the wiki. Full design: docs/wiki-system/memory-triage.md.

Disposition is decided by signal precedence, and every entry records the
`reason` (which signal fired) so the manifest explains itself:

  1. Recognized filename prefix (the author named the kind):
     user-/user_ and feedback-/feedback_  -> KEEP (preference, stays in memory)
     project-/reference-/session- (or _)  -> MIGRATE (durable work knowledge)
  2. Otherwise the YAML frontmatter `type:` resolves it (declared kind), read
     top-level or nested under `metadata:` (both shapes occur in practice).
  3. Otherwise FLAG for human review.
  An ephemeral-status body (live PID / a /tmp log path / a "delete this memory"
  note) overrides a MIGRATE to DROP: a dead working note wearing a durable
  prefix is not worth migrating. KEEP and FLAG are never auto-dropped, and DROP
  never removes a sole source of truth (see the design doc's safety rule).

The MEMORY.md index is not a memory fact and is never triaged.

Two modes:
  scan          full manifest over every in-scope dir (dry run)
  since-garden  only files modified since the latest garden-apply event
                (the recurring garden step): file mtime vs event timestamp_utc

Scope is the config-derived triage set (knob 7, wiki.toml): the wiki repo's
own project slug, every companion with `memory_triage = true` (its overlay
path slug-encoded), plus the overlay's `[memory.triage].extra_dirs` — see
wiki_config.WikiConfig.triage_project_dirs. Every other ~/.claude/projects
dir is out of scope and gets the preferences-only policy, no migration.

Usage:
  uv run scripts/wiki-memory-triage.py scan --wiki /path/to/wiki
  ... scan --json
  ... since-garden
  ... --projects-root P   # override ~/.claude/projects (tests, other hosts)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple

SCRIPT_DIR = Path(__file__).resolve().parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from wiki_config import (  # noqa: E402
    ConfigError,
    load_config,
    resolve_wiki_root,
)
from wiki_event import (  # noqa: E402
    EventType,
    load_events,
)

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
MEMORY_SUBDIR = "memory"
INDEX_FILENAME = "MEMORY.md"
TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"
FRONTMATTER_DELIM = "---"

# The leading `type:` line inside a frontmatter block, top-level or indented
# under `metadata:`. A targeted line scan, not a YAML parse: memory frontmatter
# is inconsistent (top-level vs nested type, occasionally not valid YAML), so a
# strict parser is more fragile than matching the one type: line. `node_type:`
# does not match (the line must start with `type:` after optional indent).
TYPE_LINE_RE = re.compile(r"^\s*type:\s*(\S+)\s*$")

# Conservative ephemeral-status markers. Auto-DROP is destructive, so each must
# be specific enough to stand alone with near-zero prose false positives; the
# matched label is recorded as the disposition reason so the user can veto in
# review. A bare "/tmp/*.log" path was tried and removed: it fired on durable
# design/research notes that merely cite a log path (3:1 false positives on the
# original corpus, review finding). The true ephemeral note carries a live PID
# or a self-delete line anyway, so the weak signal added only noise.
EPHEMERAL_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"delete this memory", re.IGNORECASE), "self-delete note"),
    (re.compile(r"\bPID[:*\s]+\d{2,}", re.IGNORECASE), "live PID"),
)


class PrefixClass(StrEnum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"
    SESSION = "session"
    OTHER = "other"  # unprefixed or any unknown prefix


class Disposition(StrEnum):
    KEEP = "keep"  # preferences: stays in memory
    MIGRATE = "migrate"  # durable work knowledge: drains to the wiki
    DROP = "drop"  # ephemeral, no durable content: delete (auto only on markers)
    FLAG = "flag"  # exception: human review


# Prefix/type class -> disposition. OTHER has no disposition of its own; it
# routes to the frontmatter tiebreaker, then to FLAG.
KEEP_CLASSES = frozenset({PrefixClass.USER, PrefixClass.FEEDBACK})
MIGRATE_CLASSES = frozenset(
    {PrefixClass.PROJECT, PrefixClass.REFERENCE, PrefixClass.SESSION}
)


class Decision(NamedTuple):
    disposition: Disposition
    reason: str  # deciding signal: prefix / frontmatter / ephemeral / unresolved


class TriageEntry(NamedTuple):
    project_dir: str
    filename: str
    prefix_class: PrefixClass
    disposition: Disposition
    reason: str
    is_exception: bool  # disposition is FLAG; a manifest column for filtering
    mtime_utc: str


def classify_prefix(filename: str) -> PrefixClass:
    """Filename prefix before the first '-' or '_'. Both delimiters occur in
    practice: some project dirs use hyphens (feedback-x.md), others use
    underscores (feedback_x.md). Unknown prefixes and unprefixed names both
    collapse to OTHER."""
    prefix = re.split(r"[-_]", filename.removesuffix(".md"), maxsplit=1)[0]
    try:
        return PrefixClass(prefix)
    except ValueError:
        return PrefixClass.OTHER


def disposition_for(prefix_class: PrefixClass) -> Disposition:
    if prefix_class in KEEP_CLASSES:
        return Disposition.KEEP
    if prefix_class in MIGRATE_CLASSES:
        return Disposition.MIGRATE
    return Disposition.FLAG


def frontmatter_type(text: str) -> str | None:
    """The `type:` value from the leading YAML frontmatter block (top-level or
    nested under metadata:), or None when there is no frontmatter or no type
    line. Only the block between the first two `---` fences is scanned, so a
    `type:` in the body is never read."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return None
    for line in lines[1:]:
        if line.strip() == FRONTMATTER_DELIM:
            break
        match = TYPE_LINE_RE.match(line)
        if match:
            return match.group(1)
    return None


def class_from_frontmatter_type(raw: str | None) -> PrefixClass | None:
    """A frontmatter `type:` value mapped to a PrefixClass, or None when absent
    or not one of the known kinds."""
    if raw is None:
        return None
    try:
        return PrefixClass(raw)
    except ValueError:
        return None


def ephemeral_marker(text: str) -> str | None:
    """The label of the first ephemeral-status marker found, or None."""
    for pattern, label in EPHEMERAL_MARKERS:
        if pattern.search(text):
            return label
    return None


def resolve_disposition(prefix_class: PrefixClass, text: str) -> Decision:
    """Disposition plus the signal that decided it (see module docstring for the
    precedence). A recognized prefix wins; otherwise the frontmatter type
    resolves it; otherwise FLAG. An ephemeral body overrides MIGRATE to DROP;
    KEEP and FLAG are never auto-dropped."""
    if prefix_class is not PrefixClass.OTHER:
        base = disposition_for(prefix_class)
        reason = f"prefix:{prefix_class}"
    else:
        type_class = class_from_frontmatter_type(frontmatter_type(text))
        if type_class is None:
            return Decision(
                Disposition.FLAG, "unresolved: no prefix or frontmatter type"
            )
        base = disposition_for(type_class)
        reason = f"frontmatter:type={type_class}"
    if base is Disposition.MIGRATE:
        marker = ephemeral_marker(text)
        if marker is not None:
            return Decision(Disposition.DROP, f"ephemeral:{marker}")
    return Decision(base, reason)


def memory_dir(projects_root: Path, project_dir: str) -> Path:
    return projects_root / project_dir / MEMORY_SUBDIR


def memory_files(memory_path: Path) -> list[Path]:
    """The *.md memory fact files in one dir, excluding the MEMORY.md index.

    Fails loud if the dir is missing: the triage set is curated config
    (wiki.toml companions + overlay extra_dirs), so a vanished dir is a real
    config/reality mismatch, not something to skip silently before a
    migration touches files outside git."""
    if not memory_path.is_dir():
        raise FileNotFoundError(f"memory dir missing: {memory_path}")
    return sorted(p for p in memory_path.glob("*.md") if p.name != INDEX_FILENAME)


def triage_entry(project_dir: str, path: Path, mtime_utc: str) -> TriageEntry:
    text = path.read_text(errors="replace")
    prefix_class = classify_prefix(path.name)
    decision = resolve_disposition(prefix_class, text)
    return TriageEntry(
        project_dir=project_dir,
        filename=path.name,
        prefix_class=prefix_class,
        disposition=decision.disposition,
        reason=decision.reason,
        is_exception=decision.disposition is Disposition.FLAG,
        mtime_utc=mtime_utc,
    )


def walk_triage(
    projects_root: Path,
    family_dirs: Sequence[str],
    modified_after: str | None = None,
) -> list[TriageEntry]:
    """The one walker: every in-scope memory file as a TriageEntry.

    `family_dirs` is the config-derived triage set
    (WikiConfig.triage_project_dirs), passed in explicitly so the caller —
    CLI or test — states the scope. `modified_after` is an ISO-8601 UTC
    string (since-garden mode); only files with a strictly later mtime are
    kept, and the mtime is checked before the file is read. The fixed
    timestamp format makes the lexical string compare equivalent to a
    chronological one."""
    entries: list[TriageEntry] = []
    for project_dir in family_dirs:
        for path in memory_files(memory_dir(projects_root, project_dir)):
            mtime_utc = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).strftime(
                TIMESTAMP_FMT
            )
            if modified_after is not None and mtime_utc <= modified_after:
                continue
            entries.append(triage_entry(project_dir, path, mtime_utc))
    return entries


def latest_garden_timestamp(events_dir: Path) -> str:
    """timestamp_utc of the most recent garden-apply event.

    Fails loud if the store holds no garden-apply event: since-garden has no
    baseline without one, and silently treating "no baseline" as "since the
    epoch" would re-flag every file every run."""
    garden = [
        event
        for event in load_events(events_dir)
        if event["event_type"] == EventType.GARDEN_APPLY
    ]
    if not garden:
        raise ValueError(
            "no garden-apply event in the store; since-garden has no baseline "
            "(use a full scan instead)"
        )
    # load_events returns events sorted by (timestamp_utc, event_id).
    return garden[-1]["timestamp_utc"]


def manifest_rows(entries: list[TriageEntry]) -> list[dict[str, Any]]:
    return [entry._asdict() for entry in entries]


def summarize(entries: list[TriageEntry], baseline: str | None) -> str:
    header = (
        f"Memory triage — files modified since garden-apply {baseline}"
        if baseline is not None
        else "Memory triage — full scan"
    )
    counts = {
        disposition: sum(1 for e in entries if e.disposition is disposition)
        for disposition in Disposition
    }
    lines = [
        f"{header}: {len(entries)} file(s).",
        "",
        f"  KEEP    (preferences, stay in memory): {counts[Disposition.KEEP]}",
        f"  MIGRATE (drain to the wiki):           {counts[Disposition.MIGRATE]}",
        f"  DROP    (auto, ephemeral — verify):    {counts[Disposition.DROP]}",
        f"  FLAG    (exception, needs review):     {counts[Disposition.FLAG]}",
    ]
    drops = [e for e in entries if e.disposition is Disposition.DROP]
    if drops:
        lines.append("")
        lines.append("Auto-DROP (ephemeral — verify before anything deletes):")
        lines.extend(f"  - {e.project_dir}/{e.filename} [{e.reason}]" for e in drops)
    exceptions = [e for e in entries if e.is_exception]
    if exceptions:
        lines.append("")
        lines.append("Exceptions (review — no prefix or frontmatter type):")
        lines.extend(
            f"  - {e.project_dir}/{e.filename} [class={e.prefix_class}]"
            for e in exceptions
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify configured Claude memory dirs for wiki migration"
    )
    parser.add_argument(
        "mode",
        choices=["scan", "since-garden"],
        help="scan: full manifest; since-garden: files changed since last garden",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable manifest")
    parser.add_argument(
        "--wiki",
        type=Path,
        default=None,
        help="wiki repo root (default: walk up from cwd to wiki.toml)",
    )
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=DEFAULT_PROJECTS_ROOT,
        help="override ~/.claude/projects (tests, other hosts)",
    )
    parser.add_argument(
        "--events-dir",
        type=Path,
        default=None,
        help="event store for the since-garden baseline (default: <wiki>/wiki/events)",
    )
    args = parser.parse_args(argv)
    try:
        root = resolve_wiki_root(args.wiki)
        config = load_config(root)
        family_dirs = list(config.triage_project_dirs())
        # The wiki's own slug enters the set implicitly, not by curation,
        # and a harness only creates that memory dir once a session has
        # run at the wiki root. Absent means nothing to triage there yet;
        # every curated dir still fails loud when missing.
        root_slug = config.project_slug(root)
        skipped_root = not memory_dir(args.projects_root, root_slug).is_dir()
        if skipped_root:
            family_dirs.remove(root_slug)
        events_dir = (
            args.events_dir if args.events_dir is not None else root / "wiki" / "events"
        )
        baseline = (
            latest_garden_timestamp(events_dir)
            if args.mode == "since-garden"
            else None
        )
        entries = walk_triage(args.projects_root, family_dirs, modified_after=baseline)
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        payload: dict[str, Any] = {
            "baseline": baseline,
            "manifest": manifest_rows(entries),
        }
        if skipped_root:
            payload["skipped"] = [root_slug]
        print(json.dumps(payload, indent=2))
    else:
        print(summarize(entries, baseline))
        if skipped_root:
            print(f"(no harness memory dir for the wiki root {root_slug}; skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
