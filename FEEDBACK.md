# FEEDBACK.md - process-feedback inbox

This file is wiki-kit's intake for process friction found while using the
kit from a consumer repo. Consumers append entries here; nobody edits the
kit's templates or code from a consumer repo. A maintainer session disposes
each entry - a fix in the kit, or a rejection with a recorded reason - and
deletes it from this file; the commit that deletes an entry names its
disposition and is the durable record. This file is a queue.

Format mirrors boardkit's `FEEDBACK.md` (see its own file for the fuller
rationale) since wiki-kit already follows boardkit's conventions elsewhere
(the `.wiki/` docking spec, `docs/docking-spec.md`).

## Entry format

One `##`-level section per entry, newest last, opening with a fenced YAML
block, then the finding in prose:

````markdown
## 2026-08-02 short-slug

```yaml
date: 2026-08-02
harness: claude-code
agent: <model id>
workstreams: [wiki-kit]
repo: <consumer repo the friction arose in>
source: <path or record the finding is grounded in>
```

What happened, why it is kit-relevant, and the candidate fix if one is
apparent. Ground it in a real session; do not assert from memory.
````

- `harness` uses the claude-skills vocabulary: `claude-code`, `opencode`,
  `codex`, or `antigravity`.
- An entry proposes; the maintainer disposes. Do not pre-commit the kit to
  a fix inside an entry.

## Entries

## 2026-08-29 new-handoff-no-render-step-leaves-log-stale

```yaml
date: 2026-08-29
harness: opencode
agent: glm-5.3-flash
workstreams: [wiki-kit]
repo: retro-dev-docs
source: wiki-event.py new-handoff + manual log.md append + wiki-doctor render-log FAIL this session (event 01a04fc8-fc53-7aca-ab76-5ed8b2e24d18)
```

