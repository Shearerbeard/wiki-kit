#!/usr/bin/env python3
"""Prepare a reviewed garden batch for the git commit user gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

SCRIPT_DIR = Path(__file__).resolve().parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from wiki_config import ConfigError, resolve_wiki_root  # noqa: E402
from wiki_event import (  # noqa: E402
    EventType,
    HandoffStatus,
    ValidationError,
    event_path,
    load_json,
    validate_event,
    validate_garden_apply_event,
)

STATE_FILE = "checkpoint.json"
MANIFEST_FILE = "path-manifest.z"
GENERATED_INGRESS = {
    "CLAUDE.local.md",
    "wiki/log.md",
    "wiki/pending/index.json",
    "wiki/pending/latest.md",
}
APPROVABLE_PREFIXES = ("workstreams/", "wiki/feedback/")
INDEX_ENTRY_KIND = "entry"
INDEX_DELETION_KIND = "deletion"


class CheckpointError(Exception):
    pass


class Change(NamedTuple):
    status: str
    path: str


class IndexRecord(NamedTuple):
    kind: str
    mode: str | None
    oid: str | None


def git(root: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = os.fsdecode(result.stderr or result.stdout).strip()
        raise CheckpointError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def parse_name_status(raw: bytes) -> list[Change]:
    fields = raw.split(b"\0")
    if fields[-1:] == [b""]:
        fields.pop()
    if len(fields) % 2:
        raise CheckpointError("git name-status output has an incomplete record")
    changes = []
    for index in range(0, len(fields), 2):
        status = os.fsdecode(fields[index])
        path = os.fsdecode(fields[index + 1])
        if status.startswith(("R", "C")):
            raise CheckpointError(
                f"rename/copy status {status} is not allowed for {path!r}"
            )
        changes.append(Change(status, path))
    return changes


def split_nul(raw: bytes) -> list[str]:
    fields = raw.split(b"\0")
    if fields[-1:] == [b""]:
        fields.pop()
    return [os.fsdecode(field) for field in fields]


def staged_changes(root: Path) -> list[Change]:
    return parse_name_status(
        git(root, "diff", "--cached", "--name-status", "-z", "--no-renames")
    )


def working_changes(root: Path) -> list[Change]:
    tracked = parse_name_status(
        git(root, "diff", "--name-status", "-z", "--no-renames")
    )
    untracked = split_nul(git(root, "ls-files", "--others", "--exclude-standard", "-z"))
    return [*tracked, *(Change("?", path) for path in untracked)]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_path(root: Path, path: Path) -> str:
    try:
        return os.fspath(path.resolve().relative_to(root.resolve()))
    except ValueError as exc:
        raise CheckpointError(f"path is outside the wiki repo: {path}") from exc


def event_snapshot(root: Path) -> dict[str, str]:
    events_dir = root / "wiki" / "events"
    snapshot: dict[str, str] = {}
    if not events_dir.exists():
        return snapshot
    for path in sorted(events_dir.rglob("*")):
        if path.is_dir():
            continue
        rel = relative_path(root, path)
        if path.is_symlink() or not path.is_file():
            raise CheckpointError(f"event store entry must be a regular file: {rel}")
        if path.suffix != ".json":
            raise CheckpointError(f"unexpected non-JSON event store entry: {rel}")
        snapshot[rel] = sha256(path)
    return snapshot


def validate_canonical_event(root: Path, rel: str) -> dict[str, Any]:
    path = root / rel
    if path.is_symlink() or not path.is_file():
        raise CheckpointError(f"event ingress must be a regular file: {rel}")
    try:
        event = load_json(path)
        event_type = event.get("event_type") if isinstance(event, dict) else None
        if event_type == EventType.HANDOFF:
            validate_event(event)
        elif event_type == EventType.GARDEN_APPLY:
            validate_garden_apply_event(event)
        else:
            raise CheckpointError(f"unknown event_type in {rel}: {event_type!r}")
    except (OSError, ValidationError, ValueError, KeyError) as exc:
        detail = " ".join(str(exc).split())
        raise CheckpointError(f"invalid event {rel}: {detail}") from exc
    expected = event_path(root / "wiki" / "events", event)
    if expected.resolve() != path.resolve():
        expected_rel = relative_path(root, expected)
        raise CheckpointError(
            f"event path is not canonical: {rel}; expected {expected_rel}"
        )
    return event


def state_path(state_dir: Path) -> Path:
    return state_dir / STATE_FILE


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        os.unlink(tmp_name)
        raise


def load_state(state_dir: Path) -> dict[str, Any]:
    try:
        state = json.loads(state_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"cannot read checkpoint state: {exc}") from exc
    if state.get("version") != 1:
        raise CheckpointError("unsupported checkpoint state version")
    return state


def save_state(state_dir: Path, state: dict[str, Any]) -> None:
    atomic_write_json(state_path(state_dir), state)


def repo_from_state(state: dict[str, Any]) -> Path:
    return Path(state["repo_root"])


def change_payload(changes: list[Change]) -> list[dict[str, str]]:
    return [{"status": change.status, "path": change.path} for change in changes]


def require_clean_index(root: Path) -> None:
    staged = staged_changes(root)
    if staged:
        paths = ", ".join(repr(change.path) for change in staged)
        raise CheckpointError(
            f"staged changes are not allowed at garden entry: {paths}; "
            "finish or unstage the existing workflow first"
        )


def classify_initial_ingress(root: Path, changes: list[Change]) -> list[str]:
    accepted = []
    for change in changes:
        if change.status == "M" and change.path in GENERATED_INGRESS:
            accepted.append(change.path)
            continue
        if change.status == "?" and change.path.startswith("wiki/events/"):
            event = validate_canonical_event(root, change.path)
            if (
                event["event_type"] != EventType.HANDOFF
                or event["status"] != HandoffStatus.PENDING_GARDEN
            ):
                raise CheckpointError(
                    f"starting event must be a pending handoff: {change.path}"
                )
            accepted.append(change.path)
            continue
        if change.path.startswith("docs/"):
            raise CheckpointError(
                f"sync-doc dirt is outside garden ingress: {change.path!r}; "
                "review and commit or restore synced docs before garden"
            )
        raise CheckpointError(
            f"unrelated starting change is not allowed: {change.status} {change.path!r}"
        )
    return sorted(set(accepted), key=os.fsencode)


def command_preflight(args: argparse.Namespace) -> None:
    root = (
        args.repo_root if args.repo_root is not None else resolve_wiki_root(args.wiki)
    ).resolve()
    state_dir = args.state_dir.resolve()
    if not (root / ".git").exists():
        git(root, "rev-parse", "--show-toplevel")
    try:
        state_dir.relative_to(root)
    except ValueError:
        pass
    else:
        raise CheckpointError("checkpoint state directory must be outside the repo")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state_path(state_dir).exists():
        path = state_path(state_dir)
        raise CheckpointError(f"checkpoint state already exists: {path}")
    require_clean_index(root)
    changes = working_changes(root)
    ingress = classify_initial_ingress(root, changes)
    state = {
        "version": 1,
        "repo_root": os.fspath(root),
        "head": os.fsdecode(git(root, "rev-parse", "HEAD")).strip(),
        "initial_status": change_payload(changes),
        "initial_ingress": ingress,
        "approved_paths": [],
        "event_baseline": event_snapshot(root),
        "phase": "preflight",
    }
    save_state(state_dir, state)
    print(f"checkpoint state: {state_dir}")
    if ingress:
        print("review expected handoff ingress, then run approve --initial:")
        for path in ingress:
            print(f"  {path!r}")
    else:
        print("starting tree is clean")


def normalize_literal_path(root: Path, raw: str) -> str:
    if "\0" in raw:
        raise CheckpointError("path contains NUL")
    path = Path(raw)
    if path.is_absolute():
        raise CheckpointError(f"approval path must be repo-relative: {raw!r}")
    normalized = os.path.normpath(raw)
    if normalized in (".", "") or normalized == ".." or normalized.startswith("../"):
        raise CheckpointError(f"approval path escapes the repo: {raw!r}")
    if normalized == ".git" or normalized.startswith(".git/"):
        raise CheckpointError("cannot approve git metadata")
    candidate = root / normalized
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise CheckpointError(f"approval path escapes the repo: {raw!r}") from exc
    return normalized


def current_change_map(root: Path) -> dict[str, Change]:
    return {change.path: change for change in working_changes(root)}


def approvable_semantic_path(root: Path, path: str, change: Change) -> None:
    if path in GENERATED_INGRESS:
        if change.status != "M":
            raise CheckpointError(
                f"generated path must remain a tracked modification: "
                f"{change.status} {path!r}"
            )
        return
    if path.startswith(APPROVABLE_PREFIXES):
        if change.status not in {"M", "D", "?"}:
            raise CheckpointError(
                f"unsupported approved status {change.status}: {path!r}"
            )
        return
    if path.startswith("wiki/events/") and change.status == "?":
        event = validate_canonical_event(root, path)
        if event["event_type"] == EventType.HANDOFF:
            raise CheckpointError(
                f"concurrent handoff appeared after preflight: {path!r}; "
                "leave it for the next garden batch"
            )
        if event["event_type"] != EventType.GARDEN_APPLY:
            raise CheckpointError(f"cannot approve event type in {path!r}")
        return
    if path.startswith("docs/"):
        raise CheckpointError(
            f"sync-doc dirt cannot join a garden checkpoint: {path!r}; "
            "use a separate reviewed commit"
        )
    raise CheckpointError(f"path is outside the garden checkpoint scope: {path!r}")


def command_approve(args: argparse.Namespace) -> None:
    state = load_state(args.state_dir)
    root = repo_from_state(state)
    if state["phase"] not in {"preflight", "approved", "unstaged"}:
        raise CheckpointError(f"cannot approve paths in phase {state['phase']!r}")
    approved = set(state["approved_paths"])
    if args.initial:
        approved.update(state["initial_ingress"])
    changes = current_change_map(root)
    for raw in args.paths:
        path = normalize_literal_path(root, raw)
        change = changes.get(path)
        if change is None:
            raise CheckpointError(f"approval path is not currently dirty: {path!r}")
        approvable_semantic_path(root, path, change)
        approved.add(path)
    if not args.initial and not args.paths:
        raise CheckpointError("approve requires --initial or at least one literal path")
    state["approved_paths"] = sorted(approved, key=os.fsencode)
    state["phase"] = "approved"
    save_state(args.state_dir, state)
    print("approved literal paths:")
    for path in state["approved_paths"]:
        print(f"  {path!r}")


def command_status(args: argparse.Namespace) -> None:
    state = load_state(args.state_dir)
    root = repo_from_state(state)
    approved = set(state["approved_paths"])
    baseline = set(state["event_baseline"])
    working = working_changes(root)
    adoption_paths = []
    for change in working:
        if (
            change.status != "?"
            or not change.path.startswith("wiki/events/")
            or change.path in baseline
        ):
            continue
        event = validate_canonical_event(root, change.path)
        if (
            event["event_type"] == EventType.HANDOFF
            and event["status"] == HandoffStatus.PENDING_GARDEN
        ):
            adoption_paths.append(change.path)
    adoption_paths.sort(key=os.fsencode)
    adoption_set = set(adoption_paths)
    changes = []
    for change in working:
        requires_adoption = change.path in adoption_set
        changes.append(
            {
                "status": change.status,
                "path": change.path,
                "approved": change.path in approved,
                "requires_adoption": requires_adoption,
                "approval_command": None
                if change.path in approved or requires_adoption
                else shlex.join(
                    [
                        "uv",
                        "run",
                        "--project",
                        os.fspath(root),
                        os.fspath(root / "scripts" / "wiki_checkpoint.py"),
                        "approve",
                        "--state-dir",
                        os.fspath(args.state_dir),
                        "--",
                        change.path,
                    ]
                ),
                "adoption_command": shlex.join(
                    [
                        "uv",
                        "run",
                        "--project",
                        os.fspath(root),
                        os.fspath(root / "scripts" / "wiki_checkpoint.py"),
                        "adopt-handoffs",
                        "--state-dir",
                        os.fspath(args.state_dir),
                        "--",
                        *adoption_paths,
                    ]
                )
                if requires_adoption
                else None,
            }
        )
    print(json.dumps({"phase": state["phase"], "changes": changes}, indent=2))


def require_head_unchanged(root: Path, state: dict[str, Any]) -> None:
    current = os.fsdecode(git(root, "rev-parse", "HEAD")).strip()
    if current != state["head"]:
        raise CheckpointError(
            f"HEAD changed during garden: started {state['head']}, now {current}; "
            "preserve the diff and ask the user how to reconcile it"
        )


def require_event_baseline(
    root: Path,
    state: dict[str, Any],
    selected_handoffs: set[str] | None = None,
) -> dict[str, str]:
    baseline = state["event_baseline"]
    current = event_snapshot(root)
    removed = set(baseline) - set(current)
    if removed:
        raise CheckpointError(f"event files disappeared: {sorted(removed)!r}")
    changed = [path for path in baseline if current[path] != baseline[path]]
    if changed:
        raise CheckpointError(f"existing immutable event files changed: {changed!r}")
    approved = set(state["approved_paths"])
    selected = selected_handoffs or set()
    observed_handoffs: set[str] = set()
    for path in sorted(set(current) - set(baseline), key=os.fsencode):
        event = validate_canonical_event(root, path)
        if event["event_type"] == EventType.HANDOFF:
            if event["status"] != HandoffStatus.PENDING_GARDEN:
                raise CheckpointError(
                    f"concurrent handoff is not pending_garden: {path!r}"
                )
            observed_handoffs.add(path)
            if path in selected:
                continue
            raise CheckpointError(
                f"concurrent handoff appeared after preflight: {path!r}; "
                "review it and run adopt-handoffs"
            )
        if event["event_type"] != EventType.GARDEN_APPLY or path not in approved:
            raise CheckpointError(
                f"new event is not an approved garden apply: {path!r}"
            )
    missing = selected - observed_handoffs
    if missing:
        raise CheckpointError(
            f"selected paths are not new pending handoffs: "
            f"{sorted(missing, key=os.fsencode)!r}"
        )
    return current


def command_adopt_handoffs(args: argparse.Namespace) -> None:
    state = load_state(args.state_dir)
    if state["phase"] not in {"preflight", "approved", "unstaged"}:
        raise CheckpointError(f"cannot adopt handoffs in phase {state['phase']!r}")
    root = repo_from_state(state)
    require_head_unchanged(root, state)
    require_clean_index(root)
    selected_paths = [normalize_literal_path(root, raw) for raw in args.paths]
    selected = set(selected_paths)
    if len(selected) != len(selected_paths):
        raise CheckpointError("adopt-handoffs received a duplicate path")
    current = require_event_baseline(root, state, selected)
    changes = current_change_map(root)
    for path in selected:
        change = changes.get(path)
        if change is None or change.status != "?":
            raise CheckpointError(f"adopted handoff must remain untracked: {path!r}")
        if sha256(root / path) != current[path]:
            raise CheckpointError(
                f"concurrent handoff changed during adoption: {path!r}"
            )

    approved = set(state["approved_paths"])
    initial_ingress = set(state["initial_ingress"])
    initial_statuses = {
        item["path"]: item["status"] for item in state["initial_status"]
    }
    adopted = dict(state.get("adopted_handoffs", {}))
    for path in selected:
        state["event_baseline"][path] = current[path]
        approved.add(path)
        initial_ingress.add(path)
        initial_statuses[path] = "?"
        adopted[path] = current[path]
    state["approved_paths"] = sorted(approved, key=os.fsencode)
    state["initial_ingress"] = sorted(initial_ingress, key=os.fsencode)
    state["initial_status"] = [
        {"status": initial_statuses[path], "path": path}
        for path in sorted(initial_statuses, key=os.fsencode)
    ]
    state["adopted_handoffs"] = {
        path: adopted[path] for path in sorted(adopted, key=os.fsencode)
    }
    state["phase"] = "approved"
    save_state(args.state_dir, state)
    print("adopted reviewed concurrent handoffs:")
    for path in sorted(selected, key=os.fsencode):
        print(f"  {path!r}")


def encoded_paths(paths: set[str] | list[str]) -> bytes:
    ordered = sorted(set(paths), key=os.fsencode)
    return b"".join(os.fsencode(path) + b"\0" for path in ordered)


def write_manifest(state_dir: Path, paths: set[str] | list[str]) -> Path:
    manifest = state_dir / MANIFEST_FILE
    manifest.write_bytes(encoded_paths(paths))
    return manifest


def staged_paths(root: Path) -> set[str]:
    return {change.path for change in staged_changes(root)}


def staged_index_records(root: Path, changes: list[Change]) -> dict[str, IndexRecord]:
    statuses: dict[str, str] = {}
    for change in changes:
        if change.path in statuses:
            raise CheckpointError(
                f"duplicate staged path is not allowed: {change.path!r}"
            )
        statuses[change.path] = change.status
    records: dict[str, IndexRecord] = {}
    for raw_record in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not raw_record:
            continue
        try:
            metadata, raw_path = raw_record.split(b"\t", 1)
            raw_mode, raw_oid, raw_stage = metadata.split(b" ")
        except ValueError as exc:
            raise CheckpointError("git index has an invalid stage record") from exc
        path = os.fsdecode(raw_path)
        if path not in statuses:
            continue
        if raw_stage != b"0":
            raise CheckpointError(f"unmerged index entry is not allowed: {path!r}")
        if path in records:
            raise CheckpointError(f"duplicate index entry is not allowed: {path!r}")
        if statuses[path] == "D":
            raise CheckpointError(
                f"staged deletion unexpectedly has a stage-0 index entry: {path!r}"
            )
        records[path] = IndexRecord(
            INDEX_ENTRY_KIND,
            os.fsdecode(raw_mode),
            os.fsdecode(raw_oid),
        )
    for path in statuses.keys() - records.keys():
        if statuses[path] != "D":
            raise CheckpointError(
                f"staged {statuses[path]} path is missing its stage-0 index entry: "
                f"{path!r}"
            )
        records[path] = IndexRecord(INDEX_DELETION_KIND, None, None)
    return records


def index_record_payload(
    records: dict[str, IndexRecord],
) -> dict[str, dict[str, str | None]]:
    return {
        path: {"kind": record.kind, "mode": record.mode, "oid": record.oid}
        for path, record in sorted(
            records.items(), key=lambda item: os.fsencode(item[0])
        )
    }


def cleanup_staged(root: Path, paths: set[str], state_dir: Path) -> None:
    if not paths:
        return
    cleanup_file = state_dir / "cleanup-paths.z"
    cleanup_file.write_bytes(encoded_paths(paths))
    git(
        root,
        "--literal-pathspecs",
        "restore",
        "--staged",
        f"--pathspec-from-file={cleanup_file}",
        "--pathspec-file-nul",
    )


def require_no_unstaged(root: Path) -> None:
    changes = working_changes(root)
    if changes:
        detail = ", ".join(f"{c.status} {c.path!r}" for c in changes)
        raise CheckpointError(f"unstaged changes remain after prepare: {detail}")


def validate_prepare_state(state: dict[str, Any]) -> tuple[Path, set[str]]:
    root = repo_from_state(state)
    require_head_unchanged(root, state)
    require_event_baseline(root, state)
    require_clean_index(root)
    changes = working_changes(root)
    approved = set(state["approved_paths"])
    actual = {change.path for change in changes}
    unexpected = actual - approved
    if unexpected:
        paths = sorted(unexpected, key=os.fsencode)
        raise CheckpointError(f"dirty paths lack recorded approval: {paths!r}")
    initial_statuses = {
        item["path"]: item["status"] for item in state["initial_status"]
    }
    for change in changes:
        if change.path in state["initial_ingress"]:
            initial_status = initial_statuses.get(change.path)
            if initial_status is None:
                raise CheckpointError(
                    f"initial ingress is missing its status record: {change.path!r}"
                )
            if change.status != initial_status:
                raise CheckpointError(
                    f"initial ingress status changed after preflight: "
                    f"{initial_status} -> {change.status} {change.path!r}"
                )
            continue
        approvable_semantic_path(root, change.path, change)
    if not actual:
        raise CheckpointError("garden checkpoint has no changes to prepare")
    return root, actual


def command_prepare(args: argparse.Namespace) -> None:
    state = load_state(args.state_dir)
    if state["phase"] not in {"preflight", "approved", "unstaged"}:
        raise CheckpointError(f"cannot prepare in phase {state['phase']!r}")
    root, actual = validate_prepare_state(state)
    manifest = write_manifest(args.state_dir, actual)
    try:
        git(
            root,
            "--literal-pathspecs",
            "add",
            f"--pathspec-from-file={manifest}",
            "--pathspec-file-nul",
        )
        staged = staged_changes(root)
        staged_path_set = {change.path for change in staged}
        if staged_path_set != actual:
            raise CheckpointError(
                f"staged paths differ from manifest: "
                f"staged={sorted(staged_path_set, key=os.fsencode)!r}, "
                f"manifest={sorted(actual, key=os.fsencode)!r}"
            )
        require_no_unstaged(root)
        index_records = staged_index_records(root, staged)
    except BaseException as exc:
        cleanup_staged(root, actual & staged_paths(root), args.state_dir)
        remaining = staged_paths(root)
        if remaining:
            raise CheckpointError(
                f"{exc}; unexpected staged paths remain after prepare cleanup: "
                f"{sorted(remaining, key=os.fsencode)!r}"
            ) from exc
        raise
    state["manifest"] = sorted(actual, key=os.fsencode)
    state["index_records"] = index_record_payload(index_records)
    state["phase"] = "prepared"
    save_state(args.state_dir, state)
    print(f"prepared {len(actual)} literal path(s): {manifest}")


def command_verify(args: argparse.Namespace) -> None:
    state = load_state(args.state_dir)
    if state["phase"] != "prepared":
        raise CheckpointError(f"cannot verify in phase {state['phase']!r}")
    root = repo_from_state(state)
    require_head_unchanged(root, state)
    require_event_baseline(root, state)
    expected = set(state["manifest"])
    staged = staged_changes(root)
    actual = {change.path for change in staged}
    if actual != expected:
        raise CheckpointError(
            f"staged paths changed after prepare: "
            f"expected={sorted(expected, key=os.fsencode)!r}, "
            f"actual={sorted(actual, key=os.fsencode)!r}"
        )
    expected_records = state.get("index_records")
    if not isinstance(expected_records, dict):
        raise CheckpointError("checkpoint has no prepared index fingerprint")
    actual_records = index_record_payload(staged_index_records(root, staged))
    if actual_records != expected_records:
        raise CheckpointError("staged index content changed after prepare")
    require_no_unstaged(root)
    manifest = args.state_dir / MANIFEST_FILE
    if manifest.read_bytes() != encoded_paths(expected):
        raise CheckpointError("checkpoint manifest changed after prepare")
    print(f"verified {len(expected)} staged literal path(s); commit remains user-gated")


def command_unstage(args: argparse.Namespace) -> None:
    state = load_state(args.state_dir)
    root = repo_from_state(state)
    manifest = set(state.get("manifest", []))
    if not manifest:
        raise CheckpointError("checkpoint has no prepared manifest to unstage")
    cleanup_staged(root, manifest & staged_paths(root), args.state_dir)
    remaining = staged_paths(root)
    if remaining:
        raise CheckpointError(
            f"unexpected staged paths remain after checkpoint unstage: "
            f"{sorted(remaining, key=os.fsencode)!r}"
        )
    state["phase"] = "unstaged"
    save_state(args.state_dir, state)
    print("checkpoint paths unstaged; working files preserved")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--state-dir", type=Path, required=True)
    preflight.add_argument("--repo-root", type=Path, default=None)
    preflight.add_argument("--wiki", type=Path, default=None)
    preflight.set_defaults(func=command_preflight)

    approve = subparsers.add_parser("approve")
    approve.add_argument("--state-dir", type=Path, required=True)
    approve.add_argument("--initial", action="store_true")
    approve.add_argument("paths", nargs="*")
    approve.set_defaults(func=command_approve)

    adopt = subparsers.add_parser("adopt-handoffs")
    adopt.add_argument("--state-dir", type=Path, required=True)
    adopt.add_argument("paths", nargs="+")
    adopt.set_defaults(func=command_adopt_handoffs)

    for name, func in (
        ("status", command_status),
        ("prepare", command_prepare),
        ("verify", command_verify),
        ("unstage", command_unstage),
    ):
        command = subparsers.add_parser(name)
        command.add_argument("--state-dir", type=Path, required=True)
        command.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (CheckpointError, ConfigError, OSError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
