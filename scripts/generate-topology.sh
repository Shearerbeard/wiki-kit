#!/bin/bash
# Thin exec wrapper around the Python implementation, kept for hook
# compatibility. Every argument passes straight through — see
# generate-topology.py --help for the required flags.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/generate-topology.py" "$@"
