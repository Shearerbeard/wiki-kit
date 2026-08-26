# `wiki.toml` config schema

Status: RATIFIED at K1's Gate U, 2026-08-16 (board `wk`), alongside
the charter's decision rulings (`wiki-kit`, companion list approved).
Date: 2026-08-16, revised same day for Gate A findings. Covers all 16
knobs from `04-scripts-coupling.md`'s genericization surface; each key
cites the knob it retires and the consuming code. K2 implements this
schema; where decision 2 (companion list) rules differently, the
`[companions.<name>]` tables collapse to a single table with the same
keys.

Example values throughout describe a fictional deployment (a wiki named
`acme-notes` with one companion repo `widget`). The kit repo ships only
placeholders; a deployment's own private `wiki.toml` legally carries its
own org strings - the zero-aura boundary binds the kit repo, not a
consumer's config. The aura deployment's real values are written at the
K11 adoption, if K10 rules to proceed, in the private wiki repo.

## Two files, one rule

- `wiki.toml` - committed at the wiki repo root. Semantic,
  machine-independent configuration.
- `wiki.local.toml` - gitignored overlay beside it. Machine-local facts
  only.

The overlay is allowlisted, not open: it may set exactly
`companions.<name>.path`, `[memory.triage].extra_dirs`,
`[memory].projects_root`, and `[tools].*`. Any other key appearing in
`wiki.local.toml` is a doctor error - a machine overlay must never be
able to rewrite identity, contract, or protection semantics on one
machine. Within the allowlist, the overlay value wins.

