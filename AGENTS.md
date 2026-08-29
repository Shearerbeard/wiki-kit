# AGENTS.md

wiki-kit is generic wiki machinery, installable into any content repo:
an append-only event store with schema-registry validation,
deterministic projections, garden and night pipelines, a working-tree
doctor, and an installer that wires the mechanical enforcement in.
This repo holds machinery only - a deployment (a "wiki repo") holds
content plus its config pair.

## Read order

1. `README.md` - what the kit is and how the pieces fit.
2. `docs/docking-spec.md` - the `.wiki/` dock contract (ratified).
3. `docs/enforcement-contract.md` - what each enforcement layer covers.

Adopting the kit into a consumer repo: `docs/ADOPTION.md`. First
session on a fresh deployment: `docs/QUICKSTART.md`.

## Dev commands

```sh
uv sync
uv run pytest          # includes the zero-source-strings sweep
uv run ruff check .
scripts/install-smoke/run.sh   # Docker end-to-end, offline
```

## Repo boundary

Machinery only. Deployments hold content; nothing here may carry
deployment-specific or personal strings. The sweep test
(`tests/sweep/`) enforces this on every run - the one documented
exception is the v1 legacy event shim and the ratified provenance
records, both pinned in that file.

## Standing rules an agent must not break

- Events are append-only; never edit or delete a committed event.
- Never hand-edit generated projections (`wiki/log.md`, the pending
  projection, `CLAUDE.local.md`); the renderers own them.
- Never commit machine-local files (`wiki.local.toml`,
  `.wiki/local.toml`, `.wiki/orientation.md`, rendered skills) - the
  postures' ignore mechanics exist because these embed machine paths.
- No machine-global installs before the adoption ruling: skill renders
  are project-scoped only (`wiki-dock install --skills-dir` refuses
  anything outside the consumer repo).
- If you change behavior a doc describes, update the doc in the same
  change (see `CONTRIBUTING.md`).
