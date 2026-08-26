"""Behavior tests for the end-of-day garden reminder, plus the rendered
launchd-unit contract (render_scheduler.py against a fixture wiki.toml)."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[2]
REMINDER = KIT_ROOT / "scripts" / "garden-reminder.sh"
RENDER_SCHEDULER = KIT_ROOT / "scripts" / "render_scheduler.py"
TITLE = "🌿 Wiki Garden Checkpoint"
ERROR_TITLE = "🚨 Wiki Garden Checkpoint"

# The fictional deployment the kit test corpus uses. Non-default
# [schedule] and [night] values so anything hardcoded in the templates
# or the renderer fails these tests.
WIKI_TOML = """\
[wiki]
name = "acme-notes"

[contract]
protected = ["wiki/log.md", "wiki/events/**"]

[schedule]
night = "02:15"
morning = "09:05"
garden_reminder = "17:45"

[night]
report_dir = "journal/nightly"
commit_prefix = "nightly:"
"""


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def reminder_env(tmp_path: Path) -> dict[str, str]:
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    uv_stub = tmp_path / "uv"
    notifier_stub = tmp_path / "terminal-notifier"
    write_executable(
        uv_stub,
        """#!/usr/bin/env bash
printf '%s\n' "$@" > "$UV_CAPTURE"
printf '%s\n' "$STUB_PENDING_OUTPUT"
exit "$STUB_UV_EXIT"
""",
    )
    write_executable(
        notifier_stub,
        """#!/usr/bin/env bash
printf 'CALL\n' >> "$NOTIFY_CAPTURE"
printf '<%s>\n' "$@" >> "$NOTIFY_CAPTURE"
exit "$STUB_NOTIFY_EXIT"
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "WIKI_DIR": str(wiki_dir),
            "WIKI_UV_BIN": str(uv_stub),
            "WIKI_NOTIFIER_BIN": str(notifier_stub),
            "UV_CAPTURE": str(tmp_path / "uv-args"),
            "NOTIFY_CAPTURE": str(tmp_path / "notify-args"),
            "STUB_PENDING_OUTPUT": "0",
            "STUB_UV_EXIT": "0",
            "STUB_NOTIFY_EXIT": "0",
        }
    )
    return env


def run_reminder(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(REMINDER), "run"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )


def captured_notification(env: dict[str, str]) -> str:
    capture = Path(env["NOTIFY_CAPTURE"]).read_text()
    assert capture.count("CALL\n") == 1
    return capture


def expected_notification(message: str, title: str = TITLE) -> str:
    return f"CALL\n<-title>\n<{title}>\n<-message>\n<{message}>\n<-sound>\n<Glass>\n"


def test_zero_pending_sends_honest_unconditional_reminder(
    reminder_env: dict[str, str],
) -> None:
    result = run_reminder(reminder_env)

    assert result.returncode == 0
    notification = captured_notification(reminder_env)
    message = (
        "End-of-day check: no handoffs await /garden. Hand off any still-open "
        "sessions; run /garden if that creates handoffs."
    )
    assert notification == expected_notification(message)


@pytest.mark.parametrize(
    ("pending_count", "expected_phrase"),
    [("1", "1 handoff awaits /garden"), ("3", "3 handoffs await /garden")],
)
def test_pending_count_sends_exactly_one_counted_reminder(
    reminder_env: dict[str, str], pending_count: str, expected_phrase: str
) -> None:
    reminder_env["STUB_PENDING_OUTPUT"] = pending_count

    result = run_reminder(reminder_env)

    assert result.returncode == 0
    notification = captured_notification(reminder_env)
    message = (
        f"End-of-day check: {expected_phrase}. Hand off any still-open sessions "
        "first, then run /garden."
    )
    assert notification == expected_notification(message)


@pytest.mark.parametrize(
    ("pending_output", "pending_exit"),
    [("permission denied", "1"), ("not-a-count", "0")],
)
def test_pending_check_error_notifies_once_and_fails_loud(
    reminder_env: dict[str, str], pending_output: str, pending_exit: str
) -> None:
    reminder_env.update(
        {"STUB_PENDING_OUTPUT": pending_output, "STUB_UV_EXIT": pending_exit}
    )

    result = run_reminder(reminder_env)

    assert result.returncode == 1
    assert "could not determine" in result.stderr
    notification = captured_notification(reminder_env)
    message = (
        "End-of-day check could not count pending handoffs. Hand off any "
        "still-open sessions, then check /garden manually."
    )
    assert notification == expected_notification(message, ERROR_TITLE)
    assert pending_output not in notification


def test_invokes_kit_count_pending_with_explicit_wiki(
    reminder_env: dict[str, str],
) -> None:
    """Config pass-through: the reminder runs the KIT's wiki-event.py (the
    machinery lives in the kit checkout, resolved from the script's own
    location) and passes the wiki root explicitly."""
    result = run_reminder(reminder_env)

    assert result.returncode == 0
    uv_args = Path(reminder_env["UV_CAPTURE"]).read_text().splitlines()
    assert uv_args == [
        "run",
        "--project",
        str(KIT_ROOT),
        str(KIT_ROOT / "scripts" / "wiki-event.py"),
        "count-pending",
        "--wiki",
        reminder_env["WIKI_DIR"],
    ]


