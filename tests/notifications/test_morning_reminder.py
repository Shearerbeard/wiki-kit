"""Behavior tests for the scheduled night-run morning reminder.

The report-path and commit-subject conventions come from the fixture
wiki.toml's [night] table (read through wiki_config), never from string
literals duplicated out of the scripts."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = KIT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import wiki_config  # noqa: E402

REMINDER = SCRIPTS_DIR / "morning-reminder.sh"
RENDER_SCHEDULER = SCRIPTS_DIR / "render_scheduler.py"
TODAY = "2026-07-11"

# The fictional acme-notes deployment, with non-default [night] values so
# any convention hardcoded in morning-reminder.sh fails these tests.
WIKI_TOML = """\
[wiki]
name = "acme-notes"

[contract]
protected = ["wiki/log.md", "wiki/events/**"]

[schedule]
morning = "09:05"

[night]
report_dir = "journal/nightly"
commit_prefix = "nightly:"
"""

Fixture = tuple[Path, dict[str, str], "wiki_config.NightConventions"]


def run(*args: str | Path, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(arg) for arg in args],
        capture_output=True,
        check=True,
        cwd=cwd,
        text=True,
    )


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def reminder_repo(tmp_path: Path) -> Fixture:
    repo = tmp_path / "wiki repo with spaces"
    repo.mkdir()
    run("git", "init", "-q", cwd=repo)
    run("git", "config", "user.email", "test@example.com", cwd=repo)
    run("git", "config", "user.name", "Reminder Test", cwd=repo)
    (repo / "wiki.toml").write_text(WIKI_TOML)
    night = wiki_config.load_config(repo).night

    notifier = tmp_path / "terminal-notifier"
    write_executable(
        notifier,
        """#!/usr/bin/env bash
printf 'CALL\n' >> "$NOTIFY_CAPTURE"
printf '<%s>\n' "$@" >> "$NOTIFY_CAPTURE"
exit "${STUB_NOTIFY_EXIT:-0}"
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "WIKI_DIR": str(repo),
            "WIKI_GIT_BIN": "git",
            "WIKI_NOTIFIER_BIN": str(notifier),
            "WIKI_TODAY_UTC": TODAY,
            "WIKI_NIGHT_REPORT_DIR": night.report_dir,
            "WIKI_NIGHT_COMMIT_PREFIX": night.commit_prefix,
            "NOTIFY_CAPTURE": str(tmp_path / "notification"),
            "STUB_NOTIFY_EXIT": "0",
        }
    )
    return repo, env, night


def report_text(outcome: str = "clean", mode: str = "scheduled") -> str:
    aborted = "\n**ABORTED:** apply failed\n" if outcome == "aborted" else ""
    return (
        f"# Night run report — {TODAY}\n\n"
        f"Generated: {TODAY}T09:00:00Z\n\n"
        f"**Mode:** {mode}\n"
        f"**Outcome:** {outcome}\n"
        f"{aborted}\n"
        "## Applied events\n_(none)_\n\n"
        "## Sweep findings\n_(none)_\n\n"
        "## Memory triage\n_(none)_\n\n"
        "## Doctor\n**Clean:** True\n\n"
        "## Metrics\n\n"
        "## Steps\n\n"
        "**Commit intent:** `night`\n"
    )


def write_report(
    repo: Path,
    night: wiki_config.NightConventions,
    text: str,
    name: str | None = None,
) -> Path:
    report = repo / night.report_dir / (name or f"{TODAY}.md")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(text)
    return report


def commit_report(
    repo: Path,
    night: wiki_config.NightConventions,
    report: Path,
    subject: str | None = None,
) -> None:
    run("git", "add", "--force", "--", report, cwd=repo)
    run(
        "git",
        "commit",
        "-q",
        "-m",
        subject or f"{night.commit_prefix} {TODAY}",
        cwd=repo,
    )


def invoke(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(REMINDER)],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )


def notification(env: dict[str, str]) -> str:
    captured = Path(env["NOTIFY_CAPTURE"]).read_text()
    assert captured.count("CALL\n") == 1
    return captured


def expected_notification(message: str, title: str, sound: str) -> str:
    return f"CALL\n<-title>\n<{title}>\n<-message>\n<{message}>\n<-sound>\n<{sound}>\n"


@pytest.mark.parametrize("mode", ["manual", "dry-run", "report-only", "uat"])
def test_missing_canonical_report_ignores_non_scheduled_reports(
    reminder_repo: Fixture,
    mode: str,
) -> None:
    repo, env, night = reminder_repo
    write_report(
        repo,
        night,
        report_text(mode=mode),
        name=f"{TODAY}-{mode}-{TODAY}T120000Z.md",
    )

    result = invoke(env)

    assert result.returncode == 0
    message = (
        f"NIGHT RUN MISSING: no scheduled report for {TODAY}. Check scheduler "
        "status and run /morning."
    )
    assert notification(env) == expected_notification(
        message, "🚨 Wiki Night Run Missing", "Basso"
    )


