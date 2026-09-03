#!/usr/bin/env python3
"""Night-shift T0 runner: mechanical nightly pipeline.

Orchestrates the kit's scripts in the order defined by the night-shift
operating model's T0 pipeline. No LLM runs. The action set is: lock,
apply pending events, render projections, gh-sweep, memory-triage,
collect metrics, generate morning report, doctor, commit. All on
existing scripts.

The runner aborts before commit if doctor is not clean. It stages only
the paths it touched (explicit pathspec). A clean-worktree precondition
is enforced before the run starts.

The wiki root comes from --wiki (or the walk-up resolution); report
paths and the commit-message prefix come from the deployment's
wiki.toml [night] table. Every child process receives the resolved
root explicitly - no child ever falls back to a default root of its
own.

Usage (from the kit checkout, against a wiki repo):
  uv run --project /path/to/wiki-kit scripts/wiki_night.py run --wiki /path/to/wiki
  ... run --wiki ... --dry-run       # execute steps but skip commit
  ... run --wiki ... --report-only   # generate report without applying or committing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from wiki_checkpoint import (  # noqa: E402
    GENERATED_INGRESS,
    CheckpointError,
    validate_canonical_event,
)
from wiki_config import (  # noqa: E402
    ConfigError,
    WikiConfig,
    load_config,
    resolve_wiki_root,
)
from wiki_event import (  # noqa: E402
    EventType,
    HandoffStatus,
    default_pending_dir,
    load_verified_pending_index,
    set_wiki_root,
    utc_timestamp,
)
from wiki_garden import (  # noqa: E402
    AlreadyDispositioned,
    DurableApplyNeedsRepair,
    ManualApplyRequired,
    apply_event,
)
from wiki_gh_sweep import render_summary  # noqa: E402
from wiki_render import NIGHT_REPORT_HARD_TOKENS  # noqa: E402

SWEEP_FINDINGS_TOKEN_BUDGET = 1500
SWEEP_FINDINGS_CHAR_BUDGET = SWEEP_FINDINGS_TOKEN_BUDGET * 4


@dataclass
class StepResult:
    name: str
    success: bool
    duration_seconds: float
    output: str = ""
    error: str = ""


class RunOutcome(StrEnum):
    CLEAN = "clean"
    ATTENTION = "attention"
    ABORTED = "aborted"


@dataclass(frozen=True)
class AppliedReportEvent:
    event_id: str
    summary: str
    workstream: str


@dataclass(frozen=True)
class AttentionEvent:
    event_id: str
    reason: str
    action: str


@dataclass(frozen=True)
class FileFingerprint:
    content_sha256: str
    git_mode: str


@dataclass
class NightReport:
    date: str
    mode: str
    outcome: RunOutcome = RunOutcome.ABORTED
    applied_events: list[AppliedReportEvent] = field(default_factory=list)
    manual_actions: list[AttentionEvent] = field(default_factory=list)
    reconciled_events: list[AttentionEvent] = field(default_factory=list)
    sweep_findings: str = ""
    triage_findings: str = ""
    doctor_result: str = ""
    doctor_clean: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)
    steps: list[StepResult] = field(default_factory=list)
    lock_token: str | None = None
    abort_reason: str = ""


class NightRunner:
    def __init__(
        self,
        config: WikiConfig,
        dry_run: bool = False,
        report_only: bool = False,
        scheduled: bool = False,
    ) -> None:
        self.config = config
        self.root = config.root
        self.dry_run = dry_run
        self.report_only = report_only
        self.scheduled = scheduled
        now = datetime.now(UTC)
        mode = (
            "report-only"
            if report_only
            else "dry-run"
            if dry_run
            else "scheduled"
            if scheduled
            else "manual"
        )
        self._run_stamp = now.strftime("%Y-%m-%dT%H%M%SZ")
        self.report = NightReport(date=now.strftime("%Y-%m-%d"), mode=mode)
        self._lock_token: str | None = None
        self._touched_paths: set[Path] = set()
        self._expected_fingerprints: dict[Path, FileFingerprint] = {}
        self._baseline_fingerprints: dict[Path, FileFingerprint] | None = None
        self._foreign_dirty_at_start: set[Path] = set()
        self._commit_staged = False

    @property
    def report_path(self) -> Path:
        reports_dir = self.root / self.config.night.report_dir
        if self.scheduled and not self.dry_run and not self.report_only:
            return reports_dir / f"{self.report.date}.md"
        file_name = f"{self.report.date}-{self.report.mode}-{self._run_stamp}.md"
        return reports_dir / file_name

    def _kit_cli(self, script_name: str, *args: str) -> list[str]:
        """Command line for a sibling kit script. Machinery resolves from
        the kit's own scripts dir; the wiki root is passed explicitly so
        no child ever inherits a default root (config pass-through)."""
        return [
            sys.executable,
            str(SCRIPT_DIR / script_name),
            *args,
            "--wiki",
            str(self.root),
        ]

    def _commit_intents(self) -> tuple[str, str]:
        """The scheduled commit subject is `<prefix> <date>`; the manual
        subject derives from the same configured [night].commit_prefix so
        one key governs both."""
        scheduled = self.config.night.commit_prefix.removesuffix(":")
        return scheduled, f"{scheduled}-manual"

    def run(self) -> int:
        if self.report_only:
            return self._run_report_only()

        try:
            exit_code = self._run_full()
        except Exception as exc:
            self.report.abort_reason = f"unhandled exception: {exc}"
            exit_code = 1
        if exit_code != 0:
            self.report.outcome = RunOutcome.ABORTED
            if self._commit_staged:
                self._write_failed_commit_report()
            else:
                self._write_report()
            self._recover_failed_commit_report()
        return exit_code

    def _run_report_only(self) -> int:
        try:
            pending = self._count_pending()
            self.report.metrics["pending_count"] = pending
            self.report.outcome = RunOutcome.CLEAN
            self._write_report()
            return 0
        except Exception as exc:
            self.report.abort_reason = f"report-only failed: {exc}"
            self.report.outcome = RunOutcome.ABORTED
            self._write_report()
            return 1

    def _run_full(self) -> int:
        # Step 0: pre-flight
        result = self._step("pre-flight", self._preflight)
        if not result.success:
            self.report.abort_reason = result.error
            return 1

        # Step 1: acquire lock
        result = self._step("acquire-lock", self._acquire_lock)
        if not result.success:
            self.report.abort_reason = result.error
            return 1

        try:
            # Step 2: mechanical apply
            self.report.metrics["pending_count_before"] = self._count_pending()
            result = self._step("apply-pending", self._apply_pending)
            if not result.success:
                self.report.abort_reason = f"apply failed: {result.error}"
                return 1

            # Step 3: render projections
            for render_name, render_fn in [
                ("render-log", self._render_log),
                ("render-claude-local", self._render_claude_local),
            ]:
                result = self._step(render_name, render_fn)
                if not result.success:
                    self.report.abort_reason = f"{render_name} failed: {result.error}"
                    return 1

            # Step 4: gh sweep (non-fatal: report but continue)
            self._step("gh-sweep", self._gh_sweep)

            # Step 5: memory triage (non-fatal: report but continue)
            self._step("memory-triage", self._memory_triage)

            # Step 6: collect metrics
            result = self._step("metrics", self._collect_metrics)
            if not result.success:
                self.report.abort_reason = f"metrics failed: {result.error}"
                return 1

            # Step 7: generate morning report (pass 1)
            result = self._step("report-pass-1", self._generate_report_pass1)
            if not result.success:
                self.report.abort_reason = f"report pass 1 failed: {result.error}"
                return 1

            # Step 8: doctor
            result = self._step("doctor", self._run_doctor)
            if not result.success:
                self.report.abort_reason = f"doctor failed: {result.error}"
                return 1

            # Step 9: update report (pass 2)
            self.report.outcome = (
                RunOutcome.ATTENTION if self.report.manual_actions else RunOutcome.CLEAN
            )
            result = self._step("report-pass-2", self._update_report_pass2)
            if not result.success:
                self.report.abort_reason = f"report pass 2 failed: {result.error}"
                return 1

            # Step 10: abort check
            if not self.report.doctor_clean:
                self.report.abort_reason = "doctor not clean"
                return 1

            # Step 11: lock verification
            result = self._step("lock-verify", self._verify_lock)
            if not result.success:
                self.report.abort_reason = f"lock lost: {result.error}"
                return 1

            # Step 12: commit
            if not self.dry_run:
                result = self._step("commit", self._commit)
                if not result.success:
                    self.report.abort_reason = f"commit failed: {result.error}"
                    return 1
            else:
                self._write_report()

            return 0
        finally:
            self._release_lock()

    def _step(self, name: str, fn: Any) -> StepResult:
        start = time.monotonic()
        try:
            output, error = fn()
            duration = time.monotonic() - start
            result = StepResult(
                name=name,
                success=not error,
                duration_seconds=duration,
                output=output,
                error=error,
            )
        except Exception as exc:
            duration = time.monotonic() - start
            result = StepResult(
                name=name,
                success=False,
                duration_seconds=duration,
                error=str(exc),
            )
        self.report.steps.append(result)
        status = "OK" if result.success else "FAIL"
        print(f"{status} {name} ({result.duration_seconds:.1f}s)")
        if result.error:
            print(f"  {result.error}", file=sys.stderr)
        return result

    def _is_owned_dirty_path(self, rel: str) -> bool:
        """Friction A: a dirty path is owned if it is one of the four
        regenerable projections, or an untracked wiki/events/**/*.json file
        that validates as a pending handoff. Everything else (including a
        garden-apply disposition event, which the runner only ever writes
        itself mid-run, never finds pre-existing) is foreign."""
        if rel in GENERATED_INGRESS:
            return True
        if rel.startswith("wiki/events/"):
            try:
                event = validate_canonical_event(self.root, rel)
            except CheckpointError:
                return False
            return (
                event.get("event_type") == EventType.HANDOFF
                and event.get("status") == HandoffStatus.PENDING_GARDEN
            )
        return False

    def _partition_dirty(self, paths: set[Path]) -> tuple[list[Path], list[Path]]:
        owned: list[Path] = []
        foreign: list[Path] = []
        for path in sorted(paths):
            (owned if self._is_owned_dirty_path(str(path)) else foreign).append(path)
        return owned, foreign

    def _preflight(self) -> tuple[str, str]:
        dirty = self._status_paths()
        if dirty:
            owned, foreign = self._partition_dirty(dirty)
            if not owned:
                listing = "\n".join(sorted(str(p) for p in dirty))
                return "", f"working tree is dirty (no owned paths):\n{listing}"
            self._foreign_dirty_at_start = set(foreign)
            note = f"proceeding on {len(owned)} owned dirty path(s)"
            if foreign:
                note += f"; leaving {len(foreign)} foreign path(s) untouched"
        else:
            self._foreign_dirty_at_start = set()
            note = "clean working tree"
        lock_check = subprocess.run(
            self._kit_cli("garden-lock.py", "check"),
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        if lock_check.returncode != 0:
            return "", f"garden lock is held: {lock_check.stdout.strip()}"
        try:
            self._capture_baseline()
        except RuntimeError as exc:
            return "", str(exc)
        return f"{note}, lock available", ""

    def _acquire_lock(self) -> tuple[str, str]:
        result = subprocess.run(
            self._kit_cli("garden-lock.py", "acquire"),
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        if result.returncode != 0:
            return "", f"could not acquire lock: {result.stderr.strip()}"
        token_line = result.stdout.strip()
        if "token=" in token_line:
            self._lock_token = token_line.split("token=")[1].strip()
            self.report.lock_token = self._lock_token
        return token_line, ""

    def _release_lock(self) -> None:
        if self._lock_token:
            subprocess.run(
                self._kit_cli("garden-lock.py", "release", self._lock_token),
                capture_output=True,
                text=True,
                cwd=self.root,
            )
            self._lock_token = None

    def _apply_pending(self) -> tuple[str, str]:
        events_dir = self.root / "wiki" / "events"
        index_data = load_verified_pending_index(events_dir)
        pending_events = index_data["events"]
        if not pending_events:
            return "no pending events", ""

        reconcile_pending = False
        for entry in pending_events:
            event_path = self.root / entry["event_path"]
            event = json.loads(event_path.read_text())
            if event["event_id"] != entry["event_id"]:
                raise ValueError(
                    f"pending entry {entry['event_id']} points to event "
                    f"{event['event_id']}"
                )
            try:
                proposed = event.get("proposed_workstreams", [])
                targets = [
                    item["name"]
                    for item in proposed
                    if item.get("relationship") in {"primary", "candidate_new"}
                ]
                target_paths = [
                    self.root / "workstreams" / f"{name}.md" for name in targets
                ]
                foreign_targets = [
                    p
                    for p in target_paths
                    if self._relative_path(p) in self._foreign_dirty_at_start
                ]
                if foreign_targets:
                    names = ", ".join(
                        str(self._relative_path(p)) for p in foreign_targets
                    )
                    self.report.manual_actions.append(
                        AttentionEvent(
                            event_id=event["event_id"],
                            reason=(
                                "target workstream has foreign dirt from "
                                f"before this run started: {names}"
                            ),
                            action=(
                                "Run /garden once the concurrent edit is "
                                "committed or reverted; leave pending until "
                                "then."
                            ),
                        )
                    )
                    continue
                pending_dir = default_pending_dir(events_dir)
                before_paths = [
                    *target_paths,
                    pending_dir / "index.json",
                    pending_dir / "latest.md",
                ]
                self._assert_unchanged_before_write(*before_paths)
                applied = apply_event(
                    event,
                    repo_root=self.root,
                    events_dir=events_dir,
                )
                self.report.applied_events.append(
                    AppliedReportEvent(
                        event_id=applied.event_id,
                        summary=event["summary"],
                        workstream=applied.workstream,
                    )
                )
                self._touch(
                    applied.workstream_path, applied.garden_event_path, event_path
                )
                self._touch_pending()
            except ManualApplyRequired as exc:
                self.report.manual_actions.append(
                    AttentionEvent(
                        event_id=event["event_id"],
                        reason=str(exc),
                        action=(
                            "Run /garden; leave pending until manually applied "
                            "or rejected."
                        ),
                    )
                )
            except AlreadyDispositioned as exc:
                self.report.reconciled_events.append(
                    AttentionEvent(
                        event_id=event["event_id"],
                        reason=str(exc),
                        action="Pending projection will be rebuilt; do not re-apply.",
                    )
                )
                reconcile_pending = True
            except DurableApplyNeedsRepair as exc:
                self._touch(exc.workstream_path, exc.garden_event_path)
                raise

        if reconcile_pending:
            self._rebuild_pending()
        return (
            f"applied {len(self.report.applied_events)}, "
            f"manual {len(self.report.manual_actions)}, "
            f"reconciled {len(self.report.reconciled_events)}",
            "",
        )

    def _rebuild_pending(self) -> None:
        events_dir = self.root / "wiki" / "events"
        pending_dir = default_pending_dir(events_dir)
        self._assert_unchanged_before_write(
            pending_dir / "index.json", pending_dir / "latest.md"
        )
        result = subprocess.run(
            self._kit_cli(
                "wiki_event.py", "build-pending", "--events-dir", str(events_dir)
            ),
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        self._touch_pending()

    def _render_log(self) -> tuple[str, str]:
        self._assert_unchanged_before_write(self.root / "wiki" / "log.md")
        result = subprocess.run(
            self._kit_cli("wiki_render.py", "log"),
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        if result.returncode != 0:
            return "", f"render log failed: {result.stderr.strip()}"
        self._touch(self.root / "wiki" / "log.md")
        return "wiki/log.md rendered", ""

    def _render_claude_local(self) -> tuple[str, str]:
        self._assert_unchanged_before_write(self.root / "CLAUDE.local.md")
        result = subprocess.run(
            self._kit_cli("wiki_render.py", "claude-local", "--no-lock"),
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        if result.returncode != 0:
            return "", f"render claude-local failed: {result.stderr.strip()}"
        self._touch(self.root / "CLAUDE.local.md")
        return result.stdout.strip(), ""

    def _gh_sweep(self) -> tuple[str, str]:
        result = subprocess.run(
            self._kit_cli("wiki_gh_sweep.py", "--json"),
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        if result.returncode != 0:
            self.report.sweep_findings = result.stdout.strip() or "_(sweep failed)_"
            return "", f"gh-sweep failed: {result.stderr.strip()}"
        sweep_data = json.loads(result.stdout)
        json_path = self.report_path.parent / f"gh-sweep-{self.report.date}.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(sweep_data, indent=2) + "\n", encoding="utf-8")
        full_text = render_summary(sweep_data)
        self.report.sweep_findings = full_text
        return full_text, ""

    def _memory_triage(self) -> tuple[str, str]:
        result = subprocess.run(
            self._kit_cli("wiki_memory_triage.py", "since-garden"),
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        output = result.stdout.strip()
        self.report.triage_findings = output
        if result.returncode != 0:
            return "", f"memory-triage failed: {result.stderr.strip()}"
        return output, ""

    def _collect_metrics(self) -> tuple[str, str]:
        metrics: dict[str, Any] = {"pending_count_after": self._count_pending()}
        metrics["step_durations"] = {
            s.name: s.duration_seconds for s in self.report.steps
        }
        self.report.metrics.update(metrics)
        return f"collected metrics for {len(self.report.steps)} steps", ""

    def _count_pending(self) -> int:
        data = load_verified_pending_index(self.root / "wiki" / "events")
        return data["event_count"]

    def _generate_report_pass1(self) -> tuple[str, str]:
        self._write_report()
        return "morning report pass 1 written", ""

    def _run_doctor(self) -> tuple[str, str]:
        result = subprocess.run(
            self._kit_cli("wiki_doctor.py"),
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        output = result.stdout.strip()
        stderr = result.stderr.strip()
        self.report.doctor_result = output or stderr
        self.report.doctor_clean = result.returncode == 0
        if result.returncode != 0:
            return "", output or stderr
        return output, ""

    def _update_report_pass2(self) -> tuple[str, str]:
        self._write_report()
        tokens = self._estimate_report_tokens()
        if tokens > NIGHT_REPORT_HARD_TOKENS:
            self.report.metrics["report_truncated"] = True
            self.report.metrics.pop("step_durations", None)
            self._write_report()
            tokens = self._estimate_report_tokens()
        if tokens > NIGHT_REPORT_HARD_TOKENS:
            return (
                "",
                "night report exceeds hard token budget after metrics "
                f"truncation: {tokens} > {NIGHT_REPORT_HARD_TOKENS}",
            )
        return f"report pass 2 written ({tokens} tokens est.)", ""

    def _estimate_report_tokens(self) -> int:
        if not self.report_path.exists():
            return 0
        return len(self.report_path.read_text()) // 4

    def _verify_lock(self) -> tuple[str, str]:
        if not self._lock_token:
            return "", "no lock token to verify"
        lock_path = self.root / "workstreams" / ".garden.lock"
        try:
            fields = dict(
                line.split("=", 1)
                for line in lock_path.read_text().splitlines()
                if "=" in line
            )
        except OSError as exc:
            return "", f"cannot read held lock: {exc}"
        if fields.get("token") != self._lock_token:
            return "", "garden lock token no longer belongs to this run"
        return "lock token verified", ""

    def _commit(self) -> tuple[str, str]:
        unexpected = self._unexpected_changes()
        if unexpected:
            return "", "unexpected concurrent changes:\n" + "\n".join(unexpected)

        paths = sorted(
            str(path) for path in self._touched_paths - self._machine_local_paths()
        )
        add_result = subprocess.run(
            ["git", "add", "--force", "--", *paths],
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        if add_result.returncode != 0:
            return "", f"git add failed: {add_result.stderr.strip()}"
        self._commit_staged = True

        try:
            staged_paths = self._verify_staged_manifest()
        except RuntimeError as exc:
            return "", str(exc)
        head_before = self._git_text("rev-parse", "HEAD")
        expected_tree = self._git_text("write-tree")

        _scheduled_intent, manual_intent = self._commit_intents()
        commit_msg = (
            f"{self.config.night.commit_prefix} {self.report.date}"
            if self.scheduled
            else f"{manual_intent}: {self._run_stamp}"
        )
        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        if commit_result.returncode != 0:
            return "", f"git commit failed: {commit_result.stderr.strip()}"
        committed_head = self._git_text("rev-parse", "HEAD")
        try:
            self._verify_committed_manifest(staged_paths, committed_head)
        except RuntimeError as exc:
            rollback_error = self._rollback_unverified_commit(
                head_before=head_before,
                committed_head=committed_head,
                expected_tree=expected_tree,
            )
            if rollback_error:
                return (
                    "",
                    "commit created but verification failed and safe rollback "
                    f"failed: {exc}; {rollback_error}",
                )
            return "", f"unverified commit rolled back: {exc}"
        if self._status_paths() - self._foreign_dirty_at_start:
            return (
                "",
                "commit created but verification failed: working tree is not clean",
            )
        self._commit_staged = False
        return commit_result.stdout.strip(), ""

    def _write_report(self) -> None:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_unchanged_before_write(self.report_path)
        self.report_path.write_text(self._format_report(), encoding="utf-8")
        self._touch(self.report_path)

    def _write_failed_commit_report(self) -> None:
        """Replace a staged success report without losing concurrent report state."""
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        if self._path_exists(self.report_path):
            preserved_path = self._next_commit_conflict_path()
            os.replace(self.report_path, preserved_path)
            relative = self._relative_path(self.report_path)
            expected = self._expected_fingerprints.get(relative)
            try:
                preserved = self._fingerprint(preserved_path)
            except OSError:
                preserved = None
            if preserved == expected and not preserved_path.is_dir():
                preserved_path.unlink()
            else:
                conflict_relative = self._relative_path(preserved_path)
                self.report.abort_reason += (
                    f"; concurrent report state preserved at {conflict_relative}"
                )
        self.report_path.write_text(self._format_report(), encoding="utf-8")
        self._touch(self.report_path)

    def _next_commit_conflict_path(self) -> Path:
        stem = f"{self.report_path.stem}-commit-conflict-{self._run_stamp}"
        candidate = self.report_path.with_name(f"{stem}{self.report_path.suffix}")
        sequence = 2
        while self._path_exists(candidate):
            candidate = self.report_path.with_name(
                f"{stem}-{sequence}{self.report_path.suffix}"
            )
            sequence += 1
        return candidate

    def _truncate_sweep_findings(self) -> str:
        text = self.report.sweep_findings or "_(not run)_"
        if len(text) <= SWEEP_FINDINGS_CHAR_BUDGET:
            return text
        json_path = (
            f"{self.config.night.report_dir}/gh-sweep-{self.report.date}.json"
        )
        all_lines = text.split("\n")
        kept: list[str] = []
        char_total = 0
        overflow_per_tier: dict[str, int] = {}
        current_tier = ""
        for line in all_lines:
            is_tier_header = (
                line.startswith("TIER ")
                or line.startswith("UNRESOLVABLE")
                or line.startswith("Staleness sweep:")
            )
            if is_tier_header:
                current_tier = line.split(":")[0] if ":" in line else line
                kept.append(line)
                char_total += len(line) + 1
                continue
            if line.strip() == "":
                kept.append(line)
                continue
            if char_total + len(line) + 1 <= SWEEP_FINDINGS_CHAR_BUDGET:
                kept.append(line)
                char_total += len(line) + 1
            else:
                if current_tier:
                    overflow_per_tier[current_tier] = (
                        overflow_per_tier.get(current_tier, 0) + 1
                    )
        if overflow_per_tier:
            kept.append("")
            for tier, count in overflow_per_tier.items():
                kept.append(
                    f"  ... and {count} more {tier} entries — full sweep: {json_path}"
                )
        return "\n".join(kept)

    def _format_report(self) -> str:
        lines: list[str] = []
        lines.append(f"# Night run report — {self.report.date}")
        lines.append("")
        lines.append(f"Generated: {utc_timestamp()}")
        lines.append("")
        lines.append(f"**Mode:** {self.report.mode}")
        lines.append(f"**Outcome:** {self.report.outcome.value}")
        lines.append("")

        if self.report.abort_reason:
            lines.append(f"**ABORTED:** {self.report.abort_reason}")
            lines.append("")

        lines.append("## Applied events")
        if self.report.applied_events:
            for e in self.report.applied_events:
                lines.append(f"- {e.event_id}: {e.summary} -> {e.workstream}")
        else:
            lines.append("_(none)_")
        lines.append("")

        if self.report.manual_actions:
            lines.append("## Manual action required")
            for event in self.report.manual_actions:
                lines.append(f"- {event.event_id}: {event.reason}")
                lines.append(f"  - Action: {event.action}")
            lines.append("")

        if self.report.reconciled_events:
            lines.append("## Reconciled events")
            for event in self.report.reconciled_events:
                lines.append(f"- {event.event_id}: {event.reason}")
                lines.append(f"  - Action: {event.action}")
            lines.append("")

        lines.append("## Sweep findings")
        lines.append(self._truncate_sweep_findings())
        lines.append("")

        lines.append("## Memory triage")
        lines.append(self.report.triage_findings or "_(not run)_")
        lines.append("")

        lines.append("## Doctor")
        if self.report.doctor_result:
            lines.append(f"**Clean:** {self.report.doctor_clean}")
            lines.append("```")
            lines.append(self.report.doctor_result)
            lines.append("```")
        else:
            lines.append("_(not run)_")
        lines.append("")

        lines.append("## Metrics")
        if self.report.metrics:
            before = self.report.metrics.get("pending_count_before", "n/a")
            after = self.report.metrics.get("pending_count_after", "n/a")
            lines.append(f"- Pending count (before apply): {before}")
            lines.append(f"- Pending count (after apply): {after}")
            lines.append(f"- Applied: {len(self.report.applied_events)}")
            lines.append(f"- Manual: {len(self.report.manual_actions)}")
            lines.append(f"- Reconciled: {len(self.report.reconciled_events)}")
            for name, dur in self.report.metrics.get("step_durations", {}).items():
                lines.append(f"- {name}: {dur:.1f}s")
            if self.report.metrics.get("report_truncated"):
                lines.append("- Report truncated (over budget)")
        lines.append("")

        lines.append("## Steps")
        for s in self.report.steps:
            status = "OK" if s.success else "FAIL"
            lines.append(f"- {status} {s.name} ({s.duration_seconds:.1f}s)")
            if s.error:
                lines.append(f"  - {s.error}")
        lines.append("")

        if self.dry_run or self.report_only:
            lines.append(f"**{self.report.mode}:** no commit")
        else:
            scheduled_intent, manual_intent = self._commit_intents()
            intent = scheduled_intent if self.scheduled else manual_intent
            lines.append(f"**Commit intent:** `{intent}`")

        return "\n".join(lines) + "\n"

    def _touch(self, *paths: Path) -> None:
        for path in paths:
            relative = self._relative_path(path)
            self._touched_paths.add(relative)
            self._expected_fingerprints[relative] = self._fingerprint(path)

    def _capture_baseline(self) -> None:
        paths = self._git_paths(
            "ls-files", "--cached", "--others", "--exclude-standard"
        )
        # The runner's own write set is baselined whether or not git
        # ignores it: a deployment that ignores its orientation file still
        # has the runner regenerate it, and the before-write guard needs
        # the starting fingerprint to tell a concurrent writer from the
        # file simply existing.
        paths |= {
            Path(relative)
            for relative in GENERATED_INGRESS
            if self._path_exists(self.root / relative)
        }
        gitlinks = self._gitlink_paths()
        self._baseline_fingerprints = {
            path: self._fingerprint(self.root / path)
            for path in paths
            if path not in gitlinks
        }

    def _assert_unchanged_before_write(self, *paths: Path) -> None:
        if self._baseline_fingerprints is None:
            return
        for path in paths:
            relative = self._relative_path(path)
            expected = self._expected_fingerprints.get(
                relative,
                self._baseline_fingerprints.get(relative),
            )
            actual = self._fingerprint(path) if self._path_exists(path) else None
            if actual != expected:
                raise RuntimeError(
                    f"path changed since the runner baseline: {relative}"
                )

    def _touch_pending(self) -> None:
        pending_dir = default_pending_dir(self.root / "wiki" / "events")
        self._touch(pending_dir / "index.json", pending_dir / "latest.md")

    def _unexpected_changes(self) -> list[str]:
        # Foreign dirt present before the run started is deliberately never
        # touched or staged (Friction A); it must not be flagged here as a
        # concurrent change the run needs to abort over.
        changed = self._status_paths() - self._foreign_dirty_at_start
        unexpected = changed - self._touched_paths
        overwritten = {
            path
            for path in changed & self._touched_paths
            if (
                self._fingerprint(self.root / path)
                if self._path_exists(self.root / path)
                else None
            )
            != self._expected_fingerprints[path]
        }
        return sorted(str(path) for path in unexpected | overwritten)

    def _status_paths(self) -> set[Path]:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git status failed: {result.stderr.strip()}")
        return {Path(line[3:]) for line in result.stdout.splitlines() if len(line) >= 4}

    def _machine_local_paths(self) -> set[Path]:
        """The orientation file is the one generated surface a deployment
        keeps out of git (the enforcement contract's untracked surface);
        where it is untracked and ignored the runner regenerates it in
        place and leaves it out of the commit. Every other touched path
        is commit content, ignore rules or not - the night report is
        force-added by design."""
        orientation = Path("CLAUDE.local.md")
        if orientation not in self._touched_paths or self._is_tracked(orientation):
            return set()
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--", str(orientation)],
            capture_output=True,
            cwd=self.root,
        )
        # Exit 1 means no ignore rule matched; anything else is a real
        # failure.
        if result.returncode not in (0, 1):
            raise RuntimeError(
                f"git check-ignore failed: {result.stderr.decode().strip()}"
            )
        return {orientation} if result.returncode == 0 else set()

    def _is_tracked(self, path: Path) -> bool:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(path)],
            capture_output=True,
            cwd=self.root,
        )
        return result.returncode == 0

    def _verify_staged_manifest(self) -> set[Path]:
        staged = self._git_paths("diff", "--cached", "--name-only")
        unexpected = staged - self._touched_paths
        if unexpected:
            paths = "\n".join(sorted(str(path) for path in unexpected))
            raise RuntimeError(f"git add staged unexpected paths:\n{paths}")
        self._verify_worktree_fingerprints()
        for path in staged:
            actual = self._staged_fingerprint(path)
            expected = self._expected_fingerprints[path]
            if actual != expected:
                raise RuntimeError(
                    f"staged content or mode differs from runner output: {path}"
                )
        return staged

    def _verify_committed_manifest(
        self, expected_paths: set[Path], committed_head: str
    ) -> None:
        committed = self._git_paths(
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            committed_head,
        )
        if committed != expected_paths:
            raise RuntimeError(
                "committed path manifest differs: expected "
                f"{sorted(map(str, expected_paths))}, got "
                f"{sorted(map(str, committed))}"
            )
        for path in committed:
            actual = self._committed_fingerprint(committed_head, path)
            expected = self._expected_fingerprints[path]
            if actual != expected:
                raise RuntimeError(
                    f"committed content or mode differs from runner output: {path}"
                )

    def _rollback_unverified_commit(
        self, *, head_before: str, committed_head: str, expected_tree: str
    ) -> str:
        current_head = self._git_text("rev-parse", "HEAD")
        if current_head != committed_head:
            return "HEAD changed again before rollback; refusing to overwrite it"
        parents = self._git_text(
            "rev-list", "--parents", "-n", "1", committed_head
        ).split()
        if parents != [committed_head, head_before]:
            return "unverified commit is not the expected single-parent commit"
        update = subprocess.run(
            [
                "git",
                "update-ref",
                "-m",
                f"{self.config.night.commit_prefix} reject unverified commit",
                "HEAD",
                head_before,
                committed_head,
            ],
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        if update.returncode != 0:
            return f"git update-ref failed: {update.stderr.strip()}"
        restore_index = subprocess.run(
            ["git", "read-tree", expected_tree],
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        if restore_index.returncode != 0:
            error = restore_index.stderr.strip()
            return f"HEAD rolled back but index restore failed: {error}"
        return ""

    def _verify_worktree_fingerprints(self) -> None:
        overwritten = [
            path
            for path, expected in self._expected_fingerprints.items()
            if (
                self._fingerprint(self.root / path)
                if self._path_exists(self.root / path)
                else None
            )
            != expected
        ]
        if overwritten:
            paths = "\n".join(sorted(str(path) for path in overwritten))
            raise RuntimeError(f"runner-touched paths changed concurrently:\n{paths}")

    def _git_paths(self, *args: str) -> set[Path]:
        result = subprocess.run(
            ["git", *args, "-z"],
            capture_output=True,
            cwd=self.root,
        )
        if result.returncode != 0:
            error = result.stderr.decode().strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {error}")
        return {Path(raw.decode()) for raw in result.stdout.split(b"\0") if raw}

    def _gitlink_paths(self) -> set[Path]:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--stage", "-z"],
            capture_output=True,
            cwd=self.root,
        )
        if result.returncode != 0:
            error = result.stderr.decode().strip()
            raise RuntimeError(f"git ls-files --cached --stage failed: {error}")
        return {
            Path(raw_path.decode())
            for record in result.stdout.split(b"\0")
            if record
            for metadata, raw_path in [record.split(b"\t", 1)]
            if metadata.startswith(b"160000 ")
        }

    def _git_text(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def _staged_fingerprint(self, path: Path) -> FileFingerprint:
        mode = self._git_entry_mode(["ls-files", "--stage"], path)
        return self._git_blob_fingerprint(f":{path}", mode, path)

    def _committed_fingerprint(
        self, committed_head: str, path: Path
    ) -> FileFingerprint:
        mode = self._git_entry_mode(["ls-tree", committed_head], path)
        return self._git_blob_fingerprint(f"{committed_head}:{path}", mode, path)

    def _git_entry_mode(self, args: list[str], path: Path) -> str:
        result = subprocess.run(
            ["git", *args, "-z", "--", str(path)],
            capture_output=True,
            cwd=self.root,
        )
        if result.returncode != 0:
            error = result.stderr.decode().strip()
            raise RuntimeError(f"cannot read Git entry {path}: {error}")
        records = [record for record in result.stdout.split(b"\0") if record]
        if len(records) != 1 or b"\t" not in records[0]:
            raise RuntimeError(f"expected one Git entry for {path}")
        metadata, _raw_path = records[0].split(b"\t", 1)
        fields = metadata.split()
        if not fields:
            raise RuntimeError(f"Git entry has no mode for {path}")
        return fields[0].decode()

    def _git_blob_fingerprint(
        self, object_name: str, mode: str, path: Path
    ) -> FileFingerprint:
        blob = subprocess.run(
            ["git", "show", object_name],
            capture_output=True,
            cwd=self.root,
        )
        if blob.returncode != 0:
            error = blob.stderr.decode().strip()
            raise RuntimeError(f"cannot read Git blob {path}: {error}")
        return FileFingerprint(
            content_sha256=hashlib.sha256(blob.stdout).hexdigest(),
            git_mode=mode,
        )

    def _recover_failed_commit_report(self) -> None:
        if not self._commit_staged:
            return
        relative = str(self.report_path.relative_to(self.root))
        result = None
        recovery_error = ""
        if self._path_exists(self.report_path):
            try:
                self._touch(self.report_path)
                result = subprocess.run(
                    ["git", "add", "--force", "--", relative],
                    capture_output=True,
                    text=True,
                    cwd=self.root,
                )
                expected = self._expected_fingerprints[Path(relative)]
                if result.returncode != 0:
                    recovery_error = f"git add failed: {result.stderr.strip()}"
                elif self._staged_fingerprint(Path(relative)) == expected:
                    return
                else:
                    recovery_error = "staged report differs from aborted report"
            except (OSError, RuntimeError) as exc:
                recovery_error = str(exc)
        else:
            recovery_error = "aborted report is missing"
        unstage = subprocess.run(
            ["git", "reset", "--quiet", "HEAD", "--", relative],
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        if recovery_error and unstage.returncode == 0:
            detail = (
                "failed to restage aborted report; report was unstaged: "
                f"{recovery_error}"
            )
            self.report.abort_reason += f"; {detail}"
            print(detail, file=sys.stderr)
        if unstage.returncode != 0:
            restage_error = result.stderr.strip() if result is not None else ""
            self.report.abort_reason += (
                "; failed to restage or unstage aborted report: "
                f"{restage_error}; {unstage.stderr.strip()}"
            )

    @staticmethod
    def _fingerprint(path: Path) -> FileFingerprint:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            content = os.fsencode(os.readlink(path))
            mode = "120000"
        elif stat.S_ISREG(metadata.st_mode):
            content = path.read_bytes()
            mode = "100755" if metadata.st_mode & 0o111 else "100644"
        else:
            raise OSError(f"unsupported file type for runner-owned path: {path}")
        return FileFingerprint(
            content_sha256=hashlib.sha256(content).hexdigest(),
            git_mode=mode,
        )

    def _relative_path(self, path: Path) -> Path:
        absolute = path if path.is_absolute() else self.root / path
        return absolute.absolute().relative_to(self.root.absolute())

    @staticmethod
    def _path_exists(path: Path) -> bool:
        return path.exists() or path.is_symlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="execute the T0 pipeline")
    run_parser.add_argument("--dry-run", action="store_true", help="skip commit")
    run_parser.add_argument(
        "--report-only", action="store_true", help="report without applying"
    )
    run_parser.add_argument(
        "--wiki",
        type=Path,
        default=None,
        help="wiki repo root (the directory containing wiki.toml)",
    )
    run_parser.add_argument(
        "--scheduled",
        action="store_true",
        help="write the canonical scheduled report and qualifying night commit",
    )

    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help()
        return 1

    try:
        root = resolve_wiki_root(args.wiki)
        config = load_config(root)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    set_wiki_root(root)
    runner = NightRunner(
        config,
        dry_run=args.dry_run,
        report_only=args.report_only,
        scheduled=args.scheduled,
    )
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
