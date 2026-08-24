"""Installer behavior: blank-repo boot, idempotency, and the charter
decision-4 tweak (install around pre-existing content, never over it)."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = KIT_ROOT / "scripts" / "wiki_install.py"


def run_install(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(INSTALLER), "--wiki", str(target), "--no-scheduler"],
        capture_output=True,
        text=True,
    )


def git(target: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(target), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_blank_repo_boot(tmp_path: Path) -> None:
    target = tmp_path / "blank-wiki"
    result = run_install(target)
    assert result.returncode == 0, result.stderr + result.stdout

    # Config pair: committed config seeded, machine overlay gitignored.
    config = tomllib.load((target / "wiki.toml").open("rb"))
    assert config["wiki"]["name"] == "blank-wiki"
    assert config["contract"]["protected"]
    gitignore = (target / ".gitignore").read_text()
    assert "wiki.local.toml" in gitignore
    assert "CLAUDE.local.md" in gitignore

    # The boot floor (charter decision 4): initial commit, projections,
    # orientation skeleton with an empty-state Quickstart.
    head = git(target, "rev-parse", "--verify", "HEAD").strip()
    assert head
    tracked = git(target, "ls-files").splitlines()
    assert "wiki/log.md" in tracked
    assert "wiki/pending/index.json" in tracked
    assert "wiki/pending/latest.md" in tracked
    orientation = (target / "CLAUDE.local.md").read_text()
    assert "## Quickstart" in orientation
    assert "newly initialized" in orientation

    # The kit stamp: committed wiki.toml records contract version and
    # kit commit; the machine-local overlay records the kit checkout.
    assert config["kit"]["contract_version"] == 1
    assert config["kit"]["commit"] == git(KIT_ROOT, "rev-parse", "HEAD").strip()
    overlay = tomllib.load((target / "wiki.local.toml").open("rb"))
    assert overlay["tools"]["kit"] == str(KIT_ROOT)

    # The mechanical layer the old installer never wired (recon 03): a
    # generated wrapper that bakes no machine paths - it resolves the
    # kit from the deployment's overlay at run time.
    hook = target / ".git" / "hooks" / "pre-commit"
    hook_text = hook.read_text()
    assert "# wiki-kit pre-commit wrapper" in hook_text
    assert "wiki.local.toml" in hook_text
    assert "scripts/pre-commit" in hook_text
    assert sys.executable not in hook_text
    assert hook.stat().st_mode & 0o111, "hook must be executable"

    # Deny rules derive from the deployment's own [contract].
    settings = json.loads((target / ".claude" / "settings.json").read_text())
    deny = settings["permissions"]["deny"]
    for rel in config["contract"]["protected"]:
        spec = rel if rel == "CLAUDE.local.md" else f"/{rel}"
        for tool in ("Write", "Edit", "NotebookEdit"):
            assert f"{tool}({spec})" in deny, f"missing {tool}({spec})"


def test_reinstall_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "blank-wiki"
    assert run_install(target).returncode == 0
    first_commit = git(target, "rev-parse", "HEAD").strip()
    before = {
        path: path.read_bytes()
        for path in target.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }

    second = run_install(target)
    assert second.returncode == 0, second.stderr + second.stdout
    assert git(target, "rev-parse", "HEAD").strip() == first_commit
    assert git(target, "status", "--short").strip() == ""
    after = {
        path: path.read_bytes()
        for path in target.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    assert before == after


def test_install_around_existing_content(tmp_path: Path) -> None:
    """Charter decision-4 tweak: a repo already carrying docs dirs or an
    Obsidian vault is a legal starting place; init installs around the
    content non-destructively."""
    target = tmp_path / "vault-wiki"
    (target / "docs").mkdir(parents=True)
    (target / "docs" / "notes.md").write_text("# My Notes\n")
    (target / ".obsidian").mkdir()
    (target / ".obsidian" / "app.json").write_text("{}\n")
    (target / "README.md").write_text("existing readme\n")
    (target / ".gitignore").write_text("*.tmp\n")

    result = run_install(target)
    assert result.returncode == 0, result.stderr + result.stdout

    # Pre-existing content is byte-untouched.
    assert (target / "docs" / "notes.md").read_text() == "# My Notes\n"
    assert (target / ".obsidian" / "app.json").read_text() == "{}\n"
    assert (target / "README.md").read_text() == "existing readme\n"
    # Existing .gitignore content is preserved, kit lines appended.
    gitignore = (target / ".gitignore").read_text()
    assert gitignore.startswith("*.tmp\n")
    assert "wiki.local.toml" in gitignore

    # The initial commit covers only installer-written paths: the user's
    # content stays uncommitted, theirs to commit.
    tracked = git(target, "ls-files").splitlines()
    assert "docs/notes.md" not in tracked
    assert "README.md" not in tracked
    assert "wiki/log.md" in tracked

    # Installed machinery still works here.
    assert (target / "wiki.toml").exists()
    assert (target / "CLAUDE.local.md").exists()


def test_install_preserves_preexisting_log_file(tmp_path: Path) -> None:
    """Gate A blocking finding: a file already at wiki/log.md is the
    owner's content; install must neither overwrite nor commit it."""
    target = tmp_path / "vault-wiki"
    (target / "wiki").mkdir(parents=True)
    (target / "wiki" / "log.md").write_text("# My precious note\n")

    result = run_install(target)
    assert result.returncode == 0, result.stderr + result.stdout

    assert (target / "wiki" / "log.md").read_text() == "# My precious note\n"
    assert "left in place" in result.stdout
    tracked = git(target, "ls-files").splitlines()
    assert "wiki/log.md" not in tracked
    # The rest of the boot still lands.
    assert "wiki/pending/index.json" in tracked
    assert (target / "CLAUDE.local.md").exists()


