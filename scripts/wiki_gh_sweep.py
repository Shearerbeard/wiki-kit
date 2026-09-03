#!/usr/bin/env python3
"""Garden staleness sweep: resolve every externally checkable claim in the
workstream files to live GitHub state.

Mechanical facts only — the script reports each reference's kind, live
state, and citing line; judging whether a claim is stale stays with the
interactive garden and the user. Sections swept: frontmatter (pr / issue /
branch / blocker), the curated Current State / Next / Blockers sections,
and workstreams/_archive/index.md. "What Was Done" and "Session updates"
are DELIBERATELY excluded: they are historical records whose references
are expected to be merged or closed — do not add them back.

Repo resolution comes from `wiki.toml` (knobs 2 and 3): an explicit
`owner/repo#N` names its repo, a workstream's `repo:` frontmatter covers
its bare refs, and everything else falls back to the DEFAULT companion's
`github` value. A bare ref with no fallback fails loud — guessing a repo
would resolve the ref against the wrong project. Zero companions means a
blank deployment: the sweep reports nothing to sweep and exits 0.

Resolution is one `gh api repos/{owner}/{repo}/issues/{n}` call per
distinct (repo, number): GitHub PRs share the issue number space and the
response's `pull_request` key discriminates kind; PRs get a follow-up
pulls call for merged state. HTTP 404 = unresolvable reference (recorded,
sweep continues); auth / network / server errors abort loudly — a partial
sweep that looks complete is worse than no sweep.

Usage:
  uv run scripts/wiki-gh-sweep.py --wiki /path/to/wiki
  ... --json          # full inventory for tooling
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

SCRIPT_DIR = Path(__file__).resolve().parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from wiki_config import (  # noqa: E402
    ConfigError,
    WikiConfig,
    load_config,
    resolve_wiki_root,
)
from wiki_frontmatter import parse_workstream_file  # noqa: E402

SWEPT_SECTIONS = ("Current State", "Next", "Blockers")
GH_CACHE = "1h"

# owner/repo#N or bare #N, as an alternation because the two forms need
# different guards: the repo'd form rejects '/' before the repo (else the
# tail of a file path like a/b/c.md#1 reads as a repo), while the bare
# form allows '/' before '#' so slash-chained lists ("#191/#174") yield
# every ref. Rejected either way: file.md#anchor (word char before '#'),
# alphanumeric hex colors (#11aa22 — '\b' fails between digit and 'a'),
# version-ish "#1.5". Known accepted noise: all-numeric hex colors match
# (surface visibly as unresolvable); refs inside backtick code spans match
# (a ref is a ref). Known false negative: "PR#248" with no space.
REF_RE = re.compile(
    r"(?:(?<![\w./#])(?P<repo>[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)"
    r"#(?P<number1>\d+)\b(?!\.\d))"
    r"|(?:(?<![\w.#])#(?P<number2>\d+)\b(?!\.\d))"
)


def ref_match_number(match: re.Match[str]) -> int:
    return int(match.group("number1") or match.group("number2"))


GhRunner = Callable[[list[str]], "GhResult"]


class GhResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


class SweepAbort(Exception):
    """gh auth / network / server failure — the sweep must not continue."""


class Ref(NamedTuple):
    repo: str
    number: int
    repo_source: str  # explicit | frontmatter | default
    source_file: str
    source_section: str  # frontmatter:<field> | <section name> | archive
    citing_line: str


def run_gh(args: list[str]) -> GhResult:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    return GhResult(result.returncode, result.stdout, result.stderr)


def _missing_fallback_error(source_file: str, detail: str) -> ValueError:
    return ValueError(
        f"{source_file}: {detail} has no repo to resolve against — the "
        "workstream gives no repo: frontmatter and wiki.toml configures no "
        "default companion github ([wiki].default_companion -> "
        "[companions.<name>].github); add one, or write the reference as "
        "owner/repo#N"
    )


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_refs_from_text(
    text: str,
    fallback_repo: str | None,
    fallback_source: str,
    source_file: str,
    source_section: str,
) -> list[Ref]:
    refs = []
    for line in text.splitlines():
        for match in REF_RE.finditer(line):
            explicit = match.group("repo")
            if explicit is None and fallback_repo is None:
                raise _missing_fallback_error(
                    source_file, f"bare reference #{ref_match_number(match)}"
                )
            refs.append(
                Ref(
                    repo=explicit or fallback_repo,
                    number=ref_match_number(match),
                    repo_source="explicit" if explicit else fallback_source,
                    source_file=source_file,
                    source_section=source_section,
                    citing_line=line.strip(),
                )
            )
    return refs


def extract_sections(body: str) -> dict[str, str]:
    """Text of each swept ### section (### What Was Done never included).

    Stops entirely at the Session updates heading: the validator enforces
    exactly one such section and garden apply appends to its end, so
    nothing below it is curated text — including any ### heading inside a
    session block that happens to share a swept section's name.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Session updates"):
            break
        if stripped.startswith("### "):
            title = stripped.removeprefix("### ").strip()
            current = title if title in SWEPT_SECTIONS else None
            continue
        if stripped.startswith("## "):
            current = None
            continue
        if current is not None:
            sections.setdefault(current, []).append(line)
    return {title: "\n".join(lines) for title, lines in sections.items()}