def test_notifier_failure_is_visible(reminder_env: dict[str, str]) -> None:
    reminder_env["STUB_NOTIFY_EXIT"] = "7"

    result = run_reminder(reminder_env)

    assert result.returncode == 7
    captured_notification(reminder_env)


def test_rejects_nonproduction_invocation(reminder_env: dict[str, str]) -> None:
    result = subprocess.run(
        ["/bin/bash", str(REMINDER)],
        capture_output=True,
        check=False,
        env=reminder_env,
        text=True,
    )

    assert result.returncode == 64
    assert "usage:" in result.stderr
    assert not Path(reminder_env["NOTIFY_CAPTURE"]).exists()


def test_missing_wiki_dir_fails_loud(reminder_env: dict[str, str]) -> None:
    env = dict(reminder_env)
    del env["WIKI_DIR"]

    result = subprocess.run(
        ["/bin/bash", str(REMINDER), "run"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 64
    assert "WIKI_DIR" in result.stderr
    assert not Path(env["NOTIFY_CAPTURE"]).exists()


# --- LaunchAgent contract. The source repo asserted byte-literal plist
# --- content; the kit generates units per machine, so these tests render
# --- the templates through render_scheduler.py against the fixture
# --- wiki.toml above and assert the rendered content instead.


def _render_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
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
    out_dir = tmp_path / "units"
    return wiki, out_dir, tools


def _run_renderer(wiki: Path, out_dir: Path) -> subprocess.CompletedProcess[str]:
    # These tests assert plist content; pin the target so a Linux host
    # (CI) does not default to rendering systemd units instead.
    return subprocess.run(
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


def _load_plist(path: Path) -> dict:
    with path.open("rb") as handle:
        return plistlib.load(handle)


def test_garden_launchagent_renders_from_config(tmp_path: Path) -> None:
    wiki, out_dir, tools = _render_fixture(tmp_path)
    result = _run_renderer(wiki, out_dir)
    assert result.returncode == 0, result.stderr

    plist = _load_plist(out_dir / "com.acme-notes.wiki-garden-reminder.plist")

    assert plist["Label"] == "com.acme-notes.wiki-garden-reminder"
    assert plist["ProgramArguments"] == [
        "/bin/bash",
        str(KIT_ROOT / "scripts" / "garden-reminder.sh"),
        "run",
    ]
    assert plist["WorkingDirectory"] == str(wiki)
    assert plist["StartCalendarInterval"] == {"Hour": 17, "Minute": 45}
    log_dir = wiki / "reports" / "scheduler-logs"
    assert plist["StandardOutPath"] == str(log_dir / "garden-reminder-stdout.log")
    assert plist["StandardErrorPath"] == str(log_dir / "garden-reminder-stderr.log")
    assert log_dir.is_dir()
    env = plist["EnvironmentVariables"]
    assert env["WIKI_DIR"] == str(wiki)
    assert env["WIKI_UV_BIN"] == str(tools["uv"])
    assert env["WIKI_NOTIFIER_BIN"] == str(tools["terminal-notifier"])
    assert env["HOME"] == str(Path.home())
    assert str(tools["uv"].parent) in env["PATH"].split(":")


def test_night_shift_launchagent_renders_from_config(tmp_path: Path) -> None:
    wiki, out_dir, tools = _render_fixture(tmp_path)
    result = _run_renderer(wiki, out_dir)
    assert result.returncode == 0, result.stderr

    plist = _load_plist(out_dir / "com.acme-notes.wiki-night-shift.plist")

    assert plist["Label"] == "com.acme-notes.wiki-night-shift"
    assert plist["ProgramArguments"] == [
        str(tools["uv"]),
        "run",
        "--project",
        str(KIT_ROOT),
        str(KIT_ROOT / "scripts" / "wiki_night.py"),
        "run",
        "--scheduled",
        "--wiki",
        str(wiki),
    ]
    assert plist["WorkingDirectory"] == str(wiki)
    assert plist["StartCalendarInterval"] == {"Hour": 2, "Minute": 15}


def test_render_only_prints_load_instructions(tmp_path: Path) -> None:
    wiki, out_dir, _tools = _render_fixture(tmp_path)
    result = _run_renderer(wiki, out_dir)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("wrote ") == 3
    # Loading stays a printed instruction; rendering never runs launchctl.
    assert "launchctl load" in result.stdout


def test_rerender_is_idempotent(tmp_path: Path) -> None:
    wiki, out_dir, _tools = _render_fixture(tmp_path)
    first = _run_renderer(wiki, out_dir)
    assert first.returncode == 0, first.stderr
    assert first.stdout.count("wrote ") == 3

    again = _run_renderer(wiki, out_dir)

    assert again.returncode == 0, again.stderr
    assert again.stdout.count("unchanged ") == 3
    assert "wrote " not in again.stdout


def test_missing_tool_binary_fails_loud(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "wiki.toml").write_text(WIKI_TOML)
    (wiki / "wiki.local.toml").write_text(
        '[tools]\nnotifier = "/nonexistent/notifier"\n'
    )
    out_dir = tmp_path / "units"

    result = _run_renderer(wiki, out_dir)

    assert result.returncode == 1
    assert "notifier" in result.stderr
    assert not out_dir.exists() or not list(out_dir.iterdir())
