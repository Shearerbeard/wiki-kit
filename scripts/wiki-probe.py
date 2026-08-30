#!/usr/bin/env python3
"""Probe each agent harness's view of a docked consumer repo.

For each requested harness, invoke the harness headlessly with cwd at
the consumer repo, ask it to quote the dock's orientation pointer and
list the rendered project skills, and grade the output: PASS iff the
wiki name appears (case-insensitive) AND all but at most one of the
skill names the dock's rendered-skills record lists appear (the
tolerance covers harnesses whose skill listing is capped). Empty
output, a timeout, or a non-zero harness exit
is a FAIL with the reason named. The raw transcript lands in the
consumer's .wiki/probes/ either way - a stall is a failed probe, never
a silent pass.

Probes run on the host, not in CI: the harness CLIs need their local
auth. pi gets --approve only because the probe's whole point is to
exercise project-local files (AGENTS.md, rendered skills); every probe
prompt is read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from wiki_config import (  # noqa: E402
    DOCK_DIR_NAME,
    DOCK_MANIFEST_NAME,
    PROBES_DIR_NAME,
    RENDERED_SKILLS_FILE,
    ConfigError,
)

HARNESSES = ("pi", "opencode", "claude-code", "codex")
# Caller-owned deadline per probe run (seconds).
PROBE_TIMEOUT = 600


class Grade(NamedTuple):
    passed: bool
    reasons: tuple[str, ...]


def grade_output(
    output: str, wiki_name: str, skill_names: tuple[str, ...]
) -> Grade:
    """PASS iff the wiki name appears (case-insensitive) and at most one
    skill name is missing. The one-missing tolerance exists because some
    harnesses cap their project-skill listing."""
    if not output.strip():
        return Grade(False, ("the harness produced no output",))
    lowered = output.lower()
    reasons: list[str] = []
    if wiki_name.lower() not in lowered:
        reasons.append(f"wiki name {wiki_name!r} does not appear")
    missing = [name for name in skill_names if name.lower() not in lowered]
    if len(missing) > 1:
        reasons.append(
            f"only {len(skill_names) - len(missing)} of "
            f"{len(skill_names)} skill names appear "
            f"(missing: {', '.join(missing)})"
        )
    return Grade(not reasons, tuple(reasons))


def harness_command(harness: str, prompt: str) -> list[str]:
    """The headless invocation per harness, verified against each tool's
    --help. pi: -p prints and exits, --approve trusts the project-local
    files the probe exists to exercise. opencode: run is headless, --auto
    approves permissions non-interactively. claude-code: -p is a
    read-only print. codex: exec with a read-only sandbox."""
    commands = {
        "pi": ["pi", "-p", "--approve", prompt],
        "opencode": ["opencode", "run", "--auto", prompt],
        "claude-code": ["claude", "-p", prompt],
        "codex": ["codex", "exec", "--sandbox", "read-only", prompt],
    }
    return commands[harness]


def installed(harness: str) -> bool:
    return shutil.which(harness_command(harness, "")[0]) is not None


def probe_prompt(wiki_name: str, skill_names: tuple[str, ...]) -> str:
    return (
        "This is a read-only check; do not create, edit, or delete any "
        "file. Quote the line in your project orientation that names "
        "this repo's wiki dock. Then list the project skills named "
        f"{', '.join(skill_names)} that you can see - one line each, or "
        "say which are missing."
    )


def load_expectations(repo: Path) -> tuple[str, tuple[str, ...]]:
    """The wiki name from the dock manifest and the skill names from the
    dock's rendered-skills record."""
    manifest_path = repo / DOCK_DIR_NAME / DOCK_MANIFEST_NAME
    if not manifest_path.is_file():
        raise ConfigError(
            f"{manifest_path} not found; probe only a docked consumer "
            "(run wiki-dock install first)"
        )
    with manifest_path.open("rb") as handle:
        manifest = tomllib.load(handle)
    try:
        wiki_name = manifest["dock"]["wiki"]
    except KeyError as exc:
        raise ConfigError(
            f"{manifest_path} is missing [dock].wiki"
        ) from exc
    record_path = repo / DOCK_DIR_NAME / RENDERED_SKILLS_FILE
    if not record_path.is_file():
        raise ConfigError(
            f"{record_path} not found; no skills were rendered into this "
            "repo (run wiki-dock install with --skills-dir)"
        )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    renders = record.get("renders") if isinstance(record, dict) else None
    if not isinstance(renders, dict) or record.get("version") != 1:
        raise ConfigError(
            f"{record_path} is not a version 1 rendered-skills record"
        )
    skill_names = tuple(sorted({Path(rel).parent.name for rel in renders}))
    if not skill_names:
        raise ConfigError(
            f"{record_path} records no rendered skills; reinstall with "
            "--skills-dir"
        )
    return wiki_name, skill_names