@pytest.mark.parametrize(
    "text",
    [
        "not a report\n",
        report_text(outcome="clean").replace("**Outcome:** clean\n", ""),
        report_text(outcome="clean").replace("**Mode:** scheduled\n", ""),
        report_text(outcome="clean").replace(
            "**Outcome:** clean\n", "**Outcome:** clean\n**Outcome:** attention\n"
        ),
        report_text(outcome="clean").replace(
            "**Mode:** scheduled\n", "**Mode:** scheduled\n**Mode:** scheduled\n"
        ),
        report_text(outcome="clean", mode="manual"),
    ],
)
def test_malformed_canonical_report_needs_attention(
    reminder_repo: Fixture, text: str
) -> None:
    repo, env, night = reminder_repo
    write_report(repo, night, text)

    result = invoke(env)

    assert result.returncode == 0
    assert notification(env) == expected_notification(
        "NIGHT RUN NEEDS ATTENTION: today's canonical report is malformed. Run "
        "/morning.",
        "⚠️ Wiki Night Run Needs Attention",
        "Basso",
    )


def test_aborted_report_needs_attention_even_if_committed(
    reminder_repo: Fixture,
) -> None:
    repo, env, night = reminder_repo
    report = write_report(repo, night, report_text(outcome="aborted"))
    commit_report(repo, night, report)

    result = invoke(env)

    assert result.returncode == 0
    assert notification(env) == expected_notification(
        "NIGHT RUN NEEDS ATTENTION: the scheduled run aborted. Run /morning.",
        "⚠️ Wiki Night Run Needs Attention",
        "Basso",
    )


def test_aborted_marker_overrides_clean_outcome(
    reminder_repo: Fixture,
) -> None:
    repo, env, night = reminder_repo
    text = report_text().replace(
        "## Applied events", "**ABORTED:** durable partial apply\n\n## Applied events"
    )
    write_report(repo, night, text)

    result = invoke(env)

    assert result.returncode == 0
    assert "scheduled run aborted" in notification(env)


def test_unknown_outcome_needs_attention(
    reminder_repo: Fixture,
) -> None:
    repo, env, night = reminder_repo
    write_report(repo, night, report_text(outcome="mystery"))

    result = invoke(env)

    assert result.returncode == 0
    assert notification(env) == expected_notification(
        "NIGHT RUN NEEDS ATTENTION: today's canonical report has an unknown "
        "outcome. Run /morning.",
        "⚠️ Wiki Night Run Needs Attention",
        "Basso",
    )


@pytest.mark.parametrize("outcome", ["clean", "attention"])
def test_valid_report_without_matching_commit_needs_attention(
    reminder_repo: Fixture, outcome: str
) -> None:
    repo, env, night = reminder_repo
    write_report(repo, night, report_text(outcome=outcome))

    result = invoke(env)

    assert result.returncode == 0
    message = (
        "NIGHT RUN NEEDS ATTENTION: the scheduled report has no matching "
        f"{night.commit_prefix} {TODAY} commit. Run /morning."
    )
    assert notification(env) == expected_notification(
        message, "⚠️ Wiki Night Run Needs Attention", "Basso"
    )


def test_attention_report_with_matching_commit_requests_attention_review(
    reminder_repo: Fixture,
) -> None:
    repo, env, night = reminder_repo
    report = write_report(repo, night, report_text(outcome="attention"))
    commit_report(repo, night, report)

    result = invoke(env)

    assert result.returncode == 0
    message = (
        "The scheduled night run needs review. Run /morning for its "
        "manual-action and reconciliation queues."
    )
    assert notification(env) == expected_notification(
        message, "⚠️ Wiki Morning Review", "Glass"
    )


def test_clean_report_with_matching_commit_requests_normal_review(
    reminder_repo: Fixture,
) -> None:
    repo, env, night = reminder_repo
    report = write_report(repo, night, report_text())
    commit_report(repo, night, report)

    result = invoke(env)

    assert result.returncode == 0
    assert notification(env) == expected_notification(
        "Run /morning to review the clean scheduled wiki run.",
        "🌅 Wiki Morning Review",
        "Glass",
    )


def test_report_modified_after_matching_commit_no_longer_matches(
    reminder_repo: Fixture,
) -> None:
    repo, env, night = reminder_repo
    report = write_report(repo, night, report_text())
    commit_report(repo, night, report)
    report.write_text(report_text().replace("_(none)_", "changed", 1))

    result = invoke(env)

    assert result.returncode == 0
    assert f"no matching {night.commit_prefix}" in notification(env)


