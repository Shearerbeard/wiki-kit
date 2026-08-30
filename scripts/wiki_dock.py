#!/usr/bin/env python3
"""Dock a consumer repo to a wiki: install, complete, status.

A dock is the consumer's `.wiki/` directory: a trackable identity
manifest plus a machine-local overlay (the ratified contract is the
kit's docs/docking-spec.md). This CLI is the only writer of
consumer-side docks:

- install: write the manifest and overlay, apply the posture's ignore
  mechanics, render the consumer orientation (.wiki/orientation.md),
  append the marker-delimited dock block to AGENTS.md (plus the
  CLAUDE.md shim), and render the generated wiring (post-commit hook,
  opencode handoff plugin) for companions with an outbox subpath. With
  --skills-dir, also render the contracted workflow skills
  ([contract].skills) into the chosen repo-relative directories.
- complete: create or update the overlay's [dock].path for an existing
  manifest - the command the resolver's fail-loud message names - and
  re-render the orientation.
- status: report what the resolver sees at a repo, read-only. Exit 1
  when a dock exists but cannot resolve (incomplete overlay, broken
  path, identity mismatch); an undocked repo reports cleanly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from wiki_config import (  # noqa: E402
    DOCK_DIR_NAME,
    DOCK_MANIFEST_NAME,
    DOCK_OVERLAY_NAME,
    KIT_ROOT,
    POSTURES,
    RENDERED_SKILLS_FILE,
    Companion,
    ConfigError,
    dock_complete_command,
    git_hooks_dir,
    load_config,
    load_dock,
    verify_dock_identity,
)

KIT_SCRIPTS = KIT_ROOT / "scripts"
HANDOFF_PLUGIN_TEMPLATE = KIT_ROOT / "templates" / "handoff.ts.template"

# Rendered-skill provenance: the kit-owned record of what it wrote at
# each skill path (sha256), kept INSIDE the dock - not beside the skill,
# where it was both an unchecked write path and forgeable by the same
# actor editing the skill. The rendered content embeds the machine-local
# [tools].kit path, so this record is machine-local like the overlay.

# The consumer orientation: rendered from templates/orientation.md.template
# into the dock, never committed in any posture (it embeds the
# machine-local wiki root), so it rides the same ignore lines as the
# overlay.
ORIENTATION_NAME = "orientation.md"
ORIENTATION_TEMPLATE = KIT_ROOT / "templates" / "orientation.md.template"

# Harness entry wiring: AGENTS.md carries the dock block for the
# AGENTS.md-reading harnesses; CLAUDE.md gets a one-line shim at it
# (the boardkit shim convention) for claude-code. The block region
# between the markers is kit-owned: reinstall replaces it wholesale and
# never touches text outside the markers.
AGENTS_FILE = "AGENTS.md"
CLAUDE_FILE = "CLAUDE.md"
DOCK_BLOCK_START = "<!-- wiki-kit:dock:start -->"
DOCK_BLOCK_END = "<!-- wiki-kit:dock:end -->"
CLAUDE_SHIM_LINE = (
    "Read `AGENTS.md` first; it is the stable agent handoff for this repo."
)
# claude-code's file-import syntax: the other way a CLAUDE.md points at
# AGENTS.md.
CLAUDE_IMPORT_LINE = "@AGENTS.md"

# The ignore lines each posture applies: committed tracks the manifest
# and ignores only the machine-local files (overlay, skill provenance,
# orientation); gitignored and invisible ignore the whole dock (tracked
# .gitignore vs the per-clone exclude file). Generated wiring joins
# these lines: the handoff plugin and the harness entry shims for the
# untracked postures, the rendered skill dirs for every posture (they
# embed the machine-local kit path).
IGNORE_LINES = {
    "committed": (
        f"{DOCK_DIR_NAME}/{DOCK_OVERLAY_NAME}",
        f"{DOCK_DIR_NAME}/{RENDERED_SKILLS_FILE}",
        f"{DOCK_DIR_NAME}/{ORIENTATION_NAME}",
    ),
    "gitignored": (f"{DOCK_DIR_NAME}/",),
    "invisible": (f"{DOCK_DIR_NAME}/",),
}

PLUGIN_REPO_PATH = ".opencode/plugins/handoff.ts"
PLUGIN_MARKER = "OpenCode session.idle handoff plugin"

# The kit's canonical skills (`.agents/skills/<name>/SKILL.md`) ARE the
# parameterized templates: dock install renders {{KIT_ROOT}} from the
# deployment overlay's [tools].kit and lands each contracted skill in the
# consumer-chosen target directories.
SKILLS_TEMPLATE_DIR = KIT_ROOT / ".agents" / "skills"
# Any {{...}} span is a placeholder: a token in an unexpected case or
# charset is a template bug and must fail loud, never survive rendering.
SKILL_PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}")
# Every kit-rendered skill carries this comment in its frontmatter. It
# is not proof of provenance (the dock's rendered-skills.json is); it
# only distinguishes "looks like a kit render" from a plainly foreign
# file when the provenance record cannot vouch for the file.
SKILL_MARKER = "Rendered from the wiki-kit template"

POST_COMMIT_MARKER = "# wiki-kit post-commit wrapper"


class DockError(Exception):
    pass


def note(message: str) -> None:
    print(f"  {message}")


def _require_wiki_root(wiki: Path) -> Path:
    root = wiki.expanduser().resolve()
    if not (root / "wiki.toml").is_file():
        raise DockError(f"--wiki {root} does not contain wiki.toml")
    return root


def manifest_text(wiki_name: str, companion: str) -> str:
    return (
        "# Dock identity manifest (docking spec): identity only, no\n"
        "# machine paths. The machine-local overlay lives in local.toml\n"
        "# beside this file and is never committed.\n"
        "[dock]\n"
        f"wiki = {json.dumps(wiki_name)}\n"
        f"companion = {json.dumps(companion)}\n"
    )


def overlay_text(wiki_root: Path) -> str:
    return (
        "# Machine-local dock overlay: never committed; the posture's\n"
        "# ignore mechanism covers it. [dock].path is the one allowlisted\n"
        "# key.\n"
        "[dock]\n"
        f"path = {json.dumps(str(wiki_root))}\n"
    )


def _check_manifest_slot(dock_dir: Path, wiki_name: str, companion: str) -> None:
    """The manifest is the dock's identity: a conflict with what is
    already docked here fails loud rather than re-pointing silently."""
    path = dock_dir / DOCK_MANIFEST_NAME
    if not path.exists():
        return
    current = path.read_text(encoding="utf-8")
    if current != manifest_text(wiki_name, companion):
        raise DockError(
            f"{path} already docks this repo to a different wiki or "
            f"companion; inspect it and resolve by hand before "
            f"reinstalling:\n{current}"
        )


def write_manifest(dock_dir: Path, wiki_name: str, companion: str) -> None:
    path = dock_dir / DOCK_MANIFEST_NAME
    _check_manifest_slot(dock_dir, wiki_name, companion)
    if path.exists():
        note("✓ manifest up to date")
        return
    dock_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest_text(wiki_name, companion), encoding="utf-8")
    note("✓ manifest written")


def write_overlay(dock_dir: Path, wiki_root: Path) -> None:
    """The overlay is machine-local: writing the path this machine
    resolves through is the command's purpose, create or update."""
    path = dock_dir / DOCK_OVERLAY_NAME
    content = overlay_text(wiki_root)
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current == content:
            note("✓ overlay up to date")
            return
        path.write_text(content, encoding="utf-8")
        note(f"✓ overlay re-pointed at {wiki_root}")
        return
    dock_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    note("✓ overlay written")


