#!/usr/bin/env python3
"""Build the ASCII tree index for CLAUDE.local.md from workstream files.

Deterministic: same input files always produce same output.
Python owns all mechanical logic. The garden LLM skill handles only
Quickstart synthesis and merge judgment.

Usage:
  build-index.py [--wiki PATH]              # print tree to stdout
  build-index.py [--wiki PATH] --json       # structured data for the LLM,
                                            # including stale and archival
                                            # candidates
  build-index.py [--wiki PATH] --validate   # validate frontmatter first,
                                            # then build
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import wiki_config  # noqa: E402
from wiki_event import EventType, load_events  # noqa: E402
from wiki_frontmatter import (  # noqa: E402
    FrontmatterError,
    parse_workstream_file,
    validate_frontmatter,
)

NUMBERING_RE = re.compile(r"^\d+\.\s*")


@dataclass
class Workstream:
    name: str
    status: str
    branch: str
    sha: str
    last_updated: str
    blocker: str
    epic: str
    tier: str
    pr: str
    issue: str
    # None = no repo: frontmatter field and no default-companion github to
    # fall back on; such a workstream simply gets no repo attribution.
    repo: str | None
    next_actions: list[str]
    done_items: list[str]
    body_lines: int
    sessions_since: int
    is_stale_candidate: bool
    is_thin: bool
    path: Path


def extract_section_items(path: Path, header: str) -> list[str]:
    text = path.read_text()
    in_section = False
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"### {header}"):
            in_section = True
            continue
        if in_section:
            if stripped.startswith("### ") or stripped.startswith("## "):
                break
            if stripped.startswith("#### "):
                continue
            if stripped and stripped not in ("None", "none", "N/A"):
                items.append(NUMBERING_RE.sub("", stripped).rstrip())
    return items


def count_sessions_since(last_updated: str, log_file: Path) -> int:
    if not log_file.exists():
        return 0
    count = 0
    for line in log_file.read_text().splitlines():
        if line.startswith("## ["):
            try:
                date_str = line.split("[")[1].split("T")[0]
                if date_str > last_updated:
                    count += 1
            except (IndexError, ValueError):
                continue
    return count


def load_workstreams(
    workstreams_dir: Path,
    log_file: Path,
    default_repo: str | None,
) -> tuple[list[Workstream], list[str]]:
    streams = []
    errors = []
    for p in sorted(workstreams_dir.glob("*.md")):
        if p.name == ".garden.lock":
            continue
        try:
            fm, body = parse_workstream_file(p)
        except FrontmatterError as exc:
            errors.append(str(exc))
            continue
        errs = validate_frontmatter(fm, p.name)
        if errs:
            errors.extend(errs)
            continue

        next_actions = extract_section_items(p, "Next")
        done_items = extract_section_items(p, "What Was Done")
        sessions_since = count_sessions_since(fm["last_updated"], log_file)
        has_next = bool([n for n in next_actions if n])
        has_blocker = bool(fm.get("blocker", ""))

        body_lines = len([line for line in body.splitlines() if line.strip()])

        streams.append(
            Workstream(
                name=p.stem,
                status=fm["status"],
                branch=fm["branch"],
                sha=fm["sha"][:7],
                last_updated=fm["last_updated"],
                blocker=fm.get("blocker", ""),
                epic=fm.get("epic", ""),
                tier=fm.get("tier", ""),
                pr=fm.get("pr", ""),
                issue=fm.get("issue", ""),
                repo=fm.get("repo", default_repo),
                next_actions=next_actions,
                done_items=done_items,
                body_lines=body_lines,
                sessions_since=sessions_since,
                is_thin=(body_lines < 15 and fm["status"] == "active"),
                is_stale_candidate=(
                    fm["status"] == "active"
                    and sessions_since > 8
                    and not has_next
                    and not has_blocker
                ),
                path=p,
            )
        )
    errors.extend(validate_epic_tiers(streams))
    return streams, errors


def validate_epic_tiers(streams: list[Workstream]) -> list[str]:
    """Cross-file epic/tier rules. Fail loud: a satellite whose epic has no
    single active board page would silently fall out of the rendered tree."""
    errors = []
    board_pages: dict[str, list[str]] = {}
    for s in streams:
        if s.tier == "board-page":
            board_pages.setdefault(s.epic, []).append(s.name)
            if s.status != "active":
                errors.append(
                    f"{s.name}.md: board-page for epic '{s.epic}' must be active, "
                    f"not '{s.status}'"
                )
    for epic, names in board_pages.items():
        if len(names) > 1:
            errors.append(
                f"epic '{epic}' has {len(names)} board pages ({', '.join(names)}); "
                "exactly one is required"
            )
    for s in streams:
        if s.tier == "satellite" and s.status == "active" and s.epic not in board_pages:
            errors.append(
                f"{s.name}.md: satellite of epic '{s.epic}' but no board-page "
                "workstream declares that epic"
            )
    return errors


def find_stale_next_candidates(streams: list[Workstream]) -> list[dict]:
    all_done = set()
    for s in streams:
        for item in s.done_items:
            normalized = item.lower().strip().rstrip(".")
            all_done.add(normalized)

    candidates = []
    for s in streams:
        for item in s.next_actions:
            normalized = item.lower().strip().rstrip(".")
            if normalized in all_done:
                candidates.append(
                    {
                        "workstream": s.name,
                        "next_item": item,
                        "match_type": "exact",
                    }
                )
    return candidates


def extract_blocker_prs(streams: list[Workstream]) -> list[dict]:
    pr_re = re.compile(r"#(\d+)")
    blockers = []
    for s in streams:
        if not s.blocker:
            continue
        for pr_num in pr_re.findall(s.blocker):
            blockers.append(
                {
                    "workstream": s.name,
                    "pr": int(pr_num),
                    "repo": s.repo,
                    "blocker_text": s.blocker,
                }
            )
    return blockers


TREE_FIELD_MAX_CHARS = 85


def _truncate(text: str, limit: int = TREE_FIELD_MAX_CHARS) -> str:
    """Cap a frontmatter/next-action string as it enters the tree view. The
    full value stays on disk and in --json output; only this ASCII summary
    line is bounded, so tree size scales with stream count, not per-page
    verbosity."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# A collapsed row carries the name (padded to 40), the date, and this