def test_commit_with_nonempty_pending_passes_the_hook(tmp_path: Path) -> None:
    """Gate A blocking finding: the pending check must agree with the
    CLI's own build-pending output across process boundaries (the
    repo-relative vs absolute event-path asymmetry)."""
    target = tmp_path / "blank-wiki"
    assert run_install(target).returncode == 0
    sha = git(target, "rev-parse", "--short", "HEAD").strip()
    subprocess.run(
        [
            sys.executable,
            str(KIT_ROOT / "scripts" / "wiki-event.py"),
            "new-handoff",
            "--wiki",
            str(target),
            "--tool",
            "manual",
            "--summary",
            "pending-commit regression",
            "--repo-name",
            "blank-wiki",
            "--repo-branch",
            "main",
            "--repo-sha",
            sha,
            "--workstream",
            "pending-check:candidate_new",
            "--what-was-done",
            "wrote an event",
            "--next",
            "garden it later",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(KIT_ROOT / "scripts" / "wiki-render.py"),
            "log",
            "--wiki",
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    git(target, "add", "-A")
    commit = subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "event with pending non-empty",
        ],
        capture_output=True,
        text=True,
    )
    assert commit.returncode == 0, commit.stderr


def test_hook_rejects_non_json_event_addition(tmp_path: Path) -> None:
    """Gate A minor finding: non-JSON files under the store (other than
    the installer's .gitkeep) are an unvalidatable smuggle."""
    target = tmp_path / "blank-wiki"
    assert run_install(target).returncode == 0
    stray = target / "wiki" / "events" / "note.txt"
    stray.write_text("not an event\n")
    git(target, "add", "wiki/events/note.txt")
    commit = subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "smuggle",
        ],
        capture_output=True,
        text=True,
    )
    assert commit.returncode != 0
    assert "only .json event files" in commit.stderr


def _commit(target: Path, message: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            message,
        ],
        capture_output=True,
        text=True,
    )


def test_install_preserves_dangling_log_symlink(tmp_path: Path) -> None:
    """Gate A verification finding: exists() is false for a dangling
    symlink, which is still owner content at the projection path."""
    target = tmp_path / "vault-wiki"
    (target / "wiki").mkdir(parents=True)
    (target / "wiki" / "log.md").symlink_to(target / "nowhere.md")

    result = run_install(target)
    assert result.returncode == 0, result.stderr + result.stdout
    assert (target / "wiki" / "log.md").is_symlink()
    assert "symlink" in result.stdout


def test_hook_rejects_symlink_log_projection(tmp_path: Path) -> None:
    target = tmp_path / "blank-wiki"
    assert run_install(target).returncode == 0
    log = target / "wiki" / "log.md"
    log.unlink()
    log.symlink_to(target / "nowhere.md")
    git(target, "add", "wiki/log.md")
    commit = _commit(target, "smuggle a symlink projection")
    assert commit.returncode != 0
    assert "regular file" in commit.stderr


def test_hook_rejects_symlink_event_addition(tmp_path: Path) -> None:
    target = tmp_path / "blank-wiki"
    assert run_install(target).returncode == 0
    events = target / "wiki" / "events"
    (events / "2099").mkdir()
    (events / "2099" / "evil.json").symlink_to("/etc/hostname")
    git(target, "add", "wiki/events/2099/evil.json")
    commit = _commit(target, "smuggle a symlink event")
    assert commit.returncode != 0
    assert "symlink" in commit.stderr


