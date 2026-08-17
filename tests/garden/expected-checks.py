#!/usr/bin/env python3
"""Validate build-index.py output after test scenarios are applied.

Run AFTER applying test fixtures to workstreams/ and wiki/log.md.
Exit 0 = all checks pass, exit 1 = failures found.
"""

import json
import subprocess
import sys
from pathlib import Path

# This fixture travels with the wiki deployment it exercises (unlike
# scripts/, which is shared kit machinery): tests/garden/ sits at
# <wiki-root>/tests/garden/, so self-locating three parents up is the
# wiki root, not a guess. Passed explicitly to build-index.py rather
# than relying on its own cwd-walk-up fallback, which would resolve
# against whatever directory the caller invoked from.
WIKI_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = WIKI_ROOT / "scripts"
BUILD_SCRIPT = SCRIPTS_DIR / "build-index.py"


def run_json() -> dict:
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--wiki", str(WIKI_ROOT), "--json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"FAIL: build-index.py --json exited {result.returncode}", file=sys.stderr
        )
        print(result.stderr, file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def run_tree() -> str:
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--wiki", str(WIKI_ROOT)],
        capture_output=True,
        text=True,
    )
    return result.stdout


def check(name: str, condition: bool):
    status = "PASS" if condition else "FAIL"
    print(f"  {status}: {name}")
    return condition


def count_check(total: int, passed: int, name: str, condition: bool) -> tuple[int, int]:
    total += 1
    passed += check(name, condition)
    return total, passed


def main():
    d = run_json()
    tree = run_tree()
    active_names = {s["name"] for s in d["active"]}
    parked_names = {s["name"] for s in d["parked"]}
    archival_names = {c["name"] for c in d["archival_candidates"]}
    stale_names = {c["workstream"] for c in d["stale_next_candidates"]}

    passed = 0
    total = 0

    print("=== Scenario A: Blocker cascade ===")
    total, passed = count_check(
        total,
        passed,
        "sync-transport is archived (status=archived)",
        "sync-transport" not in active_names and "sync-transport" not in parked_names,
    )
    total, passed = count_check(
        total,
        passed,
        "sync-transport appears in archival candidates",
        "sync-transport" in archival_names,
    )
    total, passed = count_check(
        total,
        passed,
        "queue-parity still active",
        "queue-parity" in active_names,
    )
    queue = next((s for s in d["active"] if s["name"] == "queue-parity"), None)
    if queue:
        total, passed = count_check(
            total,
            passed,
            "queue-parity blocker updated (no longer mentions #205)",
            "#205" not in queue.get("blocker", "") or queue.get("blocker", "") == "",
        )

    print("\n=== Scenario B: Multi-session accumulation ===")
    evo = next((s for s in d["active"] if s["name"] == "eval-benchmark"), None)
    total, passed = count_check(
        total,
        passed,
        "eval-benchmark still active",
        evo is not None,
    )
    if evo:
        total, passed = count_check(
            total,
            passed,
            "eval-benchmark updated to 2026-05-24",
            evo["last_updated"] == "2026-05-24",
        )
        total, passed = count_check(
            total,
            passed,
            "eval-benchmark has next_actions",
            len(evo.get("next_actions", [])) > 0,
        )

    print("\n=== Scenario C: New workstream ===")
    total, passed = count_check(
        total,
        passed,
        "usage-budget-monitoring is active",
        "usage-budget-monitoring" in active_names,
    )
    ubm = next(
        (s for s in d["active"] if s["name"] == "usage-budget-monitoring"), None
    )
    if ubm:
        total, passed = count_check(
            total,
            passed,
            "usage-budget-monitoring has issue 270",
            ubm.get("issue") == "270",
        )

    print("\n=== Scenario D: Parked reactivation ===")
    total, passed = count_check(
        total,
        passed,
        "task-router is active (reactivated from parked)",
        "task-router" in active_names,
    )
    total, passed = count_check(
        total,
        passed,
        "task-router NOT in parked",
        "task-router" not in parked_names,
    )

    print("\n=== Scenario E: Stale Next detection ===")
    total, passed = count_check(
        total,
        passed,
        "stale_next_candidates is non-empty",
        len(d["stale_next_candidates"]) > 0,
    )
    if d["stale_next_candidates"]:
        total, passed = count_check(
            total,
            passed,
            "eval-benchmark flagged for stale Next",
            "eval-benchmark" in stale_names,
        )

    print("\n=== Scenario F: Archival protection ===")
    for name in parked_names:
        total, passed = count_check(
            total,
            passed,
            f"parked stream '{name}' NOT in archival candidates",
            name not in archival_names,
        )

    print("\n=== Tree structure ===")
    total, passed = count_check(
        total, passed, "tree contains ACTIVE section", "ACTIVE" in tree
    )
    total, passed = count_check(
        total, passed, "tree contains PARKED section", "PARKED" in tree
    )
    total, passed = count_check(
        total, passed, "tree contains ARCHIVED section", "ARCHIVED" in tree
    )
    total, passed = count_check(
        total,
        passed,
        "usage-budget-monitoring appears in tree",
        "usage-budget-monitoring" in tree,
    )
    total, passed = count_check(
        total,
        passed,
        "task-router appears in ACTIVE (not PARKED)",
        "task-router" in tree.split("PARKED")[0],
    )

    print(f"\n{'=' * 40}")
    print(f"{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
