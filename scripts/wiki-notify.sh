#!/usr/bin/env bash

set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: wiki-notify.sh <routine|problem> <title> <message>" >&2
    exit 64
fi
SEVERITY="$1"
TITLE="$2"
MESSAGE="$3"
if [ "$SEVERITY" != "routine" ] && [ "$SEVERITY" != "problem" ]; then
    echo "usage: wiki-notify.sh <routine|problem> <title> <message>" >&2
    exit 64
fi

if [ -n "${WIKI_NOTIFIER_BIN:-}" ]; then
    NOTIFIER_BIN="$WIKI_NOTIFIER_BIN"
else
    case "$(uname -s)" in
        Darwin) NOTIFIER_BIN="terminal-notifier" ;;
        Linux) NOTIFIER_BIN="notify-send" ;;
        *)
            echo "wiki-notify.sh: no default notifier on $(uname -s); set WIKI_NOTIFIER_BIN or [tools].notifier in wiki.local.toml" >&2
            exit 1
            ;;
    esac
fi

if ! command -v "$NOTIFIER_BIN" >/dev/null 2>&1; then
    echo "wiki-notify.sh: notifier not found or not executable: $NOTIFIER_BIN" >&2
    exit 1
fi

case "$(basename "$NOTIFIER_BIN")" in
    terminal-notifier)
        if [ "$SEVERITY" = "problem" ]; then
            SOUND="Basso"
        else
            SOUND="Glass"
        fi
        "$NOTIFIER_BIN" -title "$TITLE" -message "$MESSAGE" -sound "$SOUND"
        ;;
    notify-send)
        if [ "$SEVERITY" = "problem" ]; then
            URGENCY="critical"
        else
            URGENCY="normal"
        fi
        "$NOTIFIER_BIN" -u "$URGENCY" "$TITLE" "$MESSAGE"
        ;;
    *)
        echo "wiki-notify.sh: unsupported notifier '$(basename "$NOTIFIER_BIN")'; supported binaries are terminal-notifier and notify-send (set [tools].notifier in wiki.local.toml)" >&2
        exit 1
        ;;
esac