class StreamFacts(NamedTuple):
    name: str
    repo: str | None  # None: repo-less stream, no default companion github
    branch: str
    refs: list[Ref]
    text: str  # raw file text, reused for the tier-3 mention scan


def collect_stream_facts(path: Path, default_repo: str | None) -> StreamFacts:
    fm, body = parse_workstream_file(path)
    repo = fm.get("repo", "").strip() or default_repo
    fallback_source = "frontmatter" if fm.get("repo", "").strip() else "default"
    name = path.stem
    refs: list[Ref] = []

    for field in ("pr", "issue"):
        value = fm.get(field, "").strip()
        if not value:
            continue  # legacy empty-string placeholders are data, not errors
        try:
            number = int(value)
        except ValueError:
            raise ValueError(
                f"{name}: frontmatter {field!r} is not an integer: {value!r}"
            ) from None
        if repo is None:
            raise _missing_fallback_error(name, f"frontmatter {field}: {value}")
        refs.append(
            Ref(
                repo=repo,
                number=number,
                repo_source=fallback_source,
                source_file=name,
                source_section=f"frontmatter:{field}",
                citing_line=f"{field}: {value}",
            )
        )
    blocker = fm.get("blocker", "").strip()
    if blocker:
        refs.extend(
            extract_refs_from_text(
                blocker, repo, fallback_source, name, "frontmatter:blocker"
            )
        )
    for title, text in extract_sections(body).items():
        refs.extend(extract_refs_from_text(text, repo, fallback_source, name, title))
    return StreamFacts(
        name=name,
        repo=repo,
        branch=fm.get("branch", "").strip(),
        refs=refs,
        text=path.read_text(),
    )


def archive_catalog_text(archive_index: Path) -> str:
    """The hand-maintained archive catalog, or nothing: no script writes
    it, and a deployment that has archived nothing has no file to read
    (build-index treats the archive as optional the same way)."""
    if not archive_index.exists():
        return ""
    return archive_index.read_text()


