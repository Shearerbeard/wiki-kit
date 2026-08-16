# Wiki kit charter

Status: RATIFIED at K1's Gate U, 2026-08-16 - Mike ruled all six
reserved decisions in an interactive sitting (rulings recorded inline
below) and ratified the extraction ledger and docking spec. The kit is
named **wiki-kit**, repo at `~/dev/wiki-kit`.
Date: 2026-08-16. Author: the K1 board-owner session.
Companion artifacts: `extraction-ledger-2026-08-16.md` (per-file
dispositions, freeze commit in its header), `docking-spec-2026-08-16.md`
(the `.wiki/` convention), `wiki-toml-schema-2026-08-16.md` (the config
surface), `k1-unknowns-closure-2026-08-16.md` (evidence for the closed
recon unknowns). Fact base: recon artifacts 01-13 in this directory.

## What the kit is

The kit is the generic wiki machinery extracted from `aura-session-docs`:
an append-only event store with schema-registry validation, deterministic
projections (`wiki/log.md`, the orientation index), the garden and night
pipelines, the four workflow skills, the doctor, the installer, and the
install-smoke harness. It installs into a blank repo, docks consumer
repos through a `.wiki/` directory, and runs on macOS and Linux. The
guiding line is inherited unchanged: afraid of remembering the wrong
thing. Events stay append-only, dispositions stay join-derived, curation
stays gated.

## Repo boundary

Three repos, three roles:

