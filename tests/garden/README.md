# Garden + Handoff Test Fixtures

Canonical test data simulating 3 days of workstream progression for a
fictional `acme-notes` wiki tracking a companion project called `widget`
(github `acme/widget`, branches `alex/*`, engineer Alex).
Apply with `run-tests.sh`, which gates each phase with commits for rollback.

## Scenarios

### A: Blocker cascade resolution
PR #200 merges (day 1) → sync-transport unblocked → sync-transport's own
PR #205 merges (day 2) → queue-parity and load-bench unblocked. Tests
blocker detection + cascade.

### B: Multi-session accumulation
Two sessions on the same day both update eval-benchmark:
session 1 adds scope leakage fix results, session 2 adds benchmark-viz prototype.
Tests garden consolidation of multi-session What Was Done entries.

### C: New workstream via handoff
Fresh workstream `usage-budget-monitoring` created from scratch.
Tests that handoff produces valid frontmatter and garden picks it up.

### D: Parked stream reactivation
task-router pulled from parked back to active (new work resumed).
Tests that status change flows through the index correctly.

### E: Stale Next detection
eval-benchmark has "Build `/benchmark-viz` skill" in Next.
Scenario B adds "Built benchmark-viz skill prototype" to Done.
Tests that build-index.py --json flags this as a stale Next candidate.

### F: Archival protection
Verifies parked streams are NEVER flagged for archival regardless of staleness.

## Test Phases (gated by commits)

Phase 0: Snapshot current state (rollback point)
Phase 1: Apply scenario data (log entries + workstream updates)
Phase 2: Run deterministic tests (Python scripts)
Phase 3: Run LLM tests (claude -p /garden, claude -p /handoff)
Phase 4: Validate LLM output
Rollback: git checkout main -- . (or git stash)
