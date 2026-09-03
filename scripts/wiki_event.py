#!/usr/bin/env python3
"""Create and validate wiki event records."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple, NoReturn

import jsonschema

SCRIPT_DIR = Path(__file__).resolve().parent

# Sibling imports must work from every context this module is loaded in:
# CLI run (sys.path[0] is scripts/), `scripts.wiki_event` package-style
# import (tests put the kit root on sys.path), and importlib loading.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import wiki_config  # noqa: E402
from wiki_lock import EventWriteLock, utc_timestamp  # noqa: E402, F401

# The kit's own root: the right base for the schema registry the kit
# ships, and never for wiki content paths — those come from the resolved
# wiki root (wiki_config.resolve_wiki_root) or explicit path flags.
KIT_ROOT = wiki_config.KIT_ROOT

PENDING_INDEX_FILE_NAME = "index.json"
PENDING_EVENT_COUNT_KEY = "event_count"

ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
WORKSTREAM_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ValidationError(Exception):
    pass


class EventType(StrEnum):
    HANDOFF = "handoff"
    GARDEN_APPLY = "garden-apply"


class HandoffStatus(StrEnum):
    PENDING_GARDEN = "pending_garden"
    # DEPRECATED, legacy only: pre-immutability events had status mutated in
    # place; disposition lives in garden-apply events now. Kept so the 6
    # legacy store files stay valid; pre-commit rejects it on added events.
    APPLIED = "applied"


class GardenApplyStatus(StrEnum):
    APPLIED = "applied"
    APPLIED_MANUALLY = "applied-manually"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class Tool(StrEnum):
    CLAUDE_CODE = "claude-code"
    OPENCODE = "opencode"
    CODEX = "codex"
    MANUAL = "manual"


class WorkstreamRelationship(StrEnum):
    PRIMARY = "primary"
    RELATED = "related"
    CANDIDATE_NEW = "candidate_new"
    CANDIDATE_PARENT_THEME = "candidate_parent_theme"
    SCOPE_WIDENING = "scope_widening"


SCHEMA_V1 = 1
SCHEMA_V2 = 2
SCHEMA_VERSION_LABELS = {SCHEMA_V1: "v1", SCHEMA_V2: "v2"}
# v1's envelope key; v2 renamed it to `repo` with an explicit name.
V1_ENVELOPE_KEY = "aura"
V2_ENVELOPE_KEY = "repo"
# What v1's `aura` envelope nominally described. The field actually recorded
# whichever repo the session worked in (wiki-system handoffs stored THIS
# repo's git state there) — that overload is why v2 renamed it; v1 events
# normalize to the historical name without pretending it was accurate.
V1_REPO_NAME = "aura-orchestration-mode"


def enum_values(enum_cls: type[StrEnum]) -> set[str]:
    return {member.value for member in enum_cls}


def require_schema_enum_matches(
    schema_values: set[str], enum_cls: type[StrEnum], label: str
) -> None:
    """Fail loudly when a JSON schema enum and its Python enum drift apart."""
    require(
        schema_values == enum_values(enum_cls),
        f"{label} enum drift: schema {sorted(schema_values)} "
        f"!= code {sorted(enum_values(enum_cls))}",
    )


@dataclass(frozen=True)
class SchemaEnums:
    event_types: set[str]
    tools: set[str]
    statuses: set[str]
    relationships: set[str]


@dataclass(frozen=True)
class SchemaContract:
    top_required: set[str]
    top_allowed: set[str]
    envelope_key: str  # V1_ENVELOPE_KEY (v1) or V2_ENVELOPE_KEY (v2)
    envelope_required: set[str]
    envelope_allowed: set[str]
    source_required: set[str]
    source_allowed: set[str]
    workstream_required: set[str]
    workstream_allowed: set[str]
    enums: SchemaEnums


class SchemaCache:
    """Load and resolve composed schemas from _index.json.

    Loads the index once per process, resolves $ref paths relative to the
    index file, maps schema_version to schema file, and invalidates on
    mtime change.
    """

    def __init__(self, schemas_dir: Path = KIT_ROOT / "schemas") -> None:
        self._schemas_dir = schemas_dir
        self._index_path = schemas_dir / "events" / "_index.json"
        self._index: dict[str, Any] | None = None
        self._index_mtime: float = 0.0
        self._resolved: dict[str, dict[str, Any]] = {}

    def _load_index(self) -> dict[str, Any]:
        mtime = self._index_path.stat().st_mtime
        if self._index is None or mtime > self._index_mtime:
            index = load_json(self._index_path)
            require_schema_enum_matches(
                set(index["event_types"]), EventType, "event_type registry"
            )
            self._index = index
            self._index_mtime = mtime
            self._resolved = {}  # invalidate resolved schemas when index changes
        return self._index  # type: ignore[return-value]

    def resolve_schema(
        self, event_type: str = EventType.HANDOFF, version: str | None = None
    ) -> dict[str, Any]:
        """Load a schema by event type and version, resolving all $refs."""
        index = self._load_index()
        event_entry = index["event_types"].get(event_type)
        if event_entry is None:
            raise ValidationError(f"unknown event type: {event_type}")
        if version is None:
            version = event_entry["latest"]
        schema_file = event_entry.get(version)
        if schema_file is None:
            raise ValidationError(
                f"unknown version {version} for event type {event_type}"
            )
        cache_key = f"{event_type}/{version}"
        if cache_key in self._resolved:
            return self._resolved[cache_key]
        schema_path = self._schemas_dir / schema_file
        resolved = self.resolve_schema_file(schema_path)
        self._resolved[cache_key] = resolved
        return resolved

    def resolve_schema_file(self, schema_path: Path) -> dict[str, Any]:
        """Load a schema file and inline all $refs (uncached)."""
        return self._resolve_refs(load_json(schema_path), schema_path)

    def _resolve_refs(self, schema: Any, base_path: Path) -> Any:
        """Recursively resolve $ref pointers in a schema."""
        if isinstance(schema, dict):
            if "$ref" in schema:
                ref = schema["$ref"]
                # Split file path from JSON pointer fragment
                if "#" in ref:
                    file_part, pointer = ref.split("#", 1)
                else:
                    file_part, pointer = ref, ""
                ref_path = (
                    (base_path.parent / file_part).resolve() if file_part else base_path
                )
                ref_schema = load_json(ref_path)
                # Navigate JSON pointer if present
                if pointer:
                    ref_schema = self._navigate_pointer(ref_schema, pointer)
                return self._resolve_refs(ref_schema, ref_path)
            return {
                key: self._resolve_refs(value, base_path)
                for key, value in schema.items()
            }
        if isinstance(schema, list):
            return [self._resolve_refs(item, base_path) for item in schema]
        return schema

    def _navigate_pointer(self, doc: Any, pointer: str) -> Any:
        """Navigate a JSON pointer (e.g., '/$defs/uuidv7') in a document."""
        parts = pointer.strip("/").split("/") if pointer else []
        current = doc
        for part in parts:
            # JSON pointer escaping: ~1 -> /, ~0 -> ~
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, dict):
                current = current[part]
            elif isinstance(current, list):
                current = current[int(part)]
            else:
                raise ValidationError(f"cannot navigate pointer {pointer}")
        return current


def uuid7() -> str:
    stdlib_uuid7 = getattr(uuid, "uuid7", None)
    if stdlib_uuid7 is not None:
        return str(stdlib_uuid7())

    # Stdlib uuid7 generation arrives in Python 3.14; the kit supports
    # 3.12+, so the hand-rolled RFC-9562 form covers earlier interpreters.
    timestamp_ms = int(time.time() * 1000)
    random_bits = secrets.randbits(74)
    value = (timestamp_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((random_bits >> 62) & 0x0FFF) << 64
    value |= 0b10 << 62
    value |= random_bits & ((1 << 62) - 1)
    hex_value = f"{value:032x}"
    return (
        f"{hex_value[0:8]}-{hex_value[8:12]}-{hex_value[12:16]}-"
        f"{hex_value[16:20]}-{hex_value[20:32]}"
    )


def load_json(path: Path) -> Any:
    with path.open() as f:
        return json.load(f)


# The wiki root that stored (repo-relative) paths resolve against. Set by
# the CLI once --wiki resolves, or by embedders via set_wiki_root(); when
# unset, repo_relative falls back to absolute paths — which is what
# library callers on tmp trees (the test suites) get and expect. Never
# derived from Path(__file__): that points into the kit checkout, which
# owns no content.
_wiki_root: Path | None = None


def set_wiki_root(root: Path) -> None:
    """Pin repo_relative/stored_path to a wiki root."""
    global _wiki_root
    _wiki_root = root.resolve()


def repo_relative(path: Path) -> str:
    resolved = path.resolve()
    if _wiki_root is not None:
        try:
            return str(resolved.relative_to(_wiki_root))
        except ValueError:
            pass
    return str(resolved)


def stored_path(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    require(
        _wiki_root is not None,
        f"stored path {path_text!r} is repo-relative but no wiki root is "
        "resolved; pass --wiki or run inside the wiki repo",
    )
    return _wiki_root / path


def _require_absolute_events_dir(events_dir: Path) -> None:
    """A relative --events-dir resolves its sources/pending siblings against
    the process's current directory rather than the events dir's own
    location, which is how a mistyped relative path (e.g. `--events-dir
    events` run from the repo root instead of `wiki/`) has produced stray
    top-level `events/ pending/ sources/` directories outside `wiki/` in the
    past. Requiring an absolute path makes the sibling computation below
    depend only on the given path, never on CWD. Test suites already pass
    absolute tmpdir paths (tempfile returns absolute paths), so this is not
    a behavior change for them."""
    if not events_dir.is_absolute():
        raise ValidationError(
            f"--events-dir must be an absolute path, got {events_dir!r}; "
            "a relative path resolves its sources/pending siblings against "
            "the current directory, not the events directory's location"
        )


def default_sources_dir(events_dir: Path) -> Path:
    _require_absolute_events_dir(events_dir)
    return events_dir.parent / "sources"


def default_pending_dir(events_dir: Path) -> Path:
    _require_absolute_events_dir(events_dir)
    return events_dir.parent / "pending"


# Module-level schema cache for composed schemas
_schema_cache: SchemaCache | None = None


def _get_schema_cache() -> SchemaCache:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = SchemaCache()
    return _schema_cache


def resolve_handoff_schema(version_label: str) -> dict[str, Any]:
    """The ref-free composed handoff schema from the registry."""
    return _get_schema_cache().resolve_schema(EventType.HANDOFF, version_label)


def validate_artifact(instance: Any, schema_file_name: str, label: str) -> None:
    """Validate a non-event JSON artifact (quarantine, log epoch, pending
    index, capture manifest) against its schema under the kit's schemas/.

    Event payloads go through validate_event/validate_garden_apply_event;
    this covers the config and generated-projection files around them.
    """
    schema = _get_schema_cache().resolve_schema_file(
        KIT_ROOT / "schemas" / schema_file_name
    )
    try:
        jsonschema.validate(instance=instance, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ValidationError(f"{label}: {exc}") from exc


def load_schema_contract(version_label: str) -> SchemaContract:
    """Field sets and enums extracted from the registry's handoff schema
    for the given version, drift-checked against the Python enums."""
    schema = resolve_handoff_schema(version_label)
    properties = schema["properties"]
    envelope_key = V1_ENVELOPE_KEY if V1_ENVELOPE_KEY in properties else V2_ENVELOPE_KEY
    envelope = properties[envelope_key]
    source = properties["sources"]["items"]
    workstream = properties["proposed_workstreams"]["items"]
    workstream_properties = properties["proposed_workstreams"]["items"]["properties"]
    enums = SchemaEnums(
        event_types=set(properties["event_type"]["enum"]),
        tools=set(properties["tool"]["enum"]),
        statuses=set(properties["status"]["enum"]),
        relationships=set(workstream_properties["relationship"]["enum"]),
    )
    require_schema_enum_matches(enums.statuses, HandoffStatus, "handoff status")
    require_schema_enum_matches(enums.tools, Tool, "handoff tool")
    require_schema_enum_matches(
        enums.relationships, WorkstreamRelationship, "workstream relationship"
    )
    return SchemaContract(
        top_required=set(schema["required"]),
        top_allowed=set(properties),
        envelope_key=envelope_key,
        envelope_required=set(envelope["required"]),
        envelope_allowed=set(envelope["properties"]),
        source_required=set(source["required"]),
        source_allowed=set(source["properties"]),
        workstream_required=set(workstream["required"]),
        workstream_allowed=set(workstream["properties"]),
        enums=enums,
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def require_string(value: Any, field: str) -> str:
    require(isinstance(value, str), f"{field} must be a string")
    require(bool(value), f"{field} must not be empty")
    return value


def validate_fields(
    value: dict[str, Any],
    required_fields: set[str],
    allowed_fields: set[str],
    label: str,
) -> None:
    extra_fields = set(value) - allowed_fields
    missing_fields = required_fields - set(value)
    require(not missing_fields, f"{label} missing fields: {sorted(missing_fields)}")
    require(not extra_fields, f"{label} has unknown fields: {sorted(extra_fields)}")


# Tolerated gap between an id's embedded timestamp and the validation wall
# clock. UUIDv7 ids are generated at write time, so a future-dated id means a
# hand-typed fabrication (the 2026-06-10 audit found six, decoding 2-62 days
# ahead). Backfills are unaffected: their ids postdate their declared
# timestamp_utc but never the wall clock.
UUID7_MAX_CLOCK_SKEW_MS = 60 * 60 * 1000


def uuid7_time_ms(value: uuid.UUID) -> int:
    """Unix milliseconds embedded in a UUIDv7's first 48 bits."""
    return int.from_bytes(value.bytes[:6], "big")


