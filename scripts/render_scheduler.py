#!/usr/bin/env python3
"""Render per-machine launchd units from the kit's scheduler templates.

launchd cannot expand $HOME or read environment variables, so the three
scheduler units cannot ship as static files: every path must land fully
rendered, per machine. This CLI renders
`templates/launchd/*.plist.template` against a deployment's wiki.toml:
unit labels derive from `[wiki].name` (`com.<name>.wiki-*`), run times
from `[schedule]`, tool binaries from the `[tools]` overlay (falling
back to PATH lookup, failing loud when a binary resolves nowhere), and
unit logs land in `<root>/reports/scheduler-logs`.

It only renders and writes; it never loads a unit. The launchctl
commands to load the rendered files are printed after a render. Writing
is idempotent: a file whose content already matches is left untouched.

The default --out (~/Library/LaunchAgents) is the only macOS-specific
part; any platform can render to an explicit --out.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from wiki_config import (
    KIT_ROOT,
    ConfigError,
    WikiConfig,
    load_config,
    resolve_wiki_root,
)

TEMPLATE_DIR = KIT_ROOT / "templates" / "launchd"

# Unit stem -> (template file, [schedule] key). The label is
# com.<[wiki].name>.wiki-<stem>; the rendered file is <label>.plist.
UNITS = {
    "night-shift": ("night-shift.plist.template", "night"),
    "morning-reminder": ("morning-reminder.plist.template", "morning"),
    "garden-reminder": ("garden-reminder.plist.template", "garden_reminder"),
}

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")


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


def _launchd_path(tools: dict[str, str]) -> str:
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


def render_units(config: WikiConfig) -> list[tuple[str, str]]:
    """Render all three units; returns (file name, content) pairs."""
    tools = {
        "uv": _resolve_tool(config, "uv", "uv"),
        "notifier": _resolve_tool(config, "notifier", "terminal-notifier"),
        "git": _resolve_tool(config, "git", "git"),
    }
    log_dir = config.root / "reports" / "scheduler-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    schedule_times = {
        "night": config.schedule.night,
        "morning": config.schedule.morning,
        "garden_reminder": config.schedule.garden_reminder,
    }
    rendered: list[tuple[str, str]] = []
    for stem, (template_name, schedule_key) in UNITS.items():
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
            "LOG_DIR": str(log_dir),
            "PATH": _launchd_path(tools),
            "NIGHT_REPORT_DIR": config.night.report_dir,
            "NIGHT_COMMIT_PREFIX": config.night.commit_prefix,
        }
        template_path = TEMPLATE_DIR / template_name
        text = template_path.read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", escape(value))
        leftover = sorted(set(PLACEHOLDER_RE.findall(text)))
        if leftover:
            raise ConfigError(
                f"{template_path} has unrendered placeholders: {leftover}"
            )
        rendered.append((f"{label}.plist", text))
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
        help="output directory (default: ~/Library/LaunchAgents, macOS only)",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(resolve_wiki_root(args.wiki))
        out_dir = args.out
        if out_dir is None:
            if sys.platform != "darwin":
                raise ConfigError(
                    "the default --out (~/Library/LaunchAgents) is macOS-only; "
                    "pass --out DIR on this platform"
                )
            out_dir = Path.home() / "Library" / "LaunchAgents"
        out_dir = out_dir.expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        units = render_units(config)
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
    print("Rendering never loads units. To (re)load on macOS:")
    for path in unit_paths:
        print(f"  launchctl unload {path} 2>/dev/null; launchctl load {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