def test_hook_rejects_nested_empty_gitkeep(tmp_path: Path) -> None:
    """The keeper exemption is exactly the top-level path: an empty
    .gitkeep anywhere deeper is rejected on position alone."""
    target = tmp_path / "blank-wiki"
    assert run_install(target).returncode == 0
    events = target / "wiki" / "events"
    (events / "2099").mkdir()
    (events / "2099" / ".gitkeep").write_text("")
    git(target, "add", "wiki/events/2099/.gitkeep")
    commit = _commit(target, "nested empty gitkeep")
    assert commit.returncode != 0
    assert "only .json event files" in commit.stderr


def test_hook_rejects_nonempty_top_level_gitkeep(tmp_path: Path) -> None:
    """The keeper exemption requires empty content: ADDING the exact path
    with smuggled bytes is rejected on content alone. (Modifying the
    installed keeper is separately blocked by event immutability, so the
    untracked precondition is constructed with a --no-verify commit.)"""
    target = tmp_path / "blank-wiki"
    assert run_install(target).returncode == 0
    keeper = target / "wiki" / "events" / ".gitkeep"
    git(target, "rm", "-q", "--cached", "wiki/events/.gitkeep")
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--no-verify",
            "-m",
            "untrack keeper (test precondition)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    keeper.write_text("smuggled content\n")
    git(target, "add", "wiki/events/.gitkeep")
    commit = _commit(target, "nonempty top-level gitkeep")
    assert commit.returncode != 0
    assert "only .json event files" in commit.stderr


def test_install_raises_on_operational_renderer_failure(tmp_path: Path) -> None:
    """A renderer failure that is not a projection mismatch fails the
    install loudly instead of being reported as drift."""
    target = tmp_path / "blank-wiki"
    assert run_install(target).returncode == 0
    event_dir = target / "wiki" / "events" / "2099" / "01"
    event_dir.mkdir(parents=True)
    (event_dir / "corrupt.json").write_text("{not json")

    result = run_install(target)
    assert result.returncode == 1
    assert "failed to run" in result.stderr + result.stdout


