---
name: morning
# Rendered from the wiki-kit template by wiki-dock install; a reinstall
# overwrites this file.
description: |
  Review today's canonical scheduled night-run report and its matching commit.
  Use after the 08:30 reminder, when a night run needs attention, or when the
  user asks to inspect, repair, reject, or supersede a nightly apply.
compatibility: pi opencode claude-code codex
---

Review the scheduled T0 night run. Treat the canonical report and its matching
commit as one unit. Ignore manual, dry-run, report-only, and UAT report files.

## Establish scheduled state

Run from the wiki repo root (the directory containing `wiki.toml`; resolve it
through the dock if you are working in a consumer repo):

```bash
TODAY_UTC=$(date -u +%Y-%m-%d)
REPORT="reports/night/${TODAY_UTC}.md"
test -f "$REPORT"
MODE_COUNT=$(grep -c '^\*\*Mode:\*\* ' "$REPORT" || true)
OUTCOME_COUNT=$(grep -c '^\*\*Outcome:\*\* ' "$REPORT" || true)
test "$MODE_COUNT" -eq 1
test "$OUTCOME_COUNT" -eq 1
grep -Fx '**Mode:** scheduled' "$REPORT"
OUTCOME=$(sed -n 's/^\*\*Outcome:\*\* //p' "$REPORT")
```

Require exactly one Mode line and one Outcome line. Accept only `clean`,
`attention`, or `aborted`. A suffixed manual or test report never replaces the
canonical scheduled report.

- If the report is missing, show the night scheduler state and logs. State
  `NIGHT RUN MISSING` and stop. Do not open an older report.
- If the report is malformed, state `NIGHT RUN NEEDS ATTENTION` and stop.
- If the report is aborted, state `NIGHT RUN NEEDS ATTENTION`. Enter only the
  durable partial repair below when its abort text says the apply stands or the
  pending rebuild failed. Otherwise summarize the failure and stop.
- If the outcome is `clean` or `attention`, find its commit:

```bash
COMMIT=$(git log --format='%H%x09%s' -- "$REPORT" |
  awk -F '\t' -v expected="night: ${TODAY_UTC}" \
    '$2 == expected {print $1; exit}')
test -n "$COMMIT"
git cat-file -e "${COMMIT}:${REPORT}"
git diff --quiet "$COMMIT" -- "$REPORT"
```

Require the exact `night: YYYY-MM-DD` subject and byte-identical report content.
If either check fails, state `NIGHT RUN NEEDS ATTENTION` and stop before routing
or undo review. Continue only when both checks pass.

Use these diagnostics for a missing or aborted run (the scheduler label derives
from the wiki's `[wiki].name` as `com.<wiki-name>.wiki-night-shift`):

```bash
launchctl print "gui/$(id -u)/com.<wiki-name>.wiki-night-shift"
ls -l reports/scheduler-logs/night-shift-stdout.log reports/scheduler-logs/night-shift-stderr.log
tail -n 80 reports/scheduler-logs/night-shift-stdout.log
tail -n 80 reports/scheduler-logs/night-shift-stderr.log
```

## Walk the report and commit

Read the typed report buckets in this order:

1. Outcome and abort reason
2. Applied events
3. Manual action required
4. Reconciled events
5. Sweep findings and memory triage
6. Doctor, metrics, and failed steps

Show the matching commit, not `HEAD`:

```bash
git show --stat --oneline "$COMMIT"
git show --format=fuller --find-renames "$COMMIT"
```

For each applied event, show its ID, summary, and routed workstream. Ask the
user to confirm the routing. Treat the other buckets as follows:

- `Manual action required`: mechanical apply did not run for this handoff.
  Leave it pending and route it to interactive `/garden`.
- `Reconciled events`: the event already had a disposition and the runner
  rebuilt pending state. Do not reapply it.
- Sweep, triage, or doctor findings: present the exact action and wait for user
  approval before writing.

An `attention` outcome is a committed run that still needs this queue review.
It is not an aborted run.

## Repair a durable partial apply

If the abort text says the apply stands or the pending rebuild failed, the
workstream mutation and garden-apply event are durable.

State `DO NOT REAPPLY` and name the event. Then run:

```bash
uv run --project {{KIT_ROOT}} \
  {{KIT_ROOT}}/scripts/wiki-event.py build-pending
uv run --project {{KIT_ROOT}} \
  {{KIT_ROOT}}/scripts/wiki-render.py log
uv run --project {{KIT_ROOT}} \
  {{KIT_ROOT}}/scripts/wiki-render.py claude-local
uv run --project {{KIT_ROOT}} \
  {{KIT_ROOT}}/scripts/wiki_doctor.py
```

If `build-pending` fails after reporting that a disposition stands, do not
repeat the disposition command. Fix the reported projection cause and retry
only `build-pending`, then continue with both renders and doctor. If a render
fails, retry that projection step, not the apply. Show the exact repair diff and
every dirty path. Run `gate-probes`, invoke `git-commit`, and wait for approval
before committing only the proved repair paths. Do not push.

## Supersede a bad apply

Never delete or modify the source handoff event. Before writing anything, show
the user the target event, reason, exact stamped block to remove, routed
workstream, and the proposed replacement values for `last_updated`, `branch`,
and `sha`. Obtain one explicit approval covering both the superseding
disposition and that complete workstream repair.

Only after that approval, write the real superseding disposition:

```bash
uv run --project {{KIT_ROOT}} \
  {{KIT_ROOT}}/scripts/wiki-event.py new-garden-apply \
  --target <handoff-event-id> \
  --status superseded \
  --note "undone via /morning: <reason>"
```

If the command reports that the disposition stands but pending rebuild failed,
do not rerun it. Record that pending rebuild is required, but do not render,
run doctor, invoke gates, or commit yet. Continue directly to the
already-approved workstream repair below.

Immediately perform the already-approved repair: remove only that event's
stamped uncurated block from the routed workstream and set the approved
frontmatter values. Recompute those values from the newest remaining applied
event. If no applied event remains, use the pre-night file at `${COMMIT}^` as
the source. Do not restore the whole parent frontmatter when another valid event
in the same night is newer. If the repair fails after the disposition write,
state that the disposition stands, do not issue another disposition, and finish
the pre-approved workstream repair before any commit.

Only after the workstream repair is complete, rebuild pending and then render
and verify the disposition, workstream, and projections as one coherent batch:

```bash
uv run --project {{KIT_ROOT}} \
  {{KIT_ROOT}}/scripts/wiki-event.py build-pending
uv run --project {{KIT_ROOT}} \
  {{KIT_ROOT}}/scripts/wiki-render.py log
uv run --project {{KIT_ROOT}} \
  {{KIT_ROOT}}/scripts/wiki-render.py claude-local
uv run --project {{KIT_ROOT}} \
  {{KIT_ROOT}}/scripts/wiki_doctor.py
```

Do not treat any earlier projection or doctor result as commit authority. Show
the complete disposition + workstream + projection repair diff. Invoke
`git-commit`, present its checks and exact message, and wait for approval to
commit the complete batch. Do not push.

## Safety

- Never substitute an older, manual, dry-run, report-only, or UAT report.
- Never reapply an event after a durable partial apply or existing disposition.
- Never delete or rewrite event files. Corrections are new dispositions plus
  explicit workstream repair.
- Never use broad checkout, clean, add, or reset commands for recovery.
- Stop when a report, commit, event ID, routing, or frontmatter source cannot be
  proved from the report and repository.
