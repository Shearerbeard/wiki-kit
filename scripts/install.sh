#!/bin/bash
# Thin entry point: all install logic lives in wiki_install.py, which
# consumes the deployment's own [contract] in wiki.toml - the same
# source the doctor and install-smoke read. Usage:
#   scripts/install.sh --wiki /path/to/wiki [--no-scheduler]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v python3 >/dev/null 2>&1 || {
  echo "ERROR: python3 not found on PATH" >&2
  exit 1
}

exec python3 "$SCRIPT_DIR/wiki_install.py" "$@"
