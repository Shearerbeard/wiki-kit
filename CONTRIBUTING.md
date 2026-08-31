# Contributing

## Setup

Python 3.12+, managed by uv; the only runtime dependency is
`jsonschema`.

## Checks

Before asking for review, run everything in `README.md`'s
Development section.

If you have the maintainer's Vale style pack symlinked into `.vale/`
(see `.vale.ini`), run `vale` on every markdown file you touch.

### Hooks

`.pre-commit-config.yaml` runs gitleaks on the staged tree and, at the
commit-msg stage, commitlint (`.commitlintrc.yaml`) plus the Vale
commit-message gate when the style pack is linked. One-time install:

```sh
brew install pre-commit gitleaks
pre-commit install --hook-type pre-commit --hook-type commit-msg
```

`pre-commit run --all-files` runs the file-stage hooks by hand. To
check a message without committing:

```sh
npx --yes -p @commitlint/cli commitlint --edit path/to/message.txt
```

## CI

Every push and pull request to `master` runs two jobs
(`.github/workflows/ci.yml`):

- `test`: `uv run ruff check .` and `uv run pytest -q`. On a
  failure, open the failing step's log in the run page.
- `install-smoke`: the Docker end-to-end install against a blank
  fixture. It uploads an `install-smoke-report` artifact
  (`latest.md`, `latest.json`, `latest.log`) - read `latest.md`
  first, then `latest.log`. If the artifact is absent, the job
  failed before a report was written: read the job log.

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