def run_harness(harness: str, repo: Path, prompt: str) -> tuple[str, str | None]:
    """Run one probe; return (transcript, failure-reason-or-None). The
    transcript is whatever the harness emitted, failure or not."""
    command = harness_command(harness, prompt)
    # Some harnesses (or the servers they attach to) read $PWD rather
    # than the process cwd; keep the two in agreement so the probe
    # always lands in the consumer repo.
    env = {**os.environ, "PWD": str(repo)}
    try:
        result = subprocess.run(
            command,
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        # On timeout the captured output arrives as bytes even in text
        # mode, and either stream may be None.
        partial = "".join(
            part.decode("utf-8", errors="replace")
            if isinstance(part, bytes)
            else part
            for part in (exc.stdout, exc.stderr)
            if part
        )
        return partial, f"timed out after {PROBE_TIMEOUT}s"
    except FileNotFoundError:
        return "", f"{command[0]} is not installed"
    transcript = result.stdout + result.stderr
    if result.returncode != 0:
        return transcript, f"harness exited with code {result.returncode}"
    return transcript, None


def save_transcript(repo: Path, harness: str, transcript: str) -> Path:
    probes = repo / DOCK_DIR_NAME / PROBES_DIR_NAME
    probes.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = probes / f"{harness}-{stamp}.txt"
    path.write_text(transcript, encoding="utf-8")
    return path


def probe(
    repo: Path,
    harness: str,
    wiki_name: str,
    skill_names: tuple[str, ...],
) -> bool:
    transcript, failure = run_harness(
        harness, repo, probe_prompt(wiki_name, skill_names)
    )
    path = save_transcript(repo, harness, transcript)
    transcript_label = path.relative_to(repo)
    if failure is not None:
        print(f"FAIL {harness} - {failure} (transcript: {transcript_label})")
        return False
    grade = grade_output(transcript, wiki_name, skill_names)
    if not grade.passed:
        print(
            f"FAIL {harness} - {'; '.join(grade.reasons)} "
            f"(transcript: {transcript_label})"
        )
        return False
    print(f"PASS {harness} (transcript: {transcript_label})")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        required=True,
        help="the docked consumer repo to probe",
    )
    parser.add_argument(
        "--harness",
        choices=(*HARNESSES, "all"),
        required=True,
    )
    args = parser.parse_args(argv)
    repo = args.repo.expanduser().resolve()
    try:
        wiki_name, skill_names = load_expectations(repo)
    except ConfigError as exc:
        print(f"wiki-probe: {exc}", file=sys.stderr)
        return 1
    if args.harness == "all":
        # `all` means every supported harness this machine has; an explicit
        # --harness still fails loud when its binary is absent.
        harnesses = tuple(name for name in HARNESSES if installed(name))
        for name in HARNESSES:
            if name not in harnesses:
                print(
                    f"SKIP {name} - {harness_command(name, '')[0]} is not "
                    "installed"
                )
        if not harnesses:
            print("wiki-probe: no supported harness is installed", file=sys.stderr)
            return 1
    else:
        harnesses = (args.harness,)
    results = {
        harness: probe(repo, harness, wiki_name, skill_names)
        for harness in harnesses
    }
    passed = sum(results.values())
    print(f"{passed}/{len(results)} harness(es) passed")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
