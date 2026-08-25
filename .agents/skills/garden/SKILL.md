---
name: garden
# Rendered from the wiki-kit template by wiki-dock install; a reinstall
# overwrites this file.
description: |
  Rebuild the CLAUDE.local.md index from workstream files. Python scripts handle deterministic
  parts (validation, tree generation, stale detection, and the final CLAUDE.local.md render via
  wiki-render.py claude-local). You handle curation, merge proposals, and the Quickstart synthesis
  the renderer embeds.
compatibility: pi opencode claude-code codex
---

Rebuild the CLAUDE.local.md index from workstream files. Python scripts handle deterministic parts (validation, tree generation, stale detection, and the final CLAUDE.local.md render via `wiki-render.py claude-local`). Curate the content and write the Quickstart that the renderer embeds.

**Key concept: workstreams are goals, not sessions.** Multiple parallel sessions may contribute to the same workstream (e.g., 3 benchmark sessions all feed `evolution-benchmark`). A single session may also touch multiple workstreams. The workstream file represents the *current state of a goal*, not a session log.

**Approval model:** `/garden` is the interactive parent gardening process. Raw session evidence does not need approval, but semantic changes do: session-update curation, blocker cleanup, stale Next removal, merge/archive actions, and any cross-workstream canonicalization must be presented to the user before writing. Handoff agents may write their own session summaries; garden owns cross-session normalization.

**Append + curate model:** mechanical garden apply never rewrites curated sections. It appends each session's state as a dated, event-id-stamped block under `## Session updates (uncurated)` and updates frontmatter only. The curated sections (`### Current State`, `### Next`, `### Blockers`, `### What Was Done`, `### Continuation Context`) are rewritten only here, in step 2, with user approval.

Do the following in order:

0. **Record and approve the starting state.** Do this before any write:
   - Work in the wiki repo root (the directory containing `wiki.toml`) and run:
     ```bash
     GARDEN_STATE_DIR=$(mktemp -d /tmp/garden-state.XXXXXX)
     uv run --project {{KIT_ROOT}} \
       {{KIT_ROOT}}/scripts/wiki_checkpoint.py preflight \
       --state-dir "$GARDEN_STATE_DIR"
     ```
   - Stop on any error. The helper rejects staged changes, sync-doc output,
     unrelated dirt, malformed event ingress, and noncanonical event paths.
   - If the helper lists handoff ingress, show it to the user. After the user
     accounts for every path, record that approval:
     ```bash
     uv run --project {{KIT_ROOT}} \
       {{KIT_ROOT}}/scripts/wiki_checkpoint.py approve \
       --state-dir "$GARDEN_STATE_DIR" \
       --initial
     ```
   - Stop without writing if the user declines or cannot account for a path.

0.5. **Apply pending events.** If `wiki/pending/index.json` exists and `event_count > 0`, read the pending events and apply them:
   - Run `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki-event.py count-pending` to get the count
   - If the count is > 0, read `wiki/pending/index.json` to get the event paths
   - For each pending event, ask user confirmation, then run:
     `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki-garden.py <event_path>`
   - The apply only appends an uncurated session block and updates frontmatter
     `last_updated`, `branch`, and `sha`. It preserves the curated frontmatter
     blocker until step 2.
   - The script validates the event, refuses events that already have a
     garden-apply disposition (`--force` to override after asking the user),
     and rebuilds the pending index itself after each apply — no separate
     `build-pending` run needed
   - If the apply reports that no automatic route exists, show its proposed
     workstreams and ask the user to choose. A selected route must already
     appear exactly once in the event. Re-run the apply with
     `--workstream <approved-name>`; this records the route and an
     `applied-manually` disposition. Never invent a route or use the flag to
     replace an existing primary route. A sole `candidate_new` proposal still
     uses the normal per-event user confirmation above before automatic apply.
   - If an apply fails with "the apply stands", the workstream WAS updated
     and the garden-apply event WAS written; only the derived pending index
     failed. Do NOT re-run the same event — fix the reported cause, then run
     `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki-event.py build-pending`
   - On any other failure, report the error and stop before proceeding
   - After all applies, regenerate the log projection:
     `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki-render.py log`
     New handoff entries and garden dispositions appear in `wiki/log.md`.
     The pre-commit hook rejects commits that stage events without a
     matching re-render.
   - Immediately after each approved apply and the log render, run
     `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki_checkpoint.py status --state-dir "$GARDEN_STATE_DIR"`.
     Record only the literal paths produced by that approved action with the
     exact `approval_command` emitted by `status`.
     Do not approve a path merely because it is dirty.
   - If `status` reports `requires_adoption`, do not approve those handoffs as
     ordinary paths and do not restart after durable applies. Show every listed
     handoff to the user. The emitted commands are identical and include the
     complete concurrent set; after the user reviews all of them, run that one
     atomic command. Then process each handoff through the normal apply and
     curation flow. Adoption is additive and fail-closed: an unselected or
     later handoff, staged index, changed HEAD, or changed event stops the
     batch. Only canonical, untracked `pending_garden` handoffs qualify;
     duplicate selections, other event types, or baseline removal and mutation
     are rejected.
   - If the user declines an apply, do not write a disposition or workstream
     block. Leave the handoff pending and record the decline in the report.
     The final reviewed commit may still persist the original handoff ingress.
   - If there are no pending events or all applies are declined, skip to step 1

