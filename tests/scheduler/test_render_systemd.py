"""Rendered systemd-unit contract: render_scheduler.py --target systemd
against a fixture wiki.toml, mirroring the launchd contract suite in
tests/notifications/."""

from __future__ import annotations

import configparser
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = KIT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import render_scheduler  # noqa: E402
import wiki_config  # noqa: E402

RENDER_SCHEDULER = SCRIPTS_DIR / "render_scheduler.py"
SYSTEMD_TEMPLATES = KIT_ROOT / "templates" / "systemd"

# Same fictional deployment as the launchd contract suite, with
# non-default [schedule] times so anything hardcoded fails these tests.
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

EXPECTED_FILES = {
    f"com.acme-notes.wiki-{stem}.{suffix}"
    for stem in ("night-shift", "morning-reminder", "garden-reminder")
    for suffix in ("service", "timer")
}

EXPECTED_ONCALENDAR = {
    "night-shift": "*-*-* 02:15:00",
    "morning-reminder": "*-*-* 09:05:00",
    "garden-reminder": "*-*-* 17:45:00",
}


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "wiki.toml").write_text(WIKI_TOML)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tools: dict[str, Path] = {}
    for name in ("uv", "notify-send", "git"):
        stub = bin_dir / name
        write_executable(stub, "#!/usr/bin/env bash\nexit 0\n")
        tools[name] = stub
    (wiki / "wiki.local.toml").write_text(
        "[tools]\n"
        f'uv = "{tools["uv"]}"\n'
        f'notifier = "{tools["notify-send"]}"\n'
        f'git = "{tools["git"]}"\n'
    )
    out_dir = tmp_path / "units"
    return wiki, out_dir, tools


def run_renderer(
    wiki: Path, out_dir: Path, *extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(RENDER_SCHEDULER),
            "--wiki",
            str(wiki),
            "--out",
            str(out_dir),
            *extra,
        ],
        capture_output=True,
        check=False,
        text=True,
    )


