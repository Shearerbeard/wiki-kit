#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPORT_DIR="$KIT_DIR/reports/install-smoke"
# Knob 16: cosmetic naming is never a hardcode. The kit's own harness
# has no deployment config to read at build time, so the kit repo's
# directory name is the naming source here; a deployment smoke (the
# heavy-canary stage) takes its prefix from [wiki].name instead.
KIT_NAME="$(basename "$KIT_DIR")"
IMAGE_NAME="${IMAGE_NAME:-$KIT_NAME-install-smoke:2026-08-16}"
CONTAINER_NAME="${CONTAINER_NAME:-$KIT_NAME-install-smoke-$(date -u +%Y%m%dT%H%M%SZ)}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

# In a linked worktree .git is a file, not a directory; ask git instead.
git -C "$KIT_DIR" rev-parse --git-dir >/dev/null 2>&1 \
  || fail "kit repo not found at $KIT_DIR"
mkdir -p "$REPORT_DIR"

docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"

set +e
docker run \
  --interactive \
  --name "$CONTAINER_NAME" \
  --network none \
  --mount "type=bind,src=$KIT_DIR,dst=/sources/wiki-kit,readonly" \
  --mount "type=bind,src=$REPORT_DIR,dst=/work/reports/install-smoke" \
  "$IMAGE_NAME" bash -s 2>&1 <<'CONTAINER_SCRIPT' | tee "$REPORT_DIR/latest.log"
set -Eeuo pipefail

KIT=/sources/wiki-kit
FIXTURE=/work/blank-wiki
VAULT=/work/vault-wiki
REPORT_DIR=/work/reports/install-smoke
export PYTHONDONTWRITEBYTECODE=1
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
CHECK_NAMES=()
CHECK_STATUS=()

record() {
  CHECK_NAMES+=("$1")
  CHECK_STATUS+=("$2")
}

write_json() {
  local overall="$1"
  local exit_code="$2"
  {
    printf '{\n'
    printf '  "timestamp": "%s",\n' "$TIMESTAMP"
    printf '  "overall_pass": %s,\n' "$overall"
    printf '  "exit_code": %s,\n' "$exit_code"
    printf '  "checks": [\n'
    local i
    for i in "${!CHECK_NAMES[@]}"; do
      printf '    {"name": "%s", "pass": %s}' "${CHECK_NAMES[$i]}" "${CHECK_STATUS[$i]}"
      if [ "$i" -lt "$((${#CHECK_NAMES[@]} - 1))" ]; then
        printf ','
      fi
      printf '\n'
    done
    printf '  ]\n'
    printf '}\n'
  } > "$REPORT_DIR/latest.json"
}

write_markdown() {
  local result="$1"
  {
    printf -- '# Install Smoke Report\n\n'
    printf -- '- **Date:** %s\n' "$TIMESTAMP"
    printf -- '- **Result:** %s\n' "$result"
    printf -- '- **Kit:** `%s`\n' "$KIT"
    printf -- '- **Blank fixture:** `%s`\n' "$FIXTURE"
    printf -- '\n## Checks\n\n'
    local i status
    for i in "${!CHECK_NAMES[@]}"; do
      status="FAIL"
      [ "${CHECK_STATUS[$i]}" = "true" ] && status="PASS"
      printf -- '- %s — %s\n' "$status" "${CHECK_NAMES[$i]}"
    done
  } > "$REPORT_DIR/latest.md"
}

on_error() {
  local exit_code="$?"
  write_json false "$exit_code" || true
  write_markdown FAIL || true
  exit "$exit_code"
}
trap on_error ERR

check() {
  local name="$1"
  shift
  echo "==> $name"
  "$@"
  record "$name" true
}

check_shell() {
  local name="$1"
  local script="$2"
  echo "==> $name"
  bash -c "$script"
  record "$name" true
}

# A named check that must FAIL (non-zero) to pass: the enforcement probes.
check_blocked() {
  local name="$1"
  local script="$2"
  echo "==> $name"
  if bash -c "$script" >/tmp/blocked.log 2>&1; then
    echo "expected the operation to be blocked, but it succeeded" >&2
    cat /tmp/blocked.log >&2
    false
  fi
  grep -q "wiki integrity checks" /tmp/blocked.log || {
    echo "operation failed, but not via the integrity hook:" >&2
    cat /tmp/blocked.log >&2
    false
  }
  record "$name" true
}

mkdir -p /work "$REPORT_DIR"
git config --global user.email smoke@wiki-kit.local
git config --global user.name "wiki-kit smoke"
git config --global init.defaultBranch main