1. **Validate.** Run: `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/validate-workstreams.py`
   If errors, show them and ask user whether to continue or fix first.

1.5. **Triage session feedback.** Check if `wiki/feedback/` contains any `.md` files:
   `ls wiki/feedback/*.md 2>/dev/null`
   For each file, read its frontmatter. If `triaged` is `false`:
   - Read the contradiction descriptions in the file body.
   - Check whether the workstream file's curated sections still show the reported contradiction (it may have been fixed by a prior session or handoff).
   - If still present, note it for step 2 — prioritize addressing reported contradictions during curation.
   - If already resolved, ask before updating the file's frontmatter to
     `triaged: true`. After approval and the write, record that exact path with
     the exact `approval_command` emitted by
     `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki_checkpoint.py status --state-dir "$GARDEN_STATE_DIR"`.
   - Report how many feedback records were processed and how many remain open.
   If no feedback files exist or all are triaged, skip to step 2.

2. **Curate session updates (judgment — always confirm).** For each workstream file with blocks under `## Session updates (uncurated)`:
   - Read the blocks together with the current curated sections. Each block is one session's self-report, stamped with its source event_id; blocks are in chronological order.
   - Propose updated `### Current State`, `### Next`, `### Blockers`, and `### Continuation Context` sections that absorb the blocks, and propose appending each block's "What was done" items to `### What Was Done` under the block's date.
   - Within a workstream, newer blocks win on conflicts. Never silently drop a curated Next item or blocker that no block marks resolved — ask the user.
   - Show the user the proposed section diffs and write only after approval.
   - Immediately after each approved write, record the exact workstream path
     with the exact `approval_command` emitted by
     `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki_checkpoint.py status --state-dir "$GARDEN_STATE_DIR"`.
   - After writing, prune the absorbed blocks (keep the `## Session updates (uncurated)` heading and its italic note — the section stays, empty).
   - If frontmatter `blocker` disagrees with the curated Blockers section after absorption, fix frontmatter to match.
   - Skip workstreams with no uncurated blocks. If the user declines a workstream's curation, leave its blocks in place — they remain valid input for a future garden.
   - Treat a curation decline as "keep uncurated," not "discard." Do not edit
     curated sections or prune that workstream's blocks. Record the decline;
     any already-approved mechanical apply remains eligible for the final
     reviewed commit.

2.5. **Verify curation absorbed uncurated blocks (judgment).** For each workstream where you curated session blocks in step 2:
   - Re-read the workstream file.
   - Compare the most recent uncurated session block's **Next** items against the curated `### Next` section you just wrote. Every next-action from the newest block should appear (verbatim or semantically absorbed). If any were dropped, flag them.
   - Compare the most recent block's **Current state** items against `### Current State`. The curated section should reflect the latest state, not a stale older one.
   - If any item is present in the uncurated block but absent from the curated section without explicit user approval in step 2, show the specific discrepancy and ask whether to absorb it or acknowledge its omission.
   - If step 1.5 flagged open feedback contradictions for this workstream, verify they were addressed during curation. If not, surface them again.
   Skip this step if step 2 curated no workstreams.

