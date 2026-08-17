# Enforcement contract

What mechanically protects a wiki-kit deployment's provenance classes,
what each layer covers, and what each layer deliberately does not.
Status: shipped with the kit core (program card K2); the consumer-dock
and skill layers extend it at K3.

The deployment's `wiki.toml` `[contract]` table is the single source
every enforcement consumer reads. The installer derives deny rules from
it, the doctor verifies against it, and install-smoke asserts from it;
none carries a private copy. The kit's `DEFAULT_CONTRACT` constant in
`scripts/wiki_config.py` exists only as the init template.

## Provenance classes

| Path (wiki repo) | Class | Protection |
|---|---|---|
| `wiki/events/**` | immutable source (event store) | pre-commit immutability + added-event schema validation; deny rules |
| `wiki/log-legacy.md` | immutable source (optional legacy epoch) | pre-commit immutability after creation; deny rules |
| `wiki/log.md` | generated projection | pre-commit byte-equality against the kit renderer over the staged tree; doctor re-render check; deny rules |
| `wiki/pending/index.json`, `wiki/pending/latest.md` | generated projections | pre-commit builder-equality over the staged tree (timestamp-neutralized); artifact schema validation when staged; doctor check; deny rules |
| `wiki/quarantine.json` | curated correction ledger (mutable by design) | artifact schema validation when staged; deny rules block casual tool edits |
| `wiki/log-epoch.json` | immutable-by-convention boundary marker (optional) | artifact schema validation when staged; deny rules |
| `wiki/sources/**` | content-addressed captures | write-time sha256 manifest; periodic doctor audit (`captures` check) re-hashes every committed capture |
| `CLAUDE.local.md` | generated orientation projection (gitignored) | deny rules only; no git hook can apply to an untracked file |
| `workstreams/**/*.md` | curated working state | pre-commit runs the workstream validator over the staged tree; recursive, `_archive/` included, `_reference/` and `index.md` excluded (the exclusion lives in the validator once) |
| `wiki/entities/*.md` | curated working state | convention plus doctor budgets and link checks; no write protection |
| `wiki/index.md` | curated content catalog, optional | none by design: writable working state with no generated or protected role; the doctor's link check includes it when present |
| `wiki.toml` | committed deployment config | loader strictness (unknown keys fail); doctor fails machine paths in the committed file; the overlay allowlist stops a machine file from rewriting identity or contract |

## The layers

### Layer 1: harness permission rules (deny rules)

The installer merges `Write`/`Edit`/`NotebookEdit` deny rules derived
from `[contract].protected` into the wiki repo's
`.claude/settings.json`.

Covered: file edits attempted through those editing tools by a
Claude-Code-family harness reading that settings file.

NOT covered, by construction:

- **Shell-mediated writes.** A `Bash` tool call (a redirect, `sed -i`,
  a python one-liner) is not an `Edit` call; the deny patterns are
  tool-scoped and carry no `Bash` entries, so a shell write to a
  protected path sails through this layer. This is tested, not
  inferred: the install-smoke enforcement probes write to protected
  paths with plain shell and prove it is layer 2 (the hook), not this
  layer, that blocks the commit. Sanctioned writers (the renderer, the
  event CLI, garden) depend on this gap; it is a design boundary, not
  an oversight.
- **Other harnesses and editors.** The rules bind one harness's
  configuration surface. A different agent harness or a human editor
  never reads them. (Consumer-side rules for other harnesses are a K3
  dock concern.)
- **Untracked generated surfaces.** `CLAUDE.local.md` gets ONLY this
  layer, because gitignored files never reach a git hook.

### Layer 2: the pre-commit hook (the mechanical layer)

Installed by the kit installer as a generated wrapper in the wiki
repo's hooks dir, pinning the installing interpreter and exec-ing the
kit checkout's current `scripts/pre-commit`. Checks staged content
only: log byte-equality, event immutability and validation, pending
builder-equality, artifact schema validation, legacy immutability, and
workstream validation, all against a materialized staged tree.

Covered: any write that reaches `git commit`, regardless of what tool
or human made it. This is the layer that catches the shell-write
bypass of layer 1.

NOT covered, by construction:

- **`git commit --no-verify`.** Documented in the hook's own output.
  The rules still apply; the bypass is accountable, not prevented.
- **Writes that never commit.** The hook fires at commit time only.
  A dirty working tree holds any content until then (the doctor's
  cadence is the backstop).
- **The kit checkout itself.** The hook runs the CURRENT kit checkout's
  validator and schema registry. A locally edited kit validates with
  those local edits; the registry's own integrity is the kit repo's
  test suite's job, not the deployment hook's. This is a deliberate
  shift from the pre-extraction design, where machinery lived in the
  content repo and the hook ran the STAGED validator, closing a
  "stage a loosened schema beside a bad event" hole. With the registry
  in a separate repo that hole is structurally gone (a wiki commit
  cannot stage a schema at all), and the corresponding property moved:
  schema changes are governed by the kit repo's own review and its
  never-loosened registry rule.
- **Hook not installed.** A clone that never ran the installer has no
  hook. The doctor's `install` check reports exactly that.

### Layer 3: the doctor (advisory cadence)

`scripts/wiki-doctor.py --wiki <root>` re-verifies what the other
layers assert plus what only a periodic pass can see. Its checks cover
projection staleness, pending drift, config strictness with
machine-path hygiene, the contract-derived deny rules, hook presence,
token budgets, links, and workstream validity. The capture audit closes
the recon "collision-only" gap: every committed manifest is re-hashed
on every run instead of only when a new write collides with it.

Covered: everything above, on demand and on the nightly cadence.

NOT covered: nothing blocks between doctor runs; the doctor observes
and reports. It never rewrites.

## What is convention only

The curation flow (which model class may curate workstreams, when
garden runs) is process prose in the deployment's own docs, not
mechanics. `wiki/index.md` and `wiki/entities/` are writable working
state on purpose. A deployment wanting more protection adds paths to
`[contract].protected` and re-runs the installer; the doctor then
enforces the wider set with no kit change.
