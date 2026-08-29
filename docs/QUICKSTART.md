# Quickstart: the first session on a fresh deployment

You have a wiki installed (`install.sh`) and at least one consumer
docked (`ADOPTION.md`). This walks the first work session.
Paths below are in the wiki repo unless noted; kit CLIs resolve the wiki
root themselves from a docked consumer, so run them from wherever you
are working.

## 1. Orient

In the consumer repo, your harness's entry file (`AGENTS.md`, or the
`CLAUDE.md` shim) points at `.wiki/orientation.md` - read it. It names
the wiki root on this machine, the rendered project skills, and the
commands a session needs. In the wiki repo itself, `CLAUDE.local.md`
is the generated orientation index.

## 2. Work

Do the work in the consumer repo as normal - the wiki does not watch
you; memory is written deliberately, at handoff time.

## 3. Hand off

At the end of the session, the `handoff` project skill drives the
write: it gathers the git facts and picks the workstream, then records a
handoff event through the event CLI:

```sh
uv run --project /path/to/kit /path/to/kit/scripts/wiki-event.py new-handoff ...
```

Events are append-only - you add one; you never edit another session's.

## 4. Garden

The `garden` skill applies a validated handoff to its workstream file
and re-renders the projections the handoff touched. The mechanical
parts (validation, stale detection, rendering) are scripts; the
curation judgment is the agent's. If the session surfaced stale or
contradictory wiki state that is not yours to fix, the
`session-feedback` skill files it for garden triage instead.

## 5. Render and verify

```sh
uv run --project /path/to/kit /path/to/kit/scripts/wiki-render.py log
uv run --project /path/to/kit /path/to/kit/scripts/wiki-render.py claude-local
python3 /path/to/kit/scripts/wiki-doctor.py --wiki /path/to/wiki
```

The renderers own the projections (`wiki/log.md`, `CLAUDE.local.md`) -
never hand-edit them. The doctor tells you whether the working tree is
honest; `DOCTOR-TRIAGE.md` reads every check it prints.

## 6. The scheduled loop

If the scheduler is installed, the night pipeline, the morning report
review (the `morning` skill), and the garden reminder run on the
wiki's configured times (`wiki-toml-schema.md`, `[schedule]`). The
first morning after your first night run is the moment to check that
loop end to end.