3. **Staleness sweep (mechanical).** Run:
   `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki-gh-sweep.py`
   It resolves every PR/issue reference (frontmatter pr/issue/blocker, curated Current State/Next/Blockers text, archive index) to live GitHub state and checks all frontmatter + worktree branches for merged PRs no workstream mentions. Present the tiers to the user:
   - TIER 1 (frontmatter/blocker refs now MERGED/CLOSED): propose the frontmatter/blocker update; apply only with confirmation.
   - TIER 2 (curated-text refs now dead): candidate stale claims — fold into the step-2 curation conversation.
   - TIER 3 (merged PRs no workstream tracks): reverse staleness — the wiki missed shipped work; propose where it should be recorded.
   - TIER 4 (archive PRs still live-open): archive says shipped, GitHub disagrees — investigate before touching anything.
   - UNRESOLVABLE refs: likely typos or wrong-repo resolution (check `repo_source` in the line) — surface, don't guess.
   The script reports facts only; every edit it motivates goes through user confirmation. If it aborts (gh auth/network), say so and skip the step — do not hand-roll partial gh checks in its place.
   After each confirmed edit, record its exact path with
   the exact `approval_command` emitted by
   `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki_checkpoint.py status --state-dir "$GARDEN_STATE_DIR"`.

4. **Detect stale Next items (mechanical).** Run: `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/build-index.py --json`
   Read the JSON. For each active stream, compare its `### Next` bullets against `### What Was Done` entries in other workstream files. Flag any Next item that appears word-for-word (or near-match) in another file's completed work as "possibly done." Present these to the user — do not auto-remove.

5. **Propose workstream merges (judgment — always confirm).** Read at most the 5 most recently updated workstream files. If two files describe the same goal from different angles (e.g., they share branches, PRs, or overlapping Next items), propose merging them. Show the user both files side by side and ask which to keep as primary. **Never merge without explicit user confirmation.**

6. **Review for archival.** Using the `--json` output from step 4, count sessions in `wiki/log.md` after each stream's `last_updated`:
   `grep "^## \[" wiki/log.md | awk -F'[][]' -v d="<last_updated>" '$2 > d' | wc -l`
   Propose archiving any stream where ALL of:
   - `status` is `active` (NEVER auto-propose archiving `parked` streams — parked is an intentional human decision)
   - `last_updated` > 8 sessions ago
   - `### Next` section is empty or says "None"
   - `blocker` field is empty
   OR where `status` is explicitly `archived`.

   **Parked streams are protected.** A parked stream can be reactivated by any session — just update `status: active` and `last_updated` in its frontmatter. The garden will move it from PARKED to ACTIVE in the next index rebuild.

   **Confirm with user before moving files to `_archive/`.** Do not auto-archive.
   Use an unstaged filesystem move (`mv -- <source> <destination>`), never
   `git mv`; the checkpoint helper owns the index.
   After an approved merge or archive, record every literal source and
   destination path with the exact `approval_command` emitted by
   `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki_checkpoint.py status --state-dir "$GARDEN_STATE_DIR"`.

7. **Write Quickstart (judgment).** Read the 2-3 most recently updated active workstream files. Also check other family repos you know about (for each: `git -C <repo> status --short` + unpushed commits) — cross-repo facts and judgment warnings ("do not mix into other branches") belong here, since the generated Uncommitted Changes section only covers the wiki repo and its configured companions deterministically. Write a 3-5 line Quickstart that synthesizes the cross-stream picture, and save it to a temp file:
   `/tmp/garden-quickstart.md`

   **Example of a good Quickstart:**
   ```
   Branch: `main` @ `d4920ac5`. v1.20.0 in production.
   Hot: Qwen 3.6 Plus benchmarked (run 2 needs investigation). STDIO parity and OpenRouter ready for PR.
   Blocked: SSE PR #155 waiting on #100 — STDIO and TerminalBench depend on this.
   ```

   Keep it factual and concise. Someone reading this should know what's active, what's blocked, and what to do next in under 10 seconds.

   Do NOT include: `## ` headings, `Memory:`/`Detail:` lines, or `Pending events:` counts — those are renderer-owned and the renderer rejects them.

8. **Render CLAUDE.local.md.** Run:
   `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki-render.py claude-local --quickstart-file /tmp/garden-quickstart.md`

   The renderer owns everything else deterministically: banner, pending-events warning, workstream tree, recent sessions, uncommitted changes (wiki repo + configured companions and their worktrees), and the garden lock around the write. If it exits 1 with "garden lock not acquired", another garden is running — abort and tell the user. On any other error (quickstart rejected, missing repo, build-index failure), report it, fix the cause (usually the temp file), and re-run — do not work around the renderer. It warns on stderr if the file exceeds 80 lines; relay that warning. Never Write/Edit CLAUDE.local.md directly.
   Record each renderer-owned repo path changed by this step with
   the exact `approval_command` emitted by
   `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki_checkpoint.py status --state-dir "$GARDEN_STATE_DIR"`.

