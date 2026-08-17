#!/usr/bin/env python3
"""Tests for the night-shift T0 runner (wiki_night.py)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

KIT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = KIT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "scripts.wiki_night", KIT_ROOT / "scripts" / "wiki_night.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load scripts/wiki_night.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scripts.wiki_night"] = mod
    spec.loader.exec_module(mod)
    return mod


wiki_night = _load_module()
import wiki_config  # noqa: E402
import wiki_event as real_wiki_event  # noqa: E402

WIKI_CONFIG_FILE = wiki_config.CONFIG_FILE_NAME

# The fictional deployment the kit test corpus uses: a wiki named
# acme-notes with one companion repo, widget. Non-default [night]
# values so any hardcoded convention in the runner fails these tests.
WIKI_TOML = """\
[wiki]
name = "acme-notes"

[contract]
protected = ["wiki/log.md", "wiki/events/**"]

[night]
report_dir = "journal/nightly"
commit_prefix = "nightly:"
"""


def _write_real_pending_handoff(events_dir: Path, summary: str = "real event") -> Path:
    """A schema-valid handoff event, written at its canonical path, for
    tests that exercise the real validate_canonical_event path (not a
    mocked classification)."""
    event = {
        "event_id": real_wiki_event.uuid7(),
        "event_type": "handoff",
        "schema_version": 2,
        "timestamp_utc": real_wiki_event.utc_timestamp(),
        "tool": "claude-code",
        "status": "pending_garden",
        "summary": summary,
        "repo": {"name": "widget", "branch": "main", "sha": "abc1234"},
        "sources": [],
        "workstream_state": {
            "current_state": ["state 1"],
            "what_was_done": ["did thing"],
            "next": ["next thing"],
            "blockers": [],
            "continuation_context": "context",
        },
        "proposed_workstreams": [
            {"name": "wiki-system", "relationship": "primary", "proposed_action": "u"}
        ],
    }
    real_wiki_event.validate_event(event)
    return real_wiki_event.write_event(events_dir, event)


def _make_event(
    event_id: str = "test-123",
    with_state: bool = True,
    summary: str = "test event",
) -> dict:
    event = {
        "event_id": event_id,
        "event_type": "handoff",
        "schema_version": 2,
        "timestamp_utc": "2026-07-04T00:00:00Z",
        "tool": "opencode",
        "status": "pending_garden",
        "summary": summary,
        "repo": {"name": "widget", "branch": "main", "sha": "abc1234"},
    }
    if with_state:
        event["workstream_state"] = {
            "current_state": ["state 1"],
            "what_was_done": ["did thing"],
            "next": ["next thing"],
            "blockers": [],
            "continuation_context": "context",
        }
        event["proposed_workstreams"] = [
            {
                "name": "wiki-system",
                "relationship": "primary",
                "proposed_action": "update",
            }
        ]
    return event


def _make_pending_entry(event_id: str, event_rel_path: str) -> dict:
    return {
        "event_id": event_id,
        "event_path": event_rel_path,
        "status": "pending_garden",
        "summary": "test event",
        "proposed_workstreams": [
            {
                "name": "wiki-system",
                "relationship": "primary",
                "proposed_action": "update",
            }
        ],
    }


class NightRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve() / "wiki"
        self.root.mkdir()
        (self.root / WIKI_CONFIG_FILE).write_text(WIKI_TOML)
        self.config = wiki_config.load_config(self.root)
        (self.root / "scripts").mkdir()
        (self.root / self.config.night.report_dir).mkdir(parents=True)
        (self.root / "wiki" / "events").mkdir(parents=True)
        (self.root / "wiki" / "pending").mkdir(parents=True)
        (self.root / "workstreams").mkdir()

    def _make_runner(self, **kwargs: object) -> object:
        return wiki_night.NightRunner(self.config, **kwargs)

    def _write_pending_index(self, entries: list[dict]) -> None:
        pending_dir = self.root / "wiki" / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        index = {"event_count": len(entries), "events": entries}
        (pending_dir / "index.json").write_text(json.dumps(index))

    def _write_event(self, rel_path: str, event: dict) -> None:
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(event))

    def test_report_only_generates_report(self) -> None:
        runner = self._make_runner(report_only=True)
        with patch.object(runner, "_count_pending", return_value=0):
            exit_code = runner.run()
        self.assertEqual(exit_code, 0)
        self.assertTrue(runner.report_path.exists())
        text = runner.report_path.read_text()
        self.assertIn("report-only", text)
        self.assertNotEqual(runner.report_path.name, f"{runner.report.date}.md")

    def test_preflight_aborts_on_dirty_tree(self) -> None:
        runner = self._make_runner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=" M some-file.md\n",
                stderr="",
            )
            result = runner._preflight()
        self.assertIn("dirty", result[1])

    def test_preflight_passes_on_clean_tree(self) -> None:
        runner = self._make_runner()
        calls = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="free\n", stderr=""),
        ]
        with (
            patch("subprocess.run", side_effect=calls),
            patch.object(runner, "_capture_baseline") as baseline,
        ):
            result = runner._preflight()
        self.assertEqual(result[1], "")
        baseline.assert_called_once_with()

    def test_dry_run_does_not_commit(self) -> None:
        runner = self._make_runner(dry_run=True)
        self.assertTrue(runner.dry_run)
        self.assertEqual(runner.report.mode, "dry-run")

    def test_scheduled_report_uses_canonical_path(self) -> None:
        runner = self._make_runner(scheduled=True)
        self.assertEqual(runner.report_path.name, f"{runner.report.date}.md")

    def test_report_path_comes_from_config_report_dir(self) -> None:
        runner = self._make_runner(scheduled=True)
        expected = (
            self.root / self.config.night.report_dir / f"{runner.report.date}.md"
        )
        self.assertEqual(runner.report_path, expected)

    def test_run_fails_loud_without_wiki_config(self) -> None:
        """Rewritten from the source's alternate-root guard: the kit
        runner accepts any wiki root, but only through --wiki pointing at
        a directory that actually holds wiki.toml; anything else fails
        loud before the pipeline starts."""
        bare = Path(self._tmp.name) / "not-a-wiki"
        bare.mkdir()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = wiki_night.main(["run", "--wiki", str(bare)])
        self.assertEqual(exit_code, 1)
        self.assertIn(WIKI_CONFIG_FILE, stderr.getvalue())

    def test_report_format_contains_sections(self) -> None:
        runner = self._make_runner()
        runner.report.doctor_clean = True
        runner.report.doctor_result = "PASS render-log"
        runner.report.applied_events.append(
            wiki_night.AppliedReportEvent("test-1", "test", "wiki-system")
        )
        text = runner._format_report()
        self.assertIn("## Applied events", text)
        self.assertIn("## Doctor", text)
        self.assertIn("## Metrics", text)
        self.assertIn("## Steps", text)
        self.assertIn("test-1", text)

    def test_manual_event_without_workstream_state_requires_attention(self) -> None:
        event_rel = "wiki/events/2026/07/test.json"
        self._write_event(event_rel, _make_event(with_state=False))
        self._write_pending_index([_make_pending_entry("test-123", event_rel)])
        runner = self._make_runner()
        with (
            patch.object(
                wiki_night,
                "load_verified_pending_index",
                return_value={
                    "event_count": 1,
                    "events": [_make_pending_entry("test-123", event_rel)],
                },
            ),
            patch.object(
                wiki_night,
                "apply_event",
                side_effect=wiki_night.ManualApplyRequired("manual required"),
            ),
        ):
            output, error = runner._apply_pending()
        self.assertEqual(error, "")
        self.assertIn("manual 1", output)
        self.assertEqual(len(runner.report.manual_actions), 1)
        self.assertIn("/garden", runner.report.manual_actions[0].action)

    def test_no_pending_events(self) -> None:
        runner = self._make_runner()
        with patch.object(
            wiki_night,
            "load_verified_pending_index",
            return_value={"event_count": 0, "events": []},
        ):
            output, error = runner._apply_pending()
        self.assertEqual(error, "")
        self.assertIn("no pending", output)

    def test_applied_event_records_exact_touched_paths(self) -> None:
        event_rel = "wiki/events/2026/07/test.json"
        event = _make_event()
        self._write_event(event_rel, event)
        runner = self._make_runner()
        workstream_path = self.root / "workstreams" / "wiki-system.md"
        garden_path = self.root / "wiki" / "events" / "2026" / "07" / "apply.json"
        workstream_path.write_text("workstream\n")
        garden_path.parent.mkdir(parents=True, exist_ok=True)
        garden_path.write_text("{}\n")
        (self.root / "wiki" / "pending" / "index.json").write_text("{}\n")
        (self.root / "wiki" / "pending" / "latest.md").write_text("pending\n")
        applied = MagicMock(
            event_id="test-123",
            workstream="wiki-system",
            workstream_path=workstream_path,
            garden_event_path=garden_path,
        )
        with (
            patch.object(
                wiki_night,
                "load_verified_pending_index",
                return_value={
                    "event_count": 1,
                    "events": [_make_pending_entry("test-123", event_rel)],
                },
            ),
            patch.object(wiki_night, "apply_event", return_value=applied),
        ):
            output, error = runner._apply_pending()
        self.assertEqual(error, "")
        self.assertIn("applied 1", output)
        self.assertIn(Path("workstreams/wiki-system.md"), runner._touched_paths)
        self.assertIn(Path("wiki/events/2026/07/apply.json"), runner._touched_paths)
        self.assertIn(Path("wiki/pending/index.json"), runner._touched_paths)
        # Friction A: the original input event must also be staged, or the
        # pre-commit byte-equal(log.md) invariant breaks after an automated
        # night apply.
        self.assertIn(Path(event_rel), runner._touched_paths)

    def test_apply_skips_event_whose_target_is_foreign_dirty(self) -> None:
        """The overlap the commit-time safety net can't catch on its own:
        a pending event targeting a workstream that is ALSO foreign-dirty
        at preflight must never be applied, since apply_event would read
        the foreign mid-edit content off disk and _touch would stage it
        alongside the runner's own change. Friction A requires foreign
        content to never be touched or staged, full stop."""
        self._init_git_repo()
        foreign_content = "SECRET MID-EDIT NOT MEANT TO BE COMMITTED YET\n"
        workstream_path = self.root / "workstreams" / "wiki-system.md"
        workstream_path.write_text(foreign_content)
        real_event_path = _write_real_pending_handoff(self.root / "wiki" / "events")
        event_rel = str(real_event_path.relative_to(self.root))
        real_event_id = json.loads(real_event_path.read_text())["event_id"]
        runner = self._make_runner()
        # Populate _foreign_dirty_at_start exactly as a real _preflight() run
        # would, given this real dirty tree.
        preflight_output, preflight_error = runner._preflight()
        self.assertEqual(preflight_error, "")
        self.assertIn(
            Path("workstreams/wiki-system.md"), runner._foreign_dirty_at_start
        )

        with (
            patch.object(
                wiki_night,
                "load_verified_pending_index",
                return_value={
                    "event_count": 1,
                    "events": [_make_pending_entry(real_event_id, event_rel)],
                },
            ),
            patch.object(wiki_night, "apply_event") as mock_apply,
        ):
            output, error = runner._apply_pending()

        self.assertEqual(error, "")
        self.assertIn("manual 1", output)
        mock_apply.assert_not_called()
        self.assertNotIn(Path("workstreams/wiki-system.md"), runner._touched_paths)
        self.assertEqual(workstream_path.read_text(), foreign_content)
        self.assertEqual(len(runner.report.manual_actions), 1)
        self.assertIn("foreign dirt", runner.report.manual_actions[0].reason)

    def test_durable_apply_guidance_survives_broken_pending_projection(self) -> None:
        event_rel = "wiki/events/2026/07/test.json"
        self._write_event(event_rel, _make_event())
        pending_dir = self.root / "wiki" / "pending"
        pending_dir.rmdir()
        pending_dir.write_text("projection path is not a directory\n")
        workstream_path = self.root / "workstreams" / "wiki-system.md"
        workstream_path.write_text("durable workstream\n")
        garden_path = self.root / "wiki" / "events" / "2026" / "07" / "apply.json"
        garden_path.write_text("{}\n")
        durable_error = wiki_night.DurableApplyNeedsRepair(
            "apply is durable; repair pending projection; do not re-apply",
            garden_event_path=garden_path,
            workstream_path=workstream_path,
        )
        runner = self._make_runner()
        with (
            patch.object(
                wiki_night,
                "load_verified_pending_index",
                return_value={
                    "event_count": 1,
                    "events": [_make_pending_entry("test-123", event_rel)],
                },
            ),
            patch.object(wiki_night, "apply_event", side_effect=durable_error),
            self.assertRaises(wiki_night.DurableApplyNeedsRepair) as raised,
        ):
            runner._apply_pending()

        self.assertIs(raised.exception, durable_error)
        self.assertIn("do not re-apply", str(raised.exception))
        self.assertEqual(
            runner._touched_paths,
            {
                Path("workstreams/wiki-system.md"),
                Path("wiki/events/2026/07/apply.json"),
            },
        )

    def test_abort_sets_abort_reason(self) -> None:
        runner = self._make_runner()
        runner.report.abort_reason = "doctor not clean"
        runner.report.outcome = wiki_night.RunOutcome.ABORTED
        text = runner._format_report()
        self.assertIn("**Outcome:** aborted", text)
        self.assertIn("ABORTED", text)
        self.assertIn("doctor not clean", text)

    def test_lock_token_stored(self) -> None:
        runner = self._make_runner()
        runner._lock_token = "abc123"
        runner.report.lock_token = "abc123"
        self.assertEqual(runner.report.lock_token, "abc123")

    def test_double_apply_race_reconciles_and_rebuilds(self) -> None:
        event_rel = "wiki/events/2026/07/test.json"
        self._write_event(event_rel, _make_event())
        self._write_pending_index([_make_pending_entry("test-123", event_rel)])
        runner = self._make_runner()
        with (
            patch.object(
                wiki_night,
                "load_verified_pending_index",
                return_value={
                    "event_count": 1,
                    "events": [_make_pending_entry("test-123", event_rel)],
                },
            ),
            patch.object(
                wiki_night,
                "apply_event",
                side_effect=wiki_night.AlreadyDispositioned(
                    "already has garden-apply disposition"
                ),
            ),
            patch.object(runner, "_rebuild_pending") as rebuild,
        ):
            output, error = runner._apply_pending()
        self.assertEqual(error, "")
        self.assertIn("reconciled 1", output)
        self.assertEqual(len(runner.report.reconciled_events), 1)
        self.assertIn(
            "already has garden-apply", runner.report.reconciled_events[0].reason
        )
        rebuild.assert_called_once_with()

    def test_doctor_failure_aborts_before_commit(self) -> None:
        runner = self._make_runner()
        with (
            patch.object(runner, "_preflight", return_value=("clean", "")),
            patch.object(runner, "_acquire_lock", return_value=("locked token=x", "")),
            patch.object(runner, "_release_lock"),
            patch.object(runner, "_count_pending", return_value=0),
            patch.object(runner, "_apply_pending", return_value=("applied 0", "")),
            patch.object(runner, "_render_log", return_value=("ok", "")),
            patch.object(runner, "_render_claude_local", return_value=("ok", "")),
            patch.object(runner, "_gh_sweep", return_value=("ok", "")),
            patch.object(runner, "_memory_triage", return_value=("ok", "")),
            patch.object(runner, "_collect_metrics", return_value=("ok", "")),
            patch.object(runner, "_generate_report_pass1", return_value=("ok", "")),
            patch.object(runner, "_run_doctor", return_value=("", "FAIL render-log")),
            patch.object(runner, "_commit") as mock_commit,
        ):
            exit_code = runner.run()
        self.assertEqual(exit_code, 1)
        mock_commit.assert_not_called()
        self.assertIn("doctor", runner.report.abort_reason)

    def _assert_required_step_failure_blocks_commit(
        self, method_name: str, failure: tuple[str, str]
    ) -> object:
        runner = self._make_runner()
        with (
            patch.object(runner, "_preflight", return_value=("clean", "")),
            patch.object(runner, "_acquire_lock", return_value=("locked", "")),
            patch.object(runner, "_release_lock"),
            patch.object(runner, "_count_pending", return_value=0),
            patch.object(runner, "_apply_pending", return_value=("ok", "")),
            patch.object(runner, "_render_log", return_value=("ok", "")),
            patch.object(runner, "_render_claude_local", return_value=("ok", "")),
            patch.object(runner, "_gh_sweep", return_value=("ok", "")),
            patch.object(runner, "_memory_triage", return_value=("ok", "")),
            patch.object(runner, "_collect_metrics", return_value=("ok", "")),
            patch.object(runner, "_generate_report_pass1", return_value=("ok", "")),
            patch.object(runner, "_run_doctor", return_value=("ok", "")),
            patch.object(runner, "_update_report_pass2", return_value=("ok", "")),
            patch.object(runner, method_name, return_value=failure),
            patch.object(runner, "_commit") as commit,
        ):
            self.assertEqual(runner.run(), 1)
        commit.assert_not_called()
        return runner

    def test_metrics_failure_aborts_before_commit(self) -> None:
        runner = self._assert_required_step_failure_blocks_commit(
            "_collect_metrics", ("", "pending projection corrupt")
        )
        self.assertIn("metrics failed", runner.report.abort_reason)

    def test_report_pass_two_failure_aborts_before_commit(self) -> None:
        runner = self._assert_required_step_failure_blocks_commit(
            "_update_report_pass2", ("", "injected report failure")
        )
        self.assertIn("report pass 2 failed", runner.report.abort_reason)

    def test_report_pass_two_truncates_metrics_to_fit_hard_budget(self) -> None:
        runner = self._make_runner(scheduled=True)
        runner.report.metrics["step_durations"] = {
            f"verbose-step-{index:04d}": 1.0 for index in range(800)
        }

        output, error = runner._update_report_pass2()

        self.assertEqual(error, "")
        self.assertIn("report pass 2 written", output)
        self.assertTrue(runner.report.metrics["report_truncated"])
        self.assertNotIn("step_durations", runner.report.metrics)
        self.assertLessEqual(
            runner._estimate_report_tokens(), wiki_night.NIGHT_REPORT_HARD_TOKENS
        )

    def test_report_pass_two_rejects_report_still_over_hard_budget(self) -> None:
        runner = self._make_runner(scheduled=True)
        runner.report.doctor_result = "x" * (wiki_night.NIGHT_REPORT_HARD_TOKENS * 8)

        output, error = runner._update_report_pass2()

        self.assertEqual(output, "")
        self.assertIn("exceeds hard token budget", error)
        self.assertGreater(
            runner._estimate_report_tokens(), wiki_night.NIGHT_REPORT_HARD_TOKENS
        )

    def test_sweep_findings_truncated_when_over_char_budget(self) -> None:
        runner = self._make_runner(scheduled=True)
        tier1_entries = "\n".join(
            f"  - acme/widget#{n} [pr/MERGED] ws.md (Next, repo_source=default): "
            f"{'x' * 120}"
            for n in range(50)
        )
        runner.report.sweep_findings = (
            "Staleness sweep: 100 refs checked.\n\n"
            "TIER 1 — frontmatter/blocker refs now dead: 50\n"
            f"{tier1_entries}\n\n"
            "TIER 2 — curated-text refs now dead: 0\n"
        )

        truncated = runner._truncate_sweep_findings()

        self.assertLessEqual(
            len(truncated), wiki_night.SWEEP_FINDINGS_CHAR_BUDGET + 200
        )
        self.assertIn("TIER 1", truncated)
        self.assertIn("... and ", truncated)
        self.assertIn("gh-sweep-", truncated)
        # The full-sweep pointer names the configured report dir, not a
        # built-in convention.
        self.assertIn(self.config.night.report_dir, truncated)

    def test_sweep_findings_not_truncated_when_under_budget(self) -> None:
        runner = self._make_runner(scheduled=True)
        runner.report.sweep_findings = "Staleness sweep: 5 refs checked.\nAll clean."

        result = runner._truncate_sweep_findings()

        self.assertEqual(result, runner.report.sweep_findings)

    def test_report_pass_one_failure_aborts_before_commit(self) -> None:
        runner = self._assert_required_step_failure_blocks_commit(
            "_generate_report_pass1", ("", "injected report failure")
        )
        self.assertIn("report pass 1 failed", runner.report.abort_reason)

    def test_doctor_stderr_only_failure(self) -> None:
        runner = self._make_runner()
        doctor_result = MagicMock(returncode=1, stdout="", stderr="some error")
        with patch("subprocess.run", return_value=doctor_result):
            output, error = runner._run_doctor()
        self.assertNotEqual(error, "")
        self.assertIn("some error", error)
        self.assertFalse(runner.report.doctor_clean)

    def test_count_pending_uses_correct_dir(self) -> None:
        runner = self._make_runner()
        with patch.object(
            wiki_night,
            "load_verified_pending_index",
            return_value={"event_count": 2, "events": []},
        ) as load:
            count = runner._count_pending()
        self.assertEqual(count, 2)
        load.assert_called_once_with(self.root / "wiki" / "events")

    def test_apply_pending_missing_event_path_key(self) -> None:
        runner = self._make_runner()
        with (
            patch.object(
                wiki_night,
                "load_verified_pending_index",
                return_value={"event_count": 1, "events": [{"event_id": "bad"}]},
            ),
            self.assertRaises(KeyError),
        ):
            runner._apply_pending()

    def test_crash_still_writes_report(self) -> None:
        runner = self._make_runner()
        with patch.object(runner, "_run_full", side_effect=RuntimeError("boom")):
            exit_code = runner.run()
        self.assertEqual(exit_code, 1)
        self.assertIn("unhandled exception", runner.report.abort_reason)
        self.assertIn("boom", runner.report.abort_reason)
        self.assertTrue(runner.report_path.exists())
        text = runner.report_path.read_text()
        self.assertIn("ABORTED", text)
        self.assertIn("boom", text)

    def test_success_does_not_rewrite_report_after_full_run(self) -> None:
        runner = self._make_runner()
        with (
            patch.object(runner, "_run_full", return_value=0),
            patch.object(runner, "_write_report") as write_report,
        ):
            self.assertEqual(runner.run(), 0)
        write_report.assert_not_called()

    def test_token_verification_requires_ownership(self) -> None:
        runner = self._make_runner()
        runner._lock_token = "ours"
        lock_path = self.root / "workstreams" / ".garden.lock"
        lock_path.write_text("token=theirs\n")
        _output, error = runner._verify_lock()
        self.assertIn("no longer belongs", error)

    # --- Config pass-through: every child process receives the resolved
    # --- wiki root explicitly; no child inherits a default root.

    def test_doctor_child_gets_explicit_wiki_flag(self) -> None:
        runner = self._make_runner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            runner._run_doctor()
        cmd = mock_run.call_args.args[0]
        self.assertEqual(
            cmd,
            [
                sys.executable,
                str(SCRIPTS_DIR / "wiki_doctor.py"),
                "--wiki",
                str(self.root),
            ],
        )
        self.assertEqual(mock_run.call_args.kwargs.get("cwd"), self.root)

    def test_memory_triage_child_gets_explicit_wiki_flag(self) -> None:
        runner = self._make_runner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            runner._memory_triage()
        cmd = mock_run.call_args.args[0]
        self.assertEqual(
            cmd,
            [
                sys.executable,
                str(SCRIPTS_DIR / "wiki_memory_triage.py"),
                "since-garden",
                "--wiki",
                str(self.root),
            ],
        )

    def test_render_children_get_explicit_wiki_flag(self) -> None:
        runner = self._make_runner()
        (self.root / "wiki" / "log.md").write_text("log\n")
        (self.root / "CLAUDE.local.md").write_text("index\n")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            runner._render_log()
            runner._render_claude_local()
        commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(
            commands[0],
            [
                sys.executable,
                str(SCRIPTS_DIR / "wiki_render.py"),
                "log",
                "--wiki",
                str(self.root),
            ],
        )
        self.assertEqual(
            commands[1],
            [
                sys.executable,
                str(SCRIPTS_DIR / "wiki_render.py"),
                "claude-local",
                "--no-lock",
                "--wiki",
                str(self.root),
            ],
        )

    def test_gh_sweep_child_gets_explicit_wiki_flag(self) -> None:
        runner = self._make_runner()
        with (
            patch("subprocess.run") as mock_run,
            patch.object(wiki_night, "render_summary", return_value="summary"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
            _output, error = runner._gh_sweep()
        self.assertEqual(error, "")
        cmd = mock_run.call_args.args[0]
        self.assertEqual(
            cmd,
            [
                sys.executable,
                str(SCRIPTS_DIR / "wiki_gh_sweep.py"),
                "--json",
                "--wiki",
                str(self.root),
            ],
        )

    def test_rebuild_pending_child_gets_explicit_events_dir_and_wiki(self) -> None:
        (self.root / "wiki" / "pending" / "index.json").write_text("{}\n")
        (self.root / "wiki" / "pending" / "latest.md").write_text("pending\n")
        runner = self._make_runner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            runner._rebuild_pending()
        cmd = mock_run.call_args.args[0]
        self.assertEqual(
            cmd,
            [
                sys.executable,
                str(SCRIPTS_DIR / "wiki_event.py"),
                "build-pending",
                "--events-dir",
                str(self.root / "wiki" / "events"),
                "--wiki",
                str(self.root),
            ],
        )

    def test_lock_children_get_explicit_wiki_flag(self) -> None:
        runner = self._make_runner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="locked token=abc", stderr=""
            )
            runner._acquire_lock()
            runner._release_lock()
        acquire_cmd = mock_run.call_args_list[0].args[0]
        release_cmd = mock_run.call_args_list[1].args[0]
        self.assertEqual(
            acquire_cmd,
            [
                sys.executable,
                str(SCRIPTS_DIR / "garden-lock.py"),
                "acquire",
                "--wiki",
                str(self.root),
            ],
        )
        self.assertEqual(
            release_cmd,
            [
                sys.executable,
                str(SCRIPTS_DIR / "garden-lock.py"),
                "release",
                "abc",
                "--wiki",
                str(self.root),
            ],
        )

    def test_scheduled_commit_subject_uses_config_prefix(self) -> None:
        self._init_git_repo()
        runner = self._make_runner(scheduled=True)
        runner.report.outcome = wiki_night.RunOutcome.CLEAN
        runner._write_report()
        _output, error = runner._commit()
        self.assertEqual(error, "")
        subject = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(
            subject, f"{self.config.night.commit_prefix} {runner.report.date}"
        )

    def test_manual_commit_intent_derives_from_config_prefix(self) -> None:
        runner = self._make_runner()
        scheduled_intent, manual_intent = runner._commit_intents()
        self.assertTrue(self.config.night.commit_prefix.startswith(scheduled_intent))
        self.assertEqual(manual_intent, f"{scheduled_intent}-manual")

    def test_unexpected_change_blocks_exact_path_commit(self) -> None:
        runner = self._make_runner(scheduled=True)
        runner._write_report()
        with patch.object(runner, "_unexpected_changes", return_value=["docs/x.md"]):
            output, error = runner._commit()
        self.assertEqual(output, "")
        self.assertIn("docs/x.md", error)

    def test_report_change_before_commit_is_not_adopted(self) -> None:
        self._init_git_repo()
        runner = self._make_runner(scheduled=True)
        runner.report.outcome = wiki_night.RunOutcome.CLEAN
        runner._write_report()
        runner.report_path.write_text("concurrent report bytes\n")
        os.chmod(runner.report_path, 0o755)

        output, error = runner._commit()

        self.assertEqual(output, "")
        self.assertIn(str(runner.report_path.relative_to(self.root)), error)
        self.assertIn("unexpected concurrent changes", error)
        committed = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(committed, "baseline")

    def test_same_touched_path_writer_is_detected(self) -> None:
        self._init_git_repo()
        runner = self._make_runner()
        path = self.root / "workstreams" / "wiki-system.md"
        path.write_text("runner output\n")
        runner._touch(path)
        path.write_text("concurrent writer\n")
        self.assertEqual(runner._unexpected_changes(), ["workstreams/wiki-system.md"])

    def test_chmod_of_runner_owned_file_is_detected(self) -> None:
        self._init_git_repo()
        runner = self._make_runner()
        path = self.root / "marker.txt"
        runner._touch(path)
        os.chmod(path, 0o755)

        self.assertEqual(runner._unexpected_changes(), ["marker.txt"])

    def test_regular_file_replaced_by_same_byte_symlink_is_detected(self) -> None:
        self._init_git_repo()
        runner = self._make_runner(scheduled=True)
        runner._write_report()
        relative = runner.report_path.relative_to(self.root)
        subprocess.run(
            ["git", "add", "--force", "--", relative], cwd=self.root, check=True
        )
        target = self.root.parent / "same-report-bytes.md"
        target.write_bytes(runner.report_path.read_bytes())
        runner.report_path.unlink()
        runner.report_path.symlink_to(target)

        with self.assertRaisesRegex(RuntimeError, "changed concurrently"):
            runner._verify_staged_manifest()

    def test_staged_mode_change_is_detected_after_worktree_mode_is_restored(
        self,
    ) -> None:
        self._init_git_repo()
        runner = self._make_runner(scheduled=True)
        runner._write_report()
        relative = runner.report_path.relative_to(self.root)
        os.chmod(runner.report_path, 0o755)
        subprocess.run(
            ["git", "add", "--force", "--", relative], cwd=self.root, check=True
        )
        os.chmod(runner.report_path, 0o644)

        with self.assertRaisesRegex(RuntimeError, "content or mode"):
            runner._verify_staged_manifest()

    def test_same_path_writer_before_runner_write_breaks_baseline(self) -> None:
        self._init_git_repo()
        path = self.root / "marker.txt"
        runner = self._make_runner()
        runner._capture_baseline()
        path.write_text("concurrent writer\n")
        with self.assertRaisesRegex(RuntimeError, "runner baseline"):
            runner._assert_unchanged_before_write(path)

    def test_capture_baseline_ignores_gitlink(self) -> None:
        self._init_git_repo()
        gitlink = self.root / "nested-reports"
        gitlink.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=gitlink, check=True)
        subprocess.run(
            ["git", "config", "user.email", "night-test@example.com"],
            cwd=gitlink,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Night Test"], cwd=gitlink, check=True
        )
        (gitlink / "README.md").write_text("nested repository\n")
        subprocess.run(["git", "add", "README.md"], cwd=gitlink, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "nested baseline"],
            cwd=gitlink,
            check=True,
        )
        gitlink_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=gitlink,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            [
                "git",
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{gitlink_head},nested-reports",
            ],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "track nested repository"],
            cwd=self.root,
            check=True,
        )

        runner = self._make_runner()
        runner._capture_baseline()

        self.assertIn(Path("marker.txt"), runner._baseline_fingerprints)
        self.assertNotIn(Path("nested-reports"), runner._baseline_fingerprints)
        (gitlink / "README.md").write_text("nested revision\n")
        subprocess.run(["git", "add", "README.md"], cwd=gitlink, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "nested revision"],
            cwd=gitlink,
            check=True,
        )

        self.assertEqual(runner._status_paths(), {Path("nested-reports")})
        self.assertEqual(runner._unexpected_changes(), ["nested-reports"])

    def test_preflight_owned_only_dirt_proceeds(self) -> None:
        """Friction A, owned-only case: a dirty CLAUDE.local.md (a
        GENERATED_INGRESS path) does not abort the run."""
        self._init_git_repo()
        (self.root / "CLAUDE.local.md").write_text("stale index\n")
        runner = self._make_runner()
        output, error = runner._preflight()
        self.assertEqual(error, "")
        self.assertIn("1 owned", output)
        self.assertEqual(runner._foreign_dirty_at_start, set())

    def test_preflight_mixed_dirt_proceeds_and_records_foreign(self) -> None:
        """Friction A, mixed case: a real untracked pending handoff event
        alongside a foreign dirty workstream file proceeds, and the foreign
        path is recorded so it is never touched or staged."""
        self._init_git_repo()
        event_path = _write_real_pending_handoff(self.root / "wiki" / "events")
        (self.root / "workstreams" / "widget-roadmap.md").write_text("mid-edit\n")
        runner = self._make_runner()
        output, error = runner._preflight()
        self.assertEqual(error, "")
        self.assertIn("leaving 1 foreign", output)
        self.assertEqual(
            runner._foreign_dirty_at_start,
            {Path("workstreams/widget-roadmap.md")},
        )
        self.assertNotIn(
            event_path.relative_to(self.root), runner._foreign_dirty_at_start
        )

    def test_preflight_foreign_only_dirt_aborts(self) -> None:
        """Friction A, foreign-only case: unrelated mid-edit dirt with no
        owned paths still aborts, exactly like the pre-Friction-A behavior."""
        self._init_git_repo()
        (self.root / "workstreams" / "widget-roadmap.md").write_text("mid-edit\n")
        runner = self._make_runner()
        output, error = runner._preflight()
        self.assertIn("no owned paths", error)
        self.assertEqual(output, "")

    def test_preflight_rejects_invalid_untracked_event_as_foreign(self) -> None:
        """An untracked wiki/events/**/*.json file that does not validate as
        a pending handoff (e.g. malformed, or already applied) is foreign,
        not owned - it must not be silently swept into the run."""
        self._init_git_repo()
        bad_event_dir = self.root / "wiki" / "events" / "2026" / "07"
        bad_event_dir.mkdir(parents=True)
        (bad_event_dir / "not-an-event.json").write_text("{}\n")
        runner = self._make_runner()
        output, error = runner._preflight()
        self.assertIn("no owned paths", error)
        self.assertEqual(output, "")

    def test_unexpected_changes_excludes_foreign_dirty_at_start(self) -> None:
        self._init_git_repo()
        (self.root / "workstreams" / "widget-roadmap.md").write_text("mid-edit\n")
        runner = self._make_runner()
        runner._foreign_dirty_at_start = {Path("workstreams/widget-roadmap.md")}
        self.assertEqual(runner._unexpected_changes(), [])

    def _init_git_repo(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "night-test@example.com"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Night Test"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "core.filemode", "true"], cwd=self.root, check=True
        )
        marker = self.root / "marker.txt"
        marker.write_text("baseline\n")
        # wiki.toml is the committed half of the config pair; a real
        # deployment tracks it, so the baseline commit includes it.
        subprocess.run(
            ["git", "add", "marker.txt", WIKI_CONFIG_FILE], cwd=self.root, check=True
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "baseline"], cwd=self.root, check=True
        )

    def test_commit_failure_restages_aborted_report(self) -> None:
        self._init_git_repo()
        runner = self._make_runner(scheduled=True)
        runner.report.outcome = wiki_night.RunOutcome.CLEAN
        runner._write_report()
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)

        _output, error = runner._commit()
        self.assertIn("commit failed", error)
        runner.report.outcome = wiki_night.RunOutcome.ABORTED
        runner.report.abort_reason = error
        runner._write_report()
        runner._recover_failed_commit_report()

        relative = runner.report_path.relative_to(self.root)
        staged = subprocess.run(
            ["git", "show", f":{relative}"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("**Outcome:** aborted", staged)
        self.assertEqual(staged, runner.report_path.read_text())

    def test_run_preserves_report_changed_by_failing_commit_hook(self) -> None:
        self._init_git_repo()
        runner = self._make_runner(scheduled=True)
        runner.report.outcome = wiki_night.RunOutcome.CLEAN
        relative = runner.report_path.relative_to(self.root)
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.write_text(
            f"#!/bin/sh\nprintf 'hook-owned report\\n' > '{relative}'\nexit 1\n"
        )
        os.chmod(hook, 0o755)

        def fail_during_commit() -> int:
            runner._write_report()
            _output, error = runner._commit()
            runner.report.abort_reason = f"commit failed: {error}"
            return 1

        with patch.object(runner, "_run_full", side_effect=fail_during_commit):
            self.assertEqual(runner.run(), 1)

        staged = subprocess.run(
            ["git", "show", f":{relative}"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        preserved = list(runner.report_path.parent.glob("*-commit-conflict-*.md"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_text(), "hook-owned report\n")
        self.assertIn("concurrent report state preserved", staged)
        self.assertIn("**Outcome:** aborted", staged)
        self.assertNotIn("**Outcome:** clean", staged)
        self.assertEqual(staged, runner.report_path.read_text())

    def test_post_add_same_path_change_fails_manifest_verification(self) -> None:
        self._init_git_repo()
        runner = self._make_runner(scheduled=True)
        runner._write_report()
        runner._touch(runner.report_path)
        relative = runner.report_path.relative_to(self.root)
        subprocess.run(
            ["git", "add", "--force", "--", relative], cwd=self.root, check=True
        )
        runner.report_path.write_text("concurrent writer\n")
        with self.assertRaisesRegex(RuntimeError, "changed concurrently"):
            runner._verify_staged_manifest()

    def test_post_commit_writer_fails_clean_verification(self) -> None:
        self._init_git_repo()
        runner = self._make_runner(scheduled=True)
        runner.report.outcome = wiki_night.RunOutcome.CLEAN
        runner._write_report()
        hook = self.root / ".git" / "hooks" / "post-commit"
        relative = runner.report_path.relative_to(self.root)
        hook.write_text(f"#!/bin/sh\nprintf 'writer\\n' >> '{relative}'\n")
        os.chmod(hook, 0o755)

        _output, error = runner._commit()
        self.assertIn("commit created but verification failed", error)
        self.assertIn("working tree is not clean", error)

    def test_pre_commit_hook_cannot_expand_committed_manifest(self) -> None:
        self._init_git_repo()
        runner = self._make_runner(scheduled=True)
        runner.report.outcome = wiki_night.RunOutcome.CLEAN
        runner._write_report()
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.write_text(
            "#!/bin/sh\nprintf 'not runner owned\\n' > rogue.txt\ngit add rogue.txt\n"
        )
        os.chmod(hook, 0o755)

        _output, error = runner._commit()
        self.assertIn("unverified commit rolled back", error)
        self.assertIn("committed path manifest differs", error)
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(head_after, head_before)
        tracked_rogue = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "rogue.txt"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(tracked_rogue.returncode, 0)
        staged_paths = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertEqual(staged_paths, [str(runner.report_path.relative_to(self.root))])

    def test_pre_commit_hook_mode_change_is_rolled_back(self) -> None:
        self._init_git_repo()
        runner = self._make_runner(scheduled=True)
        runner.report.outcome = wiki_night.RunOutcome.CLEAN
        runner._write_report()
        relative = runner.report_path.relative_to(self.root)
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\nchmod +x '{relative}'\ngit add '{relative}'\n")
        os.chmod(hook, 0o755)

        _output, error = runner._commit()

        self.assertIn("unverified commit rolled back", error)
        self.assertIn("content or mode differs", error)
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(head_after, head_before)
        staged_mode = subprocess.run(
            ["git", "ls-files", "--stage", "--", relative],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()[0]
        self.assertEqual(staged_mode, "100644")


if __name__ == "__main__":
    unittest.main()
