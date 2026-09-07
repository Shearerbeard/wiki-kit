# ADR 0012 - Token budgets for the wiki's read surfaces

Status: accepted (K4 Gate U, budget-adr, 2026-09-06). This record
amends and ratifies, for the kit, the source deployment's ADR 0012
(proposed 2026-07-01), whose numbers the kit's Stage 1 shipped as
constants and then as the first `[budgets]` table.
Date: 2026-09-06.
Decider: the program owner, at the human user gate K4 Gate U
(budget-adr) on the program board's card K4 (budget system); the board
owner session recorded the ruling.
Delivery: card K4. Stage 1 is kit commit `ed20b21`; the Stage 2 commits
that implement this record follow it and carry `Card: K4`.
Review: adversarial review ledger appended below.

## Context and problem statement

A wiki deployment has five read surfaces whose size is paid in tokens
by every session that reads them: the orientation index
(`CLAUDE.local.md` in the wiki repo), the harness's per-project memory
index when one exists, each workstream page, each entity page, and the
nightly report. The source deployment's ADR 0012 set two-tier budgets
for them in 2026-07 and left the record at "proposed". By the time the
kit extracted the machinery, the numbers lived in three code constants
and one doctor table that no config could change. The orientation
listed every active workstream in full. The night report's numbers had
drifted from the record: 1500/2500 written, 2500/4000 shipped.

K4 Stage 1 made the budgets configuration: a `[budgets]` table with a
warn and a hard value per surface and an optional forefront size, the
doctor and renderer reading it, and the orientation tree collapsing
past the forefront with the newest handoff's workstream pinned. A review
pass on 2026-09-06 found the mechanics sound and the shape wrong. Nine
keys named after files express two ideas a deployment thinks in: how
many subjects stay in view, and how large one subject page may be. Two
enforced budgets stayed outside the table (the night report and an
80-line warning that fired on every render of a 164-line index). The
explicit warn/hard pairs trap a deployment that lowers a ceiling below
the default warn: the config refuses to load. The docs taught that a
FAIL "blocks nothing", while the night runner aborts its commit on a
doctor FAIL and the source deployment hit exactly that. Two cheap-model
canaries reproduced that false consequence from the docs alone.

### Terms this record fixes

- **Surface**: a file or file class measured against a budget: by the
  doctor for the orientation index and the pages, by the night runner
  for its own report.
- **Orientation index**: the generated `CLAUDE.local.md` a session
  reads first. A docked consumer's `.wiki/orientation.md` is a fixed
  pointer page with no tree and is not a surface.
- **Estimated tokens**: `(bytes + 3) // 4` of the file's UTF-8 bytes,
  one function shared by the doctor, the renderer, and the night runner.
  It is an estimate; a tokenizer replaces it only if observed truncation
  diverges from it.
- **Forefront**: the active workstreams the orientation tree lists in
  full, newest `last_updated` first. The rest collapse to one-line rows
  that show the blocker, or the next step when there is none.
- **Baton**: the workstreams the newest handoff event proposes to
  update or create. They stay in the forefront by name whatever their
  sort position.
- **Ceiling**: a surface's hard budget. The doctor FAILs above it; the
  night runner trims its report to it.
- **Warn**: the doctor's WARN threshold below a ceiling. For the night
  report it is a recorded trend number, not a threshold anything acts
  on.

## Decision drivers

- The orientation index MUST stay under its ceiling, 3000 estimated
  tokens by default, the figure the source record set as the headroom a
  weak model needs for the work it was opened for. Revisit the figure
  if a deployment's models change that headroom.
- Every budget the kit enforces MUST live in the one `[budgets]` table,
  so a doc that calls the table the budget list is telling the truth.
- A misconfiguration MUST fail loud at load, naming the key; the kit
  ships no silent fallbacks.
- A baton workstream that exists as an active page MUST NOT leave the
  forefront, whatever the forefront size or the sort order.
- A deployment SHOULD be able to state its intent in two numbers,
  subjects in view and tokens per subject page, with everything else
  defaulted. Revisit if a deployment needs per-surface warn tuning that
  the escape hatch below cannot express.
- Read-on-demand pages SHOULD NOT be trimmed by machinery; over budget
  there is a curation signal, not a truncation. Revisit if a page class
  gains a mechanical writer that can trim losslessly (the Log section
  card K12 proposes).
- Growth SHOULD be visible as a trend, not a surprise: the night report
  records its own size. Revisit if the night pipeline stops being the
  deployment's regular cadence.

## Considered options

1. **Keep the Stage 1 shape and lead the docs with two of its keys.**
   Cheapest. Rejected: the docs cannot fix nine numbers standing for
   two ideas, the warn/hard load trap stays, and every rename after the
   source deployment adopts the kit is a migration.