def test_wrong_commit_subject_does_not_match(
    reminder_repo: Fixture,
) -> None:
    repo, env, night = reminder_repo
    report = write_report(repo, night, report_text())
    manual_prefix = night.commit_prefix.removesuffix(":") + "-manual:"
    commit_report(repo, night, report, subject=f"{manual_prefix} {TODAY}T090000Z")

    result = invoke(env)

    assert result.returncode == 0
    assert f"no matching {night.commit_prefix}" in notification(env)


def test_commit_body_cannot_impersonate_exact_night_subject(
    reminder_repo: Fixture,
) -> None:
    repo, env, night = reminder_repo
    report = write_report(repo, night, report_text())
    run("git", "add", "--force", "--", report, cwd=repo)
    run(
        "git",
        "commit",
        "-q",
        "-m",
        "not a scheduled run",
        "-m",
        f"{night.commit_prefix} {TODAY}",
        cwd=repo,
    )

    result = invoke(env)

    assert result.returncode == 0
    assert f"no matching {night.commit_prefix}" in notification(env)


def test_invalid_date_override_is_rejected_before_notification(
    reminder_repo: Fixture,
) -> None:
    _repo, env, _night = reminder_repo
    env["WIKI_TODAY_UTC"] = '2026-07-11"\\Ω'

    result = invoke(env)

    assert result.returncode == 64
    assert "invalid UTC date" in result.stderr
    assert not Path(env["NOTIFY_CAPTURE"]).exists()


def test_missing_wiki_dir_fails_loud(
    reminder_repo: Fixture,
) -> None:
    _repo, env, _night = reminder_repo
    env = dict(env)
    del env["WIKI_DIR"]

    result = invoke(env)

    assert result.returncode == 64
    assert "WIKI_DIR" in result.stderr
    assert not Path(env["NOTIFY_CAPTURE"]).exists()


def test_git_failure_cannot_create_a_false_match(
    reminder_repo: Fixture, tmp_path: Path
) -> None:
    repo, env, night = reminder_repo
    write_report(repo, night, report_text())
    git_stub = tmp_path / "failing-git"
    write_executable(git_stub, "#!/usr/bin/env bash\nexit 7\n")
    env["WIKI_GIT_BIN"] = str(git_stub)

    result = invoke(env)

    assert result.returncode == 0
    assert f"no matching {night.commit_prefix}" in notification(env)


def test_notifier_failure_is_visible(
    reminder_repo: Fixture,
) -> None:
    _repo, env, _night = reminder_repo
    env["STUB_NOTIFY_EXIT"] = "9"

    result = invoke(env)

    assert result.returncode == 9
    assert "notification failed with status 9" in result.stderr
    assert "Wiki Night Run Missing" in result.stderr
    notification(env)


def test_morning_launchagent_passes_night_conventions_from_config(
    tmp_path: Path,
) -> None:
    """The generated unit hands the [night] conventions to the script
    explicitly; the script's built-in defaults are only a fallback for
    manual invocation."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "wiki.toml").write_text(WIKI_TOML)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tools: dict[str, Path] = {}
    for name in ("uv", "terminal-notifier", "git"):
        stub = bin_dir / name
        write_executable(stub, "#!/usr/bin/env bash\nexit 0\n")
        tools[name] = stub
    (wiki / "wiki.local.toml").write_text(
        "[tools]\n"
        f'uv = "{tools["uv"]}"\n'
        f'notifier = "{tools["terminal-notifier"]}"\n'
        f'git = "{tools["git"]}"\n'
    )
    night = wiki_config.load_config(wiki).night
    out_dir = tmp_path / "units"

    result = subprocess.run(
        [
            sys.executable,
            str(RENDER_SCHEDULER),
            "--wiki",
            str(wiki),
            "--out",
            str(out_dir),
            "--target",
            "launchd",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    plist_path = out_dir / "com.acme-notes.wiki-morning-reminder.plist"
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)

    assert plist["Label"] == "com.acme-notes.wiki-morning-reminder"
    assert plist["ProgramArguments"] == [
        "/bin/bash",
        str(SCRIPTS_DIR / "morning-reminder.sh"),
    ]
    assert plist["StartCalendarInterval"] == {"Hour": 9, "Minute": 5}
    env_block = plist["EnvironmentVariables"]
    assert env_block["WIKI_DIR"] == str(wiki)
    assert env_block["WIKI_GIT_BIN"] == str(tools["git"])
    assert env_block["WIKI_NOTIFIER_BIN"] == str(tools["terminal-notifier"])
    assert env_block["WIKI_NIGHT_REPORT_DIR"] == night.report_dir
    assert env_block["WIKI_NIGHT_COMMIT_PREFIX"] == night.commit_prefix
