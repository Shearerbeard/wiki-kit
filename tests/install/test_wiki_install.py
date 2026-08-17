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

    # The mechanical layer the old installer never wired (recon 03): a
    # generated wrapper pinning the installing interpreter, exec-ing the
    # kit's current script.
    hook = target / ".git" / "hooks" / "pre-commit"
    hook_text = hook.read_text()
    assert "# wiki-kit pre-commit wrapper" in hook_text
    assert str(KIT_ROOT / "scripts" / "pre-commit") in hook_text
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
