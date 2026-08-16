# wiki-kit

The generic wiki machinery extracted from a private session-docs wiki:
an append-only event store with schema-registry validation,
deterministic projections, garden and night pipelines, workflow skills,
a doctor, and an installer that docks consumer repos through a `.wiki/`
directory.

Status: founding documents only. The machinery arrives at extraction
stage K2; nothing here runs yet.

- `docs/charter.md` - what the kit is, its repo boundary, and the
  ruled reserved decisions (ratified 2026-08-16).
- `docs/extraction-ledger.md` - per-file disposition of every source
  file, with the freeze commit the port is diffed against.
- `docs/docking-spec.md` - the `.wiki/` consumer docking convention.
- `docs/wiki-toml-schema.md` - the configuration surface.

Provenance: this repo starts with fresh history by ruled decision; the
extraction ledger is the provenance record, naming each ported file's
source path and the freeze commit `690b17a` in the source wiki.