def load_unit(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(path.read_text())
    return parser


def environment_lines(path: Path) -> list[str]:
    return [
        line
        for line in path.read_text().splitlines()
        if line.startswith("Environment=")
    ]


def test_six_units_render_with_shared_label_stem(fixture) -> None:
    wiki, out_dir, _tools = fixture
    result = run_renderer(wiki, out_dir, "--target", "systemd")
    assert result.returncode == 0, result.stderr

    assert {path.name for path in out_dir.iterdir()} == EXPECTED_FILES
    for path in out_dir.iterdir():
        assert "{{" not in path.read_text(), path.name


def test_timers_carry_oncalendar_from_schedule(fixture) -> None:
    wiki, out_dir, _tools = fixture
    result = run_renderer(wiki, out_dir, "--target", "systemd")
    assert result.returncode == 0, result.stderr

    for stem, oncalendar in EXPECTED_ONCALENDAR.items():
        timer = load_unit(out_dir / f"com.acme-notes.wiki-{stem}.timer")
        assert timer["Timer"]["OnCalendar"] == oncalendar
        assert timer["Timer"]["Persistent"] == "true"
        assert timer["Install"]["WantedBy"] == "timers.target"


def test_service_execstart_mirrors_the_launchd_program_arguments(
    fixture,
) -> None:
    wiki, out_dir, tools = fixture
    result = run_renderer(wiki, out_dir, "--target", "systemd")
    assert result.returncode == 0, result.stderr

    night = load_unit(out_dir / "com.acme-notes.wiki-night-shift.service")
    assert night["Service"]["Type"] == "oneshot"
    assert night["Service"]["WorkingDirectory"] == str(wiki)
    assert night["Service"]["ExecStart"] == (
        f'"{tools["uv"]}" run --project "{KIT_ROOT}" '
        f'"{KIT_ROOT}/scripts/wiki_night.py" run --scheduled --wiki "{wiki}"'
    )
    morning = load_unit(out_dir / "com.acme-notes.wiki-morning-reminder.service")
    assert morning["Service"]["ExecStart"] == (
        f'/bin/bash "{KIT_ROOT}/scripts/morning-reminder.sh"'
    )
    garden = load_unit(out_dir / "com.acme-notes.wiki-garden-reminder.service")
    assert garden["Service"]["ExecStart"] == (
        f'/bin/bash "{KIT_ROOT}/scripts/garden-reminder.sh" run'
    )


def test_service_environment_mirrors_the_launchd_environment(fixture) -> None:
    wiki, out_dir, tools = fixture
    result = run_renderer(wiki, out_dir, "--target", "systemd")
    assert result.returncode == 0, result.stderr

    morning = environment_lines(
        out_dir / "com.acme-notes.wiki-morning-reminder.service"
    )
    assert f'Environment="WIKI_DIR={wiki}"' in morning
    assert f'Environment="WIKI_GIT_BIN={tools["git"]}"' in morning
    assert f'Environment="WIKI_NOTIFIER_BIN={tools["notify-send"]}"' in morning
    assert 'Environment="WIKI_NIGHT_REPORT_DIR=journal/nightly"' in morning
    assert 'Environment="WIKI_NIGHT_COMMIT_PREFIX=nightly:"' in morning
    # Only the morning reminder reads the hint (its "no report" message);
    # garden-reminder.sh never reads it, so its unit does not carry it.
    hint = "systemctl --user list-timers 'com.acme-notes.wiki-*'"
    assert f'Environment="WIKI_SCHEDULER_HINT={hint}"' in morning

    garden = environment_lines(out_dir / "com.acme-notes.wiki-garden-reminder.service")
    assert f'Environment="WIKI_UV_BIN={tools["uv"]}"' in garden
    assert f'Environment="WIKI_NOTIFIER_BIN={tools["notify-send"]}"' in garden
    assert not any("WIKI_SCHEDULER_HINT" in line for line in garden)

    night = load_unit(out_dir / "com.acme-notes.wiki-night-shift.service")
    log_dir = wiki / "reports" / "scheduler-logs"
    assert (
        night["Service"]["StandardOutput"] == f"append:{log_dir}/night-shift-stdout.log"
    )
    assert (
        night["Service"]["StandardError"] == f"append:{log_dir}/night-shift-stderr.log"
    )
    assert log_dir.is_dir()


def test_render_only_prints_load_instructions(fixture) -> None:
    wiki, out_dir, _tools = fixture
    result = run_renderer(wiki, out_dir, "--target", "systemd")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("wrote ") == 6
    # Loading stays a printed instruction; rendering never runs systemctl.
    assert "systemctl --user daemon-reload" in result.stdout
    assert result.stdout.count("systemctl --user enable --now ") == 3
    assert ".timer" in result.stdout


def test_rerender_is_idempotent(fixture) -> None:
    wiki, out_dir, _tools = fixture
    first = run_renderer(wiki, out_dir, "--target", "systemd")
    assert first.returncode == 0, first.stderr
    assert first.stdout.count("wrote ") == 6

    again = run_renderer(wiki, out_dir, "--target", "systemd")

    assert again.returncode == 0, again.stderr
    assert again.stdout.count("unchanged ") == 6
    assert "wrote " not in again.stdout


def test_missing_tool_binary_fails_loud(fixture) -> None:
    wiki, out_dir, _tools = fixture
    (wiki / "wiki.local.toml").write_text(
        '[tools]\nnotifier = "/nonexistent/notifier"\n'
    )

    result = run_renderer(wiki, out_dir, "--target", "systemd")

    assert result.returncode == 1
    assert "notifier" in result.stderr
    assert not out_dir.exists() or not list(out_dir.iterdir())


def test_leftover_placeholder_guard_fires_on_a_broken_template(
    fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wiki, _out_dir, _tools = fixture
    broken_dir = tmp_path / "broken-templates"
    shutil.copytree(SYSTEMD_TEMPLATES, broken_dir)
    timer = broken_dir / "night-shift.timer.template"
    timer.write_text(timer.read_text() + "{{BOGUS}}\n")
    target = render_scheduler.TARGETS["systemd"]
    monkeypatch.setitem(
        render_scheduler.TARGETS,
        "systemd",
        target._replace(template_dir=broken_dir),
    )

    config = wiki_config.load_config(wiki)
    with pytest.raises(wiki_config.ConfigError, match="BOGUS"):
        render_scheduler.render_units(config, "systemd")


def test_percent_escapes_per_target(fixture, tmp_path: Path) -> None:
    """A literal % in a config value doubles under systemd (specifier
    rule) and passes through the launchd plist untouched (which
    xml-escapes instead)."""
    wiki, _out_dir, _tools = fixture
    text = (wiki / "wiki.toml").read_text()
    (wiki / "wiki.toml").write_text(
        text.replace('commit_prefix = "nightly:"', 'commit_prefix = "night%&:"')
    )
    systemd_out = tmp_path / "systemd-units"
    launchd_out = tmp_path / "launchd-units"

    systemd = run_renderer(wiki, systemd_out, "--target", "systemd")
    assert systemd.returncode == 0, systemd.stderr
    launchd = run_renderer(wiki, launchd_out, "--target", "launchd")
    assert launchd.returncode == 0, launchd.stderr

    service = (systemd_out / "com.acme-notes.wiki-morning-reminder.service").read_text()
    assert "WIKI_NIGHT_COMMIT_PREFIX=night%%&:" in service
    plist_text = (
        launchd_out / "com.acme-notes.wiki-morning-reminder.plist"
    ).read_text()
    assert "night%&amp;:" in plist_text
    assert "%%" not in plist_text


def test_spaced_wiki_root_quotes_execstart_and_warns(tmp_path: Path) -> None:
    """systemd splits unquoted unit values on whitespace. ExecStart=
    tolerates quoting (the renderer quotes its path arguments), but
    WorkingDirectory= and the StandardOutput= append: path do not, so a
    spaced wiki root renders with a loud stderr warning, not a failure."""
    wiki = tmp_path / "wiki repo with spaces"
    wiki.mkdir()
    (wiki / "wiki.toml").write_text(WIKI_TOML)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tools: dict[str, Path] = {}
    for name in ("uv", "notify-send", "git"):
        stub = bin_dir / name
        write_executable(stub, "#!/usr/bin/env bash\nexit 0\n")
        tools[name] = stub
    (wiki / "wiki.local.toml").write_text(
        "[tools]\n"
        f'uv = "{tools["uv"]}"\n'
        f'notifier = "{tools["notify-send"]}"\n'
        f'git = "{tools["git"]}"\n'
    )
    out_dir = tmp_path / "units"

    result = run_renderer(wiki, out_dir, "--target", "systemd")

    assert result.returncode == 0
    assert "contains whitespace" in result.stderr
    night = load_unit(out_dir / "com.acme-notes.wiki-night-shift.service")
    assert night["Service"]["ExecStart"] == (
        f'"{tools["uv"]}" run --project "{KIT_ROOT}" '
        f'"{KIT_ROOT}/scripts/wiki_night.py" run --scheduled --wiki "{wiki}"'
    )
    morning = load_unit(out_dir / "com.acme-notes.wiki-morning-reminder.service")
    assert morning["Service"]["ExecStart"] == (
        f'/bin/bash "{KIT_ROOT}/scripts/morning-reminder.sh"'
    )
    assert morning["Service"]["WorkingDirectory"] == str(wiki)
    garden = load_unit(out_dir / "com.acme-notes.wiki-garden-reminder.service")
    assert garden["Service"]["ExecStart"] == (
        f'/bin/bash "{KIT_ROOT}/scripts/garden-reminder.sh" run'
    )


def test_launchd_target_does_not_warn_on_spaces(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki repo with spaces"
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

    result = run_renderer(wiki, tmp_path / "units", "--target", "launchd")

    assert result.returncode == 0, result.stderr
    assert "whitespace" not in result.stderr


def test_quote_in_config_value_fails_systemd_but_not_launchd(
    fixture, tmp_path: Path
) -> None:
    """A double-quote in a value would break out of the systemd unit's
    Environment="..." quoting: the systemd render fails loud naming the
    key; the launchd render passes it through (legal in plist element
    text) and succeeds."""
    wiki, _out_dir, _tools = fixture
    text = (wiki / "wiki.toml").read_text()
    (wiki / "wiki.toml").write_text(
        text.replace('commit_prefix = "nightly:"', "commit_prefix = 'night\"'")
    )

    systemd = run_renderer(wiki, tmp_path / "systemd-units", "--target", "systemd")
    assert systemd.returncode == 1
    assert "NIGHT_COMMIT_PREFIX" in systemd.stderr

    launchd = run_renderer(wiki, tmp_path / "launchd-units", "--target", "launchd")
    assert launchd.returncode == 0, launchd.stderr
    plist = (
        tmp_path / "launchd-units" / "com.acme-notes.wiki-morning-reminder.plist"
    ).read_text()
    assert 'night"' in plist


def test_token_text_inside_a_value_renders_literally(fixture, tmp_path: Path) -> None:
    """Single-pass substitution: a config value holding the literal text
    of another token is data - it renders as-is and is never rescanned,
    while genuine template tokens still render."""
    wiki, _out_dir, tools = fixture
    text = (wiki / "wiki.toml").read_text()
    (wiki / "wiki.toml").write_text(
        text.replace('name = "acme-notes"', "name = 'x{{PATH}}'")
    )
    out_dir = tmp_path / "units"

    result = run_renderer(wiki, out_dir, "--target", "launchd")

    assert result.returncode == 0, result.stderr
    plist = (out_dir / "com.x{{PATH}}.wiki-night-shift.plist").read_text()
    # The wiki name's literal {{PATH}} survived substitution...
    assert "com.x{{PATH}}.wiki-night-shift" in plist
    # ...while the template's genuine {{PATH}} token rendered.
    assert str(tools["uv"].parent) in plist