- **The kit repo** (name and location: reserved decision 1) holds
  machinery only: scripts, tests, schemas, skills, installer, docking
  resolver, and the kit's own docs. Zero aura strings outside the
  documented v1 legacy shim and its tests (K2's grep-sweep gate).
- **A wiki repo** (per deployment) holds content: events, workstreams,
  projections, entity pages, reports, and its own `wiki.toml` +
  `wiki.local.toml` config pair. `aura-session-docs` becomes one of
  these at K11 if K10 rules to proceed, and keeps its full history
  either way (decision 10 only governs the kit repo's history).
- **Consumer repos** (companions) carry a `.wiki/` dock and whatever
  posture-appropriate orientation shims the docking spec names. They
  never carry machinery.

The live wiki is untouched before K11 (program non-goal 1). The
machine-global skill symlinks serving the production wiki are untouched
before K11 (kimi finding 1); kit skills install project-scoped into
consumers until adoption.

## Binding constraints inherited by the kit

- Northstar decision 1: new domains arrive as new event types plus an
  area workstream flavor, never as loosened schemas.
- D9/D10: registry-only schemas, one fail-loud loader, event
  immutability, corrections as new events, join-derived disposition.
- The 2026-08-09 Gate A ruling: the kit carries a durable audit trail of
  its review artifacts as a first-class feature.
- The v1 envelope key literally named `"aura"` is permanent on-disk
  legacy; it travels as a documented constant in the event loader
  (04 finding 5), never as a config value.
- The 2026-06-27 symlink-not-copy ruling stands unless decision 12
  (ruled at K3) supersedes it with a staleness-checked copy flow.

## Reserved decisions ruled at this gate

Each entry states the question, the evidence, this charter's proposal,
and a Ruling line Mike fills at the Gate U sitting. A proposal is not a
ruling; nothing below executes until ruled.

### Decision 1 - kit name and repo location

Evidence: `~/dev/mechanical-wiki/` exists as an empty vault stub; the
system's character is mechanical apply (T0 night runs, byte-equality
projections, join-derived state). The docking dir name is `.wiki/`
regardless of the kit name, per the docking spec.

Proposal: name the kit `mechanical-wiki`, repo at
`~/dev/mechanical-wiki`, fresh repo (see decision 10).

Ruling (2026-08-16, Mike, Gate U sitting): APPROVED WITH A NAME TWEAK.
The kit is `wiki-kit`, repo at `~/dev/wiki-kit`; the `.wiki/` dock name
stands. The empty `~/dev/mechanical-wiki` vault stub is unused and left
for manual deletion.

### Decision 2 - companion repos: single or list

Evidence: the render/doctor/installer code assumes exactly one companion
(04 knob 2); the aura family itself spans nine memory-triage dirs and
multiple checkouts; SRE-style families want N; the zero-event smoke
showed the single hardcoded default silently reading the wrong repo on a
foreign install.

Proposal: a list. Keyed `[companions.<name>]` tables in `wiki.toml`,
machine paths in the `wiki.local.toml` overlay, and every consumer of
today's `AURA_REPO` single value loops or takes a named companion. A
required `default_companion` resolves bare `#N` references and
workstreams with no `repo:` field whenever more than one companion is
configured. The single-repo call sites named in the ledger are K2's
rewrite scope.

Ruling (2026-08-16, Mike, Gate U sitting): APPROVED as proposed.

### Decision 3 - docking resolver: vendor now or wait for boardkit

Evidence: Mike's 2026-08-11 direction proposes one shared docking spec;
the boardkit maintainer has not disposed it; K3 needs a resolver to
build against.

Proposal: vendor the convention now, mirroring boardkit's proven
resolution order so the shapes stay congruent; track the boardkit inbox
item and swap in the shared library at K10 if the maintainer ships one.

Ruling (2026-08-16, Mike, Gate U sitting): APPROVED as proposed.

### Decision 4 - blank-repo seed: zero events or a starter event

Evidence: the zero-event smoke proves `build-pending` handles an empty
store cleanly, but the log renderer hard-requires a non-empty
`wiki/log-legacy.md` and the orientation renderer requires a pre-existing
output file with a Quickstart, plus one git commit
(`k1-unknowns-closure-2026-08-16.md` item 2).

Proposal: zero events, no synthetic entries. K2 makes the legacy log
optional (absent means the deployment has no legacy epoch and the
projection renders from events alone), and the kit's init step creates
the initial commit and a skeleton orientation file with an empty-state
Quickstart - covering all three boot requirements the smoke exposed.
Seed data proper is a workstream template, a home note, and the config
pair. The first handoff creates the first event.

Ruling (2026-08-16, Mike, Gate U sitting): APPROVED WITH A TWEAK. Zero
events stands, and the init path must also accept a repo that already
carries docs directories or a pre-existing Obsidian vault as its
starting place, installing around them non-destructively. This is not
event seeding; it eases the on-ramp. Binds K2 (init tolerates existing
content) and K6 (an existing `.obsidian/` is never clobbered).

### Decision 7 - Obsidian first-open readability: kit requirement or later layer

Evidence: ADR 0008 scopes Obsidian to a later layer; Mike's term of
completion 6 names first-open readability; recon 08 found 162 unchecked
wikilinks and an inferred-only cold-open experience.

Proposal: promote to a kit requirement, executed at K6. The ADR 0008
language is superseded for the kit by this ruling (Gate D reconciliation
records it).

Ruling (2026-08-16, Mike, Gate U sitting): APPROVED - promoted to a
kit requirement, executed at K6; ADR 0008's later-layer language is
superseded for the kit. The decision-4 tweak makes this load-bearing:
the existing-vault on-ramp needs first-open to work.

### Decision 10 - kit repo history: fresh start or filter-repo carve-out

Evidence: boardkit started fresh with `EXTRACTION.md` as provenance; a
carve-out would drag aura content through history rewriting; the wiki
repo keeps its full history either way.

Proposal: fresh start. The extraction ledger records provenance
(source path, freeze commit, disposition) for every ported file.

Ruling (2026-08-16, Mike, Gate U sitting): APPROVED - fresh start; the
ledger is the provenance record.

## Reserved decisions deferred, with their gates

| # | Decision | Ruled at |
|---|---|---|
| 5 | Non-code support: typed context on handoff-v3 vs a new event type | K5 Gate U |
| 6 | Budget overflow: amend ADR 0012 for overflow-to-compact index rendering | K4 Gate U |
| 8 | Consumer posture per repo (chore-lottery, notanton clones) | K3 Gate U |
| 9 | Content-repo canonicalization dispositions (aura-reports, diagrams, plan archives, night-report retention) - K1 evidence for all four attached in `k1-unknowns-closure-2026-08-16.md` items 3-5 | K10 Gate U |
| 11 | Harness scope for kit v1 (which of Claude Code, OpenCode, codex, agy get a supported contract) | K3 Gate U |
| 12 | Skill distribution: symlink-from-checkout vs staleness-checked copy flow | K3 Gate U |

Each row is a recorded deferral in the acceptance sense: it names the
decision and the gate that rules it, and points at the evidence trail,
so none is silently open.

## What ratification unlocks

With rulings recorded on the card, K1 closes by naming the freeze commit
in the ledger header (already stamped), merging the ledger and docking
spec into the kit repo's `docs/` under whatever name decision 1 rules,
and promoting K2 (extract the kit core) to ready.

## Adversarial review ledger (Gate A)

Author of all five K1 artifacts: Claude Fable 5 (claude-fable-5).
Reviewers, per the wk board's REVIEW-TOOLING contract: gpt-5.6-sol via
`codex exec --sandbox read-only` (repo-native), and kimi k3 via
`opencode run --agent build -m kimi-for-coding/k3` (staged packet; the
fireworks pin remains suspended, and the first two opencode attempts
failed the validity rule - a plan-mode reply asking for approval, then
an empty return - before the build-agent dispatch produced a full
report; recorded per the three-attempt cap). Both reviewed the
2026-08-16 drafts against the full recon fact base. Raw outputs in the
session task logs; findings restated with dispositions.

### Round 1 - gpt-5.6-sol (codex lane): 13 BLOCKING, 2 MINOR, VERDICT: FAIL

| # | Finding (compressed) | Disposition |
|---|---|---|
| 1 | BLOCKING - wiki root still self-derived from script location, wrong once scripts live in the kit repo | Fixed: knob 1 resolves through the docking order; walk-up finds `wiki.toml` inside the wiki repo itself |
| 2 | BLOCKING - `[[companions]]` array cannot merge with a keyed-table overlay | Fixed: keyed `[companions.<name>]` tables in both files, per-name merge |
| 3 | BLOCKING - committed example carries machine paths and aura identity against its own rule | Fixed: fictional `acme-notes` deployment throughout; `index_line` documented as display-only and exempt; `extra_dirs` moved to the overlay; note that a deployment's private config legally carries its own org strings |
| 4 | BLOCKING - overlay unconstrained, a machine file could rewrite contract or identity | Fixed: overlay allowlisted to `companions.<name>.path`, `extra_dirs`, `[tools].*`; anything else is a doctor error; dock overlay allowlisted to `[dock].path` |
| 5 | BLOCKING - no default-companion rule for bare `#N` refs under a companion list | Fixed: `[wiki].default_companion`, required with more than one companion; charter decision-2 proposal updated |
| 6 | BLOCKING - `[contract]` missing `external_allow`; protects retiring `CLAUDE.local.md`; omits `.wiki/orientation.md` | Fixed: all three contract components present; orientation surface protected; `CLAUDE.local.md` scoped to the legacy fallback's lifetime |
| 7 | BLOCKING - `global_skills` pre-introduces a forbidden global install surface | Fixed: annotated adoption-only, ignored by the K2-K10 installer, doctor shadow-check at activation |
| 8 | BLOCKING - posture rules contradict the committed-shim requirement | Fixed: shims follow the posture (tracked / gitignored / info-excluded, generated per-clone in the untracked postures) |
| 9 | BLOCKING - resolver inputs ambiguous, manifest keys unspecified | Fixed: `--wiki` = wiki root, `WIKI_DOCK` = dock dir, exact `[dock]` keys given, identity chain doctor-verified, missing-overlay behavior defined |
| 10 | BLOCKING - schema rows "as-is" would port aura-branded `$id` hosts | Fixed: all nine schema rows carry the `$id` rename (verified: all nine files carry the aura host) |
| 11 | BLOCKING - "K5 evolves handoff-v2" implies mutating a frozen schema | Fixed: v2 frozen; K5 adds a v3 schema or new event type beside it |
| 12 | BLOCKING - decision 4 proposal not actionable (unresolved either-or, omits the initial-commit requirement) | Fixed: single mechanism - K2 makes the legacy log optional, init creates the initial commit and skeleton orientation file |
| 13 | BLOCKING - decision-9 evidence attachment incomplete | Fixed: diagrams, plan-archive read audit, and night-retention evidence gathered and appended as closure item 5 |
| 14 | MINOR - K11 adoption stated as settled fact | Fixed: conditional on K10's ruling |
| 15 | MINOR - coverage command not pinned to the freeze commit | Fixed: `git ls-tree -r --name-only 690b17a` form |

### Round 1 - kimi k3 (opencode lane): 2 BLOCKING, 7 MINOR, VERDICT: FAIL

Reviewed the same pre-fix drafts; overlaps with the codex round are
marked.

| # | Finding (compressed) | Disposition |
|---|---|---|
| 1 | BLOCKING - `external_allow` missing from `[contract]` (knob 13 two-thirds covered) | Fixed with codex finding 6 |
| 2 | BLOCKING - committed-posture linked worktree breaks: tracked manifest checks out, gitignored overlay does not, and walk-up stops on the incomplete dock | Fixed: incomplete dock falls through to the common-dir fallback; fail-loud only when no step completes; identity chain still binds |
| 3 | MINOR - walk-up unbounded, can hijack an unrelated ancestor `.wiki/` | Fixed: walk-up bounded at the repository toplevel |
| 4 | MINOR - generated surfaces absent in worktrees; untracked postures lack an orientation story; invisible-posture skill installs leak in `git status` | Fixed: generated surfaces read through the resolved dock; per-posture shim rules added; the install step widens the posture's exclusion set to everything it writes |
| 5 | MINOR - knob-7 derivation drops the wiki's own project dir and non-companion family members | Fixed: wiki's own slug always included; `extra_dirs` for the rest; K11 migration check reproduces the live 9-entry set |
| 6 | MINOR - `~/`-rooted example passes the stated absolute-path doctor check | Fixed: doctor rule covers home-relative paths; the example no longer carries a path |
| 7 | MINOR - `docs_subpath` specified on both sides of the dock | Fixed: manifest is identity-only; all semantic per-consumer config lives in the companions table |
| 8 | MINOR - `global_skills` lacks the inert-before-K11 annotation | Fixed with codex finding 7 |
| 9 | MINOR - `pyproject.toml`/`uv.lock` unledgered and outside the drift refresh | Fixed: ledger addendum dispositions both plus `.vale.ini`, inside the refresh's walk |

### Round 2 - gpt-5.6-sol (codex lane), narrow verification: VERDICT: FAIL

Scope: verify all 24 round-1 dispositions against the revised
artifacts. All 17 BLOCKING dispositions verified RESOLVED with line
references. Three residuals, each fixed in-session:

| # | Finding (compressed) | Disposition |
|---|---|---|
| 1 | NEW BLOCKING - a resolved dock cannot tell which `[companions.<name>]` table is its consumer | Fixed: the manifest gains a `[dock].companion` key naming its companion table; the doctor verifies the identity chain both ways |
| 2 | codex 14 residual (MINOR) - three schema-doc statements still treat K11 adoption as unconditional | Fixed: all three now conditional on K10's ruling |
| 3 | kimi 3 residual (MINOR) - walk-up outside any git repo still ran to filesystem root | Fixed: no walk-up outside a repo; out-of-repo callers use the flag, env, or legacy channels |

### Round 3 - gpt-5.6-sol (codex lane), micro-verification: VERDICT: FAIL

Scope: the three round-2 residuals. Items 1 (dock companion key) and 3
(walk-up bound) verified RESOLVED with line references. Item 2 left one
comment line still treating the legacy surface's K11 retirement as
unconditional; fixed in-session in both the schema's `[contract]`
comment and the docking spec's legacy-fallback step.

### Round 4 - gpt-5.6-sol (codex lane), micro-verification: VERDICT: FAIL

Scope: the single item-2 fix. All five adoption-action sites verified
RESOLVED with line references. Three further flags landed on statements
describing the kit's own design rather than the aura adoption;
dispositioned by the board owner per the fix-or-reject rule:

| # | Finding (compressed) | Disposition |
|---|---|---|
| 1 | "This retires the CLAUDE.local.md symlink" reads as an aura-adoption claim | Fixed: the paragraph now separates the kit-design retirement of the pattern from the live deployment's symlink, which retires only at an adoption K10 rules to proceed; a "retired post-checkout hook" tense slip fixed the same way |
| 2 | Spec intro says the symlink is "the stored-link pattern to retire" | Rejected: the sentence quotes Mike's 2026-08-11 source direction; it records the mandate, not an adoption action |
| 3 | Step 4 unconditionally says the common-dir fallback "replaces the post-checkout hook" | Rejected: a kit-design statement - in the kit the fallback is the mechanism, unconditionally; the aura deployment's hook retirement is governed where the deployment is discussed |

The two clarifying sentences from disposition 1 postdate round 4's
read; they respond only to its own findings and are recorded here
rather than spending a fifth verification round on two sentences.

### Gate A closure

Both lanes delivered explicit verdicts on the full artifact set; all 17
round-1 BLOCKING findings plus the round-2 NEW BLOCKING are verified
RESOLVED with line references by an independent round; every remaining
flag is fixed or rejected with its reason recorded above. The
reviewer-differs-from-author invariant held throughout (Claude author;
GPT and Kimi reviewers). Gate A closed by the board-owner session,
2026-08-16.
