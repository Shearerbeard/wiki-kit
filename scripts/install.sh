#!/bin/bash
# Thin entry point: all install logic lives in wiki_install.py, which
# consumes the deployment's own [contract] in wiki.toml - the same
# source the doctor and install-smoke read. Usage:
#   scripts/install.sh --wiki /path/to/wiki [--no-scheduler]
#
# Interpreter order: the kit's own .venv when it carries jsonschema
# (uv sync), else a python3 that does (the install-smoke container),
# else uv run, which syncs the venv on the way; otherwise fail naming
# the fix. The pre-commit wrapper the installer writes probes the same
# way at commit time.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PYTHON="$KIT_ROOT/.venv/bin/python"

has_jsonschema() { "$1" -c "import jsonschema" >/dev/null 2>&1; }

if [ -x "$VENV_PYTHON" ] && has_jsonschema "$VENV_PYTHON"; then
  exec "$VENV_PYTHON" "$SCRIPT_DIR/wiki_install.py" "$@"
fi
if command -v python3 >/dev/null 2>&1 && has_jsonschema python3; then
  exec python3 "$SCRIPT_DIR/wiki_install.py" "$@"
fi
if command -v uv >/dev/null 2>&1; then
  exec uv run --project "$KIT_ROOT" python "$SCRIPT_DIR/wiki_install.py" "$@"
fi
echo "ERROR: no python with jsonschema found; run 'uv sync' in $KIT_ROOT and retry" >&2
exit 1