2. **Two-number headline, one ceiling per surface, warn derived.**
   Chosen. Details below.
3. **Derive the orientation ceiling from the forefront size in
   config.** Rejected: a budget has to be a fixed number to be a budget,
   and the collapsed-row count depends on content the config cannot
   know. The relationship is documented and used by the doctor to name
   a lever instead.
4. **Alias the Stage 1 key names with a deprecation warning.**
   Rejected: the loader has no deprecation channel (an unknown key is
   refused at load, before any value is read), an alias keeps the load
   trap alive for the old spelling, and the plumbing would serve one
   small deployment whose table, if it has one, the roll-out inventories
   anyway.

## Decision outcome

Option 2. The `[budgets]` table becomes:

| Key | Bounds | Default ceiling | Derived warn |
|---|---|---|---|
| `workstreams_in_view` | active workstreams the orientation lists in full; `0` means no cap | 8 | none (a count) |
| `workstream_tokens` | one workstream page | 4000 | 2600 |
| `orientation_index_tokens` | the orientation index, measured up to its `## Uncommitted Changes` heading | 3000 | 2000 |
| `entity_tokens` | one entity page, recursively under `wiki/entities/` | 3500 | 2300 |
| `memory_index_tokens` | the per-project memory index, when present | 2000 | 1300 |
| `night_report_tokens` | one night report | 4000 | 2600 (a trend number, never an action) |

- Warn derives as two thirds of the ceiling, rounded down to the
  hundred; when that rounding would give zero, the unrounded two thirds
  stands, and the result is never below 1 (a ceiling of 50 derives to
  33, a ceiling of 4000 to 2600). A ceiling must be at least 2. An explicit
  `<surface>_warn_tokens` overrides the derivation and must stay below
  its ceiling. Against Stage 1, four warn values move: memory index
  1500 to 1300, workstream 2500 to 2600, entity 2000 to 2300, night
  report 2500 to 2600; the orientation stays 2000.
- The Stage 1 key names are refused at load with an error naming the
  replacement key:

  | Stage 1 key | Replacement |
  |---|---|
  | `claude_local_hard` | `orientation_index_tokens` |
  | `claude_local_warn` | `orientation_index_warn_tokens` |
  | `memory_index_hard` | `memory_index_tokens` |
  | `memory_index_warn` | `memory_index_warn_tokens` |
  | `workstream_hard` | `workstream_tokens` |
  | `workstream_warn` | `workstream_warn_tokens` |
  | `entity_hard` | `entity_tokens` |
  | `entity_warn` | `entity_warn_tokens` |
  | `parallel_workstreams_target` | `workstreams_in_view` |

  A deployment that only ever set ceilings drops its old warn keys and
  takes the derived values. This is a load-time
  error in an optional, hand-authored table, not a contract version
  bump: the installer never writes the table and loads the config
  before it stamps, so the stamp mechanism could not repair it. The
  roll-out inventories every docked deployment's table and moves it in
  the same change that updates the kit checkout. `docs/VERSIONING.md`
  records the classification.
- Overflow semantics by surface class:
  - The orientation index collapses by count. The forefront holds
    `workstreams_in_view` workstreams in full with the baton pinned, or
    more when the newest handoff pins more than that many; the rest
    render as one-line rows. Its `## Uncommitted Changes` section
    is machine-local (worktrees and dirt on this machine) and sits
    outside the ceiling; the doctor and the renderer measure it
    separately and report its size.
  - Workstream, entity, and memory-index pages are curation signals:
    WARN, then FAIL, never trimmed. The doctor's finding on an
    over-ceiling orientation names the lever in workstreams: the active
    count, the in-view count, the measured cost of a full entry (about
    68 tokens) and a collapsed row (about 37), and the two actions
    (lower `workstreams_in_view`, or archive workstreams; a parked page
    still renders as a row, so parking is no lever).
  - The night report trims itself to its ceiling on every write,
    including the abort report: the doctor dump to its FAIL and WARN
    lines plus a pointer to `<report_dir>/doctor-<date>.txt`, then the
    sweep section, then metrics. It records `report_tokens` under
    Metrics on every run. The sweep-findings section cap inside the
    report is a constant derived from `night_report_tokens`, not a
    surface.
- A doctor FAIL on any surface aborts the nightly commit. The docs say
  so where the budgets are explained.
- The 80-line orientation warning is retired. Lines were the wrong unit
  in the source record already, and a warning that fires on every
  render teaches sessions to ignore warnings.

## Consequences