# ---- blank-repo boot -------------------------------------------------------
check "first install run exits zero on a blank fixture" \
  "$KIT/scripts/install.sh" --wiki "$FIXTURE" --no-scheduler
check_shell "wiki.toml seeded" '[ -f "'"$FIXTURE"'/wiki.toml" ]'
check_shell "pre-commit hook wrapper execs the kit script" \
  'grep -q "'"$KIT"'/scripts/pre-commit" "'"$FIXTURE"'/.git/hooks/pre-commit" && [ -x "'"$FIXTURE"'/.git/hooks/pre-commit" ]'
check_shell "initial commit exists and tracks the projections" \
  'cd "'"$FIXTURE"'" && git rev-parse -q --verify HEAD >/dev/null && git ls-files | grep -qx "wiki/log.md" && git ls-files | grep -qx "wiki/pending/index.json"'
check_shell "orientation skeleton rendered with a Quickstart" \
  'grep -q "^## Quickstart" "'"$FIXTURE"'/CLAUDE.local.md"'

# The deny-rule assertion CONSUMES the deployment contract ([contract] in
# wiki.toml) rather than repeating a hardcoded list: one source, three
# consumers (installer, doctor, this smoke).
check_shell "Claude deny rules derive from [contract] in wiki.toml" 'python3 - <<PYEOF
import json, sys, tomllib
fixture = "'"$FIXTURE"'"
contract = tomllib.load(open(f"{fixture}/wiki.toml", "rb"))["contract"]
deny = json.load(open(f"{fixture}/.claude/settings.json"))["permissions"]["deny"]
missing = []
for rel in contract["protected"]:
    spec = rel if rel == "CLAUDE.local.md" else f"/{rel}"
    for tool in ("Write", "Edit", "NotebookEdit"):
        rule = f"{tool}({spec})"
        if rule not in deny:
            missing.append(rule)
sys.exit(f"missing deny rules: {missing}" if missing else 0)
PYEOF'

check "doctor clean on the blank fixture" bash -c \
  'python3 "'"$KIT"'/scripts/wiki-doctor.py" --wiki "'"$FIXTURE"'" --strict-warnings'

# ---- one full handoff -> garden -> render cycle ----------------------------
check_shell "new-handoff writes the first event" '
  cd "'"$FIXTURE"'" &&
  SHA=$(git rev-parse --short HEAD) &&
  python3 "'"$KIT"'/scripts/wiki-event.py" new-handoff \
    --wiki "'"$FIXTURE"'" \
    --tool manual \
    --summary "install smoke cycle" \
    --repo-name blank-wiki \
    --repo-branch main \
    --repo-sha "$SHA" \
    --workstream smoke-check:candidate_new \
    --what-was-done "ran the install smoke handoff cycle" \
    --next "garden this event" &&
  ls wiki/events/*/*/*.json >/dev/null
'
check_shell "commit with a non-empty pending store passes the hook" '
  cd "'"$FIXTURE"'" &&
  python3 "'"$KIT"'/scripts/wiki-event.py" build-pending --wiki "'"$FIXTURE"'" &&
  python3 "'"$KIT"'/scripts/wiki-render.py" log --wiki "'"$FIXTURE"'" &&
  git add -A && git commit -q -m "smoke: event recorded, garden pending"
'
check_shell "garden apply routes the event into a workstream" '
  cd "'"$FIXTURE"'" &&
  EVENT=$(ls wiki/events/*/*/*.json | head -1) &&
  python3 "'"$KIT"'/scripts/wiki-garden.py" "$EVENT" --wiki "'"$FIXTURE"'" --workstream smoke-check &&
  [ -f workstreams/smoke-check.md ]
'
check_shell "projections rebuild after the cycle" '
  python3 "'"$KIT"'/scripts/wiki-event.py" build-pending --wiki "'"$FIXTURE"'" &&
  python3 "'"$KIT"'/scripts/wiki-render.py" log --wiki "'"$FIXTURE"'" &&
  python3 "'"$KIT"'/scripts/wiki-render.py" claude-local --wiki "'"$FIXTURE"'" --no-lock &&
  grep -q "install smoke cycle" "'"$FIXTURE"'/wiki/log.md"
'
check_shell "cycle commit passes the pre-commit hook" '
  cd "'"$FIXTURE"'" && git add -A && git commit -q -m "smoke: handoff cycle"
'
check "doctor clean after the cycle" bash -c \
  'python3 "'"$KIT"'/scripts/wiki-doctor.py" --wiki "'"$FIXTURE"'" --strict-warnings'

