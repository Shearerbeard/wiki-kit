---
name: handoff
# Rendered from the wiki-kit template by wiki-dock install; a reinstall
# overwrites this file.
description: |
  Write a session handoff summary. This captures your current context so the next session
  (which may be a different model or tool) can pick up cleanly.
compatibility: pi opencode claude-code codex
---

Write a session handoff summary. This captures your current context so the next session (which may be a different model or tool) can pick up cleanly.

**Key concept: workstreams are goals, not sessions.** Your session may contribute to an existing workstream that other sessions have also worked on. You create a structured event for the workstream you touched — you do not overwrite what previous sessions recorded.

**Scope boundary:** handoff is a session self-report, not parent gardening or wiki canonicalization. You may only create handoff events for the workstream(s) this session directly touched. Do not merge workstreams, archive workstreams, prune stale Next items, rewrite canonical entity pages, or resolve cross-session source conflicts. Those belong to interactive `/garden` or future approval-gated garden apply flows.

`<wiki-root>` below is the resolved wiki repo root (the directory containing
`wiki.toml`). The kit CLIs resolve it themselves through your repo's dock
(flag/env/walk-up/common-dir), so commands run from the repo this session
worked in; only direct file reads and plain `git` calls need the root spelled
out.

Do the following in order:

1. **Gather git facts.** Run these commands silently (do not display output) in the repo this session worked in:
   - `basename "$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"` → `REPO_NAME` (the MAIN repo's name — correct even inside a worktree)
   - `git rev-parse --abbrev-ref HEAD` → `REPO_BRANCH`
   - `git rev-parse --short HEAD` → `REPO_SHA`
   - `git status --short` → note modified files
   - `git worktree list` (or infer from context) → note active worktree

1.5. **Check repo scope.** The wiki is scoped to a known family of repos: the wiki repo itself plus the companions declared in its `wiki.toml` (`[companions.*]` tables). If `REPO_NAME` is not one of these, STOP and ask the user explicitly whether this session's work should be written to the wiki before doing anything else in this skill. Do not create a handoff event for an out-of-family repo without that confirmation - real incidents (an unrelated repo's session writing a pending event into the wiki) are why this check exists.

2. **Identify your workstream(s).** Determine which goal this session worked toward. Check if a matching file already exists in the wiki's `workstreams/` directory (`<wiki-root>/workstreams/`).
   - If a matching file exists, read it — you'll reference it in step 5.
   - If no matching file exists, you will use `candidate_new` as the relationship in step 5.
   - If this session touched multiple goals, repeat step 5 for each primary workstream.
   - If the mapping is ambiguous (the work fits several goals, or none cleanly), pick the closest existing workstream as primary with `:needs_review` and explain the ambiguity in `continuation_context`. Garden corrects mappings; a wrong-but-flagged guess beats a stalled handoff.

3. **Cross-reference memory and docs.** Before building the event, check if matching source material exists:
   - `ls ~/.claude/projects/<dash-encoded-repo-path>/memory/project-*` — scan for memory files related to your workstream's goal (the project slug is the repo's absolute path with `/` replaced by `-`)
   - `.opencode/plans/*.md` — plans from OpenCode sessions
   - the companion's docs outbox (`docs_subpath` in its `wiki.toml` companion table) — audit docs, design docs from other sessions
   Read any that match your workstream's topic. Include relevant file paths as `--source` args in step 5. The event should SUMMARIZE key findings inline and POINT TO full docs for detail. Don't write a thin summary when rich source material exists.

4. **Build session state.** Compose the following fields. All text must be concise and factual.

   `current_state` — 2-4 bullets on where the goal stands right now.

   `what_was_done` — Your session's contributions as a numbered or bulleted list.

   `next` — Updated next actions. Include items discovered this session and any still-relevant items from previous sessions.

   `blockers` — Current blockers. Include only unresolved ones.

   `continuation_context` — Self-contained paragraph, under 200 words:
   - Current branch + sha, active worktrees
   - CONTEXT: 2-3 sentences on what was just discovered or decided
   - TASK: what the next session should do
   - HAZARDS: gotchas or things not to break

