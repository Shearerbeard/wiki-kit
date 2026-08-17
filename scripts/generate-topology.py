#!/usr/bin/env python3
"""Generate a workspace-topology report for a consumer repo from git state.

Consumer-side tool: every input is an explicit argument (no config load,
no env fallbacks) — the dock wiring supplies them, typically from the
companion's wiki.toml table (base_branch, ticket_regex, branch_glob,
docs_subpath) when it installs the post-commit hook.

  --repo          the consumer checkout (its main worktree)
  --base-branch   the diff base for stacking/ticket sections
  --ticket-regex  regex whose group 1 is a ticket id, matched against
                  commit bodies; absent disables the ticket-coverage
                  section entirely
  --branch-glob   the branch namespace scanned for cleanup candidates;
                  absent disables the candidate-cleanup section entirely
  --output        where the rendered markdown is written
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

STALE_DAYS = 7
AGING_DAYS = 3
DEAD_BRANCH_DAYS = 14
SECONDS_PER_DAY = 86400
GIT_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


class Worktree(NamedTuple):
    path: Path
    branch: str


class WorktreeStatus(NamedTuple):
    name: str
    branch: str
    sha: str
    dirty: str
    unpushed: str
    last_age: str


def run_git(repo: Path, args: list[str], default: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        env=GIT_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    if default is not None:
        return default
    raise RuntimeError(f"git -C {repo} {' '.join(args)} failed")


def parse_worktrees(main_repo: Path) -> list[Worktree]:
    output = run_git(main_repo, ["worktree", "list", "--porcelain"])
    worktrees: list[Worktree] = []
    current_path: Path | None = None
    current_branch = ""

    for line in output.splitlines():
        if line.startswith("worktree "):
            if current_path is not None:
                worktrees.append(Worktree(current_path, current_branch))
            current_path = Path(line.removeprefix("worktree "))
            current_branch = ""
        elif line.startswith("branch refs/heads/"):
            current_branch = line.removeprefix("branch refs/heads/")

    if current_path is not None:
        worktrees.append(Worktree(current_path, current_branch))

    return worktrees


def current_branch(worktree: Path) -> str:
    branch = run_git(worktree, ["branch", "--show-current"], default="")
    if branch:
        return branch
    described = run_git(worktree, ["describe", "--all", "--always"], default="?")
    return f"detached ({described})"


def count_dirty(worktree: Path) -> str:
    status = run_git(worktree, ["status", "--porcelain"])
    count = sum(1 for line in status.splitlines() if line)
    if count == 0:
        return "clean"
    return f"**{count} files**"


def worktree_status(main_repo: Path, worktree: Worktree) -> WorktreeStatus:
    name = "**main**" if worktree.path == main_repo else worktree.path.name
    branch = current_branch(worktree.path)
    sha = run_git(worktree.path, ["rev-parse", "--short", "HEAD"])
    dirty = count_dirty(worktree.path)
    unpushed = run_git(
        worktree.path,
        ["rev-list", "--count", "HEAD...@{upstream}"],
        default="no-remote",
    )
    last_age = run_git(worktree.path, ["log", "-1", "--format=%cr"])
    return WorktreeStatus(name, branch, sha, dirty, unpushed, last_age)


def active_branches(worktrees: list[Worktree]) -> list[str]:
    return sorted({wt.branch for wt in worktrees if wt.branch})


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
        env=GIT_ENV,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def first_stacked_parent(
    repo: Path, branch: str, branches: list[str], base_branch: str
) -> str:
    base_branches = {"main", base_branch}
    for candidate in branches:
        if candidate in base_branches or candidate == branch:
            continue
        if is_ancestor(repo, candidate, branch):
            return candidate
    return ""


def base_ref(base_branch: str) -> str:
    return f"origin/{base_branch}"


def branch_stacking_lines(
    repo: Path, branches: list[str], base_branch: str
) -> list[str]:
    lines: list[str] = []
    base = base_ref(base_branch)

    for branch in branches:
        ahead = run_git(
            repo,
            ["rev-list", "--count", f"{base}..{branch}"],
            default="?",
        )
        behind = run_git(
            repo,
            ["rev-list", "--count", f"{branch}..{base}"],
            default="?",
        )
        merge_base = run_git(repo, ["merge-base", branch, base], default="?")[:7]
        stacked_on = first_stacked_parent(repo, branch, branches, base_branch)

        if stacked_on:
            lines.append(
                f"- `{branch}` +{ahead}/-{behind} "
                f"(stacked on `{stacked_on}`, merge-base: `{merge_base}`)"
            )
        else:
            lines.append(
                f"- `{branch}` +{ahead}/-{behind} (merge-base: `{merge_base}`)"
            )

    return lines


def ticket_lines(
    repo: Path,
    branches: list[str],
    base_branch: str,
    ticket_re: re.Pattern[str],
) -> list[str]:
    lines: list[str] = []
    base = base_ref(base_branch)

    for branch in branches:
        log_body = run_git(
            repo,
            ["log", f"{base}..{branch}", "--format=%b"],
            default="",
        )
        tickets = sorted(set(ticket_re.findall(log_body)))
        if tickets:
            lines.append(f"- `{branch}`: {', '.join(tickets)}")
        else:
            lines.append(f"- `{branch}`: *(no ticket footers)*")

    return lines


def last_commit_epoch(worktree: Path) -> int:
    return int(run_git(worktree, ["log", "-1", "--format=%ct"]))


def staleness_lines(main_repo: Path, worktrees: list[Worktree]) -> list[str]:
    now = int(time.time())
    lines: list[str] = []

    for worktree in worktrees:
        name = "main" if worktree.path == main_repo else worktree.path.name
        age_days = (now - last_commit_epoch(worktree.path)) // SECONDS_PER_DAY
        if age_days >= STALE_DAYS:
            lines.append(f"- **STALE ({age_days} days):** {name}")
        elif age_days >= AGING_DAYS:
            lines.append(f"- **aging ({age_days} days):** {name}")

    if not lines:
        lines.append("All worktrees active (last commit < 3 days).")

    return lines


def stale_branch_lines(
    repo: Path,
    worktrees: list[Worktree],
    base_branch: str,
    branch_glob: str,
) -> list[str]:
    worktree_branches = {wt.branch for wt in worktrees if wt.branch}
    branches = run_git(
        repo,
        ["branch", "--list", branch_glob, "--format=%(refname:short)"],
        default="",
    ).splitlines()
    now = int(time.time())
    lines: list[str] = []

    for branch in branches:
        if branch in worktree_branches:
            continue
        epoch = int(run_git(repo, ["log", "-1", "--format=%ct", branch], default="0"))
        age_days = (now - epoch) // SECONDS_PER_DAY
        if age_days < DEAD_BRANCH_DAYS:
            continue
        merged = (
            " **[merged]**" if is_ancestor(repo, branch, base_ref(base_branch)) else ""
        )
        lines.append(f"- `{branch}` ({age_days} days old){merged}")

    return lines


def render(
    main_repo: Path,
    worktrees: list[Worktree],
    base_branch: str,
    ticket_re: re.Pattern[str] | None,
    branch_glob: str | None,
) -> str:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    branches = active_branches(worktrees)
    lines: list[str] = [
        "# Workspace Topology",
        f"> Auto-generated by post-commit hook — {timestamp}",
        "> Do not edit. Regenerated on every commit from any tool.",
        "",
        "## Active Worktrees",
        "",
        "| Worktree | Branch | SHA | Dirty | Unpushed | Last Commit |",
        "|----------|--------|-----|-------|----------|-------------|",
    ]

    lines.extend(
        f"| {status.name} | `{status.branch}` | `{status.sha}` | "
        f"{status.dirty} | {status.unpushed} | {status.last_age} |"
        for status in (worktree_status(main_repo, wt) for wt in worktrees)
    )

    lines.extend(["", f"## Branch Stacking (vs {base_branch})", ""])
    lines.extend(branch_stacking_lines(main_repo, branches, base_branch))

    if ticket_re is not None:
        lines.extend(["", "## Ticket Coverage (active branches)", ""])
        lines.extend(ticket_lines(main_repo, branches, base_branch, ticket_re))

    lines.extend(["", "## Staleness", ""])
    lines.extend(staleness_lines(main_repo, worktrees))

    if branch_glob is not None:
        lines.extend(["", "## Candidate Cleanup (branches not in worktrees)", ""])
        lines.extend(stale_branch_lines(main_repo, worktrees, base_branch, branch_glob))

    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        required=True,
        help="consumer repo checkout (its main worktree)",
    )
    parser.add_argument(
        "--base-branch",
        required=True,
        help="diff base for the stacking and ticket sections",
    )
    parser.add_argument(
        "--ticket-regex",
        default=None,
        help="regex whose group 1 is a ticket id; absent disables ticket coverage",
    )
    parser.add_argument(
        "--branch-glob",
        default=None,
        help="branch namespace for cleanup candidates; absent disables the section",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="path the rendered markdown is written to",
    )
    args = parser.parse_args(argv)

    ticket_re: re.Pattern[str] | None = None
    if args.ticket_regex is not None:
        try:
            ticket_re = re.compile(args.ticket_regex)
        except re.error as exc:
            print(
                f"error: --ticket-regex {args.ticket_regex!r} does not "
                f"compile: {exc}",
                file=sys.stderr,
            )
            return 2

    main_repo = args.repo.resolve()
    output: Path = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    worktrees = parse_worktrees(main_repo)
    output.write_text(
        render(main_repo, worktrees, args.base_branch, ticket_re, args.branch_glob)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