# ---- enforcement probes: one seeded violation per class --------------------
check_blocked "hand-edited log.md is blocked at commit" '
  cd "'"$FIXTURE"'" &&
  echo "hand edit" >> wiki/log.md && git add wiki/log.md &&
  git commit -q -m "smoke: tamper log"
'
check_shell "reset tamper 1" 'cd "'"$FIXTURE"'" && git checkout HEAD -- wiki/log.md'
check_blocked "modifying an existing event is blocked at commit" '
  cd "'"$FIXTURE"'" &&
  EVENT=$(ls wiki/events/*/*/*.json | head -1) &&
  python3 -c "import sys; p=sys.argv[1]; s=open(p).read(); open(p,\"w\").write(s.replace(\"install smoke cycle\", \"tampered\"))" "$EVENT" &&
  git add "$EVENT" && git commit -q -m "smoke: tamper event"
'
check_shell "reset tamper 2" 'cd "'"$FIXTURE"'" && git checkout HEAD -- wiki/events'
check_blocked "stale pending projection is blocked at commit" '
  cd "'"$FIXTURE"'" &&
  python3 -c "import json,sys; p=\"wiki/pending/index.json\"; d=json.load(open(p)); d[\"event_count\"]=d.get(\"event_count\",0)+7; json.dump(d,open(p,\"w\"))" &&
  git add wiki/pending/index.json && git commit -q -m "smoke: tamper pending"
'
check_shell "reset tamper 3" 'cd "'"$FIXTURE"'" && git checkout HEAD -- wiki/pending'
check_blocked "invalid added event is blocked at commit" '
  cd "'"$FIXTURE"'" &&
  mkdir -p wiki/events/2099/01 &&
  echo "{\"event_type\": \"handoff\"}" > wiki/events/2099/01/not-an-event.json &&
  git add wiki/events/2099/01/not-an-event.json &&
  git commit -q -m "smoke: invalid event"
'
check_shell "reset tamper 4" 'cd "'"$FIXTURE"'" && git rm -q --cached wiki/events/2099/01/not-an-event.json && rm -rf wiki/events/2099'
check_blocked "invalid workstream frontmatter is blocked at commit" '
  cd "'"$FIXTURE"'" &&
  printf -- "---\nbogus: true\n---\n\nno required fields\n" > workstreams/broken.md &&
  git add workstreams/broken.md && git commit -q -m "smoke: invalid workstream"
'
check_shell "reset tamper 5" 'cd "'"$FIXTURE"'" && git rm -q --cached workstreams/broken.md && rm -f workstreams/broken.md'

# ---- idempotent reinstall --------------------------------------------------
check "second install run is idempotent" \
  "$KIT/scripts/install.sh" --wiki "$FIXTURE" --no-scheduler
check_shell "reinstall leaves the working tree clean" \
  'cd "'"$FIXTURE"'" && [ -z "$(git status --short)" ]'

# ---- decision-4 tweak: install around pre-existing content -----------------
check_shell "seed a fixture with docs and an Obsidian vault" '
  mkdir -p "'"$VAULT"'/docs" "'"$VAULT"'/.obsidian" &&
  echo "# My Notes" > "'"$VAULT"'/docs/notes.md" &&
  echo "{}" > "'"$VAULT"'/.obsidian/app.json" &&
  echo "existing readme" > "'"$VAULT"'/README.md"
'
check "install into the pre-existing vault exits zero" \
  "$KIT/scripts/install.sh" --wiki "$VAULT" --no-scheduler
check_shell "pre-existing content is byte-untouched" '
  [ "$(cat "'"$VAULT"'/docs/notes.md")" = "# My Notes" ] &&
  [ "$(cat "'"$VAULT"'/.obsidian/app.json")" = "{}" ] &&
  [ "$(cat "'"$VAULT"'/README.md")" = "existing readme" ]
'
check "doctor clean on the vault fixture" bash -c \
  'python3 "'"$KIT"'/scripts/wiki-doctor.py" --wiki "'"$VAULT"'" --strict-warnings'

write_json true 0
write_markdown PASS
echo "PASS: install smoke completed"
CONTAINER_SCRIPT
status=${PIPESTATUS[0]}
set -e

if [ "$status" -eq 0 ]; then
  if [ "${KEEP_CONTAINER:-0}" = "1" ]; then
    echo "Preserved successful container: $CONTAINER_NAME"
  else
    docker rm "$CONTAINER_NAME" >/dev/null
  fi
  exit 0
fi

echo "Smoke failed; preserved stopped container: $CONTAINER_NAME" >&2
exit "$status"