8.5. **Three-surface consistency check.** Run:
   `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/build-index.py --json`
   For each active workstream in the JSON output:
   - Read the workstream file's curated `### Next` first bullet.
   - Compare it to `next_actions[0]` from the JSON. They must match (both read the same file). If they differ, the file was modified between curation and render — flag loudly.
   Then check the Quickstart in the rendered `CLAUDE.local.md`:
   - For any workstream mentioned in the Quickstart, verify its branch/sha claims match the workstream file's frontmatter. A stale commit hash (e.g., Quickstart says `39ea5c60` but frontmatter says `61e80dc3`) means the Quickstart was not updated in step 7.
   - If any Quickstart claim contradicts a curated section, list the specific discrepancies.
   If all surfaces agree, report "Three-surface check: consistent" and proceed. If any disagree, present the discrepancies and ask the user whether to re-run steps 7 + 8 with corrections or accept the current state.

9. **Diff check.** Run `git diff CLAUDE.local.md` in the wiki repo and show the user a summary of what changed (added/removed streams, changed Quickstart, etc.). If any workstream disappeared from the tree without being archived, warn loudly.

10. **Report the garden result.** Show the user:
    - Line count (old vs new, from the diff)
    - Active/parked/archived counts
    - Feedback records triaged / remaining open (from step 1.5)
    - Curated workstreams + any declined curations (from step 2)
    - Absorption check results (from step 2.5)
    - Resolved blockers (from step 3)
    - Possibly-done Next items (from step 4)
    - Merge proposals (from step 5)
    - Archival proposals (from step 6)
    - Three-surface check result (from step 8.5)
    - Diff summary (from step 9)
    - If any triaged feedback files exist (`triaged: true`), note they can be deleted

11. **Build and verify the commit boundary.** Do not commit yet.
    - Run these deterministic checks and stop on failure:
      ```bash
      uv run --project {{KIT_ROOT}} \
        {{KIT_ROOT}}/scripts/wiki-render.py log --check
      uv run --project {{KIT_ROOT}} \
        {{KIT_ROOT}}/scripts/validate-workstreams.py
      uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki_doctor.py
      uv run --project {{KIT_ROOT}} \
        {{KIT_ROOT}}/scripts/wiki_checkpoint.py prepare \
        --state-dir "$GARDEN_STATE_DIR"
      {{KIT_ROOT}}/scripts/pre-commit
      ```
    - The helper owns NUL-safe parsing, the all-event baseline, literal-path
      staging, exact-manifest comparison, and concurrency checks. Do not stage
      paths outside the helper.
    - Run the `gate-probes` skill on the staged diff. Dispatch a context-isolated
      reviewer with the staged diff, starting status, user approvals, declined
      actions, and absorption criteria. If a fresh reviewer is unavailable,
      stop at Gate A. Resolve every finding or record the evidence for rejecting
      it.
    - If a check before `prepare` fails, stop with the index unchanged; there is
      no prepared manifest to unstage. If a later check or review fails after
      `prepare` succeeded, run
      `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki_checkpoint.py unstage --state-dir "$GARDEN_STATE_DIR"`.
      Preserve the working files and state directory. After an approved fix,
      record its path, rerun `prepare`, and repeat every check.
    - Finish with
      `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki_checkpoint.py verify --state-dir "$GARDEN_STATE_DIR"`.

12. **Request the commit gate.** Invoke the `git-commit` skill. Have it draft
    the exact message from the approved garden result and run its prose checks.
    Show the user:
    - the complete staged diff and NUL-safe path manifest in readable form;
    - deterministic and independent-review results;
    - declined applies or curations that remain pending/uncurated;
    - the exact commit message;
    - an explicit statement that garden will not push.

    Stop and wait for explicit approval. If the user declines the commit,
    preserve the working files and run
    `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki_checkpoint.py unstage --state-dir "$GARDEN_STATE_DIR"`.
    Report every dirty path and state that the tree is not ready for the night
    runner.

13. **Commit only after approval.** Immediately before committing, run
    `uv run --project {{KIT_ROOT}} {{KIT_ROOT}}/scripts/wiki_checkpoint.py verify --state-dir "$GARDEN_STATE_DIR"`.
    Stop on any
    error. Commit with the approved message; never push. Then require
    `git status --porcelain=v1 -z --untracked-files=all` to produce zero bytes.
    If the tree is not clean, report the remaining paths and do not claim the
    garden completed cleanly. Remove `$GARDEN_STATE_DIR` only after a clean
    commit; preserve it on failure and report its path.