# detail: 60 bounds the row near 125 characters, a third of the three
# lines a full entry costs, which is the point of collapsing.
OVERFLOW_DETAIL_MAX_CHARS = 60


def latest_handoff_targets(events_dir: Path) -> set[str]:
    """The baton: the workstreams the newest handoff event proposes to
    update or create. Pinned into the forefront by name, so neither a
    same-day last_updated tie nor a satellite nesting can drop the one
    page a cold session needs next."""
    handoffs = [
        event
        for event in load_events(events_dir)
        if event.get("event_type") == EventType.HANDOFF
    ]
    if not handoffs:
        return set()
    # load_events sorts by (timestamp_utc, event_id); the last is newest.
    newest = handoffs[-1]
    return {
        proposal["name"]
        for proposal in newest.get("proposed_workstreams", [])
        if proposal.get("relationship") in ("primary", "candidate_new")
    }


def select_forefront(
    active: list[Workstream],
    target: int | None,
    pinned: frozenset[str] = frozenset(),
) -> tuple[list[Workstream], list[Workstream]]:
    """Split the active workstreams (already newest-first) into the ones
    the tree lists in full and the rest, which it collapses to one-line
    rows. Pinned names (the baton) stay in the forefront whatever their
    position; beyond them the `target` newest fill it. A collapsed row
    still shows its blocker, so no stop point disappears."""
    if target is None:
        return list(active), []
    forefront = [s for s in active if s.name in pinned]
    room = max(0, target - len(forefront))
    for stream in active:
        if stream.name in pinned:
            continue
        if room > 0:
            forefront.append(stream)
            room -= 1
    chosen = {s.name for s in forefront}
    forefront.sort(key=lambda s: active.index(s))
    overflow = [s for s in active if s.name not in chosen]
    return forefront, overflow


