#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -z "${WIKI_DIR:-}" ]; then
    echo "morning-reminder.sh requires WIKI_DIR (the wiki repo root)" >&2
    exit 64
fi
REPO="$WIKI_DIR"
GIT_BIN="${WIKI_GIT_BIN:-git}"
NOTIFY_BIN="${WIKI_NOTIFY_BIN:-$SCRIPT_DIR/wiki-notify.sh}"
TODAY_UTC="${WIKI_TODAY_UTC:-$(date -u +%Y-%m-%d)}"
NIGHT_REPORT_DIR="${WIKI_NIGHT_REPORT_DIR:-reports/night}"
NIGHT_COMMIT_PREFIX="${WIKI_NIGHT_COMMIT_PREFIX:-night:}"

if ! [[ "$TODAY_UTC" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    printf 'morning reminder received invalid UTC date: %q\n' "$TODAY_UTC" >&2
    exit 64
fi

REPORT_REL="${NIGHT_REPORT_DIR}/${TODAY_UTC}.md"
REPORT="$REPO/$REPORT_REL"

notify() {
    local message="$1"
    local title="$2"
    local severity="$3"
    local status
    set +e
    "$NOTIFY_BIN" "$severity" "$title" "$message"
    status=$?
    set -e
    if [ "$status" -ne 0 ]; then
        echo "morning reminder notification failed with status ${status}: ${title}" >&2
    fi
    return "$status"
}

# The scheduler-status hint is rendered per platform into the unit env
# (WIKI_SCHEDULER_HINT); the fallback covers manual invocation.
if [ -n "${WIKI_SCHEDULER_HINT:-}" ]; then
    MISSING_HINT="Check scheduler status (${WIKI_SCHEDULER_HINT}) and run /morning."
else
    MISSING_HINT="Check scheduler status and run /morning."
fi

if [ ! -f "$REPORT" ]; then
    notify \
        "NIGHT RUN MISSING: no scheduled report for ${TODAY_UTC}. ${MISSING_HINT}" \
        "🚨 Wiki Night Run Missing" \
        "problem"
    exit 0
fi

mode_count=$(grep -c '^\*\*Mode:\*\* ' "$REPORT" || true)
outcome_count=$(grep -c '^\*\*Outcome:\*\* ' "$REPORT" || true)
mode=$(sed -n 's/^\*\*Mode:\*\* //p' "$REPORT")
outcome=$(sed -n 's/^\*\*Outcome:\*\* //p' "$REPORT")

if [ "$mode_count" -ne 1 ] || [ "$outcome_count" -ne 1 ] || \
    [ "$mode" != "scheduled" ] || \
    ! grep -Fqx "# Night run report — ${TODAY_UTC}" "$REPORT"; then
    notify \
        "NIGHT RUN NEEDS ATTENTION: today's canonical report is malformed. Run /morning." \
        "⚠️ Wiki Night Run Needs Attention" \
        "problem"
    exit 0
fi

if [ "$outcome" = "aborted" ] || \
    grep -q '^\*\*ABORTED:\*\*' "$REPORT"; then
    notify \
        "NIGHT RUN NEEDS ATTENTION: the scheduled run aborted. Run /morning." \
        "⚠️ Wiki Night Run Needs Attention" \
        "problem"
    exit 0
fi

if [ "$outcome" != "clean" ] && [ "$outcome" != "attention" ]; then
    notify \
        "NIGHT RUN NEEDS ATTENTION: today's canonical report has an unknown outcome. Run /morning." \
        "⚠️ Wiki Night Run Needs Attention" \
        "problem"
    exit 0
fi

set +e
log_output=$("$GIT_BIN" -C "$REPO" log --format='%H%x09%s' -- "$REPORT_REL" 2>/dev/null)
log_status=$?
set -e

commit=""
if [ "$log_status" -eq 0 ]; then
    commit=$(printf '%s\n' "$log_output" | awk -F '\t' \
        -v expected="${NIGHT_COMMIT_PREFIX} ${TODAY_UTC}" '$2 == expected {print $1; exit}')
fi

matches_commit=false
if [ -n "$commit" ] && \
    "$GIT_BIN" -C "$REPO" cat-file -e "${commit}:${REPORT_REL}" 2>/dev/null && \
    "$GIT_BIN" -C "$REPO" diff --quiet "$commit" -- "$REPORT_REL"; then
    matches_commit=true
fi

if [ "$matches_commit" != true ]; then
    notify \
        "NIGHT RUN NEEDS ATTENTION: the scheduled report has no matching ${NIGHT_COMMIT_PREFIX} ${TODAY_UTC} commit. Run /morning." \
        "⚠️ Wiki Night Run Needs Attention" \
        "problem"
elif [ "$outcome" = "attention" ]; then
    notify \
        "The scheduled night run needs review. Run /morning for its manual-action and reconciliation queues." \
        "⚠️ Wiki Morning Review" \
        "routine"
else
    notify \
        "Run /morning to review the clean scheduled wiki run." \
        "🌅 Wiki Morning Review" \
        "routine"
fi