def collect_archive_refs(archive_index: Path, default_repo: str | None) -> list[Ref]:
    return extract_refs_from_text(
        archive_catalog_text(archive_index),
        default_repo,
        "default",
        "_archive/index.md",
        "archive",
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def gh_api_json(gh: GhRunner, endpoint: str) -> dict[str, Any] | None:
    """GET an endpoint; None on 404, SweepAbort on any other failure."""
    result = gh(["api", "--cache", GH_CACHE, endpoint])
    if result.returncode == 0:
        return json.loads(result.stdout)
    if "HTTP 404" in result.stderr:
        return None
    raise SweepAbort(
        f"gh api {endpoint} failed (auth/network/server — aborting, a "
        f"partial sweep would masquerade as complete): {result.stderr.strip()}"
    )


def resolve_ref(gh: GhRunner, repo: str, number: int) -> dict[str, Any]:
    issue = gh_api_json(gh, f"repos/{repo}/issues/{number}")
    if issue is None:
        return {"kind": "unresolvable", "state": None, "title": None}
    if "pull_request" in issue:
        pr = gh_api_json(gh, f"repos/{repo}/pulls/{number}")
        if pr is None:
            raise SweepAbort(
                f"{repo}#{number} is a PR per the issues endpoint but the "
                "pulls endpoint 404s — inconsistent API responses"
            )
        state = "MERGED" if pr.get("merged_at") else pr["state"].upper()
        return {"kind": "pr", "state": state, "title": pr.get("title")}
    return {
        "kind": "issue",
        "state": issue["state"].upper(),
        "title": issue.get("title"),
    }


def merged_prs_for_branch(gh: GhRunner, repo: str, branch: str) -> list[dict]:
    result = gh(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            branch,
            "--state",
            "merged",
            "--json",
            "number,title,mergedAt",
        ]
    )
    if result.returncode != 0:
        raise SweepAbort(
            f"gh pr list --repo {repo} --head {branch} failed: {result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def worktree_branches(repo_path: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SweepAbort(
            f"git worktree list failed in {repo_path}: {result.stderr.strip()}"
        )
    return [
        line.removeprefix("branch refs/heads/")
        for line in result.stdout.splitlines()
        if line.startswith("branch refs/heads/")
    ]


# ---------------------------------------------------------------------------
# Sweep + tiers
# ---------------------------------------------------------------------------


def sweep(
    gh: GhRunner,
    config: WikiConfig,
    workstreams_dir: Path | None = None,
    archive_index: Path | None = None,
) -> dict[str, Any]:
    if not config.companions:
        # A blank deployment: no companions configured means nothing to
        # sweep — it may not even have workstreams/ yet. Empty result,
        # zero gh calls, exit 0 upstream.
        return {
            "findings": [],
            "untracked_merged_prs": [],
            "tiers": assign_tiers([], []),
        }
    if workstreams_dir is None:
        workstreams_dir = config.root / "workstreams"
    if archive_index is None:
        archive_index = workstreams_dir / "_archive" / "index.md"
    # With >=1 companion, load_config guarantees a default companion name;
    # its github stays optional (a companion with no GitHub presence).
    default_repo = config.companion().github

    streams = [
        collect_stream_facts(path, default_repo)
        for path in sorted(workstreams_dir.glob("*.md"))
    ]
    all_refs = [ref for stream in streams for ref in stream.refs]
    all_refs += collect_archive_refs(archive_index, default_repo)

    resolutions: dict[tuple[str, int], dict[str, Any]] = {}
    for ref in all_refs:
        key = (ref.repo, ref.number)
        if key not in resolutions:
            resolutions[key] = resolve_ref(gh, ref.repo, ref.number)

    # Reverse staleness: merged PRs from known branches whose NUMBER no
    # workstream or archive text mentions. Number-only on purpose (recorded
    # plan deviation): a frontmatter branch always appears in its own file,
    # so a branch-name rule would suppress every finding for tracked
    # branches — the historical case of a branch recorded while its merged
    # PR never was is exactly what number-only catches. Mention test is
    # set membership over extracted refs, never substring ("#17" must not
    # count as a mention of #170 or vice versa). Branch pairs come from
    # workstream frontmatter AND from every companion with both an overlay
    # path and a github value on this machine — each companion's worktree
    # branches are tagged with that companion's own slug (a github-less or
    # path-less companion contributes none, per the schema's opt-out).
    workstream_text = "\n".join(stream.text for stream in streams) + (
        archive_catalog_text(archive_index)
    )
    mentioned_numbers = {
        ref_match_number(match) for match in REF_RE.finditer(workstream_text)
    }
    branch_repo_pairs = {
        (stream.repo, stream.branch)
        for stream in streams
        if stream.repo and stream.branch and stream.branch not in ("main", "master")
    }
    for companion in config.companions.values():
        if companion.path is None or companion.github is None:
            continue
        branch_repo_pairs |= {
            (companion.github, branch)
            for branch in worktree_branches(companion.path)
            if branch not in ("main", "master")
        }
    untracked_merged = []
    for repo, branch in sorted(branch_repo_pairs):
        for pr in merged_prs_for_branch(gh, repo, branch):
            if pr["number"] not in mentioned_numbers:
                untracked_merged.append({"repo": repo, "branch": branch, **pr})

    findings = [
        {**ref._asdict(), **resolutions[(ref.repo, ref.number)]} for ref in all_refs
    ]
    return {
        "findings": findings,
        "untracked_merged_prs": untracked_merged,
        "tiers": assign_tiers(findings, untracked_merged),
    }