5. **Create the handoff event** by calling `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki-event.py new-handoff` with the following arguments:

   Required:
   - `--tool <your-tool>` (one of: `claude-code`, `opencode`, `codex`, `pi`, `manual`)
   - `--summary "<one-line summary of this session>"`
   - `--repo-name <REPO_NAME>` (which repo the branch/sha below describe)
   - `--repo-branch <REPO_BRANCH>`
   - `--repo-sha <REPO_SHA>`

   Workstream (repeatable; one per goal this session touched):
   - `--workstream <name>:primary[:needs_review]` for the main goal
   - `--workstream <name>:related[:needs_review]` for secondary goals
   - If the workstream is new, use `candidate_new` instead of `primary`.

   Sources (repeatable; include any files referenced in step 3):
   - `--source <path>` or `--source memory=<path>` or `--source plan=<path>`

   Workstream state (repeatable list args for bullets, single string for paragraph):
   - `--current-state "<bullet>"` (repeat for each bullet)
   - `--what-was-done "<bullet>"` (repeat for each item)
   - `--next "<bullet>"` (repeat for each item)
   - `--blocker "<bullet>"` (repeat for each blocker)
   - `--continuation-context "<paragraph>"`

   Example:
   ```bash
   uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki-event.py new-handoff \
     --tool claude-code \
     --summary "Refactored handoff command to use wiki-event.py" \
     --repo-name widget \
     --repo-branch main \
     --repo-sha abc1234 \
     --workstream wiki-system:primary \
     --source ~/.claude/projects/<dash-encoded-repo-path>/memory/project-wiki.md \
     --current-state "Handoff command now emits structured events" \
     --what-was-done "Rewrote handoff skill to unified agent-skills format" \
     --what-was-done "Verified with grep-based checks" \
     --next "Run /garden to apply pending events" \
     --continuation-context "Branch main @ abc1234. The handoff rewrite is complete. Next session should run /garden to canonicalize the pending event into the workstream file and wiki chronology."
   ```

    Use one `new-handoff` invocation per *primary* workstream. If the session also touched *related* workstreams, include them as additional `--workstream` flags on the primary workstream's command.

6. **Do NOT hand-edit CLAUDE.local.md.** It is a generated view owned by `scripts/wiki-render.py`. The only permitted write during handoff is the scripted refresh in step 10 — never Write/Edit the file directly. The gardener (`/garden`) owns Quickstart synthesis and curation.

7. **Do NOT directly edit curated workstream files or the generated chronology.** All state flows through `wiki-event.py`. `wiki/log.md` is rendered, never hand-edited. The gardener applies pending events during `/garden`.

8. **Defer internal-doc sync.** Do not run `sync-docs` during handoff. It can
   create docs changes outside the garden checkpoint. Cite relevant source
   paths in the event. If the user needs copies in the wiki, report a separate
   follow-up: run
   `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki-event.py sync-docs`,
   review its exact diff, and use the `git-commit` workflow before starting
   garden.

9. **Regenerate the log projection.** Run:
   ```bash
   uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki-render.py log
   ```
   Your new event becomes a `wiki/log.md` entry. The pre-commit hook rejects
   commits whose staged events do not match the staged projection. Do not
   stage or commit any handoff output. Leave it unstaged for `/garden` to
   review and commit.

10. **Refresh the orientation index.** Run:
   ```bash
   uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki-render.py claude-local
   ```
   Without `--quickstart-file` this carries the existing Quickstart forward
   and regenerates the deterministic sections, so the
   `## Pending unreviewed handoffs` warning reflects your new event
   immediately instead of going stale until the next garden. If it fails
   because another garden holds the lock, report that and continue with
   steps 11-12 — the index refresh is not worth blocking the handoff. On any other error,
   report it and stop. Leave the refreshed `CLAUDE.local.md` unstaged with
   the other handoff output.

11. **Surface the unreviewed event count.** Run:
   ```bash
   uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki-event.py count-pending
   ```
   Report the printed integer to the user, e.g.:
   > "There are 3 unreviewed pending events. Run `/garden` to process them."

12. **Report.** Tell the user:
    - Which workstream event(s) you created
    - The event path(s) printed by `new-handoff`
    - The number of unreviewed pending events
    - Every dirty wiki path from
      `git -C <wiki-root> status --short --untracked-files=all`
    - That those paths are intentionally unstaged and uncommitted
    - Next owner: run `/garden` to review and commit the batch
    - Any deferred docs-sync follow-up that must finish before garden