Positive: a deployment sets two keys and gets the intended orientation;
the schema doc's claim that the table is the budget list is true; the
load trap is gone; the doctor's overrun finding is an instruction in the
unit the operator thinks in; the docs state the real consequence of a
FAIL; two cheap models scored 6/6 against a mechanical key on the Stage 1
docs, so the reshape starts from a passing baseline.

Negative and accepted: any hand-written table on the Stage 1 spelling
fails to load until edited (one docked deployment exists, inventoried
at roll-out); the WARN band moves for deployments with no table; the
default of 8 changes the orientation for a wiki with more than eight
active workstreams, and `workstreams_in_view = 0` is the way back; the
two-thirds rule is a convention, not a measurement; the estimator is
bytes over four, not a tokenizer.

Gaps, each with an owner:

- The workstream page shape that keeps pages under their ceiling by
  construction: card K12, drafted at the Stage 2 plan's last stage and
  not yet on the board.
- The source deployment's night-shift runbook still carries a
  deferred-item paragraph this record closes: the deployment's next
  garden pass edits it.
- Tokenizer accuracy: the board owner at each K4 Gate D compares the
  doctor's estimate against any observed truncation and opens a card on
  the first divergence.

Failure modes: an over-ceiling orientation FAILs the doctor and aborts
the night commit until the forefront is lowered or workstreams are
archived; a page over its ceiling does the same until curated; a night
report still over its ceiling after the full shrink aborts the run with
that reason, the same abort pass 2 performs today after its
metrics-only shrink.

## Links

- Card K4 (budget system) on the `wk` program board, at the board's
  `docs/board/cards/k4-budget-system.md`, and its evidence records under
  the board's `docs/board/evidence/`: `2026-09-06-k4-review-pass.md`,
  `2026-09-06-budget-docs-canary.md`, `2026-09-06-orientation-canary.md`,
  and the approved plan `2026-09-06-k4-stage2-plan.md`. The board lives
  in the source deployment's private wiki, not in this repo.
- In this repo: `docs/wiki-toml-schema.md` (the table),
  `docs/DOCTOR-TRIAGE.md` (the levers), `docs/VERSIONING.md` (the rename
  classification), `docs/extraction-ledger.md` (the row assigning this
  record to K4).
- In the source deployment: `docs/wiki-system/adr/0012-token-budgets.md`
  (proposed 2026-07-01) and `docs/wiki-system/adr/0010-night-shift-operating-model.md`,
  which this record amends for the kit; the deployment adopts it at K11
  (adoption execution).

## Adversarial review ledger

Author: Claude Fable 5.1, the board-owner session (Claude Code).
Reviewer: the codex CLI (`codex exec --sandbox read-only`), model
gpt-6-astra, fresh context, run from this repo with the approved plan
and the board evidence read by absolute path. Round 1 (245 s) returned
3 BLOCKING and 7 MINOR findings, VERDICT: FAIL. Round 2 (127 s)
verified all ten dispositions, VERDICT: PASS.

| # | Sev | Finding (compressed) | Disposition |
|---|---|---|---|
| 1 | BLOCKING | Derived-warn rule gave 1 for a ceiling of 50 where the plan requires 33 | Fixed: the rule keeps the unrounded two thirds when rounding to the hundred would give zero, floor 1 |
| 2 | BLOCKING | The rename table was missing; "the other three pairs" mapped nothing | Fixed: nine-row table, old key to replacement |
| 3 | BLOCKING | Surface and threshold definitions assumed doctor enforcement, contradicting the runner-enforced night report | Fixed: definitions name the enforcer per surface; the report's warn is a trend number |
| 4 | MINOR | Decider named the agent session, not the human gate | Fixed: the program owner at the user gate; the session recorded it |
| 5 | MINOR | Forefront count omitted the pinned-overflow case | Fixed: "or more when the newest handoff pins more than that many"; the baton guarantee limited to existing active pages |
| 6 | MINOR | Rejection rationale treated the deployment's table as known | Fixed: conditional, left to the roll-out inventory |
| 7 | MINOR | "Refused before parsing" misstated loader order | Fixed: refused at load, before any value is read |
| 8 | MINOR | Links did not resolve independently | Fixed: repo and path for every reference, none absolute |
| 9 | MINOR | The principal MUST was untestable; SHOULDs lacked flip conditions | Fixed: the MUST names the 3000 ceiling; each SHOULD names its revisit condition |
| 10 | MINOR | K12 cited as existing; tokenizer accuracy unowned | Fixed: K12 marked as drafted, not yet on the board; the board owner at each K4 Gate D owns the estimator check |

Premise freshness (round 1): every stated fact about the code and the
evidence still true at `ed20b21`. Two premises unverifiable and
reworded: the docked deployment's table (the roll-out inventories it)
and "every session reads all five surfaces" (now "every session that
reads them").