def test_explicit_path_build_agrees_with_checker(tmp_path: Path) -> None:
    """Gate A verification finding: a fully-explicit build-pending at the
    conventional layout must pin the same store root the checker derives,
    so both sides store repo-relative paths."""
    root = tmp_path / "bare"
    events = root / "wiki" / "events"
    events.mkdir(parents=True)
    (root / "wiki" / "sources").mkdir()
    subprocess.run(
        [
            sys.executable,
            str(KIT_ROOT / "scripts" / "wiki-event.py"),
            "new-handoff",
            "--events-dir",
            str(events),
            "--tool",
            "manual",
            "--summary",
            "explicit-path regression",
            "--repo-name",
            "bare",
            "--repo-branch",
            "main",
            "--repo-sha",
            "abc1234",
            "--workstream",
            "explicit-check:candidate_new",
            "--what-was-done",
            "wrote an event",
            "--next",
            "nothing",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    index = json.loads((root / "wiki" / "pending" / "index.json").read_text())
    stored_paths = [event["event_path"] for event in index["events"]]
    assert stored_paths and all(
        path.startswith("wiki/events/") for path in stored_paths
    ), stored_paths

    sys.path.insert(0, str(KIT_ROOT / "scripts"))
    try:
        import wiki_event

        mismatches = wiki_event.pending_mismatch(
            events_dir=events,
            sources_dir=root / "wiki" / "sources",
            index_path=root / "wiki" / "pending" / "index.json",
            latest_path=root / "wiki" / "pending" / "latest.md",
        )
    finally:
        sys.path.pop(0)
    assert mismatches == []


def test_smoke_image_names_are_repo_derived() -> None:
    """Gate A ledger-audit drop (knob 16): no hardcoded image name; the
    prefix derives from the repo directory name."""
    text = (KIT_ROOT / "scripts" / "install-smoke" / "run.sh").read_text()
    assert 'KIT_NAME="$(basename "$KIT_DIR")"' in text
    assert "${IMAGE_NAME:-$KIT_NAME-install-smoke" in text
    assert "${CONTAINER_NAME:-$KIT_NAME-install-smoke" in text
    assert "wiki-kit-install-smoke" not in text


PREEXISTING_STALE_STAMP = """\
[wiki]
name = "acme-notes"

[contract]
protected = ["wiki/log.md"]
external_allow = []
skills = []
global_skills = []

[kit]
contract_version = 0
commit = "stale"

[schedule]
night = "04:00"
"""


def test_stamp_replaces_a_stale_section_in_place(tmp_path: Path) -> None:
    target = tmp_path / "wiki"
    target.mkdir()
    (target / "wiki.toml").write_text(PREEXISTING_STALE_STAMP)

    result = run_install(target)

    assert result.returncode == 0, result.stderr + result.stdout
    text = (target / "wiki.toml").read_text()
    assert text.count("[kit]") == 1
    assert "contract_version = 0" not in text
    assert 'commit = "stale"' not in text
    head = git(KIT_ROOT, "rev-parse", "HEAD").strip()
    assert f'commit = "{head}"' in text
    # The section after the replaced one survives untouched.
    assert 'night = "04:00"' in text


def test_overlay_kit_update_preserves_other_tools(tmp_path: Path) -> None:
    target = tmp_path / "wiki"
    target.mkdir()
    (target / "wiki.local.toml").write_text(
        '[tools]\nuv = "/opt/uv"\nkit = "/old/kit"\n'
    )

    result = run_install(target)

    assert result.returncode == 0, result.stderr + result.stdout
    overlay = (target / "wiki.local.toml").read_text()
    assert 'uv = "/opt/uv"' in overlay
    assert overlay.count("kit = ") == 1
    assert f'kit = "{KIT_ROOT}"' in overlay
    assert "/old/kit" not in overlay


def test_overlay_unrewritable_kit_line_fails_directed(tmp_path: Path) -> None:
    target = tmp_path / "wiki"
    target.mkdir()
    (target / "wiki.local.toml").write_text("[tools]\nkit = '/single/quoted'\n")

    result = run_install(target)

    assert result.returncode == 1
    assert "cannot safely rewrite" in result.stderr


def test_overlay_tools_header_with_comment_is_rewritten_not_duplicated(
    tmp_path: Path,
) -> None:
    target = tmp_path / "wiki"
    target.mkdir()
    (target / "wiki.local.toml").write_text(
        '[tools] # binaries\nuv = "/opt/uv"\n'
    )

    result = run_install(target)

    assert result.returncode == 0, result.stderr + result.stdout
    overlay = (target / "wiki.local.toml").read_text()
    assert overlay.count("[tools]") == 1
    assert 'uv = "/opt/uv"' in overlay
    assert f'kit = "{KIT_ROOT}"' in overlay


def test_overlay_stray_tools_header_fails_directed(tmp_path: Path) -> None:
    target = tmp_path / "wiki"
    target.mkdir()
    (target / "wiki.local.toml").write_text("[ tools ]\n")

    result = run_install(target)

    assert result.returncode == 1
    assert "cannot safely edit" in result.stderr


def test_install_fails_loud_when_main_checkout_unfindable(
    tmp_path: Path,
) -> None:
    # A separate-git-dir repo reports its git dir as the first worktree;
    # git cannot locate the main checkout there, so the overlay has no
    # honest home and the install must fail rather than strand the hook.
    target = tmp_path / "wiki"
    (tmp_path / "meta").mkdir()
    subprocess.run(
        [
            "git",
            "init",
            "--separate-git-dir",
            str(tmp_path / "meta" / "wiki.git"),
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    result = run_install(target)

    assert result.returncode == 1
    assert "cannot locate the main checkout" in result.stderr


sys.path.insert(0, str(KIT_ROOT / "scripts"))
import wiki_install  # noqa: E402


def test_wrapper_error_lines_render_as_single_shell_lines() -> None:
    wrapper = wiki_install.hook_wrapper_text()
    for line in wrapper.splitlines():
        assert not line.startswith('"(tried'), line
    assert (
        'echo "pre-commit: no jsonschema-capable python found '
        "(tried" in wrapper
    )


def test_install_from_a_worktree_writes_the_overlay_at_main(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main-wiki"
    assert run_install(main).returncode == 0
    worktree = tmp_path / "wt"
    git(main, "worktree", "add", str(worktree))

    result = run_install(worktree)

    assert result.returncode == 0, result.stderr + result.stdout
    overlay = tomllib.load((main / "wiki.local.toml").open("rb"))
    assert overlay["tools"]["kit"] == str(KIT_ROOT)
    assert not (worktree / "wiki.local.toml").exists()


def test_existing_repo_with_history_gets_no_new_commit(tmp_path: Path) -> None:
    target = tmp_path / "history-wiki"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    (target / "seed.md").write_text("seed\n")
    subprocess.run(["git", "add", "seed.md"], cwd=target, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
        cwd=target,
        check=True,
    )
    head_before = git(target, "rev-parse", "HEAD").strip()

    result = run_install(target)
    assert result.returncode == 0, result.stderr + result.stdout
    # No installer commit on a repo that already has history; the kit
    # files sit in the working tree for the owner to commit.
    assert git(target, "rev-parse", "HEAD").strip() == head_before
    assert (target / "wiki.toml").exists()
