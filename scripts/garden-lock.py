#!/usr/bin/env python3
"""Token-based garden lock. Cross-platform (macOS + Linux).

Acquisition is atomic (O_CREAT|O_EXCL, same pattern as EventWriteLock in
wiki_lock.py). Ownership is a random token printed at acquire time:
multi-step LLM skills run each command in a fresh shell, so PIDs — ours or
the parent shell's — are dead by the next command and useless as identity
or liveness. Staleness is therefore time-based: a lock older than the TTL
(default 45 minutes, override with GARDEN_LOCK_TTL_SECONDS) is considered
abandoned and may be cleared. A garden run that legitimately exceeds the
TTL can lose the lock to a new garden; the TTL is sized so that only a
crashed or forgotten run ever hits it.

The lock lives at <wiki root>/workstreams/.garden.lock. The root comes
from --wiki or the walk-up to wiki.toml (wiki_config.resolve_wiki_root),
never from this file's own location — Path(__file__) points into the kit
checkout, which owns no content. Importers pass the lock path explicitly.

Usage:
  garden-lock.py acquire [--wiki PATH]  # prints "locked token=<t>"; exit 1 if held
  garden-lock.py release <token> [--wiki PATH]  # release if the token matches
  garden-lock.py check [--wiki PATH]    # exit 0 if free, exit 1 if held
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import time
from pathlib import Path

import wiki_config

DEFAULT_TTL_SECONDS = 45 * 60
TTL_ENV_VAR = "GARDEN_LOCK_TTL_SECONDS"


def ttl_seconds() -> float:
    raw = os.environ.get(TTL_ENV_VAR)
    if raw is None:
        return DEFAULT_TTL_SECONDS
    return float(raw)


def read_lock(lock_path: Path) -> dict[str, str] | None:
    try:
        text = lock_path.read_text()
    except FileNotFoundError:
        return None
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def lock_age_seconds(lock_path: Path) -> float | None:
    try:
        return time.time() - lock_path.stat().st_mtime
    except FileNotFoundError:
        return None


def lock_is_stale(lock_path: Path) -> bool:
    age = lock_age_seconds(lock_path)
    return age is not None and age > ttl_seconds()


def describe_holder(lock_path: Path, fields: dict[str, str]) -> str:
    # Never include the token: printing it would let a competing garden
    # release a lock it does not own.
    age = lock_age_seconds(lock_path)
    age_text = f"{int(age)}s" if age is not None else "unknown age"
    return f"pid={fields.get('pid', 'unknown')} age={age_text}"


def acquire(lock_path: Path) -> bool:
    while True:
        token = secrets.token_hex(8)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            fields = read_lock(lock_path)
            if fields is None:
                continue  # lock vanished between open and read; retry
            if lock_is_stale(lock_path):
                print(f"clearing stale lock ({describe_holder(lock_path, fields)})")
                lock_path.unlink(missing_ok=True)
                continue  # O_EXCL decides the winner if two clear at once
            print(f"held ({describe_holder(lock_path, fields)})", file=sys.stderr)
            return False

        with os.fdopen(fd, "w") as lock_file:
            lock_file.write(f"token={token}\n")
            lock_file.write(f"pid={os.getpid()}\n")  # informational only
        print(f"locked token={token}")
        return True


def release(lock_path: Path, token: str | None) -> bool:
    fields = read_lock(lock_path)
    if fields is None:
        print("no lock to release")
        return True
    if token is not None and fields.get("token") == token:
        lock_path.unlink(missing_ok=True)
        print("released")
        return True
    if lock_is_stale(lock_path):
        lock_path.unlink(missing_ok=True)
        print(f"cleared stale lock ({describe_holder(lock_path, fields)})")
        return True
    if token is None:
        print(
            "lock is held and no token given — pass the token printed by acquire",
            file=sys.stderr,
        )
    else:
        print("token does not match the held lock, not releasing", file=sys.stderr)
    return False


def check(lock_path: Path) -> bool:
    fields = read_lock(lock_path)
    if fields is None:
        print("free")
        return True
    if lock_is_stale(lock_path):
        print(f"free (stale: {describe_holder(lock_path, fields)})")
        return True
    print(f"held ({describe_holder(lock_path, fields)})")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Token-based garden lock for multi-step skill runs",
    )
    parser.add_argument("command", choices=["acquire", "release", "check"])
    parser.add_argument(
        "token",
        nargs="?",
        default=None,
        help="ownership token printed by acquire (release only)",
    )
    parser.add_argument(
        "--wiki",
        type=Path,
        default=None,
        help="wiki repo root (default: walk up from cwd to wiki.toml)",
    )
    args = parser.parse_args(argv)
    try:
        root = wiki_config.resolve_wiki_root(args.wiki)
    except wiki_config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    lock_path = root / "workstreams" / ".garden.lock"
    if args.command == "acquire":
        return 0 if acquire(lock_path) else 1
    if args.command == "release":
        return 0 if release(lock_path, args.token) else 1
    return 0 if check(lock_path) else 1


if __name__ == "__main__":
    sys.exit(main())
