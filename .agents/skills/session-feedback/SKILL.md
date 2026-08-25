---
name: session-feedback
# Rendered from the wiki-kit template by wiki-dock install; a reinstall
# overwrites this file.
description: |
  File structured feedback about stale or contradictory wiki state so /garden can triage it.
  Use when a session loads and CLAUDE.local.md or workstream files contain outdated or
  conflicting information. User-invoked only.
compatibility: pi opencode claude-code codex
---

File structured feedback about stale or contradictory wiki state so `/garden` can triage it. Use this when a session loads and CLAUDE.local.md or workstream files contain outdated or conflicting information. User-invoked only.

**Key concept: feedback is a correction signal, not a handoff.** Feedback records describe what is wrong with the wiki's curated state right now. They land in `wiki/feedback/` as mutable working documents (not immutable events). Garden triages them during its sweep and marks them resolved. Once triaged, feedback files can be deleted during any future garden run.

**Scope boundary:** You may propose mechanical corrections to curated workstream sections (sha updates, removing merged blockers, pruning completed Next items). Do NOT rewrite `### Current State` or `### Continuation Context` narratives here -- those require `/garden` step 2 curation with user approval. Do NOT create handoff events, edit CLAUDE.local.md directly, or modify `wiki/events/` or `wiki/log.md`.

Do the following in order:

1. **Identify contradictions.** Read `CLAUDE.local.md` and the workstream file(s) relevant to this session. Check for:
   - Quickstart mentioning a branch or sha that does not match the workstream file's frontmatter `branch`/`sha`
   - `### Next` items that were completed (appear in `### What Was Done` of the same or another workstream, or are obviously done based on git state)
   - Blocker references to PRs or issues that are now merged or closed (run `gh pr view <number> --json state --jq .state` or `gh issue view <number> --json state --jq .state` to check)
   - Curated `### Current State` that contradicts the most recent block under `## Session updates (uncurated)`
   - `### Continuation Context` referencing a commit, branch, or state that no longer matches reality
   - CLAUDE.local.md Workstreams tree showing a Next action that differs from the curated `### Next` first bullet

   If you find no contradictions, tell the user and stop. Do not create an empty feedback record.

2. **Gather context.** Run these commands silently (do not display output) in the wiki repo root (the directory containing `wiki.toml`; resolve it through the dock if you are working in a consumer repo):
   ```bash
   git rev-parse --short HEAD
   ```
   -> `WIKI_SHA`
   ```bash
   date -u +%Y-%m-%dT%H:%M:%SZ
   ```
   -> `TIMESTAMP`

   Note which workstream(s) are affected. The reporter tool is your harness (one of: `claude-code`, `opencode`, `codex`, `pi`).

3. **Write the feedback record.** Create a file at:
   `wiki/feedback/<YYYY-MM-DD>-<workstream-name>.md`

   If that file already exists, check for the next available numeric suffix (`-2`, `-3`, etc.) by listing the directory.

   Format:
   ```markdown
   ---
   timestamp: "<TIMESTAMP>"
   workstream: "<name>"
   wiki_sha: "<WIKI_SHA>"
   reporter: "<tool, e.g. claude-code>"
   triaged: false
   ---

   ## Contradictions found

   ### 1. <Short description>
   - **Surface A** (<source file or section>): <what it says>
   - **Surface B** (<source file or section>): <what it says>
   - **Suggested correction:** <what the curated section should say>

   ### 2. ...
   ```

   Use `parse_frontmatter` conventions from the kit's `scripts/wiki_frontmatter.py`: quote all values, one key per line. Do NOT use `validate_frontmatter` on feedback files (it expects workstream-specific required fields that feedback files do not have).

4. **Optionally propose immediate corrections (judgment, always confirm).** For each contradiction:
   - If the fix is mechanical (sha mismatch, merged PR still listed as blocker, completed Next item still present), propose the specific edit to the workstream file's curated section.
   - Show the user the proposed changes and apply only with explicit approval.
   - After corrections, mark `triaged: true` in the feedback file's frontmatter.
   - If any corrections touched curated sections, re-render the orientation index:
     ```bash
     uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki-render.py claude-local
     ```
   - If corrections require judgment (rewriting Current State, choosing between conflicting Next items), do NOT apply them here. Leave `triaged: false` and note that `/garden` step 2 should handle it.

5. **Report.** Tell the user:
   - The feedback file path
   - How many contradictions were found and how many corrections were applied
   - Remind: `/garden` step 1.5 will triage remaining un-triaged feedback during its next run
   - Do not stage or commit the feedback file -- leave it unstaged for the next `/garden` run to pick up
