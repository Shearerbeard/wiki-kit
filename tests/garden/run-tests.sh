#!/bin/bash
set -euo pipefail

WIKI_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSTREAMS="$WIKI_DIR/workstreams"
LOG_FILE="$WIKI_DIR/wiki/log.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}PASS${NC}: $1"; }
fail() { echo -e "${RED}FAIL${NC}: $1"; exit 1; }
info() { echo -e "${YELLOW}==== $1 ====${NC}"; }

# ============================================================
info "Phase 0: Pre-flight validation"
# ============================================================
python3 "$WIKI_DIR/scripts/validate-workstreams.py" --wiki "$WIKI_DIR" || fail "pre-flight validation"
pass "existing workstream files valid"

BEFORE_TREE=$(python3 "$WIKI_DIR/scripts/build-index.py" --wiki "$WIKI_DIR")
BEFORE_ACTIVE=$(python3 "$WIKI_DIR/scripts/build-index.py" --wiki "$WIKI_DIR" --json | python3 -c "import sys,json; print(json.load(sys.stdin)['summary']['active'])")
echo "Before: $BEFORE_ACTIVE active streams"
echo ""

# ============================================================
info "Phase 1: Apply test fixtures"
# ============================================================

# Scenario A: Sync transport resolved (overwrite)
cp "$TEST_DIR/sync-transport-resolved.md" "$WORKSTREAMS/sync-transport.md"
echo "  Applied: sync-transport → archived (PR #205 merged)"

# Scenario A: Update queue-parity blocker (clear it)
sed -i.bak 's/blocker: "sync-transport PR #205 must merge first"/blocker: ""/' "$WORKSTREAMS/queue-parity.md"
sed -i.bak 's/last_updated: 2026-05-22/last_updated: 2026-05-24/' "$WORKSTREAMS/queue-parity.md"
rm -f "$WORKSTREAMS/queue-parity.md.bak"
echo "  Applied: queue-parity blocker cleared"

# Scenario A: Update load-bench blocker (clear it)
sed -i.bak 's/blocker: "sync-transport PR #205 pending"/blocker: ""/' "$WORKSTREAMS/load-bench.md"
sed -i.bak 's/last_updated: 2026-05-18/last_updated: 2026-05-24/' "$WORKSTREAMS/load-bench.md"
rm -f "$WORKSTREAMS/load-bench.md.bak"
echo "  Applied: load-bench blocker cleared"

# Scenario B: Eval benchmark multi-session update
cp "$TEST_DIR/eval-benchmark-updated.md" "$WORKSTREAMS/eval-benchmark.md"
echo "  Applied: eval-benchmark multi-session accumulation"

# Scenario C: New workstream
cp "$TEST_DIR/usage-budget-monitoring-new.md" "$WORKSTREAMS/usage-budget-monitoring.md"
echo "  Applied: usage-budget-monitoring (new workstream)"

# Scenario D: Task-router reactivated
cp "$TEST_DIR/task-router-reactivated.md" "$WORKSTREAMS/task-router.md"
echo "  Applied: task-router reactivated from parked"

# Append log entries
cat "$TEST_DIR/scenario-a-log-entries.md" >> "$LOG_FILE"
echo "  Applied: 5 log entries appended"

echo ""

# ============================================================
info "Phase 2: Deterministic validation"
# ============================================================

# Re-validate after changes
python3 "$WIKI_DIR/scripts/validate-workstreams.py" --wiki "$WIKI_DIR" || fail "post-apply validation"
pass "all workstream files still valid after applying fixtures"

# Run expected checks
python3 "$TEST_DIR/expected-checks.py" || fail "expected checks"
pass "all expected checks passed"

AFTER_ACTIVE=$(python3 "$WIKI_DIR/scripts/build-index.py" --wiki "$WIKI_DIR" --json | python3 -c "import sys,json; print(json.load(sys.stdin)['summary']['active'])")
echo ""
echo "After: $AFTER_ACTIVE active streams (was $BEFORE_ACTIVE)"

# ============================================================
info "Phase 2 complete — deterministic tests passed"
# ============================================================
echo ""
echo "Next steps:"
echo "  1. Review the changes: git diff"
echo "  2. If good, commit: git commit -am 'test: apply scenario fixtures'"
echo "  3. Run LLM tests: claude -p 'Run /garden'"
echo "  4. To rollback: git checkout -- workstreams/ wiki/"
