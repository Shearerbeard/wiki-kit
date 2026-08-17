# Extraction ledger

Freeze commit: `690b17a` (wiki repo `main`, 2026-08-16; the five scoped
directories carried no uncommitted changes at stamp time). K7
re-dispositions the delta from this commit before its canary key seals;
K10 runs the final refresh. Method inherited from boardkit's
`EXTRACTION.md`: one row per file, `git diff 690b17a..HEAD -- <file>`
drives every later refresh.

Status: RATIFIED at K1's Gate U, 2026-08-16 (board `wk`). The kit is
`wiki-kit` at `~/dev/wiki-kit` per the decision-1 ruling.

Coverage: 108 rows, one per file in the five-directory scope at the
freeze commit. The check queries the frozen tree, not the live index,
so it cannot drift as the repo moves:

```sh
git ls-tree -r --name-only 690b17a -- scripts/ tests/ schemas/ .agents/ docs/wiki-system/ | wc -l
```

Dispositions (boardkit's vocabulary): **port** - copy into the kit and
parameterize per the row's note; **author** - net-new kit artifact
informed by the source, which stays here; **template** - generic fill-in
shipped by the kit's installer or init; **sibling** - installed from a
separate repo, referenced, never vendored; **snapshot** - raw capture
kept as kit source material, stripped before publish; **dropped** -
deliberately not ported, stays in the content repo. Counts: 85 port,
13 author, 4 template, 2 snapshot, 4 dropped, 0 sibling (the label is
unused in this scope; the claude-skills distributor decision 12 may
reference is not a scoped file). Knob numbers cite
`04-scripts-coupling.md`; the config keys they land in are in
`wiki-toml-schema-2026-08-16.md`.

## .agents/skills/

| File | Disposition | Note |
|---|---|---|
| `.agents/skills/garden/SKILL.md` | port | ~30 literal `~/workspace/aura-session-docs` invocations resolve through the dock at K3; drop the hardcoded `~/dev/claude-skills` example; section-name conventions travel as layout convention |
| `.agents/skills/handoff/SKILL.md` | port | dock-resolved paths; step 3's machine-specific memory path derives from config (knobs 7/8); `--tool` enum audited at K2; worked example re-fixtured |
| `.agents/skills/morning/SKILL.md` | port | dock-resolved paths; launchd label derives from `[wiki].name` (knob 12); the missing `compatibility:` key gets an explicit ruling at K3 (05 open question) |
| `.agents/skills/session-feedback/SKILL.md` | port | dock-resolved paths; the reporter-tool hardcode in step 2 fixed |

## docs/wiki-system/

| File | Disposition | Note |
|---|---|---|
| `docs/wiki-system/adr/0008-operating-model-and-safety-boundaries.md` | author | the live ADR stays governing this wiki; the kit writes its own operating-model doc against the ported mechanism; decision 7's ruling supersedes its Obsidian later-layer language for the kit |
| `docs/wiki-system/adr/0009-curation-model-and-projection-ownership.md` | author | kit curation-model doc restates V1-V8 without aura examples; the ADR stays |
| `docs/wiki-system/adr/0010-night-shift-operating-model.md` | author | kit night-shift doc carries the T0-T3 trust ladder; the live ADR keeps governing the production streak through K11 |
| `docs/wiki-system/adr/0011-prose-first-workstreams-and-typed-state.md` | author | kit workstream-model doc; K5's decision 5 extends it |
| `docs/wiki-system/adr/0012-token-budgets.md` | author | kit budget doc written at K4 per decision 6; the known warn/hard numeric drift (06 finding 10) reconciles there, never inherits silently |
| `docs/wiki-system/adr/0013-cheap-model-verification-framing.md` | author | kit verification-framing doc; feeds K7's canary tiering |
| `docs/wiki-system/cheap-model-curation.md` | port | generic method prose; aura examples re-fixtured |
| `docs/wiki-system/entity-pages.md` | port | mechanism doc, already generic; path examples updated |
| `docs/wiki-system/flows/information-layers.mmd` | author | diagram redrawn for the generic deployment |
| `docs/wiki-system/flows/repo-boundary.mmd` | author | redrawn; the aura/wiki boundary becomes kit/wiki/consumer |
| `docs/wiki-system/flows/session-lifecycle.mmd` | author | redrawn against the kit's install surface |
| `docs/wiki-system/flows/wiki-memory-system.md` | author | rewritten alongside its diagram |
| `docs/wiki-system/flows/wiki-memory-system.mmd` | author | redrawn |
| `docs/wiki-system/memory-triage.md` | port | references to `AURA_FAMILY_DIRS` become the knob-7 config derivation |
| `docs/wiki-system/opencode-permissions-research-2026-06-09.md` | snapshot | dated deny-rule research; source material for K2's enforcement-contract doc, stripped before publish |
| `docs/wiki-system/plan-vetted-2026-06-09.md` | dropped | this repo's own planning history |
| `docs/wiki-system/primer.md` | author | the kit primer is written fresh; the live primer names aura boards and history |
| `docs/wiki-system/roadmap.md` | dropped | the live wiki's roadmap |
| `docs/wiki-system/runbooks/human-understanding-gates.md` | port | generic gate method; aura-specific gate references re-fixtured |
| `docs/wiki-system/runbooks/night-shift.md` | port | K4 rewrites the budget sections; scheduler names from config (knob 12) |
| `docs/wiki-system/runbooks/phase-5-host-smoke.md` | snapshot | draft host-smoke protocol; K7 builds the real bridge and supersedes or updates it (K7 Gate D) |
| `docs/wiki-system/workstream-template.md` | template | ships as seed data per decision 4 |

## schemas/

| File | Disposition | Note |
|---|---|---|
| `schemas/README.md` | port | registry-only rule (D9) ports verbatim; path examples updated |
| `schemas/capture-manifest.schema.json` | port | `$id` host renamed with the kit |
| `schemas/common/types.json` | port | `$id` host renamed with the kit |
| `schemas/events/_index.json` | port | the registry stays the only schema source of truth |
| `schemas/events/garden-apply-v1.schema.json` | port | `$id` host renamed with the kit |
| `schemas/events/handoff-v1.schema.json` | port | `$id` host renamed with the kit (schema metadata, not event data); the required top-level `"aura"` key is the permanent on-disk legacy shim (04 finding 5); travels documented, a fresh consumer never writes v1 |
| `schemas/events/handoff-v2.schema.json` | port | `$id` host renamed with the kit; otherwise frozen - K5's decision 5 adds a v3 schema or a new event type beside it, never edits v2 (never-loosened, registry-only) |
| `schemas/log-epoch.schema.json` | port | `$id` host renamed with the kit |
| `schemas/pending-index.schema.json` | port | `$id` host renamed with the kit |
| `schemas/quarantine.schema.json` | port | `$id` host renamed with the kit |
| `schemas/workstream-state.json` | port | `$id` host renamed with the kit |

## scripts/

| File | Disposition | Note |
|---|---|---|
| `scripts/backfill-gap-events.py` | dropped | aura content hardcoded in `GAP_ENTRIES`; one-time migration, already run |
| `scripts/build-index.py` | port | `DEFAULT_REPO` fallback becomes `companions.github` (knob 3); K4 adds budget selection |
| `scripts/com.aura.wiki-garden-reminder.plist` | template | launchd cannot expand `$HOME`; installer generates units per machine from templates (knob 12); the static file does not travel |
| `scripts/com.aura.wiki-morning-reminder.plist` | template | same as above |
| `scripts/com.aura.wiki-night-shift.plist` | template | same as above; systemd timer template is K9's Linux twin |
| `scripts/garden-lock.py` | port | already generic |
| `scripts/garden-reminder.sh` | port | `AURA_*` env names retired for `[tools]` overlay values (knob 11); notifier abstraction at K3 |
| `scripts/generate-topology.py` | port | `FEATURE_BRANCH`, `TICKET_RE`, `mshearer/*` glob, output path become companion config (knobs 4, 5, 6, 14); ticket regex optional, off when absent |
| `scripts/generate-topology.sh` | port | trivial wrapper |
| `scripts/handoff.ts` | port | `HANDOFF_PATH` from `companions.docs_subpath` (knob 14) |
| `scripts/install-smoke/Dockerfile` | port | generic already |
| `scripts/install-smoke/README.md` | port | aura references rewritten |
| `scripts/install-smoke/run.sh` | port | image names from `[wiki].name` (knob 16); assertions consume the shared `[contract]` source (knob 13); the hardcoded notifier fixture path parameterized |
| `scripts/install.sh` | author | rewritten: installs the pre-commit hook (03's largest gap), consumes `[contract]` instead of the duplicated heredoc, generates scheduler units from templates, docks consumers posture-aware |
| `scripts/morning-reminder.sh` | port | tool paths from overlay (knob 11); report/commit conventions from `[night]` (knob 15) |
| `scripts/post-checkout` | dropped | existed to recreate the `CLAUDE.local.md` symlink per worktree; the dock's common-dir fallback replaces it, and the entry shim resolves through the dock rather than storing a path, so linked worktrees need no per-worktree file at all |
| `scripts/post-commit` | port | output paths from `companions.docs_subpath` (knob 14) |
| `scripts/pre-commit` | port | as-is (no aura strings); K2 wires it into the installer and adds `wiki/pending/**` coverage |
| `scripts/validate-workstreams.py` | port | K2 fixes the non-recursive glob that makes `_archive`/`_reference` validation a no-op |
| `scripts/wiki-doctor.py` | port | thin shim, travels with its target |
| `scripts/wiki-event.py` | port | symlink, travels with its target |
| `scripts/wiki-garden.py` | port | symlink |
| `scripts/wiki-gh-sweep.py` | port | symlink |
| `scripts/wiki-memory-triage.py` | port | symlink |
| `scripts/wiki-render.py` | port | symlink |
| `scripts/wiki_checkpoint.py` | port | layout-convention paths only, already generic |
| `scripts/wiki_doctor.py` | port | `DEFAULT_AURA_REPO`/`AURA_MEMORY_PROJECT` become config (knobs 2, 8); `expected_config()` consumes `[contract]` (knob 13); `STALE_README_PATTERNS` deleted, not parameterized - it detects one past README revision |
| `scripts/wiki_event.py` | port | `AURA_REPO` default becomes companion config (knob 2); `V1_ENVELOPE_KEY`/`V1_REPO_NAME` stay as documented legacy constants; `Tool` enum audited at K2 (the `pi` member) |
| `scripts/wiki_frontmatter.py` | port | fully generic |
| `scripts/wiki_garden.py` | port | layout-convention coupling only |
| `scripts/wiki_gh_sweep.py` | port | `DEFAULT_REPO` and the worktree-branch tagging assumption become per-companion config (knobs 2, 3) |
| `scripts/wiki_lock.py` | port | fully generic |
| `scripts/wiki_memory_triage.py` | port | `AURA_FAMILY_DIRS` becomes the derived triage set (knob 7: the wiki's own slug always included, companions with `memory_triage = true` via the slug rule, plus `extra_dirs`); keeps fail-loud on missing dirs; K11 migration verifies the derived set reproduces the live 9-entry list before the literal is deleted |
| `scripts/wiki_night.py` | port | passes config through to `wiki_gh_sweep`/`wiki_render` subprocesses instead of letting them inherit hardcoded defaults (04 finding) |
| `scripts/wiki_render.py` | port | `DEFAULT_AURA_REPO`, `MEMORY_LINE`, `"aura main"` label become config (knobs 2, 9, 10); `collect_uncommitted_facts` loops companions per decision 2; the zero-event smoke's boot requirements (legacy log, pre-existing output, one commit) get K2 answers per decision 4 |

## tests/

| File | Disposition | Note |
|---|---|---|
| `tests/garden/README.md` | port | fixture-corpus doc; updates with the re-fixtured corpus |
| `tests/garden/agent-driver-reactivated.md` | port | aura-flavored sample content re-fixtured generic at K2 (the grep sweep's fixture scope) |
| `tests/garden/context-budget-monitoring-new.md` | port | re-fixtured generic at K2 |
| `tests/garden/evolution-benchmark-updated.md` | port | re-fixtured generic at K2 |
| `tests/garden/expected-checks.py` | port | assertions track the re-fixtured corpus |
| `tests/garden/run-tests.sh` | port | path resolution via dock/repo-relative |
| `tests/garden/scenario-a-log-entries.md` | port | re-fixtured generic at K2 |
| `tests/garden/sse-transport-resolved.md` | port | re-fixtured generic at K2 |
| `tests/notifications/test_garden_reminder.py` | port | the plist-literal equality assertions (04) rewrite against installer-rendered templates, in lockstep with knob 12 |
| `tests/notifications/test_morning_reminder.py` | port | conventions read from `[night]` (knob 15) |
| `tests/notifications/test_morning_skill_contract.py` | port | tracks the ported morning skill |
| `tests/wiki-checkpoint/test_wiki_checkpoint.py` | port | tmp_path-based, generic |
| `tests/wiki-doctor/test_wiki_doctor.py` | port | independent `"mezmo/aura"` literals re-anchored to config values (04's dual-edit list) |
| `tests/wiki-event/invalid-bad-enum.json` | port | schema fixture, generic |
| `tests/wiki-event/invalid-bad-event-type.json` | port | schema fixture |
| `tests/wiki-event/invalid-bad-status.json` | port | schema fixture |
| `tests/wiki-event/invalid-bad-tool.json` | port | schema fixture; tracks the K2 `Tool` enum audit |
| `tests/wiki-event/invalid-bool-schema-version.json` | port | schema fixture |
| `tests/wiki-event/invalid-empty-string-in-ws-array.json` | port | schema fixture |
| `tests/wiki-event/invalid-missing-required.json` | port | schema fixture |
| `tests/wiki-event/invalid-non-slug-source-kind.json` | port | schema fixture |
| `tests/wiki-event/invalid-uppercase-event-id.json` | port | schema fixture |
| `tests/wiki-event/invalid-ws-wrong-type.json` | port | schema fixture |
| `tests/wiki-event/source-note.md` | port | capture fixture |
| `tests/wiki-event/test_wiki_event.py` | port | v1 legacy-shim tests keep their aura literals by design - the one allowed enclave in K2's grep sweep |
| `tests/wiki-event/valid-handoff-event.json` | port | repo-name strings re-fixtured except where exercising the v1 shim |
| `tests/wiki-event/valid-partial-workstream-state.json` | port | re-fixtured as needed |
| `tests/wiki-event/valid-with-workstream-state.json` | port | re-fixtured as needed |
| `tests/wiki-frontmatter/test_wiki_frontmatter.py` | port | generic |
| `tests/wiki-garden/test_wiki_garden.py` | port | generic |
| `tests/wiki-gh-sweep/test_wiki_gh_sweep.py` | port | literal `"mezmo/aura"` assertions re-anchored to config (04's dual-edit list) |
| `tests/wiki-lock/test_wiki_lock.py` | port | generic |
| `tests/wiki-memory-triage/test_wiki_memory_triage.py` | port | already patches the family-dirs attribute; the patch target renames with the config move |
| `tests/wiki-night/test_wiki_night.py` | port | tracks the config pass-through change |
| `tests/wiki-render/test_build_index_truncation.py` | port | generic; K4 budget tests extend it |
| `tests/wiki-render/test_wiki_render.py` | port | the `"aura main"` label assertion re-anchors to `companions.display_label` (knob 10) |

## Addendum: load-bearing files outside the scoped directories

The acceptance scope fixes the five directories above; these repo-root
files are machinery the kit cannot run without (the zero-event smoke
had to copy the first two into its fixture) and are dispositioned here
so the port has no silent gap. They sit outside the 108-row coverage
count by design.

| File | Disposition | Note |
|---|---|---|
| `pyproject.toml` | port | the kit's packaging: dependency pins (`jsonschema` hard dep per D9), ruff and pytest config; project name renamed with the kit |
| `uv.lock` | port | regenerated in the kit repo from the ported `pyproject.toml` |
| `.vale.ini` | port | the prose-lint contract for kit docs; path scopes rewritten for the kit layout |

## K2 re-dispositions (2026-08-16)

Recorded at K2's Gate D per the card's ledger-update duty; the ratified
rows above stay as ratified, and this section holds the deltas the port
actually shipped. Sources: the Gate A ledger audit (gpt-5.6-sol) and
the board owner's build log.

| Row | Delta |
|---|---|
| `scripts/handoff.ts` | port -> template: ships as `templates/handoff.ts.template` with a `{{DOCS_SUBPATH}}` placeholder; K3 renders it at dock time |
| `scripts/pre-commit` | the installed form is a generated wrapper pinning the installing interpreter and exec-ing the kit's current hook, not a symlink: `#!/usr/bin/env python3` under git resolves to a PATH python that may lack jsonschema |
| `scripts/com.*.plist` rows | the rendered launchd templates carry `HOME`, `LOG_DIR`, and `PATH` placeholders beyond the originally named set - launchd expands nothing, so every runtime-derived value renders in full |
| `scripts/install-smoke/run.sh` | knob 16 lands as repo-directory-derived naming for the kit's own harness (it has no deployment config at build time); a deployment smoke (heavy-canary stage) takes its prefix from `[wiki].name` |
| `scripts/wiki_event.py` | Tool enum audit outcome: `pi` DROPPED (zero stored events carry it, so the adoption migration is unaffected); `claude-code`, `opencode`, `codex`, `manual` kept as generic; per-deployment enum extension rejected because event validity must never be deployment-relative (registry-only, one loader) |
| `schemas/events/handoff-v1.schema.json` | tool enum narrowed with the audit; title reworded to name the format, not the source family (the `"aura"` envelope property itself is untouched) |
| `schemas/events/handoff-v2.schema.json` | tool enum narrowed with the audit; the description's source-family mentions reworded neutrally (schema metadata, not the on-disk contract) |
| `tests/garden/*` sample rows | four fixture files renamed with their content re-fixturing: `agent-driver-reactivated` -> `task-router-reactivated`, `context-budget-monitoring-new` -> `usage-budget-monitoring-new`, `evolution-benchmark-updated` -> `eval-benchmark-updated`, `sse-transport-resolved` -> `sync-transport-resolved` |
| `scripts/wiki_render.py` | the decision-4 boot answers shipped: legacy log and epoch are optional as a pair; the orientation skeleton renders from `templates/orientation-quickstart.md` at init |
