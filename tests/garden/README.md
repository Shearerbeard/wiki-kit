# Garden + Handoff Test Fixtures

Canonical test data simulating 3 days of workstream progression for a
fictional `acme-notes` wiki tracking a companion project called `widget`
(github `acme/widget`, branches `alex/*`, engineer Alex).

`run-tests.sh` builds a throwaway fixture wiki (kit installer plus the
"before" workstreams in `baseline/`), applies the scenario fixtures to
it, and runs the deterministic checks. It never touches the kit repo or
any real wiki. Set `GARDEN_FIXTURE_DIR` to keep the fixture around for
inspection. `test_run_tests_harness.py` runs the whole harness under
pytest so CI catches breakage in the shell script itself.

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

## Harness phases

Phase 0: Install the throwaway fixture wiki and seed `baseline/` (the
         "before" state the scenarios mutate)
Phase 1: Apply scenario data (log entries + workstream updates)
Phase 2: Run deterministic checks (validate-workstreams.py,
         expected-checks.py --wiki <fixture>)

LLM-phase testing (running /garden and /handoff for real) is out of
scope for this harness; it happens against a live deployment.
