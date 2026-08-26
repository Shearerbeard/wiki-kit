#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(dirname "$SCRIPT_DIR")"

if [ "${1:-}" != "run" ] || [ "$#" -ne 1 ]; then
    echo "usage: garden-reminder.sh run" >&2
    exit 64
fi

if [ -z "${WIKI_DIR:-}" ]; then
    echo "garden-reminder.sh requires WIKI_DIR (the wiki repo root)" >&2
    exit 64
fi
REPO="$WIKI_DIR"
UV_BIN="${WIKI_UV_BIN:-uv}"
NOTIFY_BIN="${WIKI_NOTIFY_BIN:-$SCRIPT_DIR/wiki-notify.sh}"

set +e
pending_output=$("$UV_BIN" run --project "$KIT_ROOT" \
    "$KIT_ROOT/scripts/wiki-event.py" count-pending --wiki "$REPO" 2>&1)
pending_status=$?
set -e

exit_status=0
if [ "$pending_status" -ne 0 ] || ! [[ "$pending_output" =~ ^[0-9]+$ ]]; then
    title="🚨 Wiki Garden Checkpoint"
    message="End-of-day check could not count pending handoffs. Hand off any still-open sessions, then check /garden manually."
    exit_status=1
elif [ "$pending_output" -eq 0 ]; then
    title="🌿 Wiki Garden Checkpoint"
    message="End-of-day check: no handoffs await /garden. Hand off any still-open sessions; run /garden if that creates handoffs."
elif [ "$pending_output" -eq 1 ]; then
    title="🌿 Wiki Garden Checkpoint"
    message="End-of-day check: 1 handoff awaits /garden. Hand off any still-open sessions first, then run /garden."
else
    title="🌿 Wiki Garden Checkpoint"
    message="End-of-day check: ${pending_output} handoffs await /garden. Hand off any still-open sessions first, then run /garden."
fi

"$NOTIFY_BIN" routine "$title" "$message"

if [ "$exit_status" -ne 0 ]; then
    echo "garden reminder could not determine the pending handoff count" >&2
fi
exit "$exit_status"