The overlay lives at the MAIN checkout: a linked worktree of the wiki
repo reads the main checkout's `wiki.local.toml` through the git
common-dir rule, the same fallback the dock resolver uses for
consumers (a worktree's own overlay is ignored, never merged).

The split mirrors the dock's manifest/overlay rule
(`docking-spec-2026-08-16.md`): no machine paths in tracked files. The
doctor fails a committed `wiki.toml` containing an absolute or
home-relative filesystem path, with one documented exemption:
`[memory].index_line` is display text rendered verbatim into the
orientation index, never resolved as a path, and is exempt.

Knob 1 (wiki repo root): after extraction the scripts live in the kit
repo, so self-derivation from script location no longer works. Kit CLIs
resolve the wiki root through the docking resolution order (flag, env,
walk-up, common-dir, legacy - `docking-spec-2026-08-16.md`); a CLI
running inside the wiki repo itself finds it by walk-up to `wiki.toml`.

## Schema

```toml
[wiki]
# Knob 16 (cosmetic naming: Docker image/container prefixes) and the
# scheduler label prefix half of knob 12.
name = "acme-notes"           # slug; default: the repo directory name
# Knob 3's fallback half: the companion that resolves bare `#123`
# references and workstreams with no `repo:` field. Required when more
# than one companion is configured; defaults to the sole companion
# otherwise.
default_companion = "widget"

[memory]
# Knob 9: the memory-index pointer line rendered verbatim into the
# orientation index (today: MEMORY_LINE in wiki_render.py:73).
# Display-only text, never resolved; exempt from the no-paths doctor
# rule (see above). Optional; absent omits the line from the render.
index_line = "Memory: see your harness's per-project memory index"
# Knob 7's derivation rule: how a repo path becomes a Claude project
# slug (~/.claude/projects/<slug>). Stated once, consumed by the
# triage-dirs derivation and the doctor's budget check (knob 8).
project_slug_rule = "dash-encoded-absolute-path"   # the only rule v1 ships

[companions.widget]
# Knob 2 (companion repo pointer, today AURA_REPO/DEFAULT_AURA_REPO in
# six files) - keyed tables, one per consumer repo, per decision 2's
# proposal. The table key is the companion's name; it is the value a
# consumer's dock manifest carries in its [dock].companion key, which
# is how a resolved dock selects this table deterministically. Keyed
# tables (not an array) so the overlay merges per companion by name.
github = "acme/widget"        # knob 3: gh-sweep + workstream repo: fallback
                              # (build-index.py:33, wiki_gh_sweep.py:48,
                              # wiki_doctor.py:276). Optional: a companion
                              # with no GitHub presence omits it and the
                              # sweep skips this companion.
base_branch = "main"          # knob 4: topology diff base
                              # (generate-topology.py:15)
branch_glob = "alex/*"        # knob 6: cleanup-candidate namespace
                              # (generate-topology.py:211). Optional.
ticket_regex = "Ref: (WID-[0-9]+)"   # knob 5: ticket coverage
                                     # (generate-topology.py:17).
                                     # Optional; absent disables it.
docs_subpath = "docs/internal"  # knob 14: the internal-docs outbox,
                                # today hardcoded in 4 places (sync-docs,
                                # post-commit, generate-topology,
                                # handoff.ts). Lives here, not in the
                                # dock manifest - one home, no
                                # precedence question.
display_label = "widget main" # knob 10: the Uncommitted-Changes row
                              # label (wiki_render.py:408). Default: name.
posture = "committed"         # docking posture (spec); recorded here so
                              # the doctor can verify it, ruled per repo
                              # at K3 (decision 8)
memory_triage = true          # knob 7: include this companion's Claude
                              # project dir in memory triage. The triage
                              # set is derived: the wiki repo's OWN
                              # project slug (always included, computed
                              # from the resolved wiki root - it is not
                              # a companion and needs no listing), every
                              # companion with memory_triage = true (its
                              # overlay path slug-encoded per
                              # project_slug_rule), plus the overlay
                              # extra_dirs for family members that are
                              # neither. Replaces the 9-entry
                              # AURA_FAMILY_DIRS literal
                              # (wiki_memory_triage.py:91-101) and the
                              # AURA_MEMORY_PROJECT constant (knob 8,
                              # wiki_doctor.py:48). Migration check: an
                              # aura adoption (K11, only if K10 rules to
                              # proceed) must reproduce the live 9-entry
                              # set exactly, verified by comparing the
                              # derived set against the retired literal
                              # before the constant is deleted.

[contract]
# Knob 13: the single source of truth for the deny-rule and skill
# contract, consumed by installer, doctor, and install-smoke (today
# three independent copies: install.sh heredoc, wiki_doctor.py
# expected_config(), install-smoke/run.sh assertions). All three
# components from 04's table travel: protected paths, external allows,
# and the skill list.
protected = [
  "wiki/log.md", "wiki/log-legacy.md", "wiki/events/**",
  "wiki/pending/**", "wiki/quarantine.json", "wiki/log-epoch.json",
  ".wiki/orientation.md",     # the dock-era generated surface
  "CLAUDE.local.md",          # legacy surface; enforced only while the
                              # resolver's legacy fallback is active,
                              # and retired with it at an adoption K10
                              # rules to proceed
]
external_allow = [
  "scripts/wiki-event.py", "scripts/wiki-render.py",
  "scripts/wiki-garden.py",   # illustrative; K2 fills the real list
]
skills = ["garden", "handoff", "morning", "session-feedback"]
# Adoption-only: which skills the ADOPTION path installs machine-global.
# The K2-K10 kit installer ignores this key and installs project-scoped
# regardless (program rule: the production wiki's global symlinks stay
# byte-untouched before K11 - kimi round-1 finding on the program plan).
# It activates only at an adoption K10 has ruled to proceed, and the
# doctor first proves no same-name skill already resolves globally from
# another source.
global_skills = ["garden", "handoff", "morning"]

[schedule]
# Knob 12: scheduler units are installer-GENERATED from templates, never
# checked in; launchd cannot expand $HOME or read env vars, so the three
# com.aura.wiki-*.plist files cannot be parameterized in place. The
# label prefix derives from [wiki].name (com.<name>.wiki-*). Both
# scheduler targets ship, generated per machine with shared labels:
# launchd (templates/launchd/) on macOS, systemd user timers
# (templates/systemd/) on Linux. Known limitation: a wiki root with
# whitespace in its path renders fine but the systemd units may not run
# - systemd unquotes ExecStart= but not WorkingDirectory= or the
# StandardOutput= append: path, and the renderer warns on stderr.
night = "03:00"
morning = "08:30"
garden_reminder = "16:00"

[night]
# Knob 15: the report-path and commit-message conventions
# morning-reminder.sh re-derives independently today. One source here.
report_dir = "reports/night"
commit_prefix = "night:"

[kit]
# Installer-owned stamp (boardkit's stamp pattern): the contract
# version and kit commit this deployment was installed from. Rewritten
# on every install; the doctor flags drift between the stamp and the
# kit in front of it. Not a machine path, so committed is legal.
contract_version = 1
commit = "0000000000000000000000000000000000000000"
```

```toml
# wiki.local.toml (gitignored overlay; allowlisted keys only)

[companions.widget]
path = "/home/alex/src/widget"    # knob 2's machine half

[memory.triage]
# Knob 7 remainder: family members that are not companions (no dock, no
# sweep) but whose memory dirs the triage still reads. Slugs encode the
# machine's home directory, so they are overlay-only.
extra_dirs = ["-home-alex-src-widget-docs"]

[tools]
# Knob 11: binary locations (today the AURA_UV_BIN / AURA_NOTIFIER_BIN /
# AURA_GIT_BIN env defaults plus the one unguarded hardcode at
# install.sh:128).
uv = "/opt/homebrew/bin/uv"
notifier = "/opt/homebrew/bin/terminal-notifier"   # scripts/wiki-notify.sh wraps it: terminal-notifier (macOS), notify-send (Linux)
git = "/usr/bin/git"
# Where the kit checkout lives on this machine. Written by the
# installer; the pre-commit wrapper resolves the kit through this key
# at run time instead of baking a path into the hook.
kit = "/home/alex/src/wiki-kit"
```

## Knob coverage check

| Knob (04's numbering) | Where it lands |
|---|---|
| 1 wiki root | no key; resolved via the docking order, walk-up to `wiki.toml` inside the wiki repo itself |
| 2 companion path | `[companions.<name>]` + overlay `path` |
| 3 GitHub slug | `companions.<name>.github` + `[wiki].default_companion` for bare refs |
| 4 base branch | `companions.<name>.base_branch` |
| 5 ticket regex | `companions.<name>.ticket_regex` (optional) |
| 6 branch glob | `companions.<name>.branch_glob` (optional) |
| 7 triage family dirs | derived from `memory_triage` companions + overlay `extra_dirs` + `project_slug_rule` |
| 8 companion memory slug | derived from the same rule (constant retired) |
| 9 memory index line | `[memory].index_line` (display-only, exempt) |
| 10 display label | `companions.<name>.display_label` |
| 11 tool paths | overlay `[tools]` |
| 12 scheduler units | `[schedule]` + generated templates, label from `[wiki].name` |
| 13 deny rules + external allows + skill list | `[contract]` |
| 14 docs subpath | `companions.<name>.docs_subpath` |
| 15 night conventions | `[night]` |
| 16 image naming | derived from `[wiki].name` |

Out of config by design: the v1 `"aura"` envelope key and
`V1_REPO_NAME` legacy constants (permanent on-disk history shims,
04 finding 5), and the wiki's own layout names (`wiki/`, `workstreams/`,
`wiki/events/` - the system's directory convention travels with the
machinery unchanged; making layout a second config axis was considered
and rejected as surface without a consumer).