Creating a handoff via `wiki-event.py new-handoff` writes the event JSON
and refreshes the pending index, then prints only the event path. Nothing
in its output, `--help`, or any step of that flow mentions that
`wiki/log.md` must be re-rendered. I appended a log entry by hand,
formatted to match the existing entries exactly (it would have been
byte-close to what the renderer produces, since the renderer builds
entries from the event's own summary and `what_was_done` bullets), and
only learned the rule when I proactively ran `wiki-doctor` and
`render-log` failed with "wiki/log.md differs; run the kit renderer
(log)" - a good error message, but a first signal that arrives only if
the agent thinks to run the doctor before committing. Per its docs the
pre-commit hook re-renders and compares, so the other backstop is a
failing commit.

Two observations make this a realistic trap. First, `log.md`'s header
does say "Never hand-edit", but an agent reconstructing "the format"
reads entries tail-first (grep/tail for the newest example, exactly what
I did) and never sees the header. Second, the workflow knowledge that
handoff = create event + render log + commit presumably lives in the
declared `handoff` skill, which is not installed (see the
declared-skills entry above) - so the tool surface is the only
documentation a consumer session has, and it is silent on the render
step.

Candidate fix: `new-handoff` runs the log renderer itself after writing
the event (the projection is a pure function of its inputs, so this is
safe and makes the tool's writes self-consistent), or at minimum its
final output line names the remaining steps: run `wiki-render.py log`,
then commit.

## 2026-08-29 readme-verify-and-cli-invocations-bare-python3

```yaml
date: 2026-08-29
harness: opencode
agent: glm-5.3-flash
workstreams: [wiki-kit]
repo: retro-dev-docs
source: wiki-event.py and wiki-doctor.py invocations this session (snes-hello handoff event 01a04fd2-45ae-751a-9c3f-56522e879d4c)
```

Adjacent to `install-sh-needs-uv-run-not-bare-python3` (14760e2) but
names surfaces that entry's candidate fixes do not cover, so filing
separately for the maintainer to merge or reject. This session's first
invocation, `python3 scripts/wiki-event.py --help`, failed with the same
`ModuleNotFoundError: No module named 'jsonschema'`, and the README's
own "Verify any deployment" line - `python3 scripts/wiki-doctor.py
--wiki /path/to/your/wiki` - reproduces the identical failure live on
the machine that hosts the only real deployment. Both of the earlier
entry's proposed fixes leave this broken: making `install.sh` depend on
`uv run` internally repairs installation only, and "run `uv sync`
before install.sh" never changes what bare `python3` resolves to (the
`.venv` it populates is not on sys.path). So every CLI surface a
consumer touches outside the installer - the documented doctor verify,
`new-handoff`, `render`, `garden` - carries the same latent defect, and
the README documents one of them in broken form. Candidate fix: the
README sanctions one invocation form for all kit CLIs (e.g. `uv run
scripts/wiki-doctor.py ...`), matching the installer's existing
pattern of pinning an interpreter (the generated pre-commit wrapper).

## 2026-08-29 seeded-wikitoml-companions-comment-stranded

```yaml
date: 2026-08-29
harness: opencode
agent: glm-5.3-flash
workstreams: [wiki-kit]
repo: retro-dev-docs
source: reading the deployed wiki.toml this session; traced to default_wiki_toml() in scripts/wiki_install.py
```

The seeded `wiki.toml` places the comment "# Consumer repos dock here
as [companions.<name>] tables; the overlay carries each companion's
machine path" after `[wiki]`'s `name` line plus a blank line, directly
above `[contract]`. It documents neither neighbor: nothing under
`[wiki]` relates to companions, and `[contract]` is not a companions
table. Reading the file cold this session, the comment attached itself
to `[wiki]`, and the companions mechanism only became clear after
checking `docs/wiki-toml-schema.md`. The stranding ships from the
installer template (`default_wiki_toml()`, wiki_install.py), so every
fresh deployment inherits it. Candidate fix: anchor the comment to a
commented-out example table (`[companions.example]` with a `github`
key and a pointer at the overlay for `path`) so the guidance is
structural rather than positional.

## 2026-08-29 session-feedback-skill-unrouted-ledger-vs-tree

```yaml
date: 2026-08-29
harness: opencode
agent: glm-5.3-flash
workstreams: [wiki-kit]
repo: retro-dev-docs
source: filing today's feedback required the owner to name FEEDBACK.md; docs/extraction-ledger.md skill rows vs tests/notifications/test_morning_skill_contract.py; exhaustive SKILL.md search this session
```

Related to `declared-skills-not-actually-installed-no-signal` (the
installer materializes nothing) but a different layer, so filing
separately: this is provenance-versus-tree drift. The extraction
ledger's four skill rows (garden, handoff, morning, session-feedback)
carry disposition `port` with notes phrased as shipped work ("the
reporter-tool hardcode in step 2 fixed"), yet no skill body exists in
the kit working tree, anywhere in the kit's git history, or in any
plausible source location on this machine (an exhaustive `find` for
`SKILL.md` this session turns up only unrelated marketplace skills).
The sole artifact acknowledging the pending state is
`tests/notifications/test_morning_skill_contract.py`, whose docstring
says "the morning skill itself ports at K3" and skips until the file
lands at `.agents/skills/morning/SKILL.md` - and it guards `morning`
alone; the other three rows, including `session-feedback`, have no
such contract pin at all. The practical consequence surfaced in this
very filing: the process-feedback loop FEEDBACK.md exists for is
unrouted - nothing in the deployment, the kit README, or any installed
surface points a consumer agent at it, and the skill that would do
the routing is the one whose ledger row reads as ported. All of
today's consumer entries exist only because the owner named the file
directly. Candidate fix: record the K3-pending state on the ledger
rows themselves (a status note per row), and extend the skip-guarded
contract-test pattern to the other three skills so the pinned
contracts survive until the port lands.

## 2026-08-29 garden-apply-split-across-two-clis

```yaml
date: 2026-08-29
harness: opencode
agent: glm-5.3
workstreams: [wiki-kit]
repo: retro-dev-docs
source: wiki-garden.py --help vs wiki_event.py new-garden-apply --help this session; applied events 01a04fb3 and 01a04fe3 via wiki-garden.py
```

Performing "garden" requires knowing two CLIs that do not reference
each other: `wiki-garden.py <event>` applies an event to the
workstream page and writes disposition events, while
`wiki_event.py new-garden-apply` only records a disposition
(applied-manually/rejected/superseded) without applying anything. The
subcommand name reads as "apply now" but it is the record-only half. I
found wiki-garden.py only by listing scripts/ after
new-garden-apply's help did not do what its name suggested. Candidate
fix: each --help names the other tool's role, or apply and
disposition merge into one subcommand with a --status flag.

## 2026-08-29 validate-requires-positional-event

```yaml
date: 2026-08-29
harness: opencode
agent: glm-5.3
workstreams: [wiki-kit]
repo: retro-dev-docs
source: first wiki_event.py validate invocation this session
```

Adjacent to `event-reference-arg-convention-inconsistent` (same
family, another surface): `validate --wiki <wiki>` without the
positional event path fails with bare argparse usage. I expected the
subcommand to validate the event store; it validates one event and
the required positional is easy to miss because --wiki feels like the
complete invocation. Candidate fix: same as the family entry - accept
a bare id resolved against the events dir - plus an --all form for
validating the store.

## 2026-08-29 new-handoff-manual-repo-identity

```yaml
date: 2026-08-29
harness: opencode
agent: glm-5.3
workstreams: [wiki-kit]
repo: retro-dev-docs
source: new-handoff invocations this session (events 01a04fb3, 01a04fe3)
```

--repo-branch and --repo-sha are required even though the session
filing the handoff almost always sits inside the repo it describes; I
piped `git rev-parse HEAD` into the flags by hand both times. A wrong
sha is accepted silently - nothing checks reachability against the
named repo. Candidate fix: `--repo-from-git <path>` derives
branch/sha from the repo itself, with the explicit flags remaining as
override.

## 2026-08-29 garden-apply-leaves-pending-stale

```yaml
date: 2026-08-29
harness: opencode
agent: glm-5.3
workstreams: [wiki-kit]
repo: retro-dev-docs
source: garden apply of 01a04fb3/01a04fe3 followed by pre-commit failure this session
```

Adjacent to `new-handoff-no-render-step-leaves-log-stale` but a
different generated surface, with a better error: after wiki-garden.py
applies events, wiki/pending/latest.md no longer matches the event
store, and the pre-commit hook blocks with "run wiki-event.py
build-pending". The message names the fix, so recovery is one step -
but the apply step invalidated the surface it knew about. Candidate
fix: garden apply runs the pending builder after mutating the store,
mirroring the candidate fix proposed for the log renderer in the
adjacent entry.
