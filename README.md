# wiki-kit

Generic wiki machinery, installable into any content repo: an
append-only event store with schema-registry validation, deterministic
projections, garden and night pipelines, a working-tree doctor, and an
installer that wires the mechanical enforcement in. The guiding line:
"afraid of remembering the wrong thing." Events are append-only,
dispositions are join-derived, curation is gated.

The kit repo holds machinery only. A deployment (a "wiki repo") holds
content plus its config pair; the kit's scripts run against it from
this checkout.

## Quick start

```sh
uv sync                                       # once per kit checkout
scripts/install.sh --wiki /path/to/your/wiki
```

`install.sh` runs on the kit's `.venv`; without one it falls back to a
`python3` that has `jsonschema`, then to `uv run`, and otherwise stops
and names the fix.
Works on a blank directory, an existing git repo, or a directory that
already carries docs and an Obsidian vault (pre-existing content is
never touched). The installer seeds `wiki.toml`, the content skeleton,
the projections, an orientation skeleton, the pre-commit hook, and the
`.gitignore` lines for the machine-local files and the runtime output
the scheduler and night runner write (unit logs, night reports); on a
repo with no commits it creates an initial commit of exactly the files
it wrote. Reinstalling is idempotent. It also renders scheduler units
for the night, morning, and garden-reminder jobs: launchd units on
macOS from `templates/launchd/`, systemd user timers on Linux from
`templates/systemd/` (`--no-scheduler` skips).

Verify any deployment:

```sh
uv run scripts/wiki-doctor.py --wiki /path/to/your/wiki
```

Every kit CLI runs the same way: `uv run` inside the checkout, or
`uv run --project /path/to/kit /path/to/kit/scripts/<cli>` from
anywhere. Bare `python3` works only where it can import `jsonschema`.

Dock a consumer repo to a wiki and walk the first session:
`docs/ADOPTION.md` (install, dock, postures, verify) and
`docs/QUICKSTART.md` (the first-session loop).

## How the pieces fit

- `scripts/wiki_config.py` finds the wiki root (explicit `--wiki` flag,
  `WIKI_DOCK` env, walk-up through `.wiki/` consumer docks bounded at
  the git toplevel - the resolution order in `docs/docking-spec.md`)
  and loads the
  config pair: committed `wiki.toml` for semantic values, gitignored
  `wiki.local.toml` for machine paths, with a hard allowlist on what
  the overlay may set.
- `scripts/wiki-dock.py` docks a consumer repo: manifest + overlay,
  posture ignore mechanics, the rendered orientation
  (`.wiki/orientation.md`), the `AGENTS.md` dock block and `CLAUDE.md`
  shim, and project-scoped skill renders. `scripts/wiki-probe.py`
  drives each supported harness headlessly to prove it sees the dock.
- `scripts/wiki-event.py` writes and validates events against the
  registry in `schemas/` (versioned, never loosened). `new-handoff`
  records a session; `build-pending` rebuilds the pending projection.
- `scripts/wiki-garden.py` applies a validated handoff to its
  workstream file; `scripts/wiki-render.py` renders the log projection
  and the orientation index.
- `scripts/wiki_night.py` is the mechanical nightly pipeline; the
  reminder scripts and scheduler templates drive it on a schedule.
- `scripts/wiki-doctor.py` checks the working tree: config strictness,
  projection freshness, capture hashes, budgets, links, and that the
  installed enforcement matches `[contract]`.
- `scripts/pre-commit` is the enforcement hook the installer wires into
  a deployment; `docs/enforcement-contract.md` states exactly what each
  layer does and does not cover.
- `scripts/install-smoke/` proves the install end to end in Docker
  against a blank fixture, offline.

## Configuration

`docs/wiki-toml-schema.md` documents every key. `[contract]` is the
single source for the deny-rule and skill contract; installer, doctor,
and install-smoke all consume it. Companion repos are `[companions.*]`
tables with their machine paths in the overlay.

## Development

```sh
uv sync
uv run pytest       # unit + integration; includes the
                    # zero-source-strings sweep
uv run ruff check .
scripts/install-smoke/run.sh   # Docker end-to-end, offline
```

## Provenance

This repo starts with fresh history by ruled decision.
`docs/extraction-ledger.md` is the provenance record, naming each
ported file's source path and the freeze commit it was ported at, with
`docs/charter.md` holding the ruled decisions. One legacy shim is
permanent by design: version-1 events on disk carry an envelope key
named after the source deployment, and the loader keeps reading it
forever (`docs/enforcement-contract.md` and the ledger document the
boundary; the test suite enforces that nothing else in the machinery
names the source).