def _git_exclude_file(repo: Path) -> Path:
    """The repo's per-clone exclude file, resolved by git: in a linked
    worktree .git is a file, and git knows where the shared info/ dir
    lives."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DockError(
            f"{repo} is not a git repository: {result.stderr.strip()}"
        )
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else repo / path


def apply_posture(
    repo: Path,
    posture: str,
    wiring_written: bool,
    skill_dirs: tuple[str, ...] = (),
    shim_paths: tuple[str, ...] = (),
) -> None:
    lines = list(IGNORE_LINES[posture])
    if wiring_written and posture != "committed":
        # Generated shims follow the posture (docking spec): tracked
        # when committed, covered by the same exclusion set otherwise.
        lines.append(PLUGIN_REPO_PATH)
    # The harness entry shims (AGENTS.md block, CLAUDE.md shim) follow
    # the posture the same way: committed posture means the consumer
    # commits them; the untracked postures exclude them so nothing
    # wiki-related surfaces in git status.
    if posture != "committed":
        lines.extend(shim_paths)
    # Rendered skills are generated wiring in EVERY posture: they embed
    # the machine-local [tools].kit path, so a tracked render would put
    # a machine path in shared history, and a fresh clone (which has no
    # .wiki/rendered-skills.json) could never verify it. Committed
    # posture tracks only the dock manifest itself.
    lines.extend(f"{directory}/" for directory in skill_dirs)
    if posture == "invisible":
        path = _git_exclude_file(repo)
        label = "info/exclude"
    else:
        path = repo / ".gitignore"
        label = ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    have = existing.splitlines()
    missing = [line for line in lines if line not in have]
    if not missing:
        note(f"✓ {label} already covers the posture ({posture})")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    text = existing
    if text and not text.endswith("\n"):
        text += "\n"
    text += "\n".join(missing) + "\n"
    path.write_text(text, encoding="utf-8")
    note(f"✓ {label} gained: {', '.join(missing)} ({posture} posture)")


def post_commit_wrapper_text(wiki_root: Path, docs_subpath: str) -> str:
    """A generated wrapper, not a copy: it renders the env the kit's
    post-commit requires and exec's the kit's CURRENT script, so kit
    updates apply without a reinstall. .git/hooks is machine-local, so
    the baked paths are legal."""
    return (
        "#!/bin/sh\n"
        f"{POST_COMMIT_MARKER}\n"
        f"WIKI_KIT_ROOT={shlex.quote(str(KIT_ROOT))}\n"
        f"WIKI_ROOT={shlex.quote(str(wiki_root))}\n"
        f"WIKI_DOCS_SUBPATH={shlex.quote(docs_subpath)}\n"
        "export WIKI_KIT_ROOT WIKI_ROOT WIKI_DOCS_SUBPATH\n"
        f'exec {shlex.quote(str(KIT_SCRIPTS / "post-commit"))} "$@"\n'
    )


def _check_hook_slot(dest: Path) -> None:
    """A foreign hook is never clobbered: the conflict fails loud, and
    the preflight ordering means it fails before any write lands."""
    if dest.exists() and not dest.is_file():
        raise DockError(f"post-commit hook path is not a file: {dest}")
    if (dest.exists() or dest.is_symlink()) and dest.is_file():
        current = dest.read_text(encoding="utf-8")
        if POST_COMMIT_MARKER not in current:
            raise DockError(
                f"post-commit hook exists and is not a kit wrapper: "
                f"{dest}; resolve it by hand before reinstalling"
            )


def install_post_commit_hook(
    repo: Path, wiki_root: Path, docs_subpath: str
) -> None:
    dest = git_hooks_dir(repo) / "post-commit"
    _check_hook_slot(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    wrapper = post_commit_wrapper_text(wiki_root, docs_subpath)
    if dest.exists() and dest.read_text(encoding="utf-8") == wrapper:
        note("✓ post-commit hook wrapper up to date")
        return
    # An existing slot holds a kit wrapper from another wiki or kit
    # location (the slot check passed): rewrite.
    dest.write_text(wrapper, encoding="utf-8")
    dest.chmod(0o755)
    note("✓ post-commit hook wrapper installed")


def render_handoff_plugin(repo: Path, docs_subpath: str) -> None:
    dest = repo / PLUGIN_REPO_PATH
    content = HANDOFF_PLUGIN_TEMPLATE.read_text(encoding="utf-8").replace(
        "{{DOCS_SUBPATH}}", docs_subpath
    )
    if dest.exists():
        current = dest.read_text(encoding="utf-8")
        if current == content:
            note("✓ opencode handoff plugin up to date")
            return
        if PLUGIN_MARKER not in current:
            note(
                "✓ opencode handoff plugin exists and is not a kit "
                "render, left in place"
            )
            return
        # A kit render from another docs_subpath: rewrite so the plugin
        # and the hook wrapper never point at different outboxes.
        dest.write_text(content, encoding="utf-8")
        note("✓ opencode handoff plugin re-rendered")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    note("✓ opencode handoff plugin rendered")


def resolve_skill_targets(repo: Path, raw_targets: list[str]) -> list[Path]:
    """Skill renders are project-scoped only (ruling D12): each target is
    a repo-relative directory. Machine-global paths (~/.agents/skills,
    ~/.claude/skills) are forbidden before adoption and refused here -
    anything absolute or escaping the repo is one of those in disguise."""
    targets: list[Path] = []
    for raw in raw_targets:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            raise DockError(
                f"--skills-dir {raw} is not repo-relative; skill renders "
                "are project-scoped only and machine-global paths "
                "(~/.agents/skills, ~/.claude/skills) are forbidden "
                "before adoption"
            )
        resolved = (repo / candidate).resolve()
        if resolved != repo and repo not in resolved.parents:
            raise DockError(
                f"--skills-dir {raw} escapes the consumer repo; skill "
                "renders are project-scoped only and machine-global "
                "paths are forbidden before adoption"
            )
        if resolved not in targets:
            targets.append(resolved)
    return targets


def prepare_skill_renders(
    skills: tuple[str, ...], kit_root: str
) -> dict[str, str]:
    """Preflight: read and FULLY render every contracted skill template
    before any dock wiring or skill write happens, so a template bug
    (missing file, leftover placeholder in any case or charset) fails
    loud with nothing half-applied."""
    rendered: dict[str, str] = {}
    for name in skills:
        template_path = SKILLS_TEMPLATE_DIR / name / "SKILL.md"
        if not template_path.is_file():
            raise DockError(
                f"wiki.toml [contract].skills lists {name!r} but the kit "
                f"ships no template at {template_path}"
            )
        rendered[name] = render_template(
            template_path, {"KIT_ROOT": kit_root}
        )
    return rendered


def check_skill_paths(
    repo: Path,
    skills: tuple[str, ...],
    targets: list[Path],
    manifest_path: Path,
) -> None:
    """Preflight the write set against the repo boundary: a symlinked
    skill directory, SKILL.md, or provenance manifest must not smuggle a
    write outside the consumer repo."""
    _require_within_repo(repo, manifest_path)
    for target in targets:
        for name in skills:
            _require_within_repo(repo, target / name / "SKILL.md")


def _require_within_repo(repo: Path, path: Path) -> None:
    """Resolve through the deepest existing ancestor (symlinks included)
    and refuse anything that lands outside the consumer repo root."""
    existing = path
    while not existing.exists() and not existing.is_symlink():
        parent = existing.parent
        if parent == existing:
            raise DockError(f"cannot resolve {path} within {repo}")
        existing = parent
    resolved = existing.resolve()
    if resolved != repo and repo not in resolved.parents:
        raise DockError(
            f"skill render path {path} resolves to {resolved}, outside "
            f"the consumer repo {repo}; refusing to write across the "
            "repo boundary"
        )


def _load_render_manifest(path: Path) -> tuple[dict[str, str], bool]:
    """The dock's provenance record: repo-relative skill path -> sha256
    of the content the kit wrote there. Missing is normal before the
    first render; an unparseable or wrong-shaped file is corrupt - the
    caller notes it loudly and treats every render as unprovenanced."""
    if not path.exists():
        return {}, True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}, False
    renders = data.get("renders") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(renders, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in renders.items()
        )
    ):
        return {}, False
    return dict(renders), True


def _write_render_manifest(path: Path, entries: dict[str, str]) -> None:
    text = (
        json.dumps(
            {"version": 1, "renders": entries}, indent=2, sort_keys=True
        )
        + "\n"
    )
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def render_skills(
    rendered: dict[str, str],
    targets: list[Path],
    repo: Path,
    dock_dir: Path,
) -> None:
    """Write the prepared renders into every chosen target and record
    each write's digest in the dock's provenance manifest. Byte-identical
    is "up to date"; a file the manifest vouches for (digest match) is a
    pristine older kit render and updates in place; anything else -
    hand-edited, foreign, or unprovenanced - is left alone, never
    silently rewritten."""
    manifest_path = dock_dir / RENDERED_SKILLS_FILE
    entries, healthy = _load_render_manifest(manifest_path)
    if not healthy:
        note(
            f"! {manifest_path} is corrupt; no render can be verified "
            "against it. Every existing skill file is left in place "
            "until it verifies or is removed"
        )
    updated = dict(entries)
    for name, content in rendered.items():
        content_bytes = content.encode("utf-8")
        digest = hashlib.sha256(content_bytes).hexdigest()
        for target in targets:
            dest = target / name / "SKILL.md"
            rel = dest.relative_to(repo).as_posix()
            if dest.exists():
                current = dest.read_bytes()
                if current == content_bytes:
                    updated[rel] = digest
                    note(f"✓ skill {name!r} up to date at {dest}")
                    continue
                recorded = entries.get(rel)
                if recorded is not None and recorded == hashlib.sha256(
                    current
                ).hexdigest():
                    dest.write_bytes(content_bytes)
                    updated[rel] = digest
                    note(f"✓ skill {name!r} re-rendered at {dest}")
                    continue
                if recorded is not None:
                    note(
                        f"✓ skill {name!r} at {dest} was modified after "
                        "the kit render, left in place"
                    )
                    continue
                if SKILL_MARKER.encode() in current:
                    note(
                        f"! skill {name!r} at {dest} looks like a kit "
                        "render but has no provenance entry in "
                        f"{DOCK_DIR_NAME}/{RENDERED_SKILLS_FILE}; left "
                        "in place. Recovery: keep it as a consumer file "
                        "(no action), or delete it and rerun wiki-dock "
                        "install to adopt the kit render"
                    )
                    continue
                note(
                    f"✓ skill {name!r} at {dest} is a foreign file, "
                    "left in place"
                )
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content_bytes)
            updated[rel] = digest
            note(f"✓ skill {name!r} rendered at {dest}")
    _write_render_manifest(manifest_path, updated)


def render_template(template: Path, values: dict[str, str]) -> str:
    """Single-pass {{TOKEN}} substitution: a token in the template with
    no value is a template bug and fails loud before any write; brace
    text inside a value renders literally and is never re-scanned."""
    content = template.read_text(encoding="utf-8")
    known = {"{{" + token + "}}" for token in values}
    unknown = sorted(set(SKILL_PLACEHOLDER_RE.findall(content)) - known)
    if unknown:
        raise ConfigError(
            f"{template} has unrendered placeholders: {unknown}"
        )
    return SKILL_PLACEHOLDER_RE.sub(
        lambda match: values[match.group(0)[2:-2]], content
    )


def prepare_orientation(
    wiki_name: str,
    companion: str,
    wiki_root: Path,
    kit_root: str,
    skill_dirs: tuple[str, ...],
) -> str:
    """Render the orientation text in preflight, before any dock write,
    so a template bug fails loud with nothing half-applied."""
    listing = "\n".join(f"- `{directory}/`" for directory in skill_dirs)
    if not listing:
        listing = (
            "- none rendered; reinstall with --skills-dir to add them"
        )
    return render_template(
        ORIENTATION_TEMPLATE,
        {
            "WIKI_NAME": wiki_name,
            "COMPANION": companion,
            "WIKI_ROOT": str(wiki_root),
            "KIT_ROOT": kit_root,
            "SKILL_DIRS": listing,
        },
    )


def write_orientation(dock_dir: Path, content: str) -> None:
    path = dock_dir / ORIENTATION_NAME
    if path.exists() and path.read_text(encoding="utf-8") == content:
        note("✓ orientation up to date")
        return
    dock_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    note(f"✓ orientation written to {path}")


def recorded_skill_dirs(dock_dir: Path) -> tuple[str, ...]:
    """The skill dirs the dock's provenance manifest records, for the
    complete command's orientation re-render (complete does not take
    --skills-dir; the manifest is the record of where install put them)."""
    entries, _healthy = _load_render_manifest(
        dock_dir / RENDERED_SKILLS_FILE
    )
    dirs = {str(Path(rel).parent.parent) for rel in entries}
    return tuple(sorted(dirs))


def dock_block_text(wiki_name: str) -> str:
    return (
        f"{DOCK_BLOCK_START}\n"
        f"This repo is docked to the **{wiki_name}** wiki (wiki-kit). "
        "Read `.wiki/orientation.md` first - it names the wiki root, "
        "the rendered project skills, and the commands a session needs.\n"
        f"{DOCK_BLOCK_END}"
    )


def _read_exact(path: Path) -> str:
    """The file's text with its line endings intact (universal-newline
    reading would fold CRLF into LF and break byte-exact preservation)."""
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _block_bounds(lines: list[str], path: Path) -> tuple[int, int] | None:
    """Locate the kit-owned region: exactly one start and one end marker,
    each on a line of its own and outside fenced code blocks (a fence may
    quote the markers as documentation). None means no block; anything
    else is malformed and fails loud."""
    starts: list[int] = []
    ends: list[int] = []
    fenced = False
    for index, line in enumerate(lines):
        bare = line.rstrip("\r\n")
        if bare.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        if bare == DOCK_BLOCK_START:
            starts.append(index)
        elif bare == DOCK_BLOCK_END:
            ends.append(index)
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0] > ends[0]:
        raise DockError(
            f"{path} carries a malformed wiki-kit dock block; fix or "
            "remove the markers by hand before reinstalling"
        )
    return starts[0], ends[0]


def check_marked_block(path: Path) -> None:
    """Preflight for update_marked_block: a malformed block fails the
    install before any write lands."""
    if path.exists():
        _block_bounds(_read_exact(path).splitlines(keepends=True), path)


def update_marked_block(path: Path, block: str, label: str) -> None:
    """Create or update a file carrying the kit's dock block. The region
    between the markers is kit-owned and replaced wholesale; everything
    outside the markers is preserved byte-exact (the file's own line
    endings included), and a file with no markers gains the block
    appended after a separating blank line - the one case that touches
    existing bytes is a last line with no newline, which gains one so
    the block can start on a line of its own."""
    if not path.exists():
        path.write_text(block + "\n", encoding="utf-8")
        note(f"✓ {label} created with the wiki-kit dock block")
        return
    text = _read_exact(path)
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    rendered = block.replace("\n", newline) + newline
    bounds = _block_bounds(lines, path)
    if bounds is not None:
        start, end = bounds
        new_text = "".join(lines[:start]) + rendered + "".join(lines[end + 1 :])
        if new_text == text:
            note(f"✓ {label} dock block up to date")
            return
        path.write_text(new_text, encoding="utf-8", newline="")
        note(f"✓ {label} dock block updated")
        return
    if text and not text.endswith("\n"):
        text += newline
    separator = newline if text else ""
    path.write_text(text + separator + rendered, encoding="utf-8", newline="")
    note(f"✓ {label} gained the wiki-kit dock block")


def claude_points_at_agents(repo: Path) -> bool:
    """A CLAUDE.md points at AGENTS.md when it imports it (claude-code's
    `@AGENTS.md` syntax) or carries the kit's shim line. A mention in
    prose is not a pointer: that file still gets the dock block."""
    path = repo / CLAUDE_FILE
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    return CLAUDE_IMPORT_LINE in text or CLAUDE_SHIM_LINE in text


def ensure_claude_shim(repo: Path, block: str) -> None:
    """claude-code reads CLAUDE.md, not AGENTS.md: an absent file gets
    the one-line shim (the boardkit convention); a file that does not
    point at AGENTS.md gets the dock block appended; one that does is
    left alone."""
    path = repo / CLAUDE_FILE
    if not path.exists():
        path.write_text(
            f"# CLAUDE.md\n\n{CLAUDE_SHIM_LINE}\n", encoding="utf-8"
        )
        note("✓ CLAUDE.md shim created")
        return
    if claude_points_at_agents(repo):
        note("✓ CLAUDE.md already points at AGENTS.md")
        return
    update_marked_block(path, block, CLAUDE_FILE)


def planned_shim_paths(repo: Path) -> tuple[str, ...]:
    """Which entry files install will write, computed in preflight so
    the posture's exclusion set covers them. A CLAUDE.md that already
    points at AGENTS.md is never written and never excluded."""
    paths = [AGENTS_FILE]
    if not claude_points_at_agents(repo):
        paths.append(CLAUDE_FILE)
    return tuple(paths)


def _resolve_posture(flag: str | None, companion: Companion) -> str:
    """The companion table is the posture's one home (docking spec): an
    explicit flag may agree with it or supply it, never contradict."""
    recorded = companion.posture
    if flag is not None and recorded is not None and flag != recorded:
        raise DockError(
            f"--posture {flag} contradicts the posture recorded for "
            f"companion {companion.name!r} in wiki.toml ({recorded!r}); "
            "the companion table is the posture's one home - change it "
            "there or omit the flag"
        )
    posture = flag or recorded
    if posture is None:
        raise DockError(
            f"no --posture given and companion {companion.name!r} "
            f"records none in wiki.toml; pass --posture "
            f"{'|'.join(POSTURES)} or set posture in the companion table"
        )
    return posture


def cmd_install(args: argparse.Namespace) -> int:
    wiki_root = _require_wiki_root(args.wiki)
    repo = args.repo.expanduser().resolve()
    hooks = git_hooks_dir(repo)  # raises ConfigError unless a git repo
    config = load_config(wiki_root)
    companion = config.companion(args.companion)
    posture = _resolve_posture(args.posture, companion)
    dock_dir = repo / DOCK_DIR_NAME
    wired = companion.docs_subpath is not None
    skill_targets = resolve_skill_targets(repo, args.skills_dir or [])
    kit_root = config.tool("kit", str(KIT_ROOT))
    skill_rel_dirs = tuple(
        str(target.relative_to(repo)) for target in skill_targets
    )
    shim_paths = planned_shim_paths(repo)

    # Every conflict check runs before any write: a failed install
    # leaves nothing half-applied. Skill preflight renders every template
    # completely and proves every write path stays inside the repo.
    _check_manifest_slot(dock_dir, config.name, companion.name)
    for shim in shim_paths:
        check_marked_block(repo / shim)
    if wired:
        _check_hook_slot(hooks / "post-commit")
    skill_renders: dict[str, str] = {}
    if skill_targets:
        # {{KIT_ROOT}} renders from the deployment overlay's [tools].kit;
        # absent one, the kit performing this dock is the kit on record.
        skill_renders = prepare_skill_renders(
            config.contract.skills, kit_root
        )
        check_skill_paths(
            repo,
            config.contract.skills,
            skill_targets,
            dock_dir / RENDERED_SKILLS_FILE,
        )
    orientation = prepare_orientation(
        config.name, companion.name, wiki_root, kit_root, skill_rel_dirs
    )

    write_manifest(dock_dir, config.name, companion.name)
    write_overlay(dock_dir, wiki_root)
    write_orientation(dock_dir, orientation)
    apply_posture(
        repo,
        posture,
        wiring_written=wired,
        skill_dirs=skill_rel_dirs,
        shim_paths=shim_paths,
    )
    block = dock_block_text(config.name)
    update_marked_block(repo / AGENTS_FILE, block, AGENTS_FILE)
    ensure_claude_shim(repo, block)
    if wired:
        install_post_commit_hook(repo, wiki_root, companion.docs_subpath)
        render_handoff_plugin(repo, companion.docs_subpath)
    else:
        note(
            "- companion has no docs_subpath; post-commit hook and "
            "opencode plugin skipped"
        )
    if skill_targets:
        render_skills(skill_renders, skill_targets, repo, dock_dir)

    dock = load_dock(dock_dir)
    verify_dock_identity(dock, wiki_root)
    print(f"Docked {repo} to wiki {config.name!r} ({posture} posture).")
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    wiki_root = _require_wiki_root(args.wiki)
    repo = args.repo.expanduser().resolve()
    dock_dir = repo / DOCK_DIR_NAME
    if not (dock_dir / DOCK_MANIFEST_NAME).is_file():
        raise DockError(
            f"no dock manifest at {dock_dir / DOCK_MANIFEST_NAME}; "
            "complete only writes the overlay for an existing manifest - "
            "run install with an explicit --posture to create the dock"
        )
    dock = load_dock(dock_dir)
    verify_dock_identity(dock, wiki_root)
    config = load_config(wiki_root)
    # Render before any write, as install does: a template bug leaves
    # the overlay untouched.
    orientation = prepare_orientation(
        dock.wiki_name,
        dock.companion,
        wiki_root,
        config.tool("kit", str(KIT_ROOT)),
        recorded_skill_dirs(dock_dir),
    )
    write_overlay(dock_dir, wiki_root)
    write_orientation(dock_dir, orientation)
    print(f"Dock at {dock_dir} now resolves to {wiki_root}.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve()
    dock_dir = repo / DOCK_DIR_NAME
    if not (dock_dir / DOCK_MANIFEST_NAME).is_file():
        print(f"{repo}: no dock (no {DOCK_DIR_NAME}/{DOCK_MANIFEST_NAME})")
        return 0
    dock = load_dock(dock_dir)
    print(f"dock:      {dock_dir}")
    print(f"wiki:      {dock.wiki_name}")
    print(f"companion: {dock.companion}")
    if dock.wiki_path is None:
        print("overlay:   MISSING (incomplete dock)")
        print(f"complete:  {dock_complete_command(dock)}")
        return 1
    print(f"overlay:   {dock.wiki_path}")
    root = dock.wiki_path.resolve()
    if not (root / "wiki.toml").is_file():
        print(f"resolves:  BROKEN - {root} does not contain wiki.toml")
        return 1
    verify_dock_identity(dock, root)
    print(f"resolves:  {root} (identity chain verified)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser(
        "install", help="dock a consumer repo to a wiki"
    )
    install_parser.add_argument(
        "--wiki", type=Path, required=True, help="wiki repo root"
    )
    install_parser.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="consumer repo to dock (default: cwd)",
    )
    install_parser.add_argument("--companion", required=True)
    install_parser.add_argument(
        "--posture",
        choices=POSTURES,
        default=None,
        help="dock posture; defaults to the companion table's recorded "
        "posture and must not contradict it",
    )
    install_parser.add_argument(
        "--skills-dir",
        action="append",
        default=None,
        metavar="DIR",
        help="repo-relative directory the workflow skills render into; "
        "repeatable (e.g. --skills-dir .agents/skills --skills-dir "
        ".claude/skills). Project-scoped only: machine-global paths are "
        "refused",
    )
    install_parser.set_defaults(func=cmd_install)

    complete_parser = subparsers.add_parser(
        "complete",
        help="create or update the overlay for an existing manifest",
    )
    complete_parser.add_argument(
        "--wiki", type=Path, required=True, help="wiki repo root"
    )
    complete_parser.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="consumer repo carrying the manifest (default: cwd)",
    )
    complete_parser.set_defaults(func=cmd_complete)

    status_parser = subparsers.add_parser(
        "status", help="report what the resolver sees at a repo"
    )
    status_parser.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="repo to inspect (default: cwd)",
    )
    status_parser.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, DockError) as exc:
        print(f"wiki-dock: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
