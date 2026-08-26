"""Behavior tests for the notifier abstraction (scripts/wiki-notify.sh):
severity-mapped dispatch per notifier binary, platform-loud failures,
and exit-status propagation."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = KIT_ROOT / "scripts" / "wiki-notify.sh"
TITLE = "Some Title"
MESSAGE = "a message body"


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def notifier_stub(tmp_path: Path):
    def make(name: str) -> tuple[Path, Path]:
        stub = tmp_path / name
        capture = tmp_path / f"{name}-args"
        write_executable(
            stub,
            """#!/usr/bin/env bash
printf '<%s>\n' "$@" > "$CAPTURE"
exit "${STUB_NOTIFY_EXIT:-0}"
""",
        )
        return stub, capture

    return make


def run_wrapper(
    severity: str, notifier: Path | None, capture: Path
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CAPTURE"] = str(capture)
    if notifier is not None:
        env["WIKI_NOTIFIER_BIN"] = str(notifier)
    else:
        env.pop("WIKI_NOTIFIER_BIN", None)
    return subprocess.run(
        ["/bin/bash", str(WRAPPER), severity, TITLE, MESSAGE],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )


@pytest.mark.parametrize(
    ("severity", "sound"), [("routine", "Glass"), ("problem", "Basso")]
)
def test_terminal_notifier_argv_maps_severity_to_sound(
    notifier_stub, severity: str, sound: str
) -> None:
    stub, capture = notifier_stub("terminal-notifier")

    result = run_wrapper(severity, stub, capture)

    assert result.returncode == 0, result.stderr
    assert capture.read_text().splitlines() == [
        "<-title>",
        f"<{TITLE}>",
        "<-message>",
        f"<{MESSAGE}>",
        "<-sound>",
        f"<{sound}>",
    ]


@pytest.mark.parametrize(
    ("severity", "urgency"), [("routine", "normal"), ("problem", "critical")]
)
def test_notify_send_argv_maps_severity_to_urgency(
    notifier_stub, severity: str, urgency: str
) -> None:
    stub, capture = notifier_stub("notify-send")

    result = run_wrapper(severity, stub, capture)

    assert result.returncode == 0, result.stderr
    assert capture.read_text().splitlines() == [
        "<-u>",
        f"<{urgency}>",
        f"<{TITLE}>",
        f"<{MESSAGE}>",
    ]


def test_unknown_notifier_basename_fails_loud(notifier_stub, tmp_path: Path) -> None:
    stub, capture = notifier_stub("pigeon")

    result = run_wrapper("routine", stub, capture)

    assert result.returncode != 0
    assert "terminal-notifier" in result.stderr
    assert "notify-send" in result.stderr
    assert "[tools].notifier" in result.stderr
    assert not capture.exists()


def test_missing_notifier_binary_fails_loud(tmp_path: Path) -> None:
    result = run_wrapper("routine", tmp_path / "notify-send", tmp_path / "cap")

    assert result.returncode != 0
    assert "notify-send" in result.stderr


def test_non_executable_notifier_binary_fails_loud(tmp_path: Path) -> None:
    stub = tmp_path / "notify-send"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n")

    result = run_wrapper("routine", stub, tmp_path / "cap")

    assert result.returncode != 0
    assert "notify-send" in result.stderr


def test_bad_severity_is_a_usage_error(notifier_stub) -> None:
    stub, capture = notifier_stub("terminal-notifier")

    result = run_wrapper("urgent", stub, capture)

    assert result.returncode == 64
    assert "usage:" in result.stderr
    assert not capture.exists()


def test_notifier_exit_status_propagates(notifier_stub) -> None:
    stub, capture = notifier_stub("terminal-notifier")

    env = os.environ.copy()
    env.update(
        {
            "CAPTURE": str(capture),
            "WIKI_NOTIFIER_BIN": str(stub),
            "STUB_NOTIFY_EXIT": "7",
        }
    )
    result = subprocess.run(
        ["/bin/bash", str(WRAPPER), "problem", TITLE, MESSAGE],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 7