def assign_tiers(
    findings: list[dict[str, Any]], untracked_merged: list[dict]
) -> dict[str, list[dict[str, Any]]]:
    """Tier the actionable subset; the full inventory stays in findings.

    1: frontmatter/blocker refs that are MERGED or CLOSED (a dead PR
       unblocks nothing — closed-without-merge is as actionable as merged)
    2: curated-section refs in active/parked streams, MERGED or CLOSED
    3: merged head-branch PRs no workstream/archive mentions
    4: PR-kind refs in the archive that are live-OPEN (archive implies
       shipped). OPEN issues in archive prose are common and correct
       ("#166 still open for remaining scope") — inventory only.
    """
    dead = {"MERGED", "CLOSED"}
    tier1 = [
        f
        for f in findings
        if f["source_section"].startswith("frontmatter:") and f["state"] in dead
    ]
    tier2 = [
        f
        for f in findings
        if f["source_section"] in SWEPT_SECTIONS and f["state"] in dead
    ]
    tier4 = [
        f
        for f in findings
        if f["source_section"] == "archive"
        and f["kind"] == "pr"
        and f["state"] == "OPEN"
    ]
    unresolvable = [f for f in findings if f["kind"] == "unresolvable"]
    return {
        "tier1_frontmatter_dead": tier1,
        "tier2_text_dead": tier2,
        "tier3_untracked_merged": untracked_merged,
        "tier4_archive_open_prs": tier4,
        "unresolvable": unresolvable,
    }


def render_summary(result: dict[str, Any]) -> str:
    tiers = result["tiers"]
    labels = [
        ("tier1_frontmatter_dead", "TIER 1 — frontmatter/blocker refs now dead"),
        ("tier2_text_dead", "TIER 2 — curated-text refs now dead"),
        ("tier3_untracked_merged", "TIER 3 — merged PRs no workstream tracks"),
        ("tier4_archive_open_prs", "TIER 4 — archive PRs still open"),
        ("unresolvable", "UNRESOLVABLE refs (typo or wrong repo?)"),
    ]
    lines = [f"Staleness sweep: {len(result['findings'])} refs checked."]
    for key, label in labels:
        entries = tiers[key]
        lines.append(f"\n{label}: {len(entries)}")
        for entry in entries:
            if key == "tier3_untracked_merged":
                lines.append(
                    f"  - {entry['repo']} PR #{entry['number']} "
                    f"(head {entry['branch']}, merged {entry['mergedAt']}): "
                    f"{entry['title']}"
                )
            else:
                lines.append(
                    f"  - {entry['repo']}#{entry['number']} "
                    f"[{entry['kind']}/{entry['state']}] "
                    f"{entry['source_file']} ({entry['source_section']}, "
                    f"repo_source={entry['repo_source']}): "
                    f"{entry['citing_line']}"
                )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve workstream PR/issue refs to live GitHub state"
    )
    parser.add_argument("--json", action="store_true", help="full inventory")
    parser.add_argument(
        "--wiki",
        type=Path,
        default=None,
        help="wiki repo root (default: walk up from cwd to wiki.toml)",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(resolve_wiki_root(args.wiki))
        result = sweep(run_gh, config)
    except (SweepAbort, ConfigError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    elif not config.companions:
        print("No companions configured; nothing to sweep.")
    else:
        print(render_summary(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