def validate_uuid7(value: str) -> None:
    # uuid.UUID() is case- and format-insensitive; the schema pattern is
    # canonical lowercase-hyphenated. Hold Python to the same contract.
    require(
        value == value.lower(),
        "event_id must be lowercase (canonical UUIDv7 form)",
    )
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValidationError("event_id must be a UUIDv7 string") from exc
    require(str(parsed) == value, "event_id must be canonical hyphenated form")
    require(parsed.version == 7, "event_id must be a UUIDv7 string")
    require(parsed.variant == uuid.RFC_4122, "event_id must be RFC 4122 variant")
    id_ms = uuid7_time_ms(parsed)
    if id_ms > int(time.time() * 1000) + UUID7_MAX_CLOCK_SKEW_MS:
        id_time = datetime.fromtimestamp(id_ms / 1000, UTC)
        raise ValidationError(
            f"event_id timestamp decodes to the future "
            f"({id_time:%Y-%m-%dT%H:%M:%SZ}): UUIDv7 ids are generated, "
            "never hand-written — corrections are new events"
        )


def event_datetime(event: dict[str, Any]) -> datetime:
    return datetime.strptime(event["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ")


def event_year_month(event: dict[str, Any]) -> tuple[str, str]:
    timestamp = event_datetime(event)
    return f"{timestamp:%Y}", f"{timestamp:%m}"


def event_sort_key(event: dict[str, Any]) -> tuple[str, str]:
    return event["timestamp_utc"], event["event_id"]


class RepoFacts(NamedTuple):
    name: str
    branch: str
    sha: str


def event_repo(event: dict[str, Any]) -> RepoFacts:
    """Normalized repo facts for any handoff schema version. v1 events
    carry the overloaded legacy envelope and normalize to V1_REPO_NAME
    (see that constant's caveat); v2 events name their repo explicitly."""
    if event["schema_version"] == SCHEMA_V2:
        repo = event[V2_ENVELOPE_KEY]
        return RepoFacts(repo["name"], repo["branch"], repo["sha"])
    envelope = event[V1_ENVELOPE_KEY]
    return RepoFacts(V1_REPO_NAME, envelope["branch"], envelope["sha"])


def validate_event(event: Any) -> None:
    """Validate a handoff event against the registry's composed schema for
    its declared schema_version (v1 legacy envelope, v2 `repo` envelope).

    Dispatches on schema_version, runs jsonschema (the full schema
    contract, patterns included), then the deterministic checks, whose
    remaining value is the schema/Python enum drift guards and real-date
    strictness the pattern cannot express. For garden-apply events, use
    validate_garden_apply_event() instead.
    """
    require(isinstance(event, dict), "event must be a JSON object")
    require("schema_version" in event, "event missing schema_version")
    schema_version = event["schema_version"]
    require(
        type(schema_version) is int,
        "schema_version must be an integer",
    )
    version_label = SCHEMA_VERSION_LABELS.get(schema_version)
    if version_label is None:
        raise ValidationError(
            f"schema_version must be one of {sorted(SCHEMA_VERSION_LABELS)}"
        )
    contract = load_schema_contract(version_label)
    try:
        jsonschema.validate(
            instance=event, schema=resolve_handoff_schema(version_label)
        )
    except jsonschema.ValidationError as exc:
        raise ValidationError(str(exc)) from exc
    validate_fields(event, contract.top_required, contract.top_allowed, "event")

    event_id = require_string(event["event_id"], "event_id")
    validate_uuid7(event_id)
    timestamp = require_string(event["timestamp_utc"], "timestamp_utc")
    require(bool(ISO_UTC_RE.match(timestamp)), "timestamp_utc must be ISO-8601 UTC")
    datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    event_type = require_string(event["event_type"], "event_type")
    require(
        event_type in contract.enums.event_types,
        f"invalid event_type: {event_type}",
    )
    tool = require_string(event["tool"], "tool")
    require(tool in contract.enums.tools, f"invalid tool: {tool}")
    status = require_string(event["status"], "status")
    require(status in contract.enums.statuses, f"invalid status: {status}")
    require_string(event["summary"], "summary")

    validate_envelope(event[contract.envelope_key], contract)
    validate_sources(event["sources"], contract)
    validate_proposed_workstreams(event["proposed_workstreams"], contract)
    if "workstream_state" in event:
        validate_workstream_state(event["workstream_state"])


GARDEN_APPLY_REQUIRED_FIELDS = {
    "event_id",
    "event_type",
    "schema_version",
    "timestamp_utc",
    "target_event_id",
    "status",
}
GARDEN_APPLY_ALLOWED_FIELDS = GARDEN_APPLY_REQUIRED_FIELDS | {
    "note",
    "workstream",
}


def validate_garden_apply_event(event: Any) -> None:
    """Validate a garden-apply event against the registry's schema.

    Garden-apply events have a simpler structure: event_id, event_type,
    schema_version, timestamp_utc, target_event_id, status, and optional note or
    manually selected workstream.

    The schema is resolved through SchemaCache so all $refs are inlined
    locally; jsonschema never attempts remote resolution against the
    schema's $id URL.
    """
    require(isinstance(event, dict), "event must be a JSON object")

    schema = _get_schema_cache().resolve_schema(EventType.GARDEN_APPLY, "v1")

    # Verify JSON Schema validity against the ref-free resolved schema;
    # the deterministic checks below add enum drift guards and clearer
    # error messages on top.
    try:
        jsonschema.validate(instance=event, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ValidationError(str(exc)) from exc

    # Core field checks (deterministic, always run).
    validate_fields(
        event,
        GARDEN_APPLY_REQUIRED_FIELDS,
        GARDEN_APPLY_ALLOWED_FIELDS,
        "garden-apply event",
    )
    require(
        type(event["schema_version"]) is int,
        "schema_version must be an integer",
    )
    require(event["schema_version"] == SCHEMA_V1, f"schema_version must be {SCHEMA_V1}")
    event_id = require_string(event["event_id"], "event_id")
    validate_uuid7(event_id)
    timestamp = require_string(event["timestamp_utc"], "timestamp_utc")
    require(bool(ISO_UTC_RE.match(timestamp)), "timestamp_utc must be ISO-8601 UTC")
    datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    event_type = require_string(event["event_type"], "event_type")
    require(
        event_type == EventType.GARDEN_APPLY,
        f"expected event_type '{EventType.GARDEN_APPLY}', got '{event_type}'",
    )

    target_id = require_string(event["target_event_id"], "target_event_id")
    validate_uuid7(target_id)

    schema_statuses = schema.get("properties", {}).get("status", {}).get("enum")
    require(
        isinstance(schema_statuses, list),
        "garden-apply schema is missing properties.status.enum",
    )
    require_schema_enum_matches(
        set(schema_statuses), GardenApplyStatus, "garden-apply status"
    )
    status = require_string(event["status"], "status")
    require(status in enum_values(GardenApplyStatus), f"invalid status: {status}")

    if "note" in event:
        require_string(event["note"], "note")
    if "workstream" in event:
        require_string(event["workstream"], "workstream")
        require(
            status == GardenApplyStatus.APPLIED_MANUALLY,
            "workstream is only allowed for applied-manually dispositions",
        )


def validate_envelope(envelope: Any, contract: SchemaContract) -> None:
    """The repo-facts envelope: v1 legacy {branch, sha}, v2 `repo`
    {name, branch, sha}."""
    key = contract.envelope_key
    require(isinstance(envelope, dict), f"{key} must be an object")
    validate_fields(
        envelope, contract.envelope_required, contract.envelope_allowed, key
    )
    if "name" in contract.envelope_required:
        name = require_string(envelope["name"], f"{key}.name")
        require(
            bool(WORKSTREAM_NAME_RE.match(name)),
            f"{key}.name must be a lowercase slug",
        )
    require_string(envelope["branch"], f"{key}.branch")
    sha = require_string(envelope["sha"], f"{key}.sha")
    require(bool(GIT_SHA_RE.match(sha)), f"{key}.sha must be a 7-40 char git SHA")


def validate_sources(sources: Any, contract: SchemaContract) -> None:
    require(isinstance(sources, list), "sources must be an array")
    for index, source in enumerate(sources):
        prefix = f"sources[{index}]"
        require(isinstance(source, dict), f"{prefix} must be an object")
        validate_fields(
            source,
            contract.source_required,
            contract.source_allowed,
            prefix,
        )
        require_string(source["path"], f"{prefix}.path")
        if "kind" in source:
            kind = require_string(source["kind"], f"{prefix}.kind")
            require(
                bool(SOURCE_KIND_RE.match(kind)),
                f"{prefix}.kind must be a slug",
            )
        if "sha256" in source:
            digest = require_string(source["sha256"], f"{prefix}.sha256")
            require(
                bool(SHA256_RE.match(digest)),
                f"{prefix}.sha256 must be sha256 hex",
            )


def validate_proposed_workstreams(
    workstreams: Any,
    contract: SchemaContract,
) -> None:
    require(isinstance(workstreams, list), "proposed_workstreams must be an array")
    require(bool(workstreams), "proposed_workstreams must not be empty")
    for index, workstream in enumerate(workstreams):
        prefix = f"proposed_workstreams[{index}]"
        require(isinstance(workstream, dict), f"{prefix} must be an object")
        validate_fields(
            workstream,
            contract.workstream_required,
            contract.workstream_allowed,
            prefix,
        )

        name = require_string(workstream["name"], f"{prefix}.name")
        require(bool(WORKSTREAM_NAME_RE.match(name)), f"{prefix}.name must be a slug")
        relationship = require_string(
            workstream["relationship"],
            f"{prefix}.relationship",
        )
        require(
            relationship in contract.enums.relationships,
            f"{prefix}.relationship has invalid value: {relationship}",
        )
        require_string(
            workstream["proposed_action"],
            f"{prefix}.proposed_action",
        )


PLACEHOLDER_BLOCKERS = {"none", "n/a"}


def reject_placeholder_blockers(event: dict[str, Any]) -> None:
    """Write-time strictness, deliberately NOT in load-path validation: two
    legacy store events (019e9a42, 019e9a50) carry a literal 'None' blocker
    and must stay loadable. Same pattern as the deprecated 'applied' status:
    new-handoff refuses to write placeholders, pre-commit rejects them on
    added events. An empty blockers list already means "no blockers"."""
    blockers = event.get("workstream_state", {}).get("blockers", [])
    for index, item in enumerate(blockers):
        require(
            item.strip().lower() not in PLACEHOLDER_BLOCKERS,
            f"workstream_state.blockers[{index}] is a placeholder "
            f'({item!r}) — pass no --blocker at all for "no blockers"',
        )


def validate_workstream_state(ws: Any) -> None:
    """Validate the optional workstream_state fragment.

    Called only if the key is present. Does NOT fail if absent — that's the
    Phase B/C/D compatibility path.
    """
    require(isinstance(ws, dict), "workstream_state must be an object")

    ws_fields = {
        "current_state",
        "what_was_done",
        "next",
        "blockers",
        "continuation_context",
    }
    extra = set(ws) - ws_fields
    require(not extra, f"workstream_state has unknown fields: {sorted(extra)}")

    for field in ("current_state", "what_was_done", "next", "blockers"):
        if field in ws:
            require(
                isinstance(ws[field], list),
                f"workstream_state.{field} must be an array",
            )
            for index, item in enumerate(ws[field]):
                require(
                    isinstance(item, str),
                    f"workstream_state.{field}[{index}] must be a string",
                )
                require(
                    bool(item), f"workstream_state.{field}[{index}] must not be empty"
                )

    if "continuation_context" in ws:
        require_string(
            ws["continuation_context"], "workstream_state.continuation_context"
        )


def parse_source(value: str) -> dict[str, str]:
    if "=" not in value:
        return {"path": value}
    kind, _, path = value.partition("=")
    require(bool(kind), "source kind must not be empty")
    require(bool(path), "source path must not be empty")
    return {"kind": kind, "path": path}


def parse_capture_source(value: str) -> dict[str, str]:
    source = parse_source(value)
    source.setdefault("kind", "file")
    kind = source["kind"]
    require(bool(SOURCE_KIND_RE.match(kind)), "source kind must be a slug")
    return source


def parse_workstream(value: str) -> dict[str, str]:
    parts = value.split(":")
    require(len(parts) in {2, 3}, "workstream must be NAME:RELATIONSHIP[:ACTION]")
    name, relationship = parts[0], parts[1]
    action = parts[2] if len(parts) == 3 else "needs_review"
    return {"name": name, "relationship": relationship, "proposed_action": action}


def repo_facts_from_git(checkout: Path) -> tuple[str, str]:
    """Branch and full sha of a checkout, read from git itself so the
    event's identity cannot be mistyped."""

    def rev_parse(*flags: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", *flags],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(
                f"--repo-from-git {checkout}: git rev-parse {' '.join(flags)} "
                f"failed: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    branch = rev_parse("--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise ValueError(
            f"--repo-from-git {checkout}: detached HEAD has no branch; "
            "pass --repo-branch"
        )
    return branch, rev_parse("HEAD")


def handoff_repo_identity(args: argparse.Namespace) -> tuple[str, str]:
    branch, sha = args.repo_branch, args.repo_sha
    checkout = getattr(args, "repo_from_git", None)
    if checkout is not None:
        git_branch, git_sha = repo_facts_from_git(checkout)
        branch = branch or git_branch
        sha = sha or git_sha
    if branch is None or sha is None:
        raise ValueError(
            "new-handoff needs --repo-branch and --repo-sha, or "
            "--repo-from-git <checkout> to derive them"
        )
    return branch, sha


def build_handoff_event(args: argparse.Namespace) -> dict[str, Any]:
    repo_branch, repo_sha = handoff_repo_identity(args)
    timestamp = args.timestamp if args.timestamp else utc_timestamp()
    if args.timestamp:
        require(
            bool(ISO_UTC_RE.match(args.timestamp)), "--timestamp must be ISO-8601 UTC"
        )
        datetime.strptime(args.timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    event: dict[str, Any] = {
        "schema_version": SCHEMA_V2,
        "event_id": uuid7(),
        "event_type": EventType.HANDOFF.value,
        "timestamp_utc": timestamp,
        "tool": args.tool,
        "repo": {
            "name": args.repo_name,
            "branch": repo_branch,
            "sha": repo_sha,
        },
        "sources": [parse_source(value) for value in args.source],
        "proposed_workstreams": [parse_workstream(value) for value in args.workstream],
        "summary": args.summary,
        "status": HandoffStatus.PENDING_GARDEN.value,
    }

    ws = _build_workstream_state(args)
    if ws is not None:
        event["workstream_state"] = ws

    return event


def _build_workstream_state(args: argparse.Namespace) -> dict[str, Any] | None:
    if not any(
        [
            args.current_state,
            args.what_was_done,
            args.next,
            args.blocker,
            args.continuation_context,
        ]
    ):
        return None
    ws: dict[str, Any] = {}
    if args.current_state:
        ws["current_state"] = args.current_state
    if args.what_was_done:
        ws["what_was_done"] = args.what_was_done
    if args.next:
        ws["next"] = args.next
    # CLI arg is singular (--blocker, one item per flag); schema field is plural.
    if args.blocker:
        ws["blockers"] = args.blocker
    if args.continuation_context:
        ws["continuation_context"] = args.continuation_context
    return ws


def event_path(events_dir: Path, event: dict[str, Any]) -> Path:
    timestamp = event_datetime(event)
    return (
        events_dir / f"{timestamp:%Y}" / f"{timestamp:%m}" / f"{event['event_id']}.json"
    )


def write_event(events_dir: Path, event: dict[str, Any]) -> Path:
    path = event_path(events_dir, event)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"event already exists: {path}")
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(event, indent=2, sort_keys=True) + "\n")
    tmp_path.replace(path)
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_manifest_path(sources_dir: Path, event: dict[str, Any], kind: str) -> Path:
    year, month = event_year_month(event)
    return sources_dir / kind / year / month / event["event_id"] / "manifest.json"


def capture_file_path(manifest_path: Path, index: int, source_path: Path) -> Path:
    return manifest_path.parent / "files" / f"{index:04d}-{source_path.name}"


def build_capture_manifest(
    event_path_arg: Path,
    event: dict[str, Any],
    sources_dir: Path,
    sources: list[dict[str, str]],
) -> tuple[Path, dict[str, Any]]:
    require(bool(sources), "capture-sources requires at least one --source")
    kinds = {source["kind"] for source in sources}
    require(
        len(kinds) == 1,
        "capture-sources currently supports one source kind per run",
    )
    kind = next(iter(kinds))
    manifest_path = capture_manifest_path(sources_dir, event, kind)

    captures = []
    for index, source in enumerate(sources, start=1):
        source_path = Path(source["path"]).expanduser()
        require(source_path.is_file(), f"source path is not a file: {source_path}")
        digest = sha256_file(source_path)
        captured_path = capture_file_path(manifest_path, index, source_path)
        captures.append(
            {
                "kind": kind,
                "source_path": str(source_path),
                "captured_path": repo_relative(captured_path),
                "sha256": digest,
                "size_bytes": source_path.stat().st_size,
            }
        )

    manifest = {
        "schema_version": 1,
        "event_id": event["event_id"],
        "event_path": repo_relative(event_path_arg),
        "captured_at_utc": utc_timestamp(),
        "captures": captures,
    }
    return manifest_path, manifest


def ensure_existing_manifest_matches(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> bool:
    if not manifest_path.exists():
        return False
    existing = load_json(manifest_path)
    comparable_existing = {
        "schema_version": existing["schema_version"],
        "event_id": existing["event_id"],
        "event_path": existing["event_path"],
        "captures": existing["captures"],
    }
    comparable_new = {
        "schema_version": manifest["schema_version"],
        "event_id": manifest["event_id"],
        "event_path": manifest["event_path"],
        "captures": manifest["captures"],
    }
    require(
        comparable_existing == comparable_new,
        f"capture manifest already exists with different content: {manifest_path}",
    )
    verify_capture_files(existing)
    return True


def verify_capture_files(manifest: dict[str, Any]) -> None:
    for capture in manifest["captures"]:
        captured_path = stored_path(capture["captured_path"])
        require(captured_path.is_file(), f"captured file missing: {captured_path}")
        actual_digest = sha256_file(captured_path)
        require(
            actual_digest == capture["sha256"],
            f"captured file sha256 mismatch: {captured_path}",
        )
        sidecar_path = captured_path.with_suffix(captured_path.suffix + ".sha256")
        require(sidecar_path.is_file(), f"sha256 sidecar missing: {sidecar_path}")
        sidecar_digest = sidecar_path.read_text().strip()
        require(
            sidecar_digest == capture["sha256"],
            f"sha256 sidecar mismatch: {sidecar_path}",
        )


def write_capture_manifest(
    manifest_path: Path,
    manifest: dict[str, Any],
    sources: list[dict[str, str]],
) -> Path:
    validate_artifact(
        manifest, "capture-manifest.schema.json", repo_relative(manifest_path)
    )
    if ensure_existing_manifest_matches(manifest_path, manifest):
        return manifest_path

    files_dir = manifest_path.parent / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    for index, source in enumerate(sources, start=1):
        source_path = Path(source["path"]).expanduser()
        captured_path = capture_file_path(manifest_path, index, source_path)
        captured_path.write_bytes(source_path.read_bytes())
        (captured_path.with_suffix(captured_path.suffix + ".sha256")).write_text(
            sha256_file(source_path) + "\n"
        )

    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest_path


def event_files(events_dir: Path) -> list[Path]:
    return sorted(events_dir.glob("**/*.json"))


def default_quarantine_path(events_dir: Path) -> Path:
    _require_absolute_events_dir(events_dir)
    return events_dir.parent / "quarantine.json"


def load_quarantine(quarantine_path: Path) -> list[dict[str, str]]:
    """Validated quarantine entries: known-corrupt store files that pre-commit
    immutability forbids deleting, excluded from projections by path. Each
    must name a correcting event (the correction-event pattern); load_events
    verifies the correction exists. Absent file means an empty quarantine."""
    if not quarantine_path.exists():
        return []
    data = load_json(quarantine_path)
    validate_artifact(data, "quarantine.schema.json", str(quarantine_path))
    entries = []
    seen_ids: set[str] = set()
    for entry in data["quarantined"]:
        # Cross-field rules the schema cannot express.
        if Path(entry["path"]).name != f"{entry['event_id']}.json":
            raise ValidationError(
                f"{quarantine_path}: path {entry['path']!r} does not match "
                f"event_id {entry['event_id']!r}"
            )
        if entry["event_id"] in seen_ids:
            raise ValidationError(
                f"{quarantine_path}: duplicate event_id {entry['event_id']!r}"
            )
        seen_ids.add(entry["event_id"])
        entries.append(entry)
    return entries


def load_events(
    events_dir: Path,
    quarantine_path: Path | None = None,
) -> list[dict[str, Any]]:
    """The one event loader: all events sorted by (timestamp_utc, event_id),
    validating both handoff and garden-apply types. Unknown event types fail
    loudly. Quarantined files are skipped by path before parsing (so any
    event type can be quarantined); a quarantined file missing from the store
    fails loudly, and every corrected_by id must exist in the loaded store —
    quarantine acknowledges corruption, it never hides loss.

    quarantine_path defaults to <events_dir>/../quarantine.json, matching
    the wiki/ layout; an absent file is an empty quarantine."""
    if not events_dir.is_dir():
        raise ValidationError(f"events directory {events_dir} does not exist")
    if quarantine_path is None:
        quarantine_path = default_quarantine_path(events_dir)
    quarantine = load_quarantine(quarantine_path)
    quarantined_names = {Path(entry["path"]).name for entry in quarantine}
    seen_names: set[str] = set()
    events = []
    for path in event_files(events_dir):
        if path.name in quarantined_names:
            seen_names.add(path.name)
            continue
        try:
            event = load_json(path)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"{repo_relative(path)}: not valid JSON ({exc})"
            ) from exc
        event_type = event.get("event_type") if isinstance(event, dict) else None
        if event_type == EventType.HANDOFF:
            validate_event(event)
        elif event_type == EventType.GARDEN_APPLY:
            validate_garden_apply_event(event)
        else:
            raise ValidationError(
                f"{repo_relative(path)}: unknown event_type {event_type!r}"
            )
        event["_path"] = repo_relative(path)
        events.append(event)
    missing = quarantined_names - seen_names
    if missing:
        raise ValidationError(
            f"quarantined file(s) missing from {events_dir}: "
            f"{', '.join(sorted(missing))}"
        )
    loaded_ids = {event["event_id"] for event in events}
    broken = [
        entry["corrected_by"]
        for entry in quarantine
        if entry["corrected_by"] not in loaded_ids
    ]
    if broken:
        raise ValidationError(
            "quarantine corrected_by event(s) not present in the store: "
            f"{', '.join(sorted(broken))} — a quarantine entry must come "
            "with a real correcting event"
        )
    return sorted(events, key=event_sort_key)


def capture_manifests_for_event(sources_dir: Path, event_id: str) -> list[str]:
    manifests = sorted(sources_dir.glob(f"*/????/??/{event_id}/manifest.json"))
    return [repo_relative(path) for path in manifests]


def pending_questions(event: dict[str, Any]) -> list[str]:
    questions = []
    for workstream in event["proposed_workstreams"]:
        relationship = workstream["relationship"]
        if relationship == "primary":
            continue
        questions.append(f"Review {workstream['name']} relationship: {relationship}.")
    return questions


def build_pending_index(
    events: list[dict[str, Any]],
    sources_dir: Path,
) -> dict[str, Any]:
    applied_ids: set[str] = {
        event["target_event_id"]
        for event in events
        if event.get("event_type") == EventType.GARDEN_APPLY
        and "target_event_id" in event
    }
    pending_events = [
        event
        for event in events
        if event.get("status") == HandoffStatus.PENDING_GARDEN
        and event["event_id"] not in applied_ids
    ]
    return {
        "schema_version": 2,
        "generated_at_utc": utc_timestamp(),
        "event_count": len(pending_events),
        "events": [
            {
                "event_id": event["event_id"],
                "timestamp_utc": event["timestamp_utc"],
                "event_path": event["_path"],
                "summary": event["summary"],
                "tool": event["tool"],
                # Normalized for both schema versions; the index is a
                # rebuildable projection, so its shape tracks the latest.
                "repo": event_repo(event)._asdict(),
                "status": event["status"],
                "review_status": "unreviewed",
                "proposed_workstreams": event["proposed_workstreams"],
                "sources": event["sources"],
                "capture_manifests": capture_manifests_for_event(
                    sources_dir,
                    event["event_id"],
                ),
                "questions": pending_questions(event),
            }
            for event in pending_events
        ],
    }


def render_pending_latest(index: dict[str, Any]) -> str:
    lines = [
        "# Pending wiki events",
        "",
        f"Generated: {index['generated_at_utc']}",
        f"Pending events: {index['event_count']}",
        "",
        "Treat this file as provisional. Garden/librarian review has not applied ",
        "these events to curated workstreams or generated views.",
        "",
    ]
    for event in index["events"]:
        lines.extend(
            [
                f"## {event['timestamp_utc']} — {event['summary']}",
                "",
                f"- Event: `{event['event_id']}`",
                f"- Tool: `{event['tool']}`",
                f"- Repo: `{event['repo']['name']}` "
                f"`{event['repo']['branch']}` @ `{event['repo']['sha']}`",
                f"- Event file: `{event['event_path']}`",
                "- Proposed workstreams:",
            ]
        )
        for workstream in event["proposed_workstreams"]:
            lines.append(
                f"  - `{workstream['name']}`: {workstream['relationship']} "
                f"({workstream['proposed_action']})"
            )
        if event["capture_manifests"]:
            lines.append("- Capture manifests:")
            for manifest in event["capture_manifests"]:
                lines.append(f"  - `{manifest}`")
        if event["questions"]:
            lines.append("- Questions:")
            for question in event["questions"]:
                lines.append(f"  - {question}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_pending_files(pending_dir: Path, index: dict[str, Any]) -> tuple[Path, Path]:
    """Write the pending index pair with per-file atomic tmp+rename.

    Each file is atomic on its own; the PAIR is not (a reader can see a new
    index.json next to the old latest.md). Acceptable by design: count-pending
    reads only index.json, agents read only latest.md — no reader needs
    cross-file consistency, and both are rebuildable projections.
    """
    validate_artifact(index, "pending-index.schema.json", "pending index")
    pending_dir.mkdir(parents=True, exist_ok=True)
    index_path = pending_dir / "index.json"
    latest_path = pending_dir / "latest.md"
    for path, text in (
        (index_path, json.dumps(index, indent=2, sort_keys=True) + "\n"),
        (latest_path, render_pending_latest(index)),
    ):
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(text)
        tmp_path.replace(path)
    return index_path, latest_path


def pending_mismatch(
    events_dir: Path,
    sources_dir: Path,
    index_path: Path,
    latest_path: Path,
) -> list[str]:
    """Human-readable pending-projection mismatches; empty means the pair
    matches the store. The one shared implementation of commit-time and
    doctor coverage for the pending projection — the pre-commit hook and
    the doctor's pending check both call this instead of re-deriving the
    comparison.

    The store is authoritative: events load, the expected index rebuilds,
    and generated_at_utc neutralizes to the current file's value so only
    real drift counts; latest.md compares against the render of that
    neutralized index. A missing or unparseable projection file is a
    mismatch; a broken STORE (invalid events) raises instead — corruption
    is not staleness."""
    expected = rebuild_pending_index(events_dir, sources_dir)
    mismatches: list[str] = []
    current = None
    try:
        current = load_json(index_path)
    except FileNotFoundError:
        mismatches.append(f"{index_path}: missing; run wiki-event.py build-pending")
    except json.JSONDecodeError as exc:
        mismatches.append(f"{index_path}: not valid JSON ({exc})")
    if current is not None:
        if isinstance(current, dict):
            expected["generated_at_utc"] = current.get("generated_at_utc")
        if current != expected:
            mismatches.append(
                f"{index_path}: pending index differs from the event store; "
                "run wiki-event.py build-pending"
            )
    expected_latest = render_pending_latest(expected)
    try:
        current_latest = latest_path.read_text()
    except FileNotFoundError:
        mismatches.append(f"{latest_path}: missing; run wiki-event.py build-pending")
    else:
        if current_latest != expected_latest:
            mismatches.append(
                f"{latest_path}: pending latest.md differs from the event "
                "store; run wiki-event.py build-pending"
            )
    return mismatches


def _validate_event_file(path: Path) -> None:
    """Dispatch validation by event type; schemas come from the registry."""
    event = load_json(path)
    event_type = event.get("event_type", "")
    if event_type == EventType.GARDEN_APPLY:
        validate_garden_apply_event(event)
    elif event_type == EventType.HANDOFF:
        validate_event(event)
    else:
        raise ValueError(f"unknown event_type '{event_type}', no schema available")


def _validate_store(args: argparse.Namespace) -> Path:
    """The store validate resolves ids and --all against; derived from the
    wiki only when a path argument does not settle the invocation."""
    events_dir = getattr(args, "events_dir", None)
    if events_dir is None:
        events_dir = _wiki_root_for(args) / "wiki" / "events"
    return events_dir


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        if args.all:
            if args.event is not None:
                raise ValueError(
                    "--all validates the whole store; drop the event argument"
                )
            events_dir = _validate_store(args)
            paths = event_files(events_dir)
            if not paths:
                raise ValueError(f"no events under {events_dir}")
            for path in paths:
                try:
                    _validate_event_file(path)
                except (
                    json.JSONDecodeError,
                    KeyError,
                    ValidationError,
                    ValueError,
                ) as exc:
                    raise ValueError(f"{path}: {exc}") from exc
                print(f"valid: {path}")
            return 0
        if args.event is None:
            raise ValueError(
                "pass an event JSON path or a bare event id, or --all for the store"
            )
        path = args.event
        if not path.is_file():
            path = resolve_event_arg(_validate_store(args), path)
        _validate_event_file(path)
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        ValidationError,
        ValueError,
        wiki_config.ConfigError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"valid: {path}")
    return 0


def cmd_new_handoff(args: argparse.Namespace) -> int:
    try:
        event = build_handoff_event(args)
        validate_event(event)
        reject_placeholder_blockers(event)
        sources_dir = args.sources_dir or default_sources_dir(args.events_dir)
        pending_dir = args.pending_dir or default_pending_dir(args.events_dir)
        with EventWriteLock(args.events_dir):
            path = write_event(args.events_dir, event)
            events = load_events(args.events_dir)
            index = build_pending_index(events, sources_dir)
            write_pending_files(pending_dir, index)
    except (OSError, RuntimeError, ValidationError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(path)
    # stdout stays the bare event path callers parse; the remaining steps
    # of a handoff go to stderr, since the store write leaves the log
    # projection stale until the renderer runs.
    print(
        "next: render the log (scripts/wiki-render.py log) and commit the "
        "wiki; the handoff skill lists the full sequence",
        file=sys.stderr,
    )
    return 0


def resolve_event_arg(events_dir: Path, arg: Path) -> Path:
    """An --event value is an event JSON path or a bare event id; the id
    resolves against the store so callers need not know its year/month
    layout (the form `status` already takes)."""
    if arg.is_file():
        return arg
    matches = [path for path in event_files(events_dir) if path.stem == str(arg)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        listed = ", ".join(str(path) for path in matches)
        raise ValueError(
            f"event id {arg} is ambiguous: {len(matches)} files under "
            f"{events_dir} carry it ({listed}); pass the path"
        )
    raise ValueError(
        f"--event must be an event JSON path or an event id stored under "
        f"{events_dir}; {arg} is neither"
    )


def cmd_capture_sources(args: argparse.Namespace) -> int:
    try:
        event_arg = resolve_event_arg(args.events_dir, args.event)
        event = load_json(event_arg)
        validate_event(event)
        sources = [parse_capture_source(value) for value in args.source]
        # Manifests feed build_pending_index, so manifest writes serialize
        # with every other store writer under the same lock.
        with EventWriteLock(args.events_dir):
            manifest_path, manifest = build_capture_manifest(
                event_arg,
                event,
                args.sources_dir,
                sources,
            )
            path = write_capture_manifest(manifest_path, manifest, sources)
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        RuntimeError,
        ValidationError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


def cmd_count_pending(args: argparse.Namespace) -> int:
    try:
        index = load_verified_pending_index(
            args.events_dir,
            pending_dir=args.pending_dir,
            sources_dir=args.sources_dir,
        )
        event_count = index[PENDING_EVENT_COUNT_KEY]
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        TypeError,
        ValidationError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(event_count)
    return 0


def load_verified_pending_index(
    events_dir: Path,
    *,
    pending_dir: Path | None = None,
    sources_dir: Path | None = None,
) -> dict[str, Any]:
    """Load the pending projection only when it matches the validated store."""
    resolved_pending_dir = pending_dir or default_pending_dir(events_dir)
    resolved_sources_dir = sources_dir or default_sources_dir(events_dir)
    index_path = resolved_pending_dir / PENDING_INDEX_FILE_NAME
    current = load_json(index_path)
    validate_artifact(current, "pending-index.schema.json", str(index_path))
    expected = rebuild_pending_index(events_dir, resolved_sources_dir)
    expected["generated_at_utc"] = current["generated_at_utc"]
    if current != expected:
        raise ValidationError(
            f"{index_path}: pending projection differs from the event store; "
            "run wiki-event.py build-pending"
        )
    return current


def cmd_new_garden_apply(args: argparse.Namespace) -> int:
    """Write a validated manual/recovery disposition and rebuild pending."""
    garden_event = {
        "schema_version": SCHEMA_V1,
        "event_id": uuid7(),
        "event_type": EventType.GARDEN_APPLY.value,
        "timestamp_utc": utc_timestamp(),
        "target_event_id": args.target,
        "status": args.status,
    }
    if args.note is not None:
        note = args.note.strip()
        if not note:
            print("error: --note must not be empty", file=sys.stderr)
            return 1
        garden_event["note"] = note

    path: Path | None = None
    try:
        validate_garden_apply_event(garden_event)
        sources_dir = args.sources_dir or default_sources_dir(args.events_dir)
        pending_dir = args.pending_dir or default_pending_dir(args.events_dir)
        with EventWriteLock(args.events_dir):
            events = load_events(args.events_dir)
            target = next(
                (event for event in events if event["event_id"] == args.target),
                None,
            )
            if target is None:
                raise ValidationError(
                    f"target handoff event {args.target} does not exist"
                )
            if target["event_type"] != EventType.HANDOFF:
                raise ValidationError(
                    f"target {args.target} is {target['event_type']}, not a handoff"
                )
            path = write_event(args.events_dir, garden_event)
            try:
                updated_events = load_events(args.events_dir)
                index = build_pending_index(updated_events, sources_dir)
                write_pending_files(pending_dir, index)
            except BaseException as exc:
                raise RuntimeError(
                    f"wrote {path}, but pending rebuild failed ({exc}); "
                    "the disposition stands — run wiki-event.py build-pending"
                ) from exc
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        RuntimeError,
        ValidationError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


def cmd_sync_docs(args: argparse.Namespace) -> int:
    """Sync the companion repo's docs subtree into the wiki's docs dir.

    The companion is the source of truth for these files; the wiki keeps a
    synced copy. The companion's identity and docs_subpath come from
    wiki.toml, its machine-local checkout path from the overlay — no env
    vars, no guessed layouts.
    """
    try:
        root = _wiki_root_for(args)
        config = wiki_config.load_config(root)
        companion = config.companion(args.companion)
        if companion.path is None:
            raise wiki_config.ConfigError(
                f"companion {companion.name!r} has no path in "
                f"{wiki_config.OVERLAY_FILE_NAME} on this machine; add "
                f"[companions.{companion.name}] path = ... to the overlay"
            )
        if companion.docs_subpath is None:
            raise wiki_config.ConfigError(
                f"companion {companion.name!r} has no docs_subpath in "
                f"wiki.toml; sync-docs needs "
                f"[companions.{companion.name}].docs_subpath"
            )
        src = companion.path / companion.docs_subpath
        dst = args.docs_dir or (root / "docs")
        if not src.is_dir():
            print(f"error: source docs dir not found: {src}", file=sys.stderr)
            return 1
        shutil.copytree(src, dst, dirs_exist_ok=True)
    except (OSError, shutil.Error, wiki_config.ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    copied = sum(1 for path in src.rglob("*") if path.is_file())
    print(f"synced {copied} files: {src} -> {dst}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Answer "what happened to this event" from the store itself: quarantine
    state, declared status (flagging the deprecated legacy 'applied'), and
    the join-derived disposition — the latest garden-apply event targeting a
    handoff is its truth, never its status field."""
    try:
        quarantine = load_quarantine(default_quarantine_path(args.events_dir))
        events = load_events(args.events_dir)
    except (json.JSONDecodeError, OSError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    event_id = args.event_id
    quarantined = next(
        (entry for entry in quarantine if entry["event_id"] == event_id), None
    )
    if quarantined is not None:
        print(f"event:    {event_id}")
        print("state:    QUARANTINED — excluded from all projections")
        print(f"file:     {quarantined['path']}")
        print(f"reason:   {quarantined['reason']}")
        print(f"corrected_by: {quarantined['corrected_by']}")
        return 0

    event = next((e for e in events if e["event_id"] == event_id), None)
    if event is None:
        print(
            f"error: event {event_id} not found in the store or quarantine",
            file=sys.stderr,
        )
        return 1

    print(f"event:    {event_id}")
    print(f"type:     {event['event_type']}")
    print(f"time:     {event['timestamp_utc']}")
    print(f"file:     {event['_path']}")

    if event["event_type"] == EventType.GARDEN_APPLY:
        print(f"status:   {event['status']}")
        print(f"target:   {event['target_event_id']}")
        if "workstream" in event:
            print(f"workstream: {event['workstream']}")
        if "note" in event:
            print(f"note:     {event['note']}")
        return 0

    declared = event["status"]
    if declared == HandoffStatus.APPLIED:
        declared += (
            "  [DEPRECATED legacy value, mutated in place pre-immutability "
            "— truth is the garden-apply join below]"
        )
    print(f"summary:  {event['summary']}")
    print(f"declared: {declared}")

    dispositions = sorted(
        (
            e
            for e in events
            if e["event_type"] == EventType.GARDEN_APPLY
            and e["target_event_id"] == event_id
        ),
        key=event_sort_key,
    )
    if not dispositions:
        print(
            "disposition: pending garden — no garden-apply event targets this handoff"
        )
        return 0
    print("dispositions (latest wins):")
    for disposition in dispositions:
        marker = (
            "  <- join-derived disposition" if disposition is dispositions[-1] else ""
        )
        print(
            f"  {disposition['timestamp_utc']} {disposition['status']} "
            f"(event {disposition['event_id']}){marker}"
        )
    return 0


def cmd_build_pending(args: argparse.Namespace) -> int:
    try:
        sources_dir = args.sources_dir or default_sources_dir(args.events_dir)
        pending_dir = args.pending_dir or default_pending_dir(args.events_dir)
        with EventWriteLock(args.events_dir):
            events = load_events(args.events_dir)
            index = build_pending_index(events, sources_dir)
            index_path, latest_path = write_pending_files(pending_dir, index)
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        RuntimeError,
        ValidationError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(index_path)
    print(latest_path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create and validate wiki events")
    parser.set_defaults(func=missing_command)
    # Shared by every subcommand: the wiki repo root. Content-path flags
    # left unset derive from it; fully explicit invocations never resolve
    # it (the test suites run on tmp trees with no wiki.toml anywhere).
    wiki_parent = argparse.ArgumentParser(add_help=False)
    wiki_parent.add_argument(
        "--wiki",
        type=Path,
        default=None,
        help="wiki repo root (default: walk up from cwd to wiki.toml)",
    )
    subparsers = parser.add_subparsers(dest="command")

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate one event (by path or bare id) or the whole store (--all)",
        parents=[wiki_parent],
    )
    validate_parser.add_argument(
        "event",
        type=Path,
        nargs="?",
        help="event JSON path, or a bare event id resolved against the store",
    )
    validate_parser.add_argument(
        "--all", action="store_true", help="validate every event in the store"
    )
    validate_parser.add_argument(
        "--events-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="event store (default: <wiki>/wiki/events)",
    )
    validate_parser.set_defaults(func=cmd_validate)

    status_parser = subparsers.add_parser(
        "status",
        help="show an event's quarantine state and join-derived disposition",
        parents=[wiki_parent],
    )
    status_parser.add_argument("event_id")
    status_parser.add_argument(
        "--events-dir",
        type=Path,
        default=None,
        help="event store (default: <wiki>/wiki/events)",
    )
    status_parser.set_defaults(func=cmd_status)

    garden_apply_parser = subparsers.add_parser(
        "new-garden-apply",
        help=(
            "record a handoff disposition without applying it (applied-manually, "
            "rejected, superseded); wiki-garden.py is the apply"
        ),
        parents=[wiki_parent],
    )
    garden_apply_parser.add_argument("--target", required=True)
    garden_apply_parser.add_argument(
        "--status",
        required=True,
        choices=[
            GardenApplyStatus.APPLIED_MANUALLY.value,
            GardenApplyStatus.REJECTED.value,
            GardenApplyStatus.SUPERSEDED.value,
        ],
    )
    garden_apply_parser.add_argument("--note")
    garden_apply_parser.add_argument(
        "--events-dir",
        type=Path,
        default=None,
        help="event store (default: <wiki>/wiki/events)",
    )
    garden_apply_parser.add_argument("--sources-dir", type=Path)
    garden_apply_parser.add_argument("--pending-dir", type=Path)
    garden_apply_parser.set_defaults(func=cmd_new_garden_apply)

    handoff_parser = subparsers.add_parser(
        "new-handoff",
        help="create a handoff event",
        parents=[wiki_parent],
    )
    handoff_parser.add_argument("--tool", required=True)
    handoff_parser.add_argument("--summary", required=True)
    handoff_parser.add_argument(
        "--repo-name",
        required=True,
        help="slug of the repo the session worked in (e.g. acme-notes)",
    )
    handoff_parser.add_argument(
        "--repo-branch", help="branch the session worked on (see --repo-from-git)"
    )
    handoff_parser.add_argument(
        "--repo-sha", help="commit the session ended at (see --repo-from-git)"
    )
    handoff_parser.add_argument(
        "--repo-from-git",
        type=Path,
        help="derive --repo-branch and --repo-sha from this checkout; "
        "an explicit flag overrides the derived value",
    )
    handoff_parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="[KIND=]PATH",
        help="source pointer; repeatable",
    )
    handoff_parser.add_argument(
        "--workstream",
        action="append",
        required=True,
        metavar="NAME:RELATIONSHIP[:ACTION]",
        help="proposed workstream relationship; repeatable",
    )
    handoff_parser.add_argument(
        "--events-dir",
        type=Path,
        default=None,
        help="event store (default: <wiki>/wiki/events)",
    )
    handoff_parser.add_argument("--sources-dir", type=Path)
    handoff_parser.add_argument("--pending-dir", type=Path)
    handoff_parser.add_argument(
        "--current-state",
        action="append",
        default=[],
        metavar="TEXT",
        help="current state bullet; repeatable",
    )
    handoff_parser.add_argument(
        "--what-was-done",
        action="append",
        default=[],
        metavar="TEXT",
        help="what was done bullet; repeatable",
    )
    handoff_parser.add_argument(
        "--next",
        action="append",
        default=[],
        metavar="TEXT",
        help="next action bullet; repeatable",
    )
    handoff_parser.add_argument(
        "--blocker",
        action="append",
        default=[],
        metavar="TEXT",
        help="blocker bullet; repeatable",
    )
    handoff_parser.add_argument(
        "--continuation-context",
        default=None,
        metavar="TEXT",
        help="continuation context paragraph",
    )
    handoff_parser.add_argument(
        "--timestamp",
        default=None,
        metavar="ISO-8601",
        help="override timestamp (backfill/delayed handoffs)",
    )
    handoff_parser.set_defaults(func=cmd_new_handoff)

    capture_parser = subparsers.add_parser(
        "capture-sources",
        help="copy source files and write an event-linked manifest",
        parents=[wiki_parent],
    )
    capture_parser.add_argument(
        "--event",
        type=Path,
        required=True,
        help="event JSON path, or a bare event id resolved against the store",
    )
    capture_parser.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="[KIND=]PATH",
        help="source file to capture; repeatable",
    )
    capture_parser.add_argument(
        "--sources-dir",
        type=Path,
        default=None,
        help="capture store (default: <wiki>/wiki/sources)",
    )
    capture_parser.add_argument(
        "--events-dir",
        type=Path,
        default=None,
        help="event store whose write lock serializes this capture "
        "(default: <wiki>/wiki/events)",
    )
    capture_parser.set_defaults(func=cmd_capture_sources)

    count_parser = subparsers.add_parser(
        "count-pending",
        help="print the number of unreviewed pending events",
        parents=[wiki_parent],
    )
    count_parser.add_argument(
        "--pending-dir",
        type=Path,
    )
    count_parser.add_argument(
        "--events-dir",
        type=Path,
        default=None,
        help="event store (default: <wiki>/wiki/events)",
    )
    count_parser.add_argument("--sources-dir", type=Path)
    count_parser.set_defaults(func=cmd_count_pending)

    pending_parser = subparsers.add_parser(
        "build-pending",
        help="write the pending index.json and latest.md pair",
        parents=[wiki_parent],
    )
    pending_parser.add_argument(
        "--events-dir",
        type=Path,
        default=None,
        help="event store (default: <wiki>/wiki/events)",
    )
    pending_parser.add_argument(
        "--sources-dir",
        type=Path,
    )
    pending_parser.add_argument(
        "--pending-dir",
        type=Path,
    )
    pending_parser.set_defaults(func=cmd_build_pending)

    sync_docs_parser = subparsers.add_parser(
        "sync-docs",
        help="sync a companion repo's docs subtree into the wiki docs/",
        parents=[wiki_parent],
    )
    sync_docs_parser.add_argument(
        "--companion",
        default=None,
        help="companion name from wiki.toml (default: the default companion)",
    )
    sync_docs_parser.add_argument(
        "--docs-dir",
        type=Path,
        help="destination docs dir (default: <wiki>/docs)",
    )
    sync_docs_parser.set_defaults(func=cmd_sync_docs)
    return parser


def missing_command(_args: argparse.Namespace) -> NoReturn:
    raise SystemExit(
        "usage: wiki-event.py "
        "[validate|status|new-handoff|new-garden-apply|capture-sources|"
        "build-pending|count-pending|sync-docs] ..."
    )


def conventional_store_root(events_dir: Path) -> Path | None:
    """The wiki root implied by the conventional <root>/wiki/events
    layout, or None for any other layout. The layout is the explicit
    contract both sides derive the store root from: the writing CLI and
    the checking consumers (hook, doctor) pin identically, so stored
    event paths come out repo-relative on both sides. Bare library
    layouts (tmp-tree tests) derive nothing and keep absolute paths on
    both sides."""
    resolved = events_dir.resolve()
    if resolved.name == "events" and resolved.parent.name == "wiki":
        return resolved.parent.parent
    return None


@contextmanager
def store_root_pinned(events_dir: Path) -> Iterator[None]:
    """Pin stored-path resolution to the store's conventional root for the
    duration of a projection rebuild, restoring the caller's pin after.

    The event CLI writes repo-relative event paths into the pending
    index, so a rebuild that stamps absolute paths reports every
    non-empty pending store as drifted (and, written back, produces an
    index the hook and doctor reject). A non-conventional layout derives
    nothing and keeps the caller's own pin state."""
    global _wiki_root
    prior = _wiki_root
    derived = conventional_store_root(events_dir)
    if derived is not None:
        set_wiki_root(derived)
    try:
        yield
    finally:
        _wiki_root = prior


def rebuild_pending_index(events_dir: Path, sources_dir: Path) -> dict[str, Any]:
    """The pending index the store implies, stamped the way the CLI stamps
    it. Every library-side rebuild (doctor, hook, night runner, garden)
    goes through here so all of them agree with the written projection."""
    with store_root_pinned(events_dir):
        return build_pending_index(load_events(events_dir), sources_dir)


def _wiki_root_for(args: argparse.Namespace) -> Path:
    """Resolve the wiki root once per invocation and pin stored-path
    resolution (repo_relative/stored_path) to it."""
    root: Path | None = getattr(args, "resolved_wiki_root", None)
    if root is None:
        root = wiki_config.resolve_wiki_root(args.wiki)
        set_wiki_root(root)
        args.resolved_wiki_root = root
    return root


def _apply_content_defaults(args: argparse.Namespace) -> None:
    """Fill unset content-path flags from the resolved wiki root.

    Resolution is lazy: an invocation whose path flags are all explicit
    never looks for wiki.toml at all; --wiki, when given, always resolves
    so a bad value fails loudly even when no flag needs it."""
    if getattr(args, "wiki", None) is not None:
        _wiki_root_for(args)
    if getattr(args, "events_dir", False) is None:
        args.events_dir = _wiki_root_for(args) / "wiki" / "events"
    if args.func is cmd_capture_sources and args.sources_dir is None:
        args.sources_dir = _wiki_root_for(args) / "wiki" / "sources"
    # A fully-explicit invocation never resolves wiki.toml, but a store at
    # the conventional layout still pins its root so written projections
    # carry repo-relative paths - the same derivation store_root_pinned
    # applies around every library-side rebuild.
    if _wiki_root is None:
        events_dir = getattr(args, "events_dir", None)
        if isinstance(events_dir, Path):
            derived = conventional_store_root(events_dir)
            if derived is not None:
                set_wiki_root(derived)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _apply_content_defaults(args)
    except wiki_config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
