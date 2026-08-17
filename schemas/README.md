# Schema composition

This directory holds the composable JSON Schema definitions for wiki events.
There is exactly one schema source of truth: the registry in
`events/_index.json`. There is no monolithic fallback schema.

## Layout

```
schemas/
├── common/
│   └── types.json                  # Shared type definitions ($defs)
├── events/
│   ├── handoff-v1.schema.json      # Handoff event schema
│   ├── garden-apply-v1.schema.json # Garden-apply event schema
│   └── _index.json                 # Registry: event type → schema file
├── workstream-state.json           # Optional workstream_state fragment
├── quarantine.schema.json          # wiki/quarantine.json (config)
├── log-epoch.schema.json           # wiki/log-epoch.json (config)
├── pending-index.schema.json       # wiki/pending/index.json (generated)
├── capture-manifest.schema.json    # wiki/sources/**/manifest.json (generated)
└── README.md                       # This file
```

All schemas declare JSON Schema draft 2020-12.

## Two kinds of schema

- **Event schemas** live under `events/` and are discovered through the
  `_index.json` registry, keyed by `event_type` and versioned. Events are
  the immutable source of truth.
- **Artifact schemas** (`*.schema.json` at this level) cover the config and
  generated-projection files around the event store. They are referenced by
  file name through `validate_artifact()` in `scripts/wiki-event.py` at each
  read/write point - not registered in `_index.json`, because they are not
  events and carry their own `schema_version` field for future evolution.

Cross-field rules a JSON Schema cannot express stay in code and are
documented in the schema's `description` (e.g. quarantine's
path-matches-event_id, no-duplicate-ids, and corrected_by-exists-in-store
checks in `load_quarantine`/`load_events`). Note quarantine `event_id`s are
deliberately NOT the `uuidv7` type: quarantined ids are corrupt by
definition.

## How schemas compose

`events/_index.json` is the single entry point for schema discovery. It maps
event types to their schema files.

Event schemas reference shared types from `common/types.json` via `$ref`:
```json
"event_id": { "$ref": "../common/types.json#/$defs/uuidv7" }
```

Optional fragments (like `workstream-state.json`) are referenced directly by
event schemas via `$ref` - they are not registered in `_index.json`.

## How to add a new event type

1. Create the schema file under `events/` (e.g., `events/garden-apply-v1.schema.json`)
2. Add an entry to `events/_index.json` (the key must match the Python
   `EventType` enum value exactly - the registry is drift-checked against it):
   ```json
   "garden-apply": { "v1": "events/garden-apply-v1.schema.json", "latest": "v1" }
   ```
3. Add any new shared types to `common/types.json` under `$defs`
4. Add `$ref` references to the new event schema for any optional fragments

## Validation

The `SchemaCache` class in `scripts/wiki-event.py` loads `_index.json` once per
process, resolves `$ref` paths (including JSON pointer fragments like
`#/$defs/uuidv7`) relative to the referencing file, and caches the result.
It clears the resolved cache when `_index.json` changes.

Validation is two layers, both mandatory:

1. **jsonschema** (a hard dependency, declared in `pyproject.toml`) validates
   the event against the fully ref-resolved schema - patterns, enums,
   required fields, `additionalProperties`.
2. **Hand-written checks** in `wiki-event.py` (`validate_event`,
   `validate_garden_apply_event`, …) re-derive field sets and enum values
   from the resolved schema, produce clearer error messages, and fail loudly
   when a JSON Schema enum and its Python `StrEnum` drift apart
   (`require_schema_enum_matches`).

Do not read schema files directly from `wiki-event.py` - use `SchemaCache`.
There is no `--schema` CLI override: events validate against the registry's
schema for their `event_type`, nothing else.

Beyond the schema patterns, Python validation rejects any event id whose
embedded UUIDv7 timestamp exceeds the validation wall clock plus
`UUID7_MAX_CLOCK_SKEW_MS` (1 hour) - ids are generated, never hand-written,
so a future-dated id is a fabrication (DECISIONS.md D10). Backfilled and
correction events are unaffected: their ids postdate their declared
`timestamp_utc`, not the wall clock.

## Handoff v2: the repo envelope

Handoff v2 (latest) replaces v1's single-repo envelope field with
`repo: {name, branch, sha}` - v1's field nominally described the
deployment's primary repo but recorded whichever repo the session worked
in (a secondary companion's handoffs stored that companion's git state
there). Validation dispatches by the event's declared `schema_version`;
v1 store events stay valid forever and are never rewritten. Consumers
read both versions through one accessor, `event_repo()` in
`scripts/wiki_event.py`, which normalizes v1 events to the deployment's
configured legacy repo name (see the v1 shim's docstring for the
caveat). `new-handoff` emits v2 and requires `--repo-name`.

## Status semantics (join-derived disposition)

A handoff event's `status` field is `pending_garden` at write time and never
changes afterward - events are immutable. Whether a handoff has been
gardened is **join-derived**: the latest garden-apply event whose
`target_event_id` names it, ordered by `(timestamp_utc, event_id)`, is its
disposition (`applied` / `rejected` / `applied-manually`). A manual route can
also record the user-approved proposed `workstream`. No projection
reads the handoff `status` field for truth.

Ordering contract for ALL consumers: order events by
`(timestamp_utc, event_id)` - never by id-generation/append order.
Correction events legitimately carry ids minted long after their declared
timestamps, so a consumer processing in append order would replay an old
disposition over a newer one. Query a single event directly:

```
uv run scripts/wiki-event.py status <event-id>
```

The handoff-status enum value `applied` is deprecated legacy: 6 pre-hook
store files carry it from an in-place mutation (DECISIONS.md D10); the
pre-commit hook rejects it on added events.

## $ref format

The `SchemaCache._resolve_refs()` method supports these `$ref` patterns:

- **Relative file path**: `{ "$ref": "../common/types.json" }` - resolves to
  the root of the target file.
- **File path with JSON pointer**: `{ "$ref": "../common/types.json#/$defs/uuidv7" }` -
  navigates to the specified location within the target file. The fragment
  uses standard JSON pointer escaping: `~1` for `/`, `~0` for `~`.
- **Bare pointer**: `{ "$ref": "#/$defs/something" }` - navigates within the
  current file (not yet used but supported).

## Troubleshooting

If validation fails unexpectedly:

1. **Check schema JSON**: `python3 -m json.tool schemas/events/handoff-v1.schema.json`
2. **Validate the event through the CLI**:
   `uv run scripts/wiki-event.py validate event.json` - the error message
   names the failing field or pattern.
3. **Check enum drift**: an error mentioning "enum drift" means a schema enum
   and its Python `StrEnum` in `wiki-event.py` disagree - fix whichever side
   is wrong; the mismatch is the finding.
4. **Check `$ref` resolution**: run the registry resolution directly:
   `uv run python -c "import sys; sys.path.insert(0, 'scripts');
   import wiki_event;
   print(wiki_event.SchemaCache().resolve_schema('handoff', 'v2'))"`
