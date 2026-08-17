#!/usr/bin/env python3
"""Working-tree integrity doctor for a wiki-kit deployment.

Every check reads the deployment's own `wiki.toml` (through
wiki_config) as its source of truth; the doctor never carries a private
copy of the contract. What the doctor does and does not cover is stated
in the kit's docs/enforcement-contract.md.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import unquote

SCRIPT_DIR = Path(__file__).resolve().parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from wiki_config import (  # noqa: E402
    ConfigError,
    WikiConfig,
    contract_deny_rules,
    git_hooks_dir,
    load_config,
    machine_path_violations,
    resolve_wiki_root,
)
from wiki_event import (  # noqa: E402
    ValidationError,
    load_json,
    pending_mismatch,
    sha256_file,
    validate_artifact,
)
from wiki_frontmatter import (  # noqa: E402
    FrontmatterError,
    format_frontmatter,
    parse_frontmatter,
    parse_workstream_file,
    validate_frontmatter,
    validate_workstream_body,
)
from wiki_render import (  # noqa: E402
    CLAUDE_LOCAL_TOKEN_BUDGET,
    render_log,
)

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]\n]+\]\(([^)]+)\)")


class Severity(StrEnum):
    FAIL = "fail"
    WARN = "warn"


@dataclass(frozen=True)
class Finding:
    check: str
    severity: Severity
    message: str
    path: str | None = None


@dataclass(frozen=True)
class CheckOutcome:
    name: str
    findings: tuple[Finding, ...]
    summary: str = ""

    @property
    def failed(self) -> bool:
        return any(finding.severity is Severity.FAIL for finding in self.findings)

    @property
    def warned(self) -> bool:
        return any(finding.severity is Severity.WARN for finding in self.findings)


@dataclass(frozen=True)
class DoctorContext:
    config: WikiConfig
    projects_root: Path

    @property
    def repo_root(self) -> Path:
        return self.config.root

    @property
    def memory_index(self) -> Path:
        """The wiki's own per-project memory index (preferences only;
        durable state belongs in the wiki itself)."""
        slug = self.config.project_slug(self.config.root)
        return self.projects_root / slug / "memory" / "MEMORY.md"


@dataclass(frozen=True)
class Budget:
    label: str
    warn: int
    hard: int


TOKEN_BUDGETS = {
    "claude-local": Budget(
        "CLAUDE.local.md", warn=2_000, hard=CLAUDE_LOCAL_TOKEN_BUDGET
    ),
    "memory-index": Budget("MEMORY.md (per project)", warn=1_500, hard=2_000),
    "workstream": Budget("workstream file", warn=2_500, hard=4_000),
    "entity": Budget("entity page", warn=2_000, hard=3_500),
}


def repo_relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def fail(check: str, message: str, path: Path | str | None = None) -> Finding:
    return Finding(
        check, Severity.FAIL, message, str(path) if path is not None else None
    )


def warn(check: str, message: str, path: Path | str | None = None) -> Finding:
    return Finding(
        check, Severity.WARN, message, str(path) if path is not None else None
    )


def outcome(name: str, findings: Iterable[Finding], summary: str = "") -> CheckOutcome:
    return CheckOutcome(name, tuple(findings), summary)


def workstream_validation_files(root: Path) -> list[Path]:
    """The one workstream-validation scope (matches validate-workstreams.py):
    everything under workstreams/ including _archive/, excluding
    _reference/ (free-form reference pages), index.md, and lock
    artifacts."""
    workstreams = root / "workstreams"
    return sorted(
        path
        for path in workstreams.glob("**/*.md")
        if ".garden.lock" not in path.parts
        and "_reference" not in path.parts
        and path.name != "index.md"
    )


def source_markdown_files(root: Path) -> list[Path]:
    patterns = (
        "README.md",
        "CLAUDE.md",
        "DECISIONS.md",
        "wiki/index.md",
        "wiki/entities/**/*.md",
    )
    files: set[Path] = set()
    for pattern in patterns:
        files.update(root.glob(pattern))
    return sorted(path for path in files if path.is_file())


def check_config(ctx: DoctorContext) -> CheckOutcome:
    """wiki.toml is loadable (the loader enforces the overlay allowlist)
    and the committed file carries no machine paths (the wiki-repo mirror
    of the dock's RULE-2)."""
    name = "config"
    findings = [
        fail(
            name,
            f"committed wiki.toml carries a machine path: {violation} "
            "(machine facts belong in wiki.local.toml; "
            "[memory].index_line is the one display-text exemption)",
            ctx.repo_root / "wiki.toml",
        )
        for violation in machine_path_violations(ctx.repo_root)
    ]
    companions = len(ctx.config.companions)
    return outcome(
        name,
        findings,
        f"config loads; {companions} companion(s) configured",
    )


def _optional(path: Path) -> Path | None:
    return path if path.exists() else None


def check_render_log(ctx: DoctorContext) -> CheckOutcome:
    name = "render-log"
    root = ctx.repo_root
    try:
        rendered = render_log(
            events_dir=root / "wiki" / "events",
            epoch_path=_optional(root / "wiki" / "log-epoch.json"),
            legacy_path=_optional(root / "wiki" / "log-legacy.md"),
            quarantine_path=root / "wiki" / "quarantine.json",
        )
        current = (root / "wiki" / "log.md").read_text(encoding="utf-8")
    except (OSError, ValueError, KeyError, ValidationError) as exc:
        return outcome(name, [fail(name, str(exc))])
    if rendered != current:
        return outcome(
            name,
            [fail(name, "wiki/log.md differs; run the kit renderer (log)")],
        )
    return outcome(name, (), "wiki/log.md matches renderer")


def check_validate_workstreams(ctx: DoctorContext) -> CheckOutcome:
    name = "validate-workstreams"
    findings: list[Finding] = []
    files = workstream_validation_files(ctx.repo_root)
    for path in files:
        try:
            frontmatter, body = parse_workstream_file(path)
        except FrontmatterError as exc:
            findings.append(fail(name, str(exc), repo_relative(path, ctx.repo_root)))
            continue
        for error in validate_frontmatter(frontmatter, path.name):
            findings.append(fail(name, error, repo_relative(path, ctx.repo_root)))
        for error in validate_workstream_body(frontmatter, body, path.name):
            findings.append(fail(name, error, repo_relative(path, ctx.repo_root)))
    return outcome(name, findings, f"{len(files)} workstream files checked")


def check_frontmatter_roundtrip(ctx: DoctorContext) -> CheckOutcome:
    name = "frontmatter-roundtrip"
    findings: list[Finding] = []
    files = workstream_validation_files(ctx.repo_root)
    for path in files:
        text = path.read_text(encoding="utf-8")
        try:
            frontmatter, body = parse_frontmatter(text)
            canonical = format_frontmatter(frontmatter) + body
        except FrontmatterError as exc:
            findings.append(fail(name, str(exc), repo_relative(path, ctx.repo_root)))
            continue
        if canonical != text:
            findings.append(
                fail(
                    name,
                    "frontmatter is not in canonical writer form",
                    repo_relative(path, ctx.repo_root),
                )
            )
    return outcome(name, findings, f"{len(files)} workstream files checked")


GH_CACHE = "1h"


def check_repo_names(ctx: DoctorContext) -> CheckOutcome:
    name = "repo-names"
    findings: list[Finding] = []
    seen: set[str] = set()
    configured = sorted(
        companion.github
        for companion in ctx.config.companions.values()
        if companion.github
    )
    gh_missing = False
    for path in workstream_validation_files(ctx.repo_root):
        try:
            fm, _ = parse_workstream_file(path)
        except FrontmatterError:
            continue  # check_validate_workstreams already reports this file
        repo = fm.get("repo", "").strip()
        if not repo or repo in seen:
            continue
        seen.add(repo)
        try:
            result = subprocess.run(
                ["gh", "api", "--cache", GH_CACHE, f"repos/{repo}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            gh_missing = True
            break
        if result.returncode == 0:
            continue
        rel = repo_relative(path, ctx.repo_root)
        if "HTTP 404" in result.stderr:
            hint = (
                f"; configured companion repos: {', '.join(configured)}"
                if configured
                else ""
            )
            findings.append(
                fail(
                    name,
                    f"repo '{repo}' does not exist on GitHub (used by {rel})"
                    f"{hint}",
                    rel,
                )
            )
        else:
            findings.append(
                warn(
                    name,
                    f"could not verify repo '{repo}': {result.stderr.strip()}",
                    rel,
                )
            )
    if gh_missing:
        findings.append(
            warn(name, "gh is not installed; repo names not verified")
        )
    return outcome(name, findings, f"{len(seen)} repo(s) checked")


def check_pending_index(ctx: DoctorContext) -> CheckOutcome:
    name = "pending-index"
    root = ctx.repo_root
    try:
        mismatches = pending_mismatch(
            events_dir=root / "wiki" / "events",
            sources_dir=root / "wiki" / "sources",
            index_path=root / "wiki" / "pending" / "index.json",
            latest_path=root / "wiki" / "pending" / "latest.md",
        )
    except (OSError, ValueError, KeyError, ValidationError) as exc:
        return outcome(name, [fail(name, str(exc))])
    findings = [
        fail(name, f"{mismatch}; run the pending builder") for mismatch in mismatches
    ]
    return outcome(name, findings, "pending projection checked")


def estimated_tokens(path: Path) -> int:
    return (len(path.read_bytes()) + 3) // 4


def budget_finding(
    name: str, path: Path, budget: Budget, tokens: int
) -> Finding | None:
    message = (
        f"{budget.label} estimates {tokens} tokens; "
        f"warn={budget.warn}, hard={budget.hard}"
    )
    if tokens > budget.hard:
        return fail(name, message, str(path))
    if tokens > budget.warn:
        return warn(name, message, str(path))
    return None


def check_token_budgets(ctx: DoctorContext) -> CheckOutcome:
    name = "token-budgets"
    findings: list[Finding] = []
    surfaces: list[tuple[Path, Budget]] = []
    orientation = ctx.repo_root / "CLAUDE.local.md"
    if orientation.exists():
        surfaces.append((orientation, TOKEN_BUDGETS["claude-local"]))
    # The memory index is optional harness state: budget it when present,
    # say nothing when a deployment has none.
    if ctx.memory_index.exists():
        surfaces.append((ctx.memory_index, TOKEN_BUDGETS["memory-index"]))
    surfaces.extend(
        (path, TOKEN_BUDGETS["workstream"])
        for path in workstream_validation_files(ctx.repo_root)
    )
    surfaces.extend(
        (path, TOKEN_BUDGETS["entity"])
        for path in sorted((ctx.repo_root / "wiki" / "entities").glob("*.md"))
    )
    for path, budget in surfaces:
        finding = budget_finding(name, path, budget, estimated_tokens(path))
        if finding is not None:
            findings.append(finding)
    return outcome(name, findings, f"{len(surfaces)} surfaces checked")


def is_external_link(target: str) -> bool:
    return "://" in target or target.startswith(("mailto:", "tel:", "git@"))


def clean_markdown_target(raw: str) -> str:
    target = raw.strip().strip("<>")
    if " " in target:
        target = target.split(" ", 1)[0]
    return unquote(target.split("#", 1)[0])


def check_links(ctx: DoctorContext) -> CheckOutcome:
    name = "links"
    findings: list[Finding] = []
    files = source_markdown_files(ctx.repo_root)
    for path in files:
        text = path.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK_RE.finditer(text):
            target = clean_markdown_target(match.group(1))
            if not target or target.startswith("#") or is_external_link(target):
                continue
            resolved = (
                ctx.repo_root / target.removeprefix("/")
                if target.startswith("/")
                else path.parent / target
            ).resolve()
            if not resolved.exists():
                findings.append(
                    fail(
                        name,
                        f"broken markdown link: {match.group(1)!r}",
                        repo_relative(path, ctx.repo_root),
                    )
                )
    return outcome(name, findings, f"{len(files)} markdown files checked")


def check_install(ctx: DoctorContext) -> CheckOutcome:
    """Installer-owned state: the pre-commit hook wrapper and the
    [contract]-derived Claude deny rules."""
    name = "install"
    findings: list[Finding] = []
    root = ctx.repo_root
    try:
        hook = git_hooks_dir(root) / "pre-commit"
        expected = SCRIPT_DIR / "pre-commit"
        if not hook.is_file():
            findings.append(
                fail(
                    name,
                    "pre-commit hook is not installed; run the kit installer",
                    hook,
                )
            )
        elif str(expected) not in hook.read_text(encoding="utf-8"):
            findings.append(
                fail(
                    name,
                    f"pre-commit hook does not exec the kit's {expected}",
                    hook,
                )
            )
        settings_path = root / ".claude" / "settings.json"
        settings = (
            json.loads(settings_path.read_text(encoding="utf-8"))
            if settings_path.exists()
            else {}
        )
        deny = settings.get("permissions", {}).get("deny", [])
        for rule in contract_deny_rules(ctx.config):
            if rule not in deny:
                findings.append(
                    fail(
                        name,
                        f"missing Claude deny rule from [contract]: {rule}",
                        settings_path,
                    )
                )
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        findings.append(fail(name, str(exc)))
    return outcome(name, findings, "hook and contract-derived deny rules checked")


def check_captures(ctx: DoctorContext) -> CheckOutcome:
    """Periodic capture audit (recon 03 gap: sha256 checks fired only on
    write collisions): every committed capture manifest validates, and
    every captured file still hashes to its manifest entry."""
    name = "captures"
    findings: list[Finding] = []
    root = ctx.repo_root
    manifests = sorted((root / "wiki" / "sources").glob("**/manifest.json"))
    entries = 0
    for manifest_path in manifests:
        rel = repo_relative(manifest_path, root)
        try:
            manifest = load_json(manifest_path)
            validate_artifact(manifest, "capture-manifest.schema.json", rel)
        except Exception as exc:  # noqa: BLE001 - report, keep auditing
            findings.append(fail(name, f"manifest invalid: {exc}", rel))
            continue
        for capture in manifest["captures"]:
            entries += 1
            captured = root / capture["captured_path"]
            if not captured.is_file():
                findings.append(
                    fail(
                        name,
                        f"captured file missing: {capture['captured_path']}",
                        rel,
                    )
                )
                continue
            digest = sha256_file(captured)
            if digest != capture["sha256"]:
                findings.append(
                    fail(
                        name,
                        f"sha256 mismatch for {capture['captured_path']}: "
                        f"manifest {capture['sha256']}, file {digest}",
                        rel,
                    )
                )
            if captured.stat().st_size != capture["size_bytes"]:
                findings.append(
                    fail(
                        name,
                        f"size mismatch for {capture['captured_path']}",
                        rel,
                    )
                )
    return outcome(
        name,
        findings,
        f"{len(manifests)} manifest(s), {entries} capture(s) audited",
    )


def board_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line.removeprefix("## ").strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def card_links(lines: Sequence[str]) -> list[str]:
    links = []
    for line in lines:
        links.extend(re.findall(r"\[([^\]]+)\]\(cards/([^\)]+)\)", line))
    return [path for _label, path in links]


def frontmatter_value(text: str, key: str) -> str | None:
    if not text.startswith("---"):
        return None
    try:
        block = text.split("---", 2)[1]
    except IndexError:
        return None
    prefix = f"{key}:"
    for line in block.splitlines():
        if line.startswith(prefix):
            return line.partition(":")[2].strip()
    return None


def checkbox_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if re.match(r"^- \[[ xX]\]", line)]


def log_lines(text: str) -> list[str]:
    return [
        line for line in text.splitlines() if re.match(r"^- \d{4}-\d{2}-\d{2}:", line)
    ]


def check_board(ctx: DoctorContext) -> CheckOutcome:
    """A deployment MAY run a planning board at planning/board.md; the
    check is structural and skips cleanly when none exists."""
    name = "board"
    board = ctx.repo_root / "planning" / "board.md"
    if not board.exists():
        return outcome(name, (), "no planning board (optional)")
    text = board.read_text(encoding="utf-8")
    sections = board_sections(text)
    findings: list[Finding] = []
    for section in ("In progress", "Ready", "Done"):
        if section not in sections:
            findings.append(fail(name, f"missing board section {section!r}", board))
    in_progress_cards = card_links(sections.get("In progress", []))
    done_cards = card_links(sections.get("Done", []))
    for card in in_progress_cards:
        path = ctx.repo_root / "planning" / "cards" / card
        if not path.exists():
            findings.append(fail(name, "In-progress card link is broken", path))
            continue
        card_text = path.read_text(encoding="utf-8")
        owner = frontmatter_value(card_text, "owner")
        if owner in (None, "(unclaimed)"):
            findings.append(fail(name, "In-progress card has no owner", path))
        if not log_lines(card_text):
            findings.append(fail(name, "In-progress card has no Log lines", path))
    for card in done_cards:
        path = ctx.repo_root / "planning" / "cards" / card
        if not path.exists():
            findings.append(fail(name, "Done card link is broken", path))
            continue
        unchecked = [
            line
            for line in checkbox_lines(path.read_text(encoding="utf-8"))
            if "[ ]" in line
        ]
        if unchecked:
            findings.append(fail(name, "Done card has unchecked gates", path))
    return outcome(
        name,
        findings,
        f"{len(in_progress_cards)} in-progress, {len(done_cards)} done card(s)",
    )


Check = Callable[[DoctorContext], CheckOutcome]


CHECKS: tuple[Check, ...] = (
    check_config,
    check_render_log,
    check_validate_workstreams,
    check_frontmatter_roundtrip,
    check_repo_names,
    check_pending_index,
    check_token_budgets,
    check_links,
    check_install,
    check_captures,
    check_board,
)


def run_checks(ctx: DoctorContext) -> list[CheckOutcome]:
    return [check(ctx) for check in CHECKS]


def print_text(outcomes: Sequence[CheckOutcome]) -> None:
    for result in outcomes:
        if result.failed:
            status = "FAIL"
        elif result.warned:
            status = "WARN"
        else:
            status = "PASS"
        detail = f" — {result.summary}" if result.summary else ""
        print(f"{status} {result.name}{detail}")
        for finding in result.findings:
            path = f" [{finding.path}]" if finding.path else ""
            print(f"  {finding.severity.upper()}: {finding.message}{path}")


def json_outcomes(outcomes: Sequence[CheckOutcome]) -> list[dict[str, Any]]:
    return [
        {
            "name": result.name,
            "summary": result.summary,
            "failed": result.failed,
            "warned": result.warned,
            "findings": [finding.__dict__ for finding in result.findings],
        }
        for result in outcomes
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki", type=Path, default=None)
    parser.add_argument("--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT)
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable results"
    )
    parser.add_argument(
        "--strict-warnings",
        action="store_true",
        help="exit non-zero when any warning is present",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = resolve_wiki_root(args.wiki)
        config = load_config(root)
    except ConfigError as exc:
        print(f"FAIL config — {exc}", file=sys.stderr)
        return 1
    ctx = DoctorContext(config=config, projects_root=args.projects_root.resolve())
    outcomes = run_checks(ctx)
    if args.json:
        print(json.dumps(json_outcomes(outcomes), indent=2))
    else:
        print_text(outcomes)
    has_failure = any(result.failed for result in outcomes)
    has_warning = any(result.warned for result in outcomes)
    return 1 if has_failure or (args.strict_warnings and has_warning) else 0


if __name__ == "__main__":
    sys.exit(main())
