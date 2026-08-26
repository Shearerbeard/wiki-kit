#!/usr/bin/env python3
"""Render per-machine scheduler units from the kit's templates.

Schedulers cannot expand $HOME or read environment variables, so the
three scheduler units cannot ship as static files: every path must land
fully rendered, per machine. This CLI renders the templates for one
target (launchd from `templates/launchd/`, systemd user timers from
`templates/systemd/`) against a deployment's wiki.toml: unit labels
derive from `[wiki].name` (`com.<name>.wiki-*`), run times from
`[schedule]`, tool binaries from the `[tools]` overlay (falling back to
PATH lookup, failing loud when a binary resolves nowhere), and unit logs
land in `<root>/reports/scheduler-logs`.

It renders and writes: the unit files under --out, plus the
deployment's <root>/reports/scheduler-logs directory, created so the
units' log paths exist. It never loads a unit. The load commands
(launchctl or systemctl --user) are printed after a render. Writing is
idempotent: a file whose content already matches is left untouched.

--target defaults to the host's scheduler (launchd on macOS, systemd on
Linux). The default --out (~/Library/LaunchAgents or
~/.config/systemd/user) only applies where that target is the host
scheduler; any platform can render any target to an explicit --out.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import NamedTuple
from xml.sax.saxutils import escape

from wiki_config import (
    KIT_ROOT,
    ConfigError,
    WikiConfig,
    load_config,
    resolve_wiki_root,
)


class Target(NamedTuple):
    template_dir: Path
    # Unit file suffixes rendered per job, in write order.
    suffixes: tuple[str, ...]
    default_out: Path
    # sys.platform value where the default --out applies.
    host_platform: str
    notifier_default: str


TARGETS = {
    "launchd": Target(
        template_dir=KIT_ROOT / "templates" / "launchd",
        suffixes=("plist",),
        default_out=Path.home() / "Library" / "LaunchAgents",
        host_platform="darwin",
        notifier_default="terminal-notifier",
    ),
    "systemd": Target(
        template_dir=KIT_ROOT / "templates" / "systemd",
        suffixes=("service", "timer"),
        default_out=Path.home() / ".config" / "systemd" / "user",
        host_platform="linux",
        notifier_default="notify-send",
    ),
}

# Unit stem -> [schedule] key. The label is com.<[wiki].name>.wiki-<stem>;
# the rendered files are <label>.<suffix> for each of the target's suffixes.
UNITS = {
    "night-shift": "night",
    "morning-reminder": "morning",
    "garden-reminder": "garden_reminder",
}

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")


def _escape_value(value: str, target_name: str) -> str:
    # systemd expands % specifiers in unit files, so a literal % doubles;
    # launchd plists are XML.
    if target_name == "systemd":
        return value.replace("%", "%%")
    return escape(value)


def _substitute(text: str, values: dict[str, str], target_name: str) -> str:
    """One-pass token replacement: substituted text is never rescanned,
    and unknown tokens pass through for the leftover guard to catch."""

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        value = values.get(token[2:-2])
        if value is None:
            return token
        return _escape_value(value, target_name)

    return PLACEHOLDER_RE.sub(replace, text)


def _scheduler_hint(target_name: str, label_prefix: str) -> str:
    """One hint for all of a wiki's units: list its scheduler entries
    (grep -F matches com.<name>.wiki-* on the fixed com.<name>.wiki
    substring; the systemd glob needs the dash)."""
    if target_name == "systemd":
        return f"systemctl --user list-timers '{label_prefix}-*'"
    return f"launchctl list | grep -F {label_prefix}"


def _parse_time(value: str, key: str) -> tuple[int, int]:
    match = TIME_RE.match(value)
    if match is None:
        raise ConfigError(
            f"[schedule].{key} must be HH:MM (24-hour), got {value!r}"
        )
    return int(match.group(1)), int(match.group(2))


def _resolve_tool(config: WikiConfig, name: str, default: str) -> str:
    configured = config.tool(name, default)
    resolved = shutil.which(configured)
    if resolved is None:
        raise ConfigError(
            f"tool {name!r} resolves nowhere: {configured!r} is not an "
            f"executable on PATH or an absolute path; set [tools].{name} "
            "in wiki.local.toml"
        )
    return str(Path(resolved).absolute())


def _unit_path(tools: dict[str, str]) -> str:
    """A PATH for the unit environment: the resolved tools' directories
    first (so helpers the tools spawn resolve consistently), then the
    standard system directories."""
    directories: list[str] = []
    for binary in tools.values():
        parent = str(Path(binary).parent)
        if parent not in directories:
            directories.append(parent)
    for standard in ("/usr/local/bin", "/usr/bin", "/bin"):
        if standard not in directories:
            directories.append(standard)
    return ":".join(directories)


def render_units(config: WikiConfig, target_name: str) -> list[tuple[str, str]]:
    """Render all units for the target; returns (file name, content) pairs."""
    target = TARGETS[target_name]
    tools = {
        "uv": _resolve_tool(config, "uv", "uv"),
        "notifier": _resolve_tool(config, "notifier", target.notifier_default),
        "git": _resolve_tool(config, "git", "git"),
    }
    log_dir = config.root / "reports" / "scheduler-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if target_name == "systemd" and any(char.isspace() for char in str(config.root)):
        # Quoting is verified only for ExecStart=; WorkingDirectory= and
        # the append: paths stay unquoted, so a spaced wiki root may not
        # run under systemd. Unverified against a live systemd; the
        # warning keeps the risk loud at render time.
        print(
            f"warning: wiki root {config.root} contains whitespace; only "
            "ExecStart= quoting is verified - the rendered units' "
            "WorkingDirectory= and StandardOutput=/StandardError= "
            "append: paths are unquoted and a spaced wiki root may not "
            "run under systemd",
            file=sys.stderr,
        )
    schedule_times = {
        "night": config.schedule.night,
        "morning": config.schedule.morning,
        "garden_reminder": config.schedule.garden_reminder,
    }
    rendered: list[tuple[str, str]] = []
    for stem, schedule_key in UNITS.items():
        hour, minute = _parse_time(schedule_times[schedule_key], schedule_key)
        label = f"com.{config.name}.wiki-{stem}"
        values = {
            "LABEL": label,
            "WIKI_ROOT": str(config.root),
            "KIT_ROOT": str(KIT_ROOT),
            "HOME": str(Path.home()),
            "UV_BIN": tools["uv"],
            "NOTIFIER_BIN": tools["notifier"],
            "GIT_BIN": tools["git"],
            "HOUR": str(hour),
            "MINUTE": str(minute),
            "ONCALENDAR": f"*-*-* {hour:02d}:{minute:02d}:00",
            "LOG_DIR": str(log_dir),
            "PATH": _unit_path(tools),
            "NIGHT_REPORT_DIR": config.night.report_dir,
            "NIGHT_COMMIT_PREFIX": config.night.commit_prefix,
            "SCHEDULER_HINT": _scheduler_hint(target_name, f"com.{config.name}.wiki"),
        }
        if target_name == "systemd":
            for key, value in values.items():
                if '"' in value or "\n" in value:
                    raise ConfigError(
                        f"{key} contains a double-quote or newline, which "
                        f"would break out of the systemd unit's quoting: "
                        f"{value!r}"
                    )
        # Placeholder text inside a value is data, not an unrendered
        # template token (e.g. a wiki name holding literal {{PATH}}).
        literal = {
            token
            for value in values.values()
            for token in PLACEHOLDER_RE.findall(value)
        }
        for suffix in target.suffixes:
            template_path = target.template_dir / f"{stem}.{suffix}.template"
            text = _substitute(
                template_path.read_text(encoding="utf-8"), values, target_name
            )
            leftover = sorted(set(PLACEHOLDER_RE.findall(text)) - literal)
            if leftover:
                raise ConfigError(
                    f"{template_path} has unrendered placeholders: {leftover}"
                )
            rendered.append((f"{label}.{suffix}", text))
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--wiki",
        type=Path,
        default=None,
        help="wiki repo root (the directory containing wiki.toml)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output directory (default: the target's per-user unit dir, "
        "on its host platform only)",
    )
    parser.add_argument(
        "--target",
        choices=sorted(TARGETS),
        default=None,
        help="scheduler platform (default: launchd on macOS, systemd on Linux)",
    )
    args = parser.parse_args(argv)
    try:
        target_name = args.target
        if target_name is None:
            if sys.platform == "darwin":
                target_name = "launchd"
            elif sys.platform == "linux":
                target_name = "systemd"
            else:
                raise ConfigError(
                    f"no default scheduler target on {sys.platform}; "
                    "pass --target launchd|systemd"
                )
        target = TARGETS[target_name]
        config = load_config(resolve_wiki_root(args.wiki))
        out_dir = args.out
        if out_dir is None:
            if sys.platform != target.host_platform:
                raise ConfigError(
                    f"the default --out ({target.default_out}) only applies "
                    f"where {target_name} is the host scheduler; "
                    "pass --out DIR on this platform"
                )
            out_dir = target.default_out
        out_dir = out_dir.expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        units = render_units(config, target_name)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    unit_paths: list[Path] = []
    for file_name, text in units:
        path = out_dir / file_name
        unit_paths.append(path)
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            print(f"unchanged {path}")
            continue
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    print()
    if target_name == "launchd":
        print("Rendering never loads units. To (re)load on macOS:")
        for path in unit_paths:
            print(f"  launchctl unload {path} 2>/dev/null; launchctl load {path}")
    else:
        print("Rendering never loads units. To (re)load with systemd --user:")
        print("  systemctl --user daemon-reload")
        for path in unit_paths:
            if path.name.endswith(".timer"):
                print(f"  systemctl --user enable --now {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
