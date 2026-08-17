#!/usr/bin/env python3
"""Workstream frontmatter: the one parse/format/validate implementation.

Replaces three diverging copies (validate-workstreams.py, build-index.py,
wiki_garden.py) whose agreement was accidental — only wiki_garden writes,
and its quoting rules were invisible to the two readers. Parsing is
fail-loud: a file without well-formed frontmatter raises FrontmatterError;
per-file tolerance is the caller's reporting choice, never a silent None.

FrontmatterError subclasses ValueError so existing `except ValueError`
call sites keep catching it.
"""

from __future__ import annotations

import re
from pathlib import Path

DELIMITER = "---"
REQUIRED_FIELDS = ("status", "branch", "sha", "last_updated")
VALID_STATUSES = ("active", "parked", "archived")
VALID_TIERS = ("board-page", "satellite")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROSE_TEMPLATE_VERSION = "prose-v1"
SESSION_UPDATES_HEADING = "## Session updates (uncurated)"
PROSE_V1_SECTIONS = (
    "Current State",
    "What Was Done",
    "Next",
    "Blockers",
    "Curated State",
    "Continuation Context",
)


class FrontmatterError(ValueError):
    pass


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """(frontmatter, body) from a workstream file's full text.

    The body starts on the line after the closing delimiter, so
    format_frontmatter(fm) + body is the identity round-trip. (The
    pre-consolidation wiki_garden pair kept the delimiter's own newline in
    the body AND emitted one in format — every garden apply grew the file
    by a blank line; the checked-in files carry the scars.)
    """
    if not text.startswith(DELIMITER):
        raise FrontmatterError("missing frontmatter open delimiter")
    try:
        end = text.index(DELIMITER, len(DELIMITER))
    except ValueError as exc:
        raise FrontmatterError("missing frontmatter close delimiter") from exc
    block = text[len(DELIMITER) : end].strip()
    fm: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        fm[key.strip()] = val.strip().strip('"').strip("'")
    body = text[end + len(DELIMITER) :]
    body = body.removeprefix("\n")
    return fm, body


def parse_workstream_file(path: Path) -> tuple[dict[str, str], str]:
    """parse_frontmatter with the file name prefixed onto any error."""
    try:
        return parse_frontmatter(path.read_text())
    except FrontmatterError as exc:
        raise FrontmatterError(f"{path.name}: {exc}") from exc


def format_frontmatter(fm: dict[str, str]) -> str:
    """The single written form: values are quoted when empty or containing
    spaces (wiki_garden's rule, now the contract). Key order is preserved.
    Values that cannot survive the quote-strip parse round-trip are refused
    rather than written lossily."""
    lines = [DELIMITER]
    for key, val in fm.items():
        if '"' in val or "\n" in val:
            raise FrontmatterError(
                f"frontmatter value for {key!r} cannot contain double quotes "
                f"or newlines: {val!r}"
            )
        if val == "" or " " in val:
            lines.append(f'{key}: "{val}"')
        else:
            lines.append(f"{key}: {val}")
    lines.append(DELIMITER)
    return "\n".join(lines) + "\n"


def validate_frontmatter(fm: dict[str, str], name: str) -> list[str]:
    """Field-level errors for a workstream file's frontmatter. Section-level
    rules (e.g. exactly one Session-updates heading) stay with the callers
    that own them."""
    errors = []
    for field in REQUIRED_FIELDS:
        if not fm.get(field):
            errors.append(f"{name}: missing required field '{field}'")
    if "status" in fm and fm["status"] not in VALID_STATUSES:
        errors.append(
            f"{name}: invalid status '{fm['status']}' (must be one of "
            f"{', '.join(VALID_STATUSES)})"
        )
    if "last_updated" in fm and not ISO_DATE_RE.match(fm["last_updated"]):
        errors.append(
            f"{name}: invalid last_updated '{fm['last_updated']}' (must be YYYY-MM-DD)"
        )
    if "template" in fm and fm["template"] != PROSE_TEMPLATE_VERSION:
        errors.append(
            f"{name}: unsupported template '{fm['template']}' "
            f"(must be {PROSE_TEMPLATE_VERSION})"
        )
    tier = fm.get("tier", "")
    epic = fm.get("epic", "")
    if tier and tier not in VALID_TIERS:
        errors.append(
            f"{name}: invalid tier '{tier}' (must be one of {', '.join(VALID_TIERS)})"
        )
    if tier and not epic:
        errors.append(f"{name}: tier '{tier}' requires an 'epic' field")
    if epic and not tier:
        errors.append(f"{name}: epic '{epic}' requires a 'tier' field")
    return errors


def validate_workstream_body(fm: dict[str, str], body: str, name: str) -> list[str]:
    errors = []
    lines = body.splitlines()
    heading_count = lines.count(SESSION_UPDATES_HEADING)
    if heading_count != 1:
        errors.append(
            f"{name}: expected exactly one '{SESSION_UPDATES_HEADING}' section, "
            f"found {heading_count}"
        )

    if fm.get("template") != PROSE_TEMPLATE_VERSION:
        return errors

    for section in PROSE_V1_SECTIONS:
        count = lines.count(f"### {section}")
        if count != 1:
            errors.append(
                f"{name}: prose-v1 expected exactly one '### {section}' section, "
                f"found {count}"
            )
    return errors
