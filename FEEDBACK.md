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

## 2026-09-16 garden-apply-route-to-missing-workstream

```yaml
date: 2026-09-16
harness: opencode
agent: opencode-go/glm-5.3-flash
workstreams: [wiki-kit]
repo: /Users/mshearer/workspace/aura-session-docs
source: scripts/wiki_garden.py apply of handoff event 01a0aac0-3e7a-74a9-bda2-4bbd26bd7ea3
```

A handoff agent proposed workstream `aura-park-reify` as a `needs_review`
primary route, but no `workstreams/aura-park-reify.md` exists (the
established page is `hitl-271-park-reify.md`). Two dead ends followed:

1. `wiki-garden.py` refuses to run: a `needs_review` primary naming a
   missing file raises `FileNotFoundError`, and `--workstream` is only
   accepted when the event has **no** primary route - so an approved
   route correction to an existing page is impossible from the CLI.
2. The wrong name is baked into the immutable event JSON, so nothing in
   the toolchain can repair the route after the handoff validates.

The executed workaround: create a redirect stub at the proposed name,
apply there (disposition `applied`), curate the block into the real page
with user approval, then delete the stub. Works, but it burns a
garden-apply event on a page that never becomes canonical and relies on
interactive discipline to clean up.

Kit-relevant because the routing contract is wiki-kit's: the handoff
validator accepts any proposed name without checking the workstream
exists, and the apply script has no user-approved override for "route
exists but file missing". Candidate fixes (proposing, not committing):
the handoff validation could warn or fail when a sole primary names a
missing workstream, or `wiki-garden.py` could accept `--workstream` as a
user-approved re-route recorded as `applied-manually` when the primary's
file is missing (distinct from replacing an existing primary route).
