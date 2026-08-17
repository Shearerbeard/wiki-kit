#!/usr/bin/env python3
"""Validate YAML frontmatter in workstream files.

Field and body rules live in wiki_frontmatter (the shared
parse/format/validate module); this script owns the per-file error report
and the validation SCOPE, stated once here so the pre-commit hook and the
doctor call this script instead of re-deriving it: every
workstreams/**/*.md recursively, INCLUDING _archive/ (archived pages must
stay loadable), EXCLUDING _reference/ (free-form reference pages) and
index.md catalog files. Lock artifacts (.garden.lock) are not .md files
and never match.

Exit 0 = all valid, exit 1 = errors found, every failure listed on
stderr. Designed to run as the first step of a garden pass or as a
pre-commit check (the hook passes --workstreams-dir at the staged tree).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import wiki_config
from wiki_frontmatter import (
    FrontmatterError,
    parse_frontmatter,
    validate_frontmatter,
    validate_workstream_body,
)

EXCLUDED_DIR_NAMES = {"_reference"}
EXCLUDED_FILE_NAMES = {"index.md"}


def workstream_files(workstreams_dir: Path) -> list[Path]:
    """Every file the scope rule in the module docstring covers."""
    files = []
    for path in sorted(workstreams_dir.rglob("*.md")):
        relative_parts = path.relative_to(workstreams_dir).parts
        if path.name in EXCLUDED_FILE_NAMES:
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in relative_parts[:-1]):
            continue
        files.append(path)
    return files


def validate(path: Path, label: str) -> list[str]:
    # parse_frontmatter directly, not parse_workstream_file: with the
    # recursive scope the bare file name is ambiguous (a.md versus
    # _archive/a.md), so errors carry the workstreams-relative label.
    try:
        fm, body = parse_frontmatter(path.read_text())
    except FrontmatterError as exc:
        return [f"{label}: {exc}"]

    errors = validate_frontmatter(fm, label)
    errors.extend(validate_workstream_body(fm, body, label))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate workstream frontmatter and body sections",
    )
    parser.add_argument(
        "--wiki",
        type=Path,
        default=None,
        help="wiki repo root (default: walk up from cwd to wiki.toml)",
    )
    parser.add_argument(
        "--workstreams-dir",
        type=Path,
        default=None,
        help=(
            "validate this directory instead of <wiki>/workstreams "
            "(the staged-tree hook's override)"
        ),
    )
    args = parser.parse_args(argv)

    if args.workstreams_dir is not None:
        workstreams_dir = args.workstreams_dir
    else:
        try:
            workstreams_dir = wiki_config.resolve_wiki_root(args.wiki) / "workstreams"
        except wiki_config.ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if not workstreams_dir.is_dir():
        print(f"error: {workstreams_dir} not found", file=sys.stderr)
        return 1

    files = workstream_files(workstreams_dir)
    if not files:
        print("warning: no workstream files found", file=sys.stderr)
        return 0

    all_errors = []
    valid = 0
    for path in files:
        errors = validate(path, str(path.relative_to(workstreams_dir)))
        if errors:
            all_errors.extend(errors)
        else:
            valid += 1

    if all_errors:
        for error in all_errors:
            print(f"error: {error}", file=sys.stderr)
        print(
            f"\n{valid} valid, {len(all_errors)} errors in {len(files)} files",
            file=sys.stderr,
        )
        return 1
    print(f"{valid}/{len(files)} workstream files valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
