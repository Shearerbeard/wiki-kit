"""Writer lock for the wiki event store and its derived files.

Locking contract (Rank 1.6, planning/wiki-lock-unification-plan-2026-06-12.md):
one EventWriteLock serializes every WRITER that touches `wiki/events/**`,
`wiki/pending/**`, or `wiki/sources/**` manifests — new-handoff, garden
apply, build-pending, capture-sources, backfill. READERS are lock-free by
design: every writer lands files with atomic tmp+rename, so a reader sees
either the old or the new file, never a torn one. Cross-file views (an event
that the pending index doesn't reflect yet) are eventual-consistent and
rebuildable; that is acceptable for every current reader.

This lock is in-process (acquire and release within one Python invocation),
so pid-liveness works for staleness. The separate garden lock
(scripts/garden-lock.py, token + TTL) serializes multi-step LLM skill runs
whose commands each execute in a fresh shell — different ownership problem,
deliberately not merged.

Staleness is two-tier: a dead holder pid frees the lock immediately; a lock
file older than LOCK_STALE_TTL_SECONDS is stale even if its pid looks alive,
because pid recycling would otherwise make a crashed holder's lock immortal.
Legitimate holds run sub-second to seconds; the TTL is minutes.
"""

from __future__ import annotations

import os
import time
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path

LOCK_FILE_NAME = ".wiki-event.lock"
LOCK_POLL_SECONDS = 0.1
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
LOCK_TIMEOUT_ENV_VAR = "WIKI_EVENT_LOCK_TIMEOUT_SECONDS"
LOCK_STALE_TTL_SECONDS = 15 * 60


def utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def lock_timeout_seconds() -> float:
    """Default wait for a held lock; override with the env var (fail-loud)."""
    raw = os.environ.get(LOCK_TIMEOUT_ENV_VAR)
    if raw is None:
        return DEFAULT_LOCK_TIMEOUT_SECONDS
    return float(raw)


class EventWriteLock(AbstractContextManager["EventWriteLock"]):
    def __init__(self, events_dir: Path, timeout_seconds: float | None = None):
        self.events_dir = events_dir
        self.lock_path = events_dir / LOCK_FILE_NAME
        self.timeout_seconds = (
            lock_timeout_seconds() if timeout_seconds is None else timeout_seconds
        )
        self._locked = False

    def __enter__(self) -> EventWriteLock:
        self.events_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fd = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
            except FileExistsError:
                if self._clear_stale_lock():
                    continue
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"event write lock is held: {self.lock_path} "
                        f"({self._describe_holder()}) — gave up after "
                        f"{self.timeout_seconds:g}s; retry, or raise "
                        f"{LOCK_TIMEOUT_ENV_VAR} if the holder is doing "
                        "legitimate long work"
                    ) from None
                time.sleep(LOCK_POLL_SECONDS)
                continue

            with os.fdopen(fd, "w") as lock_file:
                lock_file.write(f"pid={os.getpid()}\n")
                lock_file.write(f"timestamp_utc={utc_timestamp()}\n")
            self._locked = True
            return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._locked:
            self.lock_path.unlink(missing_ok=True)
            self._locked = False

    def _clear_stale_lock(self) -> bool:
        try:
            text = self.lock_path.read_text()
        except FileNotFoundError:
            return True

        pid = parse_lock_pid(text)
        if pid is not None and not pid_alive(pid):
            self.lock_path.unlink(missing_ok=True)
            return True
        # Holder pid looks alive (possibly recycled) or the file is
        # unparseable: fail closed within the TTL, clear beyond it.
        # The staleness clock is the lock file's mtime, not its
        # timestamp_utc field — mtime exists even when the content is
        # unparseable, and the two agree at write time. timestamp_utc is
        # informational, for humans inspecting a stuck lock.
        age = self._lock_age_seconds()
        if age is not None and age > LOCK_STALE_TTL_SECONDS:
            self.lock_path.unlink(missing_ok=True)
            return True
        return False

    def _lock_age_seconds(self) -> float | None:
        try:
            return time.time() - self.lock_path.stat().st_mtime
        except FileNotFoundError:
            return None

    def _describe_holder(self) -> str:
        try:
            text = self.lock_path.read_text()
        except FileNotFoundError:
            return "holder gone"
        pid = parse_lock_pid(text)
        age = self._lock_age_seconds()
        age_text = f"{int(age)}s" if age is not None else "unknown"
        return f"holder pid={pid if pid is not None else 'unparseable'} age={age_text}"


def parse_lock_pid(text: str) -> int | None:
    for line in text.splitlines():
        if line.startswith("pid="):
            try:
                return int(line.removeprefix("pid="))
            except ValueError:
                return None
    return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
