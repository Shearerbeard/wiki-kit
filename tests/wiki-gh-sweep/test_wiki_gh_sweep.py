#!/usr/bin/env python3
"""Tests for the garden staleness sweep. All gh calls mocked.

Repo slugs are never string literals duplicated into assertions: each test
writes a fixture wiki.toml (the fictional acme-notes deployment with a
widget companion) into tmp_path and reads config-derived values back
through wiki_config.load_config.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.wiki_config import WikiConfig, load_config  # noqa: E402
from scripts.wiki_gh_sweep import (  # noqa: E402
    REF_RE,
    GhResult,
    SweepAbort,
    collect_archive_refs,
    collect_stream_facts,
    extract_sections,
    main,
    ref_match_number,
    resolve_ref,
    sweep,
)

WS_TEMPLATE = """---
status: active
branch: {branch}
sha: abc1234
last_updated: 2026-06-01
blocker: "{blocker}"
{extra_fm}---

## Test Stream

### Current State
- {current}

### What Was Done
#### 2026-06-01
1. Opened PR #900 and merged it long ago

### Next
- {next_item}

### Blockers
- {blocker}

## Session updates (uncurated)

_note_

### 2026-06-12 — event 0123 (claude-code)
- historical ref #901 must not be swept
"""


def write_wiki_config(
    root: Path,
    companions: dict[str, dict[str, str]] | None = None,
    default_companion: str | None = None,
    overlay_paths: dict[str, Path] | None = None,
) -> WikiConfig:
    """Write a fixture wiki.toml (+ machine overlay) and load it back."""
    root.mkdir(parents=True, exist_ok=True)
    lines = ["[wiki]", 'name = "acme-notes"']
    if default_companion is not None:
        lines.append(f'default_companion = "{default_companion}"')
    lines.append("")
    for name, table in (companions or {}).items():
        lines.append(f"[companions.{name}]")
        lines.extend(f'{key} = "{value}"' for key, value in table.items())
        lines.append("")
    lines += ["[contract]", 'protected = ["wiki/log.md"]', ""]
    (root / "wiki.toml").write_text("\n".join(lines))
    if overlay_paths:
        overlay: list[str] = []
        for name, path in overlay_paths.items():
            overlay += [f"[companions.{name}]", f'path = "{path}"', ""]
        (root / "wiki.local.toml").write_text("\n".join(overlay))
    return load_config(root)


def init_repo(path: Path, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "-C", str(path), "init", "-q", "-b", branch],
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=alex@example.com",
            "-c",
            "user.name=Alex",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "seed",
        ],
    ):
        subprocess.run(cmd, check=True, capture_output=True)


def fake_gh_factory(responses: dict[str, object]):
    """Map endpoint/command prefixes to canned responses.

    Values: dict -> 200 JSON; "404" -> not found; "boom" -> HTTP 500.
    """
    calls: list[list[str]] = []

    def fake_gh(args: list[str]) -> GhResult:
        calls.append(args)
        if args[0] == "api":
            endpoint = args[-1]
            value = responses.get(endpoint, "404")
        else:  # gh pr list --repo R --head B ...
            repo = args[args.index("--repo") + 1]
            branch = args[args.index("--head") + 1]
            value = responses.get(f"prlist:{repo}:{branch}", [])
        if value == "404":
            return GhResult(1, "", "gh: Not Found (HTTP 404)")
        if value == "boom":
            return GhResult(1, "", "gh: HTTP 500 server exploded")
        return GhResult(0, json.dumps(value), "")

    fake_gh.calls = calls
    return fake_gh


class RefRegexTest(unittest.TestCase):
    def refs(self, text: str) -> list[tuple[str | None, int]]:
        return [(m.group("repo"), ref_match_number(m)) for m in REF_RE.finditer(text)]

    def test_matches_expected_forms(self) -> None:
        self.assertEqual(self.refs("PR #248 is open"), [(None, 248)])
        self.assertEqual(self.refs("bare #62 ref"), [(None, 62)])
        self.assertEqual(
            self.refs("see acme/widget#209 there"), [("acme/widget", 209)]
        )

    def test_rejects_noise(self) -> None:
        self.assertEqual(self.refs("stage5-results.md#123"), [])
        self.assertEqual(self.refs("analysis/dir/stage5-results.md#123"), [])
        self.assertEqual(self.refs("color #11aa22 swatch"), [])
        self.assertEqual(self.refs("Rank #1.5 item"), [])
        self.assertEqual(self.refs("WID-1234 ticket ref"), [])

    def test_slash_chained_refs_all_match(self) -> None:
        # Real corpus style: "#191/#174".
        self.assertEqual(self.refs("#191/#174"), [(None, 191), (None, 174)])
        self.assertEqual(
            self.refs("#184/#186/#189"),
            [(None, 184), (None, 186), (None, 189)],
        )

    def test_accepted_noise_is_documented_behavior(self) -> None:
        # Backtick code spans match (a ref is a ref) and all-numeric hex
        # colors match (they surface visibly as unresolvable) — accepted
        # noise per the REF_RE comment, pinned so a change is deliberate.
        self.assertEqual(self.refs("run `gh pr view #123`"), [(None, 123)])
        self.assertEqual(self.refs("color #112233 swatch"), [(None, 112233)])
        # No-space "PR#248" is a known false negative.
        self.assertEqual(self.refs("PR#248"), [])


class ExtractionTest(unittest.TestCase):
    def make_config(self, tmp: Path) -> WikiConfig:
        return write_wiki_config(
            tmp / "acme-notes", companions={"widget": {"github": "acme/widget"}}
        )

    def make_stream(self, tmp: Path, github: str, **kwargs) -> Path:
        defaults = {
            "branch": "alex/feature-x",
            "blocker": "PR #100 needs review",
            "extra_fm": 'pr: 100\nissue: "166"\nrepo: other/repo\n',
            "current": "shipped via #101",
            "next_item": f"land {github}#209",
        }
        defaults.update(kwargs)
        path = tmp / "test-stream.md"
        path.write_text(WS_TEMPLATE.format(**defaults))
        return path

    def test_sections_exclude_what_was_done_and_session_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(Path(tmp))
            github = config.companion().github
            path = self.make_stream(Path(tmp), github)
            facts = collect_stream_facts(path, github)
            numbers = sorted(ref.number for ref in facts.refs)
            # 100 (pr fm) + 166 (issue fm) + 100 (blocker fm) + 101 + 209
            # + 100 (Blockers section mirrors frontmatter text).
            # Never 900 (What Was Done) or 901 (Session updates).
            self.assertNotIn(900, numbers)
            self.assertNotIn(901, numbers)
            self.assertIn(101, numbers)
            self.assertIn(166, numbers)

    def test_repo_resolution_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(Path(tmp))
            github = config.companion().github
            path = self.make_stream(Path(tmp), github)
            facts = collect_stream_facts(path, github)
            by_number = {}
            for ref in facts.refs:
                by_number.setdefault(ref.number, ref)
            # Explicit owner/repo wins over frontmatter repo.
            self.assertEqual(by_number[209].repo, github)
            self.assertEqual(by_number[209].repo_source, "explicit")
            # Bare refs use the frontmatter repo.
            self.assertEqual(by_number[101].repo, "other/repo")
            self.assertEqual(by_number[101].repo_source, "frontmatter")

    def test_garbage_frontmatter_pr_names_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(Path(tmp))
            github = config.companion().github
            path = self.make_stream(Path(tmp), github, extra_fm="pr: not-a-number\n")
            with self.assertRaises(ValueError) as ctx:
                collect_stream_facts(path, github)
            self.assertIn("test-stream", str(ctx.exception))
            self.assertIn("not-a-number", str(ctx.exception))

    def test_empty_placeholder_fields_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(Path(tmp))
            github = config.companion().github
            path = self.make_stream(
                Path(tmp),
                github,
                extra_fm='pr: ""\nissue: ""\n',
                blocker="no refs here",
                current="nothing",
                next_item="nothing",
            )
            facts = collect_stream_facts(path, github)
            self.assertEqual(facts.refs, [])
            self.assertEqual(facts.repo, github)

    def test_bare_ref_without_any_fallback_names_file_and_config(self) -> None:
        # A repo-less workstream with a bare ref, no default companion
        # github to fall back to: fail loud, never guess a repo.
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_stream(
                Path(tmp),
                "unused/unused",
                extra_fm="",
                blocker="none",
                current="working on #101",
                next_item="nothing",
            )
            with self.assertRaises(ValueError) as ctx:
                collect_stream_facts(path, None)
            message = str(ctx.exception)
            self.assertIn("test-stream", message)
            self.assertIn("#101", message)
            self.assertIn("github", message)

    def test_extract_sections_boundaries(self) -> None:
        sections = extract_sections(
            "### Current State\n- a\n### What Was Done\n- b\n"
            "### Next\n- c\n## Session updates (uncurated)\n### Next\n- d\n"
        )
        self.assertEqual(sections["Current State"].strip(), "- a")
        self.assertNotIn("What Was Done", sections)
        # The ## heading ends section tracking; the ### Next inside the
        # session-updates block re-opens it by title — assert it did NOT.
        self.assertNotIn("- d", sections["Next"])


class ResolutionTest(unittest.TestCase):
    def test_pr_kind_via_pull_request_key(self) -> None:
        gh = fake_gh_factory(
            {
                "repos/acme/widget/issues/5": {"state": "closed", "pull_request": {}},
                "repos/acme/widget/pulls/5": {
                    "state": "closed",
                    "merged_at": "2026-06-01T00:00:00Z",
                    "title": "merged pr",
                },
            }
        )
        self.assertEqual(
            resolve_ref(gh, "acme/widget", 5),
            {"kind": "pr", "state": "MERGED", "title": "merged pr"},
        )

    def test_issue_kind_without_pull_request_key(self) -> None:
        gh = fake_gh_factory(
            {"repos/acme/widget/issues/245": {"state": "closed", "title": "i"}}
        )
        self.assertEqual(
            resolve_ref(gh, "acme/widget", 245),
            {"kind": "issue", "state": "CLOSED", "title": "i"},
        )

    def test_404_is_unresolvable_not_fatal(self) -> None:
        gh = fake_gh_factory({})
        self.assertEqual(resolve_ref(gh, "acme/widget", 99999)["kind"], "unresolvable")

    def test_server_error_aborts(self) -> None:
        gh = fake_gh_factory({"repos/acme/widget/issues/7": "boom"})
        with self.assertRaises(SweepAbort):
            resolve_ref(gh, "acme/widget", 7)


class ArchiveRefsTest(unittest.TestCase):
    def test_no_archive_catalog_contributes_nothing(self) -> None:
        # Nothing writes the catalog; a deployment that has archived
        # nothing (or moved pages without cataloguing them) has none.
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "workstreams" / "_archive" / "index.md"
            self.assertEqual(collect_archive_refs(index, "acme/widget"), [])

    def test_sweep_runs_without_an_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = write_wiki_config(
                root / "acme-notes",
                companions={"widget": {"github": "acme/widget"}},
            )
            ws_dir = config.root / "workstreams"
            ws_dir.mkdir(parents=True, exist_ok=True)
            (ws_dir / "stream.md").write_text(
                WS_TEMPLATE.format(
                    branch="main",
                    blocker="",
                    extra_fm="",
                    current="working",
                    next_item="nothing",
                )
            )
            result = sweep(fake_gh_factory({}), config, workstreams_dir=ws_dir)
            self.assertEqual(result["tiers"]["tier4_archive_open_prs"], [])


class SweepTest(unittest.TestCase):
    def test_end_to_end_tiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            widget_dir = root / "widget"
            init_repo(widget_dir)  # checked out on main: never swept
            config = write_wiki_config(
                root / "acme-notes",
                companions={"widget": {"github": "acme/widget"}},
                overlay_paths={"widget": widget_dir},
            )
            github = config.companion().github
            ws_dir = config.root / "workstreams"
            archive_dir = ws_dir / "_archive"
            archive_dir.mkdir(parents=True)
            (ws_dir / "stream-a.md").write_text(
                WS_TEMPLATE.format(
                    branch="alex/feature-x",
                    blocker="PR #100 needs review",
                    extra_fm="",
                    current="working on #101",
                    next_item="nothing",
                )
            )
            archive_index = archive_dir / "index.md"
            archive_index.write_text(
                "# Archived\n\n- **old** — PR #300 merged.\n"
                "- **odd** — PR #301 supposedly shipped.\n"
                "- **scoped** — issue #166 still open for remaining scope.\n"
            )

            gh = fake_gh_factory(
                {
                    # blocker PR: merged -> tier 1
                    f"repos/{github}/issues/100": {
                        "state": "closed",
                        "pull_request": {},
                    },
                    f"repos/{github}/pulls/100": {
                        "state": "closed",
                        "merged_at": "x",
                        "title": "t100",
                    },
                    # text ref: open issue -> no tier
                    f"repos/{github}/issues/101": {"state": "open", "title": "t101"},
                    # archive merged PR -> inventory only
                    f"repos/{github}/issues/300": {
                        "state": "closed",
                        "pull_request": {},
                    },
                    f"repos/{github}/pulls/300": {
                        "state": "closed",
                        "merged_at": "x",
                        "title": "t300",
                    },
                    # archive OPEN PR -> tier 4
                    f"repos/{github}/issues/301": {
                        "state": "open",
                        "pull_request": {},
                    },
                    f"repos/{github}/pulls/301": {
                        "state": "open",
                        "merged_at": None,
                        "title": "t301",
                    },
                    # archive open ISSUE (the #166 case) -> NOT tier 4
                    f"repos/{github}/issues/166": {"state": "open", "title": "t166"},
                    # branch has a merged PR nobody mentions -> tier 3
                    f"prlist:{github}:alex/feature-x": [
                        {"number": 555, "title": "t555", "mergedAt": "x"}
                    ],
                }
            )
            result = sweep(
                gh, config, workstreams_dir=ws_dir, archive_index=archive_index
            )
            tiers = result["tiers"]
            t1 = {f["number"] for f in tiers["tier1_frontmatter_dead"]}
            self.assertIn(100, t1)
            self.assertEqual(
                [f["number"] for f in tiers["tier4_archive_open_prs"]], [301]
            )
            self.assertEqual(
                [pr["number"] for pr in tiers["tier3_untracked_merged"]], [555]
            )
            self.assertEqual(tiers["unresolvable"], [])
            # The open issue and merged archive PR stay inventory-only.
            self.assertNotIn(
                166, {f["number"] for f in tiers["tier4_archive_open_prs"]}
            )
            # One gh api call per distinct (repo, number), each cached.
            api_calls = [c for c in gh.calls if c[0] == "api"]
            issue_calls = [c for c in api_calls if "/issues/" in c[-1]]
            self.assertEqual(len(issue_calls), len({c[-1] for c in issue_calls}))
            for call in api_calls:
                self.assertIn("--cache", call)
            # The companion checkout sits on main, so the only reverse-check
            # branch is the stream's frontmatter branch.
            prlist_branches = {
                c[c.index("--head") + 1] for c in gh.calls if c[0] == "pr"
            }
            self.assertEqual(prlist_branches, {"alex/feature-x"})

    def test_mention_check_is_not_substring_matching(self) -> None:
        # "#170" in the corpus must NOT count as a mention of merged PR
        # #17 (and vice versa) — the prefix-collision regression.
        with tempfile.TemporaryDirectory() as tmp:
            config = write_wiki_config(
                Path(tmp) / "acme-notes",
                companions={"widget": {"github": "acme/widget"}},
            )
            github = config.companion().github
            ws_dir = config.root / "workstreams"
            (ws_dir / "_archive").mkdir(parents=True)
            (ws_dir / "_archive" / "index.md").write_text("# Archived\n")
            (ws_dir / "stream-p.md").write_text(
                WS_TEMPLATE.format(
                    branch="alex/feature-y",
                    blocker="none",
                    extra_fm="",
                    current="tracking #170 only",
                    next_item="nothing",
                )
            )
            gh = fake_gh_factory(
                {
                    f"repos/{github}/issues/170": {"state": "open", "title": "t"},
                    f"prlist:{github}:alex/feature-y": [
                        {"number": 17, "title": "t17", "mergedAt": "x"}
                    ],
                }
            )
            result = sweep(
                gh,
                config,
                workstreams_dir=ws_dir,
                archive_index=ws_dir / "_archive" / "index.md",
            )
            self.assertEqual(
                [pr["number"] for pr in result["tiers"]["tier3_untracked_merged"]],
                [17],
            )

    def test_main_branch_never_swept_for_reverse_staleness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_wiki_config(
                Path(tmp) / "acme-notes",
                companions={"widget": {"github": "acme/widget"}},
            )
            ws_dir = config.root / "workstreams"
            (ws_dir / "_archive").mkdir(parents=True)
            (ws_dir / "_archive" / "index.md").write_text("# Archived\n")
            (ws_dir / "stream-m.md").write_text(
                WS_TEMPLATE.format(
                    branch="main",
                    blocker="none",
                    extra_fm="",
                    current="nothing",
                    next_item="nothing",
                )
            )
            gh = fake_gh_factory({})
            result = sweep(
                gh,
                config,
                workstreams_dir=ws_dir,
                archive_index=ws_dir / "_archive" / "index.md",
            )
            prlist_calls = [c for c in gh.calls if c[0] == "pr"]
            self.assertEqual(prlist_calls, [])
            self.assertEqual(result["tiers"]["tier3_untracked_merged"], [])

    def test_zero_companions_is_a_no_op(self) -> None:
        # A blank deployment: no companions, no workstreams/ dir at all.
        # The sweep returns an empty result and makes zero gh calls.
        with tempfile.TemporaryDirectory() as tmp:
            config = write_wiki_config(Path(tmp) / "acme-notes")
            gh = fake_gh_factory({})
            result = sweep(gh, config)
            self.assertEqual(result["findings"], [])
            self.assertEqual(result["untracked_merged_prs"], [])
            for entries in result["tiers"].values():
                self.assertEqual(entries, [])
            self.assertEqual(gh.calls, [])

    def test_zero_companions_main_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_wiki_config(Path(tmp) / "acme-notes")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["--wiki", str(Path(tmp) / "acme-notes")])
            self.assertEqual(code, 0)
            self.assertIn("nothing to sweep", out.getvalue())

    def test_bare_ref_without_default_github_fails_loud(self) -> None:
        # One companion, no github value: bare refs have no fallback and
        # the sweep must name the workstream file and the missing config.
        with tempfile.TemporaryDirectory() as tmp:
            config = write_wiki_config(
                Path(tmp) / "acme-notes", companions={"widget": {}}
            )
            ws_dir = config.root / "workstreams"
            (ws_dir / "_archive").mkdir(parents=True)
            (ws_dir / "_archive" / "index.md").write_text("# Archived\n")
            (ws_dir / "stream-b.md").write_text(
                WS_TEMPLATE.format(
                    branch="alex/feature-z",
                    blocker="none",
                    extra_fm="",
                    current="working on #101",
                    next_item="nothing",
                )
            )
            gh = fake_gh_factory({})
            with self.assertRaises(ValueError) as ctx:
                sweep(
                    gh,
                    config,
                    workstreams_dir=ws_dir,
                    archive_index=ws_dir / "_archive" / "index.md",
                )
            message = str(ctx.exception)
            self.assertIn("stream-b", message)
            self.assertIn("github", message)
            self.assertEqual(gh.calls, [])

    def test_multi_companion_worktree_tagging(self) -> None:
        # Two companions with local checkouts: each companion's worktree
        # branches are tagged with that companion's own github slug.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            widget_dir = root / "widget"
            gadget_dir = root / "gadget"
            init_repo(widget_dir, branch="alex/feature-w")
            init_repo(gadget_dir, branch="alex/feature-g")
            config = write_wiki_config(
                root / "acme-notes",
                companions={
                    "widget": {"github": "acme/widget"},
                    "gadget": {"github": "acme/gadget"},
                },
                default_companion="widget",
                overlay_paths={"widget": widget_dir, "gadget": gadget_dir},
            )
            widget_gh = config.companion("widget").github
            gadget_gh = config.companion("gadget").github
            ws_dir = config.root / "workstreams"
            (ws_dir / "_archive").mkdir(parents=True)
            (ws_dir / "_archive" / "index.md").write_text("# Archived\n")
            gh = fake_gh_factory(
                {
                    f"prlist:{widget_gh}:alex/feature-w": [
                        {"number": 55, "title": "t55", "mergedAt": "x"}
                    ],
                    f"prlist:{gadget_gh}:alex/feature-g": [
                        {"number": 77, "title": "t77", "mergedAt": "x"}
                    ],
                }
            )
            result = sweep(
                gh,
                config,
                workstreams_dir=ws_dir,
                archive_index=ws_dir / "_archive" / "index.md",
            )
            tagged = {
                (pr["repo"], pr["branch"], pr["number"])
                for pr in result["tiers"]["tier3_untracked_merged"]
            }
            self.assertEqual(
                tagged,
                {
                    (widget_gh, "alex/feature-w", 55),
                    (gadget_gh, "alex/feature-g", 77),
                },
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
