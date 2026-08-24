#!/usr/bin/env python3
"""Validate build-index.py output after test scenarios are applied.

Run AFTER applying test fixtures to the fixture wiki's workstreams/ and
wiki/log.md (run-tests.sh owns that sequencing). Exit 0 = all checks
pass, exit 1 = failures found.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# The machinery under test is the kit's, located from this file's place
# in the kit checkout. The CONTENT it runs over is the throwaway fixture
# wiki run-tests.sh builds, passed in as --wiki: this file lives in the
# kit repo, which is not a wiki, so there is no root to self-locate.
KIT_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = KIT_ROOT / "scripts" / "build-index.py"


def run_json(wiki_root: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--wiki", str(wiki_root), "--json"],
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


def run_tree(wiki_root: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--wiki", str(wiki_root)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"FAIL: build-index.py (tree) exited {result.returncode}",
            file=sys.stderr,
        )
        print(result.stderr, file=sys.stderr)
        sys.exit(1)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wiki",
        type=Path,
        required=True,
        help="fixture wiki root the scenario fixtures were applied to",
    )
    args = parser.parse_args()

    d = run_json(args.wiki)
    tree = run_tree(args.wiki)
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
