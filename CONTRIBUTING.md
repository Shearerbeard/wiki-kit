# Contributing

## Setup

```sh
uv sync
```

Python 3.12+, managed by uv; the only runtime dependency is
`jsonschema`.

## Checks

Run all of these before asking for review:

```sh
uv run pytest          # unit + integration; includes the
                       # zero-source-strings sweep
uv run ruff check .
scripts/install-smoke/run.sh   # Docker end-to-end against a blank
                               # fixture, offline
```

If you have the maintainer's Vale style pack symlinked into `.vale/`
(see `.vale.ini`), run `vale` on every markdown file you touch.

## House rules

- Fail loud. No speculative fallbacks, no empty defaults for missing
  data; callers handle exceptions.
- State each fact once and link the rest; if you change behavior,
  update the doc that states it in the same change.
- The repo boundary is absolute: no real deployment names, machine
  paths, or personal identifiers anywhere outside the pinned enclaves
  in `tests/sweep/`.
- The schema registry in `schemas/` is never loosened: new event or
  artifact shapes get new schema versions.

## Commits

- Conventional first line: `type(scope): summary`, e.g.
  `fix(dock): refuse symlinked skill dirs`.
- No AI attribution trailers (`Co-Authored-By: ...`, "Generated with
  ..." footers); the author of record is the person who ran the
  change through review.

## Review

Adversarial review by a different model family is the standing rule for
substantive diffs - the transport, stall, and pre-vet discipline lives
in `docs/REVIEW-TOOLING.md`.