def build_tree(
    streams: list[Workstream],
    workstreams_dir: Path,
    forefront_target: int | None = None,
    pinned: frozenset[str] = frozenset(),
) -> str:
    all_active = sorted(
        [s for s in streams if s.status == "active"],
        key=lambda s: s.last_updated,
        reverse=True,
    )
    forefront, overflow = select_forefront(all_active, forefront_target, pinned)
    board_epics = {s.epic for s in forefront if s.tier == "board-page"}
    # Satellites nest under their epic's board page when both are in the
    # forefront; a satellite whose epic is collapsed or has no active
    # board page stays top-level (validate_epic_tiers reports the latter)
    # rather than vanishing with it.
    satellites: dict[str, list[Workstream]] = {}
    active = []
    for s in forefront:
        if s.tier == "satellite" and s.epic in board_epics:
            satellites.setdefault(s.epic, []).append(s)
        else:
            active.append(s)
    parked = sorted(
        [s for s in streams if s.status == "parked"],
        key=lambda s: s.last_updated,
        reverse=True,
    )
    archived_dir = workstreams_dir / "_archive"
    archive_count = len(list(archived_dir.glob("*.md"))) if archived_dir.exists() else 0

    lines = ["ACTIVE"]
    for i, s in enumerate(active):
        connector = "└─" if i == len(active) - 1 else "├─"
        pad = "   " if i == len(active) - 1 else "│  "
        first_next = s.next_actions[0] if s.next_actions else ""
        tag = (
            _truncate(s.blocker) if s.blocker else first_next[:40] if first_next else ""
        )
        if s.tier == "board-page":
            label = f"{s.name} [epic]"
        elif s.tier == "satellite" and s.epic:
            label = f"{s.name} (satellite of {s.epic})"
        else:
            label = s.name
        dots = "·" * max(1, 40 - len(label))
        lines.append(f"{connector} {label} {dots} {tag}")
        lines.append(f"{pad}{s.branch} @ {s.sha}")
        lines.append(
            f"{pad}Next: {_truncate(first_next)}"
            if first_next
            else f"{pad}Next: (none)"
        )
        for j, sat in enumerate(
            satellites.get(s.epic, []) if s.tier == "board-page" else []
        ):
            sat_last = j == len(satellites[s.epic]) - 1
            sconn = "└─" if sat_last else "├─"
            spad = "   " if sat_last else "│  "
            sat_next = sat.next_actions[0] if sat.next_actions else ""
            sat_tag = (
                _truncate(sat.blocker)
                if sat.blocker
                else sat_next[:40]
                if sat_next
                else ""
            )
            sdots = "·" * max(1, 37 - len(sat.name))
            lines.append(f"{pad}{sconn} {sat.name} {sdots} {sat_tag}")
            lines.append(f"{pad}{spad}{sat.branch} @ {sat.sha}")
            lines.append(
                f"{pad}{spad}Next: {_truncate(sat_next)}"
                if sat_next
                else f"{pad}{spad}Next: (none)"
            )
        if i < len(active) - 1:
            lines.append("│")

    if overflow:
        lines.append("")
        lines.append(f"ACTIVE, NOT IN THE FOREFRONT ({len(overflow)})")
        for s in overflow:
            if s.blocker:
                detail = "Blocked: " + _truncate(s.blocker, OVERFLOW_DETAIL_MAX_CHARS)
            elif s.next_actions:
                detail = "Next: " + _truncate(
                    s.next_actions[0], OVERFLOW_DETAIL_MAX_CHARS
                )
            else:
                detail = "Next: (none)"
            dots = "·" * max(1, 40 - len(s.name))
            lines.append(f"- {s.name} {dots} {s.last_updated} · {detail}")

    lines.append("")
    lines.append("PARKED")
    for i, s in enumerate(parked):
        connector = "└─" if i == len(parked) - 1 else "├─"
        tag = _truncate(s.blocker) if s.blocker else "(no blocker)"
        dots = "·" * max(1, 40 - len(s.name))
        lines.append(f"{connector} {s.name} {dots} {tag}")

    lines.append("")
    lines.append(f"ARCHIVED ({archive_count}) → workstreams/_archive/")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--wiki",
        type=Path,
        default=None,
        help="Wiki repo root; resolved from cwd when omitted.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured data instead of the ASCII tree.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate workstream frontmatter before building.",
    )
    args = parser.parse_args(argv)

    try:
        root = wiki_config.resolve_wiki_root(args.wiki)
        config = wiki_config.load_config(root)
    except wiki_config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    workstreams_dir = root / "workstreams"
    log_file = root / "wiki" / "log.md"
    # Knob 3's fallback half: a workstream with no repo: frontmatter field
    # attributes to the default companion's github value; without one it
    # gets no repo attribution at all (no invented fallback, no error).
    default_repo = None
    if config.default_companion_name is not None:
        default_repo = config.companion().github

    if args.validate:
        validate_script = SCRIPTS_DIR / "validate-workstreams.py"
        result = subprocess.run(
            [sys.executable, str(validate_script), "--wiki", str(root)],
            capture_output=True,
            text=True,
        )
        print(result.stdout, end="")
        if result.returncode != 0:
            print(result.stderr, end="", file=sys.stderr)
            return result.returncode

    streams, errors = load_workstreams(workstreams_dir, log_file, default_repo)

    if errors:
        for e in errors:
            print(f"warning: {e}", file=sys.stderr)

    if args.json:
        stale_next = find_stale_next_candidates(streams)
        blocker_prs = extract_blocker_prs(streams)

        data = {
            "active": [
                {
                    "name": s.name,
                    "branch": s.branch,
                    "sha": s.sha,
                    "last_updated": s.last_updated,
                    "blocker": s.blocker,
                    "epic": s.epic,
                    "tier": s.tier,
                    "next_actions": s.next_actions,
                    "pr": s.pr,
                    "issue": s.issue,
                    "sessions_since": s.sessions_since,
                    "file": str(s.path),
                }
                for s in streams
                if s.status == "active"
            ],
            "parked": [
                {
                    "name": s.name,
                    "blocker": s.blocker,
                    "last_updated": s.last_updated,
                    "sessions_since": s.sessions_since,
                    "file": str(s.path),
                }
                for s in streams
                if s.status == "parked"
            ],
            "archival_candidates": [
                {
                    "name": s.name,
                    "last_updated": s.last_updated,
                    "sessions_since": s.sessions_since,
                }
                for s in streams
                if s.is_stale_candidate or s.status == "archived"
            ],
            "thin_workstreams": [
                {"name": s.name, "body_lines": s.body_lines}
                for s in streams
                if s.is_thin
            ],
            "stale_next_candidates": stale_next,
            "blocker_prs": blocker_prs,
            "errors": errors,
            "summary": {
                "active": len([s for s in streams if s.status == "active"]),
                "parked": len([s for s in streams if s.status == "parked"]),
                "archived": len(list((workstreams_dir / "_archive").glob("*.md")))
                if (workstreams_dir / "_archive").exists()
                else 0,
                "total_files": len(streams),
            },
        }
        print(json.dumps(data, indent=2))
    else:
        target = config.budgets.parallel_workstreams_target
        pinned: frozenset[str] = frozenset()
        if target is not None:
            pinned = frozenset(latest_handoff_targets(root / "wiki" / "events"))
        print(build_tree(streams, workstreams_dir, target, pinned))
    return 0


if __name__ == "__main__":
    sys.exit(main())
