#!/usr/bin/env python3
"""Garden dispatcher for wiki events."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# Sibling imports must work from every context this module is loaded in:
# CLI run (sys.path[0] is scripts/), `scripts.wiki_garden` package-style
# import (tests put the repo root on sys.path), and importlib loading.
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from wiki_config import ConfigError, resolve_wiki_root  # noqa: E402
from wiki_event import (  # noqa: E402
    SCHEMA_V1,
    SCHEMA_VERSION_LABELS,
    EventType,
    GardenApplyStatus,
    ValidationError,
    WorkstreamRelationship,
    build_pending_index,
    default_pending_dir,
    default_sources_dir,
    event_path,
    event_repo,
    load_events,
    uuid7,
    validate_event,
    validate_garden_apply_event,
    write_event,
    write_pending_files,
)
from wiki_frontmatter import (  # noqa: E402
    format_frontmatter,
    parse_frontmatter,
)
from wiki_lock import EventWriteLock, utc_timestamp  # noqa: E402

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class UnknownSchemaVersion(Exception):
    pass


class GardenApplyError(Exception):
    pass


class ManualApplyRequired(GardenApplyError):
    """A valid handoff requires human curation; no mutation occurred."""


class AlreadyDispositioned(GardenApplyError):
    """The locked store already contains a disposition; no mutation occurred."""


class DurableApplyNeedsRepair(GardenApplyError):
    """The apply is durable, but its pending projection needs repair."""

    def __init__(
        self,
        message: str,
        *,
        garden_event_path: Path,
        workstream_path: Path,
    ) -> None:
        super().__init__(message)
        self.garden_event_path = garden_event_path
        self.workstream_path = workstream_path


@dataclass(frozen=True)
class AppliedEvent:
    event_id: str
    workstream: str
    workstream_path: Path
    garden_event_path: Path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_event(
    event: dict,
    repo_root: Path | None = None,
    events_dir: Path | None = None,
    force: bool = False,
    workstream: str | None = None,
) -> AppliedEvent:
    """Validate and dispatch a handoff event by schema_version and
    workstream_state presence.

    Refuses events that are invalid, are not handoffs, or already have a
    garden-apply disposition (unless force=True). Returns the concrete paths
    and workstream changed by the apply.

    The whole apply runs under one EventWriteLock acquisition (Rank 1.6):
    disposition check, workstream read-modify-write, garden-apply event
    write, and pending rebuild. That closes both races of the unlocked
    era — two applies of the same event both passing the disposition gate
    (TOCTOU), and two applies of different events to the same workstream
    losing a session block. EventWriteLock is not re-entrant; nothing
    below this acquisition may take it again.
    """
    root: Path = repo_root if repo_root is not None else resolve_wiki_root()
    resolved_events_dir: Path = (
        events_dir if events_dir is not None else root / "wiki" / "events"
    )

    if event.get("schema_version") not in SCHEMA_VERSION_LABELS:
        raise UnknownSchemaVersion(event.get("schema_version"))
    if event.get("event_type") != EventType.HANDOFF:
        raise GardenApplyError(
            f"only {EventType.HANDOFF} events can be applied, "
            f"got event_type '{event.get('event_type')}'"
        )
    validate_event(event)

    with EventWriteLock(resolved_events_dir):
        dispositions = existing_dispositions(resolved_events_dir, event["event_id"])
        if dispositions and not force:
            summary = ", ".join(
                f"{d.get('event_id', '<no id>')} ({d.get('status', '<no status>')})"
                for d in dispositions
            )
            raise AlreadyDispositioned(
                f"event {event['event_id']} already has garden-apply "
                f"disposition(s): {summary} — pass --force to re-apply"
            )

        if "workstream_state" in event:
            return _apply_with_workstream_state(
                event,
                repo_root=root,
                events_dir=resolved_events_dir,
                workstream=workstream,
            )
        return apply_from_sources(event)


def apply_from_sources(event: dict) -> AppliedEvent:
    """Events without workstream_state need apply_from_sources, which is
    not implemented yet (vetted plan Rank 0.4: fail loudly, never exit 0)."""
    raise ManualApplyRequired(
        f"cannot apply event {event.get('event_id', '<no id>')}: it has no "
        "workstream_state and apply_from_sources is not implemented — "
        "garden it interactively or re-emit the event with workstream_state"
    )


def existing_dispositions(events_dir: Path, target_event_id: str) -> list[dict]:
    """All garden-apply events that already disposition the target event.

    Uses the shared quarantine-aware loader: quarantined dispositions do
    not count (their correcting events do), and a corrupt or unknown-type
    file in the store fails the apply loudly (ValidationError naming the
    offending file).

    apply_event calls this under the event write lock, so the
    check-then-write window of the pre-1.6 era is closed.
    """
    if not events_dir.is_dir():
        return []
    events = load_events(events_dir)
    return [
        event
        for event in events
        if event.get("event_type") == EventType.GARDEN_APPLY
        and event.get("target_event_id") == target_event_id
    ]


def _apply_with_workstream_state(
    event: dict,
    repo_root: Path | None = None,
    events_dir: Path | None = None,
    workstream: str | None = None,
) -> AppliedEvent:
    """Apply an event with workstream_state to the target workstream file.

    Private: only apply_event may call this — it runs inside apply_event's
    EventWriteLock acquisition and must not be reachable without it.

    Append + curate model (vetted plan V4): mechanical apply never rewrites
    curated sections. It only

    - updates mechanical frontmatter: last_updated (event session date),
      branch, sha
    - appends a dated, event-id-stamped block under
      "## Session updates (uncurated)" (creating the section at end of
      file if missing)

    Curated sections (Current State / Next / Blockers / What Was Done /
    Continuation Context) are rewritten only by interactive /garden with
    user approval, which then prunes absorbed session blocks.

    If the garden-apply event cannot be recorded, the workstream file is
    restored. The pending index is rebuilt so count-pending stays accurate.
    """
    root: Path = repo_root if repo_root is not None else resolve_wiki_root()
    events_dir = events_dir if events_dir is not None else root / "wiki" / "events"

    primary, is_candidate_new = _target_workstream(event, workstream)

    ws_path = root / "workstreams" / f"{primary}.md"
    created_workstream = False
    if not ws_path.exists():
        if not is_candidate_new:
            raise FileNotFoundError(f"workstream file not found: {ws_path}")
        _atomic_write(ws_path, _new_candidate_workstream(event, primary))
        created_workstream = True

    original_text = ws_path.read_text()
    fm, body = parse_frontmatter(original_text)

    # The session date comes from the event, not the apply-time wall clock;
    # the garden-apply event below records when the apply happened.
    session_date = event["timestamp_utc"][:10]

    # Update frontmatter
    repo = event_repo(event)
    fm["last_updated"] = session_date
    fm["branch"] = repo.branch
    fm["sha"] = repo.sha

    # Curated sections are never touched here; the session state lands as
    # an uncurated block for interactive /garden to absorb.
    body = _append_session_update(body, event)

    # Build and validate the garden-apply event BEFORE mutating anything.
    garden_event = {
        "schema_version": SCHEMA_V1,
        "event_id": uuid7(),
        "event_type": EventType.GARDEN_APPLY.value,
        "timestamp_utc": utc_timestamp(),
        "target_event_id": event["event_id"],
        "status": (
            GardenApplyStatus.APPLIED_MANUALLY.value
            if workstream is not None
            else GardenApplyStatus.APPLIED.value
        ),
    }
    if workstream is not None:
        garden_event["workstream"] = workstream
    validate_garden_apply_event(garden_event)

    # Atomic write of workstream file
    _atomic_write(ws_path, format_frontmatter(fm) + body)

    # Record the garden-apply event. The workstream mutation and the event
    # must land together: if the event cannot be written, restore the
    # workstream so a retry does not duplicate session-update blocks.
    # (BaseException so Ctrl-C in the window between the two file writes
    # still triggers the restore; a hard kill remains undefendable across
    # two files. The event write lock is already held by apply_event.)
    garden_event_path = event_path(events_dir, garden_event)
    try:
        write_event(events_dir, garden_event)
        events = load_events(events_dir)
        index = build_pending_index(events, default_sources_dir(events_dir))
        write_pending_files(default_pending_dir(events_dir), index)
    except BaseException as exc:
        if not garden_event_path.exists():
            if created_workstream:
                ws_path.unlink(missing_ok=True)
            else:
                _atomic_write(ws_path, original_text)
            raise
        # The apply is durable; only the derived pending index failed.
        raise DurableApplyNeedsRepair(
            f"applied event {event['event_id']} and wrote "
            f"{garden_event_path}, but the pending index rebuild failed "
            f"({exc}) — the apply stands; fix the cause and rerun "
            "wiki-event.py build-pending; do not re-apply the event",
            garden_event_path=garden_event_path,
            workstream_path=ws_path,
        ) from exc

    return AppliedEvent(
        event_id=event["event_id"],
        workstream=primary,
        workstream_path=ws_path,
        garden_event_path=garden_event_path,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _target_workstream(
    event: dict, approved_workstream: str | None = None
) -> tuple[str, bool]:
    proposed = event.get("proposed_workstreams", [])
    primary = [
        ws
        for ws in proposed
        if ws.get("relationship") == WorkstreamRelationship.PRIMARY
    ]
    if approved_workstream is not None:
        if primary:
            raise ValueError(
                "--workstream is only allowed when the event has no primary route"
            )
        matches = [ws for ws in proposed if ws.get("name") == approved_workstream]
        if len(matches) != 1:
            raise ValueError(
                f"approved workstream is not a unique event proposal: "
                f"{approved_workstream}"
            )
        chosen = matches[0]
        return approved_workstream, (
            chosen.get("relationship") == WorkstreamRelationship.CANDIDATE_NEW
            or chosen.get("proposed_action") == "candidate_new"
        )
    if primary:
        chosen = primary[0]
        return chosen["name"], chosen.get("proposed_action") == "candidate_new"

    candidates = [
        ws
        for ws in proposed
        if ws.get("relationship") == WorkstreamRelationship.CANDIDATE_NEW
    ]
    if len(candidates) == 1:
        return candidates[0]["name"], True
    if candidates:
        names = ", ".join(ws.get("name", "<unnamed>") for ws in candidates)
        raise ValueError(
            f"multiple candidate_new workstreams found: {names}; pass "
            "--workstream <name> to choose one"
        )
    # The schema requires at least one proposal, so there is always a
    # name to offer.
    names = ", ".join(ws.get("name", "<unnamed>") for ws in proposed)
    raise ValueError(
        "event has no primary workstream route; pass --workstream "
        f"<name> with one of its proposals: {names}"
    )


def _new_candidate_workstream(event: dict, name: str) -> str:
    repo = event_repo(event)
    fm = {
        "status": "active",
        "branch": repo.branch,
        "sha": repo.sha,
        "last_updated": event["timestamp_utc"][:10],
        "blocker": "",
    }
    title = name.replace("-", " ").title()
    body = (
        f"\n## {title}\n\n"
        "### Current State\n"
        "- Candidate-new workstream created by garden apply; curate the "
        "session updates below before treating this page as authoritative.\n\n"
        "### What Was Done\n"
        "None yet.\n\n"
        "### Next\n"
        "- Curate the uncurated session updates and decide whether this "
        "workstream stays separate or merges into an existing page.\n\n"
        "### Blockers\n"
        "None recorded in curated prose yet; check frontmatter and uncurated "
        "session updates until garden curation runs.\n\n"
        "### Continuation Context\n"
        "This page was created mechanically from a candidate-new handoff event.\n\n"
        f"{SESSION_UPDATES_HEADING}\n\n{SESSION_UPDATES_NOTE}\n"
    )
    return format_frontmatter(fm) + body


def _atomic_write(path: Path, text: str) -> None:
    tmp_path = path.with_suffix(".md.tmp")
    tmp_path.write_text(text)
    tmp_path.replace(path)


SESSION_UPDATES_HEADING = "## Session updates (uncurated)"
SESSION_UPDATES_NOTE = (
    "_Appended mechanically by garden apply (one block per handoff event). "
    "Interactive /garden absorbs blocks into the curated sections above "
    "with user approval, then prunes them._"
)

# (label, workstream_state key, list formatter) — order is the block layout.
_BLOCK_FIELDS = (
    ("Current state", "current_state", "bullets"),
    ("What was done", "what_was_done", "numbered"),
    ("Next", "next", "bullets"),
    ("Blockers", "blockers", "bullets"),
)


def _render_session_update(event: dict) -> str:
    ws_state = event["workstream_state"]
    date_str = event["timestamp_utc"][:10]
    repo = event_repo(event)
    lines = [
        f"### {date_str} — event {event['event_id']} ({event['tool']})",
        "",
        f"Branch: {repo.branch} @ {repo.sha}",
    ]
    for label, key, style in _BLOCK_FIELDS:
        if key not in ws_state:
            continue
        lines += ["", f"**{label}:**"]
        items = ws_state[key]
        if not items:
            # Key present but empty is a report of "none" (e.g. blockers
            # cleared), distinct from the field being absent.
            lines.append("none")
        elif style == "numbered":
            lines += [f"{i}. {item}" for i, item in enumerate(items, 1)]
        else:
            lines += [f"- {item}" for item in items]
    if ws_state.get("continuation_context"):
        lines += ["", "**Continuation context:**", ws_state["continuation_context"]]
    return "\n".join(lines)


def _find_heading_line(body: str) -> int:
    """Offset of the Session updates heading occupying a whole line, or -1.

    Line-anchored on purpose: curated prose may mention the heading inline
    (workstreams/wiki-system.md does), and a substring match would splice
    the block into curated territory.
    """
    idx = 0
    while True:
        idx = body.find(SESSION_UPDATES_HEADING, idx)
        if idx == -1:
            return -1
        at_line_start = idx == 0 or body[idx - 1] == "\n"
        rest = body[idx + len(SESSION_UPDATES_HEADING) :]
        at_line_end = rest == "" or rest.startswith("\n")
        if at_line_start and at_line_end:
            return idx
        idx += 1


def _append_session_update(body: str, event: dict) -> str:
    """Append the event's session block at the end of the Session updates
    section, creating the section at end of file if missing."""
    block = _render_session_update(event)
    idx = _find_heading_line(body)
    if idx == -1:
        return (
            body.rstrip()
            + f"\n\n{SESSION_UPDATES_HEADING}\n\n{SESSION_UPDATES_NOTE}\n\n"
            + block
            + "\n"
        )
    next_h2 = body.find("\n## ", idx + len(SESSION_UPDATES_HEADING))
    if next_h2 == -1:
        return body.rstrip() + "\n\n" + block + "\n"
    return body[:next_h2].rstrip() + "\n\n" + block + "\n\n" + body[next_h2 + 1 :]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Garden dispatcher for wiki events")
    parser.add_argument("event", type=Path, help="event JSON file to apply")
    parser.add_argument("--wiki", type=Path, default=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-apply an event that already has a garden-apply disposition",
    )
    parser.add_argument(
        "--workstream",
        help=("user-approved proposed workstream for an event with no primary route"),
    )
    args = parser.parse_args(argv)

    try:
        root = resolve_wiki_root(args.wiki)
        event = json.loads(args.event.read_text())
        result = apply_event(
            event, repo_root=root, force=args.force, workstream=args.workstream
        )
        print(f"Applied event {result.event_id} to workstream {result.workstream}")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (
        GardenApplyError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        ValidationError,
        ValueError,
        KeyError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
